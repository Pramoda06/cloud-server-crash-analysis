"""
Catalogue of the six failure scenarios: OS subsystem, concept, expected effect,
metrics to watch, and the explanation texts used by the analysis panel.
"""

SCENARIOS = {
    "cpu": {
        "id": "cpu",
        "name": "CPU Saturation",
        "short": "Too many runnable processes compete for a limited number of CPU cores.",
        "root_cause": "Excessive processor demand caused CPU utilization and process waiting time to exceed defined thresholds.",
        "live_charts": ["cpu", "cpu_queue", "wait_ms"],
        "subsystem": "CPU Scheduling / Processor Management",
        "concept": "Excessive processor demand: more runnable processes than CPU "
                   "cores, so the scheduler's ready queue grows.",
        "expected_effect": "CPU utilization approaches 100%, the run queue and process "
                           "waiting time grow non-linearly, and response latency rises.",
        "primary_metrics": ["cpu", "cpu_queue", "wait_ms", "response_ms", "proc_count"],
        "cards": ["cpu_queue", "wait_ms", "ctx_switches", "throughput"],
        "chart": {"title": "Run Queue & Waiting Time",
                  "series": [["cpu_queue", "Run queue (procs)"], ["wait_ms", "Waiting time (ms)"]]},
        "peak_metric": ["cpu_queue", "max"],
        "detection": "CPU utilization compared against WARNING / DEGRADED / FAILED "
                     "thresholds (85 / 92 / 97 %).",
        "explanation": "Excessive processor demand caused CPU utilization to exceed the "
                      "configured threshold. With more runnable processes than cores, the "
                      "scheduler's ready queue grew and processes spent longer waiting for "
                      "CPU time, reducing system responsiveness.",
        "recovery": "Runaway batch processes were identified from the run queue, "
                    "de-prioritised (nice +19) and terminated; CPU demand fell back below "
                    "capacity and the run queue drained.",
        "prevention": "CPU quotas (cgroups), priority scheduling for critical services, "
                      "and horizontal scaling / load shedding.",
    },
    "memory": {
        "id": "memory",
        "name": "Memory Exhaustion & Thrashing",
        "short": "The working set no longer fits in RAM, so the system spends its time paging.",
        "root_cause": "Excessive memory pressure increased page faults and paging activity, reducing useful processing and causing thrashing.",
        "live_charts": ["mem", "page_faults", "swap_io"],
        "subsystem": "Memory Management / Virtual Memory",
        "concept": "The combined working set of processes exceeds physical RAM, so the "
                   "kernel constantly pages memory in and out of swap.",
        "expected_effect": "High memory pressure: available memory falls, page faults and "
                           "swap activity rise sharply, latency up, throughput down.",
        "primary_metrics": ["mem", "mem_avail_mb", "page_faults", "swap_io", "response_ms", "throughput"],
        "cards": ["mem_avail_mb", "page_faults", "swap_io", "throughput"],
        "chart": {"title": "Page Faults & Swap Activity",
                  "series": [["page_faults", "Page faults (/s)"], ["swap_io", "Swap I/O (MB/s)"]]},
        "peak_metric": ["page_faults", "max"],
        "detection": "Memory utilization AND page-fault rate must both exceed their "
                     "thresholds (85% & 500/s → 90% & 1500/s → 95% & 4000/s).",
        "explanation": "Excessive memory pressure caused a high page-fault rate and "
                      "increased paging activity. The working set no longer fitted in RAM, "
                      "so the system began thrashing: it spent more time servicing page "
                      "faults than executing useful work.",
        "recovery": "The process with the largest resident set (cache-loader) was "
                    "terminated, OOM-killer style; freed frames returned to the free list "
                    "and paging activity fell back to baseline.",
        "prevention": "Memory limits per process/cgroup, a correctly sized swap, "
                      "working-set-aware admission control and more RAM.",
        "thrashing": "Thrashing: the operating system spends excessive time servicing "
                     "page faults and memory-management operations instead of executing "
                     "useful processes.",
    },
    "disk": {
        "id": "disk",
        "name": "Disk I/O Bottleneck",
        "short": "Read/write demand exceeds disk capacity and requests pile up in the I/O queue.",
        "root_cause": "Excessive I/O demand caused the disk queue and I/O latency to increase.",
        "live_charts": ["disk", "disk_latency", "io_queue"],
        "subsystem": "I/O Management / Disk Scheduling",
        "concept": "Disk read/write demand approaches the device's IOPS capacity, so "
                   "requests wait in the block-layer I/O queue.",
        "expected_effect": "Disk utilization, I/O queue length and disk latency rise; "
                           "application response time increases while CPU stays moderate.",
        "primary_metrics": ["disk", "read_iops", "write_iops", "io_queue", "disk_latency", "response_ms"],
        "cards": ["read_iops", "write_iops", "io_queue", "disk_latency"],
        "chart": {"title": "I/O Queue & Disk Latency",
                  "series": [["io_queue", "I/O queue (reqs)"], ["disk_latency", "Disk latency (ms)"]]},
        "peak_metric": ["io_queue", "max"],
        "detection": "I/O queue length OR disk latency above threshold "
                     "(4 / 8 / 16 requests, 20 / 50 / 100 ms).",
        "explanation": "High disk I/O demand caused the I/O queue and disk latency to "
                      "exceed acceptable limits. Requests waited in the device queue, so "
                      "processes blocked on I/O and application response time increased "
                      "even though the CPU was not saturated.",
        "recovery": "The I/O-heavy backup job was throttled (ionice idle class) and the "
                    "log archiver paused; the device queue drained and latency returned to "
                    "baseline.",
        "prevention": "I/O scheduling priorities, rate-limiting batch jobs, caching, "
                      "and faster / striped storage.",
    },
    "process": {
        "id": "process",
        "name": "Critical Process Crash",
        "short": "A critical service process terminates unexpectedly.",
        "root_cause": "A critical service process transitioned from RUNNING to TERMINATED, causing service unavailability.",
        "live_charts": ["availability", "running_procs", "req_failed"],
        "subsystem": "Process Management",
        "concept": "A critical server process terminates unexpectedly "
                   "(RUNNING → TERMINATED) even though hardware resources are healthy.",
        "expected_effect": "CPU, memory and disk remain normal, but service availability "
                           "drops and client requests fail.",
        "primary_metrics": ["web_state", "running_procs", "terminated_procs", "availability", "restart_status", "response_ms"],
        "cards": ["web_state", "terminated_procs", "availability", "restart_status"],
        "chart": {"title": "Service Availability & Running Processes",
                  "series": [["availability", "Availability (%)"], ["running_procs", "Running processes"]]},
        "peak_metric": ["availability", "min"],
        "detection": "Process-table check: critical process state == TERMINATED → FAILED; "
                     "service availability below 100% / 50% → WARNING / DEGRADED.",
        "explanation": "The critical Web-Service process transitioned from RUNNING to "
                      "TERMINATED. The server's resources were healthy, but with the "
                      "process gone no program was serving requests, so availability "
                      "collapsed.",
        "recovery": "The service supervisor restarted Web-Service: TERMINATED → "
                    "RESTARTING → RUNNING with a new PID, and availability returned to 100%.",
        "prevention": "Supervisor auto-restart policies (systemd Restart=on-failure), "
                      "redundant replicas behind a load balancer, health checks.",
    },
    "deadlock": {
        "id": "deadlock",
        "name": "Process Deadlock",
        "short": "Two processes wait indefinitely for resources held by each other.",
        "root_cause": "Two processes became involved in a circular wait while holding resources required by each other.",
        "live_charts": ["waiting_procs", "blocked_procs", "response_ms"],
        "subsystem": "Process Synchronization / Resource Allocation",
        "concept": "Processes each hold a resource while waiting for a resource held by "
                   "another, forming a circular wait that none can break.",
        "expected_effect": "Circular wait between processes: they remain WAITING forever, "
                           "others block behind the locks, progress stops, CPU drops.",
        "primary_metrics": ["blocked_procs", "waiting_procs", "locks_held", "deadlock", "response_ms"],
        "cards": ["waiting_procs", "blocked_procs", "locks_held", "deadlock"],
        "chart": {"title": "Waiting & Blocked Processes",
                  "series": [["waiting_procs", "Waiting (in cycle)"], ["blocked_procs", "Blocked (queued on locks)"]]},
        "peak_metric": ["stuck_procs", "max"],
        "detection": "Wait-for graph built from the process/resource tables; a cycle "
                     "that persists > 3 s means DEADLOCK. Severity from the number of "
                     "processes unable to progress (2 / 4 / 6).",
        "explanation": "Two or more processes entered circular wait while holding mutually "
                      "required resources. All four Coffman conditions held, so no process "
                      "could proceed and work queued behind the locked resources.",
        "recovery": "Deadlock recovery by process termination: a victim with the least "
                    "progress was selected, terminated and rolled back, releasing its "
                    "lock; the other process continued and the victim was restarted.",
        "prevention": "Break circular wait with a global lock ordering (always A before B), "
                      "lock timeouts, or Banker's-algorithm style avoidance.",
    },
    "network": {
        "id": "network",
        "name": "Network / Service Communication Failure",
        "short": "Latency and packet loss break communication between clients and the server.",
        "root_cause": "High latency, packet loss, or communication failures prevented successful service requests.",
        "live_charts": ["net_latency", "packet_loss", "req_failed"],
        "subsystem": "OS Networking / Inter-process & Network Communication",
        "concept": "The server is healthy internally, but the communication path to "
                   "clients degrades (latency, packet loss, timeouts).",
        "expected_effect": "Latency and packet loss increase, requests time out and "
                           "reachability falls, while CPU and memory stay normal.",
        "primary_metrics": ["net_latency", "packet_loss", "req_ok", "req_failed", "timeouts", "reachability"],
        "cards": ["packet_loss", "req_failed", "timeouts", "reachability"],
        "chart": {"title": "Packet Loss & Reachability",
                  "series": [["packet_loss", "Packet loss (%)"], ["reachability", "Reachability (%)"]]},
        "peak_metric": ["packet_loss", "max"],
        "detection": "Network latency OR packet loss OR reachability beyond threshold "
                     "(150/400/1000 ms, 2/10/30 %, <95/80/50 %).",
        "explanation": "Network latency and packet loss caused repeated service "
                      "communication failures. TCP retransmissions and client timeouts "
                      "made the service unreachable even though CPU, memory and processes "
                      "inside the server were healthy.",
        "recovery": "Traffic was failed over from the faulty interface/route (eth0) to a "
                    "healthy one (eth1) and stale TCP connections were reset; latency and "
                    "loss returned to baseline.",
        "prevention": "Redundant network paths (NIC bonding), health-checked load "
                      "balancers, retries with back-off, and timeouts tuned per service.",
    },
}

