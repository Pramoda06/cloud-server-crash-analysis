"""
Central configuration for the Cloud Server Failure Simulator.

Every detection threshold lives here so it can be changed without touching
the detection logic (configurable thresholds, not hard-coded values).
"""

# ---------------------------------------------------------------- timing ---
TICK_SECONDS = 0.5             # monitoring sample interval (2 samples / second)
BASELINE_SECONDS = 6.0         # baseline observation window before injection
DETECT_CONFIRM_SAMPLES = 2     # a level must be observed this many samples in a row
HEALTHY_CONFIRM_SAMPLES = 3    # samples back within baseline before "recovered"
BUFFER_SAMPLES = 720           # live ring buffer (6 minutes)
AUTO_RECOVER_AFTER_S = 8.0     # demo mode: auto-recover N s after detection
MAX_FAILURE_SECONDS = 180.0    # safety: auto-recover if left failed this long

RANDOM_SEED = 2026             # seeded noise -> reproducible curves

# ---------------------------------------------------- interactive mode ---
# The control the operator sets is the TARGET. The simulated system does not
# jump to it: the effective condition follows the target as a first-order lag
#   e(t+dt) = e + (target - e) * (1 - exp(-dt / tau))
# (in-flight work finishing, pages faulting back in, I/O queue draining).
# Falling is slower than rising: draining a backlog takes longer than filling it.
CONTROL_TAU_UP_S = 1.2
CONTROL_TAU_DOWN_S = 1.8
INTERACTIVE_MAX_SECONDS = 1800.0   # safety: ramp controls down after 30 min
DEBUG_SIM = False                  # console trace ([SIMULATION]/[METRICS]/...) — app.py --debug-sim

# ------------------------------------------------------- real baseline ---
# During normal operation Server-01's CPU / memory follow the REAL host
# readings from psutil, linearly scaled into the server's healthy band so that
# a busy laptop can never trigger a false alarm during the demo.
USE_REAL_BASELINE = True
REAL_BASELINE_BAND = {
    "cpu": (12.0, 36.0),       # host 0..100 %  ->  server 12..36 %
    "mem": (36.0, 56.0),       # host 0..100 %  ->  server 36..56 %
}

# ------------------------------------------------------- simulated server ---
SERVER = {
    "id": "Server-01",
    "name": "Cloud-Server-01",
    "region": "Hyderabad (ap-south-2)",
    "os": "Ubuntu Linux 22.04 LTS",
    "kernel": "5.15.0-91-generic",
    "vcpus": 4,
    "ram_mb": 16384,
    "swap_mb": 4096,
    "disk": "NVMe SSD · 6000 IOPS",
    "disk_iops_capacity": 6000,
    "ip": "10.0.1.15",
    "scheduler": "CFS (Completely Fair Scheduler)",
}

SEVERITIES = ["LOW", "MEDIUM", "HIGH", "CRITICAL"]
INTERACTIVE = "INTERACTIVE"    # "severity" recorded for interactive (slider-driven) experiments

# ------------------------------------------------------------ thresholds ---
# W = WARNING, D = DEGRADED, F = FAILED
THRESHOLDS = {
    "cpu": {"cpu": {"W": 85, "D": 92, "F": 97}},
    "memory": {
        "mem": {"W": 85, "D": 90, "F": 95},
        "page_faults": {"W": 500, "D": 1500, "F": 4000},
    },
    "disk": {
        "io_queue": {"W": 4, "D": 8, "F": 16},
        "disk_latency": {"W": 20, "D": 50, "F": 100},
    },
    "process": {
        # lower is worse
        "availability": {"W": 100, "D": 50, "F": 0},
    },
    "deadlock": {
        "blocked_seconds": 3.0,           # cycle must persist this long
        "stuck": {"W": 2, "D": 4, "F": 6},  # processes unable to progress
    },
    "network": {
        "net_latency": {"W": 150, "D": 400, "F": 1000},
        "packet_loss": {"W": 2, "D": 10, "F": 30},
        "reachability": {"W": 95, "D": 80, "F": 50},   # lower is worse
    },
}

# baseline tolerance used to decide "recovery completed"
RECOVERY_TOLERANCE = {
    "cpu_above_baseline": 10.0,
    "mem_above_baseline": 6.0,
    "page_faults_max": 200.0,
    "io_queue_max": 1.5,
    "disk_latency_max": 8.0,
    "net_latency_max": 60.0,
    "packet_loss_max": 1.0,
}
