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
  view: "overview", // overview | project | session | memory | settings | monitor
  tab: "transcript",
  search: "",
};

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

function tile(label, value, sub, accent) {
  return `<div class="tile ${accent ? "accent" : ""}"><div class="t-label">${esc(label)}</div><div class="t-value">${value}</div>${sub ? `<div class="t-sub">${esc(sub)}</div>` : ""}</div>`;
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
  if (State.projectId) {
    el.style.display = "flex";
    el.innerHTML = projectListPane();
  } else if (State.view === "overview") {
    el.style.display = "flex";
    el.innerHTML = overviewListPane();
  } else {
    el.style.display = "none";
    el.innerHTML = "";
  }
}

function overviewListPane() {
  const ranked = [...State.projects].sort((a, b) => b.total_cost - a.total_cost);
  return `<div class="list-head"><h2>Projects</h2><div class="sub">${State.projects.length} tracked · ranked by spend</div></div>
    <div class="list-body">${ranked.map((p) => `
      <div class="session-card" data-action="project" data-id="${esc(p.id)}">
        <div class="sc-title">${p.active_count ? '<span class="dot-active"></span> ' : ""}${esc(p.name)}</div>
        <div class="sc-meta"><b>${p.session_count}</b> sessions <b>${fmt.cost(p.total_cost)}</b> <b>${fmt.tokens(p.total_tokens)}</b> tok · ${fmt.rel(p.last_activity)}</div>
      </div>`).join("")}</div>`;
}

