"""
Failure detection for the System Lab.

The detector reads ONLY observed node values (and the process / lock tables). It never
reads the operator's controls. Every numeric threshold lives in THRESHOLDS and can be
changed at runtime (/api/lab/thresholds), which immediately changes detection.

Levels: 0 OK · 1 WARNING · 2 DEGRADED · 3 CRITICAL · 4 FAILED
A rule's level is CONFIRMED when the raw level (or worse) is seen in CONFIRM consecutive
samples (filters noise). Health = worst confirmed rule.

Hysteresis (alert flapping): a confirmed level is RAISED at the trigger threshold (value > threshold,
unchanged) but only CLEARED once the value falls below its RELEASE threshold = threshold × (1 − HYSTERESIS),
e.g. CPU WARNING is raised above 75 % and cleared at or below 71.25 %. Without this, a value hovering at a
threshold toggles WARNING ↔ OK every few seconds and opens a new failure episode each time. Applies to the
numeric rules (CPU, memory, disk I/O, disk space, network, service); process, deadlock and kernel rules are
state-based.

Attribution (primary vs secondary): when several rules are active, attribute() uses the
contribution PARTS computed by the model to decide whether one active rule is explained by
another through an active cascade link (e.g. disk demand dominated by swap I/O → disk
failure is SECONDARY to memory thrashing). Rules not explained by another are PRIMARY.
"""
import copy

LEVELS = ["OK", "WARNING", "DEGRADED", "CRITICAL", "FAILED"]
HEALTH = ["HEALTHY", "WARNING", "DEGRADED", "CRITICAL", "FAILED"]
CONFIRM = 2
HYSTERESIS = 0.05            # release threshold = trigger threshold × (1 − HYSTERESIS)

# rule -> checks: key, label, unit, thresholds {W, D, C, F}, direction (">" higher is worse)
DEFAULT_THRESHOLDS = {
    "cpu": {"cpu_util": {"W": 75, "D": 85, "C": 95, "F": None},
            "run_queue_per_core": {"W": 1, "D": 2, "C": 4, "F": 8}},
    "memory": {"mem_util": {"W": 80, "D": 88, "C": None, "F": None},
               "page_faults": {"W": 500, "D": 1500, "C": 3000, "F": 6000},
               "paging_overhead": {"W": 5, "D": 15, "C": 25, "F": 50}},
    "disk": {"io_queue": {"W": 4, "D": 8, "C": 16, "F": 32},
             "disk_latency": {"W": 20, "D": 50, "C": 100, "F": 250}},
    "disk_space": {"disk_used": {"W": 85, "D": 92, "C": 97, "F": 99.9}},
    "network": {"rtt": {"W": 150, "D": 300, "C": 500, "F": 1000},
                "packet_loss": {"W": 2, "D": 5, "C": 10, "F": 30}},
    "service": {"response_ms": {"W": 300, "D": 800, "C": 1500, "F": 3000},
                "error_rate": {"W": 1, "D": 5, "C": 20, "F": 50}},
    "deadlock": {"cycle_age": {"W": 0.0, "D": None, "C": 3.0, "F": None},
                 "lock_blocked": {"W": None, "D": None, "C": None, "F": 3}},
}
RULES = {
    "cpu": {"name": "CPU saturation", "subsystem": "CPU scheduling / processor management",
            "logic": "worst of: CPU utilization, run queue per vCPU",
            "checks": {"cpu_util": ("CPU utilization", "%"), "run_queue_per_core": ("Run queue per vCPU", "procs")}},
    "memory": {"name": "Memory exhaustion / thrashing", "subsystem": "Memory management / virtual memory",
               "logic": "memory utilization (at most DEGRADED on its own)  OR  page faults AND paging overhead "
                        "(CRITICAL / FAILED = thrashing: both must hold)",
               "checks": {"mem_util": ("Memory utilization", "%"), "page_faults": ("Page fault rate", "/s"),
                          "paging_overhead": ("Paging overhead", "% CPU")}},
    "disk": {"name": "Disk I/O bottleneck", "subsystem": "I/O management / disk scheduling",
             "logic": "worst of: I/O queue length, disk latency",
             "checks": {"io_queue": ("I/O queue length", "reqs"), "disk_latency": ("Disk latency", "ms")}},
    "disk_space": {"name": "Disk space exhaustion", "subsystem": "File system / storage capacity",
                   "logic": "share of the file system in use; FAILED when it is full (new writes fail with ENOSPC). "
                            "Separate from the disk I/O rule: a full disk fails fast, a slow disk makes I/O wait",
                   "checks": {"disk_used": ("File system used", "%")}},
    "network": {"name": "Network degradation", "subsystem": "Networking / TCP",
                "logic": "worst of: RTT, packet loss",
                "checks": {"rtt": ("Network RTT", "ms"), "packet_loss": ("Packet loss", "%")}},
    "service": {"name": "Service degradation", "subsystem": "Service level (requests, workers, backlog)",
                "logic": "worst of: response latency, error rate",
                "checks": {"response_ms": ("Response latency", "ms"), "error_rate": ("Error rate", "%")}},
    "process": {"name": "Critical process failure", "subsystem": "Process management",
                "logic": "web-master or database TERMINATED or HUNG → FAILED, RESTARTING → CRITICAL; "
                         "1 worker TERMINATED / HUNG → DEGRADED, ≥ 2 → CRITICAL",
                "checks": {}},
    "kernel": {"name": "Kernel panic", "subsystem": "Kernel (core of the OS)",
               "logic": "simulated kernel panicked or halted → FAILED; rebooting → CRITICAL; "
                        "services starting / post-boot checks → DEGRADED (driven by the Kernel Failure Lab)",
               "checks": {}},
    "deadlock": {"name": "Process deadlock", "subsystem": "Process synchronization / resource allocation",
                 "logic": "cycle in the wait-for graph → WARNING; persists ≥ 3 s → CRITICAL; "
                          "≥ 3 processes blocked on locks while deadlocked → FAILED",
                 "checks": {"cycle_age": ("Circular wait persisted", "s"), "lock_blocked": ("Processes blocked on locks", "procs")}},
}
ORDER = ["kernel", "process", "deadlock", "disk_space", "network", "memory", "disk", "cpu", "service"]


