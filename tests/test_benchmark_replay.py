from __future__ import annotations

import unittest

from narratordb.benchmarks.replay import _evidence_ranks, latency_summary


class BenchmarkReplayTests(unittest.TestCase):
    def test_latency_summary_uses_nearest_rank_p95(self) -> None:
        summary = latency_summary(range(1, 21))

        self.assertEqual(summary["samples"], 20)
        self.assertEqual(summary["median"], 10.5)
        self.assertEqual(summary["p95"], 19.0)
        self.assertEqual(summary["max"], 20.0)

    def test_evidence_ranks_cover_each_labeled_session(self) -> None:
        sample = {
            "answer_session_ids": ["answer-b", "answer-a"],
            "haystack_session_ids": ["answer-a", "noise", "answer-b"],
            "haystack_sessions": [
                [{"role": "user", "content": "alpha fact"}],
                [{"role": "user", "content": "noise"}],
                [{"role": "user", "content": "beta fact"}],
            ],
        }

        ranks = _evidence_ranks(["noise", "beta fact", "alpha fact"], sample)

        self.assertEqual(ranks, {"answer-a": 3, "answer-b": 2})


if __name__ == "__main__":
    unittest.main()
