/* ======================================================================
   Shared helpers used by every page: API, formatting, sidebar status,
   charts (phase bands + markers + thresholds) and the OS-subsystem visuals.
   ====================================================================== */
"use strict";

const OS = {};
OS.meta = null;

OS.api = async function (url, opts) {
  const r = await fetch(url, Object.assign({ headers: { "Content-Type": "application/json" } }, opts || {}));
  let body = null;
  try { body = await r.json(); } catch (e) { body = null; }
  if (!r.ok) { const err = new Error((body && body.message) || r.statusText); err.body = body; throw err; }
  return body;
};

OS.loadMeta = async function () {
  if (!OS.meta) OS.meta = await OS.api("/api/meta");
  OS.scenarioById = {};
  OS.meta.scenarios.forEach(s => (OS.scenarioById[s.id] = s));
  return OS.meta;
};

/* ---------------------------------------------------------- formatting --- */
OS.$ = id => document.getElementById(id);
OS.esc = s => String(s == null ? "" : s).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
OS.clock = ts => (ts ? new Date(ts * 1000).toLocaleTimeString("en-GB", { hour12: false }) : "—");
OS.secs = v => (v == null ? "—" : Number(v).toFixed(1) + " s");
OS.num = function (key, v) {
  const m = (OS.meta && OS.meta.metrics[key]) || { dp: 1 };
  if (v == null) return "—";
  if (typeof v === "string") return v;
  const dp = m.dp == null ? 1 : m.dp;
  return Number(v).toLocaleString("en-US", { minimumFractionDigits: dp, maximumFractionDigits: dp });
};
OS.unit = key => { const u = ((OS.meta && OS.meta.metrics[key]) || {}).unit || ""; return u; };
OS.fmt = function (key, v) {
  if (v == null) return key === "response_ms" ? "no response" : "—";
  if (typeof v === "string") return v;
  const u = OS.unit(key);
  return OS.num(key, v) + (u === "%" ? "%" : u ? " " + u : "");
};
OS.label = key => ((OS.meta && OS.meta.metrics[key]) || { label: key }).label;
OS.pill = (state, text) => `<span class="pill ${OS.esc(state)}"><span class="dot"></span>${OS.esc(text || state)}</span>`;
OS.LOWER_IS_WORSE = ["mem_avail_mb", "availability", "reachability", "throughput", "req_ok", "running_procs"];

/* metric -> [threshold group, key, lowerIsWorse] */
OS.TH = {
  cpu: ["cpu", "cpu"], mem: ["memory", "mem"], page_faults: ["memory", "page_faults"],
  io_queue: ["disk", "io_queue"], disk_latency: ["disk", "disk_latency"],
  availability: ["process", "availability", true], net_latency: ["network", "net_latency"],
  packet_loss: ["network", "packet_loss"], reachability: ["network", "reachability", true],
};
OS.levelOf = function (key, v) {
  if (key === "deadlock") return v === "YES" ? 3 : 0;
  if (key === "web_state") return v === "TERMINATED" ? 3 : v === "RESTARTING" ? 1 : 0;
  if (key === "waiting_procs" || key === "blocked_procs" || key === "stuck_procs") {
    const t = OS.meta.thresholds.deadlock.stuck; return v >= t.F ? 3 : v >= t.D ? 2 : v >= t.W ? 1 : 0;
  }
  const d = OS.TH[key]; if (!d || v == null || typeof v !== "number") return 0;
  const t = OS.meta.thresholds[d[0]][d[1]];
  if (d[2]) return v <= t.F ? 3 : v <= t.D ? 2 : v < t.W ? 1 : 0;
  return v > t.F ? 3 : v > t.D ? 2 : v > t.W ? 1 : 0;
};

/* --------------------------------------------- sidebar server status ---- */
OS.setTopHealth = function (h, exp) {
  const el = OS.$("topHealth");
  if (el) { el.className = "pill " + h; el.innerHTML = `<span class="dot"></span><span>${OS.esc(h)}</span>`; }
  const live = OS.$("navLive"), se = OS.$("sideExp");
  if (live) live.hidden = !exp;
  if (se) se.textContent = exp ? "Experiment running: " + exp : "";
};
OS.pollHealth = async function () {
  try { const h = await OS.api("/api/health"); OS.setTopHealth(h.health, h.experiment); } catch (e) { /* offline */ }
};
OS.startHealthPolling = function () { OS.pollHealth(); setInterval(OS.pollHealth, 2000); };

/* ------------------------------------------------- server selection ---- */
/* The selected server is remembered in a cookie, so every page and API call
   (state, inject, recover, history …) works on that server. */
OS.selectServer = function (id, href) {
  document.cookie = `os_server=${encodeURIComponent(id)}; path=/; max-age=31536000; SameSite=Lax`;
  const p = location.pathname;
  location.href = href || (p.startsWith("/experiment/") ? "/history" : p);
};
OS.serverLabel = s => (s ? `${s.id} · ${s.name}` : "");
document.addEventListener("DOMContentLoaded", () => {
  const pick = OS.$("serverPick");
  if (!pick) return;
  const cur = pick.value;
  pick.addEventListener("change", () => {
    if (pick.value === "add") { pick.value = cur; location.href = "/?add=1"; return; }
    OS.selectServer(pick.value);
  });
});