def level_of(v, th):
    """Highest level whose threshold the value exceeds (thresholds set to None are skipped)."""
    if v is None or isinstance(v, str):
        return 0
    lvl = 0
    for i, k in enumerate(("W", "D", "C", "F")):
        t = th.get(k)
        if t is not None and v > t:
            lvl = i + 1
    return lvl


def release_th(th, hysteresis):
    """Thresholds at which an already-raised level is cleared (lower than the trigger thresholds)."""
    return {k: (None if t is None else t * (1.0 - hysteresis) if t > 0 else t) for k, t in th.items()}


class Detector:
    def __init__(self):
        self.thresholds = copy.deepcopy(DEFAULT_THRESHOLDS)
        self.hysteresis = HYSTERESIS
        self.reset()

    def reset(self):
        self.hist = {k: [] for k in RULES}
        self.confirmed = {k: 0 for k in RULES}

    def set_threshold(self, rule, check, level, value):
        th = self.thresholds[rule][check]
        if level not in th:
            raise ValueError("level must be W, D, C or F")
        th[level] = None if value in (None, "") else float(value)
        seq = [th[k] for k in ("W", "D", "C", "F") if th[k] is not None]
        if any(b < a for a, b in zip(seq, seq[1:])):
            raise ValueError("thresholds must increase from WARNING to FAILED")

    def evaluate(self, V, snap, extra):
        T = self.thresholds
        rules = {}
        for rid in ("cpu", "disk", "disk_space", "network", "service"):
            checks = []
            for key, (label, unit) in RULES[rid]["checks"].items():
                v = V.get(key)
                checks.append({"key": key, "label": label, "unit": unit, "value": v, "th": T[rid][key],
                               "level": level_of(v, T[rid][key]),
                               "hold": level_of(v, release_th(T[rid][key], self.hysteresis))})
            rules[rid] = {"level": max(c["level"] for c in checks), "checks": checks,
                          "hold": max(c["hold"] for c in checks)}
        # memory: utilization alone is at most DEGRADED; thrashing needs page faults AND paging overhead
        mc = []
        for key, (label, unit) in RULES["memory"]["checks"].items():
            mc.append({"key": key, "label": label, "unit": unit, "value": V.get(key), "th": T["memory"][key],
                       "level": level_of(V.get(key), T["memory"][key]),
                       "hold": level_of(V.get(key), release_th(T["memory"][key], self.hysteresis))})
        thrash = min(mc[1]["level"], mc[2]["level"])
        rules["memory"] = {"level": max(min(mc[0]["level"], 2), thrash), "checks": mc, "thrashing": thrash >= 3,
                           "hold": max(min(mc[0]["hold"], 2), min(mc[1]["hold"], mc[2]["hold"]))}
        # process table
        procs = snap["processes"]
        crit_dead = [p for p in procs if p["critical"] and p["state"] in ("TERMINATED", "HUNG")]   # hung = alive but useless
        crit_rest = [p for p in procs if p["critical"] and p["state"] == "RESTARTING"]
        w_dead = [p for p in procs if p["kind"] == "worker" and p["state"] in ("TERMINATED", "RESTARTING", "HUNG")]
        pl = 4 if crit_dead else 3 if crit_rest else (3 if len(w_dead) >= 2 else 2 if w_dead else 0)
        rules["process"] = {"level": pl, "checks": [
            {"key": "critical", "label": "Critical processes down or hung", "unit": "", "level": 4 if crit_dead else 3 if crit_rest else 0,
             "value": ", ".join(p["name"] for p in crit_dead + crit_rest) or "none", "th": None},
            {"key": "workers", "label": "Web-workers down or hung", "unit": "", "level": 3 if len(w_dead) >= 2 else 2 if w_dead else 0,
             "value": len(w_dead), "th": None}]}
        # deadlock (wait-for graph)
        age = extra["cycle_age"] if extra["cycle"] else None
        th = T["deadlock"]
        dl = 0
        if extra["cycle"]:
            dl = 1
            if th["cycle_age"]["C"] is not None and age >= th["cycle_age"]["C"]:
                dl = 3
                if th["lock_blocked"]["F"] is not None and V["lock_blocked"] >= th["lock_blocked"]["F"]:
                    dl = 4
        rules["deadlock"] = {"level": dl, "checks": [
            {"key": "cycle_age", "label": "Circular wait persisted", "unit": "s", "value": age if age is not None else 0.0,
             "th": th["cycle_age"], "level": dl if dl <= 3 else 3},
            {"key": "lock_blocked", "label": "Processes blocked on locks", "unit": "procs", "value": V["lock_blocked"],
             "th": th["lock_blocked"], "level": 4 if dl == 4 else 0}]}

        kl = extra.get("kernel_level", 0) or 0
        rules["kernel"] = {"level": kl, "checks": [
            {"key": "kernel_state", "label": "Kernel state", "unit": "", "level": kl, "th": None,
             "value": extra.get("kernel_stage") or "RUNNING"}]}

        transitions = []
        for rid, r in rules.items():
            h = (self.hist[rid] + [r["level"]])[-CONFIRM:]
            self.hist[rid] = h
            cand = min(h) if len(h) >= CONFIRM else self.confirmed[rid]
            if cand < self.confirmed[rid]:
                # clear only below the release threshold (hysteresis); never above the level already raised
                cand = max(cand, min(r.get("hold", 0), self.confirmed[rid]))
            if cand != self.confirmed[rid]:
                transitions.append({"rule": rid, "from": self.confirmed[rid], "to": cand, "evidence": evidence(r)})
                self.confirmed[rid] = cand
            r["confirmed"] = self.confirmed[rid]
            r["name"] = RULES[rid]["name"]
            r["level_name"] = LEVELS[r["confirmed"]]
        health = max(self.confirmed.values())
        return {"rules": rules, "health": health, "raw": max(r["level"] for r in rules.values()),
                "transitions": transitions}


