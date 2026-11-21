/* =====================================================================
   System Lab — shared browser code.
   The browser never computes a metric: every value comes from the engine
   (/api/lab/state). This file only renders it and sends control changes.
   ===================================================================== */
"use strict";
const LAB = { meta: null, st: null, samples: [], events: [], seq: 0, eseq: 0, epoch: 0, listeners: [], prevVals: [] };
const HEALTH_NAMES = ["HEALTHY", "WARNING", "DEGRADED", "CRITICAL", "FAILED"];
const LEVEL_NAMES = ["OK", "WARNING", "DEGRADED", "CRITICAL", "FAILED"];

LAB.loadMeta = async function () {
  if (!LAB.meta) LAB.meta = await OS.api("/api/lab/meta");
  LAB.spec = {}; LAB.meta.controls.forEach(c => (LAB.spec[c.key] = c));
  return LAB.meta;
};

/* -------------------------------------------------------------- polling -- */
LAB.onState = fn => LAB.listeners.push(fn);
LAB.start = function (opts) {
  if (LAB._started) return;                 // one poller per page, however many parts of the page listen
  LAB._started = true;
  opts = opts || {};
  let timer = null;
  const poll = async () => {
    clearTimeout(timer);
    try {
      const s = await OS.api(`/api/lab/state?since=${LAB.seq}&esince=${LAB.eseq}&epoch=${LAB.epoch}&full=${opts.full === false ? 0 : 1}`);
      if (s.reset) { LAB.samples = []; LAB.events = []; LAB.prevVals = []; }
      LAB.epoch = s.epoch; LAB.seq = s.seq; LAB.eseq = s.eseq;
      const last = LAB.samples.length ? LAB.samples[LAB.samples.length - 1].seq : 0;
      LAB.samples = LAB.samples.concat(s.samples.filter(x => x.seq > last));
      if (LAB.samples.length > 1440) LAB.samples = LAB.samples.slice(-1440);
      LAB.events = LAB.events.concat(s.events).slice(-800);
      LAB.prevVals.push(s.values); if (LAB.prevVals.length > 5) LAB.prevVals.shift();
      LAB.st = s;
      OS.setTopHealth(s.health, s.runner ? s.runner.name : s.recording ? "recording" : null);
      LAB.listeners.forEach(fn => { try { fn(s); } catch (e) { console.error(e); } });
      const off = document.getElementById("connLost"); if (off) off.hidden = true;
    } catch (e) {
      const off = document.getElementById("connLost"); if (off) off.hidden = false;
    }
    timer = setTimeout(poll, opts.interval || 500);
  };
  poll();
  return { refresh: poll };
};
LAB.post = async function (url, body) {
  try { return await OS.api(url, { method: "POST", body: JSON.stringify(body || {}) }); }
  catch (e) { LAB.toast(e.message, "bad"); throw e; }
};
/* replace a box's HTML only when it changed, keeping any <details> the reader opened or closed */
LAB.setHTML = function (box, html) {
  if (!box || box._html === html) return;
  const st = {};
  box.querySelectorAll("details > summary").forEach(sm => (st[sm.textContent] = sm.parentElement.open));
  box.innerHTML = html; box._html = html;
  box.querySelectorAll("details > summary").forEach(sm => { if (sm.textContent in st) sm.parentElement.open = st[sm.textContent]; });
};
LAB.toast = function (msg, cls) {
  let t = document.getElementById("labToast");
  if (!t) { t = document.createElement("div"); t.id = "labToast"; t.className = "lab-toast"; t.setAttribute("role", "status"); document.body.appendChild(t); }
  t.textContent = msg; t.className = "lab-toast show " + (cls || "");
  clearTimeout(LAB._tt); LAB._tt = setTimeout(() => (t.className = "lab-toast " + (cls || "")), 3200);
};

/* ----------------------------------------------------------- formatting -- */
LAB.unit = k => { const n = LAB.meta.nodes[k]; if (n) return n.unit; const c = LAB.spec[k]; return c ? c.unit : ""; };
LAB.label = k => { const n = LAB.meta.nodes[k]; if (n) return n.label; const c = LAB.spec[k]; return c ? c.label : k; };
LAB.num = function (v, unit) {
  if (v == null || typeof v !== "number" || !isFinite(v)) return "—";
  const a = Math.abs(v);
  const s = a >= 100 ? Math.round(v).toLocaleString("en-US") : a >= 10 ? v.toFixed(1) : a >= 1 ? v.toFixed(1) : v === 0 ? "0" : v.toFixed(2);
  return s.replace(/\.0$/, "");
};
LAB.fmt = function (k, v, unitOverride) {
  const u = unitOverride != null ? unitOverride : LAB.unit(k);
  if (v == null) return "—";
  return LAB.num(v) + (u === "%" ? "%" : u ? " " + u : "");
};
LAB.secs = v => (v == null ? "—" : Number(v).toFixed(1) + " s");
LAB.pill = (h, txt) => `<span class="pill ${OS.esc(h)}"><span class="dot"></span>${OS.esc(txt || h)}</span>`;
LAB.typeTag = t => `<span class="src ${t === "INPUT" ? "SIMULATED" : t}" title="${OS.esc(LAB.TYPE_INFO[t] || "")}">${t === "INPUT" ? "CONTROL" : t}</span>`;
LAB.TYPE_INFO = {
  INPUT: "A control you set. It is an input to the simulation, not a measurement.",
  SIMULATED: "A simulated state (process table, locks, injected network condition) — not measured on a real system.",
  DERIVED: "Computed by a formula from other values of the simulated server (see Simulation details).",
  HYBRID: "Baseline anchored to the real host reading (psutil, read-only) plus the simulated load.",
  REAL: "Read from this computer's operating system via psutil.",
};

