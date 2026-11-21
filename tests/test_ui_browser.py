"""
Browser tests (Playwright + Chromium). Skipped automatically when Playwright or a browser is
not installed:   pip install playwright && python -m playwright install chromium

Starts app.py on a free port with a temporary database, then drives the real pages.
"""
try:                                      # temporary database for the tests (must run before `import app`)
    from tests import _isolation  # noqa: F401
except ImportError:
    import _isolation  # noqa: F401
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request

try:
    from playwright.sync_api import sync_playwright
except Exception:          # pragma: no cover
    sync_playwright = None

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@unittest.skipIf(sync_playwright is None, "playwright not installed")
class TestBrowser(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.port = free_port()
        env = dict(os.environ, OS_EXPERIMENTS_DB=os.path.join(cls.tmp.name, "ui.db"))
        cls.proc = subprocess.Popen([sys.executable, "app.py", "--no-browser", "--port", str(cls.port)], cwd=BASE,
                                    env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        cls.url = f"http://127.0.0.1:{cls.port}"
        for _ in range(60):
            try:
                urllib.request.urlopen(cls.url + "/api/health", timeout=1)
                break
            except Exception:
                time.sleep(0.25)
        try:
            cls.pw = sync_playwright().start()
            cls.browser = cls.pw.chromium.launch()
        except Exception as ex:      # pragma: no cover
            cls.proc.terminate()
            raise unittest.SkipTest(f"no browser available: {ex}")

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()
        cls.proc.terminate()
        cls.proc.wait(5)
        cls.tmp.cleanup()

    def page(self, path, width=1440):
        pg = self.browser.new_page(viewport={"width": width, "height": 900})
        self.errors = []
        pg.on("pageerror", lambda e: self.errors.append(str(e)))
        pg.on("console", lambda m: self.errors.append(m.text) if m.type == "error" else None)
        pg.goto(self.url + path)
        pg.wait_for_timeout(900)
        return pg

    def test_navigation_and_no_js_errors(self):
        for path in ("/", "/lab", "/incident", "/processes", "/deadlock", "/kernel", "/history",
                     "/recovery", "/analysis", "/monitor", "/experiments"):     # the last four are old URLs (redirects)
            pg = self.page(path)
            self.assertEqual(self.errors, [], path)
            self.assertTrue(pg.locator("nav.nav a.active").count() >= 1 or path == "/", path)
            pg.close()

    def test_deadlock_dynamic_graph(self):
        pg = self.page("/deadlock")
        pg.click("text=Reset lab")
        pg.fill("#pName", "P9")
        pg.click("#fProc button[type=submit]")
        pg.fill("#rId", "Disk")
        pg.fill("#rInst", "2")
        pg.click("#fRes button[type=submit]")
        pg.wait_for_timeout(1200)
        self.assertEqual(pg.locator("svg.rag2 g.pn").count(), 4)          # nodes come from the backend state
        self.assertEqual(pg.locator("svg.rag2 g.rn").count(), 4)
        pg.click("text=Two processes, two locks")
        pg.wait_for_timeout(2500)
        self.assertIn("DEADLOCK", pg.locator("#dlVerdict").inner_text())
        self.assertGreaterEqual(pg.locator("svg.rag2 g.pn.dead").count(), 2)
        pg.click("#recover button[data-a=victim]")
        pg.wait_for_timeout(1500)
        self.assertIn("no deadlock", pg.locator("#dlVerdict").inner_text())
        self.assertEqual(self.errors, [])

    def test_kernel_lab_lifecycle_updates(self):
        pg = self.page("/kernel")
        pg.click("#btnReset")
        pg.click("#btnStart")
        pg.wait_for_timeout(3500)
        self.assertIn("Kernel panic", pg.locator("#console").inner_text())
        self.assertTrue(pg.locator("#btnStart").is_disabled())
        self.assertEqual(self.errors, [])
        pg.click("#btnReset")

    def test_system_lab_help_toggle_and_stress_line(self):
        import json
        pg = self.page("/lab")
        help_el = pg.locator("#controls .lctl-help").first
        self.assertFalse(help_el.is_visible())                    # quiet by default
        pg.check("#helpOn")
        self.assertTrue(help_el.is_visible())
        pg.uncheck("#helpOn")
        self.assertFalse(pg.locator("#stressOn").is_visible())    # no stress → no stress line
        req = urllib.request.Request(self.url + "/api/lab/stress", data=json.dumps({"kind": "network", "intensity": 60}).encode(),
                                     headers={"content-type": "application/json"})
        urllib.request.urlopen(req)
        pg.wait_for_selector("#stressOn:visible", timeout=5000)
        self.assertIn("holding the controls", pg.inner_text("#stressOn"))
        pg.click("#stressOn [data-unstress]")                      # the line removes its own stress
        pg.wait_for_selector("#stressOn", state="hidden", timeout=5000)
        self.assertEqual(self.errors, [])

    def test_process_table_groups_collapse(self):
        pg = self.page("/processes")
        pg.wait_for_selector("table.procs2")
        self.assertEqual(pg.locator("table.procs2 tbody tr:has-text('batch-job')").count(), 0)   # collapsed
        pg.click("table.procs2 button[data-g=batch]")
        pg.wait_for_timeout(300)
        self.assertGreater(pg.locator("table.procs2 tbody tr:has-text('batch-job')").count(), 0)
        pg.wait_for_timeout(1500)                                   # the group stays open across live re-renders
        self.assertGreater(pg.locator("table.procs2 tbody tr:has-text('batch-job')").count(), 0)
        self.assertEqual(pg.locator("label.notes-switch").count(), 0)          # v4: no teaching-notes switch
        pg.click("text=Kill vs Crash vs Hang")
        self.assertTrue(pg.locator("details.learn:has-text('Kill vs Crash vs Hang')").evaluate("d => d.open"))
        self.assertEqual(self.errors, [])

    def test_inapplicable_strategy_disabled_and_kernel_pick_synced(self):
        import json
        def post(path, body):
            req = urllib.request.Request(self.url + path, data=json.dumps(body).encode(), headers={"content-type": "application/json"})
            return json.load(urllib.request.urlopen(req))
        post("/api/lab/clean", {})
        post("/api/lab/stress", {"kind": "memory", "intensity": 90})
        pg = self.page("/incident")
        pg.wait_for_selector("button[data-s=mem_release]", timeout=15000)
        self.assertTrue(pg.locator("button[data-s=mem_restart_leak]").is_disabled())     # no leaking process
        self.assertFalse(pg.locator("button[data-s=mem_release]").is_disabled())
        post("/api/lab/clean", {})
        post("/api/lab/kernel", {"op": "trigger", "scenario": "mce"})
        k = self.page("/kernel")
        k.wait_for_timeout(1200)
        self.assertEqual(k.locator(".kcard.sel").get_attribute("data-s"), "mce")
        post("/api/lab/kernel", {"op": "reset"})
        self.assertEqual(self.errors, [])

    def test_hang_button_and_model_switches(self):
        import json
        urllib.request.urlopen(urllib.request.Request(self.url + "/api/lab/clean", data=b"{}", headers={"content-type": "application/json"}))
        pg = self.page("/processes")
        pg.wait_for_selector("table.procs2 button[data-a=hang]")
        pg.locator("table.procs2 tr:has-text('web-worker-3') button[data-a=hang]").click()
        pg.wait_for_timeout(1500)
        self.assertIn("HUNG", pg.locator("table.procs2 tr:has-text('web-worker-3')").inner_text())
        lab = self.page("/lab")
        lab.click(".mode-toggle button[data-mode=advanced]")
        self.assertTrue(lab.locator("#setAnchor").is_visible())
        self.assertTrue(lab.locator("#setCascade").is_checked())
        lab.click(".mode-toggle button[data-mode=basic]")
        self.assertEqual(self.errors, [])

    def test_recovery_comparison_and_mobile_layout(self):
        pg = self.page("/incident#compare")                         # opens the Compare strategies box
        self.assertTrue(pg.locator("#cmpBox").evaluate("d => d.open"))
        pg.click("#btnCmp")
        pg.wait_for_selector("table.cmp", timeout=10000)
        self.assertGreaterEqual(pg.locator("table.cmp tbody tr").count(), 2)
        self.assertEqual(self.errors, [])
        m = self.page("/deadlock", width=390)
        overflow = m.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth")
        self.assertLessEqual(overflow, 1)


    # ---------------------------------------------------------------- v4 layout
    def test_five_sections_and_lab_tabs(self):
        pg = self.page("/")
        self.assertEqual([t.strip() for t in pg.locator("nav.nav a").all_inner_texts()],
                         ["Overview", "Control Room", "Incident", "Labs", "History"])
        expect = {"/lab": ("Control Room", 0), "/incident": ("Incident", 0),
                  "/processes": ("Labs", 3), "/deadlock": ("Labs", 3), "/kernel": ("Labs", 3), "/history": ("History", 0)}
        for path, (section, tabs) in expect.items():
            pg = self.page(path)
            self.assertEqual(pg.inner_text("nav.nav a.active").strip(), section, path)
            self.assertEqual(pg.locator(".section-tabs a").count(), tabs, path)     # only the labs share a tab bar
            if tabs:
                self.assertEqual(pg.locator(".section-tabs a.sel").get_attribute("href"), path, path)
            self.assertEqual(self.errors, [], path)
            pg.close()

    def test_old_urls_redirect(self):
        for old, new in (("/monitor", "/lab"), ("/analysis", "/incident"), ("/recovery", "/incident"), ("/experiments", "/lab")):
            pg = self.page(old)
            self.assertTrue(pg.url.split("#")[0].rstrip("/").endswith(new), (old, pg.url))
            pg.close()

    def test_injected_failure_breaks_the_server_and_overview_explains_it(self):
        pg = self.page("/lab")
        self.assertEqual(pg.locator("#injSel option").count(), 12)
        pg.select_option("#injSel", "cpu")
        self.assertIn("CPU scheduling", pg.inner_text("#injWhat"))
        pg.click("#btnInject")
        pg.wait_for_selector("#whyLink:visible", timeout=20000)               # the live view points to the incident
        self.assertTrue(pg.locator("#navLive").is_visible())
        ov = self.page("/")
        ov.wait_for_selector("#dhBig.FAILED, #dhBig.CRITICAL", timeout=20000)
        self.assertIn("CPU saturation", ov.inner_text("#dhWhy"))
        self.assertEqual(ov.locator("#scenGrid").count(), 0)                   # the Overview does not ask to run failures
        ov.click("#btnDashClean")
        ov.wait_for_selector("#dhBig.HEALTHY", timeout=20000)
        self.assertEqual(self.errors, [])

    def test_incident_page_walks_from_cause_to_verified_fix(self):
        import json
        def post(path, body):
            req = urllib.request.Request(self.url + path, data=json.dumps(body).encode(), headers={"content-type": "application/json"})
            return json.load(urllib.request.urlopen(req))
        post("/api/lab/clean", {})
        pg = self.page("/incident")
        pg.wait_for_selector("#calm:visible", timeout=5000)                    # nothing wrong → a calm card, no steps
        post("/api/lab/stress", {"kind": "cpu", "intensity": 90})
        pg.wait_for_selector("#report .rca-story", timeout=20000)              # step 2: why (updates live)
        pg.wait_for_selector("#report .summary-strip:has-text('CPU saturation')", timeout=20000)
        self.assertEqual(pg.locator("#report h2:text-is('Verification')").count(), 0)   # compact report: no duplicate fix cards
        pg.wait_for_selector("#strats button[data-s]:not([disabled])", timeout=10000)
        pg.locator("#strats button[data-s]:not([disabled])").first.click()     # step 3: fix
        pg.wait_for_selector("#result :text('RECOVERED'), #result :text('PARTIAL'), #result :text('FAILED')", timeout=40000)
        self.assertEqual(self.errors, [])
        post("/api/lab/clean", {})

    def test_control_room_tools_are_plain_buttons_and_labs_are_uncluttered(self):
        pg = self.page("/lab")
        for sel in ("[data-rec=reset]", "[data-rec=now]", "#btnSnap", "#btnPause"):
            self.assertTrue(pg.locator(sel).is_visible(), sel)                 # not hidden in a menu
        self.assertEqual(pg.locator("#recMenu").count(), 0)
        for path in ("/kernel", "/deadlock", "/processes"):
            q = self.page(path)
            self.assertEqual(q.locator(".real-slot, .real-card").count(), 0, path)
            q.close()
        k = self.page("/kernel")                                               # the failure-type table is always visible
        self.assertTrue(k.locator("table.ftypes").is_visible())
        self.assertEqual(self.errors, [])

    def test_cascade_is_a_tree(self):
        import json
        def post(path, body):
            req = urllib.request.Request(self.url + path, data=json.dumps(body).encode(), headers={"content-type": "application/json"})
            return json.load(urllib.request.urlopen(req))
        post("/api/lab/clean", {})
        post("/api/lab/stress", {"kind": "memory", "intensity": 90})
        pg = self.page("/incident")
        pg.wait_for_selector(".casc .casc-list li", timeout=25000)
        self.assertIn("Memory", pg.inner_text(".casc-root"))
        self.assertGreaterEqual(pg.locator(".casc-list li .casc-why").count(), 1)   # every effect carries its reason
        self.assertEqual(self.errors, [])
        post("/api/lab/clean", {})

    def test_deadlock_table_updates_after_release(self):
        """Regression: after clicking Release the process table must show the new states at once
        (it used to stay frozen because the clicked button kept focus)."""
        import json
        def post(path, body):
            req = urllib.request.Request(self.url + path, data=json.dumps(body).encode(), headers={"content-type": "application/json"})
            return json.load(urllib.request.urlopen(req))
        post("/api/lab/clean", {})
        post("/api/lab/action", {"action": "dl_reset"})
        for t, r in (("txn P1", "A"), ("txn P2", "B"), ("txn P3", "C"), ("txn P1", "B"), ("txn P2", "C")):
            post("/api/lab/action", {"action": "acquire", "target": t, "rid": r, "count": 1})
        pg = self.page("/deadlock")
        row = lambda n: pg.locator("#ptab tbody tr").filter(has=pg.locator("td:first-child", has_text=n)).first
        pg.wait_for_selector("#ptab tr:has-text('BLOCKED')")
        self.assertIn("BLOCKED", row("P2").inner_text())
        row("P3").locator("button[data-a=rel]").click()                 # mouse stays over the table
        pg.wait_for_function("() => [...document.querySelectorAll('#ptab tbody tr')].some(r => /P2/.test(r.cells[0].innerText) && /RUNNING/.test(r.cells[1].innerText))", timeout=4000)
        row("P2").locator("button[data-a=rel]").click()
        pg.wait_for_function("() => ![...document.querySelectorAll('#ptab tr')].some(r => /BLOCKED/.test(r.innerText))", timeout=4000)
        self.assertEqual(self.errors, [])
        post("/api/lab/action", {"action": "dl_reset"})

    def test_control_room_has_no_dependency_graph(self):
        pg = self.page("/lab")
        pg.click(".mode-toggle button[data-mode=advanced]")
        self.assertEqual(pg.locator("#depGraph").count(), 0)
        pg.click(".mode-toggle button[data-mode=basic]")
        self.assertEqual(self.errors, [])

    def test_guided_demo_waits_for_real_detection(self):
        pg = self.page("/")
        pg.click("#btnDemo")
        pg.click('#demoPick [data-pick="cpu"]')
        pg.wait_for_selector("#coach h3:has-text('A healthy server')")
        pg.click('#coach [data-d="next"]')
        pg.wait_for_selector('#coach [data-d="next"][disabled]')            # waiting for the detector
        pg.wait_for_selector('#coach [data-d="next"]:not([disabled])', timeout=20000)
        self.assertIn("Detected", pg.inner_text("#coach .wait"))
        pg.click('#coach [data-d="next"]')                                  # next step is on another page
        pg.wait_for_url("**/lab")
        pg.wait_for_selector("#coach h3:has-text('Watch it spread')")
        pg.click('#coach [data-d="exit"]')
        self.assertEqual(pg.locator("#coach").count(), 0)
        pg.reload()
        pg.wait_for_timeout(800)
        self.assertEqual(pg.locator("#coach").count(), 0)                  # exit is remembered
        self.assertEqual(self.errors, [])


if __name__ == "__main__":
    unittest.main()