ORDER = ["cpu", "memory", "disk", "process", "deadlock", "network"]

# ---------------------------------------------------------------------------
# INTERACTIVE MODE (dynamic metric control)
#
# In interactive mode the operator drives the failure condition directly with
# these CONTROL INPUTS instead of choosing a preset severity. Each control is
# read by the scenario's model in failure_simulator.py, which turns it into
# simulated drivers (CPU demand, working set, I/O demand, network delay/loss,
# process / lock table). derive() then computes every observed metric from the
# drivers, exactly as in preset mode, so both modes share one simulation.
#
#   key       name used by the model and the API (/api/control)
#   primary   the control whose peak is stored as "maximum intensity"
# ---------------------------------------------------------------------------
CONTROLS = {
    "cpu": [{
        "key": "load", "label": "CPU Load Intensity", "min": 0, "max": 100, "step": 1, "unit": "%",
        "primary": True,
        "help": "Extra CPU demand from the simulated load generator (analytics-job processes), "
                "as a share of the processor capacity left free at baseline. 100% = demand equals "
                "the full capacity of all vCPUs.",
    }],
    "memory": [{
        "key": "pressure", "label": "Memory Pressure", "min": 0, "max": 100, "step": 1, "unit": "%",
        "primary": True,
        "help": "How far cache-loader grows the combined working set from its baseline size towards "
                "the full size of physical RAM. 100% = working set equals installed RAM.",
    }],
    "disk": [{
        "key": "io_load", "label": "Disk I/O Load", "min": 0, "max": 100, "step": 1, "unit": "%",
        "primary": True,
        "help": "Read/write requests issued by backup-job and log-archiver, as a share of the "
                "device's IOPS capacity left free at baseline (65% writes / 35% reads).",
    }],
    "network": [
        {"key": "delay", "label": "Added Network Delay", "min": 0, "max": 2500, "step": 10, "unit": "ms",
         "primary": True,
         "help": "Extra round-trip delay injected on the simulated eth0 path "
                 "(the same knob as Linux `tc netem delay`)."},
        {"key": "loss", "label": "Packet Loss", "min": 0, "max": 60, "step": 0.5, "unit": "%",
         "help": "Share of packets dropped on the simulated path (like `tc netem loss`). "
                 "Requests need both the request and the response to arrive."},
    ],
    "process": [],
    "deadlock": [
        {"key": "queued", "label": "Clients Queued on the Locks", "min": 0, "max": 6, "step": 1, "unit": "procs",
         "primary": True,
         "help": "Additional transaction workers that request Resource A or B. While a lock is "
                 "held they are BLOCKED behind it; this is the contention that turns a deadlock "
                 "from WARNING into DEGRADED / FAILED."},
    ],
}

