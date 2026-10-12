"""
Builds the "WHY DID THE SYSTEM FAIL?" panel and the post-experiment summary
dynamically from what was actually observed in the experiment.
"""
from .metrics import METRICS
from .scenarios import SCENARIOS
from . import config

LOWER_IS_WORSE = {"mem_avail_mb", "availability", "reachability", "throughput",
                  "req_ok", "running_procs"}

KEY_SYMPTOMS = {
    "cpu": ["cpu", "cpu_queue", "wait_ms", "response_ms"],
    "memory": ["mem", "mem_avail_mb", "page_faults", "swap_io", "response_ms", "throughput"],
    "disk": ["disk", "io_queue", "disk_latency", "write_iops", "response_ms"],
    "process": ["availability", "terminated_procs", "req_failed"],
    "deadlock": ["waiting_procs", "blocked_procs", "locks_held", "throughput", "response_ms"],
    "network": ["net_latency", "packet_loss", "reachability", "timeouts", "req_failed"],
}

SECONDARY = {
    "cpu": ["mem", "disk", "net_latency"],
    "memory": ["disk", "net_latency"],
    "disk": ["cpu", "mem", "net_latency"],
    "process": ["cpu", "mem", "disk", "net_latency"],
    "deadlock": ["cpu", "mem", "disk", "net_latency"],
    "network": ["cpu", "mem", "disk"],
}


def fmt(k, v):
    if v is None:
        return "no response"
    meta = METRICS.get(k, {})
    dp = meta.get("dp", 1)
    unit = meta.get("unit", "")
    if isinstance(v, str):
        return v
    if dp == 0:
        s = f"{v:,.0f}"
    else:
        s = f"{v:,.{dp}f}"
    sep = "" if unit in ("%",) else " "
    return f"{s}{sep}{unit}".strip()


def symptom(k, base, peak):
    label = METRICS[k]["label"]
    if peak is None:
        return None
    if base is None:
        return f"{label} reached {fmt(k, peak)}"
    worse_low = k in LOWER_IS_WORSE
    if k == "response_ms" and peak is None:
        return "No successful responses were returned to clients"
    if worse_low:
        if peak >= base * 0.97:
            return None
        return f"{label} dropped from {fmt(k, base)} to {fmt(k, peak)}"
    if base and peak <= base * 1.05 and peak - base < 1:
        return None
    ratio = (peak / base) if base and base > 0.5 else None
    tail = f" ({ratio:.0f}× baseline)" if ratio and ratio >= 3 else ""
    return f"{label} rose from {fmt(k, base)} to {fmt(k, peak)}{tail}"


def extreme(exp, k):
    if k in LOWER_IS_WORSE:
        return exp["mins"].get(k)
    return exp["peaks"].get(k)


def build_why(exp):
    """exp: live experiment dict from the engine."""
    det = exp.get("detected_class")
    sid = det or exp["scenario"]
    sc = SCENARIOS[sid]
    base = exp.get("baseline", {})
    symptoms = []
    for k in KEY_SYMPTOMS[sid]:
        s = symptom(k, base.get(k), extreme(exp, k))
        if s:
            symptoms.append(s)
    if sid == "process":
        crashed = exp.get("crashed_names") or []
        if crashed:
            symptoms.insert(0, "Process state changed RUNNING → TERMINATED for: " + ", ".join(crashed))
        if exp.get("response_none"):
            symptoms.append("No successful responses were returned while the service was down")
    if sid == "deadlock" and exp.get("cycle"):
        symptoms.insert(0, "Circular wait: " + " → ".join(exp["cycle"] + exp["cycle"][:1]))
    unaffected = []
    for k in SECONDARY[sid]:
        b, p = base.get(k), exp["peaks"].get(k)
        if b is not None and p is not None:
            unaffected.append(f"{METRICS[k]['label']}: {fmt(k, b)} → peak {fmt(k, p)}")

    notes = []
    if sid == "memory":
        notes.append(sc["thrashing"])
        notes.append("Causal chain: memory pressure → more page faults → more paging → "
                     "increased latency → reduced throughput.")
    if sid == "process":
        notes.append("A server can have healthy hardware/resource metrics while still "
                     "experiencing service failure because a critical process has terminated.")
    if sid == "deadlock":
        notes.append("All four Coffman conditions held: mutual exclusion, hold and wait, "
                     "no preemption and circular wait. Removing any one prevents deadlock.")
        notes.append("CPU utilization fell during the deadlock — blocked processes consume "
                     "no CPU, so a resource metric alone would not reveal this failure.")
    if sid == "network":
        notes.append("Healthy server internals + network communication failure = service "
                     "unavailable. Internal OS health and external availability are different.")
    if sid == "cpu":
        notes.append("As utilization approaches 100%, waiting time grows non-linearly "
                     "(queueing: wait ∝ 1 / (1 − ρ)).")

    if exp.get("detected_at"):
        detection = (f"{sc['detection']} The {sc['name']} rule fired "
                     f"{exp['detection_latency']:.1f} s after injection.")
    else:
        detection = "Not yet detected — the monitoring rules have not observed a threshold breach."

    if exp.get("recovery_completed_at"):
        recovery = f"{sc['recovery']} Measured recovery time: {exp['recovery_time']:.1f} s."
    elif exp.get("recovery_started_at"):
        recovery = f"In progress — {sc['recovery']}"
    else:
        recovery = "Pending — click Recover Server to start recovery. " \
                   f"Planned action: {sc['recovery']}"

    return {
        "available": bool(exp.get("detected_at")),
        "root_cause": sc["root_cause"],
        "explanation": sc.get("explanation", ""),
        "subsystem": sc["subsystem"],
        "symptoms": symptoms,
        "unaffected": unaffected,
        "notes": notes,
        "detection": detection,
        "evidence": exp.get("detection_evidence", []),
        "recovery": recovery,
        "prevention": sc["prevention"],
        "injected": SCENARIOS[exp["scenario"]]["name"],
        "detected": sc["name"] if det else None,
        "match": det == exp["scenario"] if det else None,
    }


