"""SQLite persistence (database/experiments.db).

Tables:
  servers      the simulated servers the operator has added (Server-01 is seeded once)
  experiments  every completed crash-analysis experiment, linked to its server
               through experiments.server_id; `mode` is preset / interactive and
               `max_intensity` the peak of the primary control (interactive only)
"""
import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime

SCHEMA = """
CREATE TABLE IF NOT EXISTS experiments (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp           TEXT NOT NULL,
    failure_type        TEXT NOT NULL,
    failure_name        TEXT NOT NULL,
    severity            TEXT NOT NULL,
    failure_injected    TEXT NOT NULL,
    failure_detected    TEXT,
    detection_match     INTEGER,
    injected_at         REAL,
    detected_at         REAL,
    detection_latency   REAL,
    max_health          TEXT,
    peak_cpu            REAL,
    peak_memory         REAL,
    peak_disk           REAL,
    peak_network_latency REAL,
    peak_response       REAL,
    peak_specific_name  TEXT,
    peak_specific_value REAL,
    recovery_started_at REAL,
    recovery_completed_at REAL,
    recovery_time       REAL,
    final_status        TEXT,
    analysis            TEXT,
    details_json        TEXT
);
"""

SERVERS_SCHEMA = """
CREATE TABLE IF NOT EXISTS servers (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL,
    server_type   TEXT NOT NULL,
    role          TEXT NOT NULL,
    os            TEXT NOT NULL,
    os_version    TEXT NOT NULL,
    cpu           INTEGER NOT NULL,
    ram_gb        INTEGER NOT NULL,
    storage_gb    INTEGER NOT NULL,
    storage_type  TEXT NOT NULL,
    filesystem    TEXT,
    network_type  TEXT,
    connection    TEXT NOT NULL DEFAULT 'Online',
    created_at    TEXT NOT NULL
);
"""

LAB_SCHEMA = """
CREATE TABLE IF NOT EXISTS lab_runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    server_id     INTEGER,
    created       TEXT NOT NULL,
    name          TEXT NOT NULL,
    kind          TEXT NOT NULL,          -- manual | program | replay
    duration_s    REAL,
    peak_health   TEXT,
    primary_failure TEXT,
    replay_of     INTEGER,
    summary_json  TEXT,
    data_json     TEXT                    -- program, config, log, samples, events, episodes
);
CREATE TABLE IF NOT EXISTS lab_snapshots (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    server_id INTEGER,
    created   TEXT NOT NULL,
    name      TEXT NOT NULL,
    data_json TEXT
);
CREATE TABLE IF NOT EXISTS lab_programs (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    created   TEXT NOT NULL,
    name      TEXT NOT NULL,
    data_json TEXT
);
"""

SERVER_COLS = ["name", "server_type", "role", "os", "os_version", "cpu", "ram_gb", "storage_gb",
               "storage_type", "filesystem", "network_type", "connection", "created_at"]


