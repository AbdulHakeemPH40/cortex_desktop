# LSP Python Conversion - Testing Requirements

> **Install lsprotocol for type safety:** `pip install lsprotocol`

## Conversion Status

| File | TypeScript | Python | Status |
|------|------------|--------|--------|
| config.ts | 80 lines | config.py (111 lines) | ✅ Converted |
| LSPDiagnosticRegistry.ts | 387 lines | lsPDiagnosticRegistry.py (363 lines) | ✅ Converted |
| passiveFeedback.ts | 329 lines | passiveFeedback.py (330 lines) | ✅ Converted |
| manager.ts | 290 lines | manager.py (306 lines) | ✅ Converted |
| LSPClient.ts | 448 lines | lspClient.py (613 lines) | ✅ Converted |
| LSPServerInstance.ts | 512 lines | lspServerInstance.py (472 lines) | ✅ Converted |
| LSPServerManager.ts | 421 lines | lspServerManager.py (432 lines) | ✅ Converted |

**All 7 LSP files converted to Python!**

---

## Type Definitions Approach

### TypeScript (External Types)

```typescript
import type { InitializeParams } from 'vscode-languageserver-protocol'
import type { ServerCapabilities } from 'vscode-languageserver-protocol'
```

### Python Options

| Option | Package | Pros | Cons |
|--------|---------|------|------|
| **Manual** (chosen) | None | No dependencies, lightweight | No IDE autocomplete |
| `lsprotocol` | `pip install lsprotocol` | Official Microsoft types, full LSP spec | ~500KB dependency |
| `pygls` | `pip install pygls` | Full LSP framework | Heavy, opinionated |
| `pyright` | ❌ NOT for types | Language server, not type library | Wrong tool |

### Current Implementation (Manual Types)

```python
# Type aliases
LspCapabilities = Dict[str, Any]
LspInitializeParams = Dict[str, Any]
LspInitializeResult = Dict[str, Any]
LspServerState = str  # 'stopped' | 'starting' | 'running' | 'stopping' | 'error'
ScopedLspServerConfig = Dict[str, Any]
```

### Alternative: Using lsprotocol

If you want proper type checking and IDE autocomplete:

```bash
pip install lsprotocol
```

```python
from lsprotocol.types import InitializeParams, InitializeResult
from lsprotocol.types import ServerCapabilities

# Now you get:
# - IDE autocomplete
# - Type checking
# - Full LSP spec compliance
```

---

## What Has Been Tested

### ✅ Syntax Validation
- All 6 Python files compile without errors (`py_compile`)
- No syntax errors, proper type annotations

### ✅ JSON-RPC Protocol Implementation
```python
# Verified correct Content-Length framing
Content-Length: 40

{"jsonrpc":"2.0","id":1,"method":"test"}
```

### ✅ Module Imports
- `from lspClient import LSPClient, create_lsp_client` works
- Class instantiation works
- Properties accessible (capabilities, is_initialized)

---

## What Needs Testing (After Cortex IDE Setup)

### ❌ 1. Real LSP Server Process Spawning

**Test with actual LSP servers:**

```bash
# Install LSP servers
pip install pyright          # Python LSP
npm install -g typescript-language-server  # TypeScript LSP
npm install -g vscode-langservers-extracted  # CSS, HTML, JSON LSPs
```

**Test code:**
```python
import asyncio
from services.lsp.lspClient import create_lsp_client

async def test_pyright():
    client = create_lsp_client('pyright')
    
    # Should spawn process and connect
    await client.start('pyright', ['--stdio'])
    
    # Should initialize successfully
    result = await client.initialize({
        'processId': os.getpid(),
        'rootUri': 'file:///path/to/project',
        'capabilities': {}
    })
    
    print(f"Server capabilities: {result.capabilities}")
    await client.stop()

asyncio.run(test_pyright())
```

**Expected behavior:**
- Process spawns without error
- JSON-RPC connection established
- `initialize` request returns server capabilities
- `initialized` notification sent
- Clean shutdown with `shutdown` + `exit`

---

### ❌ 2. Sending/Receiving Real LSP Messages

**Test file synchronization:**

```python
async def test_file_sync():
    client = create_lsp_client('pyright')
    await client.start('pyright', ['--stdio'])
    await client.initialize({...})
    
    # Send didOpen notification
    await client.send_notification('textDocument/didOpen', {
        'textDocument': {
            'uri': 'file:///path/to/test.py',
            'languageId': 'python',
            'version': 1,
            'text': 'def hello():\n    print("hello")\n'
        }
    })
    
    # Request diagnostics
    diagnostics = await client.send_request('textDocument/diagnostic', {
        'textDocument': {'uri': 'file:///path/to/test.py'}
    })
    
    print(f"Diagnostics: {diagnostics}")
    await client.stop()
```

