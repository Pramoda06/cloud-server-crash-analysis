# Data sources and provenance

This is an audit of where every number in the application comes from, written after reading the
code (not the screen). Short version: **almost everything is simulated or derived; nothing is a
production measurement; no external dataset is used.**

## 1. What is real

| Source | Code | What it reads | Where it is used |
|---|---|---|---|
| `psutil` host telemetry | `simulator/metrics.py` → `HostMonitor.sample()` | this computer's CPU %, memory %, process count, core count, disk and network byte rates (read-only) | Sampled every 0.5 s by `ServerManager`. **Used only when the `anchor` setting is on** (`model.anchor`), and then only to nudge the simulated *baseline* CPU (≤ 15 % of the cores) and memory (≤ 10 % of RAM) — those values are tagged **HYBRID**. The anchor is off by default; it is switched in the System Lab → Advanced → *Model settings* (or `POST /api/lab/settings {"anchor": true}`). |
| SQLite database | `simulator/storage.py`, `database/experiments.db` | stores servers, recorded runs, snapshots, saved programs, recovery comparisons and kernel experiments | Experiment History. It is persistence, not a measurement. |

Nothing else reads the host. No CPU is burned, no memory allocated, no disk written for load,
no network shaped, no signal sent, no lock taken, no kernel touched.

## 2. What is simulated (state the model keeps)

* **Operator controls** — workload, memory pressure, disk read/write IOPS, request rate, added delay, loss, connection failures, timeout, nice (`graph.controls_spec`). Tagged *CONTROL*.
* **Process table** — names, PIDs, states (NEW / READY / RUNNING / WAITING / BLOCKED / SLEEPING / HUNG / RESTARTING / TERMINATED), parent/child links, exit codes. *Simulated* process states, not Linux `/proc` states.
* **Lock / resource table** — resources with N instances, allocations, FIFO queues, maximum claims.
* **Queues** — CPU run queue, disk backlog, accept backlog (carried from sample to sample).
* **File-system space** — % used (62 % at boot + log files written by *log flood* processes, accelerated fill). Not this computer's disk.
* **Kernel state** (Kernel Failure Lab) — stage of the panic/reboot lifecycle and its timeline.

## 3. What is derived (formulas, every 0.5 s)

Everything else: CPU utilization and demand, run queue, waiting time, working set, page faults,
paging overhead, swap I/O, disk utilization, I/O queue, latency, RTT, retransmission delay,
timeouts, error rate, throughput, availability, in-flight requests, lock wait. The exact rule for
each one is in `simulator/lab/graph.py` (shown in the UI under *Simulation details*) and the code
in `simulator/lab/model.py`. Each node also records its **contribution parts**, which is what the
causal trace walks.

## 4. Randomness

One seeded generator (`SystemModel.rng`, default seed 2026) draws a fixed number of Gaussian
values per sample for small, smoothed noise on CPU, RTT, request rate and memory. With the anchor
off, the same seed and the same control timeline reproduce a run sample for sample (used by
Replay and by the Recovery Lab comparisons).

## 5. Thresholds

Detection thresholds (`simulator/lab/detect.py`) are **configured simulation rules** chosen for
teaching, not universal limits. A CPU at 95 % is not "crashed"; it is a rule that says the
simulated server is CRITICAL for CPU saturation.

## 6. Labels in the UI

| Tag | Meaning |
|---|---|
| CONTROL | a value you set |
| SIMULATED | state kept by the model (process / lock tables, injected path conditions) |
| DERIVED | computed by a formula from other values |
| HYBRID | baseline anchored to a real psutil reading + simulated load (anchor on only) |
| REAL | read from this computer (not shown anywhere by default) |
| HISTORICAL DATASET | a real, documented record from a public dataset (Real Kernel Bugs page only — never fed into the simulator) |

The Incident Analysis also tags each part of an explanation: **observed** (a value or event the
simulation recorded), **simulated** (a relationship implemented by the model), **inferred**
(attribution chosen from contribution shares — a diagnostic hypothesis), **confirmed** (passed the
detector's 2-sample confirmation or the recovery checks).

## 7. External datasets

One, used only for reference: a deterministic subset of **one of eleven SyzFix shards** shown on the
**Real Kernel Bugs** page (`/cases`). It is never fed into the simulator. Licence review, the two-tier
storage (committed metadata + links vs local-only text) and the selection method are in
[SYZFIX.md](SYZFIX.md). The legacy `experiments` table holds results recorded by version 1 of this app.
Other candidates are compared in [DATASETS.md](DATASETS.md); none of them is used.