/* thresholds of a node (from the detector config) -> level of a value */
LAB.TH = { cpu_util: ["cpu", "cpu_util"], run_queue: ["cpu", "run_queue_per_core", "core"], mem_util: ["memory", "mem_util"],
  page_faults: ["memory", "page_faults"], paging_overhead: ["memory", "paging_overhead"], io_queue: ["disk", "io_queue"],
  disk_latency: ["disk", "disk_latency"], rtt: ["network", "rtt"], packet_loss: ["network", "packet_loss"],
  response_ms: ["service", "response_ms"], error_rate: ["service", "error_rate"],
  availability: ["service", "error_rate", "inv"] };
LAB.thresholds = function (k) {
  const d = LAB.TH[k]; if (!d) return null;
  const th = (LAB.st && LAB.st.thresholds_live) || LAB.meta.thresholds;
  let t = Object.assign({}, th[d[0]][d[1]]);
  const vc = LAB.meta.server.vcpus;
  if (d[2] === "core") Object.keys(t).forEach(x => (t[x] = t[x] == null ? null : t[x] * vc));
  if (d[2] === "inv") Object.keys(t).forEach(x => (t[x] = t[x] == null ? null : 100 - t[x]));
  return { t, inv: d[2] === "inv" };
};
LAB.levelOf = function (k, v) {
  const r = LAB.thresholds(k); if (!r || v == null) return 0;
  let l = 0;
  ["W", "D", "C", "F"].forEach((x, i) => { const t = r.t[x]; if (t == null) return; if (r.inv ? v < t : v > t) l = i + 1; });
  return l;
};

/* ------------------------------------------------------------- sliders -- */
/* A control = slider + −/+ + numeric field. Sends the target immediately (throttled while dragging).
   The value shown comes back from the server, unless the user is editing it right now. */
