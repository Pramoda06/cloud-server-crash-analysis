# Final Technical Verification Report

> Historical record of the 12 Oct 2026 checkpoint (27 tests). For the current status after the lab upgrade see [UPGRADE_REPORT.md](UPGRADE_REPORT.md).
## Cloud Server Crash Analysis: An Operating System Perspective

- **Date of Final Verification:** October 12, 2026
- **Project Root:** `E:\OS-case study`
- **Baseline Git Commit:** `aa24deddfd874ec60a65b84606eda773d2b7a8ee`
- **Python Runtime:** Python 3.14 (x86_64) on Windows
- **Overall Verdict:** **DEMO-READY** (All 27 automated tests passing, 8 end-to-end workflows verified, database integrity invariant confirmed)

---

## 1. Automated Test Execution Evidence

### Command Executed:
```powershell
python -m unittest discover -s tests -p "test_*.py" -v
```

### Actual Terminal Execution Output:
```text
test_api_lab_controls_finite_clamping (test_api_multiserver.TestApiAndMultiServerIsolation.test_api_lab_controls_finite_clamping)
POST /api/lab/controls clamps out-of-range finite floats to valid min/max. ... ok
test_api_lab_controls_rejection_of_invalid_inputs (test_api_multiserver.TestApiAndMultiServerIsolation.test_api_lab_controls_rejection_of_invalid_inputs)
POST /api/lab/controls returns HTTP 400 for non-finite values, non-numeric strings, and bad payloads. ... ok
test_api_lab_controls_valid (test_api_multiserver.TestApiAndMultiServerIsolation.test_api_lab_controls_valid)
POST /api/lab/controls modifies simulated control parameters within bounds. ... ok
test_api_lab_state (test_api_multiserver.TestApiAndMultiServerIsolation.test_api_lab_state)
GET /api/lab/state returns complete telemetry snapshot. ... ok
test_api_servers_list (test_api_multiserver.TestApiAndMultiServerIsolation.test_api_servers_list)
GET /api/servers returns valid JSON dict with list of servers. ... ok
test_database_isolation_invariant (test_api_multiserver.TestApiAndMultiServerIsolation.test_database_isolation_invariant)
Verify tests run against the isolated test store rather than the production database. ... ok
test_multi_server_isolation (test_api_multiserver.TestApiAndMultiServerIsolation.test_multi_server_isolation)
State mutations on Server 1 must not alter the state of Server 2. ... ok
test_baseline_health (test_engine_recovery.TestEngineRecovery.test_baseline_health)
Under baseline controls, engine health is 0 (HEALTHY) and no active episode. ... ok
test_engine_reset_clears_all_episodes (test_engine_recovery.TestEngineRecovery.test_engine_reset_clears_all_episodes)
Reset clears active episodes, historical episodes, and restores baseline health. ... ok
test_new_incident_after_reset_operates_cleanly (test_engine_recovery.TestEngineRecovery.test_new_incident_after_reset_operates_cleanly)
After reset, a new incident creates a clean episode and new recovery gets next unique sequence ID. ... ok
test_recovery_idempotency (test_engine_recovery.TestEngineRecovery.test_recovery_idempotency)
Calling recover() while already RECOVERING does not allocate new sequence or reset requested_ts. ... ok
test_reset_after_recovered_clears_recovery_state (test_engine_recovery.TestEngineRecovery.test_reset_after_recovered_clears_recovery_state)
Reset after recovery completes cleans up last_recovery so subsequent sessions start fresh. ... ok
test_reset_during_recovering_clears_recovery_and_prevents_leak (test_engine_recovery.TestEngineRecovery.test_reset_during_recovering_clears_recovery_and_prevents_leak)
Reset while recovery is in-flight cancels recovery and leaks no timers or escalation. ... ok
test_three_sample_recovery_confirmation (test_engine_recovery.TestEngineRecovery.test_three_sample_recovery_confirmation)
Recovery requires 3 consecutive healthy ticks before confirming RECOVERED status. ... ok
test_two_sample_detection_debouncing_and_episode (test_engine_recovery.TestEngineRecovery.test_two_sample_detection_debouncing_and_episode)
Failure threshold requires 2 consecutive samples to confirm before opening an episode. ... ok
test_disk_queue_backlog_bounding (test_memory_disk.TestMemoryAndDiskModels.test_disk_queue_backlog_bounding)
Verify NVMe M/M/c multi-channel queueing and the 30-second backlog ceiling. ... ok
test_memory_saturation_invariants (test_memory_disk.TestMemoryAndDiskModels.test_memory_saturation_invariants)
Ensure memory invariants hold under extreme memory pressure. ... ok
test_memory_working_set_threshold_continuity (test_memory_disk.TestMemoryAndDiskModels.test_memory_working_set_threshold_continuity)
Verify the 80% working-set thrashing threshold and superlinear scaling. ... ok
test_control_input_validation_and_clamping (test_network.TestNetworkModel.test_control_input_validation_and_clamping)
SystemModel.set_control validates finite inputs, clamps out-of-bound floats, and rejects non-finite/invalid values. ... ok
test_network_loss_spectrum (test_network.TestNetworkModel.test_network_loss_spectrum)
Verify delivery, give-up probability, retransmission, and response time across the loss spectrum. ... ok
test_packet_loss_100_percent_boundary (test_network.TestNetworkModel.test_packet_loss_100_percent_boundary)
At 100% packet loss, conditional retrans delay is unavailable and client latency is timeout_ms. ... ok
test_zero_packet_loss_baseline (test_network.TestNetworkModel.test_zero_packet_loss_baseline)
At 0% added loss, retrans_ms is near zero and response_ms equals normal service + RTT. ... ok
test_custom_process_create_and_kill (test_processes_deadlock.TestProcessAndDeadlockModel.test_custom_process_create_and_kill)
Verify creating custom process and terminating it reaps properly. ... ok
test_deadlock_formation_cycle_and_preemption (test_processes_deadlock.TestProcessAndDeadlockModel.test_deadlock_formation_cycle_and_preemption)
Verify circular wait deadlock graph formation, cycle detection, and breaking via preemption. ... ok
test_kill_master_cascades_to_workers (test_processes_deadlock.TestProcessAndDeadlockModel.test_kill_master_cascades_to_workers)
Killing web-master must terminate all child worker processes. ... ok
test_process_table_initialization (test_processes_deadlock.TestProcessAndDeadlockModel.test_process_table_initialization)
Verify standard baseline process table and parent-child associations. ... ok
test_restart_process_transitions_and_increments_pid (test_processes_deadlock.TestProcessAndDeadlockModel.test_restart_process_transitions_and_increments_pid)
Restarting a terminated master enters RESTARTING, then advances to RUNNING with new PID. ... ok

----------------------------------------------------------------------
Ran 27 tests in 0.389s

OK
```

