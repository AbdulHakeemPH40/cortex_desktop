import gc
import os
import sys
import json
import re
import time
import uuid as _uuid
from pathlib import Path
import threading
from typing import Any, Dict, List, Optional, Callable, Tuple, Type, Set, Protocol, cast
from dataclasses import dataclass

# Usage tracker — records token/tool/session metrics for Settings → Profile UI
from src.ai.usage_tracker import get_usage_tracker

# CRITICAL: Defer asyncio import to avoid PyInstaller TypeError on Windows.
# PyInstaller 6.x + Python 3.14 fails with "function() argument 'code' must be code, not str"
# when asyncio.windows_utils is loaded during module initialization.
# The runtime_hook_asyncio.py pre-imports it, but we still defer here as a safety net.
import asyncio

from PyQt6.QtCore import QObject, pyqtSignal, QThread
from PyQt6.sip import isdeleted as _sip_isdeleted

# ============================================================
# SETUP PATH — expose real agent core as importable package
# ============================================================
_AGENT_SRC = os.path.abspath(
    os.path.join(os.path.dirname(__file__), '..', 'agent', 'src')
)
_PROJECT_SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if _PROJECT_SRC not in sys.path:
    sys.path.insert(0, _PROJECT_SRC)
# Keep agent internals importable, but do not prepend to avoid shadowing stdlib
# modules (e.g. src/agent/src/types.py vs Python's built-in types module).
if _AGENT_SRC not in sys.path:
    sys.path.append(_AGENT_SRC)

from src.utils.logger import get_logger  
from src.ai.streaming import get_streaming_emitter
from src.ai.agent_safety import (
    AgentSafetyGuard,
    PERSISTENT_DIRECTIVES,
    WRITE_TOOL_NAMES,
    READ_TOOL_NAMES,
    SEARCH_EXCLUDE_PATTERNS,
)
from src.ai.session_task import (  
    SessionTaskRegistry,
    SessionTaskState,
    StopTaskError,
    generate_session_task_id,
    stop_session_task,
)
from src.ai.model_limits import ModelLimits
import urllib.error
import urllib.request

# ============================================================
# SKILLS & RULES INTEGRATION (OpenCode-style)
# ============================================================
try:
    from agent.src.skills.opencode_skills import (
        SkillsManager,
        get_skills_manager,
        reset_skills_manager as _reset_skills_manager,
    )
    _HAS_SKILLS = True
except ImportError:
    _HAS_SKILLS = False
    SkillsManager = object  # type: ignore

try:
    from agent.src.skills.rules import (
        RulesManager,
        Rule as _ActualRule,
        get_rules_manager,
        reset_rules_manager as _reset_rules_manager,
    )
    _HAS_RULES = True
except ImportError:
    _HAS_RULES = False
    RulesManager = object  # type: ignore

# ============================================================
# CORTEX PROJECT CONTEXT (.cortex/ directory)
# ============================================================
try:
    from src.ai.cortex_project_context import (
        load_all_cortex_context,
        get_cortex_context_summary,
        ensure_cortex_dir,
        update_project_memory,
    )
    _HAS_CORTEX_PROJECT_CTX = True
except ImportError:
    _HAS_CORTEX_PROJECT_CTX = False

# ============================================================
# COMPACT SYSTEM INTEGRATION (autoCompact + prompt)
# ============================================================
try:
    from agent.src.services.compact.autoCompact import (
        getAutoCompactThreshold,
        getEffectiveContextWindowSize,
        calculateTokenWarningState,
        isAutoCompactEnabled,
    )
    _HAS_AUTOCOMPACT = True
except ImportError:
    _HAS_AUTOCOMPACT = False
    def getAutoCompactThreshold(model: str) -> int:
        return int(200_000 * 0.80)
    def getEffectiveContextWindowSize(model: str) -> int:
        return 180_000
    def calculateTokenWarningState(tokenUsage: int, model: str) -> dict:
        return {}
    def isAutoCompactEnabled() -> bool:
        return True

try:
    from agent.src.services.compact.prompt import (
        get_compact_user_summary_message,
        get_compact_prompt,
    )
    _HAS_COMPACT_PROMPT = True
except ImportError:
    _HAS_COMPACT_PROMPT = False
    def get_compact_user_summary_message(summary: str, *args, **kwargs) -> str:
        return summary
    def get_compact_prompt(*args, **kwargs) -> str:
        return "Please summarize the conversation."


def load_all_cortex_context(project_root: str = None) -> str:
    return ""


def get_cortex_context_summary(project_root: str = None) -> dict:
    return {"exists": False}


def ensure_cortex_dir(project_root: str = None) -> bool:
    return False


def update_project_memory(project_root: str = None, entry_type: str = "decisions", entry: str = "") -> bool:
    return False

log = get_logger("agent_bridge")

DEFAULT_READ_CHUNK_LINES_ENV = "CORTEX_READ_DEFAULT_CHUNK_LINES"
DEFAULT_READ_CHUNK_LINES_FALLBACK = 1000


class _ThinkingTimeoutError(Exception):
    """Raised when the model is stuck in an infinite reasoning loop.

    This is caught by the compact-attempt handler to inject a directive
    and retry the turn — same pattern as context-length and rate-limit errors.
    """
    pass


def _get_default_read_chunk_lines() -> int:
    """Return safe default chunk size for unbounded Read calls."""
    raw = os.environ.get(DEFAULT_READ_CHUNK_LINES_ENV)
    if raw:
        try:
            parsed = int(raw)
            if parsed > 0:
                return parsed
        except (TypeError, ValueError):
            pass
    return DEFAULT_READ_CHUNK_LINES_FALLBACK


def _msg_to_dict(msg) -> dict:
    """Convert a ChatMessage dataclass or dict to a plain dict."""
    if isinstance(msg, dict):
        return msg
    # ChatMessage dataclass — extract known fields
    d = {}
    d["role"] = getattr(msg, "role", "user")
    content = getattr(msg, "content", None)
    if content is not None:
        d["content"] = content
    tool_calls = getattr(msg, "tool_calls", None)
    if tool_calls:
        # tool_calls may be list of dataclass objects or dicts
        tc_list = []
        for tc in tool_calls:
            if isinstance(tc, dict):
                tc_list.append(tc)
            else:
                tc_dict = {}
                tc_dict["id"] = getattr(tc, "id", "")
                tc_dict["type"] = getattr(tc, "type", "function")
                fn = getattr(tc, "function", None)
                if fn:
                    if isinstance(fn, dict):
                        tc_dict["function"] = fn
                    else:
                        tc_dict["function"] = {
                            "name": getattr(fn, "name", ""),
                            "arguments": getattr(fn, "arguments", ""),
                        }
                tc_list.append(tc_dict)
        d["tool_calls"] = tc_list
    tool_call_id = getattr(msg, "tool_call_id", None)
    if tool_call_id:
        d["tool_call_id"] = tool_call_id
    name = getattr(msg, "name", None)
    if name:
        d["name"] = name
    reasoning_content = getattr(msg, "reasoning_content", None)
    if reasoning_content:
        d["reasoning_content"] = reasoning_content
    return d


def _sanitize_tool_call_messages(messages: list) -> list:
    """Strip orphaned tool_calls from assistant messages.

    DeepSeek (and other OpenAI-compatible APIs) require that every
    assistant message with ``tool_calls`` is immediately followed by
    one ``tool`` role message per ``tool_call_id``.  When chat history
    is loaded from SQLite, truncated, or context-shifted, those tool
    responses can be missing — causing a hard 400 error.

    This function walks the message list and removes the ``tool_calls``
    key from any assistant message whose tool responses are missing,
    converting it to a plain text assistant message so the API accepts
    it.

    Accepts both dict and ChatMessage dataclass objects.
    """
    if not messages:
        return messages

    # Convert all messages to plain dicts first (handles ChatMessage objects)
    sanitized = [_msg_to_dict(m) for m in messages]
    n = len(sanitized)
    i = 0
    while i < n:
        msg = sanitized[i]
        # ── Normalize tool_call arguments to valid JSON ──
        # Alibaba (qwen-flash) rejects function.arguments that aren't valid JSON.
        # Empty strings or malformed JSON must be fixed before sending to API.
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            for tc in msg["tool_calls"]:
                fn = tc.get("function", {})
                if isinstance(fn, dict):
                    args_raw = fn.get("arguments", "")
                    if not args_raw or not str(args_raw).strip():
                        fn["arguments"] = "{}"
                    else:
                        try:
                            json.loads(str(args_raw))
                        except (json.JSONDecodeError, TypeError):
                            log.warning(f"[SANITIZE] Fixed invalid JSON in tool_call arguments: {str(args_raw)[:200]}")
                            fn["arguments"] = "{}"
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            # Collect the tool_call_ids this message expects
            expected_ids = set()
            for tc in msg["tool_calls"]:
                tc_id = tc.get("id", "")
                if tc_id:
                    expected_ids.add(tc_id)

            if expected_ids:
                # Scan subsequent messages for matching tool responses
                found_ids = set()
                j = i + 1
                while j < n and sanitized[j].get("role") == "tool":
                    found_ids.add(sanitized[j].get("tool_call_id", ""))
                    j += 1

                # If any are missing, strip tool_calls from this message
                missing = expected_ids - found_ids
                if missing:
                    log.warning(
                        f"[SANITIZE] Assistant msg {i} has tool_calls {expected_ids} "
                        f"but missing tool responses for {missing} — stripping tool_calls"
                    )
                    msg.pop("tool_calls", None)
                    if "content" not in msg:
                        msg["content"] = ""
        i += 1

    return sanitized


# ============================================================
# IMPORT REAL AGENT STATE (bootstrap/state.py)
# ============================================================
_has_agent_state = False  # Internal flag (lowercase to avoid constant redefinition warning)
try:
    from agent.src.bootstrap.state import ( 
        set_original_cwd,  
        set_project_root as _agent_set_project_root,  
        getSessionId as _get_session_id,
        get_project_root as _agent_get_project_root,
    )
    _has_agent_state = True
    log.info("[BRIDGE] Real agent bootstrap/state loaded")
except ImportError as _e:
    log.warning(f"[BRIDGE] agent bootstrap/state not available: {_e}")

    def set_original_cwd(cwd: str) -> None: pass
    def _agent_set_project_root(path: str) -> None: pass
    def _get_session_id() -> str: return "default"
    def _agent_get_project_root() -> str:
        cwd = os.getcwd()
        # Fallback guard: never return Program Files as project root
        if 'Program Files' in cwd:
            return ''  # empty = no project root set
        return cwd

# Public wrapper functions
def get_session_id() -> str:
    """Get current session ID."""
    return _get_session_id()

def get_project_root() -> str:
    """Get project root directory."""
    return _agent_get_project_root()

# Backwards compatibility alias
_HAS_AGENT_STATE = _has_agent_state


# ============================================================
# LOCAL DATA CLASSES
# ============================================================

@dataclass
class ChatMessage:
    """Internal chat message used by the bridge."""
    role: str                           # system / user / assistant / tool
    content: str
    images: Optional[List[str]] = None
    tool_calls: Optional[List[Dict[str, Any]]] = None
    tool_call_id: Optional[str] = None
    reasoning_content: Optional[str] = None
    
    def get_display_content(self) -> str:
        """Return only the visible content, stripping internal reasoning/thinking."""
        content = self.content or ""
        import re as _re
        # Strip <think>...</think>, <thinking>...</thinking>, <antThinking>...</antThinking>
        content = _re.sub(r'<think>.*?</think>', '', content, flags=_re.DOTALL)
        content = _re.sub(r'<thinking>.*?</thinking>', '', content, flags=_re.DOTALL)
        content = _re.sub(r'<antThinking>.*?</antThinking>', '', content, flags=_re.DOTALL)
        content = _re.sub(r'<scratchpad>.*?</scratchpad>', '', content, flags=_re.DOTALL)
        content = _re.sub(r'<task_summary>.*?</task_summary>', '', content, flags=_re.DOTALL)
        # Strip verification loop meta-commentary
        content = _re.sub(r'The system is asking for verification.*?no tests to run\.', '', content, flags=_re.DOTALL)
        return content.strip()
    
    def __post_init__(self):
        if self.images is None:
            self.images = []


@dataclass
class ToolCall:
    tool_id: str
    tool_name: str
    arguments: Dict[str, Any]


@dataclass
class ToolResult:
    tool_id: str
    result: Any
    success: bool = True
    error: Optional[str] = None


WorkerMessage = Dict[str, Any]
ParsedToolCall = Tuple[str, str, Any]


class _ToolLimitsLike(Protocol):
    max_tool_result_chars: int


# ============================================================
# EXTRACTED UTILITIES (Phase B refactor)
# ============================================================
from src.ai.circuit_breaker import ToolCircuitBreaker
from src.ai.tool_executor import ToolExecutionEngine
from src.core.task_graph import TaskGraph, TaskNode, TaskStatus


# ============================================================
# IMPORT REAL AGENT TOOLS from src/agent/src/tools/
# These are the robust, production-quality implementations.
# ============================================================

import importlib as _importlib
import importlib.util as _importlib_util

def _load_agent_tool(module_path: str, class_name: str) -> Optional[type]:
    """Dynamically import a real agent tool class. Returns None on failure."""
    try:
        mod = _importlib.import_module(module_path)
        cls = getattr(mod, class_name)
        log.info(f"[BRIDGE] Real {class_name} loaded")
        return cls
    except Exception as exc:
        log.warning(f"[BRIDGE] {class_name} not available: {exc}")
        return None

_REAL_FILE_READ_TOOL  = _load_agent_tool("agent.src.tools.FileReadTool.FileReadTool",  "FileReadTool")
_REAL_FILE_EDIT_TOOL  = _load_agent_tool("agent.src.tools.FileEditTool.FileEditTool",  "FileEditTool")
_REAL_FILE_WRITE_TOOL = _load_agent_tool("agent.src.tools.FileWriteTool.FileWriteTool", "FileWriteTool")
_REAL_GLOB_TOOL       = _load_agent_tool("agent.src.tools.GlobTool.GlobTool",          "GlobTool")
_REAL_GREP_TOOL            = _load_agent_tool("agent.src.tools.GrepTool.GrepTool",                     "GrepTool")
_REAL_SEMANTIC_SEARCH_TOOL = _load_agent_tool("agent.src.tools.SementicSearch.SementicSearchTool", "SementicSearchTool")


# ============================================================
# IMPORT REAL AbortController from src/agent/src/utils
# Used to signal running tools when the user presses Stop.
# ============================================================
_has_real_abort = False  # Internal flag (lowercase to avoid constant redefinition warning)

# Define fallback implementation first
class _FallbackAbortController:
    """Fallback stub AbortController."""
    def __init__(self) -> None:
        class _Signal:
            aborted: bool = False
            reason: Optional[str] = None
        self.signal: _Signal = _Signal()
    
    def abort(self, reason: str = "AbortError") -> None:
        self.signal.aborted = True
        self.signal.reason = reason


def _fallback_create_abort_controller(max_listeners: int = 50) -> _FallbackAbortController:
    """Fallback stub create_abort_controller."""
    return _FallbackAbortController()


try:
    from agent.src.utils.abortController import ( 
        AbortController as _RealAbortController, 
        create_abort_controller as _real_create_abort_controller,
    )
    _has_real_abort = True
    log.info("[BRIDGE] Real AbortController loaded from utils.abortController")
    
    # Use real implementations
    AbortController = _RealAbortController
    create_abort_controller = _real_create_abort_controller
except ImportError as _e:
    log.warning(f"[BRIDGE] utils.abortController not available: {_e}")
    
    # Use fallback implementations
    AbortController = _FallbackAbortController
    create_abort_controller = _fallback_create_abort_controller


# ============================================================
# DIFF HOOKS — useDiffData + useDiffInIDE integration
# ============================================================

def _load_diff_service():
    """Load DiffDataService singleton. Returns None if unavailable."""
    try:
        mod = _importlib.import_module("agent.src.hooks.useDiffData")
        return mod.get_diff_service()
    except Exception as exc:
        log.warning(f"[BRIDGE] DiffDataService not available: {exc}")
        return None

def _load_cortex_diff_bridge():
    """Load CortexDiffBridge singleton. Returns None if unavailable."""
    try:
        mod = _importlib.import_module("agent.src.hooks.useDiffInIDE")
        return mod.CortexDiffBridge.instance()
    except Exception as exc:
        log.warning(f"[BRIDGE] CortexDiffBridge not available: {exc}")
        return None

_DIFF_SERVICE      = _load_diff_service()      # DiffDataService | None
_CORTEX_DIFF_BRIDGE = _load_cortex_diff_bridge()  # _CortexDiffBridge | None


# ============================================================
# CORTEX TOOL CONTEXT — minimal adapter for real agent tools
# Real tools call context.get_app_state(), context.read_file_state,
# context.abort_controller, context.glob_limits, etc.
# ============================================================

class _PermissionContext:
    """Stub permission context — allows everything."""
    mode = "default"
    rules = []


class _AppState:
    """AppState providing tool_permission_context and session state."""
    tool_permission_context = _PermissionContext()
    
    def __init__(self):
        self._state: Dict[str, Any] = {}
    
    def get(self, key: str, default: Any = None) -> Any:
        return self._state.get(key, default)
    
    def set(self, key: str, value: Any) -> None:
        self._state[key] = value
    
    def update(self, data: Dict[str, Any]) -> None:
        self._state.update(data)


class _GlobLimits:
    max_results = 1000


class _WaitResumeController:
    """
    Controller for wait/resume mechanism.
    Allows tools to pause execution and wait for external events.
    """
    def __init__(self):
        self._waiting = False
        self._event = None
        self._result = None
    
    def is_waiting(self) -> bool:
        return self._waiting
    
    def wait(self, timeout: float = 30.0) -> Any:
        """Block until resumed or timeout."""
        import threading
        self._waiting = True
        self._event = threading.Event()
        self._event.wait(timeout)
        self._waiting = False
        return self._result
    
    def resume(self, result: Any = None) -> None:
        """Resume execution with a result."""
        self._result = result
        self._waiting = False
        if self._event:
            self._event.set()


class _MCPHookManager:
    """
    Manager for MCP (Model Context Protocol) hooks.
    Allows registration and execution of MCP server hooks.
    """
    def __init__(self) -> None:
        self._hooks: Dict[str, List[Callable[..., Any]]] = {}
    
    def register(self, event: str, callback: Callable[..., Any]) -> None:
        if event not in self._hooks:
            self._hooks[event] = []
        self._hooks[event].append(callback)
    
    def unregister(self, event: str, callback: Callable[..., Any]) -> None:
        if event in self._hooks and callback in self._hooks[event]:
            self._hooks[event].remove(callback)
    
    async def trigger(self, event: str, *args: Any, **kwargs: Any) -> List[Any]:
        results: List[Any] = []
        for callback in self._hooks.get(event, []):
            try:
                result = callback(*args, **kwargs)
                if asyncio.iscoroutine(result):
                    result = await result
                results.append(result)
            except Exception as e:
                log.warning(f"[MCP Hook] {event} callback failed: {e}")
        return results


class _AuthHookManager:
    """
    Manager for authentication hooks.
    Allows tools to request authentication from the UI.
    """
    def __init__(self, bridge: 'CortexAgentBridge'):
        self._bridge = bridge
        self._pending_auth: Dict[str, Any] = {}
    
    def request_auth(self, service: str, scopes: Optional[List[str]] = None) -> str:
        """Request authentication for a service. Returns auth request ID."""
        import uuid
        request_id = f"auth-{uuid.uuid4().hex[:8]}"
        self._pending_auth[request_id] = {
            "service": service,
            "scopes": scopes or [],
            "status": "pending",
            "result": None,
        }
        log.info(f"[AUTH] Auth request {request_id} for {service}")
        return request_id
    
    def complete_auth(self, request_id: str, result: Any) -> None:
        """Complete an auth request with a result."""
        if request_id in self._pending_auth:
            self._pending_auth[request_id]["status"] = "completed"
            self._pending_auth[request_id]["result"] = result
    
    def get_auth_status(self, request_id: str) -> Optional[Dict[str, Any]]:
        return self._pending_auth.get(request_id)


class _SessionStateManager:
    """
    Manager for session-level state that tools can read/write.
    Provides a key-value store for tool communication.
    """
    def __init__(self) -> None:
        self._state: Dict[str, Any] = {}
        self._listeners: Dict[str, List[Callable[[str, Any, Any], None]]] = {}
    
    def get(self, key: str, default: Any = None) -> Any:
        return self._state.get(key, default)
    
    def set(self, key: str, value: Any) -> None:
        old_value = self._state.get(key)
        self._state[key] = value
        # Notify listeners
        for callback in self._listeners.get(key, []):
            try:
                callback(key, old_value, value)
            except Exception as e:
                log.warning(f"[State] Listener for {key} failed: {e}")
    
    def subscribe(self, key: str, callback: Callable[[str, Any, Any], None]) -> None:
        if key not in self._listeners:
            self._listeners[key] = []
        self._listeners[key].append(callback)
    
    def unsubscribe(self, key: str, callback: Callable[[str, Any, Any], None]) -> None:
        if key in self._listeners and callback in self._listeners[key]:
            self._listeners[key].remove(callback)


class _ContextBudgetTracker:
    """
    Tracks context budget usage across tool calls.
    Prevents context overflow by monitoring cumulative token usage.
    """
    def __init__(self, model_limits: Optional[ModelLimits] = None):
        self._model_limits = model_limits
        self._hard_context_cap_tokens = self._get_hard_context_cap_tokens()
        self._files_in_context: Dict[str, int] = {}  # path -> estimated tokens
        self._total_estimated_tokens = 0
        self._warnings: List[str] = []
        self._turn_file_read_count: int = 0  # Per-turn file read counter
        self._turn_file_read_limit: int = 50  # Max file reads per turn (Cursor-style: allow deep exploration)

    @staticmethod
    def _get_hard_context_cap_tokens() -> int:
        # Global safety ceiling for per-turn context usage.
        # Default raised to 1M so high-context models (e.g., DeepSeek V4) are
        # not artificially constrained unless user overrides via env.
        raw = os.environ.get("CORTEX_MAX_CONTEXT_TOKENS_PER_TURN", "1000000")
        try:
            parsed = int(raw)
            if parsed > 0:
                return parsed
        except Exception:
            pass
        return 1_000_000

    def _effective_budget_tokens(self) -> int:
        if self._model_limits:
            return min(self._model_limits.context_budget, self._hard_context_cap_tokens)
        return self._hard_context_cap_tokens
    
    def set_model_limits(self, limits: ModelLimits) -> None:
        self._model_limits = limits
    
    def add_file(self, path: str, char_count: int) -> int:
        estimated_tokens = char_count // 4
        self._files_in_context[path] = estimated_tokens
        self._total_estimated_tokens += estimated_tokens
        return self._check_budget()
    
    def remove_file(self, path: str) -> None:
        if path in self._files_in_context:
            self._total_estimated_tokens -= self._files_in_context[path]
            del self._files_in_context[path]
    
    def _check_budget(self) -> int:
        budget = self._effective_budget_tokens()
        used = self._total_estimated_tokens
        remaining = budget - used
        if remaining < budget * 0.2:
            self._warnings.append(f"Context budget low: {remaining:,} tokens remaining")
        safe_remaining = int(remaining * 0.8)
        return max(4_000, safe_remaining * 4)
    
    def get_remaining_budget_chars(self) -> int:
        budget = self._effective_budget_tokens()
        safe_remaining = int((budget - self._total_estimated_tokens) * 0.8)
        return max(4_000, safe_remaining * 4)
    
    def get_warnings(self) -> List[str]:
        warnings = self._warnings.copy()
        self._warnings.clear()
        return warnings
    
    def is_over_budget(self) -> bool:
        budget = self._effective_budget_tokens()
        return self._total_estimated_tokens > budget * 0.9
    
    def check_turn_file_limit(self) -> Optional[str]:
        """Check if per-turn file read limit is reached. Returns error message or None."""
        self._turn_file_read_count += 1
        if self._turn_file_read_count > self._turn_file_read_limit:
            return (
                f"Per-turn file read limit reached ({self._turn_file_read_limit} files). "
                f"You've read {self._turn_file_read_count - 1} files this turn. "
                f"Implement with what you have — you can read more files in the next turn."
            )
        return None
    
    def reset_turn_counter(self) -> None:
        """Reset per-turn file read counter (called at start of each turn)."""
        self._turn_file_read_count = 0


class CortexToolContext:
    """
    Expanded context with MODEL-AWARE FILE READ LIMITS.
    
    CRITICAL: Prevents context overflow by capping file reads based on
    model context window. Large files MUST be read in chunks.
    
    Includes:
    - Model-aware file reading limits (prevents context overflow)
    - Context budget tracking (monitors cumulative token usage)
    - File state tracking (read/modified files)
    - LRU file read dedup cache (ported from Claude Code's fileStateCache.ts)
    - App state management, Wait/resume, MCP/Auth hooks
    """

    # ── LRU File Read Cache constants ────────────────────────────────────
    # Ported from Claude Code: fileStateCache.ts (100 entries, 25MB max)
    _FILE_CACHE_MAX_ENTRIES = 100
    _FILE_CACHE_MAX_SIZE_BYTES = 25 * 1024 * 1024  # 25MB
    _FILE_UNCHANGED_STUB = (
        "[File content unchanged since last read — using cached version. "
        "The content is available in your context. Re-read with different offset/limit if you need a different section.]"
    )

    def __init__(self, bridge: 'CortexAgentBridge', model_id: str = "gpt-4o"):
        self._bridge = bridge
        self._model_id = model_id
        
        # Model-aware limits
        self._model_limits: Optional[Any] = None
        self._budget_tracker = _ContextBudgetTracker()
        
        # File reading limits - updated when model is set
        self.read_file_state: Dict[str, Any] = {}
        self.file_reading_limits = {
            "maxSizeBytes": 40_000,
            "maxTokens": 10_000,
        }
        
        # ── LRU File Read Dedup Cache ────────────────────────────────────
        # Tracks file content by normalized path. On re-read, if mtime + 
        # offset/limit match, returns FILE_UNCHANGED_STUB instead of full
        # content, saving massive context. Uses OrderedDict for LRU eviction.
        # Ported from Claude Code's FileStateCache (fileStateCache.ts)
        from collections import OrderedDict
        self._file_cache: OrderedDict[str, Dict[str, Any]] = OrderedDict()  # norm_path → {content, timestamp, offset, limit, size}
        self._file_cache_total_size: int = 0
        
        self.glob_limits = _GlobLimits()
        self.abort_controller = create_abort_controller()
        self.dynamic_skill_dir_triggers: set = set()
        self.nested_memory_attachment_triggers: set = set()
        self.user_modified = False
        
        # Content replacement state for per-message budget enforcement
        # (ported from Claude Code's ContentReplacementState)
        self._content_replacement_state = None  # lazy init

        # File state tracking
        self._files_read: Dict[str, float] = {}
        self._files_modified: Dict[str, float] = {}
        
        # Expanded state management
        self._app_state = _AppState()
        self._wait_resume = _WaitResumeController()
        self._mcp_hooks = _MCPHookManager()
        self._auth_hooks = _AuthHookManager(bridge)
        self._session_state = _SessionStateManager()
        
        # Permission context
        self._permission_context = _PermissionContext()
        
        # Initialize limits for default model
        self._init_model_limits(model_id)
    
    def _init_model_limits(self, model_id: str) -> None:
        """Initialize model-aware file reading limits."""
        try:
            from src.ai.model_limits import get_model_limits  
            self._model_limits = get_model_limits(model_id)
            self._budget_tracker.set_model_limits(self._model_limits)
            # _model_limits is guaranteed to be set here by get_model_limits()
            assert self._model_limits is not None  # Ensure type checker knows it's not None
            self.file_reading_limits = {
                "maxSizeBytes": self._model_limits.max_file_read_bytes,
                "maxTokens": self._model_limits.max_file_read_chars // 4,
            }
            log.info(f"[CTX] Model limits: {model_id} -> file_cap={self._model_limits.max_file_read_chars:,} chars")
        except Exception as e:
            log.warning(f"[CTX] Failed to get model limits: {e}")
            self.file_reading_limits = {"maxSizeBytes": 40_000, "maxTokens": 10_000}
    
    def set_model(self, model_id: str) -> None:
        if model_id != self._model_id:
            self._model_id = model_id
            self._init_model_limits(model_id)
    
    def get_max_file_read_chars(self) -> int:
        if self._model_limits:
            return self._model_limits.max_file_read_chars
        return 10_000
    def get_remaining_budget_chars(self) -> int:
        return self._budget_tracker.get_remaining_budget_chars()
    def track_file_read(self, path: str, char_count: int) -> None:
        self._budget_tracker.add_file(path, char_count)
    def is_context_over_budget(self) -> bool:
        return self._budget_tracker.is_over_budget()
    def get_budget_warnings(self) -> List[str]:
        return self._budget_tracker.get_warnings()

    # ── LRU File Read Dedup Cache methods ─────────────────────────────────
    # Ported from Claude Code's FileStateCache (fileStateCache.ts)

    def file_cache_get(self, norm_path: str, offset: Optional[int] = None, limit: Optional[int] = None) -> Optional[str]:
        """
        Check if a file read can be served from cache.
        Returns FILE_UNCHANGED_STUB if cached content matches current disk mtime
        and same offset/limit. Returns None if cache miss.
        """
        entry: Optional[Dict[str, Any]] = self._file_cache.get(norm_path)
        if entry is None:
            return None
        
        # Check mtime
        try:
            current_mtime = os.path.getmtime(norm_path)
        except OSError:
            return None
        
        if entry['timestamp'] != current_mtime:
            # File changed — invalidate cache entry
            self._file_cache_evict(norm_path)
            return None
        
        # Check offset/limit match
        if entry['offset'] != offset or entry['limit'] != limit:
            return None
        
        # Cache HIT — move to end (most recently used)
        self._file_cache.move_to_end(norm_path)
        log.info(f"[CTX] File cache HIT: {os.path.basename(norm_path)} (saved {entry['size']:,} chars)")
        return self._FILE_UNCHANGED_STUB

    def file_cache_put(self, norm_path: str, content: str, mtime: float, offset: Optional[int] = None, limit: Optional[int] = None) -> None:
        """Store a file read result in the LRU cache."""
        content_size = len(content.encode('utf-8', errors='replace'))
        
        # Evict if already present (to update size tracking)
        if norm_path in self._file_cache:
            self._file_cache_evict(norm_path)
        
        # Evict LRU entries until under size limit
        while (self._file_cache_total_size + content_size > self._FILE_CACHE_MAX_SIZE_BYTES
               and self._file_cache):
            oldest_key: str = next(iter(self._file_cache))
            self._file_cache_evict(oldest_key)
        
        # Evict if too many entries
        while len(self._file_cache) >= self._FILE_CACHE_MAX_ENTRIES:
            oldest_key = next(iter(self._file_cache))
            self._file_cache_evict(oldest_key)
        
        self._file_cache[norm_path] = {
            'content': content,
            'timestamp': mtime,
            'offset': offset,
            'limit': limit,
            'size': content_size,
        }
        self._file_cache_total_size += content_size

    def _file_cache_evict(self, norm_path: str) -> None:
        """Remove an entry from the file cache."""
        entry = self._file_cache.pop(norm_path, None)
        if entry:
            self._file_cache_total_size -= entry['size']

    def file_cache_invalidate(self, norm_path: str):
        """Invalidate cache for a file (e.g. after edit/write)."""
        self._file_cache_evict(os.path.normpath(os.path.abspath(norm_path)))

    def get_content_replacement_state(self):
        """Get or create the per-conversation content replacement state."""
        if self._content_replacement_state is None:
            from src.ai.tool_result_storage import ContentReplacementState 
            self._content_replacement_state = ContentReplacementState()
        return self._content_replacement_state

    # Real tools call context.get_app_state()
    def get_app_state(self) -> _AppState:
        return self._app_state
    
    # App state setters
    def set_app_state(self, key: str, value: Any) -> None:
        self._app_state.set(key, value)
    
    def update_app_state(self, data: Dict[str, Any]) -> None:
        self._app_state.update(data)

    # FileEditTool / FileWriteTool check this
    def file_history_enabled(self) -> bool:
        return False

    # Wait/resume mechanism
    def wait_for_event(self, timeout: float = 30.0) -> Any:
        """Wait for an external event. Tools can use this for async operations."""
        return self._wait_resume.wait(timeout)
    
    def resume_execution(self, result: Any = None) -> None:
        """Resume execution after waiting."""
        self._wait_resume.resume(result)
    
    def is_waiting(self) -> bool:
        return self._wait_resume.is_waiting()

    # MCP hooks
    def register_mcp_hook(self, event: str, callback: Callable[..., Any]) -> None:
        self._mcp_hooks.register(event, callback)
    
    def unregister_mcp_hook(self, event: str, callback: Callable[..., Any]) -> None:
        self._mcp_hooks.unregister(event, callback)
    
    async def trigger_mcp_hook(self, event: str, *args: Any, **kwargs: Any) -> List[Any]:
        return await self._mcp_hooks.trigger(event, *args, **kwargs)

    # Auth hooks
    def request_auth(self, service: str, scopes: Optional[List[str]] = None) -> str:
        return self._auth_hooks.request_auth(service, scopes)
    
    def complete_auth(self, request_id: str, result: Any) -> None:
        self._auth_hooks.complete_auth(request_id, result)
    
    def get_auth_status(self, request_id: str) -> Optional[Dict[str, Any]]:
        return self._auth_hooks.get_auth_status(request_id)

    # Session state
    def get_session_state(self, key: str, default: Any = None) -> Any:
        return self._session_state.get(key, default)
    
    def set_session_state(self, key: str, value: Any) -> None:
        self._session_state.set(key, value)
      
    def subscribe_session_state(self, key: str, callback: Callable[[str, Any, Any], None]) -> None:
        self._session_state.subscribe(key, callback)
    
    def unsubscribe_session_state(self, key: str, callback: Callable[[str, Any, Any], None]) -> None:
        self._session_state.unsubscribe(key, callback)

    # Permission context
    def get_permission_context(self) -> _PermissionContext:
        return self._permission_context

    # File state helpers
    def mark_file_read(self, path: str):
        import time
        self._files_read[os.path.normpath(path)] = time.time()

    def mark_file_modified(self, path: str):
        import time
        norm = os.path.normpath(path)
        self._files_modified[norm] = time.time()
        self._files_read.pop(norm, None)

    def is_file_known(self, path: str) -> bool:
        norm = os.path.normpath(path)
        return norm in self._files_read

    def get_known_files_summary(self) -> str:
        lines: List[str] = []
        for p in list(self._files_read)[-10:]:
            lines.append(f"  [read] {p}")
        for p in list(self._files_modified)[-10:]:
            lines.append(f"  [modified] {p}")
        return "\n".join(lines) if lines else "(none yet)"

    def get_recent_read_files(self, limit: int = 10) -> List[str]:
        return list(self._files_read.keys())[-limit:]

    def get_recent_modified_files(self, limit: int = 10) -> List[str]:
        return list(self._files_modified.keys())[-limit:]


def _always_allow_tool(*_args: Any, **_kwargs: Any) -> bool:
    """Stub can_use_tool function — always allows."""
    return True


# Stub parent message (some tools read parent_message.uuid)
_STUB_PARENT_MESSAGE = type("_Msg", (), {"uuid": None})()


# ============================================================
# BRIDGE-NATIVE TOOLS  (no real agent equivalent exists)
# ============================================================
# Destructive-command helpers (used by BridgeBashTool)
# ============================================================

def _get_destructive_warning(command: str) -> 'Optional[str]':
    """Return a human-readable warning if the command is destructive, else None."""
    try:
        from src.agent.src.tools.BashTool.destructiveCommandWarning import get_destructive_command_warning 

        return get_destructive_command_warning(command)
    except Exception:
        pass
    # Inline fallback: simple regex for the most common dangerous patterns
    import re as _re
    PATTERNS = [
        (_re.compile(r'(^|[;&|]\s*)rm\s+-[a-zA-Z]*[rR]', _re.I), 'Note: may recursively remove files'),
        (_re.compile(r'(^|[;&|]\s*)rm\s+-[a-zA-Z]*f', _re.I), 'Note: may force-remove files'),
        (_re.compile(r'(^|[;&|]\s*)rm\s+\S', _re.I), 'Note: may delete files'),
        (_re.compile(r'(^|[;&|]\s*)rmdir\b', _re.I), 'Note: may remove a directory'),
        (_re.compile(r'\bdel\b.*\b/[sS]\b', _re.I), 'Note: may delete files recursively (Windows)'),
        (_re.compile(r'(^|[;&|]\s*)del\b', _re.I), 'Note: may delete files (Windows cmd)'),
        (_re.compile(r'\bRemove-Item\b.*-Recurse', _re.I), 'Note: may recursively delete files (PowerShell)'),
        (_re.compile(r'\bRemove-Item\b', _re.I), 'Note: may delete files (PowerShell)'),
        (_re.compile(r'\bgit\s+reset\s+--hard\b'), 'Note: may discard uncommitted changes'),
        (_re.compile(r'\bgit\s+push\b.*--force\b'), 'Note: may overwrite remote history'),
        (_re.compile(r'\bgit\s+pull\s+--force\b'), 'Note: git pull --force may overwrite local code'),
        (_re.compile(r'\bgit\s+pull\s+--rebase\b'), 'Note: git pull --rebase may rewrite local commits'),
        (_re.compile(r'\bgit\s+pull\s+-f\b'), 'Note: git pull -f may overwrite local code'),
        (_re.compile(r'\bgit\s+rebase\b'), 'Note: git rebase may rewrite commit history'),
        (_re.compile(r'\bgit\s+pull\b'), 'Note: git pull may overwrite uncommitted local changes — stash first'),
        (_re.compile(r'\b(DROP|TRUNCATE)\s+(TABLE|DATABASE)\b', _re.I), 'Note: may destroy database objects'),
    ]
    for pat, msg in PATTERNS:
        if pat.search(command):
            return msg
    return None


def _extract_affected_paths(command: str) -> List[str]:
    """Extract the REAL file/dir targets of a delete command.

    Used for the permission-card display AND post-execution deletion
    verification, so it must be PRECISE. The old greedy version grabbed
    PowerShell cmdlets ($(...), Write-Output, Get-Date…) as "paths", which
    produced false "[VERIFY] Deletion may have failed" lines that made the AI
    retry the same delete over and over and finally tell the user to delete
    manually.

    Strategy:
      1. Split the command into segments on shell separators (; | && & `n).
      2. Only look at segments that actually contain a delete verb.
      3. From those, take non-flag tokens that name a file/dir.
    Windows backslash paths are preserved (shlex posix=False).
    """
    import re as _re

    _DELETE_VERB = _re.compile(r'\b(rm|rmdir|del|erase|Remove-Item|ri)\b', _re.I)
    SKIP = {
        'rm', 'rmdir', 'del', 'remove-item', 'ri', 'erase', 'git', 'kubectl',
        'terraform', 'reset', 'push', 'clean', 'hard', 'powershell.exe',
        'powershell', 'pwsh', 'cmd', 'cmd.exe', 'sudo',
    }
    _CMDLET = _re.compile(r'^(?:[A-Za-z]+)-(?:[A-Za-z]+)$')   # Verb-Noun cmdlets
    _BAREWORD = _re.compile(r'^[A-Za-z0-9_.\-]{2,}$')

    # 1. Split into segments — only delete segments are inspected.
    segments = _re.split(r';|\|\||&&|\||&|`n|\n', command)
    paths: List[str] = []
    for seg in segments:
        if not _DELETE_VERB.search(seg):
            continue
        # 2. Tokenise this segment, preserving Windows backslashes.
        try:
            import shlex as _shlex
            tokens = _shlex.split(seg, posix=False)
        except ValueError:
            tokens = seg.split()
        for raw in tokens:
            p = raw.strip('"\'').rstrip(';,')
            if not p or p.startswith('-'):
                continue
            if p.lower() in SKIP:
                continue
            # Drop shell noise: expressions, variables, globs.
            if any(ch in p for ch in ('$', '{', '}', '(', ')', '*', '=', '@')):
                continue
            # Drop PowerShell cmdlet names (Write-Output, Get-Date…).
            if _CMDLET.match(p):
                continue
            # Accept real paths: has separator, has extension, or a plain
            # file/dir bareword (build, dist, node_modules, sample.txt).
            if (_re.search(r'[/\\]', p)
                    or _re.search(r'\.[A-Za-z0-9]{1,6}$', p)
                    or _BAREWORD.match(p)):
                if p not in paths:
                    paths.append(p)
    return paths[:5]


def _is_delete_command(command: str) -> bool:
    """True if the command's intent is to DELETE files/dirs (not git reset, DROP, etc.)."""
    import re as _re
    return bool(_re.search(r'\b(rm|rmdir|del|erase|Remove-Item|ri)\b', command, _re.I))


def _build_recycle_bin_command(abs_paths: List[str]) -> str:
    """Build a PowerShell command that sends each path to the Recycle Bin
    (recoverable) instead of permanently deleting it.

    Uses Microsoft.VisualBasic.FileIO.FileSystem, which is the standard Windows
    API for recycle-bin deletion and works for both files and directories.
    """
    # PowerShell single-quote escaping: ' -> ''
    items = ",".join("'" + p.replace("'", "''") + "'" for p in abs_paths)
    return (
        "Add-Type -AssemblyName Microsoft.VisualBasic; "
        f"$__paths = @({items}); "
        "foreach ($__p in $__paths) {{ "
        "  if (Test-Path -LiteralPath $__p) {{ "
        "    if ((Get-Item -LiteralPath $__p -Force) -is [System.IO.DirectoryInfo]) {{ "
        "      [Microsoft.VisualBasic.FileIO.FileSystem]::DeleteDirectory($__p,'OnlyErrorDialogs','SendToRecycleBin') }} "
        "    else {{ "
        "      [Microsoft.VisualBasic.FileIO.FileSystem]::DeleteFile($__p,'OnlyErrorDialogs','SendToRecycleBin') }}; "
        "    Write-Output (\"Moved to Recycle Bin: \" + $__p) }} "
        "  else {{ Write-Output (\"Not found: \" + $__p) }} "
        "}}"
    ).replace("{{", "{").replace("}}", "}")


def _get_current_timestamp() -> str:
    """Get current timestamp in ISO format."""
    from datetime import datetime
    return datetime.now().isoformat()


# ============================================================

class BridgeBashTool:
    """
    Bridge-native Bash tool — real BashTool.py does not exist in
    src/agent/src/tools/BashTool/ (only helper modules).
    """
    name = "Bash"
    description = (
        "Execute a shell / PowerShell command and return its output. "
        "Commands run in the project root by default."
    )
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "Command to run"},
            "timeout": {
                "type": "integer",
                "description": "Timeout in seconds (default 30)",
                "default": 30,
            },
        },
        "required": ["command"],
    }

    def __init__(self, bridge: 'CortexAgentBridge'):
        self._bridge = bridge

    async def execute(self, args: Dict[str, Any]) -> ToolResult:
        import threading as _threading
        command: str = args.get("command", "")
        timeout: int = int(args.get("timeout", 30))
        cwd = self._bridge._get_project_root()

        # ── Block Start-Process that launches persistent servers ────────────
        # The AI often tries to "verify" by starting http.server / live-server
        # via Start-Process. These spawn background processes that never exit,
        # waste time, leak ports, and the system cannot recognize the output
        # as valid verification anyway.  Block them and suggest alternatives.
        _cmd_lower = command.lower()
        if "start-process" in _cmd_lower:
            _server_patterns = [
                "http.server", "http.server", "live-server", "npx serve",
                "npx http-server", "node server", "npm start", "npm run dev",
                "npm run serve", "yarn start", "yarn dev", "python -m http",
                "php -s", "php -S", "flask run", "uvicorn", "gunicorn",
            ]
            if any(pat in _cmd_lower for pat in _server_patterns):
                log.warning(
                    f"[BRIDGE] Blocked Start-Process server command: "
                    f"{command[:150].replace(chr(10), ' ')}"
                )
                return ToolResult(
                    tool_id="", result=None, success=False,
                    error=(
                        "BLOCKED: Do NOT use Start-Process to launch persistent "
                        "servers (http.server, live-server, npm start, etc.). "
                        "These background processes waste time and leak ports.\n\n"
                        "For verification, use DIRECT commands instead:\n"
                        "• python -c \"print(open('index.html').read()[:500])\"\n"
                        "• curl http://localhost:PORT (if server already running)\n"
                        "• Test-Connection localhost -Port PORT\n"
                        "• python -m pytest  (or other test framework)\n"
                        "• npm test\n"
                        "• npx eslint .  (lint checks)\n\n"
                        "The Bash tool has a {timeout}s timeout — servers will "
                        "time out and waste turns.  Use targeted verification instead."
                    ),
                )
        # ───────────────────────────────────────────────────────────────

        # ── Dangerous-command permission gate ────────────────────────────────
        warning = _get_destructive_warning(command)
        if warning and self._bridge.always_allowed:
            log.info(
                "[BRIDGE] Permission gate bypassed (always_allow=True): %s",
                command[:180].replace("\n", " "),
            )
        if warning and not self._bridge.stop_requested and not self._bridge.always_allowed:
            affected = _extract_affected_paths(command)
            import json as _json
            # Create a fresh event for this request
            evt = _threading.Event()
            self._bridge.permission_event = evt
            self._bridge.permission_granted = False
            self._bridge.permission_requested.emit(
                command, warning, _json.dumps(affected)
            )
            # Wait without blocking the event loop
            granted = await asyncio.to_thread(evt.wait, 60.0)  # 60 s timeout
            self._bridge.permission_event = None
            if not granted or not self._bridge.permission_granted:
                return ToolResult(
                    tool_id="", result=None, success=False,
                    error="STOP: The user REJECTED this command. It was NOT executed. "
                          "Do NOT continue with more tools. Do NOT retry this command. "
                          "The user has made their choice — STOP and wait for the user's next message."
                )
        # ───────────────────────────────────────────────────────────────

        # ── Recycle Bin redirect for file deletions ─────────────────────────
        # When the AI deletes files (rm / del / Remove-Item …), route them to
        # the Recycle Bin (recoverable) instead of permanent deletion. We only
        # rewrite when we can confidently resolve the target paths; complex
        # pipelines (no explicit path) run as-is.
        _recycle_targets: List[str] = []
        if warning and _is_delete_command(command):
            _affected = _extract_affected_paths(command)
            for _p in _affected:
                _abs = _p if os.path.isabs(_p) else (os.path.join(cwd, _p) if cwd else _p)
                _abs = os.path.normpath(_abs)
                if os.path.exists(_abs):
                    _recycle_targets.append(_abs)
            if _recycle_targets:
                log.info(
                    "[BRIDGE] Redirecting delete to Recycle Bin (%d target(s)): %s",
                    len(_recycle_targets), ", ".join(os.path.basename(t) for t in _recycle_targets),
                )
                command = _build_recycle_bin_command(_recycle_targets)

        proc = None
        try:
            # Windows: use PowerShell so .ps1 scripts execute correctly
            # (cmd.exe triggers Windows file-association dialog for .ps1).
            # Use subprocess.Popen in a thread to avoid asyncio subprocess
            # hanging in PyInstaller frozen builds on Windows.
            import subprocess as _sp
            import threading as _thr

            def _run_subprocess():
                """Run PowerShell command in a thread using subprocess.Popen."""
                try:
                    p = _sp.Popen(
                        ['powershell.exe', '-ExecutionPolicy', 'Bypass', '-NonInteractive', '-Command', command],
                        stdout=_sp.PIPE,
                        stderr=_sp.PIPE,
                        cwd=cwd,
                        creationflags=_sp.CREATE_NO_WINDOW if sys.platform == 'win32' else 0,
                    )
                    stdout_b, stderr_b = p.communicate(timeout=timeout)
                    return p.returncode, stdout_b, stderr_b
                except _sp.TimeoutExpired:
                    p.kill()
                    try:
                        p.communicate(timeout=3)
                    except _sp.TimeoutExpired:
                        pass
                    return -1, b'', b'Timed out'
                except Exception as e:
                    return -1, b'', str(e).encode()

            loop = asyncio.get_event_loop()
            returncode, stdout_b, stderr_b = await loop.run_in_executor(None, _run_subprocess)

            if returncode == -1 and stderr_b == b'Timed out':
                return ToolResult(tool_id="", result=None, success=False,
                                  error=f"Command timed out after {timeout}s")

            stdout = (stdout_b.decode('utf-8', errors='replace') if stdout_b else "")
            stderr = (stderr_b.decode('utf-8', errors='replace') if stderr_b else "")
            output = stdout
            if stderr:
                output += f"\n[stderr]\n{stderr}"
            
            # ── Post-execution verification for destructive commands ──
            # If the command was destructive, verify whether affected files
            # were actually deleted so the AI knows the true result.
            if warning and returncode == 0:
                affected = _extract_affected_paths(command)
                still_exist = []
                actually_deleted = []
                for fp in affected:
                    if not os.path.isabs(fp) and cwd:
                        fp = os.path.join(cwd, fp)
                    if os.path.exists(fp):
                        still_exist.append(os.path.basename(fp))
                    else:
                        actually_deleted.append(os.path.basename(fp))
                if actually_deleted:
                    # Strong, unambiguous confirmation so the AI does NOT retry
                    # the same delete or fall back to "please delete manually".
                    output += (
                        f"\n[VERIFY] ✓ Deletion confirmed — these files no longer exist: "
                        f"{', '.join(actually_deleted)}. The delete SUCCEEDED; do not run it again."
                    )
                if still_exist:
                    output += (
                        f"\n[VERIFY] These targets still exist: {', '.join(still_exist)}. "
                        f"If you intended to delete them, the path may be wrong — check the "
                        f"exact path with Test-Path before retrying."
                    )
            
            # Tell the AI deletions went to the Recycle Bin (recoverable).
            if _recycle_targets and returncode == 0:
                output += (
                    f"\n[INFO] Deleted file(s) were moved to the Recycle Bin "
                    f"(recoverable), not permanently deleted."
                )

            return ToolResult(
                tool_id="",
                result={
                    "command": command,
                    "stdout": stdout,
                    "stderr": stderr,
                    "returncode": returncode,
                    "output": output or "(no output)",
                    # Abs paths sent to Recycle Bin — dispatch emits a signal so
                    # the editor closes any open tabs for these files.
                    "recycled_paths": _recycle_targets if returncode == 0 else [],
                },
            )

        except asyncio.CancelledError:
            # Task was cancelled — thread subprocess may still be running
            # but result will be discarded by the event loop
            raise

        except Exception as e:
            return ToolResult(tool_id="", result=None, success=False, error=str(e))


class BridgeLSTool:
    """Bridge-native LS tool — no real agent equivalent."""
    name = "LS"
    description = "List the contents of a directory. Shows files and subdirectories."
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Directory path (default: project root)",
                "default": ".",
            },
        },
    }

    def __init__(self, bridge: 'CortexAgentBridge'):
        self._bridge = bridge

    async def execute(self, args: Dict[str, Any]) -> ToolResult:
        dirpath: str = args.get("path", ".")
        if not os.path.isabs(dirpath) and self._bridge.project_root:
            dirpath = os.path.join(self._bridge.project_root, dirpath)
        try:
            entries: List[str] = []
            for entry in sorted(os.scandir(dirpath), key=lambda e: (not e.is_dir(), e.name)):
                marker = "/" if entry.is_dir() else ""
                entries.append(f"{entry.name}{marker}")
            self._bridge.directory_contents.emit(dirpath, "\n".join(entries))
            return ToolResult(tool_id="", result={"path": dirpath, "entries": entries})
        except Exception as e:
            return ToolResult(tool_id="", result=None, success=False, error=str(e))


# ============================================================
# TOOL DEFINITIONS  (OpenAI-compatible function schemas)
# Lean set: 11 core tools (reduced from 17). Rarely-used tools
# (LSP, MCP, TaskCreate/Update/List/Get/Stop, TeamCreate/Delete)
# are removed to minimize context overhead per turn.
# ============================================================

_TOOL_SCHEMAS: List[Dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "Read",
            "description": "Read file contents. Supports text, images, PDFs, Jupyter notebooks.",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {"type": "string", "description": "File path"},
                    "offset":    {"type": "integer", "description": "Start line (1-indexed, optional)"},
                    "limit":     {"type": "integer", "description": "Max lines to read (optional)"},
                },
                "required": ["file_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "Write",
            "description": "Create a new file or overwrite an existing one.",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {"type": "string", "description": "File path"},
                    "content":   {"type": "string", "description": "Full content to write"},
                },
                "required": ["file_path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "Edit",
            "description": "Replace exact string in a file with new text. Set replace_all=true to replace all occurrences.",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path":   {"type": "string", "description": "File path"},
                    "old_string":  {"type": "string", "description": "Exact text to find"},
                    "new_string":  {"type": "string", "description": "Replacement text"},
                    "replace_all": {"type": "boolean", "description": "Replace all occurrences (default false)"},
                },
                "required": ["file_path", "old_string", "new_string"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "Bash",
            "description": "Execute a shell command. Runs in the project root.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Command to run"},
                    "timeout": {"type": "integer", "description": "Timeout in seconds (default 30)"},
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "Glob",
            "description": "Find files matching a glob pattern (e.g. **/*.py).",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Glob pattern"},
                    "path":    {"type": "string", "description": "Directory to search (default: project root)"},
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "Grep",
            "description": "Search file contents with regex. Returns matching lines with file names and line numbers.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern":          {"type": "string",  "description": "Regex/text pattern"},
                    "path":             {"type": "string",  "description": "Directory or file to search"},
                    "glob":             {"type": "string",  "description": "File glob filter e.g. *.py"},
                    "case_insensitive": {"type": "boolean", "description": "Case-insensitive (default false)"},
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "SementicSearch",
            "description": "Search codebase semantically using natural language. Finds code by MEANING, not exact text — ideal for large codebases and conceptual queries like 'authentication flow', 'error handling pattern', 'database migration logic'. Use this when Grep returns too many or too few results, or when you need to understand code behavior rather than find exact strings. First use auto-indexes the project; subsequent searches are fast (<2s).",
            "parameters": {
                "type": "object",
                "properties": {
                    "query":           {"type": "string",  "description": "Natural language query describing what code you're looking for (e.g. 'where is payment processing handled', 'retry mechanism implementation')"},
                    "path":            {"type": "string",  "description": "Directory to search within (default: project root)"},
                    "top_k":           {"type": "integer", "description": "Number of results to return (default: 10, max: 50)"},
                    "min_similarity":  {"type": "number",  "description": "Minimum relevance threshold 0.0-1.0 (default: 0.3). Lower = more results, less precise."},
                    "output_mode":     {"type": "string",  "enum": ["ranked", "content", "files_with_matches"], "description": "Output format: 'ranked' shows scores+titles, 'content' shows snippets, 'files_with_matches' shows only paths"},
                    "file_extension":  {"type": "string",  "description": "Filter by file extension (e.g. 'py', 'js', 'ts')"},
                    "force_reindex":   {"type": "boolean", "description": "Force rebuild the semantic index before searching"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "LS",
            "description": "List directory contents.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Directory path (default: project root)"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "TodoWrite",
            "description": "Update the todo list. Call FIRST for 3+ step tasks. Mark in_progress BEFORE starting, completed AFTER finishing.",
            "parameters": {
                "type": "object",
                "properties": {
                    "todos": {
                        "type": "array",
                        "description": "Todo items.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "id":         {"type": "string",  "description": "Unique task id"},
                                "content":    {"type": "string",  "description": "Imperative form (e.g. 'Run tests')"},
                                "activeForm": {"type": "string",  "description": "Present continuous (e.g. 'Running tests')"},
                                "status":     {"type": "string",  "enum": ["pending", "in_progress", "completed"], "description": "Status"},
                            },
                            "required": ["id", "content", "activeForm", "status"],
                        },
                    },
                },
                "required": ["todos"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "AskUserQuestion",
            "description": "Ask the user a multiple-choice question to gather preferences or clarify requirements.",
            "parameters": {
                "type": "object",
                "properties": {
                    "questions": {
                        "type": "array",
                        "description": "Questions (1-4).",
                        "items": {
                            "type": "object",
                            "properties": {
                                "question":    {"type": "string", "description": "The question"},
                                "header":      {"type": "string", "description": "Short label (max 12 chars)"},
                                "multiSelect": {"type": "boolean", "description": "Allow multi-select (default false)"},
                                "options": {
                                    "type": "array",
                                    "description": "2-4 options",
                                    "items": {
                                        "type": "object",
                                        "properties": {
                                            "label":       {"type": "string", "description": "Option label"},
                                            "description": {"type": "string", "description": "What this option means"},
                                        },
                                        "required": ["label", "description"],
                                    },
                                },
                            },
                            "required": ["question", "header", "options"],
                        },
                    },
                },
                "required": ["questions"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "WebFetch",
            "description": "Fetch and extract content from a URL as markdown.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url":   {"type": "string", "description": "The URL"},
                    "query": {"type": "string", "description": "Optional search query for the page"},
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "WebSearch",
            "description": "Search the web. Returns titles, URLs, and snippets.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query":           {"type": "string", "description": "Search query"},
                    "allowed_domains": {"type": "array", "items": {"type": "string"}, "description": "Restrict to these domains"},
                    "blocked_domains": {"type": "array", "items": {"type": "string"}, "description": "Exclude these domains"},
                },
                "required": ["query"],
            },
        },
    },
]


def _get_tool_definitions() -> List[Dict[str, Any]]:
    """
    Return OpenAI-compatible tool definitions.

    Returns built-in schemas directly.  The tool_registry.py stub does not
    provide a functional get_all_base_tools, so registry-based loading is
    skipped to avoid false warnings.
    """
    return list(_TOOL_SCHEMAS)


def _tool_name_from_schema(tool_def: Dict[str, Any]) -> str:
    """Extract tool function name from OpenAI-compatible schema entry."""
    fn_any = tool_def.get("function")
    fn = cast(Dict[str, Any], fn_any) if isinstance(fn_any, dict) else None
    if isinstance(fn, dict):
        name = fn.get("name")
        if isinstance(name, str):
            return name
    return ""


def _filter_tool_definitions(
    tool_defs: List[Dict[str, Any]],
    allowed_names: Set[str],
) -> List[Dict[str, Any]]:
    """Return only tool schemas whose function name is in allowed_names."""
    if not allowed_names:
        return []
    out: List[Dict[str, Any]] = []
    for td in tool_defs:
        if _tool_name_from_schema(td) in allowed_names:
            out.append(td)
    return out


# ============================================================
# UI SIGNAL ROUTING
# The bridge emits tool_activity(tool_name, info, status).
# Status values expected by script.js:
#   "running"   → shows spinner card
#   "complete"  → marks card OK  (NOT "completed" — that was the old bug)
#   "error"     → marks card red
# ============================================================

_TOOL_TO_ACTIVITY_NAME: Dict[str, str] = {
    "Read":      "read_file",
    "Write":     "write_file",
    "Edit":      "edit_file",
    "Bash":      "run_command",
    "PowerShell": "run_command",
    "Glob":      "list_directory",
    "TodoWrite": "todo_write",
    "Grep":  "grep",
    "LS":    "list_directory",
    "SementicSearch": "semantic_search",
    "WebSearch":  "web_search",
    "WebFetch":   "web_fetch",
    "TeamCreate": "team_create",
    "TeamDelete": "team_delete",
    "TaskCreate": "task_create",
    "TaskUpdate": "task_update",
    "TaskList":   "task_list",
    "TaskGet":    "task_get",
    "TaskStop":   "task_stop",
}

# Tools that trigger the "create_file" UI card (Write on a new file)
_CREATE_TOOL_NAMES = {"Write"}


# ============================================================
# AGENT WORKER THREAD
# ============================================================

class AgentWorker(QThread):
    """
    Background thread running the async agentic loop.
    Prevents UI thread from blocking during long LLM calls.
    """

    response_ready  = pyqtSignal(str)
    chunk_ready     = pyqtSignal(str)
    error_occurred  = pyqtSignal(str)
    thinking_started = pyqtSignal()
    thinking_stopped = pyqtSignal()

    def __init__(self, bridge: 'CortexAgentBridge'):
        super().__init__()
        self.bridge = bridge
        self._is_running  = False
        self._stop_req    = False
        self._queue: Optional[asyncio.Queue[WorkerMessage]] = None
        self._loop:  Optional[asyncio.AbstractEventLoop] = None
        # Tracks the asyncio.Task currently running _handle_chat.
        # Assigned right after asyncio.create_task(); used by stop_generation()
        # (via stop_session_task) to cancel mid-execution.
        self._current_chat_task: Optional[asyncio.Task[Any]] = None

    # ── QThread entry ──────────────────────────────────────────

    def run(self):
        self._is_running = True
        # ── FIX: Use SelectorEventLoop on Windows ──
        # Python 3.14 Windows ProactorEventLoop (IOCP-based) crashes with
        # "Windows fatal exception: access violation" in _poll. This is a
        # known CPython bug where the IOCP handle gets corrupted when the
        # event loop is destroyed (close/exit) while IOCP operations are
        # pending. SelectorEventLoop uses select() which is safe.
        if sys.platform == 'win32':
            try:
                self._loop = asyncio.SelectorEventLoop()
            except Exception:
                self._loop = asyncio.new_event_loop()
        else:
            self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._process_queue())
        except Exception as exc:
            log.error(f"[WORKER] Thread error: {exc}")
            self.error_occurred.emit(str(exc))
        except SystemExit:
            pass  # interpreter shutting down
        finally:
            # Cancel all pending tasks WITHOUT gathering (run_until_complete
            # in the finally block triggers Python 3.14 Windows _poll crash).
            try:
                pending = asyncio.all_tasks(self._loop)
                for task in pending:
                    task.cancel()
            except Exception:
                pass
            # Close loop directly — cancelled tasks will be garbage collected.
            # Do NOT call run_until_complete here — it can segfault on Windows
            # when the event loop's internal _poll references destroyed sockets.
            try:
                if self._loop is not None and self._loop.is_running():
                    self._loop.call_soon_threadsafe(self._loop.stop)
                # Small delay to let pending I/O settle before closing
                import time
                time.sleep(0.05)
            except Exception:
                pass
            try:
                if self._loop is not None and not self._loop.is_closed():
                    self._loop.close()
            except Exception:
                pass
            self._loop = None
            self._is_running = False

    # ── Message queue ──────────────────────────────────────────

    async def _process_queue(self):
        """
        Event loop for the agent worker thread.

        Architecture (converted from LocalMainSessionTask.ts startBackgroundSession):
          - Each chat message creates a cancellable asyncio.Task (_handle_chat).
          - While the task runs we concurrently watch the queue for a stop/new-chat
            message using asyncio.wait(FIRST_COMPLETED).  This is the Python
            equivalent of the TS AbortController.abort() / kill() pattern —
            CancelledError propagates through every await in the call chain,
            including mid-tool-execution.
          - Only _is_running=False (set by AgentWorker.stop()) exits the outer loop.
        """
        self._queue = asyncio.Queue[WorkerMessage]()

        while self._is_running:
            # ── Phase 1: Wait for the next queued message ─────────────────────
            try:
                msg: WorkerMessage = await asyncio.wait_for(self._queue.get(), timeout=0.1)
            except asyncio.TimeoutError:
                continue
            except Exception as exc:
                log.error(f"[WORKER] Queue error: {exc}")
                self.error_occurred.emit(str(exc))
                continue

            if msg.get("type") == "stop":
                # stop_generation() already called task.cancel() via
                # stop_session_task(); this message is just a queue flush.
                log.info("[WORKER] Stop message received (task cancel already in flight)")
                continue

            if msg.get("type") != "chat":
                continue

            # ── Phase 2: Run the chat task (cancellable) ──────────────────────
            self._current_chat_task = asyncio.create_task(
                self._handle_chat(msg)
            )

            # Register the asyncio.Task in the session registry so that
            # stop_session_task() (called from the Qt main thread) can cancel it.
            task_id_raw = msg.get("task_id")
            task_id = task_id_raw if isinstance(task_id_raw, str) and task_id_raw else None
            if task_id and self._current_chat_task is not None:
                self.bridge.link_worker_task(task_id, self._current_chat_task)

            # ── Phase 3: Concurrently watch task + queue ──────────────────────
            # Mirrors the TS pattern: the running query holds an AbortSignal;
            # a stop command triggers abort() → CancelledError here.
            while self._is_running and not self._current_chat_task.done():
                get_fut: asyncio.Task[WorkerMessage] = asyncio.ensure_future(self._queue.get())
                try:
                    done, _ = await asyncio.wait(
                        {get_fut, self._current_chat_task},
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                except Exception as exc:
                    get_fut.cancel()
                    log.error(f"[WORKER] asyncio.wait error: {exc}")
                    break

                # ── Task finished naturally ────────────────────────────────
                if self._current_chat_task in done:
                    get_fut.cancel()
                    break

                # ── New queue message arrived while task running ───────────
                if get_fut in done:
                    try:
                        next_msg: WorkerMessage = get_fut.result()
                    except Exception:
                        continue

                    if next_msg.get("type") == "stop":
                        log.info(
                            "[WORKER] Stop message received while running — cancelling chat task"
                        )
                        await self._cancel_active_task()
                        break

                    elif next_msg.get("type") == "chat":
                        # New prompt arrived before the old one finished.
                        # Cancel old, then re-queue the new message so the
                        # outer loop starts it fresh.
                        log.info(
                            "[WORKER] New chat arrived while running — cancelling old task and re-queuing new one"
                        )
                        await self._cancel_active_task()
                        await self._queue.put(next_msg)
                        break
                    # else: ignore unknown message types while running

            # ── Phase 4: Await final cleanup ──────────────────────────────────
            if (
                self._current_chat_task is not None
                and not self._current_chat_task.done()
            ):
                try:
                    await self._current_chat_task
                except (asyncio.CancelledError, Exception):
                    pass
            self._current_chat_task = None

    async def _cancel_active_task(self) -> None:
        """Cancel the current chat asyncio.Task and wait for cleanup."""
        task = self._current_chat_task
        if task is None or task.done():
            self._current_chat_task = None
            return
        log.info("[WORKER] Cancelling active chat task")
        task.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=5.0)
        except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
            pass
        self._current_chat_task = None
        log.info("[WORKER] Active chat task cancelled")

    async def _handle_chat(self, msg: WorkerMessage):
        self.thinking_started.emit()
        try:
            response = await self.bridge.call_llm(
                msg.get("content", ""),
                msg.get("context", {}),
                msg.get("images", []),
            )
            # ── Auto-continue loop ──────────────────────────────────────
            # When the turn budget exhausts with pending todos but progress
            # was made, _call_llm sets _auto_continue_requested = True and
            # stores compacted messages. Silently re-enter _call_llm so the
            # user never sees a manual "Continue" button unless auto-continue
            # is truly exhausted.
            _auto_continue_guard = 0
            while getattr(self.bridge, '_auto_continue_requested', False):
                _auto_continue_guard += 1
                if _auto_continue_guard > 10:  # Safety limit
                    log.warning("[WORKER] Auto-continue safety limit reached — breaking")
                    break
                # Build continuation message from pending todos
                _pending = [
                    t for t in (self.bridge.current_todos or [])
                    if str(t.get('status', '')).upper() in ('PENDING', 'IN_PROGRESS')
                ]
                _lines = ["Continue the task. Remaining todos:"]
                for _t in _pending:
                    _lines.append(f"- {_t.get('content', _t.get('description', ''))}")
                # Include tool availability so the agent knows which tools are usable
                _disabled_set = getattr(self.bridge, '_disabled_tools', set())
                _disabled_list = sorted(_disabled_set) if _disabled_set else []
                _tool_info = ''
                if _disabled_list:
                    _tool_info = (
                        f'\n\nNote: Some tools have been used extensively: {", ".join(_disabled_list)}. '
                        f'You may still use them if needed for specific information.'
                    )
                _continuation_msg = '\n'.join(_lines) + _tool_info + (
                    '\n\n[Conversation was auto-compacted and saved to MEMORY.md. '
                    'Continue from your checkpoint above. Re-read files if needed to verify context.]'
                )
                log.info(f"[WORKER] Auto-continuing (cycle {self.bridge._auto_continue_cycle}) with continuation message ({len(_continuation_msg)} chars)")
                response = await self.bridge.call_llm(
                    _continuation_msg,
                    msg.get("context", {}),
                    msg.get("images", []),
                )
            # ── INDUSTRY-STANDARD: Task Completion Validation ───────────
            # Before emitting response_ready (which triggers Windows notification),
            # verify that ALL todos are actually completed with mutations.
            # This prevents premature notifications when AI tries to skip work.
            
            _should_notify = True
            _pending_count = 0
            # Use PERSISTENT session counter, not per-turn counter
            _mutation_count = self.bridge.session_mutation_count
            
            # ── CRITICAL: Check if todos were auto-cancelled (incomplete) ──
            if getattr(self.bridge, 'todos_auto_cancelled', False):
                _should_notify = False
                msg_text = (
                    "[WORKER] Notification BLOCKED: Todos were auto-cancelled (not completed). "
                    f"AI made {_mutation_count} mutation(s) but tasks were NOT finished."
                )
                log.warning(msg_text)
                self.bridge.allow_notification = False
                self.bridge.todos_auto_cancelled = False  # Reset for next time
            
            # Check if there are pending todos
            elif self.bridge.current_todos:
                _pending_todos = [
                    t for t in self.bridge.current_todos
                    if t.get("status") not in ("COMPLETE", "CANCELLED")
                ]
                _pending_count = len(_pending_todos)
                
                # If there are pending todos, DO NOT show notification
                if _pending_count > 0:
                    _should_notify = False
                    log.warning(
                        "[WORKER] Suppressing notification: " +
                        f"{_pending_count} todo(s) still pending. " +
                        f"AI made {_mutation_count} mutation(s) but tasks incomplete."
                    )
                
                # Even if todos marked complete, check if mutations match
                elif _pending_count == 0 and _mutation_count == 0:
                    # All marked complete but zero mutations = AI skipped work
                    _should_notify = False
                    log.warning(
                        "[WORKER] Suppressing notification: Todos marked complete but NO mutations made. " +
                        "AI tried to skip work!"
                    )
            
            # Always emit response_ready (even if response is empty text) so
            # on_complete → onComplete() → _onGenerationComplete() always fires
            # in JS, which drains the message queue and un-sticks any 'Continue'
            # message that was enqueued while _isGenerating was still True.
            if not self.bridge.stop_requested:
                # Attach task completion metadata to response for notification control
                self.response_ready.emit(response or "")
                
                if _should_notify:
                    log.info(
                        f"[WORKER] Notification allowed: {_mutation_count} mutation(s), " +
                        f"{_pending_count} pending todo(s) — task genuinely complete."
                    )
                    # Set flag for main_window to check before showing notification
                    self.bridge.allow_notification = True
                else:
                    # Block notification - tasks incomplete
                    self.bridge.allow_notification = False
                    log.info(
                        f"[WORKER] Notification BLOCKED: {_pending_count} pending todo(s), " +
                        f"{_mutation_count} mutation(s) — AI must complete remaining tasks."
                    )
        except asyncio.CancelledError:
            # Task was cancelled via asyncio.Task.cancel() from stop_session_task().
            # This is an intentional stop — do NOT emit error_occurred.
            log.info("[WORKER] Chat task cancelled (CancelledError) — stop was requested")
            # CRITICAL: Emit response_complete so chat panel resets generating state.
            # Without this, the UI stays stuck in "generating" mode after stop.
            try:
                self.response_ready.emit("")
            except Exception:
                pass
            raise  # Re-raise so asyncio correctly marks the task as cancelled
        except Exception as exc:
            if not self.bridge.stop_requested:
                _exc_str = str(exc).lower()
                # Provide user-friendly error messages for common issues
                _is_connection_err = any(kw in _exc_str for kw in (
                    '10054', 'connection', 'forcibly closed', 'reset',
                    'broken pipe', 'connection aborted', 'remote host',
                ))
                _is_http_400 = 'http 400' in _exc_str or 'bad request' in _exc_str
                
                if _is_connection_err:
                    _user_msg = (
                        "Connection to AI provider was lost. This is usually a temporary network issue. "
                        "Please try again or switch to a different model."
                    )
                    log.error(f"[WORKER] Connection error: {exc}")
                    self.error_occurred.emit(_user_msg)
                elif _is_http_400:
                    _user_msg = (
                        "AI provider returned an error (HTTP 400). The server may be temporarily unavailable. "
                        "Please try again or switch to a different model."
                    )
                    log.error(f"[WORKER] HTTP 400 error: {exc}")
                    self.error_occurred.emit(_user_msg)
                else:
                    log.error(f"[WORKER] Chat error: {exc}")
                    self.error_occurred.emit(str(exc))
            else:
                log.info(f"[WORKER] Exception during stopped chat (suppressed): {exc}")
        finally:
            self.thinking_stopped.emit()

    def queue_message(self, msg: WorkerMessage):
        if self._queue and self._loop:
            asyncio.run_coroutine_threadsafe(self._queue.put(msg), self._loop)

    def stop(self):
        self._stop_req   = True
        self._is_running = False
        self.wait()


# ============================================================
# MAIN BRIDGE CLASS
# ============================================================

class CortexAgentBridge(QObject):
    """
    Bridge between Cortex IDE UI and the agentic core.

    Signals (matching StubAIAgent interface so ai_chat.py works unchanged):
        response_chunk      — streaming text token
        response_complete   — full response text when done
        request_error       — error string
        file_generated      — (filepath, content) when Write tool runs
        file_edited_diff    — (filepath, old, new) when Edit tool runs
        tool_activity       — (tool_name, info, status) real-time card updates
        directory_contents  — (path, entries) when LS runs
        thinking_started / thinking_stopped
        todos_updated       — (todos_list, main_task)
        tool_summary_ready  — dict summary
        user_question_requested — (question, options)
    """

    # ── PyQt signals ───────────────────────────────────────────
    response_chunk          = pyqtSignal(str)
    response_complete       = pyqtSignal(str)
    request_error           = pyqtSignal(str)
    file_generated          = pyqtSignal(str, str)
    file_edited_diff        = pyqtSignal(str, str, str)
    tool_activity           = pyqtSignal(str, str, str)   # name, info, status
    directory_contents      = pyqtSignal(str, str)
    thinking_started        = pyqtSignal()
    thinking_stopped        = pyqtSignal()
    todos_updated           = pyqtSignal(list, str)
    plan_created            = pyqtSignal(str)   # plan_json — emitted when PlanBuild completes
    plan_step_updated       = pyqtSignal(str, str, str)  # plan_id, step_id, status
    tool_summary_ready      = pyqtSignal(dict)
    user_question_requested = pyqtSignal(dict)  # Full payload: {"tool_call_id": str, "question": str, "type": str, "choices": list, "default": str, "details": str, "scope": str, "tool_name": str}
    # Permission request — emitted before a dangerous bash command runs.
    # JS shows an Accept/Reject card; Python waits via threading.Event.
    permission_requested = pyqtSignal(str, str, str)  # command, warning, files_json
    file_edit_permission_requested = pyqtSignal()     # first file edit this session
    project_access_requested = pyqtSignal()            # initial project access permission
    # File operation cards — show animated cards during create/edit operations
    file_creating_started = pyqtSignal(str)  # file_path
    file_editing_started = pyqtSignal(str)   # file_path
    file_operation_completed = pyqtSignal(str, str, str, str)  # card_id, file_path, content, op_type
    # Recovery signals — context compaction / turn-limit continuation
    agent_status_update = pyqtSignal(str, str)  # type ('compacting'|'retrying'|'failover'), message
    turn_limit_hit      = pyqtSignal(list, str)  # pending todos, context summary
    # Token budget signal — (used_tokens, budget_tokens, provider_name)
    context_budget_update = pyqtSignal(int, int, str)
    # MiMo thought/explore card integration signals
    thoughtDelta  = pyqtSignal(str)    # streaming reasoning text chunks
    thoughtStart  = pyqtSignal()       # first reasoning chunk arrives
    thoughtEnd    = pyqtSignal()       # reasoning ends, response text starts
    exploreFile   = pyqtSignal(str)    # JSON: {"path", "lines", "tool"} after tool result
    exploreStart  = pyqtSignal()       # first tool call begins
    exploreEnd    = pyqtSignal()       # all tools resolved, response streaming starts
    # ── INDUSTRY-STANDARD: Progress tracking signal ───────────
    # Emits (completed, total, percentage, status_message)
    # Allows UI to show progress bar: "3/5 tasks complete (60%)"
    task_progress_update = pyqtSignal(int, int, int, str)
    # File tree refresh — emitted after file create/write/delete so sidebar refreshes
    file_tree_refresh_needed = pyqtSignal(str)  # optional path hint (directory to refresh)
    files_deleted_by_agent   = pyqtSignal(list)  # abs paths the agent sent to Recycle Bin — close their editor tabs
    session_saved_to_memory  = pyqtSignal(bool)  # True=summary saved to MEMORY.md — New Chat flow can now clear

    # ── Internal state ──────────────────────────────────────────
    def __init__(self, **kwargs: Any):
        super().__init__()
        # Pre-warm ripgrep so Windows Defender caches trust on first launch
        try:
            from src.agent.src.tools.GrepTool.GrepTool import prewarm_ripgrep
            prewarm_ripgrep()
        except Exception:
            pass
        self._project_root: Optional[str] = None
        self._active_file:  Optional[str] = None
        self._cursor_pos:   Optional[int] = None
        self._terminal      = None
        self._ui_parent     = None
        self._lsp_manager   = None
        self._always_allowed: bool = False
        self._file_edit_permission: Optional[str] = None  # None=not asked, "once", "always", "rejected"
        self._file_edit_permission_event: Optional[threading.Event] = None
        self._project_access_granted: bool = False  # Single initial permission gate
        self._project_access_event: Optional[threading.Event] = None
        self._interaction_mode: str = "default"
        self._conversation_history: List[ChatMessage] = []
        self._history_lock = threading.Lock()  # Protects _conversation_history from concurrent access
        self._last_turn_reasoning: str = ""  # Preserved from final agentic turn for MiMo/DeepSeek
        self._current_conversation_id: Optional[str] = None  # Active chat ID for direct DB saves
        self._enhancement_data: Dict[str, Any] = {}
        self._streaming      = None
        self._current_todos: List[Dict[str, Any]] = []   # Persisted todo list for TodoWrite
        self._pending_questions: Dict[str, Any] = {}  # Pending AskUserQuestion items
        # ── Stale-continue detection ──────────────────────────────────
        # Track how many times the same set of todos survived a Continue cycle
        # without any progress.  After _MAX_STALE_CYCLES, auto-cancel them.
        self._continue_cycle_count: int = 0
        self._last_pending_ids: set[str] = set()
        self._MAX_STALE_CYCLES: int = 2  # Auto-cancel after 2 stale cycles (was 3)
        # ── Auto-continue: silently compact + restart when turn budget exhausts ──
        # Modern IDEs (Cursor, Copilot) auto-compact and continue without user
        # intervention. Only show manual "Continue" button as a last resort.
        self._MAX_AUTO_CONTINUE_CYCLES: int = 5  # Max auto-continue cycles before falling back to manual
        self._auto_continue_cycle: int = 0  # Current auto-continue cycle count (per _call_llm)
        self._last_cycle_session_mutations: int = 0  # Session mutation count at start of last cycle
        self._auto_continue_compacted: Optional[List[Any]] = None  # Pre-compacted messages for auto-continue
        self._auto_continue_requested: bool = False  # True when _call_llm wants auto-continue
        self._chat_context_restored: bool = False  # Guard: only restore DB chat messages once per session
        self._consecutive_bash_turns: int = 0  # DEPRECATED — kept for backward compat, see _bash_usage_history
        # Guard against "plan-only" loops where TodoWrite repeats without real action.
        self._todo_write_streak: int = 0
        self._last_todo_signature: str = ""
        self._mutation_success_count: int = 0
        self._last_todo_mutation_count: int = -1
        # ── Persistent session mutation counter ─────────────────────
        # This survives across call_llm calls to track TOTAL mutations
        # made by the AI throughout the entire session (not just one message)
        self._session_mutation_count: int = 0
        self._stop_requested: bool = False  # Set to interrupt the streaming loop
        # ── Rejection tracking — after N rejections, AI stops and asks user ──
        self._rejection_counts: Dict[str, int] = {}  # file_path -> rejection count
        self._pending_rejection_nudges: List[str] = []  # Nudges to inject on next turn
        self._MAX_REJECTIONS = 2  # Stop after this many rejections per file
        # ── Accept nudges — tell AI the file state changed after user accepts ──
        self._pending_accept_nudges: List[str] = []  # Injected on next turn
        # ── Deferred edits — accumulates multiple edits to the same file ──
        self._deferred_edits: Dict[str, str] = {}  # normalized_path -> full file content
        # ── Agentic turn tracking — True while AI is processing a turn loop ──
        # Used by warmup flush to avoid opening files during AI editing.
        self._agentic_turn_active: bool = False
        # ── Notification control flag ────────────────────────────────
        # Set by worker thread to control whether Windows notification should show.
        # Only True when ALL todos are genuinely completed with mutations.
        self._allow_notification: bool = True  # Default: allow (for simple Q&A with no todos)
        # ── AutoGen multi-agent toggle state ────────────────────────
        # Toggled via the Multi-Agent banner in the model dropdown.
        # When enabled, the message router prefers the CoordinationEngine
        # multi-agent path (Performance/Ultimate mode) over single-agent.
        self._autogen_enabled: bool = False
        # ── Auto-cancel tracking ─────────────────────────────────────
        # Tracks if todos were auto-cancelled (incomplete) vs genuinely completed
        self._todos_auto_cancelled: bool = False
        # Persistent memory dir — computed once per project root
        self._memory_dir: Optional[str] = None
        # Permission gate — used by BridgeBashTool to pause until user accepts/rejects
        self._permission_event: Optional[threading.Event] = None   # lazily created
        self._permission_granted: bool = False
        # Session task registry — converted from AppStateStore.ts tasks map.
        # Tracks the active asyncio.Task for proper cancellation on stop.
        self._task_registry: SessionTaskRegistry = SessionTaskRegistry()

        # ── Circuit breaker & Tool Execution Engine (Phase B) ───────────
        self._tool_circuit_breaker: ToolCircuitBreaker = ToolCircuitBreaker(
            threshold=5,
            repetitive_limit=50,  # Allow 50 calls per tool before repetitive-limit kicks in (was 4, which killed Write after 4 edits)
        )
        self._tool_executor: ToolExecutionEngine = ToolExecutionEngine(self._tool_circuit_breaker)
        # Legacy aliases for backward compatibility
        self._tool_fail_counts: Dict[str, int] = self._tool_circuit_breaker._fail_counts  # pyright: ignore[reportPrivateUsage]
        self._disabled_tools: Set[str] = self._tool_circuit_breaker._disabled_tools  # pyright: ignore[reportPrivateUsage]
        self._tool_total_calls: Dict[str, int] = self._tool_circuit_breaker._total_calls  # pyright: ignore[reportPrivateUsage]
        self._session_tasks: Dict[str, Dict[str, Any]] = {}  # task_id -> task payload
        self._teams: Dict[str, Dict[str, Any]] = {}  # team_id -> team payload

        # ── Test verification state ────────────────────────────────────────
        # Stores recent tool results for verification enforcement.
        # Each entry: (tool_name: str, success: bool, content_preview: str, exit_code: Optional[int])
        self._recent_tool_results: List[Tuple[str, bool, str, Optional[int]]] = []
        self._max_recent_results: int = 20  # Keep last 20 tool results
        # Detected test framework (populated lazily on first check)
        self._test_framework: Optional[str] = None
        self._test_framework_checked: bool = False

        # ── Self-healing debug loop state ─────────────────────────────────
        from src.core.debug_loop import DebugLoop
        self._debug_loop: DebugLoop = DebugLoop()
        # ── Agent Safety Guard (industry-standard protections) ──
        self._safety = AgentSafetyGuard()

        # ── Sandbox execution manager (Phase 7) ────────────────────────────
        # Lazy-initialized on first call via property
        self._sandbox_manager: Optional[Any] = None

        # ── Hierarchical task graph ───────────────────────────────────────
        self._task_graph: TaskGraph = TaskGraph()

        # ── Usage Tracker (profile + usage stats for Settings UI) ─────────────
        try:
            self._usage_tracker = get_usage_tracker()
            log.info("[BRIDGE] UsageTracker initialized (singleton)")
        except Exception as _ut_err:
            log.warning(f"[BRIDGE] UsageTracker init failed: {_ut_err}")
            self._usage_tracker = None

        log.info("[BRIDGE] Initialising Cortex Agent Bridge")

        # Initialise real agent bootstrap state
        self._init_agent_state()

        # Build tool context for real agent tools (use model from settings if available)
        _initial_model: str = 'mistral-large-latest'
        _ai_map: Optional[Dict[str, Any]] = None
        try:
            from src.config.settings import get_settings
            _raw_settings: Any = get_settings()
            _raw_ai: Any = _raw_settings.get('ai')
            _raw_model: Optional[str] = None
            if isinstance(_raw_ai, dict):
                _ai_map = cast(Dict[str, Any], _raw_ai)
                # Prefer "model_id" (runtime setting from dropdown), fall back to "model" (legacy)
                _candidate_id = _ai_map.get('model_id')
                if isinstance(_candidate_id, str) and _candidate_id:
                    _raw_model = _candidate_id
                else:
                    _candidate = _ai_map.get('model')
                    if isinstance(_candidate, str) and _candidate:
                        _raw_model = _candidate
            if _raw_model and _ai_map is not None:
                _initial_model = _raw_model
                # Also seed enhancement_data so model routing works even before
                # the model_changed signal fires (e.g., model persisted from prior session).
                self._enhancement_data["model_id"] = _raw_model
                _provider: Optional[str] = _ai_map.get('provider')
                if isinstance(_provider, str) and _provider:
                    self._enhancement_data["provider"] = _provider
        except Exception:
            pass
        self._tool_ctx = CortexToolContext(self, _initial_model)
        self._current_model_id: str = _initial_model

        # Instantiate real FileReadTool (needs instance for file-state cache)
        self._real_read_tool = None
        if _REAL_FILE_READ_TOOL is not None:
            try:
                self._real_read_tool = _REAL_FILE_READ_TOOL()
                log.info("[BRIDGE] FileReadTool instance created")
            except Exception as _e:
                log.warning(f"[BRIDGE] Could not instantiate FileReadTool: {_e}")

        # Bridge-native tools (Bash + LS — no real agent equivalents)
        self._bash_tool = BridgeBashTool(self)
        self._ls_tool   = BridgeLSTool(self)

        # Connect to Cortex streaming emitter
        self._connect_streaming()

        # Start background worker
        self._worker = AgentWorker(self)
        self._connect_qt_signal(self._worker.response_ready, self._on_response_ready)
        self._connect_qt_signal(self._worker.chunk_ready, self._on_chunk_ready)
        self._connect_qt_signal(self._worker.error_occurred, self._on_error)
        self._connect_qt_signal(self._worker.thinking_started, self.thinking_started.emit)
        self._connect_qt_signal(self._worker.thinking_stopped, self.thinking_stopped.emit)
        self._worker.start()

        # ── Phase 4: Auto-resume from previous interrupted session ────────
        _resume_requested = "--resume" in sys.argv
        _no_resume_requested = "--no-resume" in sys.argv
        if not _no_resume_requested and (_resume_requested or (Path.home() / ".cortex" / "agent_state.json").exists()):
            try:
                from src.core.agent_session_manager import load_snapshot
                snapshot = load_snapshot()
                if snapshot:
                    self._hydrate_from_snapshot(snapshot)
                    log.info("[SESSION] Session restored from snapshot")
            except Exception as _ses_exc:
                log.warning(f"[SESSION] Auto-resume failed: {_ses_exc}")

        log.info("[BRIDGE] Agent bridge ready")
        
        # Pre-warm provider registry at startup to avoid 2s delay on first message
        try:
            from src.ai.providers import get_provider_registry
            get_provider_registry()
            log.info("[BRIDGE] Provider registry pre-warmed")
        except Exception as e:
            log.warning(f"[BRIDGE] Provider pre-warm failed (will lazy-init): {e}")

        # Initialize file_edit_notification signal for WebChannel
        self.file_edit_notification = pyqtSignal(str, str, str)  # filePath, editType, status
        log.info("[BRIDGE] file_edit_notification signal initialized")

    @property
    def allow_notification(self) -> bool:
        """Whether Windows notification should show after task completion."""
        return self._allow_notification

    @allow_notification.setter
    def allow_notification(self, value: bool) -> None:
        self._allow_notification = value

    # ── AutoGen multi-agent toggle ──────────────────────────────────

    def get_autogen_status(self) -> Dict[str, Any]:
        """Return current AutoGen multi-agent toggle state.

        Called by main_window._on_toggle_autogen() to read the current
        state before flipping it.
        """
        return {"enabled": self._autogen_enabled}

    def enable_autogen(self, enabled: bool) -> None:
        """Enable or disable AutoGen multi-agent mode.

        When enabled, the message router in ai_chat.py prefers the
        CoordinationEngine multi-agent path (Performance/Ultimate mode)
        over the single-agent agent_bridge path.

        Called by main_window._on_toggle_autogen() on each toggle click.
        """
        self._autogen_enabled = bool(enabled)
        log.info(
            f"[BRIDGE] AutoGen multi-agent mode {'' if enabled else 'DIS'}ABLED"
        )

    def is_autogen_enabled(self) -> bool:
        """Return True if AutoGen multi-agent mode is currently enabled.

        Used by the MAF multi-agent orchestrator and message router to
        decide whether to run the Planner → Executor → Reviewer pipeline.
        """
        return self._autogen_enabled

    @staticmethod
    def _connect_qt_signal(signal: Any, slot: Callable[..., Any]) -> None:
        """Typed wrapper for Qt signal connections (PyQt stubs expose partial Unknown here)."""
        signal.connect(slot)

    def link_worker_task(self, task_id: str, task: asyncio.Task[Any]) -> None:
        """Link worker-created asyncio task to the registered session task state."""
        ts = self._task_registry.get(task_id)
        if ts is not None:
            ts.asyncio_task = task

    # ── Public property accessors for protected attributes ─────────────────
    # These provide controlled access to internal state from tool classes
    
    @property
    def project_root(self) -> Optional[str]:
        """Get the project root path."""
        return self._project_root
    
    @property
    def always_allowed(self) -> bool:
        """Check if tools are always allowed without permission."""
        return self._always_allowed
    
    @property
    def stop_requested(self) -> bool:
        """Check if stop has been requested."""
        return self._stop_requested
    
    @property
    def permission_event(self) -> Optional[threading.Event]:
        """Get the current permission event."""
        return self._permission_event
    
    @permission_event.setter
    def permission_event(self, value: Optional[threading.Event]) -> None:
        """Set the permission event."""
        self._permission_event = value
    
    @property
    def permission_granted(self) -> bool:
        """Check if permission was granted."""
        return self._permission_granted
    
    @permission_granted.setter
    def permission_granted(self, value: bool) -> None:
        """Set permission granted status."""
        self._permission_granted = value

    @property
    def session_mutation_count(self) -> int:
        """Get the persistent session mutation counter."""
        return self._session_mutation_count

    @property
    def todos_auto_cancelled(self) -> bool:
        """Check if todos were auto-cancelled (incomplete)."""
        return self._todos_auto_cancelled

    @todos_auto_cancelled.setter
    def todos_auto_cancelled(self, value: bool) -> None:
        """Set whether todos were auto-cancelled."""
        self._todos_auto_cancelled = value

    @property
    def current_todos(self) -> List[Dict[str, Any]]:
        """Get the current todo list."""
        return self._current_todos

    # ── Initialisation helpers ─────────────────────────────────

    def _init_agent_state(self):
        """Wire into the real agent bootstrap/state module."""
        try:
            cwd = os.getcwd()
            # CRITICAL: NEVER use Program Files as project root — causes UAC prompts
            # when AI agent tries to edit files in the installed directory
            if 'Program Files' in cwd:
                log.warning(
                    f"[BRIDGE] CWD is in Program Files ({cwd}) — "
                    f"NOT using as project root. Waiting for set_project_root() call."
                )
                set_original_cwd(cwd)
            else:
                set_original_cwd(cwd)
                _agent_set_project_root(cwd)
                log.info(
                    f"[BRIDGE] Agent state: cwd={cwd}, session={get_session_id()}"
                )
        except Exception as exc:
            log.warning(f"[BRIDGE] Could not set agent state: {exc}")

    def _connect_streaming(self):
        try:
            self._streaming = get_streaming_emitter()
            # \u2500\u2500 FILTERED TOKEN FORWARDING \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
            # Do NOT directly connect llm_token -> response_chunk.emit.
            # Use a filtering intermediary that strips internal reasoning/
            # thinking tokens, duplicate verification loops, and meta-cognitive
            # chatter before forwarding to the chat UI.
            self._connect_qt_signal(self._streaming.llm_token, self._on_filtered_token)
            self._connect_qt_signal(self._streaming.error, self.request_error.emit)
            log.info("[BRIDGE] Streaming emitter ready (real-time path: response_chunk → native bridge)")
        except Exception as exc:
            log.warning(f"[BRIDGE] Streaming not available: {exc}")
            self._streaming = None

    def _on_thought_start(self) -> None:
        """Handle thought start signal from streaming emitter."""
        try:
            # Emit start tag. JS _handleCortexThoughtTags expects <cortex_thought_start>
            self.response_chunk.emit('<cortex_thought_start>')
            log.debug("[BRIDGE] Thought start emitted")
        except Exception as e:
            log.warning(f"[BRIDGE] Failed to emit thought start: {e}")

    def _on_thought_delta(self, text: str) -> None:
        """Handle thought content delta from streaming emitter."""
        try:
            # Wrap each delta so JS can extract text with delimiter safety
            self.response_chunk.emit(f'<cortex_thought_delta>{text}</cortex_thought_delta>')
            log.debug(f"[BRIDGE] Thought delta emitted: {len(text)} chars")
        except Exception as e:
            log.warning(f"[BRIDGE] Failed to emit thought delta: {e}")

    def _on_thought_end(self) -> None:
        """Handle thought end signal from streaming emitter."""
        try:
            # Emit end tag. JS _handleCortexThoughtTags expects <cortex_thought_end>
            self.response_chunk.emit('<cortex_thought_end>')
            log.debug("[BRIDGE] Thought end emitted")
        except Exception as e:
            log.warning(f"[BRIDGE] Failed to emit thought end: {e}")

    # ── Self-talk patterns: AI meta-commentary that should NEVER reach the user ──
    # These fire when the model responds to injected nudge/system messages with
    # internal reasoning like "You're right, let me stop searching and..."
    _SELF_TALK_PATTERNS = None  # lazy-compiled

    def _get_self_talk_patterns(self):
        """Lazy-compile self-talk regex patterns (once per bridge instance)."""
        if self._SELF_TALK_PATTERNS is None:
            import re as _re
            self._SELF_TALK_PATTERNS = [
                # "You're right — enough searching" / "You're right, let me..."
                _re.compile(r"^You'?re right\b.{0,40}(enough|stop|let me|now|I'll|I will)", _re.I | _re.DOTALL),
                # "Let me read the critical..." / "Let me find how..."
                _re.compile(r"^Let me (read|find|check|look|examine|search|investigate)\b", _re.I),
                # "I need to stop searching and..." / "I should stop looking..."
                _re.compile(r"^I (need|should|must) (to )?(stop|cease|quit|halt)\b", _re.I),
                # "Enough searching, I'll..." / "Enough reading, let me..."
                _re.compile(r"^Enough (searching|reading|looking|exploring|investigating)", _re.I),
                # "I understand the issue now. The text..." (self-talk before action)
                _re.compile(r"^I understand the (issue|problem|bug) now\.\s", _re.I),
                # "Instead of forcing..." / "Instead of searching more..."
                _re.compile(r"^Instead of (forcing|searching|reading|looking|continuing)", _re.I),
                # "The system is asking for verification..."
                _re.compile(r"^The system is (asking|requesting|telling)", _re.I),
                # "I'll read the specific file..." / "I will now read..."
                _re.compile(r"^I('ll| will) (now )?(read|check|examine|look at|search|find)\b", _re.I),
                # "Let me directly fix..." / "Let me now implement..."
                _re.compile(r"^Let me (directly|now|just) (fix|implement|edit|modify|change|update)\b", _re.I),
                # "OK, I will..." / "Alright, let me..."
                _re.compile(r"^(OK|Alright|Sure|Got it)[,.]\s*(I will|let me|I'll)\b", _re.I),
            ]
        return self._SELF_TALK_PATTERNS

    def _on_filtered_token(self, token: str) -> None:
        """Filter LLM tokens to separate internal thinking from user-facing response.
        Emits text immediately for real-time streaming."""
        import re as _re

        if not token or not token.strip():
            return

        # Initialize cross-token buffers
        if not hasattr(self, '_thought_buffer'):
            self._thought_buffer = ''
            self._response_buffer = ''
            self._in_thought_tag = False
            self._selftalk_buffer = ''
            self._in_selftalk = False

        THOUGHT_OPEN = _re.compile(r'<(think|thinking|antThinking|scratchpad)(\s[^>]*)?>', _re.IGNORECASE)
        THOUGHT_CLOSE = _re.compile(r'</(think|thinking|antThinking|scratchpad)\s*>', _re.IGNORECASE)

        remaining = token

        # Handle thinking tags
        if self._in_thought_tag:
            close_match = THOUGHT_CLOSE.search(remaining)
            if close_match:
                thought_part = remaining[:close_match.start()]
                self._thought_buffer += thought_part
                self._in_thought_tag = False
                if self._thought_buffer.strip():
                    self.response_chunk.emit('<cortex_thought>' + self._thought_buffer + '</cortex_thought>')
                    self._thought_buffer = ''
                after = remaining[close_match.end():]
                if after.strip():
                    self.response_chunk.emit(after)
            else:
                self._thought_buffer += remaining
            return

        # Check for thinking tag open
        open_match = THOUGHT_OPEN.search(remaining)
        if open_match:
            before = remaining[:open_match.start()]
            if before.strip():
                self.response_chunk.emit(before)
            after_open = remaining[open_match.end():]
            close_match = THOUGHT_CLOSE.search(after_open)
            if close_match:
                thought_content = after_open[:close_match.start()]
                if thought_content.strip():
                    self.response_chunk.emit('<cortex_thought>' + thought_content + '</cortex_thought>')
                after = after_open[close_match.end():]
                if after.strip():
                    self.response_chunk.emit(after)
            else:
                self._in_thought_tag = True
                self._thought_buffer = after_open
            return

        # ── Self-talk filter: catch meta-commentary at paragraph boundaries ──
        # The model sometimes outputs internal reasoning as plain text (no XML tags).
        # This happens when nudge/system messages trigger "You're right, let me..." responses.
        # We buffer the first paragraph and check if it matches self-talk patterns.
        if not self._in_selftalk:
            self._selftalk_buffer += remaining
            # Wait for enough text to judge (at least 80 chars or a newline)
            if len(self._selftalk_buffer) < 80 and '\n' not in self._selftalk_buffer:
                return  # Still buffering
            # Check against self-talk patterns
            _buf_stripped = self._selftalk_buffer.strip()
            for _pat in self._get_self_talk_patterns():
                if _pat.search(_buf_stripped):
                    log.info(f"[BRIDGE] Self-talk filtered: {_buf_stripped[:100]}...")
                    # Route to thought card instead of visible output
                    self.response_chunk.emit('<cortex_thought>' + self._selftalk_buffer + '</cortex_thought>')
                    self._selftalk_buffer = ''
                    self._in_selftalk = True  # Skip subsequent lines until next paragraph
                    return
            # Not self-talk — emit buffered text and continue normally
            self._in_selftalk = True  # Accept rest of this response
            if self._selftalk_buffer.strip():
                self.response_chunk.emit(self._selftalk_buffer)
            self._selftalk_buffer = ''
            return

        # Inside a non-selftalk response — emit normally
        self.response_chunk.emit(remaining)
    def _get_project_root(self) -> str:
        """Return the project root, NEVER falling back to Program Files."""
        root = self._project_root or os.getcwd()
        # CRITICAL: Prevent UAC prompts — never use Program Files as project root
        if 'Program Files' in root:
            log.warning(
                f"[BRIDGE] Project root fallback is in Program Files ({root}) — "
                f"set_project_root() must be called first. Returning empty."
            )
            return ''  # Empty = no project root — will trigger guard in tools
        return root

    def _build_system_prompt(self, context: Dict[str, Any]) -> str:
        from datetime import datetime as _dt
        project_root = self._get_project_root()
        active_file  = self._active_file or ""

        # ── Auto-detect active file from context if not set ──
        if not active_file:
            ctx_active = context.get('active_file') or context.get('file_path') or ''
            if ctx_active:
                active_file = ctx_active
                self._active_file = ctx_active

        # ── Detect file type for active file ──
        file_type_note = ''
        if active_file:
            ext = Path(active_file).suffix.lower()
            ext_map = {
                '.py': 'Python', '.js': 'JavaScript', '.ts': 'TypeScript',
                '.tsx': 'TypeScript/React', '.jsx': 'JavaScript/React',
                '.html': 'HTML', '.css': 'CSS', '.scss': 'SCSS',
                '.json': 'JSON', '.md': 'Markdown', '.yaml': 'YAML',
                '.yml': 'YAML', '.go': 'Go', '.rs': 'Rust', '.java': 'Java',
                '.cpp': 'C++', '.c': 'C', '.h': 'C/C++ Header',
                '.cs': 'C#', '.sql': 'SQL', '.sh': 'Shell', '.ps1': 'PowerShell',
            }
            lang = ext_map.get(ext, ext.lstrip('.') if ext else '')
            if lang:
                file_type_note = f' ({lang})'

        # ── Auto-discover project structure (cached) ──────────
        project_info = self._get_project_summary(project_root)

        # ── Git history (cached) ──
        git_summary = self._get_git_summary(project_root)

        # ── .cortex/ project context (rules, memory, ignore, commands) ──
        cortex_context_block = ''
        if _HAS_CORTEX_PROJECT_CTX and project_root and not 'Program Files' in project_root:
            try:
                cortex_context_block = load_all_cortex_context(project_root)
                if cortex_context_block:
                    log.info("[BRIDGE] Injected .cortex/ context into system prompt")
            except Exception as exc:
                log.debug("[BRIDGE] .cortex/ context load skipped: %s", exc)

        # ── Persistent memory (project-scoped, loaded once per session) ──
        memory_section = ''
        _mem_enabled: bool = True
        try:
            from src.config.settings import get_settings
            _raw_settings: Any = get_settings()
            _raw_memory: Any = _raw_settings.get('memory')
            if isinstance(_raw_memory, dict):
                _memory_map = cast(Dict[str, Any], _raw_memory)
                _candidate = _memory_map.get('enabled')
                if isinstance(_candidate, bool):
                    _mem_enabled = _candidate
        except Exception:
            pass
        if _mem_enabled:
            memory_dir = self._get_memory_dir()
            self._ensure_memory_dir(memory_dir)
            memory_section = self._load_memory_section(memory_dir)

        prompt = f"""You are Cortex AI Agent, an autonomous coding assistant. Write clean, efficient, well-tested code.

## Environment
Project Root: {project_root}
{f'Active File: {Path(active_file).name}{file_type_note}  ({active_file})' if active_file else ''}
OS: Windows
Shell: PowerShell (use semicolons ; not &&)
Today's Date: {_dt.now().strftime('%Y-%m-%d')} (use this date, NOT a date from your training data)
CRITICAL: ALL file operations (Read, Edit, Write, Grep, Glob, Bash) MUST target files inside the Project Root above. Never search or edit files outside this directory. When using Grep/Glob, either omit the path argument (defaults to project root) or pass a path within the project root. Paths to the Cortex IDE source code or other unrelated directories are FORBIDDEN.

## Project Context
{project_info}

{f'## Recent Git History\n{git_summary}' if git_summary else ''}

{cortex_context_block}

{memory_section}

## Task Graph
{self._task_graph.build_prompt_section() if self._task_graph and self._task_graph.get_task_count() > 0 else "No tasks yet. Use TodoWrite to plan multi-step work."}

## Token Budget
You have a generous context window. Use it wisely — read when you need to, not by default.

| Model Tier | Context | File Read Cap | Turns |
|------------|---------|---------------|-------|
| 1M models (DeepSeek V4, MiMo V2.5, GPT-5.4, Gemini 2.5) | 1,000,000 | 200K chars | 60 |
| 200K models (Claude standard) | 200,000 | 30K chars | 45 |
| 128K models (Mistral, Qwen) | 128,000 | 19K chars | 30 |

Per-file budget: Each file read is capped at the model max_file_read_chars limit (shown in tool errors). Use offset/limit to read large files in chunks. The default chunk is 1000 lines.

Per-turn limit: Maximum 50 file reads per turn. Read as many files as you need to understand the codebase before editing — deep understanding prevents bugs.

## Rules
1. CALL TOOLS — never just describe what you would do. Tools are defined in the API request.
2. USE TODO for multi-step work: If a task requires 3+ distinct steps or touches multiple files, use TodoWrite to plan first. Planning prevents forgetting steps mid-execution.
3. WORKFLOW (Cursor-style): Decide your approach based on the task:
   - **Simple/known changes**: Read → Edit directly
   - **Complex/unknown code**: Search (Grep/Glob) → Read → Understand → Plan → Edit
   - **New features**: Plan (TodoWrite) → Implement → Verify
   - Never follow a rigid sequence. Adapt to what you discover.
4. NO SELF-TALK: Never output meta-commentary about your process. Do NOT say "Let me search for...", "I'll read the file...", "You're right, let me...", "Enough searching, I'll...". Just DO it — call the tool silently. The user sees your tool calls automatically. Your visible text should ONLY be: explanations, summaries, or questions to the user.
5. USE WRITE for new files, Edit for existing ones. Never Read a file that doesn't exist.
6. VERIFY EVERY CHANGE: After EVERY Edit/Write, Read the modified section to confirm the change persisted correctly. No exceptions — even for "simple" one-line fixes. Unverified changes are the #1 cause of broken code.
7. BATCH TOOLS: Call multiple independent tools (reads, searches, edits) in one turn to work efficiently.
8. TARGETED READS: Use Grep/Glob/SementicSearch to locate code, then Read with offset/limit for the relevant section. For large files (>250 lines), the system returns a skeleton first — use the line numbers to read specific functions. Prefer SementicSearch for understanding tasks in large codebases.
9. WEB SEARCH: Use WebSearch for real-time info, current events, documentation, or anything requiring up-to-date knowledge. You HAVE internet access — NEVER say you don't have internet access or can't search the web.
10. WEB FETCH: Use WebFetch to read and extract content from URLs the user provides. You CAN browse and visit URLs — NEVER say you can't browse or visit URLs.
11. DIAGRAMS: When asked about project structure, file trees, architecture, workflows, or dependencies, output a ```mermaid code block DIRECTLY IN YOUR CHAT RESPONSE — do NOT use the Write tool for diagrams. The chat viewer renders mermaid natively. Use graph TD for file/dependency trees, flowchart for processes, sequenceDiagram for interactions. Wrap labels with special characters in quotes (e.g., A["my label"]).
12. TASK COMPLETION SUMMARY: When ALL tasks/todos are finished and the work is complete, you MUST write a clear, detailed summary as VISIBLE MARKDOWN TEXT explaining what you accomplished. This summary is the most important part of your response — the user needs to understand what changed. Include:
   - A heading (### Summary or ### What I Did)
   - What files were changed/created/deleted and why
   - What bugs were fixed, features added, or improvements made
   - A table or list of changes if there were multiple files
   Write this in plain conversational language. Do NOT be terse — the user cannot see tool cards after the response ends, so this summary is their only record of what happened. After the visible summary, optionally add:
<task_summary>
{{
  "title": "Brief task title",
  "files": [{{"name": "file.py", "action": "created|modified|deleted", "path": "src/file.py"}}]
}}
</task_summary>

## Task Integrity Rules (CRITICAL)
These rules prevent the most common failure mode: falsely marking tasks as complete.

1. **VERIFICATION GATE**: NEVER mark a todo as COMPLETE unless the associated code change has been VERIFIED to exist in the file. After every Edit/Write, Read the modified file to confirm the change persisted before marking the task done.
2. **VERIFICATION STATEMENT**: When marking a task COMPLETE, include a short verification statement in the todo content (e.g., "Verified file.py L45-L52 now contains the fix").
3. **NO GUESSING**: If you are uncertain whether a task is truly complete — if you haven't read the file after editing, or if a tool call failed — keep it IN_PROGRESS. Do NOT advance status on assumptions.
4. **COMPACTION AWARENESS**: If the conversation was recently compacted, re-read the active TODO list before making status changes. Compaction may drop context — re-verify what's actually done.
5. **TASK CLOSURE**: Only mark ALL tasks complete when every file change is confirmed and the user's request is fully satisfied. Partial completion = leave pending tasks in IN_PROGRESS.

## Chat Display Formatting Rules ⚠️
The IDE chat UI (aichat.html) displays results in compact tool cards — NOT raw text dumps. The system automatically builds a ✅ checkmarks summary from your tool_activity data. **Raw JSON, code blocks, or data structures pasted into chat break the UI and confuse the user.** Follow these rules EVERY response:

**RULE 0 — NO INLINE JSON/OBJECTS IN CHAT**: This is the #1 offense. Do NOT output JSON objects, task_summary blocks, {{"key": "value"}} structures, or bracketed data as plain text in the conversation. The chat stream goes directly to the user's screen — raw JSON looks like garbage. ALL structured data goes through: (a) Write/Edit tools for file changes, (b) tool_activity for operation results, (c) `<task_summary>` tags for completion summaries. If you start typing `{{` or `[` in chat, YOU ARE WRONG — use a tool instead.

1. **NO RAW JSON DUMPS**: Do NOT paste raw JSON objects or API responses into chat. Structured data goes through tools.
2. **CODE BLOCKS ARE OK FOR EXPLANATIONS**: You CAN use ```code blocks``` in chat when explaining concepts, showing snippets, or demonstrating changes. Do NOT use code blocks to dump entire files — keep snippets focused and relevant.
3. **THOROUGH ≠ VERBOSE**: Read files thoroughly, verify every change, then explain clearly. Never skip understanding to be brief. But DO explain what you did — the user needs to understand the changes.
4. **ALWAYS EXPLAIN YOUR WORK**: After completing a task, write a clear summary with headings, lists, or tables showing what was changed and why. The user should never have to guess what you did. Being detailed in your completion summary is REQUIRED — being miser with explanations is the worst behavior.

## Explore Card & File Card Display Rules 🔍
The chat UI displays tool results in structured cards. Know how they work:

### Explore Card (🔍)
- **Each user prompt = ONE fresh Explore Card**. Old cards are NEVER reused.
- The card is created when the first explore tool runs (read_file, search, web_fetch, thinking, etc.)
- All explore tools within the same prompt share the same card (status dots update live)
- When ALL explore tools complete, the card auto-hides — no clutter
- When a NEW explore tool starts running, the card reappears
- File editing tools (edit_file, write_file, create_file) do NOT go into the Explore Card

### File Cards (edit_file, write_file, create_file)
- File cards appear **chronologically at the position where the tool was called** — NOT bunched at the end of chat
- The streaming placeholder ("Creating: file.js") is replaced in-place by the real card
- Never create duplicate file cards — the UI handles dedup automatically

### Terminal Cards (run_command, bash)
- Terminal output gets its own terminal card with live streaming output
- Not part of the Explore Card

### Turn Summary
The system builds a ✅ checkmarks summary from your tool_activity data automatically. Do NOT output your own summary of tools called.

## XML Control Tag Rules (CRITICAL for clean chat display)
The chat UI strips XML control tags like `<task_summary>`, `<think>`, `<file_edited>`, `<exploration>`, `<terminal_output>`, and `<permission>` from visible text. To prevent broken/leaking text in chat bubbles:

1. **SEPARATE LINES**: Every XML control tag MUST appear on its OWN line, separated by blank lines from display text. Example:
   ```
   Here is the fix I applied.

   <file_edited>
   {{"path": "src/file.py", "action": "modified"}}
   </file_edited>

   The file has been updated.
   ```
   NEVER embed tags inline like: "Let me <think>reason</think> explain..." — this WILL leak into the visible bubble.

2. **NO PARTIAL TAGS**: Always close every tag. `<task_summary>` must have `</task_summary>`. Unclosed tags produce garbled text.

3. **TASK_SUMMARY LAST**: The `<task_summary>` block must be the VERY LAST thing in your response with NO text after it.

4. **THINK TAGS ISOLATED**: `<think>` blocks must be self-contained — no text sharing a line with the opening or closing tag.

## Markdown Formatting Rules
For display text that reaches the user — the chat UI renders markdown via the `marked` library with `gfm: true` and `breaks: true`. Use standard GFM markdown syntax:

1. **CODE BLOCKS**: Use proper fenced blocks (```lang ... ```). Code blocks render with a language icon header and copy button. Never leave a code fence unclosed — unmatched ``` breaks the entire markdown parse.

2. **HEADINGS**: Use `###` for section headings. H1/H2 are demoted to H3. H5/H6 are omitted. Use H4 for subsections.

3. **LISTS**: Use `-` for bullet lists and `1.` for numbered lists. Task lists (`- [ ]` / `- [x]`) render with styled checkboxes.

4. **LINKS**: Use `[text](url)` Markdown link syntax, not raw URLs. Raw URLs get truncated or broken. Links automatically get `target="_blank"`.

5. **INLINE FORMATTING**: Use standard GFM: `**bold**`, `*italic*`, `` `code` ``, `~~strikethrough~~`.

6. **NO HTML**: Do not use HTML tags — use Markdown equivalents. The renderer converts markdown to HTML.

7. **NO DOUBLE-ESCAPING**: Do not output `&amp;lt;` or similar double-escaped entities. Write raw markdown characters.

8. **DIAGRAMS**: Use ```mermaid code blocks for architecture diagrams, flowcharts, and sequence diagrams. They render as interactive expandable cards.

9. **TABLES**: MANDATORY for comparing 2+ items. STRICT FORMAT — every row (header, separator, data) MUST start and end with |. Example:

| Feature | Python | JavaScript |
|---------|--------|------------|
| Typing  | Strong | Weak       |

Rules: preceded by a blank line, separator row has same column count as header, every data row has same column count. Use bold for header text. Serial number columns start from 1 not 0. NEVER put the table title inside the header row — put it on the line above as a heading.

## Reading Strategy — 5-Phase Protocol (MANDATORY)
READ AGGRESSIVELY. You have a massive context window — USE IT.
Every edit MUST be preceded by thorough reading. Follow this protocol:

1. **Phase 1 — SCAN**: List directory, glob for relevant files, grep for keywords
2. **Phase 2 — SKIM**: Read file headers, class/function signatures, imports
3. **Phase 3 — DEEP READ**: Read the specific functions/sections you'll modify
4. **Phase 4 — DEPENDENCIES**: Read imports, callers, related files, tests
5. **Phase 5 — IMPLEMENT**: Only NOW start writing/editing

Match reading depth to task complexity. A typo fix needs less reading than a refactor.
But NEVER skip reading entirely — even simple edits benefit from understanding context.

**NEVER say "I have enough context" or "let me implement now"** — these are banned phrases.

{PERSISTENT_DIRECTIVES}
## ⚠️ PERSISTENT DIRECTIVES OVERRIDE ⚠️
The following directives from agent_safety.py OVERRIDE any conflicting rules above.
They survive auto-compaction and are ALWAYS active:
- READ AGGRESSIVELY — never stop reading prematurely
- 5-Phase Protocol — SCAN → SKIM → DEEP READ → DEPENDENCIES → IMPLEMENT
- Read-Before-Edit — MUST read a file before editing it
- Doom-Loop Detection — same tool+args 3x = STOP, 5x = forced exit
- Generous tool call budget per turn (auto-continues when exhausted)
- Tool Explanation — explain WHY before every tool call
- Error Recovery Budget — max 3 consecutive same-error retries
- NEVER say "I have enough context", "let me implement now", or "stop reading"
- NEVER skip reading a file before editing — always read first, then edit
- NEVER stop searching prematurely — explore thoroughly before implementing
- Search exclusions: skip __pycache__, node_modules, .git, .venv, dist, build, *.pyc

"""
        if context.get("code_context"):
            prompt += f"\n## User's Selected Code\n```\n{context['code_context']}\n```\n"
        # P0 + P7: Append persistent directives (survive compaction)
        # These contain reading protocol, banned phrases, token budget,
        # doom-loop awareness, max iterations, and tool-explanation rule.
        prompt += "\n" + PERSISTENT_DIRECTIVES + "\n"
        # Inject mandatory search strategy (from src/agent/src/utils/searchStrategy.py)
        try:
            from src.agent.src.utils.searchStrategy import get_search_strategy_instruction
            prompt += "\n" + get_search_strategy_instruction() + "\n"
        except Exception:
            pass  # Non-critical — strategy is guidance, not required
        return prompt

    # ── Persistent Memory ───────────────────────────────────

    def _get_memory_dir(self) -> str:
        """
        Return (and cache) the memory directory for the current project.
        Stored INSIDE the project at <project_root>/.cortex/memory/
        so memories stay with the project (like .claude/, .cursor/, .vscode/).
        """
        if self._memory_dir:
            return self._memory_dir
        project = self._get_project_root()
        if project and os.path.isdir(project):
            self._memory_dir = os.path.join(project, '.cortex', 'memory')
        else:
            # Fallback only when no project root is available
            self._memory_dir = os.path.join(
                os.path.expanduser('~'), '.cortex', 'memory'
            )
        return self._memory_dir

    def _auto_create_cortex_dir(self, project_root: str) -> None:
        """Auto-create .cortex/ directory structure inside the working project.

        Called on set_project_root() so every project gets its own .cortex/
        (like .git/, .claude/, .vscode/). Creates:
          .cortex/
          .cortex/memory/          ← checkpoints + MEMORY.md
          .cortex/semantic_memory/ ← semantic search index
          .cortex/memory.json      ← decisions, bugs, preferences
          .cortex/memory/MEMORY.md ← conversation summary index
        """
        if not project_root or not os.path.isdir(project_root):
            return
        try:
            cortex_dir = os.path.join(project_root, '.cortex')
            memory_dir = os.path.join(cortex_dir, 'memory')
            semantic_dir = os.path.join(cortex_dir, 'semantic_memory')

            os.makedirs(memory_dir, exist_ok=True)
            os.makedirs(semantic_dir, exist_ok=True)

            # Create memory.json if missing
            memory_json = os.path.join(cortex_dir, 'memory.json')
            if not os.path.exists(memory_json):
                import json as _json
                with open(memory_json, 'w', encoding='utf-8') as f:
                    _json.dump({
                        "decisions": [],
                        "known_bugs": [],
                        "user_preferences": [],
                        "notes": []
                    }, f, indent=2)

            # Create MEMORY.md if missing
            memory_md = os.path.join(memory_dir, 'MEMORY.md')
            if not os.path.exists(memory_md):
                with open(memory_md, 'w', encoding='utf-8') as f:
                    f.write(f"# {os.path.basename(project_root)} Memory Index\n\n"
                            "## Conversation Summary (auto-updated on compaction)\n\n"
                            "Last compacted: (none yet)\n\n"
                            "## Other Memories\n\n(none yet)\n")

            log.info(f"[BRIDGE] .cortex/ ready at {cortex_dir}")
        except Exception as exc:
            log.warning(f'[BRIDGE] Could not auto-create .cortex/: {exc}')

    def _ensure_memory_dir(self, memory_dir: str) -> None:
        """Create memory directory if it does not exist."""
        try:
            os.makedirs(memory_dir, exist_ok=True)
        except Exception as exc:
            log.warning('[BRIDGE] Could not create memory dir: %s', exc)

    def _get_memory_md_path(self) -> str:
        """Path to MEMORY.md — kept inside <project>/.cortex/memory/.

        MEMORY.md and all checkpoint files live together inside the project's
        .cortex/memory/ directory, just like .claude/ or .cursor/ conventions.
        Falls back to the .cortex memory dir if there's no valid project root.
        """
        return os.path.join(self._get_memory_dir(), 'MEMORY.md')

    def _load_memory_section(self, memory_dir: str) -> str:
        """
        Build the complete memory prompt section to inject into the system prompt.

        Three layers:
          1. Behavioral instructions (how to save/read memories) + MEMORY.md index
             via buildMemoryPrompt() from memdir package.
          2. Content of recently-modified individual memory files (up to 10).

        Returns the combined string, or empty string if the memory system is
        not available or the directory is empty.
        """
        parts: List[str] = []

        # --- Layer 1: Instructions + MEMORY.md index ---
        if _importlib_util.find_spec("agent.src.memdir.memdir"):
            try:
                from agent.src.memdir.memdir import buildMemoryPrompt
                prompt = buildMemoryPrompt({
                    'displayName': 'Cortex Memory',
                    'memoryDir': memory_dir,
                })
                if prompt:
                    parts.append(prompt)
            except Exception as exc:
                log.debug('[BRIDGE] buildMemoryPrompt failed (%s); using fallback', exc)
        else:
            log.debug('[BRIDGE] memdir.memdir not available; using fallback')
            fallback_lines = [
                '# Cortex Memory',
                '',
                f'You have a persistent, file-based memory system at `{memory_dir}`.',
                f'MEMORY.md is at `{memory_dir}/MEMORY.md` — read it with the Read tool using that exact path.',
                'This directory already exists — write to it directly with the Write tool.',
                '',
                '## How to save memories',
                'Save each memory as a separate .md file with frontmatter:',
                '```markdown',
                '---',
                'name: <memory name>',
                'description: <one-line description for relevance matching>',
                'type: <user | feedback | project | reference>',
                '---',
                '',
                '<memory content>',
                '```',
                'Then update MEMORY.md index with a one-line pointer: `- [Title](file.md) — hook`.',
                '',
                '## Memory types',
                '- **user**: user role, goals, preferences, knowledge level',
                '- **feedback**: corrections and confirmed approaches (include Why + How to apply)',
                '- **project**: ongoing work, decisions, deadlines, context not in code',
                '- **reference**: pointers to external systems (dashboards, trackers)',
            ]
            # MEMORY.md lives inside <project>/.cortex/memory/MEMORY.md
            memory_index_path = self._get_memory_md_path()
            if not os.path.exists(memory_index_path):
                _legacy = os.path.join(memory_dir, 'MEMORY.md')
                if os.path.exists(_legacy):
                    memory_index_path = _legacy
            try:
                with open(memory_index_path, 'r', encoding='utf-8') as fh:
                    index_content = fh.read().strip()
                if index_content:
                    fallback_lines += ['', '## MEMORY.md', '', index_content]
            except FileNotFoundError:
                fallback_lines += [
                    '', '## MEMORY.md',
                    '', 'Your MEMORY.md is currently empty. When you save new memories, they will appear here.',
                ]
            except Exception:
                pass
            parts.append('\n'.join(fallback_lines))

        # --- Layer 2: Recent individual memory files ---
        memoryFreshnessNote: Callable[[float], str] = lambda mtime_ms: ""

        if _importlib_util.find_spec("agent.src.memdir.memoryAge"):
            try:
                from agent.src.memdir.memoryAge import memoryFreshnessNote as _mf
                memoryFreshnessNote = cast(Callable[[float], str], _mf)
            except Exception:
                pass

        try:
            mem_files: List[Tuple[float, str]] = []
            for dirpath, _dirs, fnames in os.walk(memory_dir):
                for fname in fnames:
                    if fname.endswith('.md') and fname != 'MEMORY.md':
                        fp = os.path.join(dirpath, fname)
                        try:
                            mtime = os.path.getmtime(fp)
                            mem_files.append((mtime, fp))
                        except OSError:
                            pass
            # Sort newest-first; load up to 10
            mem_files.sort(key=lambda x: x[0], reverse=True)
            loaded_files: List[str] = []
            for mtime, fp in mem_files[:10]:
                try:
                    with open(fp, 'r', encoding='utf-8') as fh:
                        content = fh.read().strip()
                    rel = os.path.relpath(fp, memory_dir)
                    freshness = memoryFreshnessNote(mtime * 1000)
                    header = f'### {rel}'
                    if freshness:
                        loaded_files.append(f'{header}\n{freshness}\n{content}')
                    else:
                        loaded_files.append(f'{header}\n{content}')
                except Exception:
                    pass
            if loaded_files:
                parts.append(
                    '## Other Memories\n\n'
                    + '\n\n---\n\n'.join(loaded_files)
                )
        except Exception as exc:
            log.debug('[BRIDGE] Memory file loading skipped: %s', exc)

        # --- Layer 3: Cross-session semantic memory ---
        try:
            from src.core.semantic_memory import get_semantic_memory_index
            _sem_idx = get_semantic_memory_index()
            if _sem_idx is not None and _sem_idx.count() > 0:
                # Use the actual user message for relevance-targeted search.
                # Fall back to a generic query if no message is available.
                _user_msg = getattr(self, '_last_user_message', '') or "coding project context decisions architecture"
                semantic_section = _sem_idx.build_prompt_section(
                    _user_msg,
                    max_entries=3,
                )
                if semantic_section:
                    # Keep memory section lean by tail-inserting
                    parts.append(semantic_section)
        except ImportError:
            pass
        except Exception as exc:
            log.debug('[BRIDGE] Semantic memory search skipped: %s', exc)

        return '\n\n'.join(parts)

    def _get_project_summary(self, project_root: str) -> Optional[str]:
        """Auto-discover project structure for the system prompt (cached per session)."""
        if hasattr(self, '_cached_project_summary'):
            return self._cached_project_summary
        lines: List[str] = []
        max_entries = 60  # Total cap to stay lean in system prompt
        entry_count = 0
        try:
            # Detect project type from marker files
            markers = {
                'package.json': 'Node.js/JavaScript',
                'requirements.txt': 'Python',
                'Cargo.toml': 'Rust',
                'go.mod': 'Go',
                'pom.xml': 'Java/Maven',
                'build.gradle': 'Java/Gradle',
                '.csproj': 'C#/.NET',
                'pyproject.toml': 'Python',
                'tsconfig.json': 'TypeScript',
            }
            detected: List[str] = []
            for marker, lang in markers.items():
                if os.path.exists(os.path.join(project_root, marker)):
                    detected.append(lang)
            if detected:
                lines.append(f"Tech stack: {', '.join(detected)}")

            # Show top-level directory structure
            try:
                entries = sorted(os.scandir(project_root), key=lambda e: (not e.is_dir(), e.name))
                top_level: List[str] = []
                # Key directories to show sub-structure for
                key_dirs = {'src', 'app', 'lib', 'tests', 'test', 'docs', 'config', 'public', 'assets'}
                for e in entries[:25]:
                    if e.name.startswith('.') and e.name not in ('.env', '.gitignore', '.cortex'):
                        continue
                    marker = '/' if e.is_dir() else ''
                    top_level.append(f"  {e.name}{marker}")
                    entry_count += 1

                    # ── Deep discovery: show 1 level into key directories ──
                    if e.is_dir() and e.name in key_dirs and entry_count < max_entries:
                        try:
                            sub_entries = sorted(os.scandir(e.path), key=lambda s: (not s.is_dir(), s.name))
                            sub_count = 0
                            for se in sub_entries[:12]:
                                if se.name.startswith('.'):
                                    continue
                                if entry_count >= max_entries:
                                    break
                                sm = '/' if se.is_dir() else ''
                                top_level.append(f"    {se.name}{sm}")
                                entry_count += 1
                                sub_count += 1
                            if sub_count >= 12 and entry_count < max_entries:
                                top_level.append(f"    ... (+more)")
                                entry_count += 1
                        except OSError:
                            pass

                if top_level:
                    lines.append("Project structure:")
                    lines.extend(top_level)
            except OSError:
                pass
        except Exception:
            lines.append("(could not auto-detect project info)")
        result = "\n".join(lines) if lines else "(unknown project)"
        self._cached_project_summary = result
        return result

    def _get_git_summary(self, project_root: str) -> str:
        """Get recent git history for system prompt context (cached per session)."""
        if hasattr(self, '_cached_git_summary'):
            return self._cached_git_summary
        try:
            import subprocess
            result = subprocess.run(
                ['git', 'log', '--oneline', '-10', '--no-decorate'],
                cwd=project_root,
                capture_output=True, text=True, timeout=5,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0
            )
            if result.returncode == 0 and result.stdout.strip():
                lines = result.stdout.strip().split('\n')
                summary = "Recent git commits:\n" + "\n".join(f"  {l}" for l in lines)
                self._cached_git_summary = summary
                return summary
        except Exception:
            pass
        self._cached_git_summary = ''
        return ''

    # ============================================================
    # CONTEXT CHECKPOINT & COMPACTION
    # ============================================================

    def _create_context_checkpoint(self, messages: List[Any], user_message: str = "",
                                   write_memory_md: bool = True) -> str:
        """
        Create a structured checkpoint of the current conversation state
        and persist it to MEMORY.md for cross-session recovery.

        Captures:
        - Current task / user request
        - Todo items with statuses
        - Files read and modified this session
        - Key assistant decisions
        - Conversation summary digest

        The checkpoint is saved to:
          1. A timestamped .md file in the memory dir
          2. MEMORY.md index (so it's loaded automatically on next session)

        Returns the checkpoint text (also used inline by _compact_messages).
        """
        import time as _time
        from datetime import datetime as _dt

        parts: List[str] = []

        # 1. Current user request (first user message or most recent)
        _user_msg = user_message
        if not _user_msg:
            for msg in reversed(messages):
                if getattr(msg, 'role', None) == 'user':
                    _content = getattr(msg, 'content', '') or ''
                    if not _content.startswith('[System note') and not _content.startswith('[Context Recovery'):
                        _user_msg = _content[:500]
                        break
        if _user_msg:
            parts.append(f"**Current Task:** {_user_msg[:500]}")

        # 2. Todo items
        if self._current_todos:
            todo_lines: List[str] = []
            for t in self._current_todos:
                status = str(t.get('status', 'pending')).upper()
                content = t.get('content', t.get('activeForm', ''))
                icon = {'COMPLETED': '[x]', 'IN_PROGRESS': '[~]', 'CANCELLED': '[-]'}.get(status, '[ ]')
                todo_lines.append(f"  {icon} {content}")
            parts.append("**Todo Progress:**\n" + "\n".join(todo_lines))

        # 3. Files read / modified WITH CONTENT SNAPSHOTS ──────────────
        # CRITICAL: Without content snapshots, the AI forgets what it already
        # wrote after compaction — it sees only filenames and rewrites
        # everything from scratch, creating an infinite rework loop.
        _read_files = self._tool_ctx.get_recent_read_files(10)
        _mod_files = self._tool_ctx.get_recent_modified_files(10)
        if _read_files:
            parts.append("**Files Read:** " + ", ".join(os.path.basename(f) for f in _read_files))
        if _mod_files:
            _mod_parts: List[str] = []
            for f in _mod_files:
                _bn = os.path.basename(f)
                try:
                    _size = os.path.getsize(f)
                    _size_str = f"{_size:,} bytes"
                except Exception:
                    _size_str = "unknown size"
                _mod_parts.append(f"  - `{_bn}` ({_size_str})")
            parts.append("**Files Modified (DO NOT re-read or re-write these):**\n" + "\n".join(_mod_parts))
            
            # ── FILE CONTENT SNAPSHOTS: Capture current state of modified files ──
            # After compaction the AI has zero memory of what it wrote — it sees
            # "index.html (42KB)" with no idea what's inside. This causes the
            # catastrophic rework loop: compaction → forget → re-read → exhaustion.
            # Content snapshots give the AI 600 chars of each file's content so it
            # knows exactly what was written and can continue without re-reading.
            _snapshot_lines: List[str] = []
            _snapshot_bytes = 0
            _SNAPSHOT_MAX_BYTES = 6000  # Total budget for all file snapshots
            for f in _mod_files[:6]:  # Max 6 files to avoid context bloat
                try:
                    _bn = os.path.basename(f)
                    # Read file content (binary first, then decode heuristically)
                    with open(f, 'rb') as fh:
                        _raw = fh.read(min(1800, os.path.getsize(f)))  # 600 chars ≈ 1800 bytes UTF-8
                    try:
                        _text = _raw.decode('utf-8')
                    except UnicodeDecodeError:
                        try:
                            _text = _raw.decode('cp1252')
                        except Exception:
                            _text = _raw.decode('utf-8', errors='replace')
                    # Truncate to ~600 chars for a meaningful preview
                    _preview = _text[:600]
                    _snap = f"**{_bn}** (total: {os.path.getsize(f):,} bytes):\n```\n{_preview}\n```"
                    if _snapshot_bytes + len(_snap.encode('utf-8')) > _SNAPSHOT_MAX_BYTES:
                        _snapshot_lines.append(f"**{_bn}** — (snapshot skipped, budget full)")
                    else:
                        _snapshot_lines.append(_snap)
                        _snapshot_bytes += len(_snap.encode('utf-8'))
                except Exception:
                    _snapshot_lines.append(f"**{_bn}** — (could not read file)")
            if _snapshot_lines:
                parts.append("**📸 File Content Snapshots (current state — DO NOT re-read):**\n\n" + "\n\n".join(_snapshot_lines))

        # 4. Key assistant decisions (last 5 assistant messages, truncated)
        _decisions: List[str] = []
        for msg in reversed(messages):
            if getattr(msg, 'role', None) == 'assistant':
                _content = getattr(msg, 'content', '') or ''
                if _content and not getattr(msg, 'tool_calls', None):
                    _decisions.append(_content[:300])
                    if len(_decisions) >= 5:
                        break
        if _decisions:
            parts.append("**Key Decisions:**\n" + "\n".join(f"- {d}" for d in reversed(_decisions)))

        # 5. Conversation summary digest (collect all user+assistant exchanges)
        _summary_lines: List[str] = []
        _msg_count = 0
        for msg in messages:
            _role = getattr(msg, 'role', None)
            _content = getattr(msg, 'content', '') or ''
            if _role == 'user' and _content and not _content.startswith('['):
                _summary_lines.append(f"User: {_content[:300]}")
                _msg_count += 1
            elif _role == 'assistant' and _content and not getattr(msg, 'tool_calls', None):
                _summary_lines.append(f"Assistant: {_content[:300]}")
                _msg_count += 1
            if _msg_count >= 15:  # Keep last 15 exchanges for better continuity
                break
        if _summary_lines:
            parts.append("**Conversation Digest:**\n" + "\n".join(_summary_lines))

        checkpoint_text = "\n\n".join(parts)

        # Save to persistent memory dir
        try:
            memory_dir = self._get_memory_dir()
            self._ensure_memory_dir(memory_dir)
            ts = int(_time.time())
            now_str = _dt.now().strftime('%Y-%m-%d %H:%M')
            filename = f"checkpoint_{ts}.md"
            filepath = os.path.join(memory_dir, filename)
            frontmatter = (
                "---\n"
                f"name: Context Checkpoint {now_str}\n"
                "description: Auto-saved conversation state before context compaction\n"
                "type: project\n"
                "---\n\n"
            )
            with open(filepath, 'w', encoding='utf-8') as fh:
                fh.write(frontmatter + checkpoint_text)
            log.info(f"[BRIDGE] Context checkpoint saved: {filename}")

            # ── UPDATE MEMORY.md with conversation summary ────────────────
            # The New-Chat flow writes a concise LLM "what was done" summary to
            # MEMORY.md itself, so it passes write_memory_md=False here to avoid
            # dumping the verbose verbatim digest. Auto-compaction keeps the
            # default (True) so it still self-recovers mid-session.
            if write_memory_md:
                self._update_memory_md(memory_dir, checkpoint_text, now_str, filename)

            # Clean up old checkpoints (keep only last 3)
            self._cleanup_old_checkpoints(memory_dir, keep=3)

            # ── Update cross-session semantic memory index ─────────────────
            try:
                from src.core.semantic_memory import get_semantic_memory_index
                _sem_idx = get_semantic_memory_index()
                if _sem_idx is not None:
                    # Derive a compact summary from the checkpoint text
                    _sem_summary = self._extract_semantic_summary(checkpoint_text)
                    _project_dir = os.path.basename(self._project_root) if self._project_root else 'unknown'
                    _sem_idx.store_session(
                        session_id=f"chk_{ts}",
                        summary=_sem_summary,
                        metadata={
                            "project": _project_dir,
                            "checkpoint_file": filename,
                            "source": "context_compaction",
                        },
                    )
            except ImportError:
                pass
            except Exception as exc:
                log.debug(f"[BRIDGE] Semantic memory update skipped: {exc}")

        except Exception as exc:
            log.warning(f"[BRIDGE] Failed to save context checkpoint: {exc}")

        return checkpoint_text

    def _update_memory_md(self, memory_dir: str, checkpoint_text: str, timestamp: str, checkpoint_file: str):
        """
        Update MEMORY.md with the latest compaction summary.
        
        MEMORY.md serves as the persistent conversation summary that:
        - Survives across IDE sessions
        - Gets auto-loaded into system prompt via _load_memory_section()
        - Lets the LLM continue work seamlessly after context compaction
        
        Like Qoder/VS Code Copilot: "Compacting conversation" -> save summary -> continue.
        """
        # MEMORY.md lives inside <project>/.cortex/memory/MEMORY.md
        memory_md_path = self._get_memory_md_path()

        # ── PRE-FIX BUG (2026-06-16): This function completely overwrote ──
        # MEMORY.md with a "## Conversation Summary" format, silently
        # destroying ALL "## Session Summary" blocks saved by the New Chat
        # / IDE close / project switch flow (_write_memory_summary).
        #
        # POST-FIX: PRESERVE all existing session summaries and only ADD
        # a brief checkpoint pointer. The full checkpoint is saved as a
        # separate .md file; MEMORY.md keeps its canonical format.
        # ────────────────────────────────────────────────────────────────

        import re as _re
        from datetime import datetime as _dt

        # Read existing MEMORY.md and extract everything we want to preserve
        existing_session_blocks: List[str] = []
        existing_pointers: List[str] = []
        has_checkpoint_pointer = False

        try:
            with open(memory_md_path, 'r', encoding='utf-8') as fh:
                content = fh.read()

            # Extract "## Session Summary" blocks
            in_session = False
            current_block: List[str] = []
            for line in content.split('\n'):
                s = line.strip()
                if s.startswith('## Session Summary'):
                    # Save previous block before starting a new one
                    if in_session and current_block:
                        existing_session_blocks.append('\n'.join(current_block))
                    in_session = True
                    current_block = [line]
                    continue
                if in_session and (
                    s.startswith('## Other Memories')
                    or s.startswith('## Loaded Memory Files')
                    or s.startswith('## Conversation Summary')
                ):
                    if current_block:
                        existing_session_blocks.append('\n'.join(current_block))
                    in_session = False
                    current_block = []
                    continue
                if in_session:
                    current_block.append(line)
                elif s.startswith('- ['):
                    if checkpoint_file in line:
                        has_checkpoint_pointer = True
                    if 'checkpoint_' not in s.lower():
                        existing_pointers.append(line)

            if current_block:
                existing_session_blocks.append('\n'.join(current_block))

            # Also extract legacy "## Conversation Summary" blocks and
            # convert them to session-summary format so they survive.
            in_conv = False
            conv_block: List[str] = []
            for line in content.split('\n'):
                s = line.strip()
                if s.startswith('## Conversation Summary'):
                    in_conv = True
                    conv_block = [line]
                    continue
                if in_conv and s.startswith('## ') and 'Conversation Summary' not in s:
                    if conv_block:
                        converted = '\n'.join(conv_block).replace(
                            '## Conversation Summary (auto-updated on compaction)',
                            '## Session Summary — compaction checkpoint'
                        )
                        existing_session_blocks.append(converted)
                    in_conv = False
                    conv_block = []
                    continue
                if in_conv:
                    conv_block.append(line)
            if conv_block:
                converted = '\n'.join(conv_block).replace(
                    '## Conversation Summary (auto-updated on compaction)',
                    '## Session Summary — compaction checkpoint'
                )
                existing_session_blocks.append(converted)

        except FileNotFoundError:
            pass
        except Exception as exc:
            log.debug(f"[BRIDGE] _update_memory_md read failed: {exc}")

        # ── Build updated MEMORY.md ──
        now_str = _dt.now().strftime('%Y-%m-%d %H:%M')
        out: List[str] = ['# Cortex Memory Index', '']

        # Preserve existing session blocks (up to 200 — keeps full SQLite migration)
        # Strip blank-line edges to prevent accumulation across rewrite cycles.
        def _strip_blk(b: str) -> str:
            lines = b.split('\n')
            while lines and not lines[0].strip():
                lines.pop(0)
            while lines and not lines[-1].strip():
                lines.pop()
            return '\n'.join(lines)

        existing_session_blocks = existing_session_blocks[:200]
        for block in existing_session_blocks:
            cleaned = _strip_blk(block)
            if cleaned:
                out += [cleaned, '']

        # Add new checkpoint pointer if not already present
        if not has_checkpoint_pointer:
            _ckpt_abs = os.path.join(memory_dir, checkpoint_file)
            existing_pointers.insert(0,
                f'- [Full compaction checkpoint]({_ckpt_abs}) — {now_str}')

        # Deduplicate and add pointer lines
        if existing_pointers:
            seen: set = set()
            unique_ptrs = []
            for p in existing_pointers:
                if p not in seen:
                    seen.add(p)
                    unique_ptrs.append(p)
            out += ['', '## Other Memories', ''] + unique_ptrs + ['']

        # Final normalization: collapse 3+ consecutive blank lines to max 2
        raw = '\n'.join(out)
        raw = _re.sub(r'\n{4,}', '\n\n\n', raw)
        raw = raw.rstrip('\n') + '\n'

        try:
            # Atomic write: tempfile + rename prevents partial reads
            # when IDE editor opens MEMORY.md mid-write (showing ~10 lines bug).
            import tempfile as _tf
            _tmp_fd, _tmp_path = _tf.mkstemp(suffix='.md', dir=os.path.dirname(memory_md_path))
            try:
                with os.fdopen(_tmp_fd, 'w', encoding='utf-8') as fh:
                    fh.write(raw)
                os.replace(_tmp_path, memory_md_path)
            except Exception:
                try:
                    os.unlink(_tmp_path)
                except Exception:
                    pass
                raise
            log.info(
                f"[BRIDGE] MEMORY.md checkpoint pointer added "
                f"(preserved {len(existing_session_blocks)} session blocks, "
                f"{len(existing_pointers)} pointers)"
            )
        except Exception as exc:
            log.warning(f"[BRIDGE] Failed to update MEMORY.md: {exc}")

    def _cleanup_old_checkpoints(self, memory_dir: str, keep: int = 3):
        """Remove old checkpoint files, keeping only the most recent N."""
        try:
            checkpoints: List[Tuple[float, str]] = []
            for fname in os.listdir(memory_dir):
                if fname.startswith('checkpoint_') and fname.endswith('.md'):
                    fpath = os.path.join(memory_dir, fname)
                    checkpoints.append((os.path.getmtime(fpath), fpath))
            checkpoints.sort(reverse=True)  # newest first
            for _, fpath in checkpoints[keep:]:
                try:
                    os.remove(fpath)
                    log.debug(f"[BRIDGE] Removed old checkpoint: {os.path.basename(fpath)}")
                except OSError:
                    pass
        except Exception:
            pass

    def _estimate_message_tokens(self, messages: List[Any]) -> int:
        """
        Estimate total token count of message list.
        Uses ~4 chars per token approximation.
        """
        total_chars = 0
        for msg in messages:
            content_raw = getattr(msg, 'content', '')
            content = content_raw if isinstance(content_raw, str) else ''
            total_chars += len(content)
            # Tool calls add ~100 tokens each for metadata
            tool_calls = getattr(msg, 'tool_calls', None)
            if isinstance(tool_calls, list) and tool_calls:
                total_chars += len(cast(List[Any], tool_calls)) * 400
        return total_chars // 4

    def _compact_messages(self, messages: List[Any], PCM: Type[Any]) -> List[Any]:
        """
        Trim conversation history so the next API call fits in the context window.
        Saves the conversation summary to MEMORY.md for cross-session recovery.

        Strategy
        --------
        • Always keep the system message (index 0).
        • Create a context checkpoint capturing task state, todos, files.
        • Persist the checkpoint to MEMORY.md (like Qoder/VS Code “Compacting conversation”).
        • Drop the oldest messages in the middle, keeping the last
          KEEP_TAIL messages so recent context is intact.
        • Walk the tail forward to the first safe boundary (a user or
          assistant turn) so we never orphan a tool-result block.
        • Inject the checkpoint as a rich summary so the LLM continues seamlessly.
        """
        # ── Emit UI status: "Compacting conversation..." ────────────────
        try:
            self._safe_emit(
                self.agent_status_update,
                'compacting',
                'Compacting conversation — saving summary to memory...'
            )
        except Exception:
            pass

        # Keep more tail messages to preserve recent editing context.
        # With 1M token context windows, 10 messages was absurdly conservative —
        # the AI would forget what it just edited and re-read files endlessly.
        # KEEP_TAIL scales with context budget so 1M models keep ~50 messages,
        # preserving 10+ turns of work instead of 5-8.
        _ctx_limits = getattr(self._tool_ctx, '_model_limits', None)
        _budget_tokens = _ctx_limits.context_window if _ctx_limits else 500_000
        if _budget_tokens >= 900_000:
            keep_tail = 50  # 1M models — keep ~25 turns of context
        elif _budget_tokens >= 400_000:
            keep_tail = 40  # 400K+ models
        elif _budget_tokens >= 100_000:
            keep_tail = 30  # 100K+ models
        else:
            keep_tail = 25  # Legacy — small models
        if len(messages) <= keep_tail + 2:
            return messages  # nothing meaningful to drop

        system_msg: Any = messages[0]
        rest: List[Any] = messages[1:]          # everything after the system prompt

        if len(rest) <= keep_tail:
            return messages

        tail: List[Any] = rest[-keep_tail:]
        dropped_count = len(rest) - len(tail)

        # Advance `tail` to the first safe role boundary so we never start
        # mid tool-result block (tool results must follow their assistant turn).
        for i, msg in enumerate(tail):
            if getattr(msg, 'role', None) in ('user', 'assistant'):
                tail = tail[i:]
                break

        # Create checkpoint with rich context + persist to MEMORY.md
        # Emit saving_to_memory_md status so the spinner overlay shows disk write
        try:
            self._safe_emit(
                self.agent_status_update,
                'saving_to_memory_md',
                'Saving conversation summary to MEMORY.md...'
            )
        except Exception:
            pass
        checkpoint_text = self._create_context_checkpoint(messages)

        # ── Use existing compact prompt system for summary formatting ──
        # prompt.get_compact_user_summary_message produces the standard
        # "This session is being continued from a previous conversation..."
        # format used by the production compact system.
        _summary_content = get_compact_user_summary_message(
            summary=(
                f'[Context Recovery: {dropped_count} earlier messages were compacted '
                f'and saved to MEMORY.md.]\n\n'
                f'{checkpoint_text}'
            ),
            suppress_follow_up_questions=True,
        )

        summary: Any = PCM(
            role='user',
            content=_summary_content,
        )
        compacted: List[Any] = [system_msg, summary] + tail
        log.info(
            f'[BRIDGE] Context compacted: {len(messages)} → {len(compacted)} messages (dropped {dropped_count} middle messages, summary saved to MEMORY.md)'
        )
        
        # ── CRITICAL: Update _conversation_history with compacted version ──
        # Without this, the next turn rebuilds from the full (un-compacted)
        # _conversation_history and the rich checkpoint summary is lost entirely.
        # We convert compacted PCM objects back to ChatMessage for the history.
        _new_history: List[Any] = []
        for cm in compacted:
            _role = getattr(cm, 'role', None)
            _content = getattr(cm, 'content', None) or ''
            _hist_msg = ChatMessage(role=_role, content=_content)
            _tc = getattr(cm, 'tool_calls', None)
            if _tc:
                _hist_msg.tool_calls = _tc
            if getattr(cm, 'tool_call_id', None):
                _hist_msg.tool_call_id = cm.tool_call_id
            # PRESERVE reasoning_content — MiMo/DeepSeek thinking
            # mode requires reasoning_content from previous assistant
            # messages to be passed back in subsequent requests.
            # Without this, MiMo returns HTTP 400:
            #   "The reasoning_content in the thinking mode must be
            #    passed back to the API."
            _rc = getattr(cm, 'reasoning_content', None)
            if _rc:
                _hist_msg.reasoning_content = _rc
            _new_history.append(_hist_msg)
        with self._history_lock:
            self._conversation_history = _new_history
        log.info(f'[BRIDGE] _conversation_history pruned to {len(_new_history)} entries after compaction')

        # ── Emit completion status ───────────────────────────────────────
        try:
            self._safe_emit(
                self.agent_status_update,
                'ready',
                f'Conversation compacted — {dropped_count} messages summarized to MEMORY.md'
            )
        except Exception:
            pass

        return compacted

    # ============================================================
    # PROVIDER FAILOVER HELPERS
    # ============================================================

    # Failover priority chain: MISTRAL → SILICONFLOW → DEEPSEEK → MIMO → OPENAI → OPENROUTER → ALIBABA
    _failover_chain = None  # lazily built

    def _get_failover_provider(self, current_type: Any, registry: Any) -> Optional[Any]:
        """
        Return the next provider in the failover chain, or None if exhausted.
        Skips providers that don't have a valid API key.
        Max 2 failover hops to avoid infinite cycling.
        """
        from src.ai.providers import ProviderType, ProviderRegistry  
        current_provider = cast(ProviderType, current_type)
        provider_registry = cast(ProviderRegistry, registry)
        if self._failover_chain is None:
            self._failover_chain = [
                ProviderType.MISTRAL,
                ProviderType.SILICONFLOW,
                ProviderType.DEEPSEEK,
                ProviderType.MIMO,
                ProviderType.OPENAI,
                ProviderType.OPENROUTER,
                ProviderType.ALIBABA,
            ]

        _attempted_raw = getattr(self, '_failover_attempted', None)
        _attempted: Set[Any] = cast(Set[Any], _attempted_raw) if isinstance(_attempted_raw, set) else set()
        _attempted.add(current_provider)
        self._failover_attempted = _attempted

        if len(_attempted) >= 3:  # max 2 hops
            return None

        for pt in self._failover_chain:
            if pt in _attempted:
                continue
            # Check if provider is registered and has a key
            if pt not in provider_registry.list_providers():
                continue
            _prov = provider_registry.get_provider(pt)
            try:
                if _prov.validate_api_key():
                    return pt
            except Exception:
                continue
        return None

    def _get_default_model_for_provider(self, provider_type: Any, original_model: str) -> str:
        """
        Map a provider type to a sensible default model when failing over.
        Tries to keep the same "tier" (e.g. small -> small).
        """
        from src.ai.providers import ProviderType
        provider_enum = cast(ProviderType, provider_type)
        _model_lower = original_model.lower() if original_model else ""
        _is_small = any(x in _model_lower for x in ['mini', 'nano', 'small', 'lite', 'flash'])

        _defaults = {
            ProviderType.MISTRAL:     'mistral-large-latest',
            ProviderType.SILICONFLOW: 'Qwen/Qwen3-VL-32B-Instruct' if not _is_small else 'Qwen/Qwen3-VL-8B-Instruct',
            ProviderType.DEEPSEEK:    'deepseek-v4-pro',
            ProviderType.OPENAI:      'gpt-5.4',
            ProviderType.MIMO:        original_model,
            ProviderType.OPENROUTER:  'anthropic/claude-opus-4-8',
            ProviderType.ALIBABA:     'qwen-turbo' if _is_small else 'qwen3.7-plus',
        }
        return _defaults.get(provider_enum, original_model)

    # ── Proxy routing: Check if model should use subscription proxy ──
    
    def _should_use_proxy(self, model_id: str) -> bool:
        """All LLM models are BYOK — proxy not needed."""
        return False

    # ============================================================
    # MULTI-TURN AGENTIC LOOP  (the core of the bridge)
    # ============================================================

    async def _call_llm(
        self,
        message: str,
        context: Optional[Dict[str, Any]] = None,
        images: Optional[List[str]] = None,
    ) -> Optional[str]:
        """
        Multi-turn agentic loop:
          1. Send system prompt + conversation history + user message to LLM.
          2. LLM streams text tokens and/or tool-call deltas.
          3. Execute each tool call; emit tool_activity signals.
          4. Append tool results and loop until LLM gives a plain text answer.
        """
        context = context or {}
        images  = images  or []

        # Reset failover state for this call
        self._failover_attempted: Set[Any] = set()
        self._failover_exhausted = False
        # Per-request mutation progress counters.
        self._mutation_success_count = 0
        self._last_todo_mutation_count = -1
        # Reset auto-cancel tracking
        self._todos_auto_cancelled = False
        # Reset per-turn file read counter (prevents excessive reading)
        self._tool_ctx._budget_tracker.reset_turn_counter()
        # Reset agent safety guard for new turn (P1-P4, P8)
        self._safety.reset_turn()
        # Reset auto-continue tracking (only on fresh user request, not on re-entry)
        if not getattr(self, '_auto_continue_requested', False):
            self._auto_continue_cycle = 0
            self._auto_continue_compacted = None
            self._last_cycle_session_mutations = self._session_mutation_count
            # ── Reset stale-tracking counters so manual Continue gives
            #     the AI a clean slate rather than re-triggering the
            #     staleness gate immediately on the next turn limit.
            self._continue_cycle_count = 0
            self._last_pending_ids = set()
            if hasattr(self, '_last_pending_count'):
                self._last_pending_count = 0
            # Reset verification-block counter on fresh user requests
            self._verification_block_count = 0

        merged = {**context, **self._enhancement_data}  # Live enhancement_data wins for model/provider mid-switch

        try:
            from src.ai.providers import get_provider_registry, ProviderType, ChatMessage as PCM

            registry      = get_provider_registry()
            
            # Determine provider type based on model
            model_id = merged.get("model_id", merged.get("model", "mistral-large-latest"))
            # Safety-net: if neither enhancement_data nor context have model_id, read from settings
            if model_id == "mistral-large-latest" and not merged.get("model_id"):
                try:
                    from src.config.settings import get_settings
                    _settings: Any = get_settings()
                    _ai_section: Any = cast(Dict[str, Any], _settings).get('ai') if isinstance(_settings, dict) else None
                    _settings_model: Optional[str] = cast(Optional[str], cast(Dict[str, Any], _ai_section).get('model_id')) if isinstance(_ai_section, dict) else None
                    if _settings_model:
                        model_id = _settings_model
                        log.info(f"[BRIDGE] Model resolved from settings fallback: {model_id}")
                except Exception:
                    pass
            model_lower = model_id.lower() if model_id else ""
            
            # Update tool context with current model (for model-aware file limits)
            if model_id != getattr(self, '_current_model_id', None):
                self._current_model_id = model_id
                self._tool_ctx.set_model(model_id)
                log.info(f"[BRIDGE] Updated tool context for model: {model_id}")

            # ── Model-aware context limits ─────────────────────────────────────
            # Derive all budget constants from the model's actual context window so
            # every supported LLM is handled correctly without hardcoded magic numbers.
            try:
                from src.ai.model_limits import get_model_limits, describe_model_limits
                _limits = get_model_limits(model_id)
                log.info(f"[BRIDGE] {describe_model_limits(model_id)}")
            except Exception as _lim_err:
                log.warning(f"[BRIDGE] model_limits import failed, using defaults: {_lim_err}")
                class _FallbackLimits:
                    max_output_tokens      = 32_000
                    max_tool_result_chars  = 15_000
                    max_hist_chars         = 20_000
                    max_turns              = 35
                _limits = _FallbackLimits()
            
            # Models requiring Responses API (removed - no longer supported)
            # needs_responses = any(x in model_lower for x in ["codex", "gpt-5", "o1", "o3"])
            
            # Determine provider type based on model ID
            provider_type = ProviderType.DEEPSEEK  # Default (fallback for unmatched models)

            # Reject removed/local provider prefixes — route to default instead
            if model_lower.startswith("ollama/"):
                log.warning(f"[BRIDGE] Ollama provider removed — falling back to default for '{model_id}'")
                provider_type = ProviderType.DEEPSEEK
                model_id = "deepseek-v4-pro"
            # OpenRouter model IDs use "provider/model-name" format (e.g. anthropic/claude-sonnet-4-5)
            elif "/" in model_lower:
                provider_type = ProviderType.OPENROUTER
            elif model_lower.startswith("deepseek"):
                provider_type = ProviderType.DEEPSEEK
            elif model_lower.startswith("mistral") or model_lower.startswith("codestral"):
                provider_type = ProviderType.MISTRAL
            elif model_lower.startswith("mimo-"):
                provider_type = ProviderType.MIMO
            elif "siliconflow" in model_lower:
                provider_type = ProviderType.SILICONFLOW
            elif (model_lower.startswith("qwen") or model_lower.startswith("qwq")):
                # Bare Qwen/QwQ names (qwen-max, qwen-plus, qwen-turbo, qwen3.x, qwq…)
                # → Alibaba Model Studio (DashScope). SiliconFlow's own Qwen models
                # use the "Qwen/…" slash format and route to OpenRouter above.
                provider_type = ProviderType.ALIBABA
            elif model_lower.startswith("gpt-"):
                provider_type = ProviderType.OPENAI
            
            provider = registry.get_provider(provider_type)
            model    = model_id

            # If get_provider returned a different type (e.g. MIMO→MISTRAL
            # because the provider failed to register), map the model name so
            # the actual provider receives a model it understands.
            _actual_type = provider.provider_type
            if _actual_type != provider_type:
                model = self._get_default_model_for_provider(_actual_type, model_id)
                log.warning(
                    f"[BRIDGE] Provider {provider_type.value} unavailable — "
                    f"routed to {_actual_type.value} with model={model}"
                )

            log.info(f"[BRIDGE] provider={provider_type.value} model={model}")

            # ── Proxy routing: Use Cortex server for included models ──
            _use_proxy = self._should_use_proxy(model_id)
            if _use_proxy:
                log.info(f"[BRIDGE] Model {model_id} will use subscription proxy")

            # ── Build initial message list ─────────────────────
            # Fast-path: for very simple messages (e.g. greetings), skip the heavy
            # IDE system prompt + history + tool schema. This reduces payload size
            # and improves time-to-first-token on slow/latent providers.
            _simple_query = False
            try:
                _simple_query = self._is_simple_query(message)
            except Exception:
                _simple_query = False

            # Store current user message for semantic memory search
            self._last_user_message = message
            # Reset per-session Bash nudge flag
            self._bash_nudge_already_fired = False

            # ── IMMEDIATE USER MESSAGE SAVE TO SQLITE ──────────────────────
            # Persist user message instantly so it survives ANY crash.
            self._ensure_conversation_id()
            self._save_message_immediate('user', message)

            # ── MANUAL "save on memory.md" TRIGGER ──────────────────────────
            # Detect explicit user request to save conversation to MEMORY.md
            _msg_lower = message.strip().lower()
            _memory_triggers = (
                'save on memory', 'save to memory', 'save memory.md',
                'save context', 'save on memory.md', 'save to memory.md',
                'checkpoint', 'save checkpoint',
            )
            if _msg_lower in _memory_triggers or any(
                _msg_lower.startswith(t) for t in _memory_triggers
            ):
                log.info(f"[BRIDGE] Manual memory save triggered by user: '{message}'")
                try:
                    self._safe_emit(
                        self.agent_status_update,
                        'saving_memory',
                        'Saving conversation to MEMORY.md...'
                    )
                    # Build current messages list for checkpoint
                    with self._history_lock:
                        _hist_snapshot = list(self._conversation_history)
                    _checkpoint = self._create_context_checkpoint(
                        _hist_snapshot, user_message=message
                    )
                    self._safe_emit(
                        self.agent_status_update,
                        'ready',
                        f'Saved to MEMORY.md ({len(_checkpoint)} chars)'
                    )
                    log.info(f"[BRIDGE] Manual memory save complete: {len(_checkpoint)} chars")
                except Exception as _mem_err:
                    log.error(f"[BRIDGE] Manual memory save failed: {_mem_err}", exc_info=True)
                    self._safe_emit(
                        self.agent_status_update, 'ready',
                        f'Memory save failed: {_mem_err}'
                    )
                # Return a confirmation to the user (don't send to LLM)
                return (
                    f"Conversation checkpoint saved to MEMORY.md. "
                    f"The checkpoint includes your current task, todo progress, "
                    f"files read/modified, and a conversation digest. "
                    f"You can start a new chat and I will pick up from where we left off."
                )

            messages: List[Any]

            # ── Mutation progress detector ─────────────────────────────────────
            # Track if agent has performed ANY write/edit operation
            # Cursor-style: NO forcing — the AI decides when to explore vs edit.
            # Only track for todo completion verification and hallucination detection.
            # IMPORTANT: Initialize BEFORE the _pre_compacted/_simple_query/else
            # branches so all code paths can read these variables.
            _mutation_turns = 0
            _turns_since_last_mutation = 0
            _has_mutated = False
            _turn_had_mutation = False  # Reset per-turn; set to True at L5290 if tools mutated
            self._research_nudge_count = 0  # Research-mode detector reset

            # ── Auto-continue: use pre-compacted messages from previous cycle ──
            _pre_compacted = self._auto_continue_compacted
            if _pre_compacted:
                messages = list(_pre_compacted)
                self._auto_continue_compacted = None
                self._auto_continue_requested = False  # Clear flag now that we're consuming the compacted messages
                log.info(f"[BRIDGE] Auto-continue: using pre-compacted messages ({len(messages)} messages)")
                # Append continuation user message
                messages.append(PCM(role="user", content=message))
                if images:
                    messages[-1].images = images
                system_prompt = merged.get("system_prompt") or self._build_system_prompt(context)
                tool_defs = _get_tool_definitions()
                log.info(f"[BRIDGE] Total tools after merge: {len(tool_defs)}")
                max_turns = _limits.max_turns
                try:
                    _max_turns_env = int(os.environ.get("CORTEX_MAX_AGENT_TURNS", "50"))
                    if _max_turns_env > 0:
                        max_turns = min(max_turns, _max_turns_env)
                except Exception:
                    pass
            elif _simple_query:
                system_prompt = (
                    "You are Cortex AI Chat inside a coding IDE. "
                    "Answer the user directly and concisely. "
                    "Do not mention internal tools or system details."
                )
                user_msg = PCM(role="user", content=message)
                if images:
                    user_msg.images = images
                messages = [
                    PCM(role="system", content=system_prompt),
                    user_msg,
                ]
                tool_defs = []
                log.info("[BRIDGE] Simple-query fast path: skipping tools + history + project prompt")
                max_turns = 1
            else:
                system_prompt = merged.get("system_prompt") or self._build_system_prompt(context)
                messages = [PCM(role="system", content=system_prompt)]

                # Inject conversation history (last 10 turns, reduced from 20).
                # Truncate very large messages (e.g. pasted file contents) so the
                # Continue run does not re-pay the full context cost of the first request.
                _MAX_HIST_CONTENT = _limits.max_hist_chars  # scaled to model context window
                with self._history_lock:
                    _all_history = list(self._conversation_history)  # snapshot under lock
                _hist_turns = _all_history[-10:]
                
                # Inject summary if we truncated older messages
                if len(_all_history) > 10:
                    _omitted_count = len(_all_history) - 10
                    _summary = f"[Earlier history: {_omitted_count} messages omitted. Key context from above is preserved below.]"
                    messages.append(PCM(role="system", content=_summary))
                
                for hist_msg in _hist_turns:
                    if hist_msg.role in ("user", "assistant"):
                        hist_content = hist_msg.content or ""
                        has_tool_calls = bool(hist_msg.tool_calls)
                        # Skip empty assistant messages (no content + no tool_calls)
                        # — these cause Mistral API errors
                        if hist_msg.role == 'assistant' and not hist_content and not has_tool_calls:
                            continue
                        if len(hist_content) > _MAX_HIST_CONTENT:
                            hist_content = (
                                hist_content[:_MAX_HIST_CONTENT]
                                + f"\n... [context trimmed: {len(hist_msg.content) - _MAX_HIST_CONTENT} chars omitted]"
                            )
                        cm = PCM(role=hist_msg.role, content=hist_content)
                        if getattr(hist_msg, "reasoning_content", None):
                            cm.reasoning_content = hist_msg.reasoning_content
                        if has_tool_calls:
                            cm.tool_calls = hist_msg.tool_calls
                        messages.append(cm)
                    elif hist_msg.role == "tool":
                        hist_content = hist_msg.content or ""
                        if len(hist_content) > _MAX_HIST_CONTENT:
                            hist_content = hist_content[:_MAX_HIST_CONTENT] + "\n... [context trimmed]"
                        messages.append(
                            PCM(role="tool", content=hist_content,
                                tool_call_id=hist_msg.tool_call_id)
                        )

                # Current user turn
                messages.append(PCM(role="user", content=message))

                tool_defs = _get_tool_definitions()
                log.info(f"[BRIDGE] Total tools after merge: {len(tool_defs)}")
                max_turns = _limits.max_turns
                # Keep agent loops bounded for responsiveness. Can be overridden.
                try:
                    # Higher default gives agents room to complete multi-file tasks
                    # without hitting the continue wall on small/medium projects.
                    _max_turns_env = int(os.environ.get("CORTEX_MAX_AGENT_TURNS", "50"))
                    if _max_turns_env > 0:
                        max_turns = min(max_turns, _max_turns_env)
                except Exception:
                    pass

            full_response = ""

            # Reset self-talk filter state for new request
            self._selftalk_buffer = ''
            self._in_selftalk = False
            self._todo_exit_reminder_fired = False
            # Reset progress-tracking state so resume starts clean
            self._turns_since_progress = 0
            self._research_nudge_count = 0

            # ── Circuit breaker & Tool execution engine (Phase B) ──
            # Both are persistent instances owned by the bridge.
            _cb: ToolCircuitBreaker = self._tool_circuit_breaker
            _executor: ToolExecutionEngine = self._tool_executor

            _compacted_once = False  # Track if we already compacted
            _mistral_downgraded_once = False  # Per-request, timeout-triggered model fallback within Mistral

            # ── OpenAI reasoning-only first pass flag ──────────────────────
            # GPT-5.x on Chat Completions can NOT use reasoning_effort + tools
            # together (returns 400). On turn 0, we do a reasoning-only pass
            # (tools dropped → reasoning_effort enabled → thought card renders).
            # Then we retry with full tools for the actual agentic work.
            _reasoning_only_pass = False

            # ── Auto-compact state (ported from Claude Code's autoCompact.ts) ───
            _auto_compact_state = None
            try:
                from src.ai.conversation_compactor import AutoCompactState
                _auto_compact_state = AutoCompactState()
            except ImportError:
                pass

            # ── PRESSURE: Throttle-only, NEVER abort ──
            # HIGH:     Standard throttle (GC + compact to 4 msgs + 2.5s sleep)
            # CRITICAL: Aggressive throttle (GC + compact to 2 msgs + 6-8s sleep)
            # Recovery adapts to driver: RAM → GC/compact, CPU → aggressive yield.
            # No retry loops, no abort, no user notification — just like Qoder/VSCode.
            # The IDE slows down but NEVER stops working.
            _consecutive_high_pressure_turns = 0
            _PRESSURE_ABORTED = False
            # Cache the stability engine reference once per _call_llm invocation
            _stability_engine = None
            try:
                from src.core.stability_engine import get_stability_engine, PressureLevel
                _stability_engine = get_stability_engine()
            except Exception:
                pass

            for turn in range(max_turns):
                log.info(f"[BRIDGE] === Agentic turn {turn + 1}/{max_turns} ===")
                self._agentic_turn_active = True  # Block warmup flush during AI editing
                # Track per-turn tool success so we only count successful mutations
                self._tool_call_success: Dict[str, bool] = {}

                # ── STABILITY: Breathe-and-resume between turns ──
                # Modeled after Qoder/VSCode/Cursor: the agent NEVER pauses or
                # aborts — it only throttles harder under pressure. The IDE
                # stays alive and keeps working, just slower.
                #
                # Pressure tiers (throttle-only, no abort):
                #   HIGH     → standard throttle (GC + compact + 2.5s sleep)
                #   CRITICAL → aggressive throttle (GC + compact + 6s sleep)
                # Recovery strategy adapts to the pressure DRIVER:
                #   RAM-driven  → GC + microcompact + cache clear
                #   CPU-driven  → aggressive sleep (yield to scheduler)
                #   Both        → combined strategy
                if _stability_engine is not None:
                    try:
                        _current_pressure = _stability_engine.current_pressure
                        _health = _stability_engine.last_health

                        if _current_pressure == PressureLevel.CRITICAL:
                            # ── CRITICAL: Hardest throttle, NEVER abort ──
                            # Same recovery actions as HIGH but more aggressive.
                            # No retry loop, no abort, no user notification.
                            # Just throttle hard and keep working — like Qoder.
                            _ram_driven = _health.ram_percent >= 90.0
                            _cpu_driven = _health.cpu_percent >= 95.0
                            _driver = ("RAM" if _ram_driven else "") + \
                                      ("+" if _ram_driven and _cpu_driven else "") + \
                                      ("CPU" if _cpu_driven else "")

                            _consecutive_high_pressure_turns += 1
                            log.warning(
                                f"[BRIDGE] CRITICAL ({_driver}) — aggressive throttle "
                                f"(turn #{_consecutive_high_pressure_turns} under pressure, "
                                f"RAM={_health.ram_percent:.1f}% CPU={_health.cpu_percent:.1f}%)"
                            )

                            # Save state — safety net
                            _stability_engine.emergency_save("agent_pressure_throttle")

                            # ── RAM-driven: aggressive compaction ──
                            if _ram_driven:
                                gc.collect()
                                try:
                                    from src.ai.conversation_compactor import microcompact_messages
                                    messages, _ = microcompact_messages(messages, keep_recent=2)
                                except Exception:
                                    pass
                                try:
                                    if hasattr(self, '_tool_ctx') and self._tool_ctx:
                                        if hasattr(self._tool_ctx, 'tool_cache'):
                                            self._tool_ctx.tool_cache.clear()
                                except Exception:
                                    pass

                            # ── CPU-driven: max yield to OS scheduler ──
                            if _cpu_driven:
                                time.sleep(8.0)  # CPU-bound → very long yield
                            else:
                                time.sleep(6.0)  # RAM-only → aggressive GC pause

                            _stability_engine.breathe()
                            if _ram_driven:
                                gc.collect()

                            # ── NEVER ABORT ──
                            # CRITICAL pressure just throttles harder. The agent
                            # keeps working at reduced speed — same as Qoder/VSCode.
                            # No user-visible notification. No pause. No abort.

                        elif _current_pressure == PressureLevel.HIGH:
                            # ── HIGH: Adaptive throttling, never abort ──
                            # The agent keeps working but slows down to let the
                            # system recover. Strategy adapts to the driver.
                            _ram_driven_high = _health.ram_percent >= 80.0
                            _cpu_driven_high = _health.cpu_percent >= 90.0
                            _driver_h = ("RAM" if _ram_driven_high else "") + \
                                        ("+" if _ram_driven_high and _cpu_driven_high else "") + \
                                        ("CPU" if _cpu_driven_high else "")

                            _consecutive_high_pressure_turns += 1
                            log.warning(
                                f"[BRIDGE] HIGH pressure ({_driver_h}) — throttling "
                                f"(turn #{_consecutive_high_pressure_turns} under HIGH, "
                                f"RAM={_health.ram_percent:.1f}% CPU={_health.cpu_percent:.1f}%)"
                            )

                            # ── CPU-driven: aggressive yield ──
                            if _cpu_driven_high:
                                time.sleep(4.0)  # CPU-bound → longer sleep to yield scheduler
                            else:
                                time.sleep(2.5)  # Standard pause

                            _stability_engine.breathe()

                            # ── RAM-driven: compaction + GC ──
                            if _ram_driven_high:
                                gc.collect()
                                try:
                                    from src.ai.conversation_compactor import microcompact_messages
                                    messages, _ = microcompact_messages(messages, keep_recent=4)
                                except Exception:
                                    pass

                            # ── NO HARD LIMIT ──
                            # HIGH pressure never aborts the agent. It just throttles
                            # harder. The agent keeps working at reduced speed.

                        else:
                            # Pressure dropped — reset counter
                            if _consecutive_high_pressure_turns > 0:
                                log.info(
                                    f"[BRIDGE] Pressure normalized after "
                                    f"{_consecutive_high_pressure_turns} turns"
                                )
                            _consecutive_high_pressure_turns = 0
                    except Exception:
                        pass

                
                # ── INDUSTRY-STANDARD: Agent Phase Tracking ─────────────────
                # Track which phase the agent is in to enforce proper workflow
                # Phases: READING → PLANNING → IMPLEMENTING → VERIFYING → DONE
                if turn == 0:
                    _agent_phase = "READING"
                elif _has_mutated and len(self._current_todos) == 0:
                    _agent_phase = "VERIFYING"
                elif _has_mutated:
                    _agent_phase = "IMPLEMENTING"
                elif self._current_todos:
                    _agent_phase = "PLANNING"
                else:
                    _agent_phase = "READING"
                
                # Log phase transitions
                if turn == 0 or _agent_phase != getattr(self, '_last_agent_phase', ''):
                    log.info(f"[BRIDGE] Agent phase: {_agent_phase}")
                    self._last_agent_phase = _agent_phase

                # ── Micro-compact: clear old tool results (cheap, no LLM) ────
                # Ported from Claude Code's microCompact.ts. Runs every turn
                # to keep context lean by clearing stale tool result content.
                # keep_recent scales with budget: 1M models keep 24 results (~12 turns)
                # instead of 16 (~8 turns) so recent file content isn't lost.
                if turn > 0:
                    try:
                        from src.ai.conversation_compactor import microcompact_messages
                        _mc_keep = 24 if _budget >= 800_000 else 16
                        messages, _mc_saved = microcompact_messages(messages, keep_recent=_mc_keep)
                        if _mc_saved > 0:
                            log.info(f"[BRIDGE] Micro-compact saved ~{_mc_saved:,} tokens on turn {turn + 1}")
                    except Exception as _mc_err:
                        log.debug(f"[BRIDGE] Micro-compact skipped: {_mc_err}")

                # ── Emit token budget update to UI ─────────────────────────
                _est_tokens = self._estimate_message_tokens(messages)
                _budget = getattr(_limits, 'context_budget', 100_000)
                _prov_label = provider_type.value if hasattr(provider_type, 'value') else str(provider_type)
                try:
                    self._safe_emit(self.context_budget_update, int(_est_tokens), int(_budget), _prov_label)
                except Exception:
                    pass  # UI signal failures must never break the loop

                # ── Pre-overflow detection ──────────────────────────────────
                # Estimate current context usage before sending to LLM.
                # If approaching the limit, proactively compact instead of
                # waiting for the API to reject with a context_length error.
                # Threshold scales with context: 1M models can run to 85%,
                # smaller models stay at 75% to avoid API rejections.
                if turn > 0:  # Skip first turn (messages are fresh)
                    _usage_pct = _est_tokens / max(_budget, 1)
                    # Larger budget → higher threshold (more headroom before compacting)
                    _threshold = 0.85 if _budget >= 800_000 else 0.80 if _budget >= 400_000 else 0.75
                    if _usage_pct > _threshold:
                        if not _compacted_once:
                            log.warning(
                                f"[BRIDGE] Pre-overflow: {_est_tokens:,} tokens estimated ({_usage_pct:.0%} of {_budget:,} budget) — compacting proactively"
                            )
                            self._safe_emit(
                                self.agent_status_update,
                                'compacting',
                                f'Context {_usage_pct:.0%} full — checkpointing and compacting...'
                            )
                            messages = self._compact_messages(messages, PCM)
                            _compacted_once = True
                        elif _usage_pct > (_threshold + 0.07):
                            # Already compacted once and STILL over threshold+7% — aggressive trim
                            log.warning(
                                f"[BRIDGE] Post-compact overflow: {_est_tokens:,} tokens ({_usage_pct:.0%}) — aggressive trim"
                            )
                            messages = self._compact_messages(messages, PCM)

                # ── Periodic progressive summarization (every 3 turns) ────
                if turn > 0 and turn % 3 == 0 and not _compacted_once:
                    try:
                        from src.ai.conversation_compactor import compact_if_needed
                        messages, _was_compacted = compact_if_needed(
                            messages, _est_tokens, _budget,
                            self._compact_messages, PCM,
                            state=_auto_compact_state, keep_recent=8
                        )
                        if _was_compacted:
                            _compacted_once = True
                            log.info(f"[BRIDGE] Periodic compact on turn {turn + 1}")
                    except Exception as _pc_err:
                        log.debug(f"[BRIDGE] Periodic compact skipped: {_pc_err}")

                # ── Periodic MEMORY.md save (every 100 turns) ────
                # Saves conversation state to MEMORY.md periodically so
                # context survives crashes without waiting for compaction.
                if turn > 0 and turn % 100 == 0:
                    try:
                        _checkpoint = self._create_context_checkpoint(messages)
                        log.info(f"[BRIDGE] Periodic MEMORY.md save on turn {turn + 1} ({len(_checkpoint)} chars)")
                    except Exception as _ps_err:
                        log.debug(f"[BRIDGE] Periodic MEMORY.md save skipped: {_ps_err}")

                # ── Stream LLM response (with context-compaction retry) ────
                # tool_acc is reset inside the compact-attempt retry loop below
                turn_text  = ""
                turn_reasoning = ""
                _thought_started = False
                _explore_started = False
                # ── INDUSTRY-STANDARD: Start with core toolset ────
                # Only load 11 core tools initially to:
                # 1. Save tokens (~10,000 tokens per turn)
                # 2. Reduce AI confusion
                # 3. Speed up responses (3-5s vs 10-15s)
                # WebSearch/WebFetch are always included since they're
                # read-only and don't require file mutations to be useful.
                # Expand to full set only after mutations occur
                core_names = {
                    "Read", "Write", "Edit", "Glob", "Grep", "LS", "Bash",
                    "TodoWrite", "AskUserQuestion", "WebSearch", "WebFetch",
                }
                
                # After AI has made mutations, expand toolset slightly
                # but DON'T dump all 19 tools — only add LSP
                # for research/navigation, keep task/MCP/team tools excluded
                # to save ~500+ lines of JSON per turn.
                if self._session_mutation_count > 5:
                    allowed_names = core_names | {"LSP"}
                else:
                    allowed_names = core_names
                active_tool_defs = _filter_tool_definitions(list(tool_defs), allowed_names)
                
                if self._session_mutation_count > 5:
                    log.info(
                        f"[TOOLS] Expanded to {len(active_tool_defs)} tools " +
                        f"after {self._session_mutation_count} mutations"
                    )

                # ── OpenAI GPT-5.x reasoning/tools incompatibility ──
                # GPT-5.x on Chat Completions can NOT use reasoning_effort + tools
                # together (returns 400: "use /v1/responses instead").
                # Workaround A: drop tools on the final answer turn so reasoning
                #   activates (todos done + mutations complete → genuine exit).
                # Workaround B: on turn 0, do a reasoning-first pass (no tools) so
                #   the thought card renders. Then retry with full tools for agentic
                #   work (force-action path re-adds tools on next iteration).
                if provider_type == ProviderType.OPENAI and active_tool_defs:
                    _todos_done = all(
                        str(t.get("status", "")).upper() in ("COMPLETE", "CANCELLED")
                        for t in self._current_todos
                    ) if self._current_todos else True
                    # Final answer turn: todos done + has mutations → drop tools → reasoning
                    if _has_mutated and _todos_done:
                        log.info("[BRIDGE] OpenAI final-answer turn — dropping tools to enable reasoning")
                        active_tool_defs = None
                    # First turn: reasoning-first pass → thought card renders, then retry with tools
                    elif turn == 0 and not _reasoning_only_pass:
                        log.info("[BRIDGE] OpenAI turn 0 — reasoning-first pass (no tools, thought card enabled)")
                        active_tool_defs = None
                        _reasoning_only_pass = True

                # Context-length errors are retried up to 2 times per turn by
                # compacting the message history before each retry.
                _CTX_ERR_KEYWORDS = (
                    'input is too long', 'context_length_exceeded',
                    'context length',    'prompt_too_long',
                    'too many tokens',   'maximum context',
                    'token limit',       'tokens exceed',
                    'request too large', 'content too large',
                )

                # Callback passed to the provider so we get notified before each
                # internal retry (timeout / rate-limit) and can show the user a
                # status note without waiting for the retry to succeed or fail.
                def _retry_notify(attempt_num: int, max_att: int, err_type: str) -> None:
                    if err_type == 'timeout':
                        msg = 'API timeout - retrying (%d/%d)...' % (attempt_num, max_att)
                    elif err_type == 'rate_limit':
                        msg = 'Rate limit hit - waiting before retry (%d/%d)...' % (attempt_num, max_att)
                    else:
                        msg = 'API error - retrying (%d/%d)...' % (attempt_num, max_att)
                    log.info('[BRIDGE] Provider retry: %s' % msg)
                    self._safe_emit(self.agent_status_update, 'retrying', msg)

                for _compact_attempt in range(3):  # attempt 0, 1, 2
                    tool_acc: Dict[int, Dict[str, Any]] = {}  # idx -> {id, name, arguments}
                    # Track which Write/Edit streaming cards have been shown to UI
                    _streaming_write_announced: set = set()
                    # Track last announced arg sizes for progress updates (idx → last_size)
                    _streaming_last_size: Dict[int, int] = {}
                    turn_text = ""
                    turn_reasoning = ""
                    _thought_started = False
                    _explore_started = False
                    # ── Thinking loop detection (industry standard: token budget) ──
                    # Claude/Anthropic uses budget_tokens to bound thinking, not time.
                    # When thinking exceeds the token budget without producing content
                    # or tool calls, inject a directive to force action.
                    _thinking_budget_tokens = int(os.environ.get("CORTEX_THINKING_BUDGET_TOKENS", "32000"))
                    _reasoning_token_count = 0  # approximate token count (chars / 4)
                    _has_received_content_or_tools = False
                    _thinking_budget_exceeded = False  # flag to prevent repeated warnings
                    try:
                        # Get max_tokens from model_limits
                        # Auto-escalate output cap during auto-continue cycles
                        # so the agent has more headroom for large code generation.
                        try:
                            from src.ai.model_limits import get_escalated_max_output_tokens
                            _ac_cycle = getattr(self, '_auto_continue_cycle', 0)
                            # Escalation: 0 cycles = default, 1-2 = moderate, 3+ = full
                            _esc_level = 0 if _ac_cycle == 0 else (1 if _ac_cycle <= 2 else 2)
                            max_tokens = get_escalated_max_output_tokens(model_id, _esc_level)
                            if _esc_level > 0:
                                log.info(f"[BRIDGE] Escalated output cap to {max_tokens:,} (level {_esc_level}, cycle {_ac_cycle})")
                        except Exception:
                            max_tokens = _limits.max_output_tokens
                        
                        # Apply performance mode token multiplier if set
                        try:
                            from src.config.settings import get_settings
                            settings_any: Any = get_settings()
                            _raw_mult: Any = 1.0
                            _raw_ai = settings_any.get("ai")
                            if isinstance(_raw_ai, dict):
                                _ai_map: Any = cast(Dict[str, Any], _raw_ai)
                                _raw_mult = _ai_map.get("token_multiplier", 1.0)
                            token_multiplier = float(_raw_mult) if isinstance(_raw_mult, (int, float, str)) else 1.0
                            if token_multiplier != 1.0:
                                calculated_tokens = int(max_tokens * token_multiplier)
                                if calculated_tokens > max_tokens:
                                    # Multiplier stale from previous session/model — clamp silently
                                    calculated_tokens = max_tokens
                                    # Reset stale multiplier so future turns aren't noisy
                                    settings_any.set("ai", "token_multiplier", "1.0")
                                else:
                                    max_tokens = calculated_tokens
                        except Exception as _mult_err:
                            pass  # Use base max_tokens if multiplier not available
                        
                        _chat_kwargs: Dict[str, Any] = {
                            "retry_callback": _retry_notify
                        }
                        # Per-provider retry config — ensures DeepSeek/MiMo get
                        # the same retry resilience as Mistral.
                        if active_tool_defs:
                            _chat_kwargs["max_retries"] = 3
                        elif provider_type == ProviderType.MISTRAL:
                            _chat_kwargs["max_retries"] = 3

                        # MiMo: enable deep thinking so reasoning streams to thought cards.
                        # MiMo defaults to thinking=enabled for pro/v2.5 models,
                        # which produces reasoning_content in the SSE stream.
                        # This is required for thought cards to display AI thinking.
                        # Users can disable via env override CORTEX_MIMO_THINKING=disabled.
                        if provider_type == ProviderType.MIMO:
                            _mimo_thinking_override = os.environ.get("CORTEX_MIMO_THINKING", "enabled")
                            if _mimo_thinking_override in ("disabled", "enabled"):
                                _chat_kwargs["thinking"] = {"type": _mimo_thinking_override}

                        # Adaptive output budget (Cursor-style standardized):
                        # - Tool turns: 4096 min (enough for tool args + reasoning)
                        # - Write/Edit turns: 32768 (large file content)
                        # - Thinking: 32000 (budget-bounded)
                        # - Final answer: 16384 (generous)
                        # Always respect model cap (max_tokens) and remaining context budget.
                        if active_tool_defs:
                            remaining_ctx = max(0, int(_budget - _est_tokens))
                            # ── Write/Edit/CreateFile need large output budget ──
                            # These tools take a "content" parameter that can be
                            # 30K+ chars (7,500+ tokens). The lean cap MUST be
                            # lifted, otherwise the LLM hits the token ceiling
                            # before finishing the arguments → empty args → failure.
                            _large_content_tool_names = {"Write", "Edit", "CreateFile"}
                            _has_large_content_tool = any(
                                td.get("function", {}).get("name") in _large_content_tool_names
                                for td in active_tool_defs
                            )
                            if _has_large_content_tool:
                                # Write/Edit turns need room for FULL file content.
                                # A landing page HTML is 30,000+ chars (7,500+ tokens).
                                # Allow up to model max or remaining context — whichever is smaller.
                                stream_max_tokens = min(max_tokens, max(32_768, remaining_ctx // 2)) if remaining_ctx else max_tokens
                                log.info(f"[BRIDGE] Large-content tools active (Write/Edit) — stream cap raised to {stream_max_tokens}")
                            else:
                                # ── STANDARDIZED TOOL TURN CAPS (Cursor-style) ──
                                # All tool turns get 4096 minimum — enough for:
                                # - Tool call arguments (file paths, search patterns, commands)
                                # - Brief reasoning about what to do next
                                # - Multiple tool calls in one turn
                                # Below 4096, args get truncated and AI falls back to text-only.
                                stream_max_tokens = min(max_tokens, max(4_096, remaining_ctx // 4))
                        else:
                            # Final answer / no-tool turns — allow generous output.
                            # CRITICAL: When thinking is enabled, reasoning tokens
                            # share the max_tokens budget with visible content.
                            # A 16K cap means the model spends most tokens on
                            # hidden reasoning, leaving almost nothing for the
                            # visible response (truncated output like "help you today?").
                            # Use the full model max when thinking is active.
                            _thinking_enabled = (
                                isinstance(_chat_kwargs.get('thinking'), dict)
                                and _chat_kwargs['thinking'].get('type') == 'enabled'
                            )
                            if _thinking_enabled:
                                stream_max_tokens = max_tokens  # use full model cap (e.g. 131K)
                                log.info(f'[BRIDGE] Thinking mode active — output cap: {stream_max_tokens:,} (reasoning + content share this budget)')
                            else:
                                stream_max_tokens = min(max_tokens, 16_384)

                        log.debug(f"[BRIDGE] Adaptive stream cap: {stream_max_tokens} (tool_turn={bool(active_tool_defs)}, est={_est_tokens}, budget={_budget}, base_max={max_tokens})")

                        # Batch event-loop yields: only yield every N chunks to
                        # reduce overhead while still delivering stop/cancel signals.
                        _chunk_batch = 0
                        _CHUNK_BATCH_SIZE = 5
                        # Sanitize messages: strip orphaned tool_calls that would cause 400 errors
                        try:
                            messages = _sanitize_tool_call_messages(messages)
                        except Exception as _san_err:
                            log.warning(f"[BRIDGE] Sanitizer failed (non-fatal): {_san_err} — sending messages as-is")

                        # Initialize pending tool calls list (used by both proxy and direct paths)
                        pending: List[Dict[str, Any]] = []

                        # ── PROXY PATH: Use Cortex server for subscription models ──
                        if _use_proxy:
                            try:
                                from src.core.cortex_api import get_api_client
                                api = get_api_client()
                                
                                # Check if user is logged in with valid token
                                if not api.is_logged_in() or not api.access_token:
                                    error_msg = (
                                        "**Login Required for Subscription Models**\n\n"
                                        "Your subscription requires authentication. Please:\n"
                                        "1. Go to Settings → Profile\n"
                                        "2. Click 'Sign in with Browser'\n"
                                        "3. Complete login\n\n"
                                        "Or add your own API key in Settings → Models & Providers"
                                    )
                                    self._safe_emit(self.response_chunk, error_msg)
                                    return ""
                                
                                # Get license key from server subscription
                                sub_info = api.get_subscription()
                                log.info(f"[PROXY] Subscription info: {sub_info}")
                                license_key = ""
                                if sub_info:
                                    license_key = sub_info.get("license_key", "")
                                
                                log.info(f"[PROXY] License key found: {bool(license_key)}, length: {len(license_key)}")
                                
                                if not license_key:
                                    error_msg = (
                                        "**Subscription License Key Missing**\n\n"
                                        "Your subscription doesn't have a license key assigned.\n"
                                        "Please contact support or try logging out and back in.\n\n"
                                        "Or add your own API key in Settings → Models & Providers"
                                    )
                                    self._safe_emit(self.response_chunk, error_msg)
                                    return ""
                                
                                # Convert messages to format for proxy (preserve tool_call_id and tool_calls)
                                proxy_messages = []
                                for m in messages:
                                    msg_dict = {}
                                    if hasattr(m, 'role'):
                                        msg_dict["role"] = m.role
                                    elif isinstance(m, dict):
                                        msg_dict["role"] = m.get("role", "user")
                                    
                                    # Content
                                    content = getattr(m, 'content', None) if hasattr(m, 'content') else (m.get('content') if isinstance(m, dict) else None)
                                    if content:
                                        msg_dict["content"] = content
                                    
                                    # Tool calls (assistant messages)
                                    tool_calls = getattr(m, 'tool_calls', None) if hasattr(m, 'tool_calls') else (m.get('tool_calls') if isinstance(m, dict) else None)
                                    if tool_calls:
                                        msg_dict["tool_calls"] = tool_calls
                                    
                                    # Tool call ID (tool result messages)
                                    tool_call_id = getattr(m, 'tool_call_id', None) if hasattr(m, 'tool_call_id') else (m.get('tool_call_id') if isinstance(m, dict) else None)
                                    if tool_call_id:
                                        msg_dict["tool_call_id"] = tool_call_id
                                    
                                    # Name (tool messages)
                                    name = getattr(m, 'name', None) if hasattr(m, 'name') else (m.get('name') if isinstance(m, dict) else None)
                                    if name:
                                        msg_dict["name"] = name
                                    
                                    if msg_dict.get("content") or msg_dict.get("tool_calls") or msg_dict.get("tool_call_id"):
                                        proxy_messages.append(msg_dict)
                                
                                log.info(f"[PROXY] Calling proxy: model={model_id}, messages={len(proxy_messages)}, license_key={license_key[:20]}...")
                                proxy_result = api.proxy_chat(
                                    model=model_id,
                                    messages=proxy_messages,
                                    license_key=license_key,
                                    temperature=0.7,
                                    tools=active_tool_defs if active_tool_defs else None,
                                )
                                log.info(f"[PROXY] Proxy result: {proxy_result}")
                                if proxy_result and proxy_result.get("status") == "success":
                                    # Extract response from proxy
                                    choices = proxy_result.get("choices", [])
                                    if choices:
                                        msg = choices[0].get("message", {})
                                        content = msg.get("content", "")
                                        tool_calls = msg.get("tool_calls", [])
                                        
                                        # Handle tool calls from proxy
                                        if tool_calls:
                                            _has_received_content_or_tools = True
                                            # Convert proxy tool_calls to bridge format
                                            for tc in tool_calls:
                                                pending.append({
                                                    "id": tc.get("id", ""),
                                                    "function": {
                                                        "name": tc.get("function", {}).get("name", ""),
                                                        "arguments": tc.get("function", {}).get("arguments", "{}"),
                                                    }
                                                })
                                            log.info(f"[PROXY] Received {len(tool_calls)} tool calls from proxy")
                                        
                                        # Handle text content
                                        if content:
                                            turn_text = content
                                            full_response = (full_response or "") + content
                                            _has_received_content_or_tools = True
                                            # Emit the response
                                            self._safe_emit(self.response_chunk, content)
                                    
                                    # Log billing info
                                    billing = proxy_result.get("billing", {})
                                    log.info(f"[PROXY] Credits used: {billing.get('credits_deducted', 0)}, remaining: {billing.get('credits_remaining', 0)}")
                                else:
                                    # Proxy failed - check if user has own API key as fallback
                                    from src.core.key_manager import KeyManager
                                    km = KeyManager()
                                    has_own_key = False
                                    for prefix, km_name in [("deepseek", "deepseek"), ("mimo", "mimo"), ("mistral", "mistral")]:
                                        if model_id.lower().startswith(prefix):
                                            if km.get_key(km_name):
                                                has_own_key = True
                                                break
                                    
                                    if has_own_key:
                                        log.warning(f"[PROXY] Proxy failed, falling back to BYOK")
                                        _use_proxy = False  # Fall through to direct call
                                    else:
                                        error_msg = (
                                            "**Subscription Model Unavailable**\n\n"
                                            "Could not connect to Cortex server. Please:\n"
                                            "• Check your internet connection\n"
                                            "• Verify your subscription is active\n"
                                            "• Or add your own API key in Settings → Models & Providers"
                                        )
                                        self._safe_emit(self.response_chunk, error_msg)
                                        return ""
                            except Exception as proxy_err:
                                log.warning(f"[PROXY] Proxy error: {proxy_err}")
                                # Check if user has own API key as fallback
                                from src.core.key_manager import KeyManager
                                km = KeyManager()
                                has_own_key = False
                                for prefix, km_name in [("deepseek", "deepseek"), ("mimo", "mimo"), ("mistral", "mistral")]:
                                    if model_id.lower().startswith(prefix):
                                        if km.get_key(km_name):
                                            has_own_key = True
                                            break
                                
                                if has_own_key:
                                    log.warning(f"[PROXY] Proxy error, falling back to BYOK")
                                    _use_proxy = False
                                else:
                                    error_msg = (
                                        "**Subscription Model Unavailable**\n\n"
                                        f"Error: {str(proxy_err)[:100]}\n\n"
                                        "Please add your own API key in Settings → Models & Providers"
                                    )
                                    self._safe_emit(self.response_chunk, error_msg)
                                    return ""
                        
                        # ── DIRECT PATH: Use provider directly (BYOK) ──
                        if not _use_proxy:
                            # Check if provider has API key
                            if not provider._api_key:
                                # No API key available - show helpful error
                                model_display = model_id.replace("-", " ").title()
                                error_msg = (
                                    f"**{model_display} — API Key Required**\n\n"
                                    f"No API key found for this model.\n\n"
                                    f"**To use this model:**\n"
                                    f"1. Go to Settings → Models & Providers\n"
                                    f"2. Add your API key for this provider\n\n"
                                    f"**Get API keys from:**\n"
                                    f"• MiMo: https://platform.xiaomimimo.com\n"
                                    f"• DeepSeek: https://platform.deepseek.com\n"
                                    f"• OpenAI: https://platform.openai.com/api-keys\n"
                                    f"• OpenRouter: https://openrouter.ai/keys\n"
                                    f"• Qwen: https://dashscope.console.aliyun.com/apiKey"
                                )
                                self._safe_emit(self.response_chunk, error_msg)
                                return ""
                            
                            for chunk in provider.chat_stream(
                                messages, model=model, max_tokens=stream_max_tokens, tools=active_tool_defs, **_chat_kwargs
                            ):
                                # Respect a stop request from the user
                                if self._stop_requested:
                                    log.info("[BRIDGE] Stream interrupted by stop request")
                                    break
                                # Yield to the event loop every N chunks so stop/cancel
                                # signals are delivered without per-chunk overhead.
                                _chunk_batch += 1
                                if _chunk_batch >= _CHUNK_BATCH_SIZE:
                                    _chunk_batch = 0
                                    await asyncio.sleep(0)
                                if isinstance(chunk, str) and chunk.startswith("__TOOL_CALL_DELTA__:"):
                                    _has_received_content_or_tools = True  # stop thinking timeout
                                    delta_list = json.loads(chunk[20:])
                                    # DIAGNOSTIC: Log tool call deltas at INFO for Write/Edit, DEBUG for others
                                    for _td in delta_list:
                                        _fn = _td.get("function", {})
                                        _args_val = _fn.get("arguments", "")
                                        _tname = _fn.get("name", "")
                                        _is_write_edit = _tname in ("Write", "Edit")
                                        if _args_val:
                                            if _is_write_edit:
                                                log.info(f"[TOOL-DELTA] {_tname} args chunk: idx={_td.get('index')}, args_len={len(_args_val) if isinstance(_args_val, str) else 'N/A'}")
                                            else:
                                                log.debug(f"[TOOL-DELTA] idx={_td.get('index')}, name={_tname}, args_len={len(_args_val) if isinstance(_args_val, str) else 'N/A'}")
                                        elif _is_write_edit:
                                            # EMPTY args delta for Write/Edit — normal during streaming
                                            # Arguments arrive in subsequent chunks, this is expected behavior
                                            log.debug(f"[TOOL-DELTA] {_tname} args pending idx={_td.get('index')} — waiting for arguments")
                                    # Emit exploreStart on first tool call delta
                                    if not _explore_started and delta_list:
                                        _explore_started = True
                                        try:
                                            self._safe_emit(self.exploreStart)
                                        except Exception:
                                            pass
                                    for td in delta_list:
                                        idx = td.get("index", 0)
                                        if idx not in tool_acc:
                                            tool_acc[idx] = {"id": "", "name": "", "arguments": ""}
                                        if td.get("id"):
                                            tool_acc[idx]["id"] = td["id"]
                                        if td.get("function", {}).get("name"):
                                            tool_acc[idx]["name"] = td["function"]["name"]
                                        if td.get("function", {}).get("arguments"):
                                            tool_acc[idx]["arguments"] += td["function"]["arguments"]
                                        
                                        # ── Streaming Write/Edit: show live file-creation card ──
                                        _tname = tool_acc[idx].get("name", "")
                                        if _tname in ("Write", "Edit") and idx not in _streaming_write_announced:
                                            _streaming_write_announced.add(idx)
                                            # Try to extract file_path from partial args
                                            _partial_args = tool_acc[idx].get("arguments", "")
                                            _fp = ""
                                            try:
                                                _fp_match = re.search(r'"file_path"\s*:\s*"([^"]+)"', _partial_args)
                                                if _fp_match:
                                                    _fp = _fp_match.group(1)
                                            except Exception:
                                                pass
                                            _activity_type = "write_file_streaming" if _tname == "Write" else "edit_file_streaming"
                                            _info = json.dumps({
                                                "file_path": _fp or "(streaming)",
                                                "tool": _tname,
                                                "size": len(_partial_args),
                                            })
                                            try:
                                                self._safe_emit(self.tool_activity, _activity_type, _info, "streaming")
                                            except Exception:
                                                pass
                                            _streaming_last_size[idx] = len(_partial_args)
                                            log.info(
                                                f"[STREAM-WRITE] Started streaming card for {_tname}: "
                                                f"fp={_fp or '?unknown?'}, initial_size={len(_partial_args)}"
                                            )
                                        elif _tname in ("Write", "Edit") and idx in _streaming_write_announced:
                                            # Periodic progress update every ~8KB of new content
                                            _cur_size = len(tool_acc[idx].get("arguments", ""))
                                            _last_size = _streaming_last_size.get(idx, 0)
                                            if _cur_size - _last_size >= 2048:
                                                _streaming_last_size[idx] = _cur_size
                                                _partial_args = tool_acc[idx].get("arguments", "")
                                                _fp = ""
                                                try:
                                                    _fp_match = re.search(r'"file_path"\s*:\s*"([^"]+)"', _partial_args)
                                                    if _fp_match:
                                                        _fp = _fp_match.group(1)
                                                except Exception:
                                                    pass
                                                _activity_type = "write_file_streaming" if _tname == "Write" else "edit_file_streaming"
                                                _info = json.dumps({
                                                    "file_path": _fp or "(streaming)",
                                                    "tool": _tname,
                                                    "size": _cur_size,
                                                    "progress": True,
                                                })
                                                try:
                                                    self._safe_emit(self.tool_activity, _activity_type, _info, "streaming")
                                                except Exception:
                                                    pass
                                elif isinstance(chunk, str) and chunk.startswith("__REASONING_DELTA__:"):
                                    _reason_chunk = chunk[len("__REASONING_DELTA__:"):]
                                    turn_reasoning += _reason_chunk
                                    # Reasoning is stored in turn_reasoning (→ reasoning_content in
                                    # the assistant message). It is NOT added to turn_text or
                                    # full_response — doing so would leak internal monologue into the
                                    # conversation history, causing the AI to see its own thoughts as
                                    # prior output and hallucinate/repeat them on subsequent turns.
                                    # Reasoning streams to the thought card UI via response_chunk tags
                                    # (parsed by native_chat_bridge → thinking_delta → chat_panel).
                                    # Emit thoughtStart on first reasoning chunk.
                                    if not _thought_started:
                                        _thought_started = True
                                        try:
                                            self._safe_emit(self.response_chunk, '<cortex_thought_start>')
                                        except Exception:
                                            pass
                                    # ── Thinking budget: detect infinite reasoning loops ──
                                    # Industry standard (Claude/Anthropic): bound thinking by
                                    # token count, not time. When thinking exceeds budget
                                    # without producing content or tool calls, force action.
                                    _reasoning_token_count += len(_reason_chunk) // 4  # approximate
                                    if not _has_received_content_or_tools and not _thinking_budget_exceeded and _reasoning_token_count > _thinking_budget_tokens:
                                        _thinking_budget_exceeded = True
                                        log.warning(
                                            f"[BRIDGE] Thinking budget exceeded: {_reasoning_token_count:,} tokens "
                                            f"(budget={_thinking_budget_tokens:,}) — closing thought card, continuing naturally."
                                        )
                                        # Close the thought card — let the model continue on its own
                                        try:
                                            self._safe_emit(self.response_chunk, '<cortex_thought_end>')
                                        except Exception:
                                            pass
                                        _thought_started = False
                                        continue  # SKIP emitting this thinking chunk — stop the stream
                                    if _thinking_budget_exceeded:
                                        continue  # Drop all thinking chunks after budget exceeded
                                    try:
                                        self._safe_emit(self.response_chunk,
                                            f'<cortex_thought_delta>{_reason_chunk}</cortex_thought_delta>')
                                    except Exception:
                                        pass
                                # NOTE: Do NOT also emit tool_activity for reasoning chunks.
                                # The response_chunk path (above) already routes thinking text
                                # via native_chat_bridge -> thinking_delta -> chat_panel.
                                # A duplicate tool_activity emission causes every word to appear
                                # twice in the thinking card (word doubling bug).
                                else:
                                    # Normal text chunk (not tool call, not reasoning)
                                    _has_received_content_or_tools = True  # stop thinking timeout
                                    # Emit thoughtEnd via response_chunk when response text starts flowing
                                    if _thought_started:
                                        _thought_started = False
                                        try:
                                            self._safe_emit(self.response_chunk, '<cortex_thought_end>')
                                        except Exception:
                                            pass
                                    # Normal text content — stream via response_chunk
                                    # so native_chat_bridge routes it as text_delta in real-time.
                                    turn_text += chunk
                                    full_response += chunk
                                    _yielded_content = True
                                    log.info(f"[BRIDGE] text chunk: len={len(chunk)}, preview={repr(chunk[:80])}")
                                    try:
                                        self._safe_emit(self.response_chunk, chunk)
                                    except Exception:
                                        pass
                                    # Emit exploreEnd when response text starts (all tools resolved)
                                    if _explore_started:
                                        _explore_started = False
                                        try:
                                            self._safe_emit(self.exploreEnd)
                                        except Exception:
                                            pass
                                    # Clean thought/reasoning content from stored text
                                    _clean = re.sub(r'<think[^>]*>.*?</think>', '', chunk, flags=re.DOTALL)
                                    _clean = re.sub(r'<thinking[^>]*>.*?</thinking>', '', _clean, flags=re.DOTALL)
                                    _clean = re.sub(r'<antThinking[^>]*>.*?</antThinking>', '', _clean, flags=re.DOTALL)
                                    _clean = re.sub(r'<scratchpad[^>]*>.*?</scratchpad>', '', _clean, flags=re.DOTALL)
                                    _clean = re.sub(r'<task_summary[^>]*>.*?</task_summary>', '', _clean, flags=re.DOTALL)
                                    # If _clean had XML tags stripped, replace the chunk in turn_text
                                    _diff = len(chunk) - len(_clean)
                                    if _diff > 0:
                                        turn_text = turn_text[:-len(chunk)] + _clean
                                        full_response = full_response[:-len(chunk)] + _clean
                        break  # stream completed (or stop requested) — exit retry loop

                    except Exception as _stream_exc:
                        # ── Thinking budget exceeded: just continue naturally ──
                        if isinstance(_stream_exc, _ThinkingTimeoutError):
                            log.info(
                                f"[BRIDGE] Thinking budget exceeded on turn {turn + 1} — "
                                f"continuing naturally (attempt {_compact_attempt + 1}/3)"
                            )
                            continue  # retry — thought card already closed

                        _err_lower = str(_stream_exc).lower()
                        _is_ctx_err = any(kw in _err_lower for kw in _CTX_ERR_KEYWORDS)
                        _RATE_LIMIT_KEYWORDS = (
                            'rate limit', 'rate_limit', '429', 'too many requests',
                            'quota exceeded', 'insufficient_quota', 'billing',
                            'no credits', 'exceeded your current quota',
                            'tpd_quota_exhausted', 'tpd rate limit',
                        )
                        _TIMEOUT_KEYWORDS = (
                            'timed out', 'timeout', 'read timed out',
                            'connect timeout', 'connection timed out',
                        )
                        _CONNECTION_KEYWORDS = (
                            '10054', 'connection', 'forcibly closed', 'reset',
                            'broken pipe', 'connection aborted', 'remote host',
                            'connection reset', 'connection refused',
                        )
                        _is_rate_err = any(kw in _err_lower for kw in _RATE_LIMIT_KEYWORDS)
                        _is_timeout_err = any(kw in _err_lower for kw in _TIMEOUT_KEYWORDS)
                        _is_connection_err = any(kw in _err_lower for kw in _CONNECTION_KEYWORDS)
                        if _is_ctx_err and _compact_attempt < 2:
                            log.warning(
                                f"[BRIDGE] Context limit on turn {turn + 1} (compact attempt {_compact_attempt + 1}/2): {_stream_exc}"
                            )
                            self._safe_emit(
                                self.agent_status_update,
                                'compacting',
                                'Context window exceeded - compacting history (%d/2), retrying...' % (_compact_attempt + 1)
                            )
                            messages = self._compact_messages(messages, PCM)
                            # P0: Re-inject persistent directives after compaction
                            # These safety directives survive compaction.
                            try:
                                messages.append(PCM(role="system", content=PERSISTENT_DIRECTIVES))
                                log.info("[SAFETY] Persistent directives re-injected after compaction")
                            except Exception:
                                pass
                            continue   # retry with compacted history
                        elif (_is_rate_err or _is_timeout_err) and not getattr(self, '_failover_exhausted', False):
                            # Mistral-only recovery: on timeout with large model, downgrade model tier once.
                            if (
                                _is_timeout_err
                                and provider_type == ProviderType.MISTRAL
                                and isinstance(model, str)
                                and "large" in model.lower()
                                and not _mistral_downgraded_once
                            ):
                                _mistral_downgraded_once = True
                                old_model = model
                                model = "mistral-medium-latest"
                                log.warning(
                                    f"[BRIDGE] Timeout on {old_model} — retrying with {model} (Mistral-only fallback)"
                                )
                                self._safe_emit(
                                    self.agent_status_update,
                                    'retrying',
                                    f'Timeout on {old_model} — retrying with {model}...'
                                )
                                continue

                            # ── Provider auto-failover on rate-limit/timeout ───
                            _next = self._get_failover_provider(provider_type, registry)
                            if _next is not None:
                                _old_name = provider_type.value
                                provider_type = _next
                                provider = registry.get_provider(provider_type)
                                # Re-derive model for new provider
                                model = self._get_default_model_for_provider(provider_type, model_id)
                                reason = "rate limited" if _is_rate_err else "timed out"
                                log.warning(
                                    f"[BRIDGE] Provider {_old_name} {reason} — failing over to {provider_type.value} (model={model})"
                                )
                                self._safe_emit(
                                    self.agent_status_update,
                                    'failover',
                                    f'Provider {_old_name} {reason} — switching to {provider_type.value}...'
                                )
                                continue  # retry with new provider
                            else:
                                # ── Chunk timeout: connection stalled mid-response ──
                                # This is different from rate-limit — the API connection
                                # is dead. Don't show misleading "rate limited" message.
                                _is_chunk_timeout = "chunk timeout" in _err_lower
                                if _is_chunk_timeout:
                                    _chunk_msg = (
                                        f"[SYSTEM: DeepSeek API connection stalled mid-response. "
                                        f"The server stopped sending data. "
                                        f"Please try again or switch to a different model.]"
                                    )
                                    turn_text = _chunk_msg
                                    full_response += _chunk_msg
                                    self._safe_emit(self.response_chunk, _chunk_msg)
                                    self._failover_exhausted = False
                                    break  # exit compact-attempt loop gracefully
                                # ── Rate-limit failover exhausted: don't raise —
                                # set turn_text so bridge lets AI exit gracefully
                                # instead of forcing action mode (death loop).
                                self._failover_exhausted = True
                                _rate_msg = (
                                    f"[SYSTEM: Provider rate-limited. "
                                    f"TPD limit reached (current: 1.5M+, limit: 1.5M). "
                                    f"Please switch to a different model or wait for reset.]"
                                )
                                turn_text = _rate_msg
                                full_response += _rate_msg
                                self._safe_emit(self.response_chunk, _rate_msg)
                                # Reset failover flag so next turn can retry fresh
                                self._failover_exhausted = False
                                break  # exit compact-attempt loop gracefully
                        elif _is_connection_err:
                            # ── Connection errors: graceful handling ──
                            # Connection errors (10054, connection reset, etc.) are
                            # usually transient. Show user-friendly message instead of crashing.
                            _conn_msg = (
                                f"[SYSTEM: Connection to AI provider was lost: {_stream_exc}. "
                                f"This is usually a temporary network issue. "
                                f"Please try again or switch to a different model.]"
                            )
                            log.warning(f"[BRIDGE] Connection error (handled gracefully): {_stream_exc}")
                            turn_text = _conn_msg
                            full_response += _conn_msg
                            self._safe_emit(self.response_chunk, _conn_msg)
                            break  # exit compact-attempt loop gracefully
                        else:
                            # ── Graceful error handling for provider failures ──
                            # Instead of crashing, show the error to the user and
                            # let the chat continue. This handles:
                            # - Connection errors (10054, connection reset, etc.)
                            # - HTTP 400 errors from upstream providers
                            # - Other transient network failures
                            _err_msg = str(_stream_exc)
                            _is_connection_err = any(kw in _err_msg.lower() for kw in (
                                '10054', 'connection', 'forcibly closed', 'reset',
                                'broken pipe', 'connection aborted', 'remote host',
                            ))
                            _is_http_400 = 'http 400' in _err_msg.lower() or 'bad request' in _err_msg.lower()
                            
                            if _is_connection_err or _is_http_400:
                                _user_msg = (
                                    f"[SYSTEM: Connection to AI provider failed: {_err_msg[:150]}. "
                                    f"Please try again or switch to a different model.]"
                                )
                                log.warning(f"[BRIDGE] Provider connection error (handled gracefully): {_err_msg[:200]}")
                                turn_text = _user_msg
                                full_response += _user_msg
                                self._safe_emit(self.response_chunk, _user_msg)
                                break  # exit compact-attempt loop gracefully
                            else:
                                raise      # non-context error or exhausted retries

                # P8: Error recovery budget check — track consecutive errors
                try:
                    _err_recovery_msg = self._safety.check_error_recovery(
                        str(_stream_exc) if '_stream_exc' in dir() else ""
                    )
                    if _err_recovery_msg and "STOP" in _err_recovery_msg:
                        log.warning(f"[SAFETY] Error recovery budget exhausted: {_err_recovery_msg[:200]}")
                        full_response += "\n\n" + _err_recovery_msg
                        self._safe_emit(self.response_chunk, _err_recovery_msg)
                        break
                except Exception:
                    pass

                # If stop was requested, abort the entire agentic loop immediately
                if self._stop_requested:
                    log.info("[BRIDGE] Agentic loop aborted by stop request")
                    break

                # Assemble pending tool calls from direct provider response
                # (Skip if proxy already added tool calls)
                if not pending:
                    for idx in sorted(tool_acc):
                        tc = tool_acc[idx]
                        if tc["name"]:
                            pending.append({
                                "index": idx,
                                "id":    tc["id"] or str(_uuid.uuid4()),
                                "function": {
                                    "name":      tc["name"],
                                    "arguments": tc["arguments"],
                                },
                            })

                # If no tool calls → check if we should force action or exit
                if not pending:
                    # ── INDUSTRY-STANDARD: Task Completion Verification ──────
                    # AI cannot exit unless:
                    # 1. It has made mutations (wrote/edited files)
                    # 2. All todos are marked complete
                    # 3. OR it's a legitimate informational response
                    
                    _has_pending_todos = len(self._current_todos) > 0
                    _todos_all_done = all(
                        str(t.get("status", "")).upper() in ("COMPLETE", "CANCELLED") 
                        for t in self._current_todos
                    ) if self._current_todos else True
                    
                    # Case 1: No mutations yet and early turns - FORCE ACTION
                    # SKIP if no tools were provided (simple-query fast path) — the AI
                    # can't take action when it has no tools available.
                    # ALSO skip if AI already produced a meaningful text response
                    # (read-only/informational tasks like "read this file").
                    _has_text_response = len(turn_text.strip()) > 20

                    # ── OpenAI reasoning-only retry ─────────────────────────
                    # After the reasoning-first pass on turn 0 (tools dropped to
                    # enable reasoning_effort), ALWAYS re-run with full tools.
                    # The model has thought aloud (shown in thought card) and now
                    # needs tools to plan/act. Purely informational queries that
                    # produce text on the re-run will exit naturally via Case 3.
                    if _reasoning_only_pass:
                        _reasoning_only_pass = False
                        log.info("[BRIDGE] OpenAI reasoning-only pass complete — re-running with tools")
                        # Tools will be rebuilt at the top of the next iteration
                        # (core_names + active_tool_defs are recomputed there)
                        continue

                    # Case 2: Has mutations but todos still pending — gentle reminder
                    if _has_mutated and _has_pending_todos and not _todos_all_done:
                        _pending_count = sum(
                            1 for t in self._current_todos
                            if str(t.get("status", "")).upper() not in ("COMPLETE", "CANCELLED")
                        )
                        # Only remind once — don't trap in a loop
                        _todo_reminder_fired = getattr(self, '_todo_exit_reminder_fired', False)
                        if not _todo_reminder_fired:
                            self._todo_exit_reminder_fired = True
                            log.info(
                                f"[BRIDGE] {_pending_count} pending todos — gentle reminder"
                            )
                            _verify_msg = (
                                f"Note: {_pending_count} task(s) still pending. "
                                f"Complete or cancel them when ready."
                            )
                            messages.append(PCM(role="user", content=_verify_msg))
                            continue
                    
                    # ── HALLUCINATION GUARD: AI claims action but made no tool calls ──
                    # The AI sometimes outputs <file_edited> tags or natural-language
                    # claims ("I've deleted X", "file has been removed") without actually
                    # calling any tool. Detect this and force real action.
                    _hallucinated_action = False
                    # ── Loop breaker: max 3 consecutive hallucination re-prompts ──
                    # Prevents infinite loops when the model keeps explaining about
                    # files/creation without actually needing to do anything
                    # (e.g. user asks "why did AI hallucinate creating a file?")
                    _hallucination_retry_count = getattr(self, '_hallucination_retry_count', 0)
                    if _has_text_response and _mutation_turns == 0 and _hallucination_retry_count < 3:
                        import re as _re_hall
                        # ── TIGHT patterns: only match first-person claims or
                        # completion assertions, NOT explanatory text ──
                        _HALLUCINATION_PATTERNS = [
                            # XML tags — AI faking tool output
                            _re_hall.compile(r'<file_edited>', _re_hall.I),
                            _re_hall.compile(r'<file_created>', _re_hall.I),
                            # First-person claim: "I've created/deleted/modified..."
                            _re_hall.compile(r'\bI\'?ve\s+(already\s+)?(deleted|removed|created|written|overwritten|modified|edited)\b', _re_hall.I),
                            # "The file has been/is now created/deleted" (definite article + passive)
                            _re_hall.compile(r'\bthe\s+file\s+(has\s+been|is\s+now|was)\s+(deleted|removed|created|written|modified)\b', _re_hall.I),
                            # "successfully created/written/deleted" (completion adverb)
                            _re_hall.compile(r'\bsuccessfully\s+(deleted|removed|created|written|modified|edited)\b', _re_hall.I),
                            # "file no longer exists"
                            _re_hall.compile(r'\bthe\s+file\s+no\s+longer\s+exists\b', _re_hall.I),
                            # "has been deleted/removed/erased" (subject-less passive with "has been")
                            _re_hall.compile(r'\b(?:it|this)\s+has\s+been\s+(deleted|removed|erased)\b', _re_hall.I),
                            # ── Checkmark + filename + action verb ──
                            # "✅ **`file.py` created**" or "✅ file.py created at ..."
                            _re_hall.compile(r'[✅✓✔]\s*[*`]*\S+\.(?:py|js|ts|html|css|json|md|txt|yaml|yml|toml|cfg|ini|sh|bat)\b[*`]*\s*(created|written|deleted|modified|edited|updated)', _re_hall.I),
                            _re_hall.compile(r'[✅✓✔]\s*(`[^`]+`|[*][*][^*]+[*][*])\s*(created|written|deleted|modified|edited|updated)', _re_hall.I),
                            # ── "file.py created at the project root" (claiming creation happened) ──
                            _re_hall.compile(r'(?:created|written|saved)\s+(?:the\s+)?(?:file|content)\s*(?:at|to|in)\s*(?:the\s+)?(?:project|root|directory|path|[A-Z]:)', _re_hall.I),
                        ]
                        for _pat in _HALLUCINATION_PATTERNS:
                            if _pat.search(turn_text):
                                _hallucinated_action = True
                                break

                    if _hallucinated_action:
                        self._hallucination_retry_count = _hallucination_retry_count + 1
                        log.warning(
                            f"[BRIDGE] HALLUCINATION DETECTED on turn {turn + 1} "
                            f"(retry {self._hallucination_retry_count}/3): "
                            f"AI claimed a file operation in text but made ZERO tool calls. "
                            f"Forcing real action."
                        )
                        _hallucination_msg = (
                            "STOP. You claimed to have performed a file operation in your response, "
                            "but you did NOT actually call any tool. Your text is a HALLUCINATION — "
                            "no file was created, deleted, or modified.\n\n"
                            "You MUST use the actual tools (Write, Edit, Bash) to perform real operations. "
                            "Do NOT describe actions you haven't taken. Call the appropriate tool NOW."
                        )
                        messages.append(PCM(role="user", content=_hallucination_msg))
                        continue
                    elif _hallucination_retry_count >= 3:
                        # Loop breaker: model keeps hallucinating, accept the text response
                        log.warning(
                            f"[BRIDGE] Hallucination loop breaker: {self._hallucination_retry_count} "
                            f"consecutive detections on turn {turn + 1} — accepting response to break loop."
                        )

                    # ── General loop breaker: no progress for many turns ─────
                    # Only fires as absolute last resort — AI decides its own workflow.
                    if not hasattr(self, '_turns_since_progress'):
                        self._turns_since_progress = 0
                    if _turn_had_mutation:
                        self._turns_since_progress = 0
                    else:
                        self._turns_since_progress += 1
                    if self._turns_since_progress >= 30:
                        log.warning(
                            f"[BRIDGE] No progress for {self._turns_since_progress} turns "
                            f"on turn {turn + 1} — forcing exit to break loop."
                        )
                        # Force the agent to stop by accepting whatever it has
                        self._turns_since_progress = 0
                        break

                    # Case 2.5: HALLUCINATED file operation — claim without a tool call.
                    # The AI sometimes says "I've deleted/created/edited <file>" (often
                    # with a fabricated <file_edited> tag) WITHOUT ever calling Write/
                    # Edit/Bash. Detect a mutation CLAIM made with zero actual mutations
                    # this request and force a real tool call instead of accepting the lie.
                    if _mutation_turns == 0 and active_tool_defs and (turn + 1) <= 8:
                        import re as _re_claim
                        _claim_pat = _re_claim.compile(
                            r'<file_edited>|\bi(?:\'ve| have)\s+(?:deleted|removed|created|edited|updated|modified|renamed|moved)\b'
                            r'|\b(?:file|it)\s+(?:has been|was)\s+(?:deleted|removed|created|edited|updated|modified)\b'
                            r'|\bno longer exists\b|\bsuccessfully\s+(?:deleted|removed|created)\b'
                            r'|\balready\s+deleted\b'
                            # ── Checkmark + filename + action verb ──
                            # Catches: ✅ **`sample.py` created** / ✅ file.py created at
                            r'|[✅✓✔]\s*[*`]*\S+\.(?:py|js|ts|html|css|json|md|txt|yaml|yml|toml|cfg|ini|sh|bat)\b[*`]*\s*(?:created|written|deleted|modified|edited|updated|removed)'
                            r'|[✅✓✔]\s*(`[^`]+`|[*][*][^*]+[*][*])\s*(?:created|written|deleted|modified|edited|updated|removed)'
                            r'|[✅✓✔]\s*[*`]*\S+[*`]*\s*(?:created|written|deleted|modified|edited|updated|removed)'
                            # ── Backtick filename + past tense verb ──
                            r'|`[^`]+\.\w+`\s+(?:created|written|deleted|modified|edited)',
                            _re_claim.IGNORECASE,
                        )
                        if _claim_pat.search(turn_text or ""):
                            log.warning(
                                f"[BRIDGE] AI CLAIMED a file operation on turn {turn + 1} "
                                f"but made ZERO actual mutations. Forcing a real tool call."
                            )
                            _no_fake_msg = (
                                "You claimed a file was deleted/created/modified, but you did "
                                "NOT call any tool — so NOTHING actually changed on disk. Never "
                                "claim a file operation you didn't perform with a tool, and never "
                                "emit a <file_edited> tag without a matching tool call.\n\n"
                                "Do it for real NOW:\n"
                                "1. For deletion: call the Bash tool with the exact delete "
                                "command (e.g. Remove-Item -LiteralPath '<full path>' -Force).\n"
                                "2. For create/edit: call the Write or Edit tool.\n"
                                "3. After the tool runs, the system verifies the result — only "
                                "then report what actually happened.\n\n"
                                f"User request: {message[:120]}"
                            )
                            messages.append(PCM(role="user", content=_no_fake_msg))
                            continue

                    # Case 3: Legitimate exit - all done or informational
                    log.info(f"[BRIDGE] No tool calls on turn {turn + 1} — done")
                    self._last_turn_reasoning = turn_reasoning
                    break

                log.info(
                    f"[BRIDGE] {len(pending)} tool call(s) on turn {turn + 1}: "
                    + ", ".join(p["function"]["name"] for p in pending)
                )
                # Reset hallucination counter — model made real tool calls
                self._hallucination_retry_count = 0

                # ── Append assistant turn with tool_calls ──────
                # Normalize arguments: empty string or invalid JSON → "{}"
                # Alibaba (qwen-flash) rejects function.arguments that aren't valid JSON.
                assistant_tool_calls: List[Dict[str, Any]] = []
                for tc in pending:
                    _args_raw = tc["function"]["arguments"]
                    if not _args_raw or not str(_args_raw).strip():
                        _args_json = "{}"
                    else:
                        try:
                            json.loads(str(_args_raw))
                            _args_json = str(_args_raw)
                        except (json.JSONDecodeError, TypeError):
                            log.warning(f"[BRIDGE] Invalid JSON in tool arguments for {tc['function']['name']}: {str(_args_raw)[:200]}")
                            _args_json = "{}"
                    assistant_tool_calls.append({
                        "id":   tc["id"],
                        "type": "function",
                        "function": {
                            "name":      tc["function"]["name"],
                            "arguments": _args_json,
                        },
                    })
                # --- Backend text processing pipeline ---
                # Clean AI response text before storing in conversation history.
                # This fixes malformed markdown, LaTeX artifacts, and citation leaks
                # so subsequent turns see clean context.
                if turn_text:
                    turn_text = clean_citation_markers(turn_text)
                    turn_text = clean_latex_output(turn_text)

                messages.append(
                    PCM(
                        role="assistant",
                        content=turn_text or "",
                        tool_calls=assistant_tool_calls,
                        reasoning_content=turn_reasoning or None,
                    )
                )

                # ── Execute tools via ToolExecutionEngine (Phase B) ─
                parsed_calls: List[Tuple[str, str, Any]] = []
                for tc in pending:
                    tool_name: str = str(tc["function"]["name"])
                    tool_id: str = str(tc["id"])
                    raw_args: Any = None
                    try:
                        raw_args = tc["function"]["arguments"]
                        # DIAGNOSTIC: Log raw tool call arguments for Write/Edit at INFO level
                        # to track whether accumulated arguments arrive complete or empty.
                        if tool_name in ("Write", "Edit"):
                            _args_len = len(raw_args) if isinstance(raw_args, (str, dict)) else 0
                            if _args_len == 0:
                                log.error(f"[TOOL-CALL RAW] {tool_name} arguments EMPTY — LLM did not provide any arguments for this tool call")
                            else:
                                log.info(f"[TOOL-CALL RAW] {tool_name} arguments: {_args_len} chars (type={type(raw_args).__name__})")
                        if isinstance(raw_args, dict):
                            # Arguments already parsed as dict (some LLMs do this)
                            args = raw_args
                        elif isinstance(raw_args, str):
                            if raw_args.strip():
                                args = json.loads(raw_args)
                            else:
                                # Empty string → empty args (LLM didn't provide arguments)
                                log.error(f"[TOOL-CALL] {tool_name} received EMPTY arguments string! Tool call ID: {tool_id}")
                                args = {}
                        else:
                            args = cast(Dict[str, Any], {})
                    except json.JSONDecodeError as e:
                        log.error(f"[TOOL-CALL] {tool_name} JSON parse FAILED: {e} | raw_args={str(raw_args)[:500]}")
                        # RECOVERY: Try to extract partial fields from truncated JSON.
                        # When the Mimo stream ends mid-argument, the JSON is cut off
                        # but file_path is usually near the start and still valid.
                        args = self._recover_truncated_tool_args(tool_name, raw_args)
                    parsed_calls.append((tool_name, tool_id, args))

                _nudges = await _executor.execute_turn(
                    parsed_calls,
                    self._execute_single_tool,
                    messages,
                    PCM,
                    _limits,
                )
                for _n in _nudges:
                    messages.append(PCM(role="user", content=_n))

                # ── Inject pending rejection nudges ──
                # When the user rejects an edit multiple times, we tell the AI to stop
                # and ask the user what they want. These nudges are queued by record_rejection().
                if hasattr(self, '_pending_rejection_nudges') and self._pending_rejection_nudges:
                    for _rn in self._pending_rejection_nudges:
                        messages.append(PCM(role="user", content=_rn))
                        log.info(f"[BRIDGE] Injected rejection nudge: {_rn[:80]}...")
                    self._pending_rejection_nudges.clear()

                # ── Inject pending accept nudges ──
                # When the user accepts a deferred edit, the file state changes on disk.
                # Tell the AI to re-read before making more edits to that file.
                if hasattr(self, '_pending_accept_nudges') and self._pending_accept_nudges:
                    for _an in self._pending_accept_nudges:
                        messages.append(PCM(role="user", content=_an))
                        log.info(f"[BRIDGE] Injected accept nudge: {_an[:80]}...")
                    self._pending_accept_nudges.clear()

                # Check stop after tool execution
                if self._stop_requested:
                    log.info("[BRIDGE] Aborting remaining — stop requested")
                    break

                # ── Timeout recovery: detect consecutive tool timeouts ──
                # If 2+ tools timed out in this turn, inject a recovery prompt
                # so the AI doesn't keep retrying the same failing approach.
                _timeout_count = 0
                try:
                    _timeout_count = sum(
                        1 for tc in parsed_calls
                        if "timed out" in str(
                            next((r.error for r in [getattr(self, '_last_tool_results', {}).get(tc[1], ToolResult(tool_id="", result=None, success=False, error=""))] if r and not r.success), "")
                        ).lower()
                    )
                except RuntimeError:
                    log.warning("[BRIDGE] Bridge deleted during timeout check — aborting turn")
                    break
                # Also check by looking at the tool results in messages
                _recent_errors = 0
                for _mi in range(len(messages) - 1, max(0, len(messages) - 10), -1):
                    _m = messages[_mi]
                    if hasattr(_m, 'content') and isinstance(_m.content, str) and 'timed out' in _m.content.lower():
                        _recent_errors += 1
                if _recent_errors >= 2:
                    _recovery_msg = (
                        "[SYSTEM: Multiple tools have timed out. The system may be under heavy load. "
                        "STOP retrying the same approach. Instead:\n"
                        "1. Try a completely different approach (e.g., use Read instead of Bash cat)\n"
                        "2. Skip the failing step and continue with what you CAN do\n"
                        "3. Tell the user what's failing and ask them to try manually\n"
                        "4. Do NOT keep retrying commands that have already timed out]"
                    )
                    messages.append(PCM(role="user", content=_recovery_msg))
                    log.warning(f"[BRIDGE] Injected timeout recovery prompt ({_recent_errors} timeouts detected)")

                log.info(f"[BRIDGE] Tool results sent — continuing to turn {turn + 2}")

                # ── Phase 4: Auto-save session snapshot ──────────────────────
                # Save every 3 turns so we can resume if the app crashes/restarts.
                if turn > 0 and (turn + 1) % 3 == 0:
                    try:
                        from src.core.agent_session_manager import save_snapshot  # pyright: ignore[reportUnknownVariableType]
                        save_snapshot(self)
                    except Exception as _ses_exc:
                        log.warning(f"[SESSION] Auto-save failed on turn {turn + 1}: {_ses_exc}")

                # ── Self-healing debug loop ──────────────────────────────────
                # Phase 2: If a command failed, enter structured debug cycle
                if self._debug_loop.should_enter(self._recent_tool_results):
                    self._debug_loop.enter_debug_cycle()
                    debug_nudge = self._debug_loop.build_nudge_message()
                    if debug_nudge:
                        log.info(
                            f"[DEBUG LOOP] Injecting debug nudge (cycle " +
                            f"{self._debug_loop.cycle_count})"
                        )
                        messages.append(PCM(role="user", content=debug_nudge))
                        # Reset the stop flag so debug can proceed
                        self._stop_requested = False
                        # Continue the turn loop for debugging
                        continue

                # ── Track mutation progress ───────────────────────────────────
                # Check if any tool in this turn was a write/edit/bash operation
                _mutation_tools = {"Write", "Edit", "Bash", "NotebookEdit"}
                _turn_had_mutation = any(
                    (t_name in _mutation_tools) and self._tool_call_success.get(t_id, False)
                    for t_name, t_id, _ in parsed_calls
                )
                if _turn_had_mutation:
                    _mutation_turns += 1
                    _turns_since_last_mutation = 0  # Reset sliding window on mutation
                    _has_mutated = True  # Mark that mutation has occurred
                    _post_mutation_read_count = 0  # Reset read counter after mutation
                    self._research_nudge_count = 0  # Reset research-mode detector
                    
                    # ── Track mutation type for Bash-only detection ──────
                    # Record whether this mutation was a real edit (Write/Edit)
                    # vs a Bash workaround. If the AI relies on Bash for too
                    # many mutations in a row, it's avoiding proper editing tools.
                    if not hasattr(self, '_mutation_type_history'):
                        self._mutation_type_history: List[str] = []
                    _used_real_edit = any(
                        t_name in ("Write", "Edit") and self._tool_call_success.get(t_id, False)
                        for t_name, t_id, _ in parsed_calls
                    )
                    self._mutation_type_history.append("edit" if _used_real_edit else "bash")
                    if len(self._mutation_type_history) > 8:
                        self._mutation_type_history = self._mutation_type_history[-8:]
                    
                    # ── Re-enable read-only tools that were locked by AGGRESSIVE nudge ──
                    _readonly_tools_to_unlock = {"Read", "Grep", "Glob", "LS"}
                    _re_enabled = set()
                    for _rt in _readonly_tools_to_unlock:
                        if _rt in self._disabled_tools:
                            self._disabled_tools.discard(_rt)
                            _re_enabled.add(_rt)
                    if _re_enabled:
                        log.info(
                            f"[BRIDGE] Mutation detected — re-enabled read-only tools: "
                            f"{', '.join(sorted(_re_enabled))}"
                        )
                    
                    # NOTE: Do NOT reset _verification_block_count here.
                    # The verification loop breaker (TodoWrite all_done path) relies on
                    # accumulating block counts across mutations. Resetting it on every
                    # mutation traps the AI in an infinite verify→block→mutate→verify loop.
                    # Only reset on: (a) new user request, (b) when the breaker fires.
                    
                    # Reset aggressive read counter — mutation breaks the read-only streak
                    self._aggressive_read_count = 0
                    
                    # Track which todo this mutation corresponds to
                    if self._current_todos:
                        # Credit ALL IN_PROGRESS todos — a single Write/Edit/Bash often
                        # fulfills multiple tasks (e.g., editing index.html fixes both
                        # emoji placeholders AND CTA banner in one pass).
                        # Without this, only the first IN_PROGRESS todo gets credit
                        # and the rest get falsely blocked as "no mutations".
                        _mutated_any = False
                        for _todo in self._current_todos:
                            if _todo.get("status") == "IN_PROGRESS":
                                _todo["_mutation_count"] = _todo.get("_mutation_count", 0) + 1
                                _mutated_any = True
                                # Clean stale BLOCKED warning from content —
                                # mutations are now credited so the block is resolved.
                                _c = _todo.get("content", "")
                                if " ⚠️ [BLOCKED:" in _c:
                                    _todo["content"] = _c[:_c.index(" ⚠️ [BLOCKED:")]
                                log.info(
                                    f"[TODO] Mutation #{_todo['_mutation_count']} for todo: "
                                    + f"{_todo.get('content', 'unknown')[:50]}"
                                )
                        if not _mutated_any:
                            # No IN_PROGRESS todo — credit the first PENDING one
                            for _todo in self._current_todos:
                                if _todo.get("status") == "PENDING":
                                    _todo["_mutation_count"] = _todo.get("_mutation_count", 0) + 1
                                    log.info(
                                        f"[TODO] Mutation #{_todo['_mutation_count']} for todo (was PENDING): "
                                        + f"{_todo.get('content', 'unknown')[:50]}"
                                    )
                                    break
                
                # ── Post-mutation tracking (no forcing — AI decides workflow) ──
                _read_only_tools = {"Read", "Grep", "Glob", "LS"}
                _turn_is_read_only = all(t_name in _read_only_tools for t_name, _, _ in parsed_calls)

                # Track turns since last mutation (for todo completion only)
                if not _turn_had_mutation:
                    _turns_since_last_mutation += 1

                # ── Research-mode detector: too many read-only turns ──
                # When the agent spends 15+ consecutive turns reading/searching
                # without writing anything, it's stuck in "research mode".
                # Inject a hard nudge to force action.
                if _turns_since_last_mutation >= 15 and _turn_is_read_only and not _has_mutated:
                    _research_nudge_count = getattr(self, '_research_nudge_count', 0)
                    if _research_nudge_count < 2:
                        self._research_nudge_count = _research_nudge_count + 1
                        _research_msg = (
                            f"You've spent {_turns_since_last_mutation} turns reading/searching "
                            f"without creating or editing any file. STOP researching and START "
                            f"writing the document/file NOW using the Write tool. You have enough "
                            f"information — write it based on what you've already read. "
                            f"If you're missing a specific detail, make a best-effort write and "
                            f"note what needs verification. Do NOT read more files."
                        )
                        log.warning(
                            f"[BRIDGE] Research-mode detector: {_turns_since_last_mutation} "
                            f"read-only turns with no mutation — injecting action nudge"
                        )
                        messages.append(PCM(role="user", content=_research_msg))
                        continue
                    elif _research_nudge_count >= 2 and _turns_since_last_mutation >= 25:
                        log.warning(
                            f"[BRIDGE] Research-mode hard breaker: {_turns_since_last_mutation} "
                            f"read-only turns — forcing exit"
                        )
                        break

                # ── Per-message budget enforcement ─────────────────────────────
                # Ported from Claude Code's enforceToolResultBudget().
                # Caps total tool results per turn to prevent N parallel tools
                # from collectively blowing up context.
                try:
                    from src.ai.tool_result_storage import enforce_tool_result_budget as _enforce_tool_result_budget
                    enforce_budget = cast(Callable[[List[Any], Any], List[Any]], _enforce_tool_result_budget)
                    _rep_state = self._tool_ctx.get_content_replacement_state()
                    messages = enforce_budget(messages, _rep_state)
                except Exception as _budget_err:
                    log.debug(f"[BRIDGE] Budget enforcement skipped: {_budget_err}")

                # Emit a paragraph break before the next turn's text so the UI
                # doesn't run the continuation sentence directly onto the previous
                # turn's last word (e.g. "fix this.Now let me check...").
                self.response_chunk.emit("\n\n")

            # ── Pending-todo continuation check with stale detection ──
            # If the turn loop ended with todos still PENDING/IN_PROGRESS,
            # check whether we're stuck in a loop (same todos, no progress).
            # After _MAX_STALE_CYCLES consecutive stale cycles, auto-cancel
            # the stuck todos instead of showing "Continue" again.
            #
            # IMPORTANT: Compare by CONTENT (not IDs) because the model
            # often creates fresh todos with new IDs each cycle even when
            # the actual tasks are identical.  Also track the count of
            # pending items — if the count stays the same or increases
            # across cycles, that's a strong stale signal.
            try:
                _bridge_alive = not _sip_isdeleted(self)
            except Exception:
                _bridge_alive = True
            if _bridge_alive and not self._stop_requested and not _PRESSURE_ABORTED:
                _pending_todos: List[Dict[str, Any]] = [
                    t for t in self._current_todos
                    if str(t.get('status', '')).upper() in ('PENDING', 'IN_PROGRESS')
                ]
                if _pending_todos:
                    # Use content-based fingerprint instead of IDs
                    _cur_fingerprint = set(
                        str(t.get('content', t.get('description', ''))).strip().lower()[:80]
                        for t in _pending_todos
                    )
                    _cur_count = len(_pending_todos)

                    # ── Track mutation progress this cycle ────────────────
                    _current_session_mutations = self._session_mutation_count
                    _mutations_this_cycle = max(0, _current_session_mutations - self._last_cycle_session_mutations)
                    self._last_cycle_session_mutations = _current_session_mutations

                    # Stale if: same content OR same/higher count of pending items
                    _content_same = (_cur_fingerprint == self._last_pending_ids)
                    _count_same = (_cur_count >= getattr(self, '_last_pending_count', 0))
                    # Both checks together = high confidence of no progress
                    if _content_same or (_count_same and self._continue_cycle_count > 0):
                        self._continue_cycle_count += 1
                    else:
                        self._continue_cycle_count = 1
                    self._last_pending_ids = _cur_fingerprint
                    self._last_pending_count = _cur_count

                    if self._continue_cycle_count >= self._MAX_STALE_CYCLES:
                        log.warning(
                            f'[BRIDGE] Stale continue detected: same {len(_pending_todos)} todo(s) pending for {self._continue_cycle_count} cycles — auto-cancelling to prevent infinite loop'
                        )
                        # Auto-cancel the stuck todos
                        for t in self._current_todos:
                            if str(t.get('status', '')).upper() in ('PENDING', 'IN_PROGRESS'):
                                t['status'] = 'CANCELLED'
                        
                        # CRITICAL: Mark that todos were auto-cancelled (not completed)
                        # This prevents false "task complete" notifications
                        self._todos_auto_cancelled = True
                        # Ensure the UI and system notifications do not claim completion
                        self._allow_notification = False
                        try:
                            self.todos_updated.emit(list(self._current_todos), "")
                        except Exception:
                            pass
                        try:
                            _total = len(self._current_todos)
                            _completed = sum(
                                1 for t in self._current_todos
                                if t.get("status") in ("COMPLETE", "CANCELLED")
                            )
                            _pct = int((_completed / _total) * 100) if _total > 0 else 0
                            self.task_progress_update.emit(
                                _completed, _total, _pct, f"{_completed}/{_total} tasks complete ({_pct}%)"
                            )
                        except Exception:
                            pass
                        
                        self._continue_cycle_count = 0
                        self._last_pending_ids = set()
                        # Emit a final note to the user
                        self._safe_emit(
                            self.response_chunk,
                            f'\n\n---\n*Remaining tasks were auto-cancelled after repeated attempts without progress. You can start a new request if needed.*\n'
                        )
                    elif _mutations_this_cycle > 0 and self._auto_continue_cycle < self._MAX_AUTO_CONTINUE_CYCLES:
                        # ── AUTO-CONTINUE: Agent made progress, compact + restart ──
                        # ── Staleness gate: don't auto-continue if the exact same
                        #     todos have been pending across cycles.  Mutations
                        #     without todo resolution = busywork, not progress.
                        if self._continue_cycle_count >= 2:
                            log.info(
                                f'[BRIDGE] Stale continue blocked: same {len(_pending_todos)} todo(s) '
                                f'pending for {self._continue_cycle_count} cycles despite {_mutations_this_cycle} '
                                f'mutations — falling back to manual'
                            )
                            try:
                                _checkpoint = self._create_context_checkpoint(messages)
                            except Exception:
                                _checkpoint = "Context checkpoint unavailable."
                            self._safe_emit(self.turn_limit_hit, _pending_todos, _checkpoint)
                        else:
                            self._auto_continue_cycle += 1
                            log.info(
                                f'[BRIDGE] Auto-continue cycle {self._auto_continue_cycle}/{self._MAX_AUTO_CONTINUE_CYCLES}: '
                                + f'{len(_pending_todos)} todo(s) remaining, {_mutations_this_cycle} mutations this cycle '
                                + f'(session total: {_current_session_mutations})'
                            )
                            self._safe_emit(
                                self.agent_status_update,
                                'auto-continue',
                                f'Auto-compacting conversation — continuing task (cycle {self._auto_continue_cycle}/{self._MAX_AUTO_CONTINUE_CYCLES})...'
                            )
                            # Only compact if we have enough messages to justify it.
                            # _compact_messages keeps the last 25-50 messages (scaled by model).
                            # If fewer than 45 messages, skip compaction to avoid unnecessary
                            # context rewriting — matching Cursor/VSCode behavior where compaction
                            # only fires under real context pressure.
                            if len(messages) > 45:
                                messages = self._compact_messages(messages, PCM)
                            else:
                                log.info(
                                    f'[BRIDGE] Skipping compaction on cycle {self._auto_continue_cycle}: '
                                    f'only {len(messages)} messages (< 45) - plenty of context remaining'
                                )
                            # Store compacted messages for next _call_llm entry
                            self._auto_continue_compacted = list(messages)
                            self._auto_continue_requested = True
                            # ── DO NOT reset stale tracking here — the counter
                            #     must persist across cycles so auto-cancel can
                            #     eventually fire when the AI spins on the same
                            #     todos without resolving them.
                            #
                            #     _continue_cycle_count and _last_pending_ids
                            #     are only reset when the fingerprint changes
                            #     (see else-branch at line ~3660).
                        # ── Reset circuit breaker state between cycles ──
                        # Tool failures in previous cycle are often due to context
                        # exhaustion, not real tool problems. A fresh cycle with
                        # compacted context deserves a clean slate.
                        _disabled_before = len(self._disabled_tools)
                        if _disabled_before > 0:
                            self._tool_circuit_breaker.reset()
                            log.info(
                                f'[BRIDGE] Circuit breaker reset: {_disabled_before} disabled tool(s) '
                                f're-enabled for auto-continue cycle {self._auto_continue_cycle}'
                            )
                        # Also reset post-mutation read counter and Bash overuse history
                        self._post_mutation_read_count = 0
                        if hasattr(self, '_bash_usage_history'):
                            self._bash_usage_history = []
                        if hasattr(self, '_mutation_type_history'):
                            self._mutation_type_history = []
                        log.info(
                            f'[BRIDGE] Auto-continue requested — _handle_chat will re-enter _call_llm'
                        )
                    else:
                        # ── MANUAL FALLBACK: No progress or auto-continue exhausted ──
                        log.info(
                            f'[BRIDGE] {len(_pending_todos)} todos still pending — '
                            f'mutations this cycle={_mutations_this_cycle}, '
                            f'auto-continue={self._auto_continue_cycle}/{self._MAX_AUTO_CONTINUE_CYCLES} — emitting turn_limit_hit'
                        )
                        # Build context checkpoint so the AI has memory when resumed
                        try:
                            _checkpoint = self._create_context_checkpoint(messages)
                        except Exception:
                            _checkpoint = "Context checkpoint unavailable."
                        self._safe_emit(self.turn_limit_hit, _pending_todos, _checkpoint)
                else:
                    # All done — reset stale tracking
                    self._continue_cycle_count = 0
                    self._last_pending_ids = set()

            # ── Phase 4: Save/clear snapshot on session exit ────────────
            try:
                from src.core.agent_session_manager import save_snapshot, clear_snapshot  # pyright: ignore[reportUnknownVariableType]
                _pending = [t for t in self._current_todos
                            if t.get("status") in ("PENDING", "IN_PROGRESS")]
                if _pending:
                    # Still has pending work — save snapshot for resume later
                    save_snapshot(self)
                else:
                    # All done — clean up snapshot
                    clear_snapshot()
            except Exception as _ses_exc:
                log.warning(f"[SESSION] Exit save failed: {_ses_exc}")

            # ── Usage Tracker: record token usage for this request ──────
            try:
                _ut = getattr(self, '_usage_tracker', None)
                if _ut and full_response:
                    _prompt_est = len(str(message)) // 4 if message else 0
                    _completion_est = len(full_response) // 4
                    _model_id = getattr(self, '_current_model_id', None) or 'unknown'
                    _ut.record_token_usage(_model_id, _prompt_est, _completion_est)
                    log.debug(f"[USAGE] Recorded {_prompt_est}+{_completion_est} tokens, model={_model_id}")
                    
                    # Record reasoning if thinking was used
                    _turn_reasoning = getattr(self, '_last_turn_reasoning', None)
                    if _turn_reasoning and len(_turn_reasoning) > 10:
                        _ut.record_reasoning_level("high")
                        log.debug(f"[USAGE] Recorded reasoning: {len(_turn_reasoning)} chars")
            except Exception as _ut_err:
                log.debug(f"[USAGE] Token tracking skipped: {_ut_err}")

            return full_response

        except Exception as exc:
            # Known provider errors (credits, quota, model unavailable) — log cleanly
            # without full traceback to avoid flooding error.txt with noise.
            _exc_str = str(exc).lower()
            _is_known = any(kw in _exc_str for kw in (
                "credits insufficient", "insufficient credits", "quota",
                "payment required", "model not available", "402",
            ))
            if _is_known:
                log.error(f"[BRIDGE] _call_llm failed: {exc}")
            else:
                log.error(f"[BRIDGE] _call_llm failed: {exc}", exc_info=True)
            raise  # Let _handle_chat route this through error_occurred → onError in JS

    async def call_llm(
        self,
        message: str,
        context: Optional[Dict[str, Any]] = None,
        images: Optional[List[str]] = None,
    ) -> Optional[str]:
        """
        Public wrapper for _call_llm.
        Provides controlled access for inner classes (AgentWorker).
        """
        return await self._call_llm(message, context, images)

    def _safe_emit(self, signal: Any, *args: Any) -> None:
        """Emit a PyQt signal only if the C++ object is still alive.
        
        Includes signal rate limiting to prevent Qt event-loop flooding
        from crashing the IDE. If more than 50 signals are emitted in
        100ms, subsequent signals are silently dropped.
        """
        # Strip NULL bytes from string arguments — some LLMs embed \x00
        args = tuple(
            a.replace('\x00', '') if isinstance(a, str) and '\x00' in a else a
            for a in args
        )
        # ── Critical signal bypass ──────────────────────────────
        # session_saved_to_memory and agent_status_update('ready') must never
        # be rate-limited — dropping them causes stuck UI (New Chat spinner).
        # response_chunk must also never be dropped — dropping causes empty chat.
        _is_critical = (
            signal is self.session_saved_to_memory
            or signal is self.agent_status_update
            or signal is self.response_chunk
        )

        # ── Signal rate limiter ──────────────────────────────────
        # Prevents the worker thread from flooding the Qt event loop
        # with signals during high-throughput tool execution.
        # Instead of dropping signals, we batch them and emit the latest one.
        _now = time.monotonic()
        if not hasattr(self, '_signal_rate_window_start'):
            self._signal_rate_window_start = _now
            self._signal_rate_count = 0
            self._signal_batch = {}
        if _now - self._signal_rate_window_start < 0.1:  # 100ms window
            self._signal_rate_count += 1
            if self._signal_rate_count > 50 and not _is_critical:
                # Batch non-critical signal — will emit latest at end of window
                self._signal_batch[signal] = args
                if self._signal_rate_count == 51:
                    log.info(
                        f"[SIGNAL-FLOOD] Rate limit hit: {self._signal_rate_count} signals/100ms — "
                        f"batching non-critical signals"
                    )
                return
        else:
            # Emit any batched signals before resetting
            if hasattr(self, '_signal_batch') and self._signal_batch:
                for batched_signal, batched_args in self._signal_batch.items():
                    try:
                        batched_signal.emit(*batched_args)
                    except Exception:
                        pass
                self._signal_batch.clear()
            # Reset window
            self._signal_rate_window_start = _now
            self._signal_rate_count = 1

        # ── TOCTOU-safe emit ─────────────────────────────────────
        try:
            from PyQt6.sip import isdeleted as _isdeleted
            if _isdeleted(self):
                return  # Object destroyed — silently drop
        except ImportError:
            pass
        except Exception:
            pass  # If sip check itself fails, try emit anyway
        try:
            signal.emit(*args)
        except AttributeError:
            # Signal is still the class-level pyqtSignal descriptor
            # (not yet bound to a QObject instance). Expected during
            # startup/shutdown transitions — silently skip.
            pass
        except RuntimeError:
            pass  # C++ object destroyed mid-emit — non-recoverable, skip
        except Exception:
            pass  # Any other emit failure — catch and survive

    def _build_activity_info(
        self, activity: str, tool_name: str, args: Dict[str, Any],
        result_str: Optional[str], status: str,
    ) -> str:
        """Build structured JSON info for tool_activity signal.

        Returns a JSON string with rich details for the UI to render
        Cursor-style activity cards (file paths, line ranges, match results).
        """
        info: Dict[str, Any] = {}
        try:
            fp_raw = args.get("file_path")
            if not isinstance(fp_raw, str) or not fp_raw:
                fp_raw = args.get("path")
            fp = fp_raw if isinstance(fp_raw, str) else ""
            # Make paths relative to project root for compact display
            if fp and self._project_root:
                try:
                    rel = os.path.relpath(fp, self._project_root)
                    if not rel.startswith('..'):
                        fp = rel.replace('\\', '/')
                except ValueError:
                    pass

            if activity == "read_file":
                info["file_path"] = fp
                requested_offset_raw = args.get("offset", 1)
                requested_limit_raw = args.get("limit")
                requested_offset = (
                    requested_offset_raw
                    if isinstance(requested_offset_raw, int) and requested_offset_raw >= 1
                    else 1
                )
                requested_limit = (
                    requested_limit_raw
                    if isinstance(requested_limit_raw, int) and requested_limit_raw > 0
                    else _get_default_read_chunk_lines()
                )
                info["offset"] = requested_offset
                info["limit"] = requested_limit
                info["requested_offset"] = requested_offset
                info["requested_limit"] = requested_limit
                if status == "complete" and result_str:
                    try:
                        parsed = json.loads(result_str)
                    except Exception:
                        parsed = None

                    if isinstance(parsed, dict):
                        parsed_map = cast(Dict[str, Any], parsed)
                        start_line = parsed_map.get("start_line")
                        num_lines = parsed_map.get("num_lines")
                        total_lines = parsed_map.get("total_lines")

                        if isinstance(start_line, int) and start_line >= 1:
                            info["offset"] = start_line
                            info["actual_start_line"] = start_line
                        if isinstance(num_lines, int) and num_lines > 0:
                            info["limit"] = num_lines
                            info["lines_read"] = num_lines
                            if isinstance(start_line, int) and start_line >= 1:
                                info["actual_end_line"] = start_line + num_lines - 1
                        if isinstance(total_lines, int) and total_lines >= 0:
                            info["total_lines"] = total_lines
                            if isinstance(start_line, int) and isinstance(num_lines, int):
                                end_line = start_line + num_lines - 1
                                if end_line < total_lines:
                                    info["remaining_lines"] = total_lines - end_line
                                    info["remaining_range"] = str(end_line + 1) + "-" + str(total_lines)

                        _parsed_content = parsed_map.get("content")
                        if "lines_read" not in info and isinstance(_parsed_content, str):
                            _content: str = _parsed_content
                            info["lines_read"] = _content.count('\n') + (1 if _content else 0)
                    else:
                        info["lines_read"] = result_str.count('\n') + (1 if result_str else 0)
                if status == "error" and result_str:
                    info["error"] = result_str[:400]

            elif activity == "edit_file":
                info["file_path"] = fp
                old_s = args.get("old_string", "")
                new_s = args.get("new_string", "")
                if old_s and new_s:
                    info["description"] = "Editing"
                elif new_s:
                    info["description"] = "Editing"
                else:
                    info["description"] = "Deleting lines"

            elif activity in ("write_file", "create_file"):
                info["file_path"] = fp
                content = args.get("content", "")
                info["lines"] = content.count('\n') + 1 if content else 0
                info["description"] = "Creating" if activity == "create_file" else "Writing"

            elif activity in ("search", "grep"):
                # Grep tool
                info["search_type"] = (tool_name or "Grep").lower()
                pattern = args.get("pattern", "")
                info["pattern"] = pattern
                info["query"] = pattern      # GrepCard reads "query" key
                info["path"] = fp or "."
                info["glob"] = args.get("glob", "")
                info["include"] = args.get("glob", args.get("include", ""))
                if status == "complete" and result_str:
                    matches: List[Dict[str, Any]] = self._parse_grep_matches(result_str)
                    info["match_count"] = len(matches)
                    info["matches"] = matches[:15]  # limit for UI

            elif activity == "list_directory":
                info["search_type"] = (tool_name or "LS").lower()
                info["path"] = fp or args.get("path", ".")
                info["pattern"] = args.get("pattern", "")
                if status == "complete" and result_str:
                    # Parse actual result data so chat panel can render file list
                    try:
                        parsed_result = json.loads(result_str) if result_str.strip().startswith('{') else None
                    except Exception:
                        parsed_result = None
                    if isinstance(parsed_result, dict):
                        parsed_map = cast(Dict[str, Any], parsed_result)
                        # LS tool: entries list
                        if 'entries' in parsed_map and isinstance(parsed_map['entries'], list):
                            info['entries'] = parsed_map['entries']
                            info['count'] = len(parsed_map['entries'])
                        # Glob tool: files list
                        if 'files' in parsed_map and isinstance(parsed_map['files'], list):
                            info['files'] = parsed_map['files']
                            info['numFiles'] = parsed_map.get('numFiles', len(parsed_map['files']))
                            info['truncated'] = parsed_map.get('truncated', False)
                        if 'pattern' in parsed_map:
                            info['pattern'] = parsed_map['pattern']
                    if 'count' not in info:
                        # Fallback: count from result_str lines
                        lines = [l for l in result_str.split('\n') if l.strip()]
                        info["count"] = len(lines)

            elif activity == "semantic_search":
                info["search_type"] = "semantic"
                info["query"] = args.get("query", "")
                info["path"] = fp or "."
                info["top_k"] = args.get("top_k", 10)
                if status == "complete" and result_str:
                    try:
                        parsed = json.loads(result_str)
                        if isinstance(parsed, dict):
                            results = parsed.get("results", [])
                            info["results"] = results[:20]
                            info["numResults"] = parsed.get("numResults", len(results))
                            info["searchTimeMs"] = parsed.get("searchTimeMs", 0)
                    except Exception:
                        pass

            elif activity == "run_command":
                cmd = args.get("command", "")
                info["command"] = cmd[:200] if cmd else ""
                info["tool_name"] = tool_name  # "Bash" or "PowerShell"
                info["label"] = tool_name      # TerminalCard display label
                info["timeout"] = args.get("timeout", "")
                # Include sandbox/container state so UI can show whether this
                # command is running in sandboxed container or local shell.
                try:
                    from src.agent.src.utils.sandbox.sandbox_adapter import SandboxManager
                    sandbox_enabled = bool(SandboxManager.is_sandbox_enabled_in_settings())
                    sandbox_runtime_enabled = bool(SandboxManager.is_sandboxing_enabled())
                    unavailable_reason = str(SandboxManager.get_sandbox_unavailable_reason() or "")
                except Exception:
                    sandbox_enabled = False
                    sandbox_runtime_enabled = False
                    unavailable_reason = ""
                info["sandbox_enabled"] = sandbox_enabled
                info["sandbox_runtime_enabled"] = sandbox_runtime_enabled
                info["sandbox_active"] = bool(sandbox_enabled and sandbox_runtime_enabled)
                if unavailable_reason:
                    info["sandbox_unavailable_reason"] = unavailable_reason
                if status in ("complete", "error") and result_str:
                    info["output"] = result_str[:2000]

            elif activity in ("team_create", "team_delete"):
                info["team_name"] = args.get("name", "")
                if status == "complete" and result_str:
                    try:
                        parsed = json.loads(result_str)
                        if isinstance(parsed, dict):
                            parsed_map = cast(Dict[str, Any], parsed)
                            info["team_id"] = parsed_map.get("teamId", "")
                            info["message"] = parsed_map.get("message", "")
                    except Exception:
                        pass

            elif activity in ("task_create", "task_update", "task_list", "task_get", "task_stop"):
                info["task_id"] = args.get("taskId", "")
                info["subject"] = args.get("subject", "")
                if status == "complete" and result_str:
                    try:
                        parsed = json.loads(result_str)
                        if isinstance(parsed, dict):
                            parsed_map = cast(Dict[str, Any], parsed)
                            info["message"] = parsed_map.get("message", "")
                            info["task_id"] = parsed_map.get("taskId", info["task_id"])
                    except Exception:
                        pass

            elif activity == "web_search":
                info["query"] = args.get("query", "")
                if status == "complete" and result_str:
                    try:
                        parsed = json.loads(result_str)
                        if isinstance(parsed, dict):
                            parsed_map = cast(Dict[str, Any], parsed)
                            results_list = parsed_map.get("results", [])
                            info["result_count"] = len(results_list)
                            info["items"] = [
                                {
                                    "title": str(r.get("title", ""))[:200],
                                    "url": str(r.get("url", "")),
                                    "snippet": str(r.get("snippet", ""))[:200],
                                }
                                for r in (results_list or [])[:8]
                                if isinstance(r, dict)
                            ]
                            formatted = parsed_map.get("formatted", "")
                            if formatted:
                                info["preview"] = formatted[:500]
                    except Exception:
                        info["preview"] = result_str[:500] if result_str else ""

            elif activity == "web_fetch":
                info["url"] = args.get("url", "")
                info["query"] = args.get("query", "")
                if status == "complete" and result_str:
                    try:
                        parsed = json.loads(result_str)
                        if isinstance(parsed, dict):
                            parsed_map = cast(Dict[str, Any], parsed)
                            content = str(parsed_map.get("content", ""))
                            info["content_length"] = len(content)
                            info["preview"] = content[:8000]
                            info["url"] = parsed_map.get("url", info["url"])
                    except Exception:
                        info["preview"] = result_str[:8000] if result_str else ""

            else:
                # Fallback: pass raw args
                info = {"raw": json.dumps(args)[:400]}

        except Exception as e:
            log.debug(f"[BRIDGE] _build_activity_info error: {e}")
            info = {"raw": json.dumps(args)[:400]}

        if status == "error" and result_str and "error" not in info:
            info["error"] = result_str[:400]

        return json.dumps(info)

    def _parse_grep_matches(self, result_str: str) -> List[Dict[str, Any]]:
        """Parse grep/search result into structured match list for UI."""
        matches: List[Dict[str, Any]] = []
        try:
            # Try JSON parse first (real Grep tool returns structured results)
            data = json.loads(result_str)
            if isinstance(data, str):
                # Plain text result — parse line-by-line
                for line in data.split('\n'):
                    line = line.strip()
                    if not line:
                        continue
                    # Format: "path/to/file.py:123: matched text"
                    parts = line.split(':', 2)
                    if len(parts) >= 3:
                        fpath = parts[0].strip()
                        try:
                            lineno = int(parts[1].strip())
                        except ValueError:
                            lineno = 0
                        text = parts[2].strip()
                        fname = fpath.split('/')[-1].split('\\')[-1]
                        matches.append({"file": fname, "line": lineno, "path": fpath, "text": text})
                    elif len(parts) == 2:
                        fpath = parts[0].strip()
                        try:
                            lineno = int(parts[1].strip())
                        except ValueError:
                            lineno = 0
                        fname = fpath.split('/')[-1].split('\\')[-1]
                        matches.append({"file": fname, "line": lineno, "path": fpath, "text": ""})
            elif isinstance(data, list):
                data_list: List[Any] = cast(List[Any], data)
                for item in data_list[:15]:
                    if isinstance(item, str):
                        fname = item.split('/')[-1].split('\\')[-1]
                        matches.append({"file": fname, "line": 0, "path": item, "text": ""})
            elif isinstance(data, dict):
                # Possible {files: [...]} or {matches: [...]}
                data_map = cast(Dict[str, Any], data)
                items_any = data_map.get('files')
                if not isinstance(items_any, list):
                    items_any = data_map.get('matches')
                if not isinstance(items_any, list):
                    items_any = data_map.get('results')
                items: List[Any] = cast(List[Any], items_any) if isinstance(items_any, list) else []
                if items:
                    for item in items[:15]:
                        if isinstance(item, str):
                            fname = item.split('/')[-1].split('\\')[-1]
                            matches.append({"file": fname, "line": 0, "path": item, "text": ""})
                        elif isinstance(item, dict):
                            # Structured match with file/line/text
                            fname = item.get("file", "").split('/')[-1].split('\\')[-1]
                            matches.append({
                                "file": fname,
                                "line": item.get("line", 0),
                                "path": item.get("file", ""),
                                "text": item.get("text", "")
                            })
        except (json.JSONDecodeError, TypeError):
            # Plain text — parse lines
            for line in result_str.split('\n')[:15]:
                line = line.strip()
                if not line or line.startswith('---') or line.startswith('==='):
                    continue
                parts = line.split(':', 2)
                if len(parts) >= 3:
                    fpath = parts[0].strip()
                    try:
                        lineno = int(parts[1].strip())
                    except ValueError:
                        lineno = 0
                    text = parts[2].strip()
                    fname = fpath.split('/')[-1].split('\\')[-1]
                    matches.append({"file": fname, "line": lineno, "path": fpath, "text": text})
                elif len(parts) == 2:
                    fpath = parts[0].strip()
                    try:
                        lineno = int(parts[1].strip())
                    except ValueError:
                        lineno = 0
                    fname = fpath.split('/')[-1].split('\\')[-1]
                    matches.append({"file": fname, "line": lineno, "path": fpath, "text": ""})
                else:
                    matches.append({"file": line[:60], "line": 0, "path": line, "text": ""})
        return matches

    @staticmethod
    def _recover_truncated_tool_args(tool_name: str, raw_args: Any) -> Dict[str, Any]:
        """Attempt to extract valid fields from truncated JSON tool arguments.

        When the Mimo stream ends mid-transmission, the JSON string is cut off
        but early fields (like file_path) are usually still parseable via regex.
        This prevents Write/Bash from getting empty args and rejecting the call.
        """
        if not isinstance(raw_args, str) or not raw_args.strip():
            return {}

        recovered: Dict[str, Any] = {}

        # Extract file_path (Write/Edit)
        fp_match = re.search(r'"file_path"\s*:\s*"((?:[^"\\]|\\.)*)"', raw_args)
        if fp_match:
            recovered["file_path"] = fp_match.group(1).replace('\\"', '"').replace('\\\\', '\\')

        # Extract command (Bash)
        cmd_match = re.search(r'"command"\s*:\s*"((?:[^"\\]|\\.)*)"', raw_args)
        if cmd_match:
            recovered["command"] = cmd_match.group(1).replace('\\"', '"').replace('\\\\', '\\')

        # Extract pattern (Grep)
        pat_match = re.search(r'"pattern"\s*:\s*"((?:[^"\\]|\\.)*)"', raw_args)
        if pat_match:
            recovered["pattern"] = pat_match.group(1).replace('\\"', '"').replace('\\\\', '\\')

        # Extract path (Grep/Read/Glob)
        path_match = re.search(r'"path"\s*:\s*"((?:[^"\\]|\\.)*)"', raw_args)
        if path_match:
            recovered["path"] = path_match.group(1).replace('\\"', '"').replace('\\\\', '\\')

        # Extract content for Write — try to get as much as possible
        # The content field starts after "content": " and goes to the truncation point
        if tool_name == "Write" and "file_path" in recovered:
            content_match = re.search(r'"content"\s*:\s*"(.*)', raw_args, re.DOTALL)
            if content_match:
                raw_content = content_match.group(1)
                raw_content = raw_content.rstrip()
                raw_content = raw_content.replace('\\n', '\n').replace('\\t', '\t').replace('\\"', '"').replace('\\\\', '\\')
                if raw_content:
                    recovered["content"] = raw_content

        # Extract old_string / new_string for Edit
        if tool_name == "Edit" and "file_path" in recovered:
            old_match = re.search(r'"old_string"\s*:\s*"((?:[^"\\]|\\.)*)"', raw_args)
            if old_match:
                recovered["old_string"] = old_match.group(1).replace('\\n', '\n').replace('\\t', '\t').replace('\\"', '"').replace('\\\\', '\\')
            new_match = re.search(r'"new_string"\s*:\s*"((?:[^"\\]|\\.)*)"', raw_args)
            if new_match:
                recovered["new_string"] = new_match.group(1).replace('\\n', '\n').replace('\\t', '\t').replace('\\"', '"').replace('\\\\', '\\')

        if recovered:
            log.warning(
                f"[TOOL-CALL] Recovered partial args for {tool_name}: "
                f"keys={list(recovered.keys())}, file_path={recovered.get('file_path', 'N/A')}"
            )

        return recovered

    async def _execute_single_tool(
        self,
        tool_name: str,
        tool_id: str,
        args: Dict[str, Any],
        messages: List[Any],
        PCM: Type[Any],
        _limits: Optional[_ToolLimitsLike] = None,
    ) -> None:
        """Execute one tool call: emit running → dispatch → emit result → append to messages.
        
        Shielded against ALL Python exceptions (RecursionError, MemoryError,
        SystemError, OSError) — a tool crash MUST NOT crash the IDE.
        """
        try:
            await self._execute_single_tool_inner(
                tool_name, tool_id, args, messages, PCM, _limits
            )
        except asyncio.CancelledError:
            raise  # Must propagate — stop was requested
        except BaseException as exc:
            # Catch ALL exception types — the IDE NEVER crashes from tool failures
            log.critical(
                f"[TOOL-SHIELD] {tool_name} crashed with {type(exc).__name__}: {exc} — "
                f"returning error result instead of crashing IDE"
            )
            try:
                import traceback as _tb
                log.debug(f"[TOOL-SHIELD] {tool_name} traceback:\n{''.join(_tb.format_exception(type(exc), exc, exc.__traceback__))}")
            except Exception:
                pass
            # Emit error activity card so the UI shows the failure
            activity = _TOOL_TO_ACTIVITY_NAME.get(tool_name, tool_name.lower())
            error_info = self._build_activity_info(activity, tool_name, args, f"Tool crashed: {exc}", "error")
            try:
                self._safe_emit(self.tool_activity, activity, error_info, "error")
            except Exception:
                pass
            # Feed error result back to LLM so it can try a different approach
            messages.append(
                PCM(role="tool", content=f"Tool crashed: {type(exc).__name__}: {exc}", tool_call_id=tool_id)
            )

    async def _execute_single_tool_inner(
        self,
        tool_name: str,
        tool_id: str,
        args: Dict[str, Any],
        messages: List[Any],
        PCM: Type[Any],
        _limits: Optional[_ToolLimitsLike] = None,
    ) -> None:
        """Inner implementation of _execute_single_tool — shielded by the outer method."""
        activity = _TOOL_TO_ACTIVITY_NAME.get(tool_name, tool_name.lower())

        # ── Interaction mode enforcement ─────────────────────────────
        # Ask mode: read-only — block write/edit/delete/execute tools
        # Plan mode: planning only — block all except planning tools
        # Agent mode: full access (default)
        _mode = getattr(self, '_interaction_mode', 'Agent')
        if _mode == 'Ask':
            _ASK_BLOCKED = {
                'Write', 'Edit', 'Bash', 'PowerShell', 'NotebookEdit',
                'AgentTool', 'SendMessageTool',
            }
            if tool_name in _ASK_BLOCKED:
                error_msg = (
                    f"Cannot use {tool_name} in Ask mode — Ask mode is read-only. "
                    f"Switch to Agent mode to use {tool_name}."
                )
                log.info(f"[MODE] Blocked {tool_name} in Ask mode")
                self._safe_emit(self.tool_activity, activity,
                    self._build_activity_info(activity, tool_name, args, error_msg, "error"), "error")
                messages.append(PCM(role="tool", content=error_msg, tool_call_id=tool_id))
                return
        elif _mode == 'Plan':
            _PLAN_ALLOWED = {
                'TodoWrite', 'EnterPlanMode', 'ExitPlanMode', 'PlanBuild',
                'Read', 'Grep', 'Glob', 'LS', 'SemanticSearch',
                'AskUserQuestion', 'WebSearch', 'WebFetch',
            }
            if tool_name not in _PLAN_ALLOWED:
                error_msg = (
                    f"Cannot use {tool_name} in Plan mode — Plan mode is for planning only. "
                    f"Switch to Agent mode to execute code."
                )
                log.info(f"[MODE] Blocked {tool_name} in Plan mode")
                self._safe_emit(self.tool_activity, activity,
                    self._build_activity_info(activity, tool_name, args, error_msg, "error"), "error")
                messages.append(PCM(role="tool", content=error_msg, tool_call_id=tool_id))
                return

        # For Write tool, detect create vs update for proper UI card
        if tool_name == "Write":
            fpath = args.get("file_path", "") or args.get("path", "")
            if fpath and not os.path.isabs(fpath) and self._project_root:
                fpath = os.path.join(self._project_root, fpath)
            activity = "create_file" if (fpath and not os.path.exists(fpath)) else "write_file"

        # TodoWrite is a silent background tool — no tool-activity card in UI
        _silent = (tool_name == "TodoWrite")
        if not _silent:
            running_info = self._build_activity_info(activity, tool_name, args, None, "running")
            self._safe_emit(self.tool_activity, activity, running_info, "running")

        # ── Per-tool timeout enforcement ──────────────────────────────
        # Each tool type gets a hard deadline. If exceeded, the tool is
        # cancelled and an error is returned instead of hanging forever.
        _TOOL_TIMEOUTS: Dict[str, float] = {
            "WebSearch": 15.0,
            "WebFetch": 15.0,
            "Bash": 30.0,
            "PowerShell": 30.0,
            "Read": 15.0,
            "Write": 20.0,
            "Edit": 20.0,
            "Grep": 30.0,
            "Glob": 10.0,
            "LS": 10.0,
            # AskUserQuestion suspends the agent and waits for a human to type a
            # response. The inner dispatcher already applies a 300s (5-min) limit.
            # This outer wrapper must be LONGER than that inner limit so it never
            # fires first — otherwise the tool times out in 15s (the default
            # fallback), the question card stays visible but any answer is
            # silently rejected as "unknown question ID".
            "AskUserQuestion": 360.0,
        }
        _timeout = _TOOL_TIMEOUTS.get(tool_name, 15.0)
        try:
            result: ToolResult = cast(ToolResult, await asyncio.wait_for(
                self._dispatch_tool(tool_name, tool_id, args),
                timeout=_timeout,
            ))
        except asyncio.TimeoutError:
            log.error(f"[TOOL-TIMEOUT] {tool_name} exceeded {_timeout}s deadline — returning error")
            result = ToolResult(
                tool_id=tool_id, result=None, success=False,
                error=(
                    f"{tool_name} timed out after {_timeout}s. "
                    f"DO NOT retry the same command. Instead:\n"
                    f"- Try a simpler/different approach\n"
                    f"- Use a different tool (e.g., Read instead of Bash cat)\n"
                    f"- Skip this step and continue with the next task\n"
                    f"- Tell the user what you were trying to do and ask for guidance"
                ),
            )

        try:
            self._tool_call_success[tool_id] = bool(result.success)
        except Exception:
            pass

        # ── Usage Tracker: record tool call ────────────────────────
        try:
            _ut = getattr(self, '_usage_tracker', None)
            if _ut:
                _ut.record_tool_call(tool_name)
        except Exception:
            pass

        # ── Record in circuit breaker ────────────────────────────────
        _error_text = str(result.error) if not result.success and result.error else ""
        self._tool_circuit_breaker.record_call(tool_name, result.success, _error_text)

        if result.success:
            if tool_name in ("Write", "Edit", "Bash", "PowerShell"):
                self._mutation_success_count += 1
                self._session_mutation_count += 1  # Persistent session counter
                # Reset TodoWrite streak — real progress was made,
                # so the next TodoWrite should be allowed.
                self._todo_write_streak = 0
                log.debug(
                    f"[MUTATION] Turn mutation: {self._mutation_success_count}, " +
                    f"Session mutation: {self._session_mutation_count}"
                )
                # Auto-save to memory.json for Write/Edit — every change is remembered
                if tool_name in ("Write", "Edit") and _HAS_CORTEX_PROJECT_CTX:
                    try:
                        _mp = args.get("file_path", "")
                        if _mp:
                            if not os.path.isabs(_mp) and self._project_root:
                                _mp = os.path.join(self._project_root, _mp)
                            _action = "Created" if activity == "create_file" else "Modified"
                            update_cortex_project_memory("decisions", f"{_action}: {_mp}")
                    except Exception:
                        pass
            result_payload: Any = result.result
            if isinstance(result_payload, dict):
                result_str = json.dumps(cast(Dict[str, Any], result_payload))
            elif isinstance(result_payload, list):
                result_str = json.dumps(cast(List[Any], result_payload))
            else:
                result_str = str(result_payload)
            if not _silent:
                complete_info = self._build_activity_info(activity, tool_name, args, result_str, "complete")
                self._safe_emit(self.tool_activity, activity, complete_info, "complete")
                # Emit exploreFile for explore-type tools (read_file, grep, glob, web_search)
                _explore_tools = {"read", "Read", "grep", "Grep", "glob", "Glob", "web_search", "WebSearch", "web_fetch", "WebFetch", "search", "Search"}
                if tool_name in _explore_tools:
                    _file_path = (
                        args.get("file_path") or args.get("filePath") or
                        args.get("path") or args.get("directory") or
                        args.get("pattern") or ""
                    )
                    if _file_path:
                        try:
                            _explore_json = json.dumps({
                                "path": str(_file_path),
                                "lines": "",
                                "tool": tool_name.lower(),
                            })
                            self._safe_emit(self.exploreFile, _explore_json)
                        except Exception:
                            pass
            # ── PlanBuild hook: emit plan_created when a plan is successfully created ──
            if tool_name == "PlanBuild" and 'plan_id' in (result_payload if isinstance(result_payload, dict) else {}):
                try:
                    plan_json = json.dumps(dict(result_payload))
                    self._safe_emit(self.plan_created, plan_json)
                    log.info(f"[PLANBUILD] Plan created: {result_payload.get('title', 'unknown')}")
                except Exception as _plan_err:
                    log.debug(f"[PLANBUILD] Failed to emit plan_created: {_plan_err}")
        else:
            result_str = f"Error: {result.error}"
            if not _silent:
                error_info = self._build_activity_info(activity, tool_name, args, result_str, "error")
                self._safe_emit(self.tool_activity, activity, error_info, "error")

        # ── Track tool results for verification enforcement ────────────────
        _exit_code: Optional[int] = None
        if tool_name in ("Bash", "PowerShell", "LS"):
            try:
                _raw = result.result if hasattr(result, 'result') else None
                if isinstance(_raw, dict):
                    _raw = cast(Dict[str, Any], _raw)
                    _exit_code = _raw.get("exit_code") or _raw.get("exitCode")  # pyright: ignore[reportUnknownMemberType]
            except Exception:
                pass
        self._recent_tool_results.append((tool_name, bool(result.success), result_str[:200], _exit_code))
        if len(self._recent_tool_results) > self._max_recent_results:
            self._recent_tool_results = self._recent_tool_results[-self._max_recent_results:]

        # Feed result back to LLM — persist large results to disk instead of truncating.
        # Ported from Claude Code's toolResultStorage.ts: results exceeding
        # the threshold are saved to disk; LLM gets a 2KB preview + file path.
        # Falls back to truncation if persistence fails.
        _MAX_TOOL_RESULT = (_limits.max_tool_result_chars if _limits is not None else 15_000)
        try:
            from src.ai.tool_result_storage import maybe_persist_large_result
            result_str_for_history = maybe_persist_large_result(
                result_str, tool_name, tool_id, threshold=_MAX_TOOL_RESULT
            )
        except Exception as _persist_err:
            log.debug(f"[BRIDGE] Persistence fallback: {_persist_err}")
            # Fallback: simple truncation
            if len(result_str) > _MAX_TOOL_RESULT:
                result_str_for_history = (
                    result_str[:_MAX_TOOL_RESULT]
                    + f"\n... [truncated: {len(result_str) - _MAX_TOOL_RESULT} chars omitted]"
                )
            else:
                result_str_for_history = result_str
        messages.append(
            PCM(role="tool", content=result_str_for_history, tool_call_id=tool_id)
        )

        # Invalidate file read cache for write/edit tools so next Read sees fresh content
        if tool_name in ("Write", "Edit"):
            _wp = args.get("file_path", "")
            if _wp:
                if not os.path.isabs(_wp) and self._project_root:
                    _wp = os.path.join(self._project_root, _wp)
                self._tool_ctx.file_cache_invalidate(_wp)
            
            # ── Write Chunking Nudge ──────────────────────────────────
            # Large single Write calls (34K+ chars) cause 5+ minute stalls
            # as DeepSeek generates the entire content. Nudge the AI to
            # split large writes into smaller chunks for faster streaming.
            _content_arg = args.get("content", "")
            if isinstance(_content_arg, str) and len(_content_arg) > 20_000:
                _chunk_nudge = (
                    f"[System: You just wrote {len(_content_arg):,} chars to {_wp}. "
                    f"For large files like this, prefer to create a skeleton first "
                    f"with the structure, then add sections incrementally with Edit. "
                    f"This approach is faster and avoids 5+ minute single-write stalls.]"
                )
                messages.append(PCM(role="user", content=_chunk_nudge))

    # ── Test framework detection and verification ─────────────────
    # Phase 1: Replace keyword-nudge verification with actual test
    # execution analysis.  See plan: frolicking-singing-garden.md

    _TEST_CONFIG_FILES: Dict[str, Tuple[str, ...]] = {
        "pytest":       ("pytest.ini", "setup.cfg", "pyproject.toml", "tox.ini"),
        "unittest":     ("",),  # built-in; always available
        "node":         ("package.json",),
        "jest":         ("jest.config.js", "jest.config.ts", "jest.config.mjs"),
        "go test":      ("go.mod",),
        "cargo test":   ("Cargo.toml",),
    }

    _TEST_COMMAND_PATTERNS: Dict[str, str] = {
        "pytest":       r"\bpytest\b",
        "unittest":     r"python\s+-m\s+unittest\b",
        "node":         r"(?:npm|yarn|pnpm)\s+(?:test|run\s+test)",
        "jest":         r"\bnpx\s+jest\b|node_modules/\.bin/jest",
        "go test":      r"\bgo\s+test\b",
        "cargo test":   r"\bcargo\s+test\b",
    }

    def _detect_test_framework(self) -> Optional[str]:
        """Detect which test framework the project uses by scanning for config files.

        Scans from project root, lazily caching result. Returns None if no
        recognised framework is detected.
        """
        if self._test_framework_checked:
            return self._test_framework
        self._test_framework_checked = True

        root = self._get_project_root()
        try:
            for framework, config_files in self._TEST_CONFIG_FILES.items():
                for cfg in config_files:
                    if not cfg:
                        continue
                    candidate = os.path.join(root, cfg)
                    if os.path.isfile(candidate):
                        self._test_framework = framework
                        log.info(f"[VERIFY] Detected test framework: {framework} (via {cfg})")
                        return framework
                    # Also check pyproject.toml content for [tool.pytest]
                    if cfg == "pyproject.toml":
                        try:
                            with open(candidate, "r", encoding="utf-8") as fh:
                                content = fh.read()
                            if "[tool.pytest" in content:
                                self._test_framework = "pytest"
                                log.info(f"[VERIFY] Detected test framework: pytest (via pyproject.toml)")
                                return "pytest"
                            if "[tool.jest" in content:
                                self._test_framework = "jest"
                                return "jest"
                        except Exception:
                            pass
        except Exception as exc:
            log.warning(f"[VERIFY] Error detecting test framework: {exc}")

        self._test_framework = "unknown"
        return None

    def handle_build_plan(self, plan_id: str) -> None:
        """Handle JS 'Build All' request for a plan card.
        
        Reads the plan .md file, formats a build instruction message,
        and triggers AI execution of all plan steps in order.
        """
        import os
        try:
            project_root = self._project_root or os.getcwd()
            plan_path = os.path.join(project_root, 'plans', f'{plan_id}.md')

            if not os.path.exists(plan_path):
                log.warning(f"[PLANBUILD] Plan file not found: {plan_path}")
                self._safe_emit(self.request_error, f"Plan file not found: {plan_path}")
                return

            with open(plan_path, 'r', encoding='utf-8') as f:
                plan_content = f.read()

            # Extract steps from plan content
            build_message = (
                f"Execute the following plan (ID: {plan_id}):\n\n"
                f"{plan_content}\n\n"
                f"Please execute ALL steps in order. After each step, verify the changes "
                f"exist in the files before marking the corresponding todo as COMPLETE. "
                f"Use the todo_write tool to track progress. If any step fails, report it "
                f"immediately and do NOT skip steps."
            )

            log.info(f"[PLANBUILD] Build triggered for plan {plan_id}")

            # Emit build message as a user message to trigger AI execution
            self._safe_emit(self.plan_step_updated, plan_id, "all", "building")
            self.process_message(build_message)

        except Exception as e:
            log.error(f"[PLANBUILD] Build failed: {e}")
            self._safe_emit(self.request_error, f"Plan build failed: {str(e)}")

    def _check_recent_tool_results_for_tests(self) -> Tuple[bool, str]:
        """Scan recent tool results for test/verification commands and their outcomes.

        Returns:
            (was_tested: bool, message: str)
            - was_tested=True + message="": Tests passed
            - was_tested=True + message!="": Tests ran but failed
            - was_tested=False + message: No tests found (explanation)
        """
        # Count how many of the last N results are test runs
        test_tool_names = {"Bash", "PowerShell", "LS"}
        recent = self._recent_tool_results[-15:]  # Look at last 15 tools

        test_runs: List[Tuple[str, bool, str, Optional[int]]] = []
        for entry in reversed(recent):
            t_name, success, preview, exit_code = entry
            if t_name not in test_tool_names:
                continue
            # Detect test commands in the preview
            preview_lower = preview.lower()
            is_test = any(
                re.search(pat, preview_lower)
                for pat in [r"\bpytest\b", r"python\s+-m\s+pytest", r"python\s+-m\s+unittest",
                            r"\bnpm\s+test", r"\byarn\s+test", r"go\s+test", r"cargo\s+test",
                            r"\bnpx\s+jest", r"\brun\s+test", r"verify",
                            # ── Web / script verification patterns ──────────
                            r"python\s+-m\s+http\.server",
                            r"\bcurl\b", r"\bwget\b",
                            r"node\s+-e\s+", r"\bstart\s+http",
                            r"invoke-webrequest", r"invoke-restmethod",
                            r"\bopen\s+http", r"\bxdg-open\s+http",
                            r"live-server", r"\bnpx\s+serve",
                            # ── Build / lint as verification ───────────────
                            r"\bnpm\s+run\s+build", r"\bnpm\s+run\s+dev",
                            r"\bnpx\s+eslint", r"\bpylint\b", r"\bflake8\b",
                            r"\bblack\s+--check", r"\bmypy\b", r"\bpyright\b"]
            )
            if is_test:
                test_runs.append((t_name, success, preview[:150], exit_code))

        if not test_runs:
            return False, "No test or verification commands found in recent tool results."

        # Check if any test run failed
        failed_runs = [(n, p, ec) for n, s, p, ec in test_runs if not s]
        if failed_runs:
            t_name, preview, exit_code = failed_runs[0]
            details = preview.strip()
            return True, (
                f"Test/verification command FAILED ({t_name}):\n"
                f"  {details}\n"
                f"  Exit code: {exit_code}\n\n"
                "Fix the issue before marking tasks complete."
            )

        # All test runs succeeded
        return True, ""

    def _is_web_or_static_project(self) -> bool:
        """Detect if the project is a web/static project without a test framework.

        Web projects (HTML/CSS/JS only) typically have no pytest/jest/etc.
        For these projects, file creation/editing IS the verification —
        blocking completion to demand test-framework commands wastes turns.
        """
        # If a real test framework is detected, it's NOT a pure web project
        framework = self._detect_test_framework()
        if framework and framework != "unknown":
            return False

        # Scan project root for web/static file types
        root = self._get_project_root()
        try:
            _web_exts = {'.html', '.htm', '.css', '.js', '.jsx', '.ts', '.tsx', '.vue', '.svelte', '.json'}
            # Check root-level files
            for entry in os.listdir(root):
                fpath = os.path.join(root, entry)
                if os.path.isfile(fpath):
                    _, ext = os.path.splitext(entry)
                    if ext.lower() in _web_exts:
                        return True
            # Check common web subdirectories
            for _sub in ('src', 'public', 'static', 'dist', 'build'):
                _sub_path = os.path.join(root, _sub)
                if os.path.isdir(_sub_path):
                    try:
                        for entry in os.listdir(_sub_path):
                            fpath = os.path.join(_sub_path, entry)
                            if os.path.isfile(fpath):
                                _, ext = os.path.splitext(entry)
                                if ext.lower() in _web_exts:
                                    return True
                    except OSError:
                        pass
        except OSError:
            pass

        # Fallback: no framework detected at all — likely web/static
        return framework is None

    def _build_verification_message(self) -> Optional[str]:
        """Build a verification-required error message if tests haven't passed.

        Returns None if verification is satisfied, or an error string to block completion.
        """
        # Quick pass: if no mutations were made, verification is not needed
        if self._mutation_success_count == 0 and self._session_mutation_count == 0:
            return None

        # ── Web/static project fast-path ──────────────────────────
        # Web projects (HTML/CSS/JS) typically have no test framework.
        # File creation IS the verification — skip the test-command check.
        if self._is_web_or_static_project() and self._session_mutation_count > 0:
            log.info(
                "[VERIFY] Web/static project detected — "
                f"skipping test-framework verification ({self._session_mutation_count} mutation(s))"
            )
            return None

        # Check if tests were run and passed
        was_tested, test_msg = self._check_recent_tool_results_for_tests()

        # Detect framework for the suggestion
        framework = self._detect_test_framework()
        framework_suggestion = ""
        if framework and framework != "unknown":
            if framework == "pytest":
                framework_suggestion = "python -m pytest"
            elif framework == "unittest":
                framework_suggestion = "python -m unittest discover"
            elif framework == "node":
                framework_suggestion = "npm test"
            elif framework == "jest":
                framework_suggestion = "npx jest"
            elif framework == "go test":
                framework_suggestion = "go test ./..."
            elif framework == "cargo test":
                framework_suggestion = "cargo test"
            else:
                framework_suggestion = "python -m pytest"
        else:
            framework_suggestion = "python -m pytest"

        if was_tested and not test_msg:
            return None  # Tests ran and passed — verification satisfied

        if was_tested and test_msg:
            # Tests ran but failed
            return (
                f"VERIFICATION FAILED: Your changes produced test/verification failures.\n\n"
                f"Details: {test_msg}\n\n"
                f"Fix the failing tests before marking tasks complete."
            )

        # No tests found — require them
        return (
            f"VERIFICATION REQUIRED: You've made changes, but no tests or verification "
            f"commands were detected.\n\n"
            f"Before marking tasks complete, you MUST:\n"
            f"1. Run tests to verify your changes work\n"
            f"2. Run your app to verify it still functions\n"
            f"3. Fix any issues found\n\n"
            f"Suggested command: {framework_suggestion}"
        )

    # ── Task Graph Sync ───────────────────────────────────────────────

    def _sync_tasks_to_graph(self) -> None:
        """Bulk-sync all existing _session_tasks into the task graph.

        Called after session restore to populate the graph from flat task dicts.
        """
        if not self._session_tasks:
            return
        from src.core.task_graph import TaskNode, TaskStatus
        for task_id, task in self._session_tasks.items():
            if self._task_graph.has_node(task_id):
                continue
            status_kind = TaskStatus.from_str(task.get("status", "pending"))
            node = TaskNode(
                id=task_id,
                subject=task.get("subject", ""),
                description=task.get("description", ""),
                status=status_kind,
                active_form=task.get("activeForm"),
                owner=task.get("owner"),
                parent_id=task.get("parentId"),
                depends_on=list(task.get("dependsOn", []) or task.get("blockedBy", [])),
                estimated_effort=task.get("estimatedEffort"),
            )
            self._task_graph.add_node(node)

    # ── Phase 4: Session restore ──────────────────────────────────

    def _hydrate_from_snapshot(self, snapshot: Dict[str, Any]) -> None:
        """Restore session state from a previously saved snapshot.

        Called during __init__ when a snapshot file is found on disk.
        Restores the task graph, mutation counters, debug loop, and tool
        circuit breaker state. Does NOT restore full conversation history.
        """
        # Tasks
        saved_tasks: Dict[str, Any] = cast(Dict[str, Any], snapshot.get("session_tasks", {}))
        if saved_tasks:
            self._session_tasks.update(saved_tasks)
            log.info(f"[SESSION] Restored {len(saved_tasks)} session tasks")

        # Task graph
        saved_graph: Dict[str, Any] = cast(Dict[str, Any], snapshot.get("task_graph", {}))
        if saved_graph and saved_graph.get("nodes"):
            try:
                from src.core.task_graph import TaskGraph
                self._task_graph = TaskGraph.from_dict(saved_graph)
                log.info(f"[SESSION] Restored task graph: {self._task_graph.get_task_count()} nodes")
            except Exception as e:
                log.warning(f"[SESSION] Failed to restore task graph: {e}")

        # Todos
        saved_todos: List[Dict[str, Any]] = cast(List[Dict[str, Any]], snapshot.get("current_todos", []))
        if saved_todos:
            self._current_todos.clear()
            self._current_todos.extend(saved_todos)
            log.info(f"[SESSION] Restored {len(saved_todos)} todos")

        # Mutation counters
        self._session_mutation_count = snapshot.get("session_mutation_count", 0)
        self._mutation_success_count = snapshot.get("mutation_success_count", 0)

        # Tool circuit breaker — reset disabled state on session restore
        # Previous session's failures should not permanently disable tools.
        # The breaker state is session-local; persisting it causes tools to be
        # permanently unavailable across restarts.
        disabled: List[str] = cast(List[str], snapshot.get("disabled_tools", []))
        if disabled:
            log.warning(f"[SESSION] Clearing {len(disabled)} disabled tools from previous session: {disabled}")
            self._disabled_tools.clear()
            # Reset breaker's internal disabled set too
            try:
                self._tool_circuit_breaker._disabled_tools.clear()
            except Exception:
                pass

        tool_fails: Dict[str, int] = cast(Dict[str, int], snapshot.get("tool_fail_counts", {}))
        if tool_fails:
            self._tool_fail_counts.update(tool_fails)

        # Recent tool results (last 10 for context continuity)
        recent: List[Any] = cast(List[Any], snapshot.get("recent_tool_results", []))
        if recent:
            self._recent_tool_results = list(recent)[-10:]

        # Debug loop
        dl_data: Dict[str, Any] = cast(Dict[str, Any], snapshot.get("debug_loop", {}))
        if dl_data and dl_data.get("state", "idle") != "idle":  # pyright: ignore[reportUnknownMemberType]
            try:
                from src.core.debug_loop import DebugLoopState
                dl_state = DebugLoopState(dl_data.get("state", "idle"))  # pyright: ignore[reportUnknownMemberType]
                self._debug_loop.cycle_count = dl_data.get("cycle_count", 0)  # pyright: ignore[reportUnknownMemberType]
                self._debug_loop.state = dl_state
                self._debug_loop.failed_tool_name = dl_data.get("failed_tool_name", "")  # pyright: ignore[reportUnknownMemberType]
                self._debug_loop.failed_exit_code = dl_data.get("failed_exit_code")  # pyright: ignore[reportUnknownMemberType]
                self._debug_loop.failed_preview = dl_data.get("failed_preview", "")  # pyright: ignore[reportUnknownMemberType]
                self._debug_loop.failed_command = dl_data.get("failed_command", "")  # pyright: ignore[reportUnknownMemberType]
                self._debug_loop.last_fix_summary = dl_data.get("last_fix_summary", "")  # pyright: ignore[reportUnknownMemberType]
                log.info(f"[SESSION] Restored debug loop: state={dl_state}, cycles={self._debug_loop.cycle_count}")
            except Exception as e:
                log.warning(f"[SESSION] Failed to restore debug loop: {e}")

        # Inject resume marker into conversation history
        saved_at: int = snapshot.get("saved_at", 0)
        task_count: int = len(saved_tasks)
        mutation_count: int = snapshot.get("session_mutation_count", 0)
        resume_msg = (
            f"[System: Session resumed from {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(saved_at))}. "
            f"{mutation_count} mutations had been made, {task_count} tasks were in progress. "
            f"Continuing from previous state.]"
        )
        self._conversation_history.append(
            ChatMessage(role="system", content=resume_msg)
        )
        log.info(f"[SESSION] Injected resume marker: {resume_msg[:80]}...")

        # ── Chat context restore is deferred to set_project_root() ────
        # _hydrate_from_snapshot runs during __init__ before the project
        # root is known. The actual DB message load happens when the
        # project is opened and set_project_root() is called.

    def _rollback_last_change(self) -> Optional[str]:
        """Roll back the last change group via the change orchestrator.

        Used by the debug loop to revert failed changes. Returns a summary
        string if a rollback occurred, or None if there was nothing to roll back.
        """
        try:
            from src.core.change_orchestrator import get_change_orchestrator
            orch = get_change_orchestrator()
            if orch.can_undo():
                group = orch.undo()
                if group:
                    summary = group.description or f"{len(group)} file(s)"
                    log.info(f"[DEBUG LOOP] Rolled back: {summary}")
                    return summary
        except Exception as exc:
            log.warning(f"[DEBUG LOOP] Rollback failed: {exc}")
        return None

    # ── Tool dispatch ──────────────────────────────────────────

    async def _dispatch_tool(
        self, tool_name: str, tool_id: str, args: Dict[str, Any]
    ) -> ToolResult:
        """
        Dispatch a tool call to the real agent tool or bridge-native fallback.

        Real tools (from src/agent/src/tools/):
            Read  → FileReadTool.call()
            Write → FileWriteTool.call()
            Edit  → FileEditTool.call()
            Glob  → GlobTool.call()
            Grep  → GrepTool.call()

        Bridge-native (no real implementation exists):
            Bash  → BridgeBashTool.execute()
            LS    → BridgeLSTool.execute()
        """
        try:
            # ── Autonomy gate (Phase 8) ────────────────────────────────────
            try:
                from src.core.autonomy_manager import get_autonomy_manager
                _auto_mgr = get_autonomy_manager()
                if _auto_mgr.get_level().value != "ask":
                    decision = _auto_mgr.check_action(tool_name, args)
                    if decision.requires_permission:
                        # ── AUTO mode: allow Bash even if autonomy says "needs permission" ──
                        # Bash is the fallback tool for when Edit/Write fail.
                        # In AUTO mode, blocking Bash traps the agent in loops.
                        if self._always_allowed and tool_name == "Bash":
                            log.info(f"[AUTONOMY] AUTO-mode: allowing Bash despite permission request ({decision.reason})")
                            # Continue to dispatch — don't return early
                        else:
                            log.info(f"[AUTONOMY] Blocked {tool_name} (mode={decision.autonomy_level.value}): {decision.reason}")
                            return ToolResult(
                                tool_id=tool_id,
                                result=None,
                                success=False,
                                error=(
                                    f"[Autonomy] Action requires permission: {decision.reason}. "
                                    f"Current mode: {decision.autonomy_level.value.upper()}. "
                                    f"Switch to ASK mode or adjust autonomy level."
                                ),
                            )
            except ImportError:
                pass
            except Exception as exc:
                log.debug(f"[AUTONOMY] Gate check skipped: {exc}")

            # ---- Agent Safety Guard (P1-P4, P8) ----
            # Runs BEFORE every tool dispatch: doom-loop, max-iterations,
            # read-before-edit, stale-read, error-recovery checks.
            try:
                _should_proceed, _safety_warnings = self._safety.check_tool_call(
                    tool_name, args,
                    is_auto_mode=self._always_allowed,
                )
                for _warn in _safety_warnings:
                    log.warning(f"[SAFETY] {tool_name}: {_warn[:200]}")
                    self._safe_emit(
                        self.agent_status_update, "safety_warning", _warn[:500]
                    )
                if not _should_proceed:
                    # ── AUTO mode: allow Bash with warnings instead of blocking ──
                    # In AUTO mode, Bash should NEVER be fully blocked — it's the
                    # fallback when Edit/Write tools fail. The agent needs Bash to
                    # inject code via echo/tee/cat when Write/Edit are broken.
                    # Only ASK mode should hard-block.
                    if self._always_allowed and tool_name == "Bash":
                        log.warning(
                            f"[SAFETY] AUTO-mode: allowing Bash despite safety warning "
                            f"({_safety_warnings[0][:100] if _safety_warnings else 'no details'})"
                        )
                        # Continue to dispatch — don't return early
                    else:
                        _block_msg = (
                            _safety_warnings[0] if _safety_warnings
                            else f"Tool call {tool_name} blocked by safety guard."
                        )
                        log.warning(f"[SAFETY] BLOCKED {tool_name}: {_block_msg[:200]}")
                        return ToolResult(
                            tool_id=tool_id, result=None, success=False,
                            error=_block_msg,
                        )
            except Exception as _safety_exc:
                log.debug(f"[SAFETY] Guard check skipped for {tool_name}: {_safety_exc}")

            # Track TodoWrite streaks so we can short-circuit planning loops.
            if tool_name == "TodoWrite":
                self._todo_write_streak += 1
            else:
                self._todo_write_streak = 0

            # ---- Dispatch via registry map (Phase B) ----
            _TOOL_DISPATCH_MAP = {
                "Read":            self._dispatch_read,
                "Write":           self._dispatch_write,
                "Edit":            self._dispatch_edit,
                "Glob":            self._dispatch_glob,
                "Grep":            self._dispatch_grep,
                "SementicSearch":  self._dispatch_semantic_search,
                "TodoWrite":       self._dispatch_todo_write,
                "AskUserQuestion": self._dispatch_ask_user_question,
                "LSP":             self._dispatch_lsp,
                "WebFetch":        self._dispatch_web_fetch,
                "WebSearch":       self._dispatch_web_search,
                "TaskCreate":      self._dispatch_task_create,
                "TaskUpdate":      self._dispatch_task_update,
                "TaskList":        self._dispatch_task_list,
                "TaskGet":         self._dispatch_task_get,
                "TaskStop":        self._dispatch_task_stop,
                "MCP":             self._dispatch_mcp,
                "TeamCreate":      self._dispatch_team_create,
                "TeamDelete":      self._dispatch_team_delete,
                "PlanBuild":       self._dispatch_plan_build,
            }

            handler = _TOOL_DISPATCH_MAP.get(tool_name)
            if handler is not None:
                return await handler(tool_id, args)

            # ---- Bridge-native tools with special pre-processing ----
            if tool_name in ("Bash", "PowerShell"):
                command = args.get("command", "")
                # Check sandbox policy if enabled (lazy init)
                if self._sandbox_manager is None:
                    try:
                        from src.core.sandbox_manager import get_sandbox_manager
                        self._sandbox_manager = get_sandbox_manager()
                    except Exception:
                        pass
                if self._sandbox_manager and self._sandbox_manager.is_enabled():
                    allowed, reason = self._sandbox_manager.is_command_allowed(command)
                    if not allowed:
                        result = ToolResult(
                            tool_id=tool_id,
                            result=None,
                            success=False,
                            error=f"[Sandbox] Command blocked: {reason}",
                        )
                        return result
                result = await self._bash_tool.execute(args)
                result.tool_id = tool_id
                # Refresh file tree after shell commands — covers file create/delete/move
                # that the AI makes via Bash/PowerShell (not tracked by Write/Edit tools)
                # Only refresh for commands that actually modify the filesystem
                _READ_ONLY_PREFIXES = (
                    'git status', 'git log', 'git diff', 'git show', 'git branch',
                    'git remote', 'git stash list', 'git tag', 'git rev-parse',
                    'ls', 'dir', 'cat', 'type', 'head', 'tail', 'wc', 'find',
                    'grep', 'rg', 'select-string', 'echo', 'pwd', 'whoami',
                    'node -v', 'python --version', 'pip list', 'npm list',
                    'test-path', 'test -f', 'test -d',
                )
                _cmd_stripped = command.strip().lower()
                _is_read_only = any(_cmd_stripped.startswith(p) for p in _READ_ONLY_PREFIXES)
                if result.success and self._project_root and not _is_read_only:
                    try:
                        log.debug(f"[BRIDGE] File tree refresh triggered by Bash: {command[:80]}")
                        self.file_tree_refresh_needed.emit(self._project_root)
                    except Exception:
                        pass
                elif _is_read_only:
                    log.debug(f"[BRIDGE] File tree refresh SKIPPED (read-only): {command[:80]}")
                # Close editor tabs for files the agent moved to the Recycle Bin.
                try:
                    _recycled = (result.result or {}).get("recycled_paths") if isinstance(result.result, dict) else None
                    if _recycled:
                        self.files_deleted_by_agent.emit(list(_recycled))
                except Exception:
                    pass
                return result
            elif tool_name == "LS":
                result = await self._ls_tool.execute(args)
                result.tool_id = tool_id
                return result
            else:
                return ToolResult(tool_id=tool_id, result=None, success=False,
                                  error=f"Unknown tool: {tool_name!r}")
        except Exception as exc:
            log.error(f"[BRIDGE] Tool {tool_name!r} raised: {exc}")
            return ToolResult(tool_id=tool_id, result=None, success=False, error=str(exc))

    # ---- Real tool dispatchers ----------------------------------------

    # ── Smart Chunk Boundary Snapping ──────────────────────────────────
    # When the AI reads a file with offset/limit, the chunk end may fall
    # in the middle of a function/class. This snaps the boundary to the
    # nearest structural break (next function/class definition) within a
    # small window, so the LLM always gets complete logical units.
    #
    # Mirrors Cursor's AST-aware chunking — uses regex instead of full
    # AST parsing for speed and broad language support.

    _RE_BOUNDARY_PYTHON = re.compile(
        r'^(\s*)(async\s+)?(def|class)\s+\w+', re.MULTILINE
    )
    _RE_BOUNDARY_JS_TS = re.compile(
        r'^(\s*)(export\s+)?(default\s+)?(async\s+)?(function|class|const\s+\w+\s*=\s*(async\s+)?\()|'
        r'^(\s*)(async\s+)?(\w+)\s*\([^)]*\)\s*\{', re.MULTILINE
    )
    _RE_BOUNDARY_JAVA = re.compile(
        r'^\s*(public|private|protected)?\s*(static\s+)?(class|interface|enum|\w+\s+\w+\s*\()', re.MULTILINE
    )
    _RE_BOUNDARY_GO = re.compile(
        r'^(func\s+|type\s+\w+\s+struct)', re.MULTILINE
    )
    _RE_BOUNDARY_RUST = re.compile(
        r'^\s*(pub\s+)?(fn|struct|enum|trait|impl|mod)\s+', re.MULTILINE
    )
    _RE_BOUNDARY_C_CPP = re.compile(
        r'^\s*(class|struct|enum)\s+\w+|^\w[\w\s*:&<>]+\s+\w+\s*\([^)]*\)\s*\{', re.MULTILINE
    )

    _BOUNDARY_PATTERNS = {
        '.py':   _RE_BOUNDARY_PYTHON,
        '.pyw':  _RE_BOUNDARY_PYTHON,
        '.js':   _RE_BOUNDARY_JS_TS,
        '.mjs':  _RE_BOUNDARY_JS_TS,
        '.jsx':  _RE_BOUNDARY_JS_TS,
        '.ts':   _RE_BOUNDARY_JS_TS,
        '.tsx':  _RE_BOUNDARY_JS_TS,
        '.java': _RE_BOUNDARY_JAVA,
        '.go':   _RE_BOUNDARY_GO,
        '.rs':   _RE_BOUNDARY_RUST,
        '.c':    _RE_BOUNDARY_C_CPP,
        '.cpp':  _RE_BOUNDARY_C_CPP,
        '.cc':   _RE_BOUNDARY_C_CPP,
        '.cxx':  _RE_BOUNDARY_C_CPP,
        '.h':    _RE_BOUNDARY_C_CPP,
        '.hpp':  _RE_BOUNDARY_C_CPP,
    }

    @staticmethod
    def _snap_chunk_boundary(
        lines: List[str], target_end: int, file_ext: str, window: int = 15
    ) -> int:
        """
        Snap a chunk end boundary to the nearest function/class definition.

        If target_end falls inside a function body, find the next structural
        definition within `window` lines and cut just before it. This ensures
        the LLM always receives complete logical units instead of fragments.

        Args:
            lines: All lines of the file.
            target_end: The raw chunk end (0-based exclusive).
            file_ext: File extension (e.g. '.py', '.js').
            window: Max lines to search forward/backward for a boundary.

        Returns:
            Adjusted chunk end (0-based exclusive).
        """
        pattern = AgentBridge._BOUNDARY_PATTERNS.get(file_ext)
        if pattern is None:
            return target_end  # Unsupported language — no snapping

        total = len(lines)
        if target_end >= total:
            return total  # Already at end of file

        # Search FORWARD from target_end for the next definition line.
        # We snap to just before that line so the current chunk doesn't
        # include the start of the next function/class.
        search_start = target_end
        search_end_line = min(total, target_end + window)
        for i in range(search_start, search_end_line):
            if pattern.match(lines[i]):
                # Found next definition — cut right before it
                adjusted = i
                # Don't snap backward by more than a few lines
                if adjusted < target_end:
                    adjusted = target_end
                return adjusted

        # No definition in forward window — extend backward to find
        # the start of the function we're inside
        if target_end < total:
            search_back = max(0, target_end - window // 2)
            for i in range(target_end - 1, search_back - 1, -1):
                if pattern.match(lines[i]):
                    return i  # Snap to start of current function

        return target_end  # No boundary found — use raw chunk end

    async def _dispatch_read(self, tool_id: str, args: Dict[str, Any]) -> ToolResult:
        """Dispatch to real FileReadTool or bridge-native fallback."""
        path = args.get("file_path", "")
        if not os.path.isabs(path) and self._project_root:
            args = {**args, "file_path": os.path.join(self._project_root, path)}
        if args.get("limit") in (None, "", 0):
            args = {**args, "limit": _get_default_read_chunk_lines()}

        # ── DIRECTORY GUARD: Reject directory paths — use LS instead ──
        _fpath_dir = args.get("file_path", "")
        if _fpath_dir and os.path.isdir(_fpath_dir):
            log.warning(f"[READ] Rejected directory path: {_fpath_dir} — use LS to list contents")
            return ToolResult(
                tool_id=tool_id, result=None, success=False,
                error=(
                    f"'{os.path.basename(_fpath_dir)}' is a directory, not a file. "
                    f"Use LS(path='{_fpath_dir}') to list its contents."
                ),
            )

        # ── FILE-SIZE GUARD: Reject reads over 5MB to prevent OOM ─────
        _fpath_check = args.get("file_path", "")
        if _fpath_check and os.path.isfile(_fpath_check):
            try:
                _fsize = os.path.getsize(_fpath_check)
                _MAX_READ_BYTES = 5 * 1024 * 1024  # 5MB hard limit
                if _fsize > _MAX_READ_BYTES:
                    log.warning(
                        f"[READ-GUARD] File too large ({_fsize / 1024 / 1024:.1f}MB > 5MB): {_fpath_check} — "
                        f"rejecting read to prevent OOM crash"
                    )
                    return ToolResult(
                        tool_id=tool_id, result=None, success=False,
                        error=(
                            f"File is too large ({_fsize / 1024 / 1024:.1f}MB). "
                            f"Use offset/limit to read in chunks, e.g. Read(file_path='{os.path.basename(_fpath_check)}', offset=1, limit=200)."
                        ),
                    )
            except OSError:
                pass  # File may have been deleted between check and read — let the tool handle it

        # ── IMAGE FILE DETECTION: Auto-fallback to Mistral for vision ─────
        _fpath_img = args.get("file_path", "")
        _IMAGE_EXTENSIONS = {'.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp', '.svg'}
        if _fpath_img:
            _ext = os.path.splitext(_fpath_img)[1].lower()
            if _ext in _IMAGE_EXTENSIONS and os.path.isfile(_fpath_img):
                log.info(f"[VISION-READ] Image file detected: {_fpath_img}")

                # ── Subscription check: vision is subscription-only ──
                _has_vision_access = False
                try:
                    from src.core.cortex_api import get_api_client
                    _api = get_api_client()
                    _has_vision_access = _api.has_subscription()
                except Exception:
                    pass

                if not _has_vision_access:
                    self.signals.tool_end.emit(
                        f"tool_{self._tool_counter}", "error", None
                    )
                    return ToolResult(
                        tool_id=tool_id, result=None, success=False,
                        error=(
                            "Image/OCR requires a Cortex subscription. "
                            "Subscribe at Settings → Billing to enable image analysis."
                        ),
                    )

                try:
                    # Read image as base64
                    import base64 as _b64
                    with open(_fpath_img, 'rb') as _f:
                        _img_data = _b64.b64encode(_f.read()).decode('utf-8')

                    # Ensure data URI prefix
                    _img_data_uri = f"data:image/{_ext.lstrip('.')};base64,{_img_data}"

                    # Emit tool_start with "read" type to show spinner
                    self._tool_counter += 1
                    _read_tool_id = f"tool_{self._tool_counter}"
                    self.signals.tool_start.emit(_read_tool_id, "read", {"path": os.path.basename(_fpath_img)})

                    # Send to Mistral via Django proxy (subscription-only)
                    _vision_prompt = (
                        f"Analyze this image file: {os.path.basename(_fpath_img)}\n"
                        f"Transcribe ALL visible text exactly (code, labels, errors, line numbers). "
                        f"Also describe visual context: UI state, highlights, selected elements, "
                        f"error indicators, diff colors, layout, and anything that looks anomalous. "
                        f"Do NOT provide solutions — only describe what is visible."
                    )

                    from src.core.cortex_api import get_api_client
                    _proxy_api = get_api_client()
                    _result = _proxy_api.proxy_service(
                        "mistral_ocr",
                        image_url=_img_data_uri,
                        prompt=_vision_prompt,
                    )

                    if _result and _result.get("status") == "success":
                        _vision_response = _result.get("data", {}).get("choices", [{}])[0].get("message", {}).get("content", "")
                    else:
                        _vision_response = None

                    # Emit tool_end to stop spinner
                    if _vision_response:
                        self.signals.tool_end.emit(_read_tool_id, "ok", None)
                        log.info(f"[VISION-READ] Mistral OCR complete: {len(_vision_response)} chars")
                        # Track OCR page usage
                        try:
                            from src.ai.usage_tracker import get_usage_tracker
                            get_usage_tracker().record_ocr_pages(1)
                        except Exception:
                            pass
                        return ToolResult(tool_id=tool_id, result={
                            "path": _fpath_img,
                            "content": f"[Image: {os.path.basename(_fpath_img)}]\n\n{_vision_response}",
                            "image": True,
                            "lines_read": _vision_response.count('\n') + 1,
                        })
                    else:
                        self.signals.tool_end.emit(_read_tool_id, "error", None)
                        return ToolResult(
                            tool_id=tool_id, result=None, success=False,
                            error="Image OCR failed via subscription proxy."
                        )

                except Exception as _img_err:
                    log.error(f"[VISION-READ] Image processing failed: {_img_err}")
                    return ToolResult(
                        tool_id=tool_id, result=None, success=False,
                        error=f"Failed to process image: {_img_err}"
                    )

        if self._tool_ctx.is_context_over_budget():
            _basename = os.path.basename(args.get("file_path", path))
            _remaining = self._tool_ctx.get_remaining_budget_chars()
            return ToolResult(
                tool_id=tool_id, result=None, success=False,
                error=(
                    f"Context budget nearly exhausted. Remaining: ~{_remaining:,} chars. "
                    f"Do NOT read full files. Use small chunks only, e.g. "
                    f"Read(file_path='{_basename}', offset=1, limit=50)."
                )
            )

        # ── FILE READ DEDUP (ported from Claude Code's fileStateCache.ts) ───
        # If we already read this file with same offset/limit and it hasn't
        # changed on disk, return a stub instead of the full content.
        _fpath_resolved = args.get("file_path", "")
        _norm = os.path.normpath(os.path.abspath(_fpath_resolved)) if _fpath_resolved else ""
        _req_offset = args.get("offset")
        _req_limit = args.get("limit")
        if _norm and os.path.isfile(_norm):
            _cached = self._tool_ctx.file_cache_get(_norm, _req_offset, _req_limit)
            if _cached is not None:
                return ToolResult(tool_id=tool_id, result={
                    "path": _fpath_resolved,
                    "content": _cached,
                    "cached": True,
                })

        if self._real_read_tool is not None:
            try:
                raw = await self._real_read_tool.call(
                    args, self._tool_ctx, _always_allow_tool, _STUB_PARENT_MESSAGE
                )
                data = raw.get("data")
                start_line: Optional[int] = None
                num_lines: Optional[int] = None
                total_lines: Optional[int] = None
                content: str = ""

                # Extract text content for LLM from the FileReadOutput
                if hasattr(data, "file") and hasattr(data.file, "content"):
                    content_raw = getattr(data.file, "content", "")
                    content = content_raw if isinstance(content_raw, str) else str(content_raw)
                    start_raw = getattr(data.file, "start_line", None)
                    num_raw = getattr(data.file, "num_lines", None)
                    total_raw = getattr(data.file, "total_lines", None)
                    start_line = start_raw if isinstance(start_raw, int) else None
                    num_lines = num_raw if isinstance(num_raw, int) else None
                    total_lines = total_raw if isinstance(total_raw, int) else None
                elif isinstance(data, dict):
                    data_map = cast(Dict[str, Any], data)
                    if "content" in data_map:
                        content_raw = data_map.get("content", "")
                        content = content_raw if isinstance(content_raw, str) else str(content_raw)
                        start_raw = data_map.get("start_line")
                        num_raw = data_map.get("num_lines")
                        total_raw = data_map.get("total_lines")
                        start_line = start_raw if isinstance(start_raw, int) else None
                        num_lines = num_raw if isinstance(num_raw, int) else None
                        total_lines = total_raw if isinstance(total_raw, int) else None
                    else:
                        file_any = data_map.get("file")
                        if isinstance(file_any, dict):
                            file_obj = cast(Dict[str, Any], file_any)
                            content_raw = file_obj.get("content", "")
                            content = content_raw if isinstance(content_raw, str) else str(content_raw)
                            start_raw = file_obj.get("start_line")
                            num_raw = file_obj.get("num_lines")
                            total_raw = file_obj.get("total_lines")
                            start_line = start_raw if isinstance(start_raw, int) else None
                            num_lines = num_raw if isinstance(num_raw, int) else None
                            total_lines = total_raw if isinstance(total_raw, int) else None
                        else:
                            content = str(data_map)
                else:
                    content = str(data)
                if start_line is None:
                    start_line = args.get("offset", 1)
                effective_limit = args.get("limit")
                if effective_limit is None and isinstance(num_lines, int) and num_lines >= 0:
                    effective_limit = num_lines
                _remaining_chars = self._tool_ctx.get_remaining_budget_chars()
                if len(content) > _remaining_chars:
                    _basename = os.path.basename(args.get("file_path", "file"))
                    return ToolResult(
                        tool_id=tool_id, result=None, success=False,
                        error=(
                            f"Read output too large for current context budget "
                            f"({len(content):,} chars > ~{_remaining_chars:,} remaining). "
                            f"Read a smaller chunk: Read(file_path='{_basename}', offset={start_line}, limit=80)."
                        )
                    )
                # Track file state
                self._tool_ctx.mark_file_read(args["file_path"])
                self._tool_ctx.track_file_read(args["file_path"], len(content))
                # Register in safety guard (P3: read-before-edit + P4: stale-read)
                # Mark as partial only if we read <50% of the file's lines
                _read_lines = num_lines if isinstance(num_lines, int) else 0
                _total = total_lines if isinstance(total_lines, int) else 0
                _is_partial = False
                if _total > 0 and _read_lines > 0:
                    _is_partial = (_read_lines / _total) < 0.5
                self._safety.register_file_read(
                    args["file_path"],
                    is_partial=_is_partial,
                    read_lines=_read_lines,
                    total_lines=_total,
                )
                warnings = self._tool_ctx.get_budget_warnings()
                if warnings:
                    log.warning(f"[CTX] Budget warnings: {warnings}")
                # Sync into context.read_file_state so FileEditTool's staleness check passes
                try:
                    _norm = os.path.abspath(args["file_path"])
                    self._tool_ctx.read_file_state[_norm] = {
                        "content": content,
                        "timestamp": os.path.getmtime(args["file_path"]),
                        "offset": start_line,
                        "limit": effective_limit,
                    }
                    # Populate LRU dedup cache
                    self._tool_ctx.file_cache_put(
                        _norm, content, os.path.getmtime(args["file_path"]),
                        start_line, effective_limit
                    )
                except Exception:
                    pass
                result_payload: Dict[str, Any] = {
                    "path": args["file_path"],
                    "content": content,
                    "start_line": start_line,
                    "num_lines": num_lines if isinstance(num_lines, int) else (content.count('\n') + (1 if content else 0)),
                    "total_lines": total_lines,
                }
                return ToolResult(tool_id=tool_id, result=result_payload)
            except Exception as exc:
                _err_str = str(exc)
                _err_lower = _err_str.lower()
                _SIZE_KEYWORDS = (
                    'exceeds maximum allowed tokens', 'maximum allowed tokens',
                    'token limit', 'file too large', 'too large to read',
                )
                if any(kw in _err_lower for kw in _SIZE_KEYWORDS):
                    # ── SKELETON-FIRST READING for real tool size errors ──────
                    # Instead of passing the error to the LLM, try to generate
                    # a skeleton so it can still get useful structure info.
                    _fpath = args.get("file_path", "")
                    _basename = os.path.basename(_fpath)
                    try:
                        from src.ai.file_skeleton import generate_skeleton
                        skeleton = generate_skeleton(_fpath)
                        if skeleton:
                            log.warning(f"[BRIDGE] FileReadTool size error → returning skeleton: {_err_str}")
                            self._tool_ctx.mark_file_read(_fpath)
                            return ToolResult(tool_id=tool_id, result={
                                "path": _fpath,
                                "content": skeleton,
                                "skeleton": True,
                                "hint": (
                                    f"This is a SKELETON view of {_basename}. "
                                    f"Use line numbers to read specific sections: "
                                    f"Read(file_path='{_basename}', offset=LINE_NUMBER, limit=80). "
                                    f"Scan function names to find what's relevant to your task."
                                )
                            })
                    except Exception as skel_err:
                        log.warning(f"[BRIDGE] Skeleton generation failed for size error: {skel_err}")
                    # Fallback: return the original error
                    log.warning(f"[BRIDGE] FileReadTool size error → returning to LLM: {_err_str}")
                    return ToolResult(tool_id=tool_id, result=None, success=False, error=_err_str)
                log.warning(f"[BRIDGE] Real FileReadTool failed, using fallback: {exc}")

        # Fallback: simple file read
        # Use the already-resolved (possibly absolute) path from args
        fpath = args.get("file_path", "")
        if not os.path.isabs(fpath) and self._project_root:
            fpath = os.path.join(self._project_root, fpath)

        # ── MODEL-AWARE SIZE GUARD (CRITICAL for context overflow prevention) ──────
        # Get model-specific limit from context
        _max_bytes = self._tool_ctx.file_reading_limits.get("maxSizeBytes", 40_000)
        _max_chars = self._tool_ctx.get_max_file_read_chars()
        
        # Check per-turn file read limit (prevents excessive reading)
        _turn_limit_err = self._tool_ctx._budget_tracker.check_turn_file_limit()
        if _turn_limit_err:
            _basename = os.path.basename(fpath)
            return ToolResult(
                tool_id=tool_id, result=None, success=False,
                error=(
                    f"TURN LIMIT: {_turn_limit_err} "
                    f"File: {_basename}. "
                    f"Implement what you have, then read more files in the next turn if needed."
                )
            )
        
        # Check if context budget is running low
        if self._tool_ctx.is_context_over_budget():
            _basename = os.path.basename(fpath)
            _remaining = self._tool_ctx.get_remaining_budget_chars()
            return ToolResult(
                tool_id=tool_id, result=None, success=False,
                error=(
                    f"Context budget nearly exhausted. "
                    f"Remaining: ~{_remaining:,} chars. "
                    f"Use Grep to find specific sections, or read with small limit: "
                    f"Read(file_path='{_basename}', offset=1, limit=50)."
                )
            )
        
        try:
            _fsize = os.path.getsize(fpath)
            _has_pagination = args.get("offset") or args.get("limit")
            if _fsize > _max_bytes and not _has_pagination:
                # ── SKELETON-FIRST READING ─────────────────────────────
                # Instead of rejecting large files outright, return a
                # structural skeleton showing class/function definitions
                # with line numbers so the LLM can do targeted reads.
                _basename = os.path.basename(fpath)
                try:
                    from src.ai.file_skeleton import generate_skeleton
                    skeleton = generate_skeleton(fpath)
                    if skeleton:
                        log.info(f"[BRIDGE] Skeleton-first read for large file: {_basename} ({_fsize:,} bytes)")
                        self._tool_ctx.mark_file_read(fpath)
                        return ToolResult(tool_id=tool_id, result={
                            "path": fpath,
                            "content": skeleton,
                            "skeleton": True,
                            "hint": (
                                f"This is a SKELETON view of {_basename} ({_fsize:,} bytes). "
                                f"Use the line numbers above to read specific sections: "
                                f"Read(file_path='{_basename}', offset=LINE_NUMBER, limit=80).\n"
                                f"For relevance-guided reading: scan function names above, "
                                f"identify which ones match your task, then read only those. "
                                f"Use SementicSearch for broader conceptual searches across the codebase."
                            )
                        })
                except Exception as skel_err:
                    log.warning(f"[BRIDGE] Skeleton generation failed: {skel_err}")

                # Fallback if skeleton fails: return the old error message
                _model_id = getattr(self._tool_ctx, '_model_id', 'unknown')
                return ToolResult(
                    tool_id=tool_id, result=None, success=False,
                    error=(
                        f"File '{_basename}' ({_fsize:,} bytes / ~{_fsize // 4:,} tokens) "
                        f"exceeds model limit ({_max_bytes:,} bytes) for {_model_id}. "
                        f"CRITICAL: Use Grep to locate code, then read with pagination: "
                        f"Read(file_path='{_basename}', offset=1, limit=100). "
                        f"NEVER read large files without offset and limit. "
                        f"Model context window is limited - respect it."
                    )
                )
        except OSError:
            pass
        # ────────────────────────────────────────────────────────────────────────

        # ── SMART CHUNK-BASED READING (like Cursor/Copilot/Claude Code) ─────
        # Instead of dumping an entire file into context, check line count first.
        # If the file is large and no pagination was requested, return a skeleton
        # so the LLM can do targeted reads with offset/limit.
        _SKELETON_LINE_THRESHOLD = 250  # Files with more lines → skeleton-first
        try:
            with open(fpath, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
            _has_pagination = args.get("offset") or args.get("limit")
            _total_lines = len(lines)

            # ── Skeleton-first for medium/large files without pagination ────
            if _total_lines > _SKELETON_LINE_THRESHOLD and not _has_pagination:
                _basename = os.path.basename(fpath)
                try:
                    from src.ai.file_skeleton import generate_skeleton
                    skeleton = generate_skeleton(fpath)
                    if skeleton:
                        log.info(f"[BRIDGE] Smart read: {_basename} has {_total_lines} lines → returning skeleton (threshold={_SKELETON_LINE_THRESHOLD})")
                        self._tool_ctx.mark_file_read(fpath)
                        # Track skeleton size (much smaller) instead of full file
                        self._tool_ctx.track_file_read(fpath, len(skeleton))
                        return ToolResult(tool_id=tool_id, result={
                            "path": fpath,
                            "content": skeleton,
                            "skeleton": True,
                            "total_lines": _total_lines,
                            "hint": (
                                f"This file has {_total_lines:,} lines — too large to read at once. "
                                f"Above is a SKELETON showing structure with line numbers.\n\n"
                                f"📌 RELEVANCE-GUIDED READING:\n"
                                f"1. Scan the function/class names above — which ones match your task?\n"
                                f"2. Read those sections directly: Read(file_path='{_basename}', offset=LINE, limit=80)\n"
                                f"3. For deeper understanding, use SementicSearch to find conceptually related code "
                                f"across the entire codebase first, then return here for targeted reads.\n"
                                f"4. Use Grep(pattern='keyword', path='{_basename}') to find exact text references."
                            )
                        })
                except Exception as skel_err:
                    log.warning(f"[BRIDGE] Skeleton gen failed for smart read, falling back to full read: {skel_err}")

            offset = max(1, int(args.get("offset", 1))) - 1
            limit = int(args.get("limit", len(lines)))
            # ── Smart chunk boundary snapping ────────────────────
            # Instead of cutting at an arbitrary line number that may
            # split a function/class in half, snap the chunk end to
            # the nearest structural boundary (next def/class/function).
            # This mirrors Cursor's AST-aware chunking.
            _raw_end = offset + limit
            _file_ext = os.path.splitext(fpath)[1].lower()
            _snapped_end = AgentBridge._snap_chunk_boundary(lines, _raw_end, _file_ext)
            if _snapped_end != _raw_end:
                _old_limit = limit
                limit = max(limit, _snapped_end - offset)
                if limit != _old_limit:
                    log.debug(
                        f"[BRIDGE] Smart chunk: snapped limit {_old_limit} → {limit} "
                        f"(boundary at line {_snapped_end + 1} in {os.path.basename(fpath)})"
                    )
            content = "".join(lines[offset: offset + limit])
            read_lines = len(lines[offset: offset + limit])
            _remaining_chars = self._tool_ctx.get_remaining_budget_chars()
            if len(content) > _remaining_chars:
                _basename = os.path.basename(fpath)
                return ToolResult(
                    tool_id=tool_id, result=None, success=False,
                    error=(
                        f"Read output too large for current context budget "
                        f"({len(content):,} chars > ~{_remaining_chars:,} remaining). "
                        f"Read a smaller chunk: Read(file_path='{_basename}', offset={offset + 1}, limit=80)."
                    )
                )
            
            # Track this read for budget purposes
            self._tool_ctx.mark_file_read(fpath)
            self._tool_ctx.track_file_read(fpath, len(content))
            
            # Check for budget warnings
            warnings = self._tool_ctx.get_budget_warnings()
            if warnings:
                log.warning(f"[CTX] Budget warnings: {warnings}")
            
            # Populate context.read_file_state so FileEditTool staleness check passes
            try:
                _norm = os.path.abspath(fpath)
                _off_raw = args.get("offset")
                _lim_raw = args.get("limit")
                _mtime = os.path.getmtime(fpath)
                self._tool_ctx.read_file_state[_norm] = {
                    "content": content,
                    "timestamp": _mtime,
                    "offset": int(_off_raw) if _off_raw is not None else None,
                    "limit": int(_lim_raw) if _lim_raw is not None else None,
                }
                # Populate LRU dedup cache
                self._tool_ctx.file_cache_put(
                    _norm, content, _mtime,
                    int(_off_raw) if _off_raw is not None else None,
                    int(_lim_raw) if _lim_raw is not None else None,
                )
            except Exception:
                pass
            return ToolResult(tool_id=tool_id, result={
                "path": fpath,
                "content": content,
                "start_line": offset + 1,
                "num_lines": read_lines,
                "total_lines": _total_lines,
            })
        except Exception as e:
            return ToolResult(tool_id=tool_id, result=None, success=False, error=str(e))

    def _resolve_directory_target_path(self, requested_path: str, content: str = "") -> Optional[str]:
        """
        Resolve a directory target to a concrete file path when possible.
        Returns None when no sensible file target can be inferred.
        """
        if not os.path.isdir(requested_path):
            return requested_path

        # Infer filename from content type when the AI forgot to specify one.
        # This prevents the active-file resolution from redirecting writes
        # (e.g., AI wants to create index.html but active file is enhancement_plan.md).
        content_stripped = content.strip() if content else ""
        if content_stripped:
            if content_stripped.startswith("<!DOCTYPE") or content_stripped.startswith("<html"):
                return os.path.join(requested_path, "index.html")
            if content_stripped.startswith("<"):
                # Generic HTML/XML/SVG — use index.html as safe default
                if "</" in content_stripped[:200] or "/>" in content_stripped[:200]:
                    return os.path.join(requested_path, "index.html")
            if content_stripped.lstrip().startswith(("from ", "import ", "def ", "class ", "async def", "#!/usr/bin/env python")):
                return os.path.join(requested_path, "main.py")
            if content_stripped.lstrip().startswith(("const ", "let ", "var ", "function ", "import ", "export ")):
                return os.path.join(requested_path, "script.js")
            if content_stripped.lstrip().startswith((".", "#", "@media", "@import", "@keyframes", "body {", "html {", ":root")):
                return os.path.join(requested_path, "style.css")
            if content_stripped.startswith("---"):
                return os.path.join(requested_path, "README.md")

        # Check existing files in preferred order
        preferred_names = [
            "index.html",
            "main.py",
            "app.py",
            "script.js",
            "style.css",
            "README.md",
        ]
        for name in preferred_names:
            candidate = os.path.join(requested_path, name)
            if os.path.isfile(candidate):
                return candidate
        return None

    # ── Rejection tracking — graceful stop after repeated rejections ──

    def record_rejection(self, file_path: str) -> Optional[str]:
        """Record that the user rejected an edit. Returns a nudge message
        if the AI should stop editing this file and ask the user."""
        if not hasattr(self, '_rejection_counts'):
            self._rejection_counts: Dict[str, int] = {}
        if not hasattr(self, '_pending_rejection_nudges'):
            self._pending_rejection_nudges: List[str] = []
        count = self._rejection_counts.get(file_path, 0) + 1
        self._rejection_counts[file_path] = count
        log.info(f"[BRIDGE] Rejection recorded for {file_path}: {count}/{self._MAX_REJECTIONS}")
        if count >= self._MAX_REJECTIONS:
            nudge = (
                f"STOP: The user has rejected your edit to {os.path.basename(file_path)} "
                f"{count} times. Do NOT attempt to edit or write to this file again. "
                f"Instead, ask the user what they want — they may have a different "
                f"requirement or want a different approach. "
                f"Respond to the user explaining what happened and ask how they'd like to proceed."
            )
            self._pending_rejection_nudges.append(nudge)
            return nudge
        return None

    def clear_rejections(self, file_path: str) -> None:
        """Clear rejection count after successful accept."""
        if hasattr(self, '_rejection_counts'):
            self._rejection_counts.pop(file_path, None)

    async def _dispatch_write(self, tool_id: str, args: Dict[str, Any]) -> ToolResult:
        """Dispatch to real FileWriteTool or bridge-native fallback."""
        # Support both "file_path" (schema standard) and "path" (some LLMs use shorthand)
        path = args.get("file_path", "") or args.get("path", "")
        content = args.get("content", "")
        
        # DIAGNOSTIC: Log full args when file_path is empty to detect parameter name mismatch
        if not path:
            log.warning(f"[WRITE] Empty file_path! Full args keys: {list(args.keys())}. Args preview: {str(args)[:500]}")
        
        # INDUSTRY-STANDARD: Strict path validation
        # Reject empty, blank, and directory paths IMMEDIATELY
        if not path or not path.strip() or path.endswith((os.sep, '/')) or os.path.isdir(path):
            reason = "empty or blank" if (not path or not path.strip()) else "directory"
            log.warning(f"[WRITE] Rejected {reason} path: '{path}'")
            return ToolResult(
                tool_id=tool_id,
                result=None,
                success=False,
                error=(
                    f"ERROR: Missing or invalid file_path parameter.\n\n"
                    f"You MUST provide the 'file_path' parameter with a complete file path including filename and extension.\n"
                    f"Examples:\n"
                    f"  CORRECT: Write(file_path='index.html', content='...')\n"
                    f"  CORRECT: Write(file_path='src/questions.js', content='...')\n"
                    f"  WRONG: Write(content='...')  ← missing file_path!\n"
                    f"  WRONG: Write(file_path='src/', content='...')  ← directory, not file\n\n"
                    f"The 'file_path' parameter is REQUIRED. Please call Write again with a valid file_path."
                ),
            )
        
        if not os.path.isabs(path) and self._project_root:
            args = {**args, "file_path": os.path.join(self._project_root, path)}
        full_path = str(args["file_path"])
        if os.path.isdir(full_path):
            resolved = self._resolve_directory_target_path(full_path, content)
            if resolved is None:
                return ToolResult(
                    tool_id=tool_id,
                    result=None,
                    success=False,
                    error=(
                        f"Write expected a file path, but received directory: {full_path}. "
                        "Provide a concrete file path (for example, index.html or main.py)."
                    ),
                )
            args = {**args, "file_path": resolved}
            full_path = resolved
            log.warning(f"[BRIDGE] Write received directory path; auto-resolved to file: {full_path}")

        # ── HALLUCINATION GUARD: Block known-hallucinated project-root memory files ──
        # The agent sometimes invents memory filenames (CORTEX_MEMORY.md, etc.) instead
        # of using the proper memory system (.cortex/memory/ and .cortex/memory.json).
        # Refuse these writes and redirect to the correct memory tools.
        _BLACKLISTED_PROJECT_ROOT_FILES = {
            'CORTEX_MEMORY.md',
            'CORTEX_MEMORY',
            'PROJECT_MEMORY.md',
            'AI_MEMORY.md',
            'AGENT_MEMORY.md',
            'MEMORY.md',  # reserved — only the memdir system writes MEMORY.md
            'SESSION_MEMORY.md',
            'PROJECT_CONTEXT.md',  # use .cortex/context.md instead
            'PROJECT_RULES.md',    # use .cortex/rules.md instead
            'AGENT_RULES.md',
            'CORTEX_NOTES.md',
            'AI_NOTES.md',
        }
        _filename = os.path.basename(full_path).upper()
        if _filename in _BLACKLISTED_PROJECT_ROOT_FILES:
            # Determine if this is being written to the project root
            _parent_dir = os.path.dirname(full_path)
            _in_project_root = (
                self._project_root
                and os.path.normpath(_parent_dir) == os.path.normpath(self._project_root)
            )
            if _in_project_root:
                return ToolResult(
                    tool_id=tool_id,
                    result=None,
                    success=False,
                    error=(
                        f"BLOCKED: Cannot create '{os.path.basename(full_path)}' in the project root.\n\n"
                        f"Cortex has a proper memory system — use these instead:\n"
                        f"  • update_cortex_project_memory(type, entry) → writes to .cortex/memory.json\n"
                        f"  • Memory directory → ~/.cortex/projects/*/memory/ (use Read/Write tools)\n"
                        f"  • .cortex/context.md → project overview and architecture\n"
                        f"  • .cortex/rules.md → coding conventions and rules\n\n"
                        f"Do NOT create memory or context files in the project root."
                    ),
                )
        is_new = not os.path.exists(full_path)
        original_content: Optional[str] = None
        if not is_new:
            try:
                with open(full_path, "r", encoding="utf-8") as f:
                    original_content = f.read()
            except Exception:
                original_content = None

        # ── SAFETY: Warn on catastrophic overwrite of large files ──
        # Warn when existing file is VERY large (>50KB) and new content
        # is extremely small (<5% of original) — almost certainly truncated output.
        # In AUTO mode: warn only (like Cursor/Claude Code). In ASK mode: block.
        if not is_new:
            try:
                existing_size = os.path.getsize(full_path)
                new_size = len(content.encode('utf-8'))
                if existing_size > 50_000 and new_size < existing_size * 0.05:
                    if self._always_allowed:
                        # AUTO mode: warn but proceed
                        log.warning(
                            f"[WRITE] Catastrophic overwrite warning: {os.path.basename(full_path)} "
                            f"({existing_size} → {new_size} bytes, {new_size*100//max(existing_size,1)}%). "
                            f"Proceeding in AUTO mode."
                        )
                    else:
                        # ASK mode: block as before
                        return ToolResult(
                            tool_id=tool_id, result=None, success=False,
                            error=(
                                f"SAFETY: Refusing to overwrite {os.path.basename(full_path)} "
                                f"({existing_size} bytes) with much smaller content "
                                f"({new_size} bytes). "
                                f"Use the Edit tool for targeted changes, or "
                                f"switch to AUTO mode to allow full rewrites."
                            ),
                        )
            except OSError:
                pass  # File stat failed — proceed with write

        # ── Project access permission gate for Write on existing files ────────
        # Single initial permission: once granted, all operations proceed without asking.
        if not is_new and not self._project_access_granted and not self._always_allowed and not self._stop_requested:
            granted = await self._request_project_access()
            if not granted:
                return ToolResult(tool_id=tool_id, result=None, success=False,
                    error=(
                        "STOP: The user REJECTED or did not respond to project access. "
                        "Do NOT continue with more tools. "
                        "Do NOT write new files or inject code into the project. "
                        "STOP and wait for the user's next message. "
                        "Respond to the user explaining that the edit was declined."
                    ))

        if not is_new and self._stop_requested:
            return ToolResult(tool_id=tool_id, result=None, success=False,
                error=(
                    "STOP: The user REJECTED this edit. Do NOT continue with more tools. "
                    "Do NOT write new files or inject code into the project. "
                    "The user has made their choice — STOP and wait for the user's next message. "
                    "Respond to the user explaining that the edit was declined."
                ))

        # Emit signal to show file operation card with animation
        card_id = None
        ui_op_type = "create" if is_new else "edit"
        try:
            import uuid
            card_id = f"file-op-{uuid.uuid4().hex[:8]}"
            if is_new:
                self.file_creating_started.emit(full_path)
            else:
                self.file_editing_started.emit(full_path)
        except Exception as e:
            log.debug(f"[BRIDGE] Failed to emit file operation started: {e}")

        if _REAL_FILE_WRITE_TOOL is not None:
            try:
                real_write_tool = cast(Any, _REAL_FILE_WRITE_TOOL)
                raw_any: Any = await real_write_tool.call(
                    args, self._tool_ctx, _always_allow_tool, _STUB_PARENT_MESSAGE
                )
                raw_map = cast(Dict[str, Any], raw_any) if isinstance(raw_any, dict) else {}
                data_any = raw_map.get("data", {})
                data_map = cast(Dict[str, Any], data_any) if isinstance(data_any, dict) else {}
                op_raw = data_map.get("type", "create" if is_new else "update")
                op_type: str = op_raw if isinstance(op_raw, str) else ("create" if is_new else "update")

                # ── DEFER MECHANISM for existing files ──
                # When Write updates an existing file, defer the change so
                # Accept/Reject works. Without this, _on_file_accepted finds
                # nothing in _deferred_edits and the accept is a no-op.
                if (not is_new) and (original_content is not None) and (original_content != content):
                    # Store new content so Accept can write it later
                    key = self._norm(full_path)
                    # Check for previous deferred BEFORE overwriting
                    _prev_deferred_w = self._deferred_edits.get(key)
                    # For Write tool: new content IS the new state (full replacement)
                    self._deferred_edits[key] = content
                    log.info(f"[BRIDGE] Write deferred: stored new content for {key} ({len(content)} chars) — waiting for user Accept")
                    # Restore disk to the correct baseline.
                    # If a previous deferred edit exists, restore to THAT content
                    # (not raw original_content) to preserve previous accepted edits.
                    _restore_w = _prev_deferred_w if _prev_deferred_w is not None else original_content
                    try:
                        with open(full_path, "w", encoding="utf-8", newline="") as _f:
                            _f.write(_restore_w)
                        # Sync read_file_state so next edit doesn't fail "unexpectedly modified"
                        _abs = os.path.abspath(full_path)
                        if hasattr(self, '_tool_ctx') and self._tool_ctx and hasattr(self._tool_ctx, 'read_file_state'):
                            self._tool_ctx.read_file_state[_abs] = {
                                "content": _restore_w,
                                "timestamp": os.path.getmtime(full_path),
                                "offset": None,
                                "limit": None,
                            }
                        _label = "PREVIOUS deferred" if _prev_deferred_w is not None else "original"
                        log.info(f"[BRIDGE] Write deferred: restored {_label} content to {full_path}")
                    except Exception as _restore_err:
                        log.warning(f"[BRIDGE] Write deferred: failed to restore: {_restore_err}")

                # ── Emit UI signals (non-critical — catch failures so the real
                #    write path succeeds even if PyQt signals aren't bound) ──
                # For deferred writes, DON'T emit file_generated — it triggers
                # _on_agent_file_generated which reloads the file from disk.
                # Since we just restored the original content, the reload would
                # show the original, but the signal carries the NEW content
                # which can confuse caches. Only emit for non-deferred writes.
                _is_deferred = (not is_new) and (original_content is not None) and (original_content != content)
                if not _is_deferred:
                    try:
                        self.file_generated.emit(full_path, content)
                    except Exception:
                        pass
                if _is_deferred:
                    try:
                        self.file_edited_diff.emit(full_path, original_content, content)
                    except Exception:
                        pass
                # Emit completion signal for card animation
                if card_id:
                    try:
                        self.file_operation_completed.emit(card_id, full_path, content, ui_op_type)
                    except Exception:
                        pass
                try:
                    self.file_tree_refresh_needed.emit(full_path)
                except Exception:
                    pass
                self._tool_ctx.mark_file_modified(full_path)
                # For deferred writes (existing files), indicate the edit is
                # pending user approval — the file hasn't been changed yet.
                if _is_deferred:
                    return ToolResult(tool_id=tool_id, result={
                        "path": full_path, "type": op_type, "written": False,
                        "deferred": True,
                        "message": f"Edit queued for user approval. The file will be updated after user accepts. You can verify the file content after the user accepts the edit.",
                    })
                return ToolResult(tool_id=tool_id, result={
                    "path": full_path, "type": op_type, "written": True,
                })
            except Exception as exc:
                # Only reach here if real_write_tool.call() itself threw —
                # signal failures are now caught inline above.
                log.warning(f"[BRIDGE] Real FileWriteTool.call() raised, using fallback: {exc}")

        # Fallback: simple write
        try:
            parent = os.path.dirname(full_path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            # CRITICAL: Normalize line endings to prevent doubled empty lines.
            # Without this, mixed \r\n and \n in content causes each line to
            # appear followed by an empty line after save.
            normalized_content = content.replace("\r\n", "\n").replace("\r", "\n")

            # ── DEFER MECHANISM for existing files (fallback path) ──
            if (not is_new) and (original_content is not None) and (original_content != content):
                key = self._norm(full_path)
                # Check for previous deferred BEFORE overwriting
                _prev_deferred_wf = self._deferred_edits.get(key)
                self._deferred_edits[key] = normalized_content
                log.info(f"[BRIDGE] Write deferred (fallback): stored new content for {key} ({len(normalized_content)} chars)")
                # Restore disk — use previous deferred if available, else original
                _restore_wf = _prev_deferred_wf if _prev_deferred_wf is not None else original_content
                with open(full_path, "w", encoding="utf-8", newline="") as f:
                    f.write(_restore_wf)
                # Sync read_file_state so next edit doesn't fail "unexpectedly modified"
                _abs = os.path.abspath(full_path)
                if hasattr(self, '_tool_ctx') and self._tool_ctx and hasattr(self._tool_ctx, 'read_file_state'):
                    self._tool_ctx.read_file_state[_abs] = {
                        "content": _restore_wf,
                        "timestamp": os.path.getmtime(full_path),
                        "offset": None,
                        "limit": None,
                    }
                _label = "PREVIOUS deferred" if _prev_deferred_wf is not None else "original"
                log.info(f"[BRIDGE] Write deferred (fallback): restored {_label} to {full_path}")
            else:
                # New file or unchanged — write directly
                with open(full_path, "w", encoding="utf-8", newline="") as f:
                    f.write(normalized_content)
            # ── Emit UI signals (non-critical) ──
            _is_deferred = (not is_new) and (original_content is not None) and (original_content != content)
            if not _is_deferred:
                try:
                    self.file_generated.emit(full_path, content)
                except Exception:
                    pass
            if _is_deferred:
                try:
                    self.file_edited_diff.emit(full_path, original_content, content)
                except Exception:
                    pass
            # Emit completion signal for card animation
            if card_id:
                try:
                    self.file_operation_completed.emit(card_id, full_path, content, ui_op_type)
                except Exception:
                    pass
            try:
                self.file_tree_refresh_needed.emit(full_path)
            except Exception:
                pass
            self._tool_ctx.mark_file_modified(full_path)
            # For deferred writes (existing files), indicate the edit is
            # pending user approval — the file hasn't been changed yet.
            if _is_deferred:
                return ToolResult(tool_id=tool_id, result={
                    "path": full_path, "type": "update", "written": False,
                    "deferred": True,
                    "message": f"Edit queued for user approval. The file will be updated after user accepts. You can verify the file content after the user accepts the edit.",
                })
            return ToolResult(tool_id=tool_id, result={
                "path": full_path, "type": "create" if is_new else "update", "written": True,
            })
        except Exception as e:
            return ToolResult(tool_id=tool_id, result=None, success=False, error=str(e))

    async def _dispatch_edit(self, tool_id: str, args: Dict[str, Any]) -> ToolResult:
        """Dispatch to real FileEditTool or bridge-native fallback."""
        # Support both "file_path" (schema standard) and "path" (some LLMs use shorthand)
        path = args.get("file_path", "") or args.get("path", "")
        old_string = args.get("old_string", "")
        new_string = args.get("new_string", "")
        
        # DIAGNOSTIC: Log full args when file_path is empty to detect parameter name mismatch
        if not path:
            log.warning(f"[EDIT] Empty file_path! Full args keys: {list(args.keys())}. Args preview: {str(args)[:500]}")
        
        # Reject empty, blank, and directory paths IMMEDIATELY
        if not path or not path.strip() or path.endswith((os.sep, '/')) or os.path.isdir(path):
            reason = "empty or blank" if (not path or not path.strip()) else "directory"
            log.warning(f"[EDIT] Rejected {reason} path: '{path}'")
            return ToolResult(
                tool_id=tool_id,
                result=None,
                success=False,
                error=(
                    f"ERROR: Missing or invalid file_path parameter.\n\n"
                    f"You MUST provide the 'file_path' parameter with a complete file path including filename and extension.\n"
                    f"Examples:\n"
                    f"  CORRECT: Edit(file_path='index.html', old_string='...', new_string='...')\n"
                    f"  CORRECT: Edit(file_path='src/questions.js', old_string='...', new_string='...')\n"
                    f"  WRONG: Edit(old_string='...', new_string='...')  ← missing file_path!\n"
                    f"  WRONG: Edit(file_path='src/', old_string='...', new_string='...')  ← directory, not file\n\n"
                    f"The 'file_path' parameter is REQUIRED. Please call Edit again with a valid file_path."
                ),
            )
        if not os.path.isabs(path) and self._project_root:
            args = {**args, "file_path": os.path.join(self._project_root, path)}
        full_path = str(args["file_path"])
        if os.path.isdir(full_path):
            resolved = self._resolve_directory_target_path(full_path)
            if resolved is None:
                return ToolResult(
                    tool_id=tool_id,
                    result=None,
                    success=False,
                    error=(
                        f"Edit expected a file path, but received directory: {full_path}. "
                        "Use a concrete file path to edit (for example, index.html or main.py)."
                    ),
                )
            args = {**args, "file_path": resolved}
            full_path = resolved
            log.warning(f"[BRIDGE] Edit received directory path; auto-resolved to file: {full_path}")

        # Register CortexDiffBridge open-diff callback (idempotent — safe to call each time)
        if _CORTEX_DIFF_BRIDGE is not None and not _CORTEX_DIFF_BRIDGE.is_registered:
            _CORTEX_DIFF_BRIDGE.register_open_diff(
                lambda fp, old, new: self.file_edited_diff.emit(fp, old, new)
            )
            log.info("[BRIDGE] CortexDiffBridge open_diff callback registered")

        # ── File edit permission gate ────────────────────────────────────────
        # Rules:
        #   Always Allow  → _always_allowed=True, never ask again this session
        #   Allow Once    → allow THIS edit, reset so card shows for next edit
        #   Reject        → reject THIS edit only, reset so card shows for next edit
        # New file creation bypasses this gate entirely (handled in _dispatch_write).
        if not self._project_access_granted and not self._always_allowed and not self._stop_requested:
            granted = await self._request_project_access()
            if not granted:
                return ToolResult(tool_id=tool_id, result=None, success=False,
                    error=(
                        "STOP: The user REJECTED or did not respond to project access. "
                        "Do NOT continue with more tools. "
                        "Do NOT write new files or inject code into the project. "
                        "STOP and wait for the user's next message. "
                        "Respond to the user explaining that the edit was declined."
                    ))

        if self._stop_requested:
            return ToolResult(tool_id=tool_id, result=None, success=False,
                error=(
                    "STOP: The user REJECTED this edit. Do NOT continue with more tools. "
                    "Do NOT write new files or inject code into the project. "
                    "The user has made their choice — STOP and wait for the user's next message. "
                    "Respond to the user explaining that the edit was declined."
                ))
        # ────────────────────────────────────────────────────────────────────

        # Emit signal to show "Editing file..." card with animation
        card_id = None
        original_content = None
        # Check for previous deferred edit BEFORE anything else.
        # If one exists, we must compute full_new on top of THAT content
        # (not disk), because the LLM sees the previous deferred state and
        # sends old_string/new_string relative to it.
        key = self._norm(full_path)
        previous_deferred = self._deferred_edits.get(key)
        try:
            import uuid
            card_id = f"file-op-{uuid.uuid4().hex[:8]}"
            # Read original content for later comparison
            try:
                with open(full_path, "r", encoding="utf-8") as f:
                    original_content = f.read()
            except Exception:
                pass
            self.file_editing_started.emit(full_path)
        except Exception as e:
            log.debug(f"[BRIDGE] Failed to emit file_editing_started: {e}")

        # ── UNIFIED IN-MEMORY EDIT HANDLER ──
        # Disk is NEVER modified during editing. Only Accept writes to disk.
        # This replaces the old 3-path system (FAST PATH + FileEditTool + Fallback)
        # with a single clean flow: find old_string in baseline, compute new content,
        # store as deferred. No disk writes, no restore logic, no race conditions.
        disk_content = original_content  # already read at line ~7173
        if disk_content is None:
            try:
                with open(full_path, "r", encoding="utf-8") as _f:
                    disk_content = _f.read()
            except Exception:
                try:
                    with open(full_path, "r", encoding="cp1252") as _f:
                        disk_content = _f.read()
                except Exception:
                    return ToolResult(tool_id=tool_id, result=None, success=False,
                                      error=f"Cannot read {full_path}")

        # Determine baseline: what the LLM "sees" (deferred state or disk)
        baseline = previous_deferred if previous_deferred is not None else disk_content

        # ── Find old_string in baseline ──
        actual_old = old_string
        actual_new = new_string
        full_new = None

        if old_string in baseline:
            full_new = baseline.replace(old_string, new_string, 1)
            log.info(f"[BRIDGE] Edit: old_string found in "
                     f"{'deferred' if previous_deferred is not None else 'disk'} "
                     f"({len(baseline)} -> {len(full_new)} chars)")
        else:
            # Try quote normalization (smart quotes, different apostrophes, etc.)
            try:
                from src.agent.src.tools.FileEditTool.FileEditTool import find_actual_string
                actual_old_found = find_actual_string(baseline, old_string)
                if actual_old_found:
                    actual_old = actual_old_found
                    full_new = baseline.replace(actual_old_found, new_string, 1)
                    log.info(f"[BRIDGE] Edit: quote-normalized match in "
                             f"{'deferred' if previous_deferred is not None else 'disk'}")
            except Exception:
                pass

        # ── Parallel edit detection ──
        # When multiple edits target the same file in one turn, both are based on
        # the same disk state. Edit 1 already accumulated into previous_deferred.
        # Edit 2's old_string may NOT exist in previous_deferred but DOES exist in
        # the original disk content. Fix: apply to disk, merge with deferred.
        if full_new is None and previous_deferred is not None and disk_content and old_string in disk_content:
            log.info(f"[BRIDGE] Edit: parallel edit detected — old_string in disk "
                     f"({len(disk_content)} chars)")
            disk_with_edit = disk_content.replace(old_string, new_string, 1)
            if disk_with_edit == previous_deferred:
                full_new = previous_deferred  # duplicate edit — skip
                log.info("[BRIDGE] Edit: parallel edit is duplicate — skipping")
            else:
                full_new = self._merge_parallel_edits(
                    previous_deferred, disk_content, disk_with_edit
                )
                if full_new:
                    log.info(f"[BRIDGE] Edit: merged parallel edit "
                             f"({len(previous_deferred)} -> {len(full_new)} chars)")
                else:
                    full_new = previous_deferred
                    log.warning("[BRIDGE] Edit: parallel edits overlap — keeping accumulated")

        # ── Error: old_string not found anywhere ──
        if full_new is None:
            _deferred_len = len(previous_deferred) if previous_deferred else 0
            log.warning(f"[BRIDGE] Edit FAILED: old_string not found "
                        f"(deferred={_deferred_len}, disk={len(disk_content)})")
            # Re-read from disk to get the absolute latest content
            try:
                with open(full_path, "r", encoding="utf-8") as _f:
                    fresh_content = _f.read()
                if old_string in fresh_content:
                    # Found in fresh content — apply directly
                    full_new = fresh_content.replace(old_string, new_string, 1)
                    log.info(f"[BRIDGE] Edit: found in fresh disk read after initial miss")
            except Exception:
                pass
            
            if full_new is None:
                snippet = baseline[:200] if baseline else "(empty)"
                return ToolResult(tool_id=tool_id, result=None, success=False,
                                  error=(
                                      f"String to replace not found in file. "
                                      f"The file may have been modified since you last read it. "
                                      f"Use the Read tool to get the current content, then try again. "
                                      f"File starts with: {snippet[:120]}..."
                                  ))

        # Log edit details
        log.info(f"[BRIDGE] Edit details: old_len={len(actual_old)}, "
                 f"new_len={len(actual_new)}, old_preview={actual_old[:80]!r}, "
                 f"new_preview={actual_new[:80]!r}")

        # ── Write to disk directly (permission granted) or store as deferred ──
        _has_permission = self._always_allowed or self._file_edit_permission in ("once", "always")
        if _has_permission:
            try:
                with open(full_path, "w", encoding="utf-8") as _f:
                    _f.write(full_new)
                log.info(f"[BRIDGE] Wrote edit directly to disk: {key} ({len(full_new)} chars)")
            except Exception as _we:
                log.error(f"[BRIDGE] Direct write failed: {_we}")
        else:
            self._deferred_edits[key] = full_new
            log.info(f"[BRIDGE] Stored deferred edit for {key} ({len(full_new)} chars)")

        # Update read_file_state so next edit's staleness check passes
        try:
            _abs = os.path.abspath(full_path)
            if (hasattr(self, '_tool_ctx') and self._tool_ctx
                    and hasattr(self._tool_ctx, 'read_file_state')):
                self._tool_ctx.read_file_state[_abs] = {
                    "content": full_new,
                    "timestamp": os.path.getmtime(full_path),
                    "offset": None,
                    "limit": None,
                }
        except Exception:
            pass

        # Emit UI signals for diff card and animation
        try:
            self.file_edited_diff.emit(full_path, disk_content, full_new)
        except Exception:
            pass
        if card_id:
            try:
                self.file_operation_completed.emit(card_id, full_path, full_new, "edit")
            except Exception:
                pass
        self._tool_ctx.mark_file_modified(full_path)
        self._safe_emit(self.file_edit_notification, full_path, "edit", "complete")
        await self._refresh_git_diff_stats(full_path)
        # Notify sidebar to refresh file tree after edit
        try:
            self.file_tree_refresh_needed.emit(full_path)
        except Exception:
            pass
        # Snapshot permission before reset so _has_perm is computed correctly
        _perm_snapshot = self._file_edit_permission
        # "once" → reset so the card appears again for the next edit
        if self._file_edit_permission == "once":
            self._file_edit_permission = None
        _has_perm = self._always_allowed or _perm_snapshot in ("once", "always")
        if _has_perm:
            return ToolResult(tool_id=tool_id, result={
                "path": full_path, "edited": True, "deferred": False,
                "message": f"Edit applied directly to {full_path}.",
            })
        return ToolResult(tool_id=tool_id, result={
            "path": full_path, "edited": False, "deferred": True,
            "message": (
                f"Edit queued for user approval. The file will be updated after user accepts. "
                f"You can verify the file content after the user accepts the edit."
            ),
        })


    async def _refresh_git_diff_stats(self, file_path: str) -> None:
        """
        After a file edit, re-fetch git diff stats via DiffDataService.
        Emits an updated file_edited_diff signal with accurate git-based
        +added/-removed counts appended to the result (used by sidebar panel).
        """
        if _DIFF_SERVICE is None:
            return
        try:
            diff_data = await _DIFF_SERVICE.fetch_diff_data()
            norm = os.path.normpath(file_path)
            # Find this file in the git diff results (match by basename or full path)
            diff_files_any = getattr(diff_data, "files", None)
            diff_files: List[Any] = cast(List[Any], diff_files_any) if isinstance(diff_files_any, list) else []
            for diff_file in diff_files:
                diff_path = getattr(diff_file, "path", "")
                if not isinstance(diff_path, str) or not diff_path:
                    continue
                git_norm = os.path.normpath(diff_path)
                if git_norm == norm or os.path.basename(git_norm) == os.path.basename(norm):
                    _added   = getattr(diff_file, 'lines_added', 0) or 0
                    _removed = getattr(diff_file, 'lines_removed', 0) or 0
                    _binary  = getattr(diff_file, 'is_binary', False) or False
                    _large   = getattr(diff_file, 'is_large_file', False) or False
                    _flags = (" [binary]" if _binary else "") + (" [large]" if _large else "")
                    log.info(
                        f"[BRIDGE] Git diff stats for {diff_path}: +{_added} -{_removed}{_flags}"
                    )
                    break
        except Exception as exc:
            log.debug(f"[BRIDGE] _refresh_git_diff_stats failed: {exc}")

    def _resolve_project_path(self, raw_path: str) -> Optional[str]:
        """
        Resolve a potentially-relative path against the project root and
        validate it stays within the project boundary.

        Returns the normalized absolute path if valid, or None if:
          - The path escapes the project root directory
        """
        if not raw_path:
            return None
        resolved = os.path.normpath(raw_path)
        if self._project_root and not os.path.isabs(resolved):
            resolved = os.path.normpath(os.path.join(self._project_root, resolved))
        # Boundary check: reject paths that escape the project root
        # Use case-insensitive comparison on Windows for robustness.
        if self._project_root:
            try:
                resolved_real = os.path.realpath(resolved)
                project_real = os.path.realpath(self._project_root)
                sep = os.sep
                # Normalize casing for cross-platform safety
                rr_norm = resolved_real.lower() if os.name == 'nt' else resolved_real
                pr_norm = project_real.lower() if os.name == 'nt' else project_real
                if not rr_norm.startswith(pr_norm + sep) and rr_norm != pr_norm:
                    log.warning(
                        f"[BRIDGE] Path '{raw_path}' resolves outside project root "
                        f"({resolved_real}) — rejecting"
                    )
                    return None
            except Exception as exc:
                log.warning(f"[BRIDGE] Path resolution failed for '{raw_path}': {exc}")
                return None
        return resolved

    async def _dispatch_glob(self, tool_id: str, args: Dict[str, Any]) -> ToolResult:
        """Dispatch to real GlobTool or bridge-native fallback."""
        # ── Pre-flight path normalization & boundary check ────────────
        _search_dir = self._resolve_project_path(args.get("path", ""))
        if _search_dir is None and args.get("path", ""):
            # Path was explicitly provided but escapes project boundary
            return ToolResult(
                tool_id=tool_id, result=None, success=False,
                error=f"[Scope] Path '{args['path']}' is outside the project root. "
                      f"Glob is restricted to the opened project."
            )
        if not _search_dir:
            _search_dir = self._get_project_root()
        if _search_dir:
            args["path"] = _search_dir
        log.info(f"[GLOB] Searching dir={_search_dir!r}, project_root={self._project_root!r}, pattern={args.get('pattern','')!r}")

        if _REAL_GLOB_TOOL is not None:
            try:
                real_glob_tool = cast(Any, _REAL_GLOB_TOOL)
                raw_any: Any = await real_glob_tool.call(args, self._tool_ctx)
                raw_map = cast(Dict[str, Any], raw_any) if isinstance(raw_any, dict) else {}
                data_any = raw_map.get("data", {})
                data_map = cast(Dict[str, Any], data_any) if isinstance(data_any, dict) else {}
                filenames_raw = data_map.get("filenames", [])
                filenames: List[str] = [
                    f for f in cast(List[Any], filenames_raw) if isinstance(f, str)
                ] if isinstance(filenames_raw, list) else []
                # P6: Apply search exclusion patterns
                filenames = AgentSafetyGuard.filter_search_results(filenames)
                return ToolResult(tool_id=tool_id, result={
                    "pattern": args.get("pattern", ""),
                    "files": filenames,
                    "numFiles": data_map.get("numFiles", len(filenames)),
                    "truncated": data_map.get("truncated", False),
                })
            except Exception as exc:
                log.warning(f"[BRIDGE] Real GlobTool failed, using fallback: {exc}")

        # Fallback: simple glob
        import glob as _glob
        pattern = args.get("pattern", "")
        search_dir = _search_dir or self._get_project_root()
        # Safety net: ensure fallback search_dir is within project
        if self._project_root and search_dir:
            try:
                sr_norm = os.path.realpath(search_dir).lower() if os.name == 'nt' else os.path.realpath(search_dir)
                pr_norm = os.path.realpath(self._project_root).lower() if os.name == 'nt' else os.path.realpath(self._project_root)
                if not sr_norm.startswith(pr_norm + os.sep) and sr_norm != pr_norm:
                    search_dir = self._project_root
                    log.warning(f"[BRIDGE] Glob fallback clamped to project root")
            except Exception:
                search_dir = self._project_root
        full_pattern = os.path.join(search_dir, pattern) if not os.path.isabs(pattern) else pattern
        try:
            files = sorted(_glob.glob(full_pattern, recursive=True))
            # P6: Apply search exclusion patterns
            files = AgentSafetyGuard.filter_search_results(files)
            return ToolResult(tool_id=tool_id, result={"pattern": pattern, "files": files})
        except Exception as e:
            return ToolResult(tool_id=tool_id, result=None, success=False, error=str(e))

    async def _dispatch_grep(self, tool_id: str, args: Dict[str, Any]) -> ToolResult:
        """Dispatch to real GrepTool or bridge-native fallback."""
        # ── Pre-flight path normalization & boundary check ─────────
        # The real GrepTool (rust rg) can choke on Windows backslash paths,
        # trailing spaces, or non-existent directories. Normalize and validate
        # before calling so we can fall back cleanly instead of crashing.
        # CRITICAL: Also enforce project-root boundary to prevent path slip.
        _search_path = self._resolve_project_path(args.get("path", ""))
        if _search_path is None and args.get("path", ""):
            # Path was explicitly provided but escapes project boundary
            return ToolResult(
                tool_id=tool_id, result=None, success=False,
                error=f"[Scope] Path '{args['path']}' is outside the project root. "
                      f"Grep is restricted to the opened project."
            )
        # Default to project root when no path given
        if not _search_path:
            _search_path = self._get_project_root()
        if _search_path:
            if not os.path.exists(_search_path):
                log.warning(
                    f"[BRIDGE] GrepTool path not found: {_search_path}, "
                    f"falling back to pure-Python grep"
                )
            else:
                args["path"] = _search_path

        log.info(f"[GREP] Searching path={_search_path!r}, project_root={self._project_root!r}, pattern={args.get('pattern','')!r}")
        if _REAL_GREP_TOOL is not None and _search_path and os.path.exists(_search_path):
            try:
                real_grep_tool = cast(Any, _REAL_GREP_TOOL)
                raw_any: Any = await real_grep_tool.call(args, self._tool_ctx)
                raw_map = cast(Dict[str, Any], raw_any) if isinstance(raw_any, dict) else {}
                data_any = raw_map.get("data", {})
                data = cast(Dict[str, Any], data_any) if isinstance(data_any, dict) else {}
                # Use map_tool_result_to_block for LLM-friendly output
                map_to_block = getattr(real_grep_tool, "map_tool_result_to_block", None)
                if callable(map_to_block):
                    map_to_block_fn = cast(Callable[[Dict[str, Any], str], Dict[str, Any]], map_to_block)
                    block = map_to_block_fn(data, tool_id)
                    return ToolResult(tool_id=tool_id, result={
                        "pattern": args.get("pattern", ""),
                        "matches": block.get("content", str(data)),
                    })
                return ToolResult(tool_id=tool_id, result=data)
            except Exception as exc:
                log.warning(f"[BRIDGE] Real GrepTool failed, using fallback: {exc}")

        # Fallback: pure-Python grep — no rg/grep binary required
        import re as _re
        import fnmatch as _fnmatch
        _FALLBACK_MATCH_LIMIT = 80  # Consistent with GrepTool.DEFAULT_HEAD_LIMIT
        pattern  = args.get("pattern", "")
        search_path = args.get("path", self._get_project_root())
        if not os.path.isabs(search_path) and self._project_root:
            search_path = os.path.join(self._project_root, search_path)
        # Safety net: clamp fallback search path to project root boundary
        if self._project_root and search_path:
            try:
                sp_norm = os.path.realpath(search_path).lower() if os.name == 'nt' else os.path.realpath(search_path)
                pr_norm = os.path.realpath(self._project_root).lower() if os.name == 'nt' else os.path.realpath(self._project_root)
                if not sp_norm.startswith(pr_norm + os.sep) and sp_norm != pr_norm:
                    search_path = self._project_root
                    log.warning(f"[BRIDGE] Grep fallback clamped to project root")
            except Exception:
                search_path = self._project_root
        include_glob = args.get("glob", args.get("include", ""))
        case_insensitive = args.get("case_insensitive", False)
        _SKIP_DIRS = {'.git', '.svn', '.hg', 'node_modules', '__pycache__', 'venv', '.venv', 'dist', 'build'}
        try:
            flags = _re.IGNORECASE if case_insensitive else 0
            compiled = _re.compile(pattern, flags)
            results: List[str] = []
            walk_target = search_path if os.path.isdir(search_path) else os.path.dirname(search_path)
            if os.path.isfile(search_path):
                # Single-file search
                files_to_scan: List[str] = [search_path]
            else:
                files_to_scan = []
                for root, dirs, files in os.walk(walk_target):
                    dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
                    for fname in files:
                        if include_glob and not _fnmatch.fnmatch(fname, include_glob):
                            continue
                        files_to_scan.append(os.path.join(root, fname))
            for fpath in files_to_scan:
                try:
                    # Read raw bytes and try multiple encodings for proper parsing.
                    # UTF-8 → Windows-1252 → Latin-1 (never fails).
                    # Avoids errors='replace' which corrupts content with U+FFFD.
                    raw_bytes = Path(fpath).read_bytes()
                    text = None
                    for enc in ('utf-8', 'cp1252', 'latin-1'):
                        try:
                            text = raw_bytes.decode(enc)
                            break
                        except (UnicodeDecodeError, ValueError):
                            continue
                    if text is None:
                        continue
                    for lineno, line in enumerate(text.splitlines(), 1):
                        if compiled.search(line):
                            results.append(f"{fpath}:{lineno}:{line.rstrip()}")
                            if len(results) >= _FALLBACK_MATCH_LIMIT:
                                break
                except (OSError, PermissionError):
                    pass
                if len(results) >= _FALLBACK_MATCH_LIMIT:
                    break
            if len(results) >= _FALLBACK_MATCH_LIMIT:
                output = "\n".join(results) + f"\n... (truncated at {_FALLBACK_MATCH_LIMIT} matches, refine your search pattern)"
            else:
                output = "\n".join(results)
            return ToolResult(tool_id=tool_id, result={
                "pattern": pattern, "matches": output or "(no matches)",
            })
        except _re.error as exc:
            return ToolResult(tool_id=tool_id, result=None, success=False,
                              error=f"Invalid regex pattern: {exc}")
        except Exception as exc:
            return ToolResult(tool_id=tool_id, result=None, success=False, error=str(exc))

    async def _dispatch_semantic_search(self, tool_id: str, args: Dict[str, Any]) -> ToolResult:
        """Dispatch to real SementicSearchTool."""
        query = args.get("query", "")
        if not query or not query.strip():
            return ToolResult(tool_id=tool_id, result=None, success=False,
                              error="query is required for semantic search")

        # ── Resolve search path ──
        _search_path = args.get("path", "")
        if _search_path:
            _resolved = self._resolve_project_path(_search_path)
            if _resolved is None:
                return ToolResult(tool_id=tool_id, result=None, success=False,
                                  error=f"[Scope] Path '{_search_path}' is outside the project root.")
            args["path"] = _resolved
        elif self._project_root:
            args["path"] = self._project_root

        if _REAL_SEMANTIC_SEARCH_TOOL is not None:
            try:
                real_tool = cast(Any, _REAL_SEMANTIC_SEARCH_TOOL)
                raw_any: Any = await real_tool.call(args, self._tool_ctx)
                raw_map = cast(Dict[str, Any], raw_any) if isinstance(raw_any, dict) else {}
                data_any = raw_map.get("data", {})
                data = cast(Dict[str, Any], data_any) if isinstance(data_any, dict) else {}

                return ToolResult(tool_id=tool_id, result={
                    "query": query,
                    "results": data.get("results", []),
                    "content": data.get("content", ""),
                    "numResults": data.get("numResults", 0),
                    "searchTimeMs": data.get("searchTimeMs", 0),
                })
            except Exception as exc:
                log.warning(f"[BRIDGE] SementicSearchTool failed: {exc}")
                return ToolResult(tool_id=tool_id, result=None, success=False, error=str(exc))

        # Fallback: use core SemanticSearch directly
        try:
            from src.core.semantic_search import get_semantic_searcher
            project_root = self._project_root or os.getcwd()
            searcher = get_semantic_searcher(project_root)

            import asyncio
            loop = asyncio.get_event_loop()
            results = await loop.run_in_executor(
                None,
                lambda: searcher.search(query=query, top_k=args.get("top_k", 10))
            )

            formatted = []
            for r in results:
                try:
                    rel = os.path.relpath(r.file_path, project_root)
                except ValueError:
                    rel = r.file_path
                formatted.append({
                    "file_path": r.file_path,
                    "relative_path": rel,
                    "similarity": round(r.similarity, 4),
                    "line_number": r.line_number,
                    "content_snippet": r.content_snippet[:200],
                })

            return ToolResult(tool_id=tool_id, result={
                "query": query,
                "results": formatted,
                "numResults": len(formatted),
                "content": "\n".join(
                    f"[{r['similarity']:.3f}] {r['relative_path']}:{r['line_number']}"
                    for r in formatted
                ),
            })
        except Exception as exc:
            log.warning(f"[BRIDGE] Semantic search fallback failed: {exc}")
            return ToolResult(tool_id=tool_id, result=None, success=False, error=str(exc))

    async def _dispatch_todo_write(self, tool_id: str, args: Dict[str, Any]) -> ToolResult:
        """
        Handle the TodoWrite agent tool.

        Stores the current todo list on the bridge and emits `todos_updated`
        so the UI panel refreshes in real time.
        """
        todos = args.get("todos", [])

        def _normalize_status(raw: Any) -> str:
            s = str(raw or "").strip().lower().replace("-", "_").replace(" ", "_")
            if s in ("complete", "completed", "done"):
                return "COMPLETE"
            if s in ("in_progress", "inprogress", "running", "active"):
                return "IN_PROGRESS"
            if s in ("cancelled", "canceled"):
                return "CANCELLED"
            return "PENDING"

        normalized_todos: List[Dict[str, Any]] = []
        todo_items: List[Any] = cast(List[Any], todos) if isinstance(todos, list) else []
        for t in todo_items:
            if not isinstance(t, dict):
                continue
            t_map = cast(Dict[str, Any], t)
            td: Dict[str, Any] = dict(t_map)
            td["status"] = _normalize_status(td.get("status"))
            
            # ── Validate todo completion ───────────────────────────────
            # If marking as COMPLETE, check if it has mutations
            if td["status"] == "COMPLETE":
                _mutation_count = td.get("_mutation_count", 0)
                _content = td.get("content", "").lower()
                
                # Skip validation for non-implementation tasks
                _is_implementation = not any(
                    _content.startswith(x) for x in 
                    ("test", "verify", "analyze", "read", "review", "check")
                )
                
                if _is_implementation and _mutation_count == 0:
                    # Only block if the session has NO mutations at all.
                    # A single Write/Edit can fulfill multiple tasks, so
                    # per-todo count of 0 is NOT a cheat signal when the
                    # session has real mutations.
                    if self._session_mutation_count == 0:
                        log.warning(
                            f"[TODO] Todo marked COMPLETE without mutations: {td.get('content', '')[:60]}"
                        )
                        # FORCE REVERT: Marking implementation tasks complete without
                        # any mutations is a clear sign the AI is trying to cheat.
                        # Revert to IN_PROGRESS and inject a warning into the todo.
                        td["status"] = "IN_PROGRESS"
                        td["_completion_blocked"] = True
                        if "content" in td:
                            td["content"] = td["content"] + " ⚠️ [BLOCKED: No mutations — do actual work first]"
                    else:
                        # Session has real mutations — per-todo count of 0 is normal
                        # when mutations were credited to other IN_PROGRESS todos.
                        log.info(
                            f"[TODO] Todo COMPLETE with 0 per-todo mutations "
                            f"(session has {self._session_mutation_count} total) — allowing: "
                            f"{td.get('content', '')[:60]}"
                        )
            
            normalized_todos.append(td)

        if self._todo_write_streak >= 2 and self._mutation_success_count == self._last_todo_mutation_count:
            return ToolResult(
                tool_id=tool_id,
                result=None,
                success=False,
                error=(
                    "TodoWrite loop detected: no successful Write/Edit since the previous TodoWrite. "
                    "Perform real implementation actions first, then update todos."
                ),
            )

        if self._todo_write_streak >= 3:
            return ToolResult(
                tool_id=tool_id,
                result=None,
                success=False,
                error=(
                    "TodoWrite loop detected (3+ consecutive TodoWrite calls). "
                    "Do not call TodoWrite again right now; run real tools "
                    "(Read/Edit/Write/Bash) and update todos only after progress."
                ),
            )

        # If every item is completed/cancelled, treat the list as cleared
        all_done = bool(normalized_todos) and all(
            t.get("status") in ("COMPLETE", "CANCELLED") for t in normalized_todos
        )
        
        # ── INDUSTRY-STANDARD: Todo Completion Validation ─────────────────
        # CRITICAL: AI cannot mark todos as complete without actual mutations!
        # Validate that mutations match the number of implementation tasks
        if all_done:
            _implementation_tasks = [
                t for t in normalized_todos 
                if t.get("status") == "COMPLETE"
                and not t.get("content", "").lower().startswith(("test", "verify", "analyze", "read"))
            ]
            _required_mutations = len(_implementation_tasks)
            
            # ── MANDATORY VERIFICATION STEP ─────────────────────
            # Industry-standard: block completion unless tests/verification
            # commands were run and passed successfully.
            _verification_msg = self._build_verification_message()

            if _verification_msg is not None:
                # ── VERIFICATION LOOP BREAKER ──────────────────────
                # If the AI has been blocked N+ times by verification,
                # the AI is clearly trying but the system isn't recognizing
                # its verification commands (e.g., web projects using
                # curl/http.server that don't match test patterns).
                # Allow completion rather than trapping the AI in an
                # infinite verification → TodoWrite → blocked → verify loop.
                #
                # Web/static projects: allow after 1 block (no test framework
                # exists, so the agent cannot possibly satisfy the requirement).
                # Other projects: allow after 2 blocks.
                _block_count = getattr(self, '_verification_block_count', 0) + 1
                self._verification_block_count = _block_count
                _break_threshold = 1 if self._is_web_or_static_project() else 2
                if _block_count >= _break_threshold:
                    log.warning(
                        f"[TODO] Verification blocked {_block_count} times — "
                        f"allowing completion despite unrecognized verification. "
                        f"AI has made {self._session_mutation_count} mutation(s)."
                    )
                    self._verification_block_count = 0
                    # Fall through to allow completion
                else:
                    log.warning("[TODO] AI trying to complete without passing verification.")
                    return ToolResult(
                        tool_id=tool_id,
                        result=None,
                        success=False,
                        error=_verification_msg,
                    )
            
            # If AI claims tasks are done but hasn't made enough mutations, BLOCK IT
            if _required_mutations > 0 and self._session_mutation_count < _required_mutations:
                log.warning(
                    f"[TODO] TodoWrite blocked: AI claims {_required_mutations} tasks complete "
                    + f"but only made {self._session_mutation_count} mutation(s). "
                    + f"Forcing actual implementation!"
                )
                return ToolResult(
                    tool_id=tool_id,
                    result=None,
                    success=False,
                    error=(
                        f"INVALID: You marked {_required_mutations} task(s) as complete, "
                        f"but you've only made {self._session_mutation_count} file change(s). "
                        f"You cannot mark tasks complete without actually doing the work!\n\n"
                        f"Required: At least {_required_mutations} Write/Edit operations\n"
                        f"Your mutations so far: {self._session_mutation_count}\n\n"
                        f"COMPLETE THE ACTUAL WORK FIRST, then update todos."
                    ),
                )
        
        old_todos = list(self._current_todos)
        new_todos = normalized_todos  # keep full list so UI shows completed state briefly

        # Block no-op TodoWrite calls (same ids/status/content repeatedly).
        try:
            _todo_sig = json.dumps(
                [
                    {
                        "id": str(t.get("id", "")),
                        "status": str(t.get("status", "")),
                        "content": str(t.get("content", "")),
                        "activeForm": str(t.get("activeForm", "")),
                    }
                    for t in new_todos
                ],
                sort_keys=True,
            )
        except Exception:
            _todo_sig = ""

        if _todo_sig and _todo_sig == self._last_todo_signature:
            return ToolResult(
                tool_id=tool_id,
                result=None,
                success=False,
                error=(
                    "TodoWrite made no changes compared to previous call. "
                    "Skip TodoWrite and continue with actual implementation actions."
                ),
            )
        self._last_todo_signature = _todo_sig
        self._last_todo_mutation_count = self._mutation_success_count

        # ── CRITICAL FIX: Never clear todos without verification ─────────
        # Even if AI marks all as "complete", we keep them in _current_todos
        # so the exit verification can check them
        # Only clear if:
        # 1. All are complete AND
        # 2. AI has made sufficient mutations AND  
        # 3. At least one verification/test action occurred after last mutation
        
        _should_clear_todos = False
        if all_done:
            _has_verification = getattr(self, '_post_mutation_read_count', 0) == 0
            # Only clear if mutations happened AND some verification occurred
            _should_clear_todos = (self._mutation_success_count > 0 and _has_verification)
            
            if not _should_clear_todos and self._mutation_success_count == 0:
                log.warning(
                    "[TODO] Todos marked complete but NO mutations made. "
                    + "Keeping todos visible - AI is trying to skip work!"
                )
        
        self._current_todos = [] if _should_clear_todos else list(new_todos)

        # Emit to update_todos() in ai_chat.py → window.updateTodos() in JS
        self.todos_updated.emit(new_todos, "")
        
        # ── INDUSTRY-STANDARD: Emit progress update ───────────
        if new_todos:
            _total = len(new_todos)
            _completed = sum(1 for t in new_todos if t.get("status") in ("COMPLETE", "CANCELLED"))
            _pct = int((_completed / _total) * 100) if _total > 0 else 0
            _msg = f"{_completed}/{_total} tasks complete ({_pct}%)" if _total > 0 else "No tasks"
            
            self.task_progress_update.emit(_completed, _total, _pct, _msg)
            log.info(f"[PROGRESS] {_msg}")

        log.info(f"[TODO] TodoWrite dispatched: {len(new_todos)} items, all_done={all_done}")

        return ToolResult(tool_id=tool_id, result={
            "oldTodos": old_todos,
            "newTodos": new_todos,
            "allDone":  all_done,
        })

    def toggle_todo_status(self, task_id: str, completed: bool) -> None:
        """
        Apply a UI todo toggle to bridge state so backend and UI stay in sync.
        """
        if not self._current_todos:
            return

        target_id = str(task_id or "")
        new_status = "COMPLETE" if completed else "PENDING"
        updated = False
        for todo in self._current_todos:
            todo_id = str(todo.get("id", ""))
            if todo_id == target_id:
                todo["status"] = new_status
                updated = True
                break

        if not updated:
            return

        # Keep main prompt state aligned with what the UI currently shows.
        self.todos_updated.emit(list(self._current_todos), "")
        log.info(f"[TODO] Backend sync from UI toggle: {target_id} -> {new_status}")

    def on_answer_question(self, question_id: str, answer: str) -> None:
        """
        Handle the user's answer to a pending question.
        Resolves the asyncio.Future so _dispatch_ask_user_question can resume.
        Called from the Qt main thread via the answer_question_requested signal.
        """
        pending = self._pending_questions.get(question_id)
        if pending:
            future_obj = pending.get("future")
            future: Optional[asyncio.Future[str]] = (
                cast(Optional[asyncio.Future[str]], future_obj)
                if isinstance(future_obj, asyncio.Future)
                else None
            )
            if future is not None and not future.done():
                # Resolve the future from the Qt main thread using
                # call_soon_threadsafe so the worker's asyncio loop picks it up
                # safely without any thread-safety violations.
                worker_loop = getattr(self._worker, "_loop", None)
                if worker_loop and worker_loop.is_running():
                    worker_loop.call_soon_threadsafe(future.set_result, answer)
                    log.info(f"[ASK] Answer routed to agent for question: {question_id}")
                else:
                    log.warning("[ASK] Worker loop not running — cannot resume agent")
            else:
                log.warning(f"[ASK] Future already resolved for question {question_id}")
        else:
            # The question timed out before the user answered (outer 15s wrapper
            # was firing before the user could type). The answer is still valid —
            # inject it as a new user message so it reaches the AI and isn't lost.
            log.warning(
                f"[ASK] Received answer for unknown/expired question ID: {question_id}. "
                f"Injecting answer as a follow-up user message so it is not lost."
            )
            if answer and answer.strip():
                try:
                    self.process_message(
                        f"[Question answer] {answer.strip()}",
                        self._project_root or "",
                    )
                    log.info(f"[ASK] Late answer injected as user message: {answer!r}")
                except Exception as _e:
                    log.warning(f"[ASK] Failed to inject late answer as message: {_e}")

    def _resume_agent_with_answer(self, _pending_question: Dict[str, Any]) -> None:
        """
        Legacy stub — superseded by the asyncio.Future approach in
        _dispatch_ask_user_question / on_answer_question.
        Kept here only to avoid AttributeError if referenced elsewhere.
        """
        question_id = _pending_question.get("id")
        log.warning(
            f"[ASK] _resume_agent_with_answer called for question_id={question_id!r} — this is a no-op; use on_answer_question instead."
        )

    async def _dispatch_ask_user_question(self, tool_id: str, args: Dict[str, Any]) -> ToolResult:
        """
        Handle the AskUserQuestion agent tool.

        Emits the question to the UI via `user_question_requested` signal, then
        suspends the agent turn loop by awaiting an asyncio.Future.  The future
        is resolved (from the Qt main thread) when the user submits an answer.
        """
        questions = args.get("questions", [])

        if not questions:
            return ToolResult(tool_id=tool_id, result=None, success=False,
                              error="AskUserQuestion requires at least one question")

        # Validate questions structure
        for i, q in enumerate(questions):
            if not q.get("question"):
                return ToolResult(tool_id=tool_id, result=None, success=False,
                                  error=f"Question {i+1} missing 'question' field")
            if not q.get("header"):
                return ToolResult(tool_id=tool_id, result=None, success=False,
                                  error=f"Question {i+1} missing 'header' field")
            if not q.get("options"):
                return ToolResult(tool_id=tool_id, result=None, success=False,
                                  error=f"Question {i+1} missing 'options' field")

        # Use first question for the UI card
        first_q = questions[0]
        question_text = first_q.get("question", "")
        options = first_q.get("options", [])
        question_id = first_q.get("id", str(_uuid.uuid4()))

        # Create a Future on the current event loop that will be resolved
        # by on_answer_question() when the user submits their answer.
        loop = asyncio.get_running_loop()
        answer_future: asyncio.Future[str] = loop.create_future()

        # Store pending question state including the future
        self._pending_questions[question_id] = {
            "id": question_id,
            "questions": questions,
            "current_question": first_q,
            "tool_id": tool_id,
            "status": "pending",
            "future": answer_future,
        }

        # Emit to UI via signal — Qt main thread will render the question card
        self.user_question_requested.emit({
            "id": question_id,
            "text": question_text,
            "type": first_q.get("type", "text"),
            "choices": options if options else [],
            "default": first_q.get("default", ""),
            "details": first_q.get("details", ""),
            "scope": first_q.get("scope", "user"),
            "tool_name": "AskUserQuestion"
        })

        log.info(f"[ASK] Agent suspended — waiting for user answer (id={question_id})")

        # Await the future — this suspends the agent turn loop until the user
        # answers (or the task is cancelled / times out after 5 minutes).
        try:
            from src.utils.timeout_strategy import get_ask_question_timeout
            ask_timeout = get_ask_question_timeout()
            answer: str = await asyncio.wait_for(answer_future, timeout=ask_timeout)
        except asyncio.TimeoutError:
            self._pending_questions.pop(question_id, None)
            log.warning(f"[ASK] Question {question_id} timed out after {ask_timeout}s")
            return ToolResult(
                tool_id=tool_id, result=None, success=False,
                error="User did not answer within 5 minutes. Proceeding without answer."
            )
        except asyncio.CancelledError:
            # Task was cancelled (e.g. user sent a new message or stopped generation).
            # Clean up and re-raise so the task cancellation propagates correctly.
            self._pending_questions.pop(question_id, None)
            raise

        # Clean up and return the actual answer as the tool result
        self._pending_questions.pop(question_id, None)
        log.info(f"[ASK] User answered question {question_id!r}: {answer!r}")

        return ToolResult(
            tool_id=tool_id,
            result={
                "answers": {question_text: answer},
                "question_id": question_id,
                "status": "answered",
            },
            success=True
        )

    async def _dispatch_lsp(self, tool_id: str, args: Dict[str, Any]) -> ToolResult:
        """
        Handle the LSP tool.

        Dispatches LSP operations to the LSP manager if available.
        Operations: goToDefinition, findReferences, hover, documentSymbol,
        workspaceSymbol, goToImplementation, call hierarchy.
        """
        operation = args.get("operation", "")
        file_path = args.get("filePath", "")
        line = args.get("line", 1)
        character = args.get("character", 1)

        if not operation:
            return ToolResult(tool_id=tool_id, result=None, success=False,
                              error="LSP requires 'operation' parameter")
        if not file_path:
            return ToolResult(tool_id=tool_id, result=None, success=False,
                              error="LSP requires 'filePath' parameter")

        # Resolve relative paths
        if not os.path.isabs(file_path) and self._project_root:
            file_path = os.path.join(self._project_root, file_path)

        # Try to use the LSP manager if available
        lsp_result = None
        if hasattr(self, '_lsp_manager') and self._lsp_manager:
            try:
                # LSP operations are synchronous in the manager
                if operation == "goToDefinition":
                    lsp_result = self._lsp_manager.go_to_definition(file_path, line, character)
                elif operation == "findReferences":
                    lsp_result = self._lsp_manager.find_references(file_path, line, character)
                elif operation == "hover":
                    lsp_result = self._lsp_manager.get_hover(file_path, line, character)
                elif operation == "documentSymbol":
                    lsp_result = self._lsp_manager.get_document_symbols(file_path)
                elif operation == "workspaceSymbol":
                    lsp_result = self._lsp_manager.get_workspace_symbols(args.get("query", ""))
                elif operation == "goToImplementation":
                    lsp_result = self._lsp_manager.go_to_implementation(file_path, line, character)
            except Exception as exc:
                log.warning(f"[LSP] LSP operation failed: {exc}")
                lsp_result = None

        if lsp_result:
            return ToolResult(tool_id=tool_id, result={
                "operation": operation,
                "file": file_path,
                "position": {"line": line, "character": character},
                "result": lsp_result,
            })

        # Fallback: return guidance for manual navigation
        return ToolResult(tool_id=tool_id, result={
            "operation": operation,
            "file": file_path,
            "position": {"line": line, "character": character},
            "result": None,
            "message": (
                f"LSP operation '{operation}' at {file_path}:{line}:{character}. "
                f"LSP server may not be running for this file type. "
                f"Use Grep or Read tools to search for definitions/references manually."
            ),
        })

    async def _dispatch_web_fetch(self, tool_id: str, args: Dict[str, Any]) -> ToolResult:
        """
        Handle the WebFetch tool.

        Fetches content from a URL and extracts the main readable content
        using BeautifulSoup (preferred) or regex (fallback).
        """
        url = args.get("url", "")
        query = args.get("query", "")

        if not url:
            return ToolResult(tool_id=tool_id, result=None, success=False,
                              error="WebFetch requires 'url' parameter")

        # Ensure URL has scheme
        if not url.startswith(('http://', 'https://')):
            url = 'https://' + url

        # Try to import and use the real WebFetchTool utils
        try:
            if _importlib_util.find_spec("agent.src.tools.WebFetchTool.utils"):
                try:
                    from agent.src.tools.WebFetchTool.utils import get_url_markdown_content
                    content = await get_url_markdown_content(url)
                    return ToolResult(tool_id=tool_id, result={
                        "url": url,
                        "content": content[:50000] if content else "",
                        "query": query,
                    })
                except Exception as exc:
                    log.warning(f"[WebFetch] WebFetchTool.utils failed: {exc} — falling back")
        except (ModuleNotFoundError, ImportError) as exc:
            log.warning(f"[WebFetch] find_spec failed ({exc}) — using built-in extraction")

        # Fallback: BeautifulSoup-based extraction (preferred)
        import re
        text = ""
        try:
            req = urllib.request.Request(url, headers={
                'User-Agent': (
                    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                    'AppleWebKit/537.36 (KHTML, like Gecko) '
                    'Chrome/120.0.0.0 Safari/537.36'
                ),
                'Accept': 'text/html,application/xhtml+xml',
                'Accept-Language': 'en-US,en;q=0.9',
            })
            with urllib.request.urlopen(req, timeout=30) as response:
                html = response.read().decode('utf-8', errors='replace')

            # Try BeautifulSoup for smart content extraction
            try:
                from bs4 import BeautifulSoup
                soup = BeautifulSoup(html, 'lxml')

                # Remove non-content elements
                for tag in soup.find_all(['script', 'style', 'nav', 'footer', 'header', 'aside',
                                           'noscript', 'iframe', 'form', 'button', 'input']):
                    tag.decompose()

                # Remove common non-content CSS classes
                for cls in ['sidebar', 'sidebar-nav', 'navigation', 'nav', 'footer', 'header',
                            'advertisement', 'ad-', 'cookie', 'popup', 'modal', 'comment']:
                    for tag in soup.find_all(class_=re.compile(cls, re.IGNORECASE)):
                        tag.decompose()

                # Try to find main content
                main = soup.find('main') or soup.find('article') or soup.find(id='content') or soup.find(id='main')
                if main:
                    text = main.get_text(separator='\n', strip=True)
                else:
                    # Extract from body without nav/footer/header
                    body = soup.find('body')
                    if body:
                        text = body.get_text(separator='\n', strip=True)

                # Clean up whitespace
                text = re.sub(r'\n{3,}', '\n\n', text)
                text = re.sub(r' +', ' ', text)
                text = text.strip()
            except ImportError:
                # Regex fallback if BeautifulSoup unavailable
                text = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.DOTALL | re.IGNORECASE)
                text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL | re.IGNORECASE)
                text = re.sub(r'<[^>]+>', ' ', text)
                text = re.sub(r'\s+', ' ', text).strip()
                log.info("[WebFetch] BeautifulSoup not available — using regex extraction")

            if not text:
                text = "No readable text content found on this page."

            return ToolResult(tool_id=tool_id, result={
                "url": url,
                "content": text[:50000],
                "query": query,
            })
        except urllib.error.URLError as exc:
            return ToolResult(tool_id=tool_id, result=None, success=False,
                              error=f"Failed to fetch URL: {exc}")
        except Exception as exc:
            return ToolResult(tool_id=tool_id, result=None, success=False,
                              error=f"WebFetch error: {exc}")

    async def _dispatch_web_search(self, tool_id: str, args: Dict[str, Any]) -> ToolResult:
        """
        Handle the WebSearch tool.

        Tier 0: Subscription proxy — SerpAPI via Django server (subscription users).
        Tier 1: MiMo native web_search — best quality, uses MiMo's built-in search tool.
        Tier 2: SerpAPI — Google search results (100 free/month, requires SERPAPI_API_KEY).
        Tier 3: DuckDuckGo HTML search scraping (free, no API key, real web results).
        Tier 4: DuckDuckGo Instant Answer API fallback (free, limited results).
        Tier 5: Brave Search API (free tier 2K queries/month, requires BRAVE_API_KEY).
        """
        import asyncio
        import json
        import urllib.parse
        query = args.get("query", "")

        if not query:
            return ToolResult(tool_id=tool_id, result=None, success=False,
                              error="WebSearch requires 'query' parameter")

        results: List[Dict[str, str]] = []
        error_msgs: List[str] = []

        # ── Tier 0: Subscription proxy (SerpAPI via Django server) ──────
        try:
            from src.core.cortex_api import get_api_client
            api = get_api_client()
            if api.has_subscription():
                proxy_result = api.proxy_service("web_search", query=query)
                if proxy_result and proxy_result.get("status") == "success":
                    results = proxy_result.get("results", [])
                    if results:
                        log.info(f"[WebSearch] Subscription proxy returned {len(results)} results for '{query}'")
                        # Track web search usage
                        try:
                            from src.ai.usage_tracker import get_usage_tracker
                            get_usage_tracker().record_web_searches(1)
                        except Exception:
                            pass
                        return ToolResult(
                            tool_id=tool_id,
                            result=json.dumps({"results": results, "query": query, "source": "serpapi_proxy"}),
                            success=True,
                        )
        except Exception as e:
            log.debug(f"[WebSearch] Subscription proxy failed: {e}")

        # ── Tier 1: MiMo Native Web Search (built-in web_search tool) ──────
        from src.core.key_manager import KeyManager
        km = KeyManager()
        mimo_key = km.get_key("mimo") or ""
        if mimo_key:
            try:
                from src.ai.providers.mimo_provider import get_mimo_provider
                mimo = get_mimo_provider()
                mimo_results = mimo.web_search(query)
                for r in mimo_results:
                    results.append({
                        'title': r.get('title', ''),
                        'url': r.get('url', ''),
                        'snippet': r.get('snippet', '')[:300],
                    })
                if results:
                    log.info(f"[WebSearch] MiMo returned {len(results)} results for '{query}'")
            except Exception as mimo_exc:
                msg = f"MiMo: {mimo_exc}"
                log.warning(f"[WebSearch] {msg} — falling through to SerpAPI")
                error_msgs.append(msg)
                await asyncio.sleep(0.5)  # Backoff before next tier
        else:
            log.debug(
                "[WebSearch] MiMo API key not set (MIMO_API_KEY). "
                "Falling through to SerpAPI / DuckDuckGo."
            )

        # ── Tier 1: SerpAPI — Google search (best quality) ─────────────────
        serpapi_key = km.get_key("serpapi") or ""
        if serpapi_key:
            try:
                encoded = urllib.parse.quote(query)
                serpapi_url = (
                    f'https://serpapi.com/search'
                    f'?q={encoded}&api_key={serpapi_key}&engine=google'
                    f'&num=10&gl=us&hl=en'
                )
                req = urllib.request.Request(
                    serpapi_url,
                    headers={'User-Agent': 'Cortex-IDE/1.0 (web-search)'}
                )
                with urllib.request.urlopen(req, timeout=12) as resp:
                    data = json.loads(resp.read().decode('utf-8'))

                # Organic results
                for r in (data.get('organic_results') or [])[:10]:
                    results.append({
                        'title': r.get('title', ''),
                        'url': r.get('link', ''),
                        'snippet': (r.get('snippet', '') or '')[:300],
                    })

                # Knowledge Graph / Answer Box (if any)
                kg = data.get('knowledge_graph') or {}
                if kg:
                    kg_title = kg.get('title', '')
                    kg_desc = kg.get('description', '')
                    kg_url = kg.get('source', {}).get('link', '') if isinstance(kg.get('source'), dict) else ''
                    if kg_title and kg_desc:
                        results.append({
                            'title': f'[Knowledge Graph] {kg_title}: {kg_desc[:200]}',
                            'url': kg_url,
                            'snippet': kg_desc[:300],
                        })

                # Featured snippet
                answer_box = data.get('answer_box') or {}
                if answer_box and not results:
                    ab_title = answer_box.get('title', '')
                    ab_answer = answer_box.get('answer', '') or answer_box.get('snippet', '')
                    ab_url = answer_box.get('link', '')
                    if ab_answer:
                        results.append({
                            'title': ab_title or 'Answer',
                            'url': ab_url,
                            'snippet': str(ab_answer)[:300],
                        })

                if results:
                    log.info(f"[WebSearch] SerpAPI returned {len(results)} results for '{query}'")
            except Exception as serp_exc:
                msg = f"SerpAPI: {serp_exc}"
                log.warning(f"[WebSearch] {msg} — falling through to DuckDuckGo")
                error_msgs.append(msg)
                await asyncio.sleep(1.0)  # Backoff before next tier
        else:
            log.info(
                "[WebSearch] SerpAPI key not set (free: https://serpapi.com/users/welcome). "
                "Falling through to DuckDuckGo HTML search."
            )

        # ── Tier 2: DuckDuckGo HTML search (real web results) ─────────────
        if not results:
            try:
                encoded = urllib.parse.quote(query)
                # DuckDuckGo HTML endpoint — returns plain HTML with real search results
                ddg_html_url = f'https://html.duckduckgo.com/html/?q={encoded}'
                req = urllib.request.Request(
                    ddg_html_url,
                    headers={
                        'User-Agent': (
                            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                            'AppleWebKit/537.36 (KHTML, like Gecko) '
                            'Chrome/120.0.0.0 Safari/537.36'
                        ),
                        'Accept': 'text/html,application/xhtml+xml',
                        'Accept-Language': 'en-US,en;q=0.9',
                    }
                )
                with urllib.request.urlopen(req, timeout=12) as resp:
                    html = resp.read().decode('utf-8', errors='replace')

                # Parse with BeautifulSoup if available, otherwise regex
                try:
                    from bs4 import BeautifulSoup
                    soup = BeautifulSoup(html, 'lxml')
                    result_blocks = soup.select('.result, .result__body, .web-result')
                    if not result_blocks:
                        # Try older DDG HTML layout
                        result_blocks = soup.select('.result__body') or soup.select('.results_links')
                        if not result_blocks:
                            # Fall back to link-level extraction
                            links = soup.select('a.result__a, a.result__url')
                            snippets = soup.select('.result__snippet, .result__extract__snippet')
                            # Pair links with snippets by position
                            for i, link in enumerate(links[:10]):
                                href = link.get('href', '')
                                if href and not href.startswith('//duckduckgo.com'):
                                    title = link.get_text(strip=True)
                                    snippet = snippets[i].get_text(strip=True) if i < len(snippets) else ''
                                    results.append({
                                        'title': title[:200],
                                        'url': href,
                                        'snippet': snippet[:300],
                                    })
                    else:
                        for block in result_blocks[:10]:
                            link_elem = block.select_one('a.result__a') or block.select_one('a')
                            snippet_elem = (
                                block.select_one('.result__snippet') or
                                block.select_one('.result__extract__snippet') or
                                block.select_one('.snippet')
                            )
                            if link_elem:
                                href = link_elem.get('href', '')
                                if href and 'duckduckgo.com' not in href:
                                    title = link_elem.get_text(strip=True)
                                    snippet = snippet_elem.get_text(strip=True) if snippet_elem else ''
                                    results.append({
                                        'title': title[:200],
                                        'url': href,
                                        'snippet': snippet[:300],
                                    })
                except ImportError:
                    # Fallback: regex-based extraction
                    import re
                    # Match DuckDuckGo HTML result links and snippets
                    link_pattern = re.compile(
                        r'<a[^>]*class="[^"]*result__a[^"]*"[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
                        re.DOTALL | re.IGNORECASE
                    )
                    snippet_pattern = re.compile(
                        r'<[^>]*class="[^"]*result__snippet[^"]*"[^>]*>(.*?)</',
                        re.DOTALL | re.IGNORECASE
                    )
                    found_links = link_pattern.findall(html)
                    found_snippets = snippet_pattern.findall(html)
                    for i, (href, title) in enumerate(found_links[:10]):
                        title_clean = re.sub(r'<[^>]+>', '', title).strip()
                        if title_clean and 'duckduckgo.com' not in href:
                            snippet = ''
                            if i < len(found_snippets):
                                snippet = re.sub(r'<[^>]+>', '', found_snippets[i]).strip()
                            results.append({
                                'title': title_clean[:200],
                                'url': href,
                                'snippet': snippet[:300],
                            })

                if results:
                    log.info(f"[WebSearch] DuckDuckGo HTML returned {len(results)} results for '{query}'")
            except Exception as ddg_exc:
                msg = f"DuckDuckGo HTML: {ddg_exc}"
                log.warning(f"[WebSearch] {msg} — trying Instant Answer API fallback")
                error_msgs.append(msg)
                await asyncio.sleep(1.5)  # Backoff before next tier

        # ── Tier 3: DuckDuckGo Instant Answer API (free, no key) ──────────
        if not results:
            try:
                encoded = urllib.parse.quote(query)
                ddg_url = (
                    f'https://api.duckduckgo.com/?q={encoded}'
                    '&format=json&no_html=1&skip_disambig=1'
                )
                req = urllib.request.Request(
                    ddg_url,
                    headers={'User-Agent': 'Cortex-IDE/1.0 (web-search)'}
                )
                with urllib.request.urlopen(req, timeout=12) as resp:
                    data = json.loads(resp.read().decode('utf-8'))

                # Abstract (plain text summary)
                abstract = (data.get('Abstract') or '').strip()
                if abstract:
                    source_url = data.get('AbstractURL', '')
                    source_name = data.get('AbstractSource', '')
                    results.append({
                        'title': f'{source_name}: {abstract}' if source_name else abstract,
                        'url': source_url,
                        'snippet': abstract[:300],
                    })

                # Related topics
                for topic in (data.get('RelatedTopics') or [])[:10]:
                    if not isinstance(topic, dict):
                        continue
                    url = topic.get('FirstURL', '')
                    text = topic.get('Text', '')
                    if url and text:
                        results.append({
                            'title': text[:120],
                            'url': url,
                            'snippet': text[:300],
                        })

                if results:
                    log.info(f"[WebSearch] DuckDuckGo API returned {len(results)} results for '{query}'")
            except Exception as ddg_exc:
                msg = f"DuckDuckGo API: {ddg_exc}"
                log.warning(f"[WebSearch] {msg} — trying Brave fallback")
                error_msgs.append(msg)
                await asyncio.sleep(2.0)  # Backoff before last tier

        # ── Tier 4: Brave Search API (free tier, 2,000 queries/month) ────
        if not results:
            brave_key = km.get_key("brave") or ""
            if brave_key:
                try:
                    encoded = urllib.parse.quote(query)
                    brave_url = f'https://api.search.brave.com/res/v1/web/search?q={encoded}&count=10'
                    req = urllib.request.Request(
                        brave_url,
                        headers={
                            'Accept': 'application/json',
                            'Accept-Encoding': 'gzip',
                            'X-Subscription-Token': brave_key,
                            'User-Agent': 'Cortex-IDE/1.0 (web-search)',
                        }
                    )
                    with urllib.request.urlopen(req, timeout=12) as resp:
                        data = json.loads(resp.read().decode('utf-8'))

                    web_results = data.get('web', {}).get('results', [])
                    for r in web_results[:10]:
                        results.append({
                            'title': r.get('title', ''),
                            'url': r.get('url', ''),
                            'snippet': (r.get('description', '') or '')[:300],
                        })

                    if results:
                        log.info(f"[WebSearch] Brave returned {len(results)} results for '{query}'")
                except Exception as brave_exc:
                    msg = f"Brave: {brave_exc}"
                    log.warning(f"[WebSearch] {msg}")
                    error_msgs.append(msg)
            else:
                log.info(
                    "[WebSearch] Brave API key not set. "
                    "Set BRAVE_API_KEY env var for fallback (free: https://brave.com/search/api/)."
                )

        # ── No results — return guidance ──────────────────────────────────
        if not results:
            error_detail = "; ".join(error_msgs) if error_msgs else ""
            return ToolResult(tool_id=tool_id, result={
                "query": query,
                "results": [],
                "message": (
                    f"Web search for '{query}' returned no results. "
                    + (f"Errors: {error_detail}. " if error_detail else "")
                    + "Try a more specific query, or use WebFetch with a direct URL."
                ),
            })

        # ── Build formatted output ────────────────────────────────────────
        formatted = f'Web search results for: "{query}"\n\n'
        for idx, r in enumerate(results, 1):
            formatted += f"{idx}. **{r['title']}**\n"
            formatted += f"   URL: {r['url']}\n"
            if r.get('snippet'):
                formatted += f"   {r['snippet']}\n"
            formatted += "\n"
        formatted += "\nREMINDER: Include sources above in your response using markdown hyperlinks."

        # Track web search usage
        try:
            from src.ai.usage_tracker import get_usage_tracker
            get_usage_tracker().record_web_searches(1)
        except Exception:
            pass

        return ToolResult(tool_id=tool_id, result={
            "query": query,
            "results": results,
            "formatted": formatted[:50000],
        })

    # ============================================================
    # TASK V2 DISPATCHERS
    # ============================================================

    async def _dispatch_task_create(self, tool_id: str, args: Dict[str, Any]) -> ToolResult:
        """
        Handle TaskCreate tool - create a new structured task.
        Supports hierarchical fields: parentId, dependsOn, estimatedEffort.
        """
        subject = args.get("subject", "")
        description = args.get("description", "")
        active_form = args.get("activeForm", "")

        if not subject:
            return ToolResult(tool_id=tool_id, result=None, success=False,
                              error="TaskCreate requires 'subject' parameter")
        if not description:
            return ToolResult(tool_id=tool_id, result=None, success=False,
                              error="TaskCreate requires 'description' parameter")

        # Generate task ID
        import uuid
        task_id = f"task-{uuid.uuid4().hex[:8]}"

        # Hierarchical fields
        parent_id = args.get("parentId")
        depends_on = args.get("dependsOn", [])
        estimated_effort = args.get("estimatedEffort")

        # Prevent circular dependencies before creating
        if depends_on and task_id in depends_on:
            return ToolResult(tool_id=tool_id, result=None, success=False,
                              error="Task cannot depend on itself")

        # Store task in session
        task: Dict[str, Any] = {
            "id": task_id,
            "subject": subject,
            "description": description,
            "activeForm": active_form or f"Working on: {subject}",
            "status": "pending",
            "owner": None,
            "blocks": [],
            "blockedBy": list(depends_on),
            "parentId": parent_id,
            "dependsOn": list(depends_on),
            "estimatedEffort": estimated_effort,
            "tags": [],
            "createdAt": _get_current_timestamp(),
        }

        # Add to session task list
        self._session_tasks[task_id] = task

        # Sync with hierarchical task graph
        node = TaskNode(
            id=task_id,
            subject=subject,
            description=description,
            status=TaskStatus.PENDING,
            active_form=active_form or f"Working on: {subject}",
            parent_id=parent_id,
            depends_on=list(depends_on),
            estimated_effort=estimated_effort,
        )
        self._task_graph.add_node(node)

        # Emit event
        try:
            from src.core.event_bus import get_event_bus, EventType, EventData
            bus = get_event_bus()
            bus.publish(EventType.TASK_GRAPH_UPDATED, EventData(source_component="agent_bridge"))
        except Exception:
            pass

        log.info(f"[TASK] Created task {task_id}: {subject}" +
                 f"{' parent=' + parent_id if parent_id else ''}" +
                 f"{' deps=' + str(depends_on) if depends_on else ''}")

        return ToolResult(tool_id=tool_id, result={
            "taskId": task_id,
            "task": task,
            "message": f"Task '{subject}' created with ID {task_id}"
        })

    async def _dispatch_task_update(self, tool_id: str, args: Dict[str, Any]) -> ToolResult:
        """
        Handle TaskUpdate tool - update task status, owner, or dependencies.
        Also syncs with hierarchical task graph.
        """
        task_id = args.get("taskId", "")
        status = args.get("status")
        owner = args.get("owner")
        blocks = args.get("blocks")
        blocked_by = args.get("blockedBy")
        parent_id = args.get("parentId")  # "" means clear parent
        depends_on = args.get("dependsOn")
        estimated_effort = args.get("estimatedEffort")

        if not task_id:
            return ToolResult(tool_id=tool_id, result=None, success=False,
                              error="TaskUpdate requires 'taskId' parameter")

        # Get task from session
        if task_id not in self._session_tasks:
            return ToolResult(tool_id=tool_id, result=None, success=False,
                              error=f"Task {task_id} not found")

        task = self._session_tasks[task_id]

        # Update flat task dict
        if status:
            task["status"] = status
        if owner is not None:
            task["owner"] = owner
        if blocks is not None:
            task["blocks"] = blocks
        if blocked_by is not None:
            task["blockedBy"] = blocked_by
        if parent_id is not None:
            task["parentId"] = parent_id if parent_id else None
        if depends_on is not None:
            task["dependsOn"] = list(depends_on)
        if estimated_effort is not None:
            task["estimatedEffort"] = estimated_effort
        task["updatedAt"] = _get_current_timestamp()

        # Sync with hierarchical task graph
        if self._task_graph.has_node(task_id):
            updates: Dict[str, Any] = {}
            if status:
                updates["status"] = TaskStatus.from_str(status)
            if owner is not None:
                updates["owner"] = owner
            if parent_id is not None:
                updates["parent_id"] = parent_id if parent_id else None
            if depends_on is not None:
                updates["depends_on"] = list(depends_on)
            if estimated_effort is not None:
                updates["estimated_effort"] = estimated_effort
            if updates:
                self._task_graph.update_node(task_id, **updates)

        # Emit event
        try:
            from src.core.event_bus import get_event_bus, EventType, EventData
            bus = get_event_bus()
            bus.publish(EventType.TASK_GRAPH_UPDATED, EventData(source_component="agent_bridge"))
        except Exception:
            pass

        log.info(f"[TASK] Updated task {task_id}: status={status or 'unchanged'}")

        return ToolResult(tool_id=tool_id, result={
            "taskId": task_id,
            "task": task,
            "message": f"Task {task_id} updated"
        })

    async def _dispatch_task_list(self, tool_id: str, args: Dict[str, Any]) -> ToolResult:
        """
        Handle TaskList tool - list all tasks in session.
        """
        status_filter = args.get("status", "all")

        tasks = list(self._session_tasks.values())

        if status_filter != "all":
            tasks = [t for t in tasks if t.get("status") == status_filter]

        log.info(f"[TASK] Listed {len(tasks)} tasks (filter={status_filter})")

        # Include task graph summary
        graph_section = self._task_graph.build_prompt_section() if self._task_graph else ""

        return ToolResult(tool_id=tool_id, result={
            "tasks": tasks,
            "count": len(tasks),
            "filter": status_filter,
            "graph": graph_section,
        })

    async def _dispatch_task_get(self, tool_id: str, args: Dict[str, Any]) -> ToolResult:
        """
        Handle TaskGet tool - get details of a specific task.
        """
        task_id = args.get("taskId", "")

        if not task_id:
            return ToolResult(tool_id=tool_id, result=None, success=False,
                              error="TaskGet requires 'taskId' parameter")

        if task_id not in self._session_tasks:
            return ToolResult(tool_id=tool_id, result=None, success=False,
                              error=f"Task {task_id} not found")

        task = self._session_tasks[task_id]

        # Include hierarchical context
        children = []
        rollup = {}
        if self._task_graph and self._task_graph.has_node(task_id):
            child_nodes = self._task_graph.get_direct_children(task_id)
            children = [{"id": c.id, "subject": c.subject, "status": c.status.value}
                        for c in child_nodes]
            rollup = self._task_graph.get_rollup_status(task_id)

        return ToolResult(tool_id=tool_id, result={
            "task": task,
            "children": children,
            "rollup": rollup,
        })

    async def _dispatch_task_stop(self, tool_id: str, args: Dict[str, Any]) -> ToolResult:
        """
        Handle TaskStop tool - stop a running task.
        """
        task_id = args.get("taskId", "")

        if not task_id:
            return ToolResult(tool_id=tool_id, result=None, success=False,
                              error="TaskStop requires 'taskId' parameter")

        if task_id not in self._session_tasks:
            return ToolResult(tool_id=tool_id, result=None, success=False,
                              error=f"Task {task_id} not found")

        task = self._session_tasks[task_id]
        task["status"] = "cancelled"
        task["stoppedAt"] = _get_current_timestamp()

        log.info(f"[TASK] Stopped task {task_id}")

        return ToolResult(tool_id=tool_id, result={
            "taskId": task_id,
            "status": "cancelled",
            "message": f"Task {task_id} stopped"
        })

    # ============================================================
    # MCP DISPATCHER
    # ============================================================

    async def _dispatch_mcp(self, tool_id: str, args: Dict[str, Any]) -> ToolResult:
        """
        Handle MCP tool - execute a tool from an MCP server.
        """
        server_name = args.get("serverName", "")
        tool_name = args.get("toolName", "")
        arguments = args.get("arguments", {})

        if not server_name:
            return ToolResult(tool_id=tool_id, result=None, success=False,
                              error="MCP requires 'serverName' parameter")
        if not tool_name:
            return ToolResult(tool_id=tool_id, result=None, success=False,
                              error="MCP requires 'toolName' parameter")

        log.info(f"[MCP] Tool call: {server_name}.{tool_name}")

        return ToolResult(tool_id=tool_id, result={
            "serverName": server_name,
            "toolName": tool_name,
            "arguments": arguments,
            "result": None,
            "message": (
                f"MCP tool '{tool_name}' on server '{server_name}'. "
                f"MCP servers need to be configured in settings. "
                f"Use the built-in tools (Read, Write, Bash, etc.) for file and command operations."
            )
        })

    # ============================================================
    # TEAM/SWARM DISPATCHERS
    # ============================================================

    async def _dispatch_team_create(self, tool_id: str, args: Dict[str, Any]) -> ToolResult:
        """
        Handle TeamCreate tool - create a multi-agent team.
        """
        name = args.get("name", "")
        description = args.get("description", "")
        teammates = args.get("teammates", [])

        if not name:
            return ToolResult(tool_id=tool_id, result=None, success=False,
                              error="TeamCreate requires 'name' parameter")

        # Generate team ID
        import uuid
        team_id = f"team-{uuid.uuid4().hex[:8]}"

        # Create team structure
        team: Dict[str, Any] = {
            "id": team_id,
            "name": name,
            "description": description,
            "teammates": [],
            "status": "active",
            "createdAt": _get_current_timestamp(),
        }

        # Add teammates
        for i, tm in enumerate(teammates):
            teammate_id = f"agent-{uuid.uuid4().hex[:6]}"
            team["teammates"].append({
                "id": teammate_id,
                "name": tm.get("name", f"agent-{i+1}"),
                "role": tm.get("role", "general"),
                "status": "idle",
            })

        # Store team
        self._teams[team_id] = team

        log.info(f"[TEAM] Created team {team_id}: {name} with {len(teammates)} teammates")

        return ToolResult(tool_id=tool_id, result={
            "teamId": team_id,
            "team": team,
            "message": f"Team '{name}' created with {len(teammates)} agents"
        })

    async def _dispatch_team_delete(self, tool_id: str, args: Dict[str, Any]) -> ToolResult:
        """
        Handle TeamDelete tool - delete a team.
        """
        team_name = args.get("teamName", "")

        if not team_name:
            return ToolResult(tool_id=tool_id, result=None, success=False,
                              error="TeamDelete requires 'teamName' parameter")

        # Find team by name
        for tid, team in list(self._teams.items()):
            if team.get("name") == team_name:
                del self._teams[tid]
                log.info(f"[TEAM] Deleted team {tid}: {team_name}")
                return ToolResult(tool_id=tool_id, result={
                    "teamId": tid,
                    "message": f"Team '{team_name}' deleted"
                })

        return ToolResult(tool_id=tool_id, result=None, success=False,
                          error=f"Team '{team_name}' not found")

    async def _dispatch_plan_build(self, tool_id: str, args: Dict[str, Any]) -> ToolResult:
        """
        Handle PlanBuild tool — create a structured plan .md file and return plan data.
        Delegates to the PlanBuild tool in src/agent/src/tools/PlanBuildTool/.
        """
        try:
            import importlib
            module = importlib.import_module('src.agent.src.tools.PlanBuildTool.PlanBuildTool')
            plan_result = await module.call(args, None)
            if isinstance(plan_result, dict) and 'data' in plan_result:
                return ToolResult(tool_id=tool_id, result=plan_result['data'])
            return ToolResult(tool_id=tool_id, result=plan_result)
        except Exception as e:
            log.error(f"[PLANBUILD] Dispatch failed: {e}")
            return ToolResult(tool_id=tool_id, result=None, success=False, error=str(e))

    # ── Worker signal handlers ─────────────────────────────────

    def _on_response_ready(self, response: str):
        try:
            # Strip NULL bytes — some LLMs embed \x00 in responses
            if response and '\x00' in response:
                response = response.replace('\x00', '')
                log.warning("[BRIDGE] Stripped NULL bytes from AI response")
            self._agentic_turn_active = False  # AI done — allow warmup to open files
            log.info(f"[BRIDGE] _on_response_ready: response={repr(response)[:120]}, len={len(response) if response else 0}")
            self.response_complete.emit(response)
            log.info("[BRIDGE] _on_response_ready: response_complete.emit() DONE")
            if self._streaming:
                try:
                    self._streaming.emit_llm_complete(response)
                except Exception:
                    pass
            # Save assistant turn to history — PRESERVE reasoning_content
            # for MiMo/DeepSeek thinking mode. Without this, the next
            # request to MiMo returns HTTP 400: "reasoning_content must be
            # passed back to the API."
            _cm = ChatMessage(role="assistant", content=response)
            if self._last_turn_reasoning:
                _cm.reasoning_content = self._last_turn_reasoning
                self._last_turn_reasoning = ""  # Clear after use
            with self._history_lock:
                self._conversation_history.append(_cm)

            # ── IMMEDIATE PERSISTENCE: Save assistant message to DB ────────
            # Save instantly so it survives ANY crash.
            try:
                self._save_message_immediate('assistant', response or '')
            except Exception:
                pass

            # ── IMMEDIATE PERSISTENCE: Save chat to DB after every response ──
            # This ensures chats survive ANY crash (Chromium crash, power loss, etc.)
            # The save is non-blocking and fire-and-forget.
            try:
                self._persist_chat_async()
            except Exception:
                pass  # Never let persistence failure break the response flow

            # ── PROACTIVE CONTEXT WINDOW MONITORING ──────────────────────
            # Check if we're approaching the context limit and trigger compact
            try:
                self._check_context_window_pressure()
            except Exception:
                pass
        except Exception as exc:
            log.error(
                "[BRIDGE] _on_response_ready CRASH: %s\nFULL TRACEBACK:",
                exc, exc_info=True
            )

    def inject_image(self, base64_data: str, description: str = "Image"):
        """Store a pasted image for inclusion in the next LLM request.
        
        The image is stored as base64 and injected into the conversation
        as a vision message when the next chat() call is made.
        """
        if not hasattr(self, '_pending_images'):
            self._pending_images = []
        self._pending_images.append({
            "data": base64_data,
            "description": description,
        })
        log.info(f"[BRIDGE] Image stored for next request: {description} ({len(base64_data)} chars base64)")

    def _current_model_supports_vision(self) -> bool:
        """Check if the currently selected model supports image/vision input."""
        model_id = getattr(self, '_current_model_id', None)
        if not model_id:
            return False
        try:
            from src.ai.providers import get_provider_registry
            registry = get_provider_registry()
            provider = registry.get_current_provider()
            if provider:
                for mi in provider.available_models:
                    if mi.id == model_id:
                        return mi.supports_vision
        except Exception:
            pass
        return False

    def inject_vision_history(self, user_text: str, assistant_response: str):
        """Inject a vision exchange into conversation history.
        
        Called when vision processing completes outside the normal agent flow.
        This ensures follow-up text messages have context about what was in images.
        
        IMPORTANT: Truncate the vision response to avoid eating the entire hist_cap.
        The full response is already displayed in the UI; we only need a summary
        in history so the model knows what was discussed.
        """
        _MAX_VISION_HIST = 3000  # Max chars for vision response in history
        
        if len(assistant_response) > _MAX_VISION_HIST:
            truncated = assistant_response[:_MAX_VISION_HIST]
            assistant_response = (
                truncated + 
                f"\n\n[... vision analysis truncated from {len(assistant_response)} to {_MAX_VISION_HIST} chars for history context]"
            )
        
        log.info(f"[BRIDGE] Injecting vision exchange into history: user={len(user_text)} chars, assistant={len(assistant_response)} chars")
        with self._history_lock:
            self._conversation_history.append(
                ChatMessage(role="user", content=user_text)
            )
            self._conversation_history.append(
                ChatMessage(role="assistant", content=assistant_response)
            )

    def _on_chunk_ready(self, chunk: str):
        # Route through filtering layer to strip thought/reasoning content
        self._on_filtered_token(chunk)

    def _on_error(self, error: str):
        self.request_error.emit(error)
    
    def _is_simple_query(self, text: str) -> bool:
        """Check if query is a pure greeting/ack that needs no tools.
            
        CONSERVATIVE: Only skip tools for pure social messages (hi, thanks, bye)
        and capability questions about the AI itself.
        Any message that MIGHT involve coding, files, or project work MUST get
        the full agentic loop with tools enabled.
            
        Returns:
            True if pure greeting/ack (skip tools), False otherwise (load tools)
        """
        import re
        text_lower = text.strip().lower()
            
        # Only exact greetings and social messages skip tools
        greeting_patterns = [
            r'^(hi|hello|hey|yo|sup|greetings)[!.s]*$',
            r'^(thanks?|thank you|thx)[!.s]*$',
            r'^(ok|okay|got it|sure|alright)[!.s]*$',
            r'^(bye|goodbye|see you|good night)[!.s]*$',
            r'^(good (morning|afternoon|evening))[!.s]*$',
            r'^how are you[?!.s]*$',
            r'^what\'?s up[?!.s]*$',
        ]
            
        for pattern in greeting_patterns:
            if re.match(pattern, text_lower):
                return True
            
        # Capability questions about the AI itself (no tools needed)
        # These are meta-questions, not coding tasks
        capability_patterns = [
            r'^what (can|do) you do',
            r'^what (are|r) your capabilities',
            r'^what can you (help |assist )?(me )?with',
            r'^what you can (do|help)',
            r'^(who|what) are you',
            r'^(how (do|can) )?you (work|help|assist|operate)',
            r'^tell me about yourself',
        ]
            
        for pattern in capability_patterns:
            if re.match(pattern, text_lower):
                return True
            
        # Everything else gets full agentic capabilities
        return False

    def _is_greeting(self, text: str) -> bool:
        """Return True for pure greeting/ack messages.

        Used to bypass the LLM entirely for instant UX on trivial inputs.
        """
        t = (text or "").strip().lower()
        if not t:
            return False
        # Normalize common punctuation
        t = t.replace("!", "").replace(".", "").replace(",", "").strip()

        greetings = {
            "hi", "hello", "hey", "hiya", "yo",
            "hi there", "hello there", "hey there",
            "good morning", "good afternoon", "good evening",
        }
        acks = {"thanks", "thank you", "thx", "ty"}
        byes = {"bye", "goodbye", "see you", "cya"}
        return t in greetings or t in acks or t in byes

    # ============================================================
    # PUBLIC INTERFACE (matching StubAIAgent so ai_chat.py works)
    # ============================================================

    def process_message(self, message: str, images: Optional[List[str]] = None):
        """Entry point: called by ai_chat.py when the user sends a message."""
        _was_stopped = self._stop_requested  # Save before reset for history cleanup
        self._stop_requested = False  # Clear any previous stop before handling new request
        # Fresh AbortController so tools from the previous (aborted) request can't
        # accidentally cancel this new one.
        self._tool_ctx.abort_controller = create_abort_controller()

        # Reset tool safety counters for genuinely new requests (not Continue).
        _is_continue = message.strip().startswith('Continue the task.')
        if not _is_continue:
            self._continue_cycle_count = 0
            self._last_pending_ids = set()
            # Reset stale session mutation count from previous session snapshot
            # so new requests start fresh instead of showing "after 50 mutations"
            self._session_mutation_count = 0
            self._mutation_success_count = 0
            # Reset stale todo state from previous session snapshot
            # so fresh requests don't show leftover "2 todos pending"
            self._current_todos.clear()
            self._todo_write_streak = 0
            self._last_todo_signature = ""
            self._last_todo_mutation_count = -1
            self._todos_auto_cancelled = False
            # Reset stale AskUserQuestion state from previous session
            # Cancel any unresolved futures to prevent memory leaks
            for _q_id, _q_data in list(self._pending_questions.items()):
                _fut = _q_data.get("future")
                if isinstance(_fut, asyncio.Future) and not _fut.done():
                    _fut.cancel()
            self._pending_questions.clear()

        # ── Strip stopped task's messages from conversation history ──
        # When the user forcefully stops the AI mid-task and then sends
        # a new prompt, the old incomplete task's messages pollute the
        # context. Remove them so the AI starts fresh.
        if _was_stopped and not _is_continue:
            self._auto_continue_requested = False
            self._auto_continue_compacted = None
            self._auto_continue_cycle = 0
            # Clear ALL todo state from the stopped task
            self._current_todos.clear()
            self._todo_write_streak = 0
            self._last_todo_signature = ""
            self._last_todo_mutation_count = -1
            self._todos_auto_cancelled = False
            self._mutation_success_count = 0
            self._session_mutation_count = 0
            # Clear disabled tools from the stopped task
            self._tool_fail_counts.clear()
            self._disabled_tools.clear()
            self._tool_total_calls.clear()
            with self._history_lock:
                if self._conversation_history:
                    # Remove ALL messages after the SECOND-TO-LAST user message
                    # (keep the new user prompt, strip everything before it)
                    last_user_idx = None
                    second_last_user_idx = None
                    for i in range(len(self._conversation_history) - 1, -1, -1):
                        if self._conversation_history[i].role == 'user':
                            if last_user_idx is None:
                                last_user_idx = i
                            else:
                                second_last_user_idx = i
                                break
                    # Strip from second-to-last user message (inclusive) to last user message (exclusive)
                    if second_last_user_idx is not None:
                        del self._conversation_history[second_last_user_idx:last_user_idx]
                    elif last_user_idx is not None:
                        # Only one user message — strip everything except it
                        del self._conversation_history[:last_user_idx]
                    # Add strong system directive
                    self._conversation_history.insert(
                        max(0, len(self._conversation_history) - 1),
                        ChatMessage(
                            role="system",
                            content=(
                                "[CRITICAL: The user FORCEFULLY STOPPED your previous task. "
                                "You MUST completely abandon that task. Do NOT continue it. "
                                "Do NOT reference it. Do NOT retry any tools that were running. "
                                "All previous todos are CANCELLED. "
                                "Treat the user's next message as a completely new, unrelated request. "
                                "Start fresh. Acknowledge the stop briefly, then work on the new request.]"
                            )
                        )
                    )

        # Always reset tool counters — even on Continue — so tools
        # aren't still disabled from the previous cycle's limits.
        self._tool_fail_counts.clear()
        self._disabled_tools.clear()
        self._tool_total_calls.clear()
        # Reset post-mutation read counters to prevent getting stuck in read loops
        self._post_mutation_read_count = 0
        self._turns_since_last_mutation = 0
        self._aggressive_read_count = 0
        self._bash_usage_history = []
        self._bash_nudge_already_fired = False

        # Generate a unique task ID for this request.
        # Converted from LocalMainSessionTask.ts generateMainSessionTaskId().
        task_id = generate_session_task_id()
        log.info(f"[BRIDGE] process_message task_id={task_id}: {message[:80]}...")

        # Register the task in the registry.  The asyncio.Task is set later
        # by the worker thread (after asyncio.create_task in _process_queue).
        self._task_registry.register(
            SessionTaskState(
                task_id=task_id,
                description=message[:100],
                abort_controller=self._tool_ctx.abort_controller,
            )
        )

        # Save user turn to history
        self._conversation_history.append(
            ChatMessage(role="user", content=message, images=images or [])
        )

        self._worker.queue_message({
            "type":    "chat",
            "content": message,
            "images":  images or [],
            "context": {
                **self._enhancement_data,
                "active_file": self._active_file,
                "cursor_pos":  self._cursor_pos,
            },
            "task_id": task_id,   # Passed to worker so it can link asyncio.Task
        })

    def stop_generation(self):
        log.info("[BRIDGE] stop_generation")
        self._stop_requested = True          # Interrupt the streaming loop immediately

        # Reset auto-continue state to prevent getting stuck in loops
        self._auto_continue_requested = False
        self._auto_continue_compacted = None
        self._auto_continue_cycle = 0
        # Reset post-mutation counters
        self._post_mutation_read_count = 0
        self._turns_since_last_mutation = 0
        self._aggressive_read_count = 0

        # If a permission gate is open, deny it automatically on stop
        if self._permission_event is not None and not self._permission_event.is_set():
            self._permission_granted = False
            self._permission_event.set()
        # Also deny any pending file-edit permission gate on stop
        if self._file_edit_permission_event is not None and not self._file_edit_permission_event.is_set():
            self._file_edit_permission = "rejected"
            self._file_edit_permission_event.set()

        # Use stop_session_task() to cancel the asyncio.Task via task.cancel().
        # Converted from stopTask.ts stopTask() → taskImpl.kill().
        # CancelledError propagates through ALL awaits in the call chain,
        # including mid-tool-execution — no polling required.
        active = self._task_registry.get_active()
        if active:
            try:
                stop_session_task(active.task_id, self._task_registry)
            except StopTaskError as exc:
                log.info(f"[BRIDGE] StopTaskError (expected if task not started): {exc}")

        # Also queue a stop message so the worker's inner asyncio.wait loop
        # (Phase 3 of _process_queue) wakes up and processes the cancellation.
        self._worker.queue_message({"type": "stop"})

    def on_file_edit_permission_respond(self, decision: str):
        """Called when user responds to the file-edit permission card.
        decision: 'once', 'always', or 'rejected'

        CRITICAL: When the user rejects a file edit, we set _stop_requested = True
        so the agentic loop breaks IMMEDIATELY. This prevents the AI from writing
        new files or injecting code after the user said NO to an edit.
        """
        log.info(f"[BRIDGE] File edit permission response: {decision}")
        self._file_edit_permission = decision
        if decision == "always":
            self._always_allowed = True
        # ── ENFORCE USER CHOICE: reject = abort the current turn ──
        if decision == "rejected":
            self._stop_requested = True
            log.info("[BRIDGE] User REJECTED file edit — stopping agentic turn")
        if self._file_edit_permission_event is not None:
            self._file_edit_permission_event.set()

    def on_project_access_respond(self, decision: str):
        """Called when user responds to the initial project access permission card.
        decision: 'once', 'always', or 'rejected'
        
        This is the SINGLE initial permission gate. Once granted, all subsequent
        operations (file edits, bash, etc.) proceed without asking again.
        """
        log.info(f"[BRIDGE] Project access permission response: {decision}")
        if decision == "once":
            self._project_access_granted = True
            self._file_edit_permission = "once"
        elif decision == "always":
            self._project_access_granted = True
            self._always_allowed = True
            self._file_edit_permission = "always"
        elif decision == "rejected":
            self._project_access_granted = False
            self._stop_requested = True
            log.info("[BRIDGE] User REJECTED project access — stopping agentic turn")
        
        if self._project_access_event is not None:
            self._project_access_event.set()

    async def _request_project_access(self) -> bool:
        """Request initial project access permission from user.
        Returns True if granted, False if rejected or timeout.
        Only asks ONCE per session - subsequent calls return True immediately.
        """
        if self._project_access_granted or self._always_allowed:
            return True
        
        evt = threading.Event()
        self._project_access_event = evt
        self.project_access_requested.emit()
        granted = await asyncio.to_thread(evt.wait, 60.0)
        self._project_access_event = None
        return granted and self._project_access_granted

    def on_permission_respond(self, decision: str):
        """Called when user clicks Accept or Reject on a permission card.
        decision: 'accept' or 'reject'

        CRITICAL: When the user rejects, we set _stop_requested = True so the
        agentic loop breaks IMMEDIATELY after this tool returns. This prevents
        the AI from continuing to execute more tools after the user said NO.
        """
        log.info(f"[BRIDGE] Permission response: {decision}")
        self._permission_granted = (decision == 'accept')
        # ── ENFORCE USER CHOICE: reject = abort the current turn ──
        if decision == 'reject':
            self._stop_requested = True
            log.info("[BRIDGE] User REJECTED permission — stopping agentic turn")
        if self._permission_event is not None:
            # _permission_event may have been created in the async worker thread.
            # threading.Event.set() is always thread-safe.
            self._permission_event.set()

    def _norm(self, path: str) -> str:
        """Canonical path for _deferred_edits keys — prevents separator mismatch."""
        return os.path.normpath(os.path.abspath(path))

    def queue_accept_nudge(self, path: str):
        """Queue a nudge to tell the AI the file state changed after user accepts.
        Called from main_window._on_accept_file_edit."""
        norm = self._norm(path)
        nudge = (
            f"[System: The user just ACCEPTED deferred edits to {os.path.basename(norm)}. "
            f"The file has been written to disk with all accumulated AI changes. "
            f"IMPORTANT: The file content has CHANGED since your last read. "
            f"If you plan to make more edits to this file, you MUST Read it first "
            f"to get the current content before using search_replace.]"
        )
        self._pending_accept_nudges.append(nudge)
        log.info(f"[BRIDGE] Queued accept nudge for {norm}")

    def apply_deferred_edit(self, path: str) -> bool:
        """Write accumulated deferred content to disk. Called by UI on Accept."""
        key = self._norm(path)
        content = self._deferred_edits.pop(key, None)
        if content is None:
            log.warning(f"[BRIDGE] apply_deferred_edit: nothing staged for {key}")
            return False
        try:
            os.makedirs(os.path.dirname(key), exist_ok=True)
            with open(key, "w", encoding="utf-8", newline="") as f:
                f.write(content)
            # Sync tool context so next edit doesn't fail staleness check
            if hasattr(self, '_tool_ctx') and self._tool_ctx:
                self._tool_ctx.read_file_state[key] = {
                    "content": content,
                    "timestamp": os.path.getmtime(key),
                    "offset": None,
                    "limit": None,
                }
            log.info(f"[BRIDGE] Applied deferred edit to {key} ({len(content)} chars)")
            return True
        except Exception as e:
            log.error(f"[BRIDGE] apply_deferred_edit failed for {key}: {e}")
            return False

    def get_deferred_edit(self, path: str) -> Optional[str]:
        """Return pending deferred content for a file (without removing it).
        Used by the editor to show deferred content when user reopens a file.
        Returns None if no deferred edit is pending."""
        key = self._norm(path)
        return self._deferred_edits.get(key)

    def get_deferred_edit_keys(self) -> List[str]:
        """Return list of normalized paths that have pending deferred edits.
        Used by main_window to know which files to refresh after auto-apply."""
        return list(self._deferred_edits.keys())

    def discard_deferred_edit(self, path: str):
        """Discard deferred content. Called by UI on Reject."""
        key = self._norm(path)
        removed = self._deferred_edits.pop(key, None)
        if removed:
            log.info(f"[BRIDGE] Discarded deferred edit for {key} ({len(removed)} chars)")

    def _merge_parallel_edits(self, deferred: str, disk: str, disk_with_edit: str) -> str | None:
        """Merge a parallel edit (applied to disk) into the accumulated deferred state.

        When the LLM sends multiple Edit calls for the same file in one turn, they are
        all based on the same disk state.  Edit 1 chains into deferred.  Edit 2 might
        target content NOT in deferred (parallel/independent edit).  We apply Edit 2 to
        the original disk content, then merge the result with deferred.

        Args:
            deferred: The accumulated deferred content (has Edit 1 applied).
            disk: The original disk content (what the LLM read).
            disk_with_edit: disk with Edit 2 applied.

        Returns:
            Merged content with BOTH edits, or None if merge fails (overlapping edits).
        """
        if deferred == disk_with_edit:
            return deferred  # Same result — no merge needed

        # Strategy: compute what changed between disk and disk_with_edit,
        # then apply that same delta to deferred.
        # For simple non-overlapping replacements, we can detect the changed region
        # and apply it if it doesn't conflict with deferred.
        try:
            # Find the common prefix and suffix between disk and disk_with_edit
            prefix_len = 0
            min_len = min(len(disk), len(disk_with_edit))
            while prefix_len < min_len and disk[prefix_len] == disk_with_edit[prefix_len]:
                prefix_len += 1

            suffix_len = 0
            while (suffix_len < (min_len - prefix_len) and
                   disk[-(suffix_len + 1)] == disk_with_edit[-(suffix_len + 1)]):
                suffix_len += 1

            # The changed region in disk: [prefix_len : len(disk)-suffix_len]
            # The changed region in disk_with_edit: [prefix_len : len(disk_with_edit)-suffix_len]
            old_region = disk[prefix_len:len(disk) - suffix_len] if suffix_len else disk[prefix_len:]
            new_region = disk_with_edit[prefix_len:len(disk_with_edit) - suffix_len] if suffix_len else disk_with_edit[prefix_len:]

            # Check if the same old_region exists in deferred at the same position
            if suffix_len:
                deferred_old_region = deferred[prefix_len:len(deferred) - suffix_len]
            else:
                deferred_old_region = deferred[prefix_len:]

            if deferred_old_region == old_region:
                # Same region in deferred — safe to apply Edit 2's replacement
                result = deferred[:prefix_len] + new_region + (deferred[len(deferred) - suffix_len:] if suffix_len else "")
                return result
            else:
                # Different region — edits overlap or deferred has different content there
                # Try a simple approach: just replace old_region in deferred
                if old_region and old_region in deferred:
                    result = deferred.replace(old_region, new_region, 1)
                    return result
                log.warning(f"[BRIDGE] _merge_parallel_edits: regions don't align, cannot merge safely")
                return None
        except Exception as e:
            log.warning(f"[BRIDGE] _merge_parallel_edits failed: {e}")
            return None

    def apply_all_deferred_edits(self) -> int:
        """Write ALL pending deferred edits to disk. Called on app shutdown
        so that AI edits are never lost even if the user forgets to click Accept.
        Returns the count of files written.
        """
        if not self._deferred_edits:
            return 0
        count = 0
        for key, content in list(self._deferred_edits.items()):
            try:
                os.makedirs(os.path.dirname(key), exist_ok=True)
                with open(key, "w", encoding="utf-8", newline="") as f:
                    f.write(content)
                if hasattr(self, '_tool_ctx') and self._tool_ctx:
                    self._tool_ctx.read_file_state[key] = {
                        "content": content,
                        "timestamp": os.path.getmtime(key),
                        "offset": None,
                        "limit": None,
                    }
                log.info(f"[BRIDGE] Auto-applied deferred edit on shutdown: {key} ({len(content)} chars)")
                count += 1
            except Exception as e:
                log.error(f"[BRIDGE] Auto-apply deferred edit failed for {key}: {e}")
        self._deferred_edits.clear()
        log.info(f"[BRIDGE] Auto-applied {count} deferred edit(s) on shutdown")
        return count

    def set_project_root(self, path: str):
        self._project_root = path
        self._memory_dir   = None  # reset so _get_memory_dir() recomputes for new project
        self._cached_project_summary = None  # reset so _get_project_summary() rebuilds for new project
        log.info(f"[BRIDGE] project root → {path}")

        # ── Auto-create .cortex/ directory structure ──
        # Every project gets its own .cortex/ (like .git/, .claude/, .vscode/)
        # so memory always stays inside the working project.
        self._auto_create_cortex_dir(path)

        # ── Restore chat context from DB for conversation continuity ──
        # This MUST happen here (not in __init__) because _project_root
        # is None during _hydrate_from_snapshot. Only restore once per
        # session to avoid duplicate context on project switches.
        if not self._chat_context_restored:
            self._chat_context_restored = True
            self._restore_latest_conversation(path)

        try:
            _agent_set_project_root(path)
        except Exception:
            pass

    def _restore_latest_conversation(self, project_path: str) -> None:
        """Restore the most recent conversation from DB into _conversation_history.
        
        Called once on project open for initial AI context.
        Uses optimized query to fetch only the latest conversation.
        """
        try:
            from src.core.chat_history import get_chat_history
            _history_mgr = get_chat_history()
            # Use optimized query - fetch only latest conversation instead of all
            _latest = _history_mgr.db.get_latest_conversation(project_path)
            if _latest:
                _conv_id = _latest.get('conversation_id', '')
                log.info(f"[SESSION] _restore_latest_conversation: latest conv_id={_conv_id}")
                if _conv_id:
                    self._restore_conversation_messages(_conv_id)
            else:
                log.info("[SESSION] _restore_latest_conversation: no conversations "
                         "found — conversation_history will be empty")
        except Exception as _e:
            log.warning(f'[SESSION] Failed to restore chat messages from DB: {_e}')

    def restore_conversation_context(self, conversation_id: str) -> None:
        """Restore a SPECIFIC conversation's context into _conversation_history.
        
        Called by the UI when the user switches to a different chat.
        Replaces the current history with the selected conversation's messages
        so the AI has correct context for follow-up questions.
        
        Args:
            conversation_id: The conversation to load context from.
        """
        if not conversation_id:
            return
        
        log.info(f'[SESSION] Switching AI context to conversation: {conversation_id}')
        self._current_conversation_id = conversation_id
        
        # Clear existing history (except system messages like resume markers)
        system_msgs = [m for m in self._conversation_history if m.role == 'system']
        self._conversation_history.clear()
        self._conversation_history.extend(system_msgs)
        
        # Load the selected conversation
        self._restore_conversation_messages(conversation_id)

    def _restore_conversation_messages(self, conversation_id: str) -> None:
        """Load messages from DB and append to _conversation_history."""
        try:
            from src.core.chat_history import get_chat_history
            _history_mgr = get_chat_history()
            _msgs = _history_mgr.get_messages(conversation_id, limit=20)
            _recent: List[Dict] = []
            for _m in reversed(_msgs):
                if _m.get('role') in ('user', 'assistant'):
                    _recent.insert(0, _m)
                    if len(_recent) >= 20:
                        break
            if _recent:
                _restored_count = 0
                for _m in _recent:
                    _content = (_m.get('content') or '').strip()
                    # Metadata blob stores reasoning_content, tool_calls,
                    # tool_call_id (serialized by save_chats_to_sqlite).
                    _meta = _m.get('metadata', {}) or {}
                    # Allow empty content for assistant messages that have
                    # tool_calls (tool-call placeholders) or reasoning_content
                    # (thinking-mode models like MiMo require these preserved).
                    _has_tc = bool(_m.get('tool_calls') or _meta.get('tool_calls'))
                    _has_rc = bool(_m.get('reasoning_content') or _meta.get('reasoning_content'))
                    if not _content and not _has_tc and not _has_rc:
                        continue
                    if len(_content) > 4000:
                        _content = _content[:4000] + '\n... [truncated on restore]'
                    _cm = ChatMessage(role=_m.get('role', 'user'), content=_content)
                    # PRESERVE reasoning_content — MiMo/DeepSeek thinking
                    # mode requires reasoning_content from previous assistant
                    # messages to be passed back in subsequent requests.
                    # Without this, MiMo returns HTTP 400:
                    #   "The reasoning_content in the thinking mode must be
                    #    passed back to the API."
                    _rc = _m.get('reasoning_content') or _meta.get('reasoning_content')
                    if _rc:
                        _cm.reasoning_content = _rc
                    _tc = _m.get('tool_calls') or _meta.get('tool_calls')
                    if _tc:
                        _cm.tool_calls = _tc
                    _tcid = _m.get('tool_call_id') or _meta.get('tool_call_id')
                    if _tcid:
                        _cm.tool_call_id = _tcid
                    self._conversation_history.append(_cm)
                    _restored_count += 1
                log.info(
                    f'[SESSION] Restored {_restored_count} messages from conversation {conversation_id}'
                )
        except Exception as _e:
            log.warning(f'[SESSION] Failed to restore conversation messages: {_e}')

    def set_project_context(self, context: Any) -> None:
        if isinstance(context, dict):
            self._enhancement_data.update(cast(Dict[str, Any], context))
        elif hasattr(context, "to_dict"):
            to_dict_fn = getattr(context, "to_dict", None)
            if callable(to_dict_fn):
                mapped = to_dict_fn()
                if isinstance(mapped, dict):
                    self._enhancement_data.update(cast(Dict[str, Any], mapped))
        elif hasattr(context, "__dict__"):
            raw_vars_any: Any = vars(context)  # pyright: ignore[reportUnknownVariableType]
            raw_vars = cast(Dict[str, Any], raw_vars_any) if isinstance(raw_vars_any, dict) else {}
            filtered: Dict[str, Any] = {
                str(k): v for k, v in raw_vars.items() if isinstance(k, str) and not k.startswith("_")
            }
            self._enhancement_data.update(filtered)

    def update_settings(self, **kwargs: Any) -> None:
        self._enhancement_data.update(kwargs)

    def set_terminal(self, terminal: Any) -> None:
        self._terminal = terminal

    def set_active_file(self, filepath: str, cursor_pos: Optional[int] = None):
        self._active_file = filepath
        self._cursor_pos  = cursor_pos

    def clear_active_file(self):
        self._active_file = None
        self._cursor_pos  = None

    def set_always_allowed(self, allowed: bool):
        self._always_allowed = allowed

    def set_interaction_mode(self, mode: str):
        self._interaction_mode = mode

    def set_ui_parent(self, parent: Any) -> None:
        self._ui_parent = parent

    def user_responded(self, question_id: str, answer: str):
        """Forward answer from UI signal (answer_question_requested) to on_answer_question."""
        log.info(f"[BRIDGE] user_responded: question_id={question_id!r}")
        self.on_answer_question(question_id, answer)

    def chat(self, message: str, context: str = ""):
        # Include any pending images from paste
        images = None
        if hasattr(self, '_pending_images') and self._pending_images:
            images = [img["data"] for img in self._pending_images]
            self._pending_images = []

        # Image fallback: if images attached, use Mistral for OCR first,
        # then pass recognized content to the user's selected model.
        if images:
            self._handle_image_with_fallback(message, images, context)
        else:
            self.process_message(message, images=None)

    def _handle_image_with_fallback(self, message: str, images: list, context: str):
        """Two-step image processing:
        1. Send image to Mistral (vision) — stream extraction to user in chat
        2. Then send extraction + user prompt to the selected model
        """
        import threading

        # Check Mistral access type — subscription only (server holds the API key)
        def _get_mistral_access_type():
            """Returns: 'subscription' or None. Vision/OCR is subscription-only."""
            try:
                from src.core.cortex_api import get_api_client
                api = get_api_client()
                if api.has_subscription():
                    return "subscription"
            except Exception:
                pass
            
            return None

        access_type = _get_mistral_access_type()
        
        if access_type is None:
            # Show subscription required message
            error_msg = (
                "🔒 **Image/OCR Feature Requires Subscription**\n\n"
                "Image analysis and OCR (text extraction from images) are available with a Cortex subscription.\n\n"
                "**Options:**\n"
                "• Subscribe to Cortex Pro ($10/mo)\n\n"
                "[View Plans →](/pricing)"
            )
            self._safe_emit(self.response_chunk, error_msg)
            return

        def _vision_thread():
            try:
                log.info(f"[VISION] Sending image to Mistral for OCR (access_type={access_type})...")

                # ── Ensure image data has proper data URI prefix ──────
                # Raw base64 from clipboard/inject is missing the required
                # 'data:image/<format>;base64,' prefix that Mistral demands.
                _img_raw = images[0] if images else ''
                if _img_raw and not _img_raw.startswith('data:'):
                    # Auto-detect format from base64 header (PNG=\x89PNG, JPEG=\xff\xd8, GIF=GIF8, WEBP=RIFF)
                    try:
                        import base64 as _b64detect
                        _decoded_head = _b64detect.b64decode(_img_raw[:32])
                        if _decoded_head[:4] == b'\x89PNG':
                            _img_fmt = 'png'
                        elif _decoded_head[:2] == b'\xff\xd8':
                            _img_fmt = 'jpeg'
                        elif _decoded_head[:4] == b'GIF8':
                            _img_fmt = 'gif'
                        elif _decoded_head[:4] == b'RIFF':
                            _img_fmt = 'webp'
                        else:
                            _img_fmt = 'png'  # safe default
                    except Exception:
                        _img_fmt = 'png'
                    _img_data_uri = f"data:image/{_img_fmt};base64,{_img_raw}"
                    log.info(f"[VISION] Prepended data URI prefix: data:image/{_img_fmt};base64,... ({len(_img_raw)} chars)")
                else:
                    _img_data_uri = _img_raw  # already a data URI or URL

                vision_prompt = (
                    "Analyze this screenshot thoroughly. Provide TWO sections:\n\n"
                    "## TEXT CONTENT\n"
                    "Transcribe ALL visible text exactly as shown — code, labels, "
                    "error messages, filenames, line numbers, terminal output, etc.\n\n"
                    "## VISUAL CONTEXT\n"
                    "Describe what you SEE beyond the text:\n"
                    "- UI state: which tab/file is active, what's selected/highlighted, "
                    "cursor position, scroll position\n"
                    "- Visual indicators: error underlines, warning icons, colored markers, "
                    "diff highlighting (green=added, red=removed), status bar info\n"
                    "- Layout: panel arrangement, which panels are open/collapsed, "
                    "relative positions of elements\n"
                    "- Behavioral clues: loading spinners, progress bars, grayed-out "
                    "elements, focus states, hover states, tooltips visible\n"
                    "- Anomalies: anything that looks broken, misaligned, truncated, "
                    "overlapping, or visually wrong\n\n"
                    "DO NOT provide solutions, fixes, or analysis. Only describe what is visible."
                )
                if message:
                    vision_prompt += f"\n\nUser's question about this image: {message}"

                vision_messages = [
                    {"role": "user", "content": [
                        {"type": "image_url", "image_url": {"url": _img_data_uri}},
                        {"type": "text", "text": vision_prompt}
                    ]}
                ]

                # Route through Django proxy (subscription-only, server holds Mistral key)
                from src.core.cortex_api import get_api_client
                api = get_api_client()
                result = api.proxy_service(
                    "mistral_ocr",
                    image_url=_img_data_uri,
                    prompt=vision_prompt,
                )
                if result and result.get("status") == "success":
                    vision_response = result.get("data", {}).get("choices", [{}])[0].get("message", {}).get("content", "")
                else:
                    vision_response = None

                # Handle response
                if not vision_response:
                    log.warning("[VISION] OCR failed or returned empty")
                    if self._current_model_supports_vision():
                        self.response_chunk.emit("\n\n*Vision extraction failed — sending image directly to model.*\n")
                        self.process_message(message, images=images)
                    else:
                        self.response_chunk.emit(
                            "\n\n*Vision extraction failed and current model does not support images. "
                            "Please try again or switch to a vision-capable model.*\n"
                        )
                    self.response_complete.emit("")
                    return

                log.info(f"[VISION] OCR complete: {len(vision_response)} chars")

                # Track OCR page usage
                try:
                    from src.ai.usage_tracker import get_usage_tracker
                    get_usage_tracker().record_ocr_pages(1)
                except Exception:
                    pass

                # Emit the OCR result
                self.response_chunk.emit("**🔍 Image Recognition:**\n\n")
                self.response_chunk.emit(vision_response)
                self.response_chunk.emit("\n\n---\n\n**Now processing with your selected model...**\n")

                # Step 2: Inject vision result as context into conversation history
                self.inject_vision_history(
                    f"[Image attached by user]\n{message}",
                    f"[Screenshot analysis — text + visual context]: {vision_response}"
                )

                # Step 3: Send to user's selected model with full visual context
                enhanced_message = (
                    f"{message}\n\n"
                    f"[Screenshot analysis from vision model — includes both transcribed text "
                    f"AND visual/behavioral context like UI state, highlights, errors, layout. "
                    f"Treat this as if you can see the screenshot yourself]:\n{vision_response}"
                )
                self.process_message(enhanced_message, images=None)

            except Exception as e:
                log.error(f"[VISION] Fallback failed: {e}")
                # Final fallback: only send images if current model supports vision
                if self._current_model_supports_vision():
                    self.process_message(message, images=images)
                else:
                    self.process_message(message, images=None)

        thread = threading.Thread(target=_vision_thread, daemon=True, name="VisionFallback")
        thread.start()

    def chat_with_enhancement(
        self,
        message: str,
        intent: Optional[str] = None,
        route: Optional[str] = None,
        tools: Optional[List[str]] = None,
        code_context: str = "",
    ):
        self._enhancement_data.update(
            {"intent": intent, "route": route, "tools": tools, "code_context": code_context}
        )
        self.process_message(message)

    def chat_with_testing(self, message: str = "", **kwargs: Any):
        self.process_message(message)

    def generate_chat_title(self, message: str, conv_id: str) -> str:
        words = message.split()[:6]
        title = " ".join(words)
        if len(message.split()) > 6:
            title += "…"
        return title

    def get_last_enhancement_data(self) -> Dict[str, Any]:
        return self._enhancement_data.copy()

    def stop(self):
        self.stop_generation()

    def cleanup(self):
        log.info("[BRIDGE] cleanup")
        self._worker.stop()

    def clear_conversation(self):
        """Clear the in-memory conversation history."""
        with self._history_lock:
            self._conversation_history.clear()

    def get_provider_status(self) -> Dict[str, Any]:
        """Collect and return live status of all providers for the pricing panel.

        Returns a dict keyed by provider name with status, tier, quota info.
        Called from JS via bridge to update the pricing panel dynamically.
        """
        result: Dict[str, Any] = {}
        try:
            from src.ai.providers import ProviderRegistry, ProviderType
            registry = ProviderRegistry.get_instance()
            providers = registry.list_providers()

            for pt in providers:
                name = pt.name.lower() if hasattr(pt, 'name') else str(pt).lower()
                prov = registry.get_provider(pt)
                info: Dict[str, Any] = {'status': 'unknown'}

                try:
                    has_key = prov.validate_api_key() if hasattr(prov, 'validate_api_key') else False
                    if not has_key:
                        info['status'] = 'exhausted'
                        info['message'] = 'No valid API key configured'
                    else:
                        # Check if we cached any rate limit info
                        info['status'] = 'active'
                        info['message'] = 'API key valid'
                except Exception:
                    info['status'] = 'unknown'
                    info['message'] = 'Could not validate'

                # Check failover state from _failover_attempted
                attempted = getattr(self, '_failover_attempted', None)
                if attempted and pt in attempted:
                    info['status'] = info.get('status', 'limited')
                    info['message'] = info.get('message', '') + ' (was attempted, may be rate limited)'

                result[name] = info

            # Ensure all four main providers are present even if not in registry
            for fallback_name in ['mimo', 'deepseek', 'openai', 'mistral']:
                if fallback_name not in result:
                    result[fallback_name] = {'status': 'unknown', 'message': 'Provider not registered'}

        except Exception as e:
            log.warning(f"[BRIDGE] get_provider_status failed: {e}")
            # Return minimal fallback data
            result = {
                'mimo': {'status': 'unknown', 'message': 'Error querying provider'},
                'deepseek': {'status': 'unknown', 'message': 'Error querying provider'},
                'openai': {'status': 'unknown', 'message': 'Error querying provider'},
                'mistral': {'status': 'unknown', 'message': 'Error querying provider'},
            }
        return result

    # ============================================================
    # IMMEDIATE PERSISTENCE HELPERS (Task 1: SQLite saves)
    # ============================================================

    def _ensure_conversation_id(self) -> str:
        """Create a conversation_id if one doesn't exist yet.

        Called at the start of every _call_llm invocation to guarantee
        the DB always has a valid conversation_id for immediate saves.
        """
        if not self._current_conversation_id:
            import uuid
            self._current_conversation_id = str(uuid.uuid4())
            log.info(f"[BRIDGE] Auto-created conversation_id: {self._current_conversation_id}")
        return self._current_conversation_id

    def save_session_to_memory(self) -> None:
        """Summarize + compact the CURRENT conversation into MEMORY.md, then emit
        session_saved_to_memory(success).

        Used by the 'New Chat' flow: the whole chat is condensed into MEMORY.md
        (task, todos, files, key decisions, conversation digest) so the user can
        start a fresh chat WITHOUT losing context — the next message auto-loads
        MEMORY.md into the system prompt. Runs on a daemon thread so the UI stays
        responsive and the 'Summarizing & compacting…' status can animate.
        """
        import threading as _th

        def _work():
            ok = False
            try:
                # Gather the FULL conversation — prefer the complete DB history
                # (survives auto-compaction/trimming) and fall back to in-memory.
                _hist = self._gather_full_history()
                log.info(f"[BRIDGE] save_session_to_memory: gathered {len(_hist)} msgs "
                         f"(conv_id={self._current_conversation_id})")
                _has_real = any(
                    getattr(m, 'role', None) in ('user', 'assistant')
                    and (getattr(m, 'content', '') or '').strip()
                    for m in _hist
                )
                # Always show spinner so the user gets visual feedback
                # (previously buried in else block — empty chats showed nothing for 15s)
                try:
                    self._safe_emit(self.agent_status_update,
                                    'saving_memory', 'Summarizing chat to memory...')
                except RuntimeError:
                    return  # C++ object destroyed during shutdown
                if not _has_real:
                    log.warning("[BRIDGE] save_session_to_memory: no real messages "
                                "-- skipping MEMORY.md write")
                    try:
                        self._safe_emit(self.agent_status_update,
                                        'ready', 'No messages to save - starting fresh chat')
                    except RuntimeError:
                        pass  # C++ object destroyed during shutdown
                    ok = False
                else:
                    # 1. Save the full checkpoint FILE (+ semantic index) for recovery,
                    #    but do NOT dump the verbatim conversation digest into MEMORY.md.
                    self._create_context_checkpoint(_hist, write_memory_md=False)
                    # 2. MEMORY.md gets ONLY a concise "what was actually done" summary
                    #    (LLM-generated). For large chats this avoids storing the whole
                    #    transcript — just the outcomes/decisions/files/next-steps.
                    _summary = self._llm_summarize_conversation(self._build_transcript(_hist))
                    if not _summary:
                        # Fallback: brief structured "what was done" (no verbatim chat).
                        _summary = self._build_brief_done_summary(_hist)
                    self._write_memory_summary(_summary)
                    try:
                        self._safe_emit(self.agent_status_update,
                                        'ready', 'Saved "what was done" to MEMORY.md')
                    except RuntimeError:
                        pass  # C++ object destroyed during shutdown
                    log.info(f"[BRIDGE] Concise 'what was done' summary saved to MEMORY.md "
                             f"({len(_hist)} messages condensed, New Chat)")
                    ok = True
            except Exception as e:
                log.error(f"[BRIDGE] save_session_to_memory failed: {e}", exc_info=True)
                try:
                    self._safe_emit(self.agent_status_update, 'ready', f'Memory save failed: {e}')
                except RuntimeError:
                    pass  # C++ object destroyed during shutdown
                ok = False
            finally:
                try:
                    self._safe_emit(self.session_saved_to_memory, ok)
                except RuntimeError:
                    pass  # C++ object destroyed during shutdown

        _th.Thread(target=_work, daemon=True, name='SaveSessionMemory').start()
    def _gather_full_history(self) -> List[Any]:
        """Return the COMPLETE conversation as ChatMessage objects.

        Tries three sources in order of completeness:
        1. chat_messages table (individual rows)
        2. conversation timeline JSON (saved by ChatPanel after each turn)
        3. in-memory conversation history
        """
        from src.ai.providers import ChatMessage as _PCM
        with self._history_lock:
            _mem = list(self._conversation_history)
        _db_msgs: List[Any] = []
        try:
            if self._current_conversation_id:
                from src.core.chat_history import get_chat_history
                _mgr = get_chat_history()
                # Source 1: individual chat_messages rows
                _rows = _mgr.get_messages(self._current_conversation_id, limit=500)
                for _r in _rows:
                    _role = _r.get('role')
                    _content = (_r.get('content') or '').strip()
                    if _role in ('user', 'assistant') and _content:
                        _db_msgs.append(_PCM(role=_role, content=_content))
                # Source 2: timeline JSON (ChatPanel saves this after each turn)
                if not _db_msgs:
                    _tl = _mgr.get_timeline(self._current_conversation_id)
                    if _tl:
                        _tl_data = _tl if isinstance(_tl, list) else _tl.get('messages', []) if isinstance(_tl, dict) else []
                        for entry in _tl_data:
                            if isinstance(entry, dict):
                                _role = entry.get('role', '')
                                _content = (entry.get('content') or entry.get('text') or '').strip()
                                if _role in ('user', 'assistant') and _content:
                                    _db_msgs.append(_PCM(role=_role, content=_content))
                        if _db_msgs:
                            log.info(f"[BRIDGE] _gather_full_history: recovered {len(_db_msgs)} msgs from timeline JSON")
        except Exception as e:
            log.debug(f"[BRIDGE] _gather_full_history DB read skipped: {e}")
        return _db_msgs if len(_db_msgs) >= len(_mem) else _mem

    def _build_transcript(self, messages: List[Any]) -> str:
        """Flatten messages into a plain User/Assistant transcript for the LLM."""
        _lines: List[str] = []
        for m in messages:
            _role = getattr(m, 'role', None)
            _content = (getattr(m, 'content', '') or '').strip()
            if _role not in ('user', 'assistant') or not _content:
                continue
            if _content.startswith('[System') or _content.startswith('[Context'):
                continue
            _lines.append(f"{_role.capitalize()}: {_content}")
        return "\n\n".join(_lines)

    def _resolve_summary_provider(self):
        """Pick (provider, model) for one-off summary calls — mirrors _call_llm routing."""
        from src.ai.providers import get_provider_registry, ProviderType
        model_id = getattr(self, '_current_model_id', None)
        if not model_id:
            try:
                from src.config.settings import get_settings
                _s: Any = get_settings()
                _ai = _s.get('ai') if isinstance(_s, dict) else None
                model_id = (_ai or {}).get('model_id') if isinstance(_ai, dict) else None
            except Exception:
                model_id = None
        model_id = model_id or 'deepseek-chat'
        ml = model_id.lower()
        if '/' in ml:
            pt = ProviderType.OPENROUTER
        elif ml.startswith('deepseek'):
            pt = ProviderType.DEEPSEEK
        elif ml.startswith('mistral') or ml.startswith('codestral'):
            pt = ProviderType.MISTRAL
        elif ml.startswith('mimo-'):
            pt = ProviderType.MIMO
        elif 'siliconflow' in ml:
            pt = ProviderType.SILICONFLOW
        elif ml.startswith('qwen') or ml.startswith('qwq'):
            pt = ProviderType.ALIBABA
        elif ml.startswith('gpt-'):
            pt = ProviderType.OPENAI
        else:
            pt = ProviderType.DEEPSEEK
        return get_provider_registry().get_provider(pt), model_id

    def _llm_summarize_conversation(self, transcript: str) -> Optional[str]:
        """Use the current LLM to condense the ENTIRE conversation into a concise
        project memory. Returns None on any failure (caller keeps the structured
        checkpoint as fallback)."""
        if not transcript.strip():
            return None
        try:
            from src.ai.providers import ChatMessage as _PCM
            provider, model = self._resolve_summary_provider()
            if not provider:
                return None
            _sys = (
                "You summarize a coding session into a concise 'what was actually done' "
                "memory. Do NOT replay or transcribe the conversation. Capture only the "
                "OUTCOMES and facts needed to continue later, as short bullets under these "
                "markdown sections (omit any that don't apply):\n"
                "## What Was Done   (concrete accomplishments — features built, bugs fixed)\n"
                "## Files Created/Modified   (names + one-line purpose)\n"
                "## Key Decisions   (choices made and why)\n"
                "## Open Items / Next Steps   (what's left, known issues)\n"
                "Be specific and factual. No greetings, no chit-chat, no message-by-message "
                "recap, no code blocks. Keep it under 300 words."
            )
            _user = ("Summarize ONLY what was accomplished in this session (not the dialogue):\n\n"
                     + transcript[:24000])
            resp = provider.chat(
                [_PCM(role='system', content=_sys), _PCM(role='user', content=_user)],
                model=model, temperature=0.2, max_tokens=1200, stream=False,
            )
            if resp and not getattr(resp, 'error', None):
                _txt = (resp.content or '').strip()
                if len(_txt) > 40:
                    return _txt
        except Exception as e:
            log.warning(f"[BRIDGE] _llm_summarize_conversation failed: {e}")
        return None

    def _build_brief_done_summary(self, messages: List[Any]) -> str:
        """Fallback 'what was done' summary (used if the LLM call is unavailable).
        Built from todos + files touched — NOT a verbatim conversation dump."""
        lines: List[str] = []
        try:
            _mod = self._tool_ctx.get_recent_modified_files(15)
        except Exception:
            _mod = []
        # Completed todos = concrete things done
        _done = [t.get('content', '') for t in self._current_todos
                 if str(t.get('status', '')).upper() in ('COMPLETED', 'COMPLETE')]
        _pending = [t.get('content', '') for t in self._current_todos
                    if str(t.get('status', '')).upper() not in ('COMPLETED', 'COMPLETE', 'CANCELLED')]
        if _done:
            lines.append("## What Was Done")
            lines += [f"- {d}" for d in _done if d]
        if _mod:
            lines.append("\n## Files Created/Modified")
            lines += [f"- `{os.path.basename(f)}`" for f in _mod]
        if _pending:
            lines.append("\n## Open Items / Next Steps")
            lines += [f"- {p}" for p in _pending if p]
        if not lines:
            # Last resort: the latest user request only.
            for m in reversed(messages):
                if getattr(m, 'role', None) == 'user' and (getattr(m, 'content', '') or '').strip():
                    lines.append("## What Was Done")
                    lines.append(f"- Worked on: {(m.content or '')[:200]}")
                    break
        return "\n".join(lines) if lines else "- (no significant actions recorded)"

    def _auto_save_to_memory_light(self) -> None:
        """Lightweight MEMORY.md update after each turn — ZERO LLM cost.

        Uses _build_brief_done_summary() to build a structured summary from
        todos, files modified, and recent actions. Writes to MEMORY.md via
        _write_memory_summary() which preserves previous sessions.

        Caller SHOULD wrap this in a daemon thread to avoid blocking the UI.
        Silently returns if there's nothing meaningful to save.
        """
        try:
            _hist = self._gather_full_history()
            if not _hist:
                return
            _summary = self._build_brief_done_summary(_hist)
            if not _summary or _summary.strip() in (
                "- (no significant actions recorded)",
                "",
            ):
                return
            self._write_memory_summary(_summary)
            log.info("[AutoSave] MEMORY.md updated after turn (lightweight)")
        except Exception as e:
            log.error(f"[AutoSave] _auto_save_to_memory_light failed: {e}", exc_info=True)

    def _write_memory_summary(self, summary: str) -> None:
        """Write a CONCISE 'what was done' summary to MEMORY.md (<project>/.cortex/memory/).

        APPENDS the new summary (preserves previous sessions). Keeps last 50 sessions
        to prevent file bloat. Deduplicates identical entries. Most recent first.
        """
        import re as _re
        from datetime import datetime as _dt
        path = self._get_memory_md_path()
        try:
            existing_sections: List[str] = []
            preserved_pointers: List[str] = []
            if os.path.exists(path):
                with open(path, 'r', encoding='utf-8') as fh:
                    content = fh.read()

                in_summary = False
                current_block: List[str] = []

                for line in content.split('\n'):
                    s = line.strip()
                    if s.startswith('## Session Summary'):
                        if in_summary and current_block:
                            existing_sections.append('\n'.join(current_block))
                        in_summary = True
                        current_block = [line]
                        continue
                    if in_summary and (
                        s.startswith('## Other Memories')
                        or s.startswith('## Loaded Memory Files')
                        or s.startswith('## Conversation Summary')
                    ):
                        if current_block:
                            existing_sections.append('\n'.join(current_block))
                        in_summary = False
                        current_block = []
                        continue
                    if in_summary:
                        current_block.append(line)
                    elif s.startswith('- ['):
                        preserved_pointers.append(line)

                if current_block:
                    existing_sections.append('\n'.join(current_block))

            # Keep ALL sessions — never delete old memory
            # Only deduplicate identical content

            # Strip blank lines from blocks
            def _strip_blk(b: str) -> str:
                lines = b.split('\n')
                while lines and not lines[0].strip():
                    lines.pop(0)
                while lines and not lines[-1].strip():
                    lines.pop()
                return '\n'.join(lines)

            existing_sections = [_strip_blk(s) for s in existing_sections if _strip_blk(s)]

            # Deduplicate: remove sections with identical content (different timestamps)
            seen_content = set()
            deduped = []
            for section in existing_sections:
                # Extract content after the timestamp line
                lines = section.split('\n')
                content_lines = [l for l in lines if not l.strip().startswith('_Last updated:') and not l.strip().startswith('##')]
                content_key = '\n'.join(content_lines).strip()[:200]
                if content_key and content_key not in seen_content:
                    seen_content.add(content_key)
                    deduped.append(section)
            existing_sections = deduped

            # Build new MEMORY.md
            now_str = _dt.now().strftime('%Y-%m-%d %H:%M')
            out: List[str] = [
                '# Cortex Memory Index',
                '',
            ]

            # Add NEW session summary FIRST (most recent)
            out += [
                '## Session Summary — what was done',
                '',
                f'_Last updated: {now_str}_',
                '',
                summary.strip(),
                '',
            ]

            # Add PREVIOUS session summaries
            for section in existing_sections:
                out += [section, '']

            # Add preserved pointer lines
            if preserved_pointers:
                out += ['## Other Memories', ''] + preserved_pointers + ['']

            # Collapse 3+ blank lines to max 2
            raw = '\n'.join(out)
            raw = _re.sub(r'\n{4,}', '\n\n\n', raw)
            raw = raw.rstrip('\n') + '\n'

            # Atomic write
            import tempfile as _tf
            _tmp_fd, _tmp_path = _tf.mkstemp(suffix='.md', dir=os.path.dirname(path))
            try:
                with os.fdopen(_tmp_fd, 'w', encoding='utf-8') as fh:
                    fh.write(raw)
                os.replace(_tmp_path, path)
            except Exception:
                try:
                    os.unlink(_tmp_path)
                except Exception:
                    pass
                raise

            total_sessions = 1 + len(existing_sections)
            log.info(f"[Memory] MEMORY.md updated — {total_sessions} sessions, {len(raw)} bytes")
            log.info(f"[BRIDGE] MEMORY.md updated: new summary added ({len(summary)} chars), "
                     f"{total_sessions} sessions preserved, {len(preserved_pointers)} pointers kept")
        except Exception as e:
            log.warning(f"[BRIDGE] _write_memory_summary failed: {e}")

    def start_fresh_session(self) -> None:
        """Reset all in-memory conversation state for a brand-new chat.

        Clears the conversation history and per-task state and starts a new
        conversation_id. Context is NOT lost — it lives in MEMORY.md (saved by
        save_session_to_memory) and is auto-loaded on the next message.
        """
        with self._history_lock:
            self._conversation_history = []
        self._current_conversation_id = None
        self._ensure_conversation_id()
        try:
            self._current_todos.clear()
            self._session_mutation_count = 0
            self._mutation_success_count = 0
            self._auto_continue_requested = False
            self._auto_continue_compacted = None
            self._auto_continue_cycle = 0
            self._last_todo_signature = ""
            self._last_todo_mutation_count = -1
            self._todos_auto_cancelled = False
        except Exception:
            pass
        log.info(f"[BRIDGE] Fresh session started (conv_id={self._current_conversation_id})")
        return self._current_conversation_id

    def _save_message_immediate(self, role: str, content: str) -> None:
        """Save a single message to SQLite immediately (fire-and-forget).

        Writes to BOTH the legacy conversations/chat_messages tables
        AND the new sessions/messages tables for timeline-based storage.
        Estimates token_count as len(content) // 4.
        """
        try:
            from src.core.chat_history import get_chat_history
            _hist = get_chat_history()
            _conv_id = self._current_conversation_id
            if not _conv_id:
                return

            # Ensure conversation record exists (legacy path)
            _hist.create_conversation(
                project_path=getattr(self, '_project_root', '') or '',
                title=f"Chat {_conv_id[:8]}",
                conversation_id=_conv_id,
            )

            # Save message immediately (bypasses debounce queue) - legacy path
            _token_count = len(content) // 4 if content else 0
            _hist.add_message(
                conversation_id=_conv_id,
                role=role,
                content=content,
                metadata={'token_count': _token_count},
                immediate=True,
            )

            # ── NEW: Also save to sessions/messages tables ──
            _session_id = get_session_id()
            _project_id = get_project_root() or ''
            try:
                _db = _hist.db
                _db.create_session(_session_id, _project_id)
                _db.add_chat_message(
                    session_id=_session_id,
                    project_id=_project_id,
                    role=role,
                    content=content,
                    token_count=_token_count,
                )
            except Exception as _ce:
                log.debug(f"[BRIDGE] Session message save skipped: {_ce}")

            log.debug(f"[BRIDGE] Immediate save: {role} message ({_token_count} tokens) to {_conv_id}")
        except Exception as e:
            log.warning(f"[BRIDGE] Immediate save failed: {e}")

    # ============================================================
    # PROACTIVE CONTEXT WINDOW MONITORING (Task 4)
    # ============================================================

    def _check_context_window_pressure(self) -> None:
        """Check if conversation history is approaching the context window limit.

        Called after every AI response. Uses the existing autoCompact system
        (agent.src.services.compact.autoCompact) for threshold calculation.
        The autoCompact module uses model-aware thresholds with proper buffer
        tokens (13K for auto-compact, 3K for manual) rather than a hard 85%.

        Emits context_budget_update signal for the UI token usage bar.
        """
        try:
            with self._history_lock:
                _hist = list(self._conversation_history)

            if not _hist:
                return

            # Estimate total tokens in history
            _total_tokens = sum(len(m.content or '') // 4 for m in _hist)

            # Get model context window
            _model_id = getattr(self, '_current_model_id', None)
            if not _model_id:
                return

            from src.ai.model_limits import get_model_limits
            _limits = get_model_limits(_model_id)
            _ctx_window = _limits.context_window

            # ── Use existing autoCompact system for threshold ──────────
            # The autoCompact module calculates: effectiveWindow - 13K buffer
            # This is more sophisticated than a hard 85% and matches the
            # production compaction system used by the agent runtime.
            _threshold = getAutoCompactThreshold(_model_id)
            _warning_state = calculateTokenWarningState(_total_tokens, _model_id)
            _auto_enabled = isAutoCompactEnabled()

            # Emit budget update for UI token bar
            self._safe_emit(
                self.context_budget_update,
                _total_tokens,
                _ctx_window,
                _model_id,
            )

            # Proactive compact using autoCompact threshold
            _should_compact = (
                _auto_enabled
                and _total_tokens >= _threshold
                and len(_hist) > 45
            )
            if _should_compact:
                log.warning(
                    f"[BRIDGE] Context pressure: {_total_tokens:,} tokens "
                    f"(threshold={_threshold:,}, window={_ctx_window:,}, "
                    f"ratio={_total_tokens / max(1, _ctx_window):.1%}) — triggering proactive compact"
                )
                self._safe_emit(
                    self.agent_status_update,
                    'compacting',
                    f'Context window {_total_tokens // 1000}K / {_ctx_window // 1000}K tokens '
                    f'— auto-compacting to prevent overflow...'
                )
                # Trigger compaction on the in-memory history
                from src.ai.providers import ChatMessage as PCM
                _compacted = self._compact_messages(_hist, PCM)
                log.info(
                    f"[BRIDGE] Proactive compact complete: {len(_hist)} \u2192 {len(_compacted)} messages"
                )
                self._safe_emit(
                    self.agent_status_update,
                    'ready',
                    f'Conversation compacted — {len(_hist) - len(_compacted)} messages summarized'
                )
        except Exception as e:
            log.debug(f"[BRIDGE] Context pressure check failed: {e}")

    def save_conversation_to_db(self) -> bool:
        """Save conversation history directly to SQLite (bypasses JS/Chromium).

        Used as fallback when Chromium renderer has crashed and
        run_javascript() can't execute saveProjectChats().

        CRITICAL: This method must NOT overwrite or duplicate messages that
        were already saved by the JS saveProjectChats() → save_chats_to_sqlite
        pipeline. The JS save stores messages from chat.messages (correct roles),
        while this method pulls from _conversation_history which may have
        extra system/tool messages or different content ordering.

        Strategy:
        1. If messages already exist in DB for this conversation → SKIP
           (JS save already persisted correct data)
        2. If DB is empty → clear + save _conversation_history as fallback

        Returns True if save succeeded.
        """
        try:
            from src.core.chat_history import get_chat_history
            _hist = get_chat_history()
            _conv_id = getattr(self, '_current_conversation_id', None)
            if not _conv_id:
                log.debug("[BRIDGE] save_conversation_to_db: no active conversation ID")
                return False

            # ── GUARD: Don't overwrite JS-saved messages ──────────────────
            # The JS saveProjectChats() path stores messages from chat.messages
            # with correct roles. If that already ran, our fallback would only
            # corrupt the DB by adding duplicate/wrongly-roled messages.
            _existing_count = _hist.db.get_message_count(_conv_id)
            if _existing_count > 0:
                log.debug(
                    f"[BRIDGE] save_conversation_to_db: SKIPPING — "
                    f"{_existing_count} messages already exist from JS save"
                )
                return True  # Not a failure — JS already saved correctly

            # Build message list from _conversation_history
            _msgs = []
            for cm in self._conversation_history:
                if cm.role in ('user', 'assistant'):
                    _meta: dict = {}
                    _rc = getattr(cm, 'reasoning_content', None)
                    if _rc:
                        _meta['reasoning_content'] = _rc
                    _tc = getattr(cm, 'tool_calls', None)
                    if _tc:
                        _meta['tool_calls'] = _tc
                    _tcid = getattr(cm, 'tool_call_id', None)
                    if _tcid:
                        _meta['tool_call_id'] = _tcid
                    _msgs.append({
                        'role': cm.role,
                        'content': cm.content or '',
                        'metadata': _meta if _meta else None,
                    })
            if not _msgs:
                return False
            # Save directly to DB
            _hist.create_conversation(
                project_path=getattr(self, '_project_root', '') or '',
                title=f"Chat {_conv_id[:8]}",
                conversation_id=_conv_id,
            )
            # Clear any stale/partial data before fallback save
            if _existing_count == 0:
                try:
                    _hist.clear_conversation_messages(_conv_id)
                except Exception:
                    pass
            for msg in _msgs:
                _hist.add_message(
                    conversation_id=_conv_id,
                    role=msg['role'],
                    content=msg['content'],
                    metadata=msg['metadata'],
                    immediate=True,
                )
            _hist.db.flush_write_queue(force=True)
            log.info(f"[BRIDGE] Direct DB save (fallback): {len(_msgs)} messages for conversation {_conv_id}")
            return True
        except Exception as e:
            log.warning(f"[BRIDGE] Direct DB save failed: {e}")
            return False

    def _persist_chat_async(self) -> None:
        """
        Non-blocking chat persistence to SQLite.

        Saves the current conversation to the database immediately after
        each AI response. This ensures chats survive ANY crash:
        - Chromium renderer crash
        - Power loss
        - IDE force-kill
        - Windows session end

        Uses a background thread with a 2-second debounce to avoid
        excessive DB writes during rapid conversation turns.
        """
        import threading as _threading

        # Debounce: skip if a persist is already scheduled
        if hasattr(self, '_persist_timer') and self._persist_timer is not None:
            if self._persist_timer.is_alive():
                return  # Already scheduled

        def _do_persist():
            try:
                time.sleep(2.0)  # Debounce: wait for turn to complete
                self.save_conversation_to_db()
            except Exception as e:
                log.debug(f"[BRIDGE] Async persist failed: {e}")

        self._persist_timer = _threading.Thread(
            target=_do_persist,
            daemon=True,
            name="ChatPersist",
        )
        self._persist_timer.start()

    @staticmethod
    def _extract_semantic_summary(checkpoint_text: str, max_chars: int = 500) -> str:
        """
        Extract a concise summary from a context checkpoint for semantic storage.

        Pulls the Conversation Digest section (most recent exchanges) which
        gives the best snapshot of what happened in the session.
        """
        # Try to find the Conversation Digest section
        digest_marker = "**Conversation Digest:**"
        digest_idx = checkpoint_text.find(digest_marker)
        if digest_idx >= 0:
            return checkpoint_text[digest_idx + len(digest_marker):].strip()[:max_chars]

        # Fallback: first non-empty lines up to max_chars
        lines = [l.strip() for l in checkpoint_text.split("\n") if l.strip()]
        return " | ".join(lines[:10])[:max_chars]


# ============================================================
# SKILLS & RULES INTEGRATION
# ============================================================

# Module-level cache for project root (set by init_skills_and_rules)
_project_root_cache: Optional[str] = None

def init_skills_and_rules(project_root: Optional[str] = None) -> None:
    """Initialize SkillsManager, RulesManager, and Cortex Project Context.

    Call this once at startup after project root is determined.
    All managers are singletons and will be lazily initialized.
    """
    global _project_root_cache
    _project_root_cache = project_root

    if _HAS_SKILLS:
        sm = get_skills_manager(project_root=project_root)
        log.info(f"[BRIDGE] SkillsManager initialized: {sm.skill_count()} skills loaded")
    if _HAS_RULES:
        rm = get_rules_manager(project_root=project_root)
        log.info(f"[BRIDGE] RulesManager initialized: {rm.rule_count()} rules loaded")
    if _HAS_CORTEX_PROJECT_CTX and project_root:
        summary = get_cortex_context_summary(project_root)
        if summary.get("exists"):
            loaded = [k for k, v in summary.get("files", {}).items() if v is not None]
            log.info(f"[BRIDGE] Cortex project context loaded: {', '.join(loaded)}")


def get_active_skills_prompt_block() -> str:
    """Get the system prompt injection for currently active skills.

    Returns empty string if no skills are active or SkillsManager unavailable.
    """
    if not _HAS_SKILLS:
        return ""
    try:
        sm = get_skills_manager()
        return sm.get_system_prompt_injection()
    except Exception as e:
        log.warning(f"[BRIDGE] Failed to get skills prompt: {e}")
        return ""


def get_active_rules_prompt_block() -> str:
    """Get the system prompt injection for enabled rules.

    Returns empty string if no rules enabled or RulesManager unavailable.
    """
    if not _HAS_RULES:
        return ""
    try:
        rm = get_rules_manager()
        return rm.get_system_prompt_injection()
    except Exception as e:
        log.warning(f"[BRIDGE] Failed to get rules prompt: {e}")
        return ""


def get_cortex_project_context_block() -> str:
    """Get the .cortex/ project context block for system prompt injection.

    Loads rules.md, context.md, commands.md, ignore.txt, and memory.json
    from the project's .cortex/ directory and formats them as XML blocks.
    Auto-creates .cortex/ directory if it doesn't exist.

    Returns empty string if no .cortex/ context is available.
    """
    if not _HAS_CORTEX_PROJECT_CTX:
        return ""
    try:
        project_root = get_project_root()
        if not project_root:
            return ""
        # Auto-create .cortex/ if missing (first project open)
        ensure_cortex_dir(project_root)
        return load_all_cortex_context(project_root)
    except Exception as e:
        log.warning(f"[BRIDGE] Failed to load cortex project context: {e}")
        return ""


def update_cortex_project_memory(
    entry_type: str = "decisions",
    entry: str = "",
) -> bool:
    """Add an entry to .cortex/memory.json (bridge function).

    Args:
        entry_type: One of "decisions", "known_bugs", "user_preferences", "notes".
        entry: The text to append.

    Returns True if successful, False otherwise.
    """
    if not _HAS_CORTEX_PROJECT_CTX or not entry:
        return False
    try:
        project_root = get_project_root()
        if not project_root:
            return False
        return update_project_memory(project_root, entry_type, entry)
    except Exception as e:
        log.warning(f"[BRIDGE] Failed to update project memory: {e}")
        return False


def get_all_system_prompt_extras() -> str:
    """Get combined system prompt injections from skills + rules + project context.

    Called when building the full system prompt to inject into the LLM.
    Includes:
    - Active skills (SKILL.md files)
    - Enabled rules (AGENTS.md)
    - .cortex/ project context (rules.md, context.md, commands.md, memory.json, ignore.txt)
    """
    parts: List[str] = []
    skills_block = get_active_skills_prompt_block()
    if skills_block:
        parts.append(skills_block)
    rules_block = get_active_rules_prompt_block()
    if rules_block:
        parts.append(rules_block)
    ctx_block = get_cortex_project_context_block()
    if ctx_block:
        parts.append(ctx_block)
    return "\n".join(parts)


def list_skills() -> List[Dict[str, Any]]:
    """List all available skills as dicts for UI display."""
    if not _HAS_SKILLS:
        return []
    try:
        sm = get_skills_manager()
        return [
            {
                "name": s.name,
                "description": s.description,
                "aliases": s.aliases,
                "tags": s.tags,
                "category": s.category,
                "active": s.name in sm.active_skill_names(),
            }
            for s in sm.list_skills()
        ]
    except Exception as e:
        log.warning(f"[BRIDGE] Failed to list skills: {e}")
        return []


def list_rules() -> List[Dict[str, Any]]:
    """List all rules as dicts for UI display."""
    if not _HAS_RULES:
        return []
    try:
        rm = get_rules_manager()
        return [
            {
                "name": r.name,
                "description": r.description,
                "scope": r.scope,
                "priority": r.priority,
                "enabled": r.enabled,
            }
            for r in rm.list_rules()
        ]
    except Exception as e:
        log.warning(f"[BRIDGE] Failed to list rules: {e}")
        return []


def activate_skill(name: str) -> bool:
    """Activate a skill by name. Returns True if successful."""
    if not _HAS_SKILLS:
        return False
    try:
        return get_skills_manager().activate_skill(name)
    except Exception as e:
        log.warning(f"[BRIDGE] Failed to activate skill '{name}': {e}")
        return False


def deactivate_skill(name: str) -> bool:
    """Deactivate a skill by name. Returns True if successful."""
    if not _HAS_SKILLS:
        return False
    try:
        get_skills_manager().deactivate_skill(name)
        return True
    except Exception as e:
        log.warning(f"[BRIDGE] Failed to deactivate skill '{name}': {e}")
        return False


def toggle_skill(name: str) -> Optional[bool]:
    """Toggle a skill on/off. Returns new state (True=active) or None on failure."""
    if not _HAS_SKILLS:
        return None
    try:
        return get_skills_manager().toggle_skill(name)
    except Exception as e:
        log.warning(f"[BRIDGE] Failed to toggle skill '{name}': {e}")
        return None


def toggle_rule(name: str) -> Optional[bool]:
    """Toggle a rule on/off. Returns new state (True=enabled) or None on failure."""
    if not _HAS_RULES:
        return None
    try:
        return get_rules_manager().toggle_rule(name)
    except Exception as e:
        log.warning(f"[BRIDGE] Failed to toggle rule '{name}': {e}")
        return None


def auto_detect_skills(user_input: str, threshold: float = 0.45) -> List[Dict[str, Any]]:
    """Auto-detect relevant skills from user input text.

    Returns skills with relevance score >= threshold, sorted by relevance.
    Used to automatically activate skills based on what the user types.
    """
    if not _HAS_SKILLS:
        return []
    try:
        sm = get_skills_manager()
        return [
            {
                "name": s.name,
                "description": s.description,
                "score": round(s.matches_keywords(user_input), 2),
            }
            for s in sm.auto_detect_skills(user_input, threshold=threshold)
        ]
    except Exception as e:
        log.warning(f"[BRIDGE] Failed to auto-detect skills: {e}")
        return []


def reload_skills() -> int:
    """Reload all skills from disk. Returns count loaded."""
    if not _HAS_SKILLS:
        return 0
    try:
        return get_skills_manager().reload()
    except Exception as e:
        log.warning(f"[BRIDGE] Failed to reload skills: {e}")
        return 0


def reload_rules() -> int:
    """Reload all rules from disk. Returns count loaded."""
    if not _HAS_RULES:
        return 0
    try:
        return get_rules_manager().reload()
    except Exception as e:
        log.warning(f"[BRIDGE] Failed to reload rules: {e}")
        return 0


# ============================================================
# FACTORY (Singleton)
# ============================================================

# ============================================================
# BACKEND TEXT PROCESSING PIPELINE
# These functions clean AI raw output BEFORE it reaches the
# frontend chat UI (aichat.html / marked.js rendering).
# ============================================================

def clean_latex_output(text: str) -> str:
    """
    Simple LaTeX cleanup - normalize delimiters and remove artifacts.
    The AI is prompted to use $...$ and $$...$$ consistently.
    """
    import re
    
    # --- FIX 0: Ensure $$ has whitespace separation from surrounding text ---
    text = re.sub(r'([^\s$])\$\$', r'\1\n\n$$', text)
    text = re.sub(r'\$\$([^\s$])', r'$$\n\n\1', text)
    
    # --- FIX 1: Close unclosed $$ blocks ---
    dd_count = len(re.findall(r'\$\$', text))
    if dd_count % 2 == 1:
        last_dd = text.rfind('$$')
        if last_dd != -1:
            after = text[last_dd + 2:]
            newline_pos = after.find('\n\n')
            if newline_pos == -1:
                newline_pos = after.find('\n')
            if newline_pos != -1:
                insert_at = last_dd + 2 + newline_pos
                text = text[:insert_at] + ' $$' + text[insert_at:]
            else:
                text = text + ' $$'
    
    # --- FIX 2: Balance unclosed braces inside $$ blocks ---
    def _balance_braces(m):
        content = m.group(1)
        depth = 0
        i = 0
        while i < len(content):
            if content[i] == '\\':
                i += 2
                continue
            if content[i] == '{':
                depth += 1
            elif content[i] == '}':
                depth -= 1
            i += 1
        if depth > 0:
            content += '\\cdots' + '}' * depth
        return '$$' + content + '$$'
    text = re.sub(r'\$\$([\s\S]*?)\$\$', _balance_braces, text)
    
    # --- FIX 3: Normalize alternative delimiters ---
    text = re.sub(r'\\\[([\s\S]*?)\\\]', r'\n$$\1$$\n', text)
    text = re.sub(r'\\\(([\s\S]*?)\\\)', r'$\1$', text)
    
    # --- FIX 4: Wrap orphaned LaTeX commands ---
    protected_blocks = {}
    block_counter = 0
    
    def protect_block(match):
        nonlocal block_counter
        key = f"<<<PROTECTED_{block_counter}>>>"
        protected_blocks[key] = match.group(0)
        block_counter += 1
        return key
    
    text = re.sub(r'\$\$([\s\S]*?)\$\$', protect_block, text)
    text = re.sub(r'\$([^\$\n]+?)\$', protect_block, text)
    text = re.sub(r'```[\s\S]*?```', protect_block, text)
    text = re.sub(r'`[^`]+`', protect_block, text)
    
    env_pattern = r'\\begin\{[^}]+\}[\s\S]*?\\end\{[^}]+\}'
    text = re.sub(env_pattern, lambda m: f'${m.group(0)}$', text)
    
    latex_pattern = r'\\[a-zA-Z]+(?:\{(?:[^{}]|\{[^}]*\})*\}|\[[^\]]*\]|_\{[^}]*\}|\^\{[^}]*\})*[^\n]*?(?=\s+(?:is|are|was|were|be|been|being|have|has|had|do|does|did|can|could|will|would|should|may|might|must|shall|important|where|when|what|which|who|why|how|meters|seconds|minutes|hours|degrees|chapter|section|example|note|their|there|these|those|about|after|again|also|back|because|before|between|both|could|down|each|even|every|first|from|good|great|into|just|know|like|look|make|many|more|most|much|must|never|next|only|other|over|people|same|should|some|such|take|than|that|them|then|there|these|they|think|this|those|through|time|under|very|want|well|what|when|where|which|while|will|with|would|year|your|[a-z]{4,})(?![a-zA-Z])|\.| \n|$)'
    
    def wrap_latex(match):
        content = match.group(0).rstrip()
        trailing = match.group(0)[len(content):]
        return f'${content}$' + trailing
    
    text = re.sub(latex_pattern, wrap_latex, text)
    
    for key, value in protected_blocks.items():
        text = text.replace(key, value)
    
    text = re.sub(r'\n\n\n+', '\n\n', text)
    return text.strip()


def fix_malformed_markdown(text: str) -> str:
    """
    Fix malformed markdown bold/italic patterns that break rendering.
    """
    import re
    
    # Fix corrupted table/extraction artifacts
    text = re.sub(r'(\d+)\s*B\s*\|\s*\?', r'\1B', text)
    text = re.sub(r'\|\s*\?', '', text)
    text = re.sub(r'\$\s+(\d)', r'$\1', text)
    
    # Convert ALL italics to bold
    text = re.sub(r'\*([^*\n]+):\*', r'**\1:**', text)
    text = re.sub(r'\*([^*\n]+)\*:', r'**\1**:', text)
    text = re.sub(r'\*([^*\n]+)\*(\s*[\u2013\-])', r'**\1**\2', text)
    text = re.sub(r'(?<!\*)\*([^*\n]+)\*(?!\*)', r'**\1**', text)
    
    # Fix merged words
    text = re.sub(r'(\d+\.?\d*)billionby(\d{4})', r'\1 billion by \2', text, flags=re.IGNORECASE)
    text = re.sub(r'(\d+\.?\d*)billion\s*by(\d{4})', r'\1 billion by \2', text, flags=re.IGNORECASE)
    text = re.sub(r'billionby(\d{4})', r'billion by \1', text, flags=re.IGNORECASE)
    text = re.sub(r'millionby(\d{4})', r'million by \1', text, flags=re.IGNORECASE)
    text = re.sub(r'upfrom', r'up from', text, flags=re.IGNORECASE)
    text = re.sub(r'in(\d{4})and', r'in \1 and ', text, flags=re.IGNORECASE)
    text = re.sub(r'likelyapproached', r'likely approached', text, flags=re.IGNORECASE)
    text = re.sub(r'andlikely', r'and likely ', text, flags=re.IGNORECASE)
    
    # Clean orphan asterisks
    text = re.sub(r'\*\*\s*\*(?!\*)', '**', text)
    text = re.sub(r'(?<!\*)\*\s*\*\*', '**', text)
    text = re.sub(r'\*\s+\*(?!\*)', '', text)
    text = re.sub(r'\*\*\s+\*\*', '', text)
    text = re.sub(r'\*\s+\*([^*]+)\*\s+\*', r'\1', text)
    text = re.sub(r'\s\*\s+\*\s', ' ', text)
    
    # Insert blank line before tables
    text = re.sub(r'([^|\n])\n(\s*\|.*\|)', r'\1\n\n\2', text)
    
    # Clean double spaces outside code blocks
    code_block_pattern = r'(```[\s\S]*?(?:```|$))'
    parts = re.split(code_block_pattern, text)
    
    cleaned_parts = []
    for part in parts:
        if part.startswith('```'):
            cleaned_parts.append(part)
        else:
            part = re.sub(r'([^|\n])\n(\s*\|.*\|)', r'\1\n\n\2', part)
            lines = part.split('\n')
            cleaned_lines = []
            for line in lines:
                indent_match = re.match(r'^(\s*)', line)
                indent = indent_match.group(1) if indent_match else ''
                content = line[len(indent):]
                content = re.sub(r'  +', ' ', content)
                content = re.sub(r'\*\*\s+\*\*', '', content)
                cleaned_lines.append(indent + content)
            cleaned_parts.append('\n'.join(cleaned_lines))
    
    return ''.join(cleaned_parts)


def clean_citation_markers(text: str) -> str:
    """
    Remove inline CITATION_URL markers from AI response content.
    Also applies fix_malformed_markdown as a pre-processing step.
    """
    import re
    
    # First fix malformed markdown from AI
    text = fix_malformed_markdown(text)
    
    # Fix backtick-corrupted URLs
    text = re.sub(r'\[`?([^\]]*?)`?\]\(\s*`(https?://[^`\s)]+)`\s*\)', r'[\1](\2)', text)
    text = re.sub(r'\[([^\]]*)\]\(([^)]*`[^)]*)\)', lambda m: '[' + m.group(1) + '](' + m.group(2).replace('`', '').replace('https:///', 'https://').replace('http:///', 'http://') + ')' if '`' in m.group(2) else m.group(0), text)
    text = re.sub(r'\[([^\]]*)\]\(([^)]*%60[^)]*)\)', lambda m: '[' + m.group(1) + '](' + re.sub(r'(https?://)/*', r'\1', m.group(2).replace('%60', '')) + ')' if '%60' in m.group(2) else m.group(0), text)
    
    # Remove inline CITATION_URL markers
    text = re.sub(r'\[?CITATION_URL:\s*(https?://[^\]\s]+)\]?', '', text)
    text = re.sub(r'\[?INTERNAL_REF_URL_DO_NOT_DISPLAY_INLINE:\s*(https?://[^\]\s]+)\]?', '', text)
    text = re.sub(r'\[?Reference URL for citations only:\s*(https?://[^\]\s]+)\]?', '', text)
    
    # Split sources section from main body
    sources_patterns = [
        r'(## Sources\s*\n)',
        r'(## References\s*\n)',
        r'(## Citations\s*\n)',
        r'(\*\*Sources:?\*\*\s*\n)',
        r'(\*\*References:?\*\*\s*\n)',
    ]
    
    for pattern in sources_patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            main_body = text[:match.start()]
            sources_section = text[match.start():]
            main_body = re.sub(
                r'(?<!\]\()https?://[^\s\)]+(?!=\))',
                '',
                main_body
            )
            main_body = re.sub(r'(?<=\S)  +', ' ', main_body)
            main_body = re.sub(r'\n\n\n+', '\n\n', main_body)
            return main_body.strip() + '\n\n' + sources_section.strip()
    
    text = re.sub(r'\[?CITATION_URL:[^\]\n]+\]?', '', text)
    text = re.sub(r'(?<=\S)  +', ' ', text)
    text = re.sub(r'\n\n\n+', '\n\n', text)
    
    return text.strip()


_AGENT_BRIDGE_INSTANCE: Optional[CortexAgentBridge] = None

def get_agent_bridge(**kwargs: Any) -> Optional[CortexAgentBridge]:
    """Return the singleton CortexAgentBridge instance.

    Creates the instance on first call with kwargs (e.g. file_manager=...).
    Subsequent calls return the same instance — no duplicate bridge init.
    """
    global _AGENT_BRIDGE_INSTANCE
    if _AGENT_BRIDGE_INSTANCE is None:
        _AGENT_BRIDGE_INSTANCE = CortexAgentBridge(**kwargs)
    return _AGENT_BRIDGE_INSTANCE


__all__ = [
    "CortexAgentBridge",
    "get_agent_bridge",
    "ChatMessage",
    "ToolCall",
    "ToolResult",
    # Skills & Rules integration
    "init_skills_and_rules",
    "get_active_skills_prompt_block",
    "get_active_rules_prompt_block",
    "get_cortex_project_context_block",
    "update_cortex_project_memory",
    "get_all_system_prompt_extras",
    "list_skills",
    "list_rules",
    "activate_skill",
    "deactivate_skill",
    "toggle_skill",
    "toggle_rule",
    "auto_detect_skills",
    "reload_skills",
    "reload_rules",
]
