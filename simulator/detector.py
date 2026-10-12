"""
Threshold-based failure detection engine.

IMPORTANT: the detector never receives the injected scenario. It only sees the
observed metrics and the process / resource tables, evaluates one rule per
failure class, and classifies the failure from what it observes.
"""
from . import config
from .scenarios import SCENARIOS

LEVELS = ["OK", "WARNING", "DEGRADED", "FAILED"]
LV = {name: i for i, name in enumerate(LEVELS)}


def level_high(v, th):
    """Higher value is worse."""
    if v is None:
        return 0
    if v > th["F"]:
        return 3
    if v > th["D"]:
        return 2
    if v > th["W"]:
        return 1
    return 0


def level_low(v, th):
    """Lower value is worse (availability, reachability)."""
    if v is None:
        return 0
    if v <= th["F"]:
        return 3
    if v <= th["D"]:
        return 2
    if v < th["W"]:
        return 1
    return 0


def check(label, key, value, unit, th, lower_is_worse=False):
    lvl = level_low(value, th) if lower_is_worse else level_high(value, th)
    return {"label": label, "key": key, "value": value, "unit": unit,
            "th": th, "dir": "<" if lower_is_worse else ">", "level": lvl}


# ------------------------------------------------------ deadlock analysis ---
def wait_for_graph(snapshot):
    """Edges P_i -> P_j when P_i waits for a resource currently held by P_j."""
    holders = {r["id"]: r["holder"] for r in snapshot["resources"]}
    edges = []
    for p in snapshot["processes"]:
        rid = p.get("waiting_for")
        if rid and holders.get(rid) is not None and p["state"] in ("WAITING", "BLOCKED"):
            edges.append((p["pid"], holders[rid], rid))
    return edges


def find_cycle(edges):
    """Depth-first search for a cycle in the wait-for graph."""
    adj = {}
    for a, b, _ in edges:
        adj.setdefault(a, []).append(b)
    WHITE, GREY, BLACK = 0, 1, 2
    color, stack = {}, []

    def dfs(u):
        color[u] = GREY
        stack.append(u)
        for v in adj.get(u, []):
            if color.get(v, WHITE) == GREY:
                return stack[stack.index(v):]
            if color.get(v, WHITE) == WHITE:
                c = dfs(v)
                if c:
                    return c
        stack.pop()
        color[u] = BLACK
        return None

    for node in list(adj):
        if color.get(node, WHITE) == WHITE:
            c = dfs(node)
            if c:
                return c
    return []


def coffman(snapshot, cycle):
    procs = snapshot["processes"]
    res = snapshot["resources"]
    held = [r for r in res if r["holder"] is not None]
    return {
        "mutual_exclusion": any(r["exclusive"] for r in held),
        "hold_and_wait": any(p["holds"] and p.get("waiting_for") for p in procs),
        "no_preemption": any(not r["preemptible"] for r in held),
        "circular_wait": bool(cycle),
    }


