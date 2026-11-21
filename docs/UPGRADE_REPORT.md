# OS Failure & Recovery Laboratory — upgrade report

Last verified: **15 Oct 2026, final verification pass + resumed fixes.** Colour palette unchanged. No dataset downloaded or integrated.

## 1. Project folder state
* Your folder and the working copy were compared file by file before this pass: 73 source/doc files,
  identical combined checksum. No newer work of yours was overwritten.
* Backup of the original project: `_original_backup_2026-10-15.tar.gz` (folder root).
* Temporary upload archives are in `_to_delete/` (deletion is blocked in the linked folder; delete it yourself).

## 2. Tests — exact results

| Where | Command | Passed | Failed | Skipped |
|---|---|---|---|---|
| Cloud workspace, Python 3.13, Playwright + Chromium | `python -m pytest -q` | **89** (+7 subtests) | 0 | 0 |
| Your computer's Linux VM, Python 3.10, temp database (after all fixes were copied there) | `python -m pytest -q` | **81** (+7 subtests) | 0 | **8** browser tests — Chromium download blocked by the network allowlist |

Browser tests therefore ran **only** in the cloud workspace, not on your computer. They use a temporary
database; the real `database/experiments.db` is never touched by tests.

Breakdown: 27 original · 17 deadlock · 11 recovery · 9 kernel · 7 lab APIs · 11 verification regressions
(defects + closed gaps) · 8 browser.

The 4 defect-specific regression tests were run against the code from **before** the verification fixes:
all 4 failed there and pass now, so they really detect the defects.

### Automated end-to-end paths (engine level, every failure type)
trigger → OS-level effect → detected primary → simulated causal chain → strategy → verified state, +10 s later:

| Failure (trigger) | OS-level effect observed | Primary detected | Strategy → outcome | 10 s later |
|---|---|---|---|---|
| CPU stress 90 % | run queue 0 → 362, CPU wait 4.5 s | CPU saturation | reduce workload → RECOVERED · kill batch jobs → RECOVERED · renice → **FAILED** (correct: overload remains) | HEALTHY / stays FAILED for renice |
| Memory stress 90 % | page faults 35 → 11 130 /s, paging 73 % CPU | Memory thrashing (CPU, service secondary) | release memory → RECOVERED · stop largest user → RECOVERED | HEALTHY |
| Memory-leak process | working set grows 120 MB/s | Memory | restart leaking process → RECOVERED (leak will recur — stated) | HEALTHY |
| Disk stress 90 % | I/O queue 723, latency 1.7 s | Disk I/O bottleneck | reduce I/O → RECOVERED · drain queue only → **FAILED** (correct: queue refills) | HEALTHY / FAILED |
| Network stress 85 % | RTT 777 ms, loss 30 %, retransmit 2.1 s | Network degradation | restore → RECOVERED · raise timeout → **FAILED** (network still bad) | HEALTHY / FAILED |
| Traffic ×3.7 | backlog full (1 024), errors 66 % | Service overload | shed load → RECOVERED · restart service → **FAILED** (backlog rebuilds) | HEALTHY / FAILED |
| Worker crash | capacity −25 % | Critical process failure | restart → RECOVERED | HEALTHY |
| web-master crash | capacity 0, errors 100 % | Critical process failure | restart → RECOVERED | HEALTHY |
| 2- and 3-process deadlock | lock wait 700 ms, 35 % errors | Process deadlock | terminate victim → RECOVERED · pre-empt → RECOVERED | HEALTHY |
| Kernel panic (5 scenarios) | all processes stopped, availability 0, no telemetry | Kernel panic | reboot lifecycle → RECOVERED; boot loop needs previous kernel; kernel.panic=0 needs power-cycle | HEALTHY |

Every failure type has at least one recovery path that the detector confirms. The FAILED outcomes
above are intended teaching results, not defects.

### UI audit (automated, Playwright)
10 pages × 3 widths (1440 / 1024 / 390 px) × 4 states (idle, failing, after recovery, kernel panic) =
**120 checks: 0 horizontal overflow, 0 overlapping controls, 0 JavaScript errors.**
Screenshots were reviewed for stale alerts: after a verified recovery, the Overview shows *No active
incidents*, the episode is closed, and the Recovery Analysis shows before → after values that match the live state.

