"""
Cloud Server Crash Analysis: An Operating System Perspective
------------------------------------------------------------
An interactive OS / cloud failure laboratory.  Run:   python app.py   then open http://127.0.0.1:5000

One LabEngine per virtual server (simulator/lab/) is the single source of truth: every page
reads the same simulated state through /api/lab/*.
"""
import argparse
import logging
import os
import sys
import threading
import webbrowser

from flask import Flask, abort, jsonify, redirect, render_template, request

from simulator import config
from simulator import servers as server_catalog
from simulator.lab import programs
from simulator.manager import ServerManager
from simulator.metrics import METRICS
from simulator.scenarios import SEVERITY_INFO, public_catalog
from simulator.storage import Store

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "database", "experiments.db")

app = Flask(__name__)
app.config["JSON_SORT_KEYS"] = False
store = Store(DB_PATH, default_server=server_catalog.DEFAULT_SERVER)
manager = ServerManager(store)
SERVER_COOKIE = "os_server"


def current():
    """Engine of the server selected in the browser (cookie), else the first server."""
    return manager.resolve(request.cookies.get(SERVER_COOKIE, type=int))


@app.after_request
def no_cache(resp):
    if request.path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-store"
    return resp


def body():
    return request.get_json(silent=True) or {}


def reply(ok, val, code_fail=409):
    if ok:
        return jsonify({"ok": True, "result": val})
    return jsonify({"ok": False, "message": val}), code_fail


# ------------------------------------------------------------------ pages --
#   Overview          what is happening?            System Lab        control the server
#   Live Monitoring   observe metrics                Failure Analysis  why did it fail?
#   Process & Resource Lab                          Experiment History review runs
def page(template, name, **kw):
    eng = current()
    return render_template(template, page=name, cfg=config, server=eng.public_server(),
                           servers=[e.public_server() for _, e in sorted(manager.engines.items())], **kw)


@app.route("/")
def overview():
    return page("overview.html", "overview", server_rows=manager.list())


@app.route("/lab")
def lab():
    return page("lab.html", "lab")


@app.route("/monitor")
def monitor():
    return page("monitor.html", "monitor")


@app.route("/analysis")
def analysis():
    return page("analysis.html", "analysis", rid=request.args.get("run", type=int))


@app.route("/processes")
def processes():
    return page("processes.html", "processes")


@app.route("/builder")
@app.route("/run")
def builder_redirect():
    return redirect("/lab")


@app.route("/history")
def history():
    return page("history.html", "history")


@app.route("/runs/<int:rid>")
def run_details(rid):
    if not store.lab_run(rid):
        abort(404)
    return page("run.html", "history", rid=rid)


@app.route("/experiment/<int:eid>")
def experiment(eid):                # experiments recorded by the previous version (read-only)
    if not store.get(eid):
        abort(404)
    return page("experiment.html", "history", eid=eid)


@app.errorhandler(404)
def not_found(e):
    return page("404.html", ""), 404


# ---------------------------------------------------------------- lab api --
@app.route("/api/lab/meta")
def api_lab_meta():
    eng = current()
    m = eng.meta()
    m["server_options"] = server_catalog.options()
    return jsonify(m)


@app.route("/api/lab/state")
def api_lab_state():
    a = request.args
    return jsonify(current().state(a.get("since", 0, type=int), a.get("esince", 0, type=int),
                                   a.get("epoch", 0, type=int), full=a.get("full", "1") != "0"))


@app.route("/api/lab/controls", methods=["POST"])
def api_lab_controls():
    return reply(*current().set_controls(body()))


@app.route("/api/lab/action", methods=["POST"])
def api_lab_action():
    return reply(*current().action(body()))


@app.route("/api/lab/stress", methods=["POST"])
def api_lab_stress():
    b = body()
    if b.get("clear"):
        return reply(*current().clear_stress())
    try:
        return reply(*current().inject_stress(b.get("kind"), b.get("intensity", 50), b.get("hold_s", 0) or 0))
    except (TypeError, ValueError) as ex:
        return reply(False, str(ex))


@app.route("/api/lab/settings", methods=["POST"])
def api_lab_settings():
    return reply(*current().settings(body()))


@app.route("/api/lab/thresholds", methods=["POST"])
def api_lab_thresholds():
    return reply(*current().set_threshold(body()))


@app.route("/api/lab/reset", methods=["POST"])
def api_lab_reset():
    return reply(*current().recover())


@app.route("/api/lab/pause", methods=["POST"])
def api_lab_pause():
    return reply(*current().pause(body().get("paused", True)))


@app.route("/api/lab/trace")
def api_lab_trace():
    t = current().trace(request.args.get("node", "response_ms"), request.args.get("ref", "baseline"))
    return jsonify(t) if t else (jsonify({"error": "unknown metric"}), 404)


@app.route("/api/lab/record", methods=["POST"])
def api_lab_record():
    b = body()
    eng = current()
    if b.get("stop"):
        return reply(*eng.stop_recording())
    return reply(*eng.start_recording(b.get("name"), reset_first=b.get("reset", True)))


