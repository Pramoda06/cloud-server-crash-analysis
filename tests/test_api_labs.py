"""
API + page tests for the new labs (Deadlock, Recovery, Kernel) using the Flask test client
and an isolated temporary database.
"""
try:                                      # temporary database for the tests (must run before `import app`)
    from tests import _isolation  # noqa: F401
except ImportError:
    import _isolation  # noqa: F401
import os
import tempfile
import unittest

from app import app, configure_storage
from simulator import servers as server_catalog
from simulator.manager import ServerManager
from simulator.storage import Store


class TestLabApis(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.store = Store(os.path.join(cls.tmp.name, "t.db"), default_server=server_catalog.DEFAULT_SERVER)
        cls.manager = ServerManager(cls.store)
        cls.orig = configure_storage(cls.store, cls.manager)
        cls.c = app.test_client()

    @classmethod
    def tearDownClass(cls):
        configure_storage(*cls.orig)
        cls.tmp.cleanup()

    def setUp(self):
        self.c.post("/api/lab/reset")
        self.eng = next(iter(self.manager.engines.values()))
        self.eng.reset()

    def post(self, url, body=None):
        r = self.c.post(url, json=body or {})
        return r.status_code, r.get_json()

    def tick(self, n):
        for _ in range(n):
            self.eng.tick()

    def test_pages_render(self):
        for path in ("/", "/lab", "/incident", "/processes", "/deadlock", "/kernel", "/cases", "/history"):
            r = self.c.get(path)
            self.assertEqual(r.status_code, 200, path)
        for old, new in (("/monitor", "/lab"), ("/analysis", "/incident"), ("/recovery", "/incident#fix"), ("/experiments", "/lab")):
            r = self.c.get(old)                                   # v4: old URLs keep working as redirects
            self.assertEqual(r.status_code, 302, old)
            self.assertTrue(r.headers["Location"].endswith(new), (old, r.headers["Location"]))
        self.assertIn(b"Deadlock Lab", self.c.get("/deadlock").data)
        self.assertIn(b"Simulation only", self.c.get("/kernel").data)

    def test_meta_lists_strategies_and_scenarios(self):
        m = self.c.get("/api/lab/meta").get_json()
        self.assertTrue(any(s["id"] == "cpu_reduce" for s in m["strategies"]))
        self.assertTrue(any(s["id"] == "rootfs" for s in m["kernel_scenarios"]))
        self.assertIn("kernel", m["rules"])

    def test_deadlock_api_flow(self):
        code, r = self.post("/api/lab/action", {"action": "dl_add_resource", "rid": "R9", "instances": 2})
        self.assertEqual(code, 200, r)
        code, r = self.post("/api/lab/action", {"action": "dl_add_resource", "rid": "R9"})
        self.assertEqual(code, 409)                                # duplicate -> clear error
        self.assertIn("already exists", r["message"])
        code, _ = self.post("/api/lab/action", {"action": "dl_example", "which": "two"})
        self.assertEqual(code, 200)
        self.tick(4)
        st = self.c.get("/api/lab/state").get_json()
        self.assertEqual(sorted(st["deadlock"]["analysis"]["deadlocked"]), ["P1", "P2"])
        self.assertTrue(st["deadlock"]["coffman"]["circular_wait"])
        code, r = self.post("/api/lab/action", {"action": "resolve_deadlock", "how": "preempt"})
        self.assertEqual(code, 200, r)
        self.tick(2)
        st = self.c.get("/api/lab/state").get_json()
        self.assertEqual(st["deadlock"]["analysis"]["deadlocked"], [])

    def test_strategy_and_compare_api(self):
        self.post("/api/lab/stress", {"kind": "memory", "intensity": 95})
        self.tick(30)
        code, r = self.post("/api/lab/strategy", {"id": "mem_release"})
        self.assertEqual(code, 200, r)
        st = self.c.get("/api/lab/state?full=0").get_json()
        self.assertEqual(st["last_recovery"]["status"], "RECOVERING")
        self.assertEqual(st["last_recovery"]["strategy"]["id"], "mem_release")
        code, r = self.post("/api/lab/compare", {"scenario": "disk", "strategies": ["disk_reduce", "disk_drain"]})
        self.assertEqual(code, 200, r)
        self.assertEqual(r["result"]["best"], "disk_reduce")
        recs = self.c.get("/api/lab/records?kind=comparison").get_json()
        self.assertTrue(recs)
        one = self.c.get(f"/api/lab/records/{recs[0]['id']}").get_json()
        self.assertEqual(one["data"]["scenario"]["id"], "disk")
        code, r = self.post("/api/lab/compare", {"scenario": "disk", "strategies": []})
        self.assertEqual(code, 400)

    def test_clean_start_resets_to_seeded_state(self):
        self.post("/api/lab/controls", {"cpu_workload": 120})
        self.post("/api/lab/action", {"action": "dl_example", "which": "two"})
        self.tick(10)
        code, r = self.post("/api/lab/clean")
        self.assertEqual(code, 200, r)
        st = self.c.get("/api/lab/state").get_json()
        self.assertEqual(st["controls"]["cpu_workload"], 10)
        self.assertEqual(st["deadlock"]["analysis"]["deadlocked"], [])
        self.assertIsNone(st["last_recovery"])
        self.assertEqual(st["health"], "HEALTHY")

    def test_kernel_api(self):
        code, r = self.post("/api/lab/kernel", {"op": "trigger", "scenario": "kill_init"})
        self.assertEqual(code, 200, r)
        self.tick(8)
        st = self.c.get("/api/lab/state?full=0").get_json()
        self.assertEqual(st["kernel"]["stage"], "PANIC")
        self.assertEqual(st["health"], "FAILED")
        code, r = self.post("/api/lab/reset")                     # full recovery refused while panicked
        self.assertEqual(code, 409)
        code, r = self.post("/api/lab/kernel", {"op": "reset"})
        self.assertEqual(code, 200)
        self.assertEqual(self.c.get("/api/lab/state?full=0").get_json()["kernel"]["stage"], "RUNNING")
        code, r = self.post("/api/lab/kernel", {"op": "bogus"})
        self.assertEqual(code, 409)


if __name__ == "__main__":
    unittest.main()
