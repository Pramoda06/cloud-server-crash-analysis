"""
The System Lab dependency graph — metadata only.

Every quantity the lab shows is a NODE. A node has
  type       INPUT (operator control) | SIMULATED (simulated state) |
             DERIVED (formula of other nodes) | HYBRID (real host reading + simulation) |
             REAL (read from the host OS)
  deps       the nodes its value is computed from (edges of the causal graph)
  rule       the exact rule model.py uses to compute it (shown in Simulation Details)
  why        the operating-system explanation

The VALUES and the per-dependency CONTRIBUTIONS ("parts") are produced every tick by
model.py; trace.py walks these parts backwards to answer "why did this change?".
"""

# --------------------------------------------------------------- controls ---
def controls_spec(server):
    """Operator controls. Ranges that depend on the server (IOPS, request rate) are scaled to it."""
    cap = int(server["disk_iops_capacity"])
    base_req = int(round(((server.get("profile") or {}).get("offered_load", 860)) / 10.0) * 10)
    return [
        # CPU
        {"key": "cpu_workload", "label": "CPU workload", "group": "cpu", "unit": "%", "min": 0, "max": 150, "step": 1,
         "default": 10, "basic": True,
         "help": "Batch CPU demand as a share of total CPU capacity (all vCPUs). Above 100% there is more "
                 "work than the CPU can execute."},
        {"key": "cpu_procs", "label": "CPU-bound processes", "group": "cpu", "unit": "procs", "min": 1, "max": 32,
         "step": 1, "default": 8, "lag": False,
         "help": "Number of batch processes sharing the workload. One process can use at most one vCPU, so "
                 "too few processes cannot saturate a multi-core CPU."},
        {"key": "web_nice", "label": "Web-service priority (nice)", "group": "cpu", "unit": "", "min": -20, "max": 19,
         "step": 1, "default": 0, "lag": False,
         "help": "nice value of the web-service processes (batch jobs stay at 0). Lower nice = larger CFS "
                 "weight = less CPU waiting for requests."},
        # memory
        {"key": "mem_demand", "label": "Memory pressure", "group": "memory", "unit": "% RAM", "min": 0, "max": 100,
         "step": 1, "default": 0, "basic": True,
         "help": "Extra memory allocated by memory-hungry processes, as a share of physical RAM, on top of "
                 "the operating system and services."},
        {"key": "mem_procs", "label": "Memory-hungry processes", "group": "memory", "unit": "procs", "min": 1,
         "max": 8, "step": 1, "default": 2, "lag": False,
         "help": "How many processes share that allocation (allocation per process = pressure ÷ processes)."},
        # disk
        {"key": "disk_read", "label": "Read workload", "group": "disk", "unit": "IOPS", "min": 0,
         "max": int(cap * 1.5), "step": 50, "default": 0,
         "help": f"Background read requests per second (scans, backups). Device capacity: {cap} IOPS."},
        {"key": "disk_write", "label": "Write workload", "group": "disk", "unit": "IOPS", "min": 0,
         "max": int(cap * 1.5), "step": 50, "default": 0,
         "help": f"Background write requests per second (logs, backups). Device capacity: {cap} IOPS."},
        # network / traffic
        {"key": "req_rate", "label": "Client request rate", "group": "network", "unit": "req/s", "min": 0,
         "max": base_req * 4, "step": 10, "default": base_req, "basic": True,
         "help": "Requests per second sent by clients. Each request costs CPU time, disk reads and memory "
                 "while it is in flight."},
        {"key": "net_delay", "label": "Network latency (added)", "group": "network", "unit": "ms", "min": 0,
         "max": 1500, "step": 10, "default": 0, "basic": True,
         "help": "Extra round-trip delay on the client ↔ server path (like Linux tc netem delay)."},
        {"key": "net_loss", "label": "Packet loss", "group": "network", "unit": "%", "min": 0, "max": 100,
         "step": 0.5, "default": 0, "basic": True,
         "help": "Share of packets dropped on the path. TCP retransmits lost packets after a timeout (RTO) "
                 "that doubles on every retry."},
        {"key": "conn_fail", "label": "Connection failures", "group": "network", "unit": "%", "min": 0, "max": 100,
         "step": 1, "default": 0,
         "help": "Share of connection attempts refused or reset before reaching the server."},
        {"key": "timeout_ms", "label": "Client timeout", "group": "network", "unit": "ms", "min": 250, "max": 5000,
         "step": 50, "default": 2000, "lag": False,
         "help": "Clients abandon a request that takes longer than this."},
    ]


