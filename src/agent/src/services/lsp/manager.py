"""
services/lsp/manager.py
Python conversion of services/lsp/manager.ts (290 lines)

Singleton LSP server manager initialization.
Manages async initialization with generation counter for invalidation.
"""

import asyncio
from typing import Any, Dict, Optional


# Type aliases
InitializationState = str  # 'not-started' | 'pending' | 'success' | 'failed'


# Global singleton instance of the LSP server manager
_lsp_manager_instance: Optional[Any] = None

# Current initialization state
_initialization_state: InitializationState = 'not-started'

# Error from last initialization attempt, if any
_initialization_error: Optional[Exception] = None

# Generation counter to prevent stale initialization promises from updating state
_initialization_generation = 0

# Promise that resolves when initialization completes (success or failure)
_initialization_task: Optional[asyncio.Task] = None


def reset_lsp_manager_for_testing() -> None:
    """
    Test-only sync reset.
    Clears the module-scope singleton state so reinitialize_lsp_server_manager() 
    early-returns on 'not-started' in downstream tests.
    """
    global _initialization_state, _initialization_error, _initialization_task, _initialization_generation
    
    _initialization_state = 'not-started'
    _initialization_error = None
    _initialization_task = None
    _initialization_generation += 1


def get_lsp_server_manager() -> Optional[Any]:
    """
    Get the singleton LSP server manager instance.
    Returns None if not yet initialized, initialization failed, or still pending.
    """
    # Don't return a broken instance if initialization failed
    if _initialization_state == 'failed':
        return None
    return _lsp_manager_instance


def get_initialization_status() -> Dict[str, Any]:
    """
    Get the current initialization status of the LSP server manager.
    
    Returns:
        Status dict with current state and error (if failed)
    """
    if _initialization_state == 'failed':
        return {
            'status': 'failed',
            'error': _initialization_error or Exception('Initialization failed'),
        }
    if _initialization_state == 'not-started':
        return {'status': 'not-started'}
    if _initialization_state == 'pending':
        return {'status': 'pending'}
    return {'status': 'success'}


def is_lsp_connected() -> bool:
    """
    Check whether at least one language server is connected and healthy.
    Backs LSPTool.isEnabled().
    """
    if _initialization_state == 'failed':
        return False
    
    manager = get_lsp_server_manager()
    if not manager:
        return False
    
    servers = manager.get_all_servers()
    if len(servers) == 0:
        return False
    
    for server in servers.values():
        if server.state != 'error':
            return True
    
    return False


async def wait_for_initialization() -> None:
    """
    Wait for LSP server manager initialization to complete.
    
    Returns immediately if initialization has already completed (success or failure).
    If initialization is pending, waits for it to complete.
    If initialization hasn't started, returns immediately.
    """
    # If already initialized or failed, return immediately
    if _initialization_state in ('success', 'failed'):
        return
    
    # If pending and we have a task, wait for it
    if _initialization_state == 'pending' and _initialization_task:
        try:
            await _initialization_task
        except Exception:
            pass  # Errors are already handled in the task


def initialize_lsp_server_manager() -> None:
    """
    Initialize the LSP server manager singleton.
    
    This function is called during Claude Code startup. It synchronously creates
    the manager instance, then starts async initialization (loading LSP configs)
    in the background without blocking the startup process.
    
    Safe to call multiple times - will only initialize once (idempotent).
    However, if initialization previously failed, calling again will retry.
    """
    from services.utils.env_utils import is_bare_mode
    
    # --bare / SIMPLE: no LSP. LSP is for editor integration (diagnostics,
    # hover, go-to-def in the REPL). Scripted -p calls have no use for it.
    if is_bare_mode():
        return
    
    log_for_debugging('[LSP MANAGER] initialize_lsp_server_manager() called')
    
    # Skip if already initialized or currently initializing
    global _lsp_manager_instance, _initialization_state, _initialization_error
    global _initialization_task, _initialization_generation
    
    if _lsp_manager_instance is not None and _initialization_state != 'failed':
        log_for_debugging('[LSP MANAGER] Already initialized or initializing, skipping')
        return
    
    # Reset state for retry if previous initialization failed
    if _initialization_state == 'failed':
        _lsp_manager_instance = None
        _initialization_error = None
    
    # Create the manager instance and mark as pending
    from services.lsp.lsp_server_manager import create_lsp_server_manager
    
    _lsp_manager_instance = create_lsp_server_manager()
    _initialization_state = 'pending'
    log_for_debugging('[LSP MANAGER] Created manager instance, state=pending')
    
    # Increment generation to invalidate any pending initializations
    current_generation = _initialization_generation + 1
    _initialization_generation = current_generation
    
    log_for_debugging(
        f'[LSP MANAGER] Starting async initialization (generation {current_generation})'
    )
    
    # Start initialization asynchronously without blocking
    async def async_init():
        """Async initialization task."""
        global _initialization_state, _initialization_error, _lsp_manager_instance
        
        try:
            await _lsp_manager_instance.initialize()
            
            # Only update state if this is still the current initialization
            if current_generation == _initialization_generation:
                _initialization_state = 'success'
                log_for_debugging('LSP server manager initialized successfully')
                
                # Register passive notification handlers for diagnostics
                if _lsp_manager_instance:
                    from services.lsp.passiveFeedback import register_lsp_notification_handlers
                    register_lsp_notification_handlers(_lsp_manager_instance)
                    
        except Exception as error:
            # Only update state if this is still the current initialization
            if current_generation == _initialization_generation:
                
                _initialization_state = 'failed'
                _initialization_error = error
                # Clear the instance since it's not usable
                _lsp_manager_instance = None
                
                log_error(error)
                log_for_debugging(
                    f'Failed to initialize LSP server manager: {error_message(error)}'
                )
    
    _initialization_task = asyncio.create_task(async_init())


