"""
Metric collection.

Two clearly separated sources:

1. HostMonitor  -> REAL metrics of the machine running this app (psutil).
2. derive()     -> the SIMULATED server metrics (Server-01, Server-02, ...), computed from a small set
                   of "drivers" (CPU demand, working-set size, I/O demand,
                   network conditions, process table) using simple
                   queueing / paging relationships, so every metric evolves
                   logically from its causes instead of being random.
"""
import math
import random
import time

from . import config

try:
    import psutil
except Exception:  # pragma: no cover - psutil missing
    psutil = None


# --------------------------------------------------------------- catalog ---
METRICS = {
    # core
    "cpu":            {"label": "CPU Utilization", "unit": "%", "dp": 1},
    "mem":            {"label": "Memory Utilization", "unit": "%", "dp": 1},
    "disk":           {"label": "Disk Utilization", "unit": "%", "dp": 1},
    "net_latency":    {"label": "Network Latency (RTT)", "unit": "ms", "dp": 0},
    "proc_count":     {"label": "Process Count", "unit": "", "dp": 0},
    "response_ms":    {"label": "Response Time", "unit": "ms", "dp": 0},
    # cpu
    "cpu_queue":      {"label": "CPU Run Queue", "unit": "procs", "dp": 1},
    "wait_ms":        {"label": "Process Waiting Time", "unit": "ms", "dp": 1},
    "ctx_switches":   {"label": "Context Switches", "unit": "/s", "dp": 0},
    # memory
    "mem_avail_mb":   {"label": "Available Memory", "unit": "MB", "dp": 0},
    "page_faults":    {"label": "Page Fault Rate", "unit": "/s", "dp": 0},
    "swap_io":        {"label": "Swap / Paging I/O", "unit": "MB/s", "dp": 1},
    "swap_used_mb":   {"label": "Swap Used", "unit": "MB", "dp": 0},
    "paging_overhead": {"label": "Paging Overhead", "unit": "% CPU", "dp": 1},
    "throughput":     {"label": "Throughput", "unit": "req/s", "dp": 0},
    # disk
    "read_iops":      {"label": "Read IOPS", "unit": "", "dp": 0},
    "write_iops":     {"label": "Write IOPS", "unit": "", "dp": 0},
    "io_queue":       {"label": "I/O Queue Length", "unit": "reqs", "dp": 1},
    "disk_latency":   {"label": "Disk Latency", "unit": "ms", "dp": 1},
    "iowait":         {"label": "CPU I/O Wait", "unit": "%", "dp": 1},
    # process
    "running_procs":  {"label": "Running Processes", "unit": "", "dp": 0},
    "terminated_procs": {"label": "Terminated Processes", "unit": "", "dp": 0},
    "availability":   {"label": "Service Availability", "unit": "%", "dp": 0},
    "web_state":      {"label": "Web-Service State", "unit": "", "dp": None},
    "restart_status": {"label": "Restart Status", "unit": "", "dp": None},
    # deadlock
    "blocked_procs":  {"label": "Blocked Processes", "unit": "", "dp": 0},
    "waiting_procs":  {"label": "Waiting Processes", "unit": "", "dp": 0},
    "locks_held":     {"label": "Resource Locks Held", "unit": "", "dp": 0},
    "deadlock":       {"label": "Deadlock Status", "unit": "", "dp": None},
    "stuck_procs":    {"label": "Processes Unable to Progress", "unit": "", "dp": 0},
    # network
    "packet_loss":    {"label": "Packet Loss", "unit": "%", "dp": 1},
    "req_ok":         {"label": "Successful Requests", "unit": "/s", "dp": 0},
    "req_failed":     {"label": "Failed Requests", "unit": "/s", "dp": 0},
    "timeouts":       {"label": "Connection Timeouts", "unit": "/s", "dp": 0},
    "reachability":   {"label": "Service Reachability", "unit": "%", "dp": 1},
    "tcp_retrans":    {"label": "TCP Retransmissions", "unit": "/s", "dp": 0},
}