/* ---------------------------------------------------------------- charts --- */
OS.CHART = {
  cpu: { title: "CPU utilization", max: 100 },
  mem: { title: "Memory utilization", max: 100 },
  disk: { title: "Disk utilization", max: 100 },
  response_ms: { title: "Response time" },
  cpu_queue: { title: "CPU run queue length" },
  wait_ms: { title: "Process waiting time" },
  page_faults: { title: "Page fault rate" },
  swap_io: { title: "Paging / swap activity" },
  disk_latency: { title: "Disk I/O latency" },
  io_queue: { title: "I/O queue length" },
  net_latency: { title: "Network latency (RTT)" },
  packet_loss: { title: "Packet loss" },
  req_failed: { title: "Successful vs failed requests", series: ["req_ok", "req_failed"], names: ["Successful /s", "Failed /s"] },
  availability: { title: "Service availability", max: 100 },
  running_procs: { title: "Running processes" },
  waiting_procs: { title: "Processes in circular wait" },
  blocked_procs: { title: "Blocked processes (queued on locks)" },
  reachability: { title: "Service reachability", max: 100 },
  throughput: { title: "Throughput (successful requests)" },
  proc_count: { title: "Active processes" },
  terminated_procs: { title: "Terminated processes" },
  timeouts: { title: "Connection timeouts" },
  mem_avail_mb: { title: "Available memory" },
  iops: { title: "Read / write IOPS", series: ["read_iops", "write_iops"], names: ["Read IOPS", "Write IOPS"] },
};
/* Interactive mode: the CONTROL INPUT chart — what the operator set (stepped line) vs the
   condition the simulated system is actually experiencing (lags behind: inertia). */
OS.controlChartDef = function (sid) {
  const sc = OS.scenarioById[sid], cs = (sc && sc.controls) || [];
  if (!cs.length) return null;
  const c = cs[0], u = c.unit === "%" ? "%" : " " + c.unit;
  const def = { title: "Control input — " + c.label.toLowerCase(), series: ["ctl_" + c.key, "eff_" + c.key],
                names: ["Set by operator (" + c.unit + ")", "Experienced by the system"], max: c.max, stepped: [0], unit: u };
  if (cs[1]) { // network: loss on a second axis
    def.series.push("ctl_" + cs[1].key, "eff_" + cs[1].key);
    def.names.push(cs[1].label + " set (%)", cs[1].label + " experienced");
    def.y2 = [2, 3]; def.y2max = cs[1].max; def.stepped = [0, 2];
    def.title = "Control input — delay and packet loss";
  }
  return def;
};
OS.CHART_TABS = [
  ["failure", "Failure-specific"],
  ["perf", "Performance", ["cpu", "mem", "response_ms"]],
  ["io", "I/O", ["disk", "disk_latency", "io_queue"]],
  ["net", "Network", ["net_latency", "packet_loss", "req_failed"]],
];
OS.COLORS = ["#2f5bd3", "#c4561a", "#7a4fc9", "#0f8a7e"];

const phasePlugin = {
  id: "phases",
  beforeDatasetsDraw(chart, args, o) {
    if (!o || !o.markers || !o.markers.length) return;
    const { ctx, chartArea: a, scales: { x } } = chart;
    const end = o.end || x.max;
    const band = (t0, t1, b) => {
      if (t0 == null) return;
      const x0 = Math.max(a.left, x.getPixelForValue(t0)), x1 = Math.min(a.right, x.getPixelForValue(t1 == null ? end : t1));
      if (x1 <= x0) return;
      ctx.save(); ctx.fillStyle = b.c; ctx.fillRect(x0, a.top, x1 - x0, a.bottom - a.top);
      if (o.labels && x1 - x0 > 58) { ctx.fillStyle = b.t; ctx.font = "700 9px Segoe UI, system-ui, sans-serif"; ctx.textAlign = "left"; ctx.fillText(b.l, x0 + 4, a.bottom - 5); }
      ctx.restore();
    };
    OS.phaseBands(o.markers).forEach(b => band(b.t0, b.t1, b));
  },
  afterDatasetsDraw(chart, args, o) {
    if (!o) return;
    const { ctx, chartArea: a, scales: { x, y } } = chart;
    (o.thresholds || []).forEach(t => {
      if (t.v == null || t.v > y.max || t.v < y.min) return;
      const py = y.getPixelForValue(t.v);
      ctx.save(); ctx.strokeStyle = t.color; ctx.setLineDash([5, 4]); ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(a.left, py); ctx.lineTo(a.right, py); ctx.stroke();
      ctx.setLineDash([]); ctx.fillStyle = t.color; ctx.font = "600 10px Segoe UI, system-ui, sans-serif";
      ctx.textAlign = "left"; ctx.fillText(t.label, a.left + 4, py - 4); ctx.restore();
    });
    const style = { inject: ["#a86b0c", o.interactive ? "Load applied" : "Injected"], detect: ["#8f1d1d", "Detected"],
                    recovery: ["#2667c9", "Recovery"], recovered: ["#157f4a", "Recovered"] };
    let lastEnd = -1e9, row = 0;
    (o.markers || []).forEach(m => {
      const s = style[m.kind]; if (!s) return;
      const px = x.getPixelForValue(m.ts);
      if (px < a.left - 1 || px > a.right + 1) return;
      ctx.save(); ctx.strokeStyle = s[0]; ctx.globalAlpha = .75; ctx.lineWidth = 1.2; ctx.setLineDash(m.kind === "detect" ? [3, 3] : []);
      ctx.beginPath(); ctx.moveTo(px, a.top); ctx.lineTo(px, a.bottom); ctx.stroke(); ctx.restore();
      ctx.save(); ctx.font = "600 10px Segoe UI, system-ui, sans-serif";
      const w = ctx.measureText(s[1]).width;
      let lx = px + 4; if (lx + w > a.right) lx = px - w - 4;
      row = lx < lastEnd + 4 ? row + 1 : 0; lastEnd = lx + w;
      ctx.fillStyle = s[0]; ctx.textBaseline = "top"; ctx.fillText(s[1], lx, a.top + 3 + row * 12); ctx.restore();
    });
  },
};
if (window.Chart) Chart.register(phasePlugin);

