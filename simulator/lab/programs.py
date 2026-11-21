"""
Experiment programs — scripted timelines of control changes and actions.

A program is plain JSON so the Experiment Builder can create, save and re-run it:

{
  "name": "...", "description": "...", "duration_s": 60,
  "initial":  {"cpu_workload": 10, ...},          # applied right after the reset
  "settings": {"cascade": true},
  "steps":  [{"at": 5, "set": {"req_rate": 2580}},
             {"at": 12, "action": {"action": "kill", "target": "web-master"}}],
  "ramps":  [{"param": "cpu_workload", "start": 3, "every": 3, "delta": 10, "until": 110}]
}

Replays use "at_tick" instead of "at" (exact sample index) so a recorded run is
reproduced sample for sample.
"""
import ast

STRESS_KINDS = {
    "cpu": "CPU spike", "memory": "Memory spike", "disk": "Disk I/O spike", "network": "Network degradation",
    "traffic": "Traffic spike", "process": "Process crash", "deadlock": "Deadlock", "multi": "Multiple stressors",
    "diskfull": "Disk space exhaustion",
}


def stress_targets(kind, intensity, server, controls):
    """Control values for a sudden stress of the given intensity (0–100).

    The intensity is the share of the way from 'normal' to 'well beyond the limit' for that stressor.
    """
    f = max(0.0, min(100.0, float(intensity))) / 100.0
    cap = server["disk_iops_capacity"]
    base_req = (server.get("profile") or {}).get("offered_load", 860)
    t = {}
    if kind in ("cpu", "multi"):
        t["cpu_workload"] = round(10 + f * 105)
    if kind in ("memory", "multi"):
        t["mem_demand"] = round(f * 62 if kind == "memory" else f * 50)
    if kind in ("disk", "multi"):
        t["disk_write"] = round(cap * 1.08 * f * 0.65 / 50) * 50
        t["disk_read"] = round(cap * 1.08 * f * 0.35 / 50) * 50
    if kind in ("network", "multi"):
        t["net_delay"] = round(f * 900 / 10) * 10
        t["net_loss"] = round(f * 35 * 2) / 2
    if kind == "traffic":
        t["req_rate"] = round(base_req * (1 + 3 * f) / 10) * 10
    return t


