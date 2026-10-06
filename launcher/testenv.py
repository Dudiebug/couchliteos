"""Test only: give the launcher modules a scratch run and state directory.

Every test file imports this before any couchliteos module, which read COUCHLITEOS_RUN_DIR and
COUCHLITEOS_STATE_DIR once, at import. So the tests never read or write the real
/run/couchliteos or /var/lib/couchliteos, and a missing or root-owned one cannot break them.
"""
import atexit
import os
import pathlib
import shutil
import tempfile

if "COUCHLITEOS_RUN_DIR" not in os.environ or "COUCHLITEOS_STATE_DIR" not in os.environ:
    _root = pathlib.Path(tempfile.mkdtemp(prefix="couchliteos-tests-"))
    atexit.register(shutil.rmtree, _root, True)
    for _name, _sub in (("COUCHLITEOS_RUN_DIR", "run"), ("COUCHLITEOS_STATE_DIR", "state")):
        (_root / _sub).mkdir()
        os.environ.setdefault(_name, str(_root / _sub))
