"""
Kernel Failure Lab — a CONCEPTUAL simulation of a kernel panic and the reboot that follows.

Nothing here touches the real kernel: no sysrq, no /proc/sysrq-trigger, no reboot command, no
module loading. The "kernel" is a state machine that drives the same simulated server the other
labs use (its process table, queues and lock table), so the effects are visible everywhere.

Lifecycle (one stage per row, durations are SIMULATED seconds):

    RUNNING ─trigger─▶ TRIGGERING ─▶ PANIC ─(kernel.panic = N s)─▶ [KDUMP] ─▶ FIRMWARE ─▶ BOOTLOADER
          ─▶ KERNEL_INIT ─▶ MOUNT_ROOT ─▶ INIT ─▶ SERVICES ─▶ POST_CHECK ─▶ RUNNING (RECOVERED / FAILED)
    kernel.panic = 0  →  HALTED (the machine stays frozen until a manual power cycle)

The scenarios are documented, typical causes — they are NOT about CPU or memory usage being high:
a kernel panic is the kernel deciding it cannot continue safely (bug, corrupted state, fatal
hardware error, PID 1 death, or a configured panic_on_* policy).
"""
import time

from .model import DT

PANIC_LEVEL, BOOT_LEVEL, CHECK_LEVEL = 4, 3, 2
STAGE_S = {"TRIGGERING": 1.5, "KDUMP": 4.0, "FIRMWARE": 3.0, "BOOTLOADER": 1.0, "KERNEL_INIT": 2.0,
           "MOUNT_ROOT": 2.0, "INIT": 1.0, "SERVICES": 2.0}
CHECK_WINDOW_S, CHECK_SAMPLES = 30.0, 3
BOOT_ORDER = ["FIRMWARE", "BOOTLOADER", "KERNEL_INIT", "MOUNT_ROOT", "INIT", "SERVICES"]
STAGE_TEXT = {
    "TRIGGERING": "the triggering condition occurs",
    "PANIC": "panic(): the kernel stops all CPUs and prints the panic message",
    "HALTED": "kernel.panic = 0 — the machine stays frozen until someone power-cycles it",
    "KDUMP": "kexec boots the capture kernel and saves the crash dump (vmcore)",
    "FIRMWARE": "firmware (UEFI/BIOS) power-on self test",
    "BOOTLOADER": "GRUB loads the kernel image and initramfs",
    "KERNEL_INIT": "the kernel initialises memory, CPUs, drivers",
    "MOUNT_ROOT": "initramfs mounts the root filesystem (ext4 journal replay)",
    "INIT": "PID 1 (systemd) starts",
    "SERVICES": "systemd starts database, web-master and workers",
    "POST_CHECK": "post-recovery health checks",
}