function projectListPane() {
  const p = State.projects.find((x) => x.id === State.projectId);
  const sessions = State.sessions.filter(sessionMatchesSearch);
  return `<div class="list-head">
      <h2>${esc(p ? p.name : "Sessions")}</h2>
      <div class="sub">${sessions.length} sessions${p && p.memory_count ? ` · ${p.memory_count} memories` : ""}</div>
    </div>
    <div class="list-body">
      <div class="file-row ${State.view === "memory" ? "active" : ""}" data-action="memory">
        <div class="file-ic">◇</div>
        <div><div class="f-name">Memory</div><div class="f-desc">${p ? p.memory_count : 0} memory files + index</div></div>
        <div class="file-meta">›</div>
      </div>
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
  return `<div class="session-card ${s.session_id === State.sessionId ? "active" : ""}" data-action="session" data-id="${esc(s.session_id)}">
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
  const totalCost = P.reduce((a, b) => a + b.total_cost, 0);
  const totalTokens = P.reduce((a, b) => a + b.total_tokens, 0);
  const totalSessions = P.reduce((a, b) => a + b.session_count, 0);
  const active = P.reduce((a, b) => a + b.active_count, 0);

  const costBars = [...P].sort((a, b) => b.total_cost - a.total_cost).slice(0, 8).map((p, i) => ({
    label: p.name, value: p.total_cost, valueText: fmt.cost(p.total_cost), color: CHART_PALETTE[i % CHART_PALETTE.length],
  }));

  return `<div class="detail-inner">
    <div class="page-head"><div><h1>Overview</h1><div class="ph-sub">All Claude Code activity on this machine</div></div>
      <div class="page-actions"><button class="btn sm" data-action="open-home">Open ~/.claude</button></div></div>
    <div class="tiles">
      ${tile("Total spend", fmt.cost(totalCost), "estimated", true)}
      ${tile("Tokens", fmt.tokens(totalTokens), "all sessions")}
      ${tile("Sessions", totalSessions, P.length + " projects")}
      ${tile("Active now", active, "live sessions")}
    </div>
    <div class="section"><div class="section-title">Spend by project</div>
      <div class="card">${costBars.length ? barChart(costBars) : '<div class="faint">No data.</div>'}</div></div>
    <div id="ov-statusline"></div>
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
    ["transcript", "Transcript"],
    ["analytics", "Analytics"],
    ["subagents", "Subagents"],
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

function transcriptTab(d) {
  const all = (d.events || []).filter((e) => !e.sidechain);
  const events = State.showNoise ? all : all.filter((e) => !isNoiseUser(e));
  const hidden = all.length - events.length;
  if (!events.length && !hidden) return emptyState("◌", "No messages");
  const toggle = hidden
    ? `<button class="btn sm" data-action="toggle-noise" style="margin-bottom:12px">${State.showNoise ? "Hide" : "Show"} ${hidden} system / slash-command message${hidden === 1 ? "" : "s"}</button>`
    : "";
  // Window the DOM: only render the most recent slice for fast paint on huge sessions.
  const limit = State.transcriptLimit || 250;
  const shown = events.slice(-limit);
  const earlier = events.length - shown.length;
  const earlierBtn = earlier > 0
    ? `<button class="btn sm" data-action="show-earlier" style="margin-bottom:12px;margin-left:6px">Load ${Math.min(earlier, 500)} earlier (${earlier} hidden)</button>`
    : "";
  return `${d.truncated ? '<div class="truncated-note" style="margin-bottom:10px">Showing the most recent messages (large session truncated for performance).</div>' : ""}
    ${toggle}${earlierBtn}
    <div class="transcript">${shown.map(renderMessage).join("")}</div>`;
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

function analyticsTab(d) {
  const u = d.usage || {};
  const byModel = d.usage_by_model || {};
  const donutItems = Object.entries(byModel).filter(([m, v]) => m !== "unknown" && m !== "<synthetic>" && (v.total || 0) > 0)
    .map(([m, v], i) => ({ label: shortModel(m), value: v.total || 0, color: modelColor(m, i) }));
  const toolBars = Object.entries(d.tool_counts || {}).slice(0, 12)
    .map(([name, n]) => ({ label: name, value: n, valueText: String(n) }));
  const ctxPoints = (d.timeline || []).map((p, i) => ({ x: i, y: p.ctx }));
  const costPoints = (d.timeline || []).map((p, i) => ({ x: i, y: p.cost }));

  return `<div class="tiles">
      ${tile("Cost", fmt.cost(d.cost), null, true)}
      ${tile("Total tokens", fmt.tokens(u.total))}
      ${tile("Output tokens", fmt.tokens(u.output))}
      ${tile("Cache read", fmt.tokens(u.cache_read))}
    </div>
    <div class="section"><div class="section-title">Token composition</div><div class="card">
      ${barChart([
        { label: "Input", value: u.input || 0, valueText: fmt.tokens(u.input), color: "#7aa2c9" },
        { label: "Output", value: u.output || 0, valueText: fmt.tokens(u.output), color: "#d97757" },
        { label: "Cache write", value: u.cache_write || 0, valueText: fmt.tokens(u.cache_write), color: "#e0b64c" },
        { label: "Cache read", value: u.cache_read || 0, valueText: fmt.tokens(u.cache_read), color: "#7fae6f" },
      ])}</div></div>
    ${donutItems.length ? `<div class="section"><div class="section-title">By model</div><div class="card">${donut(donutItems)}</div></div>` : ""}
    <div class="section"><div class="section-title">Context window over time</div><div class="card">${sparkline(ctxPoints, "#7aa2c9")}</div></div>
    <div class="section"><div class="section-title">Cumulative cost</div><div class="card">${sparkline(costPoints, "#d97757")}</div></div>
    ${toolBars.length ? `<div class="section"><div class="section-title">Tool usage</div><div class="card">${barChart(toolBars)}</div></div>` : ""}`;
}

function subagentsTab(d) {
  const side = (d.events || []).filter((e) => e.sidechain);
  const agentCalls = [];
  (d.events || []).forEach((e) => (e.blocks || []).forEach((b) => {
    if (b.type === "tool_use" && (b.name === "Agent" || b.name === "Task")) agentCalls.push(b);
  }));
  if (!side.length && !agentCalls.length) return emptyState("◊", "No subagent activity", "This session did not spawn subagents or Task/Agent tools.");
  return `${agentCalls.length ? `<div class="section"><div class="section-title">Agent / Task invocations (${agentCalls.length})</div>
      <div class="card">${agentCalls.map((b) => `<div class="block-tool"><span class="tool-name">⚒ ${esc(b.name)}</span><div class="tool-input">${esc(b.input_preview)}</div></div>`).join("")}</div></div>` : ""}
    ${side.length ? `<div class="section"><div class="section-title">Sidechain messages (${side.length})</div>
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

/* ---------- settings view ---------- */

const SETTINGS_TOGGLES = [
  ["alwaysThinkingEnabled", "Always thinking"],
  ["autoCompactEnabled", "Auto-compact context"],
  ["fileCheckpointingEnabled", "File checkpointing"],
  ["todoFeatureEnabled", "Todo feature"],
  ["promptSuggestionEnabled", "Prompt suggestions"],
  ["spinnerTipsEnabled", "Spinner tips"],
  ["verbose", "Verbose output"],
  ["inputNeededNotifEnabled", "Input-needed notifications"],
  ["agentPushNotifEnabled", "Agent push notifications"],
  ["enableArtifact", "Artifacts"],
  ["enableWorkflows", "Workflows"],
  ["workflowKeywordTriggerEnabled", "Workflow keyword trigger"],
  ["skipDangerousModePermissionPrompt", "Skip dangerous-mode prompt"],
  ["skipWorkflowUsageWarning", "Skip workflow usage warning"],
  ["autoUploadSessions", "Auto-upload sessions"],
];
const SETTINGS_SELECTS = [
  ["model", "Model", ["default", "opus", "sonnet", "haiku", "fable"]],
  ["effortLevel", "Effort level", ["low", "medium", "high", "xhigh", "max"]],
  ["theme", "Theme", ["dark", "light"]],
  ["tui", "Interface", ["fullscreen", "inline"]],
  ["permissions.defaultMode", "Permission mode", ["default", "acceptEdits", "plan", "bypassPermissions"]],
  ["teammateMode", "Teammate mode", ["auto", "on", "off"]],
  ["autoUpdatesChannel", "Updates channel", ["latest", "stable"]],
];

function getNested(obj, key) {
  return key.split(".").reduce((o, k) => (o == null ? undefined : o[k]), obj);
}
function setNested(obj, key, val) {
  const parts = key.split("."); let o = obj;
  for (let i = 0; i < parts.length - 1; i++) { if (typeof o[parts[i]] !== "object" || !o[parts[i]]) o[parts[i]] = {}; o = o[parts[i]]; }
  o[parts[parts.length - 1]] = val;
}

function settingToggle(key, label, val) {
  return `<div class="setting-row"><div><div class="s-label">${esc(label)}</div><div class="s-key">${esc(key)}</div></div>
    <label class="switch"><input type="checkbox" data-setting="${esc(key)}" data-type="bool" ${val ? "checked" : ""}><span class="track"><span class="thumb"></span></span></label></div>`;
}
function settingSelect(key, label, options, val) {
  const opts = (val != null && !options.includes(val)) ? [val, ...options] : options;
  return `<div class="setting-row"><div><div class="s-label">${esc(label)}</div><div class="s-key">${esc(key)}</div></div>
    <select class="select" data-setting="${esc(key)}" data-type="str">${opts.map((o) => `<option ${o === val ? "selected" : ""}>${esc(o)}</option>`).join("")}</select></div>`;
}

function settingsView() {
  const s = State.settings;
  if (!s) return `<div class="detail-inner"><div class="skeleton">Loading settings…</div></div>`;
  const merged = s.merged || {};
  const live = s.live;
  const sl = State.statuslineStatus || {};
  const cfg = State.configFiles || [];

  return `<div class="detail-inner">
    <div class="page-head"><div><h1>Settings</h1><div class="ph-sub mono">${esc(s.home)}</div></div>
      <div class="page-actions"><button class="btn sm" data-action="open-folder" data-path="${esc(s.home)}">Open folder</button></div></div>

    <div class="section"><div class="section-title">Preferences</div>
      <div class="card"><div class="setting-grid">
        ${SETTINGS_SELECTS.map(([k, l, o]) => settingSelect(k, l, o, getNested(merged, k))).join("")}
        ${SETTINGS_TOGGLES.map(([k, l]) => settingToggle(k, l, !!getNested(merged, k))).join("")}
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

/* ---------- monitor view ---------- */

function monitorView() {
  const active = [];
  State.projects.forEach((p) => { if (p.active_count) active.push(p); });
  const live = State.settings && State.settings.live;
  const shells = State.shells || {};
  const snaps = shells.snapshots || [];
  const envs = shells.envs || [];
  return `<div class="detail-inner">
    <div class="page-head"><div><h1>Monitor</h1><div class="ph-sub">Live sessions, shells and context pressure</div></div>
      <div class="page-actions"><button class="btn sm" data-action="refresh">Refresh</button></div></div>
    ${live ? `<div class="section"><div class="section-title">Live statusline</div><div class="card">${liveStatuslinePanel(live)}</div></div>` : ""}
    <div class="section"><div class="section-title">Active projects (${active.length})</div>
      <div class="card">${active.length ? active.map((p) => `
        <div style="display:flex;align-items:center;gap:10px;padding:8px 0;border-bottom:1px solid var(--border-soft)">
          <span class="dot-active"></span><b>${esc(p.name)}</b>
          <span class="faint" style="font-size:11.5px">${p.active_count} active · ${fmt.rel(p.last_activity)}</span>
          <span style="margin-left:auto" class="p-cost">${fmt.cost(p.total_cost)}</span></div>`).join("")
        : '<div class="faint" style="font-size:12px">No sessions active in the last 2 minutes.</div>'}</div></div>
    <div class="section"><div class="section-title">Shell snapshots (${snaps.length})</div>
      <div class="card">${snaps.length ? snaps.map((f) => `
        <div style="display:flex;align-items:center;gap:10px;padding:6px 0;border-bottom:1px solid var(--border-soft);font-size:12px">
          <span class="mono dim">${esc(f.name)}</span>
          <span class="faint" style="margin-left:auto;font-size:11px">${fmt.bytes(f.size)} · ${fmt.rel(f.mtime)}</span>
          <button class="btn sm" data-action="cfg-view" data-path="${esc(f.path)}">View</button></div>`).join("")
        : '<div class="faint" style="font-size:12px">No shell snapshots.</div>'}</div></div>
    <div class="section"><div class="section-title">Session environments (${envs.length})</div>
      <div class="card">${envs.length ? envs.map((e) => `
        <div style="display:flex;align-items:center;gap:10px;padding:6px 0;border-bottom:1px solid var(--border-soft);font-size:12px">
          <span class="mono dim">${esc(e.session_id)}</span>
          <span class="faint" style="margin-left:auto;font-size:11px">${fmt.rel(e.mtime)}</span>
          <button class="btn sm" data-action="open-folder" data-path="${esc(e.path)}">Open</button></div>`).join("")
        : '<div class="faint" style="font-size:12px">No session environments.</div>'}</div></div>
    <div class="section"><div class="section-title">All projects — spend</div>
      <div class="card chart-bars">${State.projects.map((p) => `
        <div class="bar-row"><span class="bar-label">${esc(p.name)}</span>
          <span>${meterRow("", Math.min(100, p.total_cost), fmt.cost(p.total_cost))}</span>
          <span class="bar-val">${p.session_count}s</span></div>`).join("")}</div></div>
  </div>`;
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
}

async function selectProject(id, { keepSession = false } = {}) {
  State.projectId = id;
  if (!keepSession) { State.sessionId = null; State.detail = null; }
  State.view = State.view === "memory" ? "memory" : "project";
  const r = await call("getSessions", id);
  State.sessions = (r && r.sessions) || [];
  renderRail(); renderListPane(); renderDetail();
  if (State.view === "memory") loadMemory(id);
}

function detailSig(d) {
  return ((d.events || []).length) + ":" + (((d.usage || {}).total) || 0) + ":" + ((d.scratchpad && d.scratchpad.files || []).length);
}

async function selectSession(sid) {
  State.sessionId = sid;
  State.view = "session";
  State.tab = "transcript";
  State.transcriptLimit = 250;
  document.getElementById("detail-pane").innerHTML = `<div class="detail-inner"><div class="skeleton">Loading session…</div></div>`;
  const d = await call("getSessionDetail", State.projectId, sid);
  State.detail = d || {};
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
    case "preview-file": return void previewFile(path);
    case "mem-file": { State._memFile = path; renderDetail(); const box = document.getElementById("mem-editor"); if (box) box.innerHTML = memoryEditor(path); break; }
    case "mem-save": return void saveMemory(path);
    case "mem-delete": return void confirmDeleteMemory(path);
    case "statusline-install": { const r = await call("installStatusline"); State.statuslineStatus = await call("statuslineStatus"); renderDetail(); toast(r && r.ok ? "Capture enabled" : "Failed: " + (r && r.error), r && r.ok ? "ok" : "err"); break; }
    case "statusline-uninstall": { await call("uninstallStatusline"); State.statuslineStatus = await call("statuslineStatus"); renderDetail(); toast("Capture removed", "ok"); break; }
    case "cfg-file": return void openConfigFile(path);
    case "cfg-save": return void saveConfigFile(path);
    case "cfg-view": return void viewFileModal(path);
    case "show-earlier": {
      State.transcriptLimit = (State.transcriptLimit || 250) + 500;
      document.getElementById("tab-body").innerHTML = sessionTabBody();
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

/* settings toggles + dropdowns write immediately on change */
document.addEventListener("change", async (ev) => {
  const el = ev.target.closest("[data-setting]");
  if (!el) return;
  const key = el.dataset.setting;
  const value = el.dataset.type === "bool" ? el.checked : el.value;
  const r = await call("updateSetting", key, JSON.stringify(value));
  if (r && r.ok) {
    if (State.settings && State.settings.merged) setNested(State.settings.merged, key, value);
    toast("Updated " + key.split(".").pop(), "ok");
  } else toast("Update failed", "err");
});

/* nav items */
document.querySelectorAll(".nav-item").forEach((n) => n.addEventListener("click", () => {
  const v = n.dataset.view;
  State.view = v; State.projectId = null; State.sessionId = null; State.detail = null;
  document.querySelectorAll(".nav-item").forEach((x) => x.classList.toggle("active", x === n));
  renderListPane();
  if (v === "settings") loadSettings();
  else if (v === "monitor") loadMonitor();
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
    else if (e.target.dataset.m === "ok") { back.remove(); onConfirm(); }
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
function onDataChanged(reason) {
  const dot = document.getElementById("live-dot");
  dot.classList.add("flash");
  setTimeout(() => dot.classList.remove("flash"), 600);

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
    await loadOverview();
    renderListPane();
    renderDetail();
  });
}

if (window.qt && window.qt.webChannelTransport) boot();
else window.addEventListener("load", boot);
