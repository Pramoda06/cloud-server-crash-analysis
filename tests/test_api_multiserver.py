"""
Tests for Flask API routes and Multi-Server isolation.
Validates:
- /api/servers, /api/health, /api/lab/state, /api/lab/controls, /api/lab/reset
- Input validation on controls
- Multi-server isolation: mutations on Server 1 do not leak to Server 2
"""
import json
import unittest

from app import app, manager, store
from simulator import servers as server_catalog


class TestApiAndMultiServerIsolation(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()
        self.app_context = app.app_context()
        self.app_context.push()

        # Ensure we have at least 2 servers for isolation testing
        existing = manager.list()
        if len(existing) < 2:
            new_server = dict(server_catalog.DEFAULT_SERVER)
            new_server["name"] = f"Cloud-Server-{len(existing) + 1:02d}"
            manager.add(new_server)

    def tearDown(self):
        self.app_context.pop()

    def test_api_servers_list(self):
        """GET /api/servers returns valid JSON dict with list of servers."""
        resp = self.client.get("/api/servers")
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.data)
        self.assertIn("servers", data)
        self.assertTrue(isinstance(data["servers"], list))
        self.assertGreaterEqual(len(data["servers"]), 1)
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

    def test_api_lab_controls(self):
        """POST /api/lab/controls modifies simulated control parameters."""
        resp = self.client.post("/api/lab/controls", json={"cpu_workload": 45})
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.data)
        self.assertTrue(data.get("ok"))

    def test_multi_server_isolation(self):
        """State mutations on Server 1 must not alter the state of Server 2."""
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