SCENARIOS = {
    "null_deref": {
        "name": "NULL pointer dereference in a network driver",
        "cause": "kernel bug", "trigger": "The NIC driver receives a malformed frame and follows a NULL pointer.",
        "lead": ["eth0: rx ring: malformed frame (len=0) on queue 3",
                 "BUG: kernel NULL pointer dereference, address: 0000000000000008",
                 "#PF: supervisor read access in kernel mode",
                 "Oops: 0000 [#1] SMP NOPTI",
                 "RIP: 0010:sim_nic_rx_poll+0x4c/0x1a0 [sim_nic]"],
        "panic": "Kernel panic - not syncing: Fatal exception in interrupt",
        "why": ["The fault happened inside the kernel, in interrupt context — there is no user process to kill.",
                "With panic_on_oops = 1 the kernel refuses to continue with possibly corrupted state."],
        "learn": "A bug in kernel code (here a driver) can stop the whole OS while CPU and memory look normal.",
    },
    "kill_init": {
        "name": "PID 1 (init / systemd) crashes",
        "cause": "critical process death", "trigger": "systemd (PID 1) receives SIGSEGV and exits.",
        "lead": ["systemd[1]: segfault at 0 ip 000055d1c0a1b2c3 sp 00007ffd error 4",
                 "systemd[1]: Caught <SEGV>, core dump failed"],
        "panic": "Kernel panic - not syncing: Attempted to kill init! exitcode=0x0000000b",
        "why": ["Every process descends from PID 1 and orphans are re-parented to it.",
                "The kernel cannot run without init, so its death is treated as fatal."],
        "learn": "Most process crashes are harmless to the OS — PID 1 is the special exception.",
    },
    "oom_panic": {
        "name": "Out of memory with vm.panic_on_oom = 1",
        "cause": "resource exhaustion + panic policy",
        "trigger": "A runaway allocation pushes the working set beyond RAM + swap.",
        "lead": ["mem-runaway invoked oom-killer: gfp_mask=0x140cca(GFP_HIGHUSER_MOVABLE|__GFP_COMP), order=0",
                 "Mem-Info: active_anon:3981204 inactive_anon:11 free:12544"],
        "panic": "Kernel panic - not syncing: Out of memory: system-wide panic_on_oom is enabled",
        "why": ["Memory ran out, so an allocation could not be satisfied.",
                "Only because vm.panic_on_oom = 1 does the kernel panic. With the default (0) the OOM killer "
                "kills one process and the OS keeps running — try it."],
        "learn": "Resource exhaustion causes a panic only when the system is configured to panic on it.",
        "pending": True,
    },
    "mce": {
        "name": "Uncorrectable memory error (machine check)",
        "cause": "hardware fault", "trigger": "A DIMM reports an uncorrectable ECC error in kernel memory.",
        "lead": ["mce: [Hardware Error]: CPU 2: Machine Check Exception: 4 Bank 7: be00000000800400",
                 "mce: [Hardware Error]: Uncorrected error in kernel context"],
        "panic": "Kernel panic - not syncing: Fatal machine check",
        "why": ["The hardware reported that data the kernel was using is corrupted and cannot be corrected.",
                "Continuing could silently corrupt files, so the kernel stops."],
        "learn": "Hardware can crash an OS. Rebooting restores service, but the faulty part should be replaced.",
    },
    "rootfs": {
        "name": "Root filesystem cannot be mounted (bad kernel update)",
        "cause": "boot-time failure",
        "trigger": "After an update the new kernel's initramfs lacks the NVMe storage driver; the server is rebooted.",
        "lead": ["systemd-logind: System is rebooting (kernel update 5.15.0-92)",
                 "reboot: Restarting system"],
        "panic": "Kernel panic - not syncing: VFS: Unable to mount root fs on unknown-block(0,0)",
        "why": ["The new kernel boots but cannot find the disk that holds /.",
                "Without a root filesystem there is no /sbin/init to start, so the kernel panics during boot."],
        "learn": "Rebooting into the SAME kernel repeats the panic (boot loop). Recovery needs a different "
                 "choice: boot the previous kernel from the GRUB menu.",
        "boot_panic": True,
    },
}
RECOVERY_MODES = {
    "auto": "Automatic reboot after kernel.panic seconds",
    "fallback": "Boot the previous known-good kernel (GRUB fallback)",
}


def _fmt_up(s):
    return f"[{s:12.6f}]"


