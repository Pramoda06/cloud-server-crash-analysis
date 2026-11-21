"""
System Lab — the ONE simulation model of the virtual server.

    operator controls ──lag──▶ effective inputs
                                   │
                       state variables (carry over between samples):
                       run queue, disk backlog, accept backlog, previous
                       error rate / in-flight requests (cascade feedback)
                                   │
          step(): every node of graph.py is computed here, in dependency order,
          together with its PARTS = the contribution of each dependency
                                   │
                    values + parts  ──▶  detect.py, trace.py, UI

Nothing here touches the host: no CPU burn, memory allocation, disk I/O,
network shaping, signals or locks. Process states and locks are simulated.
The model is deterministic: the only randomness is a seeded noise generator
that draws a fixed number of values per sample, so the same control timeline
(in samples) reproduces the same run.
"""
import math
import random
import re
from collections import OrderedDict

from . import rag
from .graph import controls_spec

_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,8}$")

DT = 0.5                      # seconds per sample
SERVICE_TIME_MS = {"NVMe": 2.4, "SSD": 3.0, "HDD": 8.0}
TAU_UP, TAU_DOWN = 1.0, 2.5   # inertia: load ramps up quickly, winds down more slowly (s)
MAX_BACKLOG = 1024            # listen / accept backlog (requests)
CPU_SLICE_MS = 50.0           # average CPU burst of a runnable process
REQ_CPU_MS = 0.55             # CPU time per request
REQ_IO = 0.57                 # disk requests per client request (0.35 read + 0.22 write)
REQ_MEM_MB = 1.0              # memory held per in-flight request
CONN_PER_WORKER = 256         # concurrent connections one worker can hold
LEAK_START_MB, LEAK_MB_S = 64.0, 120.0   # simulated memory leak: initial size, growth per second
LOCK_SHARE = 0.35             # share of requests that touch Resource A / B
# file-system capacity (Disk space lab). Separate from disk I/O: a full disk makes writes FAIL (ENOSPC) at once,
# a slow disk makes every I/O WAIT. The fill rate is accelerated so the experiment fits in about a minute.
DISK_BASE_USED = 62.0         # % of the file system used at boot (OS, database files, existing logs)
LOGFLOOD_PCT_S = 1.0          # a runaway log writer fills 1 % of the file system per simulated second
LOGFLOOD_IOPS = 150.0         # its sequential log writes, added to the disk demand while it can write
REQ_WRITE_SHARE = 0.22 / REQ_IO   # share of requests that must write (0.22 of 0.57 disk ops) → fail when full
ALIVE = ("RUNNING", "READY", "WAITING", "BLOCKED", "SLEEPING")
# default resources: (label, used by web requests?) — A and B are touched by LOCK_SHARE of requests
RESOURCES = OrderedDict([("A", ("Resource A · accounts table row lock", True)),
                         ("B", ("Resource B · ledger file lock", True)),
                         ("C", ("Resource C · audit queue mutex", False))])


def clamp(v, lo, hi):
    return lo if v < lo else hi if v > hi else v


class Proc(dict):
    pass


