"""
Root-cause analysis of one failure episode — built ONLY from what the engine recorded:
the trigger inputs, the threshold crossings, the detector transitions, the attribution
(primary / secondary), the causal trace at the peak and the recovery events.
"""
from .detect import HEALTH, LEVELS, RULES
from .graph import NODES

CONCURRENT_SAMPLES = 4      # 2 s at 0.5 s per sample: independent failures this close are reported as concurrent

PRIMARY_KEY = {"cpu": ["cpu_util", "run_queue", "cpu_wait", "response_ms"],
               "memory": ["mem_util", "page_faults", "paging_overhead", "response_ms"],
               "disk": ["disk_util", "io_queue", "disk_latency", "response_ms"],
               "disk_space": ["disk_used", "disk_free_gb", "disk_latency", "error_rate"],
               "network": ["rtt", "packet_loss", "retrans_ms", "error_rate"],
               "service": ["req_eff", "accept_backlog", "response_ms", "error_rate"],
               "process": ["workers_alive", "capacity", "error_rate", "availability"],
               "deadlock": ["lock_wait", "blocked_procs", "response_ms", "error_rate"],
               "kernel": ["availability", "error_rate", "workers_alive", "throughput"]}

RECOVERY_TEXT = {
    "cpu": "Runnable work fell below the CPU capacity, so the run queue drained and waiting time fell.",
    "memory": "The working set shrank back below physical RAM; pages were faulted back in and paging stopped.",
    "disk": "I/O demand fell below the device capacity; the backlog was served and latency returned to normal.",
    "disk_space": "The log writer was stopped (closing its files) and the log files were deleted, so their blocks "
                  "were returned to the file system and writes succeed again.",
    "network": "Path delay and loss were removed; retransmissions and timeouts stopped.",
    "service": "Arrivals fell below worker capacity (or capacity returned); the request backlog drained.",
    "process": "The terminated processes were restarted (TERMINATED → RESTARTING → RUNNING with new PIDs).",
    "kernel": "The machine was rebooted: firmware, kernel, root filesystem (journal replay), PID 1 and the services "
              "started again from a clean state; volatile state from before the panic was lost.",
    "deadlock": "The circular wait was broken (victim terminated / lock released or pre-empted); blocked "
                "processes were granted their locks.",
}


EXTRA_LABELS = {"workers_alive": {"label": "Web workers alive", "unit": ""}}


def _fmt(v, unit):
    if v is None:
        return "—"
    if isinstance(v, str):
        return v
    s = f"{v:,.0f}" if abs(v) >= 100 else f"{v:.1f}"
    return s + ("%" if unit == "%" else f" {unit}" if unit else "")


