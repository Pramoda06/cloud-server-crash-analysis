# Cloud Server Crash Analysis: An Operating System Perspective

An **interactive operating-system / cloud failure laboratory**. You control a virtual server — CPU workload, memory pressure, disk I/O, client traffic, network latency and loss, processes and locks — and watch every change propagate through CPU scheduling, paging, the I/O queue and TCP into latency, errors and failure. You can break the server on purpose, see which subsystem failed first and how the failure cascaded, trace *why* any number changed, recover the server, record the experiment and replay it exactly.

Everything runs locally (Flask + SQLite + Chart.js). **Nothing on your computer is stressed**: no CPU burn, memory allocation, disk writes, network shaping, signals or real locks.

```
USER CONTROL → SYSTEM STATE → OS BEHAVIOUR → METRICS → RELATIONSHIPS → FAILURE → DETECTION → CASCADE → RECOVERY
```

## 1. Pages

| Page | Question | What you do there |
|---|---|---|
| **Overview** (`/`) | What is happening? | CPU / memory / disk usage cards, selected-server details, a short how-to, and the server table (status, CPU, memory, disk, uptime, last run; add / select / remove) |
| **System Lab** (`/lab`) | Control the server | One control panel (Basic = six core controls; Advanced adds the remaining parameters), **System Health** live graph (resource usage / process activity / response time, last 2 min, with failure bands and stress / recovery markers) plus a current-status strip, **Inject Sudden Stress** (type + intensity slider), **Live consequences** (cause → effect → consequence rows with live sparklines; click any value for the traced *why did it change?* inspector), the full dependency graph (Advanced, collapsible), record, snapshot, pause, **Recovery** (removes the causes of the failure — stress, overloaded controls, failed processes, held locks — and the server returns to normal on the same, uninterrupted timeline), recent events |
| **Live Monitoring** (`/monitor`) | Observe the metrics | CPU / memory / disk / network / service graphs on **one shared time axis** (synced crosshair, health bands, control-change and detection markers, threshold lines), the failure timeline, and a **parameter status table** (current vs baseline) |
| **Failure Analysis** (`/analysis`) | Why did it fail? | Root-cause analysis of any failure episode: primary cause, OS subsystem, trigger, traced causal chain, observed effects, metrics that crossed thresholds, cascade, which subsystem failed first, detection and recovery mechanisms, timings. After **Recovery** it switches to a **Recovery Analysis** of the failure just recovered: actions taken, timeline, before → after, health-check verification and measured recovery time |
| **Process & Resource Lab** (`/processes`) | Study individual processes and the resources they consume | Process table (create / kill / crash / restart / renice), Deadlock lab (resource-allocation graph, Coffman conditions, request/release locks, victim / pre-emption, lock-ordering prevention), Memory, Disk and Network labs |
| **Experiment History** (`/history`) | Review runs | Recorded runs (click for shared-axis charts, timeline, analysis, configuration, **Replay**, JSON download), snapshot comparison, read-only list of experiments from the previous version |

**Basic mode** shows six core controls (CPU workload, memory pressure, disk I/O, request rate, latency, packet loss). **Advanced mode** keeps the same panel, splits disk I/O into read / write, and adds the remaining parameters. Detection thresholds use the built-in defaults.

## 2. Control model

| Control | Range | Meaning |
|---|---|---|
| CPU workload | 0–150 % | Batch CPU demand as a share of all vCPUs (> 100 % = more work than the CPU can do) |
| CPU-bound processes | 0–32 | Processes sharing that work — one process can use at most one vCPU |
| Web-service priority (nice) | −20…19 | CFS weight of the web service vs. batch jobs |
| Memory pressure | 0–100 % RAM | Extra allocation by memory-hungry processes |
| Memory-hungry processes | 0–8 | How many processes share that allocation |
| Read / write workload | 0–1.5 × capacity IOPS | Background disk requests (Basic mode: one "Disk I/O load %" slider, 65 % writes) |
| Client request rate | 0–4 × normal req/s | Client traffic; each request costs CPU, disk reads and memory while in flight |
| Network latency (added) | 0–1500 ms | Extra round-trip delay (like `tc netem delay`) |
| Packet loss | 0–100 % | Dropped packets (like `tc netem loss`) |
| Connection failures | 0–100 % | Refused / reset connections |
| Client timeout | 250–5000 ms | Clients abandon slower requests |
| Process / lock actions | — | create, kill (SIGKILL / SIGSEGV), restart, renice, request / release locks, pre-empt, form / resolve deadlock |
| Sudden stress | 7 kinds × 0–100 % | One-step jump of the relevant controls; optional hold time; *Remove stress* restores the previous values |

