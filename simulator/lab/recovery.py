"""
Failure-specific recovery strategies (Recovery Lab).

Each strategy is a concrete change to the SIMULATION STATE (controls, process table, queues,
lock table) — not a label. The engine applies it, then verifies the outcome with the same
detector that found the failure:

    target check   the failure the strategy is meant to fix stays below WARNING for
                   VERIFY_SAMPLES consecutive samples
    overall check  every detection rule is below WARNING for VERIFY_SAMPLES samples

    RECOVERED  overall check passed               (system healthy again)
    PARTIAL    target fixed, other problems remain after the verification window
    FAILED     target still failing after the verification window

`apply(model, params)` returns (actions, side_effects) and only touches the model, so the same
code drives the live server AND the headless comparison runs (compare()).
All durations are SIMULATED time (samples × 0.5 s), not measurements of a production system.
"""
import copy

from . import programs
from .detect import HEALTH, RULES, Detector
from .model import DT, SystemModel

VERIFY_SAMPLES = 3            # consecutive samples that must pass (0.5 s apart)
VERIFY_WINDOW_S = 30.0        # give up and judge after this much simulated time


def _u(spec, k):
    c = spec[k]
    return "%" if c["unit"] == "%" else (" " + c["unit"] if c["unit"] else "")


def _set(M, k, v, acts):
    old = M.controls[k]
    new = M.set_control(k, v)
    if abs(new - old) > 1e-9:
        acts.append(f"{M.spec[k]['label']} {old:g} → {new:g}{_u(M.spec, k)}")
    return new


def _alive(p):
    return p["state"] not in ("TERMINATED", "RESTARTING")


# ------------------------------------------------------------------ CPU --
def cpu_reduce(M, params):
    acts = []
    _set(M, "cpu_workload", params.get("to", M.spec["cpu_workload"]["default"]), acts)
    return acts, ["Batch work now runs more slowly (less CPU requested per second); nothing is lost."]


