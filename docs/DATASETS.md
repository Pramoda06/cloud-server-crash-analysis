# Dataset research (recommendation — nothing downloaded or integrated)

Researched on 15 Oct 2026, revised after your note on keeping purposes separate. **No dataset has been
downloaded or integrated**; this page is for the approval decision. Any future dataset-backed experiment must label its values as
*historical dataset observations*, separate from simulated, derived and real-host values.

## What a dataset could add

| Lab need | What kind of data helps | What it can NOT do |
|---|---|---|
| Realistic load shapes for the System Lab | CPU / memory / disk utilisation **time series** from real clusters, replayed into the controls | It does not contain crashes or root causes |
| Real kernel failures for the Kernel Failure Lab | Real **kernel crash reports** with their fixes | No resource time series; fuzzer-found bugs, not production crash rates |
| Log-based detection | Labelled **system logs** (normal vs alert) | Logs, not metrics; labels are alert tags, not causes |
| Hardware faults | Disk **SMART + failure** records | Daily snapshots, disk-only |

## Candidates

| Dataset | Contents | Size | Access & licence | Fit | Effort |
|---|---|---|---|---|---|
| **DataCenter-Traces-Datasets** (Zenodo 14564935, Univ. of Seville, v2 Dec 2024) | Pre-processed, data-centre-averaged usage: Alibaba 2018 machine usage (CPU %, memory %, net in/out, disk I/O %), Google 2019 instance usage (300 s), Azure v2 VM CPU/memory | ~1.1 MB of CSV | Direct download, **CC BY 4.0** | High for load replay — tiny, clean, permissive | Low |
| **Alibaba cluster-trace-v2018** | ~4,000 machines, 8 days; machine and container usage, batch DAGs | ~49 GB compressed (~280 GB) | Short survey before download; "research or study purpose"; no named licence | High (per-machine realism) | High — needs a justified subset |
| **Google cluster-usage traces v3 (2019)** | 8 Borg cells, May 2019; 5-min CPU usage histograms, alloc sets, job parents | ~2.4 TiB | **BigQuery only**; CC BY 4.0 | Medium — rich but heavy, requires a GCP account | Very high |
| **SyzFix** (sysec-uic) | 7,000+ fixed Linux kernel bugs from syzbot: crash report, reproducer flags, patch revisions, reviewer discussion, merged fix | ~2 GB download (~11 GB unpacked); ~4 MB index | GitHub + HuggingFace (`xiaoguangwang/syzfix-dataset`); code **MIT**, data aggregates public syzbot / lore.kernel.org / git.kernel.org content — confirm the dataset card before redistributing | High for the Kernel Lab ("real crash case studies") | Medium — use the index or a curated subset |
| **Loghub** (logpai) | 19 system-log datasets; BGL (4.7 M lines, labelled alerts), Thunderbird (labelled), HPC, Linux syslog (unlabelled) | 77 GB total; BGL ~709 MB | GitHub / Zenodo; "freely available for research or academic work" — check LICENSE | Medium (log anomaly view of failures) | Medium |
| **Backblaze Drive Stats** | Daily SMART snapshot per drive + failure records | Quarterly ZIPs, large | Free with citation; data itself may not be sold | Low–medium (hardware failure rates) | Medium |

## Purpose of each candidate (kept separate — never merged)

| Candidate | Purpose it would serve | Where it would live | Genuinely adds |
|---|---|---|---|
| **SyzFix** | A read-only **library of real Linux kernel bugs**: crash report, reproducer, patch revisions, merged fix | Kernel Failure Lab → "Real kernel crash case studies" | Yes — the lab's panics are simulated; real crash reports and their fixes show what actually causes panics (use-after-free, NULL dereference, WARN/BUG_ON, lockdep deadlocks) and how they are fixed |
| **DataCenter-Traces (Zenodo)** | **Workload replay**: drive CPU / memory controls with processed Alibaba usage | System Lab → "Replay a real workload" program | Limited — the series are data-centre averages (smooth, mostly 20–60 %), so replay alone would rarely push the simulated server into failure without artificial scaling |
| **Loghub** | **Historical log analysis**: real system logs with alert labels (BGL, Thunderbird) | Would need a new "Log analysis" page | Distinct, but it is a new feature area (log parsing / anomaly detection) rather than an improvement of the existing labs |

## Recommendation

* **Worth integrating: a small, curated SyzFix subset** (e.g. 40–80 bugs chosen from the ~4 MB index, stored with source links). It gives the Kernel Failure Lab real evidence beside its simulation, with a clear label: *fuzzer-found bugs, not production crash rates, no resource telemetry*.
* **Optional, low priority: DataCenter-Traces** — only if a "realistic daily load shape" demo is wanted; state that values are averages and do not cause failures by themselves.
* **Not now: Loghub** — it would add a separate log-analysis feature, which is outside the current learning goals.

Nothing has been downloaded. I will wait for your approval before downloading or integrating any of these.

## After approval, the workflow will be

official download → `data/raw/` (untouched) → schema notes → quality checks (missing values,
duplicates, ranges) → reproducible `scripts/prepare_<dataset>.py` → `data/processed/` → summary
statistics and plots → tests that validate the processed file → integration as a clearly
labelled *dataset replay* experiment → documentation of source, licence, preprocessing and
limitations.

## Sources

- [Google cluster-data — ClusterData2019](https://github.com/google/cluster-data/blob/master/ClusterData2019.md)
- [Alibaba clusterdata](https://github.com/alibaba/clusterdata) · [trace_2018.md](https://github.com/alibaba/clusterdata/blob/master/cluster-trace-v2018/trace_2018.md)
- [DataCenter-Traces-Datasets, Zenodo 14564935](https://zenodo.org/records/14564935)
- [SyzFix](https://github.com/sysec-uic/SyzFix)
- [Loghub](https://github.com/logpai/loghub) · [Loghub paper (arXiv 2008.06448)](https://arxiv.org/abs/2008.06448)
- [Backblaze Drive Stats](https://www.backblaze.com/drivestats)
