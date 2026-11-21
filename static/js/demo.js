/* Guided demo — scripted, page-to-page stories for presenting the lab.
   Every step waits for the REAL simulator state (detector levels, recovery verdicts, kernel stage);
   nothing is faked or timed. Progress is kept in localStorage so the story survives page changes. */
(function () {
  const KEY = "os.demo";
  const post = (url, body) => OS.api(url, { method: "POST", body: JSON.stringify(body || {}) });
  const clean = () => post("/api/lab/clean");
  const strategy = id => post("/api/lab/strategy", { id });
  const conf = (st, r) => ((st.rules || {})[r] || {}).confirmed || 0;
  const verdict = st => (st.last_recovery || {}).status;
  const failing = st => ["CRITICAL", "FAILED"].includes(st.health);

  const healthy = (extra) => ({
    page: "/", title: "A healthy server", focus: "#dashHero", enter: clean,
    body: "This is a simulated cloud server: 4 vCPUs, 16 GB RAM, a web service and a database. Nothing on this "
      + "laptop is stressed — every number comes from an operating-system model, sampled every 0.5 s." + (extra || ""),
  });
  const diagnose = (what) => ({
    page: "/incident", title: "Diagnose", focus: "#report",
    body: `The analysis names the primary cause — <b>${what}</b> — inferred from the model's own contribution terms, `
      + "the evidence that crossed thresholds, and the causal chain. Consequences are listed as secondary, not as causes.",
  });

  const SCRIPTS = {
    cpu: {
      name: "CPU overload", time: "≈ 2 min", desc: "Detection, diagnosis, a fix that fails and one that works",
      steps: [
        healthy(),
        { page: "/", title: "Break it: CPU overload", focus: "#dashHero", enter: () => post("/api/lab/stress", { kind: "cpu", intensity: 90 }),
          body: "Batch CPU demand jumps to about 105 % of the vCPUs. The detector only sees metrics — it never knows what we did.",
          wait: { test: failing, text: "Waiting for the detector to confirm (2 consecutive samples)…", done: "Detected: CPU saturation" } },
        { page: "/lab", title: "Watch it spread", focus: "#hStrip",
          body: "Utilization cannot pass 100 %, so the excess waits in the <b>run queue</b> — and every web request now waits "
            + "for a core: response time and errors climb. Click any value to see <i>why</i> it changed." },
        diagnose("CPU saturation"),
        { page: "/incident", title: "A tempting fix that fails", focus: "#result", enter: () => strategy("cpu_renice"),
          body: "We raise the web service's priority (nice −10). The scheduler favours it — but the amount of work is unchanged.",
          wait: { test: st => ["FAILED", "PARTIAL"].includes(verdict(st)), text: "Verifying for up to 30 s of simulated time…",
                  done: "FAILED — renicing changes who waits, not how much work there is", bad: true } },
        { page: "/incident", title: "The real fix", focus: "#result", enter: () => strategy("cpu_reduce"),
          body: "Now we remove the cause: the batch workload goes back to normal and the run queue drains.",
          wait: { test: st => verdict(st) === "RECOVERED", text: "Verifying…", done: "RECOVERED — every rule below WARNING for 3 samples" } },
        { page: "/incident", title: "What you just saw", end: true,
          body: "Break → <b>detect</b> from metrics alone → <b>diagnose</b> the primary cause → try a fix → <b>verify</b> it with the "
            + "same detector. A fix only counts when the measurements say so." },
      ] },
    leak: {
      name: "Memory leak", time: "≈ 2½ min", desc: "A process that never frees memory, and why a restart is only a workaround",
      steps: [
        healthy(),
        { page: "/", title: "Start a leaking process", focus: "#dashHero", enter: () => post("/api/lab/action", { action: "create", kind: "leak" }),
          body: "A new process allocates 120 MB every second and never frees it. Nothing else changes.",
          wait: { test: st => conf(st, "memory") >= 1, text: "Memory is filling — waiting for it to cross the warning threshold (≈ 1 min)…",
                  done: "Detected: memory exhaustion" } },
        { page: "/processes", title: "Find the culprit", focus: "#ptable",
          body: "In the process table, <b>mem-leaker</b> keeps growing. The OS does not know it is a bug: from its point of view "
            + "the process simply asked for memory." },
        diagnose("memory exhaustion"),
        { page: "/incident", title: "Restart the leaking process", focus: "#result", enter: () => strategy("mem_restart_leak"),
          body: "Killing and restarting it gives the process a fresh, empty heap.",
          wait: { test: st => verdict(st) === "RECOVERED", text: "Verifying…", done: "RECOVERED" } },
        { page: "/incident", title: "What you just saw", end: true,
          body: "The server is healthy again, but read the side effects: the bug is not fixed, so memory grows again at the same "
            + "rate. A restart is a workaround; the fix is in the code." },
      ] },
    diskfull: {
      name: "Disk full", time: "≈ 2 min", desc: "Capacity is not speed, and why deleting an open file frees nothing",
      steps: [
        healthy(),
        { page: "/", title: "Fill the disk", focus: "#dashHero", enter: () => post("/api/lab/stress", { kind: "diskfull", intensity: 100 }),
          body: "A runaway log writer appends to the file system at 2 % of the disk per second (accelerated so it fits in a demo).",
          wait: { test: st => conf(st, "disk_space") >= 4, text: "Filling the file system…", done: "Disk full: new writes fail with ENOSPC" } },
        { page: "/lab", title: "Full, but not slow", focus: "#hStrip",
          body: "Disk space is at 100 % and about 39 % of requests fail — the ones that must write — yet disk I/O stays fast and "
            + "response time normal. <b>A full disk fails fast; a slow disk makes everyone wait.</b>" },
        { page: "/incident", title: "Delete the logs (writer still running)", focus: "#result", enter: () => strategy("space_delete_logs"),
          body: "We rm the log files while the writer keeps running.",
          wait: { test: st => ["FAILED", "PARTIAL"].includes(verdict(st)), text: "Verifying for up to 30 s…",
                  done: "FAILED — the file is still open, so Linux keeps its blocks until it is closed", bad: true } },
        { page: "/incident", title: "Stop the writer, then delete", focus: "#result", enter: () => strategy("space_cleanup"),
          body: "Terminating the writer closes its file; now deleting it returns the space.",
          wait: { test: st => verdict(st) === "RECOVERED", text: "Verifying…", done: "RECOVERED — 62 % used again" } },
        { page: "/incident", title: "What you just saw", end: true,
          body: "Same disk, two different failures: <b>capacity</b> (writes refused at once) versus <b>speed</b> (everything queues). "
            + "And a real Linux detail: an unlinked file keeps its space while any process has it open." },
      ] },
    deadlock: {
      name: "Deadlock", time: "≈ 1 min", desc: "Circular wait, the detection algorithm and victim recovery",
      steps: [
        { page: "/deadlock", title: "The Deadlock Lab", focus: "#rag", enter: clean,
          body: "Processes and resources as the OS sees them. The resource-allocation graph is drawn from the live lock table." },
        { page: "/deadlock", title: "Create a circular wait", focus: "#rag", enter: () => post("/api/lab/action", { action: "dl_example", which: "two" }),
          body: "P1 takes A and asks for B; P2 takes B and asks for A. Each holds what the other needs.",
          wait: { test: st => conf(st, "deadlock") >= 3, text: "Cycle found — the detector escalates if it lasts 3 s…", done: "Deadlock confirmed (CRITICAL)" } },
        { page: "/deadlock", title: "Run the detection algorithm", focus: "#detect", enter: () => post("/api/lab/action", { action: "dl_detect" }),
          body: "Work / Finish vectors: no process can get what it needs, so none can finish — both are deadlocked. "
            + "All four Coffman conditions hold: mutual exclusion, hold-and-wait, no pre-emption, circular wait." },
        { page: "/deadlock", title: "Recover: terminate a victim", focus: "#rag", enter: () => strategy("dl_kill"),
          body: "The cheapest transaction is aborted; its lock goes to the waiter.",
          wait: { test: st => verdict(st) === "RECOVERED", text: "Verifying…", done: "RECOVERED" } },
        { page: "/deadlock", title: "What you just saw", end: true,
          body: "Detection and recovery. The same lab also shows <b>prevention</b> (lock ordering makes the cycle impossible) "
            + "and <b>avoidance</b> (Banker's algorithm refuses unsafe requests)." },
      ] },
    kernel: {
      name: "Kernel panic", time: "≈ 1½ min", desc: "A simulated panic, the reboot, and a real bug of the same kind",
      steps: [
        { page: "/kernel", title: "The Kernel Failure Lab", focus: "#scen", enter: clean,
          body: "A state machine of what an operator sees during a kernel panic. It never touches this computer's kernel." },
        { page: "/kernel", title: "A driver dereferences NULL", focus: "#console",
          enter: () => post("/api/lab/kernel", { op: "trigger", scenario: "null_deref" }),
          body: "With panic_on_oops = 1 the kernel cannot trust its own state, so it stops everything.",
          wait: { test: st => (st.kernel || {}).down, text: "Triggering…", done: "Kernel panic — no process can run, no telemetry" } },
        { page: "/kernel", title: "Reboot", focus: "#steps",
          body: "After kernel.panic seconds: firmware → kernel → root file system (journal replay) → init → services → health checks.",
          wait: { test: st => ((st.kernel || {}).run || {}).outcome === "RECOVERED", text: "Rebooting…", done: "Back up — verified by post-boot checks" } },
        { page: "/kernel", title: "What survived", focus: "#state",
          body: "Files on disk survive; everything in memory — queues, connections, open transactions — is lost." },
        { page: "/kernel", title: "What you just saw", end: true,
          body: "A process crash leaves the OS running; a kernel panic stops all of it, and only a reboot brings it back." },
      ] },
  };
  const ORDER = ["cpu", "diskfull", "deadlock", "kernel", "leak"];

  let S = null, timer = null, lastState = null, waitOk = false;
  const load = () => { try { return JSON.parse(localStorage.getItem(KEY)) || null; } catch (e) { return null; } };
  const save = () => { try { S ? localStorage.setItem(KEY, JSON.stringify(S)) : localStorage.removeItem(KEY); } catch (e) { /* private mode */ } };
  const path = p => p.split("#")[0];
  const here = p => path(p) === location.pathname;

  function card() {
    let c = document.getElementById("coach");
    if (!c) { c = document.createElement("section"); c.id = "coach"; c.className = "coach"; c.setAttribute("aria-live", "polite"); document.body.appendChild(c); }
    return c;
  }
  function unfocus() { document.querySelectorAll(".demo-focus").forEach(e => e.classList.remove("demo-focus")); }
  function focus(sel) {
    unfocus(); if (!sel) return;
    const tryIt = (n) => { const el = document.querySelector(sel); if (el) { el.classList.add("demo-focus"); el.scrollIntoView({ behavior: "smooth", block: "center" }); } else if (n) setTimeout(() => tryIt(n - 1), 300); };
    tryIt(8);
  }
  function stop() { S = null; save(); clearInterval(timer); unfocus(); const c = document.getElementById("coach"); if (c) c.remove(); }

  function render() {
    const sc = SCRIPTS[S.id], st = sc.steps[S.i], n = sc.steps.length, c = card();
    const away = !here(st.page);
    const w = st.wait && !away;
    c.innerHTML = `<div class="coach-h">Guided demo · ${OS.esc(sc.name)} · step ${S.i + 1} of ${n}<button type="button" class="x" data-d="min" aria-label="Minimise" title="Minimise">–</button><button type="button" class="x" data-d="exit" aria-label="Exit the guided demo" title="Exit">×</button></div>
      <div class="coach-dots">${sc.steps.map((_, k) => `<i class="${k <= S.i ? "on" : ""}"></i>`).join("")}</div>
      <h3>${OS.esc(st.title)}</h3><p>${st.body}</p>
      ${w ? `<div class="wait" id="coachWait"><span class="spinner"></span>${OS.esc(st.wait.text)}</div>` : ""}
      <div class="coach-f">${S.i > 0 && !st.end ? '<button type="button" class="btn btn-ghost btn-sm" data-d="back">Back</button>' : ""}
        ${w ? '<button type="button" class="skip" data-d="skip">skip waiting</button>' : ""}
        ${away ? `<button type="button" class="btn btn-primary btn-sm" data-d="go">Go to this step →</button>`
          : st.end ? `<button type="button" class="btn btn-ghost btn-sm" data-d="another">Another scenario</button><button type="button" class="btn btn-primary btn-sm" data-d="exit">Finish</button>`
          : `<button type="button" class="btn btn-primary btn-sm" data-d="next" ${w ? "disabled" : ""}>Next →</button>`}</div>`;
    if (!away) focus(st.focus);
    clearInterval(timer);
    if (w) { waitOk = false; poll(); timer = setInterval(poll, 1000); }
  }
  async function poll() {
    const sc = S && SCRIPTS[S.id]; if (!sc) return;
    const st = sc.steps[S.i]; if (!st.wait) return;
    try {
      const q = lastState ? `?full=0&since=${lastState.seq}&esince=${lastState.eseq}&epoch=${lastState.epoch}` : "?full=0";
      lastState = await OS.api("/api/lab/state" + q);
    } catch (e) { return; }
    if (!waitOk && st.wait.test(lastState)) {
      waitOk = true; clearInterval(timer);
      const el = document.getElementById("coachWait");
      if (el) { el.className = "wait " + (st.wait.bad ? "bad" : "ok"); el.textContent = (st.wait.bad ? "✗ " : "✓ ") + st.wait.done; }
      const b = document.querySelector('#coach [data-d="next"]'); if (b) { b.disabled = false; b.focus({ preventScroll: true }); }
      const sk = document.querySelector('#coach [data-d="skip"]'); if (sk) sk.remove();
    }
  }
  async function enter() {
    const st = SCRIPTS[S.id].steps[S.i];
    if (!here(st.page)) { render(); return; }
    if (!S.entered) {
      S.entered = true; save();
      if (st.enter) { try { await st.enter(); } catch (e) { LAB && LAB.toast && LAB.toast(e.message, "bad"); } }
    }
    render();
  }
  function goTo(i, runActions) {
    S.i = i; S.entered = !runActions; save();
    const st = SCRIPTS[S.id].steps[i];
    if (!here(st.page)) { location.href = st.page; return; }
    if (st.page.includes("#") && location.hash !== "#" + st.page.split("#")[1]) { location.hash = st.page.split("#")[1]; location.reload(); return; }
    enter();
  }
  document.addEventListener("click", e => {
    const b = e.target.closest("#coach [data-d]"); if (!b || !S) return;
    const d = b.dataset.d;
    if (d === "min") { const c = document.getElementById("coach"); c.classList.toggle("min"); b.textContent = c.classList.contains("min") ? "+" : "–"; return; }
    if (d === "exit") stop();
    else if (d === "next") goTo(S.i + 1, true);
    else if (d === "back") goTo(S.i - 1, false);
    else if (d === "skip") { waitOk = true; clearInterval(timer); const n = document.querySelector('#coach [data-d="next"]'); if (n) n.disabled = false; }
    else if (d === "go") goTo(S.i, !S.entered);
    else if (d === "another") { stop(); DEMO.pick(); }
  });

  function pick() {
    let m = document.getElementById("demoPick");
    if (!m) {
      m = document.createElement("div"); m.id = "demoPick"; m.className = "modal-back";
      m.innerHTML = `<div class="modal sm" role="dialog" aria-modal="true" aria-labelledby="dpT"><h2 id="dpT">Guided demo</h2>
        <div class="q">Pick a story. Each starts from a clean, healthy server and walks through break → detect → diagnose → fix → verify, waiting for the real simulator at every step.</div>
        <div class="demo-pick">${ORDER.map(id => `<button type="button" data-pick="${id}"><div><b>${SCRIPTS[id].name}</b><span class="d">${SCRIPTS[id].desc}</span></div><span class="t">${SCRIPTS[id].time}</span></button>`).join("")}</div>
        <div class="modal-actions"><button type="button" class="btn btn-ghost" data-close>Cancel</button></div></div>`;
      document.body.appendChild(m);
      m.addEventListener("click", e => {
        if (e.target === m || e.target.closest("[data-close]")) { m.hidden = true; return; }
        const b = e.target.closest("[data-pick]"); if (b) { m.hidden = true; start(b.dataset.pick); }
      });
    }
    m.hidden = false;
  }
  function start(id) {
    if (!SCRIPTS[id]) return;
    S = { id, i: 0, entered: false }; save(); lastState = null;
    goTo(0, true);
  }
  window.DEMO = { start, pick, stop, SCRIPTS };

  document.addEventListener("DOMContentLoaded", () => {
    const b = document.getElementById("btnDemo"); if (b) b.addEventListener("click", pick);
    const t = null;                                // (teaching-notes switch removed: explanations are inline "Learn more")
    if (t) {
      t.checked = !document.documentElement.classList.contains("notes-off");
      t.addEventListener("change", () => {
        document.documentElement.classList.toggle("notes-off", !t.checked);
        try { localStorage.setItem("os.notes", t.checked ? "on" : "off"); } catch (e) { /* ignore */ }
      });
    }
    S = load();
    if (S && SCRIPTS[S.id] && SCRIPTS[S.id].steps[S.i]) enter(); else S = null;
  });
})();
