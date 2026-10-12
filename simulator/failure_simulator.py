"""
Failure simulation layer.

A ProcessTable models the simulated server's processes and lockable resources.
Each scenario model changes *drivers* (CPU demand, working set, I/O demand,
network conditions) or the *process table* (states, locks) in its own way.
None of this touches the real machine: no real CPU burn, memory allocation,
disk writes, network changes, locks or process kills happen.
"""
import math
from collections import OrderedDict

from . import config
from .metrics import clamp

# ------------------------------------------------------------- processes ---
BASE_PROCS = [
    # pid, ppid, name, role, critical, cpu, mem_mb
    (1042, 1, "web-service", "master", True, 1.2, 180),
    (1043, 1042, "web-worker-1", "worker", False, 4.0, 210),
    (1044, 1042, "web-worker-2", "worker", False, 4.0, 210),
    (1045, 1042, "web-worker-3", "worker", False, 4.0, 210),
    (1046, 1042, "web-worker-4", "worker", False, 4.0, 210),
    (1051, 1, "database", "service", True, 6.0, 2400),
    (1060, 1, "monitoring-agent", "service", False, 0.8, 90),
    (2101, 1, "txn-worker P1", "txn", False, 3.0, 140),
    (2102, 1, "txn-worker P2", "txn", False, 3.0, 140),
]

RESOURCES = {
    "A": "Resource A · accounts.db row lock",
    "B": "Resource B · ledger.log file lock",
    "C": "Resource C · audit queue mutex",
}


class ProcessTable:
    def __init__(self, ncores=4):
        self.ncores = ncores          # vCPUs of the simulated server
        self.reset()

    def reset(self):
        self.procs = OrderedDict()
        for pid, ppid, name, role, crit, cpu, mem in BASE_PROCS:
            self.add(pid, ppid, name, role, crit, cpu, mem)
        self.resources = OrderedDict()
        for rid in ("A", "B"):
            self.resources[rid] = {"id": rid, "label": RESOURCES[rid], "holder": None,
                                   "exclusive": True, "preemptible": False}
        self.restart_status = "ACTIVE (supervised)"
        self.signal = None
        self.cores = [None] * self.ncores
        self.ready_queue = []
        self._rr = 0

    def add(self, pid, ppid, name, role, crit=False, cpu=1.0, mem=100, state="RUNNING", info=""):
        self.procs[pid] = {"pid": pid, "ppid": ppid, "name": name, "role": role,
                           "critical": crit, "state": state, "cpu": cpu, "mem_mb": mem,
                           "info": info, "holds": [], "waiting_for": None,
                           "waiting_since": None, "progress": 0.0, "exit": None}
        return self.procs[pid]

    def get(self, pid):
        return self.procs.get(pid)

    def by_name(self, name):
        for p in self.procs.values():
            if p["name"] == name:
                return p
        return None

    def list(self):
        return list(self.procs.values())

    def remove_role(self, role):
        for pid in [p for p, v in self.procs.items() if v["role"] == role]:
            del self.procs[pid]

    def add_resource(self, rid):
        if rid not in self.resources:
            self.resources[rid] = {"id": rid, "label": RESOURCES[rid], "holder": None,
                                   "exclusive": True, "preemptible": False}

    def locks_held(self):
        return sum(1 for r in self.resources.values() if r["holder"] is not None)

    def acquire(self, pid, rid):
        self.resources[rid]["holder"] = pid
        p = self.procs[pid]
        if rid not in p["holds"]:
            p["holds"].append(rid)

    def release_all(self, pid):
        for r in self.resources.values():
            if r["holder"] == pid:
                r["holder"] = None
        if pid in self.procs:
            self.procs[pid]["holds"] = []

    def wait_on(self, pid, rid, now, state="WAITING"):
        p = self.procs[pid]
        p["state"] = state
        p["waiting_for"] = rid
        p["waiting_since"] = now

    def run(self, pid):
        p = self.procs[pid]
        p["state"] = "RUNNING"
        p["waiting_for"] = None
        p["waiting_since"] = None

    def schedule(self, ncores=None):
        """Round-robin time slicing over runnable processes (for the CPU view)."""
        ncores = ncores or self.ncores
        runnable = [p for p in self.procs.values() if p["state"] in ("RUNNING", "READY")]
        if not runnable:
            self.cores = [None] * ncores
            self.ready_queue = []
            return
        n = len(runnable)
        start = self._rr % n
        order = runnable[start:] + runnable[:start]
        running = order[:ncores]
        for p in runnable:
            p["state"] = "READY"
        for p in running:
            p["state"] = "RUNNING"
        self.cores = [running[i]["pid"] if i < len(running) else None for i in range(ncores)]
        self.ready_queue = [p["pid"] for p in order[ncores:]]
        self._rr += ncores

    def snapshot(self):
        return {
            "processes": [dict(p, holds=list(p["holds"])) for p in self.procs.values()],
            "resources": [dict(r) for r in self.resources.values()],
            "restart_status": self.restart_status,
            "cores": list(self.cores),
            "ready_queue": list(self.ready_queue),
        }


