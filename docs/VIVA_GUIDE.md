# Viva guide

**Cloud Server Crash Analysis: An Operating System Perspective.** Short, accurate answers you can say
aloud, each tied to what the code actually does. Formulas are the ones in `simulator/lab/model.py`.

## A. The project

**1. What is the project, in one sentence?**
An interactive laboratory that simulates a cloud server's CPU, memory, disk, network, processes and locks,
lets you cause failures, detects them only from observed metrics, explains the cause, and verifies recovery —
plus a separate reference page of real, already-fixed Linux kernel bugs.

**2. Is anything real?**
Three kinds of data, always labelled: **simulated / derived** values (everything in the labs);
optional **REAL / HYBRID** host readings via `psutil` (off by default — then CPU and memory baselines are
nudged by this computer's load, read-only); and **HISTORICAL DATASET** records on the Real Kernel Bugs page.
Nothing on the computer is stressed, crashed or rebooted.

**3. Architecture?**
Flask (`app.py`) serves pages and a JSON API. Each server has a `LabEngine` (`engine.py`) that ticks every
0.5 s: `SystemModel.step()` computes 49 graph nodes and their contributions (`model.py`); the `Detector`
applies 9 rules (`detect.py`); attribution picks primary vs secondary failures; `rca.py` writes the
analysis; `recovery.py` holds 19 strategies and the verification. SQLite stores servers, runs, snapshots
and records. The browser polls `/api/lab/state`.

## B. OS concepts in the model

**4. How is CPU saturation modelled?**
Offered load ρ = runnable work ÷ cores. Below ρ = 0.98 the run queue relaxes to the Sakasegawa M/M/c
approximation Lq = ρ^√(2(c+1)) / (1−ρ); above it the queue grows every second. Waiting time =
queue × 50 ms ÷ cores. Utilization cannot exceed 100 % — the excess shows up as queueing.

**5. What does `nice` do here?**
CFS gives each task a weight 1024 × 1.25^(−nice); a lower nice means a larger share. Renicing the web
service lowers its CPU wait but does not remove the overload — that is why that strategy ends FAILED.

**6. Thrashing?**
When the working set exceeds RAM, the kernel evicts pages that are needed again soon, so page faults and
swap I/O explode and CPU time goes to paging. The model uses a teaching threshold at 80 % of RAM
(faults = 35 + 9000·over^2.2 …). Real kernels reclaim based on zone watermarks, not a fixed 80 %.

**7. Disk I/O bottleneck vs a full disk — what is the difference?**
*Speed vs capacity.* A busy disk makes every I/O **wait**: latency ≈ S/(1−ρ) (M/M/1), queues grow,
processes sit in I/O wait. A **full** file system makes new writes **fail at once** with ENOSPC — errors
rise but latency does not. In the lab: Disk I/O spike (85 %) → disk latency ≈ 650 ms and the errors that
appear are *timeouts*; Disk full → ~39 % errors (write refused) with ~46 ms responses.

**8. Why did deleting the log file not free space?**
On Linux `rm` only removes the directory entry (unlink). The inode and its blocks stay until the last
open file descriptor is closed, so a running writer keeps the space (`lsof +L1` lists such files). Stop the
writer first, then delete — exactly the strategy that recovers.

**9. Little's law and why it matters?**
L = λ·W. When responses slow down, more requests are in flight, and each holds memory — that is how a CPU
problem becomes a memory problem in the model (a cascade link you can turn off).

**10. Process states? Kill vs Crash vs Hang?**
NEW, READY, RUNNING, WAITING (I/O), BLOCKED (lock), SLEEPING, HUNG, RESTARTING, TERMINATED.
Kill = SIGKILL from another process (exit 137); Crash = the process faults itself, e.g. SIGSEGV (exit 139);
in the model both terminate it and release its resources. Hang = still alive but makes no progress; a
supervisor does not notice because nothing exited, so it must be killed before it can be restarted.

**11. Deadlock: conditions, detection, recovery, prevention, avoidance?**
Coffman: mutual exclusion, hold-and-wait, no pre-emption, circular wait — all four are needed.
*Detection*: with single-instance resources a cycle in the wait-for graph means deadlock; with
multi-instance resources a cycle is necessary but not sufficient, so the lab runs the detection algorithm
(work / finish vectors). *Recovery*: terminate a victim or pre-empt a resource and roll it back.
*Prevention*: lock ordering removes circular wait. *Avoidance*: Banker's algorithm grants a request only if
a safe sequence still exists.

**12. Kernel panic vs oops?**
An oops is a kernel error the kernel can survive — the offending task is killed and the kernel is marked
tainted. With `panic_on_oops = 1` it becomes a panic: the kernel stops everything, then reboots after
`kernel.panic` seconds (0 = stays halted). After a reboot, volatile state (memory, queues, connections) is
lost; files on disk survive. The Kernel Failure Lab is a state machine — it never runs a real panic.

## C. Detection and analysis

**13. How does detection avoid false alarms?**
Two mechanisms. *Confirmation*: a level counts only after 2 consecutive samples. *Hysteresis*: once
raised, a level clears only when the value falls 5 % below its threshold. Measured on this model before
hysteresis was added: CPU held at ~75 % produced 62 alerts and 26 separate failure episodes in 5 simulated
minutes; with it, one alert. The trigger threshold itself is unchanged.

**14. How is the primary cause found?**
Each node records how much each input contributed. A failing rule is *secondary* if its change is dominated
by another active failure (e.g. ≥ 30 % of disk demand is swap I/O while memory is failing). Rules not
explained by another are root causes; the one that reached failure first is listed as primary. If two
independent causes reach failure within 2 s of each other, they are shown as **concurrent** because their
order is not meaningful. It is an *inferred* attribution inside the model, not a proof.

**15. Detection vs injection?**
Injection is what you did; detection is what the monitor concluded from metrics alone — the detector
never reads the controls. The analysis shows both and the time between them.

**16. How is recovery verified?**
After a strategy is applied, the same detector must see every rule below WARNING for 3 consecutive
samples → RECOVERED. If only the targeted rule is fixed within 30 s → PARTIAL; if not → FAILED.
Strategy comparisons rerun each strategy from the same seed and the same injection time.

**17. What is a metastable failure?**
One that persists after its trigger is removed because of a feedback loop (slow responses → client retries
→ more load). The lab flags it when the server is still failed 20 s after the trigger was reduced.

## D. Real kernel bugs

**18. Where do the real cases come from, and what do they show?**
One shard (`train-00000-of-00011.parquet`, 656 rows) of the SyzFix dataset — Linux kernel bugs found by
Google's syzbot fuzzer and fixed upstream. I verified the file's SHA-256 against the published value, then
selected 42 cases deterministically (sha256 order, at most 6 per category and 2 per subsystem, one per fix
commit, no quotas). Each case shows the syzbot title, call-trace function names, the commit named in its
`Fixes:` tag, the fix's files and a short code excerpt, with links to syzbot and git.kernel.org.

**19. Why not show the full crash report and the developer's explanation?**
The dataset card says it is for research use and that patches and mailing-list text keep their original
licences — that is not a redistribution licence. Kernel code is GPL-2.0, so short code excerpts are
included with attribution; crash-report and commit-message text have no stated licence, so the page links
to them instead. Reproducers, discussions and e-mail addresses are never used.

**20. Do these cases tell us how often kernels crash?**
No. They are fuzzer-found bugs from one of eleven shards, chosen for teaching. Categories come from title
patterns; "derived" subsystems come from file paths.

**20a. How are the real cases shown in the app?**
They are kept in the project (`casestudies/`, the `/api/cases` endpoint and the unlisted `/cases` page) but not
shown in the menus, so the demo stays focused on the simulated OS behaviour. Each record links to the original
syzbot report and fix commit.

## E. Testing and limits

**21. How did you test it?**
Automated unit, integration, API and browser tests, run with `python -m pytest tests` or
`python -m unittest discover -s tests`. They use a temporary database, never `database/experiments.db`.
Examples: queueing boundaries, 100 % packet loss, deadlock detection with multi-instance resources,
Banker's safety, recovery verdicts (including strategies that must FAIL), panic refusals, hysteresis,
disk-full semantics, and validation of every committed real case. Results: see `docs/UPGRADE_REPORT.md`.

**22. Main limitations?**
Simplified analytical models, teaching thresholds, one file system with an accelerated fill rate, no real
kernel internals, the SyzFix subset is small and not representative, and the "concurrent" window is a
display rule.
