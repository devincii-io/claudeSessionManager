/* ============================================================
   Claude Session Manager — frontend application
   Vanilla JS SPA talking to the Python backend over QWebChannel.
   ============================================================ */

"use strict";

let backend = null;
const State = {
  projects: [],
  projectId: null,
  sessions: [],
  sessionId: null,
  detail: null,
  memory: null,
  settings: null,
  view: "overview", // overview | project | session | memory | settings | monitor | cleanup
  tab: "transcript",
  search: "",
  sel: new Map(),      // selKey -> { pid, sid, title, cost, bytes } — shared multiselect
  selectMode: false,   // project session pane: select-to-delete mode
  cleanup: null,       // getAllSessions payload
  cleanupSort: "size", // size | age | cost
  tune: null,          // Tune view state (see loadTune)
};

/* ---------- multiselect ---------- */

function selKey(pid, sid) { return pid + "␟" + sid; }
function isSel(pid, sid) { return State.sel.has(selKey(pid, sid)); }
function toggleSel(rec) {
  const k = selKey(rec.pid, rec.sid);
  if (State.sel.has(k)) State.sel.delete(k); else State.sel.set(k, rec);
}
function clearSel() { State.sel.clear(); }
// Re-render a scrolling pane without losing the user's scroll position — used
// when toggling selections in a potentially long list.
function keepScroll(paneId, render) {
  const before = document.getElementById(paneId);
  const top = before ? before.scrollTop : 0;
  render();
  const after = document.getElementById(paneId);
  if (after) after.scrollTop = top;
}
function selTotals() {
  let cost = 0, bytes = 0;
  for (const r of State.sel.values()) { cost += r.cost || 0; bytes += r.bytes || 0; }
  return { count: State.sel.size, cost, bytes };
}
function selItems() {
  return [...State.sel.values()].map((r) => ({ project_id: r.pid, session_id: r.sid }));
}

/* ---------- backend plumbing ---------- */

function call(method, ...args) {
  return new Promise((resolve) => {
    if (!backend || typeof backend[method] !== "function") return resolve(null);
    backend[method](...args, (res) => {
      try { resolve(typeof res === "string" ? JSON.parse(res) : res); }
      catch { resolve(res); }
    });
  });
}

/* ---------- formatting helpers ---------- */

const fmt = {
  cost: (c) => "$" + (c || 0).toFixed(2),
  tokens: (n) => {
    n = n || 0;
    if (n >= 1e9) return (n / 1e9).toFixed(2) + "B";
    if (n >= 1e6) return (n / 1e6).toFixed(2) + "M";
    if (n >= 1e3) return (n / 1e3).toFixed(1) + "k";
    return String(n);
  },
  num: (n) => (n || 0).toLocaleString(),
  bytes: (n) => {
    n = n || 0;
    if (n >= 1e6) return (n / 1e6).toFixed(1) + " MB";
    if (n >= 1e3) return (n / 1e3).toFixed(1) + " KB";
    return n + " B";
  },
  rel: (iso) => {
    if (!iso) return "—";
    const t = typeof iso === "number" ? iso * 1000 : Date.parse(iso);
    if (isNaN(t)) return "—";
    const s = (Date.now() - t) / 1000;
    if (s < 60) return "just now";
    if (s < 3600) return Math.floor(s / 60) + "m ago";
    if (s < 86400) return Math.floor(s / 3600) + "h ago";
    if (s < 604800) return Math.floor(s / 86400) + "d ago";
    return new Date(t).toLocaleDateString();
  },
  time: (iso) => {
    if (!iso) return "";
    const t = typeof iso === "number" ? iso * 1000 : Date.parse(iso);
    return isNaN(t) ? "" : new Date(t).toLocaleString();
  },
};

