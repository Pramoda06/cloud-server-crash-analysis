"""
Real Kernel Bug Case Studies: selection, licensing safeguards, validation, loading and page.

All rows below are SYNTHETIC test fixtures (fake ids, example.org addresses, invented function names).
They exist only to test the pipeline and are never shown in the application.
"""
try:                                      # temporary database for the tests (must run before `import app`)
    from tests import _isolation  # noqa: F401
except ImportError:
    import _isolation  # noqa: F401
import json
import os
import random
import subprocess
import sys
import tempfile
import unittest

from casestudies import select as S
from casestudies import store

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def fake_patch(subject, body_extra="", files=(("fs/demo/a.c", 3, 1),), fixes="0123456789ab (\"demo: add thing\")"):
    body = ("This is a synthetic commit message used only by the unit tests. It explains a fictional race "
            "between demo_open() and demo_release() where an object is freed while still referenced. " + body_extra)
    diff = ""
    for path, add, rem in files:
        diff += (f"diff --git a/{path} b/{path}\nindex 1..2 100644\n--- a/{path}\n+++ b/{path}\n"
                 f"@@ -10,{rem + 1} +10,{add + 1} @@ static int demo_open(struct inode *i)\n"
                 + "".join(f"+\tnew_line_{k};\n" for k in range(add))
                 + "".join(f"-\told_line_{k};\n" for k in range(rem)) + " context;\n")
    return (f"From abc Mon Sep 17 00:00:00 2001\nFrom: Test Author <author@example.org>\nDate: Mon, 1 Jan 2024\n"
            f"Subject: [PATCH] {subject}\n\n{body}\n\nFixes: {fixes}\n"
            f"Reported-by: bot+abc@example.org\nSigned-off-by: Test Author <author@example.org>\nCc: list@example.org\n"
            f"---\n fs/demo/a.c | 4 +++-\n 1 file changed\n\n{diff}-- \n2.40.1\n\n")


REPORT = ("BUG: KASAN: slab-use-after-free in demo_read+0x12/0x40\nRead of size 8\n\nCall Trace:\n <TASK>\n"
          " dump_stack_lvl+0x1/0x2\n print_report+0x1/0x2\n kasan_report+0x1/0x2\n demo_read+0x12/0x40\n"
          " vfs_read+0x10/0x20\n ksys_read+0x10/0x20\n do_syscall_64+0x10/0x20\nreported by someone@example.org\n")


def row(i, title, **kw):
    r = {"bug_id": f"{i:020x}", "title": title, "crash_report": REPORT, "fix_commit_hash": f"{i:040x}",
         "fix_commit_message": "demo: fix", "final_patch_diff": fake_patch("demo: fix use-after-free in demo_read"),
         "subsystem": "", "first_crash_date": "2024-01-01", "fix_date": "2024-02-10", "num_patch_versions": 2,
         "has_discussion": True, "c_reproducer": "int main(){}", "syz_reproducer": "r0 = openat()"}
    r.update(kw)
    return r


class TestClassify(unittest.TestCase):
    def test_rules_and_ambiguity(self):
        self.assertEqual(S.classify("possible deadlock in demo_lock")[0], "lock")
        self.assertEqual(S.classify("INFO: task hung in demo_sync")[0], "hang")
        self.assertEqual(S.classify("memory leak in demo_alloc")[0], "leak")
        self.assertEqual(S.classify("KASAN: slab-use-after-free Read in demo_read")[0], "memsafety")
        self.assertEqual(S.classify("WARNING in demo_check")[0], "assert")
        self.assertEqual(S.classify("KMSAN: uninit-value in demo_copy")[0], "uninit")
        cat, matched = S.classify("KASAN: null-ptr-deref Read in demo_get")
        self.assertEqual(cat, "null")                                        # precedence
        self.assertEqual(matched, ["null", "memsafety"])                     # ambiguity recorded
        self.assertEqual(S.classify("unexpected kernel reboot")[0], None)   # unclassified, never forced
        # undefined arithmetic is not a memory-safety bug; lockdep "locking bug" is lock misuse
        self.assertEqual(S.classify("UBSAN: shift-out-of-bounds in demo_bind")[0], None)
        self.assertEqual(S.classify("UBSAN: array-index-out-of-bounds in demo_x")[0], "memsafety")
        self.assertEqual(S.classify("WARNING: locking bug in demo_put")[0], "lock")


