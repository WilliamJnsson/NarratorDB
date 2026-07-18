from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from narratordb.engine import Engine


class V18ScalarResolutionTests(unittest.TestCase):
    def _materialize(
        self,
        engine: Engine,
        *,
        session_id: str,
        messages: list[dict],
        claims: list[dict],
        occurred_at: float,
    ) -> list[int]:
        stored = engine.store_session(
            messages,
            session_id=session_id,
            occurred_at=occurred_at,
        )
        compiled_claims = []
        for raw_claim in claims:
            claim = dict(raw_claim)
            source_index = int(claim.pop("source_index", 0))
            source_text = str(messages[source_index]["content"])
            claim["evidence"] = [
                {
                    "message_id": stored["message_ids"][source_index],
                    "quote": source_text,
                }
            ]
            compiled_claims.append(claim)
        job_id = engine.enqueue_compilation(
            stored["session_pk"],
            stored["source_hash"],
            "v18-synthetic-compiler",
        )
        result = engine.apply_compilation(
            job_id,
            {"claims": compiled_claims},
            processor="v18-synthetic-test",
            processor_version="1",
            prompt_version="1",
        )
        self.assertIn(result["status"], {"complete", "partial"})
        return list(stored["message_ids"])

    @staticmethod
    def _scalar_blocks(blocks, channel: str):
        return [block for block in blocks if channel in block.channels]

    def test_lifetime_counter_chooses_highest_explicit_total_and_suppresses_conflicts(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with Engine(
                str(Path(directory) / "memory.db"),
                user_id="lifetime-counter",
                semantic_dedup=False,
                local_only=True,
            ) as engine:
                older_ids = self._materialize(
                    engine,
                    session_id="older-telescope-log",
                    occurred_at=100.0,
                    messages=[
                        {
                            "role": "user",
                            "content": (
                                "I have calibrated the brass telescope 3 times "
                                "already."
                            ),
                        }
                    ],
                    claims=[
                        {
                            "kind": "fact",
                            "text": (
                                "The user has calibrated the brass telescope "
                                "3 times already."
                            ),
                            "subject": "user",
                            "predicate": "has calibrated",
                            "object_text": "brass telescope 3 times already",
                        }
                    ],
                )
                newer_ids = self._materialize(
                    engine,
                    session_id="newer-telescope-log",
                    occurred_at=200.0,
                    messages=[
                        {
                            "role": "user",
                            "content": (
                                "I have calibrated the brass telescope 8 times now."
                            ),
                        }
                    ],
                    claims=[
                        {
                            "kind": "fact",
                            "text": (
                                "The user has calibrated the brass telescope "
                                "8 times now."
                            ),
                            "subject": "user",
                            "predicate": "has calibrated",
                            "object_text": "brass telescope 8 times now",
                        }
                    ],
                )

                blocks = engine.search_memory_blocks(
                    "How many times have I calibrated the brass telescope?",
                    limit=20,
                ).blocks
                resolutions = self._scalar_blocks(
                    blocks, "cumulative_counter_resolution"
                )
                self.assertEqual(len(resolutions), 1)
                resolution = resolutions[0]
                self.assertIs(resolution, blocks[0])
                self.assertIn("8 times", resolution.text)
                self.assertIn("Candidate cumulative totals: 3, 8", resolution.text)
                represented_ids = set(older_ids + newer_ids)
                self.assertEqual(set(resolution.message_ids), represented_ids)
                self.assertTrue(
                    all(
                        represented_ids.isdisjoint(block.message_ids)
                        for block in blocks[1:]
                    )
                )

    def test_reset_correction_and_bounded_window_disable_monotonic_resolution(
        self,
    ) -> None:
        with self.subTest(reason="explicit-reset-correction"):
            with tempfile.TemporaryDirectory() as directory:
                with Engine(
                    str(Path(directory) / "memory.db"),
                    user_id="reset-counter",
                    semantic_dedup=False,
                    local_only=True,
                ) as engine:
                    for index, (session_id, source, claim_text) in enumerate(
                        (
                            (
                                "before-reset",
                                "I have serviced the cedar loom 11 times already.",
                                "The user has serviced the cedar loom 11 times already.",
                            ),
                            (
                                "after-reset",
                                (
                                    "Correction: after the counter was reset, I have "
                                    "serviced the cedar loom 2 times now."
                                ),
                                (
                                    "Correction: after the counter was reset, the user "
                                    "has serviced the cedar loom 2 times now."
                                ),
                            ),
                        ),
                        start=1,
                    ):
                        self._materialize(
                            engine,
                            session_id=session_id,
                            occurred_at=float(index * 100),
                            messages=[{"role": "user", "content": source}],
                            claims=[
                                {
                                    "kind": "fact",
                                    "text": claim_text,
                                    "subject": "user",
                                    "predicate": "has serviced",
                                    "object_text": "cedar loom lifetime service count",
                                }
                            ],
                        )
                    blocks = engine.search_memory_blocks(
                        "How many times have I serviced the cedar loom?",
                        limit=20,
                    ).blocks
                    self.assertFalse(
                        self._scalar_blocks(blocks, "cumulative_counter_resolution")
                    )

        with self.subTest(reason="bounded-time-window"):
            with tempfile.TemporaryDirectory() as directory:
                with Engine(
                    str(Path(directory) / "memory.db"),
                    user_id="windowed-counter",
                    semantic_dedup=False,
                    local_only=True,
                ) as engine:
                    for index, total in enumerate((4, 9), start=1):
                        source = (
                            f"I have inspected the quartz compass {total} times "
                            f"{'already' if total == 4 else 'now'}."
                        )
                        self._materialize(
                            engine,
                            session_id=f"compass-log-{index}",
                            occurred_at=float(index * 100),
                            messages=[{"role": "user", "content": source}],
                            claims=[
                                {
                                    "kind": "fact",
                                    "text": source.replace("I have", "The user has"),
                                    "subject": "user",
                                    "predicate": "has inspected",
                                    "object_text": "quartz compass inspection count",
                                }
                            ],
                        )
                    blocks = engine.search_memory_blocks(
                        (
                            "How many times have I inspected the quartz compass "
                            "during this month?"
                        ),
                        limit=20,
                    ).blocks
                    self.assertFalse(
                        self._scalar_blocks(blocks, "cumulative_counter_resolution")
                    )

    def test_composite_measurement_uses_exact_requested_user_operands(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with Engine(
                str(Path(directory) / "memory.db"),
                user_id="composite-pages",
                semantic_dedup=False,
                local_only=True,
            ) as engine:
                included_ids = []
                for index, (name, pages) in enumerate(
                    (("basalt field guide", 137), ("willow field guide", 264)),
                    start=1,
                ):
                    source = (
                        f"I completed the {name} in March. Its page count was "
                        f"{pages} pages."
                    )
                    included_ids.extend(
                        self._materialize(
                            engine,
                            session_id=f"march-guide-{index}",
                            occurred_at=float(index * 100),
                            messages=[{"role": "user", "content": source}],
                            claims=[
                                {
                                    "kind": "event",
                                    "text": f"The user completed the {name} in March.",
                                    "subject": "user",
                                    "predicate": "completed",
                                    "object_text": name,
                                },
                                {
                                    "kind": "fact",
                                    "text": (
                                        f"The March page count of the {name} was "
                                        f"{pages} pages."
                                    ),
                                    "subject": name,
                                    "predicate": "page count",
                                    "object_text": f"{pages} pages",
                                },
                            ],
                        )
                    )

                assistant_ids = self._materialize(
                    engine,
                    session_id="assistant-recommendation",
                    occurred_at=300.0,
                    messages=[
                        {
                            "role": "assistant",
                            "content": (
                                "You should complete the granite field guide; "
                                "its page count is 991 pages."
                            ),
                        }
                    ],
                    claims=[
                        {
                            "kind": "fact",
                            "text": (
                                "The assistant recommends the 991-page granite "
                                "field guide."
                            ),
                            "subject": "assistant",
                            "predicate": "recommends completing",
                            "object_text": "991 pages",
                        }
                    ],
                )
                april_ids = self._materialize(
                    engine,
                    session_id="april-guide",
                    occurred_at=400.0,
                    messages=[
                        {
                            "role": "user",
                            "content": (
                                "I completed the granite field guide in April. "
                                "Its page count was 887 pages."
                            ),
                        }
                    ],
                    claims=[
                        {
                            "kind": "event",
                            "text": (
                                "The user completed the granite field guide in April."
                            ),
                            "subject": "user",
                            "predicate": "completed",
                            "object_text": "granite field guide",
                        },
                        {
                            "kind": "fact",
                            "text": (
                                "The April page count of the granite field guide "
                                "was 887 pages."
                            ),
                            "subject": "granite field guide",
                            "predicate": "page count",
                            "object_text": "887 pages",
                        },
                    ],
                )

                blocks = engine.search_memory_blocks(
                    (
                        "What was the combined page count of the two field guides "
                        "I completed in March?"
                    ),
                    limit=20,
                ).blocks
                resolutions = self._scalar_blocks(
                    blocks, "composite_measurement_resolution"
                )
                self.assertEqual(len(resolutions), 1)
                resolution = resolutions[0]
                self.assertIs(resolution, blocks[0])
                self.assertIn("401 pages", resolution.text)
                self.assertRegex(
                    resolution.text,
                    r"\((?:137 \+ 264|264 \+ 137) = 401\)",
                )
                self.assertIn("Operand count required by the query: 2", resolution.text)
                self.assertEqual(set(resolution.message_ids), set(included_ids))
                self.assertTrue(set(assistant_ids).isdisjoint(resolution.message_ids))
                self.assertTrue(set(april_ids).isdisjoint(resolution.message_ids))
                self.assertNotIn("991", resolution.text)
                self.assertNotIn("887", resolution.text)

    def test_acquisition_count_keeps_named_groups_and_sums_explicit_quantities(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with Engine(
                str(Path(directory) / "memory.db"),
                user_id="acquisition-count",
                semantic_dedup=False,
                local_only=True,
            ) as engine:
                fixtures = (
                    (
                        "cufflink-pair",
                        "I acquired a matching pair of obsidian cufflinks.",
                        "The user acquired a matching pair of obsidian cufflinks.",
                        "a matching pair of obsidian cufflinks",
                    ),
                    (
                        "stamp-set",
                        "I acquired a carved stamp set.",
                        "The user acquired a carved stamp set.",
                        "a carved stamp set",
                    ),
                    (
                        "enamel-pins",
                        "I acquired 4 enamel pins.",
                        "The user acquired 4 enamel pins.",
                        "4 enamel pins",
                    ),
                )
                source_ids = []
                for index, (session_id, source, claim_text, object_text) in enumerate(
                    fixtures,
                    start=1,
                ):
                    source_ids.extend(
                        self._materialize(
                            engine,
                            session_id=session_id,
                            occurred_at=float(index * 100),
                            messages=[{"role": "user", "content": source}],
                            claims=[
                                {
                                    "kind": "event",
                                    "text": claim_text,
                                    "subject": "user",
                                    "predicate": "acquired",
                                    "object_text": object_text,
                                }
                            ],
                        )
                    )

                pack = engine.search_memory_blocks(
                    (
                        "How many cufflink pairs, stamp sets, and individual "
                        "enamel pins did I acquire?"
                    ),
                    limit=20,
                ).blocks[0]
                self.assertEqual(pack.kind, "evidence_pack")
                self.assertEqual(set(pack.message_ids), set(source_ids))
                self.assertIn(
                    "Computed scalar from exhaustive source-linked evidence: 6",
                    pack.text,
                )
                self.assertIn("1 + 1 + 4 = 6", pack.text)
                self.assertIn("named pair or set remains one acquired group", pack.text)

    def test_source_cap_never_advertises_a_completed_or_pending_total(self) -> None:
        names = (
            "amber",
            "birch",
            "cobalt",
            "dahlia",
            "elm",
            "flint",
            "garnet",
            "hazel",
            "indigo",
            "juniper",
            "kelp",
            "lilac",
            "marble",
            "nickel",
            "onyx",
            "pearl",
            "quartz",
            "ruby",
            "saffron",
            "topaz",
            "umber",
            "violet",
            "willow",
            "xenon",
            "yucca",
        )
        with tempfile.TemporaryDirectory() as directory:
            with Engine(
                str(Path(directory) / "memory.db"),
                user_id="source-cap",
                semantic_dedup=False,
                local_only=True,
            ) as engine:
                completed_messages = [
                    {
                        "role": "user",
                        "content": (
                            f"I catalogued the {name} observatory instrument."
                        ),
                    }
                    for name in names
                ]
                completed_ids = self._materialize(
                    engine,
                    session_id="completed-instrument-catalogue",
                    occurred_at=100.0,
                    messages=completed_messages,
                    claims=[
                        {
                            "kind": "event",
                            "text": (
                                f"The user catalogued the {name} observatory "
                                "instrument."
                            ),
                            "subject": "user",
                            "predicate": "catalogued",
                            "object_text": f"{name} observatory instrument",
                            "source_index": index,
                        }
                        for index, name in enumerate(names)
                    ],
                )
                completed_pack = engine._memory._build_aggregation_pack(
                    "How many observatory instruments did I catalogue altogether?",
                    completed_ids,
                    max_chars=50_000,
                )
                self.assertIsNotNone(completed_pack)
                assert completed_pack is not None

                pending_messages = [
                    {
                        "role": "user",
                        "content": (
                            f"I still need to polish the {name} observatory "
                            "instrument."
                        ),
                    }
                    for name in names
                ]
                pending_ids = self._materialize(
                    engine,
                    session_id="pending-instrument-polishing",
                    occurred_at=200.0,
                    messages=pending_messages,
                    claims=[
                        {
                            "kind": "fact",
                            "text": (
                                f"The user still needs to polish the {name} "
                                "observatory instrument."
                            ),
                            "subject": "user",
                            "predicate": "needs_to_polish",
                            "object_text": f"{name} observatory instrument",
                            "memory_key": f"user.observatory.{name}.polish",
                            "source_index": index,
                        }
                        for index, name in enumerate(names)
                    ],
                )
                pending_pack = engine._memory._build_aggregation_pack(
                    "How many observatory instruments do I need to polish?",
                    pending_ids,
                    max_chars=50_000,
                    include_uncompleted=True,
                )
                self.assertIsNotNone(pending_pack)
                assert pending_pack is not None
                for mode, pack, source_ids in (
                    ("completed", completed_pack, completed_ids),
                    ("pending", pending_pack, pending_ids),
                ):
                    with self.subTest(mode=mode):
                        self.assertLess(
                            len(pack.block.message_ids), len(source_ids)
                        )
                        self.assertNotIn("Computed scalar", pack.block.text)

    def test_separate_quantity_companion_never_defaults_to_one(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with Engine(
                str(Path(directory) / "memory.db"),
                user_id="quantity-companion",
                semantic_dedup=False,
                local_only=True,
            ) as engine:
                message_ids = self._materialize(
                    engine,
                    session_id="lapis-catalogue",
                    occurred_at=100.0,
                    messages=[
                        {
                            "role": "user",
                            "content": (
                                "I catalogued the lapis specimens. There were "
                                "4 lapis specimens in the finished catalogue."
                            ),
                        }
                    ],
                    claims=[
                        {
                            "kind": "event",
                            "text": "The user catalogued the lapis specimens.",
                            "subject": "user",
                            "predicate": "catalogued",
                            "object_text": "lapis specimens",
                        },
                        {
                            "kind": "fact",
                            "text": "There were 4 lapis specimens in the catalogue.",
                            "subject": "lapis specimens",
                            "predicate": "catalogue quantity",
                            "object_text": "4 lapis specimens",
                        },
                    ],
                )
                pack = engine._memory._build_aggregation_pack(
                    "How many lapis specimens did I catalogue?",
                    message_ids,
                    max_chars=2_000,
                )
                self.assertIsNotNone(pack)
                assert pack is not None
                self.assertNotIn(
                    "evidence: 1 completed action-object groups/items",
                    pack.block.text,
                )
                self.assertIn(
                    "evidence: 4 completed action-object groups/items",
                    pack.block.text,
                )

    def test_money_query_never_uses_an_item_or_group_count(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with Engine(
                str(Path(directory) / "memory.db"),
                user_id="money-not-items",
                semantic_dedup=False,
                local_only=True,
            ) as engine:
                message_ids = self._materialize(
                    engine,
                    session_id="pottery-sale",
                    occurred_at=100.0,
                    messages=[
                        {
                            "role": "user",
                            "content": "I sold 4 pottery bowls for 9 dollars each.",
                        }
                    ],
                    claims=[
                        {
                            "kind": "event",
                            "text": "The user sold 4 pottery bowls.",
                            "subject": "user",
                            "predicate": "sold",
                            "object_text": "4 pottery bowls",
                        },
                        {
                            "kind": "fact",
                            "text": "Each pottery bowl sold for 9 dollars.",
                            "subject": "pottery bowls",
                            "predicate": "unit sale price",
                            "object_text": "9 dollars each",
                        },
                    ],
                )
                pack = engine._memory._build_aggregation_pack(
                    "How much money did I earn from selling pottery bowls?",
                    message_ids,
                    max_chars=2_000,
                )
                self.assertIsNotNone(pack)
                assert pack is not None
                self.assertNotIn(
                    "completed action-object groups/items", pack.block.text
                )
                self.assertNotIn("Computed scalar", pack.block.text)

    def test_component_count_does_not_treat_a_named_pair_as_one_component(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with Engine(
                str(Path(directory) / "memory.db"),
                user_id="pair-components",
                semantic_dedup=False,
                local_only=True,
            ) as engine:
                message_ids = self._materialize(
                    engine,
                    session_id="cufflink-pair",
                    occurred_at=100.0,
                    messages=[
                        {
                            "role": "user",
                            "content": (
                                "I acquired a matching pair of obsidian cufflinks."
                            ),
                        }
                    ],
                    claims=[
                        {
                            "kind": "event",
                            "text": (
                                "The user acquired a matching pair of obsidian "
                                "cufflinks."
                            ),
                            "subject": "user",
                            "predicate": "acquired",
                            "object_text": "a matching pair of obsidian cufflinks",
                        }
                    ],
                )
                pack = engine._memory._build_aggregation_pack(
                    "How many individual cufflinks did I acquire?",
                    message_ids,
                    max_chars=2_000,
                )
                self.assertIsNotNone(pack)
                assert pack is not None
                self.assertNotIn(
                    "evidence: 1 completed action-object groups/items",
                    pack.block.text,
                )
                if "Computed scalar" in pack.block.text:
                    self.assertIn(
                        "evidence: 2 completed action-object groups/items",
                        pack.block.text,
                    )

    def test_duplicate_quantity_claims_do_not_double_the_source_quantity(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with Engine(
                str(Path(directory) / "memory.db"),
                user_id="duplicate-quantity",
                semantic_dedup=False,
                local_only=True,
            ) as engine:
                message_ids = self._materialize(
                    engine,
                    session_id="pin-acquisition",
                    occurred_at=100.0,
                    messages=[
                        {
                            "role": "user",
                            "content": "I acquired 4 enamel pins at the market.",
                        }
                    ],
                    claims=[
                        {
                            "kind": "event",
                            "text": "The user acquired 4 enamel pins at the market.",
                            "subject": "user",
                            "predicate": "acquired",
                            "object_text": "4 enamel pins",
                        },
                        {
                            "kind": "event",
                            "text": "Four enamel pins were acquired by the user.",
                            "subject": "user",
                            "predicate": "acquired",
                            "object_text": "four enamel pins",
                        },
                    ],
                )
                pack = engine._memory._build_aggregation_pack(
                    "How many enamel pins did I acquire?",
                    message_ids,
                    max_chars=2_000,
                )
                self.assertIsNotNone(pack)
                assert pack is not None
                self.assertIn(
                    "evidence: 4 completed action-object groups/items",
                    pack.block.text,
                )
                self.assertNotIn(
                    "evidence: 8 completed action-object groups/items",
                    pack.block.text,
                )

    def test_assistant_only_higher_cumulative_total_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with Engine(
                str(Path(directory) / "memory.db"),
                user_id="assistant-counter",
                semantic_dedup=False,
                local_only=True,
            ) as engine:
                for index, total in enumerate((3, 5), start=1):
                    self._materialize(
                        engine,
                        session_id=f"user-telescope-{index}",
                        occurred_at=float(index * 100),
                        messages=[
                            {
                                "role": "user",
                                "content": (
                                    "I have calibrated the brass telescope "
                                    f"{total} times "
                                    f"{'already' if total == 3 else 'now'}."
                                ),
                            }
                        ],
                        claims=[
                            {
                                "kind": "fact",
                                "text": (
                                    "The user has calibrated the brass telescope "
                                    f"{total} times "
                                    f"{'already' if total == 3 else 'now'}."
                                ),
                                "subject": "user",
                                "predicate": "has calibrated",
                                "object_text": "brass telescope calibration count",
                            }
                        ],
                    )
                assistant_ids = self._materialize(
                    engine,
                    session_id="assistant-guess",
                    occurred_at=300.0,
                    messages=[
                        {
                            "role": "assistant",
                            "content": (
                                "You have calibrated the brass telescope 99 times "
                                "now."
                            ),
                        }
                    ],
                    claims=[
                        {
                            "kind": "fact",
                            "text": (
                                "The assistant says the user calibrated the brass "
                                "telescope 99 times now."
                            ),
                            "subject": "assistant",
                            "predicate": "says calibration count",
                            "object_text": "brass telescope 99 times now",
                        }
                    ],
                )

                blocks = engine.search_memory_blocks(
                    "How many times have I calibrated the brass telescope?",
                    limit=20,
                ).blocks
                resolutions = self._scalar_blocks(
                    blocks, "cumulative_counter_resolution"
                )
                self.assertEqual(len(resolutions), 1)
                self.assertIn("5 times", resolutions[0].text)
                self.assertNotIn("99", resolutions[0].text)
                self.assertTrue(
                    set(assistant_ids).isdisjoint(resolutions[0].message_ids)
                )

    def test_unnumbered_reset_and_bounded_evidence_disable_monotonic_resolution(
        self,
    ) -> None:
        with self.subTest(reason="unnumbered-relevant-reset"):
            with tempfile.TemporaryDirectory() as directory:
                with Engine(
                    str(Path(directory) / "memory.db"),
                    user_id="unnumbered-reset",
                    semantic_dedup=False,
                    local_only=True,
                ) as engine:
                    for index, total in enumerate((3, 8), start=1):
                        self._materialize(
                            engine,
                            session_id=f"counter-{index}",
                            occurred_at=float(index * 100),
                            messages=[
                                {
                                    "role": "user",
                                    "content": (
                                        "I have calibrated the brass telescope "
                                        f"{total} times "
                                        f"{'already' if total == 3 else 'now'}."
                                    ),
                                }
                            ],
                            claims=[
                                {
                                    "kind": "fact",
                                    "text": (
                                        "The user has calibrated the brass telescope "
                                        f"{total} times "
                                        f"{'already' if total == 3 else 'now'}."
                                    ),
                                    "subject": "user",
                                    "predicate": "has calibrated",
                                    "object_text": "brass telescope calibration count",
                                }
                            ],
                        )
                    self._materialize(
                        engine,
                        session_id="counter-reset",
                        occurred_at=300.0,
                        messages=[
                            {
                                "role": "user",
                                "content": (
                                    "I reset the brass telescope calibration "
                                    "counter yesterday."
                                ),
                            }
                        ],
                        claims=[
                            {
                                "kind": "fact",
                                "text": (
                                    "The user reset the brass telescope calibration "
                                    "counter."
                                ),
                                "subject": "user",
                                "predicate": "reset",
                                "object_text": "brass telescope calibration counter",
                            }
                        ],
                    )
                    blocks = engine.search_memory_blocks(
                        "How many times have I calibrated the brass telescope?",
                        limit=20,
                    ).blocks
                    self.assertFalse(
                        self._scalar_blocks(
                            blocks, "cumulative_counter_resolution"
                        )
                    )

        with self.subTest(reason="totals-have-evidence-windows"):
            with tempfile.TemporaryDirectory() as directory:
                with Engine(
                    str(Path(directory) / "memory.db"),
                    user_id="evidence-window",
                    semantic_dedup=False,
                    local_only=True,
                ) as engine:
                    for index, (total, month, marker) in enumerate(
                        ((3, "June", "already"), (8, "July", "now")),
                        start=1,
                    ):
                        source = (
                            "I have calibrated the brass telescope "
                            f"{total} times {marker} in {month}."
                        )
                        self._materialize(
                            engine,
                            session_id=f"window-{month}",
                            occurred_at=float(index * 100),
                            messages=[{"role": "user", "content": source}],
                            claims=[
                                {
                                    "kind": "fact",
                                    "text": source.replace(
                                        "I have", "The user has"
                                    ),
                                    "subject": "user",
                                    "predicate": "has calibrated",
                                    "object_text": (
                                        f"brass telescope {month} calibration count"
                                    ),
                                }
                            ],
                        )
                    blocks = engine.search_memory_blocks(
                        "How many times have I calibrated the brass telescope?",
                        limit=20,
                    ).blocks
                    self.assertFalse(
                        self._scalar_blocks(
                            blocks, "cumulative_counter_resolution"
                        )
                    )

    def test_composite_measurement_refuses_three_candidates_for_two_operands(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with Engine(
                str(Path(directory) / "memory.db"),
                user_id="three-measurements",
                semantic_dedup=False,
                local_only=True,
            ) as engine:
                for index, (name, pages) in enumerate(
                    (("cedar", 101), ("basalt", 202), ("willow", 303)),
                    start=1,
                ):
                    self._materialize(
                        engine,
                        session_id=f"{name}-guide",
                        occurred_at=float(index * 100),
                        messages=[
                            {
                                "role": "user",
                                "content": (
                                    f"I completed the {name} field guide in March. "
                                    f"It has {pages} pages."
                                ),
                            }
                        ],
                        claims=[
                            {
                                "kind": "event",
                                "text": (
                                    f"The user completed the {name} field guide "
                                    "in March."
                                ),
                                "subject": "user",
                                "predicate": "completed",
                                "object_text": f"{name} field guide",
                            },
                            {
                                "kind": "fact",
                                "text": (
                                    f"The March page count of the {name} field "
                                    f"guide was {pages} pages."
                                ),
                                "subject": f"{name} field guide",
                                "predicate": "page count",
                                "object_text": f"{pages} pages",
                            },
                        ],
                    )
                blocks = engine.search_memory_blocks(
                    (
                        "What was the combined page count of the two field guides "
                        "I completed in March?"
                    ),
                    limit=20,
                ).blocks
                self.assertFalse(
                    self._scalar_blocks(
                        blocks, "composite_measurement_resolution"
                    )
                )

    def test_composite_measurement_requires_entity_linked_action_support(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with Engine(
                str(Path(directory) / "memory.db"),
                user_id="unrelated-colocated-measurement",
                semantic_dedup=False,
                local_only=True,
            ) as engine:
                self._materialize(
                    engine,
                    session_id="cedar-atlas",
                    occurred_at=100.0,
                    messages=[
                        {
                            "role": "user",
                            "content": (
                                "I completed the cedar atlas. The unrelated furnace "
                                "manual has 300 pages."
                            ),
                        }
                    ],
                    claims=[
                        {
                            "kind": "event",
                            "text": "The user completed the cedar atlas.",
                            "subject": "user",
                            "predicate": "completed",
                            "object_text": "cedar atlas",
                        },
                        {
                            "kind": "fact",
                            "text": "The furnace manual has 300 pages.",
                            "subject": "furnace manual",
                            "predicate": "page count",
                            "object_text": "300 pages",
                        },
                    ],
                )
                self._materialize(
                    engine,
                    session_id="basalt-atlas",
                    occurred_at=200.0,
                    messages=[
                        {
                            "role": "user",
                            "content": (
                                "I completed the basalt atlas, which has 200 pages."
                            ),
                        }
                    ],
                    claims=[
                        {
                            "kind": "event",
                            "text": "The user completed the basalt atlas.",
                            "subject": "user",
                            "predicate": "completed",
                            "object_text": "basalt atlas",
                        },
                        {
                            "kind": "fact",
                            "text": "The basalt atlas has 200 pages.",
                            "subject": "basalt atlas",
                            "predicate": "page count",
                            "object_text": "200 pages",
                        },
                    ],
                )
                blocks = engine.search_memory_blocks(
                    (
                        "What was the combined page count of the two atlases I "
                        "completed?"
                    ),
                    limit=20,
                ).blocks
                self.assertFalse(
                    self._scalar_blocks(
                        blocks, "composite_measurement_resolution"
                    )
                )

    def test_coordinated_duration_scope_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with Engine(
                str(Path(directory) / "memory.db"),
                user_id="duration-sum",
                semantic_dedup=False,
                local_only=True,
            ) as engine:
                fixtures = (
                    (
                        "april-lecture",
                        "I attended a 1-day lecture in April.",
                        "The user attended a 1-day lecture in April.",
                        "1-day lecture",
                        1_681_084_800.0,
                    ),
                    (
                        "april-workshop",
                        "I attended a 2-day workshop in April.",
                        "The user attended a 2-day workshop in April.",
                        "2-day workshop",
                        1_681_689_600.0,
                    ),
                )
                for session_id, source, claim_text, obj, event_start in fixtures:
                    self._materialize(
                        engine,
                        session_id=session_id,
                        occurred_at=event_start,
                        messages=[{"role": "user", "content": source}],
                        claims=[
                            {
                                "kind": "event",
                                "text": claim_text,
                                "subject": "user",
                                "predicate": "attended",
                                "object_text": obj,
                                "event_start": event_start,
                            }
                        ],
                    )

                blocks = engine.search_memory_blocks(
                    (
                        "How many days did I spend attending workshops, lectures, "
                        "and conferences in April?"
                    ),
                    limit=20,
                ).blocks
                scalar_packs = [
                    block
                    for block in blocks
                    if block.kind == "evidence_pack"
                    and "Computed scalar" in block.text
                ]
                self.assertFalse(scalar_packs)

    def test_duration_absent_from_cited_quote_cannot_certify_scalar(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with Engine(
                str(Path(directory) / "memory.db"),
                user_id="duration-quote-mismatch",
                semantic_dedup=False,
                local_only=True,
            ) as engine:
                message_ids = self._materialize(
                    engine,
                    session_id="workshop-duration-quote-mismatch",
                    occurred_at=100.0,
                    messages=[
                        {
                            "role": "user",
                            "content": "I attended the workshop.",
                        }
                    ],
                    claims=[
                        {
                            "kind": "event",
                            "text": "The user attended a 2-day workshop.",
                            "subject": "user",
                            "predicate": "attended",
                            "object_text": "2-day workshop",
                        }
                    ],
                )
                pack = engine._memory._build_aggregation_pack(
                    "How many days did I spend attending workshops?",
                    message_ids,
                    max_chars=2_000,
                )
                self.assertIsNotNone(pack)
                assert pack is not None
                self.assertNotIn("Computed scalar", pack.block.text)

    def test_detached_source_grounded_duration_companion_is_used(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with Engine(
                str(Path(directory) / "memory.db"),
                user_id="detached-duration-companion",
                semantic_dedup=False,
                local_only=True,
            ) as engine:
                message_ids = self._materialize(
                    engine,
                    session_id="two-day-workshop",
                    occurred_at=100.0,
                    messages=[
                        {
                            "role": "user",
                            "content": "I attended a 2-day cedar workshop.",
                        }
                    ],
                    claims=[
                        {
                            "kind": "event",
                            "text": "The user attended the cedar workshop.",
                            "subject": "user",
                            "predicate": "attended",
                            "object_text": "cedar workshop",
                        },
                        {
                            "kind": "fact",
                            "text": "The cedar workshop lasted 2 days.",
                            "subject": "cedar workshop",
                            "predicate": "duration",
                            "object_text": "2 days",
                        },
                    ],
                )
                pack = engine._memory._build_aggregation_pack(
                    "How many days did I spend attending workshops?",
                    message_ids,
                    max_chars=2_000,
                )
                self.assertIsNotNone(pack)
                assert pack is not None
                self.assertIn(
                    "event durations: 2 days (2 = 2)",
                    pack.block.text,
                )
                self.assertNotIn("event durations: 1 day", pack.block.text)

    def test_cumulative_scalar_identity_is_invariant_across_result_limits(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with Engine(
                str(Path(directory) / "memory.db"),
                user_id="limit-invariant-counter",
                semantic_dedup=False,
                local_only=True,
            ) as engine:
                for index, total in enumerate((3, 8), start=1):
                    marker = "already" if total == 3 else "now"
                    source = (
                        "I have calibrated the brass telescope "
                        f"{total} times {marker}."
                    )
                    self._materialize(
                        engine,
                        session_id=f"limit-counter-{index}",
                        occurred_at=float(index * 100),
                        messages=[{"role": "user", "content": source}],
                        claims=[
                            {
                                "kind": "fact",
                                "text": source.replace("I have", "The user has"),
                                "subject": "user",
                                "predicate": "has calibrated",
                                "object_text": "brass telescope calibration count",
                            }
                        ],
                    )

                identities = []
                for limit in (1, 20, 50):
                    blocks = engine.search_memory_blocks(
                        "How many times have I calibrated the brass telescope?",
                        limit=limit,
                    ).blocks
                    resolutions = self._scalar_blocks(
                        blocks, "cumulative_counter_resolution"
                    )
                    self.assertEqual(len(resolutions), 1)
                    resolution = resolutions[0]
                    identities.append(
                        (
                            resolution.text,
                            resolution.composite_id,
                            resolution.message_ids,
                        )
                    )
                self.assertEqual(identities[1:], identities[:1] * 2)

    def test_mixed_user_and_assistant_sources_cannot_certify_a_scalar(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with Engine(
                str(Path(directory) / "memory.db"),
                user_id="mixed-source-claim",
                semantic_dedup=False,
                local_only=True,
            ) as engine:
                messages = [
                    {
                        "role": "user",
                        "content": "I acquired 4 enamel pins at the market.",
                    },
                    {
                        "role": "assistant",
                        "content": "That means you acquired four enamel pins.",
                    },
                ]
                stored = engine.store_session(
                    messages,
                    session_id="mixed-evidence-acquisition",
                    occurred_at=100.0,
                )
                job_id = engine.enqueue_compilation(
                    stored["session_pk"],
                    stored["source_hash"],
                    "v18-mixed-source-compiler",
                )
                result = engine.apply_compilation(
                    job_id,
                    {
                        "claims": [
                            {
                                "kind": "event",
                                "text": "The user acquired 4 enamel pins.",
                                "subject": "user",
                                "predicate": "acquired",
                                "object_text": "4 enamel pins",
                                "evidence": [
                                    {
                                        "message_id": stored["message_ids"][0],
                                        "quote": messages[0]["content"],
                                    },
                                    {
                                        "message_id": stored["message_ids"][1],
                                        "quote": messages[1]["content"],
                                    },
                                ],
                            }
                        ]
                    },
                    processor="v18-synthetic-test",
                    processor_version="1",
                    prompt_version="1",
                )
                self.assertIn(result["status"], {"complete", "partial"})

                pack = engine._memory._build_aggregation_pack(
                    "How many enamel pins did I acquire?",
                    [stored["message_ids"][0]],
                    max_chars=2_000,
                )
                self.assertIsNotNone(pack)
                assert pack is not None
                self.assertIn("The user acquired 4 enamel pins", pack.block.text)
                self.assertNotIn("Computed scalar", pack.block.text)

    def test_more_than_twelve_claims_in_one_source_cannot_certify_a_scalar(
        self,
    ) -> None:
        instrument_names = (
            "amber",
            "birch",
            "cobalt",
            "dahlia",
            "elm",
            "flint",
            "garnet",
            "hazel",
            "indigo",
            "juniper",
            "kelp",
            "lilac",
            "zircon",
        )
        with tempfile.TemporaryDirectory() as directory:
            with Engine(
                str(Path(directory) / "memory.db"),
                user_id="per-source-claim-cap",
                semantic_dedup=False,
                local_only=True,
            ) as engine:
                source = "I catalogued these observatory instruments: " + ", ".join(
                    instrument_names
                )
                message_ids = self._materialize(
                    engine,
                    session_id="large-instrument-catalogue",
                    occurred_at=100.0,
                    messages=[{"role": "user", "content": source}],
                    claims=[
                        {
                            "kind": "event",
                            "text": (
                                f"The user catalogued the {name} observatory "
                                "instrument."
                            ),
                            "subject": "user",
                            "predicate": "catalogued",
                            "object_text": f"{name} observatory instrument",
                        }
                        for name in instrument_names
                    ],
                )
                pack = engine._memory._build_aggregation_pack(
                    "How many observatory instruments did I catalogue?",
                    message_ids,
                    max_chars=10_000,
                )
                self.assertIsNotNone(pack)
                assert pack is not None
                self.assertNotIn("Computed scalar", pack.block.text)
                self.assertNotIn("zircon observatory instrument", pack.block.text)

    def test_cumulative_counter_ignores_future_and_third_party_totals(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with Engine(
                str(Path(directory) / "memory.db"),
                user_id="counter-actor-and-completion-scope",
                semantic_dedup=False,
                local_only=True,
            ) as engine:
                valid_ids = []
                for index, total in enumerate((3, 5), start=1):
                    marker = "already" if total == 3 else "now"
                    source = (
                        "I have calibrated the brass telescope "
                        f"{total} times {marker}."
                    )
                    valid_ids.extend(
                        self._materialize(
                            engine,
                            session_id=f"completed-calibration-{index}",
                            occurred_at=float(index * 100),
                            messages=[{"role": "user", "content": source}],
                            claims=[
                                {
                                    "kind": "fact",
                                    "text": source.replace(
                                        "I have", "The user has"
                                    ),
                                    "subject": "user",
                                    "predicate": "has calibrated",
                                    "object_text": (
                                        "brass telescope calibration count"
                                    ),
                                }
                            ],
                        )
                    )

                future_ids = self._materialize(
                    engine,
                    session_id="future-calibration-plan",
                    occurred_at=300.0,
                    messages=[
                        {
                            "role": "user",
                            "content": (
                                "I plan to calibrate the brass telescope 99 times "
                                "now as part of next year's training target."
                            ),
                        }
                    ],
                    claims=[
                        {
                            "kind": "event",
                            "text": (
                                "The user plans to calibrate the brass telescope "
                                "99 times as a future training target."
                            ),
                            "subject": "user",
                            "predicate": "plans_to_calibrate",
                            "object_text": "brass telescope 99 times",
                        }
                    ],
                )
                third_party_ids = self._materialize(
                    engine,
                    session_id="sister-calibration-total",
                    occurred_at=400.0,
                    messages=[
                        {
                            "role": "user",
                            "content": (
                                "My sister has calibrated the brass telescope "
                                "50 times now."
                            ),
                        }
                    ],
                    claims=[
                        {
                            "kind": "fact",
                            "text": (
                                "The user's sister has calibrated the brass "
                                "telescope 50 times now."
                            ),
                            "subject": "sister",
                            "predicate": "has calibrated",
                            "object_text": "brass telescope 50 times now",
                        }
                    ],
                )

                blocks = engine.search_memory_blocks(
                    "How many times have I calibrated the brass telescope?",
                    limit=20,
                ).blocks
                resolutions = self._scalar_blocks(
                    blocks, "cumulative_counter_resolution"
                )
                self.assertEqual(len(resolutions), 1)
                resolution = resolutions[0]
                self.assertIn("5 times", resolution.text)
                self.assertIn("Candidate cumulative totals: 3, 5", resolution.text)
                self.assertNotIn("50", resolution.text)
                self.assertNotIn("99", resolution.text)
                self.assertEqual(set(resolution.message_ids), set(valid_ids))
                self.assertTrue(
                    set(future_ids + third_party_ids).isdisjoint(
                        resolution.message_ids
                    )
                )

    def test_planned_atlas_completions_cannot_certify_a_measurement_sum(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with Engine(
                str(Path(directory) / "memory.db"),
                user_id="planned-atlas-measurements",
                semantic_dedup=False,
                local_only=True,
            ) as engine:
                for index, (name, pages) in enumerate(
                    (("cedar", 140), ("basalt", 260)),
                    start=1,
                ):
                    self._materialize(
                        engine,
                        session_id=f"planned-{name}-atlas",
                        occurred_at=float(index * 100),
                        messages=[
                            {
                                "role": "user",
                                "content": (
                                    f"I plan to complete the {name} atlas next "
                                    f"month. It has {pages} pages."
                                ),
                            }
                        ],
                        claims=[
                            {
                                "kind": "event",
                                "text": (
                                    f"The user plans to complete the {name} atlas."
                                ),
                                "subject": "user",
                                "predicate": "plans_to_complete",
                                "object_text": f"{name} atlas",
                            },
                            {
                                "kind": "fact",
                                "text": f"The {name} atlas has {pages} pages.",
                                "subject": f"{name} atlas",
                                "predicate": "page count",
                                "object_text": f"{pages} pages",
                            },
                        ],
                    )

                blocks = engine.search_memory_blocks(
                    (
                        "What was the combined page count of the two atlases I "
                        "completed?"
                    ),
                    limit=20,
                ).blocks
                self.assertFalse(
                    self._scalar_blocks(
                        blocks, "composite_measurement_resolution"
                    )
                )
                self.assertFalse(
                    any("Computed scalar" in block.text for block in blocks)
                )

    def test_pending_explicit_quantity_counts_requested_items(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with Engine(
                str(Path(directory) / "memory.db"),
                user_id="pending-battery-quantity",
                semantic_dedup=False,
                local_only=True,
            ) as engine:
                self._materialize(
                    engine,
                    session_id="battery-shopping",
                    occurred_at=100.0,
                    messages=[
                        {
                            "role": "user",
                            "content": "I still need to buy 4 batteries.",
                        }
                    ],
                    claims=[
                        {
                            "kind": "fact",
                            "text": "The user still needs to buy 4 batteries.",
                            "subject": "user",
                            "predicate": "needs_to_buy",
                            "object_text": "4 batteries",
                            "memory_key": "user.shopping.batteries.buy",
                        }
                    ],
                )

                pack = engine.search_memory_blocks(
                    "How many batteries do I need to buy?",
                    limit=20,
                ).blocks[0]
                self.assertEqual(pack.kind, "evidence_pack")
                self.assertIn(
                    "open obligations: 4 requested items",
                    pack.text,
                )
                self.assertNotIn("1 distinct action-object group", pack.text)

    def test_equivalent_same_source_object_claims_count_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with Engine(
                str(Path(directory) / "memory.db"),
                user_id="equivalent-necklace-claims",
                semantic_dedup=False,
                local_only=True,
            ) as engine:
                message_ids = self._materialize(
                    engine,
                    session_id="ruby-necklace-acquisition",
                    occurred_at=100.0,
                    messages=[
                        {
                            "role": "user",
                            "content": "I acquired a ruby necklace at the market.",
                        }
                    ],
                    claims=[
                        {
                            "kind": "event",
                            "text": "The user acquired a ruby necklace.",
                            "subject": "user",
                            "predicate": "acquired",
                            "object_text": "a ruby necklace",
                        },
                        {
                            "kind": "event",
                            "text": "The user acquired the ruby necklace.",
                            "subject": "user",
                            "predicate": "acquired",
                            "object_text": "the ruby necklace",
                        },
                    ],
                )
                pack = engine._memory._build_aggregation_pack(
                    "How many ruby necklaces did I acquire?",
                    message_ids,
                    max_chars=2_000,
                )
                self.assertIsNotNone(pack)
                assert pack is not None
                self.assertIn(
                    "evidence: 1 completed action-object groups/items (1 = 1)",
                    pack.block.text,
                )
                self.assertNotIn(
                    "evidence: 2 completed action-object groups/items",
                    pack.block.text,
                )

    def test_unsupported_pending_quantities_do_not_fall_back_to_one(
        self,
    ) -> None:
        for quantity in ("a dozen", "twenty-one"):
            with self.subTest(quantity=quantity):
                with tempfile.TemporaryDirectory() as directory:
                    with Engine(
                        str(Path(directory) / "memory.db"),
                        user_id=f"unsupported-pending-{quantity}",
                        semantic_dedup=False,
                        local_only=True,
                    ) as engine:
                        source = f"I still need to buy {quantity} batteries."
                        self._materialize(
                            engine,
                            session_id="battery-shopping",
                            occurred_at=100.0,
                            messages=[{"role": "user", "content": source}],
                            claims=[
                                {
                                    "kind": "fact",
                                    "text": source.replace(
                                        "I still need", "The user still needs"
                                    ),
                                    "subject": "user",
                                    "predicate": "needs_to_buy",
                                    "object_text": f"{quantity} batteries",
                                    "memory_key": "user.shopping.batteries.buy",
                                }
                            ],
                        )
                        blocks = engine.search_memory_blocks(
                            "How many batteries do I need to buy?",
                            limit=20,
                        ).blocks
                        self.assertFalse(
                            any("Computed scalar" in block.text for block in blocks)
                        )

    def test_measurement_value_absent_from_cited_quote_cannot_certify_sum(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with Engine(
                str(Path(directory) / "memory.db"),
                user_id="measurement-quote-mismatch",
                semantic_dedup=False,
                local_only=True,
            ) as engine:
                for index, (name, pages) in enumerate(
                    (("cedar", 140), ("basalt", 260)),
                    start=1,
                ):
                    self._materialize(
                        engine,
                        session_id=f"{name}-atlas-quote-mismatch",
                        occurred_at=float(index * 100),
                        messages=[
                            {
                                "role": "user",
                                "content": f"I completed the {name} atlas.",
                            }
                        ],
                        claims=[
                            {
                                "kind": "event",
                                "text": f"The user completed the {name} atlas.",
                                "subject": "user",
                                "predicate": "completed",
                                "object_text": f"{name} atlas",
                            },
                            {
                                "kind": "fact",
                                "text": f"The {name} atlas has {pages} pages.",
                                "subject": f"{name} atlas",
                                "predicate": "page count",
                                "object_text": f"{pages} pages",
                            },
                        ],
                    )
                blocks = engine.search_memory_blocks(
                    (
                        "What was the combined page count of the two atlases I "
                        "completed?"
                    ),
                    limit=20,
                ).blocks
                self.assertFalse(
                    self._scalar_blocks(
                        blocks, "composite_measurement_resolution"
                    )
                )
                self.assertFalse(
                    any("Computed scalar" in block.text for block in blocks)
                )

    def test_cumulative_value_absent_from_cited_quote_cannot_resolve(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with Engine(
                str(Path(directory) / "memory.db"),
                user_id="cumulative-quote-mismatch",
                semantic_dedup=False,
                local_only=True,
            ) as engine:
                for index, total in enumerate((3, 5), start=1):
                    self._materialize(
                        engine,
                        session_id=f"calibration-quote-mismatch-{index}",
                        occurred_at=float(index * 100),
                        messages=[
                            {
                                "role": "user",
                                "content": (
                                    "I calibrated the brass telescope during "
                                    "maintenance."
                                ),
                            }
                        ],
                        claims=[
                            {
                                "kind": "fact",
                                "text": (
                                    "The user has calibrated the brass telescope "
                                    f"{total} times "
                                    f"{'already' if total == 3 else 'now'}."
                                ),
                                "subject": "user",
                                "predicate": "has calibrated",
                                "object_text": (
                                    f"brass telescope {total} times "
                                    f"{'already' if total == 3 else 'now'}"
                                ),
                            }
                        ],
                    )
                blocks = engine.search_memory_blocks(
                    "How many times have I calibrated the brass telescope?",
                    limit=20,
                ).blocks
                self.assertFalse(
                    self._scalar_blocks(
                        blocks, "cumulative_counter_resolution"
                    )
                )

    def test_other_person_user_authored_totals_cannot_enter_user_counter(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with Engine(
                str(Path(directory) / "memory.db"),
                user_id="same-message-other-person-counter",
                semantic_dedup=False,
                local_only=True,
            ) as engine:
                for index, total in enumerate((3, 5), start=1):
                    marker = "already" if total == 3 else "now"
                    source = (
                        "My sister has calibrated the brass telescope "
                        f"{total} times {marker}."
                    )
                    self._materialize(
                        engine,
                        session_id=f"sister-calibration-{index}",
                        occurred_at=float(index * 100),
                        messages=[{"role": "user", "content": source}],
                        claims=[
                            {
                                "kind": "fact",
                                "text": (
                                    "The user's sister has calibrated the brass "
                                    f"telescope {total} times {marker}."
                                ),
                                "subject": "user's sister",
                                "predicate": "has calibrated",
                                "object_text": (
                                    f"brass telescope {total} times {marker}"
                                ),
                            }
                        ],
                    )
                blocks = engine.search_memory_blocks(
                    "How many times have I calibrated the brass telescope?",
                    limit=20,
                ).blocks
                self.assertFalse(
                    self._scalar_blocks(
                        blocks, "cumulative_counter_resolution"
                    )
                )

    def test_distinct_cumulative_subcounter_identities_do_not_merge(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with Engine(
                str(Path(directory) / "memory.db"),
                user_id="distinct-subcounters",
                semantic_dedup=False,
                local_only=True,
            ) as engine:
                fixtures = (
                    (
                        "primary-lens-counter",
                        "primary lens",
                        3,
                        "already",
                    ),
                    (
                        "tracking-motor-counter",
                        "tracking motor",
                        8,
                        "now",
                    ),
                )
                for index, (session_id, component, total, marker) in enumerate(
                    fixtures,
                    start=1,
                ):
                    source = (
                        "I have calibrated the brass telescope "
                        f"{component} {total} times {marker}."
                    )
                    self._materialize(
                        engine,
                        session_id=session_id,
                        occurred_at=float(index * 100),
                        messages=[{"role": "user", "content": source}],
                        claims=[
                            {
                                "kind": "fact",
                                "text": source.replace(
                                    "I have", "The user has"
                                ),
                                "subject": "user",
                                "predicate": "has calibrated",
                                "object_text": (
                                    f"brass telescope {component} {total} times "
                                    f"{marker}"
                                ),
                            }
                        ],
                    )
                blocks = engine.search_memory_blocks(
                    "How many times have I calibrated the brass telescope?",
                    limit=20,
                ).blocks
                self.assertFalse(
                    self._scalar_blocks(
                        blocks, "cumulative_counter_resolution"
                    )
                )

    def test_approximate_and_range_measurements_do_not_become_operands(
        self,
    ) -> None:
        for uncertain_measurement in (
            "about 140 pages",
            "between 140 and 150 pages",
        ):
            with self.subTest(measurement=uncertain_measurement):
                with tempfile.TemporaryDirectory() as directory:
                    with Engine(
                        str(Path(directory) / "memory.db"),
                        user_id=f"uncertain-measurement-{uncertain_measurement}",
                        semantic_dedup=False,
                        local_only=True,
                    ) as engine:
                        fixtures = (
                            ("cedar", uncertain_measurement),
                            ("basalt", "260 pages"),
                        )
                        for index, (name, measurement) in enumerate(
                            fixtures,
                            start=1,
                        ):
                            source = (
                                f"I completed the {name} atlas. It has "
                                f"{measurement}."
                            )
                            self._materialize(
                                engine,
                                session_id=f"{name}-uncertain-atlas",
                                occurred_at=float(index * 100),
                                messages=[{"role": "user", "content": source}],
                                claims=[
                                    {
                                        "kind": "event",
                                        "text": (
                                            f"The user completed the {name} atlas."
                                        ),
                                        "subject": "user",
                                        "predicate": "completed",
                                        "object_text": f"{name} atlas",
                                    },
                                    {
                                        "kind": "fact",
                                        "text": (
                                            f"The {name} atlas has {measurement}."
                                        ),
                                        "subject": f"{name} atlas",
                                        "predicate": "page count",
                                        "object_text": measurement,
                                    },
                                ],
                            )
                        blocks = engine.search_memory_blocks(
                            (
                                "What was the combined page count of the two "
                                "atlases I completed?"
                            ),
                            limit=20,
                        ).blocks
                        self.assertFalse(
                            any("Computed scalar" in block.text for block in blocks)
                        )

    def test_measurement_rejects_temporally_conflicting_action_support(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with Engine(
                str(Path(directory) / "memory.db"),
                user_id="measurement-action-time-conflict",
                semantic_dedup=False,
                local_only=True,
            ) as engine:
                fixtures = (
                    ("cedar", 140, "April", 1_681_084_800.0),
                    ("basalt", 260, "March", 1_678_406_400.0),
                )
                for name, pages, month, event_start in fixtures:
                    source = (
                        f"I completed the {name} atlas in {month}. "
                        f"It has {pages} pages."
                    )
                    self._materialize(
                        engine,
                        session_id=f"{name}-{month}-atlas",
                        occurred_at=event_start,
                        messages=[{"role": "user", "content": source}],
                        claims=[
                            {
                                "kind": "event",
                                "text": (
                                    f"The user completed the {name} atlas in "
                                    f"{month}."
                                ),
                                "subject": "user",
                                "predicate": "completed",
                                "object_text": f"{name} atlas",
                                "event_start": event_start,
                            },
                            {
                                "kind": "fact",
                                "text": f"The {name} atlas has {pages} pages.",
                                "subject": f"{name} atlas",
                                "predicate": "page count",
                                "object_text": f"{pages} pages",
                            },
                        ],
                    )
                blocks = engine.search_memory_blocks(
                    (
                        "What was the combined page count of the two atlases I "
                        "completed in March?"
                    ),
                    limit=20,
                ).blocks
                self.assertFalse(
                    any("Computed scalar" in block.text for block in blocks)
                )

    def test_identical_measurement_retellings_do_not_double_sum(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with Engine(
                str(Path(directory) / "memory.db"),
                user_id="measurement-retellings",
                semantic_dedup=False,
                local_only=True,
            ) as engine:
                event_start = 1_681_084_800.0
                source = "I completed the cedar atlas, which has 140 pages."
                for index in range(2):
                    self._materialize(
                        engine,
                        session_id=f"cedar-atlas-retelling-{index}",
                        occurred_at=float(100 + index),
                        messages=[{"role": "user", "content": source}],
                        claims=[
                            {
                                "kind": "event",
                                "text": "The user completed the cedar atlas.",
                                "subject": "user",
                                "predicate": "completed",
                                "object_text": "cedar atlas",
                                "event_start": event_start,
                            },
                            {
                                "kind": "fact",
                                "text": "The cedar atlas has 140 pages.",
                                "subject": "cedar atlas",
                                "predicate": "page count",
                                "object_text": "140 pages",
                            },
                        ],
                    )
                blocks = engine.search_memory_blocks(
                    (
                        "What was the combined page count of the two atlases I "
                        "completed?"
                    ),
                    limit=20,
                ).blocks
                self.assertFalse(
                    self._scalar_blocks(
                        blocks, "composite_measurement_resolution"
                    )
                )
                self.assertFalse(
                    any("Computed scalar" in block.text for block in blocks)
                )

    def test_relative_year_measurement_scope_rejects_this_year_operand(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with Engine(
                str(Path(directory) / "memory.db"),
                user_id="relative-year-measurements",
                semantic_dedup=False,
                local_only=True,
            ) as engine:
                fixtures = (
                    ("cedar", 140, "last year", 1_704_067_200.0),
                    ("basalt", 260, "this year", 1_735_689_600.0),
                )
                for name, pages, period, event_start in fixtures:
                    source = (
                        f"I completed the {name} atlas {period}. "
                        f"It has {pages} pages."
                    )
                    self._materialize(
                        engine,
                        session_id=f"{name}-{period.replace(' ', '-')}-atlas",
                        occurred_at=event_start,
                        messages=[{"role": "user", "content": source}],
                        claims=[
                            {
                                "kind": "event",
                                "text": (
                                    f"The user completed the {name} atlas "
                                    f"{period}."
                                ),
                                "subject": "user",
                                "predicate": "completed",
                                "object_text": f"{name} atlas",
                                "event_start": event_start,
                            },
                            {
                                "kind": "fact",
                                "text": f"The {name} atlas has {pages} pages.",
                                "subject": f"{name} atlas",
                                "predicate": "page count",
                                "object_text": f"{pages} pages",
                            },
                        ],
                    )
                blocks = engine.search_memory_blocks(
                    (
                        "What was the combined page count of the two atlases I "
                        "completed last year?"
                    ),
                    limit=20,
                ).blocks
                self.assertFalse(
                    any("Computed scalar" in block.text for block in blocks)
                )

    def test_one_token_project_name_overlap_does_not_link_manual_pages(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with Engine(
                str(Path(directory) / "memory.db"),
                user_id="project-report-manual-overlap",
                semantic_dedup=False,
                local_only=True,
            ) as engine:
                for index, (name, pages) in enumerate(
                    (("Cedar", 140), ("Basalt", 260)),
                    start=1,
                ):
                    source = (
                        f"I completed the {name} project report. The {name} "
                        f"operations manual has {pages} pages."
                    )
                    self._materialize(
                        engine,
                        session_id=f"{name.casefold()}-project-and-manual",
                        occurred_at=float(index * 100),
                        messages=[{"role": "user", "content": source}],
                        claims=[
                            {
                                "kind": "event",
                                "text": (
                                    f"The user completed the {name} project report."
                                ),
                                "subject": "user",
                                "predicate": "completed",
                                "object_text": f"{name} project report",
                            },
                            {
                                "kind": "fact",
                                "text": (
                                    f"The {name} operations manual has {pages} "
                                    "pages."
                                ),
                                "subject": f"{name} operations manual",
                                "predicate": "page count",
                                "object_text": f"{pages} pages",
                            },
                        ],
                    )
                blocks = engine.search_memory_blocks(
                    (
                        "What was the combined page count of the two project "
                        "reports I completed?"
                    ),
                    limit=20,
                ).blocks
                self.assertFalse(
                    any("Computed scalar" in block.text for block in blocks)
                )

    def test_measurement_retelling_with_event_time_drift_is_not_two_operands(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with Engine(
                str(Path(directory) / "memory.db"),
                user_id="measurement-retelling-time-drift",
                semantic_dedup=False,
                local_only=True,
            ) as engine:
                source = "I completed the cedar atlas, which has 140 pages."
                for index, event_start in enumerate((1_000.0, 2_000.0)):
                    self._materialize(
                        engine,
                        session_id=f"cedar-atlas-time-drift-{index}",
                        occurred_at=float(100 + index),
                        messages=[{"role": "user", "content": source}],
                        claims=[
                            {
                                "kind": "event",
                                "text": "The user completed the cedar atlas.",
                                "subject": "user",
                                "predicate": "completed",
                                "object_text": "cedar atlas",
                                "event_start": event_start,
                            },
                            {
                                "kind": "fact",
                                "text": "The cedar atlas has 140 pages.",
                                "subject": "cedar atlas",
                                "predicate": "page count",
                                "object_text": "140 pages",
                            },
                        ],
                    )
                blocks = engine.search_memory_blocks(
                    (
                        "What was the combined page count of the two atlases I "
                        "completed?"
                    ),
                    limit=20,
                ).blocks
                self.assertFalse(
                    any("Computed scalar" in block.text for block in blocks)
                )

    def test_multisource_measurement_requires_operand_in_every_cited_quote(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with Engine(
                str(Path(directory) / "memory.db"),
                user_id="multisource-measurement-grounding",
                semantic_dedup=False,
                local_only=True,
            ) as engine:
                messages = [
                    {
                        "role": "user",
                        "content": (
                            "I completed the cedar atlas. It has 140 pages."
                        ),
                    },
                    {
                        "role": "user",
                        "content": "I discussed the cedar atlas binding again.",
                    },
                ]
                stored = engine.store_session(
                    messages,
                    session_id="multisource-cedar-atlas",
                    occurred_at=100.0,
                )
                job_id = engine.enqueue_compilation(
                    stored["session_pk"],
                    stored["source_hash"],
                    "v18-multisource-measurement-compiler",
                )
                result = engine.apply_compilation(
                    job_id,
                    {
                        "claims": [
                            {
                                "kind": "event",
                                "text": "The user completed the cedar atlas.",
                                "subject": "user",
                                "predicate": "completed",
                                "object_text": "cedar atlas",
                                "evidence": [
                                    {
                                        "message_id": stored["message_ids"][0],
                                        "quote": messages[0]["content"],
                                    }
                                ],
                            },
                            {
                                "kind": "fact",
                                "text": "The cedar atlas has 140 pages.",
                                "subject": "cedar atlas",
                                "predicate": "page count",
                                "object_text": "140 pages",
                                "evidence": [
                                    {
                                        "message_id": stored["message_ids"][0],
                                        "quote": messages[0]["content"],
                                    },
                                    {
                                        "message_id": stored["message_ids"][1],
                                        "quote": messages[1]["content"],
                                    },
                                ],
                            },
                        ]
                    },
                    processor="v18-synthetic-test",
                    processor_version="1",
                    prompt_version="1",
                )
                self.assertIn(result["status"], {"complete", "partial"})
                blocks = engine.search_memory_blocks(
                    "What was the page count of the one atlas I completed?",
                    limit=20,
                ).blocks
                self.assertFalse(
                    any("Computed scalar" in block.text for block in blocks)
                )

    def test_structured_april_event_cannot_support_march_measurement(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with Engine(
                str(Path(directory) / "memory.db"),
                user_id="structured-action-time-conflict",
                semantic_dedup=False,
                local_only=True,
            ) as engine:
                fixtures = (
                    (
                        "cedar",
                        140,
                        "I completed the cedar atlas. It has 140 pages.",
                        "The user completed the cedar atlas.",
                        1_681_084_800.0,
                    ),
                    (
                        "basalt",
                        260,
                        (
                            "I completed the basalt atlas in March. It has "
                            "260 pages."
                        ),
                        "The user completed the basalt atlas in March.",
                        1_678_406_400.0,
                    ),
                )
                for name, pages, source, action_text, event_start in fixtures:
                    self._materialize(
                        engine,
                        session_id=f"{name}-structured-time-atlas",
                        occurred_at=event_start,
                        messages=[{"role": "user", "content": source}],
                        claims=[
                            {
                                "kind": "event",
                                "text": action_text,
                                "subject": "user",
                                "predicate": "completed",
                                "object_text": f"{name} atlas",
                                "event_start": event_start,
                            },
                            {
                                "kind": "fact",
                                "text": f"The {name} atlas has {pages} pages.",
                                "subject": f"{name} atlas",
                                "predicate": "page count",
                                "object_text": f"{pages} pages",
                            },
                        ],
                    )
                blocks = engine.search_memory_blocks(
                    (
                        "What was the combined page count of the two atlases I "
                        "completed in March?"
                    ),
                    limit=20,
                ).blocks
                self.assertFalse(
                    any("Computed scalar" in block.text for block in blocks)
                )

    def test_unnumbered_plural_does_not_certify_one_but_singular_group_can(
        self,
    ) -> None:
        with self.subTest(surface="unnumbered-plural"):
            with tempfile.TemporaryDirectory() as directory:
                with Engine(
                    str(Path(directory) / "memory.db"),
                    user_id="unnumbered-plural-acquisition",
                    semantic_dedup=False,
                    local_only=True,
                ) as engine:
                    message_ids = self._materialize(
                        engine,
                        session_id="plural-necklace-acquisition",
                        occurred_at=100.0,
                        messages=[
                            {
                                "role": "user",
                                "content": "I acquired ruby necklaces.",
                            }
                        ],
                        claims=[
                            {
                                "kind": "event",
                                "text": "The user acquired ruby necklaces.",
                                "subject": "user",
                                "predicate": "acquired",
                                "object_text": "ruby necklaces",
                            }
                        ],
                    )
                    pack = engine._memory._build_aggregation_pack(
                        "How many ruby necklaces did I acquire?",
                        message_ids,
                        max_chars=2_000,
                    )
                    self.assertIsNotNone(pack)
                    assert pack is not None
                    self.assertNotIn("Computed scalar", pack.block.text)

        with self.subTest(surface="singular-group"):
            with tempfile.TemporaryDirectory() as directory:
                with Engine(
                    str(Path(directory) / "memory.db"),
                    user_id="singular-group-acquisition",
                    semantic_dedup=False,
                    local_only=True,
                ) as engine:
                    message_ids = self._materialize(
                        engine,
                        session_id="singular-necklace-acquisition",
                        occurred_at=100.0,
                        messages=[
                            {
                                "role": "user",
                                "content": "I acquired a ruby necklace.",
                            }
                        ],
                        claims=[
                            {
                                "kind": "event",
                                "text": "The user acquired a ruby necklace.",
                                "subject": "user",
                                "predicate": "acquired",
                                "object_text": "a ruby necklace",
                            }
                        ],
                    )
                    pack = engine._memory._build_aggregation_pack(
                        "How many ruby necklace groups did I acquire?",
                        message_ids,
                        max_chars=2_000,
                    )
                    self.assertIsNotNone(pack)
                    assert pack is not None
                    self.assertIn(
                        "evidence: 1 completed action-object groups/items",
                        pack.block.text,
                    )

    def test_pending_obligations_reject_non_user_actors(self) -> None:
        fixtures = (
            (
                "alice",
                "Alice needs to buy 4 batteries.",
                "Alice needs to buy 4 batteries.",
                "Alice",
            ),
            (
                "sister",
                "My sister needs to buy 4 batteries.",
                "The user's sister needs to buy 4 batteries.",
                "user's sister",
            ),
        )
        for label, source, claim_text, subject in fixtures:
            with self.subTest(actor=label):
                with tempfile.TemporaryDirectory() as directory:
                    with Engine(
                        str(Path(directory) / "memory.db"),
                        user_id=f"pending-other-actor-{label}",
                        semantic_dedup=False,
                        local_only=True,
                    ) as engine:
                        self._materialize(
                            engine,
                            session_id=f"{label}-battery-obligation",
                            occurred_at=100.0,
                            messages=[{"role": "user", "content": source}],
                            claims=[
                                {
                                    "kind": "fact",
                                    "text": claim_text,
                                    "subject": subject,
                                    "predicate": "needs_to_buy",
                                    "object_text": "4 batteries",
                                    "memory_key": f"{label}.shopping.batteries.buy",
                                }
                            ],
                        )
                        blocks = engine.search_memory_blocks(
                            "How many batteries do I need to buy?",
                            limit=20,
                        ).blocks
                        self.assertFalse(
                            any("Computed scalar" in block.text for block in blocks)
                        )

    def test_pending_obligations_reject_negated_or_completed_need(self) -> None:
        fixtures = (
            (
                "negated",
                "I do not need to buy batteries.",
                "does_not_need_to_buy",
            ),
            (
                "no-longer",
                "I no longer need to buy batteries.",
                "no_longer_needs_to_buy",
            ),
            (
                "already-completed",
                "I needed to buy batteries, but I already bought them.",
                "needed_to_buy_but_already_bought",
            ),
        )
        for label, source, predicate in fixtures:
            with self.subTest(state=label):
                with tempfile.TemporaryDirectory() as directory:
                    with Engine(
                        str(Path(directory) / "memory.db"),
                        user_id=f"closed-pending-{label}",
                        semantic_dedup=False,
                        local_only=True,
                    ) as engine:
                        self._materialize(
                            engine,
                            session_id=f"{label}-battery-state",
                            occurred_at=100.0,
                            messages=[{"role": "user", "content": source}],
                            claims=[
                                {
                                    "kind": "fact",
                                    "text": source.replace("I ", "The user ", 1),
                                    "subject": "user",
                                    "predicate": predicate,
                                    "object_text": "batteries",
                                    "memory_key": "user.shopping.batteries.buy",
                                }
                            ],
                        )
                        blocks = engine.search_memory_blocks(
                            "How many batteries do I need to buy?",
                            limit=20,
                        ).blocks
                        self.assertFalse(
                            any("Computed scalar" in block.text for block in blocks)
                        )

    def test_completed_quantity_parses_plural_and_group_unit_queries(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with Engine(
                str(Path(directory) / "memory.db"),
                user_id="pin-quantity-morphology",
                semantic_dedup=False,
                local_only=True,
            ) as engine:
                message_ids = self._materialize(
                    engine,
                    session_id="four-pin-acquisition",
                    occurred_at=100.0,
                    messages=[
                        {
                            "role": "user",
                            "content": "I acquired 4 enamel pins.",
                        }
                    ],
                    claims=[
                        {
                            "kind": "event",
                            "text": "The user acquired 4 enamel pins.",
                            "subject": "user",
                            "predicate": "acquired",
                            "object_text": "4 enamel pins",
                        }
                    ],
                )
                for query in (
                    "How many pins did I acquire?",
                    "How many pin items did I acquire?",
                ):
                    with self.subTest(query=query):
                        pack = engine._memory._build_aggregation_pack(
                            query,
                            message_ids,
                            max_chars=2_000,
                        )
                        self.assertIsNotNone(pack)
                        assert pack is not None
                        self.assertIn(
                            "evidence: 4 completed action-object groups/items",
                            pack.block.text,
                        )

    def test_other_actor_quantity_cannot_attach_to_user_completed_object(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with Engine(
                str(Path(directory) / "memory.db"),
                user_id="cross-actor-quantity-companion",
                semantic_dedup=False,
                local_only=True,
            ) as engine:
                source = (
                    "I acquired a ruby necklace. "
                    "Alice acquired 4 ruby necklaces."
                )
                message_ids = self._materialize(
                    engine,
                    session_id="mixed-actor-necklace-acquisition",
                    occurred_at=100.0,
                    messages=[{"role": "user", "content": source}],
                    claims=[
                        {
                            "kind": "event",
                            "text": "The user acquired a ruby necklace.",
                            "subject": "user",
                            "predicate": "acquired",
                            "object_text": "a ruby necklace",
                        },
                        {
                            "kind": "event",
                            "text": "Alice acquired 4 ruby necklaces.",
                            "subject": "Alice",
                            "predicate": "acquired",
                            "object_text": "4 ruby necklaces",
                        },
                    ],
                )
                pack = engine._memory._build_aggregation_pack(
                    "How many ruby necklace groups did I acquire?",
                    message_ids,
                    max_chars=2_000,
                )
                self.assertIsNotNone(pack)
                assert pack is not None
                self.assertNotIn("Computed scalar", pack.block.text)
                self.assertNotIn(
                    "evidence: 4 completed action-object groups/items",
                    pack.block.text,
                )

    def test_completed_retelling_with_event_time_drift_does_not_total_two(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with Engine(
                str(Path(directory) / "memory.db"),
                user_id="completed-retelling-time-drift",
                semantic_dedup=False,
                local_only=True,
            ) as engine:
                source_ids = []
                source = "I acquired a ruby necklace at the market."
                for index, event_start in enumerate((1_000.0, 2_000.0)):
                    source_ids.extend(
                        self._materialize(
                            engine,
                            session_id=f"ruby-necklace-retelling-{index}",
                            occurred_at=float(100 + index),
                            messages=[{"role": "user", "content": source}],
                            claims=[
                                {
                                    "kind": "event",
                                    "text": "The user acquired a ruby necklace.",
                                    "subject": "user",
                                    "predicate": "acquired",
                                    "object_text": "a ruby necklace",
                                    "event_start": event_start,
                                }
                            ],
                        )
                    )
                pack = engine._memory._build_aggregation_pack(
                    "How many ruby necklace groups did I acquire?",
                    source_ids,
                    max_chars=4_000,
                )
                self.assertIsNotNone(pack)
                assert pack is not None
                self.assertNotIn("Computed scalar", pack.block.text)
                self.assertNotIn(
                    "evidence: 2 completed action-object groups/items",
                    pack.block.text,
                )

    def test_completed_count_requires_raw_user_completed_action(self) -> None:
        fixtures = (
            "Alice acquired 4 enamel pins.",
            "I did not acquire 4 enamel pins.",
            "I plan to acquire 4 enamel pins.",
        )
        for index, source in enumerate(fixtures):
            with self.subTest(source=source):
                with tempfile.TemporaryDirectory() as directory:
                    with Engine(
                        str(Path(directory) / "memory.db"),
                        user_id=f"raw-completed-count-{index}",
                        semantic_dedup=False,
                        local_only=True,
                    ) as engine:
                        message_ids = self._materialize(
                            engine,
                            session_id=f"raw-count-source-{index}",
                            occurred_at=100.0,
                            messages=[{"role": "user", "content": source}],
                            claims=[
                                {
                                    "kind": "event",
                                    "text": "The user acquired 4 enamel pins.",
                                    "subject": "user",
                                    "predicate": "acquired",
                                    "object_text": "4 enamel pins",
                                }
                            ],
                        )
                        pack = engine._memory._build_aggregation_pack(
                            "How many enamel pins did I acquire?",
                            message_ids,
                            max_chars=2_000,
                        )
                        self.assertIsNotNone(pack)
                        assert pack is not None
                        self.assertNotIn("Computed scalar", pack.block.text)

    def test_duration_requires_raw_user_completed_and_linked_duration(
        self,
    ) -> None:
        direct_fixtures = (
            "Alice attended a 2-day workshop.",
            "I did not attend a 2-day workshop.",
            "I plan to attend a 2-day workshop.",
        )
        for index, source in enumerate(direct_fixtures):
            with self.subTest(kind="direct-action", source=source):
                with tempfile.TemporaryDirectory() as directory:
                    with Engine(
                        str(Path(directory) / "memory.db"),
                        user_id=f"raw-duration-action-{index}",
                        semantic_dedup=False,
                        local_only=True,
                    ) as engine:
                        message_ids = self._materialize(
                            engine,
                            session_id=f"raw-duration-action-{index}",
                            occurred_at=100.0,
                            messages=[{"role": "user", "content": source}],
                            claims=[
                                {
                                    "kind": "event",
                                    "text": (
                                        "The user attended a 2-day workshop."
                                    ),
                                    "subject": "user",
                                    "predicate": "attended",
                                    "object_text": "2-day workshop",
                                }
                            ],
                        )
                        pack = engine._memory._build_aggregation_pack(
                            "How many days did I spend attending workshops?",
                            message_ids,
                            max_chars=2_000,
                        )
                        self.assertIsNotNone(pack)
                        assert pack is not None
                        self.assertNotIn("Computed scalar", pack.block.text)

        companion_fixtures = (
            (
                "vacation",
                "I attended the cedar workshop. My vacation lasted 7 days.",
                "The vacation lasted 7 days.",
                "vacation",
            ),
            (
                "alice",
                (
                    "I attended the cedar workshop. "
                    "Alice attended a 7-day conference."
                ),
                "Alice's conference lasted 7 days.",
                "Alice conference",
            ),
        )
        for label, source, companion_text, companion_subject in companion_fixtures:
            with self.subTest(kind="detached-companion", source=label):
                with tempfile.TemporaryDirectory() as directory:
                    with Engine(
                        str(Path(directory) / "memory.db"),
                        user_id=f"raw-duration-companion-{label}",
                        semantic_dedup=False,
                        local_only=True,
                    ) as engine:
                        message_ids = self._materialize(
                            engine,
                            session_id=f"duration-companion-{label}",
                            occurred_at=100.0,
                            messages=[{"role": "user", "content": source}],
                            claims=[
                                {
                                    "kind": "event",
                                    "text": (
                                        "The user attended the cedar workshop."
                                    ),
                                    "subject": "user",
                                    "predicate": "attended",
                                    "object_text": "cedar workshop",
                                },
                                {
                                    "kind": "fact",
                                    "text": companion_text,
                                    "subject": companion_subject,
                                    "predicate": "duration",
                                    "object_text": "7 days",
                                },
                            ],
                        )
                        pack = engine._memory._build_aggregation_pack(
                            "How many days did I spend attending workshops?",
                            message_ids,
                            max_chars=2_000,
                        )
                        self.assertIsNotNone(pack)
                        assert pack is not None
                        self.assertNotIn("Computed scalar", pack.block.text)

    def test_pending_requires_raw_user_present_open_obligation(self) -> None:
        rejected_sources = (
            "Alice still needs to buy 4 lithium batteries.",
            "My sister still needs to buy 4 lithium batteries.",
            "I needed to buy 4 lithium batteries last year.",
            "I had to buy 4 lithium batteries last year.",
            "I don't have to buy 4 lithium batteries.",
            "I have no need to buy 4 lithium batteries.",
            "I never need to buy 4 lithium batteries.",
        )
        for index, source in enumerate(rejected_sources):
            with self.subTest(source=source):
                with tempfile.TemporaryDirectory() as directory:
                    with Engine(
                        str(Path(directory) / "memory.db"),
                        user_id=f"raw-pending-reject-{index}",
                        semantic_dedup=False,
                        local_only=True,
                    ) as engine:
                        message_ids = self._materialize(
                            engine,
                            session_id=f"raw-pending-reject-{index}",
                            occurred_at=100.0,
                            messages=[{"role": "user", "content": source}],
                            claims=[
                                {
                                    "kind": "fact",
                                    "text": (
                                        "The user still needs to buy 4 lithium "
                                        "batteries."
                                    ),
                                    "subject": "user",
                                    "predicate": "needs_to_buy",
                                    "object_text": "4 lithium batteries",
                                    "memory_key": (
                                        "user.shopping.lithium_batteries.buy"
                                    ),
                                }
                            ],
                        )
                        pack = engine._memory._build_aggregation_pack(
                            "How many lithium batteries do I need to buy?",
                            message_ids,
                            max_chars=2_000,
                            include_uncompleted=True,
                        )
                        if pack is not None:
                            self.assertNotIn("Computed scalar", pack.block.text)

        with self.subTest(source="positive-still-need"):
            with tempfile.TemporaryDirectory() as directory:
                with Engine(
                    str(Path(directory) / "memory.db"),
                    user_id="raw-pending-positive",
                    semantic_dedup=False,
                    local_only=True,
                ) as engine:
                    source = "I still need to buy 4 lithium batteries."
                    message_ids = self._materialize(
                        engine,
                        session_id="raw-pending-positive",
                        occurred_at=100.0,
                        messages=[{"role": "user", "content": source}],
                        claims=[
                            {
                                "kind": "fact",
                                "text": source.replace("I ", "The user ", 1),
                                "subject": "user",
                                "predicate": "needs_to_buy",
                                "object_text": "4 lithium batteries",
                                "memory_key": (
                                    "user.shopping.lithium_batteries.buy"
                                ),
                            }
                        ],
                    )
                    pack = engine._memory._build_aggregation_pack(
                        "How many lithium batteries do I need to buy?",
                        message_ids,
                        max_chars=2_000,
                        include_uncompleted=True,
                    )
                    self.assertIsNotNone(pack)
                    assert pack is not None
                    self.assertIn(
                        "open obligations: 4 requested items",
                        pack.block.text,
                    )

    def test_measurement_requires_raw_user_completed_and_owned_operand(
        self,
    ) -> None:
        raw_action_fixtures = (
            "Alice completed the cedar atlas. It has 140 pages.",
            "I plan to complete the cedar atlas. It has 140 pages.",
            "I did not complete the cedar atlas. It has 140 pages.",
        )
        for index, source in enumerate(raw_action_fixtures):
            with self.subTest(kind="raw-action", source=source):
                with tempfile.TemporaryDirectory() as directory:
                    with Engine(
                        str(Path(directory) / "memory.db"),
                        user_id=f"raw-measurement-action-{index}",
                        semantic_dedup=False,
                        local_only=True,
                    ) as engine:
                        self._materialize(
                            engine,
                            session_id=f"raw-measurement-action-{index}",
                            occurred_at=100.0,
                            messages=[{"role": "user", "content": source}],
                            claims=[
                                {
                                    "kind": "event",
                                    "text": "The user completed the cedar atlas.",
                                    "subject": "user",
                                    "predicate": "completed",
                                    "object_text": "cedar atlas",
                                },
                                {
                                    "kind": "fact",
                                    "text": "The cedar atlas has 140 pages.",
                                    "subject": "cedar atlas",
                                    "predicate": "page count",
                                    "object_text": "140 pages",
                                },
                            ],
                        )
                        blocks = engine.search_memory_blocks(
                            (
                                "What was the page count of the one atlas I "
                                "completed?"
                            ),
                            limit=20,
                        ).blocks
                        self.assertFalse(
                            any("Computed scalar" in block.text for block in blocks)
                        )

        with self.subTest(kind="other-owned-page-number"):
            with tempfile.TemporaryDirectory() as directory:
                with Engine(
                    str(Path(directory) / "memory.db"),
                    user_id="raw-measurement-other-owned-value",
                    semantic_dedup=False,
                    local_only=True,
                ) as engine:
                    source = (
                        "I completed the cedar atlas. "
                        "Alice's operations manual has 140 pages."
                    )
                    self._materialize(
                        engine,
                        session_id="raw-measurement-other-owned-value",
                        occurred_at=100.0,
                        messages=[{"role": "user", "content": source}],
                        claims=[
                            {
                                "kind": "event",
                                "text": "The user completed the cedar atlas.",
                                "subject": "user",
                                "predicate": "completed",
                                "object_text": "cedar atlas",
                            },
                            {
                                "kind": "fact",
                                "text": "The cedar atlas has 140 pages.",
                                "subject": "cedar atlas",
                                "predicate": "page count",
                                "object_text": "140 pages",
                            },
                        ],
                    )
                    blocks = engine.search_memory_blocks(
                        "What was the page count of the one atlas I completed?",
                        limit=20,
                    ).blocks
                    self.assertFalse(
                        any("Computed scalar" in block.text for block in blocks)
                    )

        with self.subTest(kind="undated-march-scope"):
            with tempfile.TemporaryDirectory() as directory:
                with Engine(
                    str(Path(directory) / "memory.db"),
                    user_id="raw-measurement-undated-march",
                    semantic_dedup=False,
                    local_only=True,
                ) as engine:
                    for index, (name, pages) in enumerate(
                        (("cedar", 140), ("basalt", 260)),
                        start=1,
                    ):
                        source = (
                            f"I completed the {name} atlas. It has {pages} pages."
                        )
                        self._materialize(
                            engine,
                            session_id=f"undated-{name}-atlas",
                            occurred_at=float(index * 100),
                            messages=[{"role": "user", "content": source}],
                            claims=[
                                {
                                    "kind": "event",
                                    "text": (
                                        f"The user completed the {name} atlas."
                                    ),
                                    "subject": "user",
                                    "predicate": "completed",
                                    "object_text": f"{name} atlas",
                                },
                                {
                                    "kind": "fact",
                                    "text": (
                                        f"The {name} atlas has {pages} pages."
                                    ),
                                    "subject": f"{name} atlas",
                                    "predicate": "page count",
                                    "object_text": f"{pages} pages",
                                },
                            ],
                        )
                    blocks = engine.search_memory_blocks(
                        (
                            "What was the combined page count of the two atlases "
                            "I completed in March?"
                        ),
                        limit=20,
                    ).blocks
                    self.assertFalse(
                        any("Computed scalar" in block.text for block in blocks)
                    )

    def test_cumulative_raw_other_actor_misattribution_cannot_resolve(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with Engine(
                str(Path(directory) / "memory.db"),
                user_id="raw-cumulative-other-actor",
                semantic_dedup=False,
                local_only=True,
            ) as engine:
                valid_source = (
                    "I have calibrated the brass telescope 3 times already."
                )
                self._materialize(
                    engine,
                    session_id="valid-user-calibration",
                    occurred_at=100.0,
                    messages=[{"role": "user", "content": valid_source}],
                    claims=[
                        {
                            "kind": "fact",
                            "text": valid_source.replace(
                                "I have", "The user has"
                            ),
                            "subject": "user",
                            "predicate": "has calibrated",
                            "object_text": "brass telescope 3 times already",
                        }
                    ],
                )
                invalid_source = (
                    "Alice has calibrated the brass telescope 99 times now."
                )
                self._materialize(
                    engine,
                    session_id="misattributed-alice-calibration",
                    occurred_at=200.0,
                    messages=[{"role": "user", "content": invalid_source}],
                    claims=[
                        {
                            "kind": "fact",
                            "text": (
                                "The user has calibrated the brass telescope "
                                "99 times now."
                            ),
                            "subject": "user",
                            "predicate": "has calibrated",
                            "object_text": "brass telescope 99 times now",
                        }
                    ],
                )
                blocks = engine.search_memory_blocks(
                    "How many times have I calibrated the brass telescope?",
                    limit=20,
                ).blocks
                self.assertFalse(
                    self._scalar_blocks(
                        blocks, "cumulative_counter_resolution"
                    )
                )

    def test_ordinary_completed_quantities_sum_instead_of_using_maximum(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with Engine(
                str(Path(directory) / "memory.db"),
                user_id="ordinary-completed-quantity-sum",
                semantic_dedup=False,
                local_only=True,
            ) as engine:
                source_ids = []
                for index, (color, quantity) in enumerate(
                    (("red", 2), ("blue", 3)),
                    start=1,
                ):
                    source = f"I acquired {quantity} {color} enamel pins."
                    source_ids.extend(
                        self._materialize(
                            engine,
                            session_id=f"{color}-pin-acquisition",
                            occurred_at=float(index * 100),
                            messages=[{"role": "user", "content": source}],
                            claims=[
                                {
                                    "kind": "event",
                                    "text": source.replace(
                                        "I acquired", "The user acquired"
                                    ),
                                    "subject": "user",
                                    "predicate": "acquired",
                                    "object_text": (
                                        f"{quantity} {color} enamel pins"
                                    ),
                                    "event_start": float(index * 100),
                                }
                            ],
                        )
                    )
                pack = engine._memory._build_aggregation_pack(
                    "How many enamel pins did I acquire?",
                    source_ids,
                    max_chars=4_000,
                )
                self.assertIsNotNone(pack)
                assert pack is not None
                self.assertIn(
                    "evidence: 5 completed action-object groups/items (2 + 3 = 5)",
                    pack.block.text,
                )
                self.assertNotIn("completed cumulative/ordinal count", pack.block.text)
                self.assertNotIn(
                    "evidence: 3 completed action-object groups/items",
                    pack.block.text,
                )

    def test_scalar_modes_reject_approximate_quantity_surfaces(self) -> None:
        surfaces = (
            "up to 4",
            "as many as 4",
            "4 or more",
            "4+",
            "4-ish",
            "circa 4",
            "max 4",
            "min 4",
            "a maximum of 4",
            "a minimum of 4",
            "~4",
        )
        for index, surface in enumerate(surfaces):
            with self.subTest(mode="completed", surface=surface):
                with tempfile.TemporaryDirectory() as directory:
                    with Engine(
                        str(Path(directory) / "memory.db"),
                        user_id=f"approx-completed-{index}",
                        semantic_dedup=False,
                        local_only=True,
                    ) as engine:
                        source = f"I acquired {surface} enamel pins."
                        message_ids = self._materialize(
                            engine,
                            session_id=f"approx-completed-{index}",
                            occurred_at=100.0,
                            messages=[{"role": "user", "content": source}],
                            claims=[
                                {
                                    "kind": "event",
                                    "text": source.replace(
                                        "I acquired", "The user acquired"
                                    ),
                                    "subject": "user",
                                    "predicate": "acquired",
                                    "object_text": f"{surface} enamel pins",
                                }
                            ],
                        )
                        pack = engine._memory._build_aggregation_pack(
                            "How many enamel pins did I acquire?",
                            message_ids,
                            max_chars=2_000,
                        )
                        if pack is not None:
                            self.assertNotIn("Computed scalar", pack.block.text)

            with self.subTest(mode="duration", surface=surface):
                with tempfile.TemporaryDirectory() as directory:
                    with Engine(
                        str(Path(directory) / "memory.db"),
                        user_id=f"approx-duration-{index}",
                        semantic_dedup=False,
                        local_only=True,
                    ) as engine:
                        source = (
                            "I attended the cedar workshop for "
                            f"{surface} days."
                        )
                        message_ids = self._materialize(
                            engine,
                            session_id=f"approx-duration-{index}",
                            occurred_at=100.0,
                            messages=[{"role": "user", "content": source}],
                            claims=[
                                {
                                    "kind": "event",
                                    "text": source.replace(
                                        "I attended", "The user attended"
                                    ),
                                    "subject": "user",
                                    "predicate": "attended",
                                    "object_text": (
                                        f"cedar workshop for {surface} days"
                                    ),
                                }
                            ],
                        )
                        pack = engine._memory._build_aggregation_pack(
                            "How many days did I spend attending workshops?",
                            message_ids,
                            max_chars=2_000,
                        )
                        if pack is not None:
                            self.assertNotIn("Computed scalar", pack.block.text)

            with self.subTest(mode="pending", surface=surface):
                with tempfile.TemporaryDirectory() as directory:
                    with Engine(
                        str(Path(directory) / "memory.db"),
                        user_id=f"approx-pending-{index}",
                        semantic_dedup=False,
                        local_only=True,
                    ) as engine:
                        source = (
                            f"I still need to buy {surface} lithium batteries."
                        )
                        message_ids = self._materialize(
                            engine,
                            session_id=f"approx-pending-{index}",
                            occurred_at=100.0,
                            messages=[{"role": "user", "content": source}],
                            claims=[
                                {
                                    "kind": "fact",
                                    "text": source.replace(
                                        "I still need", "The user still needs"
                                    ),
                                    "subject": "user",
                                    "predicate": "needs_to_buy",
                                    "object_text": f"{surface} lithium batteries",
                                    "memory_key": (
                                        "user.shopping.lithium_batteries.buy"
                                    ),
                                }
                            ],
                        )
                        pack = engine._memory._build_aggregation_pack(
                            "How many lithium batteries do I need to buy?",
                            message_ids,
                            max_chars=2_000,
                            include_uncompleted=True,
                        )
                        if pack is not None:
                            self.assertNotIn("Computed scalar", pack.block.text)

            with self.subTest(mode="measurement", surface=surface):
                with tempfile.TemporaryDirectory() as directory:
                    with Engine(
                        str(Path(directory) / "memory.db"),
                        user_id=f"approx-measurement-{index}",
                        semantic_dedup=False,
                        local_only=True,
                    ) as engine:
                        source = (
                            "I completed the cedar atlas. It has "
                            f"{surface} pages."
                        )
                        self._materialize(
                            engine,
                            session_id=f"approx-measurement-{index}",
                            occurred_at=100.0,
                            messages=[{"role": "user", "content": source}],
                            claims=[
                                {
                                    "kind": "event",
                                    "text": "The user completed the cedar atlas.",
                                    "subject": "user",
                                    "predicate": "completed",
                                    "object_text": "cedar atlas",
                                },
                                {
                                    "kind": "fact",
                                    "text": (
                                        f"The cedar atlas has {surface} pages."
                                    ),
                                    "subject": "cedar atlas",
                                    "predicate": "page count",
                                    "object_text": f"{surface} pages",
                                },
                            ],
                        )
                        blocks = engine.search_memory_blocks(
                            (
                                "What was the page count of the one atlas I "
                                "completed?"
                            ),
                            limit=20,
                        ).blocks
                        self.assertFalse(
                            any("Computed scalar" in block.text for block in blocks)
                        )

    def test_numeric_battery_labels_are_not_item_quantities(self) -> None:
        for index, classifier in enumerate(("Type", "Model", "Version")):
            with self.subTest(mode="completed", classifier=classifier):
                with tempfile.TemporaryDirectory() as directory:
                    with Engine(
                        str(Path(directory) / "memory.db"),
                        user_id=f"numeric-label-completed-{index}",
                        semantic_dedup=False,
                        local_only=True,
                    ) as engine:
                        source = (
                            f"I acquired a {classifier} 2 lithium battery."
                        )
                        message_ids = self._materialize(
                            engine,
                            session_id=f"numeric-label-completed-{index}",
                            occurred_at=100.0,
                            messages=[{"role": "user", "content": source}],
                            claims=[
                                {
                                    "kind": "event",
                                    "text": source.replace(
                                        "I acquired", "The user acquired"
                                    ),
                                    "subject": "user",
                                    "predicate": "acquired",
                                    "object_text": (
                                        f"a {classifier} 2 lithium battery"
                                    ),
                                }
                            ],
                        )
                        pack = engine._memory._build_aggregation_pack(
                            "How many lithium battery groups did I acquire?",
                            message_ids,
                            max_chars=2_000,
                        )
                        self.assertIsNotNone(pack)
                        assert pack is not None
                        self.assertIn(
                            "evidence: 1 completed action-object groups/items",
                            pack.block.text,
                        )
                        self.assertNotIn(
                            "evidence: 2 completed action-object groups/items",
                            pack.block.text,
                        )

            with self.subTest(mode="pending", classifier=classifier):
                with tempfile.TemporaryDirectory() as directory:
                    with Engine(
                        str(Path(directory) / "memory.db"),
                        user_id=f"numeric-label-pending-{index}",
                        semantic_dedup=False,
                        local_only=True,
                    ) as engine:
                        source = (
                            f"I still need to buy a {classifier} 2 lithium battery."
                        )
                        message_ids = self._materialize(
                            engine,
                            session_id=f"numeric-label-pending-{index}",
                            occurred_at=100.0,
                            messages=[{"role": "user", "content": source}],
                            claims=[
                                {
                                    "kind": "fact",
                                    "text": source.replace(
                                        "I still need", "The user still needs"
                                    ),
                                    "subject": "user",
                                    "predicate": "needs_to_buy",
                                    "object_text": (
                                        f"a {classifier} 2 lithium battery"
                                    ),
                                    "memory_key": (
                                        "user.shopping.lithium_battery.buy"
                                    ),
                                }
                            ],
                        )
                        pack = engine._memory._build_aggregation_pack(
                            "How many lithium batteries do I need to buy?",
                            message_ids,
                            max_chars=2_000,
                            include_uncompleted=True,
                        )
                        if pack is not None:
                            self.assertNotIn(
                                "open obligations: 2 requested items",
                                pack.block.text,
                            )

    def test_raw_remembered_alice_actions_cannot_certify_user_scalars(self) -> None:
        with self.subTest(mode="completed"):
            with tempfile.TemporaryDirectory() as directory:
                with Engine(
                    str(Path(directory) / "memory.db"),
                    user_id="remember-alice-completed",
                    semantic_dedup=False,
                    local_only=True,
                ) as engine:
                    source = "I remember Alice acquired 4 enamel pins."
                    message_ids = self._materialize(
                        engine,
                        session_id="remember-alice-completed",
                        occurred_at=100.0,
                        messages=[{"role": "user", "content": source}],
                        claims=[
                            {
                                "kind": "event",
                                "text": "The user acquired 4 enamel pins.",
                                "subject": "user",
                                "predicate": "acquired",
                                "object_text": "4 enamel pins",
                            }
                        ],
                    )
                    pack = engine._memory._build_aggregation_pack(
                        "How many enamel pins did I acquire?",
                        message_ids,
                        max_chars=2_000,
                    )
                    self.assertIsNotNone(pack)
                    assert pack is not None
                    self.assertNotIn("Computed scalar", pack.block.text)

        with self.subTest(mode="duration"):
            with tempfile.TemporaryDirectory() as directory:
                with Engine(
                    str(Path(directory) / "memory.db"),
                    user_id="remember-alice-duration",
                    semantic_dedup=False,
                    local_only=True,
                ) as engine:
                    source = "I remember Alice attended a 4-day workshop."
                    message_ids = self._materialize(
                        engine,
                        session_id="remember-alice-duration",
                        occurred_at=100.0,
                        messages=[{"role": "user", "content": source}],
                        claims=[
                            {
                                "kind": "event",
                                "text": "The user attended a 4-day workshop.",
                                "subject": "user",
                                "predicate": "attended",
                                "object_text": "4-day workshop",
                            }
                        ],
                    )
                    pack = engine._memory._build_aggregation_pack(
                        "How many days did I spend attending workshops?",
                        message_ids,
                        max_chars=2_000,
                    )
                    self.assertIsNotNone(pack)
                    assert pack is not None
                    self.assertNotIn("Computed scalar", pack.block.text)

        with self.subTest(mode="cumulative"):
            with tempfile.TemporaryDirectory() as directory:
                with Engine(
                    str(Path(directory) / "memory.db"),
                    user_id="remember-alice-cumulative",
                    semantic_dedup=False,
                    local_only=True,
                ) as engine:
                    for index, total in enumerate((3, 5), start=1):
                        marker = "already" if total == 3 else "now"
                        source = (
                            "I remember Alice has calibrated the brass telescope "
                            f"{total} times {marker}."
                        )
                        self._materialize(
                            engine,
                            session_id=f"remember-alice-counter-{index}",
                            occurred_at=float(index * 100),
                            messages=[{"role": "user", "content": source}],
                            claims=[
                                {
                                    "kind": "fact",
                                    "text": (
                                        "The user has calibrated the brass "
                                        f"telescope {total} times {marker}."
                                    ),
                                    "subject": "user",
                                    "predicate": "has calibrated",
                                    "object_text": (
                                        f"brass telescope {total} times {marker}"
                                    ),
                                }
                            ],
                        )
                    blocks = engine.search_memory_blocks(
                        "How many times have I calibrated the brass telescope?",
                        limit=20,
                    ).blocks
                    self.assertFalse(
                        self._scalar_blocks(
                            blocks, "cumulative_counter_resolution"
                        )
                    )

    def test_duration_month_scope_excludes_april_and_exclusions_fail_closed(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with Engine(
                str(Path(directory) / "memory.db"),
                user_id="duration-month-filtering",
                semantic_dedup=False,
                local_only=True,
            ) as engine:
                source_ids = []
                fixtures = (
                    ("March", 1, 1_678_406_400.0),
                    ("April", 2, 1_681_084_800.0),
                )
                for month, days, event_start in fixtures:
                    source = (
                        f"I attended a {days}-day workshop in {month}."
                    )
                    source_ids.extend(
                        self._materialize(
                            engine,
                            session_id=f"{month.casefold()}-workshop",
                            occurred_at=event_start,
                            messages=[{"role": "user", "content": source}],
                            claims=[
                                {
                                    "kind": "event",
                                    "text": source.replace(
                                        "I attended", "The user attended"
                                    ),
                                    "subject": "user",
                                    "predicate": "attended",
                                    "object_text": (
                                        f"{days}-day workshop in {month}"
                                    ),
                                    "event_start": event_start,
                                }
                            ],
                        )
                    )
                march_pack = engine._memory._build_aggregation_pack(
                    "How many days did I spend attending workshops in March?",
                    source_ids,
                    max_chars=4_000,
                )
                self.assertIsNotNone(march_pack)
                assert march_pack is not None
                self.assertNotIn("event durations: 3 days", march_pack.block.text)
                if "Computed scalar" in march_pack.block.text:
                    self.assertIn("event durations: 1 days", march_pack.block.text)

                excluding_pack = engine._memory._build_aggregation_pack(
                    (
                        "How many days did I spend attending workshops, "
                        "excluding April?"
                    ),
                    source_ids,
                    max_chars=4_000,
                )
                self.assertIsNotNone(excluding_pack)
                assert excluding_pack is not None
                self.assertNotIn("Computed scalar", excluding_pack.block.text)

    def test_raw_alice_manual_page_clause_cannot_certify_user_atlas(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with Engine(
                str(Path(directory) / "memory.db"),
                user_id="raw-alice-cedar-manual-page",
                semantic_dedup=False,
                local_only=True,
            ) as engine:
                source = (
                    "I completed the Cedar atlas. "
                    "Alice's Cedar operations manual has 140 pages."
                )
                self._materialize(
                    engine,
                    session_id="raw-alice-cedar-manual-page",
                    occurred_at=100.0,
                    messages=[{"role": "user", "content": source}],
                    claims=[
                        {
                            "kind": "event",
                            "text": "The user completed the Cedar atlas.",
                            "subject": "user",
                            "predicate": "completed",
                            "object_text": "Cedar atlas",
                        },
                        {
                            "kind": "fact",
                            "text": "The Cedar atlas has 140 pages.",
                            "subject": "Cedar atlas",
                            "predicate": "page count",
                            "object_text": "140 pages",
                        },
                    ],
                )
                blocks = engine.search_memory_blocks(
                    "What was the page count of the one atlas I completed?",
                    limit=20,
                ).blocks
                self.assertFalse(
                    any("Computed scalar" in block.text for block in blocks)
                )

    def test_composite_measurement_normalizes_units_and_uses_raw_scope(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with Engine(
                str(Path(directory) / "memory.db"),
                user_id="novel-unit-stem-and-raw-scope",
                semantic_dedup=False,
                local_only=True,
            ) as engine:
                first_source = (
                    "I just finished a historical fiction novel, "
                    '"The Nightingale" by Kristin Hannah, which had 440 pages.'
                )
                first_ids = self._materialize(
                    engine,
                    session_id="named-440-page-novel",
                    occurred_at=100.0,
                    messages=[{"role": "user", "content": first_source}],
                    claims=[
                        {
                            "kind": "event",
                            "text": (
                                'The user finished "The Nightingale" by '
                                "Kristin Hannah."
                            ),
                            "subject": "user",
                            "predicate": "finished",
                            "object_text": (
                                '"The Nightingale" by Kristin Hannah'
                            ),
                        },
                        {
                            "kind": "fact",
                            "text": '"The Nightingale" has 440 pages.',
                            "subject": '"The Nightingale"',
                            "predicate": "page count",
                            "object_text": "440 pages",
                        },
                    ],
                )
                second_source = "I just finished a 416-page novel."
                second_ids = self._materialize(
                    engine,
                    session_id="anonymous-416-page-novel",
                    occurred_at=200.0,
                    messages=[{"role": "user", "content": second_source}],
                    claims=[
                        {
                            "kind": "event",
                            "text": (
                                "The user finished a 416-page novel before the "
                                "current request."
                            ),
                            "subject": "user",
                            "predicate": "finished",
                            "object_text": "a 416-page novel",
                        }
                    ],
                )

                blocks = engine.search_memory_blocks(
                    (
                        "What was the page count of the two novels I finished "
                        "in January and March?"
                    ),
                    limit=20,
                ).blocks
                resolutions = self._scalar_blocks(
                    blocks,
                    "composite_measurement_resolution",
                )
                self.assertEqual(len(resolutions), 1)
                self.assertIn("856 pages (440 + 416 = 856)", resolutions[0].text)
                self.assertEqual(
                    set(resolutions[0].message_ids),
                    set(first_ids + second_ids),
                )

    def test_date_ordinal_does_not_veto_completed_item_count(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with Engine(
                str(Path(directory) / "memory.db"),
                user_id="jewelry-date-ordinal",
                semantic_dedup=False,
                local_only=True,
            ) as engine:
                source_ids = []
                fixtures = (
                    (
                        "silver-necklace",
                        (
                            "I got a new silver necklace with a small pendant "
                            "on the 15th of last month."
                        ),
                        "got",
                        (
                            "a new silver necklace with a small pendant on the "
                            "15th of last month"
                        ),
                        "user.jewelry.silver_necklace.acquired",
                        1_685_264_520.0,
                        1_681_516_800.0,
                    ),
                    (
                        "engagement-ring",
                        "I got my engagement ring a month ago.",
                        "got",
                        "my engagement ring a month ago",
                        "user.jewelry.engagement_ring.acquired",
                        1_685_284_800.0,
                        1_682_692_800.0,
                    ),
                    (
                        "emerald-earrings",
                        (
                            "I got a new pair of emerald earrings at a flea "
                            "market last weekend."
                        ),
                        "acquired",
                        (
                            "a new pair of emerald earrings at a flea market "
                            "last weekend"
                        ),
                        "user.jewelry.emerald_earrings.acquired",
                        1_684_701_480.0,
                        1_684_022_400.0,
                    ),
                )
                for session_id, source, predicate, obj, key, occurred_at, event in (
                    fixtures
                ):
                    source_ids.extend(
                        self._materialize(
                            engine,
                            session_id=session_id,
                            occurred_at=occurred_at,
                            messages=[{"role": "user", "content": source}],
                            claims=[
                                {
                                    "kind": "event",
                                    "text": source.replace(
                                        "I got", "The user got", 1
                                    ),
                                    "subject": "user",
                                    "predicate": predicate,
                                    "object_text": obj,
                                    "memory_key": key,
                                    "event_start": event,
                                }
                            ],
                        )
                    )

                pack = engine._memory._build_aggregation_pack(
                    (
                        "How many pieces of jewelry did I acquire in the last "
                        "two months?"
                    ),
                    source_ids,
                    max_chars=4_000,
                )
                self.assertIsNotNone(pack)
                assert pack is not None
                self.assertIn(
                    (
                        "evidence: 3 completed action-object groups/items "
                        "(1 + 1 + 1 = 3)"
                    ),
                    pack.block.text,
                )

    def test_same_day_event_without_raw_duration_cannot_become_one_day(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with Engine(
                str(Path(directory) / "memory.db"),
                user_id="same-day-event-without-duration",
                semantic_dedup=False,
                local_only=True,
            ) as engine:
                source = "I attended the cedar workshop on April 10."
                message_ids = self._materialize(
                    engine,
                    session_id="same-day-cedar-workshop",
                    occurred_at=1_681_084_800.0,
                    messages=[{"role": "user", "content": source}],
                    claims=[
                        {
                            "kind": "event",
                            "text": (
                                "The user attended the cedar workshop on "
                                "April 10."
                            ),
                            "subject": "user",
                            "predicate": "attended",
                            "object_text": "cedar workshop on April 10",
                            "event_start": 1_681_084_800.0,
                            "event_end": 1_681_113_600.0,
                        }
                    ],
                )
                pack = engine._memory._build_aggregation_pack(
                    "How many days did I spend attending workshops?",
                    message_ids,
                    max_chars=2_000,
                )
                self.assertIsNotNone(pack)
                assert pack is not None
                self.assertNotIn("Computed scalar", pack.block.text)
                self.assertNotIn("event durations: 1 day", pack.block.text)

    def test_duration_day_and_week_scopes_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with Engine(
                str(Path(directory) / "memory.db"),
                user_id="duration-fine-grained-scope",
                semantic_dedup=False,
                local_only=True,
            ) as engine:
                source = "I attended a 2-day cedar workshop in April."
                message_ids = self._materialize(
                    engine,
                    session_id="april-two-day-workshop",
                    occurred_at=1_681_084_800.0,
                    messages=[{"role": "user", "content": source}],
                    claims=[
                        {
                            "kind": "event",
                            "text": source.replace(
                                "I attended", "The user attended"
                            ),
                            "subject": "user",
                            "predicate": "attended",
                            "object_text": "2-day cedar workshop in April",
                            "event_start": 1_681_084_800.0,
                            "event_end": 1_681_171_200.0,
                        }
                    ],
                )
                for query in (
                    (
                        "How many days did I spend attending workshops on "
                        "April 10?"
                    ),
                    (
                        "How many days did I spend attending workshops in the "
                        "first week of April?"
                    ),
                ):
                    with self.subTest(query=query):
                        pack = engine._memory._build_aggregation_pack(
                            query,
                            message_ids,
                            max_chars=2_000,
                        )
                        if pack is not None:
                            self.assertNotIn("Computed scalar", pack.block.text)

    def test_red_box_does_not_make_blue_pins_red(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with Engine(
                str(Path(directory) / "memory.db"),
                user_id="blue-pins-in-red-box",
                semantic_dedup=False,
                local_only=True,
            ) as engine:
                source = "I acquired 3 blue pins in a red box."
                message_ids = self._materialize(
                    engine,
                    session_id="blue-pins-red-box",
                    occurred_at=100.0,
                    messages=[{"role": "user", "content": source}],
                    claims=[
                        {
                            "kind": "event",
                            "text": source.replace(
                                "I acquired", "The user acquired"
                            ),
                            "subject": "user",
                            "predicate": "acquired",
                            "object_text": "3 blue pins in a red box",
                        }
                    ],
                )
                pack = engine._memory._build_aggregation_pack(
                    "How many red pin items did I acquire?",
                    message_ids,
                    max_chars=2_000,
                )
                if pack is not None:
                    self.assertNotIn("Computed scalar", pack.block.text)
                    self.assertNotIn(
                        "evidence: 3 completed action-object groups/items",
                        pack.block.text,
                    )

    def test_coordinated_acquire_and_donate_query_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with Engine(
                str(Path(directory) / "memory.db"),
                user_id="coordinated-acquire-donate",
                semantic_dedup=False,
                local_only=True,
            ) as engine:
                source = (
                    "I acquired 2 enamel pins and donated 3 enamel pins."
                )
                message_ids = self._materialize(
                    engine,
                    session_id="acquire-and-donate-pins",
                    occurred_at=100.0,
                    messages=[{"role": "user", "content": source}],
                    claims=[
                        {
                            "kind": "event",
                            "text": "The user acquired 2 enamel pins.",
                            "subject": "user",
                            "predicate": "acquired",
                            "object_text": "2 enamel pins",
                        },
                        {
                            "kind": "event",
                            "text": "The user donated 3 enamel pins.",
                            "subject": "user",
                            "predicate": "donated",
                            "object_text": "3 enamel pins",
                        },
                    ],
                )
                pack = engine._memory._build_aggregation_pack(
                    "How many enamel pins did I acquire and donate?",
                    message_ids,
                    max_chars=2_000,
                )
                self.assertIsNotNone(pack)
                assert pack is not None
                self.assertNotIn("Computed scalar", pack.block.text)

    def test_explicit_retelling_markers_veto_deterministic_totals(self) -> None:
        with self.subTest(mode="completed"):
            with tempfile.TemporaryDirectory() as directory:
                with Engine(
                    str(Path(directory) / "memory.db"),
                    user_id="explicit-completed-retelling",
                    semantic_dedup=False,
                    local_only=True,
                ) as engine:
                    source_ids = []
                    for index, source in enumerate(
                        (
                            "I acquired 2 red enamel pins.",
                            (
                                "As I mentioned earlier, I acquired 3 blue "
                                "enamel pins."
                            ),
                        ),
                        start=1,
                    ):
                        source_ids.extend(
                            self._materialize(
                                engine,
                                session_id=f"explicit-pin-retelling-{index}",
                                occurred_at=float(index * 100),
                                messages=[{"role": "user", "content": source}],
                                claims=[
                                    {
                                        "kind": "event",
                                        "text": source[source.rfind("I acquired") :]
                                        .replace(
                                            "I acquired", "The user acquired"
                                        ),
                                        "subject": "user",
                                        "predicate": "acquired",
                                        "object_text": (
                                            "2 red enamel pins"
                                            if index == 1
                                            else "3 blue enamel pins"
                                        ),
                                        "event_start": float(index * 100),
                                    }
                                ],
                            )
                        )
                    pack = engine._memory._build_aggregation_pack(
                        "How many enamel pins did I acquire?",
                        source_ids,
                        max_chars=3_000,
                    )
                    self.assertIsNotNone(pack)
                    assert pack is not None
                    self.assertNotIn("Computed scalar", pack.block.text)

        with self.subTest(mode="measurement"):
            with tempfile.TemporaryDirectory() as directory:
                with Engine(
                    str(Path(directory) / "memory.db"),
                    user_id="explicit-measurement-retelling",
                    semantic_dedup=False,
                    local_only=True,
                ) as engine:
                    fixtures = (
                        (
                            "cedar",
                            140,
                            (
                                "To repeat, I completed the cedar atlas. "
                                "It has 140 pages."
                            ),
                        ),
                        (
                            "basalt",
                            260,
                            (
                                "I completed the basalt atlas. It has 260 "
                                "pages."
                            ),
                        ),
                    )
                    for index, (name, pages, source) in enumerate(
                        fixtures,
                        start=1,
                    ):
                        self._materialize(
                            engine,
                            session_id=f"explicit-atlas-retelling-{index}",
                            occurred_at=float(index * 100),
                            messages=[{"role": "user", "content": source}],
                            claims=[
                                {
                                    "kind": "event",
                                    "text": (
                                        f"The user completed the {name} atlas."
                                    ),
                                    "subject": "user",
                                    "predicate": "completed",
                                    "object_text": f"{name} atlas",
                                },
                                {
                                    "kind": "fact",
                                    "text": (
                                        f"The {name} atlas has {pages} pages."
                                    ),
                                    "subject": f"{name} atlas",
                                    "predicate": "page count",
                                    "object_text": f"{pages} pages",
                                },
                            ],
                        )
                    blocks = engine.search_memory_blocks(
                        (
                            "What was the combined page count of the two atlases "
                            "I completed?"
                        ),
                        limit=20,
                    ).blocks
                    self.assertFalse(
                        self._scalar_blocks(
                            blocks,
                            "composite_measurement_resolution",
                        )
                    )
                    self.assertFalse(
                        any("Computed scalar" in block.text for block in blocks)
                    )

    def test_completed_quantity_is_scoped_to_the_queried_modifier(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with Engine(
                str(Path(directory) / "memory.db"),
                user_id="same-source-pin-color-scope",
                semantic_dedup=False,
                local_only=True,
            ) as engine:
                source = (
                    "I acquired 2 red enamel pins and 3 blue enamel pins."
                )
                message_ids = self._materialize(
                    engine,
                    session_id="mixed-color-pin-acquisition",
                    occurred_at=100.0,
                    messages=[{"role": "user", "content": source}],
                    claims=[
                        {
                            "kind": "event",
                            "text": "The user acquired 2 red enamel pins.",
                            "subject": "user",
                            "predicate": "acquired",
                            "object_text": "2 red enamel pins",
                            "event_start": 100.0,
                        },
                        {
                            "kind": "event",
                            "text": "The user acquired 3 blue enamel pins.",
                            "subject": "user",
                            "predicate": "acquired",
                            "object_text": "3 blue enamel pins",
                            "event_start": 100.0,
                        },
                    ],
                )
                for query in (
                    "How many red enamel pins did I acquire?",
                    "How many red pin items did I acquire?",
                ):
                    with self.subTest(query=query):
                        pack = engine._memory._build_aggregation_pack(
                            query,
                            message_ids,
                            max_chars=3_000,
                        )
                        self.assertIsNotNone(pack)
                        assert pack is not None
                        self.assertIn(
                            (
                                "evidence: 2 completed action-object "
                                "groups/items (2 = 2)"
                            ),
                            pack.block.text,
                        )
                        self.assertNotIn(
                            "evidence: 3 completed action-object groups/items",
                            pack.block.text,
                        )
                        self.assertNotIn(
                            "evidence: 5 completed action-object groups/items",
                            pack.block.text,
                        )

    def test_pending_quantity_is_scoped_to_the_queried_battery_kind(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with Engine(
                str(Path(directory) / "memory.db"),
                user_id="same-source-battery-kind-scope",
                semantic_dedup=False,
                local_only=True,
            ) as engine:
                source = (
                    "I still need to buy 2 lithium batteries and 5 alkaline "
                    "batteries."
                )
                message_ids = self._materialize(
                    engine,
                    session_id="mixed-battery-shopping",
                    occurred_at=100.0,
                    messages=[{"role": "user", "content": source}],
                    claims=[
                        {
                            "kind": "fact",
                            "text": (
                                "The user still needs to buy 2 lithium batteries."
                            ),
                            "subject": "user",
                            "predicate": "needs_to_buy",
                            "object_text": "2 lithium batteries",
                            "memory_key": (
                                "user.shopping.lithium_batteries.buy"
                            ),
                        },
                        {
                            "kind": "fact",
                            "text": (
                                "The user still needs to buy 5 alkaline batteries."
                            ),
                            "subject": "user",
                            "predicate": "needs_to_buy",
                            "object_text": "5 alkaline batteries",
                            "memory_key": (
                                "user.shopping.alkaline_batteries.buy"
                            ),
                        },
                    ],
                )
                for query in (
                    "How many lithium batteries do I need to buy?",
                    "How many lithium battery items do I need to buy?",
                ):
                    with self.subTest(query=query):
                        pack = engine._memory._build_aggregation_pack(
                            query,
                            message_ids,
                            max_chars=3_000,
                            include_uncompleted=True,
                        )
                        self.assertIsNotNone(pack)
                        assert pack is not None
                        self.assertIn(
                            "open obligations: 2 requested items",
                            pack.block.text,
                        )
                        self.assertNotIn(
                            "open obligations: 5 requested items",
                            pack.block.text,
                        )
                        self.assertNotIn(
                            "open obligations: 7 requested items",
                            pack.block.text,
                        )

    def test_set_scope_exclusions_never_advertise_a_scalar(self) -> None:
        exclusion_phrases = (
            "excluding blue pins",
            "except blue pins",
            "not counting blue pins",
            "other than blue pins",
            "besides blue pins",
            "with the exception of blue pins",
            "leaving out blue pins",
        )
        with tempfile.TemporaryDirectory() as directory:
            with Engine(
                str(Path(directory) / "memory.db"),
                user_id="completed-set-scope-exclusions",
                semantic_dedup=False,
                local_only=True,
            ) as engine:
                source = "I acquired 2 red pins and 3 blue pins."
                message_ids = self._materialize(
                    engine,
                    session_id="mixed-pin-acquisition-for-exclusions",
                    occurred_at=100.0,
                    messages=[{"role": "user", "content": source}],
                    claims=[
                        {
                            "kind": "event",
                            "text": "The user acquired 2 red pins.",
                            "subject": "user",
                            "predicate": "acquired",
                            "object_text": "2 red pins",
                        },
                        {
                            "kind": "event",
                            "text": "The user acquired 3 blue pins.",
                            "subject": "user",
                            "predicate": "acquired",
                            "object_text": "3 blue pins",
                        },
                    ],
                )
                for phrase in exclusion_phrases:
                    with self.subTest(mode="completed", phrase=phrase):
                        pack = engine._memory._build_aggregation_pack(
                            f"How many pins did I acquire, {phrase}?",
                            message_ids,
                            max_chars=3_000,
                        )
                        self.assertIsNotNone(pack)
                        assert pack is not None
                        self.assertNotIn("Computed scalar", pack.block.text)

        pending_phrases = (
            "excluding alkaline batteries",
            "except alkaline batteries",
            "not counting alkaline batteries",
            "other than alkaline batteries",
            "besides alkaline batteries",
            "with the exception of alkaline batteries",
            "leaving out alkaline batteries",
        )
        with tempfile.TemporaryDirectory() as directory:
            with Engine(
                str(Path(directory) / "memory.db"),
                user_id="pending-set-scope-exclusions",
                semantic_dedup=False,
                local_only=True,
            ) as engine:
                source = "I still need to buy 2 lithium batteries."
                message_ids = self._materialize(
                    engine,
                    session_id="battery-shopping-for-exclusions",
                    occurred_at=100.0,
                    messages=[{"role": "user", "content": source}],
                    claims=[
                        {
                            "kind": "fact",
                            "text": source.replace(
                                "I still need", "The user still needs"
                            ),
                            "subject": "user",
                            "predicate": "needs_to_buy",
                            "object_text": "2 lithium batteries",
                            "memory_key": (
                                "user.shopping.lithium_batteries.buy"
                            ),
                        }
                    ],
                )
                for phrase in pending_phrases:
                    with self.subTest(mode="pending", phrase=phrase):
                        pack = engine._memory._build_aggregation_pack(
                            f"How many batteries do I need to buy, {phrase}?",
                            message_ids,
                            max_chars=3_000,
                            include_uncompleted=True,
                        )
                        if pack is not None:
                            self.assertNotIn("Computed scalar", pack.block.text)

    def test_hyphenated_numeric_modifiers_are_not_item_quantities(self) -> None:
        fixtures = (
            ("two-factor token", "tokens", 2),
            ("three-ring binder", "binders", 3),
            ("4-pin connector", "connectors", 4),
        )
        for index, (item, query_noun, modifier_value) in enumerate(fixtures):
            with self.subTest(mode="completed", item=item):
                with tempfile.TemporaryDirectory() as directory:
                    with Engine(
                        str(Path(directory) / "memory.db"),
                        user_id=f"hyphen-modifier-completed-{index}",
                        semantic_dedup=False,
                        local_only=True,
                    ) as engine:
                        source = f"I acquired a {item}."
                        message_ids = self._materialize(
                            engine,
                            session_id=f"hyphen-modifier-completed-{index}",
                            occurred_at=100.0,
                            messages=[{"role": "user", "content": source}],
                            claims=[
                                {
                                    "kind": "event",
                                    "text": source.replace(
                                        "I acquired", "The user acquired"
                                    ),
                                    "subject": "user",
                                    "predicate": "acquired",
                                    "object_text": f"a {item}",
                                }
                            ],
                        )
                        pack = engine._memory._build_aggregation_pack(
                            f"How many {query_noun} did I acquire?",
                            message_ids,
                            max_chars=2_000,
                        )
                        self.assertIsNotNone(pack)
                        assert pack is not None
                        self.assertNotIn(
                            "evidence: "
                            f"{modifier_value} completed action-object groups/items",
                            pack.block.text,
                        )
                        if "Computed scalar" in pack.block.text:
                            self.assertIn(
                                "evidence: 1 completed action-object groups/items",
                                pack.block.text,
                            )

            with self.subTest(mode="pending", item=item):
                with tempfile.TemporaryDirectory() as directory:
                    with Engine(
                        str(Path(directory) / "memory.db"),
                        user_id=f"hyphen-modifier-pending-{index}",
                        semantic_dedup=False,
                        local_only=True,
                    ) as engine:
                        source = f"I still need to buy a {item}."
                        message_ids = self._materialize(
                            engine,
                            session_id=f"hyphen-modifier-pending-{index}",
                            occurred_at=100.0,
                            messages=[{"role": "user", "content": source}],
                            claims=[
                                {
                                    "kind": "fact",
                                    "text": source.replace(
                                        "I still need", "The user still needs"
                                    ),
                                    "subject": "user",
                                    "predicate": "needs_to_buy",
                                    "object_text": f"a {item}",
                                    "memory_key": (
                                        f"user.shopping.hyphen_item_{index}.buy"
                                    ),
                                }
                            ],
                        )
                        pack = engine._memory._build_aggregation_pack(
                            f"How many {query_noun} do I need to buy?",
                            message_ids,
                            max_chars=2_000,
                            include_uncompleted=True,
                        )
                        self.assertIsNotNone(pack)
                        assert pack is not None
                        self.assertNotIn(
                            "open obligations: "
                            f"{modifier_value} requested items",
                            pack.block.text,
                        )
                        self.assertNotIn(
                            "open obligations: "
                            f"{modifier_value} distinct action-object groups",
                            pack.block.text,
                        )

    def test_single_ordinal_item_does_not_fall_through_to_scalar_one(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with Engine(
                str(Path(directory) / "memory.db"),
                user_id="single-second-pin-ordinal",
                semantic_dedup=False,
                local_only=True,
            ) as engine:
                source = "I acquired a 2nd enamel pin."
                message_ids = self._materialize(
                    engine,
                    session_id="second-pin-acquisition",
                    occurred_at=100.0,
                    messages=[{"role": "user", "content": source}],
                    claims=[
                        {
                            "kind": "event",
                            "text": "The user acquired a 2nd enamel pin.",
                            "subject": "user",
                            "predicate": "acquired",
                            "object_text": "a 2nd enamel pin",
                        }
                    ],
                )
                pack = engine._memory._build_aggregation_pack(
                    "How many enamel pins did I acquire?",
                    message_ids,
                    max_chars=2_000,
                )
                self.assertIsNotNone(pack)
                assert pack is not None
                self.assertNotIn(
                    "evidence: 1 completed action-object groups/items",
                    pack.block.text,
                )
                if "Computed scalar" in pack.block.text:
                    self.assertIn("2 completed items", pack.block.text)
                    self.assertIn(
                        "completed cumulative/ordinal count",
                        pack.block.text,
                    )

    def test_truncated_evidence_pack_does_not_advertise_a_computed_scalar(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with Engine(
                str(Path(directory) / "memory.db"),
                user_id="truncated-count",
                semantic_dedup=False,
                local_only=True,
            ) as engine:
                source_ids = []
                for index, mineral in enumerate(
                    ("azurite", "malachite", "fluorite"),
                    start=1,
                ):
                    source = (
                        f"I catalogued one {mineral} specimen after documenting "
                        "its color, origin, and storage tray."
                    )
                    source_ids.extend(
                        self._materialize(
                            engine,
                            session_id=f"specimen-{mineral}",
                            occurred_at=float(index * 100),
                            messages=[{"role": "user", "content": source}],
                            claims=[
                                {
                                    "kind": "event",
                                    "text": (
                                        f"The user catalogued one {mineral} specimen "
                                        "after recording its provenance and storage."
                                    ),
                                    "subject": "user",
                                    "predicate": "catalogued",
                                    "object_text": f"one {mineral} specimen",
                                }
                            ],
                        )
                    )

                pack = engine._memory._build_aggregation_pack(
                    "How many mineral specimens did I catalogue?",
                    source_ids,
                    max_chars=360,
                )
                self.assertIsNotNone(pack)
                assert pack is not None
                self.assertLess(len(pack.block.message_ids), len(source_ids))
                self.assertIn("no answer has been computed", pack.block.text)
                self.assertNotIn("Computed scalar", pack.block.text)


if __name__ == "__main__":
    unittest.main()
