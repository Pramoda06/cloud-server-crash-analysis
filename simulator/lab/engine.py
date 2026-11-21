"""
LabEngine — the single source of truth for one virtual server.

Every TICK (0.5 s):
    program runner (if an experiment program runs) applies its scheduled changes
    model.step()                → node values + contribution parts
    detector.evaluate()         → rule levels (observed values only)
    attribution                 → primary / secondary failures
    episode tracking            → trigger, first crossing, detection, peak, recovery
    events + sample             → timeline and charts (shared time axis)
    recording                   → control log + samples for replay / history

All API calls (controls, actions, stress, thresholds, reset, record, run) go through
this object under one lock, so the UI, the charts, the timeline and the analysis always
describe the same state.
"""
import copy
import logging
import math
import threading
import time
from collections import deque
from datetime import datetime

from . import detect, programs
from .detect import HEALTH, LEVELS, RULES, Detector
from .graph import CASCADE_EDGES, NODES, SUBSYSTEMS, controls_spec, layers
from .model import DT, SystemModel
from . import recovery as strategies
from .kernel import KernelSim, catalogue as kernel_catalogue
from .rca import build_rca
from .trace import trace

log = logging.getLogger("simulator")

SAMPLE_KEYS = ["cpu_util", "cpu_demand", "run_queue", "cpu_wait", "cpu_wait_web", "mem_util", "mem_avail",
               "page_faults", "paging_overhead", "swap_iops", "disk_util", "disk_demand", "read_iops", "write_iops",
               "io_queue", "disk_latency", "rtt", "packet_loss", "retrans_ms", "req_eff", "throughput", "req_failed",
               "timeouts", "error_rate", "availability", "response_ms", "service_ms", "queue_wait", "accept_backlog",
               "capacity", "inflight", "lock_wait", "paging_penalty", "running_procs", "blocked_procs",
               "terminated_procs", "workers_alive", "iowait_procs", "mem_ws", "disk_used", "disk_free_gb"]
CONTROL_KEYS = ["cpu_workload", "cpu_procs", "web_nice", "mem_demand", "mem_procs", "disk_read", "disk_write",
                "req_rate", "net_delay", "net_loss", "conn_fail", "timeout_ms"]
WARMUP_TICKS = 12


def clock(ts):
    return datetime.fromtimestamp(ts).strftime("%H:%M:%S")


def _short(v, unit=""):
    if v is None:
        return "—"
    s = f"{v:,.0f}" if abs(v) >= 100 else (f"{v:.1f}" if abs(v) >= 1 or v == 0 else f"{v:.2f}")
    s = s.replace(".0", "") if s.endswith(".0") else s
    return s + ("%" if unit == "%" else f" {unit}" if unit else "")


def rnd(v):
    if isinstance(v, float):
        if not math.isfinite(v):
            return None
        return round(v, 2) if abs(v) < 1000 else round(v, 1)
    return v


