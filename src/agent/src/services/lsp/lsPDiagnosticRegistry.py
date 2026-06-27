"""
services/lsp/lsPDiagnosticRegistry.py
Python conversion of services/lsp/LSPDiagnosticRegistry.ts (387 lines)

Stores LSP diagnostics received asynchronously from LSP servers.
Follows same pattern as async hook registry for consistent async attachment delivery.
"""

import time
import uuid
from typing import Any, Dict, List, Optional, Set
from collections import OrderedDict


# Volume limiting constants
MAX_DIAGNOSTICS_PER_FILE = 10
MAX_TOTAL_DIAGNOSTICS = 30

# Max files to track for deduplication - prevents unbounded memory growth
MAX_DELIVERED_FILES = 500


# Type aliases
DiagnosticFile = Dict[str, Any]


class PendingLSPDiagnostic:
    """Pending LSP diagnostic notification."""
    
    def __init__(self, server_name: str, files: List[DiagnosticFile]):
        self.server_name = server_name
        self.files = files
        self.timestamp = 0  # Will be set on creation
        self.attachment_sent = False


class LRUCache(OrderedDict):
    """Simple LRU cache for diagnostic deduplication."""
    
    def __init__(self, max_size: int = MAX_DELIVERED_FILES):
        super().__init__()
        self.max_size = max_size
    
    def __setitem__(self, key, value):
        if key in self:
            self.move_to_end(key)
        super().__setitem__(key, value)
        if len(self) > self.max_size:
            self.popitem(last=False)


# Global registry state
_pending_diagnostics: Dict[str, PendingLSPDiagnostic] = {}

# Cross-turn deduplication: tracks diagnostics that have been delivered
# Maps file URI to a set of diagnostic keys (hash of message+severity+range)
_delivered_diagnostics = LRUCache(MAX_DELIVERED_FILES)


def register_pending_lsp_diagnostic(
    server_name: str,
    files: List[DiagnosticFile],
) -> None:
    """
    Register LSP diagnostics received from a server.
    These will be delivered as attachments in the next query.
    
    Args:
        server_name: Name of LSP server that sent diagnostics
        files: Diagnostic files to deliver
    """
    # Use UUID for guaranteed uniqueness (handles rapid registrations)
    diagnostic_id = str(uuid.uuid4())
    
    log_for_debugging(
        f'LSP Diagnostics: Registering {len(files)} diagnostic file(s) from {server_name} (ID: {diagnostic_id})'
    )
    
    diagnostic = PendingLSPDiagnostic(server_name, files)
    diagnostic.timestamp = int(time.time() * 1000)
    _pending_diagnostics[diagnostic_id] = diagnostic


def _severity_to_number(severity: Optional[str]) -> int:
    """
    Maps severity string to numeric value for sorting.
    Error=1, Warning=2, Info=3, Hint=4
    """
    severity_map = {
        'Error': 1,
        'Warning': 2,
        'Info': 3,
        'Hint': 4,
    }
    return severity_map.get(severity, 4)


def _create_diagnostic_key(diag: Dict[str, Any]) -> str:
    """
    Creates a unique key for a diagnostic based on its content.
    Used for both within-batch and cross-turn deduplication.
    """
    import json
    
    key_data = {
        'message': diag.get('message'),
        'severity': diag.get('severity'),
        'range': diag.get('range'),
        'source': diag.get('source'),
        'code': diag.get('code'),
    }
    return json.dumps(key_data, sort_keys=True)


