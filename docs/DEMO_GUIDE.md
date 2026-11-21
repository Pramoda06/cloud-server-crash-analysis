# Faculty demonstration guide

**Cloud Server Crash Analysis: An Operating System Perspective** — about 15 minutes.
Every number below was checked against the current code (15 Oct 2026). Times are *simulated* seconds
(one sample every 0.5 s); the live app runs in real time, so they match the clock during the demo.

## Before the demo

```bat
cd /d "E:\OS-case study"
run.bat                      :: first time: creates venv, installs Flask + psutil, starts the app
:: or, with the venv already set up:
venv\Scripts\activate
python app.py
```

Open **http://127.0.0.1:5000**. Check the console banner shows `Database: …\database\experiments.db`.
If the server shows any failure from earlier practice, press **Reset to healthy** on the Overview.

> Say once, at the start: *"Every failure here is simulated inside a model — nothing on this laptop is
> stressed or crashed. The only real records are on the Real Kernel Bugs page, and they are historical."*

## Fastest path: the guided demo (recommended for a client or a short slot)

Press **Guided demo** in the sidebar and pick a story. A coach card leads you page by page and waits for the real
simulator before each *Next*; you only talk and click *Next*.

| Story | Time | What it shows |
|---|---|---|
| CPU overload | ≈ 2 min | detection from metrics alone, diagnosis, a fix that FAILS (renice) and one that works |
| Disk full | ≈ 2 min | full ≠ slow (errors without latency), why deleting an open file frees nothing |
| Deadlock | ≈ 1 min | circular wait, the detection algorithm, victim recovery |
| Kernel panic | ≈ 1½ min | panic, reboot, lost vs preserved state, then a *real* kernel bug of the same kind |
| Memory leak | ≈ 2½ min | a process that never frees memory; restart is a workaround, not a fix |

Suggested order for 10 minutes: CPU overload → Disk full → Kernel panic. Open the collapsed *How this works* sections when
an examiner wants the formulas and "what to learn" boxes; leave it off for a clean presentation.

The manual walkthrough below covers the same ground with more freedom.

## 1. Overview and the model (1 min)

* **Overview**: one simulated server (4 vCPU, 16 GB RAM, 6,000-IOPS NVMe). HEALTHY.
* **Control Room → status strip**: click *Response* → *why did it change?* walks back through the model to the control that changed.
  The inspector shows the value, its baseline, its thresholds and its tag (CONTROL / SIMULATED / DERIVED).

## 2. CPU saturation → detection → analysis → targeted recovery (3 min)

1. **Control Room → Inject a failure → CPU overload → Run.**
   Within a few seconds: CPU saturation **FAILED**, Service degradation **FAILED** (secondary).
2. Point out: a level counts only after **2 consecutive samples**, and clears only **5 % below** its
   threshold (hysteresis, so values near a threshold do not flap).
3. **Incident → step 2 · Why**: primary cause *CPU saturation* (tagged **inferred**), evidence (**observed**),
   simulated causal chain (**simulated**), detection latency.
4. **Incident → step 3 · Fix**: apply **Raise the web service priority (nice −10)** → after the 30 s window:
   **FAILED** — renicing changes *who* waits, not how much work there is.
   Then **Reduce the CPU workload** → **RECOVERED** (every rule below WARNING for 3 samples).
5. Optional: **Compare strategies** for *CPU overload* — identical seeded runs, one chart.

## 3. Disk full is not a slow disk (2 min)

1. **Control Room → Inject a failure → Disk full → Run**.
   A runaway log writer fills the file system (accelerated). **Control Room** shows the *Disk space* cell at 100 % while *Disk latency* stays normal.
2. When full: errors ≈ 39 % (writes fail at once with **ENOSPC**) while **response time stays ~46 ms** and
   **disk latency does not rise**. Contrast: a *Disk I/O spike* raises latency instead.
3. Incident → step 3 · Fix: **Delete the log files only** → nothing freed (the writer still has its file open — Linux
   frees an unlinked file's blocks only when it is closed) → **FAILED**.
   **Stop the writer, then delete its logs** → **RECOVERED**.

## 4. Deadlock: detect, recover, prevent, avoid (3 min)

1. **Labs → Deadlock → Two processes, two locks.** The resource-allocation graph comes from the backend
   state. Detector: **WARNING** at once, **CRITICAL** after the cycle persists 3 s.
2. **Detect deadlock** → open *Algorithm steps* (work / finish vectors). Name the four **Coffman conditions**.
3. **Terminate victim** → locks granted to the waiter → **RECOVERED**.
4. **Cycle, but no deadlock**: with multi-instance resources a cycle is necessary but not sufficient.
5. **Banker's algorithm**: safe sequence shown; a request that would make the state unsafe is delayed.
6. Turn on **lock ordering** and retry the first example — the out-of-order request is refused (prevention).

## 5. Processes: Kill vs Crash vs Hang (1 min)

**Labs → Processes**: *Crash* a web-worker (SIGSEGV, exit 139) — capacity drops, the OS keeps running.
*Hang* a worker — still alive (HUNG), serves nothing; **Restart failed processes** (Incident → Fix) is *not applicable*
(it never exited); **Terminate & restart an unresponsive process** fixes it. Kill the **web-master**: its
workers die with it (SIGHUP) and availability falls to 0 % while CPU and memory look fine.

## 6. Kernel panic (2 min)

**Labs → Kernel panic.** Point at the **Simulation only** banner. Run **NULL pointer dereference in a network driver**: panic
message, every page shows FAILED with no telemetry, boot stages after `kernel.panic` seconds, post-boot
checks → RECOVERED, **Lost vs preserved** state. Then set `panic_on_oops = 0` and repeat: only the task
is killed and the kernel is *tainted* but keeps running.

## 7. Real kernel bugs (optional, only if asked)

The real SyzFix cases are not in the menus. If an examiner asks, open `/cases` directly: 42 real, already-fixed
Linux bugs found by syzbot, with links to the crash report and the fix. Say: *a deterministic subset of one of eleven
SyzFix shards; fuzzer-found bugs, not production crash rates.*

## 8. Wrap-up (30 s)

**History** keeps the recorded runs and recovery comparisons. Close with the limitations:
simplified, deterministic models; thresholds are teaching values; no real kernel is touched.

## Do not claim

* that the app reproduces or causes real kernel crashes, or measures this laptop's failures;
* that the real cases are representative of all SyzFix bugs or of production crash frequencies;
* that the primary cause is *proven* — it is an attribution from the model's contribution terms;
* that the thresholds are Linux defaults — they are teaching values you can change in the Control Room (Advanced).
