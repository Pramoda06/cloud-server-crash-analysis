"""Point the application at a throw-away SQLite database BEFORE any test imports `app`.

`app.py` opens its database when it is imported, so without this the tests would open (and could write to)
database/experiments.db — your recorded experiments. Every test module imports this first, so it works with
`python -m pytest`, `python -m unittest discover -s tests` and `python -m unittest discover -s tests -t .`.
Set OS_EXPERIMENTS_DB yourself to choose another database.
"""
import atexit
import os
import shutil
import tempfile

if not os.environ.get("OS_EXPERIMENTS_DB"):
    _TMP = tempfile.mkdtemp(prefix="oslab-tests-")
    os.environ["OS_EXPERIMENTS_DB"] = os.path.join(_TMP, "test_experiments.db")
    atexit.register(shutil.rmtree, _TMP, True)