LAB.controls = {};
LAB.bigStep = c => Math.max(c.step, Math.round(((c.max - c.min) / 20) / c.step) * c.step);
LAB.fmtCtl = (c, v) => (v == null ? "—" : (c.step < 1 ? Number(v).toFixed(1).replace(/\.0$/, "") : Math.round(v).toLocaleString("en-US")));
LAB.controlEl = function (c, opts) {
  opts = opts || {};
  const el = document.createElement("div");
  el.className = "lctl";
  el.dataset.key = c.key;
  el.innerHTML = `<div class="lctl-head"><label for="rg_${c.key}" title="${OS.esc(c.help || "")}">${OS.esc(c.label)}</label>
      <span class="lctl-num"><button type="button" class="stp" data-d="-1" aria-label="Decrease ${OS.esc(c.label)}">−</button>
      <input type="number" id="nb_${c.key}" min="${c.min}" max="${c.max}" step="${c.step}" aria-label="${OS.esc(c.label)}">
      <span class="u">${OS.esc(c.unit)}</span>
      <button type="button" class="stp" data-d="1" aria-label="Increase ${OS.esc(c.label)}">+</button></span></div>
    <input type="range" id="rg_${c.key}" min="${c.min}" max="${c.max}" step="${c.step}">
    <div class="lctl-scale"><span>${LAB.fmtCtl(c, c.min)}</span><span class="lag"></span><span>${LAB.fmtCtl(c, c.max)}</span></div>
    ${opts.help === false ? "" : `<div class="lctl-help">${OS.esc(c.help || "")}</div>`}`;
  const rg = el.querySelector("input[type=range]"), nb = el.querySelector("input[type=number]");
  const st = { el, rg, nb, c, t: 0, drag: false, timer: null, virtual: !!c.virtual };
  if (!opts.onChange) LAB.controls[c.key] = st;
  el._ctl = st;
  const local = v => { v = Math.min(c.max, Math.max(c.min, Number(v))); if (!isFinite(v)) return null; rg.value = v; nb.value = LAB.fmtCtl(c, v).replace(/,/g, ""); st.t = Date.now(); LAB.paintFill(st); return v; };
  const send = (v, now) => {
    if (opts.onChange) { opts.onChange(v); return; }     // plan-only slider (e.g. chaos targets)
    const go = async () => { st.timer = null; const body = {}; body[c.key] = v; try { await LAB.post("/api/lab/controls", body); } catch (e) {} };
    if (now) { clearTimeout(st.timer); go(); } else if (!st.timer) st.timer = setTimeout(go, 130);
  };
  rg.addEventListener("pointerdown", () => (st.drag = true));
  rg.addEventListener("input", () => { const v = local(rg.value); if (v != null) send(v, false); });
  rg.addEventListener("change", () => { const v = local(rg.value); if (v != null) send(v, true); });
  nb.addEventListener("change", () => { const v = local(nb.value); if (v != null) send(v, true); });
  el.querySelectorAll(".stp").forEach(b => b.addEventListener("click", () => {
    const v = local(Number(rg.value) + Number(b.dataset.d) * LAB.bigStep(c)); if (v != null) send(v, true);
  }));
  return el;
};
window.addEventListener("pointerup", () => Object.values(LAB.controls).forEach(s => (s.drag = false)));
LAB.paintFill = st => st.rg.style.setProperty("--fill", ((Number(st.rg.value) - st.c.min) / (st.c.max - st.c.min) * 100) + "%");
LAB.syncControls = function (s, disabled) {
  Object.values(LAB.controls).forEach(st => {
    if (!document.body.contains(st.el)) return;
    const c = st.c;
    let target, eff;
    if (c.key === "disk_io") {
      const cap = LAB.meta.server.disk_iops_capacity;
      target = (s.controls.disk_read + s.controls.disk_write) / cap * 100;
      eff = (s.effective.disk_read + s.effective.disk_write) / cap * 100;
    } else { target = s.controls[c.key]; eff = s.effective[c.key]; }
    const editing = st.drag || Date.now() - st.t < 1200 || document.activeElement === st.nb;
    if (!editing && target != null) { st.rg.value = target; st.nb.value = LAB.fmtCtl(c, target).replace(/,/g, ""); LAB.paintFill(st); }
    [st.rg, st.nb].concat([...st.el.querySelectorAll(".stp")]).forEach(x => (x.disabled = !!disabled));
    const lag = eff != null && target != null && Math.abs(eff - target) > (c.max - c.min) * 0.004;
    st.el.querySelector(".lag").textContent = lag ? `system now at ${LAB.fmtCtl(c, eff)} ${c.unit} (${eff < target ? "rising" : "winding down"})` : "";
    st.el.classList.toggle("moving", lag);
  });
};
LAB.BASIC_DISK = { key: "disk_io", label: "Disk I/O load", unit: "% of capacity", min: 0, max: 130, step: 1, virtual: true,
  help: "Background disk requests as a share of the device's IOPS capacity (65% writes, 35% reads)." };

/* ------------------------------------------------------- sparkline tile -- */
LAB.spark = function (key, w, h) {
  const pts = LAB.samples.slice(-120).map(s => s[key]).filter(v => v != null);
  if (pts.length < 2) return "";
  let lo = Math.min(...pts), hi = Math.max(...pts);
  if (hi - lo < 1e-9) { hi = lo + 1; }
  const d = pts.map((v, i) => `${(i / (pts.length - 1) * w).toFixed(1)},${(h - 2 - (v - lo) / (hi - lo) * (h - 4)).toFixed(1)}`).join(" ");
  return `<svg class="spark" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" aria-hidden="true"><polyline points="${d}"/></svg>`;
};
LAB.tile = function (key, s, opts) {
  opts = opts || {};
  const v = s.values[key], b = s.baseline[key], lvl = LAB.levelOf(key, v);
  const prev = LAB.prevVals[0] ? LAB.prevVals[0][key] : null;
  let arrow = "";
  if (prev != null && v != null) {
    const d = v - prev, rel = Math.abs(d) / Math.max(Math.abs(v), Math.abs(prev), 1e-6);
    if (rel > 0.03 && Math.abs(d) > 0.05) arrow = `<span class="arr ${d > 0 ? "up" : "dn"}">${d > 0 ? "▲" : "▼"}</span>`;
  }
  const t = (s.node_types || {})[key] || (LAB.meta.nodes[key] || {}).type || "DERIVED";
  return `<div class="ltile l${lvl}">
    <span class="lt-k">${OS.esc(opts.label || LAB.label(key))}${LAB.typeTag(t)}</span>
    <span class="lt-v">${LAB.num(v)}<span class="u">${OS.esc(LAB.unit(key))}</span>${arrow}</span>
    <span class="lt-b">baseline ${LAB.fmt(key, b)}</span>${LAB.spark(key, 120, 26)}</div>`;
};