/* Experiment phases as time bands, derived from the marker sequence:
   BASELINE → CONDITION APPLIED (degrading, not yet detected) → FAILURE (detected)
   → RECOVERY (→ back to FAILURE if interrupted) → done; PAUSED drawn on top. */
OS.BAND = {
  baseline: { c: "rgba(102,112,133,.07)", t: "#667085", l: "BASELINE" },
  applied: { c: "rgba(168,107,12,.07)", t: "#a86b0c", l: "LOAD APPLIED" },
  failure: { c: "rgba(192,48,48,.07)", t: "#c03030", l: "FAILURE" },
  recovery: { c: "rgba(38,103,201,.08)", t: "#2667c9", l: "RECOVERY" },
  paused: { c: "rgba(16,24,40,.10)", t: "#344054", l: "PAUSED" },
};
OS.phaseBands = function (markers) {
  const ms = (markers || []).slice().sort((p, q) => p.ts - q.ts), out = [];
  let cur = null, t0 = null, detected = false;
  const go = (k, t) => { if (cur) out.push(Object.assign({ t0, t1: t }, OS.BAND[cur])); cur = k; t0 = t; };
  ms.forEach(m => {
    if (m.kind === "baseline") go("baseline", m.ts);
    else if (m.kind === "ready" && cur === "baseline") go(null, m.ts);
    else if (m.kind === "inject") go("applied", m.ts);
    else if (m.kind === "detect") { detected = true; if (cur !== "recovery") go("failure", m.ts); }
    else if (m.kind === "recovery") go("recovery", m.ts);
    else if (m.kind === "interrupt") go(detected ? "failure" : "applied", m.ts);
    else if (m.kind === "recovered" || m.kind === "finished") go(null, m.ts);
  });
  if (cur) out.push(Object.assign({ t0, t1: null }, OS.BAND[cur]));
  // pauses drawn last (on top)
  let p0 = null;
  ms.forEach(m => { if (m.kind === "pause") p0 = m.ts; else if (m.kind === "resume" && p0 != null) { out.push(Object.assign({ t0: p0, t1: m.ts }, OS.BAND.paused)); p0 = null; } });
  if (p0 != null) out.push(Object.assign({ t0: p0, t1: null }, OS.BAND.paused));
  return out;
};

OS.thresholdLines = function (key) {
  const d = OS.TH[key]; if (!d || !OS.meta) return [];
  const t = OS.meta.thresholds[d[0]][d[1]];
  if (d[2]) return [{ v: t.D, label: "Degraded ≤ " + t.D, color: "#c4561a" }];
  return [{ v: t.W, label: "Warning > " + t.W, color: "#a86b0c" }, { v: t.F, label: "Failed > " + t.F, color: "#c03030" }];
};

OS.chartDef = function (key, sid) {
  if (key === "control") return OS.controlChartDef(sid) || { title: "Control input" };
  return OS.CHART[key] || { title: OS.label(key) };
};
OS.makeChart = function (canvas, key, sid, opts) {
  const def = OS.chartDef(key, sid);
  const keys = def.series || [key];
  const y2 = def.y2 || [], stepped = def.stepped || [];
  const datasets = keys.map((k, i) => ({
    label: def.names ? def.names[i] : OS.label(k), key: k, data: [],
    borderColor: OS.COLORS[i % OS.COLORS.length], backgroundColor: i === 0 ? "rgba(47,91,211,.07)" : "transparent",
    fill: i === 0 && !stepped.length, borderWidth: stepped.includes(i) ? 2.2 : 2, pointRadius: 0,
    tension: stepped.includes(i) ? 0 : 0.25, stepped: stepped.includes(i) ? "before" : false,
    borderDash: key === "control" && !stepped.includes(i) ? [] : undefined,
    yAxisID: y2.includes(i) ? "y2" : "y",
  }));
  const unit = u => (u ? (u === "%" ? "%" : " " + u) : "");
  const yu = key === "control" ? "" : unit(OS.unit(keys[0]));
  const scales = {
    x: { type: "linear", grid: { color: "#f0f2f5" }, ticks: { maxTicksLimit: 5, color: "#98a2b3", font: { size: 11 }, callback: v => OS.clock(v) } },
    y: { beginAtZero: true, grid: { color: "#f0f2f5" }, ticks: { color: "#687385", font: { size: 11 }, maxTicksLimit: 5, callback: v => Number(v).toLocaleString() + yu } },
  };
  if (def.max) scales.y.max = def.max;
  if (y2.length) scales.y2 = { position: "right", beginAtZero: true, max: def.y2max, grid: { display: false }, ticks: { color: "#7a4fc9", font: { size: 11 }, maxTicksLimit: 5, callback: v => v + "%" } };
  return new Chart(canvas, {
    type: "line",
    data: { datasets },
    options: {
      animation: false, responsive: true, maintainAspectRatio: false, parsing: false, normalized: true,
      interaction: { mode: "index", intersect: false },
      plugins: {
        legend: { display: keys.length > 1, position: "bottom", labels: { boxWidth: 12, boxHeight: 2, font: { size: 12 }, color: "#3d4757" } },
        tooltip: { callbacks: { title: it => (it.length ? OS.clock(it[0].parsed.x) : "") } },
        phases: { markers: [], thresholds: OS.thresholdLines(key), labels: !!(opts && opts.labels), interactive: !!(opts && opts.interactive) },
      },
      scales,
    },
  });
};

OS.fillChart = function (chart, samples, xmin, xmax, markers) {
  chart.data.datasets.forEach(ds => { ds.data = samples.map(s => ({ x: s.ts, y: s[ds.key] == null ? null : s[ds.key] })); });
  chart.options.scales.x.min = xmin;
  chart.options.scales.x.max = xmax;
  chart.options.plugins.phases.markers = markers || [];
  chart.options.plugins.phases.end = xmax;
  chart.update("none");
};