def cpu_kill_batch(M, params):
    cores = int(M.server["vcpus"])
    keep = max(1, cores // 2)
    acts, killed = [], []
    for p in [p for p in M.procs.values() if p["kind"] == "custom_cpu" and _alive(p)]:
        M.kill(p["pid"], "SIGTERM")
        killed.append(p["name"])
    batch = [p for p in M.procs.values() if p["kind"] == "batch" and _alive(p)]
    for p in sorted(batch, key=lambda q: -q["pid"])[:max(0, len(batch) - keep)]:
        M.kill(p["pid"], "SIGTERM")
        killed.append(p["name"])
    if killed:
        acts.append(f"SIGTERM → {len(killed)} non-essential CPU-bound process(es): {', '.join(killed)}")
    acts.append(f"{keep} batch job(s) keep running: one process can use at most one of the {cores} vCPUs")
    return acts, [f"The work of the {len(killed)} terminated job(s) is lost and must be re-run later."]


def cpu_renice(M, params):
    acts = []
    _set(M, "web_nice", params.get("nice", -10), acts)
    return acts, ["CPU stays saturated: renicing changes WHO waits, not how much work there is.",
                  "Batch jobs now wait longer in the run queue (their share of CPU time falls)."]


# --------------------------------------------------------------- memory --
def mem_release(M, params):
    acts = []
    _set(M, "mem_demand", 0, acts)
    return acts, ["The memory-hungry processes keep running, but give back their extra allocation."]


def mem_kill_largest(M, params):
    """Simplified OOM-killer rule: the non-critical process with the largest resident memory."""
    cand = [p for p in M.procs.values() if _alive(p) and not p["critical"] and (p.get("mem_mb") or 0) > 0]
    if not cand:
        raise ValueError("No non-critical process holds memory")
    v = max(cand, key=lambda p: p["mem_mb"])
    mb = v["mem_mb"]
    M.kill(v["pid"], "SIGKILL")
    return ([f"SIGKILL → {v['name']} (PID {v['pid']}), the largest non-critical memory user ({mb:,.0f} MB)"],
            ["Simplified rule: largest resident memory, critical services excluded. The real Linux OOM killer "
             "uses an oom_score (memory share + oom_score_adj) and runs only when an allocation fails.",
             "Everything the victim had in memory is lost."])


def mem_restart_leak(M, params):
    leak = [p for p in M.procs.values() if p["kind"] == "custom_leak" and _alive(p)]
    if not leak:
        raise ValueError("No leaking process — create one in the Process Lab (Memory leak)")
    acts = []
    for p in leak:
        mb = p["load"]
        M.kill(p["pid"], "SIGTERM")
        M.restart(p["pid"])
        acts.append(f"Restarted {p['name']} (held {mb:,.0f} MB): SIGTERM, then fork + exec with a fresh heap")
    return acts, ["The leak is NOT fixed: memory will grow again at the same rate until the bug is fixed.",
                  "The process is unavailable for ~2 s while it restarts."]


# ----------------------------------------------------------------- disk --
def disk_reduce(M, params):
    acts = []
    _set(M, "disk_read", 0, acts)
    _set(M, "disk_write", 0, acts)
    io = [p for p in M.procs.values() if p["kind"] == "custom_io" and _alive(p)]
    for p in io:
        M.kill(p["pid"], "SIGTERM")
    if io:
        acts.append("SIGTERM → I/O-bound processes: " + ", ".join(p["name"] for p in io))
    return acts, ["Background jobs (backups, scans, log shipping) are paused; they must be rescheduled."]


def disk_drain(M, params):
    q = M.disk_backlog
    M.disk_backlog, M.io_lq = 0.0, 0.0
    return ([f"Cancelled {q:,.0f} queued background I/O requests (block-layer queue emptied)"],
            ["The cancelled requests must be re-issued by their applications.",
             "The workload is unchanged: if demand still exceeds the device capacity, the queue refills."])


# ----------------------------------------------------------- disk space --
def _writers(M):
    return [p for p in M.procs.values() if p["kind"] == "custom_logflood" and p["state"] != "TERMINATED"]


def _gb(M, pct):
    return pct / 100.0 * float(M.server.get("storage_gb", 250))


def space_stop_writer(M, params):
    w = _writers(M)
    if not w:
        raise ValueError("No process is writing log files")
    for p in w:
        M.kill(p["pid"], "SIGTERM")
    return ([f"SIGTERM → {', '.join(p['name'] for p in w)}: no new log data is written"],
            [f"Its log files stay on disk ({M.disk_logs_closed:.1f}% of the file system, "
             f"{_gb(M, M.disk_logs_closed):,.0f} GB): stopping the writer frees no space, so writes keep failing."])


def space_delete_logs(M, params):
    open_pct = sum(p.get("written", 0.0) for p in _writers(M))
    if M.disk_logs_closed <= 0 and open_pct <= 0:
        raise ValueError("No log files written by created processes")
    freed, held, names = M.delete_logs()
    side = []
    if held > 0:
        side.append(f"{', '.join(names)} still has its log file open: the file is unlinked (no longer listed by ls) "
                    f"but its {held:.1f}% of the disk is only returned when the process closes it — on Linux, "
                    "'df' still shows the space as used ('lsof +L1' lists such files). The writer also keeps writing.")
    return ([f"rm log files: {freed:.1f}% of the file system ({_gb(M, freed):,.0f} GB) freed"], side or
            ["Deleted logs are gone: they can no longer be used for debugging or auditing."])


def space_cleanup(M, params):
    w = _writers(M)
    if not w and M.disk_logs_closed <= 0:
        raise ValueError("No log writer and no log files to delete")
    acts = []
    if w:
        for p in w:
            M.kill(p["pid"], "SIGTERM")
        acts.append(f"SIGTERM → {', '.join(p['name'] for p in w)} (its log file is closed)")
    freed, _, _ = M.delete_logs()
    acts.append(f"rm log files: {freed:.1f}% of the file system ({_gb(M, freed):,.0f} GB) freed")
    return acts, ["Deleted logs are gone. In production, rotate and compress logs (logrotate) and alert on free "
                  "space before the disk fills."]


# -------------------------------------------------------------- network --
def net_restore(M, params):
    acts = []
    for k in ("net_delay", "net_loss", "conn_fail"):
        _set(M, k, M.spec[k]["default"], acts)
    return acts, ["Simulated path repaired (like removing a tc netem rule). Requests already timed out are not "
                  "replayed — clients retry them."]


def net_timeout(M, params):
    acts = []
    _set(M, "timeout_ms", params.get("to", 5000), acts)
    return acts, ["Slow requests now succeed instead of timing out, but users wait longer.",
                  "The network is still degraded: RTT and packet loss do not change."]


# -------------------------------------------------------------- process --
def proc_restart(M, params):
    dead = [p for p in M.procs.values() if p["state"] == "TERMINATED" and not p["kind"].startswith("custom")
            and p["kind"] in ("master", "worker", "db", "agent")]
    if not dead:
        raise ValueError("No terminated service process to restart")
    names = []
    for p in sorted(dead, key=lambda q: (q["kind"] != "master", q["pid"])):
        if p["state"] == "TERMINATED":
            M.restart(p["pid"])
            names.append(p["name"])
    return ([f"Restarted {', '.join(names)} (TERMINATED → RESTARTING → RUNNING; a master restarts its workers)"],
            ["New PIDs are assigned. Requests that were in progress in the crashed processes were lost."])


def proc_kill_hung(M, params):
    hung = [p for p in M.procs.values() if p["state"] == "HUNG"]
    if not hung:
        raise ValueError("No unresponsive (HUNG) process")
    names = []
    for p in sorted(hung, key=lambda q: q["kind"] != "master"):
        if p["state"] != "HUNG":
            continue
        pid = p["pid"]
        M.kill(pid, "SIGKILL")
        if pid in M.procs and M.procs[pid]["state"] == "TERMINATED":       # created processes are removed by kill()
            M.restart(pid)
        names.append(p["name"])
    return ([f"SIGKILL → unresponsive {', '.join(names)}, then restarted by the supervisor (new PID)"],
            ["A hung process ignores SIGTERM-style polite requests in this model, so it is killed (SIGKILL).",
             "Work it was doing and requests it held are lost; a master's workers restart with it."])


# -------------------------------------------------------------- service --
def svc_shed(M, params):
    acts = []
    _set(M, "req_rate", M.spec["req_rate"]["default"], acts)
    return acts, ["Excess clients are refused or rate-limited at the edge instead of queueing inside the server."]


def svc_restart(M, params):
    dropped = M.accept_backlog
    M.accept_backlog = 0.0
    M.prev = dict(M.prev, error_frac=0.0, inflight=0.0)
    acts = [f"Restarted the web service: dropped {dropped:,.0f} queued requests and cleared in-flight state"]
    dead = [p for p in M.procs.values() if p["kind"] in ("master", "worker") and p["state"] == "TERMINATED"]
    for p in sorted(dead, key=lambda q: q["kind"] != "master"):
        if p["state"] == "TERMINATED":
            M.restart(p["pid"])
    if dead:
        acts.append("Restarted dead web processes: " + ", ".join(p["name"] for p in dead))
    return acts, ["Every queued request is lost (clients see errors and retry).",
                  "If the incoming load is still higher than capacity, the backlog builds up again."]


# ------------------------------------------------------------- deadlock --
def _dl(M, how, params):
    r = M.deadlock_report()
    if not r["deadlocked"]:
        raise ValueError("No deadlock detected")
    if params.get("victim") is not None:
        v = params["victim"]
    else:
        v = min(r["deadlocked"], key=lambda pid: (sum(x["alloc"].get(pid, 0) for x in M.locks.values()), -pid))
    name = M._short(v)
    if how == "kill":
        M.kill(v, "SIGKILL")
        return [f"Terminated deadlock victim {name}; its resources were released and granted to waiters"], \
               [f"{name}'s transaction is aborted and must be re-run."]
    rid = next((h for h in M.procs[v]["holds"]
                if any(M.procs[w].get("waiting_for") == h for w in r["deadlocked"] if w != v)), M.procs[v]["holds"][0])
    M.preempt(rid, v)
    return [f"Pre-empted {rid} from {name} and rolled {name} back"], \
           [f"{name} must request {rid} again; choosing the same victim repeatedly can starve it."]


def dl_kill(M, params):
    return _dl(M, "kill", params)


def dl_preempt(M, params):
    return _dl(M, "preempt", params)


# ------------------------------------------------------------- catalogue --
S = []


def _s(sid, rule, name, cat, fn, does, when):
    S.append({"id": sid, "rule": rule, "name": name, "category": cat, "fn": fn, "does": does, "when": when})


_s("cpu_reduce", "cpu", "Reduce the CPU workload", "reduce load", cpu_reduce,
   "Lowers batch CPU demand to its normal level.", "Demand is temporary or can be postponed.")
_s("cpu_kill_batch", "cpu", "Terminate non-essential CPU jobs", "terminate", cpu_kill_batch,
   "SIGTERM to batch / created CPU-bound processes, keeping half as many as there are vCPUs.",
   "A few jobs use too much CPU and can be re-run later.")
_s("cpu_renice", "cpu", "Raise the web service priority (nice −10)", "reconfigure", cpu_renice,
   "The scheduler gives the web processes a larger share; batch jobs wait instead.",
   "Latency matters more than batch throughput. Does not remove the overload.")
_s("mem_release", "memory", "Release simulated memory", "reduce load", mem_release,
   "Memory-hungry processes give back their extra allocation.", "The allocation is a cache or can shrink.")
_s("mem_kill_largest", "memory", "Stop the largest memory user (simplified OOM rule)", "terminate", mem_kill_largest,
   "SIGKILL to the non-critical process with the largest resident memory.", "Memory must be freed now, at any cost.")
_s("mem_restart_leak", "memory", "Restart the leaking process", "restart", mem_restart_leak,
   "Kills and restarts a process whose memory keeps growing, giving it a fresh heap.",
   "A memory leak — a workaround until the bug is fixed.")
_s("disk_reduce", "disk", "Reduce the I/O demand", "reduce load", disk_reduce,
   "Stops background reads/writes and I/O-bound processes.", "Background I/O (backup, scan) competes with the service.")
_s("disk_drain", "disk", "Drain the I/O queue only", "reconfigure", disk_drain,
   "Cancels the queued background requests but leaves the workload running.",
   "A one-off burst. Shows why draining a queue does not fix an ongoing overload.")
_s("space_stop_writer", "disk_space", "Stop the log writer only", "terminate", space_stop_writer,
   "SIGTERM to the process that keeps writing log files.",
   "To stop the disk filling further. Shows that stopping the cause does not free space.")
_s("space_delete_logs", "disk_space", "Delete the log files only", "clean up", space_delete_logs,
   "rm the log files written by created processes, leaving the writer running.",
   "Space is needed now. Shows why deleting a file that is still open frees nothing.")
_s("space_cleanup", "disk_space", "Stop the writer, then delete its logs", "repair", space_cleanup,
   "Terminates the log writer (closing its file), then deletes the log files.",
   "A runaway log filled the disk: remove the cause AND the data it wrote.")
_s("net_restore", "network", "Restore connectivity", "repair", net_restore,
   "Removes injected delay, packet loss and connection failures.", "The path fault is found and fixed.")
_s("net_timeout", "network", "Raise the client timeout (5 s)", "reconfigure", net_timeout,
   "Clients wait longer before giving up and retrying.", "Slow but working network; avoids retry storms.")
_s("proc_restart", "process", "Restart failed processes", "restart", proc_restart,
   "Restarts terminated web-master / workers / database (master first).", "A process crashed but the OS is healthy.")
_s("proc_kill_hung", "process", "Terminate & restart an unresponsive process", "terminate", proc_kill_hung,
   "SIGKILL to the HUNG process, then the supervisor restarts it.",
   "A process is alive but not responding. Plain restart does not apply — it never exited.")
_s("svc_shed", "service", "Reduce incoming workload (shed load)", "reduce load", svc_shed,
   "Brings client traffic back to the normal rate (rate limiting / load shedding).", "Arrivals exceed worker capacity.")
_s("svc_restart", "service", "Restart the web service", "restart", svc_restart,
   "Drops the request backlog and in-flight state, restarts dead web processes.",
   "A self-sustaining backlog (retry storm) keeps the service down.")
_s("dl_kill", "deadlock", "Terminate a deadlock victim", "terminate", dl_kill,
   "Aborts the cheapest deadlocked transaction; its locks go to the waiters.", "A deadlock was detected.")
_s("dl_preempt", "deadlock", "Pre-empt a resource", "pre-empt", dl_preempt,
   "Takes one resource from a victim and rolls it back.", "The holder's work can be undone (transactions).")
STRATEGIES = {s["id"]: s for s in S}


def catalogue():
    return [{k: v for k, v in s.items() if k != "fn"} for s in S]


def for_rule(rule):
    return [s["id"] for s in S if s["rule"] == rule]


def apply(M, sid, params=None):
    s = STRATEGIES.get(sid)
    if s is None:
        raise ValueError("Unknown recovery strategy")
    return s["fn"](M, params or {})


# ---------------------------------------------------------- verification --
def new_verification(rule):
    return {"target": rule, "t_ok": 0, "all_ok": 0, "samples": 0, "target_ok_at": None, "healthy_at": None}


def judge(v, det):
    """Update a verification record with one detector result. Returns None while undecided,
    else RECOVERED / PARTIAL / FAILED."""
    v["samples"] += 1
    t_ok = det["rules"][v["target"]]["confirmed"] == 0
    v["t_ok"] = v["t_ok"] + 1 if t_ok else 0
    v["all_ok"] = v["all_ok"] + 1 if det["health"] == 0 else 0
    if v["t_ok"] == 1:
        v["target_ok_at"] = v["samples"] * DT
    if v["all_ok"] == 1:
        v["healthy_at"] = v["samples"] * DT
    if not t_ok:
        v["target_ok_at"] = None
    if det["health"] != 0:
        v["healthy_at"] = None
    if v["all_ok"] >= VERIFY_SAMPLES:
        return "RECOVERED"
    if v["samples"] * DT >= VERIFY_WINDOW_S:
        return "PARTIAL" if v["t_ok"] >= VERIFY_SAMPLES else "FAILED"
    return None


# ------------------------------------------------------- headless compare --
def _inject(M, sc):
    for k, v in (sc.get("set") or {}).items():
        M.set_control(k, eval_v(v, M))
    for a in sc.get("do") or []:
        if a[0] == "kill":
            p = next(p for p in M.procs.values() if p["name"] == a[1])
            M.kill(p["pid"], "SIGSEGV")
        elif a[0] == "hang":
            p = next(p for p in M.procs.values() if p["name"] == a[1])
            M.hang(p["pid"])
        elif a[0] == "create":
            M.create_process(a[1])
        elif a[0] == "deadlock":
            M.form_deadlock(False)


def eval_v(v, M):
    return programs.resolve(v, M.server, M.controls, M.spec)      # numbers or 'cap*0.75' / 'req*3'


SCENARIOS = [
    {"id": "cpu", "name": "CPU overload (workload 140%)", "rule": "cpu", "set": {"cpu_workload": 140}},
    {"id": "memory", "name": "Memory pressure (60% RAM extra)", "rule": "memory", "set": {"mem_demand": 60}},
    {"id": "leak", "name": "Memory leak in one process", "rule": "memory", "do": [("create", "leak")]},
    {"id": "disk", "name": "Disk I/O overload (115% of IOPS)", "rule": "disk",
     "set": {"disk_write": "cap*0.75", "disk_read": "cap*0.40"}},
    {"id": "network", "name": "Network degradation (600 ms, 20% loss)", "rule": "network",
     "set": {"net_delay": 600, "net_loss": 20}},
    {"id": "traffic", "name": "Traffic spike (3× requests)", "rule": "service", "set": {"req_rate": "req*3"}},
    {"id": "crash", "name": "web-master crash (SIGSEGV)", "rule": "process", "do": [("kill", "web-master")]},
    {"id": "hang", "name": "web-master hangs (unresponsive)", "rule": "process", "do": [("hang", "web-master")]},
    {"id": "deadlock", "name": "Two-process deadlock on A/B", "rule": "deadlock", "do": [("deadlock",)]},
    {"id": "diskfull", "name": "Runaway log writer fills the file system", "rule": "disk_space",
     "do": [("create", "logflood")]},
]
SCENARIO = {s["id"]: s for s in SCENARIOS}
SERIES_KEYS = ["cpu_util", "mem_util", "disk_util", "response_ms", "error_rate", "page_faults", "io_queue", "rtt",
               "disk_used"]


def run_one(server, sc, sid, seed=2026, fail_s=None):
    """One reproducible run: warm up, inject the scenario, wait a fixed time, apply the strategy,
    verify. Every strategy for a scenario starts from the identical state (same seed, same tick)."""
    M = SystemModel(server, seed)
    D = Detector()
    for _ in range(12):
        M.step()
    D.reset()
    _inject(M, sc)
    if sc["id"] == "leak":
        fail_s = fail_s or 75.0
    if sc["id"] == "diskfull":
        fail_s = fail_s or 50.0           # 62 % → 100 % at 1 % per second, then a few seconds full
    apply_tick = int((fail_s or 25.0) / DT)
    series, events = [], []
    peak, before, actions, side, err, outcome, ver = 0, None, [], [], None, None, None
    target_seen = False
    for i in range(apply_tick + int(VERIFY_WINDOW_S / DT) + 2):
        V, P, X = M.step()
        V["run_queue_per_core"] = V["run_queue"] / server["vcpus"]
        det = D.evaluate(V, M.snapshot(), X)
        if det["rules"][sc["rule"]]["confirmed"] > 0:
            target_seen = True
        series.append({"t": round(i * DT, 1), "health": det["health"], **{k: _r(V.get(k)) for k in SERIES_KEYS}})
        if i < apply_tick:
            peak = max(peak, det["health"])
            continue
        if i == apply_tick:
            before = {"health": HEALTH[det["health"]], **{k: _r(V.get(k)) for k in SERIES_KEYS},
                      "target_level": det["rules"][sc["rule"]]["confirmed"]}
            try:
                actions, side = apply(M, sid)
            except ValueError as ex:
                err = str(ex)
                break
            ver = new_verification(sc["rule"])
            continue
        outcome = judge(ver, det)
        if outcome:
            break
    after = {"health": HEALTH[series[-1]["health"]], **{k: series[-1][k] for k in SERIES_KEYS}}
    return {"strategy": sid, "name": STRATEGIES[sid]["name"], "category": STRATEGIES[sid]["category"],
            "error": err, "outcome": outcome if not err else "NOT APPLICABLE", "actions": actions, "side_effects": side,
            "target_seen": target_seen, "peak": HEALTH[peak], "apply_at": apply_tick * DT,
            "target_ok_after": ver and ver["target_ok_at"], "healthy_after": ver and ver["healthy_at"],
            "before": before, "after": after, "series": series[::2]}


def _r(v):
    return None if v is None else round(float(v), 2)


def compare(server, scenario_id, strategy_ids, seed=2026):
    sc = SCENARIO.get(scenario_id)
    if sc is None:
        raise ValueError("Unknown scenario")
    ids = [s for s in strategy_ids if s in STRATEGIES]
    if not ids:
        raise ValueError("Choose at least one strategy")
    if len(ids) > 4:
        raise ValueError("Compare at most 4 strategies")
    server = copy.deepcopy(server)
    runs = [run_one(server, sc, sid, seed) for sid in ids]
    rank = {"RECOVERED": 0, "PARTIAL": 1, "FAILED": 2, "NOT APPLICABLE": 3}
    best = min((r for r in runs if r["outcome"] in rank),
               key=lambda r: (rank[r["outcome"]], r["healthy_after"] or 1e9, r["target_ok_after"] or 1e9))
    return {"scenario": {k: sc[k] for k in ("id", "name", "rule")}, "rule_name": RULES[sc["rule"]]["name"],
            "seed": seed, "runs": runs, "best": best["strategy"],
            "note": "Each run starts from the same seeded state; the failure is injected at t = 0 and the strategy is "
                    f"applied at t = {runs[0]['apply_at']:.0f} s (simulated time). Verification: the target rule and "
                    f"then every rule must stay below WARNING for {VERIFY_SAMPLES} samples; the run is judged after "
                    f"{VERIFY_WINDOW_S:.0f} s at most."}