/* ------------------------------------------------- health / status box -- */
LAB.statusHtml = function (s, opts) {
  opts = opts || {};
  const S = s.status;
  const prim = S.primary.length ? S.primary.map(p => `<div class="st-prim"><b>${OS.esc(p.name)}</b> <span class="muted">· ${OS.esc(p.subsystem)}</span><div class="small">${OS.esc(p.via)}</div></div>`).join("") : "";
  const sec = S.secondary.length ? `<div class="label" style="margin-top:10px">Secondary effects (cascade)</div><ul class="clean small">${S.secondary.map(x => `<li><b>${OS.esc(x.name)}</b> ← ${OS.esc(x.by_name)}: ${OS.esc(x.via)}</li>`).join("")}</ul>` : "";
  const ev = S.active.length ? `<div class="label" style="margin-top:10px">Why</div><ul class="clean small">${S.active.map(a => a.evidence.map(e => `<li>${OS.esc(e)}</li>`).join("")).join("")}</ul>` : "";
  const ms = S.episode && S.episode.metastable ? `<div class="meta-warn"><b>Self-sustaining failure.</b> The trigger was reduced, but a feedback loop keeps the system failed: ${OS.esc(S.episode.metastable.loop.join(" → "))}.</div>` : "";
  return `<div class="hb-row"><div class="big-status s-${s.health}"><span class="status-dot"></span><span class="bw">${s.health}</span></div>
    ${S.episode && S.episode.recovering ? '<span class="pill RECOVERING"><span class="dot"></span>recovering</span>' : ""}${s.paused ? '<span class="pill NEUTRAL">paused</span>' : ""}</div>
    ${S.primary.length ? `<div class="label" style="margin-top:12px">Primary cause</div>${prim}` : `<div class="small muted" style="margin-top:8px">Every detection rule is below its warning threshold.</div>`}
    ${sec}${ev}${ms}`;
};