/* Builds a set of charts into `grid` (first chart full width when there are 3). */
OS.buildCharts = function (grid, keys, sid, opts) {
  grid.innerHTML = "";
  grid.className = "chart-grid" + (keys.length === 1 ? " one" : "");
  return keys.map((k, i) => {
    const def = OS.chartDef(k, sid);
    const el = document.createElement("div");
    el.className = "chart" + (k === "control" ? " control" : "");
    if (keys.length === 3 && i === 0) el.style.gridColumn = "1 / -1";
    el.innerHTML = `<div class="ch">${OS.esc(def.title)}<span class="now"></span></div><div class="chart-box"><canvas></canvas></div>`;
    grid.appendChild(el);
    return { key: k, def, el, now: el.querySelector(".now"), chart: OS.makeChart(el.querySelector("canvas"), k, sid, opts) };
  });
};
/* value shown in a chart header ("now" / "peak") */
OS.chartNow = function (c, sample) {
  if (!sample) return "";
  if (c.key === "control") {
    const sc = c.def.series, u = c.def.unit || "";
    const v = k => (sample[k] == null ? "—" : Number(sample[k]).toFixed(sample[k] >= 100 ? 0 : 1).replace(/\.0$/, ""));
    let t = `set ${v(sc[0])}${u} · experienced ${v(sc[1])}${u}`;
    if (sc[2]) t += ` · loss ${v(sc[2])}%`;
    return t;
  }
  if (c.key === "iops") return `${OS.num("read_iops", sample.read_iops)} r · ${OS.num("write_iops", sample.write_iops)} w`;
  return OS.fmt(c.key, sample[c.key]);
};
OS.destroyCharts = list => (list || []).forEach(c => c.chart.destroy());
OS.chartLegend = `<div class="chart-legend"><span><i style="background:rgba(102,112,133,.16)"></i>Baseline</span><span><i style="background:rgba(168,107,12,.18)"></i>Condition applied (not yet detected)</span><span><i style="background:rgba(192,48,48,.16)"></i>Failure detected</span><span><i style="background:rgba(38,103,201,.18)"></i>Recovery</span><span><i style="background:rgba(16,24,40,.18)"></i>Paused</span><span><i class="dash"></i>Detection threshold</span></div>`;

/* --------------------------------------------------------- event log --- */
OS.isKeyEvent = e => !!e.marker || ["INJECTION", "DETECTION", "STATE"].includes(e.kind) || /RECOVERY (STARTED|COMPLETED)/.test(e.msg);
OS.renderEvent = e => `<div class="ev ${e.level} ${OS.isKeyEvent(e) ? "key" : ""}"><div class="et">${OS.esc(e.time)}</div><div class="ed"></div><div><span class="ek">${OS.esc(e.kind)}</span><span class="em">${OS.esc(e.msg)}</span></div></div>`;

/* ----------------------------------------------------- process table --- */
OS.renderProcTable = function (snap) {
  if (!snap) return "";
  const rows = snap.processes.map(p => `<tr>
      <td class="mono">${p.pid}</td><td><b>${OS.esc(p.name)}</b>${p.critical ? ' <span class="muted small">(critical)</span>' : ""}</td>
      <td><span class="st ${p.state}">${p.state}</span></td>
      <td class="mono">${p.state === "TERMINATED" ? "0.0" : Number(p.cpu || 0).toFixed(1)}</td>
      <td class="mono">${Math.round(p.mem_mb || 0).toLocaleString()} MB</td>
      <td class="mono">${p.holds && p.holds.length ? p.holds.join(", ") : "—"}</td>
      <td class="mono">${p.waiting_for || "—"}</td>
      <td class="muted">${OS.esc(p.exit || p.info || "")}</td></tr>`).join("");
  return `<div class="tbl-wrap"><table class="data compact"><thead><tr><th>PID</th><th>Process</th><th>State</th><th>CPU %</th><th>Memory</th><th>Holds</th><th>Waits for</th><th>Note</th></tr></thead><tbody>${rows}</tbody></table></div>`;
};

/* ------------------------------------------------------------ deadlock --- */
OS.shortName = p => p.name.split(" ").pop();

