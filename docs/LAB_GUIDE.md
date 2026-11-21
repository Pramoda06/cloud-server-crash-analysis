# Lab guide & concepts for viva

Every lab follows the same loop: **understand → break → observe → explain → recover → verify → compare.**
Everything is simulated; nothing on the computer is stressed.

## How to use each lab

Quickest start: **Control Room → Inject a failure** has twelve one-click failures; **Guided demo** (sidebar) runs five guided stories. Explanations sit in collapsed *How this works* sections.

### Control Room (`/lab`) — resource pressure
1. Raise **CPU workload** to ~120 %. Watch utilization hit 100 %, then the **run queue** and **waiting time** grow.
2. Lower it again. Utilization drops at once; the queue drains more slowly.
3. Try **Memory pressure** 55–60 %: page faults and paging overhead explode once the working set passes ~80 % of RAM (thrashing), and paging steals CPU (cascade).
4. Open **Incident Analysis** for the root cause.

### Labs → Processes (`/processes`)
* Create a **Memory leak** process and watch memory grow by itself; restarting it clears memory but not the bug.
* Press **Hang** on a web-worker: it stays alive (HUNG) but serves nothing — a crash you cannot see in the process count. In Incident → Fix & verify, *Restart failed processes* is not applicable (it never exited); *Terminate & restart an unresponsive process* fixes it.
* Create a **Log flood** process and open the **Disk** tab: the file-system bar fills (accelerated). When it is full, errors jump to ~39 % while disk latency stays ~2.7 ms — a full disk fails fast, a busy disk waits. Killing the writer frees nothing (its files stay); deleting the logs while it runs frees nothing either (the file is still open). Stop it, then delete.
* Crash a **web-worker** (SIGSEGV): the OS keeps running, capacity drops. Kill the **web-master**: its workers die with it (SIGHUP) and the service fails, while CPU and memory look fine.

### Labs → Deadlock (`/deadlock`)
1. Load **Two processes, two locks** → the graph shows the cycle; the detector confirms a deadlock after 3 s.
2. Press **Detect deadlock** and open *Algorithm steps*: no holding process can finish → deadlocked.
3. Recover by **Terminate victim** or **Pre-empt resource**; the graph, tables and queues update.
4. Load **Cycle, but no deadlock**: R has 2 instances, P3 can finish and free one → cycle without deadlock. Tick **Transactions finish on their own** to watch P3, then P2, then P1 finish and release.
5. Turn on **lock ordering** and try the first example again: the request is refused (prevention).
6. Load **Banker's algorithm**: safe sequence P2 → P1 → P3. Ask P3 for 1 more T: delayed because the state would become unsafe (avoidance).
7. Build your own: add processes and resources (1–12 instances), request/release, remove, **Reset lab**.

### Labs → Kernel panic (`/kernel`)
1. Choose *NULL pointer dereference*, press **Start**. Watch PANIC → reboot stages → health checks.
2. Read **Lost vs preserved**, the console and the timeline.
3. Turn off `panic_on_oops` and repeat: only one task dies; the kernel is tainted but keeps running.
4. Run *Root filesystem cannot be mounted*: it boot-loops until you **Boot the previous kernel**.
5. Set `kernel.panic = 0`: the machine stays frozen until **Power-cycle**.

### Incident → steps 3–4, Fix & verify (`/incident#fix`)
1. Create a failure (e.g. CPU spike). The page lists the active failures and the strategies that fit.
2. **Apply** one. The result stays *verifying* until the detector decides: RECOVERED / PARTIAL / FAILED.
3. **Compare strategies**: each runs from the same seed, same failure, same moment. Example: for CPU overload, *renice* fails (it changes who waits, not how much work there is); for a disk overload, *drain the queue only* fails because the queue refills.

### Incident → steps 1–2, What failed & why (`/incident`)
Primary cause (inferred) · trigger · evidence · simulated causal chain · observed effects · recovery · verification. Each part is tagged with where it comes from. Two independent causes that fail within 2 s of each other are shown together as *concurrent*.

### Real Kernel Bugs (`/cases`)
42 documented, already-fixed Linux kernel bugs (a deterministic subset of one of eleven SyzFix shards), grouped by kind with the matching lab. Read one NULL-dereference case after the Kernel Failure Lab: symptom, the commit named in `Fixes:`, the fix, the sources. See SYZFIX.md for what is and is not included.

## Concepts for viva

| Question | Short answer |
|---|---|
| Utilization vs demand vs run queue? | Utilization caps at 100 %; demand can exceed it; the excess waits in the run queue, which is what makes latency grow. |
| Memory utilization vs pressure vs thrashing? | High utilization alone is fine. Thrashing = the working set no longer fits, so pages are evicted and faulted back constantly; the lab needs high page faults **and** high paging overhead. |
| Disk utilization vs queue vs latency? | Near 100 % utilization the queue grows as ρ²/(1−ρ) and latency as service time/(1−ρ). Free space is a different problem: a full file system makes writes fail at once (ENOSPC) without any extra latency. |
| Waiting vs circular wait vs deadlock? | Waiting usually ends. A cycle with single-instance resources is a deadlock; with multiple instances it may not be — use the detection algorithm. |
| Four Coffman conditions? | Mutual exclusion, hold and wait, no pre-emption, circular wait — all four are needed. |
| Prevention vs avoidance vs detection vs recovery? | Prevention breaks a condition by design (lock ordering). Avoidance checks each request (Banker's: grant only if safe). Detection finds deadlocks that happened. Recovery breaks them (victim / pre-emption). |
| Safe vs unsafe state? | Safe: some order lets everyone get its maximum and finish. Unsafe is not yet deadlock, but may become one. |
| Process crash vs service failure vs kernel panic? | A process crash leaves the OS running. A service fails when its processes cannot serve. A kernel panic stops the whole OS — and need not involve high CPU. |
| Why can't restarting a process fix a panic? | After a panic no process runs at all; only a reboot (firmware → kernel → init → services) brings the system back. |
| What survives a reboot? | Data on disk (journal replay), configuration, a kdump vmcore if enabled. Memory, queues, connections and open transactions are lost. |
| When is recovery "successful" here? | Only after the detector sees every rule below WARNING in 3 consecutive samples. PARTIAL if the target is fixed but another failure remains. |
| Why does an alert not flicker at a threshold? | Hysteresis: raised above the threshold, cleared only 5 % below it. |
| What is real in this app? | Optional psutil readings (off by default) and the historical records on the Real Kernel Bugs page. Everything else is simulated or derived — see DATA_SOURCES.md. |