def build_rca(ep, eng):
    prim = ep.get("primary")
    pv = ep.get("peak_values") or {}
    base = eng.base_V
    trig = (ep.get("trigger") or {}).get("last")
    fd = ep.get("first_detection")
    fc = ep.get("first_crossing")
    t_trig = trig["ts"] if trig else ep["start_ts"]
    timing = {
        "trigger_ts": trig["ts"] if trig else None,
        "first_crossing_ts": fc["ts"] if fc else None,
        "first_detection_ts": fd["ts"] if fd else None,
        "failure_ts": (ep.get("failure_at") or {}).get("ts"),
        "peak_ts": ep.get("peak_ts"),
        "recovery_start_ts": ep.get("recovery_start_ts"),
        "recovered_ts": ep.get("recovered_ts"),
        "detection_latency": (fd["ts"] - t_trig) if fd else None,
        "confirm_delay": (fd["ts"] - fc["ts"]) if (fd and fc) else None,
        "recovery_time": (ep["recovered_ts"] - (ep.get("recovery_start_ts") or ep["peak_ts"]))
        if ep.get("recovered_ts") else None,
        "failure_duration": ((ep.get("recovered_ts") or ep.get("peak_ts")) - ep["start_ts"]),
    }
    # symptoms: worst crossing per check, in time order
    worst = {}
    for c in ep.get("crossings") or []:
        k = (c["rule"], c["check"])
        if k not in worst or c["level"] > worst[k]["level"]:
            worst[k] = dict(c, first_ts=worst.get(k, c)["ts"])
    symptoms = sorted(worst.values(), key=lambda c: c["first_ts"])
    sym_txt = [f"{c['label']} reached {LEVELS[c['level']]} ({_fmt(c['value'], c['unit'])}"
               + (f" > {_fmt(c['threshold'], c['unit'])}" if c.get("threshold") is not None else "") + ")"
               for c in symptoms]
    observed = []
    if prim:
        for k in PRIMARY_KEY[prim]:
            n = NODES.get(k) or EXTRA_LABELS.get(k, {"label": k, "unit": ""})
            if k in pv and pv[k] is not None and base.get(k) is not None:
                observed.append({"key": k, "label": n["label"], "unit": n["unit"], "baseline": round(base[k], 2),
                                 "peak": pv[k],
                                 "text": f"{n['label']}: {_fmt(base[k], n['unit'])} → {_fmt(pv[k], n['unit'])}"})
    # recovery mechanism: what changed between the peak and the end
    mech = []
    pc, fcn = ep.get("peak_controls") or {}, ep.get("final_controls") or eng.model.controls
    for k, v in pc.items():
        if k in fcn and fcn[k] != v and k in eng.spec:
            c = eng.spec[k]
            mech.append(f"{c['label']} {v:g} → {fcn[k]:g}{'%' if c['unit'] == '%' else ' ' + c['unit'] if c['unit'] else ''}")
    for e in ep.get("events") or []:
        if e["kind"] in ("ACTION",) and e["ts"] >= ep.get("peak_ts", 0) - 0.01:
            mech.append(e["msg"])
        if e["kind"] == "STRESS" and "removed" in e["msg"]:
            mech.append(e["msg"])
    sec = ep.get("secondary") or []
    detection = None
    if prim:
        th = eng.detector.thresholds.get(prim, {})
        detection = {"rule": RULES[prim]["name"], "logic": RULES[prim]["logic"],
                     "thresholds": {RULES[prim]["checks"][k][0] if k in RULES[prim]["checks"] else k: v
                                    for k, v in th.items()},
                     "confirm": "a level counts only after it is seen in 2 consecutive samples (0.5 s apart); once raised, "
                                "it clears only when the value falls 5 % below its threshold (hysteresis)",
                     "first": fd}
    # independent root causes that reached failure within CONCURRENT_SAMPLES of each other cannot be ordered
    # reliably (each rule needs 2 samples to confirm and metrics ramp at different speeds):
    # report them together instead of presenting the tie-break order (detect.ORDER) as a cause
    prims = ep.get("primaries") or ([prim] if prim else [])
    ftick = ep.get("first_failed_tick") or {}           # sample numbers: exact in live and headless runs
    concurrent = [r for r in prims if prim and r in ftick and prim in ftick
                  and abs(ftick[r] - ftick[prim]) <= CONCURRENT_SAMPLES]
    concurrent = concurrent if len(concurrent) > 1 else []
    chain = (ep.get("peak_trace") or {}).get("sentences") or []
    final = HEALTH[eng.health] if ep.get("active") else "HEALTHY"
    return {
        "primary": {"rule": prim, "name": RULES[prim]["name"] if prim else None,
                    "subsystem": RULES[prim]["subsystem"] if prim else None,
                    "via": (ep.get("attribution") or {}).get(prim, {}).get("via") if prim else None,
                    "others": [RULES[r]["name"] for r in (ep.get("primaries") or [])[1:]],
                    "concurrent": [RULES[r]["name"] for r in concurrent],
                    "concurrent_subsystems": [RULES[r]["subsystem"] for r in concurrent]},
        "trigger": {"text": trig["text"] if trig else "no operator input in the preceding 30 s",
                    "inputs": [i["text"] for i in (ep.get("trigger") or {}).get("inputs", [])]},
        "observed": observed, "symptoms": sym_txt,
        "first_crossing": ({"text": f"{fc['label']} crossed {LEVELS[fc['level']]}", "ts": fc["ts"]} if fc else None),
        "secondary": [{"name": s["name"], "by": RULES[s["by"]]["name"], "via": s["via"]} for s in sec],
        "detection": detection,
        "recovery": {"mechanism": mech[:8], "explanation": RECOVERY_TEXT.get(prim, "") if prim else "",
                     "completed": bool(ep.get("recovered_ts"))},
        "chain": chain, "peak": HEALTH[ep.get("peak", 0)], "final": final, "timing": timing,
        "loop": ep.get("loop"), "metastable": ep.get("metastable"),
        "detections": [{"name": d["name"], "level": LEVELS[d["level"]], "ts": d["ts"]} for d in ep.get("detections") or []],
    }