class SystemModel:
    def __init__(self, server, seed=2026):
        self.server = server
        self.spec = {c["key"]: c for c in controls_spec(server)}
        self.reset(seed)

    # ------------------------------------------------------------ reset --
    def reset(self, seed=2026):
        S = self.server
        self.seed = seed
        self.rng = random.Random(seed)
        self._noise = {}
        self.tick = 0
        self.controls = {k: float(c["default"]) for k, c in self.spec.items()}
        self.eff = dict(self.controls)
        self.cascade = True
        self.anchor = False         # anchor baseline CPU / memory to the real host (HYBRID)
        self.lock_ordering = False  # deadlock prevention policy: locks must be taken in resource order (A < B < C …)
        self.halted = False         # set by the Kernel Failure Lab while the simulated kernel is panicked / booting
        self.txn_autocomplete = False  # Deadlock Lab: a running transaction finishes and releases after TXN_WORK_S
        self.avoidance = False      # deadlock avoidance: Banker's algorithm grants a request only if the state stays safe
        self.host = {}
        # state variables
        self.q_cpu = 0.0
        self.io_lq = 0.0
        self.disk_backlog = 0.0
        self.accept_backlog = 0.0
        self.disk_logs_closed = 0.0  # % of the file system held by log files nobody has open (deletable)
        self.prev = {"error_frac": 0.0, "inflight": 0.0, "capacity": 1e9}
        self.cycle_since = None
        self.next_pid = 4001
        self.events = []            # events produced by the model (process / lock), drained by the engine
        self._build_procs()

    def _build_procs(self):
        P = self.procs = OrderedDict()
        base = [(1042, 1, "web-master", "master", True), (1043, 1042, "web-worker-1", "worker", False),
                (1044, 1042, "web-worker-2", "worker", False), (1045, 1042, "web-worker-3", "worker", False),
                (1046, 1042, "web-worker-4", "worker", False), (1051, 1, "database", "db", True),
                (1060, 1, "monitor-agent", "agent", False), (2101, 1, "txn P1", "txn", False),
                (2102, 1, "txn P2", "txn", False), (2103, 1, "txn P3", "txn", False)]
        for pid, ppid, name, kind, crit in base:
            self._add(pid, ppid, name, kind, crit, extra={"max": {}, "req": None} if kind == "txn" else None)
        self._res_order = 0
        self.locks = OrderedDict()
        for rid, (label, svc) in RESOURCES.items():
            self.locks[rid] = self._new_resource(rid, label, 1, service=svc)
        self._sync_batch()
        self._sync_mem()

    def _add(self, pid, ppid, name, kind, crit=False, state="RUNNING", nice=0, extra=None):
        p = Proc(pid=pid, ppid=ppid, name=name, kind=kind, critical=crit, state=state, nice=nice, cpu=0.0,
                 mem_mb=0.0, holds=[], waiting_for=None, exit=None, info="", load=0.0, restart_at=None,
                 born=self.tick)
        if extra:
            p.update(extra)
        self.procs[pid] = p
        return p

    # ------------------------------------------------------ noise (seeded) --
    def n(self, key, phi=0.85):
        v = self._noise.get(key, 0.0)
        v = clamp(phi * v + (1 - phi) * self.rng.gauss(0, 1) * 2.2, -2.5, 2.5)
        self._noise[key] = v
        return v

    # --------------------------------------------------------- controls --
    def set_control(self, key, value):
        c = self.spec.get(key)
        if c is None:
            raise KeyError(key)
        if isinstance(value, bool):
            raise TypeError("boolean not permitted as control value")
        v = float(value)
        if not math.isfinite(v):
            raise ValueError("not finite")
        v = clamp(v, c["min"], c["max"])
        if c["step"]:
            v = round(round((v - c["min"]) / c["step"]) * c["step"] + c["min"], 6)
        self.controls[key] = v
        if key == "web_nice":
            for p in self.procs.values():
                if p["kind"] in ("master", "worker"):
                    p["nice"] = int(v)
        return v

    def _lag(self):
        for k, c in self.spec.items():
            t, e = self.controls[k], self.eff.get(k, self.controls[k])
            if c.get("lag") is False:
                self.eff[k] = t
                continue
            tau = TAU_UP if t > e else TAU_DOWN
            e += (t - e) * (1 - math.exp(-DT / tau))
            if abs(t - e) < 0.002 * ((c["max"] - c["min"]) or 1):
                e = t
            self.eff[k] = e

    def settled(self):
        return all(abs(self.eff[k] - self.controls[k]) < 1e-9 for k in self.spec)

    # ---------------------------------------------------- process actions --
    def _alive(self, p):
        return p is not None and p["state"] in ALIVE + ("NEW",)

    def _sync_batch(self):
        want = int(self.controls["cpu_procs"])
        b = [p for p in self.procs.values() if p["kind"] == "batch" and p["state"] != "TERMINATED"]
        while len(b) < want:
            pid = 3001
            while pid in self.procs:
                pid += 1
            b.append(self._add(pid, 1, f"batch-job-{pid - 3000}", "batch", state="SLEEPING"))
        while len(b) > want:
            del self.procs[b.pop()["pid"]]

    def _sync_mem(self):
        want = int(self.controls["mem_procs"])
        m = [p for p in self.procs.values() if p["kind"] == "memhog" and p["state"] != "TERMINATED"]
        while len(m) < want:
            pid = 3301
            while pid in self.procs:
                pid += 1
            m.append(self._add(pid, 1, f"mem-loader-{pid - 3300}", "memhog", state="SLEEPING"))
        while len(m) > want:
            del self.procs[m.pop()["pid"]]

    def create_process(self, kind):
        kinds = {"cpu": ("cpu-worker", 0.95, "CPU-bound: needs one full core"),
                 "memory": ("mem-alloc", 1024.0, "allocates 1 GB"),
                 "io": ("io-worker", 400.0, "issues 400 IOPS"),
                 "leak": ("mem-leaker", LEAK_START_MB, f"memory leak: grows {LEAK_MB_S:.0f} MB/s and never frees"),
                 "logflood": ("log-flooder", LOGFLOOD_PCT_S, f"writes log files nonstop: fills {LOGFLOOD_PCT_S:g} % "
                                                             "of the file system per second (accelerated)"),
                 "idle": ("idle-proc", 0.0, "sleeps")}
        if kind not in kinds:
            raise ValueError("unknown process kind")
        name, load, info = kinds[kind]
        pid = self.next_pid
        self.next_pid += 1
        p = self._add(pid, 1, f"{name}-{pid}", "custom_" + kind, state="NEW",
                      extra={"load": load, "written": 0.0} if kind == "logflood" else {"load": load})
        p["info"] = info + " (just created)"
        self.events.append(("PROCESS", f"fork(): created PID {pid} {p['name']} → NEW", "info"))
        return p

    def kill(self, pid, sig="SIGKILL"):
        p = self.procs.get(pid)
        if p is None:
            raise ValueError("no such process")
        if p["state"] == "TERMINATED":
            raise ValueError(f"{p['name']} is already terminated")
        code = {"SIGKILL": "SIGKILL (137)", "SIGSEGV": "SIGSEGV (139)", "SIGTERM": "SIGTERM (143)"}.get(sig, sig)
        self._terminate(p, code)
        self.events.append(("PROCESS", f"{sig} → PID {pid} {p['name']} TERMINATED", "fail" if p["critical"] else "warn"))
        if p["kind"] == "master":
            for c in self.procs.values():
                if c["ppid"] == pid and c["state"] != "TERMINATED":
                    self._terminate(c, "SIGHUP (parent died)")
            self.events.append(("PROCESS", "web-workers terminated with their parent (no master)", "fail"))
        if p["kind"] == "batch":
            del self.procs[pid]
            self.controls["cpu_procs"] = self.eff["cpu_procs"] = max(0, self.controls["cpu_procs"] - 1)
        if p["kind"] == "memhog":
            n = max(1, self.controls["mem_procs"])
            share = self.controls["mem_demand"] / n
            del self.procs[pid]
            self.controls["mem_procs"] = self.eff["mem_procs"] = max(0, self.controls["mem_procs"] - 1)
            self.controls["mem_demand"] = max(0.0, self.controls["mem_demand"] - share)
            self.events.append(("PROCESS", f"its {share:.0f}% of RAM was freed (memory pressure → "
                                           f"{self.controls['mem_demand']:.0f}%)", "info"))
        return p

    def _terminate(self, p, code):
        if p["kind"] == "custom_logflood" and p.get("written"):
            # the process exits, so its log file is closed: the data stays on disk until someone deletes it
            self.disk_logs_closed += p["written"]
            p["written"] = 0.0
        p["state"], p["exit"], p["cpu"], p["info"] = "TERMINATED", code, 0.0, "terminated"
        p["waiting_for"], p["req"] = None, None
        self._release_all(p["pid"])
        self._dequeue(p["pid"])

    HANG_KINDS = ("master", "worker", "db", "custom_cpu", "custom_memory", "custom_io", "custom_leak", "custom_idle",
                  "custom_logflood")

    # ------------------------------------------------------ disk space --
    def disk_used_pct(self):
        held_open = sum(p.get("written", 0.0) for p in self.procs.values()
                        if p["kind"] == "custom_logflood" and p["state"] != "TERMINATED")
        return min(100.0, DISK_BASE_USED + self.disk_logs_closed + held_open)

    def delete_logs(self):
        """rm the log files written by created processes. Space is returned only for files no process has
        open; a file still open in a running (or hung) writer is unlinked but keeps its blocks until it is
        closed (Linux: the inode lives until the last open file descriptor is closed)."""
        freed = self.disk_logs_closed
        self.disk_logs_closed = 0.0
        still_open = [p for p in self.procs.values() if p["kind"] == "custom_logflood"
                      and p["state"] != "TERMINATED" and p.get("written", 0) > 0]
        held = sum(p["written"] for p in still_open)
        self.events.append(("PROCESS", f"rm old log files: {freed:.1f}% of the file system freed"
                            + (f"; {held:.1f}% still held by open file(s) of {', '.join(p['name'] for p in still_open)}"
                               if still_open else ""), "info"))
        return freed, held, [p["name"] for p in still_open]

    def hang(self, pid):
        """The process stops responding (e.g. stuck in an uninterruptible wait or an infinite loop on a lock).
        It still exists and keeps its memory, but does no work and serves no requests. Unlike a crash, the
        supervisor does not notice: it must be killed, then restarted."""
        p = self.procs.get(pid)
        if p is None:
            raise ValueError("no such process")
        if p["kind"] not in self.HANG_KINDS:
            raise ValueError(f"{p['name']} cannot be made unresponsive in this lab")
        if p["state"] not in ALIVE:
            raise ValueError(f"{p['name']} is {p['state']} — only a running process can hang")
        p["state"], p["cpu"], p["info"] = "HUNG", 0.0, "unresponsive: alive but not making progress"
        self.events.append(("PROCESS", f"{p['name']} (PID {pid}) stopped responding → HUNG (still alive, serves nothing)",
                            "fail" if p["critical"] else "warn"))
        return p

    def restart(self, pid):
        p = self.procs.get(pid)
        if p is None:
            raise ValueError("no such process")
        if p["state"] != "TERMINATED":
            raise ValueError(f"{p['name']} is not terminated")
        group = [p]
        if p["kind"] == "master":
            group += [c for c in self.procs.values() if c["ppid"] == pid and c["state"] == "TERMINATED"]
        for q in group:
            q["state"], q["restart_at"], q["info"] = "RESTARTING", self.tick + 4, "fork + exec"
        self.events.append(("PROCESS", f"Restarting {', '.join(q['name'] for q in group)} → RESTARTING", "info"))
        return p

    def renice(self, pid, nice):
        p = self.procs.get(pid)
        if p is None:
            raise ValueError("no such process")
        nice = int(clamp(int(nice), -20, 19))
        if p["kind"] in ("master", "worker"):
            self.set_control("web_nice", nice)
        else:
            p["nice"] = nice
        self.events.append(("PROCESS", f"renice {nice:+d} → {p['name']} (PID {pid})", "info"))

    def reap(self):
        for pid in [k for k, p in self.procs.items() if p["state"] == "TERMINATED" and p["kind"].startswith("custom")]:
            del self.procs[pid]

    # ------------------------------------------------------- lock actions --
    # Resource model (Deadlock Lab). A resource has N identical instances; a transaction
    # process may hold several instances and may wait for at most ONE outstanding request.
    #   r["alloc"]  {pid: instances held}      r["queue"]  [pid, ...] FIFO of waiting processes
    #   p["holds"]  [rid, ...]                 p["waiting_for"] rid,  p["req"] instances wanted
    #   p["max"]    {rid: n} declared maximum claim (needed only by the Banker's algorithm)
    # "holder" (first holder or None) is kept for backward compatibility with older pages.
    def _new_resource(self, rid, label, instances=1, service=False):
        self._res_order += 1
        return {"id": rid, "label": label, "instances": int(instances), "alloc": OrderedDict(), "queue": [],
                "holder": None, "order": self._res_order, "service": service}

    @staticmethod
    def _avail(r):
        return r["instances"] - sum(r["alloc"].values())

    def _sync_res(self, r):
        r["alloc"] = OrderedDict((k, v) for k, v in r["alloc"].items() if v > 0)
        r["holder"] = next(iter(r["alloc"]), None)
        for pid in list(r["alloc"]):
            p = self.procs.get(pid)
            if p is not None and r["id"] not in p["holds"]:
                p["holds"].append(r["id"])
        for p in self.procs.values():
            if r["id"] in p["holds"] and p["pid"] not in r["alloc"]:
                p["holds"].remove(r["id"])

    def _short(self, pid):
        p = self.procs.get(pid)
        return p["name"].split()[-1] if p else str(pid)

    def _txn(self, target, need_alive=True):
        p = self.procs.get(target) if isinstance(target, int) else None
        if p is None:
            raise ValueError("unknown process")
        if p["kind"] != "txn":
            raise ValueError("only transaction processes take part in the Deadlock Lab")
        if need_alive and p["state"] in ("TERMINATED", "RESTARTING"):
            raise ValueError(f"{self._short(p['pid'])} is {p['state']} — restart it before it can use resources")
        return p

    def _matrices(self, live_only=True):
        pids = [p["pid"] for p in self.procs.values() if p["kind"] == "txn"
                and (not live_only or p["state"] not in ("TERMINATED", "RESTARTING"))]
        avail = {rid: self._avail(r) for rid, r in self.locks.items()}
        alloc = {pid: {rid: r["alloc"].get(pid, 0) for rid, r in self.locks.items() if r["alloc"].get(pid, 0)} for pid in pids}
        req = {}
        for pid in pids:
            p = self.procs[pid]
            if p["state"] == "BLOCKED" and p.get("waiting_for") in self.locks:
                req[pid] = {p["waiting_for"]: p.get("req") or 1}
        need = {}
        for pid in pids:
            mx = self.procs[pid].get("max") or {}
            need[pid] = {rid: max(0, int(mx.get(rid, 0)) - alloc[pid].get(rid, 0)) for rid in self.locks}
        return pids, avail, alloc, req, need

    def banker(self):
        """Banker's safety check of the current state (only meaningful when maximum claims are declared)."""
        pids, avail, alloc, _, need = self._matrices()
        declared = [pid for pid in pids if self.procs[pid].get("max")]
        res = rag.is_safe(pids, avail, alloc, need)
        res["declared"] = len(declared) == len(pids) and bool(pids)
        res["missing"] = [self._short(p) for p in pids if p not in declared]
        return res

    def _safe_if_granted(self, pid, rid, n):
        pids, avail, alloc, _, need = self._matrices()
        avail[rid] -= n
        alloc[pid][rid] = alloc[pid].get(rid, 0) + n
        need[pid][rid] = max(0, need[pid].get(rid, 0) - n)
        return rag.is_safe(pids, avail, alloc, need)

    def acquire(self, pid, rid, count=1):
        """Request `count` instances of resource `rid` for transaction `pid`.
        Granted at once if enough instances are free (and, with avoidance on, the state stays safe);
        otherwise the process becomes BLOCKED and waits in the resource's FIFO queue."""
        p, r = self.procs.get(pid), self.locks.get(rid)
        if p is None or r is None:
            raise ValueError("unknown process or resource")
        p = self._txn(pid)
        if p["state"] not in ("RUNNING", "READY", "SLEEPING"):
            raise ValueError(f"{p['name']} is {p['state']} and cannot request a lock")
        try:
            n = int(count)
        except (TypeError, ValueError):
            raise ValueError("instance count must be a whole number")
        short = self._short(pid)
        have = r["alloc"].get(pid, 0)
        if n < 1:
            raise ValueError("request at least one instance")
        if have + n > r["instances"]:
            if r["instances"] == 1 and have:
                raise ValueError(f"{p['name']} already holds {rid}")
            raise ValueError(f"{short} would hold {have + n} of {rid}, but {rid} has only {r['instances']} instance(s)")
        if self.lock_ordering:
            higher = [h for h in p["holds"] if self.locks[h]["order"] > r["order"] or (h == rid)]
            if higher:
                self.events.append(("RESOURCE", f"Lock-ordering policy refused: {short} holds {', '.join(p['holds'])} "
                                                f"and may not request {rid} ({self.order_text()})", "warn"))
                raise ValueError(f"Lock ordering policy: acquire resources in the order {self.order_text()}"
                                 f" and all instances of a resource in one request ({short} already holds {', '.join(p['holds'])})")
        if self.avoidance:
            mx = (p.get("max") or {}).get(rid)
            if mx is None or not p.get("max"):
                raise ValueError(f"Banker's algorithm is on: declare {short}'s maximum claim first")
            if have + n > mx:
                raise ValueError(f"{short} declared a maximum of {mx} × {rid}; requesting {have + n} exceeds its claim")
        free = self._avail(r)
        unsafe = False
        if free >= n and self.avoidance:
            unsafe = not self._safe_if_granted(pid, rid, n)["safe"]
        if free >= n and not unsafe:
            r["alloc"][pid] = have + n
            self._sync_res(r)
            self.events.append(("RESOURCE", f"{short} acquired {n} × {rid}" + (" (exclusive)" if r["instances"] == 1 else
                                f" ({self._avail(r)} of {r['instances']} left)"), "info"))
            return "granted"
        p["state"], p["waiting_for"], p["req"] = "BLOCKED", rid, n
        if unsafe:
            p["info"] = f"waiting: granting {n} × {rid} now would leave an UNSAFE state (Banker's)"
            self.events.append(("RESOURCE", f"Banker's algorithm delayed {short}'s request for {n} × {rid}: "
                                            "the resulting state would be unsafe → BLOCKED", "warn"))
        else:
            hn = ", ".join(self._short(h) for h in r["alloc"]) or "nobody"
            p["info"] = f"waiting for {n} × {rid} (held by {hn})"
            self.events.append(("RESOURCE", f"{short} requests {n} × {rid}; only {free} free (held by {hn}) → BLOCKED", "warn"))
        r["queue"].append(pid)
        return "blocked"

    def order_text(self):
        return " < ".join(r["id"] for r in sorted(self.locks.values(), key=lambda r: r["order"]))

    def release(self, pid, rid=None, count=None):
        p = self.procs.get(pid)
        if p is None:
            raise ValueError("no such process")
        if not p["holds"]:
            raise ValueError(f"{p['name']} holds no locks")
        if rid is None:
            held = list(p["holds"])
            self._release_all(pid)
            self.events.append(("RESOURCE", f"{self._short(pid)} released {', '.join(held)}", "ok"))
            return
        r = self.locks.get(rid)
        if r is None or not r["alloc"].get(pid):
            raise ValueError(f"{self._short(pid)} does not hold {rid}")
        n = r["alloc"][pid] if count in (None, "") else int(count)
        if n < 1 or n > r["alloc"][pid]:
            raise ValueError(f"{self._short(pid)} holds {r['alloc'][pid]} × {rid}")
        r["alloc"][pid] -= n
        self._sync_res(r)
        self.events.append(("RESOURCE", f"{self._short(pid)} released {n} × {rid}", "ok"))
        self._grant_waiting()

    def _release_all(self, pid):
        for r in self.locks.values():
            if r["alloc"].get(pid):
                r["alloc"][pid] = 0
                self._sync_res(r)
        if pid in self.procs:
            self.procs[pid]["holds"] = []
        self._grant_waiting()

    def _dequeue(self, pid):
        for r in self.locks.values():
            if pid in r["queue"]:
                r["queue"].remove(pid)

    def _grant_waiting(self):
        """Serve every resource queue in FIFO order, granting each request that now fits
        (first-fit: a large request at the head does not block a smaller one behind it)."""
        changed = True
        while changed:
            changed = False
            for r in self.locks.values():
                for wpid in list(r["queue"]):
                    w = self.procs.get(wpid)
                    if w is None or w["state"] != "BLOCKED" or w.get("waiting_for") != r["id"]:
                        r["queue"].remove(wpid)
                        continue
                    n = w.get("req") or 1
                    if self._avail(r) < n:
                        continue
                    if self.avoidance and not self._safe_if_granted(wpid, r["id"], n)["safe"]:
                        continue
                    r["queue"].remove(wpid)
                    r["alloc"][wpid] = r["alloc"].get(wpid, 0) + n
                    w["state"], w["waiting_for"], w["req"], w["info"] = "RUNNING", None, None, ""
                    self._sync_res(r)
                    self.events.append(("RESOURCE", f"{self._short(wpid)} was granted {n} × {r['id']} → RUNNING", "ok"))
                    changed = True

    def _grant_next(self, r):          # kept for older callers
        self._grant_waiting()

    def preempt(self, rid, victim=None):
        """Pre-emption: take every instance of `rid` from the victim, roll the victim back
        (its outstanding request is cancelled; it must request again) and serve the waiters."""
        r = self.locks.get(rid)
        if r is None or not r["alloc"]:
            raise ValueError(f"Resource {rid} is not held")
        vpid = victim if victim is not None else next(iter(r["alloc"]))
        if not r["alloc"].get(vpid):
            raise ValueError(f"{self._short(vpid)} does not hold {rid}")
        h = self.procs[vpid]
        n = r["alloc"][vpid]
        r["alloc"][vpid] = 0
        self._sync_res(r)
        if h["state"] == "BLOCKED":
            self._dequeue(vpid)
            h["state"], h["waiting_for"], h["req"] = "READY", None, None
        h["info"] = f"rolled back: lost {n} × {rid} (pre-empted) — must request it again"
        self.events.append(("RESOURCE", f"{n} × {rid} PRE-EMPTED from {self._short(vpid)}, which is rolled back "
                                        "(breaks 'no preemption')", "warn"))
        self._grant_waiting()

    def form_deadlock(self, three=False):
        """Convenience: run the classic opposite-order lock sequence on P1–P3 / A–C."""
        names = {p["name"]: p["pid"] for p in self.procs.values() if p["kind"] == "txn"}
        pid = {k: names.get(f"txn {k}") for k in ("P1", "P2", "P3")}
        if None in (pid["P1"], pid["P2"]) or (three and pid["P3"] is None) or not {"A", "B"} <= set(self.locks) \
                or (three and "C" not in self.locks):
            raise ValueError("the guided example needs P1–P3 and resources A–C (use Reset lab first)")
        seq = [("P1", "A"), ("P2", "B")] + ([("P3", "C")] if three else [])
        seq += [("P1", "B"), ("P2", "C" if three else "A")] + ([("P3", "A")] if three else [])
        for who, rid in seq:
            self.acquire(pid[who], rid)

    def wait_for_graph(self):
        waiting = {p["pid"]: p["waiting_for"] for p in self.procs.values()
                   if p.get("waiting_for") and p["state"] == "BLOCKED" and p["waiting_for"] in self.locks}
        alloc = {pid: {rid: n for rid, r in self.locks.items() for q, n in r["alloc"].items() if q == pid}
                 for pid in self.procs}
        return rag.wait_for_edges(waiting, alloc)

    find_cycle = staticmethod(rag.find_cycle)

    def deadlock_report(self):
        """Full analysis of the resource state: detection algorithm, WFG cycle, Banker's safety."""
        pids, avail, alloc, req, need = self._matrices()
        det = rag.detect(pids, avail, alloc, req)
        edges = self.wait_for_graph()
        any_cycle = rag.find_cycle(edges)
        dead = set(det["deadlocked"])
        cycle = rag.find_cycle(edges, restrict=dead) if dead else []
        single = all(r["instances"] == 1 for r in self.locks.values())
        behind = [p for p, rid in ((p, self.procs[p].get("waiting_for")) for p in req)
                  if p not in dead and any(h in dead for h in self.locks[rid]["alloc"])]
        return {"deadlocked": det["deadlocked"], "cycle": cycle, "any_cycle": any_cycle, "single_instance": single,
                "reduction": det["order"], "steps": det["steps"], "behind": behind, "edges": edges,
                "avail": avail, "alloc": alloc, "req": req, "need": need,
                "banker": self.banker() if any(self.procs[p].get("max") for p in pids) else None}

    TXN_WORK_S = 5.0

    def _complete_transactions(self):
        """Optional mode: a transaction that holds resources and is NOT waiting finishes its work after
        TXN_WORK_S seconds and releases everything — the assumption the detection algorithm makes."""
        for p in list(self.procs.values()):
            if p["kind"] != "txn":
                continue
            if p["holds"] and p["state"] in ("RUNNING", "READY", "SLEEPING"):
                p.setdefault("work_since", None)
                if p["work_since"] is None:
                    p["work_since"] = self.tick
                elif (self.tick - p["work_since"]) * DT >= self.TXN_WORK_S:
                    held = list(p["holds"])
                    p["work_since"] = None
                    self._release_all(p["pid"])
                    p["info"] = "transaction committed"
                    self.events.append(("RESOURCE", f"{self._short(p['pid'])} finished its transaction and released "
                                                    f"{', '.join(held)}", "ok"))
            else:
                p["work_since"] = None

    # ------------------------------------------------ Deadlock Lab editing --
    TXN_LIMIT, RES_LIMIT, INST_LIMIT = 12, 10, 12

    def add_txn(self, name=None, max_claim=None):
        txns = [p for p in self.procs.values() if p["kind"] == "txn"]
        if len(txns) >= self.TXN_LIMIT:
            raise ValueError(f"at most {self.TXN_LIMIT} transaction processes")
        used = {p["name"].split()[-1] for p in txns}
        name = (name or "").strip()
        if not name:
            i = 1
            while f"P{i}" in used:
                i += 1
            name = f"P{i}"
        if not _ID_RE.match(name):
            raise ValueError("process name: 1–8 letters, digits, '-' or '_' (no spaces)")
        if name in used:
            raise ValueError(f"a process named {name} already exists")
        pid = 2101
        while pid in self.procs or any(q.get("was_pid") == pid for q in self.procs.values()):
            pid += 1
        p = self._add(pid, 1, f"txn {name}", "txn", state="RUNNING", extra={"max": {}, "req": None})
        if max_claim:
            self.set_max(pid, max_claim, quiet=True)
        self.events.append(("PROCESS", f"Deadlock Lab: created transaction {name} (PID {pid})", "info"))
        return p

    def remove_txn(self, pid):
        p = self._txn(pid, need_alive=False)
        if p["state"] not in ("TERMINATED",) and (p["holds"] or p["state"] == "BLOCKED"):
            raise ValueError(f"{self._short(pid)} holds or waits for resources — release or terminate it first")
        self._dequeue(pid)
        name = self._short(pid)
        del self.procs[pid]
        self.events.append(("PROCESS", f"Deadlock Lab: removed transaction {name}", "info"))

    def set_max(self, pid, claim, quiet=False):
        p = self._txn(pid, need_alive=False)
        out = {}
        for rid, n in (claim or {}).items():
            if rid not in self.locks:
                raise ValueError(f"unknown resource {rid}")
            try:
                n = int(n)
            except (TypeError, ValueError):
                raise ValueError("maximum claim must be a whole number")
            if n < 0 or n > self.locks[rid]["instances"]:
                raise ValueError(f"maximum claim for {rid} must be 0–{self.locks[rid]['instances']}")
            if n < self.locks[rid]["alloc"].get(pid, 0):
                raise ValueError(f"{self._short(pid)} already holds {self.locks[rid]['alloc'][pid]} × {rid}")
            if n:
                out[rid] = n
        p["max"] = out
        if not quiet:
            self.events.append(("RESOURCE", f"{self._short(pid)} declared maximum claim: "
                                + (", ".join(f"{n} × {r}" for r, n in out.items()) or "none"), "info"))

    def add_resource(self, rid=None, label=None, instances=1):
        if len(self.locks) >= self.RES_LIMIT:
            raise ValueError(f"at most {self.RES_LIMIT} resources")
        rid = (rid or "").strip()
        if not rid:
            i = 1
            while f"R{i}" in self.locks:
                i += 1
            rid = f"R{i}"
        if not _ID_RE.match(rid):
            raise ValueError("resource id: 1–8 letters, digits, '-' or '_' (no spaces)")
        if rid in self.locks:
            raise ValueError(f"resource {rid} already exists")
        try:
            n = int(instances)
        except (TypeError, ValueError):
            raise ValueError("instances must be a whole number")
        if not 1 <= n <= self.INST_LIMIT:
            raise ValueError(f"instances must be 1–{self.INST_LIMIT}")
        label = (label or "").strip()[:40] or f"Resource {rid}"
        self.locks[rid] = self._new_resource(rid, label, n)
        self.events.append(("RESOURCE", f"Deadlock Lab: added resource {rid} ({n} instance{'s' if n > 1 else ''})", "info"))
        return self.locks[rid]

    def remove_resource(self, rid):
        r = self.locks.get(rid)
        if r is None:
            raise ValueError(f"unknown resource {rid}")
        if r["alloc"] or r["queue"]:
            raise ValueError(f"{rid} is allocated or has waiting processes — release it first")
        if r["service"]:
            raise ValueError(f"{rid} is used by the web service model and cannot be removed")
        del self.locks[rid]
        for p in self.procs.values():
            (p.get("max") or {}).pop(rid, None)
        self.events.append(("RESOURCE", f"Deadlock Lab: removed resource {rid}", "info"))

    def reset_locks(self):
        """Deadlock Lab reset: default P1–P3 and resources A–C, nothing held, no claims."""
        for pid in [k for k, p in self.procs.items() if p["kind"] == "txn"]:
            del self.procs[pid]
        for pid, name in ((2101, "txn P1"), (2102, "txn P2"), (2103, "txn P3")):
            self._add(pid, 1, name, "txn", extra={"max": {}, "req": None})
        self._res_order = 0
        self.locks = OrderedDict()
        for rid, (label, svc) in RESOURCES.items():
            self.locks[rid] = self._new_resource(rid, label, 1, service=svc)
        self.procs = OrderedDict(sorted(self.procs.items()))
        self.avoidance = False          # maximum claims were cleared, so the Banker's algorithm cannot run
        self.cycle_since = None

    # ------------------------------------------------------------- step --
    def step(self):
        """Advance the simulated server by one sample (DT seconds). Returns (values, parts, extra)."""
        S = self.server
        self.tick += 1
        self._lag()
        e = self.eff
        V, P = {}, {}           # node values, node parts {name: (dep_node or None, contribution)}
        cores = int(S["vcpus"])
        ram = float(S["ram_mb"])
        cap = float(S["disk_iops_capacity"])
        svc = SERVICE_TIME_MS.get(S.get("storage_type", "NVMe"), 2.4)
        prof = S.get("profile") or {}
        cascade = self.cascade
        prev = self.prev
        nz = {k: self.n(k) for k in ("cpu", "rtt", "req", "mem")}   # fixed draws per sample

        # --- process table: restarts / new processes / controlled counts (frozen while the kernel is down)
        for p in ([] if self.halted else list(self.procs.values())):
            if p["state"] == "RESTARTING" and p["restart_at"] is not None and self.tick >= p["restart_at"]:
                old = p["pid"]
                new = old + 3000 if old < 9000 else old + 1
                p.update(state="RUNNING", restart_at=None, exit=None, info=f"restarted (was PID {old})")
                del self.procs[old]
                p["pid"] = new
                self.procs[new] = p
                for c in self.procs.values():
                    if c["ppid"] == old:
                        c["ppid"] = new
                self.events.append(("PROCESS", f"{p['name']} RUNNING again with PID {new} (was {old})", "ok"))
                if p["kind"] == "custom_leak":
                    p["load"] = LEAK_START_MB          # a restarted process starts with a fresh heap
            elif p["state"] == "NEW" and self.tick > p["born"] + 1:
                p["state"], p["info"] = "READY", p["info"].replace(" (just created)", "")
        if not self.halted:
            self._sync_batch()
            self._sync_mem()
        procs = list(self.procs.values())
        master = next((p for p in procs if p["kind"] == "master"), None)
        db = next((p for p in procs if p["kind"] == "db"), None)
        master_up, db_up = self._alive(master), self._alive(db)
        workers_alive = sum(1 for p in procs if p["kind"] == "worker" and self._alive(p)) if master_up else 0
        custom = [p for p in procs if p["kind"].startswith("custom_") and p["state"] in ALIVE]
        hung_mem = [p for p in procs if p["kind"] in ("custom_memory", "custom_leak") and p["state"] == "HUNG"]
        c_cpu = sum(p["load"] for p in custom if p["kind"] == "custom_cpu")
        for p in custom:
            if p["kind"] == "custom_leak":          # the leak grows while the process is alive
                p["load"] = min(p["load"] + LEAK_MB_S * DT, 0.75 * float(S["ram_mb"]))
        c_mem = sum(p["load"] for p in custom + hung_mem if p["kind"] in ("custom_memory", "custom_leak"))  # a hung process keeps its memory
        c_io = sum(p["load"] for p in custom if p["kind"] == "custom_io")
        # --- file-system capacity: running log writers append until the file system is full
        flooders = [p for p in custom if p["kind"] == "custom_logflood" and p["state"] != "NEW"]
        if flooders:
            for p in flooders:
                room = 100.0 - self.disk_used_pct()
                if room > 0:
                    p["written"] = p.get("written", 0.0) + min(room, p["load"] * DT)
            writing = sum(1 for p in flooders if self.disk_used_pct() < 100.0)
            c_io += LOGFLOOD_IOPS * writing
        batch_alive = sum(1 for p in procs if p["kind"] == "batch" and p["state"] in ALIVE)
        mem_alive = sum(1 for p in procs if p["kind"] == "memhog" and p["state"] in ALIVE)
        for k in ("cpu_workload", "cpu_procs", "web_nice", "mem_demand", "disk_read", "disk_write", "req_rate",
                  "net_delay", "net_loss", "conn_fail", "timeout_ms"):
            V[k] = e[k]

        # --- traffic (retry storm = cascade)
        lam = max(0.0, e["req_rate"] * (1 + 0.006 * nz["req"]))
        retries = min(lam, 0.8 * lam * prev["error_frac"]) if cascade else 0.0
        V["req_eff"] = lam + retries
        P["req_eff"] = {"client requests": ("req_rate", lam), "retries of failed requests": ("error_rate", retries)}
        accepted = V["req_eff"] * (1 - e["conn_fail"] / 100.0)

        # --- memory
        base_ws = 0.38 + prof.get("mem_offset", 0.0) / 100.0
        if self.anchor and self.host.get("mem") is not None:
            base_ws += 0.10 * self.host["mem"] / 100.0
        hog = e["mem_demand"] / 100.0 if mem_alive > 0 else 0.0
        inflight_mem = prev["inflight"] * REQ_MEM_MB / ram if cascade else 0.0
        cust_mem = c_mem / ram
        ws = base_ws + hog + inflight_mem + cust_mem + 0.0015 * nz["mem"]
        V["inflight"] = prev["inflight"]
        V["mem_ws"] = ws * 100
        P["mem_ws"] = {"OS & services": (None, base_ws * 100), "memory pressure": ("mem_demand", hog * 100),
                       "in-flight requests": ("inflight", inflight_mem * 100),
                       "created processes": ("procs_table", cust_mem * 100)}
        over = clamp((ws - 0.80) / 0.20, 0.0, 2.0)
        o1 = min(over, 1.0)
        V["mem_util"] = min(ws, 0.995) * 100
        V["mem_avail"] = max(ram * (1 - ws), 0.005 * ram)
        V["page_faults"] = 35 + 9000 * o1 ** 2.2 + 12000 * max(over - 1, 0)
        V["paging_overhead"] = min(90.0, 70 * o1 ** 1.8 + 15 * max(over - 1, 0))
        V["swap_iops"] = V["page_faults"] * 0.25
        V["swap_used"] = min(S.get("swap_mb", 4096), max(0.0, (ws - 0.9) * ram))
        for k in ("mem_util", "mem_avail", "page_faults", "paging_overhead", "swap_iops"):
            P[k] = {"working set": ("mem_ws" if k in ("mem_util", "mem_avail", "page_faults") else "page_faults", V[k])}

        # --- CPU
        batch_cores = min(cores * e["cpu_workload"] / 100.0, float(batch_alive))
        req_cores = min(accepted, prev["capacity"]) * REQ_CPU_MS / 1000.0 if master_up else 0.0
        paging_cores = V["paging_overhead"] / 100.0 * cores if cascade else 0.0
        sys_cores = 0.03 * cores
        if self.anchor and self.host.get("cpu_smooth") is not None:
            sys_cores += 0.15 * cores * self.host["cpu_smooth"] / 100.0
        demand = batch_cores + req_cores + paging_cores + c_cpu + sys_cores
        rho = demand / cores
        V["cpu_demand"] = rho * 100
        P["cpu_demand"] = {"batch workload": ("cpu_workload", batch_cores / cores * 100),
                           "client requests": ("req_eff", req_cores / cores * 100),
                           "paging overhead": ("paging_overhead", paging_cores / cores * 100),
                           "created processes": ("procs_table", c_cpu / cores * 100),
                           "system": (None, sys_cores / cores * 100)}
        V["cpu_util"] = clamp(min(rho, 1.0) * 100 + 0.5 * nz["cpu"] * (1 if rho < 0.995 else 0), 0, 100)
        if rho < 0.98:
            target = rho ** math.sqrt(2 * (cores + 1)) / (1 - rho)
            if self.q_cpu > target:
                drain = (0.98 - rho) * cores / (CPU_SLICE_MS / 1000.0)
                self.q_cpu = max(target, min(self.q_cpu - drain * DT,
                                             target + (self.q_cpu - target) * math.exp(-DT / 1.0)))
            else:
                self.q_cpu += (target - self.q_cpu) * (1 - math.exp(-DT / 1.0))
        else:
            self.q_cpu += (rho - 0.98) * cores / (CPU_SLICE_MS / 1000.0) * DT
        self.q_cpu = min(self.q_cpu, 1200.0)           # bounded: at most ~1200 runnable tasks queued
        V["run_queue"] = self.q_cpu
        V["cpu_wait"] = self.q_cpu * CPU_SLICE_MS / cores
        w_web = 1024 * 1.25 ** (-e["web_nice"])
        batch_nices = [p["nice"] for p in procs if p["kind"] == "batch"] or [0]
        w_b = sum(1024 * 1.25 ** (-n) for n in batch_nices) / len(batch_nices)
        f = 2 * w_b / (w_b + w_web)
        V["cpu_wait_web"] = V["cpu_wait"] * f
        P["cpu_util"] = {"CPU demand": ("cpu_demand", V["cpu_util"])}
        P["run_queue"] = {"CPU demand": ("cpu_demand", V["run_queue"])}
        P["cpu_wait"] = {"run queue": ("run_queue", V["cpu_wait"])}
        P["cpu_wait_web"] = {"CPU waiting time": ("cpu_wait", V["cpu_wait_web"])}

        # --- disk
        req_io = (min(accepted, prev["capacity"]) if master_up else 0.0) * REQ_IO
        swap_io = V["swap_iops"] if cascade else 0.0
        dd = e["disk_read"] + e["disk_write"] + req_io + swap_io + c_io
        V["disk_demand"] = dd
        P["disk_demand"] = {"read workload": ("disk_read", e["disk_read"]), "write workload": ("disk_write", e["disk_write"]),
                            "client requests": ("req_eff", req_io), "swap I/O": ("swap_iops", swap_io),
                            "created processes": ("procs_table", c_io)}
        rho_d = dd / cap
        r = min(rho_d, 0.98)
        lq_target = r * r / (1 - r)
        self.io_lq += (lq_target - self.io_lq) * (1 - math.exp(-DT / 0.5))
        if rho_d >= 0.98:
            self.disk_backlog += (dd - 0.98 * cap) * DT
        else:
            self.disk_backlog = max(0.0, self.disk_backlog - (0.98 * cap - dd) * DT)
        self.disk_backlog = min(self.disk_backlog, cap * 30.0)   # bounded: 30 s of device work queued
        channels = cap * svc / 1000.0
        V["disk_util"] = min(rho_d, 1.0) * 100
        V["io_queue"] = self.io_lq + self.disk_backlog / channels
        V["disk_latency"] = svc / (1 - r) + self.disk_backlog / cap * 1000.0
        served_io = min(dd, cap)
        rd = e["disk_read"] + req_io * 0.35 / REQ_IO + swap_io
        V["read_iops"] = served_io * (rd / dd if dd > 0 else 0.6)
        V["write_iops"] = served_io - V["read_iops"]
        V["iowait_procs"] = min(5.0, V["io_queue"] / 2.0)
        for k in ("disk_util", "io_queue"):
            P[k] = {"disk demand": ("disk_demand", V[k])}
        P["disk_latency"] = {"device service time": (None, svc), "queueing": ("io_queue", V["disk_latency"] - svc)}
        P["iowait_procs"] = {"I/O queue": ("io_queue", V["iowait_procs"])}
        used = self.disk_used_pct()
        V["disk_used"] = used
        V["disk_free_gb"] = (100.0 - used) / 100.0 * float(S.get("storage_gb", 250))
        P["disk_used"] = {"files at boot (OS, database, logs)": (None, DISK_BASE_USED),
                          "log files written by created processes": ("procs_table", used - DISK_BASE_USED)}
        P["disk_free_gb"] = {"used space": ("disk_used", V["disk_free_gb"])}
        full = used >= 100.0 - 1e-9

        # --- network
        rtt = max(1.0, prof.get("net_latency", 18.0) + e["net_delay"] + 1.5 * nz["rtt"])
        loss = clamp(prof.get("packet_loss", 0.08) + e["net_loss"], 0.0, 100.0)
        V["rtt"], V["packet_loss"] = rtt, loss
        P["rtt"] = {"base path": (None, rtt - e["net_delay"]), "added latency": ("net_delay", e["net_delay"])}
        P["packet_loss"] = {"base loss": (None, loss - e["net_loss"]), "injected loss": ("net_loss", e["net_loss"])}
        p = loss / 100.0
        rto = max(200.0, 2 * rtt)
        combos = []        # (probability, extra delay) over retransmission counts of request and reply
        for k1 in range(6):
            for k2 in range(6):
                pr = (p ** k1) * (1 - p) * (p ** k2) * (1 - p)
                combos.append((pr, rto * (2 ** k1 - 1) + rto * (2 ** k2 - 1)))
        delivered = sum(c[0] for c in combos)
        giveup = max(0.0, 1.0 - delivered)
        if delivered > 0:
            V["retrans_ms"] = sum(pr * x for pr, x in combos) / delivered
        else:
            V["retrans_ms"] = None
        P["retrans_ms"] = {"packet loss": ("packet_loss", V["retrans_ms"] if V["retrans_ms"] is not None else (e["timeout_ms"] - rtt))}

        # --- locks (deadlock lab)
        if self.txn_autocomplete:
            self._complete_transactions()
        edges = self.wait_for_graph()
        dl = self.deadlock_report()
        cycle = dl["cycle"]                 # a cycle among DEADLOCKED processes (detection algorithm)
        if cycle:
            if self.cycle_since is None:
                self.cycle_since = self.tick
        else:
            self.cycle_since = None
        dead = set(dl["deadlocked"])
        held = [r for r in self.locks.values() if r["service"] and r["alloc"]]
        if cycle and any(h in dead for r in held for h in r["alloc"]):
            lock_each, lock_fail = e["timeout_ms"], LOCK_SHARE * len(held) / 2.0
        elif held:
            lock_each, lock_fail = 50.0, 0.0
        else:
            lock_each, lock_fail = 0.0, 0.0
        V["lock_wait"] = LOCK_SHARE * len(held) / 2.0 * lock_each
        P["lock_wait"] = {"locked resources": ("locks", V["lock_wait"])}

        # --- service capacity and accept backlog
        per_worker = 2.0 * prof.get("offered_load", 860.0) / 4.0
        cpu_factor = min(1.0, 1.0 / rho) if rho > 0 else 1.0
        thrash = 1 - 0.85 * o1 ** 2
        capacity = workers_alive * per_worker * cpu_factor * thrash if (master_up and db_up) else 0.0
        V["capacity"] = capacity
        P["capacity"] = {"live workers": ("procs_table", workers_alive * per_worker if (master_up and db_up) else 0.0),
                         "CPU share lost": ("cpu_demand", -(workers_alive * per_worker * (1 - cpu_factor))),
                         "thrashing": ("page_faults", -(workers_alive * per_worker * cpu_factor * (1 - thrash)))}
        before = self.accept_backlog
        if not master_up:
            self.accept_backlog = before = 0.0     # listening socket closed: queued connections are dropped
        self.accept_backlog += (accepted - capacity) * DT if master_up else 0.0
        refused = 0.0
        if self.accept_backlog > MAX_BACKLOG:
            refused = (self.accept_backlog - MAX_BACKLOG) / DT
            self.accept_backlog = MAX_BACKLOG
        self.accept_backlog = max(0.0, self.accept_backlog)
        served = min(capacity, accepted + before / DT)
        V["accept_backlog"] = self.accept_backlog
        P["accept_backlog"] = {"arrivals beyond capacity": ("req_eff", self.accept_backlog)}
        V["queue_wait"] = min(30000.0, self.accept_backlog / capacity * 1000.0) if capacity > 0 else e["timeout_ms"]
        # with no live workers every request waits until the client gives up: the cause is capacity, not the backlog
        P["queue_wait"] = ({"request backlog": ("accept_backlog", V["queue_wait"])} if capacity > 0 else
                           {"no live workers (capacity 0)": ("capacity", V["queue_wait"])})

        # --- latency
        pen = 600 * o1 ** 2.5 * ((V["disk_latency"] / svc) ** 0.5 if cascade else 1.0)
        V["paging_penalty"] = min(pen, 8000.0)
        P["paging_penalty"] = {"page faults": ("page_faults", 600 * o1 ** 2.5),
                               "slow swap device": ("disk_latency", V["paging_penalty"] - 600 * o1 ** 2.5)}
        V["service_ms"] = 22 + V["cpu_wait_web"] + 2 * V["disk_latency"] + V["paging_penalty"] + V["lock_wait"] + V["queue_wait"]
        P["service_ms"] = {"application work": (None, 22.0), "CPU wait": ("cpu_wait_web", V["cpu_wait_web"]),
                           "disk reads (2 per request)": ("disk_latency", 2 * V["disk_latency"]),
                           "page-fault stalls": ("paging_penalty", V["paging_penalty"]),
                           "lock wait": ("lock_wait", V["lock_wait"]), "backlog wait": ("queue_wait", V["queue_wait"])}
        T = float(e["timeout_ms"])
        if delivered > 0:
            V["response_ms"] = min(T, V["service_ms"] + rtt + (V["retrans_ms"] or 0.0))
        else:
            V["response_ms"] = T
        P["response_ms"] = {"server processing": ("service_ms", V["service_ms"]), "network RTT": ("rtt", rtt),
                            "retransmissions": ("retrans_ms", V["retrans_ms"] if V["retrans_ms"] is not None else max(0.0, T - rtt - V["service_ms"]))}

        # --- timeouts / errors
        T = e["timeout_ms"]
        s_mean = max(1.0, V["service_ms"] - V["lock_wait"] + LOCK_SHARE * len(held) / 2.0 * (0 if lock_fail else lock_each))
        if capacity <= 0:
            p_to = 1.0
        else:
            acc = 0.0
            for pr, x in combos:
                net = rtt + x
                acc += pr * (1.0 if net >= T else math.exp(-(T - net) / s_mean))
            p_to = acc / delivered if delivered > 0 else 1.0
        ok_frac = (1 - p_to) * (1 - giveup) * (1 - lock_fail)
        nospace = REQ_WRITE_SHARE if full else 0.0     # writes fail at once with ENOSPC: an error, not a delay
        success = served * ok_frac * (1 - nospace)
        attempts = V["req_eff"]
        err = clamp(1 - success / attempts, 0.0, 1.0) if attempts > 0 else 0.0
        V["timeouts"] = served * (1 - ok_frac)
        V["enospc"] = served * ok_frac * nospace
        if not master_up:
            refused = accepted
        V["refused"] = refused + (accepted - served if capacity <= 0 and master_up else 0.0)
        V["throughput"] = success
        V["req_ok"] = success
        V["req_failed"] = max(0.0, attempts - success)
        V["error_rate"] = err * 100
        V["availability"] = 100 - V["error_rate"]
        if attempts > 0:
            P["error_rate"] = {"timeouts": ("timeouts", served * p_to * (1 - lock_fail) / attempts * 100),
                               "connection failures": ("conn_fail", attempts * e["conn_fail"] / 100 / attempts * 100),
                               "backlog full / no capacity": ("capacity" if capacity <= 0 else "accept_backlog",
                                                              max(0.0, accepted - served) / attempts * 100),
                               "deadlocked lock waits": ("lock_wait", served * lock_fail / attempts * 100),
                               "TCP give-up": ("packet_loss", served * (1 - p_to) * giveup * (1 - lock_fail) / attempts * 100)}
            if nospace:
                P["error_rate"]["disk full (writes fail: ENOSPC)"] = ("disk_used", V["enospc"] / attempts * 100)
        else:
            P["error_rate"] = {}
        P["timeouts"] = {"slow responses": ("response_ms", served * p_to * (1 - lock_fail)),
                         "deadlocked lock waits": ("lock_wait", served * lock_fail)}
        P["throughput"] = {"attempts": ("req_eff", attempts), "failed": ("error_rate", -V["req_failed"])}
        P["availability"] = {"error rate": ("error_rate", V["availability"])}
        # Little's law, bounded by the connections the live workers can hold
        inflight = min(served * V["response_ms"] / 1000.0, workers_alive * CONN_PER_WORKER) + self.accept_backlog
        V["inflight"] = inflight
        P["inflight"] = {"requests being served": ("response_ms", served * V["response_ms"] / 1000.0),
                         "requests waiting in backlog": ("accept_backlog", self.accept_backlog)}

        # --- process states (scheduling view)
        self._schedule(cores, V, batch_cores, batch_alive, hog, mem_alive, o1, workers_alive)
        st = [p["state"] for p in self.procs.values()]
        V["running_procs"] = sum(1 for s in st if s in ("RUNNING", "READY"))
        V["blocked_procs"] = sum(1 for s in st if s in ("WAITING", "BLOCKED"))
        V["lock_blocked"] = sum(1 for p in self.procs.values() if p["state"] == "BLOCKED")
        V["terminated_procs"] = sum(1 for s in st if s == "TERMINATED")
        V["proc_count"] = len(st) + 120 - V["terminated_procs"]
        V["workers_alive"] = workers_alive
        V["master_up"] = 1.0 if master_up else 0.0
        V["db_up"] = 1.0 if db_up else 0.0
        P["running_procs"] = {"process table": ("procs_table", V["running_procs"])}
        P["blocked_procs"] = {"I/O wait": ("iowait_procs", V["blocked_procs"] - V["lock_blocked"]),
                              "lock waits": ("locks", V["lock_blocked"])}

        # cascade feedback for the next sample
        self.prev = {"error_frac": err, "inflight": inflight, "capacity": capacity if capacity > 0 else 1e-9}
        extra = {"hung": [p["name"] for p in self.procs.values() if p["state"] == "HUNG"], "cycle": cycle, "edges": edges, "deadlocked": dl["deadlocked"], "any_cycle": dl["any_cycle"],
                 "cycle_age": (self.tick - self.cycle_since) * DT if self.cycle_since is not None else 0.0,
                 "refused": refused, "giveup": giveup, "p_timeout": p_to, "served": served, "rho": rho,
                 "rho_disk": rho_d, "over": over, "per_worker": per_worker, "svc_ms": svc}
        return V, P, extra

    def _schedule(self, cores, V, batch_cores, batch_alive, hog, mem_alive, o1, workers_alive):
        """Assign RUNNING / READY / WAITING / SLEEPING for the process view (simulated scheduler)."""
        procs = list(self.procs.values())
        per_batch = batch_cores / batch_alive if batch_alive else 0.0
        io_wait = int(round(V["iowait_procs"]))
        runnable = []
        for p in procs:
            if p["state"] in ("TERMINATED", "RESTARTING", "NEW", "BLOCKED", "HUNG"):
                continue
            k = p["kind"]
            if k == "batch":
                p["load"] = per_batch
                active = per_batch > 0.01
            elif k == "memhog":
                p["mem_mb"] = hog * self.server["ram_mb"] / max(1, mem_alive)
                active = hog > 0
            elif k in ("worker", "master", "db"):
                active = True
            elif k == "custom_cpu":
                active = True
            elif k in ("custom_memory", "custom_leak"):
                p["mem_mb"] = p["load"]
                active = True
            elif k in ("custom_io", "custom_logflood"):
                active = True
            else:
                active = k == "txn"
            if not active:
                p["state"], p["info"] = "SLEEPING", "idle"
                p["cpu"] = 0.0
                continue
            runnable.append(p)
        # I/O wait: database and workers first
        waiting = [p for p in runnable if p["kind"] in ("db", "worker", "custom_io")][:io_wait]
        if o1 > 0.4:
            waiting += [p for p in runnable if p["kind"] == "memhog"]
        for p in runnable:
            if p in waiting:
                p["state"] = "WAITING"
                p["info"] = "page-fault stall (swap-in)" if p["kind"] == "memhog" else "waiting for disk I/O"
                p["cpu"] = 0.0
        ready = [p for p in runnable if p not in waiting]
        ready.sort(key=lambda p: (p["nice"], (p["pid"] + self.tick * 7) % 97))
        demand = V["cpu_demand"] / 100.0 * cores
        share = min(1.0, cores / demand) if demand > 0 else 1.0
        for i, p in enumerate(ready):
            p["state"] = "RUNNING" if i < cores else "READY"
            if p["kind"] == "batch":
                p["cpu"] = p["load"] * share * 100
            elif p["kind"] == "custom_cpu":
                p["cpu"] = 95 * share
            elif p["kind"] in ("worker",):
                p["cpu"] = 100 * (V["req_eff"] * REQ_CPU_MS / 1000.0 / max(1, workers_alive)) * share
            else:
                p["cpu"] = 1.0
            if p["kind"] in ("batch", "custom_cpu", "worker") and not p["info"].startswith("restarted"):
                p["info"] = "on CPU" if p["state"] == "RUNNING" else "in ready queue"

    # ---------------------------------------------------------- snapshot --
    def snapshot(self):
        return {
            "processes": [dict(p, holds=list(p["holds"]), max=dict(p.get("max") or {})) for p in self.procs.values()],
            "resources": [{"id": r["id"], "label": r["label"], "holder": r["holder"], "queue": list(r["queue"]),
                           "instances": r["instances"], "alloc": dict(r["alloc"]), "available": self._avail(r),
                           "order": r["order"], "service": r["service"]}
                          for r in self.locks.values()],
            "disk_space": {"used_pct": round(self.disk_used_pct(), 2), "base_pct": DISK_BASE_USED,
                           "deletable_pct": round(self.disk_logs_closed, 2),
                           "held_open_pct": round(sum(p.get("written", 0.0) for p in self.procs.values()
                                                      if p["kind"] == "custom_logflood" and p["state"] != "TERMINATED"), 2),
                           "size_gb": float(self.server.get("storage_gb", 250))},
        }

    def state_dict(self):
        """Full reproducible state (for snapshots / recordings)."""
        return {"controls": dict(self.controls), "cascade": self.cascade, "anchor": self.anchor,
                "lock_ordering": self.lock_ordering, "avoidance": self.avoidance,
                "txn_autocomplete": self.txn_autocomplete, "seed": self.seed}