Controls are **targets**. The simulated system follows them with inertia (time constant 1 s rising, 2.5 s falling). The cards show *set* versus *system now at* while the system is still catching up.

## 3. Simulation model (simulator/lab/model.py)

One `SystemModel` per server is the single source of truth. Every 0.5 s, `step()` computes ~45 nodes in dependency order. Each node also records its **parts** (how much each dependency contributed), which is what the causal trace and the attribution use.

**State variables** (carry over between samples): CPU run queue, disk backlog, request (accept) backlog, the previous error rate and in-flight count (cascade feedback), and the process and lock tables.

| Subsystem | Rule (as implemented) |
|---|---|
| CPU | ρ = [min(workload·cores, batch procs) + λ·0.55 ms + paging overhead·cores + created procs + 3 %] / cores. ρ < 0.98: run queue relaxes to **Lq = ρ^√(2(c+1))/(1−ρ)** (Sakasegawa M/M/c approximation). ρ ≥ 0.98: grows by (ρ−0.98)·c / 50 ms per second. Run queue is bounded at ≤ 1200 processes. Waiting time = queue × 50 ms ÷ cores. Request CPU wait × 2·w_batch/(w_batch+w_web), w = 1024·1.25^−nice |
| Memory | Working set ws = 38 % (OS & services) + pressure + 1 MB × in-flight requests + created procs. Pedagogical thrashing threshold at **ws > 80 %**: over = clamp((ws−80 %)/20 %, 0, 2), o1 = min(over, 1.0). Page faults = 35 + 9000·o1^2.2 + 12000·max(over−1, 0) /s. Paging overhead = min(90 %, 70·o1^1.8 + 15·max(over−1, 0)) % CPU. Available RAM is clamped to a kernel reserve floor of ≥ 0.5 % RAM. Swap used ≤ swap capacity |
| Disk | Demand = reads + writes + λ·0.57 + swap I/O + created procs. ρ < 0.98: **Lq = ρ²/(1−ρ)**, latency = S/(1−ρ) with S = 2.4 / 3 / 8 ms (NVMe / SSD / HDD). Hardware parallelism modeled via c = cap · S / 1000 channels (14.4 parallel channels for NVMe). Backlog is bounded by **cap × 30 s** (180,000 IOPS for NVMe), capping maximum queue latency to ~30.1 s |
| Network | RTT = base + delay. Retransmissions: enumerate retry counts k ∈ [0..5] with probability p^k(1−p), delay RTO·(2^k−1), RTO = max(200 ms, 2·RTT). At **100 % packet loss**, conditional retransmission delay is mathematically undefined (None / —) and client-observed latency is clamped to **timeout_ms** (2000 ms default), eliminating latency collapse artifacts |
| Service | Capacity = live workers × 2·λ_base/4 × min(1, 1/ρ) × (1−0.85·over²), and 0 if web-master or database is down. Backlog B += (arrivals − capacity)·dt, max 1024; beyond that requests are refused. Response = 22 ms + CPU wait + 2·disk latency + page-fault stalls + lock wait + B/capacity + RTT + retransmissions, bounded by timeout_ms. Timeouts = P(response > timeout), with service time exponential and network delay enumerated. Error rate = 1 − successful/attempts |
| Locks | Wait-for graph + DFS cycle search. 35 % of requests touch Resource A/B: +50 ms if held by a running process; until timeout if the holder is deadlocked |

**Cascade links** (Advanced → *Cross-subsystem cascades*, on by default):