class LabEngine:
    def __init__(self, store, server, host=None, seed=2026):
        self.store = store
        self.server = server
        self.server_id = server.get("db_id")
        self.offline = server.get("connection") == "Offline"
        self.host = host
        self.lock = threading.RLock()
        self.spec = {c["key"]: c for c in controls_spec(server)}
        self.seed = seed
        self.model = SystemModel(server, seed)
        self.detector = Detector()
        self.samples = deque(maxlen=1440)        # 12 minutes at 0.5 s
        self.events = deque(maxlen=800)
        self.seq = 0
        self.eseq = 0
        self.epoch = 0
        self.paused = False
        self.recording = None
        self.runner = None
        self.stress = None
        self.last_run_id = None
        self.last_recovery = None                # analysis of the failure most recently fixed by Recovery
        self.recovery_seq = 0
        self.episodes = []
        self.episode = None
        self.ctl_logged = {}
        self.input_events = deque(maxlen=200)    # (ts, tick, text, node) — candidate triggers
        self.recent = deque(maxlen=12)           # (V, P) of the last samples
        self.boot_ts = time.time()               # simulated uptime starts here (and again after a recovery)
        self.kernel = KernelSim(self)
        self._reset_state(initial=True)

    # =============================================================== reset ==
    def _reset_state(self, initial=False, seed=None):
        self.seed = seed if seed is not None else self.seed
        self.model.reset(self.seed)
        self.detector.reset()
        self.samples.clear()
        self.recent.clear()
        self.input_events.clear()
        self.episodes = []
        self.episode = None
        self.stress = None
        self.last_recovery = None
        self.last_run_id = None
        self.epoch += 1
        self.first_seen = {}
        self.first_failed = {}
        self.first_failed_tick = {}               # sample number of first failure level (orders concurrent causes)
        self._frozen_idle = {}
        self.V, self.P, self.X, self.det, self.att = {}, {}, {}, None, {}
        # warm up silently to the steady state, then capture the BASELINE used for "why did it change?"
        for _ in range(WARMUP_TICKS):
            V, P, X = self.model.step()
            V["run_queue_per_core"] = V["run_queue"] / self.server["vcpus"]
        self.model.events.clear()
        self.base_V, self.base_P = copy.deepcopy(V), copy.deepcopy(P)
        self.V, self.P, self.X = V, P, X
        self.det = self.detector.evaluate(V, self.model.snapshot(), X)
        self.detector.reset()
        self.att = {}
        self._pre_max, self._pending_cross, self._last_h = {}, [], 0
        self.ctl_logged = dict(self.model.controls)
        self.health = 0
        self.t0_tick = self.model.tick
        if not initial:
            self.emit("RECOVERY", "Recovery: stress removed, processes restarted, locks released and every "
                                  "control restored to the healthy baseline", "ok", marker="reset")

    def reset(self, seed=None):
        with self.lock:
            if self.runner:
                self._stop_runner("reset")
            if self.recording:
                self.stop_recording(reason="reset")
            self.paused = False
            keep = {k: getattr(self.model, k) for k in SETTING_KEYS}
            self._reset_state(seed=seed)
            self.boot_ts = time.time()
            for k, v in keep.items():
                setattr(self.model, k, v)
            self.kernel.reset()
            return True, "Recovery complete"

    # ============================================================ recovery ==
    REC_KEYS = ["cpu_util", "mem_util", "disk_util", "disk_used", "run_queue", "io_queue", "disk_latency", "rtt", "response_ms",
                "error_rate", "availability", "throughput", "running_procs", "blocked_procs", "terminated_procs",
                "workers_alive"]

    def _rec_snapshot(self):
        snap = self.model.snapshot()
        cyc = self.X.get("cycle") or []
        names = {p["pid"]: p["name"] for p in snap["processes"]}
        return {"health": HEALTH[self.health],
                "values": {k: rnd(self.V.get(k)) for k in self.REC_KEYS},
                "failed_procs": [{"name": p["name"], "state": p["state"]} for p in snap["processes"]
                                 if p["state"] in ("TERMINATED", "RESTARTING", "CRASHING", "HUNG")],
                "blocked_procs": [p["name"] for p in snap["processes"] if p["state"] == "BLOCKED"],
                "locks_held": sum(1 for r in snap["resources"] if r["holder"] is not None),
                "deadlock": [names.get(x, x) for x in cyc],
                "rules": {r: v["confirmed"] for r, v in self.det["rules"].items()}}

    def recover(self):
        """Operator Recovery — a state transition, NOT a reset.

        The simulation keeps running (same clock, same samples, same episode). Recovery removes the
        causes of the failure — stress, overloaded controls, failed processes, held locks — and the
        model then returns to health on its own over the following ticks: the controls fall back with
        their normal inertia and the queues drain at the rate the system can serve them. The failure
        that was active is kept so the Recovery Analysis can explain exactly that failure."""
        with self.lock:
            if self.offline:
                return False, "This server is offline"
            if self.kernel.down:
                return False, "The simulated kernel has panicked — reboot it in the Kernel Failure Lab (or reset that lab)"
            now = time.time()
            ep = self.episode
            M = self.model
            if ep is not None:
                cur = dict(ep)
                cur["rca"] = build_rca(cur, self)
                failure = _episode_public(cur)
            else:
                failure = None
            before = self._rec_snapshot()
            before["controls"] = dict(M.controls)
            actions = []
            # 1. stop whatever is driving the load
            if self.runner:
                actions.append(f"Stopped the running experiment “{self.runner['name']}”")
                self._stop_runner("recovery")
            if self.recording:
                actions.append("Saved and closed the active recording")
                self.stop_recording(reason="recovery")
            if self.paused:
                actions.append("Resumed the paused simulation")
                self.paused = False
            if self.stress:
                actions.append(f"Removed the active sudden stress ({programs.STRESS_KINDS[self.stress['kind']]} at "
                               f"{self.stress['intensity']:.0f}%)")
                self.stress = None
            # 2. return the overloaded controls to their healthy values (they follow with normal inertia)
            changed = [(k, v) for k, v in M.controls.items()
                       if k in self.spec and abs(v - self.spec[k]["default"]) > 1e-9]
            if changed:
                u = lambda c: "%" if c["unit"] == "%" else (" " + c["unit"] if c["unit"] else "")
                actions.append("Returned the overloaded controls to their healthy values: " + "; ".join(
                    f"{self.spec[k]['label']} {v:g} → {self.spec[k]['default']:g}{u(self.spec[k])}" for k, v in changed))
                self.set_controls({k: self.spec[k]["default"] for k, _ in changed}, source="recovery")
            # 3. stop extra processes started in the Process Lab
            extra = [p for p in M.procs.values() if p["kind"].startswith("custom") and p["state"] != "TERMINATED"]
            for p in extra:
                M.kill(p["pid"], "SIGTERM")
            if extra:
                actions.append("Stopped the extra processes created during the session: " + ", ".join(p["name"] for p in extra))
            if M.disk_logs_closed > 0:            # their log files stay on disk until deleted
                freed, _, _ = M.delete_logs()
                actions.append(f"Deleted the log files they wrote: {freed:.1f}% of the file system "
                               f"({freed / 100 * float(self.server.get('storage_gb', 250)):,.0f} GB) freed")
            # 4. release every lock (breaks a deadlock: waiting processes are granted, then release too)
            if before["deadlock"] or before["locks_held"] or before["blocked_procs"]:
                for _ in range(len(M.locks) + 2):
                    holders = [r["holder"] for r in M.locks.values() if r["holder"] is not None]
                    if not holders:
                        break
                    for pid in dict.fromkeys(holders):
                        M._release_all(pid)
                for p in M.procs.values():
                    if p["state"] == "BLOCKED":
                        M._dequeue(p["pid"])
                        p["state"], p["waiting_for"], p["info"] = "RUNNING", None, ""
                actions.append(("Broke the deadlock (circular wait " + " → ".join(before["deadlock"]) + ") and released all locks")
                               if before["deadlock"] else
                               f"Released {before['locks_held']} held lock(s) and woke {len(before['blocked_procs'])} blocked process(es)")
            # 5. restart failed processes (master first: it restarts its workers too); hung ones are killed first
            hung = [p for p in M.procs.values() if p["state"] == "HUNG"]
            for p in hung:
                if p["state"] == "HUNG":
                    M.kill(p["pid"], "SIGKILL")
            if hung:
                actions.append("Killed unresponsive (HUNG) processes: " + ", ".join(p["name"] for p in hung))
            dead = [p for p in M.procs.values() if p["state"] == "TERMINATED" and not p["kind"].startswith("custom")]
            restarted = []
            for p in sorted(dead, key=lambda q: (q["kind"] != "master", q["pid"])):
                if p["state"] == "TERMINATED":
                    M.restart(p["pid"])
                    restarted.append(p["name"])
            if restarted:
                actions.append("Restarted the failed processes: " + ", ".join(restarted)
                               + " (TERMINATED → RESTARTING → RUNNING)")
            V = before["values"]
            queues = []
            if (V.get("run_queue") or 0) >= 1:
                queues.append(f"CPU run queue ({V['run_queue']:.0f} processes)")
            if (V.get("io_queue") or 0) >= 1:
                queues.append(f"disk I/O queue ({V['io_queue']:.0f} requests)")
            if (self.V.get("accept_backlog") or 0) >= 1:
                queues.append(f"request backlog ({self.V['accept_backlog']:.0f} requests)")
            if queues:
                actions.append("Let the queues drain as the load fell: " + ", ".join(queues))
            self._drain_model_events()
            self.emit("RECOVERY", "Recovery started by the operator: " + ("; ".join(a.split(":")[0] for a in actions)
                                                                           if actions else "server checked"),
                      "info", marker="recovery")
            self._input("Operator Recovery", "recovery")
            stale = self.last_recovery
            if ep is None and self.health == 0 and not actions:
                # nothing was wrong: keep the analysis of the last recovered failure (if still the latest)
                if stale and any(e["start_ts"] > stale["requested_ts"] for e in self.episodes):
                    self.last_recovery = None
                return True, "The server is already healthy"
            if stale and stale.get("status") == "RECOVERING":
                # Recovery is already active: actions re-applied without resetting recovery sequence or start timestamp
                return True, "Recovery is already in progress — stabilizing system"
            self.recovery_seq += 1
            self.last_recovery = {
                "id": self.recovery_seq, "requested_ts": now, "status": "RECOVERING", "ok_streak": 0,
                "episode_id": ep["id"] if ep is not None else None,
                "failure": failure, "before": before, "actions": actions, "after": None, "checks": [],
                "verified_ts": None, "recovery_time": None, "flushed": False,
                "failure_duration": (now - ep["start_ts"]) if ep is not None else None,
            }
            return True, "Recovery started"

    # ---------------------------------------------- targeted strategies ==
    def apply_strategy(self, sid, params=None):
        """Apply ONE failure-specific recovery strategy to the live simulation and start verifying it.
        Unlike the generic Recovery, nothing else is changed: if other problems exist they remain."""
        with self.lock:
            if self.offline:
                return False, "This server is offline"
            if getattr(self, "kernel", None) and self.kernel.down:
                return False, "The simulated kernel has panicked — use the Kernel Failure Lab to reboot"
            st = strategies.STRATEGIES.get(sid)
            if st is None:
                return False, "Unknown recovery strategy"
            rule = st["rule"]
            ep = self.episode
            failure = None
            if ep is not None:
                cur = dict(ep)
                cur["rca"] = build_rca(cur, self)
                failure = _episode_public(cur)
            before = self._rec_snapshot()
            before["controls"] = dict(self.model.controls)
            params = dict(params or {})
            if params.get("victim") is not None and rule == "deadlock":
                v = self._proc_by(params["victim"])
                params["victim"] = v["pid"] if v else None
            try:
                actions, side = strategies.apply(self.model, sid, params)
            except ValueError as ex:
                self._drain_model_events()
                return False, str(ex)
            if self.stress:                     # the strategy took over the controls the stress had set
                for k in list(self.stress["targets"]):
                    if abs(self.model.controls[k] - self.stress["targets"][k]) > 1e-9:
                        self.stress["targets"].pop(k)
                        self.stress["prev"].pop(k, None)
                if not self.stress["targets"]:
                    self.stress = None
            self._drain_model_events()
            self.emit("RECOVERY", f"Recovery strategy applied: {st['name']} — " + "; ".join(actions), "info",
                      marker="recovery", data={"strategy": sid})
            self._input(f"Recovery strategy: {st['name']}", "recovery")
            self._rec({"op": "action", "action": "strategy", "id": sid})
            self.recovery_seq += 1
            self.last_recovery = {
                "id": self.recovery_seq, "requested_ts": time.time(), "status": "RECOVERING", "ok_streak": 0,
                "episode_id": ep["id"] if ep is not None else None, "failure": failure, "before": before,
                "actions": actions, "side_effects": side, "after": None, "checks": [], "verified_ts": None,
                "recovery_time": None, "flushed": True, "mode": "strategy",
                "strategy": {k: st[k] for k in ("id", "name", "category", "does", "when", "rule")},
                "target_name": RULES[rule]["name"], "target_active": self.det["rules"][rule]["confirmed"] > 0,
                "verify": strategies.new_verification(rule),
                "failure_duration": (time.time() - ep["start_ts"]) if ep is not None else None,
            }
            return True, f"{st['name']}: " + "; ".join(actions)

    def _verify_strategy(self, rec, now):
        v = rec["verify"]
        out = strategies.judge(v, self.det)
        if self.health == 0 and rec.get("healthy_ts") is None:
            rec["healthy_ts"] = now
        if out is None:
            return
        R = self.det["rules"]
        tgt = v["target"]
        f = lambda k, unit="": _short(self.V.get(k), unit)
        others = [RULES[r]["name"] for r, x in R.items() if x["confirmed"] > 0 and r != tgt]
        rec["checks"] = [
            {"ok": R[tgt]["confirmed"] == 0, "label": f"{RULES[tgt]['name']} cleared",
             "detail": "; ".join(detect.evidence(R[tgt])) or f"below WARNING for {v['t_ok']} consecutive samples"},
            {"ok": R["service"]["confirmed"] == 0, "label": "Service responding to clients",
             "detail": f"error rate {f('error_rate', '%')}, response {f('response_ms', 'ms')}"},
            {"ok": not others, "label": "No other active failures", "detail": ", ".join(others) or "none"},
            {"ok": out == "RECOVERED", "label": f"System status: {HEALTH[self.health]}",
             "detail": f"{strategies.VERIFY_SAMPLES} consecutive healthy samples required"},
        ]
        rec["after"] = self._rec_snapshot()
        rec["verified_ts"] = now
        rec["status"] = out
        rec["recovery_time"] = now - rec["requested_ts"]
        txt = {"RECOVERED": "server HEALTHY", "PARTIAL": f"{RULES[tgt]['name']} fixed, but other failures remain",
               "FAILED": f"{RULES[tgt]['name']} NOT fixed"}[out]
        self.emit("RECOVERY", f"Strategy verified ({rec['strategy']['name']}): {txt} after {rec['recovery_time']:.1f} s",
                  "ok" if out == "RECOVERED" else "warn")

    def compare_strategies(self, scenario, ids):
        res = strategies.compare(self.server, scenario, ids, seed=self.seed)
        summary = {"scenario": res["scenario"]["name"], "best": res["best"],
                   "outcomes": {r["strategy"]: r["outcome"] for r in res["runs"]}}
        try:
            res["record_id"] = self.store.save_record(self.server_id, "comparison",
                                                      f"Recovery comparison · {res['scenario']['name']}", summary, res)
        except Exception:
            res["record_id"] = None
        return res

    def _verify_recovery(self, now):
        """Health check after a Recovery: the server counts as recovered only after every detection
        rule is below its warning level in 3 consecutive samples (same rule as a normal episode)."""
        rec = self.last_recovery
        if not rec or rec["status"] != "RECOVERING":
            return
        if rec.get("mode") == "strategy":
            return self._verify_strategy(rec, now)
        rec["ok_streak"] = rec["ok_streak"] + 1 if self.health == 0 else 0
        if rec["ok_streak"] == 1:
            rec["healthy_ts"] = now               # first sample with every rule back below its warning level
        if rec["ok_streak"] < 3 and not rec["flushed"] and now - rec["requested_ts"] >= 20:
            # still failing 20 s after the causes were removed: a self-sustaining backlog (clients keep
            # retrying). Second recovery step = restart the web service, which drops the queued requests.
            M = self.model
            dropped = M.accept_backlog
            M.accept_backlog, M.q_cpu, M.disk_backlog = 0.0, 0.0, 0.0
            M.prev["error_frac"] = 0.0
            rec["flushed"] = True
            rec["actions"].append(f"Restarted the web service to break the retry loop (dropped {dropped:.0f} queued requests)")
            self.emit("RECOVERY", "Recovery step 2: web service restarted — queued requests dropped to break the retry loop",
                      "info", marker="recovery")
        if rec["ok_streak"] < 3 and now - rec["requested_ts"] < 45:
            return
        V, R = self.V, self.det["rules"]
        snap = self.model.snapshot()
        crit = [p for p in snap["processes"] if p["name"] in ("web-master", "database")]
        ok = lambda r: R[r]["confirmed"] == 0
        f = lambda k, unit="": _short(V.get(k), unit)
        rec["checks"] = [
            {"ok": ok("service") and ok("network"), "label": "Server responding to clients",
             "detail": f"availability {f('availability', '%')}, error rate {f('error_rate', '%')}, response {f('response_ms', 'ms')}"},
            {"ok": ok("process") and all(p["state"] == "RUNNING" or p["state"] == "READY" or p["state"] == "SLEEPING" for p in crit),
             "label": "Required processes running",
             "detail": f"web-master and database alive, {int(V.get('workers_alive') or 0)}/4 web workers"},
            {"ok": ok("cpu"), "label": "CPU utilization normalized",
             "detail": f"{f('cpu_util', '%')} used, run queue {f('run_queue')}"},
            {"ok": ok("memory"), "label": "Memory state restored",
             "detail": f"{f('mem_util', '%')} used, {f('page_faults', '/s')} page faults"},
            {"ok": ok("disk"), "label": "Disk I/O normal", "detail": f"I/O queue {f('io_queue')}, latency {f('disk_latency', 'ms')}"},
            {"ok": ok("deadlock"), "label": "No deadlock or blocked processes", "detail": f"{int(V.get('blocked_procs') or 0)} blocked"},
            {"ok": self.health == 0, "label": f"System status: {HEALTH[self.health]}",
             "detail": "confirmed in 3 consecutive samples (0.5 s apart)" if rec["ok_streak"] >= 3 else "not confirmed"},
        ]
        after = self._rec_snapshot()
        rec["after"] = after
        rec["verified_ts"] = now
        rec["status"] = "RECOVERED" if rec["ok_streak"] >= 3 and all(c["ok"] for c in rec["checks"]) else "FAILED"
        rec["recovery_time"] = now - rec["requested_ts"]
        self.emit("RECOVERY", f"Recovery verified: server {'HEALTHY' if rec['status'] == 'RECOVERED' else 'NOT fully restored'} "
                              f"{rec['recovery_time']:.1f} s after Recovery was requested", "ok" if rec["status"] == "RECOVERED" else "warn")

    def recovery_public(self):
        rec = self.last_recovery
        return None if rec is None else {k: v for k, v in rec.items() if k not in ("ok_streak", "verify")}

    # ============================================================== events ==
    def emit(self, kind, msg, level="info", marker=None, data=None):
        self.eseq += 1
        now = time.time()
        e = {"seq": self.eseq, "ts": now, "time": clock(now), "tick": self.model.tick, "kind": kind,
             "msg": msg, "level": level, "marker": marker}
        if data:
            e["data"] = data
        self.events.append(e)
        if self.recording is not None:
            self.recording["events"].append(e)
        if self.episode is not None:
            self.episode["events"].append(e)
        return e

    def _input(self, text, node):
        now = time.time()
        self.input_events.append({"ts": now, "tick": self.model.tick, "text": text, "node": node})

    def _rec(self, op):
        if self.recording is not None and not (self.runner and self.runner.get("replay")):
            op = dict(op, at_tick=self.model.tick - self.recording["start_tick"] + 1)
            self.recording["log"].append(op)

    # ============================================================ controls ==
    def set_controls(self, values, source="operator"):
        with self.lock:
            if self.offline:
                return False, "This server is offline"
            if not isinstance(values, dict):
                return False, "Invalid controls payload: expected a JSON object"
            out = {}
            for k, v in values.items():
                if isinstance(v, bool):
                    label = "Disk I/O" if k == "disk_io" else (self.spec.get(k, {}).get("label") or k)
                    return False, f"{label}: value must be a finite number"
                if k == "disk_io":            # Basic-mode combined disk slider (% of capacity)
                    try:
                        pct = float(v)
                        if not math.isfinite(pct):
                            raise ValueError("not finite")
                    except (TypeError, ValueError):
                        return False, "Disk I/O: value must be a finite number"
                    cap = self.server["disk_iops_capacity"]
                    out["disk_write"] = self.model.set_control("disk_write", cap * pct / 100 * 0.65)
                    out["disk_read"] = self.model.set_control("disk_read", cap * pct / 100 * 0.35)
                    self._rec({"op": "set", "key": "disk_write", "value": out["disk_write"]})
                    self._rec({"op": "set", "key": "disk_read", "value": out["disk_read"]})
                    continue
                if k not in self.spec:
                    return False, f"Unknown parameter '{k}'"
                try:
                    out[k] = self.model.set_control(k, v)
                except (TypeError, ValueError):
                    return False, f"{self.spec[k]['label']}: value must be a finite number"
                self._rec({"op": "set", "key": k, "value": out[k]})
            if out and source == "operator" and self.stress:
                pass
            return True, out

    def _log_control_changes(self):
        for k, c in self.spec.items():
            new, old = self.model.controls[k], self.ctl_logged.get(k)
            if new != old:
                u = c["unit"]
                f = lambda v: (f"{v:,.0f}" if abs(v) >= 100 or c["step"] >= 1 else f"{v:g}") + ("%" if u == "%" else f" {u}" if u else "")
                txt = f"{c['label']} {f(old)} → {f(new)}"
                self.emit("CONTROL", txt, "info", data={"key": k, "from": old, "to": new})
                self._input(txt, k)
                self.ctl_logged[k] = new
                if log.isEnabledFor(logging.INFO) and self._debug:
                    plain = txt.replace("\u2192", "->")
                    log.info(f"[SIMULATION] {self.server['id']}: {plain}")

    _debug = False

    # ============================================================= actions ==
    def _proc_by(self, target):
        if isinstance(target, int) or (isinstance(target, str) and target.isdigit()):
            return self.model.procs.get(int(target))
        for p in self.model.procs.values():
            if p["name"] == target:
                return p
        return None

    def action(self, body, source="operator"):
        with self.lock:
            if self.offline:
                return False, "This server is offline"
            a = body.get("action")
            if a == "strategy":                 # recorded recovery strategy (replays)
                return self.apply_strategy(body.get("id"), body.get("params"))
            if self.kernel.down and a not in ("dl_detect",):
                return False, ("The simulated kernel is down (" + self.kernel.stage + "): no process can be created, "
                               "scheduled or take locks until the reboot completes")
            M = self.model
            p = None
            try:
                if a == "create":
                    p = M.create_process(body.get("kind", "cpu"))
                    if p["kind"] == "custom_logflood" and body.get("rate") is not None:   # replay of a disk-full stress
                        p["load"] = max(0.1, min(5.0, float(body["rate"])))
                    msg = f"Created {p['name']} (PID {p['pid']})"
                elif a in DL_ACTIONS:
                    msg = self._deadlock_action(a, body)
                elif a in ("kill", "restart", "renice", "acquire", "release", "hang"):
                    p = self._proc_by(body.get("target") if body.get("target") is not None else body.get("pid"))
                    if p is None:
                        return False, "No such process"
                    if a == "kill":
                        M.kill(p["pid"], body.get("signal", "SIGKILL"))
                        msg = f"{body.get('signal', 'SIGKILL')} sent to {p['name']}"
                    elif a == "restart":
                        M.restart(p["pid"])
                        msg = f"Restarting {p['name']}"
                    elif a == "hang":
                        M.hang(p["pid"])
                        msg = f"{p['name']} is now unresponsive (HUNG)"
                    elif a == "renice":
                        M.renice(p["pid"], body.get("nice", 0))
                        msg = f"{p['name']} reniced"
                    elif a == "acquire":
                        n = body.get("count", 1)
                        res = M.acquire(p["pid"], body.get("rid"), n)
                        msg = f"{p['name']} requested {n} × {body.get('rid')} → {res}"
                    else:
                        M.release(p["pid"], body.get("rid"), body.get("count"))
                        msg = f"{p['name']} released " + (f"{body.get('rid')}" if body.get("rid") else "its locks")
                elif a == "preempt":
                    v = self._proc_by(body.get("victim")) if body.get("victim") is not None else None
                    M.preempt(body.get("rid"), v["pid"] if v else None)
                    msg = f"Resource {body.get('rid')} pre-empted"
                elif a == "form_deadlock":
                    M.form_deadlock(bool(body.get("three")))
                    msg = "Lock sequence applied: circular wait formed"
                elif a == "resolve_deadlock":
                    msg = self._resolve_deadlock(body.get("how", "victim"), body.get("victim"), body.get("rid"))
                elif a == "reap":
                    M.reap()
                    msg = "Terminated processes reaped"
                else:
                    return False, "Unknown action"
            except ValueError as ex:
                self._drain_model_events()
                return False, str(ex)
            self._drain_model_events()
            self.emit("ACTION", msg, "info")
            self._input(msg, "procs_table" if a not in ("acquire", "release", "preempt", "form_deadlock",
                                                          "resolve_deadlock") + DL_ACTIONS else "locks")
            rec = {k: v for k, v in body.items()}
            if "target" not in rec and body.get("pid") is not None and p is not None and a not in ("create",):
                rec["target"] = p["name"]
            self._rec({"op": "action", **rec})
            return True, msg

    def _resolve_deadlock(self, how, victim=None, rid=None):
        """Deadlock RECOVERY (after detection). Victim = the one chosen by the student, otherwise the
        cheapest deadlocked process: fewest resource instances held (least work lost), newest on a tie."""
        M = self.model
        dead = M.deadlock_report()["deadlocked"]
        if not dead:
            raise ValueError("No deadlock detected — there is nothing to resolve")
        if victim is not None:
            v = self._proc_by(victim)
            if v is None or v["pid"] not in dead:
                raise ValueError("Choose a victim from the deadlocked set: " + ", ".join(M._short(p) for p in dead))
        else:
            cost = lambda pid: (sum(r["alloc"].get(pid, 0) for r in M.locks.values()), -pid)
            v = M.procs[min(dead, key=cost)]
        why = "" if victim is not None else " (auto: holds the fewest instances)"
        if how == "preempt":
            rid = rid or next((h for h in v["holds"]
                               if any(M.procs[w].get("waiting_for") == h for w in dead if w != v["pid"])), None) \
                or (v["holds"][0] if v["holds"] else None)
            if rid is None:
                raise ValueError(f"{v['name']} holds nothing that can be pre-empted")
            M.preempt(rid, v["pid"])
            return f"Deadlock recovery by pre-emption: took {rid} from {M._short(v['pid'])}{why} and rolled it back"
        M.kill(v["pid"], "SIGKILL")
        return f"Deadlock recovery by termination: victim {M._short(v['pid'])}{why} aborted, its resources released"

    def _deadlock_action(self, a, b):
        M = self.model
        pid = lambda: (self._proc_by(b.get("target")) or {}).get("pid")
        if a == "dl_add_process":
            p = M.add_txn(b.get("name"), b.get("max"))
            return f"Transaction {M._short(p['pid'])} created (PID {p['pid']})"
        if a == "dl_remove_process":
            if pid() is None:
                raise ValueError("No such process")
            M.remove_txn(pid())
            return "Transaction removed"
        if a == "dl_set_max":
            if pid() is None:
                raise ValueError("No such process")
            M.set_max(pid(), b.get("max") or {})
            return "Maximum claim declared"
        if a == "dl_add_resource":
            r = M.add_resource(b.get("rid"), b.get("label"), b.get("instances", 1))
            return f"Resource {r['id']} added ({r['instances']} instance{'s' if r['instances'] > 1 else ''})"
        if a == "dl_remove_resource":
            M.remove_resource(b.get("rid"))
            return f"Resource {b.get('rid')} removed"
        if a == "dl_reset":
            M.reset_locks()
            M.events.append(("RESOURCE", "Deadlock Lab reset: P1–P3, resources A–C, nothing allocated", "info"))
            return "Deadlock Lab reset"
        if a == "dl_example":
            return self._dl_example(b.get("which", "two"))
        if a == "dl_detect":
            r = M.deadlock_report()
            txt = ("DEADLOCK: " + ", ".join(M._short(p) for p in r["deadlocked"]) + " can never finish") \
                if r["deadlocked"] else ("cycle in the graph but NO deadlock (enough instances can be freed)"
                                         if r["any_cycle"] else "no deadlock")
            M.events.append(("RESOURCE", f"Detection algorithm run by the student: {txt}", "fail" if r["deadlocked"] else "ok"))
            return "Detection: " + txt
        raise ValueError("Unknown action")

    def _dl_example(self, which):
        M = self.model
        M.reset_locks()
        P = {M._short(p): p for p in M.procs if M.procs[p]["kind"] == "txn"}
        if which == "two":
            M.form_deadlock(False)
            return "Example: P1 holds A and wants B, P2 holds B and wants A"
        if which == "three":
            M.form_deadlock(True)
            return "Example: 3-process circular wait P1 → P2 → P3 → P1"
        if which == "multi":
            # a cycle that is NOT a deadlock: R has 2 instances and P3 (not waiting) will return one
            M.add_resource("R", "Database connection pool", 2)
            M.add_resource("S", "Printer spooler slot", 1)
            M.acquire(P["P1"], "R")
            M.acquire(P["P2"], "S")
            M.acquire(P["P3"], "R")
            M.acquire(P["P1"], "S")
            M.acquire(P["P2"], "R")
            return "Example: cycle P1 → P2 → P1 with a 2-instance resource — the cycle is NOT a deadlock"
        if which == "banker":
            # the textbook example (Silberschatz): 12 tape drives, maximum claims 10 / 4 / 9
            M.add_resource("T", "Tape drives", 12)
            for k, mx, n in (("P1", 10, 5), ("P2", 4, 2), ("P3", 9, 2)):
                M.set_max(P[k], {"T": mx}, quiet=True)
                M.acquire(P[k], "T", n)
            self.model.avoidance = True
            M.events.append(("SETTING", "Avoidance ON — Banker's algorithm checks every request. Safe sequence: "
                                        "P2, P1, P3. Try: P3 requests 1 more tape drive.", "info"))
            return "Example: Banker's algorithm, 12 tape drives (avoidance ON)"
        raise ValueError("Unknown example")

    def _drain_model_events(self):
        for kind, msg, lvl in self.model.events:
            self.emit(kind, msg, lvl)
        self.model.events.clear()

    # ============================================================== stress ==
    def inject_stress(self, kind, intensity, hold_s=0):
        with self.lock:
            if kind not in programs.STRESS_KINDS:
                return False, "Unknown stress type"
            if self.kernel.down:
                return False, "The simulated kernel is down — wait for the reboot or reset the Kernel Failure Lab"
            intensity = max(0.0, min(100.0, float(intensity)))
            if kind == "process":
                victims = ["web-worker-1"] if intensity < 40 else ["web-worker-1", "web-worker-2"] if intensity < 75 \
                    else ["web-master"]
                for v in victims:
                    p = self._proc_by(v)
                    if p and p["state"] != "TERMINATED":
                        self.model.kill(p["pid"], "SIGSEGV")
                        self._rec({"op": "action", "action": "kill", "target": v, "signal": "SIGSEGV"})
                self._drain_model_events()
                msg = f"STRESS INJECTED: process crash ({', '.join(victims)} ← SIGSEGV)"
            elif kind == "deadlock":
                try:
                    self.model.form_deadlock(three=intensity >= 75)
                    self._rec({"op": "action", "action": "form_deadlock", "three": intensity >= 75})
                except ValueError as ex:
                    return False, str(ex)
                self._drain_model_events()
                msg = "STRESS INJECTED: deadlock (opposite-order lock acquisition)"
            elif kind == "diskfull":
                p = self.model.create_process("logflood")
                p["load"] = round(0.5 + 1.5 * intensity / 100.0, 2)        # % of the file system per second
                p["info"] = (f"writes log files nonstop: fills {p['load']:g} % of the file system per second "
                             "(accelerated) (just created)")
                self._rec({"op": "action", "action": "create", "kind": "logflood", "rate": p["load"]})
                self._drain_model_events()
                msg = (f"STRESS INJECTED: disk space exhaustion — {p['name']} writes log files at "
                       f"{p['load']:g} % of the file system per second")
            else:
                t = programs.stress_targets(kind, intensity, self.server, self.model.controls)
                prev = {k: self.model.controls[k] for k in t}
                self.set_controls(t, source="stress")
                self.stress = {"kind": kind, "intensity": intensity, "prev": prev, "targets": t,
                               "until_tick": self.model.tick + int(hold_s / DT) if hold_s else None,
                               "started": time.time()}
                msg = f"STRESS INJECTED: {programs.STRESS_KINDS[kind]} at {intensity:.0f}% → " + \
                      ", ".join(f"{self.spec[k]['label']} {v:g}" for k, v in t.items())
            self.emit("STRESS", msg, "fail", marker="stress")
            self._input(msg, "stress")
            return True, msg

    def clear_stress(self):
        with self.lock:
            if not self.stress:
                return False, "No stress is active"
            self.set_controls(self.stress["prev"], source="stress")
            self.emit("STRESS", "Stress removed — controls restored to their values before the injection", "ok",
                      marker="stress_end")
            self.stress = None
            return True, "Stress removed"

    # ============================================================ settings ==
    def settings(self, body):
        with self.lock:
            for k in SETTING_KEYS:
                if k in body:
                    if k == "avoidance" and body[k]:
                        bk = self.model.banker()
                        if not bk["declared"]:
                            return False, ("Declare a maximum claim for every transaction first (missing: "
                                           + ", ".join(bk["missing"]) + ")")
                    setattr(self.model, k, bool(body[k]))
                    label = {"cascade": "Cross-subsystem cascades", "anchor": "Host-anchored baseline (HYBRID)",
                             "lock_ordering": "Lock-ordering policy (deadlock prevention)",
                             "avoidance": "Banker's algorithm (deadlock avoidance)",
                             "txn_autocomplete": "Transactions finish on their own"}[k]
                    self.emit("SETTING", f"{label} {'ON' if body[k] else 'OFF'}", "info")
                    self._rec({"op": "setting", "key": k, "value": bool(body[k])})
            return True, self.settings_dict()

    def settings_dict(self):
        return {k: getattr(self.model, k) for k in SETTING_KEYS}

    def set_threshold(self, body):
        with self.lock:
            if body.get("reset"):
                self.detector.thresholds = copy.deepcopy(detect.DEFAULT_THRESHOLDS)
                self.emit("SETTING", "Detection thresholds restored to defaults", "info")
                return True, self.detector.thresholds
            rule, check, lvl = body.get("rule"), body.get("check"), body.get("level")
            if rule not in self.detector.thresholds or check not in self.detector.thresholds[rule]:
                return False, "Unknown threshold"
            old = self.detector.thresholds[rule][check][lvl] if lvl in self.detector.thresholds[rule][check] else None
            try:
                self.detector.set_threshold(rule, check, lvl, body.get("value"))
            except (ValueError, TypeError) as ex:
                if lvl in self.detector.thresholds[rule][check]:
                    self.detector.thresholds[rule][check][lvl] = old
                return False, str(ex)
            label = RULES[rule]["checks"][check][0]
            self.emit("SETTING", f"Threshold changed: {label} {LEVELS['_WDCF'.index(lvl)] if lvl in 'WDCF' else lvl} "
                                 f"{old} → {body.get('value')}", "info")
            self._rec({"op": "threshold", "rule": rule, "check": check, "level": lvl, "value": body.get("value")})
            return True, self.detector.thresholds

    def pause(self, on):
        with self.lock:
            self.paused = bool(on)
            self.emit("SYSTEM", "Simulation PAUSED — state frozen" if on else "Simulation RESUMED", "info",
                      marker="pause" if on else "resume")
            return True, self.paused

    # =============================================================== tick ===
    def tick(self, host=None):
        if self.paused or self.offline:
            return
        if host is not None:
            self.model.host = host
        M = self.model
        if self.runner:
            self._run_program_tick()
        if self.stress and self.stress["until_tick"] is not None and M.tick >= self.stress["until_tick"]:
            self.clear_stress()
        self.kernel.before_step()
        self._log_control_changes()
        V, P, X = M.step()
        V["run_queue_per_core"] = V["run_queue"] / self.server["vcpus"]
        if self.kernel.host_halted:              # no telemetry from a halted host: the agent ran on it
            for k in HOST_KEYS:
                V[k] = None
        X["kernel_level"], X["kernel_stage"] = self.kernel.level(), self.kernel.stage
        # what made the simulated kernel panic, when the scenario has a resource cause (OOM → panic_on_oom)
        X["kernel_cause"] = "memory" if self.kernel.scenario == "oom_panic" and self.kernel.stage != "RUNNING" else None
        self._drain_model_events()
        det = self.detector.evaluate(V, M.snapshot(), X)
        self.kernel.after_step(V, det)
        # attribution compares with the state just BEFORE the current failure episode began
        refV, refP = (self.episode["pre_V"], self.episode["pre_P"]) if self.episode else \
            (self.recent[0] if self.recent else (self.base_V, self.base_P))
        att = detect.attribute(det, V, P, refV, refP, X, M.cascade)
        self.V, self.P, self.X, self.det = V, P, X, det
        if self.episode is not None:
            self.episode["first_seen"] = dict(self.episode.get("first_seen", {}), **{k: v for k, v in self.first_seen.items() if k not in self.episode.get("first_seen", {})})
            self.episode["first_failed"] = dict(self.episode.get("first_failed", {}), **{k: v for k, v in self.first_failed.items() if k not in self.episode.get("first_failed", {})})
            self.episode["first_failed_tick"] = dict(self.episode.get("first_failed_tick", {}), **{k: v for k, v in self.first_failed_tick.items() if k not in self.episode.get("first_failed_tick", {})})
        self.recent.append((V, P))
        now = time.time()
        self._threshold_events(det, now)
        for t in det["transitions"]:
            r = RULES[t["rule"]]["name"]
            up = t["to"] > t["from"]
            if up and t["rule"] not in self.first_seen:
                self.first_seen[t["rule"]] = now
            if up and t["to"] >= 2 and t["rule"] not in self.first_failed:
                self.first_failed[t["rule"]] = now
                self.first_failed_tick[t["rule"]] = M.tick
            if t["to"] == 0:
                self.first_seen.pop(t["rule"], None)
                self.first_failed.pop(t["rule"], None)
                self.first_failed_tick.pop(t["rule"], None)
            self.emit("DETECTION", f"{r} → {LEVELS[t['to']]}" + (": " + "; ".join(t["evidence"]) if up and t["evidence"] else ""),
                      "fail" if t["to"] >= 3 else "warn" if t["to"] >= 1 else "ok",
                      data={"rule": t["rule"], "from": t["from"], "to": t["to"]}, marker="detect" if up else None)
        self.att = self._freeze(att, det)
        old = self.health
        self.health = det["health"]
        if self.health != old:
            self.emit("HEALTH", f"SYSTEM {HEALTH[old]} → {HEALTH[self.health]}",
                      "ok" if self.health == 0 else "fail" if self.health >= 3 else "warn",
                      data={"from": old, "to": self.health})
        self._track_episode(now)
        self._verify_recovery(now)
        self.seq += 1
        s = {"seq": self.seq, "ts": round(now, 3), "tick": M.tick, "health": self.health}
        for k in SAMPLE_KEYS:
            s[k] = rnd(V.get(k))
        for k in CONTROL_KEYS:
            s["c_" + k] = rnd(M.controls[k])
            s["e_" + k] = rnd(M.eff[k])
        for rid, r in det["rules"].items():
            s["r_" + rid] = r["confirmed"]
        self.samples.append(s)
        if self.recording is not None:
            self.recording["samples"].append(s)
        if self._debug and M.tick % 4 == 0:
            log.info(f"[METRICS] {self.server['id']}: cpu={V['cpu_util']:.1f}% rq={V['run_queue']:.1f} "
                     f"mem={V['mem_util']:.1f}% pf={V['page_faults']:.0f} disk={V['disk_util']:.0f}% "
                     f"lat={V['response_ms']:.0f}ms err={V['error_rate']:.1f}% health={HEALTH[self.health]}")

    def _threshold_events(self, det, now):
        """First crossing of each level per check within the current episode (raw, unconfirmed)."""
        ep = self.episode
        for rid, r in det["rules"].items():
            for c in r["checks"]:
                key = f"{rid}.{c['key']}"
                lvl = c["level"]
                seen = (ep or {}).get("max_check", {}).get(key, 0) if ep else self._pre_max.get(key, 0)
                if lvl > seen:
                    th = (c.get("th") or {}).get(["W", "D", "C", "F"][lvl - 1]) if lvl else None
                    txt = (f"{c['label']} crossed the {LEVELS[lvl]} threshold"
                           + (f" ({detect.fmt_v(c['value'], c['unit'])} > {detect.fmt_v(th, c['unit'])})" if th is not None else
                              f" ({detect.fmt_v(c['value'], c['unit'])})"))
                    self.emit("THRESHOLD", txt, "warn" if lvl < 3 else "fail",
                              data={"rule": rid, "check": c["key"], "level": lvl, "value": c["value"]})
                    rec = {"rule": rid, "check": c["key"], "label": c["label"], "level": lvl, "value": c["value"],
                           "threshold": th, "unit": c["unit"], "ts": now, "tick": self.model.tick}
                    if ep is None:
                        self._pending_cross.append(rec)
                    else:
                        ep["crossings"].append(rec)
                        if ep["first_crossing"] is None:
                            ep["first_crossing"] = rec
                    if ep:
                        ep["max_check"][key] = lvl
                    else:
                        self._pre_max[key] = lvl
                elif lvl == 0 and not ep:
                    self._pre_max.pop(key, None)


    # ============================================================ episodes ==
    def _track_episode(self, now):
        h = self.health
        ep = self.episode
        if ep is None:
            if h == 0:
                if self.det["raw"] == 0:
                    self._pending_cross, self._pre_max = [], {}
                return
            trig = self._trigger(now)
            pre = self.recent[0] if self.recent else (self.base_V, self.base_P)
            ep = self.episode = {"pre_V": pre[0], "pre_P": pre[1],
                "id": len(self.episodes) + 1, "start_ts": now, "start_tick": self.model.tick, "trigger": trig,
                "first_crossing": self._pending_cross[0] if self._pending_cross else None,
                "crossings": list(self._pending_cross), "max_check": dict(self._pre_max),
                "detections": [], "first_detection": None, "failure_at": None,
                "peak": 0, "peak_ts": now, "peak_values": None, "peak_trace": None, "primary": None,
                "secondary": [], "recovery_start_ts": None, "recovered_ts": None, "ok_streak": 0,
                "events": [], "status_log": [], "active": True,
            }
            self._pending_cross, self._pre_max = [], {}
            self.emit("EPISODE", f"Failure episode #{ep['id']} started", "warn", marker="episode")
        for t in self.det["transitions"]:
            if t["to"] > t["from"]:
                d = {"rule": t["rule"], "name": RULES[t["rule"]]["name"], "level": t["to"], "ts": now,
                     "evidence": t["evidence"]}
                ep["detections"].append(d)
                if ep["first_detection"] is None:
                    ep["first_detection"] = d
                if t["to"] >= 2 and ep["failure_at"] is None:
                    ep["failure_at"] = d
        # primary / secondary over everything detected in this episode (frozen attribution)
        eatt = self._episode_attribution(ep)
        prim = detect.primaries(eatt, ep.get("first_seen", {}), ep.get("first_failed", {}))
        if prim:
            ep["primary"] = prim[0]
            ep["primaries"] = prim
            ep["secondary"] = [{"rule": r, "name": RULES[r]["name"], "by": a["by"], "via": a["via"]}
                               for r, a in eatt.items() if a["by"]]
            ep["attribution"] = {r: dict(a) for r, a in eatt.items()}
            ep["loop"] = next((a["loop"] for a in eatt.values() if a.get("loop")), ep.get("loop"))
        if h > ep["peak"]:
            ep["peak"], ep["peak_ts"] = h, now
            ep["peak_values"] = {k: rnd(self.V.get(k)) for k in SAMPLE_KEYS}
            ep["peak_controls"] = dict(self.model.controls)
            # keep the state at the worst moment so the chain can be re-traced once the primary is known
            ep["peak_VP"] = (dict(self.V), {k: dict(v) for k, v in self.P.items()})
            ep["peak_det"] = {r: [{"key": c["key"], "level": c["level"]} for c in x["checks"]] for r, x in self.det["rules"].items()}
            ep["peak_trace_key"] = self._key_metric(ep["primary"], ep["peak_det"])
            ep["peak_trace"] = trace(ep["peak_trace_key"], self.V, self.P, self.base_V, self.base_P)
            ep["recovery_start_ts"] = None
        if ep.get("peak_VP") and ep["primary"] and self._key_metric(ep["primary"], ep["peak_det"]) != ep.get("peak_trace_key"):
            # the episode's first sample often comes before attribution has found the primary cause:
            # re-trace the worst moment from the primary's key metric (no guessing from a later state)
            ep["peak_trace_key"] = self._key_metric(ep["primary"], ep["peak_det"])
            ep["peak_trace"] = trace(ep["peak_trace_key"], ep["peak_VP"][0], ep["peak_VP"][1], self.base_V, self.base_P)
        if h < ep["peak"] and ep["recovery_start_ts"] is None:
            ep["recovery_start_ts"] = now
            self.emit("RECOVERY", f"Recovery started: health improving ({HEALTH[ep['peak']]} → {HEALTH[h]})", "info",
                      marker="recovery")
        self._check_metastable(ep, now)
        if h == 0:
            ep["ok_streak"] += 1
            if ep["ok_streak"] >= 3:
                ep["recovered_ts"], ep["active"] = now, False
                ep["final_values"] = {k: rnd(self.V.get(k)) for k in SAMPLE_KEYS}
                ep["final_controls"] = dict(self.model.controls)
                ep["rca"] = build_rca(ep, self)
                self.episodes.append(ep)
                self.episode = None
                self.emit("RECOVERY", f"System HEALTHY again — episode #{ep['id']} recovered "
                                      f"in {ep['recovered_ts'] - (ep['recovery_start_ts'] or ep['peak_ts']):.1f} s", "ok",
                          marker="recovered")
        else:
            ep["ok_streak"] = 0
            if ep["recovery_start_ts"] and h > self._last_h:
                ep["recovery_start_ts"] = None
                self.emit("RECOVERY", "Recovery interrupted — health got worse again", "warn", marker="interrupt")
        self._last_h = h

    def _freeze(self, att, det):
        """Keep each rule's attribution from the moment it was first detected (updated once when it
        first reaches a failure level). Measured later, side effects of the cascade would dominate."""
        ep = self.episode
        store = ep.setdefault("frozen", {}) if ep is not None else self._frozen_idle
        for r, a in att.items():
            lvl = det["rules"][r]["confirmed"]
            f = store.get(r)
            if f is None or (lvl >= 2 and f["level"] < 2):
                store[r] = dict(a, level=lvl)
        for r in list(store):
            if det["rules"][r]["confirmed"] == 0 and ep is None:
                store.pop(r)
        cur = {r: {"by": store[r]["by"], "via": store[r]["via"], "input": store[r]["input"]} for r in att if r in store}
        for r, a in cur.items():                      # a 'by' rule that is no longer active cannot explain it
            if a["by"] and a["by"] not in cur:
                a["by"] = None
        return detect.break_cycles(cur, self.first_failed, self.first_seen)

    def _episode_attribution(self, ep):
        """Attribution over every rule seen in the episode (frozen values)."""
        store = ep.get("frozen") or {}
        cur = {r: {"by": a["by"], "via": a["via"], "input": a["input"]} for r, a in store.items()}
        for r, a in cur.items():
            if a["by"] and a["by"] not in cur and not (a["by"] == "kernel" and self.kernel.down):
                a["by"] = None
        return detect.break_cycles(cur, ep.get("first_failed", {}), ep.get("first_seen", {}))

    def _check_metastable(self, ep, now):
        """Self-sustaining (metastable) failure: the operator has reduced the inputs that triggered the
        episode, yet the system has stayed failed for 20 s — a feedback loop now keeps it down."""
        if self.health < 2:
            ep["last_bad_check"] = None
            return
        if ep.get("last_bad_check") is None:
            ep["bad_since"] = now
        ep["last_bad_check"] = now
        if ep.get("metastable") or not ep.get("peak_controls") or now - ep.get("bad_since", now) < 20:
            return
        lowered = [k for k, v in ep["peak_controls"].items() if self.model.controls.get(k, v) < v - 1e-9]
        last_input = self.input_events[-1]["ts"] if self.input_events else 0
        if lowered and now - last_input >= 20 and now - ep.get("last_bad_check", now) < 1:
            loop = ["requests time out", "clients retry", "arrivals exceed worker capacity", "backlog stays full"]
            if (self.V.get("mem_ws") or 0) > 80 and self.model.cascade:
                loop = ["in-flight requests hold memory", "memory thrashes", "workers slow down"] + loop[1:]
            ep["metastable"] = {"ts": now, "loop": loop}
            self.emit("METASTABLE", "Self-sustaining failure: the trigger was reduced but the system stays failed — "
                                    + " → ".join(loop) + " → … Break the loop: lower client traffic, free memory, "
                                    "shorten retries (raise the timeout) or restart the web service.", "fail",
                      marker="metastable")

    def _key_metric(self, rule, det_rules=None):
        """The node to trace for a rule: its worst check (mapped to a graph node)."""
        if rule is None:
            return "response_ms"
        r = {"checks": det_rules[rule]} if det_rules and rule in det_rules else self.det["rules"][rule]
        if rule == "process":
            return "capacity"
        if rule == "deadlock":
            return "lock_wait"
        if rule == "kernel":
            return "availability"
        best = max(r["checks"], key=lambda c: c["level"]) if r["checks"] else None
        k = best["key"] if best else PRIMARY_METRIC[rule]
        return {"run_queue_per_core": "run_queue"}.get(k, k)

    def _trigger(self, now):
        """The operator input(s) that preceded the episode (within 30 s)."""
        evs = [e for e in self.input_events if now - e["ts"] <= 30]
        return {"inputs": evs[-6:], "last": evs[-1] if evs else None}

    # ============================================================ programs ==
    def run_program(self, prog, name=None, replay_of=None):
        with self.lock:
            if self.offline:
                return False, "This server is offline"
            errs = programs.validate(prog, self.spec)
            if errs:
                return False, "; ".join(errs)
            seed = prog.get("seed", self.seed)
            self.reset(seed=seed)
            M = self.model
            for k, v in (prog.get("settings") or {}).items():
                if k in SETTING_KEYS:
                    setattr(M, k, bool(v))
            if prog.get("thresholds"):
                self.detector.thresholds = copy.deepcopy(prog["thresholds"])
            init = {}
            for k, v in (prog.get("initial") or {}).items():
                init[k] = programs.resolve(v, self.server, M.controls, self.spec)
            for k, v in init.items():
                M.set_control(k, v)
                M.eff[k] = M.controls[k]          # initial state applies immediately (not a change)
            self.ctl_logged = dict(M.controls)
            self.runner = {"prog": prog, "start_tick": M.tick, "done": set(), "replay": bool(replay_of),
                           "ramp_start": {}, "name": prog.get("name", "Experiment")}
            self.start_recording(name or prog.get("name", "Experiment"), kind="replay" if replay_of else "program",
                                 program=prog, replay_of=replay_of)
            self.emit("PROGRAM", f"Experiment started: {prog.get('name', 'program')} "
                                 f"({prog.get('duration_s')} s)", "info", marker="program")
            return True, "Experiment started"

    def _run_program_tick(self):
        R = self.runner
        prog, M = R["prog"], self.model
        k = M.tick - R["start_tick"] + 1          # the sample about to be produced
        t = k * DT
        for i, s in enumerate(prog.get("steps") or []):
            due = (s["at_tick"] <= k) if "at_tick" in s else (s.get("at", 0) <= t + 1e-9)
            if i in R["done"] or not due:
                continue
            R["done"].add(i)
            if "set" in s:
                vals = {}
                for key, v in s["set"].items():
                    r = programs.resolve(v, self.server, M.controls, self.spec)
                    vals[key] = self.spec[key]["default"] if r is None else r
                self.set_controls(vals, source="program")
            if "action" in s:
                ok, msg = self.action(dict(s["action"]), source="program")
                if not ok:
                    self.emit("PROGRAM", f"Step skipped: {msg}", "warn")
            if "setting" in s:
                setattr(M, s["setting"]["key"], bool(s["setting"]["value"]))
            if "threshold" in s:
                th = s["threshold"]
                try:
                    self.detector.set_threshold(th["rule"], th["check"], th["level"], th["value"])
                except ValueError:
                    pass
        for j, r in enumerate(prog.get("ramps") or []):
            start, every = float(r.get("start", 0)), float(r["every"])
            if t + 1e-9 < start:
                continue
            if j in R.setdefault("ramp_done", set()):
                continue
            n_due = int((t - start + 1e-9) // every) + 1
            done = R.setdefault("ramp_n", {}).get(j, 0)
            if n_due <= done:
                continue
            R["ramp_n"][j] = n_due
            key = r["param"]
            if j not in R["ramp_start"]:
                R["ramp_start"][j] = M.controls[key]
            delta = programs.resolve(r["delta"], self.server, M.controls, self.spec, start=R["ramp_start"][j])
            until = programs.resolve(r.get("until", self.spec[key]["max"]), self.server, M.controls, self.spec)
            cur = M.controls[key]
            nv = cur + delta
            nv = min(nv, until) if delta >= 0 else max(nv, until)
            if nv != cur:
                self.set_controls({key: nv}, source="program")
            if nv == until or (r.get("end") is not None and t >= float(r["end"])):
                R["ramp_done"].add(j)          # a ramp runs once up to its limit, then stops
        if t > float(prog.get("duration_s", 60)) + 1e-9:
            self._stop_runner("completed")

    def _stop_runner(self, why):
        R = self.runner
        self.runner = None
        self.emit("PROGRAM", f"Experiment {why}: {R['name']}", "ok" if why == "completed" else "info", marker="program_end")
        if self.recording is not None:
            self.stop_recording(reason=why)

    def stop_program(self):
        with self.lock:
            if not self.runner:
                return False, "No experiment is running"
            self._stop_runner("stopped by the operator")
            return True, "Experiment stopped"

    def replay(self, run_id):
        """Re-run a recording sample-for-sample: same seed, same initial state, same thresholds and
        settings, and every logged control change / action applied at the same sample index."""
        run = self.store.lab_run(run_id)
        if not run:
            return False, "Run not found"
        cfg = run["config"]
        prog = {"name": f"Replay of run #{run_id} · {run['name']}", "duration_s": max(5, round(run["duration_s"] or 30)),
                "initial": cfg.get("controls", {}), "settings": cfg.get("settings", {}),
                "thresholds": cfg.get("thresholds"), "seed": cfg.get("seed", self.seed), "ramps": [],
                "steps": [self._log_to_step(op) for op in run["log"]]}
        return self.run_program(prog, replay_of=run_id)

    @staticmethod
    def _log_to_step(op):
        s = {"at_tick": op["at_tick"]}
        if op["op"] == "set":
            s["set"] = {op["key"]: op["value"]}
        elif op["op"] == "action":
            s["action"] = {k: v for k, v in op.items() if k not in ("op", "at_tick")}
        elif op["op"] == "setting":
            s["setting"] = {"key": op["key"], "value": op["value"]}
        elif op["op"] == "threshold":
            s["threshold"] = {k: op[k] for k in ("rule", "check", "level", "value")}
        return s

    # =========================================================== recording ==
    def start_recording(self, name=None, kind="manual", program=None, replay_of=None, reset_first=False):
        with self.lock:
            if self.recording is not None:
                return False, "Already recording"
            if reset_first:
                self.reset()
            M = self.model
            self.recording = {
                "name": name or f"Recording {datetime.now().strftime('%H:%M:%S')}", "kind": kind,
                "start_ts": time.time(), "start_tick": M.tick, "program": program, "replay_of": replay_of,
                "config": {"controls": dict(M.controls), "settings": self.settings_dict(), "seed": M.seed,
                           "thresholds": copy.deepcopy(self.detector.thresholds), "rng_tick": M.tick,
                           "processes": [{"pid": p["pid"], "name": p["name"], "state": p["state"]} for p in M.procs.values()],
                           "server": {k: v for k, v in self.server.items() if k != "profile"}},
                "log": [], "events": [], "samples": [], "episodes_before": len(self.episodes),
            }
            self.emit("RECORD", f"Recording started: {self.recording['name']}", "info", marker="record")
            return True, "Recording"

    def stop_recording(self, reason="stopped"):
        with self.lock:
            R = self.recording
            if R is None:
                return False, "Not recording"
            self.recording = None
            eps = self.episodes[R["episodes_before"]:]
            if self.episode is not None:
                cur = dict(self.episode)
                cur["rca"] = build_rca(cur, self)
                eps = eps + [cur]
            dur = (self.model.tick - R["start_tick"]) * DT
            peak = max([e["peak"] for e in eps] or [0])
            prim = next((e["primary"] for e in eps if e.get("primary")), None)
            summary = {"episodes": len(eps), "peak": HEALTH[peak], "primary": RULES[prim]["name"] if prim else None,
                       "final_health": HEALTH[self.health], "reason": reason,
                       "first_crossing": next((e["first_crossing"] for e in eps if e.get("first_crossing")), None),
                       "detection_latency": next((e["rca"]["timing"]["detection_latency"] for e in eps
                                                  if e.get("rca") and e["rca"]["timing"].get("detection_latency") is not None), None),
                       "recovery_time": next((e["rca"]["timing"]["recovery_time"] for e in eps
                                              if e.get("rca") and e["rca"]["timing"].get("recovery_time") is not None), None)}
            rec = {"server_id": self.server_id, "name": R["name"], "kind": R["kind"],
                   "created": datetime.fromtimestamp(R["start_ts"]).strftime("%Y-%m-%d %H:%M:%S"),
                   "duration_s": dur, "peak_health": HEALTH[peak], "primary_failure": summary["primary"],
                   "replay_of": R["replay_of"], "program": R["program"], "config": R["config"], "log": R["log"],
                   "samples": R["samples"], "events": R["events"],
                   "episodes": [_episode_public(e) for e in eps], "summary": summary}
            try:
                rid = self.store.save_lab_run(rec)
                self.last_run_id = rid
                self.emit("RECORD", f"Recording saved as run #{rid} ({dur:.0f} s, {len(eps)} failure episode(s))", "ok",
                          marker="record_end")
            except Exception as ex:
                rid = None
                self.emit("SYSTEM", f"Could not save the recording: {ex}", "warn")
            return True, rid

    # ============================================================ snapshot ==
    def snapshot_data(self, name):
        with self.lock:
            M = self.model
            return {"name": name, "server_id": self.server_id, "created": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "data": {"controls": dict(M.controls), "values": {k: rnd(self.V.get(k)) for k in SAMPLE_KEYS},
                             "health": HEALTH[self.health],
                             "rules": {r: LEVELS[v["confirmed"]] for r, v in self.det["rules"].items()},
                             "primary": RULES[self.episode["primary"]]["name"] if self.episode and self.episode.get("primary") else None,
                             "processes": {"running": rnd(self.V["running_procs"]), "blocked": rnd(self.V["blocked_procs"]),
                                           "terminated": rnd(self.V["terminated_procs"])},
                             "settings": self.settings_dict()}}

    # =============================================================== trace ==
    def trace(self, node, ref="baseline"):
        with self.lock:
            if node not in NODES and node not in self.V:
                return None
            if ref == "recent" and len(self.recent) >= 6:
                rv, rp = self.recent[0]
            else:
                rv, rp = self.base_V, self.base_P
            t = trace(node, self.V, self.P, rv, rp)
            meta = dict(NODES.get(node, {}))
            meta["value"] = rnd(self.V.get(node))
            meta["baseline"] = rnd(self.base_V.get(node))
            meta["deps_values"] = [{"id": d, "label": NODES[d]["label"], "unit": NODES[d]["unit"],
                                    "value": rnd(self.V.get(d)), "type": NODES[d]["type"]} for d in meta.get("deps", [])]
            meta["affects_values"] = [{"id": d, "label": NODES[d]["label"], "unit": NODES[d]["unit"],
                                       "value": rnd(self.V.get(d))} for d in meta.get("affects", [])]
            meta["type_now"] = self.node_type(node)
            meta["threshold"] = self._threshold_for(node)
            meta["parts_now"] = [{"part": k, "dep": d, "value": rnd(v)} for k, (d, v) in (self.P.get(node) or {}).items()]
            return {"meta": meta, "trace": t, "ref": "baseline" if rv is self.base_V else "recent"}

    def node_type(self, node):
        if node in ("cpu_util", "cpu_demand", "mem_util", "mem_ws", "mem_avail") and self.model.anchor:
            return "HYBRID"
        return NODES.get(node, {}).get("type", "DERIVED")

    def _threshold_for(self, node):
        key = "run_queue_per_core" if node == "run_queue" else node
        for rid, checks in self.detector.thresholds.items():
            if key in checks:
                lvl = 0
                for r in self.det["rules"][rid]["checks"]:
                    if r["key"] == key:
                        lvl = r["level"]
                th = dict(checks[key])
                if node == "run_queue":
                    th = {k: (v * self.server["vcpus"] if v is not None else None) for k, v in th.items()}
                return {"rule": rid, "rule_name": RULES[rid]["name"], "levels": th, "status": LEVELS[lvl],
                        "per_core": node == "run_queue"}
        return None

    # =============================================================== state ==
    def status(self):
        """Health + primary cause + secondary effects, from the current detection and attribution."""
        h = self.health
        det = self.det
        active = sorted([(r, v["confirmed"]) for r, v in det["rules"].items() if v["confirmed"] > 0],
                        key=lambda x: (-x[1], detect.ORDER.index(x[0])))
        prim = detect.primaries(self.att, self.first_seen, self.first_failed)
        out = {"health": HEALTH[h], "level": h, "raw": HEALTH[det["raw"]],
               "active": [{"rule": r, "name": RULES[r]["name"], "level": LEVELS[l], "evidence": detect.evidence(det["rules"][r]),
                           "by": self.att.get(r, {}).get("by"), "via": self.att.get(r, {}).get("via")} for r, l in active],
               "primary": [{"rule": r, "name": RULES[r]["name"], "subsystem": RULES[r]["subsystem"],
                            "via": self.att[r]["via"], "level": LEVELS[det["rules"][r]["confirmed"]]} for r in prim],
               "secondary": [{"rule": r, "name": RULES[r]["name"], "by": a["by"], "by_name": RULES[a["by"]]["name"],
                              "via": a["via"], "level": LEVELS[det["rules"][r]["confirmed"]]}
                             for r, a in self.att.items() if a["by"]]}
        if self.episode:
            ep = self.episode
            out["episode"] = {"id": ep["id"], "start_ts": ep["start_ts"], "peak": HEALTH[ep["peak"]],
                              "metastable": ep.get("metastable"),
                              "recovering": ep["recovery_start_ts"] is not None,
                              "first_crossing": ep["first_crossing"],
                              "first_detection": ep["first_detection"], "failure_at": ep["failure_at"]}
        return out

    def state(self, since=0, esince=0, epoch=0, full=True):
        with self.lock:
            M = self.model
            reset = epoch != self.epoch
            samples = [s for s in self.samples if reset or s["seq"] > since]
            if reset or since == 0:
                samples = samples[-720:]
            events = [e for e in self.events if reset or e["seq"] > esince]
            out = {
                "epoch": self.epoch, "seq": self.seq, "eseq": self.eseq, "reset": reset, "now": time.time(),
                "paused": self.paused, "offline": self.offline,
                "health": HEALTH[self.health], "level": self.health,
                "values": {k: rnd(v) for k, v in self.V.items() if isinstance(v, (int, float))},
                "baseline": {k: rnd(self.base_V.get(k)) for k in SAMPLE_KEYS},
                "controls": dict(M.controls), "effective": {k: rnd(v) for k, v in M.eff.items()},
                "settings": self.settings_dict(),
                "rules": {r: {"name": v["name"], "level": v["level"], "confirmed": v["confirmed"],
                              "checks": [{k: (rnd(c[k]) if k == "value" else c[k]) for k in ("key", "label", "unit", "value", "level", "th")}
                                         for c in v["checks"]],
                              "thrashing": v.get("thrashing", False)} for r, v in self.det["rules"].items()},
                "status": self.status(),
                "samples": samples, "events": events,
                "recording": ({"name": self.recording["name"], "since": self.recording["start_ts"],
                               "kind": self.recording["kind"]} if self.recording else None),
                "runner": ({"name": self.runner["name"], "elapsed": (M.tick - self.runner["start_tick"]) * DT,
                            "duration": self.runner["prog"].get("duration_s")} if self.runner else None),
                "stress": ({k: self.stress[k] for k in ("kind", "intensity", "targets", "started")} if self.stress else None),
                "last_run_id": self.last_run_id,
                "node_types": {n: self.node_type(n) for n in NODES},
                "thresholds_live": self.detector.thresholds,
            }
            out["last_recovery"] = self.recovery_public()
            out["kernel"] = self.kernel.public()
            if full:
                snap = M.snapshot()
                cyc = self.X.get("cycle") or []
                names = {p["pid"]: p["name"] for p in snap["processes"]}
                out["processes"] = snap["processes"]
                out["resources"] = snap["resources"]
                out["disk_space"] = snap["disk_space"]
                out["deadlock"] = {
                    "cycle": [names.get(p, p) for p in cyc], "age": rnd(self.X.get("cycle_age", 0.0)),
                    "edges": [{"from": names.get(a, a), "to": names.get(b, b), "resource": r} for a, b, r in self.X.get("edges", [])],
                    "coffman": {"mutual_exclusion": any(r["holder"] is not None for r in snap["resources"]),
                                "hold_and_wait": any(p["holds"] and p.get("waiting_for") for p in snap["processes"]),
                                "no_preemption": any(r["holder"] is not None for r in snap["resources"]),
                                "circular_wait": bool(cyc)},
                    "detected": self.det["rules"]["deadlock"]["confirmed"] >= 3,
                    "analysis": self._deadlock_public(names),
                }
                out["episodes"] = [_episode_public(e) for e in self.episodes[-5:]]
                if self.episode:
                    cur = dict(self.episode)
                    cur["rca"] = build_rca(cur, self)
                    out["current_episode"] = _episode_public(cur)
            return out

    def kernel_op(self, b):
        with self.lock:
            if self.offline:
                return False, "This server is offline"
            K = self.kernel
            op = b.get("op")
            try:
                if op == "options":
                    return True, K.set_options(b.get("options") or {})
                if op == "trigger":
                    if self.runner:
                        self._stop_runner("kernel experiment")
                    return True, K.trigger(b.get("scenario"), b.get("recovery", "auto"))
                if op == "power_cycle":
                    return True, K.power_cycle()
                if op == "fallback":
                    return True, K.choose_fallback()
                if op == "reset":
                    if K.down:
                        self.reset()
                        return True, "Kernel lab reset (fresh boot)"
                    K.reset()
                    return True, "Kernel lab reset"
            except ValueError as ex:
                return False, str(ex)
            return False, "Unknown kernel operation"

    def _deadlock_public(self, names):
        r = self.model.deadlock_report()
        nm = lambda p: names.get(p, str(p)).split()[-1]
        vec = lambda d: {k: v for k, v in (d or {}).items() if v}
        bk = r["banker"]
        return {
            "deadlocked": [nm(p) for p in r["deadlocked"]], "cycle": [nm(p) for p in r["cycle"]],
            "any_cycle": [nm(p) for p in r["any_cycle"]], "single_instance": r["single_instance"],
            "behind": [nm(p) for p in r["behind"]], "reduction": [nm(p) for p in r["reduction"]],
            "available": r["avail"],
            "matrix": [{"pid": p, "name": nm(p), "alloc": vec(r["alloc"].get(p)), "request": vec(r["req"].get(p)),
                        "max": vec(self.model.procs[p].get("max")), "need": vec(r["need"].get(p))} for p in r["alloc"]],
            "steps": [{"name": nm(s["pid"]), "demand": s["demand"], "work_after": s["work_after"]} for s in r["steps"]],
            "banker": None if bk is None else {"safe": bk["safe"], "sequence": [nm(p) for p in bk["sequence"]],
                                               "stuck": [nm(p) for p in bk["stuck"]], "declared": bk["declared"],
                                               "missing": bk["missing"]},
            "order": self.model.order_text(),
        }

    def meta(self):
        return {"controls": list(self.spec.values()), "nodes": NODES, "layers": layers(),
                "rules": RULES, "thresholds": self.detector.thresholds, "subsystems": SUBSYSTEMS,
                "stress_kinds": programs.STRESS_KINDS, "predefined": programs.PREDEFINED, "break": programs.BREAK,
                "sample_keys": SAMPLE_KEYS, "control_keys": CONTROL_KEYS, "tick": DT,
                "server": {k: v for k, v in self.server.items() if k != "profile"},
                "levels": LEVELS, "health_names": HEALTH, "cascade_edges": sorted(CASCADE_EDGES),
                "strategies": strategies.catalogue(), "kernel_scenarios": kernel_catalogue(),
                "scenarios": [{k: s[k] for k in ("id", "name", "rule")} for s in strategies.SCENARIOS]}

    # ---- legacy-compatible fields used by the server list / sidebar
    def display_health(self):
        return "OFFLINE" if self.offline else HEALTH[self.health]

    def live_summary(self):
        """Current simulated utilisation for the server list (values of the model, never fabricated)."""
        if self.offline:
            return {"cpu_util": None, "mem_util": None, "disk_util": None, "procs": None, "uptime_s": None}
        V = self.V or {}
        r = lambda k: None if V.get(k) is None else round(float(V[k]), 1)
        return {"cpu_util": r("cpu_util"), "mem_util": r("mem_util"), "disk_util": r("disk_util"),
                "procs": V.get("running_procs"), "uptime_s": int(time.time() - self.boot_ts)}

    def public_server(self):
        return {k: v for k, v in self.server.items() if k != "profile"}

    @property
    def phase(self):
        return "RUNNING" if (self.runner or self.recording) else "IDLE"

    @property
    def exp(self):
        return None


HOST_KEYS = ("cpu_util", "cpu_demand", "run_queue", "run_queue_per_core", "cpu_wait", "cpu_wait_web", "mem_util",
             "mem_ws", "mem_avail", "page_faults", "paging_overhead", "swap_iops", "disk_util", "disk_demand",
             "read_iops", "write_iops", "io_queue", "disk_latency", "iowait_procs", "lock_wait", "paging_penalty",
             "service_ms", "disk_used", "disk_free_gb")
SETTING_KEYS = ("cascade", "anchor", "lock_ordering", "avoidance", "txn_autocomplete")
DL_ACTIONS = ("dl_add_process", "dl_remove_process", "dl_set_max", "dl_add_resource", "dl_remove_resource",
              "dl_reset", "dl_example", "dl_detect")

PRIMARY_METRIC = {"kernel": "availability", "cpu": "run_queue", "memory": "page_faults", "disk": "disk_latency",
                  "disk_space": "disk_used", "network": "rtt",
                  "service": "response_ms", "process": "error_rate", "deadlock": "lock_wait"}


def _episode_public(e):
    keep = ("id", "start_ts", "trigger", "first_crossing", "crossings", "detections", "first_detection", "failure_at",
            "peak", "peak_ts", "peak_values", "peak_controls", "peak_trace", "primary", "primaries", "secondary",
            "loop", "metastable",
            "recovery_start_ts", "recovered_ts", "final_values", "final_controls", "rca", "active", "attribution")
    out = {k: e.get(k) for k in keep}
    out["peak_name"] = HEALTH[e.get("peak", 0)]
    return out