def smoothstep(u):
    u = clamp(u, 0.0, 1.0)
    return u * u * (3 - 2 * u)


# ---------------------------------------------------------------- models ---
class BaseModel:
    sid = "base"
    continuous = True
    RAMP = {"LOW": 6.5, "MEDIUM": 6.0, "HIGH": 6.0, "CRITICAL": 4.0}
    TAU = {"LOW": 0.7, "MEDIUM": 0.9, "HIGH": 1.1, "CRITICAL": 1.4}
    ACTION_DELAY = 0.8
    RECOVERY_STEPS = []  # (seconds after recovery start, message)

    def __init__(self, severity, procs, emit, noise, server=None):
        self.server = server or config.SERVER     # spec of the server being analysed
        self.sev = severity
        self.procs = procs
        self.emit = emit
        self.noise = noise
        self.t = 0.0
        self.x = 0.0
        self.recovering = False
        self.rt = 0.0
        self.x_at_recovery = 0.0
        self.recovery_done = False
        self._steps_done = set()

    # lifecycle ---------------------------------------------------------
    def on_inject(self, now):
        pass

    def start_recovery(self, now):
        self.recovering = True
        self.rt = 0.0
        self.x_at_recovery = self.x

    def step(self, dt, now, d):
        self.t += dt
        if self.recovering:
            self.rt += dt
            for i, (off, msg) in enumerate(self.RECOVERY_STEPS):
                if i not in self._steps_done and self.rt >= off:
                    self._steps_done.add(i)
                    self.emit("RECOVERY", msg, "info")
                    self.on_recovery_step(i, now)
        if self.continuous:
            if not self.recovering:
                self.x = smoothstep(self.t / self.RAMP[self.sev])
            else:
                k = max(0.0, self.rt - self.ACTION_DELAY)
                self.x = self.x_at_recovery * math.exp(-k / self.TAU[self.sev])
                if self.x < 0.01 and self.rt > self.ACTION_DELAY:
                    self.x = 0.0
                    self.recovery_done = True
        self.apply(dt, now, d)

    def on_recovery_step(self, i, now):
        pass

    def apply(self, dt, now, d):
        raise NotImplementedError

    def finish(self):
        """Return the process table to its normal state after recovery."""
        pass