def reinitialize_lsp_server_manager() -> None:
    """
    Force re-initialization of the LSP server manager, even after a prior
    successful init. Called from refresh_active_plugins() after plugin caches
    are cleared, so newly-loaded plugin LSP servers are picked up.
    
    Fixes https://github.com/anthropics/claude-code/issues/15521:
    load_all_plugins() is memoized and can be called very early in startup
    (via get_commands prefetch in setup.ts) before marketplaces are reconciled,
    caching an empty plugin list. initialize_lsp_server_manager() then reads that
    stale memoized result and initializes with 0 servers. Unlike commands/agents/
    hooks/MCP, LSP was never re-initialized on plugin refresh.
    
    Safe to call when no LSP plugins changed: initialize() is just config
    parsing (servers are lazy-started on first use). Also safe during pending
    init: the generation counter invalidates the in-flight promise.
    """
    
    global _lsp_manager_instance, _initialization_state, _initialization_error
    
    if _initialization_state == 'not-started':
        # initialize_lsp_server_manager() was never called (e.g. headless subcommand
        # path). Don't start it now.
        return
    
    log_for_debugging('[LSP MANAGER] reinitialize_lsp_server_manager() called')
    
    # Best-effort shutdown of any running servers on the old instance so
    # /reload-plugins doesn't leak child processes. Fire-and-forget: the
    # primary use case (issue #15521) has 0 servers so this is usually a no-op.
    if _lsp_manager_instance:
        async def shutdown_old():
            try:
                await _lsp_manager_instance.shutdown()
            except Exception as err:
                from services.utils.errors import error_message
                from services.utils.debug import log_for_debugging
                log_for_debugging(
                    f'[LSP MANAGER] old instance shutdown during reinit failed: {error_message(err)}'
                )
        
        asyncio.create_task(shutdown_old())
    
    # Force the idempotence check in initialize_lsp_server_manager() to fall
    # through. Generation counter handles invalidating any in-flight init.
    _lsp_manager_instance = None
    _initialization_state = 'not-started'
    _initialization_error = None
    
    initialize_lsp_server_manager()


async def shutdown_lsp_server_manager() -> None:
    """
    Shutdown the LSP server manager and clean up resources.
    
    This should be called during Claude Code shutdown. Stops all running LSP servers
    and clears internal state. Safe to call when not initialized (no-op).
    
    NOTE: Errors during shutdown are logged for monitoring but NOT propagated to the caller.
    State is always cleared even if shutdown fails, to prevent resource accumulation.
    """
    
    global _lsp_manager_instance, _initialization_state, _initialization_error
    global _initialization_task, _initialization_generation
    
    if _lsp_manager_instance is None:
        return
    
    try:
        await _lsp_manager_instance.shutdown()
        log_for_debugging('LSP server manager shut down successfully')
    except Exception as error:
        log_error(error)
        log_for_debugging(f'Failed to shutdown LSP server manager: {error_message(error)}')
    finally:
        # Always clear state even if shutdown failed
        _lsp_manager_instance = None
        _initialization_state = 'not-started'
        _initialization_error = None
        _initialization_task = None
        # Increment generation to invalidate any pending initializations
        _initialization_generation += 1


__all__ = [
    'reset_lsp_manager_for_testing',
    'get_lsp_server_manager',
    'get_initialization_status',
    'is_lsp_connected',
    'wait_for_initialization',
    'initialize_lsp_server_manager',
    'reinitialize_lsp_server_manager',
    'shutdown_lsp_server_manager',
]