- **Exit Code:** `0`
- **Total Tests Run:** `27`
- **Failures:** `0`
- **Errors:** `0`
- **Skipped:** `0`

---

## 2. Production Database Integrity Verification

The production database is located at `E:\OS-case study\database\experiments.db`.

To guarantee that automated tests and verification suites operate with complete data safety and never mutate user records:
- **Pre-Test SHA-256 Hash:** `fa2e7a528ca163c94d8a092e1510baafbcf821c88fffa98871adaa0ffb73254e`
- **Post-Test SHA-256 Hash:** `fa2e7a528ca163c94d8a092e1510baafbcf821c88fffa98871adaa0ffb73254e`
- **Verification Invariant:** The hashes match bit-for-bit. Integration tests execute against an isolated temporary database created in a `tempfile.TemporaryDirectory()`, injected cleanly via `configure_storage()`.

---

## 3. End-to-End Application Workflow Results

All 8 application workflows were executed programmatically against the real Flask routes, live `LabEngine`, and `SystemModel`:

| Workflow | Description | Observed Evidence | Result |
|---|---|---|---|
| **WF 1: Normal Operation** | Baseline telemetry without stress | Health = `HEALTHY`, availability > 99%, active episode = `None`. | **PASS** |
| **WF 2: CPU Overload** | CPU workload set to 150% | CPU util > 85%, run queue elevated, CPU detector rule confirmed $\ge 2$, RCA generated, recovery restored baseline in 3-sample streak. | **PASS** |
| **WF 3: Memory Pressure** | Memory demand set to 90% | Memory util > 80%, page faults > 500/s (thrashing explosion), memory rule confirmed, recovery restored healthy memory. | **PASS** |
| **WF 4: Disk Saturation** | Disk write set to 8000 IOPS | Disk util > 95%, I/O queue accumulation, backlog bounded, recovery drained backlog to healthy state. | **PASS** |
| **WF 5: Network Loss Boundary** | 50% loss then 100% partition | 50% loss increased latency; 100% loss resulted in `retrans_ms = None`, response clamped to 2000 ms timeout, 0% availability, 100% error rate. | **PASS** |
| **WF 6: Process Cascade & Recovery** | `SIGKILL` sent to `web-master` | Cascaded via `SIGHUP` to terminate workers (`workers_alive = 0`); recovery restarted master and workers (`RUNNING`). | **PASS** |
| **WF 7: Reset During Recovery** | Reset invoked while `RECOVERING` | In-flight recovery canceled immediately, `last_recovery` cleared to `None`, no timers or escalation actions leaked into subsequent ticks. | **PASS** |
| **WF 8: Multi-Server Isolation** | Server 1 mutated to 120% CPU | Server 2 telemetry observed; Server 2 controls and metrics remained completely unmutated at baseline. | **PASS** |