def fmt_v(v, unit):
    if isinstance(v, str):
        return v
    if v is None:
        return "—"
    if isinstance(v, int) and not isinstance(v, bool):
        return f"{v:,}" + ("%" if unit == "%" else f" {unit}" if unit else "")
    s = f"{v:,.0f}" if abs(v) >= 100 else f"{v:.1f}"
    return s + ("%" if unit == "%" else f" {unit}" if unit else "")


def evidence(r):
    out = []
    for c in r["checks"]:
        if c["level"] > 0:
            th = c.get("th") or {}
            k = ["W", "D", "C", "F"][c["level"] - 1]
            t = th.get(k)
            if t is not None:
                out.append(f"{c['label']} {fmt_v(c['value'], c['unit'])} > {fmt_v(t, c['unit'])} ({LEVELS[c['level']]})")
            else:
                out.append(f"{c['label']}: {fmt_v(c['value'], c['unit'])} ({LEVELS[c['level']]})")
    return out


# ------------------------------------------------------------- attribution --
def _delta_parts(P, refP, node):
    cur, ref = P.get(node, {}), (refP or {}).get(node, {})
    return {name: (dep, val - (ref.get(name, (None, 0.0))[1])) for name, (dep, val) in cur.items()}


def _share(parts, name):
    pos = sum(d for _, d in parts.values() if d > 0)
    return (parts.get(name, (None, 0.0))[1] / pos) if pos > 0 else 0.0


