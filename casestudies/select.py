"""
Deterministic selection of a small SyzFix subset (pure functions, no I/O, no network).

Input rows are plain dicts with the SyzFix Parquet columns (bug_id, title, crash_report,
fix_commit_hash, fix_commit_message, final_patch_diff, subsystem, first_crash_date, fix_date,
num_patch_versions, has_discussion). Reproducers and discussions are never read.

Two output tiers (see docs/SYZFIX.md for the licence review):
  committed  metadata, links, derived facts, short GPL-2.0 code excerpt   -> safe to commit
  local      crash-report and commit-message excerpts from the user's own -> git-ignored,
             downloaded copy (no reuse licence stated, so never committed)    never redistributed
"""
import hashlib
import re

# ------------------------------------------------------------------ categories
# Ordered by precedence. A title can match several patterns (e.g. "KASAN: null-ptr-deref" is both a KASAN
# report and a NULL dereference); it is assigned to the FIRST matching category and the ambiguity is recorded.
CATEGORIES = [
    ("lock", "Lock ordering and lock misuse", [
        r"possible deadlock in", r"possible circular locking dependency", r"inconsistent lock state",
        r"possible recursive locking", r"possible irq lock inversion", r"bad unlock balance",
        r"^WARNING: locking bug", r"lock held when returning to user space"]),
    ("hang", "Hung tasks and stalls", [
        r"^INFO: task hung", r"rcu.*detected.*stall", r"soft lockup", r"hard lockup", r"^INFO: task .* blocked"]),
    ("leak", "Memory leaks", [r"^memory leak in", r"^BUG: memory leak"]),
    ("null", "NULL pointer / bad address access", [
        r"null-ptr-deref", r"^general protection fault", r"unable to handle (page fault|kernel NULL|kernel paging)",
        r"kernel NULL pointer dereference"]),
    ("memsafety", "Use-after-free and out-of-bounds access", [
        # "shift-out-of-bounds" is undefined arithmetic, not a memory access, so it is excluded here
        r"^KASAN: ", r"^KFENCE: ", r"use-after-free", r"(?<!shift-)out-of-bounds", r"double-free", r"invalid-free"]),
    ("uninit", "Use of uninitialized memory", [r"^KMSAN: ", r"uninit-value"]),
    ("assert", "Kernel assertions (WARN / BUG_ON)", [r"^WARNING in ", r"^WARNING: ", r"^kernel BUG", r"^BUG: "]),
]
CATEGORY_NAMES = {c: n for c, n, _ in CATEGORIES}
_COMPILED = [(c, [re.compile(p, re.I) for p in pats]) for c, _, pats in CATEGORIES]


def classify(title):
    """Return (category or None, [every category whose pattern matched])."""
    matched = [c for c, pats in _COMPILED if any(p.search(title or "") for p in pats)]
    return (matched[0] if matched else None), matched


# ------------------------------------------------------------- patch parsing
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(\.[\w-]+)+")
_TRAILER = re.compile(r"^(Signed-off-by|Reported-by|Reported-and-tested-by|Tested-by|Reviewed-by|Acked-by|Cc|"
                      r"Suggested-by|Co-developed-by|Link|Closes|Message-ID|Debugged-by|Analyzed-by):", re.I)
_FIXES = re.compile(r"^Fixes:\s*([0-9a-f]{8,40})\s*(?:\(\"(.+)\"\))?", re.I | re.M)
_HUNK_FN = re.compile(r"^@@ [^@]+ @@ ?(.*)$")
_DIFF_FILE = re.compile(r"^diff --git a/(\S+) b/(\S+)")
_HUNK_RANGE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def strip_emails(text):
    return _EMAIL.sub("[email removed]", text or "")