PREDEFINED = [
    {"id": "cpu", "name": "1 · CPU saturation", "duration_s": 75,
     "description": "CPU workload is raised step by step until runnable work exceeds the vCPUs, then lowered again.",
     "question": "How do run queue, waiting time and latency respond as utilization approaches 100%?",
     "initial": {}, "ramps": [{"param": "cpu_workload", "start": 4, "every": 3, "delta": 10, "until": 110}],
     "steps": [{"at": 48, "set": {"cpu_workload": 20}}]},
    {"id": "memory", "name": "2 · Memory exhaustion & thrashing", "duration_s": 80,
     "description": "Memory-hungry processes allocate more and more RAM until the working set no longer fits.",
     "question": "When do page faults start to climb, and when does paging leave too little useful CPU?",
     "initial": {}, "ramps": [{"param": "mem_demand", "start": 4, "every": 3, "delta": 5, "until": 62}],
     "steps": [{"at": 52, "set": {"mem_demand": 0}}]},
    {"id": "disk", "name": "3 · Disk I/O bottleneck", "duration_s": 75,
     "description": "Background read/write requests grow towards the device's IOPS capacity.",
     "question": "Why does disk latency explode only near 100% utilization?",
     "initial": {}, "ramps": [{"param": "disk_write", "start": 4, "every": 3, "delta": "cap*0.065", "until": "cap*0.70"},
                              {"param": "disk_read", "start": 4, "every": 3, "delta": "cap*0.035", "until": "cap*0.38"}],
     "steps": [{"at": 48, "set": {"disk_write": 0, "disk_read": 0}}]},
    {"id": "network", "name": "4 · Network failure", "duration_s": 60,
     "description": "Latency and packet loss on the client path grow while the server itself stays healthy.",
     "question": "How do loss and delay turn into retransmissions, timeouts and failed requests?",
     "initial": {}, "ramps": [{"param": "net_delay", "start": 4, "every": 2, "delta": 60, "until": 900},
                              {"param": "net_loss", "start": 4, "every": 2, "delta": 2, "until": 30}],
     "steps": [{"at": 40, "set": {"net_delay": 0, "net_loss": 0}}]},
    {"id": "process", "name": "5 · Critical process crash", "duration_s": 45,
     "description": "A web-worker is killed, then the web-master itself; later the service is restarted.",
     "question": "Why can a service be DOWN while CPU, memory and disk look healthy?",
     "initial": {}, "ramps": [],
     "steps": [{"at": 5, "action": {"action": "kill", "target": "web-worker-1", "signal": "SIGSEGV"}},
               {"at": 14, "action": {"action": "kill", "target": "web-master", "signal": "SIGSEGV"}},
               {"at": 28, "action": {"action": "restart", "target": "web-master"}},
               {"at": 30, "action": {"action": "restart", "target": "web-worker-1"}}]},
    {"id": "deadlock", "name": "6 · Process deadlock", "duration_s": 45,
     "description": "P1 and P2 lock Resource A and B in opposite order; later the deadlock is broken by killing a victim.",
     "question": "Which Coffman conditions hold, and why does CPU utilization fall during a deadlock?",
     "initial": {}, "ramps": [],
     "steps": [{"at": 4, "action": {"action": "acquire", "target": "txn P1", "rid": "A"}},
               {"at": 6, "action": {"action": "acquire", "target": "txn P2", "rid": "B"}},
               {"at": 8, "action": {"action": "acquire", "target": "txn P1", "rid": "B"}},
               {"at": 10, "action": {"action": "acquire", "target": "txn P2", "rid": "A"}},
               {"at": 12, "action": {"action": "acquire", "target": "txn P3", "rid": "A"}},
               {"at": 28, "action": {"action": "kill", "target": "txn P2"}},
               {"at": 31, "action": {"action": "release", "target": "txn P1"}},
               {"at": 33, "action": {"action": "release", "target": "txn P3"}},
               {"at": 34, "action": {"action": "restart", "target": "txn P2"}}]},
    {"id": "cascade", "name": "7 · CPU → memory → disk cascade", "duration_s": 80,
     "description": "Memory is already tight and the disk busy. CPU overload slows requests, requests pile up in "
                    "memory, memory starts paging, and paging floods the disk.",
     "question": "Which subsystem fails first, and which failures are only consequences?",
     "initial": {"mem_demand": 41, "disk_write": "cap*0.55", "req_rate": "req*1.5"},
     "settings": {"cascade": True},
     "ramps": [{"param": "cpu_workload", "start": 5, "every": 3, "delta": 10, "until": 115}],
     "steps": [{"at": 52, "set": {"cpu_workload": 10, "req_rate": "req"}}]},
    {"id": "multi", "name": "8 · Multi-resource overload", "duration_s": 70,
     "description": "CPU, memory, disk and network are all pushed at the same time.",
     "question": "Which metric crosses its threshold first when everything is stressed together?",
     "initial": {}, "ramps": [{"param": "cpu_workload", "start": 4, "every": 2, "delta": 6, "until": 88},
                              {"param": "mem_demand", "start": 4, "every": 2, "delta": 3, "until": 46},
                              {"param": "disk_write", "start": 4, "every": 2, "delta": "cap*0.05", "until": "cap*0.58"},
                              {"param": "disk_read", "start": 4, "every": 2, "delta": "cap*0.03", "until": "cap*0.32"},
                              {"param": "net_loss", "start": 4, "every": 2, "delta": 0.5, "until": 6}],
     "steps": [{"at": 50, "set": {"cpu_workload": 10, "mem_demand": 0, "disk_write": 0, "disk_read": 0, "net_loss": 0}}]},
    {"id": "traffic", "name": "9 · Sudden traffic spike", "duration_s": 50,
     "description": "Client traffic triples in one step; failures trigger client retries.",
     "question": "What happens when requests arrive faster than the workers can serve them?",
     "initial": {}, "ramps": [], "steps": [{"at": 5, "set": {"req_rate": "req*3"}}, {"at": 30, "set": {"req_rate": "req"}}]},
    {"id": "recovery", "name": "10 · Recovery experiment", "duration_s": 60,
     "description": "The server starts overloaded (CPU and memory); the load is then removed gradually.",
     "question": "How long does recovery take, and do the metrics recover in the same order they failed?",
     "initial": {"cpu_workload": 105, "mem_demand": 54},
     "ramps": [{"param": "cpu_workload", "start": 14, "every": 2, "delta": -10, "until": 10},
               {"param": "mem_demand", "start": 14, "every": 2, "delta": -6, "until": 0}],
     "steps": []},
]

