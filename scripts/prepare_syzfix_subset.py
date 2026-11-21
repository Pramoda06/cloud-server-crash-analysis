"""
Prepare the Real Kernel Bug Case Studies subset from ONE SyzFix Parquet shard.

    pip install -r requirements-dev.txt          # pyarrow (only this script needs it)
    python scripts/prepare_syzfix_subset.py --verify-only
    python scripts/prepare_syzfix_subset.py                 # committed tier only
    python scripts/prepare_syzfix_subset.py --local-text    # + git-ignored local excerpts

Input  data/raw/syzfix/train-00000-of-00011.parquet   (git-ignored; never committed)
Output data/processed/syzfix_cases.json               (committed tier: metadata, links, derived facts,
                                                        short GPL-2.0 code excerpts)
       data/processed/syzfix_cases.meta.json          (provenance, checksum, selection report)
       data/local/syzfix_local_text.json              (only with --local-text; git-ignored)

The script reads only the columns it needs: reproducer and discussion columns are never loaded.
"""
import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from casestudies import select as S  # noqa: E402

EXPECTED_NAME = "train-00000-of-00011.parquet"
RAW = os.path.join(ROOT, "data", "raw", "syzfix", EXPECTED_NAME)
DATA = os.path.join(ROOT, "data")


def paths(data_dir):
    return (os.path.join(data_dir, "processed", "syzfix_cases.json"),
            os.path.join(data_dir, "processed", "syzfix_cases.meta.json"),
            os.path.join(data_dir, "local", "syzfix_local_text.json"))
COLUMNS = ["bug_id", "title", "crash_report", "fix_commit_hash", "fix_commit_message", "final_patch_diff",
           "subsystem", "first_crash_date", "fix_date", "num_patch_versions", "has_discussion"]
SOURCE = {"dataset": "SyzFix (sysec-uic) — Hugging Face xiaoguangwang/syzfix-dataset",
          "dataset_revision": "c0c94c4fd64381e840ffd047cf553e70464334df (lastModified 2026-10-12)",
          "published_sha256": "a98ac4c4dc4572e51c98fee48c507a3b2ff2ed40aa15583c6eed6509e4450af8",
          "published_bytes": 54077962,
          "citation": "Please cite https://github.com/sysec-uic/syzfix (dataset card request)",
          "shard": EXPECTED_NAME, "shards_total": 11,
          "url": f"https://huggingface.co/datasets/xiaoguangwang/syzfix-dataset/blob/main/data/{EXPECTED_NAME}",
          "upstream": ["syzkaller.appspot.com (syzbot)", "git.kernel.org", "lore.kernel.org", "patchwork.kernel.org"]}


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def verify(path, check_published=True):
    if os.path.basename(path) != EXPECTED_NAME:
        raise SystemExit(f"Refusing {os.path.basename(path)}: expected exactly {EXPECTED_NAME}")
    if not os.path.isfile(path):
        raise SystemExit(f"Not found: {path}")
    import pyarrow.parquet as pq
    pf = pq.ParquetFile(path)
    names = pf.schema_arrow.names
    missing = [c for c in COLUMNS if c not in names]
    info = {"file": EXPECTED_NAME, "bytes": os.path.getsize(path), "sha256": sha256(path),
            "rows": pf.metadata.num_rows, "columns": names,
            "column_types": {n: str(pf.schema_arrow.field(n).type) for n in names}, "missing_required": missing}
    info["matches_published"] = (info["sha256"] == SOURCE["published_sha256"]
                                 and info["bytes"] == SOURCE["published_bytes"])
    if check_published and not info["matches_published"]:
        raise SystemExit("Checksum/size differ from the published shard "
                         f"({SOURCE['published_sha256'][:16]}…, {SOURCE['published_bytes']} bytes). "
                         "Refusing: this is not the verified file. Re-download it, do not rename another file.")
    if missing:
        raise SystemExit(f"Schema mismatch — required columns missing: {missing}\n{json.dumps(info, indent=1)}")
    return info, pf


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", default=RAW)
    ap.add_argument("--data-dir", default=DATA, help="where processed/ and local/ are written")
    ap.add_argument("--verify-only", action="store_true")
    ap.add_argument("--local-text", action="store_true", help="also write git-ignored local excerpts")
    ap.add_argument("--skip-published-check", action="store_true",
                    help="tests only: accept a file whose checksum differs from the published shard")
    a = ap.parse_args(argv)
    OUT, META, LOCAL = paths(a.data_dir)
    info, pf = verify(a.input, check_published=not a.skip_published_check)
    print(json.dumps({k: v for k, v in info.items() if k != "column_types"}, indent=1))
    if a.verify_only:
        return
    rows = pf.read(columns=COLUMNS).to_pylist()
    for r in rows:                                       # normalise dates/booleans to plain types
        for k in ("first_crash_date", "fix_date"):
            if r.get(k) is not None and not isinstance(r[k], str):
                r[k] = str(r[k])
    cases, report, local = S.select(rows)
    bad = {c["bug_id"]: S.validate_case(c) for c in cases if S.validate_case(c)}
    if bad:
        raise SystemExit(f"Validation failed: {bad}")
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(cases, f, ensure_ascii=False, indent=1)
    meta = {"source": SOURCE, "input": {k: info[k] for k in ("file", "bytes", "sha256", "rows", "matches_published")},
            "generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            "method": {"eligibility": {"max_files": S.MAX_FILES, "max_lines": S.MAX_LINES,
                                       "min_commit_message_chars": S.MIN_BODY},
                       "order": "sha256(bug_id) ascending within each category",
                       "per_category_max": S.PER_CATEGORY, "per_subsystem_max": S.PER_SUBSYSTEM,
                       "categories": [{"id": c, "name": n, "title_patterns": p} for c, n, p in S.CATEGORIES],
                       "no_quotas": "categories with fewer eligible bugs simply contribute fewer cases"},
            "report": report, "cases": len(cases)}
    with open(META, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=1)
    if a.local_text:
        os.makedirs(os.path.dirname(LOCAL), exist_ok=True)
        with open(LOCAL, "w", encoding="utf-8") as f:
            json.dump({"note": "Local-only excerpts from your own downloaded copy. Not redistributed; git-ignored.",
                       "shard_sha256": info["sha256"], "cases": local}, f, ensure_ascii=False, indent=1)
    print(json.dumps({"cases": len(cases), **{k: report[k] for k in ("rows_read", "eligible_by_category",
          "selected_by_category", "ambiguous_count", "unclassified_eligible", "ineligible")}}, indent=1))


if __name__ == "__main__":
    main()