class TestPatchParsing(unittest.TestCase):
    def test_parse_strips_trailers_and_emails(self):
        p = S.parse_patch(fake_patch("demo: fix it", "Contact me at x@example.org."))
        self.assertEqual(p["subject"], "demo: fix it")
        self.assertNotIn("Signed-off-by", p["body"])
        self.assertNotIn("@example.org", p["body"])
        self.assertIn("[email removed]", p["body"])
        self.assertEqual(p["fixes"], [{"hash": "0123456789ab", "title": "demo: add thing"}])
        self.assertEqual(p["files"], [{"path": "fs/demo/a.c", "added": 3, "removed": 1}])
        self.assertEqual(p["functions"], ["demo_open"])

    def test_signature_and_headers_not_counted(self):
        # the format-patch '-- ' signature and '---'/'+++' headers sit outside hunks and must not count
        p = S.parse_patch(fake_patch("x", files=(("a/b.c", 0, 2), ("a/c.c", 1, 0))))
        self.assertEqual([(f["added"], f["removed"]) for f in p["files"]], [(0, 2), (1, 0)])
        self.assertNotIn("2.40.1", p["diff"])
        self.assertNotIn("2.40.1", S.diff_excerpt(p["diff"]))

    def test_call_trace_functions_skip_reporting_frames(self):
        self.assertEqual(S.call_trace_functions(REPORT), ["demo_read", "vfs_read", "ksys_read", "do_syscall_64"])
        lockdep = " print_deadlock_bug+0x1/0x2\n validate_chain+0x1/0x2\n down_read+0x1/0x2\n demo_open+0x1/0x2\n"
        self.assertEqual(S.call_trace_functions(lockdep), ["down_read", "demo_open"])

    def test_address_kind_and_links(self):
        rep_ = "general protection fault, probably for non-canonical address\nKASAN: null-ptr-deref in range [0x0-0x7]\n"
        self.assertEqual(S.address_kind("general protection fault in x", rep_), "NULL pointer (KASAN: null-ptr-deref)")
        self.assertEqual(S.address_kind("general protection fault in x", "KASAN: maybe wild-memory-access in range"),
                         "wild (corrupted) pointer")
        self.assertIsNone(S.address_kind("general protection fault in x", "nothing stated"))
        self.assertTrue(S.syzbot_link("a" * 20).endswith("?extid=" + "a" * 20))
        self.assertTrue(S.syzbot_link("b" * 40).endswith("?id=" + "b" * 40))

    def test_unparseable_patch(self):
        self.assertEqual(S.parse_patch("")["files"], [])
        self.assertEqual(S.eligibility(row(1, "WARNING in x", final_patch_diff=""), S.parse_patch("")),
                         "fix patch text empty in the dataset")
        junk = "Subject: x\n\nno diff here"
        self.assertEqual(S.eligibility(row(1, "WARNING in x", final_patch_diff=junk), S.parse_patch(junk)),
                         "fix patch unparseable")

    def test_hunk_context_that_is_not_a_function_is_skipped(self):
        p = S.parse_patch(fake_patch("x").replace("static int demo_open(struct inode *i)", "struct demo_ops {"))
        self.assertEqual(p["functions"], [])


