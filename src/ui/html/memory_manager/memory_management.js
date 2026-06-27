/* CORTEX SETTINGS — JS: Navigation, Modals, Memory Bridge, Live Settings Hydration */
(function () {
  "use strict";

  let bridge = null, bridgeInitAttempts = 0, bridgeInitScheduled = false;
  let state = { enabled: true, activeScope: "project", scopes: { project: { name: "Current Project", projectRoot: "", memoryDir: "", memories: [] }, global: { name: "Global", memoryDir: "", memories: [] } } };
  let uiState = { query: "", type: "all", isSearchMode: false, searchQuery: "" };
  const $ = (id) => document.getElementById(id);

  /* ═══════════════════════════════════════════════════════════════
     SETTINGS MAP — HTML control ID → Python settings dotted path
     Maps every <input/select/textarea/toggle> ID in the HTML to
     the corresponding key in ~/.cortex/settings.json.
     ═══════════════════════════════════════════════════════════════ */
  const SETTINGS_MAP = {
    /* General */
    restoreSession: "memory.restore_session",
    checkUpdates: "ui.check_updates",
    notifications: "notifications.task_complete_enabled",
    soundAlerts: "notifications.sound_alerts",
    telemetry: "ui.telemetry",

    /* Appearance — Editor */
    editorFontSize: "editor.font_size",
    editorFont: "editor.font_family",
    tabSize: "editor.tab_size",
    wordWrap: "editor.word_wrap",
    minimap: "editor.minimap",

    /* Appearance — Interface */
    uiScale: "ui.ui_scale",
    sidebarPosition: "layout.sidebar_position",

    /* Models & Providers */
    defaultModel: "ai.model",
    openaiKey: "ai.openai_key",
    deepseekKey: "ai.deepseek_key",
    mistralKey: "ai.mistral_key",
    kimiKey: "ai.kimi_key",
    mimoKey: "ai.mimo_key",
    googleKey: "ai.google_key",
    openrouterKey: "ai.openrouter_key",
    alibabaKey: "ai.alibaba_key",
    ollamaUrl: "ai.ollama_url",

    /* Personalization */
    systemInstructions: "ai.system_instructions",
    verbosity: "ai.verbosity",
    codeStyle: "ai.code_style",
    rememberConvos: "memory.remember_conversations",
    contextWindow: "ai.context_window",

    /* Safety & Permissions */
    allowFileCreate: "safety.allow_file_create",
    allowFileDelete: "safety.allow_file_delete",
    allowTerminal: "safety.allow_terminal",
    requireApproval: "safety.require_approval",
    privacyMode: "safety.privacy_mode",
    localOnly: "safety.local_only",

    /* Git */
    autoCommit: "git.auto_commit",
    commitPrefix: "git.commit_prefix",
    defaultBranch: "git.default_branch",

    /* Terminal */
    defaultShell: "terminal.default_shell",
    shellArgs: "terminal.shell_args",
    termFontSize: "terminal.font_size",
    scrollback: "terminal.scrollback",
    cursorStyle: "terminal.cursor_style",
    copyOnSelect: "terminal.copy_on_select",

    /* Performance */
    gpuAccel: "performance.gpu_accel",
    limitBackground: "performance.limit_background",
    watcherDebounce: "performance.watcher_debounce",
    requestTimeout: "ai.request_timeout",
    proxy: "network.proxy",
  };

  /* Reverse map: dotted path → control ID (for quick lookup) */
  const _pathToId = {};
  for (const [id, path] of Object.entries(SETTINGS_MAP)) _pathToId[path] = id;

  /* ═══════ Bridge helpers ═══════ */
  function resolveBridgeMethod(obj, names) { for (const n of names) { if (obj && typeof obj[n] === "function") return obj[n].bind(obj); } return null; }
  function callBridge(methodNames, args = [], timeoutMs = 6000) {
    const names = Array.isArray(methodNames) ? methodNames : [methodNames];
    return new Promise((resolve, reject) => {
      const fn = resolveBridgeMethod(bridge, names);
      if (!fn) return reject(new Error("Bridge unavailable: " + names.join(", ")));
      let settled = false;
      const timer = setTimeout(() => { if (!settled) { settled = true; reject(new Error("Timeout: " + names[0])); } }, timeoutMs);
      try { fn(...args, (r) => { if (!settled) { settled = true; clearTimeout(timer); resolve(r); } }); } catch (e) { if (!settled) { settled = true; clearTimeout(timer); reject(e); } }
    });
  }

  /* ═══════ Section Nav ═══════ */
  function switchSection(id) {
    document.querySelectorAll(".settings-section").forEach(s => s.classList.remove("active"));
    document.querySelectorAll(".nav-item").forEach(n => n.classList.remove("active"));
    const sec = document.querySelector('.settings-section[data-section="' + id + '"]');
    const nav = document.querySelector('.nav-item[data-section="' + id + '"]');
    if (sec) sec.classList.add("active");
    if (nav) nav.classList.add("active");
  }

  /* ═══════ Upgrade Modal ═══════ */
  function showUpgradeModal() { $("upgradeModal").classList.remove("hidden"); }
  function hideUpgradeModal() { $("upgradeModal").classList.add("hidden"); }

  /* ═══════ Toast ═══════ */
  function showToast(msg, ms = 2500) {
    const host = $("toastHost"), t = document.createElement("div");
    t.textContent = msg;
    Object.assign(t.style, { background: "var(--surface-2)", border: "1px solid var(--border)", borderRadius: "12px", padding: "10px 18px", color: "var(--text)", fontSize: "13px", marginTop: "8px", boxShadow: "0 8px 24px rgba(0,0,0,.3)", animation: "fadeInSection .2s ease", fontFamily: "var(--font-ui)" });
    host.appendChild(t);
    setTimeout(() => { t.style.opacity = "0"; t.style.transition = "opacity .3s"; setTimeout(() => t.remove(), 300); }, ms);
  }

  /* ═══════ Helpers ═══════ */
  function esc(s) { const d = document.createElement("div"); d.textContent = s || ""; return d.innerHTML; }
  function timeAgo(ts) { const d = Date.now() - new Date(ts).getTime(); if (d < 60000) return "just now"; if (d < 3600000) return Math.floor(d / 60000) + "m ago"; if (d < 86400000) return Math.floor(d / 3600000) + "h ago"; return Math.floor(d / 86400000) + "d ago"; }

  /* Flatten a nested dict into dotted-path keys: {a:{b:1}} → {"a.b":1} */
  function flattenObj(obj, prefix) {
    prefix = prefix || "";
    const out = {};
    for (const [k, v] of Object.entries(obj || {})) {
      const path = prefix ? prefix + "." + k : k;
      if (v !== null && typeof v === "object" && !Array.isArray(v)) {
        Object.assign(out, flattenObj(v, path));
      } else {
        out[path] = v;
      }
    }
    return out;
  }

  /* ═══════ LIVE SETTINGS HYDRATION ═══════
     Called once on bridge connect with the full nested settings dict
     from Python's getSettings(). Iterates SETTINGS_MAP and sets every
     matching HTML control to its saved value. */
  function applySettingsFromBridge(data) {
    if (!data || typeof data !== "object") return;
    const flat = flattenObj(data);          // e.g. {"editor.font_size": 14, "ai.model": "gpt-4o", ...}

    /* Walk every mapped control */
    for (const [ctrlId, settingsPath] of Object.entries(SETTINGS_MAP)) {
      const el = $(ctrlId);
      if (!el) continue;
      const val = flat[settingsPath];
      if (val === undefined || val === null) continue;

      if (el.type === "checkbox") {
        el.checked = !!val;
      } else if (el.type === "range") {
        el.value = val;
        /* Update the sibling <span class="range-value"> */
        const valEl = $(ctrlId + "Val");
        if (valEl) {
          const unit = valEl.textContent.replace(/[\d.]+/, "");
          valEl.textContent = val + unit;
        }
      } else if (el.tagName === "TEXTAREA") {
        el.value = val;
      } else {
        el.value = val;
      }
    }

    /* Theme picker — special case (button group, not a single control) */
    const theme = flat["theme"];
    if (theme) {
      document.querySelectorAll(".theme-option").forEach(btn => {
        btn.classList.toggle("active", btn.dataset.theme === theme);
      });
    }

    /* AI model — highlight the matching option if it exists */
    const model = flat["ai.model"];
    if (model) {
      const sel = $("defaultModel");
      if (sel) {
        const match = Array.from(sel.options).find(o => o.value === model);
        if (match) sel.value = model;
      }
    }

    console.info("[SETTINGS] Hydrated", Object.keys(SETTINGS_MAP).length, "controls from bridge");
  }

  /* ═══════ Memory List ═══════ */
  function renderMemoryList() {
    const scope = state.scopes[state.activeScope]; if (!scope) return;
    const list = $("listView"), empty = $("emptyState"), memories = scope.memories || [];
    let filtered = memories;
    if (uiState.query) { const q = uiState.query.toLowerCase(); filtered = filtered.filter(m => (m.title || "").toLowerCase().includes(q) || (m.content || "").toLowerCase().includes(q) || (m.source_file || "").toLowerCase().includes(q)); }
    $("countLabel").textContent = filtered.length + " memor" + (filtered.length === 1 ? "y" : "ies");
    if (filtered.length === 0) { list.innerHTML = ""; if (empty) empty.classList.remove("hidden"); return; }
    if (empty) empty.classList.add("hidden");
    list.innerHTML = filtered.map((m, i) => {
      const type = m.type || "general", age = m.created_at ? timeAgo(m.created_at) : "";
      const sim = m._similarity != null ? '<span style="display:inline-block;background:linear-gradient(135deg,var(--accent-2),var(--accent));color:#0e1116;padding:2px 8px;border-radius:12px;font-size:11px;font-weight:600;margin-right:6px;">' + Math.round(m._similarity * 100) + '%</span>' : "";
      return '<div style="background:var(--surface);border:1px solid var(--border);border-radius:14px;padding:14px 18px;">' +
        '<div style="display:flex;justify-content:space-between;align-items:flex-start;"><div><div style="font-weight:600;font-size:14px;">' + sim + esc(m.title || "Untitled") + '</div>' +
        '<div style="font-size:12px;color:var(--muted);margin-top:3px;"><span style="background:var(--surface-3);padding:2px 8px;border-radius:999px;font-size:11px;">' + esc(type) + '</span>' + (m.source_file ? " · " + esc(m.source_file.split(/[\\\/]/).pop()) : "") + (age ? " · " + age : "") + '</div></div>' +
        '<button class="setting-btn danger-btn" style="padding:4px 10px;font-size:11px;" onclick="window._deleteMemory(' + i + ')">Delete</button></div>' +
        '<div style="margin-top:10px;font-size:13px;color:var(--muted);line-height:1.5;white-space:pre-wrap;">' + esc((m.content || "").slice(0, 300)) + ((m.content || "").length > 300 ? "…" : "") + '</div></div>';
    }).join("");
  }

  /* ═══════ State Apply (Memory) ═══════ */
  function applyState(data) {
    if (!data) return;
    state.enabled = data.enabled !== false;
    if (data.scopes) { for (const k of Object.keys(data.scopes)) { if (!state.scopes[k]) state.scopes[k] = { name: k, projectRoot: "", memoryDir: "", memories: [] }; Object.assign(state.scopes[k], data.scopes[k]); } }
    const toggle = $("enabledToggle"); if (toggle) toggle.checked = state.enabled;
    const dot = $("statusDot"); if (dot) dot.classList.toggle("enabled", state.enabled);
    if (data.activeScope) state.activeScope = data.activeScope;
    document.querySelectorAll(".scope-tab").forEach(t => t.classList.toggle("active", t.dataset.scope === state.activeScope));
    renderMemoryList();
  }

  window.receiveMemoryState = function (data) { try { applyState(typeof data === "string" ? JSON.parse(data) : data); window.__memMgrDebug.loaded = true; } catch (e) { console.error("[SETTINGS] parse error:", e); } };

  /* ═══════ Bridge Init ═══════ */
  function scheduleBridgeInitRetry() { if (bridge || bridgeInitScheduled) return; bridgeInitScheduled = true; setTimeout(() => { bridgeInitScheduled = false; initBridge(); }, 200); }
  function initBridge() {
    const transport = (typeof qt !== "undefined" && qt.webChannelTransport) || (typeof window !== "undefined" && window.qt && window.qt.webChannelTransport) || null;
    if (!transport) { if (++bridgeInitAttempts < 40) scheduleBridgeInitRetry(); return; }
    try {
      new QWebChannel(transport, (ch) => {
        bridge = ch.objects.bridge || ch.objects.cortex_bridge || null;
        if (!bridge) { const keys = Object.keys(ch.objects || {}); if (keys.length) bridge = ch.objects[keys[0]]; }
        if (bridge) {
          console.info("[SETTINGS] Bridge connected");
          /* Load memory state */
          const loadFn = bridge.loadInitialData || bridge.getState || bridge.refresh;
          if (typeof loadFn === "function") loadFn.call(bridge, (s) => window.receiveMemoryState(s));
          /* Load ALL settings and hydrate every control */
          if (typeof bridge.getSettings === "function") {
            bridge.getSettings((raw) => {
              if (!raw) return;
              try {
                const data = typeof raw === "string" ? JSON.parse(raw) : raw;
                applySettingsFromBridge(data);
              } catch (e) { console.error("[SETTINGS] getSettings parse error:", e); }
            });
          }
        }
      });
    } catch (e) { if (++bridgeInitAttempts < 20) scheduleBridgeInitRetry(); }
  }

  /* ═══════ Persist a setting via bridge ═══════
     Sends the dotted path (e.g. "editor.font_size") to Python's setSetting(). */
  function persistSetting(ctrlId, value) {
    const settingsPath = SETTINGS_MAP[ctrlId];
    if (!settingsPath) return; // unmapped control — skip
    if (bridge && typeof bridge.setSetting === "function") {
      bridge.setSetting(settingsPath, String(value));
    }
  }

  window._deleteMemory = function (idx) {
    const scope = state.scopes[state.activeScope]; if (!scope || !scope.memories[idx]) return;
    const mem = scope.memories[idx];
    callBridge(["deleteMemory", "delete_memory"], [state.activeScope, mem.source_file || mem.id || idx]).then(() => { scope.memories.splice(idx, 1); renderMemoryList(); showToast("Memory deleted"); }).catch(() => { scope.memories.splice(idx, 1); renderMemoryList(); });
  };

  /* ═══════════ DOM READY ═══════════ */
  document.addEventListener("DOMContentLoaded", () => {

    /* ── Nav ── */
    document.querySelectorAll(".nav-item[data-section]").forEach(btn => btn.addEventListener("click", () => switchSection(btn.dataset.section)));
    $("backBtn")?.addEventListener("click", () => { if (bridge && typeof bridge.onSettingsClosed === "function") bridge.onSettingsClosed(); else if (bridge && typeof bridge.closeSettings === "function") bridge.closeSettings(); else { window.history.back(); } });

    /* ── Nav search ── */
    $("settingsSearch")?.addEventListener("input", (e) => { const q = e.target.value.toLowerCase().trim(); document.querySelectorAll(".nav-item").forEach(btn => { btn.style.display = (!q || btn.textContent.toLowerCase().includes(q)) ? "" : "none"; }); });

    /* ── Theme picker (special — button group, not a single input) ── */
    document.querySelectorAll(".theme-option").forEach(btn => btn.addEventListener("click", () => {
      document.querySelectorAll(".theme-option").forEach(b => b.classList.remove("active"));
      btn.classList.add("active");
      if (bridge && typeof bridge.setSetting === "function") bridge.setSetting("theme", btn.dataset.theme);
      showToast("Theme: " + btn.dataset.theme);
    }));

    /* ── Range sliders — update display label ── */
    [["editorFontSize", "editorFontSizeVal", "px"], ["uiScale", "uiScaleVal", "%"], ["termFontSize", "termFontSizeVal", "px"], ["watcherDebounce", "watcherDebounceVal", "ms"], ["requestTimeout", "requestTimeoutVal", "s"]].forEach(([i, v, u]) => { const inp = $(i), val = $(v); if (inp && val) inp.addEventListener("input", () => { val.textContent = inp.value + u; persistSetting(i, inp.value); }); });

    /* ── All toggles, selects, inputs → live persist ── */
    document.querySelectorAll(".switch input").forEach(t => t.addEventListener("change", () => persistSetting(t.id, t.checked)));
    document.querySelectorAll(".setting-select").forEach(s => s.addEventListener("change", () => persistSetting(s.id, s.value)));
    document.querySelectorAll(".setting-input, .setting-textarea").forEach(i => i.addEventListener("blur", () => persistSetting(i.id, i.value)));

    /* ── Memory controls ── */
    $("enabledToggle")?.addEventListener("change", () => { state.enabled = $("enabledToggle").checked; $("statusDot")?.classList.toggle("enabled", state.enabled); callBridge(["setMemoryEnabled", "setEnabled", "toggle_memory"], [state.enabled]).catch(() => { }); });
    document.querySelectorAll(".scope-tab").forEach(tab => tab.addEventListener("click", () => { state.activeScope = tab.dataset.scope; document.querySelectorAll(".scope-tab").forEach(t => t.classList.toggle("active", t === tab)); callBridge(["setActiveScope", "setScope", "switch_scope"], [state.activeScope]).catch(() => { }); renderMemoryList(); }));
    $("searchInput")?.addEventListener("input", (e) => { uiState.query = e.target.value; renderMemoryList(); });
    $("refreshBtn")?.addEventListener("click", () => callBridge(["refresh", "refreshMemories", "loadInitialData"], []).then(s => { if (s) window.receiveMemoryState(s); showToast("Refreshed"); }).catch(() => showToast("Refresh failed")));
    $("clearBtn")?.addEventListener("click", () => { if (confirm("Clear ALL memories? This cannot be undone.")) callBridge(["clearAll", "clear_all_memories"], [state.activeScope]).then(() => { state.scopes.project.memories = []; state.scopes.global.memories = []; renderMemoryList(); showToast("All memories cleared"); }).catch(() => showToast("Clear failed")); });
    $("statsBtn")?.addEventListener("click", () => callBridge(["getMemoryStats", "getStats", "get_memory_stats"], [state.activeScope]).then(s => { if (s) showToast(JSON.stringify(s).slice(0, 200)); }).catch(() => showToast("Stats unavailable")));
    $("consolidateBtn")?.addEventListener("click", () => callBridge(["runConsolidation", "consolidate", "consolidate_memories"], [state.activeScope, true]).then(() => { showToast("Consolidation complete"); $("consolidationModal")?.classList.remove("hidden"); }).catch(() => showToast("Consolidation unavailable")));
    $("closeConsolidationModal")?.addEventListener("click", () => $("consolidationModal")?.classList.add("hidden"));
    $("closeConsolidationModalBtn")?.addEventListener("click", () => $("consolidationModal")?.classList.add("hidden"));
    $("openRulesBtn")?.addEventListener("click", () => callBridge(["openRulesDir", "openRules", "edit_rules"], [state.activeScope]).catch(() => showToast("Rules editor unavailable")));
    $("syncGlobalBtn")?.addEventListener("click", () => { const root = state.scopes.project.projectRoot || ""; callBridge(["syncGlobalMemoriesToProject", "syncGlobal", "sync_global_memories"], [root, true]).then(() => showToast("Synced")).catch(() => showToast("Sync unavailable")); });

    /* ── Escape ── */
    document.addEventListener("keydown", (e) => { if (e.key === "Escape") { $("consolidationModal")?.classList.add("hidden"); $("modalHost")?.classList.add("hidden"); } });

    /* ── Init bridge ── */
    initBridge();

    /* ── Standalone demo data (only when no bridge) ── */
    setTimeout(() => {
      if (!bridge && !window.__memMgrDebug.loaded) {
        console.info("[SETTINGS] Standalone mode — loading demo data");
        state.scopes.project.memories = [
          { id: "1", title: "Project uses Django 4.2", content: "This project is built on Django 4.2 with PostgreSQL. REST API uses DRF.", type: "architecture", created_at: new Date(Date.now() - 3600000).toISOString() },
          { id: "2", title: "Prefer pytest over unittest", content: "User prefers pytest for all testing. Fixtures in conftest.py.", type: "preference", created_at: new Date(Date.now() - 86400000).toISOString() },
          { id: "3", title: "Database: PostgreSQL 16", content: "Production uses PostgreSQL 16 on AWS RDS. Connection pooling via pgBouncer.", type: "infrastructure", created_at: new Date(Date.now() - 172800000).toISOString() },
        ];
        renderMemoryList();
      }
    }, 1000);
  });
})();