def _deduplicate_diagnostic_files(all_files: List[DiagnosticFile]) -> List[DiagnosticFile]:
    """
    Deduplicates diagnostics by file URI and diagnostic content.
    Also filters out diagnostics that were already delivered in previous turns.
    """
    # Group diagnostics by file URI
    file_map: Dict[str, Set[str]] = {}
    deduped_files: List[DiagnosticFile] = []
    
    for file in all_files:
        uri = file.get('uri', '')
        if uri not in file_map:
            file_map[uri] = set()
            deduped_files.append({'uri': uri, 'diagnostics': []})
        
        seen_diagnostics = file_map[uri]
        deduped_file = next(f for f in deduped_files if f['uri'] == uri)
        
        # Get previously delivered diagnostics for this file (for cross-turn dedup)
        previously_delivered = _delivered_diagnostics.get(uri, set())
        
        for diag in file.get('diagnostics', []):
            try:
                key = _create_diagnostic_key(diag)
                
                # Skip if already seen in this batch OR already delivered in previous turns
                if key in seen_diagnostics or key in previously_delivered:
                    continue
                
                seen_diagnostics.add(key)
                deduped_file['diagnostics'].append(diag)
            except Exception as error:
                
                err = to_error(error)
                truncated_message = diag.get('message', '<no message>')[:100]
                log_error(
                    Exception(
                        f'Failed to deduplicate diagnostic in {uri}: {err}. '
                        f'Diagnostic message: {truncated_message}'
                    )
                )
                # Include the diagnostic anyway to avoid losing information
                deduped_file['diagnostics'].append(diag)
    
    # Filter out files with no diagnostics after deduplication
    return [f for f in deduped_files if len(f['diagnostics']) > 0]


def check_for_lsp_diagnostics() -> List[Dict[str, Any]]:
    """
    Get all pending LSP diagnostics that haven't been delivered yet.
    Deduplicates diagnostics to prevent sending the same diagnostic multiple times.
    Marks diagnostics as sent to prevent duplicate delivery.
    
    Returns:
        List of pending diagnostics ready for delivery (deduplicated)
    """
    
    log_for_debugging(
        f'LSP Diagnostics: Checking registry - {len(_pending_diagnostics)} pending'
    )
    
    # Collect all diagnostic files from all pending notifications
    all_files: List[DiagnosticFile] = []
    server_names = set()
    diagnostics_to_mark = []
    
    for diagnostic in _pending_diagnostics.values():
        if not diagnostic.attachment_sent:
            all_files.extend(diagnostic.files)
            server_names.add(diagnostic.server_name)
            diagnostics_to_mark.append(diagnostic)
    
    if len(all_files) == 0:
        return []
    
    # Deduplicate diagnostics across all files
    try:
        deduped_files = _deduplicate_diagnostic_files(all_files)
    except Exception as error:
        from services.utils.errors import to_error
        from services.utils.log import log_error
        
        err = to_error(error)
        log_error(Exception(f'Failed to deduplicate LSP diagnostics: {err}'))
        # Fall back to undedup'd files to avoid losing diagnostics
        deduped_files = all_files
    
    # Only mark as sent AFTER successful deduplication, then delete from map
    for diagnostic in diagnostics_to_mark:
        diagnostic.attachment_sent = True
    
    # Remove sent diagnostics from pending
    sent_ids = [
        id for id, diagnostic in _pending_diagnostics.items()
        if diagnostic.attachment_sent
    ]
    for id in sent_ids:
        del _pending_diagnostics[id]
    
    original_count = sum(len(f['diagnostics']) for f in all_files)
    deduped_count = sum(len(f['diagnostics']) for f in deduped_files)
    
    if original_count > deduped_count:
        log_for_debugging(
            f'LSP Diagnostics: Deduplication removed {original_count - deduped_count} duplicate diagnostic(s)'
        )
    
    # Apply volume limiting: cap per file and total
    total_diagnostics = 0
    truncated_count = 0
    
    for file in deduped_files:
        # Sort by severity (Error=1 < Warning=2 < Info=3 < Hint=4) to prioritize errors
        file['diagnostics'].sort(
            key=lambda d: _severity_to_number(d.get('severity'))
        )
        
        # Cap per file
        if len(file['diagnostics']) > MAX_DIAGNOSTICS_PER_FILE:
            truncated_count += len(file['diagnostics']) - MAX_DIAGNOSTICS_PER_FILE
            file['diagnostics'] = file['diagnostics'][:MAX_DIAGNOSTICS_PER_FILE]
        
        # Cap total
        remaining_capacity = MAX_TOTAL_DIAGNOSTICS - total_diagnostics
        if len(file['diagnostics']) > remaining_capacity:
            truncated_count += len(file['diagnostics']) - remaining_capacity
            file['diagnostics'] = file['diagnostics'][:remaining_capacity]
        
        total_diagnostics += len(file['diagnostics'])
    
    # Filter out files that ended up with no diagnostics after limiting
    deduped_files = [f for f in deduped_files if len(f['diagnostics']) > 0]
    
    if truncated_count > 0:
        log_for_debugging(
            f'LSP Diagnostics: Volume limiting removed {truncated_count} diagnostic(s) '
            f'(max {MAX_DIAGNOSTICS_PER_FILE}/file, {MAX_TOTAL_DIAGNOSTICS} total)'
        )
    
    # Track delivered diagnostics for cross-turn deduplication
    for file in deduped_files:
        uri = file['uri']
        if uri not in _delivered_diagnostics:
            _delivered_diagnostics[uri] = set()
        
        delivered = _delivered_diagnostics[uri]
        for diag in file['diagnostics']:
            try:
                delivered.add(_create_diagnostic_key(diag))
            except Exception as error:
                # Log but continue - failure to track shouldn't prevent delivery
                from services.utils.errors import to_error
                from services.utils.log import log_error
                
                err = to_error(error)
                truncated_message = diag.get('message', '<no message>')[:100]
                log_error(
                    Exception(
                        f'Failed to track delivered diagnostic in {uri}: {err}. '
                        f'Diagnostic message: {truncated_message}'
                    )
                )
    
    final_count = sum(len(f['diagnostics']) for f in deduped_files)
    
    # Return empty if no diagnostics to deliver (all filtered by deduplication)
    if final_count == 0:
        log_for_debugging(
            'LSP Diagnostics: No new diagnostics to deliver (all filtered by deduplication)'
        )
        return []
    
    log_for_debugging(
        f'LSP Diagnostics: Delivering {len(deduped_files)} file(s) with {final_count} '
        f'diagnostic(s) from {len(server_names)} server(s)'
    )
    
    # Return single result with all deduplicated diagnostics
    return [
        {
            'serverName': ', '.join(server_names),
            'files': deduped_files,
        }
    ]