OS.renderDeadlockSvg = function (snap, dl) {
  const procs = snap.processes.filter(p => p.role === "txn").sort((a, b) => a.name.localeCompare(b.name));
  const res = snap.resources;
  const n = procs.length;
  const cycle = new Set((dl && dl.cycle) || []);
  const pos = {}, ring = [];
  const between = n === 3 ? ["B", "C", "A"] : ["B", "A"];
  procs.forEach((p, i) => { ring.push({ t: "p", id: OS.shortName(p), p }); const r = res.find(z => z.id === between[i]); if (r) ring.push({ t: "r", id: r.id, r }); });
  let W = 380, H = 260;
  if (n === 2) { const c = [[85, 65], [295, 65], [295, 195], [85, 195]]; ring.forEach((nd, i) => (pos[nd.t + nd.id] = c[i])); }
  else { H = 320; ring.forEach((nd, i) => { const a = (-90 + i * 60) * Math.PI / 180; pos[nd.t + nd.id] = [190 + 125 * Math.cos(a), 160 + 125 * Math.sin(a)]; }); }
  const nameOf = {}; snap.processes.forEach(p => (nameOf[p.pid] = OS.shortName(p)));
  const edges = [];
  res.forEach(r => { if (r.holder != null && pos["p" + nameOf[r.holder]] && pos["r" + r.id]) edges.push({ from: "r" + r.id, to: "p" + nameOf[r.holder], kind: "held by", hot: cycle.size > 0 && cycle.has(nameOf[r.holder]) }); });
  procs.forEach(p => { if (p.waiting_for && pos["r" + p.waiting_for]) edges.push({ from: "p" + OS.shortName(p), to: "r" + p.waiting_for, kind: "waits for", hot: cycle.has(OS.shortName(p)) }); });
  const line = e => {
    const [x1, y1] = pos[e.from], [x2, y2] = pos[e.to];
    const dx = x2 - x1, dy = y2 - y1, L = Math.hypot(dx, dy) || 1;
    const ax = x1 + dx / L * 32, ay = y1 + dy / L * 32, bx = x2 - dx / L * 36, by = y2 - dy / L * 36;
    const col = e.hot ? "#c03030" : e.kind === "held by" ? "#475467" : "#a86b0c";
    const mx = (ax + bx) / 2, my = (ay + by) / 2, vert = Math.abs(dx) < Math.abs(dy);
    return `<line x1="${ax}" y1="${ay}" x2="${bx}" y2="${by}" stroke="${col}" stroke-width="${e.hot ? 2.4 : 1.8}" ${e.kind === "waits for" ? 'stroke-dasharray="6 4"' : ""} marker-end="url(#dl-${e.hot ? "hot" : e.kind === "held by" ? "held" : "wait"})"/>
      ${n === 2 ? `<text x="${vert ? mx + 8 : mx}" y="${vert ? my + 4 : my - 8}" text-anchor="${vert ? "start" : "middle"}" font-size="11" font-weight="600" fill="${col}">${e.kind}</text>` : ""}`;
  };
  const stc = { RUNNING: ["#e8f5ee", "#157f4a"], WAITING: ["#fdf5e6", "#a86b0c"], TERMINATED: ["#fcecec", "#c03030"], BLOCKED: ["#fdf0e8", "#c4561a"], READY: ["#f1f3f6", "#475467"] };
  const nodes = ring.map(nd => {
    const [x, y] = pos[nd.t + nd.id];
    if (nd.t === "p") {
      const c = stc[nd.p.state] || stc.READY;
      return `<circle cx="${x}" cy="${y}" r="28" fill="${c[0]}" stroke="${c[1]}" stroke-width="2"/>
        <text x="${x}" y="${y + 1}" text-anchor="middle" font-size="16" font-weight="700" fill="#1d2433">${nd.id}</text>
        <text x="${x}" y="${y + 15}" text-anchor="middle" font-size="8.5" font-weight="700" fill="${c[1]}">${nd.p.state}</text>`;
    }
    const locked = nd.r.holder != null;
    return `<rect x="${x - 30}" y="${y - 26}" width="60" height="52" rx="8" fill="${locked ? "#fff" : "#f8f9fb"}" stroke="${locked ? "#475467" : "#d0d5dd"}" stroke-width="1.8"/>
      <text x="${x}" y="${y - 2}" text-anchor="middle" font-size="11" font-weight="600" fill="#687385">Resource</text>
      <text x="${x}" y="${y + 15}" text-anchor="middle" font-size="16" font-weight="700" fill="#1d2433">${nd.id}</text>`;
  }).join("");
  const mk = (id, c) => `<marker id="dl-${id}" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0 0L10 5L0 10z" fill="${c}"/></marker>`;
  return `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Resource allocation graph"><defs>${mk("held", "#475467")}${mk("wait", "#a86b0c")}${mk("hot", "#c03030")}</defs>${edges.map(line).join("")}${nodes}</svg>`;
};

OS.renderDeadlock = function (snap, dl, opts) {
  opts = opts || {};
  dl = dl || { coffman: {}, cycle: [], detected: false, cycle_age: 0 };
  const th = OS.meta.thresholds.deadlock.blocked_seconds;
  const procs = snap.processes.filter(p => p.role === "txn").sort((a, b) => a.name.localeCompare(b.name));
  const lines = procs.map(p => {
    const holds = p.holds && p.holds.length ? p.holds.map(r => `<span class="res">${r}</span>`).join(" ") : null;
    const parts = [`<span class="p">${OS.esc(OS.shortName(p))}</span>`];
    if (holds) parts.push(`<span class="arr">→</span> holds ${holds}`);
    if (p.waiting_for) parts.push(`<span class="arr">→</span> <span class="w">waits for</span> <span class="res">${p.waiting_for}</span>`);
    if (!holds && !p.waiting_for) parts.push(`<span class="muted">${p.state === "TERMINATED" ? "terminated (victim)" : "running — no locks held"}</span>`);
    return `<div class="dl-line">${parts.join(" ")}</div>`;
  }).join("");
  const cyc = dl.cycle && dl.cycle.length ? dl.cycle.concat(dl.cycle[0]).join(" → ") : "";
  let verdict;
  if (dl.detected) verdict = `<div class="dl-verdict yes">Circular wait detected — deadlock (${OS.esc(cyc)})</div>`;
  else if (dl.cycle && dl.cycle.length) verdict = `<div class="dl-verdict forming">Circular wait forming — confirming (${Number(dl.cycle_age).toFixed(1)} / ${th} s)</div>`;
  else verdict = `<div class="dl-verdict ${opts.cleared ? "clear" : ""}">${opts.cleared ? "Deadlock resolved — no circular wait" : "No circular wait"}</div>`;
  const C = [["mutual_exclusion", "Mutual Exclusion"], ["hold_and_wait", "Hold and Wait"], ["no_preemption", "No Preemption"], ["circular_wait", "Circular Wait"]];
  const coff = C.map(([k, t]) => `<li class="${dl.coffman && dl.coffman[k] ? "on" : ""}"><span class="ck">✓</span>${t}</li>`).join("");
  const blocked = snap.processes.filter(p => p.role === "queued" && p.state === "BLOCKED").length;
  return `<div class="deadlock">
      <div>${verdict}<div class="dl-lines">${lines}</div>
        <div class="sub-label">Coffman conditions</div><ul class="coffman">${coff}</ul>
        <p class="small muted" style="margin-top:12px">${blocked ? `${blocked} more process${blocked > 1 ? "es are" : " is"} blocked behind the locked resources. ` : ""}Simulated locks only — the application itself never deadlocks.</p></div>
      <div class="dl-svg">${OS.renderDeadlockSvg(snap, dl)}<p class="small muted" style="text-align:center;margin-top:6px">Solid arrow: resource <b>held by</b> process · dashed arrow: process <b>waits for</b> resource</p></div>
    </div>`;
};