# ---- 1. CPU saturation ---------------------------------------------------
class CpuSaturation(BaseModel):
    sid = "cpu"
    TARGET = {"LOW": 88.5, "MEDIUM": 94.5, "HIGH": 98.3, "CRITICAL": 99.5}
    HOGS = {"LOW": 3, "MEDIUM": 8, "HIGH": 26, "CRITICAL": 38}
    RECOVERY_STEPS = [
        (0.0, "Scheduler run queue inspected: runaway analytics-job processes identified"),
        (0.5, "Priority lowered for analytics-job processes (renice +19)"),
        (1.0, "Runaway analytics-job processes terminated (SIGTERM) — CPU demand falling"),
    ]

    def on_inject(self, now):
        self.emit("SIMULATION", f"Load generator started: {self.HOGS[self.sev]} CPU-bound "
                                f"analytics-job processes queued for the scheduler", "info")

    def apply(self, dt, now, d):
        x = self.x
        target = self.TARGET[self.sev]
        d["cpu"] = d["cpu"] * (1 - x) + target * x + x * self.noise.n("cpu_sat") * 0.3
        d["ws"] += 0.03 * x
        nh = int(round(self.HOGS[self.sev] * x))
        d["extra_procs"] = nh
        hogs = [p for p in self.procs.list() if p["role"] == "hog"]
        while len(hogs) < nh:
            pid = 3401 + len(hogs)
            hogs.append(self.procs.add(pid, 1, f"analytics-job-{len(hogs) + 1}", "hog",
                                       cpu=0, mem=60, state="READY", info="CPU-bound"))
        while len(hogs) > nh:
            del self.procs.procs[hogs.pop()["pid"]]
        # run-queue length implied by utilization (same relation as derive())
        rho = min(0.997, max(0.01, d["cpu"] / 100.0))
        nc = self.procs.ncores
        want_ready = int(round(min(40.0, 0.3 + 0.12 * nc * rho / (1 - rho))))
        cands = hogs + [p for p in self.procs.list() if p["role"] != "hog"]
        runnable = cands[:want_ready + nc]
        rset = {p["pid"] for p in runnable}
        for p in cands:
            if p["pid"] not in rset:
                p["state"] = "SLEEPING"
                p["info"] = "waiting for requests"
            elif p["state"] == "SLEEPING":
                p["state"] = "READY"
        self.procs.schedule(nc)
        self.queue_len = len(self.procs.ready_queue)
        share = d["cpu"] * nc / max(1, len(runnable))
        for p in cands:
            if p["state"] == "SLEEPING":
                p["cpu"] = 0.1
            elif p["role"] == "hog":
                p["cpu"] = share * 1.3
            else:
                p["info"] = "time-sliced (CFS)"
                p["cpu"] = share * 0.6

    def finish(self):
        self.procs.remove_role("hog")
        for p in self.procs.list():
            p["state"] = "RUNNING"
            p["info"] = ""


# ---- 2. Memory exhaustion & thrashing ------------------------------------
class MemoryThrashing(BaseModel):
    sid = "memory"
    TARGET = {"LOW": 0.885, "MEDIUM": 0.935, "HIGH": 0.975, "CRITICAL": 0.99}
    RAMP = {"LOW": 7.0, "MEDIUM": 7.0, "HIGH": 6.5, "CRITICAL": 4.5}
    RECOVERY_STEPS = [
        (0.0, "Memory pressure analysed: cache-loader (PID 3301) has the largest resident set"),
        (0.6, "cache-loader terminated (OOM-killer style) — page frames returned to free list"),
        (1.6, "Swapped pages faulted back in; kswapd0 reclaim activity subsiding"),
    ]

    def on_inject(self, now):
        self.base_ws = None
        self.procs.add(3301, 1, "cache-loader", "memhog", cpu=2, mem=200,
                       info="allocating memory")
        self.emit("SIMULATION", "cache-loader (PID 3301) started allocating memory "
                                "beyond available RAM (simulated)", "info")

    def apply(self, dt, now, d):
        x = self.x
        if self.base_ws is None:
            self.base_ws = d["ws"]
        target = self.TARGET[self.sev]
        d["ws"] = d["ws"] * (1 - x) + target * x
        d["extra_procs"] = 2 if x > 0.15 else 1
        hog = self.procs.get(3301)
        if hog and hog["state"] != "TERMINATED":
            hog["mem_mb"] = 200 + max(0.0, d["ws"] - self.base_ws) * self.server["ram_mb"]
            hog["info"] = "RSS growing" if not self.recovering else "terminating"
        over = clamp((d["ws"] - 0.80) / 0.20, 0, 1)
        k = self.procs.get(64)
        if over > 0.15 and not k:
            k = self.procs.add(64, 2, "kswapd0", "kernel", cpu=0, mem=0, info="page reclaim")
            self.emit("KERNEL", "kswapd0 woke up: free pages below low watermark, reclaiming", "warn")
        if k:
            k["cpu"] = 5 + 30 * over
            if over < 0.05 and self.recovering:
                del self.procs.procs[64]
        for p in self.procs.list():
            if p["role"] in ("worker", "service", "txn"):
                p["info"] = "page-fault stalls" if over > 0.4 else ""

    def on_recovery_step(self, i, now):
        if i == 1 and self.procs.get(3301):
            self.procs.get(3301)["state"] = "TERMINATED"
            self.procs.get(3301)["exit"] = "SIGKILL"
            self.procs.get(3301)["info"] = "killed — memory freed"
        if i == 2:
            self.procs.procs.pop(3301, None)   # reaped by its parent

    def finish(self):
        for pid in (3301, 64):
            self.procs.procs.pop(pid, None)
        for p in self.procs.list():
            p["info"] = ""


