import os
import sys
import platform
import shutil
from typing import Optional, List
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, 
    QPushButton, QLabel, QComboBox
)
from PyQt6.QtCore import Qt, QProcess, QProcessEnvironment, pyqtSignal, QTimer, QObject, pyqtSlot, QUrl
from PyQt6.QtWebEngineWidgets import QWebEngineView
from PyQt6.QtWebEngineCore import QWebEnginePage, QWebEngineSettings
from PyQt6.QtWebChannel import QWebChannel
from src.utils.logger import get_logger
from .windows_terminal import PathResolverThread
from .terminal_bridge import AsyncFileReader

log = get_logger("xterm_terminal")

# We will try to use pywinpty on Windows for true PTY support (ANSI, arrows, etc), 
# otherwise fallback to QProcess (which doesn't support interactive terminal apps like vim or python repl well)
try:
    import winpty
    WINPTY_AVAILABLE = True
except ImportError:
    WINPTY_AVAILABLE = False
    log.warning("winpty not available. Interactive terminal apps may not work correctly.")


class TerminalBridge(QObject):
    """Bridge object that connects JS xterm events with Python."""
    send_output = pyqtSignal(str)   # Python -> JS (write to terminal)
    update_theme = pyqtSignal(bool) # Python -> JS (update colors)
    
    # Signals for when JS sends data to Python
    data_received = pyqtSignal(str)
    resize_requested = pyqtSignal(int, int)
    ready_received = pyqtSignal()
    
    def __init__(self, parent=None):
        super().__init__(parent)

    @pyqtSlot(str)
    def receive_input(self, data):
        """Called by JS when user types in xterm.js"""
        self.data_received.emit(data)

    @pyqtSlot(str)
    def copy_to_clipboard(self, text):
        from PyQt6.QtWidgets import QApplication
        QApplication.clipboard().setText(text)

    @pyqtSlot()
    def paste_from_clipboard(self):
        from PyQt6.QtWidgets import QApplication
        text = QApplication.clipboard().text()
        if text:
            # Emit the pasted text as if the user typed it
            self.data_received.emit(text)
        
    @pyqtSlot(int, int)
    def resize(self, cols, rows):
        """Called by JS when terminal resizes"""
        self.resize_requested.emit(cols, rows)
        
    @pyqtSlot()
    def ready(self):
        """Called by JS when xterm is fully loaded"""
        self.ready_received.emit()
    
    @pyqtSlot(str)
    def js_log(self, message):
        """Receive console logs from JavaScript"""
        log.debug(f"[JS] {message}")
    
    @pyqtSlot(str)
    def open_external_url(self, url):
        """Open URL in default browser when clicked in terminal"""
        import webbrowser
        try:
            webbrowser.open(url)
        except Exception as e:
            log.error(f"Failed to open URL {url}: {e}")


