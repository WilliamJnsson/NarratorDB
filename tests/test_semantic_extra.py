from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

try:
    import numpy as np
except ImportError:  # pragma: no cover - exercised by the dependency-free CI job
    np = None

from narratordb import engine as engine_module


class _FakeSentenceTransformer:
    @staticmethod
    def encode(texts, **_kwargs):
        vectors = []
        for text in texts:
            lowered = str(text).lower()
            if any(word in lowered for word in ("cycling", "bicycle", "transportation")):
                vectors.append([1.0, 0.0, 0.0])
            elif any(word in lowered for word in ("violin", "music")):
                vectors.append([0.0, 1.0, 0.0])
            else:
                vectors.append([0.0, 0.0, 1.0])
        return np.asarray(vectors, dtype=np.float32)


@unittest.skipIf(np is None, "semantic extra is not installed")
class SemanticExtraTests(unittest.TestCase):
    def test_semantic_backend_persists_and_searches_embeddings(self) -> None:
        model = _FakeSentenceTransformer()
        with tempfile.TemporaryDirectory() as directory, patch.object(
            engine_module,
            "_load_sentence_transformer",
            return_value=(model, "test/fake-sentence-transformer"),
        ):
            database = Path(directory) / "semantic.db"
            with engine_module.Engine(
                str(database),
                user_id="semantic-extra",
                context_window=0,
            ) as engine:
                expected_id = engine.store("user", "Cycling is how I get across town.")
                engine.store("user", "I practice the violin on weekends.")

                result = engine.search(
                    "What transportation do I prefer?",
                    limit=5,
                    max_context=5,
                    full_context_threshold=0,
                )
                direct_ids = [message.id for message in result.direct_hits]
                persisted = engine._conn.execute(
                    "SELECT COUNT(*) FROM embeddings WHERE user_id = ?",
                    ("semantic-extra",),
                ).fetchone()[0]

                self.assertIn(expected_id, direct_ids)
                self.assertEqual(persisted, 2)
                self.assertEqual(engine.embedding_source, "test/fake-sentence-transformer")


if __name__ == "__main__":
    unittest.main()
