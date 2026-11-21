/* Root-cause analysis + run reports (Failure Analysis page and run details). Everything shown here
   was recorded by the engine during the episode — nothing is written in advance. */
"use strict";
LAB.ts = (t, t0) => (t == null ? "—" : OS.clock(t) + (t0 != null ? ` <span class="muted small">(+${(t - t0).toFixed(1)} s)</span>` : ""));
LAB.prov = (k, title) => `<span class="prov ${k}" title="${OS.esc(title || LAB.PROV[k] || "")}">${k}</span>`;
LAB.PROV = {
  observed: "Read from the simulated server's metrics or event log at that moment (an observation inside the simulation).",
  derived: "Computed by a formula from other values (see Simulation details).",
  simulated: "A relationship implemented in the simulation model — the model's own contribution terms.",
  inferred: "Attribution: chosen from the model's contribution shares; a diagnostic hypothesis, not a proof.",
  confirmed: "Confirmed inside the simulation by the detector (2 consecutive samples) or by the recovery checks.",
};
LAB.renderRCA = function (ep, opts) {
  opts = opts || {};                        // opts.compact: the Incident page shows fix + verification itself
  const r = ep.rca;
  if (!r) return '<div class="muted">No analysis available.</div>';
  const t = r.timing, t0 = t.trigger_ts || ep.start_ts;
  const esc = OS.esc, li = a => (a || []).map(x => `<li>${esc(x)}</li>`).join("");
  const status = r.metastable ? ["FAILED", "self-sustaining"] : t.recovered_ts ? ["RECOVERED", "recovered"] : ["DEGRADED", "ongoing"];
  const prim = r.primary.name;
  const conc = r.primary.concurrent || [];
  const rule = r.primary.rule;
  const strategies = rule ? (LAB.meta.strategies || []).filter(x => x.rule === rule) : [];
  /* a small tree: the primary cause, then each effect with the reason under it (and its own cause when that
     is another effect rather than the primary) */
  const casc = r.secondary.length ? `<div class="casc"><div class="casc-root"><span class="casc-dot"></span><b>${esc(prim)}</b><span class="casc-tag">primary cause</span></div>
      <ul class="casc-list">${r.secondary.map(s => `<li><div class="casc-n">${esc(s.name)}${s.by && s.by !== prim ? `<span class="casc-by">via ${esc(s.by)}</span>` : ""}</div>
        <div class="casc-why">${esc(s.via)}</div></li>`).join("")}</ul></div>`
    : '<div class="small muted">No secondary failure: the failure stayed inside one subsystem.</div>';
  const det = r.detection ? `<div class="small">${esc(r.detection.rule)} rule — ${esc(r.detection.logic)}.</div>
      <div class="small" style="margin-top:4px">Configured thresholds (simulation rules, not universal limits): ${Object.entries(r.detection.thresholds).map(([k, v]) => `<b>${esc(k)}</b> ${["W", "D", "C", "F"].filter(x => v[x] != null).map(x => x + " " + v[x]).join(" / ")}`).join(" · ")}</div>
      <div class="small muted">${esc(r.detection.confirm)}. The detector reads only observed metrics, the process table and the lock table — never the controls.</div>` : "";
  const order = (r.detections || []).map(d => `<tr><td class="mono">${LAB.ts(d.ts, t0)}</td><td>${esc(d.name)}</td><td>${LAB.pill(d.level === "OK" ? "HEALTHY" : d.level, d.level)}</td></tr>`).join("");
  const verified = t.recovered_ts ? `<ul class="vchecks"><li class="ok"><span class="ck">✓</span><div><b>Every detection rule below WARNING</b><div class="small muted">3 consecutive samples, ${LAB.secs(t.recovery_time)} after recovery began ${LAB.prov("confirmed")}</div></div></li></ul>`
    : rule === "kernel" ? `<div class="small muted">Not recovered yet. The <a href="/kernel">Kernel Failure Lab</a> shows the reboot and its post-boot checks.</div>`
    : `<div class="small muted">Not recovered yet. Open <a href="/incident#fix">Fix &amp; verify</a> to apply a strategy — its checks will appear there.</div>`;
  return `
  <section class="card"><div class="summary-strip">
    <div><div class="k">${conc.length ? "Primary causes (concurrent)" : "Primary cause"}</div><div class="v">${esc(conc.length ? conc.join(" + ") : (prim || "—"))}</div></div>
    <div><div class="k">OS subsystem</div><div class="v small">${esc(conc.length && r.primary.concurrent_subsystems ? r.primary.concurrent_subsystems.join(" + ") : (r.primary.subsystem || "—"))}</div></div>
    <div><div class="k">Worst state</div><div class="v">${LAB.pill(r.peak, r.peak)}</div></div>
    <div><div class="k">Outcome</div><div class="v">${LAB.pill(status[0], status[1])}</div></div></div>
    <div class="timing-strip" style="margin-top:18px;padding-top:16px;border-top:1px solid var(--line)">
    <div><div class="k">Trigger → detected</div><div class="v">${LAB.secs(t.detection_latency)}</div></div>
    <div><div class="k">First crossing → confirmed</div><div class="v">${LAB.secs(t.confirm_delay)}</div></div>
    <div><div class="k">Time in failure</div><div class="v">${LAB.secs(t.failure_duration)}</div></div>
    <div><div class="k">Recovery time</div><div class="v">${LAB.secs(t.recovery_time)}</div></div></div>
    <p class="small muted" style="margin:10px 0 0">All times are simulated-server time in this lab, not production measurements.</p></section>
  <div class="rca-grid" style="margin-top:20px">
    <section class="card rca-story">
      <div class="rca-k">Primary cause ${LAB.prov("inferred")}</div>
      <div class="rca-v"><b>${esc(prim || "—")}</b>${r.primary.via ? ` — ${esc(r.primary.via)}` : ""}</div>
      ${conc.length ? `<div class="small muted"><b>${esc(conc.join(" and "))}</b> are independent causes that reached failure within 2 s of each other (each needs two 0.5 s samples to confirm, and metrics ramp at different speeds), so their order is not meaningful: treat them as causes that happened together, not as one causing the other.</div>`
        : r.primary.others.length ? `<div class="small muted">Independent failures in the same episode: ${esc(r.primary.others.join(", "))}</div>` : ""}
      <div class="rca-k">Trigger ${LAB.prov("observed")}</div><div class="rca-v">${esc(r.trigger.text)}</div>
      ${r.trigger.inputs.length > 1 ? `<div class="small muted">Inputs in the 30 s before: ${esc(r.trigger.inputs.join(" · "))}</div>` : ""}
      <div class="rca-k">Evidence ${LAB.prov("observed")}</div>
      <ul class="clean small">${li(r.symptoms.slice(0, 5))}</ul>
      ${r.symptoms.length > 5 ? `<details class="learn"><summary>${r.symptoms.length - 5} more threshold crossings</summary><ul class="clean small">${li(r.symptoms.slice(5))}</ul></details>` : ""}
      <div class="rca-k">Simulated causal chain ${LAB.prov("simulated")}</div>
      <ol class="trace">${li(r.chain) || "<li>No chain recorded.</li>"}</ol>
      <div class="small muted">Each step follows the model's own contribution terms at the worst moment — only relationships the simulation implements are shown.</div>
      <div class="rca-k">Observed effects ${LAB.prov("observed")}</div><ul class="clean small">${(r.observed || []).map(o => `<li>${esc(o.text)}</li>`).join("") || "<li>—</li>"}</ul>
      ${r.metastable ? `<div class="meta-warn"><b>Self-sustaining failure</b> — after the trigger was reduced, a feedback loop kept the server failed: ${esc(r.metastable.loop.join(" → "))}.</div>` : ""}
    </section>
    <div style="display:grid;gap:20px;align-content:start;min-width:0;grid-template-columns:minmax(0,1fr)">
      ${opts.compact ? "" : `<section class="card"><div class="card-head"><h2>Recovery</h2></div>
        ${r.recovery.mechanism.length ? `<div class="rca-k" style="margin-top:0">What changed ${LAB.prov("observed")}</div><ul class="clean small">${li(r.recovery.mechanism)}</ul>` : ""}
        ${r.recovery.explanation ? `<p class="small">${esc(r.recovery.explanation)}</p>` : ""}
        ${strategies.length ? `<div class="rca-k">Strategies that fit this failure</div><ul class="clean small">${strategies.map(x => `<li><b>${esc(x.name)}</b> — ${esc(x.does)}</li>`).join("")}</ul><a class="btn btn-ghost btn-sm" href="/incident#fix">Next: fix it →</a>` : rule === "kernel" ? '<p class="small">Recovery needs a reboot — see the <a href="/kernel">Kernel Failure Lab</a>.</p>' : ""}
      </section>
      <section class="card"><div class="card-head"><h2>Verification</h2></div>${verified}</section>`}
      <section class="card"><div class="card-head"><h2>Cascade</h2><span class="aside">what the primary cause led to</span></div>${casc}
        ${r.loop ? `<p class="small muted">Feedback loop: ${esc(r.loop.join(" → "))}. The subsystem that failed first is treated as the root.</p>` : ""}</section>
      <details class="card tech"><summary>Technical details · detection order and rules</summary>
        ${r.first_crossing ? `<p class="small">First threshold crossed: <b>${esc(r.first_crossing.text)}</b> at ${LAB.ts(r.first_crossing.ts, t0)}</p>` : ""}
        <div class="tbl-wrap"><table class="data compact"><thead><tr><th>Time</th><th>Detection</th><th>Level</th></tr></thead><tbody>${order || '<tr><td colspan="3" class="muted">none</td></tr>'}</tbody></table></div>
        ${det}</details>
    </div></div>`;
};
