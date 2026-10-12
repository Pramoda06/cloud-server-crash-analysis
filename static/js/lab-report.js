/* Root-cause analysis + run reports (Failure Analysis page and run details). Everything shown here
   was recorded by the engine during the episode — nothing is written in advance. */
"use strict";
LAB.ts = (t, t0) => (t == null ? "—" : OS.clock(t) + (t0 != null ? ` <span class="muted small">(+${(t - t0).toFixed(1)} s)</span>` : ""));
LAB.renderRCA = function (ep) {
  const r = ep.rca;
  if (!r) return '<div class="muted">No analysis available.</div>';
  const t = r.timing, t0 = t.trigger_ts || ep.start_ts;
  const li = a => (a || []).map(x => `<li>${OS.esc(x)}</li>`).join("");
  const status = r.metastable ? ["FAILED", "self-sustaining"] : t.recovered_ts ? ["RECOVERED", "recovered"] : ["DEGRADED", "ongoing"];
  const prim = r.primary.name;
  const casc = r.secondary.length ? `<div class="cascade-flow"><span class="cf p">${OS.esc(prim)}</span>${r.secondary.map(s => `<span class="arr">→</span><span class="cf">${OS.esc(s.name)}</span><span class="cv">← ${OS.esc(s.by)}: ${OS.esc(s.via)}</span>`).join("")}</div>`
    : '<div class="small muted">No secondary failure: the failure stayed inside one subsystem.</div>';
  const det = r.detection ? `<div class="rca-v">${OS.esc(r.detection.rule)} rule — ${OS.esc(r.detection.logic)}.</div>
      <div class="small" style="margin-top:4px">Thresholds: ${Object.entries(r.detection.thresholds).map(([k, v]) => `<b>${OS.esc(k)}</b> ${["W", "D", "C", "F"].filter(x => v[x] != null).map(x => x + " " + v[x]).join(" / ")}`).join(" · ")}</div>
      <div class="small muted">Confirmation: ${OS.esc(r.detection.confirm)}. The detector never sees the controls — only the observed metrics, process and lock tables.</div>` : "—";
  const order = (r.detections || []).map(d => `<tr><td class="mono">${LAB.ts(d.ts, t0)}</td><td>${OS.esc(d.name)}</td><td>${LAB.pill(d.level === "OK" ? "HEALTHY" : d.level, d.level)}</td></tr>`).join("");
  return `
  <section class="card"><div class="summary-strip">
    <div><div class="k">Primary failure</div><div class="v">${OS.esc(prim || "—")}</div></div>
    <div><div class="k">OS subsystem</div><div class="v small">${OS.esc(r.primary.subsystem || "—")}</div></div>
    <div><div class="k">Worst state</div><div class="v">${LAB.pill(r.peak, r.peak)}</div></div>
    <div><div class="k">Outcome</div><div class="v">${LAB.pill(status[0], status[1])}</div></div></div></section>
  <section class="card" style="margin-top:20px"><div class="timing-strip">
    <div><div class="k">Detection latency (trigger → detected)</div><div class="v">${LAB.secs(t.detection_latency)}</div></div>
    <div><div class="k">Confirmation delay (first crossing → confirmed)</div><div class="v">${LAB.secs(t.confirm_delay)}</div></div>
    <div><div class="k">Time in failure</div><div class="v">${LAB.secs(t.failure_duration)}</div></div>
    <div><div class="k">Recovery time</div><div class="v">${LAB.secs(t.recovery_time)}</div></div></div></section>
  <div class="rca-grid" style="margin-top:20px">
    <section class="card"><div class="card-head"><h2>Why did the server fail?</h2></div>
      <div class="rca-k">Primary cause</div><div class="rca-v"><b>${OS.esc(prim || "—")}</b> — ${OS.esc(r.primary.via || "")}</div>
      ${r.primary.others.length ? `<div class="small muted">Independent failures in the same episode: ${OS.esc(r.primary.others.join(", "))}</div>` : ""}
      <div class="rca-k">Trigger</div><div class="rca-v">${OS.esc(r.trigger.text)}</div>
      ${r.trigger.inputs.length > 1 ? `<div class="small muted">Inputs in the 30 s before: ${OS.esc(r.trigger.inputs.join(" · "))}</div>` : ""}
      <div class="rca-k">Causal chain (traced through the simulation, at the worst moment)</div>
      <ol class="trace">${li(r.chain)}</ol>
      <div class="rca-k">Observed effects</div><ul class="clean">${(r.observed || []).map(o => `<li>${OS.esc(o.text)}</li>`).join("")}</ul>
      <div class="rca-k">Metrics that crossed thresholds</div><ul class="clean">${li(r.symptoms)}</ul>
      ${r.metastable ? `<div class="meta-warn"><b>Self-sustaining failure detected</b> — after the trigger was reduced, a feedback loop kept the server failed: ${OS.esc(r.metastable.loop.join(" → "))}.</div>` : ""}
    </section>
    <div style="display:grid;gap:20px;align-content:start">
      <section class="card"><div class="card-head"><h2>Cascade — primary → secondary</h2></div>${casc}
        ${r.loop ? `<p class="small muted">Feedback loop found: ${OS.esc(r.loop.join(" → "))}. The subsystem that failed first is treated as the root.</p>` : ""}</section>
      <section class="card"><div class="card-head"><h2>Which subsystem failed first?</h2></div>
        ${r.first_crossing ? `<p class="small" style="margin-top:0">First threshold crossed: <b>${OS.esc(r.first_crossing.text)}</b> at ${LAB.ts(r.first_crossing.ts, t0)}</p>` : ""}
        <div class="tbl-wrap"><table class="data compact"><thead><tr><th>Time</th><th>Detection</th><th>Level</th></tr></thead><tbody>${order || '<tr><td colspan="3" class="muted">none</td></tr>'}</tbody></table></div></section>
      <section class="card"><div class="card-head"><h2>Detection mechanism</h2></div>${det}</section>
      <section class="card"><div class="card-head"><h2>Recovery mechanism</h2></div>
        ${r.recovery.mechanism.length ? `<ul class="clean">${li(r.recovery.mechanism)}</ul>` : '<div class="small muted">No recovery action recorded yet.</div>'}
        <p class="small" style="margin-bottom:0">${OS.esc(r.recovery.explanation)}${r.recovery.completed ? "" : " <b>Not recovered yet.</b>"}</p></section>
    </div></div>`;
};
