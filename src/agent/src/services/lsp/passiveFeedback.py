"""
services/lsp/passiveFeedback.py
Python conversion of services/lsp/passiveFeedback.ts (329 lines)

Register LSP notification handlers on all servers.
Sets up handlers to listen for textDocument/publishDiagnostics notifications.
"""

from typing import Any, Dict, List, Optional
from urllib.parse import unquote


def _map_lsp_severity(lsp_severity: Optional[int]) -> str:
    """
    Map LSP severity to Claude diagnostic severity.
    
    LSP DiagnosticSeverity enum:
    1 = Error, 2 = Warning, 3 = Information, 4 = Hint
    
    Args:
        lsp_severity: Numeric severity value
        
    Returns:
        Severity string ('Error', 'Warning', 'Info', 'Hint')
    """
    severity_map = {
        1: 'Error',
        2: 'Warning',
        3: 'Info',
        4: 'Hint',
    }
    return severity_map.get(lsp_severity, 'Error')


def format_diagnostics_for_attachment(params: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Convert LSP diagnostics to Claude diagnostic format.
    
    Converts LSP PublishDiagnosticsParams to DiagnosticFile[] format
    used by Claude's attachment system.
    
    Args:
        params: LSP PublishDiagnosticsParams
        
    Returns:
        List of DiagnosticFile dicts
    """
    # Parse URI (may be file:// or plain path) and normalize to file system path
    uri = params.get('uri', '')
    
    try:
        # Handle both file:// URIs and plain paths
        if uri.startswith('file://'):
            # Convert file:// URI to path
            uri = unquote(uri[7:])  # Remove 'file://' prefix and decode
            # Handle Windows paths (file:///C:/...)
            if uri.startswith('/') and len(uri) > 2 and uri[2] == ':':
                uri = uri[1:]  # Remove leading slash for Windows
    except Exception as error:
        
        err = to_error(error)
        log_error(err)
        log_for_debugging(
            f'Failed to convert URI to file path: {uri}. Error: {err}. Using original URI as fallback.'
        )
        # Gracefully fallback to original URI
        uri = params.get('uri', '')
    
    diagnostics = []
    for diag in params.get('diagnostics', []):
        diagnostics.append({
            'message': diag.get('message', ''),
            'severity': _map_lsp_severity(diag.get('severity')),
            'range': {
                'start': {
                    'line': diag['range']['start']['line'],
                    'character': diag['range']['start']['character'],
                },
                'end': {
                    'line': diag['range']['end']['line'],
                    'character': diag['range']['end']['character'],
                },
            },
            'source': diag.get('source'),
            'code': str(diag['code']) if diag.get('code') is not None else None,
        })
    
    return [
        {
            'uri': uri,
            'diagnostics': diagnostics,
        }
    ]


def register_lsp_notification_handlers(manager: Any) -> Dict[str, Any]:
    """
    Register LSP notification handlers on all servers.
    
    Sets up handlers to listen for textDocument/publishDiagnostics notifications
    from all LSP servers and routes them to Claude's diagnostic system.
    
    Args:
        manager: LSP server manager instance
        
    Returns:
        Tracking data for registration status and runtime failures
    """
    # Register handlers on all configured servers to capture diagnostics from any language
    servers = manager.get_all_servers()
    
    # Track partial failures - allow successful server registrations even if some fail
    registration_errors: List[Dict[str, str]] = []
    success_count = 0
    
    # Track consecutive failures per server to warn users after 3+ failures
    diagnostic_failures: Dict[str, Dict[str, Any]] = {}
    
    for server_name, server_instance in servers.items():
        try:
            # Validate server instance has on_notification method
            if not server_instance or not hasattr(server_instance, 'on_notification'):
                error_msg = (
                    'Server instance is null/undefined'
                    if not server_instance
                    else 'Server instance has no on_notification method'
                )
                
                registration_errors.append({
                    'serverName': server_name,
                    'error': error_msg,
                })
                
                
                log_error(Exception(f'{error_msg} for {server_name}'))
                log_for_debugging(f'Skipping handler registration for {server_name}: {error_msg}')
                continue  # Skip this server but track the failure
            
            # Errors are isolated to avoid breaking other servers
            # Pass diagnostic_failures dict to handler for cross-invocation tracking (closure pattern)
            def create_handler(s_name: str, failures_dict: Dict[str, Dict[str, Any]]):
                """Create a notification handler for a specific server."""
                def handler(params: Any):
                    
                    log_for_debugging(
                        f'[PASSIVE DIAGNOSTICS] Handler invoked for {s_name}! Params type: {type(params)}'
                    )
                    try:
                        # Validate params structure before casting
                        if (
                            not params
                            or not isinstance(params, dict)
                            or 'uri' not in params
                            or 'diagnostics' not in params
                        ):
                            import json
                            
                            err = Exception(
                                f'LSP server {s_name} sent invalid diagnostic params (missing uri or diagnostics)'
                            )
                            log_error(err)
                            log_for_debugging(
                                f'Invalid diagnostic params from {s_name}: {json.dumps(params, default=str)}'
                            )
                            return
                        
                        log_for_debugging(
                            f'Received diagnostics from {s_name}: {len(params["diagnostics"])} diagnostic(s) for {params["uri"]}'
                        )
                        
                        # Convert LSP diagnostics to Claude format (can throw on invalid URIs)
                        diagnostic_files = format_diagnostics_for_attachment(params)
                        
                        # Only send notification if there are diagnostics
                        first_file = diagnostic_files[0] if diagnostic_files else None
                        if (
                            not first_file
                            or len(diagnostic_files) == 0
                            or len(first_file.get('diagnostics', [])) == 0
                        ):
                            log_for_debugging(
                                f'Skipping empty diagnostics from {s_name} for {params["uri"]}'
                            )
                            return
                        
                        # Register diagnostics for async delivery via attachment system
                        try:
                            from services.lsp.lsPDiagnosticRegistry import register_pending_lsp_diagnostic
                            
                            register_pending_lsp_diagnostic(
                                server_name=s_name,
                                files=diagnostic_files,
                            )
                            
                            log_for_debugging(
                                f'LSP Diagnostics: Registered {len(diagnostic_files)} diagnostic file(s) from {s_name} for async delivery'
                            )
                            
                            # Success - reset failure counter for this server
                            if s_name in failures_dict:
                                del failures_dict[s_name]
                                
                        except Exception as error:
                            
                            err = to_error(error)
                            log_error(err)
                            log_for_debugging(
                                f'Error registering LSP diagnostics from {s_name}: '
                                f'URI: {params["uri"]}, '
                                f'Diagnostic count: {len(first_file["diagnostics"])}, '
                                f'Error: {err}'
                            )
                            
                            # Track consecutive failures and warn after 3+
                            if s_name not in failures_dict:
                                failures_dict[s_name] = {'count': 0, 'lastError': ''}
                            failures = failures_dict[s_name]
                            failures['count'] += 1
                            failures['lastError'] = str(err)
                            
                            if failures['count'] >= 3:
                                log_for_debugging(
                                    f'WARNING: LSP diagnostic handler for {s_name} has failed {failures["count"]} times consecutively. '
                                    f'Last error: {failures["lastError"]}. '
                                    f'This may indicate a problem with the LSP server or diagnostic processing. '
                                    f'Check logs for details.'
                                )
                    except Exception as error:
                        # Catch any unexpected errors from the entire handler to prevent breaking the notification loop
                        
                        err = to_error(error)
                        log_error(err)
                        log_for_debugging(
                            f'Unexpected error processing diagnostics from {s_name}: {err}'
                        )
                        
                        # Track consecutive failures and warn after 3+
                        if s_name not in failures_dict:
                            failures_dict[s_name] = {'count': 0, 'lastError': ''}
                        failures = failures_dict[s_name]
                        failures['count'] += 1
                        failures['lastError'] = str(err)
                        
                        if failures['count'] >= 3:
                            log_for_debugging(
                                f'WARNING: LSP diagnostic handler for {s_name} has failed {failures["count"]} times consecutively. '
                                f'Last error: {failures["lastError"]}. '
                                f'This may indicate a problem with the LSP server or diagnostic processing. '
                                f'Check logs for details.'
                            )
                        # Don't re-throw - isolate errors to this server only
                
                return handler
            
            server_instance.on_notification(
                'textDocument/publishDiagnostics',
                create_handler(server_name, diagnostic_failures),
            )
            
            log_for_debugging(f'Registered diagnostics handler for {server_name}')
            success_count += 1
            
        except Exception as error:
            
            err = to_error(error)
            registration_errors.append({
                'serverName': server_name,
                'error': str(err),
            })
            
            log_error(err)
            log_for_debugging(
                f'Failed to register diagnostics handler for {server_name}: '
                f'Error: {err}'
            )
    
    # Report overall registration status
    total_servers = len(servers)
    if len(registration_errors) > 0:
        failed_servers = ', '.join(
            f'{e["serverName"]} ({e["error"]})' for e in registration_errors
        )
        # Log aggregate failures for tracking
        
        log_error(
            Exception(f'Failed to register diagnostics for {len(registration_errors)} LSP server(s): {failed_servers}')
        )
        log_for_debugging(
            f'LSP notification handler registration: {success_count}/{total_servers} succeeded. '
            f'Failed servers: {failed_servers}. '
            f'Diagnostics from failed servers will not be delivered.'
        )
    else:
        log_for_debugging(
            f'LSP notification handlers registered successfully for all {total_servers} server(s)'
        )
    
    # Return tracking data for monitoring and testing
    return {
        'totalServers': total_servers,
        'successCount': success_count,
        'registrationErrors': registration_errors,
        'diagnosticFailures': diagnostic_failures,
    }


__all__ = [
    'format_diagnostics_for_attachment',
    'register_lsp_notification_handlers',
]