function esc(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

const MODEL_COLORS = {
  "claude-fable-5": "#b98cc9",
  "claude-opus-4-8": "#d97757",
  "claude-opus-4-7": "#e0956f",
  "claude-sonnet-5": "#7aa2c9",
  "claude-sonnet-4-6": "#6fb3ab",
  "claude-haiku-4-5": "#7fae6f",
};
const CHART_PALETTE = ["#d97757", "#7aa2c9", "#b98cc9", "#7fae6f", "#e0b64c", "#6fb3ab", "#e0956f"];
function modelColor(m, i) { return MODEL_COLORS[m] || CHART_PALETTE[i % CHART_PALETTE.length]; }
function shortModel(m) { return (m || "unknown").replace("claude-", ""); }

/* ---------- reusable components ---------- */

function meter(pct, opts = {}) {
  pct = Math.max(0, Math.min(100, pct || 0));
  const filled = Math.round(pct / 10);
  let cls = "meter";
  if (pct > 80) cls += " crit"; else if (pct > 50) cls += " warn";
  let slots = "";
  for (let i = 0; i < 10; i++) slots += `<span class="slot ${i < filled ? "on" : ""}"></span>`;
  return `<span class="${cls}">${slots}</span>`;
}

function meterRow(label, pct, valText) {
  return `<div class="meter-row"><span class="m-label">${esc(label)}</span>${meter(pct)}<span class="m-val">${esc(valText != null ? valText : pct.toFixed(0) + "%")}</span></div>`;
}

function tile(label, value, sub, accent, tip) {
  return `<div class="tile ${accent ? "accent" : ""}" ${tip ? `title="${esc(tip)}"` : ""}><div class="t-label">${esc(label)}</div><div class="t-value">${value}</div>${sub ? `<div class="t-sub">${esc(sub)}</div>` : ""}</div>`;
}

/* Muted one-line explanation under a section title. */
function desc(text) {
  return `<div class="section-desc">${esc(text)}</div>`;
}

function barChart(items) {
  // items: [{label, value, valueText, color}]
  const max = Math.max(1, ...items.map((i) => i.value));
  return `<div class="chart-bars">${items.map((i, idx) => `
    <div class="bar-row">
      <span class="bar-label" title="${esc(i.label)}">${esc(i.label)}</span>
      <span class="bar-track"><span class="bar-fill" style="width:${(100 * i.value / max).toFixed(1)}%;${i.color ? `background:${i.color}` : ""}"></span></span>
      <span class="bar-val">${esc(i.valueText != null ? i.valueText : i.value)}</span>
    </div>`).join("")}</div>`;
}

function donut(items) {
  // items: [{label, value, color}]
  const total = items.reduce((a, b) => a + b.value, 0) || 1;
  const R = 46, C = 2 * Math.PI * R;
  let off = 0;
  const rings = items.map((it) => {
    const frac = it.value / total;
    const seg = `<circle r="${R}" cx="60" cy="60" fill="none" stroke="${it.color}" stroke-width="16"
      stroke-dasharray="${(frac * C).toFixed(2)} ${C.toFixed(2)}" stroke-dashoffset="${(-off * C).toFixed(2)}"
      transform="rotate(-90 60 60)"></circle>`;
    off += frac;
    return seg;
  }).join("");
  const legend = items.map((it) => `<div class="legend-item"><span class="legend-swatch" style="background:${it.color}"></span>${esc(it.label)} <span class="faint">${(100 * it.value / total).toFixed(0)}%</span></div>`).join("");
  return `<div class="donut-wrap">
    <svg width="120" height="120" viewBox="0 0 120 120"><circle r="${R}" cx="60" cy="60" fill="none" stroke="#17150f" stroke-width="16"></circle>${rings}</svg>
    <div class="legend">${legend}</div></div>`;
}

function sparkline(points, color = "#d97757", w = 640, h = 70) {
  if (!points || points.length < 2) return `<div class="faint" style="font-size:12px">Not enough data for a timeline.</div>`;
  const xs = points.map((p) => p.x), ys = points.map((p) => p.y);
  const minX = Math.min(...xs), maxX = Math.max(...xs), maxY = Math.max(...ys) || 1;
  const sx = (x) => ((x - minX) / (maxX - minX || 1)) * (w - 8) + 4;
  const sy = (y) => h - 6 - (y / maxY) * (h - 12);
  const d = points.map((p, i) => `${i ? "L" : "M"}${sx(p.x).toFixed(1)},${sy(p.y).toFixed(1)}`).join(" ");
  const area = `${d} L${sx(maxX).toFixed(1)},${h} L${sx(minX).toFixed(1)},${h} Z`;
  return `<svg width="100%" height="${h}" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none">
    <defs><linearGradient id="sg" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="${color}" stop-opacity="0.28"/><stop offset="1" stop-color="${color}" stop-opacity="0"/></linearGradient></defs>
    <path d="${area}" fill="url(#sg)"/><path d="${d}" fill="none" stroke="${color}" stroke-width="2"/></svg>`;
}

function badge(text, cls = "") { return `<span class="badge ${cls}">${esc(text)}</span>`; }

/* ---------- rail ---------- */

function renderRail() {
  document.querySelectorAll(".nav-item").forEach((n) =>
    n.classList.toggle("active", n.dataset.view === State.view && !State.projectId ||
      (State.view === "settings" && n.dataset.view === "settings") ||
      (State.view === "monitor" && n.dataset.view === "monitor") ||
      (State.view === "overview" && n.dataset.view === "overview" && !State.projectId)));

  const q = State.search.toLowerCase();
  const list = State.projects.filter((p) =>
    !q || p.name.toLowerCase().includes(q) || (p.path || "").toLowerCase().includes(q));
  document.getElementById("project-list").innerHTML = list.map((p) => `
    <div class="project-item ${p.id === State.projectId ? "active" : ""}" data-action="project" data-id="${esc(p.id)}">
      <div class="p-name">${p.active_count ? '<span class="dot-active"></span>' : ""}${esc(p.name)}</div>
      <div class="p-meta">
        <span>${p.session_count} sess</span>
        <span class="p-cost">${fmt.cost(p.total_cost)}</span>
        <span>${fmt.tokens(p.total_tokens)}</span>
      </div>
    </div>`).join("") || `<div class="faint" style="padding:10px;font-size:12px">No projects found.</div>`;
}

/* ---------- list pane ---------- */

function renderListPane() {
  const el = document.getElementById("list-pane");
  // The rail already lists projects — the middle pane only appears inside a
  // project (sessions + memory) so information is never shown twice.
  if (State.projectId) {
    el.style.display = "flex";
    el.innerHTML = projectListPane();
  } else {
    el.style.display = "none";
    el.innerHTML = "";
  }
}

function projectListPane() {
  const p = State.projects.find((x) => x.id === State.projectId);
  const sessions = State.sessions.filter(sessionMatchesSearch);
  const sm = State.selectMode;
  const t = selTotals();
  return `<div class="list-head">
      <h2>${esc(p ? p.name : "Sessions")}</h2>
      <div class="sub" style="display:flex;align-items:center;justify-content:space-between;gap:8px">
        <span>${sessions.length} sessions${p && p.memory_count ? ` · ${p.memory_count} memories` : ""}</span>
        <button class="link-btn" data-action="toggle-select">${sm ? "Cancel" : "Select"}</button>
      </div>
      ${sm ? `<div class="mini-bar">
        <span>${t.count ? `<b>${t.count}</b> selected · ${fmt.bytes(t.bytes)}` : "Tap sessions to select"}</span>
        <span style="display:flex;gap:6px">
          ${t.count ? `<button class="btn sm" data-action="sel-clear">Clear</button>
          <button class="btn sm primary danger" data-action="bulk-delete">Delete ${t.count}</button>` : ""}
        </span></div>` : ""}
    </div>
    <div class="list-body">
      ${sm ? "" : `<div class="file-row ${State.view === "memory" ? "active" : ""}" data-action="memory">
        <div class="file-ic">◇</div>
        <div><div class="f-name">Memory</div><div class="f-desc">${p ? p.memory_count : 0} memory files + index</div></div>
        <div class="file-meta">›</div>
      </div>`}
      ${sessions.map(sessionCard).join("") || '<div class="faint" style="padding:14px;font-size:12px">No sessions.</div>'}
    </div>`;
}

function sessionMatchesSearch(s) {
  const q = State.search.toLowerCase();
  if (!q) return true;
  return (s.title || "").toLowerCase().includes(q) ||
    (s.first_prompt || "").toLowerCase().includes(q) ||
    s.session_id.includes(q);
}

function sessionCard(s) {
  const title = s.title || s.first_prompt || "Untitled session";
  const badges = [];
  if (s.active) badges.push('<span class="badge green"><span class="dot-active"></span> live</span>');
  if (s.has_subagents) badges.push(badge("subagents", "magenta"));
  (s.models || []).slice(0, 2).forEach((m) => badges.push(badge(shortModel(m), "")));
  const sm = State.selectMode;
  const sel = sm && isSel(State.projectId, s.session_id);
  const cls = sel ? "sel" : (s.session_id === State.sessionId ? "active" : "");
  return `<div class="session-card ${sm ? "selectable" : ""} ${cls}" data-action="${sm ? "session-toggle" : "session"}" data-id="${esc(s.session_id)}">
    ${sm ? `<input type="checkbox" class="chk sc-chk" ${sel ? "checked" : ""} tabindex="-1">` : ""}
    <div class="sc-title">${esc(title)}</div>
    <div class="sc-meta">
      <span><b>${s.assistant_messages}</b> turns</span>
      <span><b>${s.tool_calls}</b> tools</span>
      <span class="p-cost">${fmt.cost(s.cost)}</span>
      <span>${fmt.rel(s.updated || s.mtime)}</span>
    </div>
    <div style="margin-top:8px">${meterRow("ctx", s.context_pct, s.context_pct + "%")}</div>
    ${badges.length ? `<div class="sc-badges">${badges.join("")}</div>` : ""}
  </div>`;
}

/* ---------- detail pane router ---------- */

function renderDetail() {
  const el = document.getElementById("detail-pane");
  if (State.view === "settings") return void (el.innerHTML = settingsView());
  if (State.view === "monitor") return void (el.innerHTML = monitorView());
  if (State.view === "cleanup") return void (el.innerHTML = cleanupView());
  if (State.view === "tune") return void (el.innerHTML = tuneView());
  if (State.view === "search") return void (el.innerHTML = searchView());
  if (State.view === "memory") return void (el.innerHTML = memoryView());
  if (State.view === "session" && State.detail) return void (el.innerHTML = sessionView());
  if (State.projectId) return void (el.innerHTML = projectView());
  el.innerHTML = overviewView();
}

/* ---------- global search view ---------- */

function searchView() {
  const r = State.searchResults;
  if (!r) return `<div class="detail-inner"><div class="skeleton">Searching…</div></div>`;
  const sess = r.sessions || [];
  const prompts = r.prompts || [];
  return `<div class="detail-inner">
    <div class="page-head"><div><h1>Search: “${esc(State.searchQuery)}”</h1>
      <div class="ph-sub">${sess.length} sessions · ${prompts.length} prompts (Esc to clear)</div></div></div>
    ${sess.length ? `<div class="section"><div class="section-title">Sessions</div>
      <div class="list-body" style="padding:0">${sess.map((x) => `
        <div class="file-row" data-action="goto-session" data-pid="${esc(x.project_id)}" data-sid="${esc(x.session_id)}">
          <div class="file-ic">◈</div>
          <div style="min-width:0"><div class="f-name">${esc(x.title || x.session_id)}</div>
          <div class="f-desc">${esc(x.project_name)} · ${fmt.rel(x.mtime)}</div></div>
          <div class="file-meta">${fmt.cost(x.cost)}</div>
        </div>`).join("")}</div></div>` : ""}
    ${prompts.length ? `<div class="section"><div class="section-title">Prompt history</div>
      <div class="list-body" style="padding:0">${prompts.map((x) => `
        <div class="file-row" data-action="goto-session" data-pid="${esc(x.project_id)}" data-sid="${esc(x.session_id)}">
          <div class="file-ic">›_</div>
          <div style="min-width:0"><div class="f-name" style="font-weight:400">${esc(x.display)}</div>
          <div class="f-desc">${esc(x.project_name)} · ${fmt.rel(x.timestamp)}</div></div>
        </div>`).join("")}</div></div>` : ""}
    ${!sess.length && !prompts.length ? emptyState("🔎", "No matches", "Nothing found across sessions or prompt history.") : ""}
  </div>`;
}

async function runGlobalSearch(q) {
  State.view = "search";
  State.searchQuery = q;
  State.searchResults = null;
  renderListPane(); renderDetail();
  State.searchResults = await call("searchAll", q);
  renderDetail();
}

/* ---------- overview dashboard ---------- */

function overviewView() {
  const P = State.projects;
  const g = State.globalStats;
  if (!g) return `<div class="detail-inner"><div class="skeleton">Aggregating all sessions…</div></div>`;
  const u = g.usage || {};
  const ctxTotal = (u.input || 0) + (u.cache_read || 0) + (u.cache_write || 0);
  const cacheHit = ctxTotal ? (100 * (u.cache_read || 0) / ctxTotal) : 0;

  const costBars = [...P].sort((a, b) => b.total_cost - a.total_cost).slice(0, 8).map((p, i) => ({
    label: p.name, value: p.total_cost, valueText: fmt.cost(p.total_cost), color: CHART_PALETTE[i % CHART_PALETTE.length],
  }));
  const models = Object.entries(g.by_model || {}).filter(([, v]) => (v.total || 0) > 0)
    .sort((a, b) => b[1].cost - a[1].cost);
  const donutItems = models.map(([m, v], i) => ({ label: shortModel(m), value: v.total, color: modelColor(m, i) }));
  const toolBars = Object.entries(g.tool_counts || {}).slice(0, 12)
    .map(([name, n]) => ({ label: name, value: n, valueText: fmt.num(n) }));
  const dayVals = (g.sessions_by_day || []).map(([, n]) => n);
  const dayLabels = (g.sessions_by_day || []).map(([d]) => d.slice(8));

  return `<div class="detail-inner">
    <div class="page-head"><div><h1>Overview</h1><div class="ph-sub">Everything Claude Code has done on this machine — all projects, all sessions</div></div>
      <div class="page-actions"><button class="btn sm" data-action="open-home" title="Open the Claude data folder in your file manager">Open ~/.claude</button></div></div>
    <div class="tiles">
      ${tile("Total spend", fmt.cost(g.cost), fmt.cost(g.sessions ? g.cost / g.sessions : 0) + " avg / session", true,
        "Estimated from per-model token usage and list prices (cache reads ~0.1x, cache writes ~1.25x input price)")}
      ${tile("Tokens", fmt.tokens(u.total), fmt.tokens(u.output) + " generated",
        false, "All tokens across every session: input + output + cache reads/writes")}
      ${tile("Cache hit rate", cacheHit.toFixed(0) + "%", fmt.tokens(u.cache_read) + " served from cache",
        false, "Share of context tokens served from the prompt cache — cache reads cost ~10x less than fresh input")}
      ${tile("Sessions", g.sessions, g.active + " live · " + P.length + " projects",
        false, "Every transcript under ~/.claude/projects; live = written to in the last 2 minutes")}
    </div>
    <div class="tiles">
      ${tile("Prompts", fmt.num(g.prompts), "messages you sent", false, "User messages across all sessions (tool results excluded)")}
      ${tile("Assistant turns", fmt.num(g.turns), "API responses", false, "Deduplicated assistant responses across all sessions")}
      ${tile("Tool calls", fmt.num(g.tool_calls), Object.keys(g.tool_counts || {}).length + " distinct tools",
        false, "Bash, Edit, Read, subagents… every tool invocation Claude made")}
      ${tile("Subagent sessions", g.subagent_sessions, "used Agent/Task", false, "Sessions that spawned subagents or used the Agent/Task tools")}
    </div>
    <div class="section"><div class="section-title">Spend by project</div>
      ${desc("Estimated cost of every session, grouped by the project it ran in.")}
      <div class="card">${costBars.length ? barChart(costBars) : '<div class="faint">No data.</div>'}</div></div>
    ${donutItems.length ? `<div class="section"><div class="section-title">Models — tokens & cost</div>
      ${desc("Which models did the work, and what each one cost across all sessions.")}
      <div class="card"><div style="display:flex;gap:28px;align-items:center;flex-wrap:wrap">${donut(donutItems)}
        <div class="legend">${models.map(([m, v], i) => `<div class="legend-item"><span class="legend-swatch" style="background:${modelColor(m, i)}"></span>${esc(shortModel(m))} <b style="color:var(--text)">${fmt.cost(v.cost)}</b> <span class="faint">${fmt.tokens(v.total)} tok</span></div>`).join("")}</div>
      </div></div></div>` : ""}
    <div class="section"><div class="section-title">Activity — last 14 days</div>
      ${desc("Sessions with activity per day (by last write to the transcript).")}
      <div class="card">${columns(dayVals, "#7aa2c9", dayLabels)}</div></div>
    ${toolBars.length ? `<div class="section"><div class="section-title">Tool usage — all sessions</div>
      ${desc("Total invocations per tool across every session on this machine.")}
      <div class="card">${barChart(toolBars)}</div></div>` : ""}
    <div class="section"><div class="section-title">Token composition</div>
      ${desc("Where all those tokens went. Cache reads are re-served context (cheap); cache writes populate the cache (~1.25x input price).")}
      <div class="card">${barChart([
        { label: "Input", value: u.input || 0, valueText: fmt.tokens(u.input), color: "#7aa2c9" },
        { label: "Output", value: u.output || 0, valueText: fmt.tokens(u.output), color: "#d97757" },
        { label: "Cache write", value: u.cache_write || 0, valueText: fmt.tokens(u.cache_write), color: "#e0b64c" },
        { label: "Cache read", value: u.cache_read || 0, valueText: fmt.tokens(u.cache_read), color: "#7fae6f" },
      ])}</div></div>
  </div>`;
}

/* ---------- project view ---------- */

function projectView() {
  const p = State.projects.find((x) => x.id === State.projectId);
  if (!p) return emptyState("◈", "Select a project");
  const sessions = State.sessions;
  const totalCost = sessions.reduce((a, b) => a + b.cost, 0);
  const totalTokens = sessions.reduce((a, b) => a + (b.usage && b.usage.total || 0), 0);
  const totalTools = sessions.reduce((a, b) => a + b.tool_calls, 0);
  // model spend aggregation
  const modelTokens = {};
  sessions.forEach((s) => {
    Object.entries(s.usage_by_model || {}).forEach(([m, u]) => {
      if (m === "unknown" || m === "<synthetic>") return;
      modelTokens[m] = (modelTokens[m] || 0) + (u.total || 0);
    });
  });
  const donutItems = Object.entries(modelTokens).map(([m, v], i) => ({ label: shortModel(m), value: v, color: modelColor(m, i) }));

  return `<div class="detail-inner">
    <div class="page-head">
      <div><h1>${esc(p.name)}</h1><div class="ph-sub mono">${esc(p.path || p.id)}</div></div>
      <div class="page-actions">
        <button class="btn sm" data-action="open-editor" data-path="${esc(p.path)}">VS Code</button>
        <button class="btn sm" data-action="open-folder" data-path="${esc(p.path)}">Open folder</button>
      </div>
    </div>
    <div class="tiles">
      ${tile("Spend", fmt.cost(totalCost), null, true)}
      ${tile("Tokens", fmt.tokens(totalTokens))}
      ${tile("Sessions", sessions.length, p.active_count + " active")}
      ${tile("Tool calls", fmt.num(totalTools))}
    </div>
    ${donutItems.length ? `<div class="section"><div class="section-title">Token share by model</div><div class="card">${donut(donutItems)}</div></div>` : ""}
    <div class="section"><div class="section-title">Sessions</div>
      <div class="card">${barChart(sessions.slice(0, 12).map((s) => ({ label: s.title || s.session_id.slice(0, 8), value: s.cost, valueText: fmt.cost(s.cost) })))}</div></div>
    <div class="empty" style="height:auto;padding:20px"><p>Select a session on the left to inspect its transcript, analytics, subagents and scratchpad.</p></div>
  </div>`;
}

/* ---------- session view (tabbed) ---------- */

function sessionView() {
  const d = State.detail;
  const s = State.sessions.find((x) => x.session_id === State.sessionId) || {};
  const title = s.title || s.first_prompt || "Session";
  const tabs = [
    ["analytics", "Analytics"],
    ["transcript", "Transcript (" + (d.total_events || 0) + ")"],
    ["subagents", "Subagents (" + ((d.subagents && d.subagents.count) || 0) + ")"],
    ["tasks", "Tasks (" + (d.tasks || []).length + ")"],
    ["scratchpad", "Workspace (" + ((d.scratchpad && d.scratchpad.files || []).length) + ")"],
    ["images", "Images (" + ((d.images || []).length) + ")"],
    ["raw", "Raw"],
  ];
  return `<div class="detail-inner">
    <div class="page-head">
      <div><h1>${esc(title)}</h1>
        <div class="ph-sub mono">${esc(State.sessionId)} · ${(s.models || []).map(shortModel).join(", ")}</div></div>
      <div class="page-actions">
        <button class="btn sm" data-action="copy-resume" title="Copy: claude --resume">Copy resume</button>
        <button class="btn sm" data-action="open-jsonl">Open .jsonl</button>
        <button class="btn sm danger" data-action="delete-session">Delete</button>
      </div>
    </div>
    <div class="tabs">${tabs.map(([k, l]) => `<button class="tab ${State.tab === k ? "active" : ""}" data-action="tab" data-tab="${k}">${l}</button>`).join("")}</div>
    <div id="tab-body">${sessionTabBody()}</div>
  </div>`;
}

function sessionTabBody() {
  const d = State.detail;
  if (State.tab === "analytics") return analyticsTab(d);
  if (State.tab === "subagents") return subagentsTab(d);
  if (State.tab === "tasks") return tasksTab(d);
  if (State.tab === "scratchpad") return scratchpadTab(d);
  if (State.tab === "images") return imagesTab(d);
  if (State.tab === "raw") return rawTab(d);
  return transcriptTab(d);
}

function imagesTab(d) {
  const imgs = d.images || [];
  if (!imgs.length) return emptyState("🖼", "No images", "No pasted images cached for this session.");
  return `<div class="img-grid">${imgs.map((f) => `
    <figure class="img-card" data-action="open-folder" data-path="${esc(f.path)}" title="${esc(f.name)} · ${fmt.bytes(f.size)}">
      <img src="file://${esc(f.path)}" loading="lazy" alt="${esc(f.name)}">
      <figcaption>${esc(f.name)} <span class="faint">${fmt.bytes(f.size)}</span></figcaption>
    </figure>`).join("")}</div>`;
}

const NOISE_RE = /^<(local-command-caveat|local-command-stdout|command-name|command-message|command-args|system-reminder|bash-(input|stdout|stderr))/;
function isNoiseUser(e) {
  if (e.role !== "user") return false;
  const txt = (e.blocks || []).map((b) => b.text || "").join("").trimStart();
  return !!txt && NOISE_RE.test(txt);
}

function transcriptTab() {
  // Paged: the backend only ever sends a window of events, never the full log.
  const t = State.transcript || { events: [], start: 0, total: 0 };
  const all = t.events.filter((e) => !e.sidechain);
  const events = State.showNoise ? all : all.filter((e) => !isNoiseUser(e));
  const hidden = all.length - events.length;
  if (!events.length && !hidden && !t.start) return emptyState("◌", "No messages");
  const toggle = hidden
    ? `<button class="btn sm" data-action="toggle-noise" style="margin-bottom:12px">${State.showNoise ? "Hide" : "Show"} ${hidden} system / slash-command message${hidden === 1 ? "" : "s"}</button>`
    : "";
  const earlierBtn = t.start > 0
    ? `<button class="btn sm" data-action="show-earlier" style="margin-bottom:12px;margin-left:6px">Load earlier (${t.start} before this)</button>`
    : "";
  return `${toggle}${earlierBtn}
    <div class="transcript">${events.map(renderMessage).join("")}</div>`;
}

function renderMessage(e) {
  const cls = e.role + (e.sidechain ? " sidechain" : "");
  const av = e.role === "user" ? "U" : (e.sidechain ? "S" : "C");
  const head = [`<span class="msg-role">${e.sidechain ? "subagent" : e.role}</span>`];
  if (e.model && e.model !== "<synthetic>") head.push(`<span>${esc(shortModel(e.model))}</span>`);
  if (e.ts) head.push(`<span>${esc(fmt.rel(e.ts))}</span>`);
  return `<div class="msg ${cls}">
    <div class="msg-avatar">${av}</div>
    <div class="msg-body">
      <div class="msg-head">${head.join('<span class="faint">·</span>')}</div>
      ${(e.blocks || []).map(renderBlock).join("")}
    </div></div>`;
}

function renderBlock(b) {
  if (b.type === "text") return `<div class="msg-text">${esc(b.text)}${b.truncated ? '<span class="truncated-note"> …truncated</span>' : ""}</div>`;
  if (b.type === "thinking") return `<div class="block-thinking">${esc(b.text)}${b.truncated ? " …" : ""}</div>`;
  if (b.type === "tool_use") return `<div class="block-tool"><span class="tool-name">⚒ ${esc(b.name)}</span><div class="tool-input">${esc(b.input_preview)}${b.input_truncated ? " …" : ""}</div></div>`;
  if (b.type === "tool_result") return `<div class="block-result ${b.is_error ? "error" : ""}">${esc(b.content_preview) || "(empty)"}${b.content_truncated ? " …" : ""}</div>`;
  if (b.type === "image") return `<div class="block-tool"><span class="tool-name">🖼 image</span></div>`;
  return "";
}

function columns(values, color = "#d97757", labels = null, h = 74) {
  // Compact column chart (svg) for histograms.
  const max = Math.max(1, ...values);
  const n = values.length;
  const w = 480, gap = 2, bw = (w - gap * n) / n;
  const bars = values.map((v, i) => {
    const bh = Math.max(v > 0 ? 2 : 0, (v / max) * (h - 16));
    return `<rect x="${(i * (bw + gap)).toFixed(1)}" y="${(h - 14 - bh).toFixed(1)}" width="${bw.toFixed(1)}" height="${bh.toFixed(1)}" rx="1.5" fill="${color}" opacity="${v ? 0.9 : 0.25}"><title>${labels ? labels[i] + ": " : ""}${v}</title></rect>`;
  }).join("");
  const ticks = labels
    ? labels.map((l, i) => (i % Math.ceil(n / 8) === 0 ? `<text x="${(i * (bw + gap) + bw / 2).toFixed(1)}" y="${h - 3}" font-size="8" fill="#726c61" text-anchor="middle">${esc(String(l))}</text>` : "")).join("")
    : "";
  return `<svg width="100%" height="${h}" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none">${bars}${ticks}</svg>`;
}

function durationText(a, b) {
  const t0 = Date.parse(a), t1 = Date.parse(b);
  if (isNaN(t0) || isNaN(t1) || t1 <= t0) return "—";
  const m = Math.round((t1 - t0) / 60000);
  if (m < 60) return m + "m";
  return Math.floor(m / 60) + "h " + (m % 60) + "m";
}

function analyticsTab(d) {
  const u = d.usage || {};
  const a = d.analytics || {};
  const byModel = d.usage_by_model || {};
  const donutItems = Object.entries(byModel).filter(([m, v]) => m !== "unknown" && m !== "<synthetic>" && (v.total || 0) > 0)
    .map(([m, v], i) => ({ label: shortModel(m), value: v.total || 0, color: modelColor(m, i) }));
  const costByModel = Object.entries(byModel).filter(([m, v]) => m !== "unknown" && m !== "<synthetic>" && (v.total || 0) > 0);
  const toolBars = Object.entries(d.tool_counts || {}).slice(0, 12)
    .map(([name, n]) => ({ label: name, value: n, valueText: String(n) }));
  const fileBars = Object.entries(a.files_touched || {}).slice(0, 12)
    .map(([f, n]) => ({ label: f.split("/").slice(-2).join("/"), value: n, valueText: String(n), color: "#6fb3ab" }));
  const bashBars = Object.entries(a.bash_commands || {}).slice(0, 10)
    .map(([c, n]) => ({ label: c, value: n, valueText: String(n), color: "#b98cc9" }));
  const errBars = Object.entries(a.tool_errors || {})
    .map(([name, n]) => ({ label: name, value: n, valueText: String(n), color: "#d8695a" }));
  const ctxPoints = (d.timeline || []).map((p, i) => ({ x: i, y: p.ctx }));
  const costPoints = (d.timeline || []).map((p, i) => ({ x: i, y: p.cost }));
  const outPoints = (a.output_per_turn || []).map((v, i) => ({ x: i, y: v }));

  const ctxTotal = (u.input || 0) + (u.cache_read || 0) + (u.cache_write || 0);
  const cacheHit = ctxTotal ? (100 * (u.cache_read || 0) / ctxTotal) : 0;
  const turns = a.assistant_turns || 1;
  const thinkShare = (a.thinking_chars + a.text_chars) ? (100 * a.thinking_chars / (a.thinking_chars + a.text_chars)) : 0;
  const errRate = a.tool_calls ? (100 * a.tool_error_total / a.tool_calls) : 0;

  return `<div class="tiles">
      ${tile("Cost", fmt.cost(d.cost), "$" + (d.cost / turns).toFixed(3) + " / turn", true,
        "Estimated from this session's per-model token usage at list prices")}
      ${tile("Total tokens", fmt.tokens(u.total), fmt.tokens(Math.round((u.output || 0) / turns)) + " out / turn",
        false, "Input + output + cache reads/writes for this session")}
      ${tile("Cache hit rate", cacheHit.toFixed(0) + "%", fmt.tokens(u.cache_read) + " read",
        false, "Share of context served from the prompt cache — cached context costs ~10x less than fresh input")}
      ${tile("Duration", durationText(a.first_ts, a.last_ts), (a.user_prompts || 0) + " prompts · " + turns + " turns",
        false, "Wall-clock time from the first to the last message")}
    </div>
    <div class="tiles">
      ${tile("Tool calls", fmt.num(a.tool_calls), Object.keys(d.tool_counts || {}).length + " distinct tools",
        false, "Every Bash, Edit, Read, Agent… invocation Claude made in this session")}
      ${tile("Tool errors", a.tool_error_total || 0, errRate.toFixed(1) + "% error rate",
        false, "Tool results that came back as errors (failed commands, bad edits…)")}
      ${tile("Compactions", a.compactions || 0, "context resets",
        false, "Times the conversation was compacted/summarized to free context — the context meter drops sharply at each one")}
      ${tile("Thinking share", thinkShare.toFixed(0) + "%", "of generated text",
        false, "How much of Claude's generated text was internal reasoning rather than visible replies")}
    </div>
    <div class="section"><div class="section-title">Token composition</div><div class="card">
      ${barChart([
        { label: "Input", value: u.input || 0, valueText: fmt.tokens(u.input), color: "#7aa2c9" },
        { label: "Output", value: u.output || 0, valueText: fmt.tokens(u.output), color: "#d97757" },
        { label: "Cache write", value: u.cache_write || 0, valueText: fmt.tokens(u.cache_write), color: "#e0b64c" },
        { label: "Cache read", value: u.cache_read || 0, valueText: fmt.tokens(u.cache_read), color: "#7fae6f" },
      ])}</div></div>
    ${donutItems.length ? `<div class="section"><div class="section-title">Tokens by model</div><div class="card">
      <div style="display:flex;gap:28px;align-items:center;flex-wrap:wrap">${donut(donutItems)}
      <div class="legend">${costByModel.map(([m, v], i) => `<div class="legend-item"><span class="legend-swatch" style="background:${modelColor(m, i)}"></span>${esc(shortModel(m))} <b style="color:var(--text)">${fmt.cost(v.cost)}</b> <span class="faint">${fmt.tokens(v.total)} tok · ${fmt.tokens(v.output)} out</span></div>`).join("")}</div></div></div></div>` : ""}
    <div class="section"><div class="section-title">Context window over time · ${a.compactions || 0} compaction${(a.compactions || 0) === 1 ? "" : "s"}</div><div class="card">${sparkline(ctxPoints, "#7aa2c9")}</div></div>
    <div class="section"><div class="section-title">Cumulative cost</div><div class="card">${sparkline(costPoints, "#d97757")}</div></div>
    ${outPoints.length > 1 ? `<div class="section"><div class="section-title">Output tokens per turn</div><div class="card">${sparkline(outPoints, "#7fae6f")}</div></div>` : ""}
    <div class="section"><div class="section-title">Activity by hour (UTC)</div><div class="card">${columns(a.hourly_utc || [], "#e0b64c", Array.from({ length: 24 }, (_, i) => i))}</div></div>
    ${toolBars.length ? `<div class="section"><div class="section-title">Tool usage</div><div class="card">${barChart(toolBars)}</div></div>` : ""}
    ${fileBars.length ? `<div class="section"><div class="section-title">Hottest files</div><div class="card">${barChart(fileBars)}</div></div>` : ""}
    ${bashBars.length ? `<div class="section"><div class="section-title">Top shell commands</div><div class="card">${barChart(bashBars)}</div></div>` : ""}
    ${errBars.length ? `<div class="section"><div class="section-title">Errors by tool</div><div class="card">${barChart(errBars)}</div></div>` : ""}`;
}

function subagentsTab(d) {
  // Server-aggregated: works regardless of which transcript window is loaded.
  const sub = d.subagents || {};
  const calls = sub.agent_calls || [];
  const side = sub.events || [];
  if (!side.length && !calls.length) return emptyState("◊", "No subagent activity", "This session did not spawn subagents or Agent/Task tools.");
  return `${calls.length ? `<div class="section"><div class="section-title">Agent / Task invocations (${calls.length})</div>
      <div class="card">${calls.map((c) => `<div class="block-tool"><span class="tool-name">⚒ ${esc(c.name)}</span><div class="tool-input">${esc(c.desc)}</div></div>`).join("")}</div></div>` : ""}
    ${side.length ? `<div class="section"><div class="section-title">Sidechain messages (last ${side.length} of ${sub.count})</div>
      <div class="transcript">${side.map(renderMessage).join("")}</div></div>` : ""}`;
}

function tasksTab(d) {
  const tasks = d.tasks || [];
  if (!tasks.length) return emptyState("☑", "No tasks", "No task board was created for this session.");
  const statusBadge = (st) => {
    const map = { completed: "green", in_progress: "yellow", pending: "" };
    return badge(st || "pending", map[st] || "");
  };
  return `<div class="card"><div class="chart-bars">${tasks.map((t) => `
    <div style="display:flex;align-items:flex-start;gap:10px;padding:8px 0;border-bottom:1px solid var(--border-soft)">
      <div style="flex:1"><div style="font-size:13px">${esc(t.subject || t.description || "task")}</div>
      ${t.description && t.description !== t.subject ? `<div class="faint" style="font-size:11.5px;margin-top:2px">${esc(t.description)}</div>` : ""}</div>
      ${statusBadge(t.status)}</div>`).join("")}</div></div>`;
}

function scratchpadTab(d) {
  const sp = d.scratchpad || {};
  if (!sp.exists || !(sp.files || []).length) return emptyState("▤", "No scratchpad", "No scratchpad files for this session (or it was cleaned up).");
  return `<div class="section"><div class="section-title">${sp.files.length} files
      <button class="btn sm" data-action="open-folder" data-path="${esc(sp.dir)}">Open folder</button></div>
    <div class="list-body" style="padding:0">${sp.files.map((f) => `
      <div class="file-row" data-action="preview-file" data-path="${esc(f.path)}">
        <div class="file-ic">${esc((f.ext || "·").slice(0, 3))}</div>
        <div style="min-width:0"><div class="f-name">${esc(f.name)}</div></div>
        <div class="file-meta">${fmt.bytes(f.size)}<br>${fmt.rel(f.mtime)}</div>
      </div>`).join("")}</div></div>
    <div id="file-preview"></div>`;
}

function rawTab(d) {
  const jsonl = `${State.claudeHome || ""}/projects/${State.projectId}/${State.sessionId}.jsonl`;
  const fh = d.file_history || {};
  return `<div class="card">
    <div class="kv">
      <div class="k">Session ID</div><div class="v">${esc(State.sessionId)}</div>
      <div class="k">Transcript</div><div class="v">${esc(jsonl)}</div>
      <div class="k">Events (shown)</div><div class="v">${(d.events || []).length}${d.truncated ? " (truncated)" : ""}</div>
      <div class="k">File checkpoints</div><div class="v">${fh.count || 0} snapshots · ${fmt.bytes(fh.bytes)}</div>
      <div class="k">Resume</div><div class="v">claude --resume ${esc(State.sessionId)}</div>
    </div>
    <div style="margin-top:14px;display:flex;gap:8px">
      <button class="btn" data-action="open-jsonl">Open transcript in editor</button>
      ${fh.count ? `<button class="btn" data-action="open-folder" data-path="${esc(fh.dir)}">Open checkpoints</button>` : ""}
    </div>
  </div>`;
}

/* ---------- memory view ---------- */

function memoryView() {
  const m = State.memory;
  if (!m) return `<div class="detail-inner"><div class="skeleton">Loading memory…</div></div>`;
  const files = m.files || [];
  const active = State._memFile;
  return `<div class="detail-inner">
    <div class="page-head"><div><h1>Memory</h1><div class="ph-sub mono">${esc(m.dir)}</div></div>
      <div class="page-actions"><button class="btn sm" data-action="open-folder" data-path="${esc(m.dir)}">Open folder</button></div></div>
    ${m.index ? `<div class="section"><div class="section-title">MEMORY.md · index</div>
      <div class="card"><pre class="code">${esc(m.index)}</pre></div></div>` : ""}
    <div class="section"><div class="section-title">${files.length} memory files</div>
      <div class="list-body" style="padding:0;gap:6px">${files.map((f) => `
        <div class="file-row ${active === f.path ? "active" : ""}" data-action="mem-file" data-path="${esc(f.path)}">
          <div class="file-ic">◇</div>
          <div style="min-width:0"><div class="f-name">${esc(f.title)} ${f.type ? badge(f.type) : ""}</div>
          <div class="f-desc">${esc(f.description || f.name)}</div></div>
          <div class="file-meta">${fmt.bytes(f.size)}</div>
        </div>`).join("") || '<div class="faint" style="font-size:12px;padding:10px">No memory files.</div>'}</div></div>
    <div id="mem-editor"></div>
  </div>`;
}

function memoryEditor(path) {
  const f = (State.memory.files || []).find((x) => x.path === path);
  if (!f) return "";
  return `<div class="section"><div class="section-title">${esc(f.name)}
      <span style="display:flex;gap:6px">
        <button class="btn sm primary" data-action="mem-save" data-path="${esc(path)}">Save</button>
        <button class="btn sm danger" data-action="mem-delete" data-path="${esc(path)}">Delete</button>
      </span></div>
    <textarea class="editor" id="mem-textarea" spellcheck="false">${esc(f.content)}</textarea></div>`;
}

/* ---------- settings view (catalog-driven; only set keys are written) -------- */

// Single source of truth for known settings. Add a row here to surface a new
// setting — nothing touches settings.json until the user actually sets it.
// type: bool | enum | number | string | envflag | envstr
//   envflag → lives under env.*, stored as the string "1" (a switch)
//   envstr  → lives under env.*, free-form string value
const SETTING_CATALOG = [
  // [group, key, label, type, options?, desc?]
  ["Model & behavior", "model", "Model", "enum", ["default", "opus", "sonnet", "haiku", "fable"]],
  ["Model & behavior", "effortLevel", "Effort level", "enum", ["low", "medium", "high", "xhigh", "max"]],
  ["Model & behavior", "outputStyle", "Output style", "string"],
  ["Model & behavior", "alwaysThinkingEnabled", "Always thinking", "bool"],
  ["Model & behavior", "autoCompactEnabled", "Auto-compact context", "bool"],
  ["Model & behavior", "fileCheckpointingEnabled", "File checkpointing", "bool"],
  ["Model & behavior", "todoFeatureEnabled", "Todo feature", "bool"],
  ["Model & behavior", "promptSuggestionEnabled", "Prompt suggestions", "bool"],
  ["Model & behavior", "enableAllProjectMcpServers", "Enable all project MCP servers", "bool"],
  ["Model & behavior", "cleanupPeriodDays", "Keep transcripts (days)", "number", null, "Auto-delete local session transcripts after this many days."],

  ["Interface", "theme", "Theme", "enum", ["dark", "light"]],
  ["Interface", "tui", "Interface", "enum", ["fullscreen", "inline"]],
  ["Interface", "verbose", "Verbose output", "bool"],
  ["Interface", "spinnerTipsEnabled", "Spinner tips", "bool"],
  ["Interface", "enableArtifact", "Artifacts", "bool"],

  ["Notifications", "preferredNotifChannel", "Notification channel", "enum", ["iterm2", "terminal_bell", "iterm2_with_bell", "kitty", "notifications_disabled"]],
  ["Notifications", "inputNeededNotifEnabled", "Input-needed notifications", "bool"],
  ["Notifications", "agentPushNotifEnabled", "Agent push notifications", "bool"],
  ["Notifications", "messageIdleNotifThresholdMs", "Idle-notify threshold (ms)", "number"],

  ["Permissions", "permissions.defaultMode", "Permission mode", "enum", ["default", "acceptEdits", "plan", "bypassPermissions"]],
  ["Permissions", "skipDangerousModePermissionPrompt", "Skip dangerous-mode prompt", "bool"],
  ["Permissions", "teammateMode", "Teammate mode", "enum", ["auto", "on", "off"]],

  ["Workflows", "enableWorkflows", "Workflows", "bool"],
  ["Workflows", "workflowKeywordTriggerEnabled", "Workflow keyword trigger", "bool"],
  ["Workflows", "skipWorkflowUsageWarning", "Skip workflow usage warning", "bool"],

  ["Updates", "autoUpdatesChannel", "Updates channel", "enum", ["latest", "stable"]],

  ["Privacy & data", "autoUploadSessions", "Auto-upload sessions to claude.ai", "bool"],
  ["Privacy & data", "includeCoAuthoredBy", "Add “Co-Authored-By: Claude” to commits", "bool"],
  ["Privacy & data", "apiKeyHelper", "API key helper (script path)", "string"],

  ["Environment", "env.DISABLE_TELEMETRY", "Disable telemetry", "envflag"],
  ["Environment", "env.DISABLE_ERROR_REPORTING", "Disable error reporting", "envflag"],
  ["Environment", "env.CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "Disable all non-essential traffic", "envflag"],
  ["Environment", "env.DISABLE_AUTOUPDATER", "Disable auto-updater", "envflag"],
  ["Environment", "env.ANTHROPIC_MODEL", "ANTHROPIC_MODEL", "envstr"],
  ["Environment", "env.ANTHROPIC_SMALL_FAST_MODEL", "ANTHROPIC_SMALL_FAST_MODEL", "envstr"],
  ["Environment", "env.BASH_DEFAULT_TIMEOUT_MS", "BASH_DEFAULT_TIMEOUT_MS", "envstr"],
  ["Environment", "env.BASH_MAX_TIMEOUT_MS", "BASH_MAX_TIMEOUT_MS", "envstr"],
  ["Environment", "env.MCP_TIMEOUT", "MCP_TIMEOUT", "envstr"],
  ["Environment", "env.CLAUDE_CODE_MAX_OUTPUT_TOKENS", "CLAUDE_CODE_MAX_OUTPUT_TOKENS", "envstr"],
];
const CATALOG_BY_KEY = Object.fromEntries(SETTING_CATALOG.map((c) => [c[1], c]));
// First path segment of every known key — used to leave truly-unknown top-level
// keys alone in the "Other" section instead of double-editing them.
const CATALOG_TOP = new Set(SETTING_CATALOG.map((c) => c[1].split(".")[0]).concat("env"));

// Curated privacy-first switches. `private` is the most-private value; turning a
// protection ON writes it, OFF removes the key (revert to default) — so the file
// only ever holds choices you actively made.
const PRIVACY_ITEMS = [
  { key: "autoUploadSessions", label: "Keep sessions on this machine", desc: "Don’t mirror your sessions to claude.ai.", private: false },
  { key: "env.CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", label: "Disable all non-essential traffic", desc: "Master switch — turns off telemetry, error reports, feedback & surveys at once.", private: "1" },
  { key: "env.DISABLE_TELEMETRY", label: "Disable usage telemetry", desc: "No usage or latency metrics leave your machine.", private: "1" },
  { key: "env.DISABLE_ERROR_REPORTING", label: "Disable error reporting", desc: "No crash reports or stack traces are sent.", private: "1" },
  { key: "includeCoAuthoredBy", label: "No “Co-Authored-By: Claude” in commits", desc: "Keep Claude out of your git history and PRs.", private: false },
];

function getNested(obj, key) {
  return key.split(".").reduce((o, k) => (o == null ? undefined : o[k]), obj);
}
function setNested(obj, key, val) {
  const parts = key.split("."); let o = obj;
  for (let i = 0; i < parts.length - 1; i++) { if (typeof o[parts[i]] !== "object" || !o[parts[i]]) o[parts[i]] = {}; o = o[parts[i]]; }
  o[parts[parts.length - 1]] = val;
}
function isSet(merged, key) { return getNested(merged, key) !== undefined; }
function scalar(v) { return v === null || ["boolean", "number", "string"].includes(typeof v); }

function privacyOn(merged, item) {
  const v = getNested(merged, item.key);
  if (v === undefined) return false;
  if (item.private === "1") return v === "1" || v === "true" || v === 1 || v === true;
  return v === item.private;
}

// One editable control for a scalar setting, tagged so the change handler knows
// how to coerce and write it. `catalog` = the [group,key,label,type,opts,desc] row.
function settingControl(key, type, options, val) {
  const k = esc(key);
  if (type === "bool") {
    return `<label class="switch"><input type="checkbox" data-setting="${k}" data-type="bool" ${val ? "checked" : ""}><span class="track"><span class="thumb"></span></span></label>`;
  }
  if (type === "envflag") {
    const on = val === "1" || val === "true" || val === true || val === 1;
    return `<label class="switch"><input type="checkbox" data-setting="${k}" data-type="envflag" ${on ? "checked" : ""}><span class="track"><span class="thumb"></span></span></label>`;
  }
  if (type === "enum") {
    const opts = (val != null && !options.includes(val)) ? [val, ...options] : options;
    return `<select class="select" data-setting="${k}" data-type="str">${opts.map((o) => `<option ${o === val ? "selected" : ""}>${esc(o)}</option>`).join("")}</select>`;
  }
  const dt = type === "number" ? "num" : (type === "envstr" ? "envstr" : "str");
  return `<input class="s-input" type="${type === "number" ? "number" : "text"}" data-setting="${k}" data-type="${dt}" value="${esc(val == null ? "" : val)}" placeholder="unset">`;
}

// A full settings row: label + key + control + a remove (×) button that unsets it.
function settingRow(key, val) {
  const cat = CATALOG_BY_KEY[key];
  const label = cat ? cat[2] : key;
  const type = cat ? cat[3] : (typeof val === "boolean" ? "bool" : typeof val === "number" ? "number" : "string");
  const options = cat ? cat[4] : null;
  const desc = cat && cat[5] ? cat[5] : "";
  return `<div class="setting-row">
    <div class="s-main"><div class="s-label">${esc(label)}</div><div class="s-key">${esc(key)}</div>${desc ? `<div class="s-desc">${esc(desc)}</div>` : ""}</div>
    <div class="s-ctl">${settingControl(key, type, options, val)}
      <button class="s-x" data-action="setting-remove" data-key="${esc(key)}" title="Remove from settings.json">×</button></div>
  </div>`;
}

function complexRow(key, val) {
  let preview; try { preview = JSON.stringify(val); } catch { preview = String(val); }
  if (preview.length > 120) preview = preview.slice(0, 117) + "…";
  return `<div class="setting-row">
    <div class="s-main"><div class="s-label">${esc(key)}</div><div class="s-key mono">${esc(preview)}</div></div>
    <div class="s-ctl"><button class="btn sm" data-action="open-settings-json">Edit in settings.json</button>
      <button class="s-x" data-action="setting-remove" data-key="${esc(key)}" title="Remove from settings.json">×</button></div>
  </div>`;
}

function privacyRow(merged, item) {
  const on = privacyOn(merged, item);
  return `<div class="setting-row priv ${on ? "on" : ""}">
    <div class="s-main"><div class="s-label">${esc(item.label)}${on ? ' <span class="priv-tag">on</span>' : ""}</div><div class="s-desc">${esc(item.desc)}</div><div class="s-key">${esc(item.key)}</div></div>
    <label class="switch"><input type="checkbox" data-role="privacy" data-key="${esc(item.key)}" ${on ? "checked" : ""}><span class="track"><span class="thumb"></span></span></label>
  </div>`;
}

function settingsView() {
  const s = State.settings;
  if (!s) return `<div class="detail-inner"><div class="skeleton">Loading settings…</div></div>`;
  const merged = s.merged || {};
  const env = merged.env && typeof merged.env === "object" ? merged.env : {};
  const live = s.live;
  const sl = State.statuslineStatus || {};
  const cfg = State.configFiles || [];

  // Known catalog settings that are currently set (excludes env — its own section).
  const activeKnown = SETTING_CATALOG.filter((c) => !c[1].startsWith("env.") && isSet(merged, c[1]));
  const allPrivOn = PRIVACY_ITEMS.every((i) => privacyOn(merged, i));

  // Genuinely unknown top-level keys the user set by hand.
  const otherKeys = Object.keys(merged).filter((k) => !CATALOG_TOP.has(k));

  // "Add a setting" — known catalog settings (non-env) not yet set, grouped.
  const groups = [];
  for (const c of SETTING_CATALOG) {
    if (c[1].startsWith("env.") || isSet(merged, c[1])) continue;
    let g = groups.find((x) => x.name === c[0]);
    if (!g) { g = { name: c[0], items: [] }; groups.push(g); }
    g.items.push(c);
  }

  return `<div class="detail-inner">
    <div class="page-head"><div><h1>Settings</h1><div class="ph-sub mono">${esc(s.home)}/settings.json</div></div>
      <div class="page-actions"><button class="btn sm" data-action="open-settings-json">Open settings.json</button>
        <button class="btn sm" data-action="open-folder" data-path="${esc(s.home)}">Open folder</button></div></div>

    <div class="section"><div class="section-title">Privacy &amp; data
      <button class="btn sm ${allPrivOn ? "" : "primary"}" data-action="privacy-apply-all" ${allPrivOn ? "disabled" : ""}>${allPrivOn ? "All protections on" : "Apply privacy-first defaults"}</button></div>
      ${desc("Turn a protection on to write only that switch to settings.json; turn it off to remove it. Values are written verbatim as documented Claude Code settings and env vars.")}
      <div class="card"><div class="setting-list">
        ${PRIVACY_ITEMS.map((i) => privacyRow(merged, i)).join("")}
      </div></div></div>

    <div class="section"><div class="section-title">Active settings
      ${activeKnown.length || otherKeys.length ? `<span class="faint" style="font-weight:400;font-size:11px">${activeKnown.length + otherKeys.length} set</span>` : ""}</div>
      ${desc("Only settings you've actually set live in settings.json. Add more below; remove with ×.")}
      <div class="card"><div class="setting-list">
        ${activeKnown.map((c) => settingRow(c[1], getNested(merged, c[1]))).join("")}
        ${otherKeys.map((k) => (scalar(merged[k]) ? settingRow(k, merged[k]) : complexRow(k, merged[k]))).join("")}
        ${(activeKnown.length || otherKeys.length) ? "" : '<div class="faint" style="font-size:12px;padding:6px 2px">Nothing set yet — a clean slate. Add settings below.</div>'}
      </div>
      <div class="add-bar">
        <select class="tune-select" data-role="add-setting">
          <option value="">+ Add a setting…</option>
          ${groups.map((g) => `<optgroup label="${esc(g.name)}">${g.items.map((c) => `<option value="${esc(c[1])}">${esc(c[2])}</option>`).join("")}</optgroup>`).join("")}
        </select>
        <span class="add-sep">or</span>
        <input class="s-input" id="custom-key" placeholder="custom.key.path" style="max-width:180px">
        <input class="s-input" id="custom-val" placeholder='value (JSON or text)' style="max-width:150px">
        <button class="btn sm" data-action="add-custom">Add</button>
      </div></div></div>

    <div class="section"><div class="section-title">Environment variables</div>
      ${desc("The env block Claude Code injects into every session. Add anything — DISABLE_TELEMETRY, ANTHROPIC_MODEL, proxies, timeouts.")}
      <div class="card"><div class="setting-list">
        ${Object.keys(env).length ? Object.entries(env).map(([name, val]) => `
          <div class="setting-row">
            <div class="s-main"><div class="s-label mono">${esc(name)}</div></div>
            <div class="s-ctl"><input class="s-input" data-setting="env.${esc(name)}" data-type="envstr" value="${esc(val == null ? "" : val)}">
              <button class="s-x" data-action="setting-remove" data-key="env.${esc(name)}" title="Remove">×</button></div>
          </div>`).join("") : '<div class="faint" style="font-size:12px;padding:6px 2px">No environment variables set.</div>'}
      </div>
      <div class="add-bar">
        <input class="s-input" id="env-name" list="known-env" placeholder="ENV_VAR_NAME" style="max-width:220px">
        <input class="s-input" id="env-val" placeholder="value" style="max-width:160px">
        <button class="btn sm" data-action="add-env">Add env var</button>
        <span class="add-sep" style="margin-left:auto"></span>
        <datalist id="known-env">${SETTING_CATALOG.filter((c) => c[1].startsWith("env.")).map((c) => `<option value="${esc(c[1].slice(4))}">`).join("")}</datalist>
      </div></div></div>

    <div class="section"><div class="section-title">Live statusline capture</div>
      <div class="card">
        <p class="dim" style="font-size:12.5px;margin-bottom:12px">Rate limits (5h / 7d) and live context % are only handed to your statusline command by Claude Code — they aren't stored on disk. Enable capture to let this app read the latest values. It inserts one guarded line into your statusline script and can be removed anytime.</p>
        <div style="display:flex;align-items:center;gap:12px">
          ${sl.installed
            ? `<span class="badge green">● capture installed</span><button class="btn sm danger" data-action="statusline-uninstall">Remove</button>`
            : `<button class="btn sm primary" data-action="statusline-install">Enable capture</button>`}
          <span class="faint" style="font-size:11.5px">${esc(sl.script || "no statusline script found")}</span>
        </div>
        ${live ? liveStatuslinePanel(live) : '<div class="faint" style="font-size:12px;margin-top:12px">No live snapshot captured yet — run Claude Code once after enabling.</div>'}
      </div></div>

    <div class="section"><div class="section-title">Config files</div>
      <div class="card">
        <div class="setting-grid" style="gap:6px 12px">${cfg.map((f) => `
          <div class="cfg-file ${State._cfgFile === f.path ? "active" : ""}" data-action="cfg-file" data-path="${esc(f.path)}">
            <span class="cf-ext">${esc(f.ext || "txt")}</span>
            <span class="f-name">${esc(f.name)}</span>
            <span class="file-meta" style="margin-left:auto">${fmt.bytes(f.size)}</span>
          </div>`).join("") || '<div class="faint" style="font-size:12px">No config files.</div>'}</div>
        <div id="cfg-editor" style="margin-top:14px"></div>
      </div></div>
  </div>`;
}

function liveStatuslinePanel(live) {
  const ctx = live.context_window || {};
  const rl = live.rate_limits || {};
  const fh = rl.five_hour || {};
  const sd = rl.seven_day || {};
  const rows = [];
  if (ctx.used_percentage != null) rows.push(meterRow("ctx", ctx.used_percentage, (+ctx.used_percentage).toFixed(0) + "%"));
  if (fh.used_percentage != null) rows.push(meterRow("5h", fh.used_percentage, (+fh.used_percentage).toFixed(0) + "%"));
  if (sd.used_percentage != null) rows.push(meterRow("7d", sd.used_percentage, (+sd.used_percentage).toFixed(0) + "%"));
  return `<div style="margin-top:14px;display:flex;flex-direction:column;gap:8px">
    <div class="faint" style="font-size:11px">Live snapshot · ${esc((live.model && live.model.display_name) || "")} · captured ${fmt.rel(live._captured_mtime)}</div>
    ${rows.join("") || '<div class="faint">No meters in snapshot.</div>'}</div>`;
}

// Write one setting (value === null deletes it), then refetch so the "active"
// list — and any server-side pruning of empty parents — stays authoritative.
async function applySetting(key, value, quiet) {
  const r = await call("updateSetting", key, JSON.stringify(value === undefined ? null : value));
  if (r && r.ok) {
    State.settings = await call("getSettings");
    renderDetail();
    if (!quiet) toast(value === null ? "Removed " + key.split(".").pop() : "Saved " + key.split(".").pop(), "ok");
  } else toast("Update failed", "err");
  return r && r.ok;
}

async function addCatalogSetting(key) {
  const cat = CATALOG_BY_KEY[key];
  const type = cat ? cat[3] : "string";
  const def = type === "bool" ? true : type === "enum" ? cat[4][0]
    : type === "number" ? 0 : type === "envflag" ? "1" : "";
  await applySetting(key, def);
}

async function applyPrivacy(key, on) {
  const item = PRIVACY_ITEMS.find((i) => i.key === key);
  if (!item) return;
  await applySetting(key, on ? item.private : null, true);
  toast(on ? "Protection on" : "Protection off", "ok");
}

async function applyPrivacyDefaults() {
  const items = PRIVACY_ITEMS.map((i) => ({ key: i.key, value: i.private }));
  const r = await call("updateSettings", JSON.stringify(items));
  if (r && r.ok) { State.settings = await call("getSettings"); renderDetail(); toast("Privacy-first defaults applied", "ok"); }
  else toast("Failed", "err");
}

async function addCustomSetting() {
  const kEl = document.getElementById("custom-key");
  const vEl = document.getElementById("custom-val");
  const key = (kEl && kEl.value || "").trim();
  if (!key) { toast("Enter a key", "err"); return; }
  let raw = (vEl && vEl.value || "").trim();
  let value;
  try { value = JSON.parse(raw); } catch { value = raw; }  // fall back to a plain string
  await applySetting(key, value);
}

async function addCustomEnv() {
  const nEl = document.getElementById("env-name");
  const vEl = document.getElementById("env-val");
  const name = (nEl && nEl.value || "").trim();
  if (!name) { toast("Enter a variable name", "err"); return; }
  await applySetting("env." + name, (vEl && vEl.value) || "");
}

/* ---------- monitor view ---------- */

function monitorView() {
  const active = [];
  State.projects.forEach((p) => { if (p.active_count) active.push(p); });
  const live = State.settings && State.settings.live;
  const shells = State.shells || {};
  const snaps = shells.snapshots || [];
  const envs = shells.envs || [];
  return `<div class="detail-inner">
    <div class="page-head"><div><h1>Monitor</h1><div class="ph-sub">What Claude Code is doing right now (spend & totals live on Overview)</div></div>
      <div class="page-actions"><button class="btn sm" data-action="refresh">Refresh</button></div></div>
    ${live ? `<div class="section"><div class="section-title">Live statusline</div>
      ${desc("The exact context and rate-limit payload Claude Code last handed to your statusline command — the same numbers your terminal statusline shows.")}
      <div class="card">${liveStatuslinePanel(live)}</div></div>` : ""}
    <div class="section"><div class="section-title">Active projects (${active.length})</div>
      ${desc("Projects whose session transcripts were written to in the last 2 minutes — i.e. Claude is (or just was) working there.")}
      <div class="card">${active.length ? active.map((p) => `
        <div style="display:flex;align-items:center;gap:10px;padding:8px 0;border-bottom:1px solid var(--border-soft)">
          <span class="dot-active"></span><b>${esc(p.name)}</b>
          <span class="faint" style="font-size:11.5px">${p.active_count} active · ${fmt.rel(p.last_activity)}</span>
          <span style="margin-left:auto" class="p-cost">${fmt.cost(p.total_cost)}</span></div>`).join("")
        : '<div class="faint" style="font-size:12px">No sessions active in the last 2 minutes.</div>'}</div></div>
    <div class="section"><div class="section-title">Shell snapshots (${snaps.length})</div>
      ${desc("When a session starts, Claude Code snapshots your shell profile (aliases, functions, PATH) and sources it for every Bash tool call — these are those snapshot scripts.")}
      <div class="card">${snaps.length ? snaps.map((f) => `
        <div style="display:flex;align-items:center;gap:10px;padding:6px 0;border-bottom:1px solid var(--border-soft);font-size:12px">
          <span class="mono dim">${esc(f.name)}</span>
          <span class="faint" style="margin-left:auto;font-size:11px">${fmt.bytes(f.size)} · ${fmt.rel(f.mtime)}</span>
          <button class="btn sm" data-action="cfg-view" data-path="${esc(f.path)}">View</button></div>`).join("")
        : '<div class="faint" style="font-size:12px">No shell snapshots.</div>'}</div></div>
    <div class="section"><div class="section-title">Session environments (${envs.length})</div>
      ${desc("Per-session working state kept under ~/.claude/session-env so a session can be resumed. Purged automatically with the session; the Delete button's purge option also removes them.")}
      <div class="card">${envs.length ? envs.map((e) => `
        <div style="display:flex;align-items:center;gap:10px;padding:6px 0;border-bottom:1px solid var(--border-soft);font-size:12px">
          <span class="mono dim">${esc(e.session_id)}</span>
          <span class="faint" style="margin-left:auto;font-size:11px">${fmt.rel(e.mtime)}</span>
          <button class="btn sm" data-action="open-folder" data-path="${esc(e.path)}">Open</button></div>`).join("")
        : '<div class="faint" style="font-size:12px">No session environments.</div>'}</div></div>
  </div>`;
}

/* ---------- cleanup / deletion helper ---------- */

const PRESETS = [
  ["empty", "Empty", "Sessions with 0 assistant replies — usually opened by mistake"],
  ["small", "Small talk", "≤ 2 assistant replies — quick throwaway chats"],
  ["cheap", "Under 1¢", "Cost below $0.01 — negligible sessions"],
  ["old", "Older than 30d", "Not touched in the last 30 days"],
  ["large10", "Largest 10", "The 10 biggest sessions on disk"],
];

function cleanupRec(s) {
  return { pid: s.project_id, sid: s.session_id, title: s.title, cost: s.cost, bytes: (s.size_bytes || 0) + (s.extra_bytes || 0) };
}

function sortedCleanup(sessions) {
  const arr = [...sessions];
  if (State.cleanupSort === "age") arr.sort((a, b) => a.mtime - b.mtime);        // oldest first
  else if (State.cleanupSort === "cost") arr.sort((a, b) => b.cost - a.cost);    // priciest first
  else arr.sort((a, b) => (b.size_bytes + b.extra_bytes) - (a.size_bytes + a.extra_bytes)); // heaviest first
  return arr;
}

function applyPreset(id) {
  const list = ((State.cleanup && State.cleanup.sessions) || []).filter((s) => !s.active);
  const now = Date.now();
  clearSel();
  let matched;
  if (id === "large10") {
    matched = [...list].sort((a, b) => (b.size_bytes + b.extra_bytes) - (a.size_bytes + a.extra_bytes)).slice(0, 10);
  } else {
    matched = list.filter((s) => {
      const ageDays = (now - s.mtime * 1000) / 86400000;
      if (id === "empty") return (s.assistant_messages || 0) === 0;
      if (id === "small") return (s.assistant_messages || 0) <= 2;
      if (id === "cheap") return (s.cost || 0) < 0.01;
      if (id === "old") return ageDays > 30;
      return false;
    });
  }
  matched.forEach((s) => State.sel.set(selKey(s.project_id, s.session_id), cleanupRec(s)));
  renderDetail();
  if (!matched.length) toast("No sessions match that filter", "");
}

function cleanupView() {
  const c = State.cleanup;
  if (!c) return `<div class="detail-inner"><div class="skeleton">Scanning every session on disk…</div></div>`;
  const list = sortedCleanup(c.sessions);
  const t = selTotals();
  const sort = State.cleanupSort;
  const sortBtn = (k, l) => `<button class="chip ${sort === k ? "on" : ""}" data-action="cleanup-sort" data-sort="${k}">${l}</button>`;
  return `<div class="detail-inner">
    <div class="page-head"><div><h1>Cleanup</h1>
      <div class="ph-sub">Reclaim disk space — multi-select old, small or heavy sessions and delete them together. Live sessions are protected.</div></div>
      <div class="page-actions"><button class="btn sm" data-action="open-home" title="Open the Claude data folder">Open ~/.claude</button></div></div>
    <div class="tiles">
      ${tile("Sessions", c.sessions.length, fmt.bytes(c.total_bytes) + " on disk", false,
        "Every transcript plus its ancillary data (tasks, file-history, images, session-env)")}
      ${tile("Selected", t.count, fmt.cost(t.cost) + " of history", !!t.count, "Sessions currently checked for deletion")}
      ${tile("Reclaims", fmt.bytes(t.bytes), t.count ? "delete + purge" : "nothing selected", !!t.count,
        "Approximate disk space freed if you delete the current selection with purge on")}
    </div>
    <div class="section">
      <div class="cleanup-toolbar">
        <span class="tb-label">Quick select</span>
        ${PRESETS.map(([id, l, tip]) => `<button class="chip" data-action="cleanup-preset" data-preset="${id}" title="${esc(tip)}">${l}</button>`).join("")}
        <button class="chip" data-action="sel-all">All</button>
        <button class="chip" data-action="sel-clear">None</button>
        <span class="tb-label" style="margin-left:auto">Sort</span>
        ${sortBtn("size", "Size")}${sortBtn("age", "Age")}${sortBtn("cost", "Cost")}
      </div>
      <div class="clean-list">
        ${list.map(cleanupRow).join("") || '<div class="faint" style="padding:14px">No sessions on disk.</div>'}
      </div>
    </div>
    ${selBar()}
  </div>`;
}

function cleanupRow(s) {
  const sel = isSel(s.project_id, s.session_id);
  const size = (s.size_bytes || 0) + (s.extra_bytes || 0);
  const tags = [];
  if (s.active) tags.push('<span class="badge green"><span class="dot-active"></span> live</span>');
  if (s.has_subagents) tags.push(badge("subagents", "magenta"));
  return `<div class="clean-row ${sel ? "sel" : ""} ${s.active ? "live" : ""}" data-action="cleanup-row" data-pid="${esc(s.project_id)}" data-sid="${esc(s.session_id)}">
    ${s.active ? '<span class="chk-lock" title="Live session — protected">🔒</span>' : `<input type="checkbox" class="chk" ${sel ? "checked" : ""} tabindex="-1">`}
    <div class="cr-main">
      <div class="cr-title">${esc(s.title)}</div>
      <div class="cr-meta"><span class="cr-proj">${esc(s.project_name)}</span>
        <span>${s.assistant_messages} turns</span><span>${s.tool_calls} tools</span>
        <span>${fmt.rel(s.mtime)}</span>${tags.join("")}</div>
    </div>
    <div class="cr-nums"><span class="p-cost">${fmt.cost(s.cost)}</span><span class="cr-size">${fmt.bytes(size)}</span></div>
  </div>`;
}

function selBar() {
  const t = selTotals();
  if (!t.count) return "";
  return `<div class="sel-bar">
    <div class="sb-info"><b>${t.count}</b> selected · <span class="p-cost">${fmt.cost(t.cost)}</span> · reclaims <b>${fmt.bytes(t.bytes)}</b></div>
    <button class="btn sm" data-action="sel-clear">Clear</button>
    <button class="btn sm primary danger" data-action="bulk-delete">Delete ${t.count} session${t.count === 1 ? "" : "s"}</button>
  </div>`;
}

async function loadCleanup() {
  State.cleanup = null; clearSel();
  renderDetail();
  State.cleanup = await call("getAllSessions");
  renderDetail();
}

function confirmBulkDelete() {
  const t = selTotals();
  if (!t.count) return;
  const items = selItems();
  const extra = `<label class="checkbox-row"><input type="checkbox" id="bulk-purge" checked> Also purge tasks, file-history, image-cache &amp; env (reclaims more space)</label>`;
  modal(`Delete ${t.count} session${t.count === 1 ? "" : "s"}?`,
    `Permanently deletes ${t.count} transcript${t.count === 1 ? "" : "s"} — about ${fmt.cost(t.cost)} of history and ${fmt.bytes(t.bytes)} on disk. This cannot be undone.`,
    async () => {
      const purge = document.getElementById("bulk-purge") && document.getElementById("bulk-purge").checked;
      const r = await call("deleteSessions", JSON.stringify(items), !!purge);
      if (r && r.ok) {
        toast(`Deleted ${r.deleted} session${r.deleted === 1 ? "" : "s"}`, "ok");
        clearSel();
        await loadOverview();
        if (State.view === "cleanup") await loadCleanup();
        else if (State.projectId) await selectProject(State.projectId);
      } else toast("Delete failed", "err");
    }, extra);
}

/* ---------- tune / assistant (drives the local `claude` CLI) ---------- */

function projById(id) { return State.projects.find((p) => p.id === id); }

// The sessions fed to the assistant as context: recent-first, capped. Scoped to
// one project for Memory mode or project-scoped guidance, else machine-wide.
function tuneContextItems() {
  const t = State.tune;
  let pool = (t.sessions || []).filter((s) => s.assistant_messages > 0);
  if (t.mode === "memory" || t.scope === "project") pool = pool.filter((s) => s.project_id === t.projectId);
  return [...pool].sort((a, b) => b.mtime - a.mtime).slice(0, 60);
}

async function loadTune() {
  if (!State.tune) {
    State.tune = {
      mode: "guidance",       // guidance | memory
      scope: "global",        // guidance only: global | project
      projectId: (State.projects[0] || {}).id || "",
      instruction: "",
      sessions: null,         // every session on disk (context pool)
      guidance: null,         // current CLAUDE.md for the chosen target
      proposal: null,         // generated CLAUDE.md text (guidance mode)
      notes: null,            // parsed memory notes (memory mode)
      noteSel: new Set(),
      busy: false, jobId: null, cost: 0, error: null,
    };
  }
  renderDetail();
  if (!State.tune.sessions) {
    const all = await call("getAllSessions");
    State.tune.sessions = (all && all.sessions) || [];
  }
  await tuneRefreshGuidance();
  renderDetail();
}

// Load the CLAUDE.md that the current target (global or a project) would write to.
async function tuneRefreshGuidance() {
  const t = State.tune;
  if (t.mode !== "guidance") return;
  const proj = t.scope === "project" ? projById(t.projectId) : null;
  const path = proj ? (proj.path || "") : "";
  if (t.scope === "project" && !path) { t.guidance = { exists: false, content: "", path: "" }; return; }
  t.guidance = await call("getGuidance", t.scope, path);
}

function tuneView() {
  const t = State.tune;
  if (!t) return `<div class="detail-inner"><div class="skeleton">Loading…</div></div>`;
  const modeChip = (k, l) => `<button class="chip ${t.mode === k ? "on" : ""}" data-action="tune-mode" data-mode="${k}">${l}</button>`;
  return `<div class="detail-inner">
    <div class="page-head"><div><h1>Tune</h1>
      <div class="ph-sub">Put your own signed-in Claude to work on your history — refine CLAUDE.md guidance or distill sessions into memory. Runs the local <span class="mono">claude</span> CLI; nothing leaves your machine beyond a normal Claude request.</div></div></div>
    <div class="seg">${modeChip("guidance", "✦ Refine CLAUDE.md")}${modeChip("memory", "◇ Consolidate → memory")}</div>
    ${t.mode === "guidance" ? tuneGuidance(t) : tuneMemory(t)}
  </div>`;
}

function tuneProjectSelect(t) {
  const opts = State.projects.map((p) => `<option value="${esc(p.id)}" ${p.id === t.projectId ? "selected" : ""}>${esc(p.name)}</option>`).join("");
  return `<select class="tune-select" data-tune="project">${opts || '<option value="">No projects</option>'}</select>`;
}

function tuneGuidance(t) {
  const n = tuneContextItems().length;
  const proj = t.scope === "project" ? projById(t.projectId) : null;
  const where = t.scope === "global" ? "every project on this machine" : (proj ? proj.name : "this project");
  const g = t.guidance || {};
  const scopeChip = (k, l) => `<button class="chip ${t.scope === k ? "on" : ""}" data-action="tune-scope" data-scope="${k}">${l}</button>`;
  const canRun = !t.busy && n > 0 && (t.scope === "global" || !!proj);
  return `<div class="tune-form">
    <div class="fld">
      <label class="fld-label">Target file</label>
      <div class="seg">${scopeChip("global", "Global · ~/.claude")}${scopeChip("project", "A project")}
        ${t.scope === "project" ? tuneProjectSelect(t) : ""}</div>
      <div class="tune-hint mono">${esc(g.path || (t.scope === "project" ? "select a project" : "~/.claude/CLAUDE.md"))} · ${g.exists ? `${(g.content || "").length} chars now` : "none yet — will be created"}</div>
    </div>
    <div class="fld">
      <label class="fld-label">Instruction <span class="faint">(optional)</span></label>
      <textarea class="tune-ta" id="tune-instruction" spellcheck="false" placeholder="Leave blank to let Claude fold in durable conventions and drop anything stale. Or steer it, e.g. “Emphasize our testing setup; remove the old build notes.”">${esc(t.instruction || "")}</textarea>
    </div>
    <div class="tune-hint">Context: the <b>${n}</b> most recent session${n === 1 ? "" : "s"} in ${esc(where)} (summaries only — never full transcripts).</div>
    <div style="margin-top:14px">
      <button class="btn primary" data-action="tune-run" ${canRun ? "" : "disabled"}>${t.busy ? "Working…" : "Generate CLAUDE.md"}</button>
    </div>
    ${tuneStatus(t)}
    ${t.proposal != null ? tuneProposal(t, g) : ""}
  </div>`;
}

function tuneProposal(t, g) {
  const proj = t.scope === "project" ? projById(t.projectId) : null;
  return `<div class="section" style="margin-top:18px">
    <div class="section-title">Proposed CLAUDE.md
      <span style="display:flex;gap:6px">
        <button class="btn sm primary" data-action="tune-save">Save${g.exists ? " (overwrite)" : ""}</button>
        <button class="btn sm" data-action="tune-run">Regenerate</button>
      </span></div>
    <textarea class="editor" id="tune-proposal" spellcheck="false">${esc(t.proposal)}</textarea>
    <div class="tune-hint">Writes to <span class="mono">${esc(g.path || (proj ? proj.path + "/CLAUDE.md" : ""))}</span>${t.cost ? ` · this run cost ${fmt.cost(t.cost)}` : ""}. Edit freely before saving.</div>
  </div>`;
}

function tuneMemory(t) {
  const proj = projById(t.projectId);
  const n = tuneContextItems().length;
  const canRun = !t.busy && !!proj && n > 0;
  return `<div class="tune-form">
    <div class="fld">
      <label class="fld-label">Project to distill into memory</label>
      <div class="seg">${tuneProjectSelect(t)}</div>
      <div class="tune-hint">Notes are written to this project's memory store (<span class="mono">${esc(proj ? proj.name : "—")}/memory</span>) and indexed in its MEMORY.md.</div>
    </div>
    <div class="tune-hint">Context: the <b>${n}</b> most recent session${n === 1 ? "" : "s"} in ${esc(proj ? proj.name : "—")} (summaries only).</div>
    <div style="margin-top:14px">
      <button class="btn primary" data-action="tune-run" ${canRun ? "" : "disabled"}>${t.busy ? "Distilling…" : "Distill memory notes"}</button>
    </div>
    ${tuneStatus(t)}
    ${t.notes != null ? tuneNotes(t) : ""}
  </div>`;
}

function tuneNotes(t) {
  if (!t.notes.length) return `<div class="tune-hint" style="margin-top:14px">Claude found nothing durable worth saving from these sessions.</div>`;
  const chosen = t.notes.filter((_, i) => t.noteSel.has(i)).length;
  return `<div class="section" style="margin-top:18px">
    <div class="section-title">${t.notes.length} proposed note${t.notes.length === 1 ? "" : "s"}
      <button class="btn sm primary" data-action="tune-write-notes" ${chosen ? "" : "disabled"}>Write ${chosen} to memory</button></div>
    ${t.notes.map((note, i) => {
      const on = t.noteSel.has(i);
      return `<div class="note-card ${on ? "" : "off"}" data-action="tune-note-toggle" data-i="${i}">
        <input type="checkbox" class="chk" ${on ? "checked" : ""} tabindex="-1">
        <div class="nc-main">
          <div class="note-name">${esc(note.name)} ${note.type ? badge(note.type) : ""}</div>
          ${note.description ? `<div class="note-desc">${esc(note.description)}</div>` : ""}
          <div class="note-body">${esc(note.body)}</div>
        </div></div>`;
    }).join("")}
    ${t.cost ? `<div class="tune-hint">This run cost ${fmt.cost(t.cost)}.</div>` : ""}
  </div>`;
}

function tuneStatus(t) {
  if (t.busy) return `<div class="tune-status"><span class="spinner"></span>Running <span class="mono">claude</span> on your history — this can take a bit…</div>`;
  if (t.error) return `<div class="tune-err">⚠ ${esc(t.error)}</div>`;
  return "";
}

// Persist whatever's in the instruction box into State before any re-render.
function syncTuneInstruction() {
  const ins = document.getElementById("tune-instruction");
  if (ins && State.tune) State.tune.instruction = ins.value;
}

async function runTune() {
  const t = State.tune;
  if (t.busy) return;
  syncTuneInstruction();
  const items = tuneContextItems().map((s) => ({ project_id: s.project_id, session_id: s.session_id }));
  if (!items.length) { toast("No sessions to learn from", "err"); return; }
  t.busy = true; t.error = null; t.proposal = null; t.notes = null; t.cost = 0;
  let req;
  if (t.mode === "memory") {
    const proj = projById(t.projectId);
    if (!proj) { t.busy = false; toast("Pick a project", "err"); return; }
    req = { kind: "consolidate", sessions: items, project_id: t.projectId, project_name: proj.name };
  } else {
    const proj = t.scope === "project" ? projById(t.projectId) : null;
    req = { kind: "tune", scope: t.scope, sessions: items, instruction: t.instruction,
            current_md: (t.guidance && t.guidance.content) || "", project_name: proj ? proj.name : "" };
  }
  renderDetail();
  const r = await call("startAssistant", JSON.stringify(req));
  if (!r || !r.ok) { t.busy = false; t.error = (r && r.error) || "Could not start the claude CLI."; renderDetail(); return; }
  t.jobId = r.job_id;
}

// Async result of a startAssistant job, pushed from the bridge.
function onAssistantEvent(json) {
  let res; try { res = typeof json === "string" ? JSON.parse(json) : json; } catch { return; }
  const t = State.tune;
  if (!t || !res || res.job_id !== t.jobId) return;   // stale, or user navigated away
  t.busy = false; t.jobId = null; t.cost = res.cost || 0;
  if (!res.ok) { t.error = res.error || "The assistant failed."; renderDetail(); toast("Assistant error", "err"); return; }
  if (res.kind === "consolidate") {
    t.notes = res.notes || [];
    t.noteSel = new Set(t.notes.map((_, i) => i));   // include all by default
  } else {
    t.proposal = res.text || "";
    if (!t.proposal) t.error = "The assistant returned an empty document.";
  }
  renderDetail();
}

async function saveTuneGuidance() {
  const t = State.tune;
  const ta = document.getElementById("tune-proposal");
  const content = ta ? ta.value : (t.proposal || "");
  if (!content.trim()) { toast("Nothing to save", "err"); return; }
  const proj = t.scope === "project" ? projById(t.projectId) : null;
  const r = await call("saveGuidance", t.scope, content, proj ? (proj.path || "") : "");
  if (r && r.ok) {
    toast("Saved " + r.path.split("/").pop(), "ok");
    t.guidance = { ok: true, exists: true, path: r.path, content };
    t.proposal = null;
    renderDetail();
  } else toast(r && r.error ? "Save failed: " + r.error : "Save failed", "err");
}

async function writeTuneNotes() {
  const t = State.tune;
  const chosen = (t.notes || []).filter((_, i) => t.noteSel.has(i));
  if (!chosen.length) { toast("No notes selected", "err"); return; }
  const r = await call("writeMemoryNotes", t.projectId, JSON.stringify(chosen));
  if (r && r.ok) {
    toast(`Wrote ${r.count} note${r.count === 1 ? "" : "s"} to memory`, "ok");
    t.notes = null; t.noteSel = new Set();
    renderDetail();
    await loadOverview();   // memory counts changed
  } else toast("Write failed", "err");
}

/* ---------- shared ---------- */

function emptyState(ic, title, sub) {
  return `<div class="empty"><div class="empty-ic">${ic}</div><h3>${esc(title)}</h3>${sub ? `<p>${esc(sub)}</p>` : ""}</div>`;
}

/* ---------- data loaders ---------- */

async function loadOverview() {
  const o = await call("getOverview");
  if (o) { State.projects = o.projects || []; State.claudeHome = o.home; }
  renderRail();
  // Global aggregates power the Overview dashboard; refresh alongside.
  const g = await call("getGlobalStats");
  if (g) { State.globalStats = g; if (State.view === "overview" && !State.projectId) renderDetail(); }
}

async function selectProject(id, { keepSession = false } = {}) {
  State.projectId = id;
  if (!keepSession) { State.sessionId = null; State.detail = null; State.selectMode = false; clearSel(); }
  State.view = State.view === "memory" ? "memory" : "project";
  const r = await call("getSessions", id);
  State.sessions = (r && r.sessions) || [];
  renderRail(); renderListPane(); renderDetail();
  if (State.view === "memory") loadMemory(id);
}

function detailSig(d) {
  return (d.total_events || 0) + ":" + (((d.usage || {}).total) || 0) + ":" + ((d.scratchpad && d.scratchpad.files || []).length);
}

async function selectSession(sid) {
  State.sessionId = sid;
  State.view = "session";
  State.tab = "analytics"; // analytics-first
  document.getElementById("detail-pane").innerHTML = `<div class="detail-inner"><div class="skeleton">Loading session…</div></div>`;
  const d = await call("getSessionDetail", State.projectId, sid);
  State.detail = d || {};
  State.transcript = { events: d && d.events || [], start: d && d.events_start || 0, total: d && d.total_events || 0 };
  State._detailSig = detailSig(State.detail);
  renderListPane(); renderDetail();
}

async function loadMemory(id) {
  State.memory = await call("getMemory", id);
  State._memFile = null;
  renderDetail();
}

async function loadSettings() {
  State.settings = await call("getSettings");
  State.statuslineStatus = await call("statuslineStatus");
  const cf = await call("listConfigFiles");
  State.configFiles = (cf && cf.files) || [];
  renderDetail();
}

async function openConfigFile(path) {
  State._cfgFile = path;
  document.querySelectorAll(".cfg-file").forEach((x) => x.classList.toggle("active", x.dataset.path === path));
  const box = document.getElementById("cfg-editor");
  if (!box) return;
  box.innerHTML = `<div class="skeleton">Loading…</div>`;
  const r = await call("readFile", path);
  if (!r || !r.ok) { box.innerHTML = `<div class="faint">Could not read file.</div>`; return; }
  box.innerHTML = `<div class="section-title" style="margin-top:4px">${esc(path.split("/").pop())}
      <span style="display:flex;gap:6px">
        <button class="btn sm primary" data-action="cfg-save" data-path="${esc(path)}">Save</button>
        <button class="btn sm" data-action="open-editor" data-path="${esc(path)}">Open externally</button>
      </span></div>
    <textarea class="editor" id="cfg-textarea" spellcheck="false">${esc(r.content)}${r.truncated ? "" : ""}</textarea>
    ${r.truncated ? '<div class="truncated-note">File is large — editing here would truncate it. Use “Open externally”.</div>' : ""}`;
}

async function saveConfigFile(path) {
  const ta = document.getElementById("cfg-textarea");
  if (!ta) return;
  const r = await call("writeClaudeFile", path, ta.value);
  toast(r && r.ok ? "Saved " + path.split("/").pop() : "Save failed", r && r.ok ? "ok" : "err");
}

/* ---------- event delegation ---------- */

document.addEventListener("click", async (ev) => {
  const t = ev.target.closest("[data-action]");
  if (!t) return;
  const a = t.dataset.action;
  const path = t.dataset.path;

  switch (a) {
    case "project": return void selectProject(t.dataset.id);
    case "session": return void selectSession(t.dataset.id);
    case "memory": { State.view = "memory"; renderListPane(); loadMemory(State.projectId); break; }
    case "tab": { State.tab = t.dataset.tab; document.getElementById("tab-body").innerHTML = sessionTabBody(); document.querySelectorAll(".tab").forEach((x) => x.classList.toggle("active", x.dataset.tab === State.tab)); break; }
    case "toggle-noise": { State.showNoise = !State.showNoise; document.getElementById("tab-body").innerHTML = sessionTabBody(); break; }
    case "open-editor": { const r = await call("openInEditor", path); toast(r && r.ok ? "Opened in " + (r.editor || "editor") : "Could not open", r && r.ok ? "ok" : "err"); break; }
    case "open-folder": { await call("openPath", path); break; }
    case "open-home": { await call("openPath", State.claudeHome); break; }
    case "open-jsonl": { const p = `${State.claudeHome}/projects/${State.projectId}/${State.sessionId}.jsonl`; await call("openInEditor", p); break; }
    case "refresh": { await loadOverview(); if (State.projectId) await selectProject(State.projectId, { keepSession: true }); toast("Refreshed", "ok"); break; }
    case "delete-session": return void confirmDeleteSession();
    case "toggle-select": { State.selectMode = !State.selectMode; clearSel(); renderListPane(); break; }
    case "session-toggle": {
      const s = State.sessions.find((x) => x.session_id === t.dataset.id);
      if (!s) break;
      if (s.active) { toast("Live session — protected", "err"); break; }
      toggleSel({ pid: State.projectId, sid: s.session_id, title: s.title, cost: s.cost, bytes: s.size_bytes || 0 });
      keepScroll("list-pane", renderListPane);
      break;
    }
    case "cleanup-row": {
      const s = ((State.cleanup && State.cleanup.sessions) || []).find((x) => x.project_id === t.dataset.pid && x.session_id === t.dataset.sid);
      if (!s) break;
      if (s.active) { toast("Live session — protected from cleanup", "err"); break; }
      toggleSel(cleanupRec(s));
      keepScroll("detail-pane", renderDetail);
      break;
    }
    case "cleanup-preset": return void applyPreset(t.dataset.preset);
    case "cleanup-sort": { State.cleanupSort = t.dataset.sort; renderDetail(); break; }
    case "tune-mode": {
      if (State.tune.busy || State.tune.mode === t.dataset.mode) break;
      syncTuneInstruction();
      State.tune.mode = t.dataset.mode; State.tune.error = null;
      await tuneRefreshGuidance(); renderDetail(); break;
    }
    case "tune-scope": {
      if (State.tune.busy || State.tune.scope === t.dataset.scope) break;
      syncTuneInstruction();
      State.tune.scope = t.dataset.scope; State.tune.proposal = null; State.tune.error = null;
      await tuneRefreshGuidance(); renderDetail(); break;
    }
    case "tune-run": return void runTune();
    case "tune-save": return void saveTuneGuidance();
    case "tune-write-notes": return void writeTuneNotes();
    case "tune-note-toggle": {
      const i = Number(t.dataset.i);
      if (State.tune.noteSel.has(i)) State.tune.noteSel.delete(i); else State.tune.noteSel.add(i);
      renderDetail(); break;
    }
    case "sel-all": {
      if (State.view === "cleanup") {
        ((State.cleanup && State.cleanup.sessions) || []).filter((s) => !s.active)
          .forEach((s) => State.sel.set(selKey(s.project_id, s.session_id), cleanupRec(s)));
        keepScroll("detail-pane", renderDetail);
      } else {
        State.sessions.filter((s) => !s.active)
          .forEach((s) => State.sel.set(selKey(State.projectId, s.session_id), { pid: State.projectId, sid: s.session_id, title: s.title, cost: s.cost, bytes: s.size_bytes || 0 }));
        keepScroll("list-pane", renderListPane);
      }
      break;
    }
    case "sel-clear": { clearSel(); if (State.view === "cleanup") keepScroll("detail-pane", renderDetail); else keepScroll("list-pane", renderListPane); break; }
    case "bulk-delete": return void confirmBulkDelete();
    case "preview-file": return void previewFile(path);
    case "mem-file": { State._memFile = path; renderDetail(); const box = document.getElementById("mem-editor"); if (box) box.innerHTML = memoryEditor(path); break; }
    case "mem-save": return void saveMemory(path);
    case "mem-delete": return void confirmDeleteMemory(path);
    case "statusline-install": { const r = await call("installStatusline"); State.statuslineStatus = await call("statuslineStatus"); renderDetail(); toast(r && r.ok ? "Capture enabled" : "Failed: " + (r && r.error), r && r.ok ? "ok" : "err"); break; }
    case "statusline-uninstall": { await call("uninstallStatusline"); State.statuslineStatus = await call("statuslineStatus"); renderDetail(); toast("Capture removed", "ok"); break; }
    case "cfg-file": return void openConfigFile(path);
    case "cfg-save": return void saveConfigFile(path);
    case "cfg-view": return void viewFileModal(path);
    case "setting-remove": return void applySetting(t.dataset.key, null);
    case "add-custom": return void addCustomSetting();
    case "add-env": return void addCustomEnv();
    case "privacy-apply-all": return void applyPrivacyDefaults();
    case "open-settings-json": { await call("openInEditor", `${State.claudeHome}/settings.json`); break; }
    case "show-earlier": {
      const tr = State.transcript;
      if (!tr || tr.start <= 0) break;
      const page = await call("getTranscriptBefore", State.projectId, State.sessionId, tr.start, 200);
      if (page && page.events) {
        tr.events = page.events.concat(tr.events);
        tr.start = page.start;
        document.getElementById("tab-body").innerHTML = sessionTabBody();
      }
      break;
    }
    case "copy-resume": {
      const cmd = `claude --resume ${State.sessionId}`;
      try { await navigator.clipboard.writeText(cmd); toast("Copied: " + cmd, "ok"); }
      catch { const ta = document.createElement("textarea"); ta.value = cmd; document.body.appendChild(ta); ta.select(); document.execCommand("copy"); ta.remove(); toast("Copied: " + cmd, "ok"); }
      break;
    }
    case "goto-session": {
      const pid = t.dataset.pid, sid = t.dataset.sid;
      if (!pid) { toast("Unknown project for this entry", "err"); break; }
      State.search = ""; document.getElementById("search").value = "";
      await selectProject(pid);
      if (sid) await selectSession(sid);
      break;
    }
  }
});

async function viewFileModal(path) {
  const r = await call("readFile", path);
  const back = document.createElement("div");
  back.className = "modal-back";
  back.innerHTML = `<div class="modal" style="width:760px;max-width:92vw">
    <h3 class="mono" style="font-size:13px">${esc(path.split("/").pop())}</h3>
    <pre class="code" style="max-height:60vh;overflow:auto;margin:12px 0">${esc(r && r.ok ? r.content : "Could not read file.")}</pre>
    <div class="modal-actions">
      <button class="btn" data-m="open">Open externally</button>
      <button class="btn primary" data-m="cancel">Close</button></div></div>`;
  document.body.appendChild(back);
  back.addEventListener("click", async (e) => {
    if (e.target === back || e.target.dataset.m === "cancel") back.remove();
    else if (e.target.dataset.m === "open") { await call("openInEditor", path); back.remove(); }
  });
}

/* tune project dropdown */
document.addEventListener("change", async (ev) => {
  const el = ev.target.closest("[data-tune='project']");
  if (!el || !State.tune) return;
  State.tune.projectId = el.value;
  State.tune.proposal = null; State.tune.notes = null; State.tune.error = null;
  await tuneRefreshGuidance();
  renderDetail();
});

/* settings: privacy switches, add-a-setting picker, and every editable control
   write straight to settings.json on change (empty text field = remove the key) */
document.addEventListener("change", async (ev) => {
  const pv = ev.target.closest("[data-role='privacy']");
  if (pv) return void applyPrivacy(pv.dataset.key, pv.checked);
  const add = ev.target.closest("[data-role='add-setting']");
  if (add) { const k = add.value; add.value = ""; if (k) await addCatalogSetting(k); return; }

  const el = ev.target.closest("[data-setting]");
  if (!el) return;
  const key = el.dataset.setting;
  const type = el.dataset.type;
  let value;
  if (type === "bool") value = el.checked;
  else if (type === "envflag") value = el.checked ? "1" : null;
  else if (type === "num") { const t = el.value.trim(); if (t === "") value = null; else { value = Number(t); if (Number.isNaN(value)) return void toast("Not a number", "err"); } }
  else { const t = el.value; value = t === "" ? null : t; }   // str / enum / envstr — blank removes
  await applySetting(key, value);
});

/* nav items */
document.querySelectorAll(".nav-item").forEach((n) => n.addEventListener("click", () => {
  const v = n.dataset.view;
  State.view = v; State.projectId = null; State.sessionId = null; State.detail = null;
  State.selectMode = false; clearSel();
  document.querySelectorAll(".nav-item").forEach((x) => x.classList.toggle("active", x === n));
  renderListPane();
  if (v === "settings") loadSettings();
  else if (v === "monitor") loadMonitor();
  else if (v === "cleanup") loadCleanup();
  else if (v === "tune") loadTune();
  else renderDetail();
}));

async function loadMonitor() {
  renderDetail();
  const [settings, shells] = await Promise.all([call("getSettings"), call("getShells")]);
  State.settings = settings || State.settings;
  State.shells = shells || State.shells;
  renderDetail();
}

document.getElementById("refresh-btn").addEventListener("click", async () => {
  await loadOverview();
  if (State.projectId) await selectProject(State.projectId, { keepSession: true });
});

/* window controls — WSLg's native title bar is easy to miss */
document.getElementById("win-min").addEventListener("click", () => backend && backend.windowMinimize());
document.getElementById("win-close").addEventListener("click", () => backend && backend.windowClose());

document.getElementById("search").addEventListener("input", (e) => {
  State.search = e.target.value;
  renderRail(); renderListPane();
});

document.getElementById("search").addEventListener("keydown", (e) => {
  const q = e.target.value.trim();
  if (e.key === "Enter" && q) runGlobalSearch(q);
  else if (e.key === "Escape") {
    e.target.value = ""; State.search = "";
    if (State.view === "search") { State.view = State.projectId ? "project" : "overview"; }
    renderRail(); renderListPane(); renderDetail();
  }
});

/* ---------- actions requiring UI ---------- */

async function previewFile(path) {
  const box = document.getElementById("file-preview");
  if (!box) return;
  box.innerHTML = `<div class="skeleton">Loading…</div>`;
  const r = await call("readFile", path);
  if (!r || !r.ok) { box.innerHTML = `<div class="faint">Could not read file.</div>`; return; }
  box.innerHTML = `<div class="section"><div class="section-title">${esc(path.split("/").pop())}
      <button class="btn sm" data-action="open-editor" data-path="${esc(path)}">Open</button></div>
    <div class="card"><pre class="code">${esc(r.content)}${r.truncated ? "\n… (truncated)" : ""}</pre></div></div>`;
}

async function saveMemory(path) {
  const ta = document.getElementById("mem-textarea");
  if (!ta) return;
  const r = await call("saveMemory", path, ta.value);
  toast(r && r.ok ? "Saved" : "Save failed", r && r.ok ? "ok" : "err");
}

function confirmDeleteMemory(path) {
  modal("Delete memory file?", "This permanently removes the memory file from disk. Claude won't recall it again.", async () => {
    const r = await call("deleteMemory", path);
    if (r && r.ok) { toast("Deleted", "ok"); State._memFile = null; await loadMemory(State.projectId); }
    else toast("Delete failed", "err");
  });
}

function confirmDeleteSession() {
  const html = `<label class="checkbox-row"><input type="checkbox" id="purge-chk"> Also purge tasks, file-history, image-cache & env</label>`;
  modal("Delete this session?", "This permanently deletes the transcript. This cannot be undone.", async () => {
    const purge = document.getElementById("purge-chk") && document.getElementById("purge-chk").checked;
    const r = await call("deleteSession", State.projectId, State.sessionId, !!purge);
    if (r && r.ok) {
      toast("Session deleted", "ok");
      State.sessionId = null; State.detail = null; State.view = "project";
      await loadOverview();
      await selectProject(State.projectId);
    } else toast("Delete failed", "err");
  }, html);
}

/* ---------- modal + toast ---------- */

function modal(title, body, onConfirm, extraHtml = "") {
  const back = document.createElement("div");
  back.className = "modal-back";
  back.innerHTML = `<div class="modal"><h3>${esc(title)}</h3><p>${esc(body)}</p>${extraHtml}
    <div class="modal-actions"><button class="btn" data-m="cancel">Cancel</button>
    <button class="btn primary danger" data-m="ok">Delete</button></div></div>`;
  document.body.appendChild(back);
  back.addEventListener("click", (e) => {
    if (e.target === back || e.target.dataset.m === "cancel") back.remove();
    // Run the handler while the modal's own inputs (e.g. the purge checkbox)
    // are still in the DOM, then tear the overlay down.
    else if (e.target.dataset.m === "ok") { onConfirm(); back.remove(); }
  });
}

function toast(msg, kind = "") {
  let wrap = document.getElementById("toast-wrap");
  if (!wrap) { wrap = document.createElement("div"); wrap.id = "toast-wrap"; document.body.appendChild(wrap); }
  const el = document.createElement("div");
  el.className = "toast " + kind;
  el.textContent = msg;
  wrap.appendChild(el);
  setTimeout(() => { el.style.opacity = "0"; el.style.transition = "opacity .3s"; setTimeout(() => el.remove(), 300); }, 2600);
}

/* ---------- live updates ---------- */

let liveTimer = null;

/* Steady green "watching" while idle; amber "activity" while Claude writes to disk. */
function indicateActivity() {
  const dot = document.getElementById("live-dot");
  const label = document.getElementById("live-label");
  dot.classList.add("flash");
  if (label) label.textContent = "activity";
  clearTimeout(indicateActivity._t);
  indicateActivity._t = setTimeout(() => {
    dot.classList.remove("flash");
    if (label) label.textContent = "watching";
  }, 1200);
}

function onDataChanged(reason) {
  indicateActivity();

  if (reason === "statusline") {
    // Cheap path: only refresh the live meters, never a full rescan.
    clearTimeout(State._slTimer);
    State._slTimer = setTimeout(async () => {
      if (State.view === "monitor" || State.view === "settings") {
        const live = await call("getStatuslineLive");
        if (live && Object.keys(live).length && State.settings) { State.settings.live = live; renderDetail(); }
      }
    }, 250);
    return;
  }

  clearTimeout(liveTimer);
  liveTimer = setTimeout(async () => {
    await loadOverview();
    if (State.view === "project" && State.projectId) {
      const r = await call("getSessions", State.projectId);
      State.sessions = (r && r.sessions) || [];
      renderListPane();
    } else if (State.view === "session" && State.sessionId) {
      const r = await call("getSessions", State.projectId);
      State.sessions = (r && r.sessions) || [];
      const s = State.sessions.find((x) => x.session_id === State.sessionId);
      if (s && s.active) {
        const d = await call("getSessionDetail", State.projectId, State.sessionId);
        // Skip the (expensive) re-render when nothing actually changed.
        if (d && detailSig(d) !== State._detailSig) {
          State._detailSig = detailSig(d);
          State.detail = d;
          const tr = State.transcript;
          if (tr && tr.events.length) {
            // Append only the newly written events; keep any earlier pages loaded.
            const lastIdx = tr.start + tr.events.length - 1;
            const page = await call("getTranscriptAfter", State.projectId, State.sessionId, lastIdx);
            if (page && page.events && page.events.length) {
              tr.events = tr.events.concat(page.events);
              tr.total = page.total;
            }
          } else {
            State.transcript = { events: d.events || [], start: d.events_start || 0, total: d.total_events || 0 };
          }
          renderDetail();
        }
      }
      renderListPane();
    } else if (State.view === "monitor") {
      State.shells = await call("getShells");
      renderDetail();
    }
  }, 300);
}

/* ---------- boot ---------- */

function boot() {
  new QWebChannel(qt.webChannelTransport, async (channel) => {
    backend = channel.objects.backend;
    backend.dataChanged.connect(onDataChanged);
    backend.assistantEvent.connect(onAssistantEvent);
    await loadOverview();
    renderListPane();
    renderDetail();
  });
}

if (window.qt && window.qt.webChannelTransport) boot();
else window.addEventListener("load", boot);