def attribute(det, V, P, refV, refP, extra, cascade):
    """For each active rule: what caused it? Returns {rule: {"by": rule|None, "via": text, "input": node|None}}."""
    active = {k: r["confirmed"] for k, r in det["rules"].items() if r["confirmed"] > 0}
    # the simulated kernel's own state, not its (2-sample) confirmed alert: once it is down, nothing else can run
    kernel_down = (extra.get("kernel_level") or 0) >= 3
    out = {}
    for rid in active:
        by, via, inp = None, "", None
        if rid == "kernel":
            if extra.get("kernel_cause") == "memory":
                via, inp = ("memory ran out (working set beyond RAM + swap); because vm.panic_on_oom = 1 the kernel "
                            "panicked instead of killing one process"), "mem_demand"
            else:
                via, inp = f"simulated kernel panic / reboot ({extra.get('kernel_stage', '')})", "kernel"
        elif ("kernel" in active or kernel_down) and rid in ("process", "service", "deadlock"):
            by, via = "kernel", "the host is down (kernel panic / reboot): no process can run or answer requests"
        elif rid == "cpu":
            d = _delta_parts(P, refP, "cpu_demand")
            if cascade and _share(d, "paging overhead") >= 0.3 and "memory" in active:
                by, via = "memory", "paging overhead consumes CPU time (kernel page reclaim / fault handling)"
            elif _share(d, "client requests") >= 0.3:
                retry = _delta_parts(P, refP, "req_eff")
                if cascade and _share(retry, "retries of failed requests") >= 0.3 and "service" in active:
                    by, via = "service", "clients retry failed requests (retry storm) → more CPU work"
                else:
                    via, inp = "client request rate increased → more CPU time per second", "req_rate"
            elif _share(d, "created processes") >= 0.3:
                via, inp = "CPU-bound processes created in the Process Lab", "procs_table"
            else:
                via, inp = "batch CPU workload increased", "cpu_workload"
        elif rid == "disk":
            d = _delta_parts(P, refP, "disk_demand")
            if cascade and _share(d, "swap I/O") >= 0.3 and "memory" in active:
                by, via = "memory", "page faults are served from swap → extra disk requests"
            elif _share(d, "client requests") >= 0.3:
                via, inp = "client request rate increased → more disk reads/writes", "req_rate"
            elif _share(d, "created processes") >= 0.3:
                via, inp = "I/O-bound processes created in the Process Lab", "procs_table"
            else:
                rw = d.get("read workload", (0, 0))[1], d.get("write workload", (0, 0))[1]
                via, inp = "background disk workload increased", ("disk_write" if rw[1] >= rw[0] else "disk_read")
        elif rid == "memory":
            d = _delta_parts(P, refP, "mem_ws")
            if cascade and _share(d, "in-flight requests") >= 0.3:
                # requests pile up because responses are slow: blame what makes them slow
                sb, svia, _ = _service_cause(det, P, refP, extra, active)
                if sb and sb != "memory" and sb in active:
                    by, via = sb, f"{svia}; slow requests pile up in flight and hold memory (Little's law)"
                elif "service" in active:
                    by, via = "service", "slow requests accumulate in flight and hold memory (Little's law)"
                else:
                    via, inp = "requests in flight hold more memory", "req_rate"
            elif _share(d, "created processes") >= 0.3:
                via, inp = "memory-allocating processes created in the Process Lab", "procs_table"
            else:
                via, inp = "memory pressure (allocation by memory-hungry processes) increased", "mem_demand"
        elif rid == "service":
            by, via, inp = _service_cause(det, P, refP, extra, active)
        elif rid == "network":
            dl = (V["net_delay"], V["net_loss"])
            via, inp = ("injected packet loss" if V["packet_loss"] - (refV or {}).get("packet_loss", 0) > 1 else
                        "added path latency"), ("net_loss" if V["packet_loss"] - (refV or {}).get("packet_loss", 0) > 1 else "net_delay")
        elif rid == "process":
            via, inp = ("a critical / worker process stopped responding (hung): " + ", ".join(extra["hung"]) if extra.get("hung") else
                        "a critical / worker process terminated"), "procs_table"
        elif rid == "deadlock":
            via, inp = "processes hold locks while waiting for each other (circular wait)", "locks"
        elif rid == "disk_space":
            via, inp = "a created process keeps writing log files until the file system is full", "procs_table"
        out[rid] = {"by": by, "via": via, "input": inp}
    # resolve chains: a rule explained by an inactive rule becomes primary
    for rid, a in out.items():
        if a["by"] and a["by"] not in out and not (a["by"] == "kernel" and kernel_down):
            a["by"] = None
    return out


