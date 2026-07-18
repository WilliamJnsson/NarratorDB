#!/usr/bin/env python3
"""Exercise the installed wheel from outside the source checkout."""

import importlib.util

from narratordb import Engine, NarratorDB, __version__
from narratordb.benchmarks.splits import DEFAULT_OUTPUT_DIR, verify_split


assert __version__ == "2.2.1"
assert importlib.util.find_spec("narratordb.cloud") is None
assert NarratorDB.__name__ == "NarratorDB"
with Engine(user_id="wheel", semantic_dedup=False) as engine:
    assert engine.store("user", "standalone wheel works")
    assert engine.search("standalone wheel").messages
    assert engine.health_check(full=True)["ok"]
split_report = verify_split()
assert split_report["complete"]
assert split_report["development_questions"] == 42
assert split_report["holdout_questions"] == 458
assert "narratordb/benchmarks/data/splits" in DEFAULT_OUTPUT_DIR.as_posix()
print("NarratorDB wheel smoke passed")