class TestSelection(unittest.TestCase):
    def rows(self):
        out = []
        for i in range(1, 15):
            out.append(row(i, f"KASAN: slab-use-after-free Read in demo_{i}",
                           final_patch_diff=fake_patch("demo: fix", files=((f"drivers/d{i % 4}/x.c", 2, 1),))))
        out.append(row(100, "possible deadlock in demo_lock"))                                   # 1 eligible lock case
        out.append(row(101, "WARNING in big", final_patch_diff=fake_patch("x", files=(("a/b.c", 80, 0),))))
        out.append(row(102, "WARNING in many", final_patch_diff=fake_patch("x", files=tuple((f"a/{k}.c", 1, 0) for k in range(5)))))
        out.append(row(103, "WARNING in empty", crash_report=""))
        out.append(row(104, "something odd happened"))
        return out

    def test_deterministic_caps_no_quotas(self):
        rows = self.rows()
        a, rep, local = S.select(rows)
        shuffled = rows[:]
        random.Random(7).shuffle(shuffled)
        b, _, _ = S.select(shuffled)
        self.assertEqual([c["bug_id"] for c in a], [c["bug_id"] for c in b])        # order-independent
        mem = [c for c in a if c["category"] == "memsafety"]
        self.assertLessEqual(len(mem), S.PER_CATEGORY)
        subs = {}
        for c in mem:
            subs[c["subsystem"]] = subs.get(c["subsystem"], 0) + 1
        self.assertTrue(all(v <= S.PER_SUBSYSTEM for v in subs.values()))
        self.assertEqual(rep["selected_by_category"]["lock"], 1)                    # fewer eligible -> fewer shown
        self.assertEqual(rep["selected_by_category"]["hang"], 0)
        self.assertEqual(rep["unclassified_eligible"], 1)
        self.assertEqual(rep["ineligible"].get("no crash report"), 1)
        self.assertEqual(rep["ineligible"].get("fix changes more than 60 lines"), 1)
        self.assertEqual(rep["ineligible"].get("fix touches more than 3 files"), 1)
        self.assertEqual(set(local), {c["bug_id"] for c in a})

    def test_one_case_per_fix_commit(self):
        same = "f" * 40
        rows = [row(200 + k, f"WARNING in shared_{k}", fix_commit_hash=same,
                    final_patch_diff=fake_patch("x", files=((f"d{k}/x.c", 1, 0),))) for k in range(4)]
        cases, rep_, _ = S.select(rows)
        self.assertEqual(len(cases), 1)
        self.assertEqual(rep_["skipped_same_fix"], 3)
        self.assertEqual(cases[0]["fix"]["bugs_in_shard"], 4)

    def test_committed_tier_has_no_unclear_text_or_personal_data(self):
        cases, _, local = S.select(self.rows())
        blob = json.dumps(cases)
        for c in cases:
            self.assertEqual(S.validate_case(c), [], c["bug_id"])
        self.assertNotIn("synthetic commit message", blob)       # commit-message prose stays out
        self.assertNotIn("Read of size 8", blob)                 # crash-report text stays out
        self.assertNotIn("reproducer", blob)
        self.assertNotIn("int main", blob)
        self.assertNotIn("example.org", blob)
        some = next(iter(local.values()))
        self.assertIn("synthetic commit message", some["commit_message_excerpt"])   # local tier only
        self.assertNotIn("example.org", json.dumps(local))
        self.assertEqual(cases[0]["subsystem_source"], "derived from the files the fix changed")

    def test_validator_rejects_forbidden_content(self):
        c, _, _ = S.select([row(5, "WARNING in demo")])
        good = c[0]
        self.assertEqual(S.validate_case(good), [])
        bad = dict(good, c_reproducer="x")
        self.assertTrue(S.validate_case(bad))
        bad = json.loads(json.dumps(good))
        bad["fix"]["title"] = "mail me@example.org"
        self.assertIn("email address present", S.validate_case(bad))


