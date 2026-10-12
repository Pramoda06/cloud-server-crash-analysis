"""
Experiment engine: runs the monitoring loop in a background thread and drives
the lifecycle

    NORMAL → BASELINE → INJECTION → DEGRADATION → DETECTION → FAILED
           → RECOVERY → NORMAL

Server health is ALWAYS taken from the detector's observations, never from
what was injected.

One Engine monitors ONE simulated server. With several servers, the
ServerManager (manager.py) owns one Engine per server, so the process table,
detector state, samples, events and experiment of each server stay separate.
"""
import threading
import time
from collections import deque
from datetime import datetime

from . import config
from .analysis import build_why, summary_text
from .detector import Detector, LEVELS
from .failure_simulator import ProcessTable, make_model
from .metrics import HostMonitor, Noise, base_drivers, derive
from .recovery import within_baseline
from .scenarios import SCENARIOS

HEALTH = {0: "HEALTHY", 1: "WARNING", 2: "DEGRADED", 3: "FAILED"}
TRACK = ["cpu", "mem", "disk", "net_latency", "response_ms", "proc_count", "cpu_queue",
         "wait_ms", "mem_avail_mb", "page_faults", "swap_io", "throughput", "read_iops",
         "write_iops", "io_queue", "disk_latency", "availability", "running_procs",
         "terminated_procs", "blocked_procs", "waiting_procs", "stuck_procs", "locks_held",
         "packet_loss", "req_ok", "req_failed", "timeouts", "reachability", "paging_overhead"]
SERIES = TRACK + ["ctx_switches", "iowait", "tcp_retrans", "swap_used_mb"]


def clock(ts):
    return datetime.fromtimestamp(ts).strftime("%H:%M:%S")


def r(v, n=2):
    return round(v, n) if isinstance(v, float) else v