---

## 4. Remediation Log of Implemented Changes

1. **Recovery & Reset Lifecycle (`simulator/lab/engine.py`):**
   - Resetting via `_reset_state()` now explicitly clears `self.last_recovery = None` and `self.last_run_id = None`.
   - Prevents stale timers, second-stage escalation web service restarts, or ghost recovery verifications from surviving a reset.
2. **Database Test Isolation (`app.py`, `tests/test_api_multiserver.py`):**
   - Implemented `configure_storage(new_store, new_manager)` in `app.py` and supported `OS_EXPERIMENTS_DB` environment variable.
   - Refactored `test_api_multiserver.py` to use a `tempfile.TemporaryDirectory()` store fixture, leaving production `experiments.db` untouched.
3. **Controls Input Validation (`simulator/lab/model.py`, `simulator/lab/engine.py`, `app.py`):**
   - `SystemModel.set_control()` explicitly rejects booleans (`TypeError`) and non-finite floats (`ValueError`).
   - `LabEngine.set_controls()` validates dictionary payloads, checks `disk_io` finite numbers, rejects booleans, non-numeric strings, and non-finite numbers with HTTP 400.
   - Clamps valid finite out-of-range floats to min/max bounds as designed.
4. **Windows Console Encoding Safety (`simulator/lab/engine.py`, `app.py`):**
   - Console `log.info` replaces `\u2192` (`→`) with ASCII `->` specifically for console output, while preserving the Unicode character for the browser web UI.
   - Startup banner uses ASCII hyphen instead of em-dash `\u2014`.
5. **Queueing Model Documentation Accuracy (`README.md`):**
   - Correctly distinguishes multi-core CPU Sakasegawa $M/M/c$ queueing from single-device disk $M/M/1$ steady-state queueing with Little's-Law-scaled parallel backlog channels.

---

## 5. Scope Boundaries & Academic Limitations

1. **Educational Simulation:** The system is an educational continuous-time simulation designed for teaching and demonstration. It does not invoke actual Linux kernel crash dumps (`kdump`) or hardware register faults.
2. **Simplified Network Model:** Simulates client-server RTT and exponential backoff retransmission delays; does not simulate full TCP BGP dynamic routing or congestion window (CWND) packet streams.
3. **Deterministic Seed Behavior:** When the host load anchor is disabled, simulation runs are sample-for-sample reproducible across platforms.