# ---- 3. Disk I/O bottleneck ----------------------------------------------
class DiskBottleneck(BaseModel):
    sid = "disk"
    TARGET = {"LOW": 0.905, "MEDIUM": 0.957, "HIGH": 0.985, "CRITICAL": 0.992}
    RECOVERY_STEPS = [
        (0.0, "I/O accounting: backup-job (PID 3501) issuing most of the write I/O"),
        (0.6, "backup-job throttled to idle I/O class (ionice -c3)"),
        (1.2, "log-archiver paused; device queue draining"),
    ]

    def on_inject(self, now):
        self.procs.add(3501, 1, "backup-job", "iohog", cpu=3, mem=300,
                       state="IO-WAIT", info="sequential writes")
        self.procs.add(3502, 1, "log-archiver", "iohog", cpu=1, mem=120,
                       state="IO-WAIT", info="random reads")
        dev = "/dev/nvme0n1" if self.server.get("storage_type", "NVMe") == "NVMe" else "/dev/sda"
        self.emit("SIMULATION", "backup-job and log-archiver started heavy read/write "
                                f"I/O against {dev} (simulated)", "info")

    def apply(self, dt, now, d):
        x = self.x
        cap = self.server["disk_iops_capacity"]
        base_total = d["read_iops"] + d["write_iops"]
        target_total = self.TARGET[self.sev] * cap
        total = base_total * (1 - x) + target_total * x + x * self.noise.n("iops") * 8
        extra = max(0.0, total - base_total)
        d["read_iops"] += extra * 0.35
        d["write_iops"] += extra * 0.65
        d["extra_procs"] = 2
        rho = total / cap
        for p in self.procs.list():
            if p["role"] == "iohog":
                p["state"] = "IO-WAIT" if x > 0.05 else "RUNNING"
            elif p["name"] == "database":
                p["state"] = "IO-WAIT" if rho > 0.88 else "RUNNING"
                p["info"] = "blocked on disk I/O" if rho > 0.88 else ""
            elif p["role"] == "worker":
                p["info"] = "slow disk reads" if rho > 0.9 else ""

    def finish(self):
        self.procs.remove_role("iohog")
        for p in self.procs.list():
            p["state"] = "RUNNING"
            p["info"] = ""