class TestPrepareScript(unittest.TestCase):
    def test_refuses_other_filenames(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "train-00001-of-00011.parquet")
            open(p, "wb").close()
            r = subprocess.run([sys.executable, "scripts/prepare_syzfix_subset.py", "--input", p, "--verify-only"],
                               cwd=BASE, capture_output=True, text=True)
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("Refusing", r.stdout + r.stderr)

    def test_end_to_end_on_synthetic_parquet(self):
        try:
            import pyarrow as pa
            import pyarrow.parquet as pq
        except ImportError:
            self.skipTest("pyarrow not installed (only needed to build the subset)")
        with tempfile.TemporaryDirectory() as d:
            raw = os.path.join(d, "train-00000-of-00011.parquet")
            rows = TestSelection().rows()
            pq.write_table(pa.Table.from_pylist(rows), raw)
            r = subprocess.run([sys.executable, "scripts/prepare_syzfix_subset.py", "--input", raw, "--data-dir", d,
                                "--local-text", "--skip-published-check"], cwd=BASE, capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            r2 = subprocess.run([sys.executable, "scripts/prepare_syzfix_subset.py", "--input", raw, "--verify-only"],
                                cwd=BASE, capture_output=True, text=True)
            self.assertNotEqual(r2.returncode, 0)                       # a non-published file is refused
            self.assertIn("differ from the published shard", r2.stdout + r2.stderr)
            meta = json.load(open(os.path.join(d, "processed", "syzfix_cases.meta.json")))
            self.assertEqual(meta["input"]["rows"], len(rows))
            self.assertRegex(meta["input"]["sha256"], r"^[0-9a-f]{64}$")
            self.assertEqual(meta["source"]["shards_total"], 11)
            self.assertFalse(meta["input"]["matches_published"])
            cases = json.load(open(os.path.join(d, "processed", "syzfix_cases.json")))
            self.assertNotIn("int main", json.dumps(cases))
            self.assertTrue(os.path.isfile(os.path.join(d, "local", "syzfix_local_text.json")))


@unittest.skipUnless(os.path.isfile(os.path.join(BASE, "data", "processed", "syzfix_cases.json")),
                     "no prepared subset in data/processed (run scripts/prepare_syzfix_subset.py)")
class TestCommittedSubset(unittest.TestCase):
    """The real, committed subset: every record valid, provenance recorded, safeguards hold."""
    def setUp(self):
        d = os.path.join(BASE, "data", "processed")
        self.raw = open(os.path.join(d, "syzfix_cases.json"), encoding="utf-8").read()
        self.cases = json.loads(self.raw)
        self.meta = json.load(open(os.path.join(d, "syzfix_cases.meta.json"), encoding="utf-8"))

    def test_records_valid_and_provenance(self):
        self.assertTrue(self.cases)
        for c in self.cases:
            self.assertEqual(S.validate_case(c), [], c["bug_id"])
        self.assertTrue(self.meta["input"]["matches_published"])
        self.assertEqual(self.meta["input"]["sha256"], self.meta["source"]["published_sha256"])
        self.assertEqual(self.meta["source"]["shards_total"], 11)
        self.assertEqual(len(self.cases), self.meta["cases"])

    def test_caps_and_uniqueness(self):
        by_cat = {}
        for c in self.cases:
            by_cat[c["category"]] = by_cat.get(c["category"], 0) + 1
        self.assertTrue(all(v <= S.PER_CATEGORY for v in by_cat.values()))
        self.assertEqual(by_cat, {k: v for k, v in self.meta["report"]["selected_by_category"].items() if v})
        fixes = [c["fix"]["hash"] for c in self.cases]
        self.assertEqual(len(fixes), len(set(fixes)))
        self.assertEqual(len({c["bug_id"] for c in self.cases}), len(self.cases))

    def test_no_unclear_or_personal_content(self):
        self.assertNotRegex(self.raw, r"[\w.+-]+@[\w-]+\.[\w.-]+")
        for word in ("Signed-off-by", "Reported-by", "reproducer", "syz_reproducer", "patch_evolution"):
            self.assertNotIn(word, self.raw)

    def test_api_serves_all_records(self):
        from app import app
        d = app.test_client().get("/api/cases").get_json()
        self.assertTrue(d["available"])
        self.assertEqual(len(d["cases"]), len(self.cases))
        self.assertEqual(d["errors"], [])


class TestStoreAndPage(unittest.TestCase):
    def test_load_missing_and_invalid(self):
        orig = (store.CASES, store.META, store.LOCAL)
        with tempfile.TemporaryDirectory() as d:
            store.CASES, store.META, store.LOCAL = (os.path.join(d, n) for n in ("c.json", "m.json", "l.json"))
            try:
                self.assertFalse(store.load()["available"])
                good, _, _ = S.select([row(9, "WARNING in demo")])
                bad = dict(good[0], bug_id="nothex")
                json.dump(good + [bad], open(store.CASES, "w"))
                out = store.load()
                self.assertEqual(len(out["cases"]), 1)
                self.assertEqual(out["errors"][0]["bug_id"], "nothex")
                self.assertIsNone(out["cases"][0]["local_text"])
            finally:
                store.CASES, store.META, store.LOCAL = orig

    def test_page_and_api(self):
        from app import app
        c = app.test_client()
        r = c.get("/cases")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"Real records, not a simulation", r.data)
        self.assertIn(b"one of eleven", r.data)
        self.assertIn("available", c.get("/api/cases").get_json())

    def test_simulator_does_not_depend_on_case_studies(self):
        for d, _, fs in os.walk(os.path.join(BASE, "simulator")):
            for f in fs:
                if f.endswith(".py"):
                    self.assertNotIn("casestudies", open(os.path.join(d, f), encoding="utf-8").read(), f)

    def test_kernel_scenario_mapping_is_honest(self):
        self.assertEqual(store.KERNEL_SCENARIO_CATEGORY["null_deref"], "null")
        for k in ("kill_init", "oom_panic", "mce", "rootfs"):
            self.assertIsNone(store.KERNEL_SCENARIO_CATEGORY[k])


if __name__ == "__main__":
    unittest.main()