def break_cycles(att, first_failed, first_seen):
    """Feedback loops (e.g. CPU → backlog → memory → paging → CPU) make every rule look 'caused'.
    Inside a loop, the rule that failed first is the root: its 'by' link is removed."""
    for start in list(att):
        path, cur = [], start
        while cur is not None and cur in att and cur not in path:
            path.append(cur)
            cur = att[cur]["by"]
        if cur is not None and cur in path:
            loop = path[path.index(cur):]
            root = min(loop, key=lambda r: (first_failed.get(r, 1e18), first_seen.get(r, 1e18), ORDER.index(r)))
            att[root]["loop"] = [RULES[r]["name"] for r in loop]
            att[root]["via"] = ("failed first in a feedback loop: " + " → ".join(RULES[r]["name"] for r in loop)
                                + " → " + RULES[loop[0]]["name"])
            att[root]["by"] = None
    return att


def _service_cause(det, P, refP, extra, active):
    rp = _delta_parts(P, refP, "response_ms")
    sp = _delta_parts(P, refP, "service_ms")
    ep = _delta_parts(P, refP, "error_rate")
    cands = {
        "cpu": sp.get("CPU wait", (0, 0))[1],
        "disk": sp.get("disk reads (2 per request)", (0, 0))[1],
        "memory": sp.get("page-fault stalls", (0, 0))[1],
        "deadlock": sp.get("lock wait", (0, 0))[1],
        "backlog": sp.get("backlog wait", (0, 0))[1],
        "network": rp.get("network RTT", (0, 0))[1] + rp.get("retransmissions", (0, 0))[1],
        "disk_space": 0.0,
    }
    cands["disk_space"] += 40 * ep.get("disk full (writes fail: ENOSPC)", (0, 0))[1]
    # error contributions (converted to an ms-like weight so both can be compared)
    cands["network"] += 40 * (ep.get("connection failures", (0, 0))[1] + ep.get("TCP give-up", (0, 0))[1])
    cands["deadlock"] += 40 * ep.get("deadlocked lock waits", (0, 0))[1]
    cands["backlog"] += 40 * ep.get("backlog full / no capacity", (0, 0))[1]
    dom = max(cands, key=lambda k: cands[k])
    if dom == "backlog":
        if "process" in active:
            return "process", "fewer live worker processes → requests queue for a worker", None
        if "cpu" in active and extra["rho"] > 1.0:
            return "cpu", "a saturated CPU slows the workers → requests queue up", None
        if "memory" in active and extra["over"] > 0.5:
            return "memory", "thrashing slows the workers → requests queue up", None
        return None, "client requests arrive faster than the workers can serve them (overload)", "req_rate"
    if dom in active:
        via = {"cpu": "requests wait for the CPU", "disk": "requests wait for slow disk reads",
               "memory": "requests stall on page faults", "deadlock": "requests wait on deadlocked locks",
               "network": "network delay / loss slows or breaks requests",
               "disk_space": "requests that must write fail at once: the file system is full (ENOSPC)"}[dom]
        return dom, via, None
    return None, "response time / errors increased", None


def primaries(att, first_seen, first_failed=None):
    """Root rules (not explained by another active rule), ordered by who reached a FAILURE level
    (DEGRADED or worse) first, then by first warning."""
    ff = first_failed or {}
    prim = [r for r, a in att.items() if not a["by"]]
    return sorted(prim, key=lambda r: (ff.get(r, 1e18), first_seen.get(r, 1e18), ORDER.index(r)))