@app.route("/api/lab/run", methods=["POST"])
def api_lab_run():
    """Run an experiment: {"preset": id} | {"program_id": id} | {"program": {...}} | {"chaos": {...}} | {"stop": true}"""
    b = body()
    eng = current()
    if b.get("stop"):
        return reply(*eng.stop_program())
    if b.get("preset"):
        prog = next((p for p in programs.PREDEFINED if p["id"] == b["preset"]), None)
        if b["preset"] == "break":
            prog = programs.BREAK
        if prog is None:
            return reply(False, "Unknown experiment")
    elif b.get("program_id"):
        prog = store.program(b["program_id"])
        if prog is None:
            return reply(False, "Unknown experiment")
    elif b.get("chaos"):
        c = b["chaos"]
        targets = {k: float(v) for k, v in (c.get("targets") or {}).items() if k in eng.spec}
        if not targets:
            return reply(False, "Choose at least one stressor")
        prog = programs.chaos_program(targets, c.get("ramp_s", 20), c.get("hold_s", 20), c.get("release", True))
    else:
        prog = b.get("program")
    return reply(*eng.run_program(prog))


@app.route("/api/lab/programs", methods=["GET"])
def api_lab_programs():
    return jsonify({"custom": store.programs(), "predefined": programs.PREDEFINED, "break": programs.BREAK})


@app.route("/api/lab/programs", methods=["POST"])
def api_lab_save_program():
    prog = body().get("program") or {}
    errs = programs.validate(prog, current().spec)
    if errs:
        return reply(False, "; ".join(errs), 400)
    return reply(True, store.save_program(prog.get("name") or "Custom experiment", prog))


@app.route("/api/lab/programs/<int:pid>", methods=["DELETE"])
def api_lab_delete_program(pid):
    return reply(True, store.delete_program(pid))


@app.route("/api/lab/runs")
def api_lab_runs():
    return jsonify(store.lab_runs(server_id=current().server_id))


@app.route("/api/lab/runs/<int:rid>")
def api_lab_run_get(rid):
    r = store.lab_run(rid)
    return jsonify(r) if r else (jsonify({"error": "not found"}), 404)


@app.route("/api/lab/runs/<int:rid>", methods=["DELETE"])
def api_lab_run_delete(rid):
    return reply(True, store.delete_lab_run(rid))


@app.route("/api/lab/runs/<int:rid>/replay", methods=["POST"])
def api_lab_run_replay(rid):
    return reply(*current().replay(rid))


@app.route("/api/lab/snapshots", methods=["GET"])
def api_lab_snapshots():
    return jsonify(store.snapshots(server_id=current().server_id))


@app.route("/api/lab/snapshots", methods=["POST"])
def api_lab_snapshot_save():
    name = (body().get("name") or "").strip() or "Snapshot"
    snap = current().snapshot_data(name[:60])
    return reply(True, store.save_snapshot(snap))


@app.route("/api/lab/snapshots/<int:sid>", methods=["DELETE"])
def api_lab_snapshot_delete(sid):
    return reply(True, store.delete_snapshot(sid))


@app.route("/api/health")
def api_health():
    eng = current()
    return jsonify({"health": eng.display_health(), "phase": eng.phase, "server": eng.server["id"],
                    "server_name": eng.server["name"], "offline": eng.offline,
                    "experiment": (eng.runner or {}).get("name") or (eng.recording or {}).get("name")})


# ------------------------------------------------- legacy (previous version) --
@app.route("/api/meta")
def api_meta():
    """Catalogue used to render experiments recorded by the previous version."""
    return jsonify({"scenarios": public_catalog(), "severities": config.SEVERITIES, "severity_info": SEVERITY_INFO,
                    "metrics": METRICS, "thresholds": config.THRESHOLDS, "server": current().public_server(),
                    "server_options": server_catalog.options(), "tick": config.TICK_SECONDS,
                    "baseline_seconds": config.BASELINE_SECONDS, "source_info": {}, "detect_confirm_samples": 2})


@app.route("/api/experiments", methods=["GET"])
def api_experiments():
    return jsonify(store.list(server_id=current().server_id))


@app.route("/api/experiments/<int:eid>")
def api_experiment(eid):
    e = store.get(eid)
    return jsonify(e) if e else (jsonify({"error": "not found"}), 404)


# ---------------------------------------------------------------- servers --
@app.route("/api/servers", methods=["GET"])
def api_servers():
    return jsonify({"servers": manager.list(), "selected": current().server_id})


@app.route("/api/servers", methods=["POST"])
def api_add_server():
    spec, errors = manager.add(body())
    if errors:
        return jsonify({"ok": False, "errors": errors, "message": "Please complete the highlighted fields."}), 400
    return jsonify({"ok": True, "server": spec})


@app.route("/api/servers/<int:sid>", methods=["DELETE"])
def api_remove_server(sid):
    ok, msg, code = manager.remove(sid)
    return jsonify({"ok": ok, "message": msg, "selected": current().server_id if ok else None}), code


def main():
    ap = argparse.ArgumentParser(description="Cloud Server Crash Analysis — interactive OS failure lab")
    ap.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", 5000)))
    ap.add_argument("--no-browser", action="store_true", help="do not open a browser tab")
    ap.add_argument("--debug-sim", action="store_true",
                    help="print the [SIMULATION]/[METRICS] trace of the simulation to the console")
    args = ap.parse_args()
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    if args.debug_sim or os.environ.get("SIM_DEBUG") == "1":
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
        for e in manager.engines.values():
            e._debug = True
    manager.start()
    url = f"http://127.0.0.1:{args.port}"
    print("=" * 64)
    print(" Cloud Server Crash Analysis — interactive OS failure laboratory")
    print(f" Open {url} in your browser   (Ctrl+C to stop)")
    print(f" Database: {DB_PATH}")
    print("=" * 64)
    if not args.no_browser and "WSL_DISTRO_NAME" not in os.environ:
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    app.run(host=args.host, port=args.port, debug=False, use_reloader=False, threaded=True)


if __name__ == "__main__":
    sys.exit(main())