def parse_patch(patch):
    """Split a git format-patch text (as served by git.kernel.org /patch/) into its parts.

    Returns dict(subject, body, fixes, files, functions, diff) — body WITHOUT personal trailers."""
    out = {"subject": None, "body": "", "fixes": [], "files": [], "functions": [], "diff": ""}
    if not patch or "diff --git" not in patch:
        return out
    head, _, rest = patch.partition("\n---\n")
    if not rest:                                   # no '---' separator: cannot split reliably
        head, rest = patch[:patch.index("diff --git")], patch[patch.index("diff --git"):]
    lines = head.splitlines()
    subj, body_start = None, None
    for i, ln in enumerate(lines):
        if ln.startswith("Subject:"):
            subj = re.sub(r"^Subject:\s*(\[PATCH[^\]]*\]\s*)?", "", ln).strip()
            j = i + 1
            while j < len(lines) and lines[j].startswith((" ", "\t")):      # folded header
                subj += " " + lines[j].strip()
                j += 1
            body_start = j
            break
    out["subject"] = subj
    if body_start is not None:
        body = lines[body_start:]
        while body and not body[0].strip():
            body = body[1:]
        kept = [ln for ln in body if not _TRAILER.match(ln.strip())]
        out["body"] = strip_emails("\n".join(kept).strip())
    out["fixes"] = [{"hash": m.group(1)[:12], "title": m.group(2)} for m in _FIXES.finditer(head)]
    diff = rest[rest.index("diff --git"):] if "diff --git" in rest else ""
    diff = re.sub(r"\n-- ?\n\d[\w.+-]*\s*$", "\n", diff)    # drop the trailing '-- <git version>' signature
    out["diff"] = diff
    files, cur = [], None
    old_left = new_left = 0                        # lines still expected in the current hunk (from its @@ header)
    for ln in diff.splitlines():
        m = _DIFF_FILE.match(ln)
        if m:
            cur = {"path": m.group(2), "added": 0, "removed": 0}
            files.append(cur)
            old_left = new_left = 0
            continue
        if cur is None:
            continue
        if old_left > 0 or new_left > 0:           # inside a hunk: count by prefix, honouring hunk lengths
            if ln.startswith("+"):
                cur["added"] += 1
                new_left -= 1
            elif ln.startswith("-"):
                cur["removed"] += 1
                old_left -= 1
            elif not ln.startswith("\\"):          # context line ("\ No newline" is not a line)
                old_left -= 1
                new_left -= 1
            continue
        hr = _HUNK_RANGE.match(ln)
        if hr:                                     # outside hunks: '---'/'+++' headers and the '-- ' signature
            old_left = int(hr.group(2)) if hr.group(2) is not None else 1
            new_left = int(hr.group(4)) if hr.group(4) is not None else 1
        h = _HUNK_FN.match(ln)
        if h and h.group(1):
            fn = re.findall(r"([A-Za-z_]\w*)\s*\(", h.group(1))
            name = fn[-1] if fn else None          # hunk context that is not a function (a struct...) is skipped
            if name and name not in out["functions"]:
                out["functions"].append(name)
    out["files"] = files
    return out


def diff_excerpt(diff, max_lines=30):
    """First hunk(s) of the change, at most max_lines, emails removed (kernel code is GPL-2.0)."""
    lines, take = [], False
    for ln in diff.splitlines():
        if ln.startswith("@@"):
            take = True
        if take:
            lines.append(strip_emails(ln))
        if len(lines) >= max_lines:
            lines.append("[… excerpt truncated — full change at the source link]")
            break
    return "\n".join(lines)


_FRAME = re.compile(r"^\s*(?:\[[^\]]*\]\s*)?([A-Za-z_][\w.]*)\+0x[0-9a-f]+/0x[0-9a-f]+", re.M)
_SKIP_FRAMES = {"dump_stack", "dump_stack_lvl", "print_report", "kasan_report", "print_address_description",
                "__warn", "report_bug", "handle_bug", "exc_invalid_op", "asm_exc_invalid_op", "warn_slowpath_fmt",
                "show_stack", "panic", "__dump_stack", "check_noncircular", "__lock_acquire", "lock_acquire",
                "kasan_check_range", "__asan_load8", "__asan_store8", "__kasan_check_read", "__kasan_check_write",
                # lockdep's own reporting/validation frames (the locking code under test comes after them)
                "print_deadlock_bug", "print_circular_bug", "print_usage_bug", "print_bad_irq_dependency",
                "print_irq_inversion_bug", "print_lock_invalid_wait_context", "check_deadlock", "check_prev_add",
                "check_prevs_add", "check_irq_usage", "validate_chain", "mark_lock", "mark_usage",
                "lockdep_print_held_locks", "debug_show_all_locks"}


def call_trace_functions(report, limit=6):
    """Function names of the first call-trace frames (facts derived from the report, not its text)."""
    out = []
    for m in _FRAME.finditer(report or ""):
        fn = m.group(1).split(".")[0]
        if (fn in _SKIP_FRAMES or fn in out or fn.startswith(("__sanitizer", "__ubsan", "ubsan_", "kasan_", "__kasan",
                                                              "kmsan_", "__msan"))):
            continue
        out.append(fn)
        if len(out) >= limit:
            break
    return out


def crash_excerpt(report, max_lines=25):
    """LOCAL TIER ONLY: the report from its first error line, emails removed."""
    lines = (report or "").splitlines()
    start = next((i for i, ln in enumerate(lines)
                  if re.search(r"(BUG:|WARNING:|KASAN:|KMSAN:|general protection fault|INFO: |kernel BUG|"
                               r"unreferenced object|possible deadlock|circular locking)", ln)), 0)
    ex = [strip_emails(ln) for ln in lines[start:start + max_lines]]
    if len(lines) - start > max_lines:
        ex.append("[… truncated — full report at the syzbot link]")
    return "\n".join(ex)