/* ------------------------------------------- failure-specific visuals --- */
OS.renderSubsystem = function (sid, cur, det, opts) {
  if (!cur) return "";
  const m = cur.metrics, snap = cur.snapshot, f = OS.fmt;
  const S = Object.assign({ id: "Server-01", ip: "10.0.1.15", vcpus: 4, ram_mb: 16384, disk_iops_capacity: 6000 },
    (OS.meta && OS.meta.server) || {}, (opts && opts.server) || {});
  const byPid = {}; snap.processes.forEach(p => (byPid[p.pid] = p));
  if (sid === "deadlock") return OS.renderDeadlock(snap, det && det.deadlock, opts);
  if (sid === "cpu") {
    const sched = snap.cores.some(x => x != null);
    const cores = Array.from({ length: S.vcpus }, (_, i) => i).map(i => {
      const p = sched ? byPid[snap.cores[i]] : null;
      return `<div class="core ${m.cpu > 85 ? "busy" : ""}"><div class="cn">vCPU ${i}</div><div class="cname">${p ? OS.esc(p.name) : "normal load"}</div></div>`;
    }).join("");
    const rq = snap.ready_queue.map(pid => byPid[pid]).filter(Boolean);
    const chips = rq.length ? rq.map(p => `<span class="qchip ${p.role === "hog" ? "hot" : ""}">${p.pid} ${OS.esc(p.name)}</span>`).join("") : '<span class="small muted">Empty — every runnable process gets a core immediately.</span>';
    return `<p class="viz-caption">The scheduler shares ${S.vcpus} vCPUs between runnable processes. When there are more runnable processes than cores, the rest wait in the ready queue.</p>
      <div class="sub-label">On CPU now</div><div class="cores">${cores}</div>
      <div class="sub-label">Ready queue — ${rq.length} waiting</div><div class="queue">${chips}</div>`;
  }
  if (sid === "memory") {
    const ram = S.ram_mb, used = ram - m.mem_avail_mb;
    const hog = byPid[3301], hogMb = hog && hog.state !== "TERMINATED" ? hog.mem_mb : 0;
    const pct = v => Math.max(0, Math.min(100, v / ram * 100));
    const useful = Math.max(0, 100 - m.paging_overhead);
    const on = [m.mem > 85, m.page_faults > 400, m.swap_io > 2, m.response_ms == null || m.response_ms > 250, m.throughput < 600];
    const L = (t, i) => `<span class="link ${on[i] ? "on" : ""}">${t}</span>`, A = '<span class="arr">→</span>';
    return `<p class="viz-caption">When the combined working set exceeds physical RAM, pages are constantly moved to and from swap. The CPU then spends its time servicing page faults instead of useful work — thrashing.</p>
      <div class="hbar-label"><span>Physical RAM (${Math.round(ram / 1024)} GB)</span><span class="mono">${f("mem", m.mem)} used</span></div>
      <div class="hbar"><div style="width:${pct(Math.max(0, used - hogMb))}%;background:#667085">processes</div><div style="width:${pct(hogMb)}%;background:#c03030">${hogMb > ram * 0.09 ? "cache-loader" : ""}</div></div>
      <div class="hbar-label"><span>CPU time: useful work vs paging</span><span class="mono">${useful.toFixed(0)}% useful</span></div>
      <div class="hbar"><div style="width:${useful}%;background:#157f4a">useful work</div><div style="width:${100 - useful}%;background:#c03030">${m.paging_overhead > 14 ? "paging" : ""}</div></div>
      <div class="chain">${L("Memory pressure", 0)}${A}${L("Page faults ↑", 1)}${A}${L("Paging ↑", 2)}${A}${L("Latency ↑", 3)}${A}${L("Throughput ↓", 4)}</div>`;
  }
  if (sid === "disk") {
    const q = Math.round(m.io_queue), cap = S.disk_iops_capacity;
    const chips = Array.from({ length: Math.min(q, 40) }, () => '<span class="qchip io">req</span>').join("") + (q > 40 ? `<span class="qchip io">+${q - 40}</span>` : "");
    const iow = snap.processes.filter(p => p.state === "IO-WAIT");
    return `<p class="viz-caption">Every read and write waits in the block-layer queue until the disk serves it. As demand approaches capacity, the queue grows and processes block in I/O wait — even though the CPU is free.</p>
      <div class="hbar-label"><span>Disk throughput vs capacity (${cap} IOPS)</span><span class="mono">${OS.num("read_iops", m.read_iops)} read + ${OS.num("write_iops", m.write_iops)} write</span></div>
      <div class="hbar"><div style="width:${m.read_iops / cap * 100}%;background:#2f5bd3">read</div><div style="width:${m.write_iops / cap * 100}%;background:#c4561a">write</div></div>
      <div class="sub-label">I/O queue — ${OS.num("io_queue", m.io_queue)} pending</div><div class="queue">${chips || '<span class="small muted">Empty — requests are served immediately.</span>'}</div>
      <div class="sub-label">Processes in I/O wait</div><div class="queue">${iow.length ? iow.map(p => `<span class="qchip io">${p.pid} ${OS.esc(p.name)}</span>`).join("") : '<span class="small muted">None</span>'}</div>`;
  }
  if (sid === "process") {
    const master = snap.processes.find(p => p.role === "master");
    const st = master ? master.state : "RUNNING";
    const S = ["RUNNING", "CRASHING", "TERMINATED", "RESTARTING", "RUNNING"];
    const NAME = { CRASHING: "CRASH REQUESTED" };
    const restarted = st === "RUNNING" && /restarted/.test(snap.restart_status || "");
    const diag = S.map((s, i) => {
      const on = s === st && (s !== "RUNNING" || (i === 0) !== restarted);
      return `<span class="lstate ${s} ${on ? "on" : ""}">${NAME[s] || s}</span>${i < S.length - 1 ? '<span class="muted">→</span>' : ""}`;
    }).join("");
    const workers = snap.processes.filter(p => p.role === "master" || p.role === "worker").map(p =>
      `<div class="wbox ${p.state}"><div class="wn">${OS.esc(p.name)}</div><div class="wp">PID ${p.pid}</div><span class="st ${p.state}">${p.state === "CRASHING" ? "CRASH REQUESTED" : p.state}</span></div>`).join("");
    const resOk = m.cpu < 85 && m.mem < 85 && m.disk < 85;
    const svcUp = m.availability >= 100;
    return `<p class="viz-caption">If the critical web-service process terminates, CPU, memory and disk can look perfectly healthy while the service itself is gone.</p>
      <div class="sub-label">web-service lifecycle</div><div class="lifecycle">${diag}</div>
      <div class="sub-label">Process tree</div><div class="workers">${workers}</div>
      <p class="small" style="margin-top:14px">Restart status: <b>${OS.esc(snap.restart_status)}</b></p>
      <div class="equation">Resource health <span class="${resOk ? "good" : "bad"}">${resOk ? "NORMAL" : "STRESSED"}</span> · critical service <span class="${svcUp ? "good" : "bad"}">${svcUp ? "UP" : m.availability > 0 ? "DEGRADED (" + OS.fmt("availability", m.availability) + ")" : "DOWN"}</span></div>`;
  }
  if (sid === "network") {
    const cls = m.net_latency > 400 || m.packet_loss > 10 ? "bad" : m.net_latency > 150 || m.packet_loss > 2 ? "warn" : "";
    const ok = (c, t) => `<b class="${c ? "good" : "bad"}">${c ? "✓" : "✗"} ${t}</b>`;
    const allRunning = snap.processes.every(p => ["RUNNING", "READY", "SLEEPING"].includes(p.state));
    const internalOk = m.cpu < 85 && m.mem < 85 && allRunning, externalOk = m.reachability >= 95 && m.net_latency <= 150;
    return `<p class="viz-caption">The server is healthy inside, but delay and packet loss on the network path cause retransmissions and client timeouts.</p>
      <div class="net-diagram"><div class="net-node"><div class="nt">Clients</div><div class="ns">${f("req_ok", m.req_ok)} ok · ${f("req_failed", m.req_failed)} failed</div></div>
        <div><div class="wire ${cls}"></div><div class="wl">RTT ${f("net_latency", m.net_latency)} · loss ${f("packet_loss", m.packet_loss)}</div></div>
        <div class="net-node"><div class="nt">${OS.esc(S.id)}</div><div class="ns">eth0 · ${OS.esc(S.ip)}</div></div></div>
      <div class="checklist">
        <div><div class="sub-label">Inside the server</div><div class="ci"><span>CPU</span>${ok(m.cpu < 85, f("cpu", m.cpu))}</div><div class="ci"><span>Memory</span>${ok(m.mem < 85, f("mem", m.mem))}</div><div class="ci"><span>Processes</span>${ok(allRunning, allRunning ? "running" : "problem")}</div></div>
        <div><div class="sub-label">Communication</div><div class="ci"><span>Latency</span>${ok(m.net_latency <= 150, f("net_latency", m.net_latency))}</div><div class="ci"><span>Packet loss</span>${ok(m.packet_loss <= 2, f("packet_loss", m.packet_loss))}</div><div class="ci"><span>Reachability</span>${ok(m.reachability >= 95, f("reachability", m.reachability))}</div></div></div>
      <div class="equation">Internal health <span class="${internalOk ? "good" : "bad"}">${internalOk ? "OK" : "DEGRADED"}</span> + communication <span class="${externalOk ? "good" : "bad"}">${externalOk ? "OK" : "FAILING"}</span> = service <span class="${internalOk && externalOk ? "good" : "bad"}">${internalOk && externalOk ? "AVAILABLE" : "UNAVAILABLE"}</span></div>`;
  }
  return "";
};

