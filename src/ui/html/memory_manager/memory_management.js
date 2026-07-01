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
    mimoKey: "ai.mimo_key",
    openrouterKey: "ai.openrouter_key",
    alibabaKey: "ai.alibaba_key",
    kimiKey: "ai.kimi_key",
    mistralKey: "ai.mistral_key",
    siliconflowKey: "ai.siliconflow_key",
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
          /* Load profile and usage data after bridge connects */
          loadProfile();
          loadUsageStats();
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

    /* ── Default Model → also sync chat panel model button ── */
    const modelSelect = $("defaultModel");
    if (modelSelect) {
      modelSelect.addEventListener("change", () => {
        const value = modelSelect.value;
        const label = modelSelect.options[modelSelect.selectedIndex].text;
        persistSetting("defaultModel", value);
        /* Notify chat panel to update model button */
        if (bridge && typeof bridge.setDefaultModel === "function") {
          bridge.setDefaultModel(value, label);
        } else if (bridge && typeof bridge.setSetting === "function") {
          bridge.setSetting("ai.model", value);
          bridge.setSetting("ai.model_label", label);
        }
        showToast("Default model: " + label);
      });
    }

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
    document.addEventListener("keydown", (e) => { if (e.key === "Escape") { $("consolidationModal")?.classList.add("hidden"); $("modalHost")?.classList.add("hidden"); closeEditProfileModal(); } });

    /* ═══════════════════════════════════════════════════════════════
       PROFILE & USAGE
       ═══════════════════════════════════════════════════════════════ */

    /* ── Token formatting ── */
    function formatTokens(n) {
      if (!n || n === 0) return '0';
      if (n >= 1e9) return (n / 1e9).toFixed(1) + 'B';
      if (n >= 1e6) return (n / 1e6).toFixed(1) + 'M';
      if (n >= 1e3) return (n / 1e3).toFixed(1) + 'K';
      return n.toString();
    }

    function formatDuration(seconds) {
      if (!seconds || seconds === 0) return '0m 0s';
      if (seconds >= 3600) return Math.floor(seconds / 3600) + 'h ' + Math.floor((seconds % 3600) / 60) + 'm';
      if (seconds >= 60) return Math.floor(seconds / 60) + 'm ' + (seconds % 60) + 's';
      return seconds + 's';
    }

    /* ── Cached usage data for chart range switching ── */
    const MODEL_NAMES={'mimo-v2.5':{n:'MiMo V2.5',c:'#f97316'},'mimo-v2.5-pro':{n:'MiMo V2.5 Pro',c:'#f97316'},'deepseek-v4':{n:'DeepSeek V4',c:'#a78bfa'},'deepseek-v4-pro':{n:'DeepSeek V4 Pro',c:'#a78bfa'},'gpt-5.5':{n:'GPT-5.5',c:'#10b981'},'gpt-5.4':{n:'GPT-5.4',c:'#10b981'},'gpt-4o':{n:'GPT-4o',c:'#10b981'},'claude-opus':{n:'Claude Opus',c:'#d77b4a'},'claude-sonnet':{n:'Claude Sonnet',c:'#d77b4a'},'qwen3.7-plus':{n:'Qwen 3.7 Plus',c:'#f59e0b'},'qwen3.6-plus':{n:'Qwen 3.6 Plus',c:'#f59e0b'},'gemini-2.5-pro':{n:'Gemini 2.5 Pro',c:'#4285f4'},'glm-5.2':{n:'GLM 5.2',c:'#eab308'},'mistral-large-latest':{n:'Mistral Large',c:'#f43f5e'}};
    const _IM=/^(test|mock|fake|placeholder|unknown|default)/i;
    function isValidModel(id){return id&&typeof id==='string'&&id.length>=2&&id.length<=80&&!_IM.test(id);} function getModelName(id){if(!id)return'Unknown';var m=MODEL_NAMES[id];if(m)return m.n;return id.split(/[-_]/).map(function(w){return w.charAt(0).toUpperCase()+w.slice(1);}).join(' ');} function getModelColor(id){var m=MODEL_NAMES[id];return m?m.c:'#6b7280';}
    let _cachedUsageData = null;

    /* ── Chart pagination state ── */
    const ITEMS_PER_PAGE = { daily: 14, weekly: 8, cumulative: 12 };
    let _chartPage = 9999; /* Start on last page (most recent data), gets clamped in renderActivityChart */
    let _chartTotalPages = 1;
    let _chartRange = 'daily';
    let _allChartPoints = [];

    /* ── Load profile data from bridge ── */
    function loadProfile() {
      /* First check auth status */
      callBridge(["getAuthStatus"], []).then(authData => {
        let auth = {};
        try { auth = typeof authData === 'string' ? JSON.parse(authData) : authData || {}; } catch(e) {}
        
        const isLoggedIn = auth.logged_in || false;
        
        /* Show/hide login card vs profile hero */
        const loginCard = $("loginCard");
        const profileHero = $("profileHero");
        
        if (loginCard) loginCard.style.display = isLoggedIn ? 'none' : 'block';
        if (profileHero) profileHero.style.display = isLoggedIn ? 'flex' : 'none';
        
        /* If logged in, populate profile from auth data */
        if (isLoggedIn && auth.user) {
          const u = auth.user;
          const name = u.display_name || u.email?.split('@')[0] || 'User';
          const email = u.email || '';
          const initials = name.charAt(0).toUpperCase() + (name.split(' ')[1]?.charAt(0) || '').toUpperCase();
          
          /* Plan display — use plan_display from API or fallback */
          const planMap = { 'starter': 'Starter', 'pro': 'Pro' };
          const planText = u.plan_display || planMap[u.plan] || 'Free';
          
          if ($("profileAvatar")) $("profileAvatar").textContent = initials;
          if ($("profileName")) $("profileName").textContent = name;
          if ($("profileEmail")) $("profileEmail").textContent = email;
          if ($("profilePlan")) $("profilePlan").textContent = planText;
          if ($("serverStatus")) $("serverStatus").textContent = 'Connected';
        }
      }).catch(() => {});
      
      /* Also fetch full profile from server for more details */
      callBridge(["getProfile", "get_profile"], []).then(data => {
        if (!data) return;
        try {
          const p = typeof data === 'string' ? JSON.parse(data) : data;
          const profile = p.profile || p;
          const auth = p.auth || {};
          
          /* Use server data if available */
          const displayName = auth.display_name || profile.display_name || '';
          const email = auth.email || profile.email || '';
          
          /* Plan display — use plan_display from API or fallback */
          const planMap = { 'pro': 'Pro', 'pro_yearly': 'Pro (Yearly)' };
          const planText = auth.plan_display || profile.plan_display || planMap[auth.plan || profile.plan] || 'Free';
          
          if (displayName && $("profileName")) $("profileName").textContent = displayName;
          if (email && $("profileEmail")) $("profileEmail").textContent = email;
          if ($("profilePlan")) $("profilePlan").textContent = planText;
          
          /* Update avatar initials */
          if (displayName && $("profileAvatar")) {
            const parts = displayName.split(' ');
            const initials = parts[0].charAt(0).toUpperCase() + (parts[1]?.charAt(0) || '').toUpperCase();
            $("profileAvatar").textContent = initials;
          }
        } catch (e) { console.error('[PROFILE] parse error:', e); }
      }).catch(() => { /* no bridge — use defaults */ });
    }

    /* ── Auth functions ── */
    function startBrowserLogin() {
      const status = $("loginStatus");
      const error = $("loginError");
      if (status) status.textContent = 'Checking server connection...';
      if (error) error.style.display = 'none';
      
      callBridge(["startLogin"], []).then(ok => {
        if (ok) {
          if (status) status.textContent = 'Browser opened. Complete login there, then return here.';
        } else {
          if (status) status.textContent = '';
          if (error) {
            error.textContent = 'Cannot connect to server. Make sure Django is running.';
            error.style.display = 'block';
          }
        }
      }).catch(() => {
        if (status) status.textContent = '';
        if (error) {
          error.textContent = 'Login failed. Check server connection.';
          error.style.display = 'block';
        }
      });
    }
    
    function logoutUser() {
      callBridge(["logout"], []).then(() => {
        loadProfile();
        loadUsageStats();
      }).catch(() => {});
    }
    
    /* Expose auth functions to global scope for inline onclick handlers */
    window.startBrowserLogin = startBrowserLogin;
    window.logoutUser = logoutUser;
    window.openUpgradePage = openUpgradePage;
    window.loadProfile = loadProfile;
    window.loadUsageStats = loadUsageStats;

    /* ── Apply usage data to UI (shared by bridge + demo) ── */
    function applyUsageData(u) {
      if (!u) return;
      const life = u.lifetime || {};
      const peak = u.peak || {};
      const streaks = u.streaks || {};
      const period = u.current_period || {};
      const models = u.model_usage || {};
      const insights = u.insights || {};

      /* Update stat cards */
      if ($("lifetimeTokens")) $("lifetimeTokens").textContent = formatTokens(life.total_tokens);
      if ($("peakTokens")) $("peakTokens").textContent = formatTokens(peak.peak_tokens_single_session);
      if ($("longestTask")) $("longestTask").textContent = formatDuration(life.longest_task_seconds);
      if ($("currentStreak")) $("currentStreak").textContent = (streaks.current_streak_days || 0) + ' days';
      if ($("longestStreak")) $("longestStreak").textContent = (streaks.longest_streak_days || 0) + ' days';

      /* Update usage meters */
      const monthlyPct = period.tokens_limit ? Math.round((period.tokens_used / period.tokens_limit) * 100) : 0;
      if ($("monthlyPercent")) $("monthlyPercent").textContent = monthlyPct + '%';
      if ($("monthlyFill")) { $("monthlyFill").style.width = monthlyPct + '%'; $("monthlyFill").className = 'meter-fill' + (monthlyPct > 85 ? ' danger' : monthlyPct > 60 ? ' warning' : ''); }
      if ($("monthlyDetail")) $("monthlyDetail").textContent = formatTokens(period.tokens_used) + ' / ' + formatTokens(period.tokens_limit) + ' tokens';
      if ($("monthlyReset") && period.end_date) $("monthlyReset").textContent = 'Resets ' + period.end_date;

      const dailyPct = period.requests_limit ? Math.round((period.requests_used / period.requests_limit) * 100) : 0;
      if ($("dailyPercent")) $("dailyPercent").textContent = dailyPct + '%';
      if ($("dailyFill")) { $("dailyFill").style.width = dailyPct + '%'; $("dailyFill").className = 'meter-fill' + (dailyPct > 85 ? ' danger' : dailyPct > 60 ? ' warning' : ''); }
      if ($("dailyDetail")) $("dailyDetail").textContent = (period.requests_used || 0) + ' / ' + (period.requests_limit || 0) + ' requests';

      /* Update insights */
      const fastMode = insights.fast_mode_percent || 0;
      if ($("fastModePercent")) $("fastModePercent").textContent = fastMode > 0 ? fastMode + '%' : 'N/A';
      if ($("fastModeBar")) $("fastModeBar").style.width = fastMode + '%';
      
      const reasoningLevel = insights.most_reasoning_level || 'none';
      const reasoningPercent = insights.reasoning_percent || 0;
      if ($("reasoningLevel")) {
        if (reasoningLevel === 'none' || reasoningPercent === 0) {
          $("reasoningLevel").textContent = 'Not tracked';
        } else {
          $("reasoningLevel").textContent = reasoningLevel.charAt(0).toUpperCase() + reasoningLevel.slice(1) + ' - ' + reasoningPercent + '%';
        }
      }
      if ($("reasoningBar")) $("reasoningBar").style.width = reasoningPercent + '%';
      
      const skillsExplored = insights.skills_explored || [];
      if ($("skillsExplored")) $("skillsExplored").textContent = skillsExplored.length > 0 ? skillsExplored.slice(0, 5).join(', ') + (skillsExplored.length > 5 ? '...' : '') : 'None yet';
      if ($("totalSkills")) $("totalSkills").textContent = (insights.total_skills_used || 0) > 0 ? insights.total_skills_used : '0';

      /* Update model usage list */
      const modelList = $("modelUsageList");
      if (modelList && Object.keys(models).length > 0) {
        const sorted = Object.entries(models).filter(function(e){return isValidModel(e[0]);}).sort((a, b) => (b[1].total_tokens || 0) - (a[1].total_tokens || 0));
        const maxTokens = sorted.length > 0 ? sorted[0][1].total_tokens : 1;
        modelList.innerHTML = sorted.map(([name, info], i) => {
          const pct = Math.round((info.total_tokens / maxTokens) * 100);
          return '<div class="model-usage-item">' +
            '<span class="model-usage-rank">' + (i + 1) + '</span>' +
            '<div class="model-usage-bar-container">' +
            '<div class="model-usage-name">' + esc(getModelName(name)) + '</div>' +
            '<div class="model-usage-bar"><div class="model-usage-fill" style="width:' + pct + '%;background:' + getModelColor(name) + '"></div></div>' +
            '</div>' +
            '<span class="model-usage-percent">' + formatTokens(info.total_tokens) + '</span>' +
            '</div>';
        }).join('');
      }

      /* Update local usage list (AI Model Usage) */
      const localList = $("localUsageList");
      if (localList && Object.keys(models).length > 0) {
        const sorted = Object.entries(models).filter(function(e){return isValidModel(e[0]);}).sort((a, b) => (b[1].total_tokens || 0) - (a[1].total_tokens || 0));
        const maxTokens = sorted.length > 0 ? sorted[0][1].total_tokens : 1;
        localList.innerHTML = sorted.map(([name, info], i) => {
          const pct = Math.round((info.total_tokens / maxTokens) * 100);
          return '<div style="display:flex;align-items:center;gap:12px;padding:8px 0;' + (i < sorted.length - 1 ? 'border-bottom:1px solid rgba(255,255,255,0.05);' : '') + '">' +
            '<span style="font-size:12px;color:#8b949e;width:20px;">' + (i + 1) + '</span>' +
            '<div style="flex:1;">' +
            '<div style="font-size:13px;color:#e5e7eb;margin-bottom:4px;">' + esc(getModelName(name)) + '</div>' +
            '<div style="height:4px;background:rgba(255,255,255,0.05);border-radius:2px;overflow:hidden;"><div style="height:100%;width:' + pct + '%;background:' + getModelColor(name) + ';border-radius:2px;"></div></div>' +
            '</div>' +
            '<span style="font-size:12px;color:#8b949e;min-width:60px;text-align:right;">' + formatTokens(info.total_tokens) + '</span>' +
            '</div>';
        }).join('');
      } else if (localList) {
        localList.innerHTML = '<div class="empty-state-small"><p>No model usage data yet</p></div>';
      }

      /* Update model breakdown in Usage section */
      const breakdown = $("modelBreakdown");
      if (breakdown && Object.keys(models).length > 0) {
        const sorted = Object.entries(models).filter(function(e){return isValidModel(e[0]);}).sort((a, b) => (b[1].total_tokens || 0) - (a[1].total_tokens || 0));
        const maxTokens = sorted.length > 0 ? sorted[0][1].total_tokens : 1;
        breakdown.innerHTML = sorted.map(([name, info]) => {
          const pct = Math.round((info.total_tokens / maxTokens) * 100);
          return '<div class="model-breakdown-item">' +
            '<div class="model-breakdown-header">' +
            '<span class="model-breakdown-name">' + esc(getModelName(name)) + '</span>' +
            '<span class="model-breakdown-tokens">' + formatTokens(info.total_tokens) + ' tokens</span>' +
            '</div>' +
            '<div class="model-breakdown-bar"><div class="model-breakdown-fill" style="width:' + pct + '%;background:' + getModelColor(name) + '"></div></div>' +
            '</div>';
        }).join('');
      }

      /* Render activity chart with current range */
      const activeTab = document.querySelector('.activity-tab.active');
      const range = activeTab ? activeTab.dataset.range : 'daily';
      renderActivityChart(u.daily_usage || {}, range);
    }

    /* ── Load usage stats from bridge ── */
    function loadUsageStats() {
      callBridge(["getUsageStats", "get_usage_stats"], []).then(data => {
        if (!data) return;
        try {
          const u = typeof data === 'string' ? JSON.parse(data) : data;
          _cachedUsageData = u;
          applyUsageData(u);
          
          /* Update server data if available */
          const server = u.server || {};
          const sub = server.subscription || {};
          const credits = server.credits || {};
          const usage = server.usage || {};
          
          /* Update plan card with server data */
          const hasPlan = sub.plan && sub.plan !== 'none' && sub.plan !== 'free';
          const planNames = { 'pro': 'Pro', 'pro_yearly': 'Pro (Yearly)', 'starter': 'Starter', 'free': 'Free' };
          
          if ($("planName")) {
            $("planName").textContent = hasPlan ? (planNames[sub.plan] || sub.plan) : 'Free';
          }
          // Hide price display
          if ($("planPrice")) {
            $("planPrice").textContent = '';
            $("planPrice").style.display = 'none';
          }
          
          // Show/hide upgrade button and features
          const upgradeBtn = $("upgradePlanBtn");
          const planFeatures = $("planFeatures");
          const serviceCard = $("serviceUsageCard");
          
          if (hasPlan) {
            // User has subscription - hide upgrade, show features
            if (upgradeBtn) upgradeBtn.style.display = 'none';
            if (planFeatures) planFeatures.style.display = 'block';
            if (serviceCard) serviceCard.style.display = 'block';
          } else {
            // No subscription - show upgrade, hide features
            if (upgradeBtn) upgradeBtn.style.display = 'inline-block';
            if (planFeatures) planFeatures.style.display = 'none';
            if (serviceCard) serviceCard.style.display = 'none';
          }
          
          /* Show credits if available */
          if (credits.monthly_allocation > 0) {
            const creditsInfo = $("creditsInfo");
            if (creditsInfo) creditsInfo.style.display = 'block';
            if ($("creditBalance")) $("creditBalance").textContent = '$' + (credits.balance || 0).toFixed(2);
            if ($("creditsUsed")) $("creditsUsed").textContent = '$' + (credits.used_this_month || 0).toFixed(2);
          }
          
          /* Show server usage card */
          if (usage.tokens_this_month > 0) {
            const serverCard = $("serverUsageCard");
            if (serverCard) serverCard.style.display = 'block';
            const serverInfo = $("serverUsageInfo");
            if (serverInfo) {
              serverInfo.innerHTML = 
                'Tokens this month: <strong>' + formatTokens(usage.tokens_this_month) + '</strong><br>' +
                'Requests this month: <strong>' + (usage.requests_this_month || 0) + '</strong>';
            }
          }
        } catch (e) { console.error('[USAGE] parse error:', e); }
      }).catch(() => { /* no bridge — use defaults */ });
    }
    
    function openUpgradePage() {
      const url = 'http://127.0.0.1:8000/pricing/';
      // Try bridge method first (opens in system browser)
      if (bridge && typeof bridge.openExternal === 'function') {
        bridge.openExternal(url);
      } else {
        window.open(url, '_blank');
      }
    }

    /* ── Render activity chart with pagination ── */
    function renderActivityChart(dailyUsage, range) {
      const bars = $("chartBars");
      if (!bars) return;
      range = range || 'daily';
      _chartRange = range;

      const allDays = Object.keys(dailyUsage).sort();
      if (allDays.length === 0) {
        bars.innerHTML = '<div class="empty-state-small"><p>No activity data yet</p></div>';
        _allChartPoints = [];
        updatePagination(1, 1);
        return;
      }

      let points = [];

      if (range === 'daily') {
        /* Only days with actual usage — skip zero-value to avoid gaps */
        allDays.forEach(d => {
          const val = (dailyUsage[d] || {}).tokens || 0;
          if (val > 0) points.push({ label: d.slice(5), value: val });
        });
      } else if (range === 'weekly') {
        /* Aggregate into weekly buckets */
        const weeks = {};
        allDays.forEach(d => {
          const dt = new Date(d);
          const weekStart = new Date(dt);
          weekStart.setDate(dt.getDate() - dt.getDay());
          const key = weekStart.toISOString().slice(0, 10);
          if (!weeks[key]) weeks[key] = 0;
          weeks[key] += (dailyUsage[d] || {}).tokens || 0;
        });
        const weekKeys = Object.keys(weeks).sort();
        const months = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
        weekKeys.forEach(w => {
          if (weeks[w] > 0) {
            const dt = new Date(w);
            points.push({ label: months[dt.getMonth()] + ' ' + dt.getDate(), value: weeks[w] });
          }
        });
      } else if (range === 'cumulative') {
        /* Monthly aggregation */
        const months = {};
        allDays.forEach(d => {
          const key = d.slice(0, 7); /* YYYY-MM */
          if (!months[key]) months[key] = 0;
          months[key] += (dailyUsage[d] || {}).tokens || 0;
        });
        const monthKeys = Object.keys(months).sort();
        const monthNames = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
        monthKeys.forEach(m => {
          if (months[m] > 0) {
            const dt = new Date(m + '-01');
            points.push({ label: monthNames[dt.getMonth()], value: months[m] });
          }
        });
      }

      _allChartPoints = points;
      if (points.length === 0) {
        bars.innerHTML = '<div class="empty-state-small"><p>No activity data yet</p></div>';
        updatePagination(1, 1);
        return;
      }

      /* Calculate pagination */
      const perPage = ITEMS_PER_PAGE[range] || 14;
      _chartTotalPages = Math.ceil(points.length / perPage);
      /* Default to last page (most recent data) */
      if (_chartPage > _chartTotalPages) _chartPage = _chartTotalPages;
      if (_chartPage < 1) _chartPage = 1;

      const startIdx = (_chartPage - 1) * perPage;
      const pagePoints = points.slice(startIdx, startIdx + perPage);

      const maxVal = Math.max(...pagePoints.map(p => p.value), 1);
      bars.innerHTML = pagePoints.map(p => {
        const pct = Math.round((p.value / maxVal) * 100);
        return '<div class="chart-column">' +
          '<div class="chart-bar" style="height:' + Math.max(pct, 4) + '%" title="' + p.label + ': ' + formatTokens(p.value) + '"></div>' +
          '<span class="chart-label">' + p.label + '</span>' +
          '</div>';
      }).join('');

      updatePagination(_chartPage, _chartTotalPages);
    }

    /* ── Update pagination controls ── */
    function updatePagination(current, total) {
      const pagination = $("chartPagination");
      if (!pagination) return;

      if (total <= 1) {
        pagination.classList.add('hidden');
        return;
      }
      pagination.classList.remove('hidden');

      /* Update nav arrow states */
      const firstBtn = $("pageFirst");
      const prevBtn = $("pagePrev");
      const nextBtn = $("pageNext");
      const lastBtn = $("pageLast");

      if (firstBtn) firstBtn.disabled = (current <= 1);
      if (prevBtn) prevBtn.disabled = (current <= 1);
      if (nextBtn) nextBtn.disabled = (current >= total);
      if (lastBtn) lastBtn.disabled = (current >= total);

      /* Render page number buttons (show max 5 page numbers) */
      const pageNumbers = $("pageNumbers");
      if (pageNumbers) {
        let pages = [];
        const maxVisible = 5;
        let start = Math.max(1, current - Math.floor(maxVisible / 2));
        let end = Math.min(total, start + maxVisible - 1);
        if (end - start + 1 < maxVisible) {
          start = Math.max(1, end - maxVisible + 1);
        }

        /* Add ellipsis + first page if needed */
        if (start > 1) {
          pages.push(1);
          if (start > 2) pages.push('...');
        }
        for (let i = start; i <= end; i++) {
          pages.push(i);
        }
        /* Add ellipsis + last page if needed */
        if (end < total) {
          if (end < total - 1) pages.push('...');
          pages.push(total);
        }

        pageNumbers.innerHTML = pages.map(p => {
          if (p === '...') {
            return '<span style="color:var(--muted);font-size:12px;padding:0 4px;">…</span>';
          }
          return '<button class="page-btn' + (p === current ? ' active' : '') + '" data-page="' + p + '">' + p + '</button>';
        }).join('');

        /* Bind click events on page buttons */
        pageNumbers.querySelectorAll('.page-btn').forEach(btn => {
          btn.addEventListener('click', () => {
            const page = parseInt(btn.dataset.page, 10);
            if (page && page !== _chartPage) {
              _chartPage = page;
              renderActivityChart(_cachedUsageData?.daily_usage || {}, _chartRange);
            }
          });
        });
      }

      /* Page info text */
      const pageInfo = $("pageInfo");
      if (pageInfo) {
        const perPage = ITEMS_PER_PAGE[_chartRange] || 14;
        const startItem = (current - 1) * perPage + 1;
        const endItem = Math.min(current * perPage, _allChartPoints.length);
        pageInfo.textContent = startItem + '–' + endItem + ' of ' + _allChartPoints.length;
      }
    }

    /* ── Activity tab toggle ── */
    document.querySelectorAll('.activity-tab').forEach(tab => {
      tab.addEventListener('click', () => {
        document.querySelectorAll('.activity-tab').forEach(t => t.classList.remove('active'));
        tab.classList.add('active');
        const range = tab.dataset.range;
        /* Reset to last page (most recent data) on range change */
        _chartPage = 9999; /* will be clamped to last page in renderActivityChart */
        if (_cachedUsageData) {
          renderActivityChart(_cachedUsageData.daily_usage || {}, range);
        } else {
          loadUsageStats();
        }
      });
    });

    /* ── Pagination nav button handlers ── */
    $("pageFirst")?.addEventListener('click', () => {
      if (_chartPage > 1) { _chartPage = 1; renderActivityChart(_cachedUsageData?.daily_usage || {}, _chartRange); }
    });
    $("pagePrev")?.addEventListener('click', () => {
      if (_chartPage > 1) { _chartPage--; renderActivityChart(_cachedUsageData?.daily_usage || {}, _chartRange); }
    });
    $("pageNext")?.addEventListener('click', () => {
      if (_chartPage < _chartTotalPages) { _chartPage++; renderActivityChart(_cachedUsageData?.daily_usage || {}, _chartRange); }
    });
    $("pageLast")?.addEventListener('click', () => {
      if (_chartPage < _chartTotalPages) { _chartPage = _chartTotalPages; renderActivityChart(_cachedUsageData?.daily_usage || {}, _chartRange); }
    });

    /* ── Edit Profile modal (using CSS classes) ── */
    function closeEditProfileModal() {
      const existing = document.querySelector('.edit-profile-modal');
      if (existing) existing.remove();
    }

    const editBtn = $("editProfileBtn");
    if (editBtn) {
      editBtn.addEventListener('click', () => {
        closeEditProfileModal(); /* close any existing */
        const currentName = $("profileName")?.textContent || 'User';
        const currentUsername = $("profileUsername")?.textContent?.replace('@', '') || 'user';
        const currentInitials = $("profileAvatar")?.textContent || 'HA';
        /* Extract the first #hex color from the background style */
        const bgStyle = $("profileAvatar")?.style.background || '';
        const colorMatch = bgStyle.match(/#[0-9a-fA-F]{6}/);
        const currentColor = colorMatch ? colorMatch[0] : '#f97316';
        const colors = ['#f97316', '#3b82f6', '#8b5cf6', '#10b981', '#ef4444', '#f59e0b', '#ec4899', '#06b6d4'];

        const overlay = document.createElement('div');
        overlay.className = 'edit-profile-modal';
        overlay.innerHTML =
          '<div class="ep-card">' +
          '<h2>Edit Profile</h2>' +
          '<div class="ep-avatar-wrap">' +
          '<div class="ep-avatar" id="editAvatarPreview" style="background:linear-gradient(135deg,' + currentColor + ',' + currentColor + ')">' + currentInitials + '</div>' +
          '<div class="ep-colors">' +
          colors.map(c => '<button class="ep-color-dot' + (c === currentColor ? ' selected' : '') + '" data-color="' + c + '" style="background:' + c + '"></button>').join('') +
          '</div></div>' +
          '<div class="ep-field"><label>Display Name</label><input type="text" id="editDisplayName" value="' + esc(currentName) + '"></div>' +
          '<div class="ep-field"><label>Username</label><input type="text" id="editUsername" value="' + esc(currentUsername) + '"></div>' +
          '<div class="ep-actions">' +
          '<button class="setting-btn" id="epCancelBtn">Cancel</button>' +
          '<button class="setting-btn primary-btn" id="epSaveBtn">Save</button>' +
          '</div></div>';

        document.body.appendChild(overlay);

        /* Color dot selection */
        overlay.querySelectorAll('.ep-color-dot').forEach(dot => {
          dot.addEventListener('click', () => {
            overlay.querySelectorAll('.ep-color-dot').forEach(d => d.classList.remove('selected'));
            dot.classList.add('selected');
            const c = dot.dataset.color;
            const avatar = overlay.querySelector('#editAvatarPreview');
            if (avatar) avatar.style.background = 'linear-gradient(135deg,' + c + ',' + c + ')';
          });
        });

        /* Cancel */
        overlay.querySelector('#epCancelBtn')?.addEventListener('click', closeEditProfileModal);

        /* Backdrop click */
        overlay.addEventListener('click', (e) => { if (e.target === overlay) closeEditProfileModal(); });

        /* Save */
        overlay.querySelector('#epSaveBtn')?.addEventListener('click', () => {
          const name = $("editDisplayName")?.value || 'User';
          const username = $("editUsername")?.value || 'user';
          const selectedDot = overlay.querySelector('.ep-color-dot.selected') || overlay.querySelector('.ep-color-dot');
          const color = selectedDot?.dataset?.color || '#f97316';
          const initials = name.split(' ').map(w => w[0]).join('').toUpperCase().slice(0, 2);

          /* Update UI */
          if ($("profileAvatar")) { $("profileAvatar").textContent = initials; $("profileAvatar").style.background = 'linear-gradient(135deg, ' + color + ', ' + color + ')'; }
          if ($("profileName")) $("profileName").textContent = name;
          if ($("profileUsername")) $("profileUsername").textContent = '@' + username;

          /* Save to bridge */
          callBridge(["setProfile", "set_profile"], [JSON.stringify({ display_name: name, username: username, avatar_color: color, avatar_initials: initials })]).catch(() => {});
          closeEditProfileModal();
          showToast('Profile saved');
        });
      });
    }

    /* ── Browse plugins link ── */
    $("browsePluginsLink")?.addEventListener('click', (e) => {
      e.preventDefault();
      switchSection('extensions');
    });

    /* ── Upgrade plan button ── */
    $("upgradePlanBtn")?.addEventListener('click', () => {
      showToast('Upgrade plan — coming soon!');
    });

    /* ── Load profile/usage when bridge connects ── */
    /* Already called above — load data after a delay */
    setTimeout(() => { loadProfile(); loadUsageStats(); }, 2000);

    /* ── Refresh profile/usage when switching to those sections ── */
    document.querySelectorAll('.nav-item[data-section="profile"], .nav-item[data-section="usage"]').forEach(btn => {
      btn.addEventListener('click', () => { loadProfile(); loadUsageStats(); });
    });

    /* ── Init bridge ── */
    initBridge();

    /* ── Load profile data on init (check if logged in) ── */
    // Wait a bit for bridge to connect, then load data
    setTimeout(() => {
      loadProfile();
      loadUsageStats();
    }, 500);

    /* ═══════════════════════════════════════════════════════════════
       API KEY MANAGEMENT
       ═══════════════════════════════════════════════════════════════ */
    
    const PROVIDER_CONFIG = {
      mimo:      { input: 'mimoKey',      mask: 'mimoKeyMask',      eye: 'mimoEye',      test: 'mimoTest',      remove: 'mimoRemove',      settingsKey: 'ai.mimo_key',      kmName: 'mimo' },
      deepseek:  { input: 'deepseekKey',  mask: 'deepseekKeyMask',  eye: 'deepseekEye',  test: 'deepseekTest',  remove: 'deepseekRemove',  settingsKey: 'ai.deepseek_key',  kmName: 'deepseek' },
      openai:    { input: 'openaiKey',    mask: 'openaiKeyMask',    eye: 'openaiEye',    test: 'openaiTest',    remove: 'openaiRemove',    settingsKey: 'ai.openai_key',    kmName: 'openai' },
      openrouter:{ input: 'openrouterKey',mask: 'openrouterKeyMask',eye: 'openrouterEye',test: 'openrouterTest',remove: 'openrouterRemove',settingsKey: 'ai.openrouter_key',kmName: 'openrouter' },
      alibaba:   { input: 'alibabaKey',  mask: 'alibabaKeyMask',   eye: 'alibabaEye',   test: 'alibabaTest',   remove: 'alibabaRemove',   settingsKey: 'ai.alibaba_key',   kmName: 'alibaba' },
    };

    /* Track which providers have keys stored */
    const _providerHasKey = {};
    /* Track which providers were explicitly removed (don't reload from .env) */
    const _providerRemoved = {};

    /* Show masked key when a key is stored */
    function _showMaskedState(provider) {
      const cfg = PROVIDER_CONFIG[provider];
      if (!cfg) return;
      const input = $(cfg.input);
      const mask = $(cfg.mask);
      const row = input?.closest('.provider-row');
      if (input && mask) {
        input.style.display = 'none';
        mask.style.display = 'inline-block';
        mask.textContent = '••••••••••••••••••••••••••••';
        if (row) row.setAttribute('data-has-key', 'true');
        _providerHasKey[provider] = true;
      }
    }

    /* Show input field when editing */
    function _showEditState(provider) {
      const cfg = PROVIDER_CONFIG[provider];
      if (!cfg) return;
      const input = $(cfg.input);
      const mask = $(cfg.mask);
      const row = input?.closest('.provider-row');
      if (input && mask) {
        input.style.display = '';
        input.type = 'password';
        mask.style.display = 'none';
        if (row) row.setAttribute('data-has-key', 'false');
      }
    }

    /* Toggle eye icon */
    function _toggleEye(provider) {
      const cfg = PROVIDER_CONFIG[provider];
      if (!cfg) return;
      const eyeBtn = $(cfg.eye);
      const input = $(cfg.input);
      const mask = $(cfg.mask);
      if (!eyeBtn || !input) return;

      const isActive = eyeBtn.classList.contains('active');
      if (isActive) {
        /* Hide: show masked state */
        eyeBtn.classList.remove('active');
        if (_providerHasKey[provider]) {
          _showMaskedState(provider);
        } else {
          input.type = 'password';
        }
      } else {
        /* Show: reveal key */
        eyeBtn.classList.add('active');
        if (_providerHasKey[provider]) {
          /* Need to load the actual key first */
          if (bridge && typeof bridge.getApiKey === 'function') {
            bridge.getApiKey(cfg.kmName, (key) => {
              if (key) {
                input.style.display = '';
                input.value = key;
                input.type = 'text';
                mask.style.display = 'none';
              }
            });
          } else {
            input.style.display = '';
            input.type = 'text';
            mask.style.display = 'none';
          }
        } else {
          input.type = 'text';
        }
      }
    }

    /* Test connection */
    function _testConnection(provider) {
      const cfg = PROVIDER_CONFIG[provider];
      if (!cfg) return;
      const testBtn = $(cfg.test);
      if (!testBtn) return;

      testBtn.classList.add('testing');
      testBtn.classList.remove('success', 'error');
      testBtn.title = 'Testing...';

      if (bridge && typeof bridge.testApiKey === 'function') {
        bridge.testApiKey(cfg.kmName, (raw) => {
          // QWebChannel returns result=str as a JSON string — parse it
          let result;
          try {
            result = typeof raw === 'string' ? JSON.parse(raw) : raw;
          } catch(e) { result = raw; }
          testBtn.classList.remove('testing');
          if (result && result.success) {
            testBtn.classList.add('success');
            testBtn.title = 'Connected ✓';
            showToast(`${provider} connected successfully`);
          } else {
            testBtn.classList.add('error');
            testBtn.title = 'Failed ✗';
            showToast(`${provider} connection failed: ${result?.error || 'Unknown error'}`);
          }
          setTimeout(() => {
            testBtn.classList.remove('success', 'error');
            testBtn.title = 'Test connection';
          }, 3000);
        });
      } else {
        /* No bridge — simulate */
        testBtn.classList.remove('testing');
        testBtn.classList.add('success');
        testBtn.title = 'Connected ✓';
        setTimeout(() => {
          testBtn.classList.remove('success');
          testBtn.title = 'Test connection';
        }, 2000);
      }
    }

    /* Remove key */
    function _removeKey(provider) {
      const cfg = PROVIDER_CONFIG[provider];
      if (!cfg) return;
      
      if (!confirm(`Remove ${provider} API key? You'll need to re-enter it to use this provider.`)) return;

      /* Mark as removed FIRST (prevents re-appearance from .env on page reload) */
      _providerRemoved[provider] = true;
      _providerHasKey[provider] = false;
      
      /* Always update UI immediately */
      _showEditState(provider);
      const input = $(cfg.input);
      if (input) input.value = '';
      persistSetting(cfg.settingsKey, '');
      showToast(`${provider} key removed`);
      
      /* Try to delete from backend (non-blocking) */
      if (bridge && typeof bridge.removeApiKey === 'function') {
        bridge.removeApiKey(cfg.kmName, (ok) => {
          console.log(`[KEY] removeApiKey(${provider}) backend result:`, ok);
        });
      }
    }

    /* Save key on blur */
    function _saveKey(provider) {
      const cfg = PROVIDER_CONFIG[provider];
      if (!cfg) return;
      const input = $(cfg.input);
      if (!input) return;
      const value = input.value.trim();
      
      /* Don't save empty or placeholder values */
      if (!value || value === '***' || value === '••••••••••••••••') return;

      /* Clear removed flag when saving a new key */
      _providerRemoved[provider] = false;

      /* Save via bridge */
      if (bridge && typeof bridge.setApiKey === 'function') {
        bridge.setApiKey(cfg.kmName, value, (ok) => {
          if (ok) {
            _showMaskedState(provider);
            showToast(`${provider} key saved`);
          } else {
            showToast(`Failed to save ${provider} key`);
          }
        });
      } else {
        /* Fallback: save via setSetting */
        persistSetting(cfg.settingsKey, value);
        _showMaskedState(provider);
        showToast(`${provider} key saved`);
      }
    }

    /* Bind events for all providers */
    Object.entries(PROVIDER_CONFIG).forEach(([provider, cfg]) => {
      const input = $(cfg.input);
      const eyeBtn = $(cfg.eye);
      const testBtn = $(cfg.test);
      const removeBtn = $(cfg.remove);

      /* Eye toggle */
      if (eyeBtn) {
        eyeBtn.addEventListener('click', () => _toggleEye(provider));
      }

      /* Test connection */
      if (testBtn) {
        testBtn.addEventListener('click', () => _testConnection(provider));
      }

      /* Remove key */
      if (removeBtn) {
        removeBtn.addEventListener('click', () => _removeKey(provider));
      }

      /* Save on blur */
      if (input) {
        input.addEventListener('blur', () => _saveKey(provider));
        /* Also save on Enter */
        input.addEventListener('keydown', (e) => {
          if (e.key === 'Enter') {
            input.blur();
          }
        });
      }

      /* Check if key exists and show masked state */
      /* Will be populated when bridge connects */
      _providerHasKey[provider] = false;
    });

    /* Load stored key status when bridge connects */
    function _loadKeyStatus() {
      Object.entries(PROVIDER_CONFIG).forEach(([provider, cfg]) => {
        if (bridge && typeof bridge.getApiKey === 'function') {
          bridge.getApiKey(cfg.kmName, (key) => {
            /* Only show masked state if key is valid (not empty, not placeholder) */
            if (key && key.length > 8 && key !== '***' && key !== '***') {
              _showMaskedState(provider);
            } else {
              /* No valid key — show empty input */
              _showEditState(provider);
              const input = $(cfg.input);
              if (input) input.value = '';
              _providerHasKey[provider] = false;
            }
          });
        } else {
          /* No bridge — check settings for placeholder */
          const settingsVal = state?.settings?.[cfg.settingsKey];
          if (settingsVal && settingsVal !== '***' && settingsVal !== '') {
            _showMaskedState(provider);
          } else {
            _showEditState(provider);
          }
        }
      });
    }

    /* Call after bridge connects */
    if (bridge) {
      _loadKeyStatus();
    } else {
      /* Retry after bridge connects */
      setTimeout(_loadKeyStatus, 2000);
    }

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

        /* Demo profile data */
        const demoProfile = { display_name: 'hakeemph', username: 'hakeemph', avatar_color: '#f97316', avatar_initials: 'HA', plan: 'Free' };
        if ($("profileAvatar")) { $("profileAvatar").textContent = demoProfile.avatar_initials; $("profileAvatar").style.background = 'linear-gradient(135deg, ' + demoProfile.avatar_color + ', ' + demoProfile.avatar_color + ')'; }
        if ($("profileName")) $("profileName").textContent = demoProfile.display_name;
        if ($("profileUsername")) $("profileUsername").textContent = '@' + demoProfile.username;
        if ($("profilePlan")) $("profilePlan").textContent = demoProfile.plan;

        /* Demo usage data — generate realistic daily usage for last 30 days */
        const demoDaily = {};
        for (let i = 30; i >= 0; i--) {
          const d = new Date(); d.setDate(d.getDate() - i);
          const key = d.toISOString().slice(0, 10);
          /* Simulate: some days heavy, some light, some zero */
          const base = [0, 0, 8000, 12000, 25000, 45000, 32000, 0, 5000, 18000, 35000, 42000, 28000, 0, 0, 15000, 22000, 38000, 50000, 31000, 0, 9000, 20000, 27000, 41000, 33000, 0, 0, 11000, 19000, 45000];
          demoDaily[key] = { tokens: base[i] || 0, requests: Math.floor((base[i] || 0) / 3500), tool_calls: Math.floor((base[i] || 0) / 1800), models: {} };
        }

        const demoUsage = {
          lifetime: { total_tokens: 209100000, total_requests: 15420, total_tool_calls: 8230, total_sessions: 342, longest_task_seconds: 614 },
          current_period: { start_date: '2026-06-19', end_date: '2026-07-19', tokens_used: 134000, tokens_limit: 200000, requests_used: 32, requests_limit: 100, tool_calls_used: 440, tool_calls_limit: 0 },
          streaks: { current_streak_days: 0, longest_streak_days: 2 },
          daily_usage: demoDaily,
          model_usage: {
            'deepseek-v4': { total_tokens: 72000, total_requests: 180 },
            'gpt-5.4': { total_tokens: 38000, total_requests: 95 },
            'qwen3.7-plus': { total_tokens: 21000, total_requests: 52 },
            'claude-opus': { total_tokens: 12000, total_requests: 30 }
          },
          insights: { fast_mode_percent: 56, most_reasoning_level: 'medium', reasoning_percent: 53, skills_explored: [], total_skills_used: 0, plugins_used: [] },
          peak: { peak_tokens_single_session: 58100000 }
        };
        _cachedUsageData = demoUsage;
        applyUsageData(demoUsage);
      }
    }, 1000);
  });
})();
