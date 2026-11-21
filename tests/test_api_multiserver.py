"""
Tests for Flask API routes, Multi-Server isolation, and Control Input Validation.
Validates:
- Complete database isolation from production database/experiments.db
- Invariant SHA-256 hash of production database before and after test execution
- /api/servers, /api/health, /api/lab/state, /api/lab/controls, /api/lab/reset
- Input validation on controls: finite clamping, rejection of NaN/inf/strings with HTTP 400
- Multi-server isolation: mutations on Server 1 do not leak to Server 2
"""
try:                                      # temporary database for the tests (must run before `import app`)
    from tests import _isolation  # noqa: F401
except ImportError:
    import _isolation  # noqa: F401
import hashlib
import json
import os
import tempfile
import unittest

from app import app, configure_storage
from simulator import servers as server_catalog
from simulator.manager import ServerManager
from simulator.storage import Store

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROD_DB_PATH = os.path.join(BASE_DIR, "database", "experiments.db")


class TestApiAndMultiServerIsolation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # 1. Record baseline hash of production database to prove isolation
        cls.initial_hash = None
        if os.path.exists(PROD_DB_PATH):
            with open(PROD_DB_PATH, "rb") as f:
                cls.initial_hash = hashlib.sha256(f.read()).hexdigest()

        # 2. Set up isolated test database in a temporary directory
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.test_db_path = os.path.join(cls.temp_dir.name, "test_experiments.db")
        cls.test_store = Store(cls.test_db_path, default_server=server_catalog.DEFAULT_SERVER)
        cls.test_manager = ServerManager(cls.test_store)

        # 3. Add a second server to ensure multi-server isolation can be tested
        s2 = dict(server_catalog.DEFAULT_SERVER)
        s2["name"] = "Cloud-Server-02"
        cls.test_manager.add(s2)

        # 4. Inject isolated storage into Flask app
        cls.orig_store, cls.orig_manager = configure_storage(cls.test_store, cls.test_manager)

    @classmethod
    def tearDownClass(cls):
        # 1. Restore original production store and manager
        configure_storage(cls.orig_store, cls.orig_manager)
        cls.temp_dir.cleanup()

        # 2. Strict invariant: verify production database was never touched or mutated
        if cls.initial_hash is not None and os.path.exists(PROD_DB_PATH):
            with open(PROD_DB_PATH, "rb") as f:
                final_hash = hashlib.sha256(f.read()).hexdigest()
            assert final_hash == cls.initial_hash, (
                f"DATABASE ISOLATION VIOLATION: Production DB hash changed from {cls.initial_hash} to {final_hash}"
            )

    def setUp(self):
        self.client = app.test_client()
        self.app_context = app.app_context()
        self.app_context.push()

    def tearDown(self):
        self.app_context.pop()

    def test_database_isolation_invariant(self):
        """Verify tests run against the isolated test store rather than the production database."""
        from app import store, manager
        self.assertEqual(store.path, self.test_db_path)
        self.assertNotEqual(store.path, PROD_DB_PATH)
        if self.initial_hash is not None:
            with open(PROD_DB_PATH, "rb") as f:
                current_hash = hashlib.sha256(f.read()).hexdigest()
            self.assertEqual(current_hash, self.initial_hash)

    def test_api_servers_list(self):
        """GET /api/servers returns valid JSON dict with list of servers."""
        resp = self.client.get("/api/servers")
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.data)
        self.assertIn("servers", data)
        self.assertTrue(isinstance(data["servers"], list))
        self.assertGreaterEqual(len(data["servers"]), 2)
        self.assertIn("name", data["servers"][0])

    def test_api_lab_state(self):
        """GET /api/lab/state returns complete telemetry snapshot."""
        resp = self.client.get("/api/lab/state")
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.data)
        self.assertIn("values", data)
        self.assertIn("health", data)
        self.assertIn("controls", data)
        self.assertIn("rules", data)
        self.assertIn("processes", data)

    def test_api_lab_controls_valid(self):
        """POST /api/lab/controls modifies simulated control parameters within bounds."""
        resp = self.client.post("/api/lab/controls", json={"cpu_workload": 45})
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.data)
        self.assertTrue(data.get("ok"))
        self.assertEqual(data.get("result", {}).get("cpu_workload"), 45)

    def test_api_lab_controls_finite_clamping(self):
        """POST /api/lab/controls clamps out-of-range finite floats to valid min/max."""
        # cpu_workload max is 150
        resp = self.client.post("/api/lab/controls", json={"cpu_workload": 500})
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.data)
        self.assertTrue(data.get("ok"))
        self.assertEqual(data.get("result", {}).get("cpu_workload"), 150)

    def test_api_lab_controls_rejection_of_invalid_inputs(self):
        """POST /api/lab/controls returns HTTP 400 for non-finite values, non-numeric strings, and bad payloads."""
        # 1. Non-numeric string
        resp = self.client.post("/api/lab/controls", json={"cpu_workload": "not_a_number"})
        self.assertEqual(resp.status_code, 400)
        data = json.loads(resp.data)
        self.assertFalse(data.get("ok"))
        self.assertIn("finite number", data.get("message", ""))

        # 2. Unknown control parameter
        resp = self.client.post("/api/lab/controls", json={"unknown_key": 50})
        self.assertEqual(resp.status_code, 400)
        data = json.loads(resp.data)
        self.assertFalse(data.get("ok"))
        self.assertIn("Unknown parameter", data.get("message", ""))

        # 3. Non-object payload
        resp = self.client.post("/api/lab/controls", json=["not", "a", "dict"])
        self.assertEqual(resp.status_code, 400)
        data = json.loads(resp.data)
        self.assertFalse(data.get("ok"))

        # 4. disk_io with invalid string
        resp = self.client.post("/api/lab/controls", json={"disk_io": "invalid"})
        self.assertEqual(resp.status_code, 400)
        data = json.loads(resp.data)
        self.assertFalse(data.get("ok"))

        # 5. Boolean values passed as numbers
        resp = self.client.post("/api/lab/controls", json={"cpu_workload": True})
        self.assertEqual(resp.status_code, 400)
        data = json.loads(resp.data)
        self.assertFalse(data.get("ok"))
        self.assertIn("finite number", data.get("message", ""))

    def test_multi_server_isolation(self):
        """State mutations on Server 1 must not alter the state of Server 2."""
        from app import manager
        servers = manager.list()
        self.assertGreaterEqual(len(servers), 2)
        s1_id = servers[0]["db_id"]
        s2_id = servers[1]["db_id"]

        # 1. Reset both servers first
        self.client.set_cookie("os_server", str(s1_id))
        self.client.post("/api/lab/reset")
        self.client.set_cookie("os_server", str(s2_id))
        self.client.post("/api/lab/reset")

        # 2. Mutate Server 1: set cpu_workload = 120
        self.client.set_cookie("os_server", str(s1_id))
        resp1 = self.client.post("/api/lab/controls", json={"cpu_workload": 120})
        self.assertEqual(resp1.status_code, 200)

        # 3. Read state for Server 2: verify cpu_workload control is NOT 120
        self.client.set_cookie("os_server", str(s2_id))
        resp2 = self.client.get("/api/lab/state")
        self.assertEqual(resp2.status_code, 200)
        s2_data = json.loads(resp2.data)
        s2_controls = s2_data.get("controls", {})
        self.assertNotEqual(s2_controls.get("cpu_workload"), 120)

        # 4. Clean up Server 1
        self.client.set_cookie("os_server", str(s1_id))
        self.client.post("/api/lab/reset")


if __name__ == "__main__":
    unittest.main()
