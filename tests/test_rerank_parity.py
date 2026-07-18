"""Golden-ordering parity guard for the search/rerank pipeline.

Captures the exact result ordering produced by ``Engine.search`` for a
fixed corpus and a query set spanning every intent branch (money,
quantity, current-state, historical, code locator, audit, OR-fallback).
Refactors that must not change behavior (regex hoisting, loop fusion,
intent gating) are verified against the stored golden file.

Regenerate deliberately after an intentional ranking change:
    NARRATORDB_REGEN_PARITY_GOLDEN=1 python3 -m unittest tests.test_rerank_parity
and review the golden diff in code review.
"""

from __future__ import annotations

import dataclasses
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from narratordb.engine import Engine

GOLDEN_PATH = Path(__file__).parent / "data" / "rerank_parity_golden.json"
ROOT = Path(__file__).resolve().parents[1]

# Ages in seconds relative to test start; widely separated so the recency
# component orders identically across runs despite a moving "now".
DAY = 86400


def build_corpus(now: float) -> list[dict]:
    rows = []

    def add(text: str, age_secs: float, speaker: str = "user", run_id: str = "session-a"):
        rows.append(
            {
                "speaker": speaker,
                "text": text,
                "timestamp": now - age_secs,
                "provenance": {"provider": "parity", "run_id": run_id},
            }
        )

    # Money / pricing facts, old vs new state
    add("The starter plan price was 12 dollars per month last spring.", 90 * DAY, run_id="session-a")
    add("Pricing update: the pro plan now costs $18 per month.", 2 * DAY, run_id="session-b")
    add("I paid the invoice yesterday and the charge cleared.", 1 * DAY, run_id="session-b")
    # Quantity / state counts
    add("The aquarium currently has nine fish after the two new tetras.", 3 * DAY, run_id="session-c")
    add("We started with four fish in the aquarium.", 60 * DAY, run_id="session-a")
    add("I have visited the climbing gym six times so far this year.", 5 * DAY, run_id="session-c")
    # Current vs historical approver
    add("Marta was the release approver before the reorg.", 45 * DAY, run_id="session-a")
    add("After the reorg, Deniz took over as the current release approver.", 4 * DAY, run_id="session-d")
    # Code locator
    add("The renderComparePopout function in provider-service.ts handles the popout dialog.", 20 * DAY, run_id="session-d")
    add("We patched the viewport module to fix the clipping issue.", 15 * DAY, run_id="session-d")
    # Audit / review
    add("Security review verdict: approved with two minor issues to track.", 10 * DAY, run_id="session-e")
    add("The audit flagged a risk in the token refresh flow.", 9 * DAY, run_id="session-e")
    # Interval / measurement facts
    add("The durability suite ran for 45 minutes on the release candidate.", 6 * DAY, run_id="session-e")
    add("The backup restore took 30 minutes during the last drill.", 35 * DAY, run_id="session-a")
    # Generic filler to give BM25 a spread and populate context windows
    for index in range(40):
        add(
            f"Routine diary note {index}: weather log, lunch order, and commute status entry.",
            (30 + index) * DAY,
            run_id=f"noise-{index % 3}",
        )
    return rows


QUERIES = [
    ("money_current", "how much does the pro plan cost per month now"),
    ("money_past", "what was the starter plan price before"),
    ("quantity_state", "how many fish do I have now"),
    ("quantity_times", "how many times have I visited the climbing gym"),
    ("current_state", "who is the current release approver"),
    ("before_state", "who was the release approver before the reorg"),
    ("code_locator", "which function handles the compare popout dialog"),
    ("audit", "what was the security review verdict"),
    ("measurement", "how many minutes did the durability suite run"),
    ("plain", "aquarium tetras"),
    ("vague_or_fallback", "drill restore timing"),
]


def capture(engine: Engine) -> dict:
    captured = {}
    for name, query in QUERIES:
        result = engine.search(query, limit=15, max_context=30, full_context_threshold=0)
        captured[name] = {
            "direct": [message.text for message in result.direct_hits],
            "context": [message.text for message in result.context_messages],
        }
    return captured