/* --------------------------------------------- detection rule table --- */
OS.renderRule = function (sid, det) {
  if (!det || !det.rules) return "";
  const rule = det.rules[sid];
  const lv = ["OK", "WARNING", "DEGRADED", "FAILED"];
  const fv = c => {
    if (typeof c.value === "string") return c.value;
    if (c.value == null) return "—";
    const v = c.value; return (Math.abs(v) >= 100 ? Math.round(v).toLocaleString() : Number(v).toFixed(1)) + (c.unit === "%" ? "%" : c.unit ? " " + c.unit : "");
  };
  const th = (c, k) => (c.th ? `${c.dir === "<" && k !== "W" ? "≤" : c.dir} ${c.th[k]}` : k === "F" ? `= ${c.expect}` : "—");
  const logic = { single: "single condition", AND: "all conditions must hold (AND)", OR: "any condition (OR)" }[rule.logic] || rule.logic;
  return `<p class="small muted" style="margin-top:0">Rule <b>${OS.esc(rule.name)}</b> — ${logic}. The detector never knows what was injected; it only sees metrics and the process/resource tables, and a level must hold for ${OS.meta ? 2 : 2} consecutive samples.</p>
    <div class="tbl-wrap"><table class="data compact"><thead><tr><th>Check</th><th>Observed</th><th>Warning</th><th>Degraded</th><th>Failed</th><th>Level</th></tr></thead><tbody>
    ${rule.checks.map(c => `<tr><td>${OS.esc(c.label)}</td><td class="mono"><b>${OS.esc(fv(c))}</b></td><td class="mono">${th(c, "W")}</td><td class="mono">${th(c, "D")}</td><td class="mono">${th(c, "F")}</td><td>${OS.pill(lv[c.level], lv[c.level])}</td></tr>`).join("")}
    </tbody></table></div>`;
};

