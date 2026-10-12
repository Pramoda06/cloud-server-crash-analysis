"""
Recovery verification.

Recovery actions themselves are modelled per scenario in failure_simulator.py
(each model has RECOVERY_STEPS). This module decides — from observations only —
when the server is genuinely back to normal: the detector reports OK AND every
key metric is back within tolerance of the baseline captured before injection.
"""
from . import config


def within_baseline(m, baseline, snap):
    tol = config.RECOVERY_TOLERANCE
    checks = {
        "cpu": m["cpu"] <= baseline.get("cpu", 30) + tol["cpu_above_baseline"],
        "mem": m["mem"] <= baseline.get("mem", 50) + tol["mem_above_baseline"],
        "page_faults": m["page_faults"] <= tol["page_faults_max"],
        "io_queue": m["io_queue"] <= tol["io_queue_max"],
        "disk_latency": m["disk_latency"] <= tol["disk_latency_max"],
        "net_latency": m["net_latency"] <= tol["net_latency_max"],
        "packet_loss": m["packet_loss"] <= tol["packet_loss_max"],
        "availability": m["availability"] >= 100,
        "processes": all(p["state"] in ("RUNNING", "READY", "SLEEPING") for p in snap["processes"]),
        "locks": not any(r["holder"] for r in snap["resources"]),
    }
    return all(checks.values()), checks