# ---- 4. Critical process crash -------------------------------------------
class ProcessCrash(BaseModel):
    sid = "process"
    continuous = False
    PLAN = {
        "LOW": [(1.0, [1044])],
        "MEDIUM": [(1.0, [1044]), (2.5, [1046])],
        "HIGH": [(1.0, [1042])],
        "CRITICAL": [(1.0, [1042])],
    }

    def on_inject(self, now):
        self.crashed = []
        self._plan_done = set()
        self.loop_attempts = 0
        self.next_attempt = 3.5
        self.loop_state = None
        self.emit("SIMULATION", "Fault injected into Web-Service: invalid memory access "
                                "will raise SIGSEGV (simulated)", "info")
        self.RECOVERY_STEPS = []
        if self.sev == "CRITICAL":
            self.RECOVERY_STEPS.append((0.0, "Root cause found: corrupted web-service config — "
                                             "rolled back to last known good version"))
        else:
            self.RECOVERY_STEPS.append((0.0, "Recovery: systemctl restart web-service"))
        base = 0.4 if self.sev != "CRITICAL" else 1.0
        self.RECOVERY_STEPS += [(base, "Terminated process(es) → RESTARTING (fork + exec)"),
                                (base + 1.6, "Restarted process(es) → RUNNING; health check passed")]

    def _kill(self, pid, now):
        p = self.procs.get(pid)
        if not p or p["state"] == "TERMINATED":
            return
        p["state"] = "TERMINATED"
        p["exit"] = "SIGSEGV (139)"
        p["cpu"] = 0
        p["info"] = "segmentation fault"
        self.crashed.append(pid)
        self.emit("PROCESS", f"PID {pid} {p['name']} received SIGSEGV → TERMINATED (exit code 139)", "fail")
        if p["role"] == "master":
            for c in self.procs.list():
                if c["ppid"] == pid and c["state"] != "TERMINATED":
                    c["state"] = "TERMINATED"
                    c["exit"] = "SIGHUP (parent died)"
                    c["cpu"] = 0
                    c["info"] = "parent terminated"
                    self.crashed.append(c["pid"])
            self.emit("PROCESS", "Child workers 1043–1046 terminated with their parent (no master)", "fail")
        self.procs.restart_status = ("CRASH LOOP — supervisor retrying" if self.sev == "CRITICAL"
                                     else "NOT RESTARTED (manual recovery required)")

    def _avail(self):
        m = next((p for p in self.procs.list() if p["role"] == "master"), None)
        if not m or m["state"] != "RUNNING":
            return 0.0
        workers = [p for p in self.procs.list() if p["role"] == "worker"]
        alive = sum(1 for p in workers if p["state"] in ("RUNNING", "READY"))
        return 100.0 * alive / max(1, len(workers))

    def apply(self, dt, now, d):
        for i, (off, pids) in enumerate(self.PLAN[self.sev]):
            if i not in self._plan_done and self.t >= off and not self.recovering:
                self._plan_done.add(i)
                for pid in pids:
                    self._kill(pid, now)
        # CRITICAL: supervisor crash loop (restart → crash again → give up)
        if self.sev == "CRITICAL" and self.crashed and not self.recovering and self.loop_attempts < 3:
            if self.loop_state is None and self.t >= self.next_attempt:
                self.loop_attempts += 1
                self.loop_state = self.t
                self.procs.get(1042)["state"] = "RESTARTING"
                self.procs.restart_status = f"CRASH LOOP — attempt {self.loop_attempts}/3"
                self.emit("PROCESS", f"Supervisor auto-restart attempt {self.loop_attempts}/3 → RESTARTING", "warn")
            elif self.loop_state is not None and self.t - self.loop_state >= 1.0:
                self.loop_state = None
                self.next_attempt = self.t + 2.0
                self.procs.get(1042)["state"] = "TERMINATED"
                self.emit("PROCESS", "web-service crashed again during start-up → TERMINATED (SIGSEGV)", "fail")
                if self.loop_attempts >= 3:
                    self.procs.restart_status = "GAVE UP — start-limit-hit"
                    self.emit("PROCESS", "Supervisor gave up: start limit hit (3 failures)", "fail")
        a = self._avail()
        d["availability"] = a
        frac = a / 100.0
        d["cpu"] *= 0.6 + 0.4 * frac
        d["ws"] -= 0.012 * len(self.crashed) / 5
        d["server_factor"] = 1 + (1 - frac) * 2.5 if frac > 0 else 1
        d["extra_procs"] = 0

    def on_recovery_step(self, i, now):
        if i == len(self.RECOVERY_STEPS) - 2:
            for pid in self.crashed:
                p = self.procs.get(pid)
                if p:
                    p["state"] = "RESTARTING"
                    p["info"] = "starting"
            self.procs.restart_status = "RESTARTING"
        if i == len(self.RECOVERY_STEPS) - 1:
            new = OrderedDict()
            for pid, p in self.procs.procs.items():
                if pid in self.crashed:
                    npid = pid + 3000
                    p = dict(p, pid=npid, state="RUNNING", exit=None, info=f"restarted (was PID {pid})",
                             cpu=4.0 if p["role"] == "worker" else 1.2)
                    if p["ppid"] in self.crashed:
                        p["ppid"] += 3000
                    new[npid] = p
                    self.emit("PROCESS", f"{p['name']} RUNNING with new PID {npid} (was {pid})", "ok")
                else:
                    new[pid] = p
            self.procs.procs = new
            self.procs.restart_status = "ACTIVE (restarted)"
            self.crashed = []
            self.recovery_done = True

    def finish(self):
        pass


