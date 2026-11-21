"""Load the prepared case-study subset for the web page (read-only; no simulator access)."""
import json
import os

from . import select as S

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CASES = os.path.join(ROOT, "data", "processed", "syzfix_cases.json")
META = os.path.join(ROOT, "data", "processed", "syzfix_cases.meta.json")
LOCAL = os.path.join(ROOT, "data", "local", "syzfix_local_text.json")

# Lab explanations — written for this project, NOT taken from the dataset. Shown with a "lab explanation" label.
EXPLAIN = {
    "lock": {"what": "The kernel's lock validator (lockdep) saw locks taken in an order that could deadlock, "
                     "or a lock used in an unsafe context.",
             "concept": "Coffman conditions; lock ordering as deadlock prevention.",
             "lab": ("/deadlock", "Deadlock Lab")},
    "hang": {"what": "A task or CPU made no progress for too long (hung task / RCU stall / soft lockup).",
             "concept": "A process can be alive but unresponsive — like the HUNG state in the Process Lab.",
             "lab": ("/processes", "Process Lab → Hang")},
    "leak": {"what": "kmemleak found kernel memory that was allocated but no longer referenced.",
             "concept": "Memory leaks grow the working set until memory pressure appears.",
             "lab": ("/processes", "Process Lab → Memory leak")},
    "null": {"what": "The kernel dereferenced a NULL or invalid pointer (general protection fault / page fault "
                     "in kernel mode).",
             "concept": "Why a bug in kernel code can stop the whole OS — compare the simulated driver-bug panic.",
             "lab": ("/kernel", "Kernel Failure Lab → NULL pointer dereference")},
    "memsafety": {"what": "KASAN/KFENCE caught a use-after-free or out-of-bounds access to kernel memory.",
                  "concept": "Kernel memory safety; object lifetime and reference counting.",
                  "lab": ("/kernel", "Kernel Failure Lab")},
    "uninit": {"what": "KMSAN caught the kernel using memory before it was initialised.",
               "concept": "Uninitialised data can leak information or steer control flow.",
               "lab": None},
    "assert": {"what": "A kernel assertion (WARN_ON / BUG_ON) fired because an invariant did not hold.",
               "concept": "Kernel invariants; with panic_on_warn a WARNING becomes a panic.",
               "lab": ("/kernel", "Kernel Failure Lab → kernel settings")},
}
# simulated Kernel Lab scenario -> real-case category of the same kind (None = no honest match)
KERNEL_SCENARIO_CATEGORY = {"null_deref": "null", "kill_init": None, "oom_panic": None, "mce": None, "rootfs": None}


def load():
    """Return dict(available, cases, meta, local_available, errors). Invalid cases are dropped and reported."""
    if not os.path.isfile(CASES):
        return {"available": False, "cases": [], "meta": None, "local_available": False, "errors": []}
    with open(CASES, encoding="utf-8") as f:
        raw = json.load(f)
    meta = None
    if os.path.isfile(META):
        with open(META, encoding="utf-8") as f:
            meta = json.load(f)
    cases, errors = [], []
    for c in raw:
        e = S.validate_case(c)
        (errors.append({"bug_id": c.get("bug_id"), "errors": e}) if e else cases.append(c))
    local = {}
    if os.path.isfile(LOCAL):
        try:
            with open(LOCAL, encoding="utf-8") as f:
                local = (json.load(f) or {}).get("cases") or {}
        except (OSError, ValueError):
            local = {}
    for c in cases:
        c["local_text"] = local.get(c["bug_id"])
    return {"available": True, "cases": cases, "meta": meta, "local_available": bool(local), "errors": errors,
            "explain": EXPLAIN, "category_names": S.CATEGORY_NAMES,
            "category_order": [c for c, _, _ in S.CATEGORIES]}   # JSON objects lose order; keep it explicitly