/* ---------------------------------------------------------- timeline --- */
/* Builds the Normal → Injected → Detected → Degraded/Failed → Recovery → Normal milestones. */
OS.milestones = function (markers, result, samples) {
  const at = k => { const m = (markers || []).filter(z => z.kind === k); return m.length ? m[m.length - 1].ts : null; };
  const inj = at("inject"), det = at("detect"), rec = at("recovery"), done = at("recovered");
  const worst = result.max_health || "HEALTHY";
  let worstAt = at("failed");
  if (!worstAt && samples && inj) { const s = samples.find(z => z.ts >= inj && z.health === worst); worstAt = s ? s.ts : null; }
  const inter = result.mode === "interactive";
  const peak = OS.intensityText(result);
  return [
    { t: at("baseline"), l: "Normal operation", s: "Baseline captured before the failure", c: "ok" },
    { t: inj, l: inter ? "Condition applied" : "Failure injected", s: inter ? `${result.failure_name} · operator control${peak ? " (peak " + peak + ")" : ""}` : `${result.failure_name} · ${result.severity}`, c: "bad" },
    ...(inter ? [{ t: result.first_breach_at, l: "First threshold crossing", s: "single sample — not yet confirmed", c: "warn" }] : []),
    { t: det, l: "Failure detected", s: det ? `by the ${result.detected} rule · ${OS.secs(result.detection_latency)} after ${inter ? "the condition was applied" : "injection"}${result.confirm_delay != null ? " · confirmed " + OS.secs(result.confirm_delay) + " after the first crossing" : ""}` : "not detected", c: "warn" },
    { t: worstAt, l: worst === "FAILED" ? "Service failed" : "Service " + worst.toLowerCase(), s: `Worst state reached: ${worst}`, c: "bad" },
    { t: rec, l: "Recovery started", s: inter ? "Operator reduced the condition / ran the recovery action" : "Corrective OS action applied", c: "rec" },
    { t: done, l: "Recovery completed", s: done ? (inter ? `All rules OK and condition settled in ${OS.secs(result.recovery_time)}` : `Metrics back to baseline in ${OS.secs(result.recovery_time)}`) : "in progress", c: "ok" },
    { t: done, l: "Normal operation", s: `${result.server_code || (OS.meta && OS.meta.server ? OS.meta.server.id : "Server-01")} HEALTHY again`, c: "ok" },
  ];
};
OS.renderTimeline = function (ms) {
  return `<ol class="vtimeline">${ms.map(m => `<li class="${m.c} ${m.t ? "" : "skip"}"><div class="tt">${m.t ? OS.clock(m.t) : "—"}</div><div class="td"><i></i></div><div><div class="tl">${OS.esc(m.l)}</div><div class="ts">${OS.esc(m.s)}</div></div></li>`).join("")}</ol>`;
};

/* "peak 95%" for interactive experiments, else the severity */
OS.intensityText = function (r) {
  if (!r || r.mode !== "interactive" || r.max_intensity == null) return "";
  const u = r.intensity_unit === "%" ? "%" : " " + (r.intensity_unit || "");
  return Number(r.max_intensity).toLocaleString() + u;
};
OS.severityText = function (r) {
  if (!r) return "—";
  if (r.mode === "interactive" || r.severity === "INTERACTIVE") {
    const p = OS.intensityText(r);
    return "Interactive" + (p ? " · peak " + p : "");
  }
  return r.severity;
};
OS.sourceTag = function (key) {
  const m = OS.meta && OS.meta.metrics[key]; if (!m || !m.source) return "";
  const info = (OS.meta.source_info || {})[m.source] || "";
  return `<span class="src ${m.source}" title="${OS.esc(info)}">${m.source}</span>`;
};

/* ------------------------------------------------- key metrics table --- */
OS.KEY_METRICS = {
  cpu: ["cpu", "cpu_queue", "wait_ms", "response_ms"],
  memory: ["mem", "page_faults", "swap_io", "throughput"],
  disk: ["disk", "io_queue", "disk_latency", "response_ms"],
  process: ["availability", "running_procs", "terminated_procs", "req_failed"],
  deadlock: ["stuck_procs", "waiting_procs", "blocked_procs", "throughput"],
  network: ["net_latency", "packet_loss", "reachability", "req_failed"],
};
OS.renderKeyMetrics = function (sid, baseline, peaks, mins) {
  const rows = OS.KEY_METRICS[sid].map(k => {
    const low = OS.LOWER_IS_WORSE.includes(k);
    const b = baseline ? baseline[k] : null, w = low ? (mins || {})[k] : (peaks || {})[k];
    return `<tr><td>${OS.esc(OS.label(k))}</td><td class="num">${OS.fmt(k, b)}</td><td class="num"><b>${OS.fmt(k, w)}</b></td></tr>`;
  }).join("");
  return `<div class="tbl-wrap"><table class="data"><thead><tr><th>Metric</th><th>Baseline</th><th>Worst during failure</th></tr></thead><tbody>${rows}</tbody></table></div>`;
};