class KernelSim:
    def __init__(self, eng):
        self.eng = eng
        self.opts = {"panic_timeout": 10, "panic_on_oops": True, "panic_on_oom": True, "kdump": False}
        self.reset()

    # ------------------------------------------------------------ state --
    def reset(self):
        self.stage = "RUNNING"
        self.scenario = None
        self.run = None              # the current / last experiment
        self.tainted = False
        self.bad_kernel = False      # rootfs scenario: the default boot entry is the broken kernel
        self.boot_id = 1
        self.uptime0 = time.time()

    @property
    def down(self):
        return self.stage not in ("RUNNING", "TRIGGERING", "POST_CHECK")

    @property
    def host_halted(self):
        """No telemetry: the monitoring agent runs on the crashed host until services start again."""
        return self.stage in ("PANIC", "HALTED", "KDUMP", "FIRMWARE", "BOOTLOADER", "KERNEL_INIT", "MOUNT_ROOT", "INIT")

    def level(self):
        if self.stage in ("PANIC", "HALTED", "KDUMP"):
            return PANIC_LEVEL
        if self.stage in BOOT_ORDER:
            return BOOT_LEVEL
        if self.stage == "POST_CHECK":
            return CHECK_LEVEL
        return 0

    def _t(self):
        return round((self.eng.model.tick - self.run["t0_tick"]) * DT, 1) if self.run else 0.0

    def _log(self, stage, text, kind="info"):
        if self.run is not None:
            self.run["timeline"].append({"t": self._t(), "stage": stage, "text": text, "kind": kind})

    def _console(self, line):
        if self.run is not None:
            up = time.time() - self.uptime0
            self.run["console"].append(f"{_fmt_up(up)} {line}")

    def _enter(self, stage):
        self.stage = stage
        self.stage_tick = self.eng.model.tick
        self._log(stage, STAGE_TEXT.get(stage, stage))

    # ---------------------------------------------------------- actions --
    def set_options(self, o):
        if self.stage != "RUNNING":
            raise ValueError("Change the kernel settings while the system is running")
        if "panic_timeout" in o:
            v = int(o["panic_timeout"])
            if not 0 <= v <= 60:
                raise ValueError("kernel.panic must be 0–60 s")
            self.opts["panic_timeout"] = v
        for k in ("panic_on_oops", "panic_on_oom", "kdump"):
            if k in o:
                self.opts[k] = bool(o[k])
        return dict(self.opts)

    def trigger(self, sid, recovery="auto"):
        if self.stage != "RUNNING":
            raise ValueError("A kernel experiment is already in progress — wait for it or reset the lab")
        sc = SCENARIOS.get(sid)
        if sc is None:
            raise ValueError("Unknown kernel failure scenario")
        if recovery not in RECOVERY_MODES:
            raise ValueError("Unknown recovery mode")
        e, M = self.eng, self.eng.model
        self.scenario = sid
        self.bad_kernel = False
        self.run = {"scenario": sid, "name": sc["name"], "cause": sc["cause"], "trigger": sc["trigger"],
                    "recovery": recovery, "options": dict(self.opts), "t0_tick": M.tick, "start_ts": time.time(),
                    "timeline": [], "console": [], "before_events": [ev["msg"] for ev in list(e.events)[-6:]],
                    "lost": [], "preserved": [], "unavailable": [], "checks": [], "outcome": None,
                    "panics": 0, "boot_attempts": 0, "oops_only": False, "oom_killed": None,
                    "before": {"health": e.display_health(), "processes": sum(1 for p in M.procs.values()
                                                                             if p["state"] not in ("TERMINATED",))}}
        self._log("RUNNING", "System healthy; experiment started", "ok")
        e.emit("KERNEL", f"[SIMULATED] Kernel failure experiment: {sc['name']}", "warn", marker="stress")
        e._input(f"Kernel experiment: {sc['name']}", "kernel")
        if sid == "oom_panic":
            self._enter("TRIGGERING")
            self.run["oom_prev"] = M.controls["mem_demand"]
            M.set_control("mem_demand", 85)
            self._log("TRIGGERING", "Runaway allocation: memory pressure 85% of RAM on top of the OS")
            return "Runaway memory allocation started"
        if sid == "rootfs":
            self.bad_kernel = True
            self._enter("TRIGGERING")
            for line in sc["lead"]:
                self._console(line)
            return "Rebooting into the updated kernel"
        self._enter("TRIGGERING")
        for line in sc["lead"]:
            self._console(line)
        return "Triggering condition injected"

    def power_cycle(self):
        if self.stage not in ("HALTED", "PANIC"):
            raise ValueError("Power-cycle is only needed while the kernel is panicked")
        self._log(self.stage, "Operator pressed reset (hard power cycle)", "warn")
        self.eng.emit("KERNEL", "[SIMULATED] Hard reset by the operator", "info")
        self._start_boot()
        return "Power cycle — the machine restarts"

    def choose_fallback(self):
        """During a boot loop the operator selects the previous kernel in the GRUB menu."""
        if self.run is None or self.stage not in ("PANIC", "HALTED", "RUNNING", "FIRMWARE", "BOOTLOADER") \
                or not self.bad_kernel:
            raise ValueError("Nothing to fall back from")
        self.run["recovery"] = "fallback"
        self._log(self.stage, "Operator selects 'Advanced options → previous kernel' in GRUB", "info")
        if self.stage in ("PANIC", "HALTED"):
            self._start_boot()
        return "The previous kernel will be booted"

    # ------------------------------------------------------------- crash --
    def _panic(self, text):
        e, M, sc = self.eng, self.eng.model, SCENARIOS[self.scenario]
        self.run["panics"] += 1
        self._console(text)
        self._console("CPU: 2 PID: 0 Comm: swapper/2 Tainted: G      D           5.15.0-91-generic")
        self._console("Kernel Offset: disabled")
        self._console(f"---[ end Kernel panic - not syncing: {text.split('not syncing: ')[-1]} ]---")
        self._enter("PANIC")
        self._log("PANIC", text, "fail")
        e.emit("KERNEL", f"[SIMULATED] {text}", "fail", marker="detect")
        if self.run["panics"] == 1 and not self.run["lost"]:
            self._record_loss(M)
        self._crash_model(M)
        if self.opts["panic_timeout"] == 0:
            self._enter("HALTED")
            e.emit("KERNEL", "[SIMULATED] kernel.panic = 0: the machine is frozen until it is power-cycled", "fail")
        else:
            self._console(f"Rebooting in {self.opts['panic_timeout']} seconds..")

    def _record_loss(self, M):
        procs = [p for p in M.procs.values() if p["state"] not in ("TERMINATED",)]
        held = sum(1 for r in M.locks.values() if r["alloc"])
        self.run["lost"] = [
            f"{len(procs)} running processes and everything in their memory (heap, stacks, open sockets)",
            f"{M.prev.get('inflight', 0):,.0f} in-flight client requests and {M.accept_backlog:,.0f} queued requests",
            f"CPU run queue ({M.q_cpu:,.1f} runnable tasks) and the disk I/O queue ({M.disk_backlog:,.0f} pending requests)",
            "the page cache and any dirty pages not yet written to disk",
            f"the lock table ({held} held lock(s)); open transactions are rolled back",
        ]
        self.run["preserved"] = [
            "data already written to disk (ext4 journal is replayed at mount)",
            "configuration files, installed software and the kernel command line",
            "this application's experiment history (the real SQLite database is not part of the simulation)",
        ] + (["the crash dump /var/crash/vmcore (kdump)"] if self.opts["kdump"] else
             ["nothing about the crash except what reached the console / serial log (kdump is off)"])
        self.run["unavailable"] = ["all client requests (connection refused / timeout)", "SSH and every service",
                                   "monitoring: the agent ran on the crashed host, so there is no telemetry",
                                   "every process — the scheduler is stopped"]

    def _crash_model(self, M):
        for p in M.procs.values():
            if p["state"] != "TERMINATED":
                p["state"], p["exit"], p["cpu"] = "TERMINATED", "kernel panic (CPU halted)", 0.0
            p["waiting_for"], p["req"], p["holds"] = None, None, []
            p["restart_at"] = None
        for r in M.locks.values():
            r["alloc"].clear()
            r["queue"].clear()
            r["holder"] = None
        M.halted = True
        M.accept_backlog = M.q_cpu = M.disk_backlog = M.io_lq = 0.0
        M.prev = {"error_frac": 0.0, "inflight": 0.0, "capacity": 1e-9}
        e = self.eng
        if e.runner:
            e._stop_runner("kernel panic")
        e.stress = None

    def _start_boot(self):
        self.run["boot_attempts"] += 1
        if self.opts["kdump"] and self.run["boot_attempts"] == 1 and self.stage == "PANIC":
            self._enter("KDUMP")
            return
        self._enter("FIRMWARE")
        self.eng.emit("KERNEL", f"[SIMULATED] Reboot #{self.run['boot_attempts']}: firmware POST", "info",
                      marker="recovery")

    # -------------------------------------------------------------- tick --
    def before_step(self):
        """Advance the lifecycle by one sample (called by the engine before model.step())."""
        if self.run is None or self.stage == "RUNNING":
            return
        e, M = self.eng, self.eng.model
        sc = SCENARIOS[self.scenario]
        el = (M.tick - self.stage_tick) * DT
        st = self.stage
        if st == "TRIGGERING":
            if sc.get("pending"):
                return                                   # oom: waits for the allocation to fail (after_step)
            if el >= STAGE_S["TRIGGERING"]:
                if self.scenario == "null_deref" and not self.opts["panic_on_oops"]:
                    return self._oops_only()
                if sc.get("boot_panic"):
                    self._panic_rootfs_first()
                else:
                    self._panic(sc["panic"])
        elif st == "PANIC":
            if el >= self.opts["panic_timeout"]:
                self._start_boot()
        elif st == "KDUMP":
            if el >= STAGE_S["KDUMP"]:
                self._log("KDUMP", "vmcore saved to /var/crash/ — it can be analysed later with the crash tool", "ok")
                self._enter("FIRMWARE")
        elif st in BOOT_ORDER:
            if el < STAGE_S[st]:
                return
            if st == "BOOTLOADER":
                use_bad = self.bad_kernel and self.run["recovery"] != "fallback"
                self._log("BOOTLOADER", "booting 5.15.0-92 (updated, broken initramfs)" if use_bad else
                          ("booting 5.15.0-91 (previous, known good)" if self.bad_kernel else "booting 5.15.0-91"))
            if st == "MOUNT_ROOT" and self.bad_kernel and self.run["recovery"] != "fallback":
                self._console("List of all partitions: (none)")
                self._panic(sc["panic"])
                self._log("PANIC", "Boot loop: the same broken kernel panics again. Choose the previous kernel.", "fail")
                if self.run["panics"] >= 3 and self.stage == "PANIC":
                    self._enter("HALTED")
                    self._log("HALTED", "Boot loop detected (3 panics). The server cannot recover by itself: "
                                        "boot the previous kernel or power-cycle.", "fail")
                    e.emit("KERNEL", "[SIMULATED] Boot loop: 3 consecutive panics — operator action needed", "fail")
                return
            if st == "MOUNT_ROOT":
                self._log("MOUNT_ROOT", "EXT4-fs (nvme0n1p2): recovery complete; mounted filesystem with ordered data mode", "ok")
            nxt = BOOT_ORDER.index(st) + 1
            if nxt < len(BOOT_ORDER):
                self._enter(BOOT_ORDER[nxt])
            else:
                self._services_up()
        elif st == "POST_CHECK":
            pass                                           # judged in after_step with fresh detector output

    def after_step(self, V, det):
        if self.run is None:
            return
        e, M = self.eng, self.eng.model
        if self.stage == "TRIGGERING" and self.scenario == "oom_panic":
            if (V.get("mem_ws") or 0) >= 100.0:
                if self.opts["panic_on_oom"]:
                    for line in SCENARIOS["oom_panic"]["lead"]:
                        self._console(line)
                    self._panic(SCENARIOS["oom_panic"]["panic"])
                else:
                    self._oom_kill(M)
            elif (M.tick - self.stage_tick) * DT > 40:
                self._finish("FAILED", "memory never ran out — no panic happened")
        elif self.stage == "POST_CHECK":
            v = self.run.setdefault("verify", {"ok": 0, "n": 0})
            v["n"] += 1
            R = det["rules"]
            ok = R["process"]["confirmed"] == 0 and R["service"]["confirmed"] == 0
            v["ok"] = v["ok"] + 1 if ok else 0
            if v["ok"] >= CHECK_SAMPLES or v["n"] * DT >= CHECK_WINDOW_S:
                crit = [p for p in M.procs.values() if p["critical"]]
                self.run["checks"] = [
                    {"ok": True, "label": "Kernel running", "detail": f"boot #{self.boot_id}, no panic since boot"},
                    {"ok": True, "label": "Root filesystem mounted read-write", "detail": "ext4 journal replayed"},
                    {"ok": all(p["state"] != "TERMINATED" for p in crit), "label": "PID 1 and critical services running",
                     "detail": ", ".join(f"{p['name']} {p['state']}" for p in crit)},
                    {"ok": R["service"]["confirmed"] == 0, "label": "Service responding",
                     "detail": f"error rate {V.get('error_rate', 0):.1f}%, response {V.get('response_ms', 0):.0f} ms"},
                    {"ok": True, "label": "Uptime reset", "detail": "new boot: all volatile state was rebuilt"},
                ]
                good = v["ok"] >= CHECK_SAMPLES and all(c["ok"] for c in self.run["checks"])
                self._finish("RECOVERED" if good else "FAILED",
                             "all post-boot checks passed in 3 consecutive samples" if good else "post-boot checks failed")

    # ----------------------------------------------------- special paths --
    def _oops_only(self):
        e, M = self.eng, self.eng.model
        self._console("Oops: 0000 [#1] SMP NOPTI — panic_on_oops = 0: killing the current task, kernel continues")
        victim = next((p for p in M.procs.values() if p["kind"] == "worker" and p["state"] != "TERMINATED"), None)
        if victim:
            M.kill(victim["pid"], "SIGKILL")
            e._drain_model_events()
        self.tainted = True
        self.run["oops_only"] = True
        self._log("RUNNING", f"Kernel oops (no panic): {victim['name'] if victim else 'the current task'} was killed; "
                             "kernel marked TAINTED (D)", "warn")
        e.emit("KERNEL", "[SIMULATED] Kernel oops without panic (panic_on_oops = 0): one task killed, kernel tainted", "warn")
        self.stage = "RUNNING"
        self.run["checks"] = [
            {"ok": True, "label": "Kernel still running", "detail": "no panic — panic_on_oops is 0"},
            {"ok": False, "label": "Kernel state trustworthy", "detail": "tainted (D): a reboot is still recommended"},
            {"ok": True, "label": "Other processes", "detail": "unaffected; restart the killed worker in the Process Lab"},
        ]
        self._finish("OOPS (NO PANIC)", "the oops killed one task; the OS kept running but is tainted")

    def _oom_kill(self, M):
        from . import recovery
        acts, _ = recovery.mem_kill_largest(M, {})
        self.eng._drain_model_events()
        self._console("Out of memory: Killed process (largest resident set) — " + acts[0].split("→ ")[-1])
        self.run["oom_killed"] = acts[0]
        self.stage = "RUNNING"
        self._log("RUNNING", "panic_on_oom = 0: the OOM killer killed one process; the kernel kept running", "warn")
        self.eng.emit("KERNEL", "[SIMULATED] OOM killer: " + acts[0] + " — no kernel panic (panic_on_oom = 0)", "warn")
        self.run["checks"] = [{"ok": True, "label": "Kernel still running", "detail": "OOM killer chose a victim"},
                              {"ok": True, "label": "Memory freed", "detail": acts[0]}]
        self._finish("OOM KILL (NO PANIC)", "the OOM killer sacrificed one process; no panic")

    def _panic_rootfs_first(self):
        e, M = self.eng, self.eng.model
        self._record_loss(M)
        self._crash_model(M)                 # the planned reboot itself stops every process cleanly
        self.run["lost"][0] = "every process — stopped cleanly by the planned reboot (not by the panic)"
        self._enter("FIRMWARE")
        self.run["boot_attempts"] += 1

    def _services_up(self):
        e, M = self.eng, self.eng.model
        keep = {k: M.controls[k] for k in M.controls}
        if self.scenario == "oom_panic":
            keep["mem_demand"] = self.run.get("oom_prev", 0)     # the runaway job is not started at boot
        M._build_procs()
        M.halted = False
        M.next_pid = 4001
        for k, v in keep.items():
            M.controls[k] = v
        M.eff["mem_demand"] = M.controls["mem_demand"]
        M.cycle_since = None
        if self.scenario == "rootfs" and self.run["recovery"] == "fallback":
            self._log("SERVICES", "Running the previous kernel. Fix the initramfs (update-initramfs) before the "
                                  "next update.", "info")
        self.boot_id += 1
        self.uptime0 = time.time()
        e.boot_ts = time.time()
        self._log("SERVICES", "database, web-master and 4 web-workers started (new boot)", "ok")
        self._enter("POST_CHECK")
        e.emit("KERNEL", "[SIMULATED] Boot complete — services started; running post-recovery checks", "info")

    def _finish(self, outcome, why):
        e = self.eng
        r = self.run
        r["outcome"], r["why_outcome"] = outcome, why
        r["duration_s"] = self._t()
        downs = [x for x in r["timeline"] if x["stage"] == "PANIC"]
        ups = [x for x in r["timeline"] if x["stage"] == "POST_CHECK"]
        r["downtime_s"] = round(ups[-1]["t"] - downs[0]["t"], 1) if downs and ups else None
        self._log("DONE", f"Outcome: {outcome} — {why}", "ok" if outcome == "RECOVERED" else "warn")
        if self.stage == "POST_CHECK":
            self.stage = "RUNNING"
        e.emit("KERNEL", f"[SIMULATED] Kernel experiment finished: {outcome} ({why})",
               "ok" if outcome == "RECOVERED" else "warn", marker="recovered" if outcome == "RECOVERED" else None)
        try:
            r["record_id"] = e.store.save_record(e.server_id, "kernel", f"Kernel lab · {r['name']}",
                                                 {"scenario": r["name"], "outcome": outcome, "panics": r["panics"],
                                                  "downtime_s": r["downtime_s"], "recovery": RECOVERY_MODES[r["recovery"]]},
                                                 self.public())
        except Exception:
            r["record_id"] = None

    # ------------------------------------------------------------ public --
    def public(self):
        r = self.run
        return {"stage": self.stage, "stage_text": STAGE_TEXT.get(self.stage, "normal operation"),
                "down": self.down, "halted": self.host_halted, "level": self.level(), "tainted": self.tainted,
                "options": dict(self.opts), "boot_id": self.boot_id, "scenario": self.scenario,
                "stage_elapsed": round((self.eng.model.tick - getattr(self, "stage_tick", 0)) * DT, 1)
                if self.stage != "RUNNING" else None,
                "bad_kernel": self.bad_kernel,
                "run": None if r is None else {k: v for k, v in r.items() if k not in ("t0_tick",)}}


def catalogue():
    return [{"id": k, **{x: v[x] for x in ("name", "cause", "trigger", "why", "learn")}} for k, v in SCENARIOS.items()]