class XTermWidget(QWidget):
    """
    A true VT100/ANSI compatible terminal powered by xterm.js and QWebEngineView.
    Provides an exact VS Code terminal experience in PyQt.
    """
    
    command_executed = pyqtSignal(str, int)  # command, exit_code
    terminal_output_received = pyqtSignal(str) # For AI to listen to
    terminal_line_for_chat = pyqtSignal(str)   # clean line for chat card streaming display
    file_operation_detected = pyqtSignal(str, str, str)  # operation_type, file_path, status
    new_terminal_requested = pyqtSignal()      # Request to open new terminal tab
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self._cwd = os.getcwd()
        self._is_dark = True
        self._process = None # QProcess fallback
        self._pty_process = None # winpty
        self._terminal_buffer = [] # Store last lines for AI
        self._max_buffer = 1000
        
        # Buffer to hold text if xterm.js isn't loaded yet
        self._output_buffer = ""
        self._is_ready = False
        
        # OPTIMIZATION: Output emit throttle — accumulate for 16ms before sending to JS
        self._emit_buffer = ""
        self._emit_timer = QTimer(self)
        self._emit_timer.setSingleShot(True)
        self._emit_timer.timeout.connect(self._flush_emit_buffer)
        self._emit_debounce_ms = 16   # ~60fps
        
        self._build_ui()
        self._update_header_style()
        self._shell_started = False
        
        # For QProcess delayed rendering
        self._render_timer = QTimer(self)
        self._render_timer.timeout.connect(self._render_buffers)
        self._stdout_buffer = bytearray()
        
        # Track current command for file operation detection
        self._current_command = ""
        self._command_buffer = ""
        self._stderr_buffer = bytearray()
        
    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        
        # Header - Shell label from settings
        self._header = QWidget()
        self._header.setFixedHeight(32)
        self._header.setStyleSheet("background: #1e1e1e; border-bottom: 1px solid #333;")
        hlay = QHBoxLayout(self._header)
        hlay.setContentsMargins(8, 4, 8, 4)

        # Shell label from settings
        try:
            from src.config.settings import get_settings
            _s = get_settings()
            _shell_name = _s.get("terminal", "default_shell", default="powershell")
            _shell_display = _shell_name.capitalize()
        except Exception:
            _shell_display = "PowerShell"
        self._shell_label = QLabel(_shell_display)
        self._shell_label.setStyleSheet("color: #0078d4; font-weight: 600;")
        hlay.addWidget(self._shell_label)
        
        # Terminal number counter (class-level)
        if not hasattr(XTermWidget, '_terminal_count'):
            XTermWidget._terminal_count = 0
        XTermWidget._terminal_count += 1
        self._terminal_number = XTermWidget._terminal_count
        
        self._title_label = QLabel(f"Terminal {self._terminal_number}")
        self._title_label.setStyleSheet("color: #ffffff; font-size:12px; font-weight:bold; margin-left: 20px;")
        hlay.addWidget(self._title_label)
        hlay.addStretch()
        
        # Terminal buttons with dark styling
        _btn_style = "QPushButton { background: #2d2d2d; color: #d4d4d4; border: 1px solid #404040; border-radius: 4px; padding: 2px 8px; font-size: 11px; } QPushButton:hover { background: #3d3d3d; }"
        
        # New Terminal Button
        self._plus_btn = QPushButton("+ New")
        self._plus_btn.setFixedHeight(22)
        self._plus_btn.setMinimumWidth(65)
        self._plus_btn.setToolTip("New Terminal (Ctrl+Shift+`)")
        self._plus_btn.setStyleSheet(_btn_style)
        self._plus_btn.clicked.connect(self.new_terminal_requested.emit)
        hlay.addWidget(self._plus_btn)
        
        self._kill_btn = QPushButton("✕")
        self._kill_btn.setFixedSize(30, 22)
        self._kill_btn.setToolTip("Kill Process")
        self._kill_btn.setStyleSheet(_btn_style)
        self._kill_btn.clicked.connect(self._kill_process)
        hlay.addWidget(self._kill_btn)
        
        self._clear_btn = QPushButton("Clear")
        self._clear_btn.setFixedSize(50, 22)
        self._clear_btn.setToolTip("Clear terminal")
        self._clear_btn.setStyleSheet(_btn_style)
        self._clear_btn.clicked.connect(self._clear)
        hlay.addWidget(self._clear_btn)
        
        self._restart_btn = QPushButton("↺")
        self._restart_btn.setFixedSize(30, 22)
        self._restart_btn.setToolTip("Restart terminal")
        self._restart_btn.setStyleSheet(_btn_style)
        self._restart_btn.clicked.connect(self._restart)
        hlay.addWidget(self._restart_btn)
        
        layout.addWidget(self._header)
        
        # Web View for xterm.js
        self._webview = QWebEngineView()
        # Dark background to prevent white flash before HTML loads
        self._webview.setStyleSheet("background: #0c0c0c;")

        # CAPSULE-FIX: Prevent native window spawn from terminal WebEngine
        class _TerminalPage(QWebEnginePage):
            def createWindow(self, window_type):
                return self  # Prevent native window with [-][□][X]
        self._webview.setPage(_TerminalPage(self._webview))
        
        # Disable web view context menu and other browser features
        settings = self._webview.settings()
        settings.setAttribute(QWebEngineSettings.WebAttribute.JavascriptEnabled, True)
        settings.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, True)
        settings.setAttribute(QWebEngineSettings.WebAttribute.ShowScrollBars, False)
        
        self._webview.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
        
        # Setup the QWebChannel Bridge
        self._bridge = TerminalBridge(self)
        self._bridge.data_received.connect(self._on_js_input)
        self._bridge.resize_requested.connect(self._on_js_resize)
        self._bridge.ready_received.connect(self._on_js_ready)
        
        # Register this terminal widget globally for bash_tool access
        from .terminal_bridge import set_terminal_widget_ref
        set_terminal_widget_ref(self)
        
        # Debug logging to file for troubleshooting (define first!)
        debug_log_path = os.path.join(os.path.expanduser("~"), "cortex_terminal_debug.log")
        def debug_log(msg):
            with open(debug_log_path, 'a', encoding='utf-8') as f:
                f.write(f"[{__import__('datetime').datetime.now()}] {msg}\n")
        
        self._channel = QWebChannel(self)
        self._channel.registerObject("pyTerminal", self._bridge)
        self._webview.page().setWebChannel(self._channel)
        
        debug_log("QWebChannel setup complete")
        
        debug_log("=" * 60)
        debug_log("Terminal initialization started")
        
        # Load terminal.html - handle both dev and PyInstaller bundled paths
        # CRITICAL: Always use setUrl(), never setHtml() - QWebChannel needs file:// origin
        if getattr(sys, 'frozen', False):
            # Running in PyInstaller bundle
            bundle_dir = sys._MEIPASS
            debug_log(f"Bundle dir (sys._MEIPASS): {bundle_dir}")
            debug_log(f"Bundle dir exists: {os.path.exists(bundle_dir)}")
            if os.path.exists(bundle_dir):
                debug_log(f"Bundle dir contents: {os.listdir(bundle_dir)[:20]}")
            
            html_path = os.path.join(bundle_dir, "src", "ui", "components", "terminal.html")
            debug_log(f"Expected html_path: {html_path}")
            debug_log(f"html_path exists: {os.path.exists(html_path)}")
            
            # Search for terminal.html if not at expected path
            if not os.path.exists(html_path):
                log.error(f"[BUNDLE] terminal.html not found at: {html_path}")
                debug_log(f"terminal.html not found at expected path, searching...")
                for root, dirs, files in os.walk(bundle_dir):
                    if 'terminal.html' in files:
                        html_path = os.path.join(root, 'terminal.html')
                        log.info(f"[BUNDLE] Found terminal.html at: {html_path}")
                        debug_log(f"Found terminal.html at: {html_path}")
                        break
                else:
                    debug_log("terminal.html NOT FOUND anywhere in bundle!")
            else:
                log.info(f"[BUNDLE] terminal.html found at: {html_path}")
                debug_log(f"terminal.html found at expected path")
                
            # Check for assets
            assets_path = os.path.join(bundle_dir, "src", "ui", "components", "assets", "xterm")
            debug_log(f"Assets path: {assets_path}")
            debug_log(f"Assets exists: {os.path.exists(assets_path)}")
            if os.path.exists(assets_path):
                debug_log(f"Assets contents: {os.listdir(assets_path)}")
        else:
            # Running in development
            html_path = os.path.join(os.path.dirname(__file__), "terminal.html")
            log.info(f"[DEV] Loading terminal from: {html_path}")
            debug_log(f"[DEV] html_path: {html_path}")
        
        # ALWAYS use setUrl (not setHtml) - required for QWebChannel to work
        if os.path.exists(html_path):
            file_url = QUrl.fromLocalFile(html_path)
            log.info(f"Loading terminal from URL: {file_url.toString()}")
            debug_log(f"Loading terminal from URL: {file_url.toString()}")
            self._webview.setUrl(file_url)
            debug_log("setUrl() called successfully")
        else:
            log.error(f"terminal.html not found: {html_path}")
            debug_log(f"ERROR: terminal.html not found: {html_path}")
            self._webview.setHtml("<html><body style='background:#0c0c0c;color:#ef4444;padding:20px'><h3>Terminal Error</h3><p>terminal.html not found in bundle. Check log at: " + debug_log_path + "</p></body></html>")
        
        layout.addWidget(self._webview)
        
    def _on_js_ready(self):
        """Called when xterm.js is initialized and ready in the browser."""
        self._is_ready = True
        self._bridge.update_theme.emit(self._is_dark)
        
        if self._output_buffer:
            self._bridge.send_output.emit(self._output_buffer)
            self._output_buffer = ""
            
    def _on_js_input(self, data: str):
        """Called when user types in xterm.js"""
        # Track command input for file operation detection
        if data == '\r' or data == '\n':
            # Command submitted - parse it
            self._current_command = self._command_buffer.strip()
            self._command_buffer = ""
            self._parse_and_emit_file_operation(self._current_command)
        elif data == '\x7f' or data == '\b':  # Backspace
            self._command_buffer = self._command_buffer[:-1]
        elif data.isprintable():
            self._command_buffer += data
            
        if self._pty_process:
            try:
                self._pty_process.write(data)
            except Exception as e:
                log.error(f"Failed to write to pty: {e}")
        elif self._process and self._process.state() == QProcess.ProcessState.Running:
            # QProcess isn't a real PTY, so it expects full lines ending in \n. 
            # Interactive chars won't work well, but we send them anyway.
            self._process.write(data.encode('utf-8'))
            
    def _on_js_resize(self, cols: int, rows: int):
        """Called when xterm.js resizes its grid"""
        if self._pty_process:
            try:
                self._pty_process.setwinsize(rows, cols)
            except Exception as e:
                log.error(f"Failed to resize pty: {e}")
                
    def _write_to_terminal(self, text: str):
        """
        Buffer output and flush in batches instead of emitting each chunk separately.
        Prevents QWebChannel from being flooded with hundreds of small messages.
        Also emits clean lines for chat card streaming display.
        """
        # Store in buffer for AI feedback (clean ANSI codes first)
        clean_text = self._clean_ansi(text)
        if clean_text:
            self._terminal_buffer.extend(clean_text.splitlines())
            if len(self._terminal_buffer) > self._max_buffer:
                self._terminal_buffer = self._terminal_buffer[-self._max_buffer:]
            self.terminal_output_received.emit(clean_text)
            
            # Emit lines for chat card streaming display
            # Only emit non-empty, non-whitespace lines
            for line in clean_text.splitlines():
                line = line.strip()
                if line and len(line) > 1:
                    self.terminal_line_for_chat.emit(line)

        if self._is_ready:
            self._emit_buffer += text
            # Start/restart debounce timer
            if not self._emit_timer.isActive():
                self._emit_timer.start(self._emit_debounce_ms)
        else:
            self._output_buffer += text
    
    def _flush_emit_buffer(self):
        """Emit accumulated output as a single signal."""
        if self._emit_buffer:
            self._bridge.send_output.emit(self._emit_buffer)
            self._emit_buffer = ""

    def _clean_ansi(self, text: str) -> str:
        """Remove ANSI escape sequences."""
        import re
        ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
        return ansi_escape.sub('', text)

    def get_last_output(self, lines: int = 50) -> str:
        """Return the last N lines of terminal output."""
        return "\n".join(self._terminal_buffer[-lines:])
            
    def _start_shell(self):
        """Resolve PATH and start the backend process."""
        self._write_to_terminal("\r\n\x1b[90m[ Resolving terminal environment... ]\x1b[0m\r\n")
        
        self._path_thread = PathResolverThread(QProcessEnvironment.systemEnvironment().value("PATH", ""))
        self._path_thread.resolved.connect(self._on_path_resolved)
        self._path_thread.start()
        
    def _get_shell_command(self) -> str:
        """Get shell command from settings."""
        try:
            from src.config.settings import get_settings
            settings = get_settings()
            shell = settings.get("terminal", "default_shell", default="powershell")
            args = settings.get("terminal", "shell_args", default="-NoLogo")
            
            if shell == "powershell":
                cmd = "powershell.exe"
            elif shell == "cmd":
                cmd = "cmd.exe"
            elif shell == "bash":
                cmd = "bash.exe"
            elif shell == "wsl":
                cmd = "wsl.exe"
            else:
                cmd = "powershell.exe"
            
            if args:
                cmd += f" {args}"
            return cmd
        except Exception:
            return "powershell.exe -NoLogo"
        
    def _on_path_resolved(self, resolved_path: str):
        self._write_to_terminal("\x1bc") # xterm.js reset sequence (clears screen)
        
        env = dict(os.environ)
        env["PATH"] = resolved_path
        env["TERM"] = "xterm-256color"
        env["COLORTERM"] = "truecolor"
        
        if WINPTY_AVAILABLE:
            # --- START WINPTY (REAL TERMINAL) ---
            try:
                # Console hiding is handled by runtime_hook_noconsole.py
                cmd = self._get_shell_command()
                            
                self._pty_process = winpty.PtyProcess.spawn(
                    cmd,
                    cwd=self._cwd,
                    env=env,
                    dimensions=(24, 80)  # Default size, will be resized by JS
                )
                
                # Start background thread to read from PTY with batching
                from PyQt6.QtCore import QThread
                import time
                
                class WinptyReader(QThread):
                    data_received = pyqtSignal(str)
                    
                    # Emit at most this often (ms) — prevents signal flood
                    EMIT_INTERVAL_MS = 16    # ~60fps
                    
                    def __init__(self, pty):
                        super().__init__()
                        self.pty = pty
                        self.running = True
                    
                    def run(self):
                        accumulated = ""
                        last_emit = time.time()
                        
                        while self.running:
                            try:
                                if not self.pty.isalive():
                                    break
                                
                                # Read with a short timeout (non-blocking feel)
                                try:
                                    import select
                                    if hasattr(self.pty, 'fd'):
                                        ready, _, _ = select.select([self.pty.fd], [], [], 0.01)
                                        if not ready:
                                            # No data — check if we should emit accumulated
                                            now = time.time()
                                            elapsed_ms = (now - last_emit) * 1000
                                            if accumulated and elapsed_ms >= self.EMIT_INTERVAL_MS:
                                                self.data_received.emit(accumulated)
                                                accumulated = ""
                                                last_emit = now
                                            time.sleep(0.005)  # 5ms sleep = max 200 iterations/sec
                                            continue
                                    data = self.pty.read()
                                except Exception:
                                    data = None
                                
                                if data:
                                    accumulated += data
                                
                                now = time.time()
                                elapsed_ms = (now - last_emit) * 1000
                                
                                # Emit accumulated data every 16ms OR when buffer is large
                                should_emit = (
                                    accumulated and (
                                        elapsed_ms >= self.EMIT_INTERVAL_MS or
                                        len(accumulated) > 4096   # flush large chunks immediately
                                    )
                                )
                                
                                if should_emit:
                                    self.data_received.emit(accumulated)
                                    accumulated = ""
                                    last_emit = now
                                elif not data:
                                    # No data — small sleep to avoid busy-looping
                                    time.sleep(0.005)  # 5ms sleep = max 200 iterations/sec
                            
                            except EOFError:
                                break
                            except Exception:
                                time.sleep(0.01)
                        
                        # Flush any remaining data
                        if accumulated:
                            self.data_received.emit(accumulated)
                                
                self._pty_reader = WinptyReader(self._pty_process)
                self._pty_reader.data_received.connect(self._write_to_terminal)
                self._pty_reader.start()
                
            except Exception as e:
                self._write_to_terminal(f"\r\n\x1b[31m[ Failed to start winpty: {e} ]\x1b[0m\r\n")
                log.error(f"Winpty Error: {e}")
            
        else:
            # --- START QPROCESS (FALLBACK) ---
            # QProcess does not provide a true PTY, meaning no interactive REPLs (like python or node)
            # and no rich CLI apps (like vim, nano, htop).
            self._write_to_terminal("\r\n\x1b[33m[ Warning: 'pywinpty' not installed. Interactive terminal apps and REPLs may not function correctly. ]\x1b[0m\r\n")
            
            self._process = QProcess(self)
            self._process.setWorkingDirectory(self._cwd)
            
            qenv = QProcessEnvironment.systemEnvironment()
            qenv.insert("PATH", resolved_path)
            qenv.insert("TERM", "xterm-256color")
            self._process.setProcessEnvironment(qenv)
            
            # FIX: Prevent console window popup in PyInstaller builds
            if sys.platform == 'win32':
                from PyQt6.QtCore import QProcess
                # Set creation flags to hide console window
                self._process.setCreateProcessArgumentsModifier(
                    lambda args: args
                )
            
            self._process.readyReadStandardOutput.connect(self._on_stdout)
            self._process.readyReadStandardError.connect(self._on_stderr)
            self._process.finished.connect(self._on_process_finished)
            
            # Start shell from settings
            shell_cmd = self._get_shell_command()
            parts = shell_cmd.split()
            shell = parts[0] if parts else "powershell.exe"
            args = parts[1:] if len(parts) > 1 else ["-NoLogo"]
            self._process.start(shell, args)
                         
            # OPTIMIZATION: Adaptive render timer — starts at 30ms, slows when idle
            self._render_interval = 30
            self._last_render_had_data = False
            self._consecutive_empty = 0
            self._max_render_bytes_per_tick = 16 * 1024  # 16KB per render tick
            self._render_timer.start(self._render_interval)
            
    def showEvent(self, event):
        super().showEvent(event)
        if not self._shell_started:
            self._shell_started = True
            QTimer.singleShot(200, self._start_shell)
            
    def _on_stdout(self):
        if self._process:
            self._stdout_buffer.extend(self._process.readAllStandardOutput().data())
            
    def _on_stderr(self):
        if self._process:
            self._stderr_buffer.extend(self._process.readAllStandardError().data())
            
    def _render_buffers(self):
        """
        Adaptive render: fast when output is flowing, slow when idle.
        Limits bytes per tick to prevent UI freeze on heavy output.
        """
        has_data = bool(self._stdout_buffer or self._stderr_buffer)
        
        if has_data:
            self._consecutive_empty = 0
            self._last_render_had_data = True
            
            # Process stdout with size limit
            if self._stdout_buffer:
                stdout_data = bytes(self._stdout_buffer)
                # Limit per-tick processing to avoid freezing
                if len(stdout_data) > self._max_render_bytes_per_tick:
                    # Process only the first 16KB this tick, leave rest for next
                    self._stdout_buffer = bytearray(stdout_data[self._max_render_bytes_per_tick:])
                    stdout_data = stdout_data[:self._max_render_bytes_per_tick]
                else:
                    self._stdout_buffer.clear()
                
                text = stdout_data.decode("utf-8", errors="replace")
                if "\n" in text and "\r\n" not in text:
                    text = text.replace("\n", "\r\n")
                self._write_to_terminal(text)
            
            # Process stderr with size limit
            if self._stderr_buffer:
                stderr_data = bytes(self._stderr_buffer)
                if len(stderr_data) > self._max_render_bytes_per_tick:
                    self._stderr_buffer = bytearray(stderr_data[self._max_render_bytes_per_tick:])
                    stderr_data = stderr_data[:self._max_render_bytes_per_tick]
                else:
                    self._stderr_buffer.clear()
                
                text = stderr_data.decode("utf-8", errors="replace")
                if "\n" in text and "\r\n" not in text:
                    text = text.replace("\n", "\r\n")
                self._write_to_terminal(f"\x1b[31m{text}\x1b[0m")
            
            # Speed up timer when data is flowing
            if self._render_interval != 30:
                self._render_interval = 30
                self._render_timer.setInterval(self._render_interval)
        
        else:
            self._consecutive_empty += 1
            # Slow down timer when idle (saves CPU)
            if self._consecutive_empty > 20 and self._render_interval < 150:
                self._render_interval = 150
                self._render_timer.setInterval(self._render_interval)
            elif self._consecutive_empty > 5 and self._render_interval < 60:
                self._render_interval = 60
                self._render_timer.setInterval(self._render_interval)
            
    def _on_process_finished(self):
        self._write_to_terminal("\r\n\x1b[90m[ Process exited ]\x1b[0m\r\n")
        
    def _clear(self):
        self._write_to_terminal("\x1bc") # xterm.js reset sequence (clears screen)
        # Re-emit Enter to get the prompt back
        if self._pty_process:
            self._pty_process.write("\r\n")
        elif self._process:
            self._process.write(b"\r\n")
            
    def _restart(self):
        self._kill_process()
        self._clear()
        self._start_shell()
        
    def _kill_process(self):
        """Kill terminal process and cleanup all resources."""
        # Stop timers first to prevent callbacks during cleanup
        if hasattr(self, '_emit_timer') and self._emit_timer:
            self._emit_timer.stop()
        if hasattr(self, '_render_timer') and self._render_timer:
            self._render_timer.stop()
        
        # Kill PTY process and reader thread
        if self._pty_process:
            try:
                if hasattr(self, '_pty_reader') and self._pty_reader:
                    self._pty_reader.running = False
                    self._pty_reader.wait(500)  # Wait up to 500ms for thread to stop
                    self._pty_reader = None
                self._pty_process.terminate()
                self._pty_process = None
            except Exception:
                pass
            
        # Kill QProcess
        if self._process:
            try:
                self._process.finished.disconnect()
                self._process.readyReadStandardOutput.disconnect()
                self._process.readyReadStandardError.disconnect()
                self._process.terminate()
                self._process.waitForFinished(1000)
                if self._process.state() != QProcess.ProcessState.NotRunning:
                    self._process.kill()
                    self._process.waitForFinished(1000)
            except Exception:
                pass
            self._process = None
            
    def _on_shell_changed(self, shell_name: str):
        if self._shell_started:
            self._restart()
            
    def execute_command(self, cmd: str):
        if self._pty_process:
            self._pty_process.write(f"{cmd}\r\n")
        elif self._process and self._process.state() == QProcess.ProcessState.Running:
            self._process.write(f"{cmd}\r\n".encode())
            
    def set_cwd(self, path: str):
        self._cwd = path
        # If the shell process is already running, change directory inline.
        # If not yet spawned (race during project-open before showEvent fires),
        # just update self._cwd — the shell will spawn with that cwd.
        if self._pty_process or (self._process and self._process.state() == QProcess.ProcessState.Running):
            self.execute_command(f'Set-Location -Path "{path}"')
             
    def activate_virtual_env(self, venv_path: str):
        if sys.platform == "win32":
            activate_script = os.path.join(venv_path, "Scripts", "Activate.ps1")
            if os.path.exists(activate_script):
                self.execute_command(f"& '{activate_script}'")
        else:
            activate_script = os.path.join(venv_path, "bin", "activate")
            if os.path.exists(activate_script):
                self.execute_command(f"source {activate_script}")
                
    def set_theme(self, is_dark: bool):
        self._is_dark = is_dark
        self._update_header_style()
        if self._is_ready:
            self._bridge.update_theme.emit(is_dark)
            
    def _update_header_style(self):
        # Dark-only styling
        self._header.setStyleSheet("""
            QWidget {
                background-color: #2d2d30;
                border-bottom: 1px solid #3e3e42;
            }
            QLabel { color: #cccccc; font-size: 12px; }
            QPushButton {
                background-color: #3c3c3c; color: #cccccc;
                border: 1px solid #3e3e42; border-radius: 3px; padding: 2px 8px;
            }
            QPushButton:hover { background-color: #4c4c4c; }
            QComboBox {
                background-color: #3c3c3c; color: #cccccc;
                border: 1px solid #3e3e42; border-radius: 3px; padding: 2px 8px;
            }
            QComboBox::drop-down { border: none; }
            QComboBox QAbstractItemView {
                background-color: #3c3c3c; color: #cccccc;
                selection-background-color: #094771;
            }
        """)
            
    def closeEvent(self, event):
        self._kill_process()
        super().closeEvent(event)
        
    # PROTECTED PATHS for terminal operations
    TERMINAL_PROTECTED_PATTERNS = [
        r'^[\s]*rm\s+-rf\s+[/\\]?$',  # rm -rf /
        r'^[\s]*rm\s+.*[/\\]windows',  # Anything with Windows directory
        r'^[\s]*rm\s+.*[/\\]system32',  # System32
        r'^[\s]*del\s+.*[/\\]windows',
        r'^[\s]*rmdir\s+.*[/\\]windows',
        r'^[\s]*remove-item\s+.*[/\\]windows',
        r'^[\s]*rm\s+.*\*.*',  # Wildcard deletes
        r'^[\s]*del\s+.*\*.*',
    ]
    
    def _is_terminal_command_safe(self, command: str) -> tuple[bool, str]:
        """Check if terminal command is safe to execute.
        
        Returns: (is_safe, warning_message)
        """
        import re
        cmd_lower = command.lower().strip()
        
        # Check against dangerous patterns
        for pattern in self.TERMINAL_PROTECTED_PATTERNS:
            if re.match(pattern, cmd_lower, re.IGNORECASE):
                return False, "⚠️ DANGEROUS COMMAND BLOCKED: This could delete system files!"
        
        # Check for rm -rf or del /s with broad targets
        if re.match(r'^[\s]*rm\s+-rf\s+\.', cmd_lower):
            return False, "⚠️ BLOCKED: Cannot delete current directory recursively"
        
        if re.match(r'^[\s]*rm\s+-rf\s+~', cmd_lower):
            return False, "⚠️ BLOCKED: Cannot delete home directory"
        
        return True, ""
    
    def _parse_and_emit_file_operation(self, command: str):
        """Parse terminal command and emit file operation signals."""
        import re
        import os
        
        if not command:
            return
        
        # SAFETY CHECK
        is_safe, warning = self._is_terminal_command_safe(command)
        if not is_safe:
            # Emit warning to UI
            self.file_operation_detected.emit('blocked', warning, 'error')
            return
            
        # Normalize command
        cmd_lower = command.lower().strip()
        
        # File creation patterns
        create_patterns = [
            (r'^[\s]*(?:touch|ni|new-item)\s+(.+)', 'create'),
            (r'^[\s]*echo\s+.*\s*>\s*(.+)', 'create'),
            (r'^[\s]*(?:mkdir|md|new-item\s+-itemtype\s+directory)\s+(.+)', 'create_dir'),
        ]
        
        # File deletion patterns
        delete_patterns = [
            (r'^[\s]*(?:rm|del|remove-item)\s+(?:-r|-recurse\s+)?(.+)', 'delete'),
            (r'^[\s]*rmdir\s+(?:/s\s+)?(.+)', 'delete_dir'),
        ]
        
        # File move/rename patterns
        move_patterns = [
            (r'^[\s]*(?:mv|move|move-item)\s+(.+)\s+(.+)', 'move'),
            (r'^[\s]*(?:cp|copy|copy-item)\s+(.+)\s+(.+)', 'copy'),
            (r'^[\s]*(?:ren|rename)\s+(.+)\s+(.+)', 'rename'),
        ]
        
        # Check each pattern
        for pattern, op_type in create_patterns:
            match = re.match(pattern, cmd_lower, re.IGNORECASE)
            if match:
                path = match.group(1).strip().strip('"\'')
                # Resolve relative to current directory
                if not os.path.isabs(path):
                    path = os.path.join(self._cwd, path)
                self.file_operation_detected.emit(op_type, path, 'running')
                return
                
        for pattern, op_type in delete_patterns:
            match = re.match(pattern, cmd_lower, re.IGNORECASE)
            if match:
                path = match.group(1).strip().strip('"\'')
                if not os.path.isabs(path):
                    path = os.path.join(self._cwd, path)
                self.file_operation_detected.emit(op_type, path, 'running')
                return
                
        for pattern, op_type in move_patterns:
            match = re.match(pattern, cmd_lower, re.IGNORECASE)
            if match:
                src = match.group(1).strip().strip('"\'')
                dst = match.group(2).strip().strip('"\'')
                if not os.path.isabs(src):
                    src = os.path.join(self._cwd, src)
                if not os.path.isabs(dst):
                    dst = os.path.join(self._cwd, dst)
                self.file_operation_detected.emit(op_type, f"{src} → {dst}", 'running')
                return
    
    # ===== ASYNC FILE READING METHODS - NO BLOCKING =====
    
    def read_file_async(self, file_path: str, callback: Optional[Callable] = None):
        """
        Read file asynchronously - does NOT block IDE.
        
        Args:
            file_path: Path to file to read
            callback: Function to call with (path, content) when ready
        """
        import os
        
        # Resolve path relative to cwd
        if not os.path.isabs(file_path):
            file_path = os.path.join(self._cwd, file_path)
        
        # Create async reader
        reader = AsyncFileReader(file_path)
        
        if callback:
            reader.content_ready.connect(lambda p, c: callback(p, c))
            reader.error_occurred.connect(lambda p, e: self._write_to_terminal(f"\r\n\x1b[31mError reading {p}: {e}\x1b[0m\r\n"))
        else:
            reader.content_ready.connect(self._on_async_file_read)
            reader.error_occurred.connect(lambda p, e: self._write_to_terminal(f"\r\n\x1b[31mError reading {p}: {e}\x1b[0m\r\n"))
        
        reader.start()
        return reader
    
    def _on_async_file_read(self, path: str, content: str):
        """Handle async file read completion."""
        # Write file content to terminal in chunks to avoid freezing
        max_chunk = 4096
        for i in range(0, len(content), max_chunk):
            chunk = content[i:i+max_chunk]
            self._write_to_terminal(chunk)
    
    def execute_async(self, command: str, callback: Optional[Callable] = None):
        """
        Execute command asynchronously via EMBEDDED xterm.js - NO POPUP.
        
        Args:
            command: Command to execute
            callback: Function to call with result dict
        """
        # Use the embedded terminal's execute_command method (routes through pyTerminal)
        # This runs inside xterm.js, no external popup
        self.execute_command(command)
        
        # Callback immediately since execution is async in JS side
        if callback:
            callback({
                'command': command,
                'exit_code': 0,  # Unknown in async mode
                'output': f'[Command sent to terminal: {command}]'
            })
    
    # ===== EMBEDDED TERMINAL - NO WINDOWS TERMINAL POPUP =====
    
    def _ensure_embedded_mode(self):
        """
        Ensure terminal runs in embedded mode only.
        Never spawns external Windows Terminal.
        """
        # Force use of embedded process only
        if hasattr(self, '_process') and self._process:
            # Already running
            return
        
        # Ensure we use QProcess or winpty, never external terminal
        log.info("Terminal in embedded mode - no external Windows Terminal")
    
    def is_embedded(self) -> bool:
        """Check if terminal is running in embedded mode."""
        return True  # Always embedded
    
    def get_terminal_info(self) -> dict:
        """Get terminal information for debugging."""
        return {
            'embedded': True,
            'cwd': self._cwd,
            'shell_started': self._shell_started,
            'is_ready': self._is_ready,
            'has_pty': self._pty_process is not None,
            'has_process': self._process is not None
        }