# ----------------------------------------------------------------- selection
MAX_FILES, MAX_LINES, MIN_BODY = 3, 60, 200
PER_CATEGORY, PER_SUBSYSTEM = 6, 2


_ADDR = [  # (pattern in the crash report, short label) — first match wins; a label, not copied text
    (r"KASAN: null-ptr-deref in range", "NULL pointer (KASAN: null-ptr-deref)"),
    (r"kernel NULL pointer dereference", "NULL pointer"),
    (r"KASAN: (maybe )?wild-memory-access in range", "wild (corrupted) pointer"),
    (r"KASAN: probably user-memory-access in range", "user-space address used in kernel mode"),
    (r"unable to handle (page fault for address|kernel paging request)", "unmapped kernel address"),
]


def address_kind(title, report):
    """What kind of bad address the report says was accessed (only for the NULL / bad-address category)."""
    text = (report or "") + "\n" + (title or "")
    for pat, label in _ADDR:
        if re.search(pat, text):
            return label
    if re.search(r"null-ptr-deref", title or "", re.I):
        return "NULL pointer (KASAN: null-ptr-deref)"
    return None


def syzbot_link(bug_id):
    # syzbot uses 20-hex "extid" values and 40-hex "id" values for its bug pages
    return f"https://syzkaller.appspot.com/bug?{'extid' if len(bug_id) == 20 else 'id'}={bug_id}"


def subsystem_of(row, files):
    s = (row.get("subsystem") or "").strip()
    if s:
        return s, "syzbot"
    if files:
        parts = files[0]["path"].split("/")
        return "/".join(parts[:2]) if len(parts) > 2 else parts[0], "derived from the files the fix changed"
    return None, "not documented in the source"


def eligibility(row, parsed):
    """Return None if eligible, else the reason it is not (used for the selection report)."""
    if not (row.get("crash_report") or "").strip():
        return "no crash report"
    if not row.get("fix_commit_hash"):
        return "no fix commit"
    if not (row.get("final_patch_diff") or "").strip():
        return "fix patch text empty in the dataset"
    if not parsed["files"]:
        return "fix patch unparseable"
    if len(parsed["body"]) < MIN_BODY:
        return f"commit message shorter than {MIN_BODY} characters"
    if len(parsed["files"]) > MAX_FILES:
        return f"fix touches more than {MAX_FILES} files"
    if sum(f["added"] + f["removed"] for f in parsed["files"]) > MAX_LINES:
        return f"fix changes more than {MAX_LINES} lines"
    return None


def order_key(bug_id):
    return hashlib.sha256(bug_id.encode()).hexdigest()


def _date(s):
    return (s or "")[:10] or None


def _days(a, b):
    from datetime import date
    try:
        return (date.fromisoformat(b[:10]) - date.fromisoformat(a[:10])).days
    except Exception:
        return None


def build_case(row, parsed, category, matched, bugs_with_fix=1):
    sub, sub_src = subsystem_of(row, parsed["files"])
    h = row["fix_commit_hash"]
    return {
        "bug_id": row["bug_id"],
        "title": row["title"],
        "category": category, "category_name": CATEGORY_NAMES[category], "matched_categories": matched,
        "subsystem": sub, "subsystem_source": sub_src,
        "first_crash_date": _date(row.get("first_crash_date")), "fix_date": _date(row.get("fix_date")),
        "days_to_fix": _days(row.get("first_crash_date") or "", row.get("fix_date") or ""),
        "call_trace_functions": call_trace_functions(row.get("crash_report")),
        "address_kind": address_kind(row.get("title"), row.get("crash_report")) if category == "null" else None,
        "fix": {
            "hash": h, "title": parsed["subject"] or row.get("fix_commit_message") or None,
            "fixes": parsed["fixes"], "files": parsed["files"], "functions": parsed["functions"][:6],
            "lines_changed": sum(f["added"] + f["removed"] for f in parsed["files"]),
            "diff_excerpt": diff_excerpt(parsed["diff"]),
            "bugs_in_shard": bugs_with_fix,       # syzbot can link several bug reports to one fix commit
        },
        "num_patch_versions": int(row.get("num_patch_versions") or 0),
        "has_discussion": bool(row.get("has_discussion")),
        "links": {
            "syzbot": syzbot_link(row["bug_id"]),
            "commit": f"https://git.kernel.org/pub/scm/linux/kernel/git/torvalds/linux.git/commit/?id={h}",
        },
    }


