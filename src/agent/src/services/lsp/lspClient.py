"""
services/lsp/lspClient.py
Python conversion of services/lsp/LSPClient.ts (448 lines)

JSON-RPC LSP client wrapper.
Manages communication with an LSP server process via stdio using JSON-RPC protocol.
"""

import asyncio
import json
import os
from typing import Any, Callable, Dict, List, Optional, Tuple


# Type aliases
LspCapabilities = Dict[str, Any]
LspInitializeParams = Dict[str, Any]
LspInitializeResult = Dict[str, Any]


class LSPClient:
    """
    LSP client that communicates with server processes via JSON-RPC over stdio.
    
    Manages:
    - Process lifecycle (spawn, stop, crash recovery)
    - JSON-RPC message framing (Content-Length headers)
    - Request/response correlation
    - Notification/request handler registration
    - Error handling and state tracking
    """
    
    def __init__(
        self,
        server_name: str,
        on_crash: Optional[Callable[[Exception], None]] = None,
    ):
        """
        Create an LSP client wrapper.
        
        Args:
            server_name: Unique identifier for this server
            on_crash: Called when server exits unexpectedly (non-zero exit code)
        """
        self.server_name = server_name
        self.on_crash = on_crash
        
        # State variables
        self._process: Optional[asyncio.subprocess.Process] = None
        self._capabilities: Optional[LspCapabilities] = None
        self._is_initialized = False
        self._start_failed = False
        self._start_error: Optional[Exception] = None
        self._is_stopping = False  # Track intentional shutdown
        
        # Handler queues for lazy initialization
        self._pending_handlers: List[Tuple[str, Callable[[Any], None]]] = []
        self._pending_request_handlers: List[
            Tuple[str, Callable[[Any], Any]]
        ] = []
        
        # JSON-RPC state
        self._message_id = 0
        self._pending_requests: Dict[int, asyncio.Future] = {}
        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._read_task: Optional[asyncio.Task] = None
    
    @property
    def capabilities(self) -> Optional[LspCapabilities]:
        return self._capabilities
    
    @property
    def is_initialized(self) -> bool:
        return self._is_initialized
    
    def _check_start_failed(self) -> None:
        """Check if start failed and throw error."""
        if self._start_failed:
            raise self._start_error or Exception(
                f'LSP server {self.server_name} failed to start'
            )
    
    async def start(
        self,
        command: str,
        args: List[str],
        options: Optional[Dict[str, Any]] = None,
    ) -> None:
        """
        Start the LSP server process and establish JSON-RPC connection.
        
        Args:
            command: Command to execute
            args: Command arguments
            options: Optional env and cwd
            
        Raises:
            Exception: If process fails to start
        """
        try:
            # Prepare environment
            env = {**os.environ, **(options.get('env') if options else {})}
            cwd = options.get('cwd') if options else None
            
            # Spawn LSP server process
            import subprocess as _sp
            self._process = await asyncio.create_subprocess_exec(
                command,
                *args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                cwd=cwd,
                # Prevent visible console window on Windows
                creationflags=_sp.CREATE_NO_WINDOW if os.name == 'nt' else 0,
            )
            
            if not self._process.stdout or not self._process.stdin:
                raise Exception('LSP server process stdio not available')
            
            self._reader = self._process.stdout
            self._writer = self._process.stdin
            
            # Capture stderr for server diagnostics
            if self._process.stderr:
                asyncio.create_task(self._capture_stderr())
            
            # Handle process errors
            asyncio.create_task(self._monitor_process())
            
            # Start reading messages
            self._read_task = asyncio.create_task(self._read_messages())
            
            # Apply queued notification handlers
            for method, handler in self._pending_handlers:
                self._register_notification_handler(method, handler)
            self._pending_handlers.clear()
            
            # Apply queued request handlers
            for method, handler in self._pending_request_handlers:
                self._register_request_handler(method, handler)
            self._pending_request_handlers.clear()
            
            log_for_debugging(f'LSP client started for {self.server_name}')
            
        except Exception as error:
            log_error(
                Exception(
                    f'LSP server {self.server_name} failed to start: {error}'
                )
            )
            raise
    
    async def _capture_stderr(self) -> None:
        """Capture and log stderr output from LSP server."""
        
        if not self._process or not self._process.stderr:
            return
        
        try:
            while True:
                line = await self._process.stderr.readline()
                if not line:
                    break
                output = line.decode('utf-8', errors='replace').strip()
                if output:
                    log_for_debugging(f'[LSP SERVER {self.server_name}] {output}')
        except Exception:
            pass  # Process may have exited
    
    async def _monitor_process(self) -> None:
        """Monitor process for crashes and errors."""
        if not self._process:
            return
        
        try:
            await self._process.wait()
            
            code = self._process.returncode
            if code is not None and code != 0 and not self._is_stopping:
                self._is_initialized = False
                self._start_failed = False
                self._start_error = None
                
                crash_error = Exception(
                    f'LSP server {self.server_name} crashed with exit code {code}'
                )
                
                log_error(crash_error)
                
                if self.on_crash:
                    self.on_crash(crash_error)
                    
        except Exception as error:
            if not self._is_stopping:
                log_for_debugging(
                    f'LSP server {self.server_name} monitor error: {error}'
                )
    
    async def _read_messages(self) -> None:
        """Read JSON-RPC messages from server."""
        if not self._reader:
            return
        
        try:
            while True:
                # Read headers
                headers = {}
                while True:
                    line = await self._reader.readline()
                    if not line:
                        return  # Connection closed
                    
                    line_str = line.decode('utf-8', errors='replace').strip()
                    if not line_str:
                        break  # Empty line marks end of headers
                    
                    if ':' in line_str:
                        key, value = line_str.split(':', 1)
                        headers[key.strip().lower()] = value.strip()
                
                # Read content
                content_length = int(headers.get('content-length', 0))
                if content_length == 0:
                    continue
                
                content_bytes = await self._reader.readexactly(content_length)
                content_str = content_bytes.decode('utf-8', errors='replace')
                
                try:
                    message = json.loads(content_str)
                    await self._handle_message(message)
                except json.JSONDecodeError as error:
                    from services.utils.log import log_error
                    log_error(
                        Exception(f'Invalid JSON-RPC message: {error}')
                    )
                    
        except Exception as error:
            if not self._is_stopping:
                log_error(
                    Exception(
                        f'LSP server {self.server_name} connection error: {error}'
                    )
                )
                self._start_failed = True
                self._start_error = error
    
    async def _handle_message(self, message: Dict[str, Any]) -> None:
        """Handle incoming JSON-RPC message."""
        # Response to our request (must have id AND either result or error)
        if 'id' in message and ('result' in message or 'error' in message):
            msg_id = message['id']
            if msg_id in self._pending_requests:
                future = self._pending_requests.pop(msg_id)
                if 'error' in message:
                    error_info = message['error']
                    future.set_exception(
                        Exception(
                            f"LSP error {error_info.get('code', -1)}: "
                            f"{error_info.get('message', 'Unknown error')}"
                        )
                    )
                else:
                    future.set_result(message.get('result'))
        
        # Request from server (reverse direction)
        elif 'method' in message and 'id' in message:
            method = message['method']
            params = message.get('params')
            msg_id = message['id']
            
            handler = getattr(self, f'_request_handler_{method.replace("/", "_")}', None)
            if handler:
                try:
                    result = await handler(params) if asyncio.iscoroutinefunction(handler) else handler(params)
                    await self._send_response(msg_id, result)
                except Exception as error:
                    await self._send_error(msg_id, error)
            else:
                log_for_debugging(
                    f'No handler for LSP request {method} from {self.server_name}'
                )
        
        # Notification from server
        elif 'method' in message:
            method = message['method']
            params = message.get('params')
            
            handler = getattr(self, f'_notification_handler_{method.replace("/", "_")}', None)
            if handler:
                try:
                    handler(params)
                except Exception as error:
                    from services.utils.log import log_error
                    log_error(
                        Exception(
                            f'Notification handler error for {method}: {error}'
                        )
                    )
    
    async def _send_response(self, msg_id: Any, result: Any) -> None:
        """Send JSON-RPC response to server."""
        message = {
            'jsonrpc': '2.0',
            'id': msg_id,
            'result': result,
        }
        await self._send_message(message)
    
    async def _send_error(self, msg_id: Any, error: Exception) -> None:
        """Send JSON-RPC error response to server."""
        message = {
            'jsonrpc': '2.0',
            'id': msg_id,
            'error': {
                'code': -32603,  # Internal error
                'message': str(error),
            },
        }
        await self._send_message(message)
    
    async def _send_message(self, message: Dict[str, Any]) -> None:
        """Send JSON-RPC message with Content-Length framing."""
        if not self._writer:
            raise Exception('LSP client not started')
        
        content = json.dumps(message, separators=(',', ':'))
        content_bytes = content.encode('utf-8')
        header = f'Content-Length: {len(content_bytes)}\r\n\r\n'
        
        self._writer.write(header.encode('utf-8'))
        self._writer.write(content_bytes)
        await self._writer.drain()
    
    async def initialize(self, params: LspInitializeParams) -> LspInitializeResult:
        """
        Initialize the LSP server with workspace info.
        
        Args:
            params: InitializeParams with capabilities and workspace info
            
        Returns:
            InitializeResult with server capabilities
        """
        if not self._writer:
            raise Exception('LSP client not started')
        
        self._check_start_failed()
        
        try:
            # Send initialize request
            result = await self.send_request('initialize', params)
            self._capabilities = result
            
            # Send initialized notification
            await self.send_notification('initialized', {})
            
            self._is_initialized = True
            
            log_for_debugging(f'LSP server {self.server_name} initialized')
            
            return result
            
        except Exception as error:
            log_error(
                Exception(
                    f'LSP server {self.server_name} initialize failed: {error}'
                )
            )
            raise
    
    async def send_request(self, method: str, params: Any) -> Any:
        """
        Send JSON-RPC request and wait for response.
        
        Args:
            method: LSP method name
            params: Method parameters
            
        Returns:
            Server response
        """
        if not self._writer:
            raise Exception('LSP client not started')
        
        self._check_start_failed()
        
        if not self._is_initialized:
            raise Exception('LSP server not initialized')
        
        msg_id = self._message_id
        self._message_id += 1
        
        # Create future for response
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self._pending_requests[msg_id] = future
        
        # Send request
        message = {
            'jsonrpc': '2.0',
            'id': msg_id,
            'method': method,
            'params': params,
        }
        
        try:
            await self._send_message(message)
            return await future
        except Exception as error:
            # Clean up pending request
            self._pending_requests.pop(msg_id, None)
            
            log_error(
                Exception(
                    f'LSP server {self.server_name} request {method} failed: {error}'
                )
            )
            raise
    
    async def send_notification(self, method: str, params: Any) -> None:
        """
        Send JSON-RPC notification (fire-and-forget).
        
        Args:
            method: LSP method name
            params: Method parameters
        """
        if not self._writer:
            raise Exception('LSP client not started')
        
        self._check_start_failed()
        
        try:
            message = {
                'jsonrpc': '2.0',
                'method': method,
                'params': params,
            }
            await self._send_message(message)
            
        except Exception as error:
            log_error(
                Exception(
                    f'LSP server {self.server_name} notification {method} failed: {error}'
                )
            )
            # Don't re-throw for notifications - fire-and-forget
            log_for_debugging(f'Notification {method} failed but continuing')
    
    def on_notification(self, method: str, handler: Callable[[Any], None]) -> None:
        """
        Register notification handler.
        
        Args:
            method: LSP notification method name
            handler: Callback function
        """
        if not self._writer:
            # Queue handler for when connection is ready
            self._pending_handlers.append((method, handler))
            log_for_debugging(
                f'Queued notification handler for {self.server_name}.{method} (connection not ready)'
            )
            return
        
        self._check_start_failed()
        self._register_notification_handler(method, handler)
    
    def _register_notification_handler(
        self, method: str, handler: Callable[[Any], None]
    ) -> None:
        """Register notification handler (internal)."""
        safe_method = method.replace('/', '_')
        setattr(self, f'_notification_handler_{safe_method}', handler)
    
    def on_request(
        self,
        method: str,
        handler: Callable[[Any], Any],
    ) -> None:
        """
        Register request handler for server-to-client requests.
        
        Args:
            method: LSP request method name
            handler: Callback function (can be async)
        """
        if not self._writer:
            # Queue handler for when connection is ready
            self._pending_request_handlers.append((method, handler))
            log_for_debugging(
                f'Queued request handler for {self.server_name}.{method} (connection not ready)'
            )
            return
        
        self._check_start_failed()
        self._register_request_handler(method, handler)
    
    def _register_request_handler(
        self, method: str, handler: Callable[[Any], Any]
    ) -> None:
        """Register request handler (internal)."""
        safe_method = method.replace('/', '_')
        setattr(self, f'_request_handler_{safe_method}', handler)
    
    async def stop(self) -> None:
        """
        Stop the LSP server gracefully.
        
        Sends shutdown request and exit notification, then kills process.
        """
        shutdown_error: Optional[Exception] = None
        
        # Mark as stopping to prevent spurious error logging
        self._is_stopping = True
        
        try:
            if self._writer and self._is_initialized:
                # Send shutdown request and exit notification
                try:
                    await self.send_request('shutdown', {})
                    await self.send_notification('exit', {})
                except Exception as error:
                    shutdown_error = error
                    # Continue to cleanup despite shutdown failure
                    
        finally:
            # Always cleanup resources
            if self._read_task:
                self._read_task.cancel()
                try:
                    await self._read_task
                except asyncio.CancelledError:
                    pass
                self._read_task = None
            
            # Cancel pending requests
            for future in self._pending_requests.values():
                if not future.done():
                    future.cancel()
            self._pending_requests.clear()
            
            # Kill process
            if self._process:
                try:
                    self._process.kill()
                    await self._process.wait()
                except Exception as error:
                    from services.utils.debug import log_for_debugging
                    log_for_debugging(
                        f'Process kill failed for {self.server_name}: {error}'
                    )
                self._process = None
            
            self._reader = None
            self._writer = None
            self._is_initialized = False
            self._capabilities = None
            self._is_stopping = False  # Reset for potential restart
            
            if shutdown_error:
                self._start_failed = True
                self._start_error = shutdown_error
            
            log_for_debugging(f'LSP client stopped for {self.server_name}')
        
        # Re-throw shutdown error after cleanup
        if shutdown_error:
            raise shutdown_error


def create_lsp_client(
    server_name: str,
    on_crash: Optional[Callable[[Exception], None]] = None,
) -> LSPClient:
    """
    Factory function to create an LSP client.
    
    Args:
        server_name: Unique identifier for this server
        on_crash: Called when server exits unexpectedly
        
    Returns:
        LSPClient instance
    """
    return LSPClient(server_name, on_crash)


__all__ = [
    'LSPClient',
    'create_lsp_client',
]
