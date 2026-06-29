# Chat History Storage, Recovery & Loading Audit

_Audit Date: 2026-06-29_
_Files Analyzed: `chat_panel.py`, `crash_persistence.py`, `chat_store.py`, `native_chat_bridge.py`, `chat_text.py`_

---

## Table of Contents

1. [Architecture Overview](#1-architecture-overview)
2. [Current Implementation — File by File](#2-current-implementation)
3. [Industry Comparison](#3-industry-comparison)
4. [Identified Issues](#4-identified-issues)
5. [Recommended Improvements](#5-recommended-improvements)

---

## 1. Architecture Overview

### Data Flow Diagram

```
┌───────────────────────────────────────────────────────────┐
│                      User Interaction                      │
│  (New Chat → Send Message → Switch Chat → Close IDE)      │
└──────────────┬────────────────────────────┬───────────────┘
               │                            │
               ▼                            ▼
┌──────────────────────┐    ┌───────────────────────────────┐
│   native_chat_bridge  │    │     crash_persistence.py      │
│  (agent_bridge →      │    │  (CRASH-SAFE: IMMEDIATE       │
│   AgentSignals)       │    │   SQLite writes on every      │
└──────────┬───────────┘    │   user message & AI response) │
           │                └──────────────┬────────────────┘
           ▼                               │
┌──────────────────────┐                   │
│    chat_panel.py     │                   │
│  (ChatPanel UI —     │◄──────────────────┘
│   MessageWidget,     │
│   streaming,         │
│   serialization)     │
└──────────┬───────────┘
           │
           ▼
┌──────────────────────┐
│    chat_store.py     │  ← Timeline persistence layer
│  (JSON serialization │    (saves/loads conversation timelines)
│   + DB read/write)   │
└──────────────────────┘
```

### Storage Mechanism

Cortex uses **SQLite** (via `crash_persistence.py` / `chat_store.py`) for all chat persistence. Key tables:

| Table | Purpose | Key Fields |
|-------|---------|------------|
| `conversations` | Conversation metadata | `conversation_id`, `title`, `timeline_json`, `created_at` |
| `chat_messages` | Individual messages | `conversation_id`, `role`, `content`, `timestamp` |
| `chat_parts` | Serialized UI blocks | `message_id`, `type`, `data` (JSON) |
| `crash_recovery_log` | Crash recovery audit trail | `conversation_id`, `action`, `saved_at` |

---

## 2. Current Implementation

### 2.1 Crash Persistence (`crash_persistence.py`)

**What it does:** Crash-safe immediate writes to SQLite on every user message and AI response.

**Write path:**
- `save_user_message()` → Called BEFORE message goes to AI agent
- `save_assistant_response()` → Called when AI turn completes

**Key design decisions:**
- Uses `PRAGMA journal_mode=WAL` + `PRAGMA synchronous=NORMAL` for crash safety
- Each save is a standalone transaction (no batching)
- Thread-safe with `threading.Lock()`
- Singleton pattern via `get_crash_store()`

**Recovery path:**
- On IDE startup, checks `crash_recovery_log` for unsaved responses
- `get_unsaved_conversation()` retrieves messages that need recovery
- `chat_panel.load_recovered_messages()` restores them with batched rendering

### 2.2 Chat Store (`chat_store.py`)

**What it does:** Timeline persistence — saves/loads full conversation timelines as JSON in SQLite.

**Serialization format:** Each message is serialized with its full UI state:
```python
{
    "role": "user" | "assistant",
    "content": "original text",
    "blocks": [
        {"type": "thinking", "content": "..."},
        {"type": "prose", "content": "rendered HTML", "hl_html": "pre-highlighted"},
        {"type": "code", "content": "...", "lang": "python", "hl_html": "..."},
        {"type": "tools", "items": [...]},
        {"type": "diff", "filename": "...", "hunk_lines": [...]}
    ]
}
```

**Save triggers:**
- End of each assistant turn (`on_turn_done`)
- Crash save via `_crash_save_turn_response()`

### 2.3 Chat Panel Loading (`chat_panel.py`)

**Two load paths exist:**

#### Path A: `load_timeline()` — Synchronous (Legacy)
```python
def load_timeline(self, data: dict):
    # 1. clear_messages()
    # 2. For each message: MessageWidget.from_serialized(m, _restoring=True)
    # 3. _refit_all_bodies() → _autoscroll()
```
- Blocks UI during load
- No progress indicator
- Suitable for small conversations only

#### Path B: `load_timeline_async()` — Async Batched (Primary)
```python
def load_timeline_async(self, data: dict):
    # 1. Show spinner overlay
    # 2. clear_messages()
    # 3. Process in BATCH_SIZE=24 batches:
    #    a. _freeze_viewport() → add widgets → _thaw_viewport()
    #    b. QTimer.singleShot(0) → yield to event loop
    # 4. _refit_all_bodies() → _autoscroll()
```
- Non-blocking with spinner overlay
- Viewport freeze/thaw per batch
- `_restoring=True` skips expensive `_fit()` calls during restore
- Safety timeout: 30 seconds

#### Path C: `load_recovered_messages()` — Crash Recovery
```python
def load_recovered_messages(self, messages: list):
    # Similar to load_timeline_async but for crash-recovered data
    # Uses BATCH_SIZE=24 with spinner overlay
```

### 2.4 Message Serialization / Deserialization

**`MessageWidget.serialize()`** — Captures full UI state:
- User messages: raw text + images
- Assistant messages: iterates all child widgets (ThoughtsBlock, QTextBrowser, ToolGroup, DiffCard, CodeBlockWidget)
- Preserves syntax-highlighted HTML (`hl_html`) to avoid re-highlighting on restore

**`MessageWidget.from_serialized()`** — Rebuilds from serialized data:
- `_restoring=True` mode:
  - Skips `QTimer._fit()` calls (avoids hundreds of deferred timers)
  - Truncates prose > 20KB (performance guard)
  - Uses cached `hl_html` instead of re-running syntax highlighting
  - Sets `_streaming_skip_fit` to suppress content-change refits

### 2.5 Background Virtualization

`_virtualize_old_messages(keep_recent=30)` runs on a 60-second timer:
- Replaces old message widget children with lightweight QLabel summaries
- Keeps last 30 messages fully rendered
- Collapsed messages show: `"User: first 80 chars..."` or `"AI: first 80 chars..."`
- Prevents widget accumulation in long sessions

### 2.6 Scroll Management

**`_autoscroll()`** — Debounced auto-scroll to bottom:
- 50ms timer coalesces rapid layout changes
- Smooth animation via QPropertyAnimation (80ms during streaming, 150-260ms idle)
- Skipped when user manually scrolls up (`_scroll_locked`)

**`_scroll_locked`** — User scroll intent tracking:
- Locked when user scrolls > threshold pixels from bottom
- Unlocked when user scrolls to within 5px of bottom or presses End key
- New message pill appears when locked + new message arrives

**`_stabilize_scroll()`** — Context manager for layout mutations:
- Freezes viewport during changes
- Restores scroll position synchronously
- Prevents scroll jumps during tool card updates

---

## 3. Industry Comparison

### How Major AI Chat Tools Handle History

| Feature | Cursor | VS Code Copilot | ChatGPT | Claude | **Cortex** |
|---------|--------|-----------------|---------|--------|------------|
| **Storage** | SQLite per workspace | StateDB (VS Code internal) | Server-side | Server-side | SQLite (WAL) |
| **Crash Recovery** | ✅ SQLite WAL | ❌ Known data loss | ✅ Server persistence | ✅ Server persistence | ✅ SQLite WAL + immediate writes |
| **Load Strategy** | Lazy (recent 50, then load more) | Full load (known slow) | Full load (DOM bloat at 100+) | Full load | Full load with batched async |
| **Virtual Scrolling** | ✅ Virtual list | ❌ | ❌ (community complaints) | ❌ | ❌ (uses collapse instead) |
| **Serialization** | JSON in SQLite | JSON in StateDB | Server-side | Server-side | JSON in SQLite (timeline_json) |
| **Syntax Highlight Cache** | ❌ | ❌ | N/A | N/A | ✅ `hl_html` field |
| **Background Cleanup** | ✅ | ❌ | ❌ | ❌ | ✅ (60s timer, keep 30) |
| **Progress Indicator** | ✅ | ❌ | ❌ | ❌ | ✅ (spinner overlay) |

### Key Findings from Industry

1. **Cursor** stores chat history per workspace in SQLite (`state.vscdb` files). Uses virtual scrolling for long conversations. Known issue: history can become inaccessible after workspace folder changes.

2. **VS Code Copilot** uses VS Code's internal StateDB. Known issues: conversations lost after crash/restart, slow loading for long chats. Multiple open issues about data loss.

3. **ChatGPT** and **Claude** store server-side. Both suffer from DOM bloat with long conversations (100+ messages). ChatGPT has community-built Chrome extensions for virtual scrolling. Google AI Studio explicitly noted as lacking virtual scrolling.

4. **Windsurf** (Codeium) stores chat in its own format. Users report only seeing ~20 past conversations. Chat history export is a requested feature.

---

## 4. Identified Issues

### Issue 1: CRITICAL — Full Conversation Load on Switch

**Problem:** When switching to a previous chat, `load_timeline_async()` loads ALL messages at once (in batches of 24, but still all of them). For conversations with 100+ messages, this causes:
- 2-5 second loading time
- "Pull together" effect as widgets settle
- Hundreds of QTextBrowser widgets in memory
- `_refit_all_bodies()` must iterate all widgets

**Impact:** Large conversations become sluggish and visually jarring on open.

**Evidence:**
```python
# chat_panel.py — load_timeline_async()
BATCH_SIZE = 24  # Processes ALL messages, just in batches
for i in range(batch_start, batch_end):
    msg_widget = MessageWidget.from_serialized(m, _restoring=True)
    self.col.addWidget(msg_widget)  # ALL messages added to layout
```

### Issue 2: HIGH — No Virtual Scrolling / Windowed Rendering

**Problem:** All message widgets exist in the QVBoxLayout simultaneously. `_virtualize_old_messages()` only collapses (replaces children with a label) — it doesn't remove widgets from the layout. A 200-message conversation has 200 MessageWidget instances in memory even after virtualization.

**Impact:** Memory usage grows linearly with conversation length. Layout calculations become O(n).

### Issue 3: HIGH — Expensive `_refit_all_bodies()` Pass

**Problem:** After loading, `_refit_all_bodies()` calls `findChildren(QTextBrowser)` on the entire widget tree, then calls `_fit()` on each visible one. For a 100-message conversation, this could be 300+ QTextBrowser widgets (prose + code + thinking blocks per message).

**Impact:** Second UI freeze after the batch-load freeze completes.

**Evidence:**
```python
# chat_panel.py — _refit_all_bodies()
all_bodies = [
    tb for tb in self.findChildren(QTextBrowser)  # Traverses entire tree
    if tb.isVisible() and hasattr(tb, "_fit") ...
]
# Then iterates ALL in batches of 24
```

### Issue 4: MEDIUM — No Lazy Loading of Older Messages

**Problem:** There's no "load more" or pagination mechanism. Users can't load only the most recent N messages and lazy-load older ones on scroll-up.

**Industry pattern:** Cursor loads recent 50 messages, then loads more on scroll-up. ChatGPT/Discord use virtual lists that only render visible messages.

### Issue 5: MEDIUM — No Scroll Position Restoration

**Problem:** When switching chats, `_autoscroll()` always scrolls to bottom. There's no mechanism to save/restore scroll position per conversation. Users lose their place in long conversations.

### Issue 6: LOW — Serialization Stores Pre-Rendered HTML

**Problem:** `serialize()` saves `hl_html` (pre-highlighted HTML) in the timeline. While this speeds up restore (avoids re-highlighting), it:
- Bloats the database (highlighted HTML is 2-5x larger than source)
- Makes timeline data harder to search/migrate
- Creates dependency on highlight format compatibility

### Issue 7: LOW — No Conversation Size Limits or Warnings

**Problem:** No guard against extremely large conversations. A 500-message conversation with code blocks will serialize to megabytes of JSON in `timeline_json`.

---

## 5. Recommended Improvements

### Priority 1: Virtual/Windowed Rendering (Fix Issues 1-3)

**Approach:** Implement a virtual scroll container that only renders messages visible in the viewport + a buffer zone.

```
┌─────────────────────────────┐
│  [Spacer — invisible]       │  ← Height = sum of off-screen messages
│  ┌─────────────────────────┐│
│  │ Message N-5 (buffer)    ││  ← Rendered but may be off-screen
│  │ Message N-4             ││
│  │ ...                     ││
│  │ Message N (visible)     ││  ← User sees this
│  │ Message N+1             ││
│  │ ...                     ││
│  │ Message N+5 (buffer)    ││
│  └─────────────────────────┘│
│  [Spacer — invisible]       │  ← Height = sum of remaining messages
└─────────────────────────────┘
```

**Implementation steps:**
1. Replace `QVBoxLayout` with a custom `VirtualChatLayout` using `QScrollArea`
2. Maintain a message data list (lightweight — just serialized dicts)
3. Only create `MessageWidget` instances for visible + buffer messages
4. Recycle widgets as user scrolls (reuse off-screen widgets for new messages)
5. Use placeholder heights for off-screen messages

**Estimated effort:** Large (2-3 days)

### Priority 2: Lazy Loading with Scroll-Up Trigger (Fix Issue 4)

**Approach:** Load only the last 30-50 messages initially. When user scrolls to top, load 30 more.

```python
def load_timeline_async(self, data: dict):
    # Load only the last N messages initially
    messages = data["messages"]
    initial_load = messages[-50:] if len(messages) > 50 else messages
    self._all_messages = messages  # Keep reference for lazy loading
    self._loaded_count = len(initial_load)
    # ... load only initial_load ...

def _on_scroll_at_top(self):
    """Called when scroll reaches top — load older messages."""
    if self._loaded_count >= len(self._all_messages):
        return  # All loaded
    # Load 30 more messages above current position
    older = self._all_messages[-(self._loaded_count + 30):-self._loaded_count]
    self._prepend_messages(older)
    self._loaded_count += len(older)
```

**Estimated effort:** Medium (1 day)

### Priority 3: Scroll Position Save/Restore (Fix Issue 5)

**Approach:** Save scroll position (message index + pixel offset) when switching away, restore when switching back.

```python
self._scroll_positions: dict[str, tuple[int, int]] = {}

def _save_scroll_position(self):
    if self._conversation_id:
        idx = self._get_visible_message_index()
        offset = self.scroll.verticalScrollBar().value()
        self._scroll_positions[self._conversation_id] = (idx, offset)

def _restore_scroll_position(self):
    pos = self._scroll_positions.get(self._conversation_id)
    if pos:
        self._scroll_to_message(pos[0], pos[1])
    else:
        self._autoscroll()  # Default: scroll to bottom
```

**Estimated effort:** Small (half day)

### Priority 4: Conversation Size Guards (Fix Issue 7)

**Approach:** Add limits and warnings for large conversations.

- Warn at 200+ messages: "This conversation is large. Loading may be slow."
- Cap serialization at 500 messages (archive older messages separately)
- Compress `timeline_json` with gzip before storing

**Estimated effort:** Small (half day)

---

## Summary Table

| Issue | Severity | Fix | Effort | Status |
|-------|----------|-----|--------|--------|
| Full conversation load on switch | CRITICAL | Virtual/windowed rendering | Large | TODO |
| No virtual scrolling | HIGH | Custom VirtualChatLayout | Large | TODO |
| Expensive _refit_all_bodies | HIGH | Limit to visible widgets only | Medium | TODO |
| No lazy loading | MEDIUM | Paginated load with scroll-up | Medium | TODO |
| No scroll position restore | MEDIUM | Save/restore per conversation | Small | TODO |
| Pre-rendered HTML bloat | LOW | Compression / lazy highlight | Small | TODO |
| No size limits | LOW | Guards + warnings | Small | TODO |

---

_Sources:_
- _[Cursor chat history stored in SQLite](https://forum.cursor.com/t/chat-history-folder/7653)_
- _[Cursor workspace chat history issues](https://forum.cursor.com/t/cursor-is-really-bad-at-keeping-track-of-workspaces-and-ai-chats-essions/154004)_
- _[VS Code Copilot chat history persistence issues](https://github.com/microsoft/vscode/issues/295813)_
- _[ChatGPT UI performance complaints](https://community.openai.com/t/catastrophic-failures-of-chatgpt-thats-creating-major-problems-for-users/1156230)_
- _[Google AI Studio slow with long conversations](https://www.reddit.com/r/Bard/comments/1j2kh4z/google_ai_studio_really_slow_with_long/)_
- _[Figma chat performance with large threads](https://forum.figma.com/ask-the-community-7/figma-make-works-slow-after-many-iterations-41562)_
- _[Windsurf chat history export request](https://github.com/Exafunction/codeium/issues/127)_