def select(rows):
    """rows: iterable of dicts. Returns (cases, report, local_text)."""
    report = {"rows_read": 0, "ineligible": {}, "eligible_by_category": {}, "unclassified_eligible": 0,
              "ambiguous_titles": [], "selected_by_category": {}, "skipped_subsystem_cap": 0,
              "skipped_same_fix": 0}
    rows = list(rows)
    per_fix = {}
    for row in rows:
        if row.get("fix_commit_hash"):
            per_fix[row["fix_commit_hash"]] = per_fix.get(row["fix_commit_hash"], 0) + 1
    pool = {c: [] for c, _, _ in CATEGORIES}
    keep = {}
    for row in rows:
        report["rows_read"] += 1
        parsed = parse_patch(row.get("final_patch_diff"))
        why = eligibility(row, parsed)
        if why:
            report["ineligible"][why] = report["ineligible"].get(why, 0) + 1
            continue
        cat, matched = classify(row.get("title"))
        if cat is None:
            report["unclassified_eligible"] += 1
            continue
        report["eligible_by_category"][cat] = report["eligible_by_category"].get(cat, 0) + 1
        if len(matched) > 1:
            report["ambiguous_titles"].append({"bug_id": row["bug_id"], "title": row["title"],
                                               "matched": matched, "assigned": cat})
        pool[cat].append(row["bug_id"])
        keep[row["bug_id"]] = (row, parsed, cat, matched)
    cases, local, used_fix = [], {}, set()
    for cat, _, _ in CATEGORIES:
        per_sub = {}
        n = 0
        for bid in sorted(pool[cat], key=order_key):
            if n >= PER_CATEGORY:
                break
            row, parsed, c, matched = keep[bid]
            sub, _ = subsystem_of(row, parsed["files"])
            if row["fix_commit_hash"] in used_fix:            # one case per fix commit (variety, no duplicates)
                report["skipped_same_fix"] += 1
                continue
            if per_sub.get(sub, 0) >= PER_SUBSYSTEM:
                report["skipped_subsystem_cap"] += 1
                continue
            per_sub[sub] = per_sub.get(sub, 0) + 1
            used_fix.add(row["fix_commit_hash"])
            cases.append(build_case(row, parsed, c, matched, per_fix.get(row["fix_commit_hash"], 1)))
            local[bid] = {"crash_excerpt": crash_excerpt(row.get("crash_report")),
                          "commit_message_excerpt": parsed["body"][:1500] + ("…" if len(parsed["body"]) > 1500 else "")}
            n += 1
        report["selected_by_category"][cat] = n
    report["ambiguous_count"] = len(report["ambiguous_titles"])
    return cases, report, local


# ---------------------------------------------------------------- validation
CASE_KEYS = {"bug_id", "title", "category", "category_name", "matched_categories", "subsystem", "subsystem_source",
             "first_crash_date", "fix_date", "days_to_fix", "call_trace_functions", "address_kind", "fix",
             "num_patch_versions",
             "has_discussion", "links"}
FORBIDDEN = ("reproducer", "discussion_text", "patch_evolution", "crash_report", "commit_message", "author", "email")


def validate_case(c):
    """Return a list of problems (empty = valid). Enforces the committed-tier safeguards."""
    errs = []
    if set(c) != CASE_KEYS:
        errs.append(f"keys differ: {sorted(set(c) ^ CASE_KEYS)}")
    for k in c:
        if any(f in k for f in FORBIDDEN):
            errs.append(f"forbidden field {k}")
    if not re.fullmatch(r"[0-9a-f]{20}|[0-9a-f]{40}", c.get("bug_id") or ""):
        errs.append("bug_id is not a syzbot extid")
    if c.get("category") not in CATEGORY_NAMES:
        errs.append("unknown category")
    fx = c.get("fix") or {}
    if not re.fullmatch(r"[0-9a-f]{12,40}", fx.get("hash") or ""):
        errs.append("fix hash malformed")
    if set(fx) != {"hash", "title", "fixes", "files", "functions", "lines_changed", "diff_excerpt", "bugs_in_shard"}:
        errs.append("fix keys differ")
    if len((fx.get("diff_excerpt") or "").splitlines()) > 31:
        errs.append("diff excerpt too long")
    links = c.get("links") or {}
    if not (links.get("syzbot") == syzbot_link(c.get("bug_id") or "")
            and links.get("commit", "").startswith("https://git.kernel.org/")):
        errs.append("source links missing")
    blob = repr(c)
    if _EMAIL.search(blob.replace("[email removed]", "")):
        errs.append("email address present")
    return errs