# ---- 5. Process deadlock -------------------------------------------------
class Deadlock(BaseModel):
    sid = "deadlock"
    continuous = False
    QUEUED = {"LOW": 0, "MEDIUM": 2, "HIGH": 4, "CRITICAL": 6}

    def on_inject(self, now):
        self.cycle_formed_at = None
        crit = self.sev == "CRITICAL"
        P = self.procs
        if crit:
            P.add_resource("C")
            P.add(2103, 1, "txn-worker P3", "txn", cpu=3, mem=140)
        # scripted resource-allocation sequence (time offset, action)
        self.script = [
            (0.5, lambda n: self._acq(2101, "A")),
            (1.0, lambda n: self._acq(2102, "B")),
        ]
        if crit:
            self.script.append((1.3, lambda n: self._acq(2103, "C")))
            self.script += [(1.6, lambda n: self._req(2101, "B", n)),
                            (2.2, lambda n: self._req(2102, "C", n)),
                            (2.6, lambda n: self._req(2103, "A", n))]
        else:
            self.script += [(1.6, lambda n: self._req(2101, "B", n)),
                            (2.2, lambda n: self._req(2102, "A", n))]
        for k in range(self.QUEUED[self.sev]):
            rid = "A" if k % 2 == 0 else "B"
            self.script.append((5.0 + 1.2 * k, lambda n, k=k, rid=rid: self._queue(k, rid, n)))
        self._done = set()
        self.victim = 2102
        self.RECOVERY_STEPS = [
            (0.0, "Deadlock recovery initiated: selecting a victim from the cycle"),
            (0.8, "Victim selected: P2 (least progress → lowest rollback cost)"),
            (1.6, "P2 terminated and rolled back → its locks are released"),
            (2.4, "P1 acquires Resource B → state RUNNING (continues)"),
            (3.2, "P1 completes its critical section → releases Resource A and B"),
            (3.8, "Blocked processes acquire locks in FIFO order → RUNNING"),
            (4.4, "P2 restarted from a clean state → RUNNING"),
        ]
        self.emit("SIMULATION", "Transaction workers P1 and P2 scheduled to lock resources "
                                "in opposite order (simulated, no real locks)", "info")

    def _acq(self, pid, rid):
        self.procs.acquire(pid, rid)
        p = self.procs.get(pid)
        self.emit("RESOURCE", f"{p['name'].split()[-1]} acquired {rid and 'Resource ' + rid} "
                              f"(exclusive lock)", "info")

    def _req(self, pid, rid, now):
        holder = self.procs.resources[rid]["holder"]
        p = self.procs.get(pid)
        self.procs.wait_on(pid, rid, now, "WAITING")
        hn = self.procs.get(holder)["name"].split()[-1] if holder else "?"
        self.emit("RESOURCE", f"{p['name'].split()[-1]} requests Resource {rid} (held by {hn}) "
                              f"→ {p['name'].split()[-1]} WAITING", "warn")

    def _queue(self, k, rid, now):
        pid = 2104 + k
        self.procs.add(pid, 1, f"txn-worker P{4 + k}", "queued", cpu=0, mem=120)
        self.procs.wait_on(pid, rid, now, "BLOCKED")
        self.emit("RESOURCE", f"P{4 + k} requests Resource {rid} → BLOCKED behind the deadlock", "warn")

    def apply(self, dt, now, d):
        for i, (off, fn) in enumerate(self.script):
            if i not in self._done and self.t >= off and not self.recovering:
                self._done.add(i)
                fn(now)
        stuck = [p for p in self.procs.list() if p["state"] in ("WAITING", "BLOCKED")]
        cyc = [p for p in stuck if p["state"] == "WAITING"]
        if len(cyc) >= 2 and self.cycle_formed_at is None:
            self.cycle_formed_at = self.t
        n = len(stuck)
        age = (self.t - self.cycle_formed_at) if self.cycle_formed_at is not None else 0
        d["cpu"] *= 1 - 0.5 * min(1.0, n / 6.0)
        d["deadlock_tp_factor"] = max(0.05, 1 - 0.13 * n)
        d["deadlock_penalty_ms"] = min(4000.0, n * 110 + 70 * age * n / 6.0)
        d["extra_procs"] = len([p for p in self.procs.list() if p["role"] in ("queued",)]) + \
            (1 if self.procs.get(2103) else 0)
        # progress: txn workers advance only when RUNNING
        for p in self.procs.list():
            if p["role"] in ("txn", "queued"):
                if p["state"] == "RUNNING":
                    p["progress"] = (p["progress"] + dt * 9) % 100
                    p["info"] = ""
                elif p["state"] == "WAITING":
                    p["info"] = f"waiting for Resource {p['waiting_for']}"
                elif p["state"] == "BLOCKED":
                    p["info"] = f"queued on Resource {p['waiting_for']}"

    def on_recovery_step(self, i, now):
        P = self.procs
        if i == 2:
            v = P.get(self.victim)
            v["state"] = "TERMINATED"
            v["exit"] = "killed (deadlock victim)"
            v["waiting_for"] = None
            v["progress"] = 0.0
            P.release_all(self.victim)
        elif i == 3:
            P.acquire(2101, "B")
            P.run(2101)
        elif i == 4:
            P.release_all(2101)
            if P.get(2103):            # 3-process cycle: P3 can now take A
                P.release_all(2103)
                P.run(2103)
        elif i == 5:
            for p in P.list():
                if p["state"] in ("BLOCKED", "WAITING") and p["pid"] != self.victim:
                    P.run(p["pid"])
        elif i == 6:
            P.run(self.victim)
            P.get(self.victim)["exit"] = None
            for r in P.resources.values():
                r["holder"] = None
            for p in P.list():
                p["holds"] = []
            self.recovery_done = True

    def finish(self):
        self.procs.remove_role("queued")
        self.procs.procs.pop(2103, None)
        self.procs.resources.pop("C", None)