- paging → disk (swap I/O)
- paging → CPU (kernel time)
- slow responses → memory (in-flight requests, Little's law)
- failures → load (clients retry 80 % of failed requests)
- slow swap → longer page-fault stalls

**Determinism:** the only randomness is a seeded noise generator that draws a fixed number of values per sample. With the host anchor off, a run is reproduced **sample for sample**. This was tested: 46/46 samples identical, including a process kill, a threshold change, a settings change and a deadlock.

## 4. Real vs simulated

Every value carries a tag:

- **REAL** — psutil readings of this computer (only when *Anchor baseline to this computer* is on).
- **HYBRID** — CPU and memory with the anchor on.
- **SIMULATED** — the controls, the process and lock tables, and the injected path conditions.
- **DERIVED** — everything else, computed by the rule shown in *Simulation details*.

The application never claims to control the Linux kernel.

## 5. Detection (simulator/lab/detect.py)

Levels are **HEALTHY → WARNING → DEGRADED → CRITICAL → FAILED**. A level counts only after 2 consecutive samples. Health = the worst rule. The detector never reads the controls.

| Rule | Checks (W / D / C / F) |
|---|---|
| CPU saturation | CPU utilization 75 / 85 / 95 / — ; run queue per vCPU 1 / 2 / 4 / 8 |
| Memory exhaustion / thrashing | memory utilization 80 / 88 (on its own at most DEGRADED); page faults 500 / 1500 / 3000 / 6000 **AND** paging overhead 5 / 15 / 25 / 50 % — CRITICAL/FAILED (= thrashing) only when both hold |
| Disk I/O bottleneck | I/O queue 4 / 8 / 16 / 32 ; disk latency 20 / 50 / 100 / 250 ms |
| Network degradation | RTT 150 / 300 / 500 / 1000 ms ; packet loss 2 / 5 / 10 / 30 % |
| Service degradation | response 300 / 800 / 1500 / 3000 ms ; error rate 1 / 5 / 20 / 50 % |
| Critical process failure | web-master / database TERMINATED → FAILED, RESTARTING → CRITICAL; 1 worker down → DEGRADED, ≥ 2 → CRITICAL |
| Process deadlock | cycle → WARNING; persists ≥ 3 s → CRITICAL; ≥ 3 processes blocked on locks → FAILED |

The thresholds above are the built-in defaults used by the detector.

**Failure episodes** record:

- the trigger (the operator input that pushed the server over)
- the first raw threshold crossing and every detection, in order
- the primary failure, the secondary failures and any feedback loop
- the causal trace at the worst moment
- the recovery start and end
- a self-sustaining (metastable) failure flag

## 6. Recovery

Recovery starts when health first improves after its worst point. It completes when every rule is OK for 3 consecutive samples (0.5 s apart). Queues drain at the rate of spare capacity, so recovery is gradual.

**Recovery State Machine Idempotency:**
- Triggering Recovery transitions the engine into `RECOVERING` status.
- Repeated recovery requests while recovery is already active are strictly idempotent: baseline controls and cleanup are re-applied without allocating a new sequence ID, resetting `requested_ts`, or restarting the 20-second escalation timer.
- Verification checks 6 health criteria (Service, Process, CPU, Memory, Disk, Deadlock). When all rules confirm below WARNING for 3 consecutive ticks, status advances to `RECOVERED`. If self-sustaining backlogs persist past 20 s, an escalation step flushes queues and restarts the web service.

Recovery mechanisms:

- lower a control or remove stress
- kill the hog process
- restart a crashed process (TERMINATED → RESTARTING → RUNNING with a new PID)
- kill a deadlock victim or pre-empt its lock

If a feedback loop keeps the server down after the trigger is removed, the lab reports a **self-sustaining failure** and how to break it (lower traffic, free memory, raise the timeout or restart the service).

## 7. Predefined experiments (measured results)

| Experiment | Primary failure found | Secondary |
|---|---|---|
| 1 CPU saturation | CPU saturation | Service ← CPU |
| 2 Memory exhaustion | Memory thrashing | CPU ← memory (paging), Service ← memory |
| 3 Disk I/O bottleneck | Disk I/O bottleneck | Service ← disk |
| 4 Network failure | Network degradation | Service ← network |
| 5 Critical process crash | Critical process failure | Service ← process |
| 6 Deadlock | Process deadlock | Service ← deadlock |
| 7 CPU → memory → disk cascade | CPU saturation | Memory ← CPU (requests pile up in flight), Service ← CPU |
| 8 Multi-resource overload | Network degradation (first to fail) + independent CPU, disk and memory failures | Service ← CPU |
| 9 Sudden traffic spike | Service overload (requests > worker capacity) | — |
| 10 Recovery experiment | CPU saturation and memory thrashing in a feedback loop | — |
| Break the server | Network degradation fails first; memory thrashing independently | CPU, disk and service ← memory |

## 8. Files

```
app.py                       Flask: pages + /api/lab/* (+ read-only routes for legacy history)
simulator/lab/graph.py       controls and the dependency graph metadata (labels, types, rules, explanations)
simulator/lab/model.py       SystemModel: the active simulation engine (state, nodes, contributions, processes, locks)
simulator/lab/detect.py      thresholds, levels, confirmation, primary/secondary attribution
simulator/lab/trace.py       "why did this change?" — walks the contributions back to an input
simulator/lab/rca.py         root-cause analysis of an episode
simulator/lab/programs.py    predefined, chaos, break and custom experiment programs; stress mappings
simulator/lab/engine.py      LabEngine: tick loop, events, episodes, stress, record/replay, snapshots, state API
simulator/manager.py         one LabEngine per server, one shared 0.5 s loop
simulator/storage.py         SQLite: servers, lab_runs, lab_snapshots, lab_programs (+ old experiments table)
simulator/servers.py         server options → server spec (vCPUs, RAM, disk IOPS, workload, network)
templates/                   overview, lab, monitor, analysis, processes, history, run
static/js/lab-core.js        polling, sliders, tiles, status, dependency graph, inspector, shared-axis charts
static/js/lab-report.js      root-cause analysis rendering
static/js/common.js, report.js   shared helpers + rendering of the previous version's experiments
tests/                       automated test suite:
  test_network.py            packet loss spectrum (0–100%), timeout bounds, undefined retransmission handling
  test_memory_disk.py        80% RAM thrashing threshold, memory floors, NVMe M/M/c queueing & backlog caps
  test_processes_deadlock.py process hierarchy, SIGHUP cascades, PID increments, deadlocks & preemption
  test_engine_recovery.py    2-sample debouncing, recovery idempotency, 3-sample confirmation streak, reset
  test_api_multiserver.py    API routes, input validation, and multi-server state isolation
```

## 9. Installation

Requires **Python 3.8 or newer** (`python --version`).

```bash
pip install -r requirements.txt
```

(Installs only `Flask` and `psutil`.)

## 10. Running & Automated Testing

### Start the application
```bash
python app.py
```

Open **http://127.0.0.1:5000**. On Windows the browser opens automatically. Stop the app with `Ctrl+C`.
Options: `python app.py --port 8080`, `--no-browser`, `--host 0.0.0.0`.

### Run automated tests
```bash
python -m unittest discover -s tests -p "test_*.py"
```

Runs the 20 unit tests verifying the network boundary conditions, memory thrashing, disk queueing, process lifecycle, deadlock resolution, recovery state machine, and multi-server isolation.

### Windows setup
The easiest way is to double-click **`run.bat`**. On the first run it creates a `venv`, installs requirements, and starts the app. To do the same manually:

```bat
cd "E:\OS-case study"
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
python app.py
```

### WSL (Ubuntu)

```bash
cd "/mnt/e/OS-case study"
sudo apt install -y python3-venv python3-pip     # first time only
bash run.sh
```

## 11. Viva Preparation

**1. Why is this an OS case study?**
Every failure is a failure of something the OS manages: CPU scheduling, virtual memory, the I/O subsystem, the process lifecycle, synchronization/locks, or the network stack. The project observes those resources through OS metrics and explains each failure in OS terms.

**2. How is CPU saturation different from memory exhaustion?**
CPU saturation means there are more runnable processes than cores. The ready queue and waiting time grow, but memory stays normal. Memory exhaustion means the working set exceeds RAM. Page faults and swap explode, and CPU rises only moderately, mostly from paging overhead. One is a *processor* shortage; the other is a *memory* shortage.

**3. What is thrashing?**
The OS spends most of its time servicing page faults and moving pages between RAM and swap, instead of executing useful process work. Throughput collapses even though the CPU looks busy.

**4. What is a page fault?**
An access to a virtual page that is not currently in physical memory. The MMU traps to the kernel, which loads the page (from swap or file) and updates the page table. Many *major* page faults mean heavy disk paging.

**5. Why can disk I/O become a bottleneck?**
The disk serves a limited number of operations per second. When demand approaches that capacity, requests wait in the I/O queue, and latency rises sharply (≈ service time / (1 − utilization)). Processes block in I/O-wait even though the CPU is idle.

**6. What happens when a process crashes?**
It moves to TERMINATED (for example on SIGSEGV, exit code 139). Its resources are released and the parent is notified. If it was a critical service, nothing serves requests any more: availability drops to 0 % while CPU, memory and disk look healthy. A supervisor or operator must restart it, and it gets a new PID.

**7. What is deadlock?**
A set of processes in which each one holds a resource and waits for a resource held by another in the set. None of them can ever proceed.

**8. What are the four Coffman conditions?**
- **Mutual exclusion:** a resource is non-shareable.
- **Hold and wait:** a process holds one resource while waiting for another.
- **No preemption:** resources cannot be forcibly taken away.
- **Circular wait:** a cycle P1 → P2 → … → P1.

All four must hold for deadlock to occur. Breaking any one prevents it, for example with a lock ordering.

**9. How is deadlock detected?**
Build a wait-for graph from the process and resource tables (Pᵢ → Pⱼ if Pᵢ waits for a resource held by Pⱼ). Run a DFS for a cycle. Here a cycle must also persist for more than 3 s. Recovery: terminate or roll back a victim, or preempt its resources.

**10. Why can a server be internally healthy but externally unreachable?**
Internal health (CPU, memory, processes) and external availability depend on different things. With high latency or packet loss on the network path, TCP retransmits and clients time out, so the service is unavailable even though the server itself is fine.

**11. How is failure detection different from failure injection?**
Injection is what the experimenter deliberately did. Detection is what the monitor independently concluded from the observed metrics, using threshold rules that know nothing about the injection. The lab shows both: the trigger (the control change that pushed the server over), the first raw threshold crossing, and the confirmed detection — and measures the time between them.

**12. How is recovery time calculated?**
Recovery time = time when the system is HEALTHY again (every rule OK for 3 consecutive samples) − time when health first started improving after its worst point.

**13. Why are the failures simulated instead of actually crashing the machine?**
Safety and reliability. Really exhausting memory, burning CPU, filling the disk or deadlocking threads could freeze the laptop during a live demo, and the results would not be reproducible. The simulation models the same OS behaviour in a controlled way, while real host metrics provide the baseline.

**14. Which OS subsystem is responsible for each failure?**

| Failure | OS subsystem |
|---|---|
| CPU saturation | CPU scheduler / processor management |
| Memory thrashing | Memory management / virtual memory |
| Disk bottleneck | I/O management / disk scheduling |
| Process crash | Process management |
| Deadlock | Process synchronization / resource allocation |
| Network failure | Networking / communication |

**15. What is a cascading failure, and how does the lab tell primary from secondary?**
A failure in one subsystem pushes another past its limit (thrashing → swap I/O → disk bottleneck). Each node records the contribution of each input; at the moment a rule first fires, the lab checks whether its change is dominated by another active failure (e.g. ≥ 30% of disk demand is swap I/O while memory is failing). If so it is SECONDARY; otherwise it is a root. Among roots, the one that reached a failure level first is the PRIMARY failure. Feedback loops are broken at the subsystem that failed first.

**16. What is a metastable (self-sustaining) failure?**
A failure that persists after its trigger is removed because of positive feedback — e.g. slow responses → clients retry → more load → slower responses. The lab flags it when the trigger has been reduced but the server stays failed for 20 s, and names the loop to break.

**17. Why is Little's law important here?**
L = λ·W: the number of requests in flight equals arrival rate × time in the system. When latency rises, in-flight requests (and the memory they hold) rise with it — that is how a CPU problem becomes a memory problem.

## 12. Architectural Reality & Limitations (honest)

- **Dual-Engine Architecture:** The active runtime engine is strictly `simulator/lab/engine.py` (`LabEngine`), instantiated once per server by `ServerManager`. Legacy files in the repository root (`simulator/engine.py`, `simulator/failure_simulator.py`) belong to Version 1; they are retained solely for backward compatibility when reading historically recorded runs via `/api/meta` and `/api/experiments`.
- **Pedagogical Simplifications:**
  - **Memory Thrashing Threshold:** The working-set thrashing point at $ws > 80\%$ is an intentional educational threshold to demonstrate page-fault explosions without requiring complex kernel zone watermarks (`min_free_kbytes`).
  - **No Autonomous Kernel OOM-Killer:** The simulation intentionally does not kill processes automatically under extreme memory pressure. This design allows students to observe sustained thrashing and examine the resulting cascade into swap and disk I/O. Memory reclamation is initiated through operator intervention (killing the hog process) or automated Recovery.
- **Queueing Models:** CPU and disk queues use standard queueing formulas (Sakasegawa $M/M/c$, multi-channel NVMe queueing) with bounded backlogs rather than tick-by-tick microarchitectural event simulation.
- **Network Scope:** Retransmission delay models single client-server round trips with exponential backoff and timeout bounding; it does not simulate topological BGP routing or full congestion window (CWND) dynamics.
- **Multi-Server Isolation:** Virtual servers maintain strictly segregated state, process tables, detection rules, and event histories in independent `LabEngine` instances.