**Expected behavior:**
- `didOpen` notification sent successfully
- Server responds with diagnostics
- No JSON parsing errors
- Correct message correlation (request ID matching)

---

### ❌ 3. Integration with LSPServerInstance/LSPServerManager

**After converting remaining files:**

```python
from services.lsp.lsp_server_manager import create_lsp_server_manager

async def test_full_integration():
    manager = create_lsp_server_manager()
    await manager.initialize()
    
    # Should auto-detect file type and route to correct server
    server = await manager.ensure_server_started('/path/to/test.py')
    
    # Should sync file content
    await manager.open_file('/path/to/test.py', 'print("hello")')
    
    # Should send request through manager
    result = await manager.send_request(
        '/path/to/test.py',
        'textDocument/hover',
        {'position': {'line': 0, 'character': 5}}
    )
    
    print(f"Hover result: {result}")
    await manager.shutdown()
```

**Expected behavior:**
- Manager loads LSP configs from plugins
- Correct server selected based on file extension
- File synchronization (didOpen/didChange/didClose)
- Requests routed to appropriate server
- Multi-server support (Python + TypeScript simultaneously)

---

### ❌ 4. Diagnostic Delivery Pipeline

**Test passive diagnostics:**

```python
from services.lsp.manager import initialize_lsp_server_manager
from services.lsp.lsPDiagnosticRegistry import check_for_lsp_diagnostics

async def test_diagnostics():
    initialize_lsp_server_manager()
    
    # Edit a file with intentional error
    with open('test.py', 'w') as f:
        f.write('def broken(\n')  # Syntax error
    
    # Wait for LSP to process
    await asyncio.sleep(2)
    
    # Check for pending diagnostics
    diagnostics = check_for_lsp_diagnostics()
    
    print(f"Pending diagnostics: {diagnostics}")
    # Should show syntax error from LSP
```

**Expected behavior:**
- LSP sends `textDocument/publishDiagnostics` notification
- `passiveFeedback.py` registers diagnostics
- `lsPDiagnosticRegistry.py` stores pending diagnostics
- `check_for_lsp_diagnostics()` returns deduplicated diagnostics
- Cross-turn deduplication works (same error not delivered twice)

---

## Known Issues to Test

### 1. Process Lifecycle Edge Cases
- [ ] Server crashes during operation (onCrash callback)
- [ ] Server fails to start (ENOENT for missing command)
- [ ] Server hangs during initialization (startupTimeout)
- [ ] Multiple start/stop cycles

### 2. Message Handling Edge Cases
- [ ] Malformed JSON from server
- [ ] Missing Content-Length header
- [ ] Request timeout (no response)
- [ ] Server sends unexpected notifications
- [ ] Large messages (multi-KB)

### 3. Concurrency
- [ ] Multiple simultaneous requests
- [ ] Request during shutdown
- [ ] Notification during initialization

---

## Testing Commands

```bash
# 1. Syntax check all files
python -m py_compile services/lsp/*.py

# 2. Import test
cd services/lsp
python -c "from lspClient import LSPClient; print('OK')"

# 3. Integration test (requires LSP servers installed)
python -m pytest tests/lsp/ -v

# 4. Manual protocol test
python tests/lsp/test_protocol.py
```

---

## Dependencies Required for Testing

```bash
# Python LSP
pip install pyright

# TypeScript/JavaScript LSP
npm install -g typescript typescript-language-server

# Web LSPs
npm install -g vscode-langservers-extracted

# Testing
pip install pytest pytest-asyncio
```

---

## Test Files to Create

After Cortex IDE setup, create these test files:

1. `tests/lsp/test_lsp_client.py` - LSPClient unit tests
2. `tests/lsp/test_server_instance.py` - LSPServerInstance tests
3. `tests/lsp/test_server_manager.py` - LSPServerManager tests
4. `tests/lsp/test_diagnostics.py` - Diagnostic pipeline tests
5. `tests/lsp/integration_test.py` - Full end-to-end test

---

## Next Steps

1. **✅ All files converted!**

2. **Install LSP servers** for testing
   ```bash
   pip install pyright
   npm install -g typescript-language-server
   ```

3. **Optional: Add lsprotocol for type safety**
   ```bash
   pip install lsprotocol
   ```

4. **Create test suite** with pytest

5. **Run integration tests** with real LSP servers

6. **Document any issues** found during testing