class Store:
    def __init__(self, path, default_server=None):
        self.path = path
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.lock = threading.Lock()
        with self._conn() as c:
            c.executescript(SCHEMA)
            c.executescript(SERVERS_SCHEMA)
            c.executescript(LAB_SCHEMA)
            # removing the last server is not allowed, so an empty table means "never seeded"
            had_servers = c.execute("SELECT COUNT(*) FROM servers").fetchone()[0] > 0
            cols = [r[1] for r in c.execute("PRAGMA table_info(experiments)")]
            if "server_id" not in cols:
                c.execute("ALTER TABLE experiments ADD COLUMN server_id INTEGER")
            # interactive (dynamic metric control) experiments: mode + peak control intensity
            if "mode" not in cols:
                c.execute("ALTER TABLE experiments ADD COLUMN mode TEXT DEFAULT 'preset'")
            if "max_intensity" not in cols:
                c.execute("ALTER TABLE experiments ADD COLUMN max_intensity REAL")
            if not had_servers and default_server:
                # first run with server management: seed the original Server-01 and
                # attach every experiment recorded before servers existed to it
                cur = c.execute(
                    f"INSERT INTO servers ({','.join(SERVER_COLS)}) VALUES ({','.join('?' * len(SERVER_COLS))})",
                    [default_server.get(k, datetime.now().strftime("%Y-%m-%d %H:%M:%S")) for k in SERVER_COLS])
                c.execute("UPDATE experiments SET server_id=? WHERE server_id IS NULL", (cur.lastrowid,))

    @contextmanager
    def _conn(self):
        c = sqlite3.connect(self.path, timeout=10)
        c.row_factory = sqlite3.Row
        try:
            yield c
            c.commit()
        finally:
            c.close()

    def save(self, rec, details):
        cols = list(rec.keys()) + ["details_json"]
        vals = list(rec.values()) + [json.dumps(details)]
        q = f"INSERT INTO experiments ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})"
        with self.lock, self._conn() as c:
            cur = c.execute(q, vals)
            return cur.lastrowid

    def next_id(self):
        """ID the next saved experiment will receive (shown live as the Experiment ID)."""
        with self._conn() as c:
            row = c.execute("SELECT MAX(id) FROM experiments").fetchone()
        return (row[0] or 0) + 1

    def list(self, limit=200, server_id=None):
        with self._conn() as c:
            if server_id is None:
                rows = c.execute(
                    "SELECT * FROM experiments ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
            else:
                rows = c.execute("SELECT * FROM experiments WHERE server_id=? ORDER BY id DESC LIMIT ?",
                                 (server_id, limit)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d.pop("details_json", None)
            out.append(d)
        return out

    def get(self, eid):
        with self._conn() as c:
            r = c.execute("SELECT * FROM experiments WHERE id=?", (eid,)).fetchone()
        if not r:
            return None
        d = dict(r)
        d["details"] = json.loads(d.pop("details_json") or "{}")
        return d

    def clear(self, server_id=None):
        with self.lock, self._conn() as c:
            if server_id is None:
                c.execute("DELETE FROM experiments")
                c.execute("DELETE FROM sqlite_sequence WHERE name='experiments'")
            else:
                c.execute("DELETE FROM experiments WHERE server_id=?", (server_id,))

    def count(self, server_id=None):
        with self._conn() as c:
            if server_id is None:
                return c.execute("SELECT COUNT(*) FROM experiments").fetchone()[0]
            return c.execute("SELECT COUNT(*) FROM experiments WHERE server_id=?", (server_id,)).fetchone()[0]

    # ------------------------------------------------------------ servers --
    def servers(self):
        with self._conn() as c:
            return [dict(r) for r in c.execute("SELECT * FROM servers ORDER BY id")]

    def server(self, sid):
        with self._conn() as c:
            r = c.execute("SELECT * FROM servers WHERE id=?", (sid,)).fetchone()
        return dict(r) if r else None

    def add_server(self, rec):
        with self.lock, self._conn() as c:
            cur = c.execute(
                f"INSERT INTO servers ({','.join(SERVER_COLS)}) VALUES ({','.join('?' * len(SERVER_COLS))})",
                [rec.get(k) for k in SERVER_COLS])
            sid = cur.lastrowid
        return self.server(sid)

    def delete_server(self, sid):
        """Remove a server together with its crash-analysis history."""
        with self.lock, self._conn() as c:
            n = c.execute("DELETE FROM experiments WHERE server_id=?", (sid,)).rowcount
            n += c.execute("DELETE FROM lab_runs WHERE server_id=?", (sid,)).rowcount
            c.execute("DELETE FROM lab_snapshots WHERE server_id=?", (sid,))
            c.execute("DELETE FROM servers WHERE id=?", (sid,))
        return n

    def last_per_server(self):
        """Latest experiment summary and experiment count for every server."""
        with self._conn() as c:
            rows = c.execute(
                "SELECT e.server_id, e.id, e.timestamp, e.failure_type, e.failure_name, e.severity, "
                "e.failure_detected, e.max_health, e.final_status, "
                "(SELECT COUNT(*) FROM experiments x WHERE x.server_id = e.server_id) AS n "
                "FROM experiments e WHERE e.id IN (SELECT MAX(id) FROM experiments GROUP BY server_id)"
            ).fetchall()
        return {r["server_id"]: dict(r) for r in rows}

    # ------------------------------------------------------- system lab --
    def save_lab_run(self, rec):
        data = {k: rec[k] for k in ("program", "config", "log", "samples", "events", "episodes")}
        with self.lock, self._conn() as c:
            cur = c.execute(
                "INSERT INTO lab_runs (server_id, created, name, kind, duration_s, peak_health, primary_failure, "
                "replay_of, summary_json, data_json) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (rec["server_id"], rec["created"], rec["name"], rec["kind"], rec["duration_s"], rec["peak_health"],
                 rec["primary_failure"], rec["replay_of"], json.dumps(rec["summary"]), json.dumps(data)))
            return cur.lastrowid

    def lab_runs(self, server_id=None, limit=200):
        q = "SELECT id, server_id, created, name, kind, duration_s, peak_health, primary_failure, replay_of, " \
            "summary_json FROM lab_runs"
        args = ()
        if server_id is not None:
            q += " WHERE server_id=?"
            args = (server_id,)
        with self._conn() as c:
            rows = c.execute(q + " ORDER BY id DESC LIMIT ?", args + (limit,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["summary"] = json.loads(d.pop("summary_json") or "{}")
            out.append(d)
        return out

    def lab_run(self, rid):
        with self._conn() as c:
            r = c.execute("SELECT * FROM lab_runs WHERE id=?", (rid,)).fetchone()
        if not r:
            return None
        d = dict(r)
        d["summary"] = json.loads(d.pop("summary_json") or "{}")
        d.update(json.loads(d.pop("data_json") or "{}"))
        return d

    def delete_lab_run(self, rid):
        with self.lock, self._conn() as c:
            return c.execute("DELETE FROM lab_runs WHERE id=?", (rid,)).rowcount

    def save_snapshot(self, snap):
        with self.lock, self._conn() as c:
            return c.execute("INSERT INTO lab_snapshots (server_id, created, name, data_json) VALUES (?,?,?,?)",
                             (snap["server_id"], snap["created"], snap["name"], json.dumps(snap["data"]))).lastrowid

    def snapshots(self, server_id=None):
        with self._conn() as c:
            rows = c.execute("SELECT * FROM lab_snapshots" + (" WHERE server_id=?" if server_id is not None else "")
                             + " ORDER BY id DESC", (server_id,) if server_id is not None else ()).fetchall()
        return [dict(dict(r), data=json.loads(r["data_json"] or "{}"), data_json=None) for r in rows]

    def delete_snapshot(self, sid):
        with self.lock, self._conn() as c:
            return c.execute("DELETE FROM lab_snapshots WHERE id=?", (sid,)).rowcount

    def save_program(self, name, prog):
        with self.lock, self._conn() as c:
            return c.execute("INSERT INTO lab_programs (created, name, data_json) VALUES (?,?,?)",
                             (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), name, json.dumps(prog))).lastrowid

    def programs(self):
        with self._conn() as c:
            rows = c.execute("SELECT * FROM lab_programs ORDER BY id DESC").fetchall()
        return [{"id": r["id"], "created": r["created"], "name": r["name"], "program": json.loads(r["data_json"])}
                for r in rows]

    def program(self, pid):
        with self._conn() as c:
            r = c.execute("SELECT * FROM lab_programs WHERE id=?", (pid,)).fetchone()
        return json.loads(r["data_json"]) if r else None

    def delete_program(self, pid):
        with self.lock, self._conn() as c:
            return c.execute("DELETE FROM lab_programs WHERE id=?", (pid,)).rowcount
