"""Contract tests for retrieval-time result shaping (NarratorDB 1.3).

Covers Engine._shape_results (near-duplicate demotion, session
diversification, temporal cluster selection, relative confidence floor)
and the benchmark server's adjacent same-session merge. Tests are
skipped until the mechanisms exist so the suite stays green during
development.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from narratordb.engine import Engine

HAS_SHAPING = hasattr(Engine, "_shape_results")


def make_row(row_id, text, timestamp, position, terms=None):
    normalized = terms if terms is not None else " ".join(
        token.lower() for token in text.split()
    )
    return (row_id, "user", text, timestamp, position, normalized)


@unittest.skipUnless(HAS_SHAPING, "Engine._shape_results not implemented yet")
class ShapeResultsTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="narratordb_shape_")
        self.engine = Engine(
            db_path=str(Path(self._tmp.name) / "shape.db"),
            user_id="shape-test",
            semantic_dedup=False,
        )

    def tearDown(self):
        self.engine.close()
        self._tmp.cleanup()

    def shape(self, ranked, provenance=None, current_intent=False, before_intent=False):
        return self.engine._shape_results(
            ranked,
            provenance_map=provenance or {},
            current_intent=current_intent,
            before_intent=before_intent,
        )

    def test_near_duplicates_demoted_not_dropped(self):
        base = "the pro plan now costs 18 dollars per month"
        ranked = [
            (0.95, make_row(1, base, 1000.0, 10)),
            (0.90, make_row(2, base + " indeed", 2000.0, 50, terms=base)),
            (0.60, make_row(3, "a completely different fact about fish", 1500.0, 90)),
        ]
        shaped = self.shape(ranked)
        self.assertEqual(len(shaped), 3, "demotion must never drop hits")
        self.assertEqual([entry[1][0] for entry in shaped][:2], [1, 3],
                         "near-dup of rank-1 should demote below distinct hits")
        self.assertEqual(shaped[-1][1][0], 2)

    def test_session_cap_within_top_window(self):
        provenance = {}
        ranked = []
        for index in range(6):
            row = make_row(index, f"session alpha fact number {index}", 1000.0 + index, index)
            ranked.append((0.9 - index * 0.01, row))
            provenance[index] = {"run_id": "alpha"}
        distinct = make_row(99, "unique beta session fact", 5000.0, 99)
        ranked.append((0.5, distinct))
        provenance[99] = {"run_id": "beta"}

        shaped = self.shape(ranked, provenance=provenance)
        self.assertEqual(len(shaped), 7, "cap demotes, never drops")
        cap = self.engine.session_cap
        top_alpha = [e for e in shaped[: cap + 1] if provenance.get(e[1][0], {}).get("run_id") == "alpha"]
        self.assertLessEqual(len(top_alpha), cap)
        self.assertIn(99, [e[1][0] for e in shaped[: cap + 1]],
                      "other-session hit should surface once cap applies")

    def test_temporal_cluster_prefers_latest_under_current_intent(self):
        text = "the release approver is deniz after the reorg"
        old = make_row(1, text, 1000.0, 10, terms=text)
        new = make_row(2, text + " confirmed", 9000.0, 80, terms=text)
        shaped = self.shape([(0.9, old), (0.85, new)], current_intent=True)
        self.assertEqual(shaped[0][1][0], 2,
                         "current intent: latest cluster member takes best rank")

    def test_temporal_cluster_prefers_earliest_under_before_intent(self):
        text = "the release approver was marta before the reorg"
        old = make_row(1, text, 1000.0, 10, terms=text)
        new = make_row(2, text + " noted", 9000.0, 80, terms=text)
        shaped = self.shape([(0.9, new), (0.85, old)], before_intent=True)
        self.assertEqual(shaped[0][1][0], 1,
                         "before intent: earliest cluster member takes best rank")

    def test_confidence_floor_trims_weak_tail_but_keeps_minimum(self):
        ranked = [(1.0 if index == 0 else 0.05, make_row(index, f"fact {index} entry", 1000.0, index))
                  for index in range(30)]
        shaped = self.shape(ranked)
        floor_min = self.engine.score_floor_min_results
        self.assertGreaterEqual(len(shaped), floor_min)
        self.assertLess(len(shaped), 30, "weak tail below ratio floor should be trimmed")

    def test_no_provenance_is_a_noop_for_diversification(self):
        ranked = [(0.9 - i * 0.05, make_row(i, f"distinct fact {i} {'x' * i}", 1000.0 + i, i))
                  for i in range(5)]
        shaped = self.shape(ranked)
        self.assertEqual([entry[1][0] for entry in shaped], [0, 1, 2, 3, 4])


@unittest.skipUnless(HAS_SHAPING, "server merge ships with shaping")
class AdjacentMergeTest(unittest.TestCase):
    def test_merge_adjacent_same_session_hits(self):
        from narratordb.benchmark_server import merge_adjacent_hits

        class Hit:
            def __init__(self, id, text, position, run_id, timestamp=1000.0):
                self.id = id
                self.text = text
                self.position = position
                self.timestamp = timestamp
                self.speaker = "user"
                self.provenance = {"run_id": run_id}

        hits = [
            Hit(1, "first part of the evidence", 10, "s1"),
            Hit(2, "second part of the evidence", 11, "s1"),
            Hit(3, "unrelated other-session hit", 40, "s2"),
        ]
        merged = merge_adjacent_hits(hits, gap=1, max_chars=1200)
        self.assertEqual(len(merged), 2)
        self.assertIn("first part", merged[0]["memory"])
        self.assertIn("second part", merged[0]["memory"])

    def test_merge_respects_char_cap_and_gap(self):
        from narratordb.benchmark_server import merge_adjacent_hits

        class Hit:
            def __init__(self, id, text, position, run_id):
                self.id = id
                self.text = text
                self.position = position
                self.timestamp = 1000.0
                self.speaker = "user"
                self.provenance = {"run_id": run_id}

        far_apart = [Hit(1, "a", 10, "s1"), Hit(2, "b", 20, "s1")]
        self.assertEqual(len(merge_adjacent_hits(far_apart, gap=1, max_chars=1200)), 2)

        long_text = "x" * 900
        big = [Hit(1, long_text, 10, "s1"), Hit(2, long_text, 11, "s1")]
        merged = merge_adjacent_hits(big, gap=1, max_chars=1200)
        for entry in merged:
            self.assertLessEqual(len(entry["memory"]), 1200)


if __name__ == "__main__":
    unittest.main()