def summary_text(exp):
    sc = SCENARIOS[exp["scenario"]]
    sid = exp["scenario"]
    b, p = exp["baseline"], exp["peaks"]
    sev = exp["severity"]
    lvl = exp.get("max_level_name", "OK")
    parts = [f"Injecting {sc['name']} at {sev} severity drove {exp.get('server_code') or 'Server-01'} to the {lvl} state."]
    pm, how = sc["peak_metric"]
    v = exp["mins"].get(pm) if how == "min" else p.get(pm)
    if sid == "cpu":
        parts.append(f"Processor utilization rose from {fmt('cpu', b.get('cpu'))} to "
                     f"{fmt('cpu', p.get('cpu'))}; the run queue reached {fmt('cpu_queue', p.get('cpu_queue'))} "
                     f"and waiting time {fmt('wait_ms', p.get('wait_ms'))}, so response time peaked at "
                     f"{fmt('response_ms', p.get('response_ms'))}. Memory and disk stayed near baseline, "
                     f"confirming a scheduling bottleneck.")
    elif sid == "memory":
        parts.append(f"Memory use reached {fmt('mem', p.get('mem'))} (available memory fell to "
                     f"{fmt('mem_avail_mb', exp['mins'].get('mem_avail_mb'))}); page faults rose to "
                     f"{fmt('page_faults', p.get('page_faults'))} and swap I/O to {fmt('swap_io', p.get('swap_io'))}. "
                     f"Throughput fell to {fmt('throughput', exp['mins'].get('throughput'))} — the system was thrashing. "
                     f"CPU peaked at only {fmt('cpu', p.get('cpu'))}, mostly paging overhead.")
    elif sid == "disk":
        parts.append(f"Disk utilization reached {fmt('disk', p.get('disk'))}, the I/O queue grew to "
                     f"{fmt('io_queue', p.get('io_queue'))} and disk latency to "
                     f"{fmt('disk_latency', p.get('disk_latency'))}, raising response time to "
                     f"{fmt('response_ms', p.get('response_ms'))} while CPU peaked at only {fmt('cpu', p.get('cpu'))}.")
    elif sid == "process":
        parts.append(f"Service availability fell to {fmt('availability', v)} while CPU "
                     f"({fmt('cpu', p.get('cpu'))}) and memory ({fmt('mem', p.get('mem'))}) stayed normal — "
                     f"a process-lifecycle failure, not a resource shortage.")
    elif sid == "deadlock":
        parts.append(f"A circular wait ({' → '.join((exp.get('cycle') or []) + (exp.get('cycle') or [])[:1])}) "
                     f"left {fmt('stuck_procs', v)} processes unable to progress; throughput fell to "
                     f"{fmt('throughput', exp['mins'].get('throughput'))} and CPU dropped to "
                     f"{fmt('cpu', exp['mins'].get('cpu'))} because blocked processes do not run.")
    elif sid == "network":
        parts.append(f"Network RTT rose to {fmt('net_latency', p.get('net_latency'))} with "
                     f"{fmt('packet_loss', p.get('packet_loss'))} packet loss, and reachability fell to "
                     f"{fmt('reachability', exp['mins'].get('reachability'))}, while CPU "
                     f"({fmt('cpu', p.get('cpu'))}) and memory stayed normal.")
    if exp.get("detected_at"):
        m = "matching" if exp.get("detected_class") == sid else "NOT matching"
        parts.append(f"The monitor independently classified the failure as "
                     f"{SCENARIOS[exp['detected_class']]['name']} ({m} the injected fault) "
                     f"{exp['detection_latency']:.1f} s after injection.")
    else:
        parts.append("The monitoring rules did not detect a failure before recovery.")
    if exp.get("recovery_time") is not None:
        parts.append(f"Recovery restored baseline behaviour in {exp['recovery_time']:.1f} s.")
    return " ".join(parts)
