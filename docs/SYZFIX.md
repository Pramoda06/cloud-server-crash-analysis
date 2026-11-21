# Real Kernel Bug Case Studies (SyzFix subset)

A **reference section**, separate from the simulator: documented, already-fixed Linux kernel bugs that
help explain the simulated Kernel, Deadlock and Process labs. Page: `/cases` · API: `GET /api/cases`.
Provenance tag in the UI: **HISTORICAL DATASET** (real records from a public dataset — not simulated,
not derived, not read from this computer).

Nothing in `simulator/` imports this code; simulation logic, thresholds, recovery behaviour and the
colour palette are unchanged. A test (`test_simulator_does_not_depend_on_case_studies`) enforces this.

## 1. Source

| Item | Value |
|---|---|
| Dataset | SyzFix, sysec-uic — <https://huggingface.co/datasets/xiaoguangwang/syzfix-dataset> |
| Revision | `c0c94c4fd64381e840ffd047cf553e70464334df` (dataset last modified 12 Oct 2026; earlier seen as `e434df5`) |
| File used | **`train-00000-of-00011.parquet` only — 1 of 11 shards.** No other file is substituted. |
| Verified (16 Oct 2026) | 54,077,962 bytes · SHA-256 `a98ac4c4dc4572e51c98fee48c507a3b2ff2ed40aa15583c6eed6509e4450af8` — **identical to the value Hugging Face publishes for this file** · readable with pyarrow · 656 rows · all 14 expected columns (bug_id, title, crash_report, c_reproducer, syz_reproducer, fix_commit_hash, fix_commit_message, final_patch_diff, patch_evolution, subsystem, first_crash_date, fix_date, num_patch_versions, has_discussion) |
| Citation | The dataset card asks users to cite <https://github.com/sysec-uic/syzfix> (no paper yet) |
| Pipeline code | <https://github.com/sysec-uic/SyzFix> (MIT). Read for the schema only; not executed, not vendored. |
| Upstream of the records | syzbot (<https://syzkaller.appspot.com>), git.kernel.org, lore.kernel.org, patchwork.kernel.org |

The shard is **downloaded by you** into `data/raw/syzfix/` (git-ignored). This project never downloads it.

## 2. Licence review — field by field

The dataset card says the data is *"intended for research use"* and that *"kernel patches and
mailing-list text remain under their original licenses/copyrights"*. That is **not** a blanket
redistribution licence, so each field was judged on its own source:

| Field | Original source & terms | Decision |
|---|---|---|
| `bug_id`, `title`, dates, `subsystem`, `num_patch_versions`, `has_discussion` | syzbot metadata / derived counts — short factual identifiers | **Committed** |
| Links (syzbot report, git.kernel.org commit) | built from ids | **Committed** — the full text stays at the original source |
| Call-trace function names | kernel symbol names extracted from the crash report (facts, not prose) | **Committed** (≤ 6 names) |
| Fix commit hash + title, `Fixes:` reference (hash + title) | commit identifiers and one-line titles | **Committed** |
| Changed files, +/− line counts, changed function names | derived from the patch | **Committed** |
| Code excerpt of the fix (≤ 30 lines) | Linux kernel source — **GPL-2.0** (kernel `COPYING`) | **Committed**, labelled GPL-2.0 and linked to the full commit |
| `crash_report` text | syzbot output; **no reuse licence stated** | **Not committed.** Optional *local-only* excerpt (`--local-text`), git-ignored |
| Commit-message prose (developer's explanation) | written by individual developers; the kernel licence rules do not state a licence for commit messages | **Not committed.** Optional *local-only* excerpt, git-ignored; otherwise linked |
| Author names, e-mail addresses, `Signed-off-by` / `Reported-by` / `Cc` trailers | personal data | **Never used** — stripped even from local excerpts |
| `c_reproducer`, `syz_reproducer` | exploit-like test programs | **Never read** (column not loaded) |
| `patch_evolution`, mailing-list discussions | reviewer e-mail threads | **Never read** |

Result (rule 7 of the brief): the unresolved fields are handled with the **metadata-and-links-only**
alternative for the repository, plus an opt-in local tier generated from your own copy.

## 3. Two tiers

| Tier | File | In Git? | Contents |
|---|---|---|---|
| Committed | `data/processed/syzfix_cases.json` + `.meta.json` | yes (after generation) | metadata, links, derived facts, short GPL-2.0 code excerpt, checksum + selection report |
| Local only | `data/local/syzfix_local_text.json` | **no** (`.gitignore`) | ≤ 25-line crash-report excerpt, ≤ 1,500-char commit-message excerpt (trailers and e-mails removed) |
| Raw | `data/raw/syzfix/train-00000-of-00011.parquet` | **no** (`.gitignore`) | your downloaded shard |

The page shows local excerpts only when the local file exists, labelled "your local copy".

## 4. Steps

```bash
pip install -r requirements-dev.txt                       # pyarrow — only the prepare script needs it
# put train-00000-of-00011.parquet in data/raw/syzfix/
python scripts/prepare_syzfix_subset.py --verify-only     # filename, schema, size, SHA-256, rows
python scripts/prepare_syzfix_subset.py                   # committed tier
python scripts/prepare_syzfix_subset.py --local-text      # + local-only excerpts (optional)
```

The script refuses any other filename **and any file whose SHA-256 or size differs from the published
shard**, checks the required columns, records the checksum in the meta file, and validates every case
before writing. (`--skip-published-check` exists only for the tests' synthetic files.) The application itself needs only Flask; if no subset
has been prepared, `/cases` shows instructions instead.

## 5. Selection method (deterministic, no quotas)

1. **Eligible** if: crash report present; fix patch parseable; commit message ≥ 200 characters
   (so a documented explanation exists at the source); the fix touches ≤ 3 files and ≤ 60 changed lines
   (small enough to read).
2. **Category** from the syzbot title with fixed regular expressions, in precedence order:
   lock/deadlock → hang/stall → memory leak → NULL/invalid pointer → memory safety (KASAN/KFENCE) →
   uninitialised memory (KMSAN) → assertion (WARNING/BUG). A title that matches several is assigned to the
   first and the ambiguity is **recorded and shown**. Titles that match none are counted as unclassified,
   never forced into a category.
3. **Order** within a category: `sha256(bug_id)` ascending — independent of file order.
4. **Caps**: at most 6 per category and 2 per subsystem, for variety. **No quotas** — a category with
   fewer eligible bugs shows fewer; an empty category is not filled.
5. **Subsystem**: syzbot's label when present; otherwise derived from the changed file paths and marked
   "derived"; otherwise "not documented in the source".

6. **One case per fix commit**: syzbot can link several bug reports to one fix (one commit in this shard is
   linked to 19 reports); only the first in order is shown and the card says how many reports share it.
7. **NULL / bad-address cases** carry a short *address kind* label taken from the report
   (e.g. "NULL pointer (KASAN: null-ptr-deref)", "wild (corrupted) pointer") — most "general protection
   fault" titles are NULL dereferences, but not all.

`syzfix_cases.meta.json` reports rows read, ineligible counts by reason, eligible and selected counts
per category, ambiguous and unclassified counts.

## 5a. Actual result (shard 1 of 11, generated 16 Oct 2026)

| | |
|---|---|
| Rows read | 656 |
| Not eligible | 126 no fix commit · 137 fix patch text empty in the dataset · 32 commit message < 200 chars · 22 fix touches > 3 files · 10 fix changes > 60 lines |
| Eligible, classified | lock 44 · hang 8 · leak 20 · NULL/bad address 31 · memory safety 53 · uninitialised 24 · assertion 128 |
| Eligible, unclassified | 21 (e.g. UBSAN shift-out-of-bounds — undefined arithmetic, deliberately not called memory safety) |
| Titles matching > 1 category | 12 (assigned by precedence, shown on the card) |
| Selected | **42** — 6 in each of the 7 categories (every category had ≥ 8 eligible, so caps, not quotas, limited it); 5 skipped because their fix was already shown |
| Subsystem | the dataset's `subsystem` field is **empty in all 656 rows of this shard**, so every subsystem shown is *derived from file paths* and labelled so |
| Local-only text | not generated (the conservative default) |

The same input gives byte-identical output on Windows (Python 3.10) and Linux (Python 3.13): output SHA-256
`4f5bb692a620cc4315e0db6a988ca9fbc590cb561548d97d3c2bfaed9d77272c`.

Quality checks done on the real output before committing, and fixed: shift-out-of-bounds no longer counted
as memory safety; lockdep "WARNING: locking bug" counted as lock misuse; lockdep / UBSAN reporting frames
removed from call traces; struct declarations no longer listed as "functions changed"; 40-hex syzbot ids
link with `?id=` (20-hex ids use `?extid=`); duplicate fixes removed.

## 6. What each case shows

Symptom (syzbot title + top call-trace functions, link to full report) · Subsystem (with source) ·
Identified cause (the commit named in the fix's `Fixes:` tag; developer's explanation via link or local
excerpt — "not documented in the source" when absent) · Fix (title, hash, files ±, functions, GPL-2.0
excerpt) · Timeline · Source links. The short "what this kind of crash means" text is written for this
project and labelled **lab explanation**; it is not dataset content.

## 7. Limitations

* **One of eleven shards**: a subset, not all SyzFix bugs; shard contents are not a random sample.
* **Fuzzer-found bugs**: syzbot finds bugs by fuzzing; counts are **not production crash frequencies**.
* Categories come from title patterns, not from root-cause analysis.
* "Derived" subsystems are inferred from file paths.
* The `Fixes:` tag names the commit that introduced the bug; it is not a full root-cause narrative.
* Dates are syzbot's first-crash and fix dates; "days to fix" is their difference, not developer effort.
* The simulated Kernel Lab scenarios are linked to a real category only where the kind genuinely matches
  (NULL dereference). Other simulated scenarios say that no documented case is in the subset.
