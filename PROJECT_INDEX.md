# Cortex Desktop — Project Index

_Regenerated: 2026-06-29_

## Overview

**Cortex Desktop** is an AI-native IDE built with **PyQt6 + Qt WebEngine** that wraps a multi-provider LLM agent with 26+ coding tools inside a native chat UI + Monaco editor shell. It supports autonomous coding, streaming responses, crash persistence, semantic code search, plugin extensibility, and a full profile/usage tracking system.

---

## Key Metrics

| Metric | Value |
|--------|-------|
| Python files | 565 (excl. `node_modules/venv/build/dist`) |
| HTML files | 126 |
| CSS files | 102 |
| JS files | 149 (excl. `node_modules`) |
| Agent tools | 26 |
| LLM providers | 8 (OpenAI, DeepSeek, Mistral, Alibaba, Kimi, MiMo, OpenRouter, SiliconFlow) |
| Main entry | `src/main.py` (775 lines) |
| Main window | `src/main_window.py` (7,085 lines) |
| Agent bridge | `src/ai/agent_bridge.py` (12,064 lines) |
| Chat panel | `src/ui/chat_panel.py` (8,089 lines) |

---

## Architecture

```
main.py
  └─ main_window.py — IDE Shell
       ├─ chat_panel.py — Native Chat UI (lazy loading, scroll restore)
       ├─ editor.py — Monaco Editor
       ├─ sidebar_bridge.py — HTML Sidebar
       ├─ xterm_terminal.py — Terminal
       ├─ webview_panel.py — Webview Tabs
       ├─ diff_viewer.py — Diff Dialog
       └─ memory_manager.py — Settings/Profile UI
            └─ agent_bridge.py — Core AI Brain
                 ├─ 8 LLM Providers (providers/)
                 ├─ 26 Agent Tools (agent/src/tools/)
                 ├─ Context Compaction (conversation_compactor.py)
                 ├─ Memory System (semantic_memory.py, embeddings.py)
                 ├─ streaming.py — Emitter
                 ├─ usage_tracker.py — Profile/Usage
                 └─ agent_safety.py — Tool budget, doom-loop detection
```

---

## Directory Structure

```
src/
├── main.py                      # Entry point (775 lines)
├── main_window.py               # IDE shell, menu bar, signals (7,085 lines)
│
├── ai/                          # AI agent core
│   ├── agent_bridge.py          # Central brain — tool loop, streaming (12,064 lines)
│   ├── agent_safety.py          # Tool budget, doom-loop, read-before-edit (617 lines)
│   ├── usage_tracker.py         # Token/request/tool tracking (591 lines)
│   ├── streaming.py             # SSE event emitter
│   ├── conversation_compactor.py # Context window compaction
│   ├── tool_executor.py         # Tool execution engine
│   ├── tool_result_storage.py   # Tool output storage
│   ├── model_limits.py          # Per-model context limits
│   ├── model_registry.py        # Model metadata registry
│   ├── project_context.py       # Project file context
│   ├── cortex_project_context.py # Project indexing
│   ├── file_skeleton.py         # File structure extraction
│   ├── circuit_breaker.py       # API failure protection
│   ├── session_task.py          # Session management
│   ├── stub_agent.py            # Fallback agent
│   ├── changes/                 # Change tracking
│   └── providers/               # LLM provider implementations
│       ├── __init__.py          # Provider base + registry
│       ├── openai_provider.py
│       ├── deepseek_provider.py
│       ├── mistral_provider.py
│       ├── alibaba_provider.py
│       ├── mimo_provider.py
│       ├── openrouter_provider.py
│       └── siliconflow_provider.py
│
├── ui/                          # UI components
│   ├── chat_panel.py            # Main chat UI — lazy load, scroll restore (8,089 lines)
│   ├── chat_store.py            # Timeline serialization/persistence
│   ├── chat_text.py             # Text cleaning, INLINE marker handling (594 lines)
│   ├── tokens.py                # Design tokens — dark theme (595 lines)
│   ├── native_chat_bridge.py    # AgentBridge → ChatPanel bridge
│   ├── tool_cards.py            # Tool call card rendering
│   ├── syntax_highlight.py      # Code syntax highlighting
│   ├── table_normalize.py       # Markdown table normalization
│   ├── spinner.py               # Loading spinner
│   ├── spinner_overlay.py       # Full-screen spinner
│   ├── edit_state_manager.py    # File edit state tracking
│   ├── secondary_ui.py          # Secondary panels
│   ├── icons.py                 # SVG icon definitions
│   ├── agent_signals.py         # Qt signals for agent events
│   ├── components/              # Reusable UI components
│   │   ├── editor.py            # Monaco editor wrapper
│   │   ├── webview_panel.py     # Webview tab manager
│   │   ├── sidebar.py           # HTML sidebar
│   │   ├── sidebar_bridge.py    # Sidebar ↔ Python bridge
│   │   ├── terminal.html        # xterm.js terminal
│   │   ├── xterm_terminal.py    # Terminal wrapper
│   │   ├── problems_panel.py    # Problems/diagnostics
│   │   ├── windows_terminal.py  # Windows terminal
│   │   ├── cursor_split_handle.py # Split view handle
│   │   ├── chat_enhanced/       # Enhanced chat components
│   │   └── permission/          # Permission card renderer
│   ├── dialogs/                 # Dialog windows
│   │   ├── diff_viewer.py       # Diff viewer dialog
│   │   └── memory_manager.py    # Settings/profile UI
│   ├── html/                    # HTML assets
│   │   ├── sidebar.html         # Sidebar UI
│   │   ├── ai_chat/             # Mermaid, spinner demo
│   │   ├── memory_manager/      # Settings page (HTML/CSS/JS)
│   │   └── icons/               # SVG icon library (1,246 files)
│   └── themes/                  # QSS theme files
│       └── dark.qss             # Dark theme stylesheet
│
├── core/                        # Core systems
│   ├── crash_persistence.py     # Crash-safe SQLite writes (481 lines)
│   ├── database.py              # Database schema + migrations
│   ├── chat_history.py          # Chat message persistence
│   ├── semantic_memory.py       # Vector memory search
│   ├── embeddings.py            # Embedding generation
│   ├── siliconflow_embeddings.py # SiliconFlow embedding provider
│   ├── stability_engine.py      # RAM/CPU monitoring, emergency save
│   ├── file_manager.py          # File operations
│   ├── git_manager.py           # Git integration
│   ├── project_manager.py       # Project management
│   ├── session_manager.py       # Session persistence
│   ├── agent_session_manager.py # Agent session state
│   ├── autonomy_manager.py      # Agent autonomy levels
│   ├── codebase_index.py        # Code indexing
│   ├── code_chunker.py          # Code splitting
│   ├── memory_storage.py        # Memory persistence
│   ├── memory_types.py          # Memory type definitions
│   ├── event_bus.py             # Event system
│   ├── background_worker.py     # Background tasks
│   ├── change_orchestrator.py   # Change coordination
│   ├── debug_loop.py            # Debug loop detection
│   ├── key_manager.py           # API key management
│   ├── live_server.py           # Dev server
│   ├── task_graph.py            # Task dependency graph
│   ├── sandbox_manager.py       # Code sandboxing
│   └── worker_entrypoint.py     # Worker process entry
│
├── config/                      # Configuration
│   ├── settings.py              # App settings
│   ├── theme_manager.py         # Theme management (dark-only, 60 lines)
│   └── points_manager.py        # Points/rewards system
│
├── coordinator/                 # Multi-agent coordinator
│   ├── coordinator_prompt.py    # Coordinator system prompt
│   ├── coordinator_system.py    # Coordinator logic
│   └── agent_context.py         # Agent context management
│
├── plugin/                      # Plugin system
│   └── plugin_manager.py        # Plugin loader
│
├── services/                    # Services
│   ├── usage_tracker.py         # Usage tracking service
│   └── errors.py                # Error definitions
│
├── utils/                       # Utilities
│   ├── helpers.py               # General helpers
│   ├── logger.py                # Logging configuration
│   ├── icons.py                 # Icon generation
│   ├── language_detector.py     # File language detection
│   ├── image_processing.py      # Image handling
│   ├── git_utils.py             # Git utilities
│   ├── diff/                    # Diff utilities
│   ├── notifications.py         # System notifications
│   ├── timeout_strategy.py      # API timeout handling
│   ├── safe_delete.py           # Safe file deletion
│   ├── startup_profiler.py      # Startup performance
│   ├── pyinstaller_hooks/       # PyInstaller build hooks
│   └── runtime_hook_*.py        # Runtime hooks (4 files)
│
├── assets/                      # Static assets
│   ├── editor.html              # Monaco editor HTML
│   └── logo/                    # App icons
│
└── agent/                       # Agent framework (internal)
    └── src/
        ├── tools/               # 26 agent tools
        └── ...                  # Agent internals
```