### Manual flows checked (screenshots)
System Lab (control, stress, help toggle), Process Lab (grouped table, leak, tabs), Deadlock Lab (custom
processes/resources, 12-process graph, detect, victim, pre-empt, ordering, Banker's), Kernel Failure Lab
(panic → reboot → checks, oops without panic, OOM without panic, boot loop + fallback, power-cycle),
Recovery Lab (apply, verify, compare, start clean), Live Monitoring (charts, changed controls, timeline),
Incident Analysis, Experiment History.

## 4. Defects found in this pass and fixed

| # | Severity | Defect | Fix | Test |
|---|---|---|---|---|
| 1 | Critical (misleading explanation) | The causal chain was traced at the episode's first sample, before the primary cause was known; for a memory leak or a single worker crash it "explained" the failure with network RTT noise | The worst-moment state is kept and the chain is re-traced from the primary's own metric once attribution finds it | `test_memory_leak_chain_points_at_memory`, `test_worker_crash_chain_points_at_capacity` |
| 2 | Important (misleading contribution) | With no live workers, backlog wait was attributed to the request backlog (which was 0) — the chain ended "Request backlog has not changed" | The contribution now points to service capacity = 0 (no live workers) | `test_master_crash_blames_capacity_not_backlog` |
| 3 | Important (inconsistent state) | Processes could be created, restarted or take locks while the simulated kernel was panicked | Process/lock actions and process/deadlock stress are refused while the kernel is down | `test_no_process_or_lock_changes_while_panicked` |
| 4 | Minor | Recovery Lab offered strategies that cannot apply (e.g. restart leaking process with no leak) and failed only on click | Shown disabled with the reason ("Not applicable now: …") | browser test |
| 5 | Minor | Kernel Lab highlighted the first scenario card while another scenario was running | Selection follows the running experiment | browser test |
| 6 | Minor (docs) | DEMO_GUIDE had stale facts (8 GB RAM, 25 % CPU default, PID 1000, "Operator Recovery", "Reset Server", "Failure Analysis"); VIVA_GUIDE said "6 detector rules" | Corrected; DEMO_GUIDE Steps 9–12 added for the new labs; FINAL_VERIFICATION_REPORT marked as the 12 Oct historical record | — |
| 7 | Minor (wording) | Deadlock Lab said a waiting holder "can finish and release", but simulated transactions never finish by themselves | Text now says to press Release on the holder | — |
| 8 | Minor (wording) | README called simulator outputs "measured results" | "results observed in the simulator" | — |

## 5. Remaining gaps (not fixed — prioritised)

**Critical correctness:** none known.

**Important:**
* *(Closed 16 Oct — concurrent causes are now reported together; all 15 pairs of injected failures were checked: Service degradation is secondary in every pair.)* **Attribution with simultaneous independent causes:** if two failures are triggered at the same moment (e.g. memory stress and a deadlock together), *Service degradation* can be labelled primary as well, because attribution is frozen when each rule is first detected. Single-cause scenarios are attributed correctly. Changing this would alter the existing detection design, so it was left and documented.
* *(Closed 16 Oct — modelled, see section 11.)* ~~Not modelled: disk free-space exhaustion.~~

**Closed after the verification (resumed fixes):**
* **Hung / unresponsive process** is now modelled: *Hang* in the Process Lab makes a process alive-but-useless (state HUNG, keeps its memory, serves nothing). The detector counts it like a failed process; plain *Restart failed processes* does not apply (it never exited); the new strategy **Terminate & restart an unresponsive process** fixes it; *Full recovery* now kills hung processes before restarting. Comparison scenario: *web-master hangs*.
* **Transactions finish on their own** (Deadlock Lab, optional, off by default): a transaction that holds resources and is not waiting releases them after 5 s, so *Cycle, but no deadlock* resolves visibly while a real deadlock stays deadlocked.
* **Model settings switches** in the System Lab (Advanced): cross-subsystem cascades, and the optional host anchor (psutil, read-only; values become HYBRID).
* The failure-types table scrolls sideways on phones instead of cramming.

**Minor polish:**
* *(Closed 16 Oct — hysteresis added, see section 11.)* No hysteresis in detection: a metric sitting exactly on a threshold (CPU ≈ 75 %) flaps between WARNING and HEALTHY, filling the timeline. Realistic, but noisy.
* Final light colour palette — your decision after reviewing the interface.

## 6. Technical accuracy statement
* Everything shown is **simulated or derived** from the simulation model. Optional real host telemetry
  (psutil) only affects the model when the anchor setting is on (off by default). See DATA_SOURCES.md.
* The Kernel Failure Lab is a labelled **conceptual simulation**. The app never causes a real kernel panic,
  reboot or host slowdown; a test scans the source for host-level crash commands.
* Durations are simulated seconds, not production measurements. No real production crash data is used.
* **No external dataset** is used.

## 7. Dataset recommendation (superseded — see section 11)
| Candidate | Purpose | Verdict |
|---|---|---|
| SyzFix | Library of real Linux kernel bugs, crash reports and fixes | **Recommended**: a small curated subset (≈40–80 bugs) as read-only case studies in the Kernel Failure Lab, labelled as fuzzer-found bugs (not production crash rates, no telemetry) |
| Zenodo DataCenter-Traces | Processed Alibaba workload traces for realistic CPU/memory load | Optional, low priority: data-centre averages are smooth and rarely cause failures without artificial scaling |
| Loghub | Historical system-log analysis | Not now: it would be a separate new feature |

Datasets stay separate; nothing is downloaded until you approve. Details: DATASETS.md.

## 8. How to run
```
pip install -r requirements.txt
python app.py                      # http://127.0.0.1:5000   (or run.bat / run.sh)
python -m pytest -q                # or: python -m unittest discover -s tests -p "test_*.py"
# browser tests (optional): pip install playwright && python -m playwright install chromium
```

## 9. Key files
New: `simulator/lab/rag.py`, `recovery.py`, `kernel.py`; `templates/deadlock.html`, `recovery.html`,
`kernel.html`; tests `test_deadlock_lab.py`, `test_recovery_strategies.py`, `test_kernel_lab.py`,
`test_api_labs.py`, `test_ui_browser.py`, `test_audit_regressions.py`; docs `LAB_GUIDE.md`,
`KERNEL_LAB.md`, `DATA_SOURCES.md`, `DATASETS.md`, this report. Modified: `model.py`, `engine.py`,
`detect.py`, `rca.py`, `storage.py` (additive `lab_records` table), `app.py`, `lab-core.js`,
`lab-report.js`, `style.css`, page templates, README and the existing guides. Removed: nothing.

## Real Kernel Bug Case Studies (SyzFix) — status before the data arrived (superseded by section 11)

* New, separate reference page `/cases` (nav → Reference → Real Kernel Bugs), tagged **HISTORICAL DATASET**.
  `simulator/` does not import it; simulation, thresholds, recovery and palette unchanged.
* Licence review per field, two-tier storage (committed metadata + links + short GPL-2.0 code excerpts;
  crash-report and commit-message text local-only and git-ignored), deterministic selection without
  quotas: [SYZFIX.md](SYZFIX.md).
* Defect found by the new tests and fixed before any real data was processed: the patch parser counted
  the format-patch `-- ` signature as a removed line; counts now follow the `@@` hunk lengths.
* Tests: `tests/test_casestudies.py` (14, synthetic fixtures only). Full suite: container 103 passed
  (incl. browser); this computer (Python 3.10) 95 passed, 8 skipped (browser tests, Chromium not installed).
* **No real cases yet**: the shard `train-00000-of-00011.parquet` has not been downloaded. Until it is,
  `/cases` shows setup instructions.

## 11. Final completion pass (16 Oct 2026)

### Completed and verified
* **SyzFix shard validated**: `train-00000-of-00011.parquet`, 54,077,962 bytes, SHA-256 `a98ac4c4…50af8` —
  identical to the value Hugging Face publishes; 656 rows; all 14 columns. The prepare script now refuses
  any file whose checksum differs.
* **Real Kernel Bugs library built**: 42 cases, 6 in each of 7 categories (caps, not quotas). Not
  eligible: 126 no fix commit, 137 empty patch, 32 short message, 22 > 3 files, 10 > 60 lines; 21 eligible
  but unclassified; 12 ambiguous titles. Details: [SYZFIX.md](SYZFIX.md). Output is byte-identical on
  Windows/Python 3.10 and Linux/Python 3.13.
* **Data-quality fixes found on the real output**: UBSAN shift-out-of-bounds was counted as memory safety;
  lockdep "locking bug" was counted as an assertion; reporting frames leaked into call traces; struct
  declarations were listed as functions; 40-hex syzbot ids got broken links; duplicate fix commits;
  NULL-category cases now state the address kind reported.
* **Test database isolation (defect)**: `app.py` opens its database on import, and the tests swapped in a
  temporary store only afterwards, so every test run opened `database/experiments.db` (that is how the
  leftover `experiments.db-journal` appeared on the Windows copy). Now every test module imports
  `tests/_isolation.py` first; verified for `pytest`, `unittest discover -s tests` and `-t .`, and a test
  asserts it. The database and journal were **not** deleted; their content was verified unchanged
  (7 experiments, 2 servers, integrity ok).
* **Concurrent root causes (defect)**: two independent failures injected together were presented as
  "primary + other", ranked by a fixed display order. Independent causes that reach failure within 2 s are
  now shown together as *concurrent*, with an explanation. Backwards compatible (`primary` field kept).
* **Alert hysteresis (defect)**: CPU held at ~75 % produced 62 alerts and 26 failure episodes in 5 simulated
  minutes. Levels now clear 5 % below their trigger threshold; the trigger itself is unchanged (a test
  shows the raise happens at the identical sample). State-based rules are unaffected.
* **Disk space exhaustion (new, additive)**: file-system capacity separate from I/O speed; *Log flood*
  process and *Disk space exhaustion* stress; ENOSPC errors without latency; Linux open-file semantics;
  new detection rule; three strategies (two must FAIL, one RECOVERS); comparison scenario; UI in the System,
  Process, Recovery and Analysis pages. Verified **bit-identical** outputs (all values, contributions and
  detector levels) for the 10 existing scenarios × 120 samples with no log flood running.
* **Smaller fixes**: Overview / Analysis labelled I/O busy time "Disk usage" (now "Disk I/O busy");
  the Analysis page overflowed horizontally at 390 px after a recovery (pre-existing; fixed with
  `min-width: 0` grid columns); Kill vs Crash vs Hang explained in the Process Lab; `.gitignore` now also
  ignores SQLite journals and `_to_delete/`.
* **Verified, already working**: hang vs kill vs crash, hung-process recovery, optional transaction
  completion, deadlocks stay blocked until recovered, operations refused during a simulated panic,
  reboot/recovery consistency, inapplicable strategies disabled with reasons, failed strategies never
  report RECOVERED, recovery clears alerts but keeps history, Start clean.

### Tests (final runs)
| Environment | Command | Passed | Failed | Skipped |
|---|---|---|---|---|
| Cloud workspace, Linux, Python 3.13, Playwright + Chromium | `python -m pytest tests` | 132 | 0 | 0 |
| same | `python -m unittest discover -s tests -p "test_*.py"` | 132 | 0 | 0 |
| Your project folder (E:\OS-case study) via the Linux VM, Python 3.10 | `python -m pytest tests` | 124 | 0 | 8 |
| same | `python -m unittest discover -s tests -p "test_*.py"` | 124 (incl. skipped) | 0 | 1 class (8 tests) |

The 8 skips are the browser tests: Chromium for Playwright is not installed in that environment
(`python -m playwright install chromium` enables them). They passed in the cloud workspace.
UI audit (Playwright, cloud workspace): 12 pages × 5 states (idle, failing, recovered, kernel panic,
disk full) × 3 widths (1440 / 820 / 390 px): no JavaScript errors, no overlapping controls; one horizontal
overflow found and fixed.

### Not verified here
* Starting the app against your real database from the VM fails with "disk I/O error": that connection
  cannot delete files, and SQLite must remove the old journal. Startup was verified on a copy of your
  database (all routes 200, 42 cases, your 7 experiments readable). On Windows, `python app.py` opens it
  normally and SQLite rolls back the leftover journal (it holds only an unfinished table creation).

## 12. Redesign for presentation (23 Oct 2026)

Backend, simulation, detection, recovery and data unchanged; only the interface was reorganised.

* **Five sections instead of ten pages**: Dashboard · Control Room (Controls & live view, All metrics) · Incident
  (1 · Diagnose, 2 · Fix & verify) · Deep-dive labs (Processes, Deadlock, Kernel panic) · Library (Real kernel bugs,
  Experiment history). Each section's pages share a tab bar; every old URL still works.
* **Dashboard**: live health with the primary cause, four vitals with sparklines, nine one-click scenarios (each
  starts from a clean server), context actions (Diagnose / Fix it / Reset to healthy).
* **Guided demo**: five scripted stories with a coach card that waits for the real detector / recovery / kernel
  state before *Next*. All five were driven end to end in a browser: CPU overload 48 s, Disk full 61 s,
  Deadlock 14 s, Kernel panic 33 s, Memory leak 70 s (clicking *Next* immediately); no JavaScript errors.
* **Teaching notes** switch (off by default) hides the learning explanations for a clean presentation.
* Shorter page subtitles; redundant cross-page buttons removed; page names updated in all visible text.
* Tests: 3 new browser tests (sections and tabs, dashboard scenario breaks and resets the server, guided demo waits
  for real detection and remembers exit); the existing notes test now switches notes on first.
  Cloud workspace: **135 passed, 0 failed, 0 skipped**.
* A backup of the previous interface is kept outside the project (restorable on request).

## 13. Console redesign and simplification (23 Oct 2026)

Requested after review of v3: less clutter, no repeated features, real bugs explained in plain words, a more
polished look. Simulation, detection and recovery logic unchanged except one bug fix (below).

* **Six destinations**: Overview · Experiments · Control Room · Incident · Labs · History. Only the labs share a tab
  bar. `/monitor` → `/lab`, `/analysis` → `/incident`, `/recovery` → `/incident#fix` (redirects); `/cases` still
  works but is not in the menu.
* **Console look**: dark ink sidebar with icons, health ring (share of requests served) on the Overview, tabular
  numbers, calmer cards. The colour meanings of the states are unchanged.
* **Overview is status only** — no more "run a failure" grid there; failures moved to a separate **Experiments**
  page (5 guided stories + 12 single failures, including process hang and out-of-memory → panic).
* **Incident is one page** in four steps (What failed → Why → Fix → Verify) instead of two pages. The "create a
  failure" chips and the separate *Full recovery* button in the Control Room were removed; *Full recovery* is now a
  link inside step 3. Compare strategies is a collapsible box on the same page.
* **Control Room**: the stress card, *Live consequences* rows and the *All metrics* page were removed as duplicates;
  one live chart with a metric picker (15 metrics) and a clickable six-cell status strip remain. Recording,
  snapshots and pause moved into *More*.
* **Labs**: the memory / disk / network panes of the Processes lab (duplicates of the Control Room) and their unused
  code were removed; repeated explanation cards in the Deadlock and Kernel labs were folded into collapsed
  *How this works* sections. The Kernel lab gained **What happens to data in a real kernel panic** (RAM and dirty
  page cache lost; fsync'd data and the file-system journal survive; kdump can save a vmcore).
* **Teaching-notes switch removed**: explanations are simply collapsed by default.
* **Real Linux cards** replace the Library page: one plain-English card per lab, built only from the SyzFix record's
  fields, with the crash-report and fix links and an "Another example" button.
* **Bug fix — out-of-memory panic diagnosis**: with `vm.panic_on_oom = 1` the report blamed *Service degradation* as
  the primary cause once the kernel halted. The kernel is now kept as primary while it is down, with the cause
  "memory ran out … the kernel panicked instead of killing one process"; Service is secondary (by Kernel panic).
  Covered by new tests in `test_audit_regressions.py`.
* **Tests**: browser tests rewritten for the new structure, not weakened — six sections and lab tabs, old-URL
  redirects, an experiment breaks the server and the Overview explains it, the Incident page goes from cause to a
  verified fix, the Real Linux cards render with both links and "Another example". Page-render test now checks the
  redirects. Cloud workspace: **140 passed, 0 failed, 0 skipped**. All five guided stories driven end to end in a
  browser (CPU 49 s, Disk full 61 s, Deadlock 14 s, Kernel panic 33 s, Memory leak 69 s), no JavaScript errors.
  No page overflows horizontally at 390, 820 or 1440 px in healthy, CPU-failed, disk-full or kernel-down states.

## 14. Simplification after review (23 Oct 2026)

* **Experiments page removed** (it repeated the Control Room and the guided demo). Its 12 one-click failures are now
  the **Inject a failure** card at the top of the Control Room (a picker, a one-line explanation and *Run*; each run
  resets to a healthy server first). Guided stories start from the sidebar's *Guided demo*. `/experiments` redirects
  to `/lab`. Sidebar: Overview · Control Room · Incident · Labs · History.
* **Real Linux cards removed from the labs** at the user's request (and the kernel lab's link and guided-demo step
  that pointed to them). The SyzFix data, `/api/cases` and the unlisted `/cases` page are unchanged.
* **Not every failure is a kernel panic** is now an always-visible card in the Kernel lab.
* **Cascade** is drawn as a tree: the primary cause, then each effect with its reason under it.
* **Control Room header**: *Record from clean start*, *Record now*, *Save snapshot* and *Pause* are plain buttons
  instead of a *More* menu.
* Files no longer used by any route (`analysis.html`, `recovery.html`, `monitor.html`, `experiments.html`,
  `monitor.js`, `realcase.js`) were moved out of the project; a copy is kept in `_to_delete/`.
* Tests updated, not weakened: five sections, `/experiments` redirect, injecting a failure from the Control Room,
  header tools visible, no real cards in the labs, failure-type table visible, cascade tree. Cloud workspace:
  **141 passed, 0 failed, 0 skipped**; all five guided stories end to end with no JavaScript errors.