# --------------------------------------------------------------- detector ---
class Detector:
    def __init__(self):
        self.reset()

    def reset(self):
        self.streak_level = 0
        self.streak = 0
        self.confirmed = 0
        self.rule_confirmed = {k: 0 for k in SCENARIOS}
        self.rule_streak = {k: (0, 0) for k in SCENARIOS}
        self.cycle_since = None
        self.cycle_key = None

    def evaluate(self, m, snap, now):
        T = config.THRESHOLDS
        rules = {}

        # 1. CPU saturation ------------------------------------------------
        c = check("CPU utilization", "cpu", m["cpu"], "%", T["cpu"]["cpu"])
        rules["cpu"] = {"logic": "single", "checks": [c], "level": c["level"],
                        "info": [("Run queue", m["cpu_queue"], "procs"),
                                 ("Waiting time", m["wait_ms"], "ms")]}

        # 2. Memory thrashing (memory AND page faults) --------------------
        c1 = check("Memory utilization", "mem", m["mem"], "%", T["memory"]["mem"])
        c2 = check("Page-fault rate", "page_faults", m["page_faults"], "/s", T["memory"]["page_faults"])
        rules["memory"] = {"logic": "AND", "checks": [c1, c2], "level": min(c1["level"], c2["level"]),
                           "info": [("Swap I/O", m["swap_io"], "MB/s"),
                                    ("Paging overhead", m["paging_overhead"], "% CPU")]}

        # 3. Disk I/O (queue OR latency) ----------------------------------
        c1 = check("I/O queue length", "io_queue", m["io_queue"], "reqs", T["disk"]["io_queue"])
        c2 = check("Disk latency", "disk_latency", m["disk_latency"], "ms", T["disk"]["disk_latency"])
        rules["disk"] = {"logic": "OR", "checks": [c1, c2], "level": max(c1["level"], c2["level"]),
                         "info": [("Disk utilization", m["disk"], "%")]}

        # 4. Process crash (process table) --------------------------------
        crit_term = [p for p in snap["processes"] if p["critical"] and p["state"] in ("TERMINATED", "RESTARTING")]
        c1 = {"label": "Critical process state", "key": "web_state",
              "value": crit_term[0]["state"] if crit_term else "RUNNING", "unit": "",
              "th": None, "dir": "==", "expect": "TERMINATED", "level": 3 if crit_term else 0}
        c2 = check("Service availability", "availability", m["availability"], "%",
                   T["process"]["availability"], lower_is_worse=True)
        rules["process"] = {"logic": "OR", "checks": [c1, c2], "level": max(c1["level"], c2["level"]),
                            "info": [("Terminated processes", m["terminated_procs"], "")],
                            "crashed": [p["name"] for p in crit_term]}

        # 5. Deadlock (wait-for graph cycle + duration) -------------------
        edges = wait_for_graph(snap)
        cycle = find_cycle(edges)
        key = tuple(sorted(cycle))
        if cycle:
            if self.cycle_key != key or self.cycle_since is None:
                self.cycle_since, self.cycle_key = now, key
        else:
            self.cycle_since, self.cycle_key = None, None
        dur = (now - self.cycle_since) if self.cycle_since else 0.0
        th_s = T["deadlock"]["blocked_seconds"]
        deadlocked = bool(cycle) and dur > th_s
        stuck = m["stuck_procs"]
        lvl = 0
        if deadlocked:  # stuck >= W -> WARNING, >= D -> DEGRADED, >= F -> FAILED
            th = T["deadlock"]["stuck"]
            lvl = 3 if stuck >= th["F"] else 2 if stuck >= th["D"] else 1
        rules["deadlock"] = {
            "logic": "AND",
            "checks": [
                {"label": "Circular wait in wait-for graph", "key": "cycle",
                 "value": "YES" if cycle else "NO", "unit": "", "th": None, "dir": "==",
                 "expect": "YES", "level": 3 if cycle else 0},
                {"label": "Cycle persisted", "key": "cycle_age", "value": round(dur, 1), "unit": "s",
                 "th": {"W": th_s, "D": th_s, "F": th_s}, "dir": ">", "level": 3 if dur > th_s else 0},
                {"label": "Processes unable to progress", "key": "stuck_procs", "value": stuck, "unit": "",
                 "th": dict(T["deadlock"]["stuck"]), "dir": "≥",
                 "level": lvl},
            ],
            "level": lvl,
            "info": [("Locks held", m["locks_held"], "")],
        }
        names = {p["pid"]: p["name"].split()[-1] for p in snap["processes"]}
        deadlock_info = {
            "edges": [{"from": names.get(a, a), "to": names.get(b, b), "resource": r} for a, b, r in edges],
            "cycle": [names.get(p, p) for p in cycle],
            "cycle_age": round(dur, 1),
            "detected": deadlocked,
            "coffman": coffman(snap, cycle),
        }

        # 6. Network (latency OR loss OR reachability) --------------------
        c1 = check("Network latency (RTT)", "net_latency", m["net_latency"], "ms", T["network"]["net_latency"])
        c2 = check("Packet loss", "packet_loss", m["packet_loss"], "%", T["network"]["packet_loss"])
        c3 = check("Reachability", "reachability", m["reachability"], "%", T["network"]["reachability"],
                   lower_is_worse=True)
        rules["network"] = {"logic": "OR", "checks": [c1, c2, c3],
                            "level": max(c1["level"], c2["level"], c3["level"]),
                            "info": [("Failed requests", m["req_failed"], "/s"),
                                     ("Timeouts", m["timeouts"], "/s")]}

        # ---- per-rule debounce & transitions ------------------------------
        transitions = []
        for k, r in rules.items():
            lvl_k = r["level"]
            # a level is confirmed when it (or worse) is seen in N consecutive samples
            hist = list(self.rule_streak[k]) if isinstance(self.rule_streak[k], list) else []
            hist = (hist + [lvl_k])[-config.DETECT_CONFIRM_SAMPLES:]
            self.rule_streak[k] = hist
            cand = min(hist) if len(hist) >= config.DETECT_CONFIRM_SAMPLES else self.rule_confirmed[k]
            if cand != self.rule_confirmed[k]:
                if cand > self.rule_confirmed[k]:
                    transitions.append({"rule": k, "from": self.rule_confirmed[k], "to": cand,
                                        "evidence": self.evidence(k, r)})
                self.rule_confirmed[k] = cand
            r["confirmed"] = self.rule_confirmed[k]
            r["name"] = SCENARIOS[k]["name"]
            r["level_name"] = LEVELS[r["confirmed"]]

        raw = max(r["level"] for r in rules.values())
        confirmed = max(self.rule_confirmed.values())
        order = ["deadlock", "process", "memory", "disk", "network", "cpu"]  # specificity tiebreak
        classification = None
        if confirmed > 0:
            best = [k for k in order if self.rule_confirmed[k] == confirmed]
            classification = best[0]
        self.confirmed = confirmed
        return {
            "rules": rules,
            "raw_level": raw,
            "level": confirmed,
            "level_name": LEVELS[confirmed],
            "classification": classification,
            "classification_name": SCENARIOS[classification]["name"] if classification else None,
            "evidence": self.evidence(classification, rules[classification]) if classification else [],
            "transitions": transitions,
            "deadlock": deadlock_info,
        }

    @staticmethod
    def evidence(k, r):
        out = []
        for c in r["checks"]:
            if c["level"] > 0:
                v = c["value"]
                if c.get("th"):
                    lvl = c["level"]
                    th = c["th"][["W", "W", "D", "F"][lvl]]
                    if isinstance(v, float):
                        v = round(v, 1)
                    op = c["dir"]
                    if op == "<" and lvl >= 2:
                        op = "≤"
                    out.append(f"{c['label']} {v}{c['unit']} {op} {th:g}{c['unit']} ({LEVELS[lvl]})")
                else:
                    out.append(f"{c['label']} = {v}")
        return out