/* ---------------------------------------------------- relationship graph -- */
/* Swimlane layout: one lane per subsystem, flowing top → bottom in dependency order. */
LAB.LANES = [["cpu", "CPU"], ["memory", "Memory"], ["disk", "Disk"], ["network", "Network"], ["process", "Processes & locks"], ["service", "Service"]];
LAB.laneOf = n => (n.subsystem === "deadlock" ? "process" : n.subsystem);
LAB.Graph = function (svgBox, opts) {
  opts = opts || {};
  const G = { view: "all", sel: null, pos: {}, nodes: [], onSelect: opts.onSelect || (() => {}) };
  const NW = 136, NH = 44, LW = 154, RH = 60, TOP = 40;
  G.layout = function () {
    const M = LAB.meta, L = M.layers;
    let ids = Object.keys(M.nodes);
    const cascadeOff = LAB.st && !LAB.st.settings.cascade;
    if (G.view !== "all") {
      const core = ids.filter(i => LAB.laneOf(M.nodes[i]) === G.view);
      const set = new Set(core);
      core.forEach(i => { M.nodes[i].deps.forEach(d => set.add(d)); M.nodes[i].affects.forEach(a => set.add(a)); });
      ids = [...set];
    }
    const lanes = LAB.LANES.filter(([k]) => ids.some(i => LAB.laneOf(M.nodes[i]) === k));
    G.pos = {};
    lanes.forEach(([k], li) => {
      const mine = ids.filter(i => LAB.laneOf(M.nodes[i]) === k).sort((a, b) => (L[a] - L[b]) || a.localeCompare(b));
      mine.forEach((id, r) => (G.pos[id] = { x: 12 + li * LW, y: TOP + r * RH }));
    });
    G.lanes = lanes;
    G.ids = ids;
    G.W = 24 + lanes.length * LW;
    G.H = TOP + Math.max(...lanes.map(([k]) => ids.filter(i => LAB.laneOf(M.nodes[i]) === k).length)) * RH + 6;
    G.edges = [];
    ids.forEach(id => M.nodes[id].deps.forEach(d => {
      if (!G.pos[d]) return;
      const cas = M.cascade_edges && M.cascade_edges.some(e => e[0] === d && e[1] === id);
      G.edges.push({ from: d, to: id, cascade: cas, off: cas && cascadeOff });
    }));
    G.draw();
  };
  G.related = function (id) {
    const M = LAB.meta, up = new Set(), dn = new Set();
    const walk = (n, set, key) => M.nodes[n][key].forEach(x => { if (!set.has(x)) { set.add(x); walk(x, set, key); } });
    walk(id, up, "deps"); walk(id, dn, "affects");
    return { up, dn };
  };
  G.draw = function () {
    const M = LAB.meta;
    const rel = G.sel && G.pos[G.sel] ? G.related(G.sel) : null;
    const lanes = G.lanes.map(([k, name], i) => `<g class="lane"><rect x="${4 + i * LW}" y="4" width="${LW - 8}" height="${G.H - 8}" rx="10"/><text x="${4 + i * LW + (LW - 8) / 2}" y="24" text-anchor="middle">${name}</text></g>`).join("");
    const edge = e => {
      const a = G.pos[e.from], b = G.pos[e.to];
      let x1 = a.x + NW / 2, y1 = a.y + NH, x2 = b.x + NW / 2, y2 = b.y;
      if (a.x !== b.x) { x1 = a.x + (b.x > a.x ? NW : 0); y1 = a.y + NH / 2; x2 = b.x + (b.x > a.x ? 0 : NW); y2 = b.y + NH / 2; }
      if (a.x === b.x && b.y <= a.y) { x1 = a.x + NW; y1 = a.y + NH / 2; x2 = b.x + NW; y2 = b.y + NH / 2; }
      const dx = Math.abs(x2 - x1) * 0.45 + 10;
      const d = a.x === b.x && b.y > a.y ? `M${x1},${y1} C${x1},${y1 + 18} ${x2},${y2 - 18} ${x2},${y2}`
        : a.x === b.x ? `M${x1},${y1} C${x1 + 30},${y1} ${x2 + 30},${y2} ${x2},${y2}`
        : `M${x1},${y1} C${x1 + (x2 > x1 ? dx : -dx)},${y1} ${x2 - (x2 > x1 ? dx : -dx)},${y2} ${x2},${y2}`;
      let cls = "edge" + (e.cascade ? " cascade" : "") + (e.off ? " off" : "");
      if (rel) { if ((rel.up.has(e.from) || e.from === G.sel) && (rel.up.has(e.to) || e.to === G.sel)) cls += " up"; else if ((rel.dn.has(e.to) || e.to === G.sel) && (rel.dn.has(e.from) || e.from === G.sel)) cls += " dn"; else cls += " dim"; }
      return `<path class="${cls}" data-e="${e.from}>${e.to}" d="${d}" marker-end="url(#gArr)"/>`;
    };
    const node = id => {
      const n = M.nodes[id], p = G.pos[id];
      let cls = "gnode t-" + n.type;
      if (rel) cls += id === G.sel ? " sel" : rel.up.has(id) ? " up" : rel.dn.has(id) ? " dn" : " dim";
      return `<g class="${cls}" data-node="${id}" transform="translate(${p.x},${p.y})" tabindex="0" role="button" aria-label="${OS.esc(n.label)}">
        <rect width="${NW}" height="${NH}" rx="8"/><text class="gl" x="9" y="17">${OS.esc(n.label.length > 20 ? n.label.slice(0, 19) + "…" : n.label)}</text>
        <text class="gv" x="9" y="36">—</text><text class="ga" x="${NW - 9}" y="36" text-anchor="end"></text></g>`;
    };
    svgBox.innerHTML = `<svg viewBox="0 0 ${G.W} ${G.H}" width="${G.W}" height="${G.H}" class="rgraph" role="img" aria-label="Parameter relationship graph">
      <defs><marker id="gArr" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse"><path d="M0 0L10 5L0 10z"/></marker></defs>
      ${lanes}<g class="edges">${G.edges.map(edge).join("")}</g><g>${G.ids.map(node).join("")}</g></svg>`;
    svgBox.querySelectorAll(".gnode").forEach(g => {
      const go = () => { G.select(g.dataset.node); };
      g.addEventListener("click", go);
      g.addEventListener("keydown", e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); go(); } });
    });
    if (LAB.st) G.update(LAB.st);
  };
  G.select = function (id) { G.sel = G.sel === id ? null : id; G.draw(); G.onSelect(G.sel); };
  G.setView = function (v) { G.view = v; G.layout(); };
  G.update = function (s) {
    const prev = LAB.prevVals[0] || {};
    const changed = new Set();
    svgBox.querySelectorAll(".gnode").forEach(g => {
      const id = g.dataset.node, n = LAB.meta.nodes[id];
      const v = id === "procs_table" ? s.values.workers_alive : id === "locks" ? null : s.values[id];
      const tv = g.querySelector(".gv"), ta = g.querySelector(".ga");
      if (id === "procs_table") tv.textContent = `${LAB.num(s.values.workers_alive)}/4 workers`;
      else if (id === "locks") tv.textContent = s.deadlock && s.deadlock.cycle.length ? "DEADLOCK" : `${(s.resources || []).filter(r => r.holder != null).length} held`;
      else tv.textContent = LAB.fmt(id, v);
      const p = id === "procs_table" ? prev.workers_alive : prev[id];
      let a = "";
      if (p != null && v != null) {
        const d = v - p, rel = Math.abs(d) / Math.max(Math.abs(v), Math.abs(p), 1e-6);
        if (rel > 0.03 && Math.abs(d) > 0.02) { a = d > 0 ? "▲" : "▼"; changed.add(id); }
      }
      ta.textContent = a;
      ta.setAttribute("class", "ga " + (a === "▲" ? "up" : a ? "dn" : ""));
      const lvl = LAB.levelOf(id, v);
      g.classList.remove("l1", "l2", "l3", "l4"); if (lvl) g.classList.add("l" + lvl);
      g.classList.toggle("chg", changed.has(id));
    });
    svgBox.querySelectorAll("path.edge").forEach(p => {
      const [a, b] = p.dataset.e.split(">");
      p.classList.toggle("flow", changed.has(a) && changed.has(b));
    });
  };
  return G;
};