# discrete actions (scenarios that are state machines, not percentages)
ACTIONS = {
    "process": [
        {"id": "crash_worker", "label": "Crash a worker", "kind": "danger",
         "help": "Deliver SIGSEGV to the next running web-worker."},
        {"id": "crash_master", "label": "Crash web-service (master)", "kind": "danger",
         "help": "Deliver SIGSEGV to the critical master process; its children die with it."},
        {"id": "restart", "label": "Restart service", "kind": "success", "recovery": True,
         "help": "Supervisor restart: TERMINATED → RESTARTING → RUNNING with new PIDs."},
    ],
    "deadlock": [
        {"id": "step", "label": "Next allocation step", "kind": "danger",
         "help": "Advance the lock sequence one step towards circular wait."},
        {"id": "resolve", "label": "Resolve deadlock", "kind": "success", "recovery": True,
         "help": "Deadlock recovery: choose a victim, terminate and roll it back, release its lock."},
    ],
}

DEADLOCK_STEPS = [
    "P1 acquires Resource A (mutual exclusion)",
    "P2 acquires Resource B (mutual exclusion)",
    "P1 requests Resource B → WAITING (hold and wait)",
    "P2 requests Resource A → WAITING (circular wait)",
]

# metrics shown as live cards / charts in interactive mode ("control" = the input chart)
INTERACTIVE_VIEW = {
    "cpu": {"tiles": ["cpu", "cpu_queue", "wait_ms", "response_ms", "proc_count", "throughput"],
            "charts": ["control", "cpu", "cpu_queue", "wait_ms", "response_ms", "throughput"]},
    "memory": {"tiles": ["mem", "mem_avail_mb", "page_faults", "swap_io", "response_ms", "throughput"],
               "charts": ["control", "mem", "page_faults", "swap_io", "response_ms", "throughput"]},
    "disk": {"tiles": ["disk", "read_iops", "write_iops", "io_queue", "disk_latency", "response_ms"],
             "charts": ["control", "disk", "iops", "io_queue", "disk_latency", "response_ms"]},
    "network": {"tiles": ["net_latency", "packet_loss", "req_ok", "req_failed", "timeouts", "reachability"],
                "charts": ["control", "net_latency", "packet_loss", "req_failed", "timeouts", "reachability"]},
    "process": {"tiles": ["web_state", "running_procs", "terminated_procs", "availability", "restart_status", "response_ms"],
                "charts": ["availability", "running_procs", "terminated_procs", "req_failed", "response_ms", "cpu"]},
    "deadlock": {"tiles": ["deadlock", "waiting_procs", "blocked_procs", "locks_held", "throughput", "response_ms"],
                 "charts": ["control", "waiting_procs", "blocked_procs", "throughput", "response_ms", "cpu"]},
}

for _k in ORDER:
    SCENARIOS[_k]["controls"] = CONTROLS[_k]
    SCENARIOS[_k]["actions"] = ACTIONS.get(_k, [])
    SCENARIOS[_k]["interactive"] = INTERACTIVE_VIEW[_k]
SCENARIOS["deadlock"]["steps"] = DEADLOCK_STEPS


def primary_control(sid):
    for c in CONTROLS[sid]:
        if c.get("primary"):
            return c
    return None

SEVERITY_INFO = {
    "LOW": "Moderate deviation — expected to reach WARNING.",
    "MEDIUM": "Noticeable degradation — expected to reach DEGRADED.",
    "HIGH": "Severe degradation — expected to reach FAILED.",
    "CRITICAL": "Fast onset, deepest impact — FAILED state.",
    "INTERACTIVE": "No preset — the operator sets the intensity live with the experiment controls.",
}


def public_catalog():
    return [SCENARIOS[k] for k in ORDER]