BREAK = {"id": "break", "name": "Stress test — break the server", "duration_s": 40,
         "description": "Within 10 seconds every resource is pushed beyond its safe operating limit. "
                        "The virtual server is driven to FAILED; your computer is not affected.",
         "question": "Which subsystem fails first?",
         "initial": {},
         "ramps": [{"param": "cpu_workload", "start": 1, "every": 1, "delta": 11, "until": 120},
                   {"param": "mem_demand", "start": 1, "every": 1, "delta": 6, "until": 58},
                   {"param": "disk_write", "start": 1, "every": 1, "delta": "cap*0.08", "until": "cap*0.75"},
                   {"param": "disk_read", "start": 1, "every": 1, "delta": "cap*0.04", "until": "cap*0.40"},
                   {"param": "net_delay", "start": 1, "every": 1, "delta": 100, "until": 1000},
                   {"param": "net_loss", "start": 1, "every": 1, "delta": 2, "until": 20}],
         "steps": []}


def chaos_program(targets, ramp_s=20, hold_s=20, release=True):
    """Ramp several stressors from normal to the chosen targets together, hold, then release."""
    ramp_s = max(2, int(ramp_s))
    steps_n = max(1, ramp_s // 2)
    ramps = []
    for k, v in targets.items():
        ramps.append({"param": k, "start": 2, "every": 2, "delta": f"({v}-start)/{steps_n}", "until": v})
    steps = []
    if release:
        steps.append({"at": 2 + ramp_s + hold_s, "set": {k: "default" for k in targets}})
    return {"id": "chaos", "name": "Chaos experiment", "duration_s": 2 + ramp_s + hold_s + (25 if release else 5),
            "description": "Several stressors ramped together: " + ", ".join(f"{k} → {v}" for k, v in targets.items()),
            "question": "Which subsystem fails first? Does a cascade occur? How does the system recover?",
            "initial": {}, "ramps": ramps, "steps": steps}


def resolve(expr, server, controls, spec, start=None):
    """Evaluate symbolic values: numbers, 'cap*0.5', 'req*3', 'default', '(v-start)/n'."""
    if isinstance(expr, (int, float)):
        return float(expr)
    if expr == "default":
        return None
    env = {"cap": float(server["disk_iops_capacity"]),
           "req": float((server.get("profile") or {}).get("offered_load", 860)),
           "start": float(start) if start is not None else 0.0}
    try:
        return float(_safe_eval(ast.parse(str(expr), mode="eval").body, env))
    except Exception:
        raise ValueError(f"cannot evaluate '{expr}'")


def _safe_eval(n, env):
    """Arithmetic only: numbers, + − × ÷, parentheses and the names cap / req / start."""
    if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)):
        return n.value
    if isinstance(n, ast.Name) and n.id in env:
        return env[n.id]
    if isinstance(n, ast.UnaryOp) and isinstance(n.op, (ast.USub, ast.UAdd)):
        v = _safe_eval(n.operand, env)
        return -v if isinstance(n.op, ast.USub) else v
    if isinstance(n, ast.BinOp) and isinstance(n.op, (ast.Add, ast.Sub, ast.Mult, ast.Div)):
        a, b = _safe_eval(n.left, env), _safe_eval(n.right, env)
        return a + b if isinstance(n.op, ast.Add) else a - b if isinstance(n.op, ast.Sub) else \
            a * b if isinstance(n.op, ast.Mult) else a / b
    raise ValueError("unsupported expression")


def validate(prog, spec):
    """Light validation for user-built programs."""
    errs = []
    if not isinstance(prog, dict):
        return ["program must be an object"]
    d = prog.get("duration_s")
    if not isinstance(d, (int, float)) or not 5 <= d <= 900:
        errs.append("duration must be 5–900 s")
    for k in (prog.get("initial") or {}):
        if k not in spec:
            errs.append(f"unknown parameter '{k}'")
    for r in prog.get("ramps") or []:
        if r.get("param") not in spec:
            errs.append(f"unknown ramp parameter '{r.get('param')}'")
        if not isinstance(r.get("every"), (int, float)) or r["every"] <= 0:
            errs.append("ramp interval must be > 0")
    for s in prog.get("steps") or []:
        for k in (s.get("set") or {}):
            if k not in spec:
                errs.append(f"unknown parameter '{k}'")
    return errs
