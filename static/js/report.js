/* Renders a completed experiment: used by Failure Analysis (mode "analysis")
   and Experiment Details (mode "details", adds charts, OS state and logs). */
"use strict";

OS.loadExperiment = async function (id) {
  const e = await OS.api("/api/experiments/" + id);
  const d = e.details || {};
  return {
    id: e.id, timestamp: e.timestamp, scenario: e.failure_type, severity: e.severity,
    result: d.result || {}, why: d.why || {}, markers: d.markers || [], samples: d.samples || [],
    baseline: d.baseline || {}, peaks: d.peaks || {}, mins: d.mins || {},
    snapshot: d.failure_snapshot, events: d.events || [], host: d.host_baseline || {},
    server: d.server || null, mode: d.mode || e.mode || "preset",
  };
};

OS.renderReport = function (root, R, mode) {
  const sc = OS.scenarioById[R.scenario];
  const r = R.result, w = R.why;
  const li = a => (a || []).map(x => `<li>${OS.esc(x)}</li>`).join("");
  const status = r.final_status === "RECOVERED" ? OS.pill("RECOVERED", "Recovered") : OS.pill("NEUTRAL", r.final_status || "—");
  const inter = R.mode === "interactive";
  const notes = (w.notes || []).slice(0, 2).map(n => `<div class="note">${OS.esc(n)}</div>`).join("");
  const matchTxt = w.detected ? (w.match ? `correctly classified as ${w.detected}` : `classified as ${w.detected} (injected: ${w.injected})`) : "not detected";

  const summary = `<section class="card"><div class="card-head"><h2>Failure summary</h2><span class="aside">${R.server ? OS.esc(OS.serverLabel(R.server)) + " · " : (r.server_code ? OS.esc(r.server_code) + " · " : "")}${OS.esc(R.timestamp || "")}</span></div>
    <div class="summary-strip">
      <div><div class="k">Failure</div><div class="v">${OS.esc(r.failure_name || sc.name)}</div></div>
      <div><div class="k">${inter ? "Mode" : "Severity"}</div><div class="v">${OS.esc(inter ? OS.severityText(r) : R.severity)}</div></div>
      <div><div class="k">Status</div><div class="v">${status}</div><div class="small muted" style="margin-top:4px">worst state: ${OS.esc(r.max_health || "—")}</div></div>
      <div><div class="k">Detected in · Recovered in</div><div class="v mono">${OS.secs(r.detection_latency)} · ${OS.secs(r.recovery_time)}</div></div>
    </div></section>`;

  const why = `<section class="card"><div class="card-head"><h2>Why did the system fail?</h2></div>
    <div class="label" style="margin-bottom:6px">Root cause</div>
    <div class="why-root">${OS.esc(w.root_cause || sc.root_cause)}</div>
    ${w.explanation ? `<div class="why-expl">${OS.esc(w.explanation)}</div>` : ""}
    <div class="why-sub">Affected OS subsystem: ${OS.esc(w.subsystem || sc.subsystem)}</div>
    <div class="label" style="margin:22px 0 6px">Observed symptoms</div>
    <ul class="clean">${li(w.symptoms)}</ul>
    ${notes}
    ${(w.causal_chain || []).length ? `<div class="label" style="margin:22px 0 6px">Causal chain — control → OS mechanism → metrics → detection</div>
      <div class="chain" style="margin-top:6px">${w.causal_chain.map(t => `<span class="link on">${OS.esc(t)}</span>`).join('<span class="arr">→</span>')}</div>` : ""}
    ${(w.control_trace || []).length ? `<div class="label" style="margin:22px 0 6px">How the operator's control relates to detection and recovery</div><ol class="trace">${li(w.control_trace)}</ol>` : ""}
    </section>`;

  const detrec = `<div class="grid-2" style="margin-top:20px">
    <section class="card"><div class="card-head"><h2>Detection</h2></div>
      <p>${OS.esc(w.detection || "—")}</p>
      <p class="small muted">The monitor was not told what was injected — it ${OS.esc(matchTxt)}.</p>
      ${r.first_breach_at && r.detected_at ? `<p class="small">First threshold crossing ${OS.clock(r.first_breach_at)} → confirmed ${OS.clock(r.detected_at)} (confirmation delay ${OS.secs(r.confirm_delay)}: a level must hold for consecutive samples).</p>` : ""}
      ${w.evidence && w.evidence.length ? `<div class="label" style="margin-top:12px">Evidence at detection</div><div class="evidence">${w.evidence.map(x => `<div>${OS.esc(x)}</div>`).join("")}</div>` : ""}
    </section>
    <section class="card"><div class="card-head"><h2>Recovery</h2></div>
      <p>${OS.esc(w.recovery || "—")}</p>
      ${w.prevention ? `<div class="label" style="margin-top:12px">Prevention</div><p class="muted" style="margin-top:4px">${OS.esc(w.prevention)}</p>` : ""}
    </section></div>`;

  const tlKm = `<div class="grid-2" style="margin-top:20px">
    <section class="card"><div class="card-head"><h2>Timeline</h2></div>${OS.renderTimeline(OS.milestones(R.markers, r, R.samples))}</section>
    <section class="card"><div class="card-head"><h2>Key metrics</h2><span class="aside">for this failure</span></div>${OS.renderKeyMetrics(R.scenario, R.baseline, R.peaks, R.mins)}</section></div>`;

  let osState = "";
  if (R.snapshot && (R.scenario === "deadlock" || mode === "details")) {
    const title = R.scenario === "deadlock" ? "Deadlock at its worst point" : "OS state at the worst point";
    osState = `<section class="card" style="margin-top:20px"><div class="card-head"><h2>${title}</h2><span class="aside">${OS.esc(sc.subsystem)}</span></div>
      ${OS.renderSubsystem(R.scenario, R.snapshot, { deadlock: R.snapshot.deadlock }, { server: R.server })}</section>`;
  }

  let chartsHtml = "", logs = "";
  if (mode === "details") {
    chartsHtml = `<section class="card" style="margin-top:20px"><div class="tabs" id="rTabs"></div><div class="chart-grid" id="rCharts"></div>${OS.chartLegend}</section>`;
    logs = `<div style="margin-top:20px">
      <details class="more"><summary>Event log<span class="aside">${R.events.length} events</span></summary><div class="body"><div class="events">${R.events.map(OS.renderEvent).join("")}</div></div></details>
      ${R.snapshot ? `<details class="more"><summary>Process table at the worst point</summary><div class="body">${OS.renderProcTable(R.snapshot.snapshot)}</div></details>` : ""}</div>`;
  }

  root.innerHTML = mode === "details"
    ? summary + tlKm + chartsHtml + `<div style="margin-top:20px">${why}</div>` + detrec + osState + logs
    : summary + `<div style="margin-top:20px">${why}</div>` + detrec + tlKm + osState;

  if (mode === "details") {
    let tab = "failure", list = null;
    const s = R.samples, xmin = s.length ? s[0].ts - 1 : 0, xmax = s.length ? s[s.length - 1].ts + 1 : 1;
    const tabs = document.getElementById("rTabs");
    tabs.innerHTML = OS.CHART_TABS.map(([id, n]) => `<button type="button" data-tab="${id}" class="${id === tab ? "sel" : ""}">${n}</button>`).join("");
    const draw = () => {
      OS.destroyCharts(list);
      const keys = tab === "failure" ? (inter ? sc.interactive.charts : sc.live_charts) : OS.CHART_TABS.find(t => t[0] === tab)[2];
      list = OS.buildCharts(document.getElementById("rCharts"), keys, R.scenario, { labels: true, interactive: inter });
      list.forEach(c => {
        OS.fillChart(c.chart, s, xmin, xmax, R.markers);
        if (c.key === "control") { const p = OS.intensityText(r); c.now.textContent = p ? "peak " + p : ""; return; }
        if (c.key === "iops") { c.now.textContent = R.peaks.write_iops != null ? "peak " + OS.num("write_iops", R.peaks.write_iops) + " w" : ""; return; }
        const low = OS.LOWER_IS_WORSE.includes(c.key), v = low ? R.mins[c.key] : R.peaks[c.key];
        c.now.textContent = v != null ? (low ? "lowest " : "peak ") + OS.fmt(c.key, v) : "";
      });
    };
    tabs.addEventListener("click", e => {
      const b = e.target.closest("button"); if (!b) return;
      tab = b.dataset.tab; tabs.querySelectorAll("button").forEach(x => x.classList.toggle("sel", x === b)); draw();
    });
    draw();
  }
};