class Engine:
    def __init__(self, store, server=None, host=None, id_allocator=None):
        self.store = store
        self.server = server or dict(config.SERVER)      # spec of the monitored server
        self.server_id = self.server.get("db_id")
        self.offline = self.server.get("connection") == "Offline"
        self.id_allocator = id_allocator
        self.lock = threading.RLock()
        self.host = host or HostMonitor()
        self.seed_offset = ((self.server_id or 1) - 1) * 1009   # Server-01 keeps the original curves
        self.noise = Noise(config.RANDOM_SEED + self.seed_offset)
        self.detector = Detector()
        self.procs = ProcessTable(ncores=int(self.server.get("vcpus", 4)))
        self.samples = deque(maxlen=config.BUFFER_SAMPLES)
        self.events = deque(maxlen=400)
        self.seq = 0
        self.eseq = 0
        self.phase = "IDLE"
        self.health = "HEALTHY"
        self.model = None
        self.exp = None
        self.last_exp = None
        self.last_result = None
        self.last_recovery = None
        self.auto_recover = False
        self.started = time.time()
        self.current = None
        self.det = None
        self.host_last = {}
        self._ok_streak = 0
        self._thread = None
        self._running = False
        if self.offline:
            self.emit("SYSTEM", f"{self.server['id']} is OFFLINE (connection status) — crash analysis unavailable", "warn")
        else:
            self.emit("SYSTEM", f"Monitoring started — {self.server['id']} in NORMAL operation", "ok")

    # ------------------------------------------------------------ events --
    def emit(self, kind, msg, level="info", marker=None):
        self.eseq += 1
        now = time.time()
        e = {"seq": self.eseq, "ts": now, "time": clock(now), "kind": kind, "msg": msg,
             "level": level, "marker": marker}
        self.events.append(e)
        if self.exp is not None:
            self.exp["events"].append(e)
        return e

    # ------------------------------------------------------------ thread --
    def start(self):
        if self._thread:
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, name="monitor", daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False

    def _loop(self):
        nxt = time.monotonic()
        while self._running:
            try:
                with self.lock:
                    self.tick()
            except Exception as ex:  # never let the monitor die during a demo
                import traceback
                traceback.print_exc()
                self.emit("SYSTEM", f"Internal simulator error handled: {ex}", "warn")
            nxt += config.TICK_SECONDS
            delay = nxt - time.monotonic()
            if delay < -1:
                nxt = time.monotonic()
                delay = 0
            time.sleep(max(0.0, delay))

    # ----------------------------------------------------------- controls --
    def inject(self, sid, severity):
        with self.lock:
            if sid not in SCENARIOS:
                return False, "Unknown failure type"
            if severity not in config.SEVERITIES:
                return False, "Unknown severity"
            if self.offline:
                return False, f"{self.server['id']} is offline — crash analysis needs an online server"
            if self.phase != "IDLE":
                return False, f"An experiment is already in progress ({self.phase})"
            now = time.time()
            self.noise.reseed(config.RANDOM_SEED + self.seed_offset + config.SEVERITIES.index(severity) * 7 +
                              list(SCENARIOS).index(sid) * 101)
            self.detector.reset()
            self.procs.reset()
            try:
                planned = self.id_allocator() if self.id_allocator else self.store.next_id()
            except Exception:
                planned = None
            self.exp = {
                "planned_id": planned,
                "server_id": self.server_id, "server_code": self.server["id"],
                "server_name": self.server.get("name"),
                "scenario": sid, "scenario_name": SCENARIOS[sid]["name"], "severity": severity,
                "baseline_started_at": now, "injected_at": None, "detected_at": None,
                "detection_latency": None, "detected_class": None, "detection_evidence": [],
                "detected_level": 0, "failed_at": None, "recovery_started_at": None,
                "recovery_completed_at": None, "recovery_time": None, "max_level": 0,
                "baseline": {}, "host_baseline": {}, "peaks": {}, "mins": {},
                "events": [], "samples": [], "baseline_samples": [], "crashed_names": [],
                "cycle": [], "response_none": False, "failure_snapshot": None,
                "markers": [{"ts": now, "label": "BASELINE", "kind": "baseline"}],
            }
            self.phase = "BASELINE"
            self.emit("EXPERIMENT", f"Experiment started: {SCENARIOS[sid]['name']} / {severity}", "info")
            self.emit("MONITOR", f"Baseline monitoring started ({config.BASELINE_SECONDS:.0f} s of normal observations)",
                      "info", marker="baseline")
            return True, "Baseline monitoring started"

    def recover(self):
        with self.lock:
            if self.phase != "RUNNING" or self.model is None:
                return False, "No active failure to recover from"
            self._start_recovery(manual=True)
            return True, "Recovery started"

    def _start_recovery(self, manual):
        now = time.time()
        self.phase = "RECOVERING"
        self.exp["recovery_started_at"] = now
        self.exp["markers"].append({"ts": now, "label": "RECOVERY STARTED", "kind": "recovery"})
        self._ok_streak = 0
        how = "operator" if manual else "demo mode (auto)"
        self.emit("RECOVERY", f"RECOVERY STARTED by {how}", "info", marker="recovery")
        self._set_health("RECOVERING")
        self.model.start_recovery(now)

    def reset(self):
        with self.lock:
            was = self.phase
            self.model = None
            self.exp = None
            self.phase = "IDLE"
            self.procs.reset()
            self.detector.reset()
            self._ok_streak = 0
            if was != "IDLE":
                self.emit("EXPERIMENT", "Experiment aborted and server reset to baseline (not saved)", "warn")
            self._set_health("HEALTHY")
            return True, "Reset complete"

    def set_auto(self, on):
        with self.lock:
            self.auto_recover = bool(on)

    # --------------------------------------------------------------- tick --
    def _set_health(self, h):
        if h != self.health:
            old = self.health
            self.health = h
            lvl = {"HEALTHY": "ok", "WARNING": "warn", "DEGRADED": "warn",
                   "FAILED": "fail", "RECOVERING": "info"}[h]
            self.emit("STATE", f"SERVER STATE {old} → {h}", lvl)

    def tick(self, host=None):
        now = time.time()
        dt = config.TICK_SECONDS
        host = host if host is not None else self.host.sample()
        self.host_last = host
        exp = self.exp

        # baseline window finished -> inject the failure
        if self.phase == "BASELINE" and now - exp["baseline_started_at"] >= config.BASELINE_SECONDS:
            self._capture_baseline()
            self.phase = "RUNNING"
            exp["injected_at"] = now
            exp["markers"].append({"ts": now, "label": "FAILURE INJECTED", "kind": "inject"})
            self.model = make_model(exp["scenario"], exp["severity"], self.procs, self.emit, self.noise,
                                    self.server)
            self.emit("INJECTION", f"FAILURE INJECTED: {exp['scenario_name']} / {exp['severity']}",
                      "fail", marker="inject")
            self.model.on_inject(now)

        d = base_drivers(self.noise, host, self.server)
        if self.model is not None:
            self.model.step(dt, now, d)
        else:
            for p in self.procs.list():   # normal: transaction workers make progress
                if p["role"] == "txn":
                    p["progress"] = (p["progress"] + dt * 9) % 100
        m = derive(d, self.procs, self.server)
        if self.model is not None and getattr(self.model, "queue_len", None) is not None:
            m["cpu_queue"] = float(self.model.queue_len)   # run queue = processes in READY state
        snap = self.procs.snapshot()
        det = self.detector.evaluate(m, snap, now)
        m["deadlock"] = "YES" if det["deadlock"]["detected"] else "NO"
        self.det = det

        self._handle_detection(det, now, m)

        # recovery completion: model finished AND observations back to baseline
        if self.phase == "RECOVERING":
            ok, _ = within_baseline(m, exp["baseline"], snap)
            if self.model.recovery_done and ok and det["raw_level"] == 0:
                self._ok_streak += 1
            else:
                self._ok_streak = 0
            if self._ok_streak >= config.HEALTHY_CONFIRM_SAMPLES:
                self._complete(now)
        elif self.phase == "RUNNING":
            if self.auto_recover and exp["detected_at"] and now - exp["detected_at"] >= config.AUTO_RECOVER_AFTER_S:
                self._start_recovery(manual=False)
            elif now - exp["injected_at"] > config.MAX_FAILURE_SECONDS:
                self.emit("SYSTEM", "Safety limit reached — automatic recovery", "warn")
                self._start_recovery(manual=False)

        # health shown to the user comes from the DETECTOR
        if self.phase != "RECOVERING":
            self._set_health(HEALTH[det["level"]])

        self.seq += 1
        sample = {"seq": self.seq, "ts": round(now, 3), "phase": self.phase, "health": self.health,
                  "real": d.get("real_anchored", False) and (self.model is None)}
        for k in SERIES:
            sample[k] = r(m[k])
        self.samples.append(sample)
        self.current = {"metrics": {k: r(v) for k, v in m.items()}, "snapshot": snap}
        if exp is not None and self.exp is exp:
            if self.phase in ("BASELINE",):
                exp["baseline_samples"].append(sample)
            exp["samples"].append(sample)
            if self.phase in ("RUNNING", "RECOVERING"):
                self._track(exp, m, det, snap)

    def _capture_baseline(self):
        exp = self.exp
        bs = exp["baseline_samples"] or list(self.samples)[-6:]
        base = {}
        for k in TRACK:
            vals = [s[k] for s in bs if isinstance(s.get(k), (int, float))]
            if vals:
                base[k] = sum(vals) / len(vals)
        exp["baseline"] = base
        h = self.host_last
        exp["host_baseline"] = {k: h.get(k) for k in ("cpu", "mem", "procs", "disk_mb_s", "net_kb_s")}
        src = "REAL host-anchored CPU/memory (psutil)" if config.USE_REAL_BASELINE and h.get("available") else "model"
        self.emit("MONITOR", f"Baseline captured — CPU {base.get('cpu', 0):.1f}%, "
                             f"memory {base.get('mem', 0):.1f}%, response {base.get('response_ms', 0):.0f} ms "
                             f"[{src}]", "ok")

    def _track(self, exp, m, det, snap):
        for k in TRACK:
            v = m.get(k)
            if v is None:
                if k == "response_ms":
                    exp["response_none"] = True
                continue
            if k not in exp["peaks"] or v > exp["peaks"][k]:
                exp["peaks"][k] = v
            if k not in exp["mins"] or v < exp["mins"][k]:
                exp["mins"][k] = v
        if det["level"] > exp["max_level"]:
            exp["max_level"] = det["level"]
            exp["failure_snapshot"] = {"metrics": {k: r(v) for k, v in m.items()}, "snapshot": snap,
                                       "deadlock": det["deadlock"]}
        for p in snap["processes"]:
            if p["state"] == "TERMINATED" and p["critical"] and p["name"] not in exp["crashed_names"]:
                exp["crashed_names"].append(p["name"])
        if det["deadlock"]["cycle"] and not exp["cycle"]:
            exp["cycle"] = det["deadlock"]["cycle"]

    def _handle_detection(self, det, now, m):
        exp = self.exp
        for t in det["transitions"]:
            rule = SCENARIOS[t["rule"]]["name"]
            lvl = LEVELS[t["to"]]
            ev = "; ".join(t["evidence"]) or "threshold exceeded"
            self.emit("DETECTOR", f"{rule.upper()} rule → {lvl}: {ev}",
                      "fail" if t["to"] == 3 else "warn")
        if exp is None or self.phase != "RUNNING":
            return
        if det["level"] > 0 and exp["detected_at"] is None:
            exp["detected_at"] = now
            exp["detection_latency"] = now - exp["injected_at"]
            exp["detected_class"] = det["classification"]
            exp["detection_evidence"] = det["evidence"]
            exp["detected_level"] = det["level"]
            exp["markers"].append({"ts": now, "label": "FAILURE DETECTED", "kind": "detect"})
            self.emit("DETECTION", f"FAILURE DETECTED: {det['classification_name']} "
                                   f"(latency {exp['detection_latency']:.1f} s)", "fail", marker="detect")
        elif exp["detected_at"] is not None and det["level"] > exp["detected_level"]:
            exp["detected_level"] = det["level"]
            exp["detected_class"] = det["classification"]
            exp["detection_evidence"] = det["evidence"]
        if det["level"] == 3 and exp["failed_at"] is None:
            exp["failed_at"] = now
            exp["markers"].append({"ts": now, "label": "FAILED", "kind": "failed"})

    def _complete(self, now):
        exp = self.exp
        exp["recovery_completed_at"] = now
        exp["recovery_time"] = now - exp["recovery_started_at"]
        exp["markers"].append({"ts": now, "label": "RECOVERY COMPLETED", "kind": "recovered"})
        self.emit("RECOVERY", f"RECOVERY COMPLETED in {exp['recovery_time']:.1f} s — "
                              f"metrics back within baseline tolerance", "ok", marker="recovered")
        self.model.finish()
        self.model = None
        self.procs.reset()
        self.phase = "IDLE"
        self._set_health("HEALTHY")
        self.last_recovery = {"ts": now, "time": clock(now), "duration": exp["recovery_time"]}
        exp["max_level_name"] = LEVELS[exp["max_level"]]
        exp["final_status"] = "RECOVERED"
        try:
            eid = self._save(exp)
            exp["id"] = eid
            self.emit("EXPERIMENT", f"Experiment #{eid} saved to history (SQLite)", "ok")
        except Exception as ex:
            exp["id"] = None
            self.emit("SYSTEM", f"Could not save experiment: {ex}", "warn")
        self.last_exp = exp
        self.last_result = self.result_of(exp)
        self.exp = None

    # ------------------------------------------------------------- output --
    def result_of(self, exp):
        sc = SCENARIOS[exp["scenario"]]
        pm, how = sc["peak_metric"]
        pv = exp["mins"].get(pm) if how == "min" else exp["peaks"].get(pm)
        from .metrics import METRICS
        return {
            "id": exp.get("id"),
            "failure_type": exp["scenario"],
            "failure_name": exp["scenario_name"],
            "severity": exp["severity"],
            "subsystem": sc["subsystem"],
            "injected": exp["scenario_name"],
            "detected": SCENARIOS[exp["detected_class"]]["name"] if exp["detected_class"] else "NOT DETECTED",
            "match": exp["detected_class"] == exp["scenario"],
            "injected_at": exp["injected_at"], "injected_time": clock(exp["injected_at"]) if exp["injected_at"] else None,
            "detected_at": exp["detected_at"], "detected_time": clock(exp["detected_at"]) if exp["detected_at"] else None,
            "detection_latency": exp["detection_latency"],
            "recovery_started_at": exp["recovery_started_at"],
            "recovery_started_time": clock(exp["recovery_started_at"]) if exp["recovery_started_at"] else None,
            "recovery_completed_at": exp["recovery_completed_at"],
            "recovery_completed_time": clock(exp["recovery_completed_at"]) if exp["recovery_completed_at"] else None,
            "recovery_time": exp["recovery_time"],
            "max_health": HEALTH[exp["max_level"]],
            "peak_cpu": exp["peaks"].get("cpu"), "peak_memory": exp["peaks"].get("mem"),
            "peak_disk": exp["peaks"].get("disk"), "peak_network_latency": exp["peaks"].get("net_latency"),
            "peak_response": exp["peaks"].get("response_ms"),
            "peak_specific_name": ("Min " if how == "min" else "Peak ") + METRICS[pm]["label"],
            "peak_specific_key": pm, "peak_specific_value": pv,
            "final_status": exp.get("final_status", "IN PROGRESS"),
            "server_id": exp.get("server_id"), "server_code": exp.get("server_code"),
            "server_name": exp.get("server_name"),
            "analysis": summary_text(exp) if exp.get("final_status") else None,
        }

    def _save(self, exp):
        res = self.result_of(exp)
        rec = {
            "timestamp": datetime.fromtimestamp(exp["baseline_started_at"]).strftime("%Y-%m-%d %H:%M:%S"),
            "failure_type": exp["scenario"], "failure_name": exp["scenario_name"],
            "severity": exp["severity"], "failure_injected": res["injected"],
            "failure_detected": res["detected"], "detection_match": 1 if res["match"] else 0,
            "injected_at": exp["injected_at"], "detected_at": exp["detected_at"],
            "detection_latency": exp["detection_latency"], "max_health": res["max_health"],
            "peak_cpu": res["peak_cpu"], "peak_memory": res["peak_memory"], "peak_disk": res["peak_disk"],
            "peak_network_latency": res["peak_network_latency"], "peak_response": res["peak_response"],
            "peak_specific_name": res["peak_specific_name"], "peak_specific_value": res["peak_specific_value"],
            "recovery_started_at": exp["recovery_started_at"],
            "recovery_completed_at": exp["recovery_completed_at"], "recovery_time": exp["recovery_time"],
            "final_status": exp["final_status"], "analysis": res["analysis"],
            "server_id": exp.get("server_id"),
        }
        if exp.get("planned_id"):
            rec = dict(id=exp["planned_id"], **rec)
        details = {
            "result": res,
            "why": build_why(exp),
            "baseline": exp["baseline"], "host_baseline": exp["host_baseline"],
            "peaks": exp["peaks"], "mins": exp["mins"],
            "events": exp["events"], "samples": exp["samples"], "markers": exp["markers"],
            "failure_snapshot": exp["failure_snapshot"],
            "server": {k: v for k, v in self.server.items() if k != "profile"},
        }
        try:
            return self.store.save(rec, details)
        except Exception:
            rec.pop("id", None)          # planned id taken -> let SQLite choose
            return self.store.save(rec, details)

    def display_health(self):
        """Health shown to the user: detector-driven, or OFFLINE when the server is not connected."""
        return "OFFLINE" if self.offline else self.health

    def public_server(self):
        return {k: v for k, v in self.server.items() if k != "profile"}

    def stage(self):
        """Lifecycle stage index for the UI stepper."""
        exp = self.exp
        if self.phase == "IDLE":
            return 0 if not self.last_exp or (time.time() - self.last_exp["recovery_completed_at"] > 30) else 7
        if self.phase == "BASELINE":
            return 1
        if self.phase == "RUNNING":
            if exp["detected_at"] is None:
                return 3 if time.time() - exp["injected_at"] > 0.6 else 2
            return 5 if self.det and self.det["level"] >= 2 else 4
        if self.phase == "RECOVERING":
            return 6
        return 0

    def state(self, since=0, esince=0):
        with self.lock:
            latest = self.seq
            reset = since > latest
            if reset:
                since = 0
            samples = [s for s in self.samples if s["seq"] > since]
            if since == 0:
                samples = samples[-180:]
            events = [e for e in self.events if e["seq"] > (0 if reset else esince)]
            exp = self.exp or self.last_exp
            live = None
            if exp is not None:
                live = {
                    "active": self.exp is not None,
                    "id": exp.get("id") or exp.get("planned_id"),
                    "phase": self.phase,
                    "scenario": exp["scenario"], "scenario_name": exp["scenario_name"],
                    "severity": exp["severity"], "markers": exp["markers"],
                    "baseline_started_at": exp["baseline_started_at"],
                    "baseline_remaining": max(0.0, config.BASELINE_SECONDS - (time.time() - exp["baseline_started_at"]))
                    if self.phase == "BASELINE" else 0,
                    "baseline": {k: r(v) for k, v in exp["baseline"].items()},
                    "result": self.result_of(exp),
                    "why": build_why(exp) if exp["detected_at"] else None,
                    "peaks": {k: r(v) for k, v in exp["peaks"].items()},
                    "mins": {k: r(v) for k, v in exp["mins"].items()},
                    "failure_snapshot": exp["failure_snapshot"],
                }
            det = self.det or {}
            uptime = time.time() - self.started + 12 * 86400 + 4 * 3600   # simulated server uptime offset
            return {
                "seq": latest, "eseq": self.eseq, "reset": reset,
                "now": time.time(),
                "phase": self.phase, "health": self.display_health(), "stage": self.stage(),
                "auto_recover": self.auto_recover,
                "server": dict(self.public_server(), uptime_s=uptime,
                               current_failure=(self.exp["scenario_name"] if self.exp else None),
                               severity=(self.exp["severity"] if self.exp else None),
                               current_event=(self.events[-1]["msg"] if self.events else ""),
                               last_recovery=self.last_recovery),
                "host": self.host_last,
                "data_mode": ("SIMULATED FAILURE MODEL ACTIVE" if self.model is not None
                              else "REAL BASELINE (psutil-anchored)" if config.USE_REAL_BASELINE and self.host_last.get("available")
                              else "MODEL BASELINE"),
                "current": None if self.offline else self.current,
                "detector": {"level": det.get("level_name"), "classification": det.get("classification"),
                             "classification_name": det.get("classification_name"),
                             "evidence": det.get("evidence"), "rules": det.get("rules"),
                             "deadlock": det.get("deadlock")},
                "samples": samples, "events": events,
                "experiment": live,
                "last_result": self.last_result,
            }