---

## Key Systems

### Chat History (Lazy Loading + Scroll Restore)
- **`chat_panel.py`** — `load_timeline_async()` loads last 50 messages as complete turns
- **Scroll-up pagination** — loads 30 more messages per scroll-up, complete turns only
- **Scroll position save/restore** — per-conversation scroll memory
- **Viewport-aware refit** — only refits visible QTextBrowser widgets
- **`chat_store.py`** — timeline JSON serialization in SQLite
- **`crash_persistence.py`** — immediate SQLite writes on every message

### Agent Safety (`agent_safety.py`)
- Tool budget: unlimited (soft reminder every 50 calls)
- Doom-loop detection: same tool + same args 5x → stop
- Read-before-edit enforcement
- Stale-read detection
- Error recovery budget (3 retries)

### Thinking Budget (`agent_bridge.py`)
- Default: 32,000 tokens (configurable via `CORTEX_THINKING_BUDGET_TOKENS`)
- Exceeded → close thought card, drop further thinking chunks
- Content and tool calls still processed normally

### Stability Engine (`stability_engine.py`)
- Monitors RAM/CPU every 5 seconds
- Pressure levels: normal → elevated → high → critical
- Emergency save throttled to once per 30 seconds at critical
- GC triggered at high pressure

### Design Tokens (`tokens.py`)
- Dark theme only — all colors from `DARK` dict
- Single source of truth for UI theming
- `build_markdown_css()` — markdown rendering CSS
- `build_qss()` — Qt stylesheet generation

---

## Build System

| File | Purpose |
|------|---------|
| `cortex.spec` | PyInstaller spec — bundles Python + assets |
| `build.ps1` | PowerShell build script |
| `cortex_setup.iss` | Inno Setup installer script |
| `build_installer.bat` | Batch installer builder |

### Recent Build Fixes (2026-06-29)
- Monaco editor bundled in `cortex.spec`
- Fixed hidden import names (`mem0`, `PIL`, `frontmatter`)
- Fixed `codecs.mbcs` import error (try/except)
- Fixed `cortex_setup.iss` duplicate content

---

## Environment Variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `CORTEX_MAX_TOOL_ITERATIONS` | `0` (unlimited) | Tool call limit per turn |
| `CORTEX_THINKING_BUDGET_TOKENS` | `32000` | Thinking token budget |