# ---- 6. Network / service communication failure --------------------------
class NetworkFailure(BaseModel):
    sid = "network"
    LAT = {"LOW": 190, "MEDIUM": 470, "HIGH": 1150, "CRITICAL": 2300}
    LOSS = {"LOW": 3.5, "MEDIUM": 13.0, "HIGH": 36.0, "CRITICAL": 62.0}
    RAMP = {"LOW": 7.0, "MEDIUM": 7.0, "HIGH": 7.5, "CRITICAL": 6.0}
    RECOVERY_STEPS = [
        (0.0, "Interface eth0 diagnosed: high RTT and packet loss on the primary route"),
        (0.6, "Traffic failed over to standby interface eth1 (bonding active-backup)"),
        (1.2, "Stale TCP connections reset; clients reconnecting"),
    ]

    def on_inject(self, now):
        self.emit("SIMULATION", "Fault injected on eth0 path: added delay + packet drop "
                                "(simulated, host network untouched)", "info")

    def apply(self, dt, now, d):
        x = self.x
        lat_t = self.LAT[self.sev]
        d["net_latency"] = d["net_latency"] * (1 - x) + lat_t * x * (1 + 0.04 * self.noise.n("jit"))
        d["packet_loss"] = d["packet_loss"] * (1 - x) + self.LOSS[self.sev] * x * (1 + 0.03 * self.noise.n("pl"))
        d["cpu"] -= 5 * x
        for p in self.procs.list():
            if p["role"] in ("worker", "master"):
                p["info"] = "TCP retransmits / client timeouts" if x > 0.3 else ""

    def finish(self):
        for p in self.procs.list():
            p["info"] = ""


MODELS = {m.sid: m for m in (CpuSaturation, MemoryThrashing, DiskBottleneck,
                              ProcessCrash, Deadlock, NetworkFailure)}


def make_model(sid, severity, procs, emit, noise, server=None):
    return MODELS[sid](severity, procs, emit, noise, server)