# Where every metric of the SIMULATED server comes from (shown as a tag on each card).
#   REAL      read from the host OS (psutil) — only the Host Telemetry panel
#   HYBRID    baseline follows the real host reading (scaled into a healthy band);
#             the failure contribution on top of it is simulated
#   SIMULATED a simulated state or input (process/lock table, injected path delay/loss)
#   DERIVED   computed by a formula from simulated drivers (queueing / paging relations)
SOURCE_INFO = {
    "REAL": "Read from the host operating system via psutil (read-only).",
    "HYBRID": "Baseline anchored to the real host reading (psutil), scaled into the server's healthy "
              "band; the failure contribution is simulated.",
    "SIMULATED": "A simulated state or input — not measured on any real system.",
    "DERIVED": "Computed by a formula from the simulated drivers (queueing / paging / loss relations).",
}
_SIM = {"net_latency", "packet_loss", "running_procs", "terminated_procs", "web_state", "restart_status",
        "blocked_procs", "waiting_procs", "locks_held", "deadlock", "stuck_procs", "proc_count"}
for _k, _m in METRICS.items():
    _m["source"] = "HYBRID" if _k in ("cpu", "mem") else "SIMULATED" if _k in _SIM else "DERIVED"


def clamp(v, lo, hi):
    return lo if v < lo else hi if v > hi else v


# ------------------------------------------------------- real host data ---
class HostMonitor:
    """Reads REAL metrics from the host machine via psutil (read-only)."""

    def __init__(self):
        self.ok = psutil is not None
        self.cpu_smooth = None
        self.mem = None
        self._last_disk = None
        self._last_net = None
        self._last_t = None
        self.last = {"available": self.ok}
        if self.ok:
            try:
                psutil.cpu_percent(interval=None)  # prime the counter
            except Exception:
                self.ok = False

    def sample(self):
        if not self.ok:
            self.last = {"available": False}
            return self.last
        now = time.monotonic()
        out = {"available": True}
        try:
            cpu = float(psutil.cpu_percent(interval=None))
            self.cpu_smooth = cpu if self.cpu_smooth is None else 0.8 * self.cpu_smooth + 0.2 * cpu
            out["cpu"] = round(cpu, 1)
            out["cpu_smooth"] = round(self.cpu_smooth, 1)
        except Exception:
            pass
        try:
            vm = psutil.virtual_memory()
            self.mem = float(vm.percent)
            out["mem"] = round(vm.percent, 1)
            out["mem_total_gb"] = round(vm.total / 2 ** 30, 1)
        except Exception:
            pass
        try:
            out["procs"] = len(psutil.pids())
        except Exception:
            pass
        try:
            out["cores"] = psutil.cpu_count(logical=True)
        except Exception:
            pass
        dt = (now - self._last_t) if self._last_t else None
        try:
            d = psutil.disk_io_counters()
            if d is not None:
                tot = d.read_bytes + d.write_bytes
                if self._last_disk is not None and dt:
                    out["disk_mb_s"] = round(max(0.0, (tot - self._last_disk) / dt / 2 ** 20), 2)
                self._last_disk = tot
        except Exception:
            pass
        try:
            n = psutil.net_io_counters()
            tot = n.bytes_sent + n.bytes_recv
            if self._last_net is not None and dt:
                out["net_kb_s"] = round(max(0.0, (tot - self._last_net) / dt / 1024), 1)
            self._last_net = tot
        except Exception:
            pass
        self._last_t = now
        # keep previous rate values when not computable this tick
        for k in ("disk_mb_s", "net_kb_s"):
            if k not in out and k in self.last:
                out[k] = self.last[k]
        self.last = out
        return out


