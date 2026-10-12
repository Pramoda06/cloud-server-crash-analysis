/* Live Monitoring: polls the engine every 0.5 s and shows only what matters now. */
"use strict";
(function () {
  const $ = OS.$;
  const TILES = {
    cpu: ["cpu", "cpu_queue", "wait_ms"],
    memory: ["mem", "page_faults", "swap_io"],
    disk: ["disk", "disk_latency", "io_queue"],
    process: ["web_state", "availability", "req_failed"],
    deadlock: ["deadlock", "waiting_procs", "blocked_procs"],
    network: ["net_latency", "packet_loss", "req_failed"],
  };
  const ALL = ["cpu", "mem", "disk", "net_latency", "response_ms", "throughput", "proc_count", "cpu_queue", "wait_ms",
               "ctx_switches", "page_faults", "swap_io", "mem_avail_mb", "io_queue", "disk_latency", "iowait",
               "availability", "terminated_procs", "blocked_procs", "locks_held", "packet_loss", "req_failed", "timeouts", "reachability"];
  const PHASES = ["Baseline", "Injected", "Detected", "Degraded / Failed", "Recovering", "Recovered"];
  const PHASE_MARK = ["baseline", "inject", "detect", "failed", "recovery", "recovered"];
  const STAGE_TO_PHASE = { 1: 0, 2: 1, 3: 1, 4: 2, 5: 3, 6: 4, 7: 5 };

  let st = null, samples = [], events = [], seq = 0, eseq = 0;
  let tab = "failure", charts = null, chartKey = "", busy = false, abortArmed = false;

  async function init() {
    await OS.loadMeta();
    $("tabs").innerHTML = OS.CHART_TABS.map(([id, name]) => `<button type="button" data-tab="${id}" class="${id === tab ? "sel" : ""}">${name}</button>`).join("");
    $("tabs").addEventListener("click", e => {
      const b = e.target.closest("button"); if (!b) return;
      tab = b.dataset.tab;
      document.querySelectorAll("#tabs button").forEach(x => x.classList.toggle("sel", x === b));
      buildCharts(); updateCharts();
    });
    $("legend").innerHTML = OS.chartLegend;
    $("btnRecover").addEventListener("click", () => act("/api/recover"));
    $("btnAbort").addEventListener("click", () => {
      if (!abortArmed) { abortArmed = true; $("btnAbort").textContent = "Click again to abort (not saved)"; setTimeout(() => { abortArmed = false; $("btnAbort").textContent = "Abort experiment"; }, 4000); return; }
      abortArmed = false; $("btnAbort").textContent = "Abort experiment"; act("/api/reset");
    });
    $("chkAuto").addEventListener("change", e => OS.api("/api/settings", { method: "POST", body: JSON.stringify({ auto_recover: e.target.checked }) }));
    ["moreMetrics", "moreRule", "moreProcs", "moreLog"].forEach(id => $(id).addEventListener("toggle", renderMore));
    poll();
  }

  async function act(url) {
    if (busy) return; busy = true;
    try { await OS.api(url, { method: "POST" }); } catch (e) { $("statusMsg").textContent = e.message; }
    busy = false; poll();
  }

  let timer = null;
  async function poll() {
    clearTimeout(timer);
    try { apply(await OS.api(`/api/state?since=${seq}&esince=${eseq}`)); }
    catch (e) { $("statusMsg").textContent = "Connection to the simulator lost — is app.py still running? Retrying…"; }
    timer = setTimeout(poll, 500);
  }

  function apply(s) {
    // the selected server was changed in another tab: reload so samples never mix between servers
    if (OS.meta.server && s.server && s.server.db_id !== OS.meta.server.db_id) { location.reload(); return; }
    if (s.reset) { samples = []; events = []; }
    st = s; seq = s.seq; eseq = s.eseq;
    samples = samples.concat(s.samples).filter(x => x.ts > s.now - 400);
    events = events.concat(s.events).slice(-400);
    const ex = s.experiment;
    OS.setTopHealth(s.health, ex && ex.active ? ex.scenario_name : null);
    $("idleView").hidden = !!ex;
    $("liveView").hidden = !ex;
    if (!ex) { $("idleStatus").className = "big-status s-" + s.health; $("idleStatus").querySelector(".bw").textContent = s.health; return; }
    renderStatus(ex); renderInfo(ex); renderTiles(ex); renderViz(ex);
    if (chartKey !== ex.scenario + tab) buildCharts();
    updateCharts(); renderMore();
  }

  /* ------------------------------------------------------------ status */
  function renderStatus(ex) {
    const h = st.health, r = ex.result, done = !ex.active;
    $("bigStatus").className = "big-status s-" + h;
    $("bigStatus").querySelector(".bw").textContent = h;
    let msg = "";
    if (st.phase === "BASELINE") msg = `Recording a healthy baseline — ${ex.scenario_name} will be injected in ${ex.baseline_remaining.toFixed(1)} s.`;
    else if (st.phase === "RUNNING" && !r.detected_at) msg = `${ex.scenario_name} injected. Resources are changing; the monitor has not confirmed a threshold breach yet.`;
    else if (st.phase === "RUNNING") msg = `Failure detected: ${r.detected} (${OS.secs(r.detection_latency)} after injection). ` + (st.auto_recover ? "Automatic recovery starts shortly." : "Click Recover Server when you are ready.");
    else if (st.phase === "RECOVERING") {
      const last = events.filter(e => e.kind === "RECOVERY").pop();
      msg = "Recovery in progress" + (last ? " — " + last.msg : "") + ".";
    } else if (done) msg = r.recovery_time != null ? `Recovered in ${OS.secs(r.recovery_time)}. All metrics are back within baseline tolerance.` : "Experiment ended.";
    $("statusMsg").textContent = msg;

    // health transitions seen during this experiment
    const trail = ["HEALTHY"];
    events.filter(e => e.kind === "STATE" && e.ts >= ex.baseline_started_at - 0.5).forEach(e => {
      const m = /→ (\w+)$/.exec(e.msg); if (m && trail[trail.length - 1] !== m[1]) trail.push(m[1]);
    });
    $("trail").innerHTML = trail.length > 1 ? trail.map(t => OS.pill(t, t)).join('<span class="arr">→</span>') : "";

    // lifecycle phases
    const idx = st.stage === 0 && done ? 5 : (STAGE_TO_PHASE[st.stage] ?? -1);
    const mk = k => { const m = ex.markers.filter(z => z.kind === k); return m.length ? m[m.length - 1].ts : null; };
    $("phases").innerHTML = PHASES.map((p, i) => {
      let c = i < idx ? "done" : i === idx ? "now" : "";
      if (i === idx) c += i === 1 || i === 2 || i === 3 ? (h === "HEALTHY" ? "" : " bad") : i === 4 ? " rec" : i === 5 ? " ok" : "";
      const t = mk(PHASE_MARK[i]);
      return `<div class="phase ${c}"><i></i>${p}<span class="pt">${t && i <= idx ? OS.clock(t) : "&nbsp;"}</span></div>`;
    }).join("");

    $("btnRecover").disabled = st.phase !== "RUNNING" || busy;
    $("btnRecover").hidden = done;
    $("btnAbort").hidden = st.phase === "IDLE";
    $("chkAuto").checked = st.auto_recover;
    $("chkAuto").parentElement.hidden = done;
    $("doneBanner").hidden = !done;
    if (done) {
      $("doneText").innerHTML = r.id ? `Experiment <b>#${r.id}</b> recovered and saved to history.` : "Experiment finished.";
      $("doneLink").href = r.id ? `/analysis?id=${r.id}` : "/analysis";
    }
  }

  function renderInfo(ex) {
    const r = ex.result;
    $("iFailure").textContent = ex.scenario_name;
    $("iSeverity").textContent = ex.severity;
    $("iId").textContent = ex.id ? "#" + ex.id : "—";
    $("iInj").textContent = r.injected_at ? OS.clock(r.injected_at) : (st.phase === "BASELINE" ? "in " + ex.baseline_remaining.toFixed(0) + " s" : "—");
    $("iDet").innerHTML = r.detected_at ? `${OS.clock(r.detected_at)} <span class="small muted">+${r.detection_latency.toFixed(1)} s</span>` : "—";
    const names = { BASELINE: "Baseline monitoring", RUNNING: r.detected_at ? "Failure detected" : "Failure injected", RECOVERING: "Recovering", IDLE: "Completed" };
    $("iPhase").textContent = ex.active ? names[st.phase] : "Completed";
  }

  /* ------------------------------------------------------------- tiles */
  function renderTiles(ex) {
    const m = st.current.metrics, base = ex.baseline || {};
    $("tilesAside").textContent = ex.scenario_name;
    $("tiles").innerHTML = TILES[ex.scenario].map(k => {
      const v = m[k], lvl = OS.levelOf(k, v), text = typeof v === "string" || v == null;
      const u = OS.unit(k);
      const val = v == null ? (k === "response_ms" ? "NO RESPONSE" : "—") : typeof v === "string" ? OS.esc(v) : OS.num(k, v) + `<span class="u">${OS.esc(u)}</span>`;
      const b = typeof base[k] === "number" ? "baseline " + OS.fmt(k, base[k]) : k === "deadlock" ? "wait-for graph cycle check" : k === "web_state" ? "critical process state" : "&nbsp;";
      return `<div class="tile l${lvl}"><div class="tk">${OS.esc(OS.label(k))}</div><div class="tv ${text ? "text" : ""}">${val}</div><div class="tb">${b}</div></div>`;
    }).join("");
  }

  function renderViz(ex) {
    const sc = OS.scenarioById[ex.scenario];
    $("vizTitle").textContent = ex.scenario === "deadlock" ? "Deadlock — resource allocation" : sc.subsystem;
    $("vizAside").textContent = ex.scenario === "deadlock" ? sc.subsystem : "";
    $("viz").innerHTML = OS.renderSubsystem(ex.scenario, st.current, st.detector, { cleared: !ex.active || st.phase === "RECOVERING" });
  }

  /* ------------------------------------------------------------ charts */
  function chartKeys(ex) {
    if (tab === "failure") return OS.scenarioById[ex.scenario].live_charts;
    return OS.CHART_TABS.find(t => t[0] === tab)[2];
  }
  function buildCharts() {
    const ex = st && st.experiment; if (!ex) return;
    OS.destroyCharts(charts);
    charts = OS.buildCharts($("charts"), chartKeys(ex));
    chartKey = ex.scenario + tab;
  }
  function updateCharts() {
    const ex = st && st.experiment; if (!charts || !ex) return;
    const done = ex.markers.find(m => m.kind === "recovered");
    const xmax = done ? Math.min(st.now, done.ts + 12) : st.now;
    const xmin = Math.max(ex.baseline_started_at - 4, xmax - 240);
    const vis = samples.filter(s => s.ts >= xmin - 1 && s.ts <= xmax + 1);
    const last = vis[vis.length - 1];
    charts.forEach(c => { OS.fillChart(c.chart, vis, xmin, xmax, ex.markers); c.now.textContent = last ? OS.fmt(c.key, last[c.key]) : ""; });
    const note = $("chartNote");
    note.hidden = vis.length > 4;
    if (!note.hidden) note.innerHTML = ex.result.id ? `Live samples for this experiment are no longer in memory — see the full charts in <a href="/experiment/${ex.result.id}">Experiment #${ex.result.id}</a>.` : "Waiting for samples…";
  }

  /* --------------------------------------------- collapsible sections */
  function renderMore() {
    if (!st || !st.experiment) return;
    const ex = st.experiment, m = st.current.metrics, base = ex.baseline || {};
    $("logCount").textContent = events.length + " events";
    if ($("moreMetrics").open) {
      const h = st.host || {};
      $("allMetrics").innerHTML = `<div class="tbl-wrap"><table class="data compact"><thead><tr><th>${OS.esc(OS.meta.server.id)} metric</th><th>Now</th><th>Baseline</th></tr></thead><tbody>
        ${ALL.map(k => `<tr><td>${OS.esc(OS.label(k))}</td><td class="num">${OS.fmt(k, m[k])}</td><td class="num muted">${typeof base[k] === "number" ? OS.fmt(k, base[k]) : "—"}</td></tr>`).join("")}
        </tbody></table></div>
        <p class="small muted" style="margin:14px 0 6px">Host computer (real, read-only via psutil): CPU ${h.cpu ?? "n/a"}% · memory ${h.mem ?? "n/a"}% · ${h.procs ?? "n/a"} processes · disk ${h.disk_mb_s ?? "n/a"} MB/s · network ${h.net_kb_s ?? "n/a"} KB/s. Data source now: ${OS.esc(st.data_mode)}.</p>`;
    }
    if ($("moreRule").open) $("rule").innerHTML = OS.renderRule(ex.scenario, st.detector);
    if ($("moreProcs").open) $("procs").innerHTML = OS.renderProcTable(st.current.snapshot);
    if ($("moreLog").open) {
      const mine = events.filter(e => e.ts >= ex.baseline_started_at - 0.5);
      const html = mine.map(OS.renderEvent).join("");
      if ($("log").dataset.n !== String(mine.length)) { $("log").innerHTML = html; $("log").dataset.n = mine.length; }
    }
  }

  document.addEventListener("DOMContentLoaded", init);
})();