# a single "Disk I/O" slider for Basic mode drives both read and write workload
BASIC_DISK = {"key": "disk_io", "label": "Disk I/O load", "unit": "% of capacity", "min": 0, "max": 130, "step": 1,
              "write_share": 0.65,
              "help": "Background disk requests as a share of the device's IOPS capacity (65% writes, 35% reads)."}

# --------------------------------------------------------------- nodes ---
# (id, label, unit, type, subsystem, deps, rule, why)
_N = [
    # ---- inputs
    ("cpu_workload", "CPU workload", "%", "INPUT", "cpu", [], "operator control", "Batch CPU demand requested by the operator."),
    ("cpu_procs", "CPU-bound processes", "procs", "INPUT", "cpu", [], "operator control", "Number of batch processes."),
    ("web_nice", "Web priority (nice)", "", "INPUT", "cpu", [], "operator control", "Priority of the web service."),
    ("mem_demand", "Memory pressure", "% RAM", "INPUT", "memory", [], "operator control", "Extra allocation by memory-hungry processes."),
    ("disk_read", "Read workload", "IOPS", "INPUT", "disk", [], "operator control", "Background read requests."),
    ("disk_write", "Write workload", "IOPS", "INPUT", "disk", [], "operator control", "Background write requests."),
    ("req_rate", "Client request rate", "req/s", "INPUT", "service", [], "operator control", "Client traffic."),
    ("net_delay", "Added latency", "ms", "INPUT", "network", [], "operator control", "Injected path delay."),
    ("net_loss", "Packet loss (injected)", "%", "INPUT", "network", [], "operator control", "Injected packet loss."),
    ("conn_fail", "Connection failures", "%", "INPUT", "network", [], "operator control", "Refused / reset connections."),
    ("timeout_ms", "Client timeout", "ms", "INPUT", "network", [], "operator control", "Client patience."),
    ("procs_table", "Process table", "", "SIMULATED", "process", [], "simulated process states (Process Lab)",
     "Which service processes are alive determines how many requests the server can serve."),
    ("locks", "Lock table", "", "SIMULATED", "deadlock", [], "simulated resource locks (Deadlock Lab)",
     "Requests that touch a locked resource must wait for its holder."),

    # ---- traffic
    ("req_eff", "Effective arrivals", "req/s", "DERIVED", "service", ["req_rate", "error_rate"],
     "λ_eff = λ + retries;  retries = 0.8 · λ · error_rate(previous sample)   [cascade link]",
     "Clients retry failed requests, so failures generate extra load — a retry storm."),
    # ---- memory
    ("inflight", "Requests in flight", "req", "DERIVED", "service", ["req_eff", "response_ms", "accept_backlog"],
     "Little's law: L = min(served · response_time, live workers × 256) + accept backlog",
     "Every in-flight request holds a worker buffer in memory."),
    ("mem_ws", "Working set", "% RAM", "DERIVED", "memory", ["mem_demand", "inflight", "procs_table"],
     "WS = OS & services (~38% RAM) + memory pressure + 1 MB × in-flight requests [cascade] + created processes",
     "The working set is the memory processes actively use. It must fit in physical RAM."),
    ("mem_util", "Memory utilization", "%", "DERIVED", "memory", ["mem_ws"], "min(WS, 99.5%)",
     "Share of physical RAM in use."),
    ("mem_avail", "Available memory", "MB", "DERIVED", "memory", ["mem_ws"], "RAM − WS (floor 0.5% RAM)",
     "Free page frames the kernel can still hand out."),
    ("page_faults", "Page fault rate", "/s", "DERIVED", "memory", ["mem_ws"],
     "over = (WS − 80%)/20%;  faults = 35 + 9000·over^2.2 (+12000·(over−1) beyond RAM)",
     "Below ~80% of RAM page faults stay at a background rate. Above it the kernel evicts pages that are "
     "soon needed again, so major faults climb steeply."),
    ("paging_overhead", "Paging overhead", "% CPU", "DERIVED", "memory", ["page_faults"],
     "70 · over^1.8 % of CPU time (kswapd reclaim + fault handling)",
     "Time the CPU spends moving pages instead of running applications — the signature of thrashing."),
    ("swap_iops", "Swap I/O", "IOPS", "DERIVED", "memory", ["page_faults"], "faults × 0.25 (page-ins needing disk)",
     "Major faults read pages back from the swap device, adding disk requests [cascade memory → disk]."),
    # ---- cpu
    ("cpu_demand", "CPU demand", "% capacity", "DERIVED", "cpu",
     ["cpu_workload", "cpu_procs", "req_eff", "paging_overhead", "procs_table"],
     "ρ = [min(workload·cores, batch procs) + λ·0.55 ms + paging overhead·cores [cascade] + created procs + 3% system] / cores",
     "Total runnable work offered to the scheduler per second, relative to what the vCPUs can execute."),
    ("cpu_util", "CPU utilization", "%", "DERIVED", "cpu", ["cpu_demand"], "min(ρ, 100%) ± 0.5 noise",
     "Busy time of the vCPUs. It cannot exceed 100% — excess demand waits in the run queue instead."),
    ("run_queue", "CPU run queue", "procs", "DERIVED", "cpu", ["cpu_demand"],
     "ρ<0.98: relaxes (τ 1 s) to Lq = ρ^√(2(c+1)) / (1−ρ)  [Sakasegawa M/M/c];  ρ≥0.98: grows by (ρ−0.98)·c / 50 ms per second",
     "Runnable processes waiting for a core. Near 100% utilization the queue grows non-linearly; above it "
     "the backlog keeps growing every second."),
    ("cpu_wait", "CPU waiting time", "ms", "DERIVED", "cpu", ["run_queue"], "run_queue × 50 ms average CPU burst ÷ cores",
     "How long a runnable process waits before it gets a core."),
    ("cpu_wait_web", "Request CPU wait", "ms", "DERIVED", "cpu", ["cpu_wait", "web_nice"],
     "cpu_wait × 2·w_batch/(w_batch + w_web),  w = 1024 · 1.25^(−nice)  [CFS weights]",
     "CFS shares the CPU in proportion to weights; a higher-priority web service waits less."),
    # ---- disk
    ("disk_demand", "Disk demand", "IOPS", "DERIVED", "disk", ["disk_read", "disk_write", "req_eff", "swap_iops", "procs_table"],
     "background read + write + λ·0.57 (requests) + swap I/O [cascade] + created I/O processes",
     "All read/write requests offered to the block device per second."),
    ("disk_util", "Disk utilization", "%", "DERIVED", "disk", ["disk_demand"], "min(demand / capacity, 100%)",
     "Share of time the device is busy."),
    ("io_queue", "I/O queue length", "reqs", "DERIVED", "disk", ["disk_demand"],
     "ρ<0.98: Lq = ρ²/(1−ρ)  [M/M/1];  ρ≥0.98: backlog grows by (demand − capacity) per second, drains when demand falls",
     "Requests waiting in the block-layer queue."),
    ("disk_latency", "Disk latency", "ms", "DERIVED", "disk", ["io_queue"],
     "S/(1−ρ) + backlog/capacity,  S = device service time (NVMe 2.4 ms, SSD 3 ms, HDD 8 ms)",
     "Time per I/O including queueing. It explodes as utilization approaches 100%."),
    ("iowait_procs", "Processes in I/O wait", "procs", "DERIVED", "disk", ["io_queue"], "min(5, io_queue / 2)",
     "Processes sleeping (WAITING) until their I/O completes."),
    ("disk_used", "File system used", "%", "SIMULATED", "disk", ["procs_table"],
     "62% at boot + log files written by created log writers (1% of the disk per second, accelerated), max 100%",
     "Capacity, not speed: when the file system is full, every new write fails at once with ENOSPC "
     "(\"No space left on device\"). Disk latency does not rise. Deleting a file frees its blocks only "
     "when no process still has it open."),
    ("disk_free_gb", "Free disk space", "GB", "DERIVED", "disk", ["disk_used"], "(100% − used) × file-system size",
     "Space left for new data; at 0 GB every write fails."),
    # ---- network
    ("rtt", "Network latency (RTT)", "ms", "DERIVED", "network", ["net_delay"], "base RTT + added latency ± jitter",
     "Round-trip time between clients and the server."),
    ("packet_loss", "Packet loss", "%", "DERIVED", "network", ["net_loss"], "base loss + injected loss",
     "Share of packets that never arrive."),
    ("retrans_ms", "Retransmission delay", "ms", "DERIVED", "network", ["packet_loss", "rtt"],
     "Σ over retry counts k (request & reply): p·RTO·(2^k − 1),  RTO = max(200 ms, 2·RTT)",
     "TCP waits a retransmission timeout before resending a lost packet, doubling it each time."),
    # ---- locks
    ("lock_wait", "Lock wait", "ms", "DERIVED", "deadlock", ["locks", "timeout_ms"],
     "35% of requests touch Resource A/B: +50 ms if a running process holds the lock; until the client "
     "timeout if the holder is deadlocked",
     "Requests needing a locked resource must wait for the holder to release it."),
    # ---- service
    ("capacity", "Service capacity", "req/s", "DERIVED", "service", ["procs_table", "cpu_demand", "page_faults"],
     "alive workers × 2·λ_base/4 × min(1, 1/ρ) × (1 − 0.85·over²); 0 if web-master or database is down",
     "How many requests per second the live worker processes can complete."),
    ("accept_backlog", "Request backlog", "req", "DERIVED", "service", ["req_eff", "capacity"],
     "B += (arrivals − capacity)·dt, 0 ≤ B ≤ 1024 (listen backlog); arrivals beyond a full backlog are refused",
     "Requests waiting for a free worker — the accept queue."),
    ("queue_wait", "Backlog wait", "ms", "DERIVED", "service", ["accept_backlog", "capacity"], "B / capacity (Little's law)",
     "Time a request waits for a worker."),
    ("paging_penalty", "Page-fault stalls", "ms", "DERIVED", "memory", ["page_faults", "disk_latency"],
     "600 · over^2.5 × (disk_latency / S) [cascade]",
     "Requests stall while their pages are read back from swap."),
    ("service_ms", "Server processing time", "ms", "DERIVED", "service",
     ["cpu_wait_web", "disk_latency", "paging_penalty", "lock_wait", "queue_wait"],
     "22 ms work + request CPU wait + 2 × disk latency + page-fault stalls + lock wait + backlog wait",
     "Time spent inside the server."),
    ("response_ms", "Response latency", "ms", "DERIVED", "service", ["service_ms", "rtt", "retrans_ms"],
     "min(client timeout, server processing time + RTT + retransmission delay)",
     "What a client experiences end to end (bounded by client timeout)."),
    ("timeouts", "Timeouts", "/s", "DERIVED", "service", ["response_ms", "timeout_ms"],
     "served × P(response > timeout): service time ~ exponential(mean), network delay enumerated over retransmissions",
     "Requests that take longer than the client is willing to wait."),
    ("error_rate", "Error rate", "%", "DERIVED", "service", ["timeouts", "accept_backlog", "conn_fail", "lock_wait", "capacity",
                                                           "disk_used"],
     "1 − successful / attempts  (timeouts + refused + connection failures + deadlocked lock waits + "
     "writes refused by a full disk (39% of requests write))",
     "Share of request attempts that fail."),
    ("throughput", "Throughput", "req/s", "DERIVED", "service", ["req_eff", "error_rate"], "successful requests per second",
     "Useful work completed."),
    ("availability", "Service availability", "%", "DERIVED", "service", ["error_rate"], "100 − error rate",
     "Share of requests that succeed."),
    # ---- process counters
    ("running_procs", "Running processes", "procs", "SIMULATED", "process", ["procs_table", "cpu_demand"],
     "processes in RUNNING or READY state", "Processes able to run."),
    ("blocked_procs", "Blocked processes", "procs", "SIMULATED", "process", ["locks", "iowait_procs"],
     "processes WAITING on I/O or BLOCKED on a lock", "Processes that cannot make progress right now."),
]