/* -------------------------------------------------------- node inspector -- */
LAB.inspect = async function (box, id, opts) {
  opts = opts || {};
  if (!id) { box.innerHTML = `<div class="muted small">Click any value — a node in the graph or a metric card — to see what affects it, what it affects, the rule that computes it, and why it changed.</div>`; return; }
  const ref = box.dataset.ref || "baseline";
  let r;
  try { r = await OS.api(`/api/lab/trace?node=${encodeURIComponent(id)}&ref=${ref}`); } catch (e) { box.innerHTML = `<div class="bad small">${OS.esc(e.message)}</div>`; return; }
  const m = r.meta, t = r.trace;
  const link = d => `<button type="button" class="link-btn" data-node="${d.id}">${OS.esc(d.label)}</button> <span class="muted mono small">${d.value == null ? "" : LAB.fmt(d.id, d.value)}</span>`;
  const th = m.threshold ? `<div class="kvx"><span>Threshold (${OS.esc(m.threshold.rule_name)})</span><b>${["W", "D", "C", "F"].filter(k => m.threshold.levels[k] != null).map(k => `${k} ${LAB.num(m.threshold.levels[k])}`).join(" · ")}${m.threshold.per_core ? " (per-vCPU × " + LAB.meta.server.vcpus + ")" : ""}</b></div>
    <div class="kvx"><span>Status</span><b>${LAB.pill(m.threshold.status === "OK" ? "HEALTHY" : m.threshold.status, m.threshold.status)}</b></div>` : "";
  const parts = (m.parts_now || []).length ? `<table class="data compact parts"><thead><tr><th>Contribution</th><th>From</th><th>Now</th></tr></thead><tbody>${m.parts_now.map(p => `<tr><td>${OS.esc(p.part)}</td><td>${p.dep ? `<button type="button" class="link-btn" data-node="${p.dep}">${OS.esc(LAB.label(p.dep))}</button>` : '<span class="muted">constant</span>'}</td><td class="mono">${LAB.fmt(id, p.value)}</td></tr>`).join("")}</tbody></table>` : "";
  box.innerHTML = `<div class="ins-head"><h3>${OS.esc(m.label)}</h3>${LAB.typeTag(m.type_now)}</div>
    <div class="kvx"><span>Now</span><b class="mono">${LAB.fmt(id, m.value)}</b></div>
    <div class="kvx"><span>Baseline</span><b class="mono">${LAB.fmt(id, m.baseline)}</b></div>${th}
    <p class="small" style="margin:10px 0">${OS.esc(m.why)}</p>
    <div class="label">Why did it change? <span class="seg-mini"><button type="button" data-ref="baseline" class="${r.ref === "baseline" ? "sel" : ""}">vs baseline</button><button type="button" data-ref="recent" class="${r.ref === "recent" ? "sel" : ""}">last 5 s</button></span></div>
    <ol class="trace">${(t.sentences.length ? t.sentences : ["No change."]).map(x => `<li>${OS.esc(x)}</li>`).join("")}</ol>
    <div class="ins-cols"><div><div class="label">Depends on</div>${m.deps_values.length ? m.deps_values.map(d => `<div>${link(d)}</div>`).join("") : '<div class="muted small">nothing — it is an input</div>'}</div>
      <div><div class="label">Affects</div>${m.affects_values.length ? m.affects_values.map(d => `<div>${link(d)}</div>`).join("") : '<div class="muted small">nothing downstream</div>'}</div></div>
    <details class="simdet" ${opts.advanced ? "open" : ""}><summary>Simulation details</summary>
      <div class="kvx"><span>Type</span><b>${m.type_now}</b></div><div class="kvx"><span>Subsystem</span><b>${OS.esc(LAB.meta.subsystems[m.subsystem] || m.subsystem)}</b></div>
      <div class="label" style="margin-top:8px">Simulation rule (as implemented)</div><code class="rule">${OS.esc(m.rule)}</code>${parts}</details>`;
  box.querySelectorAll("[data-node]").forEach(b => b.addEventListener("click", () => (opts.onNode ? opts.onNode(b.dataset.node) : LAB.inspect(box, b.dataset.node, opts))));
  box.querySelectorAll("[data-ref]").forEach(b => b.addEventListener("click", () => { box.dataset.ref = b.dataset.ref; LAB.inspect(box, id, opts); }));
};

/* ------------------------------------------------------------ timeline -- */
LAB.TL_KINDS = ["CONTROL", "STRESS", "ACTION", "THRESHOLD", "DETECTION", "HEALTH", "RECOVERY", "KERNEL", "PROCESS", "RESOURCE", "PROGRAM", "EPISODE", "METASTABLE", "SETTING", "RECORD", "SYSTEM"];
LAB.eventRow = e => `<div class="ev ${e.level} ${["DETECTION", "HEALTH", "STRESS", "METASTABLE", "RECOVERY", "KERNEL"].includes(e.kind) ? "key" : ""}"><div class="et">${OS.clock(e.ts)}</div><div class="ed"></div><div><span class="ek">${OS.esc(e.kind)}</span><span class="em">${OS.esc(e.msg)}</span></div></div>`;