# ------------------------------------------------------------ smooth noise ---
class Noise:
    """Seeded AR(1) noise per channel: smooth, bounded, reproducible."""

    def __init__(self, seed):
        self.rng = random.Random(seed)
        self.state = {}

    def reseed(self, seed):
        self.rng.seed(seed)
        self.state = {}

    def n(self, key, phi=0.85):
        v = self.state.get(key, 0.0)
        v = phi * v + (1 - phi) * self.rng.gauss(0, 1) * 2.2
        v = clamp(v, -2.5, 2.5)
        self.state[key] = v
        return v


# ----------------------------------------------------------------- drivers ---
def base_drivers(noise, host, server=None):
    """Normal-operation drivers. CPU/memory anchored to the real host if enabled.

    `server` is the spec of the simulated server being monitored (see
    servers.build_spec). Its workload profile sets the normal request rate,
    disk mix and network path; without one the original Server-01 values apply.
    """
    pr = (server or {}).get("profile") or {}
    lo, hi = config.REAL_BASELINE_BAND["cpu"]
    mlo, mhi = config.REAL_BASELINE_BAND["mem"]
    real = config.USE_REAL_BASELINE and host.get("available") and "cpu_smooth" in host
    if real:
        cpu = lo + (hi - lo) * host["cpu_smooth"] / 100.0
        mem = mlo + (mhi - mlo) * host.get("mem", 50.0) / 100.0
    else:
        cpu, mem = 22.0, 44.0
    cpu *= pr.get("cpu_scale", 1.0)
    mem += pr.get("mem_offset", 0.0)
    return {
        "real_anchored": bool(real),
        "cpu": cpu + noise.n("cpu") * 1.2,
        "ws": (mem + noise.n("mem", 0.95) * 0.3) / 100.0,   # working-set fraction of RAM
        "read_iops": pr.get("read_iops", 300) + noise.n("rio") * 18,
        "write_iops": pr.get("write_iops", 190) + noise.n("wio") * 12,
        "net_latency": pr.get("net_latency", 18) + noise.n("lat") * 1.5,
        "packet_loss": max(0.0, pr.get("packet_loss", 0.08) + noise.n("loss") * 0.03),
        "offered_load": pr.get("offered_load", 860) + noise.n("load") * 15,  # client requests / s
        "extra_procs": 0,
        "deadlock_penalty_ms": 0.0,
        "deadlock_tp_factor": 1.0,
        "availability": 100.0,
        "server_factor": 1.0,       # remaining service capacity overload factor
    }