NODES = {}
for nid, label, unit, typ, sub, deps, rule, why in _N:
    NODES[nid] = {"id": nid, "label": label, "unit": unit, "type": typ, "subsystem": sub, "deps": deps,
                  "rule": rule, "why": why, "affects": []}
for n in NODES.values():
    for d in n["deps"]:
        NODES[d]["affects"].append(n["id"])

# dependency edges that only exist when cross-subsystem coupling ("cascades") is on
CASCADE_EDGES = {("error_rate", "req_eff"), ("inflight", "mem_ws"), ("paging_overhead", "cpu_demand"),
                 ("swap_iops", "disk_demand"), ("disk_latency", "paging_penalty")}

SUBSYSTEMS = {
    "cpu": "CPU scheduling / processor management",
    "memory": "Memory management / virtual memory",
    "disk": "I/O management / disk scheduling",
    "network": "Networking / TCP",
    "service": "Service level (requests, workers, backlog)",
    "process": "Process management",
    "deadlock": "Process synchronization / resource allocation",
}


def layers():
    """Longest-path layer for drawing the graph left → right (cycles via cascade edges ignored)."""
    out = {}

    def depth(n, seen=()):
        if n in out:
            return out[n]
        ds = [d for d in NODES[n]["deps"] if (d, n) not in CASCADE_EDGES and d not in seen]
        out[n] = 0 if not ds else 1 + max(depth(d, seen + (n,)) for d in ds)
        return out[n]

    for n in NODES:
        depth(n)
    return out