/* ------------------------------------------------------- shared-axis charts -- */
/* All charts on a page share one x-range; hovering one draws a crosshair on all of them. */
LAB.CHART_DEF = {
  cpu_util: { t: "CPU utilization", max: 100 }, run_queue: { t: "CPU run queue" }, cpu_wait: { t: "CPU waiting time" },
  response_ms: { t: "Response latency" }, mem_util: { t: "Memory utilization", max: 100 }, mem_avail: { t: "Available memory" },
  page_faults: { t: "Page fault rate" }, paging_overhead: { t: "Paging overhead (CPU time)" }, swap_iops: { t: "Swap I/O" },
  disk_util: { t: "Disk I/O utilization (busy time)", max: 100 }, disk_used: { t: "File system used (space)", max: 100 }, iops: { t: "Read / write IOPS", series: ["read_iops", "write_iops"], names: ["read", "write"] },
  io_queue: { t: "I/O queue length" }, disk_latency: { t: "Disk latency" }, rtt: { t: "Network latency (RTT)" },
  packet_loss: { t: "Packet loss" }, reqs: { t: "Successful vs failed requests", series: ["throughput", "req_failed"], names: ["successful /s", "failed /s"] },
  timeouts: { t: "Timeouts" }, error_rate: { t: "Error rate" }, accept_backlog: { t: "Request backlog" }, availability: { t: "Service availability", max: 100 },
  throughput: { t: "Throughput" }, blocked_procs: { t: "Blocked processes" }, inflight: { t: "Requests in flight" }, capacity: { t: "Service capacity" },
};
LAB.GROUPS = [
  ["cpu", "CPU", ["cpu_util", "run_queue", "cpu_wait", "response_ms"]],
  ["memory", "Memory", ["mem_util", "mem_avail", "page_faults", "paging_overhead", "response_ms"]],
  ["disk", "Disk", ["disk_util", "iops", "io_queue", "disk_latency", "disk_used", "response_ms"]],
  ["network", "Network", ["rtt", "packet_loss", "reqs", "timeouts", "error_rate"]],
  ["service", "Service", ["response_ms", "error_rate", "accept_backlog", "throughput", "availability"]],
];
LAB.COLORS = ["#2f5bd3", "#c4561a", "#7a4fc9", "#0f8a7e"];
LAB.hoverX = null;
const labPlugin = {
  id: "labOverlay",
  beforeDatasetsDraw(chart, a, o) {
    if (!o || !o.on) return;
    const { ctx, chartArea: A, scales: { x } } = chart;
    // health background per sample
    const cols = [null, "rgba(168,107,12,.08)", "rgba(196,86,26,.09)", "rgba(192,48,48,.10)", "rgba(143,29,29,.14)"];
    const ss = o.samples || [];
    for (let i = 0; i < ss.length; i++) {
      const s = ss[i]; if (!s.health) continue;
      const x0 = x.getPixelForValue(s.ts - 0.25), x1 = x.getPixelForValue(s.ts + 0.25);
      if (x1 < A.left || x0 > A.right) continue;
      ctx.fillStyle = cols[s.health]; ctx.fillRect(Math.max(A.left, x0), A.top, Math.min(A.right, x1) - Math.max(A.left, x0), A.bottom - A.top);
    }
  },
  afterDatasetsDraw(chart, a, o) {
    if (!o || !o.on) return;
    const { ctx, chartArea: A, scales: { x, y } } = chart;
    (o.thresholds || []).forEach(t => {
      if (t.v == null || t.v > y.max || t.v < y.min) return;
      const py = y.getPixelForValue(t.v);
      ctx.save(); ctx.strokeStyle = t.c; ctx.setLineDash([5, 4]); ctx.lineWidth = 1; ctx.beginPath(); ctx.moveTo(A.left, py); ctx.lineTo(A.right, py); ctx.stroke();
      ctx.setLineDash([]); ctx.fillStyle = t.c; ctx.font = "600 10px Segoe UI, system-ui, sans-serif"; ctx.fillText(t.l, A.left + 4, py - 3); ctx.restore();
    });
    (o.marks || []).forEach(m => {
      const px = x.getPixelForValue(m.ts); if (px < A.left || px > A.right) return;
      ctx.save(); ctx.strokeStyle = m.c; ctx.globalAlpha = m.kind === "CONTROL" ? .45 : .8; ctx.lineWidth = 1; ctx.setLineDash(m.kind === "DETECTION" ? [3, 3] : []);
      ctx.beginPath(); ctx.moveTo(px, A.top); ctx.lineTo(px, m.kind === "CONTROL" ? A.top + 10 : A.bottom); ctx.stroke(); ctx.restore();
    });
    if (LAB.hoverX != null) {
      const px = x.getPixelForValue(LAB.hoverX);
      if (px >= A.left && px <= A.right) { ctx.save(); ctx.strokeStyle = "#344054"; ctx.globalAlpha = .5; ctx.beginPath(); ctx.moveTo(px, A.top); ctx.lineTo(px, A.bottom); ctx.stroke(); ctx.restore(); }
    }
  },
};
if (window.Chart) Chart.register(labPlugin);
LAB.thLines = function (k) {
  const r = LAB.thresholds(k); if (!r) return [];
  const out = [], C = { W: ["#a86b0c", "Warning"], C: ["#c03030", "Critical"], D: ["#c4561a", "Degraded"] };
  ["W", "C"].forEach(x => { if (r.t[x] != null) out.push({ v: r.t[x], c: C[x][0], l: `${C[x][1]} ${r.inv ? "<" : ">"} ${LAB.num(r.t[x])}` }); });
  return out;
};
/* smallest y-range per metric, so a quiet baseline is not stretched over the whole chart
   (e.g. a run queue of 0.02 would otherwise produce axis labels 0.01, 0.01, 0.01) */