def clear_all_lsp_diagnostics() -> None:
    """
    Clear all pending diagnostics.
    Used during cleanup/shutdown or for testing.
    Note: Does NOT clear delivered_diagnostics - that's for cross-turn deduplication
    """
    
    log_for_debugging(
        f'LSP Diagnostics: Clearing {len(_pending_diagnostics)} pending diagnostic(s)'
    )
    _pending_diagnostics.clear()


def reset_all_lsp_diagnostic_state() -> None:
    """
    Reset all diagnostic state including cross-turn tracking.
    Used on session reset or for testing.
    """
    
    log_for_debugging(
        f'LSP Diagnostics: Resetting all state ({len(_pending_diagnostics)} pending, '
        f'{len(_delivered_diagnostics)} files tracked)'
    )
    _pending_diagnostics.clear()
    _delivered_diagnostics.clear()


def clear_delivered_diagnostics_for_file(file_uri: str) -> None:
    """
    Clear delivered diagnostics for a specific file.
    Should be called when a file is edited so that new diagnostics for that file
    will be shown even if they match previously delivered ones.
    
    Args:
        file_uri: URI of the file that was edited
    """
    
    if file_uri in _delivered_diagnostics:
        log_for_debugging(f'LSP Diagnostics: Clearing delivered diagnostics for {file_uri}')
        del _delivered_diagnostics[file_uri]


def get_pending_lsp_diagnostic_count() -> int:
    """Get count of pending diagnostics (for monitoring)"""
    return len(_pending_diagnostics)


__all__ = [
    'register_pending_lsp_diagnostic',
    'check_for_lsp_diagnostics',
    'clear_all_lsp_diagnostics',
    'reset_all_lsp_diagnostic_state',
    'clear_delivered_diagnostics_for_file',
    'get_pending_lsp_diagnostic_count',
]
