"""
ServerManager: one monitoring Engine per simulated server.

* Servers are stored in SQLite (servers table); one Engine is created per server.
* A single background thread ticks every engine every TICK_SECONDS, sharing one
  real-host (psutil) sample, so adding servers never adds threads.
* Each engine keeps its own process table, detector, samples, events and
  experiment, so analyses of different servers never mix.
"""
import threading
import time

from . import config
from .lab.engine import LabEngine
from .metrics import HostMonitor
from .servers import build_spec, validate


class ServerManager:
    def __init__(self, store):
        self.store = store
        self.lock = threading.RLock()
        self.host = HostMonitor()
        self.engines = {}
        self._last_planned = 0
        self._thread = None
        self._running = False
        for row in store.servers():
            self._add_engine(row)

    # ---------------------------------------------------------- engines --
    def _add_engine(self, row):
        eng = LabEngine(self.store, build_spec(row), host=self.host)
        self.engines[row["id"]] = eng
        return eng

    def allocate_id(self):
        """Experiment ID shown live; unique even when several servers run experiments at once."""
        with self.lock:
            n = max(self.store.next_id(), self._last_planned + 1)
            self._last_planned = n
            return n

    def get(self, sid):
        return self.engines.get(sid)

    def default_id(self):
        return min(self.engines) if self.engines else None

    def resolve(self, sid):
        """Selected server id -> engine (falls back to the first server)."""
        with self.lock:
            if sid in self.engines:
                return self.engines[sid]
            d = self.default_id()
            return self.engines[d] if d is not None else None

    # ---------------------------------------------------------- servers --
    def list(self):
        out = []
        with self.lock:
            items = sorted(self.engines.items())
        for sid, eng in items:
            runs = self.store.lab_runs(server_id=sid, limit=500)
            spec = eng.public_server()
            out.append(dict(spec, **eng.live_summary(), status=eng.display_health(), phase=eng.phase,
                            experiment=(eng.runner or {}).get("name") or ((eng.recording or {}).get("name")),
                            last_run=runs[0] if runs else None, runs=len(runs)))
        return out

    def add(self, body):
        names = [e.server["name"] for e in self.engines.values()]
        rec, errors = validate(body, names)
        if errors:
            return None, errors
        row = self.store.add_server(rec)
        with self.lock:
            eng = self._add_engine(row)
        return eng.public_server(), None

    def remove(self, sid):
        with self.lock:
            eng = self.engines.get(sid)
            if not eng:
                return False, "Server not found", 404
            if len(self.engines) <= 1:
                return False, "At least one server is needed — add another server before removing this one.", 409
            if eng.phase != "IDLE":
                return False, (f"An experiment or recording is running on {eng.server['id']}. "
                               f"Stop it in the System Lab before removing the server."), 409
            del self.engines[sid]
        removed = self.store.delete_server(sid)
        return True, f"{eng.server['id']} removed together with {removed} analysis record(s)", 200

    # ----------------------------------------------------------- thread --
    def start(self):
        if self._thread:
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, name="monitor", daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False

    def _loop(self):
        nxt = time.monotonic()
        while self._running:
            try:
                host = self.host.sample()
            except Exception:
                host = {"available": False}
            with self.lock:
                engines = list(self.engines.values())
            for eng in engines:
                try:
                    with eng.lock:
                        eng.tick(host)
                except Exception as ex:  # never let the monitor die during a demo
                    import traceback
                    traceback.print_exc()
                    eng.emit("SYSTEM", f"Internal simulator error handled: {ex}", "warn")
            nxt += config.TICK_SECONDS
            delay = nxt - time.monotonic()
            if delay < -1:
                nxt = time.monotonic()
                delay = 0
            time.sleep(max(0.0, delay))