LAB.MIN_RANGE = { cpu_wait: 20, cpu_wait_web: 20, accept_backlog: 50, inflight: 200, timeouts: 10, blocked_procs: 4,
  lock_wait: 100, retrans_ms: 50, swap_iops: 100, req_failed: 10, queue_wait: 100, procs: 4, iops: 500 };
LAB.suggestedMax = function (key, keys) {
  if ((LAB.CHART_DEF[key] || {}).max != null) return undefined;
  const r = LAB.thresholds(keys[0]);
  if (r && !r.inv && r.t.W != null) return r.t.W * 1.15;          // the warning line is always in view
  return LAB.MIN_RANGE[key] != null ? LAB.MIN_RANGE[key] : LAB.MIN_RANGE[keys[0]];
};
LAB.makeChart = function (canvas, key, all) {
  const def = LAB.CHART_DEF[key] || { t: LAB.label(key) };
  const keys = def.series || [key];
  const ch = new Chart(canvas, {
    type: "line",
    data: { datasets: keys.map((k, i) => ({ label: def.names ? def.names[i] : LAB.label(k), key: k, data: [], borderColor: LAB.COLORS[i], backgroundColor: "transparent", borderWidth: 1.8, pointRadius: 0, tension: .2 })) },
    options: {
      animation: false, responsive: true, maintainAspectRatio: false, parsing: false, normalized: true,
      interaction: { mode: "nearest", axis: "x", intersect: false },
      onHover: (e, el, chart) => { const v = chart.scales.x.getValueForPixel(e.x); LAB.hoverX = v; (all || []).forEach(c => c.chart !== chart && c.chart.draw()); },
      plugins: { legend: { display: keys.length > 1, position: "bottom", labels: { boxWidth: 10, boxHeight: 2, font: { size: 11 } } },
        tooltip: { callbacks: { title: it => (it.length ? OS.clock(it[0].parsed.x) : ""), label: it => `${it.dataset.label}: ${LAB.fmt(it.dataset.key, it.parsed.y)}` } },
        labOverlay: { on: true, thresholds: LAB.thLines(key), marks: [], samples: [] } },
      scales: { x: { type: "linear", grid: { color: "#f0f2f5" }, ticks: { maxTicksLimit: 5, color: "#98a2b3", font: { size: 10 }, callback: v => OS.clock(v) } },
        y: { beginAtZero: true, max: def.max, suggestedMax: LAB.suggestedMax(key, keys), grid: { color: "#f0f2f5" }, ticks: { color: "#687385", font: { size: 10 }, maxTicksLimit: 5, callback: v => LAB.num(v) + (LAB.unit(keys[0]) === "%" ? "%" : "") } } },
    },
  });
  canvas.addEventListener("mouseleave", () => { LAB.hoverX = null; (all || []).forEach(c => c.chart.draw()); });
  return { key, def, keys, chart: ch };
};
LAB.markColor = e => (e.kind === "DETECTION" ? (e.data && e.data.to === 0 ? "#157f4a" : "#8f1d1d") : e.kind === "STRESS" ? "#c03030" : e.kind === "RECOVERY" ? "#157f4a" : e.kind === "ACTION" ? "#7a4fc9" : "#2f5bd3");
LAB.fillCharts = function (list, samples, xmin, xmax, events) {
  const marks = (events || []).filter(e => ["CONTROL", "DETECTION", "STRESS", "ACTION", "RECOVERY"].includes(e.kind)).map(e => ({ ts: e.ts, kind: e.kind, c: LAB.markColor(e) }));
  list.forEach(c => {
    c.chart.data.datasets.forEach(ds => { ds.data = samples.map(s => ({ x: s.ts, y: s[ds.key] == null ? null : s[ds.key] })); });
    c.chart.options.scales.x.min = xmin; c.chart.options.scales.x.max = xmax;
    const o = c.chart.options.plugins.labOverlay; o.marks = marks; o.samples = samples; o.thresholds = LAB.thLines(c.key);
    c.chart.update("none");
  });
};