def derive(d, procs, server=None):
    """Compute every simulated-server metric from the drivers + process table."""
    S = server or config.SERVER
    m = {}

    # ---- memory / virtual memory -------------------------------------
    ws = clamp(d["ws"], 0.05, 1.20)
    mem = clamp(ws * 100.0, 1, 99.2)
    over = clamp((ws - 0.80) / 0.20, 0.0, 1.0)        # memory over-commit pressure
    page_faults = 35 + 9000 * over ** 2.2
    swap_io = 0.05 + page_faults * 0.0042           # ~4 KB pages
    paging_overhead = 1.0 + 84 * over ** 1.5         # % of CPU time spent paging
    m["mem"] = mem
    m["mem_avail_mb"] = S["ram_mb"] * (1 - mem / 100.0)
    m["page_faults"] = page_faults
    m["swap_io"] = swap_io
    m["swap_used_mb"] = min(S["swap_mb"], 40 + over ** 1.3 * 3600)
    m["paging_overhead"] = paging_overhead

    # ---- disk / I/O ----------------------------------------------------
    cap = S["disk_iops_capacity"]
    # paging I/O scaled to the device (identical to the original on the 6000-IOPS NVMe of
    # Server-01) so memory thrashing stays a memory failure and does not cascade into a
    # disk failure on slower storage
    swap_iops = page_faults * 0.25 * cap / 6000.0
    demand = d["read_iops"] + d["write_iops"] + swap_iops
    rho_d = clamp(demand / cap, 0.01, 0.994)
    served = demand if demand < cap * rho_d else cap * rho_d
    rshare = d["read_iops"] / max(1.0, d["read_iops"] + d["write_iops"])
    m["read_iops"] = (served - swap_iops * 0.5) * rshare + swap_iops * 0.5
    m["write_iops"] = served - m["read_iops"]
    m["disk"] = rho_d * 100
    m["io_queue"] = 0.15 + 0.5 * rho_d / (1 - rho_d)
    m["disk_latency"] = 2.4 / (1 - rho_d)
    m["iowait"] = 1.2 + 38 * rho_d ** 6

    # ---- CPU / scheduling ---------------------------------------------
    cpu = d["cpu"] + 22 * over + 6 * rho_d ** 4
    cpu = clamp(cpu, 0.5, 99.7)
    rho_c = cpu / 100.0
    m["cpu"] = cpu
    m["cpu_queue"] = min(40.0, 0.3 + 0.12 * S["vcpus"] * rho_c / (1 - rho_c))
    m["wait_ms"] = min(900.0, 2.5 / (1 - rho_c))
    m["ctx_switches"] = 2100 + 9000 * rho_c ** 3 + 4000 * over

    # ---- process table -------------------------------------------------
    states = [p["state"] for p in procs.list()]
    m["running_procs"] = sum(1 for s in states if s in ("RUNNING", "READY"))
    m["terminated_procs"] = sum(1 for s in states if s == "TERMINATED")
    m["waiting_procs"] = sum(1 for s in states if s == "WAITING")
    m["blocked_procs"] = sum(1 for s in states if s == "BLOCKED")
    m["stuck_procs"] = m["waiting_procs"] + m["blocked_procs"]
    m["locks_held"] = procs.locks_held()
    m["proc_count"] = 138 + d["extra_procs"] - m["terminated_procs"]
    web = procs.get(1042)
    m["web_state"] = web["state"] if web else "RUNNING"
    m["restart_status"] = procs.restart_status
    m["availability"] = d["availability"]
    m["deadlock"] = "NO"  # the detector decides; engine overwrites from detector

    # ---- network -------------------------------------------------------
    lat = max(1.0, d["net_latency"])
    loss = clamp(d["packet_loss"], 0.0, 100.0)
    p_loss_ok = (1 - loss / 100.0) ** 2              # request + response delivered
    p_in_time = 1.0 / (1.0 + math.exp((lat - 2000.0) / 180.0))   # client timeout 2 s
    succ_net = p_loss_ok * p_in_time
    m["net_latency"] = lat
    m["packet_loss"] = loss
    m["reachability"] = succ_net * 100
    m["tcp_retrans"] = 2 + loss * 38

    # ---- service level -------------------------------------------------
    avail = d["availability"] / 100.0
    tp_factor = (1 - 0.92 * over ** 1.6) * d["deadlock_tp_factor"]
    tp_factor *= 1.0 / (1 + 0.35 * max(0.0, rho_c - 0.9) * 40)   # CPU starvation
    tp_factor *= 1.0 / (1 + max(0.0, rho_d - 0.85) * 25)          # I/O starvation
    offered = d["offered_load"]
    ok = offered * avail * succ_net * clamp(tp_factor, 0.02, 1.0)
    m["throughput"] = ok
    m["req_ok"] = ok
    m["req_failed"] = max(0.0, offered - ok)
    net_fail = offered * avail * (1 - succ_net)
    m["timeouts"] = net_fail * 0.6 + offered * avail * (1 - p_in_time) * 0.4

    if avail <= 0.0001:
        m["response_ms"] = None          # no successful responses at all
    else:
        server_ms = 40 * (1 + 0.35 * rho_c / (1 - rho_c)) * d["server_factor"]
        resp = server_ms + 3 * m["disk_latency"] + 900 * over ** 2.5 + lat + d["deadlock_penalty_ms"]
        m["response_ms"] = min(5000.0, resp)
    return m