class RerankParityTest(unittest.TestCase):
    def test_full_order_is_invariant_to_search_wall_clock(self):
        corpus_time = 1_700_000_000.0
        with tempfile.TemporaryDirectory(prefix="narratordb_clock_order_") as tmp:
            with Engine(
                db_path=str(Path(tmp) / "clock-order.db"),
                user_id="clock-order-test",
                semantic_dedup=False,
            ) as engine:
                for row in build_corpus(corpus_time):
                    engine.store(
                        row["speaker"],
                        row["text"],
                        timestamp=row["timestamp"],
                        provenance=row["provenance"],
                    )

                def capture_full() -> bytes:
                    results = {}
                    for name, query in QUERIES:
                        raw = engine.search(
                            query,
                            limit=200,
                            max_context=200,
                            full_context_threshold=0,
                        )
                        ranked = engine.search_memory_blocks(
                            query,
                            limit=200,
                            include_derived=True,
                        )
                        results[name] = {
                            "messages": [message.id for message in raw.messages],
                            "direct": [message.id for message in raw.direct_hits],
                            "context": [message.id for message in raw.context_messages],
                            "scores": [format(score, ".17g") for score in raw.scores],
                            "blocks": [
                                dataclasses.asdict(block) for block in ranked.blocks
                            ],
                        }
                    return json.dumps(
                        results,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode()

                with patch("narratordb.engine.time.time", return_value=corpus_time):
                    first = capture_full()
                with patch(
                    "narratordb.engine.time.time",
                    return_value=corpus_time + 10 * 365 * DAY,
                ):
                    much_later = capture_full()

        self.assertEqual(first, much_later)

    def test_equal_dense_scores_ignore_candidate_input_order(self):
        metadata = [
            (101, 4, 1004.0),
            (102, 8, 1008.0),
            (103, 8, 1008.0),
            (104, 2, 1002.0),
        ]

        def search(permutation: list[int]) -> bytes:
            engine = object.__new__(Engine)
            engine._np = np
            engine._embedding_matrix = np.asarray(
                [[1.0, 0.0] for _ in permutation], dtype=np.float32
            )
            engine._embedding_row_meta = [metadata[index] for index in permutation]
            engine._query_embedding_cache = {
                "stable dense tie": np.asarray([[1.0, 0.0]], dtype=np.float32)
            }
            engine._ensure_embedding_index = lambda: True
            results = engine._semantic_search("stable dense tie", limit=4)
            return json.dumps(results, separators=(",", ":")).encode()

        forward = search([0, 1, 2, 3])
        reordered = search([2, 0, 3, 1])
        self.assertEqual(forward, reordered)
        self.assertEqual(
            [row[0] for row in json.loads(forward)],
            [103, 102, 101, 104],
        )

    def test_fts_expansion_is_hash_seed_invariant(self):
        script = """
import json
from narratordb.engine import Engine, STOP_WORDS_EN
engine = object.__new__(Engine)
engine.stop_words = STOP_WORDS_EN
query = "cobalt widget lantern"
print(json.dumps([engine._expand_query(query), engine._expand_query_or(query)]))
"""

        expected = [
            '("cobalt" OR "cobalted" OR "cobalter" OR "cobalting" OR '
            '"cobalts") AND ("widget" OR "widgeted" OR "widgeter" OR '
            '"widgeting" OR "widgets") AND ("lantern" OR "lanterned" OR '
            '"lanterner" OR "lanterning" OR "lanterns")',
            '"cobalt" OR "cobalted" OR "cobalter" OR "cobalting" OR '
            '"cobalts" OR "lantern" OR "lanterned" OR "lanterner" OR '
            '"lanterning" OR "lanterns" OR "widget" OR "widgeted" OR '
            '"widgeter" OR "widgeting" OR "widgets"',
        ]

        def expand(seed: str) -> bytes:
            environment = os.environ.copy()
            environment.update(
                {
                    "PYTHONHASHSEED": seed,
                    "PYTHONDONTWRITEBYTECODE": "1",
                }
            )
            completed = subprocess.run(
                [sys.executable, "-c", script],
                cwd=ROOT,
                env=environment,
                capture_output=True,
                check=True,
                timeout=30,
            )
            return completed.stdout

        outputs = []
        # These synthetic terms make the pre-fix set iteration produce a
        # different expansion for every selected seed.  Assert the canonical
        # bytes too, so two coincidentally equal unordered runs cannot pass.
        for seed in ("0", "1", "2", "7", "31", "8675309"):
            with self.subTest(seed=seed):
                output = expand(seed)
                outputs.append(output)
                self.assertEqual(json.loads(output), expected)
        self.assertEqual(len(set(outputs)), 1)

    def test_orderings_match_golden(self):
        now = time.time()
        with tempfile.TemporaryDirectory(prefix="narratordb_parity_") as tmp:
            engine = Engine(
                db_path=str(Path(tmp) / "parity.db"),
                user_id="parity-test",
                semantic_dedup=False,  # deterministic: no embedding model
            )
            with engine:
                for row in build_corpus(now):
                    engine.store(
                        row["speaker"],
                        row["text"],
                        timestamp=row["timestamp"],
                        provenance=row["provenance"],
                    )
                captured = capture(engine)

        if os.environ.get("NARRATORDB_REGEN_PARITY_GOLDEN") == "1" or not GOLDEN_PATH.exists():
            GOLDEN_PATH.write_text(json.dumps(captured, indent=2) + "\n", encoding="utf-8")
            if os.environ.get("NARRATORDB_REGEN_PARITY_GOLDEN") == "1":
                self.skipTest("golden regenerated deliberately; review the diff")
            self.skipTest("golden captured on first run; rerun to enforce")

        golden = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
        for name, _query in QUERIES:
            with self.subTest(query=name):
                self.assertEqual(golden[name]["direct"], captured[name]["direct"])
                self.assertEqual(golden[name]["context"], captured[name]["context"])


if __name__ == "__main__":
    unittest.main()
