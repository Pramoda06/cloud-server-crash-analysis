# Kernel Failure Lab

## What it is

A **conceptual simulation** of a kernel panic and the reboot that follows
(`simulator/lab/kernel.py`). It drives the same simulated server as the other labs, so the panic
is visible everywhere: the process table empties, the service fails, monitoring goes blank, and
the Incident Analysis names *Kernel panic* as the primary cause.

It never touches the real kernel — no `sysrq`, no `/proc/sysrq-trigger`, no `reboot`, no module
loading, no `subprocess`. `tests/test_kernel_lab.py` scans the simulator source to keep it that way.

## Lifecycle

```
RUNNING → TRIGGERING → PANIC ─(kernel.panic = N s)→ [KDUMP] → FIRMWARE → BOOTLOADER → KERNEL_INIT
        → MOUNT_ROOT → INIT → SERVICES → POST_CHECK → RUNNING      (kernel.panic = 0 → HALTED until power-cycle)
```

| Stage | Simulated duration | What the model does |
|---|---|---|
| PANIC | `kernel.panic` seconds (default 10) | every process → TERMINATED, queues, locks, in-flight requests dropped; host metrics become “no telemetry” |
| KDUMP (optional) | 4 s | capture kernel saves a vmcore (listed as preserved) |
| FIRMWARE → INIT | 3 + 1 + 2 + 2 + 1 s | boot stages; the root filesystem journal is replayed at MOUNT_ROOT |
| SERVICES | 2 s | a fresh process table is created (new boot) |
| POST_CHECK | until 3 healthy samples (≤ 30 s) | kernel running, root fs mounted, critical services, service responding, uptime reset |

Durations are illustrative simulated seconds, not hardware measurements.

## Scenarios

| Scenario | Cause | Teaches |
|---|---|---|
| NULL pointer dereference in a network driver | kernel bug | a panic can happen with normal CPU and memory; with `panic_on_oops = 0` only one task dies and the kernel is *tainted* |
| PID 1 crashes | critical process death | ordinary process crashes are harmless to the OS — init is the exception |
| Out of memory with `vm.panic_on_oom = 1` | resource exhaustion + policy | exhaustion panics only because of the policy; with 0 the (simplified) OOM killer kills one process |
| Uncorrectable memory error (machine check) | hardware fault | hardware can stop the OS; reboot restores service but the part should be replaced |
| Root filesystem cannot be mounted | boot-time failure | rebooting the same broken kernel loops; recovery = boot the previous kernel |

## Lost vs preserved

Lost: all processes and their memory, in-flight and queued requests, run queue and I/O queue, page
cache and unwritten dirty pages, the lock table (open transactions rolled back).
Preserved: data already on disk (journal replay), configuration, this application's real database,
and the crash dump if kdump is enabled.

## Studying a real panic safely (optional, not part of the app)

If a teacher wants to see a real panic, do it **only in a disposable virtual machine** that holds
no data you care about (for example a fresh Ubuntu VM in VirtualBox/QEMU with a snapshot taken
first). The usual approach is the magic SysRq crash trigger, run as root inside that VM; the VM
console then shows the panic and, if `kernel.panic` is set, the VM reboots. Never run it on the
host, in WSL, or on any shared machine. Restore the VM snapshot afterwards. The application itself
does not and will not include such a trigger.
