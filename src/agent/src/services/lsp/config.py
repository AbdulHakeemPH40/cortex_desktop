"""
services/lsp/config.py
Python conversion of services/lsp/config.ts (80 lines)

Get all configured LSP servers from plugins.
LSP servers are only supported via plugins, not user/project settings.
"""

import asyncio
from typing import Any, Dict, List, Tuple


# Type aliases
PluginError = Dict[str, Any]
ScopedLspServerConfig = Dict[str, Any]


async def get_all_lsp_servers() -> Dict[str, Dict[str, ScopedLspServerConfig]]:
    """
    Get all configured LSP servers from plugins.
    
    Returns:
        Dict containing servers configuration keyed by scoped server name
    """
    all_servers: Dict[str, ScopedLspServerConfig] = {}
    
    try:
        # Get all enabled plugins
        from services.plugins.plugin_loader import load_all_plugins_cache_only
        result = await load_all_plugins_cache_only()
        plugins = result.get('enabled', [])
        
        # Load LSP servers from each plugin in parallel
        # Each plugin is independent — results are merged in original order
        results = await asyncio.gather(
            *[_load_plugin_servers(plugin) for plugin in plugins],
            return_exceptions=True,
        )
        
        for plugin_result in results:
            if isinstance(plugin_result, Exception):
                continue
            
            plugin = plugin_result['plugin']
            scoped_servers = plugin_result['scopedServers']
            errors = plugin_result['errors']
            
            server_count = len(scoped_servers) if scoped_servers else 0
            if server_count > 0:
                # Merge into all servers (already scoped by get_plugin_lsp_servers)
                all_servers.update(scoped_servers)
                
                log_for_debugging(
                    f'Loaded {server_count} LSP server(s) from plugin: {plugin["name"]}'
                )
            
            # Log any errors encountered
            if len(errors) > 0:
                log_for_debugging(
                    f'{len(errors)} error(s) loading LSP servers from plugin: {plugin["name"]}'
                )
        
        log_for_debugging(f'Total LSP servers loaded: {len(all_servers)}')
        
    except Exception as error:
        # Log error for monitoring production issues
        # LSP is optional, so we don't throw - but we need visibility
        
        log_error(to_error(error))
        log_for_debugging(f'Error loading LSP servers: {error_message(error)}')
    
    return {
        'servers': all_servers,
    }


async def _load_plugin_servers(plugin: Dict[str, Any]) -> Dict[str, Any]:
    """Load LSP servers from a single plugin."""
    errors: List[PluginError] = []
    
    try:
        from services.plugins.lsp_plugin_integration import get_plugin_lsp_servers
        scoped_servers = await get_plugin_lsp_servers(plugin, errors)
        return {
            'plugin': plugin,
            'scopedServers': scoped_servers,
            'errors': errors,
        }
    except Exception as e:
        # Defensive: if one plugin throws, don't lose results from others
        log_for_debugging(
            f'Failed to load LSP servers for plugin {plugin["name"]}: {e}',
            {'level': 'error'},
        )
        return {
            'plugin': plugin,
            'scopedServers': None,
            'errors': errors,
        }


__all__ = [
    'get_all_lsp_servers',
]
