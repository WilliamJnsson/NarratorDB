from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from narratordb.engine import Engine, _aggregation_query_mode
from narratordb.intelligence import (
    _AggregationClaimEvidence,
    _aggregation_action_is_noncompleted,
    _aggregation_claim_matches_query,
    _aggregation_query_action_tokens,
    _aggregation_query_domain_tokens,
    _aggregation_query_focus_tokens,
)


class AggregationEvidenceTests(unittest.TestCase):
    def _materialize(
        self,
        engine: Engine,
        *,
        session_id: str,
        messages: list[dict],
        claims: list[dict],
        occurred_at: float,
    ) -> tuple[dict, list[int]]:
        stored = engine.store_session(
            messages,
            session_id=session_id,
            occurred_at=occurred_at,
        )
        payload_claims = []
        for claim in claims:
            item = dict(claim)
            source_index = int(item.pop("source_index", 0))
            source_text = str(
                messages[source_index].get("content")
                or messages[source_index].get("text")
                or ""
            )
            item["evidence"] = [
                {
                    "message_id": stored["message_ids"][source_index],
                    "quote": source_text,
                }
            ]
            payload_claims.append(item)
        job_id = engine.enqueue_compilation(
            stored["session_pk"], stored["source_hash"], "synthetic-compiler-v1"
        )
        result = engine.apply_compilation(
            job_id,
            {"claims": payload_claims},
            processor="synthetic-test",
            processor_version="1",
            prompt_version="1",
        )
        self.assertIn(result["status"], {"complete", "partial"})
        return stored, list(stored["message_ids"])

    def test_direct_action_precedes_incidental_embedded_clause(self) -> None:
        self.assertEqual(
            _aggregation_query_action_tokens(
                "How many products did I sell after I made money from investing?"
            ),
            frozenset({"sell"}),
        )
        self.assertEqual(
            _aggregation_query_action_tokens(
                "How many days did I work before I spent time attending workshops?"
            ),
            frozenset({"work"}),
        )
        self.assertEqual(
            _aggregation_query_action_tokens(
                "What total amount of money did I earn from selling products?"
            ),
            frozenset({"sell"}),
        )
        self.assertEqual(
            _aggregation_query_action_tokens(
                "How many days did I spend working after a neighbor earned "
                "money from selling crafts?"
            ),
            frozenset({"work", "working"}),
        )
        self.assertEqual(
            _aggregation_query_action_tokens(
                "How much money did I earn from selling products after I "
                "spent time attending a seminar?"
            ),
            frozenset({"sell"}),
        )
        self.assertEqual(
            _aggregation_query_action_tokens(
                "How many certifications did I earn by completing courses "
                "with a $50 fee?"
            ),
            frozenset({"earn"}),
        )

    def test_noncompletion_scope_handles_structured_and_negative_outcome_text(
        self,
    ) -> None:
        sell_tokens = _aggregation_query_action_tokens(
            "How many products did I sell?"
        )

        def claim(value: str) -> _AggregationClaimEvidence:
            return _AggregationClaimEvidence(
                claim_id=1,
                kind="event",
                text=value,
                status="active",
                subject="user",
                predicate=value,
                object_text="pottery",
                memory_key="",
                document_time=None,
                event_start=1.0,
            )

        for value in (
            "did_not_sell",
            "didn’t_sell",
            "failed_to_sell",
            "not_sold",
            "chose not to sell",
            "left_without_selling",
            "couldn’t_sell",
            "wasn't_able_to_sell",
            "did not buy or sell pottery",
            "never bought nor sold pottery",
            "has_not_chosen_to_sell",
            "does_not_think_sold",
            "cannot_remember_whether_sold",
            "doesn't_sell",
            "doesn’t think they sold pottery",
            "didn't buy and sell pottery",
        ):
            with self.subTest(value=value):
                candidate = claim(value)
                self.assertTrue(
                    _aggregation_action_is_noncompleted(candidate, sell_tokens)
                )
                self.assertFalse(
                    _aggregation_claim_matches_query(
                        _aggregation_query_focus_tokens(
                            "How many pottery items did I sell?"
                        ),
                        candidate,
                        query_action_tokens=sell_tokens,
                        query_domain_tokens=_aggregation_query_domain_tokens(
                            "How many pottery items did I sell?",
                            sell_tokens,
                        ),
                    )
                )

        for value in (
            "The user did not enjoy the market where they sold pottery.",
            "The user sold pottery but did not enjoy the market.",
        ):
            with self.subTest(value=value):
                candidate = claim(value)
                self.assertFalse(
                    _aggregation_action_is_noncompleted(candidate, sell_tokens)
                )
                self.assertTrue(
                    _aggregation_claim_matches_query(
                        _aggregation_query_focus_tokens(
                            "How many pottery items did I sell?"
                        ),
                        candidate,
                        query_action_tokens=sell_tokens,
                        query_domain_tokens=_aggregation_query_domain_tokens(
                            "How many pottery items did I sell?",
                            sell_tokens,
                        ),
                    )
                )

    def test_structured_future_predicate_is_not_a_completed_action(self) -> None:
        query = "How many workshops did I attend?"
        action_tokens = _aggregation_query_action_tokens(query)
        candidate = _AggregationClaimEvidence(
            claim_id=1,
            kind="event",
            text="The seminar decision remains open.",
            status="active",
            subject="user",
            predicate="plans_to_attend",
            object_text="safety workshop",
            memory_key="",
            document_time=None,
            event_start=None,
        )
        self.assertFalse(
            _aggregation_claim_matches_query(
                _aggregation_query_focus_tokens(query),
                candidate,
                query_action_tokens=action_tokens,
                query_domain_tokens=_aggregation_query_domain_tokens(
                    query,
                    action_tokens,
                ),
            )
        )

    def test_completed_action_is_not_canceled_by_prior_plan_wording(self) -> None:
        query = "How many pottery items did I sell?"
        action_tokens = _aggregation_query_action_tokens(query)
        domain_tokens = _aggregation_query_domain_tokens(query, action_tokens)
        for text, predicate in (
            ("The user sold the pottery as planned.", "sold_as_planned"),
            (
                "The user finally sold pottery they had planned to sell.",
                "sold",
            ),
        ):
            with self.subTest(text=text):
                candidate = _AggregationClaimEvidence(
                    claim_id=1,
                    kind="event",
                    text=text,
                    status="active",
                    subject="user",
                    predicate=predicate,
                    object_text="pottery item",
                    memory_key="",
                    document_time=None,
                    event_start=1.0,
                )
                self.assertTrue(
                    _aggregation_claim_matches_query(
                        _aggregation_query_focus_tokens(query),
                        candidate,
                        query_action_tokens=action_tokens,
                        query_domain_tokens=domain_tokens,
                    )
                )

        for text in (
            "The user plans to sell pottery.",
            "The user intends to sell pottery.",
            "The user should sell pottery.",
            "The user plans to buy and sell pottery.",
        ):
            with self.subTest(text=text):
                candidate = _AggregationClaimEvidence(
                    claim_id=2,
                    kind="event",
                    text=text,
                    status="active",
                    subject="user",
                    predicate="sell",
                    object_text="pottery item",
                    memory_key="",
                    document_time=None,
                    event_start=None,
                )
                self.assertFalse(
                    _aggregation_claim_matches_query(
                        _aggregation_query_focus_tokens(query),
                        candidate,
                        query_action_tokens=action_tokens,
                        query_domain_tokens=domain_tokens,
                    )
                )

    def test_how_much_and_irregular_embedded_actions_are_normalized(self) -> None:
        cases = (
            (
                "How much money did I earn from writing articles?",
                "wrote",
                "articles",
                "write",
            ),
            (
                "How much money did I earn from making pottery?",
                "made",
                "pottery",
                "make",
            ),
            (
                "How much money did I earn from running tours?",
                "ran",
                "tours",
                "run",
            ),
        )
        for query, predicate, object_text, expected_action in cases:
            with self.subTest(query=query):
                action_tokens = _aggregation_query_action_tokens(query)
                self.assertEqual(action_tokens, frozenset({expected_action}))
                self.assertEqual(_aggregation_query_mode(query), "completed")
                candidate = _AggregationClaimEvidence(
                    claim_id=1,
                    kind="event",
                    text=f"The user {predicate} {object_text} for customers.",
                    status="active",
                    subject="user",
                    predicate=predicate,
                    object_text=object_text,
                    memory_key="",
                    document_time=None,
                    event_start=1.0,
                )
                self.assertTrue(
                    _aggregation_claim_matches_query(
                        _aggregation_query_focus_tokens(query),
                        candidate,
                        query_action_tokens=action_tokens,
                        query_domain_tokens=_aggregation_query_domain_tokens(
                            query,
                            action_tokens,
                        ),
                    )
                )

        self.assertEqual(
            _aggregation_query_mode(
                "What was the total time since January spent working?"
            ),
            "completed",
        )
        self.assertEqual(
            _aggregation_query_mode(
                "How much time did I spend attending workshops?"
            ),
            "completed",
        )
        for query in (
            "How much did I weigh at birth?",
            "How much did I pay for the laptop?",
            "How much time ago did I attend the workshop?",
            "How much time before the meeting did I attend the workshop?",
        ):
            with self.subTest(query=query):
                self.assertIsNone(_aggregation_query_mode(query))

    def test_pack_keeps_completed_user_events_and_excludes_plans_and_advice(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with Engine(
                str(Path(directory) / "memory.db"),
                user_id="completed-events",
                semantic_dedup=False,
                local_only=True,
            ) as engine:
                first, first_ids = self._materialize(
                    engine,
                    session_id="copper-session",
                    occurred_at=100.0,
                    messages=[
                        {"role": "user", "content": "I restored the copper lantern."},
                        {
                            "role": "assistant",
                            "content": "You should restore a glass lantern next.",
                        },
                    ],
                    claims=[
                        {
                            "kind": "event",
                            "text": "The user restored the copper lantern.",
                            "subject": "user",
                            "predicate": "restored",
                            "object_text": "copper lantern",
                            "event_start": 90.0,
                        },
                        {
                            "kind": "event",
                            "text": "The assistant recommends restoring a glass lantern.",
                            "subject": "assistant",
                            "predicate": "recommends",
                            "object_text": "glass lantern restoration",
                            "source_index": 1,
                        },
                    ],
                )
                self._materialize(
                    engine,
                    session_id="silver-plan",
                    occurred_at=200.0,
                    messages=[
                        {
                            "role": "user",
                            "content": "I plan to restore the silver lantern next week.",
                        }
                    ],
                    claims=[
                        {
                            "kind": "event",
                            "text": "The user plans to restore the silver lantern.",
                            "subject": "user",
                            "predicate": "plans to restore",
                            "object_text": "silver lantern",
                        }
                    ],
                )
                third, third_ids = self._materialize(
                    engine,
                    session_id="brass-session",
                    occurred_at=300.0,
                    messages=[
                        {"role": "user", "content": "I restored the brass lantern."}
                    ],
                    claims=[
                        {
                            "kind": "event",
                            "text": "The user restored the brass lantern.",
                            "subject": "user",
                            "predicate": "restored",
                            "object_text": "brass lantern",
                        }
                    ],
                )

                result = engine.search_memory_blocks(
                    "How many lantern restorations did I complete in total?",
                    limit=20,
                )
                pack = result.blocks[0]
                self.assertEqual(pack.kind, "evidence_pack")
                self.assertIn("no answer has been computed", pack.text)
                self.assertIn("copper lantern", pack.text)
                self.assertIn("brass lantern", pack.text)
                self.assertNotIn("silver lantern", pack.text)
                self.assertNotIn("assistant recommends", pack.text.casefold())
                self.assertEqual(set(pack.message_ids), {first_ids[0], third_ids[0]})
                self.assertIn("event_time=unspecified", pack.text)
                self.assertEqual(first["session_pk"] > 0, True)
                self.assertEqual(third["session_pk"] > 0, True)

    def test_related_quantities_stay_in_one_source_group_with_time_roles(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with Engine(
                str(Path(directory) / "memory.db"),
                user_id="related-quantities",
                semantic_dedup=False,
                local_only=True,
            ) as engine:
                _, message_ids = self._materialize(
                    engine,
                    session_id="ceramic-sale",
                    occurred_at=200.0,
                    messages=[
                        {
                            "role": "user",
                            "content": "I sold 7 ceramic bowls for 9 credits each.",
                        }
                    ],
                    claims=[
                        {
                            "kind": "event",
                            "text": "The user sold 7 ceramic bowls.",
                            "subject": "user",
                            "predicate": "sold",
                            "object_text": "7 ceramic bowls",
                            "event_start": 100.0,
                        },
                        {
                            "kind": "fact",
                            "text": "Each ceramic bowl sold for 9 credits.",
                            "subject": "ceramic bowls",
                            "predicate": "unit sale price",
                            "object_text": "9 credits each",
                        },
                        {
                            "kind": "fact",
                            "text": "The user's unrelated travel budget is 900 credits.",
                            "subject": "user travel",
                            "predicate": "budgeted",
                            "object_text": "900 credits",
                            "memory_key": "user.travel.budget",
                        },
                    ],
                )

                pack = engine.search_memory_blocks(
                    "What was the total amount earned from ceramic sales altogether?",
                    limit=20,
                ).blocks[0]
                self.assertEqual(pack.kind, "evidence_pack")
                self.assertIn("sold 7 ceramic bowls", pack.text)
                self.assertIn("9 credits", pack.text)
                self.assertNotIn("900 credits", pack.text)
                self.assertNotIn("travel budget", pack.text)
                self.assertEqual(pack.text.count(f"message:{message_ids[0]}"), 1)
                self.assertIn("document_time=1970-01-01T00:03:20Z", pack.text)
                self.assertIn("event_time=1970-01-01T00:01:40Z", pack.text)
                self.assertIn("event_time=unspecified", pack.text)
                # NarratorDB supplies evidence but deliberately does not perform
                # the multiplication or state a final answer.
                self.assertNotIn("63 credits", pack.text)

    def test_pending_obligation_count_keeps_distinct_actions_for_one_item(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with Engine(
                str(Path(directory) / "memory.db"),
                user_id="pending-obligations",
                semantic_dedup=False,
                local_only=True,
            ) as engine:
                fixtures = (
                    (
                        "cleaner-pickup",
                        "I still need to pick up my navy blazer from the cleaner.",
                        "The user still needs to pick up a navy blazer from the cleaner.",
                        "pick up",
                        "navy blazer",
                        "user.errands.cleaner.navy_blazer.pickup",
                    ),
                    (
                        "old-boots-return",
                        "I need to return the old boots to the store.",
                        "The user needs to return the old boots to the store.",
                        "return",
                        "old boots",
                        "user.errands.store.old_boots.return",
                    ),
                    (
                        "new-boots-pickup",
                        "I still need to pick up the replacement boots.",
                        "The user still needs to pick up the replacement boots.",
                        "pick up",
                        "replacement boots",
                        "user.errands.store.replacement_boots.pickup",
                    ),
                    (
                        "new-boots-retelling",
                        (
                            "I exchanged the old boots and still need to pick up "
                            "the replacement pair."
                        ),
                        (
                            "The user exchanged old boots and still needs to pick "
                            "up the replacement pair."
                        ),
                        "exchanged_and_needs_pickup",
                        "old boots exchanged; replacement boots still need pickup",
                        "user.shopping.store.replacement_boots.pickup_status",
                    ),
                )
                source_ids = []
                for index, (
                    session_id,
                    source,
                    claim,
                    predicate,
                    obj,
                    memory_key,
                ) in enumerate(fixtures, start=1):
                    _, ids = self._materialize(
                        engine,
                        session_id=session_id,
                        occurred_at=float(index * 100),
                        messages=[{"role": "user", "content": source}],
                        claims=[
                            {
                                "kind": "fact",
                                "text": claim,
                                "subject": "user",
                                "predicate": predicate,
                                "object_text": obj,
                                "memory_key": memory_key,
                            },
                            *(
                                [
                                    {
                                        "kind": "fact",
                                        "text": "The user plans to store winter clothes in boxes.",
                                        "subject": "user",
                                        "predicate": "plans to store",
                                        "object_text": "winter clothes in boxes",
                                    }
                                ]
                                if session_id == "cleaner-pickup"
                                else []
                            ),
                        ],
                    )
                    source_ids.append(ids[0])

                _, unrelated_ids = self._materialize(
                    engine,
                    session_id="scarf-washing",
                    occurred_at=450.0,
                    messages=[
                        {
                            "role": "user",
                            "content": "I still need to wash my wool scarf.",
                        }
                    ],
                    claims=[
                        {
                            "kind": "fact",
                            "text": "The user still needs to wash a wool scarf.",
                            "subject": "user",
                            "predicate": "needs_to_wash",
                            "object_text": "wool scarf",
                        }
                    ],
                )

                blocks = engine.search_memory_blocks(
                    "How many clothing items do I need to pick up or return?",
                    limit=20,
                ).blocks
                pack = blocks[0]
                self.assertEqual(pack.kind, "evidence_pack")
                self.assertEqual(set(pack.message_ids), set(source_ids))
                self.assertIn("navy blazer", pack.text)
                self.assertIn("old boots", pack.text)
                self.assertIn("replacement boots", pack.text)
                self.assertNotIn("winter clothes", pack.text)
                self.assertNotIn("wool scarf", pack.text)
                self.assertNotIn(unrelated_ids[0], pack.message_ids)
                self.assertIn(
                    "Computed scalar from exhaustive source-linked open "
                    "obligations: 3 distinct action-object groups",
                    pack.text,
                )

                tight_pack = engine.search_memory_blocks(
                    "How many clothing items do I need to pick up or return?",
                    limit=20,
                    max_chars=500,
                ).blocks[0]
                self.assertEqual(tight_pack.kind, "evidence_pack")
                self.assertNotIn("groups represented", tight_pack.text)

                self._materialize(
                    engine,
                    session_id="borrowed-sweater",
                    occurred_at=500.0,
                    messages=[
                        {
                            "role": "user",
                            "content": (
                                "I am not sure when my sister will return "
                                "the sweater I lent her."
                            ),
                        }
                    ],
                    claims=[
                        {
                            "kind": "fact",
                            "text": (
                                "The user is not sure when their sister will "
                                "return the borrowed sweater."
                            ),
                            "subject": "user sister",
                            "predicate": "will return",
                            "object_text": "borrowed sweater",
                        }
                    ],
                )
                updated_pack = engine.search_memory_blocks(
                    "How many clothing items do I need to pick up or return?",
                    limit=20,
                ).blocks[0]
                self.assertNotIn("borrowed sweater", updated_pack.text)

                for session_id, key in (
                    ("first-parcel", "user.delivery.alpha.pickup"),
                    ("second-parcel", "user.collection.beta.pickup"),
                ):
                    self._materialize(
                        engine,
                        session_id=session_id,
                        occurred_at=600.0,
                        messages=[
                            {
                                "role": "user",
                                "content": "I still need to pick up a parcel.",
                            }
                        ],
                        claims=[
                            {
                                "kind": "fact",
                                "text": "The user still needs to pick up a parcel.",
                                "subject": "user",
                                "predicate": "needs_to_pick_up",
                                "object_text": "a parcel",
                                "memory_key": key,
                            }
                        ],
                    )
                self._materialize(
                    engine,
                    session_id="medicine-pickup",
                    occurred_at=700.0,
                    messages=[
                        {
                            "role": "user",
                            "content": "I still need to pick up my medicine.",
                        }
                    ],
                    claims=[
                        {
                            "kind": "fact",
                            "text": "The user still needs to pick up medicine.",
                            "subject": "user",
                            "predicate": "needs_to_pick_up",
                            "object_text": "medicine",
                            "memory_key": "user.pharmacy.medicine.pickup",
                        }
                    ],
                )
                parcel_pack = engine.search_memory_blocks(
                    "How many parcels do I need to pick up?",
                    limit=20,
                ).blocks[0]
                self.assertEqual(parcel_pack.text.count("pick up a parcel"), 2)
                self.assertIn(
                    "Computed scalar from exhaustive source-linked open "
                    "obligations: 2 distinct action-object groups",
                    parcel_pack.text,
                )
                self.assertNotIn("medicine", parcel_pack.text)

    def test_completed_sale_pack_excludes_consideration_and_keeps_unit_price(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with Engine(
                str(Path(directory) / "memory.db"),
                user_id="completed-sales",
                semantic_dedup=False,
                local_only=True,
            ) as engine:
                self._materialize(
                    engine,
                    session_id="bike-consideration",
                    occurred_at=50.0,
                    messages=[
                        {
                            "role": "user",
                            "content": "I am thinking about selling my old bike.",
                        }
                    ],
                    claims=[
                        {
                            "kind": "event",
                            "text": "The user is thinking about selling an old bike.",
                            "subject": "user",
                            "predicate": "is thinking about selling",
                            "object_text": "old bike",
                            "memory_key": "user.bike.sale.consideration",
                        }
                    ],
                )
                sale_ids = []
                for index, (session_id, source, amount) in enumerate(
                    (
                        (
                            "herbs",
                            "I sold herbs at the market for 120 credits.",
                            "120 credits",
                        ),
                        (
                            "jam",
                            "I sold jam at the market for 225 credits.",
                            "225 credits",
                        ),
                    ),
                    start=1,
                ):
                    _, ids = self._materialize(
                        engine,
                        session_id=session_id,
                        occurred_at=float(index * 100),
                        messages=[{"role": "user", "content": source}],
                        claims=[
                            {
                                "kind": "event",
                                "text": source,
                                "subject": "user",
                                "predicate": "sold at market",
                                "object_text": amount,
                            }
                        ],
                    )
                    sale_ids.append(ids[0])
                _, plant_ids = self._materialize(
                    engine,
                    session_id="plant-sale",
                    occurred_at=400.0,
                    messages=[
                        {
                            "role": "user",
                            "content": "I sold 20 herb plants at the market for 7.5 credits each.",
                        }
                    ],
                    claims=[
                        {
                            "kind": "event",
                            "text": "The user sold 20 herb plants at the market.",
                            "subject": "user",
                            "predicate": "sold at market",
                            "object_text": "20 herb plants",
                        },
                        {
                            "kind": "fact",
                            "text": "The herb plants sold for 7.5 credits each.",
                            "subject": "herb plants",
                            "predicate": "sold at price",
                            "object_text": "7.5 credits each",
                        },
                        {
                            "kind": "fact",
                            "text": "The plant nursery budget is 900 credits.",
                            "subject": "plant nursery",
                            "predicate": "budgeted",
                            "object_text": "900 credits",
                        },
                    ],
                )
                sale_ids.extend(plant_ids)

                pack = engine.search_memory_blocks(
                    "What is the total amount of money I earned from selling "
                    "my products at the markets?",
                    limit=20,
                ).blocks[0]
                self.assertEqual(pack.kind, "evidence_pack")
                self.assertEqual(set(pack.message_ids), set(sale_ids))
                self.assertNotIn("old bike", pack.text)
                self.assertIn("20 herb plants", pack.text)
                self.assertIn("7.5 credits each", pack.text)
                self.assertNotIn("900 credits", pack.text)

    def test_time_spent_uses_embedded_attending_action(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with Engine(
                str(Path(directory) / "memory.db"),
                user_id="embedded-attendance-action",
                semantic_dedup=False,
                local_only=True,
            ) as engine:
                attended_ids = []
                for index, (session_id, noun) in enumerate(
                    (
                        ("lecture", "a conservation lecture"),
                        ("workshop", "a two-day restoration workshop"),
                        ("conference", "a regional conservation conference"),
                    ),
                    start=1,
                ):
                    source = f"I attended {noun} in April."
                    _, ids = self._materialize(
                        engine,
                        session_id=session_id,
                        occurred_at=float(index * 100),
                        messages=[{"role": "user", "content": source}],
                        claims=[
                            {
                                "kind": "event",
                                "text": source,
                                "subject": "user",
                                "predicate": f"attended_{session_id}",
                                "object_text": noun,
                                "event_start": float(index * 90),
                            }
                        ],
                    )
                    attended_ids.extend(ids)

                _, negative_outcome_ids = self._materialize(
                    engine,
                    session_id="unenjoyed-workshop",
                    occurred_at=450.0,
                    messages=[
                        {
                            "role": "user",
                            "content": (
                                "I did not enjoy the workshop that I attended "
                                "in April."
                            ),
                        }
                    ],
                    claims=[
                        {
                            "kind": "event",
                            "text": (
                                "The user did not enjoy the workshop they "
                                "attended in April."
                            ),
                            "subject": "user",
                            "predicate": "attended_workshop",
                            "object_text": "workshop in April",
                            "event_start": 440.0,
                        }
                    ],
                )
                attended_ids.extend(negative_outcome_ids)

                _, noise_ids = self._materialize(
                    engine,
                    session_id="unrelated-spending",
                    occurred_at=500.0,
                    messages=[
                        {
                            "role": "user",
                            "content": "I spent three days restoring a desk in April.",
                        }
                    ],
                    claims=[
                        {
                            "kind": "event",
                            "text": "The user spent three days restoring a desk.",
                            "subject": "user",
                            "predicate": "spent restoring",
                            "object_text": "wooden desk",
                            "event_start": 450.0,
                        }
                    ],
                )

                pack = engine.search_memory_blocks(
                    "How many days did I spend attending workshops, lectures, "
                    "and conferences in April?",
                    limit=20,
                ).blocks[0]
                self.assertEqual(pack.kind, "evidence_pack")
                self.assertEqual(set(pack.message_ids), set(attended_ids))
                self.assertTrue(set(noise_ids).isdisjoint(pack.message_ids))
                self.assertNotIn("restoring a desk", pack.text)

    def test_nonmonetary_earned_object_keeps_earn_as_the_action(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with Engine(
                str(Path(directory) / "memory.db"),
                user_id="earned-certification-action",
                semantic_dedup=False,
                local_only=True,
            ) as engine:
                _, certification_ids = self._materialize(
                    engine,
                    session_id="earned-certification",
                    occurred_at=100.0,
                    messages=[
                        {
                            "role": "user",
                            "content": (
                                "I earned a safety certification by completing "
                                "the required courses."
                            ),
                        }
                    ],
                    claims=[
                        {
                            "kind": "event",
                            "text": "The user earned one safety certification.",
                            "subject": "user",
                            "predicate": "earned_certification",
                            "object_text": "one safety certification",
                            "event_start": 90.0,
                        }
                    ],
                )
                _, course_ids = self._materialize(
                    engine,
                    session_id="completed-course",
                    occurred_at=200.0,
                    messages=[
                        {
                            "role": "user",
                            "content": (
                                "I completed a practice course without earning "
                                "a certification."
                            ),
                        }
                    ],
                    claims=[
                        {
                            "kind": "event",
                            "text": (
                                "The user completed a practice course without "
                                "earning a certification."
                            ),
                            "subject": "user",
                            "predicate": "completed_course",
                            "object_text": "practice course",
                            "event_start": 190.0,
                        }
                    ],
                )

                pack = engine.search_memory_blocks(
                    "How many certifications did I earn by completing courses?",
                    limit=20,
                ).blocks[0]
                self.assertEqual(pack.kind, "evidence_pack")
                self.assertEqual(set(pack.message_ids), set(certification_ids))
                self.assertTrue(set(course_ids).isdisjoint(pack.message_ids))
                self.assertNotIn("practice course", pack.text)

    def test_tight_pack_budget_keeps_raw_source_when_all_claims_do_not_fit(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with Engine(
                str(Path(directory) / "memory.db"),
                user_id="atomic-source-render",
                semantic_dedup=False,
                local_only=True,
            ) as engine:
                long_source = (
                    "Background notes " + "about glazing technique " * 20 + "\n"
                    "Mosaic sale proceeds were 17 credits per tile."
                )
                _, long_ids = self._materialize(
                    engine,
                    session_id="long-mosaic-source",
                    occurred_at=100.0,
                    messages=[{"role": "user", "content": long_source}],
                    claims=[
                        {
                            "kind": "event",
                            "text": "The user completed a mosaic tile sale "
                            + "with carefully recorded context " * 12,
                            "subject": "user",
                            "predicate": "completed sale",
                            "object_text": "mosaic tiles",
                        },
                        {
                            "kind": "fact",
                            "text": "Mosaic sale proceeds were 17 credits per tile.",
                            "subject": "mosaic tiles",
                            "predicate": "unit proceeds",
                            "object_text": "17 credits per tile",
                        },
                    ],
                )
                self._materialize(
                    engine,
                    session_id="short-mosaic-source",
                    occurred_at=200.0,
                    messages=[
                        {
                            "role": "user",
                            "content": "I did complete another mosaic sale.",
                        }
                    ],
                    claims=[
                        {
                            "kind": "event",
                            "text": "The user did complete another mosaic sale.",
                            "subject": "user",
                            "predicate": "completed sale",
                            "object_text": "another mosaic sale",
                        }
                    ],
                )

                result = engine.search_memory_blocks(
                    "What were the total mosaic sale proceeds altogether?",
                    limit=20,
                    max_chars=260,
                )
                self.assertEqual(result.blocks[0].kind, "evidence_pack")
                long_raw = next(
                    block
                    for block in result.blocks
                    if block.kind == "raw_message" and long_ids[0] in block.message_ids
                )
                self.assertIn("17 credits per tile", long_raw.text)

    def test_distinct_session_first_pass_precedes_extra_old_sources(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with Engine(
                str(Path(directory) / "memory.db"),
                user_id="session-diversity",
                semantic_dedup=False,
                local_only=True,
            ) as engine:
                old_messages = [
                    {"role": "user", "content": f"I did log prism specimen {name}."}
                    for name in ("alpha", "beta", "gamma")
                ]
                _, old_ids = self._materialize(
                    engine,
                    session_id="old-prism-session",
                    occurred_at=100.0,
                    messages=old_messages,
                    claims=[
                        {
                            "kind": "event",
                            "text": f"The user did log prism specimen {name}.",
                            "subject": "user",
                            "predicate": "logged prism specimen",
                            "object_text": name,
                            "event_start": float(index * 10),
                            "source_index": index - 1,
                        }
                        for index, name in enumerate(
                            ("alpha", "beta", "gamma"), start=1
                        )
                    ],
                )
                _, new_ids = self._materialize(
                    engine,
                    session_id="new-prism-session",
                    occurred_at=300.0,
                    messages=[
                        {"role": "user", "content": "I did log prism specimen delta."}
                    ],
                    claims=[
                        {
                            "kind": "event",
                            "text": "The user did log prism specimen delta.",
                            "subject": "user",
                            "predicate": "logged prism specimen",
                            "object_text": "delta",
                            "event_start": 280.0,
                        }
                    ],
                )

                pack = engine.search_memory_blocks(
                    "How many prism specimens did I log in total?",
                    limit=20,
                    max_chars=620,
                ).blocks[0]
                self.assertEqual(pack.kind, "evidence_pack")
                self.assertIn(old_ids[0], pack.message_ids)
                self.assertIn(new_ids[0], pack.message_ids)
                self.assertTrue(set(old_ids[1:]).isdisjoint(pack.message_ids))
                self.assertLess(
                    pack.text.index(f"message:{old_ids[0]}"),
                    pack.text.index(f"message:{new_ids[0]}"),
                )

    def test_possible_retellings_are_grouped_but_distinct_events_remain(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with Engine(
                str(Path(directory) / "memory.db"),
                user_id="retellings",
                semantic_dedup=False,
                local_only=True,
            ) as engine:
                source_ids = []
                fixtures = (
                    (
                        "first-chair",
                        100.0,
                        80.0,
                        "I did restore the walnut chair.",
                        "The user did restore the walnut chair.",
                        "walnut chair",
                    ),
                    (
                        "chair-retelling",
                        200.0,
                        80.0,
                        "I mentioned again that I did restore the walnut chair.",
                        "The walnut chair was restored by the user.",
                        "walnut chair",
                    ),
                    (
                        "separate-table",
                        300.0,
                        280.0,
                        "I did restore the oak table.",
                        "The user did restore the oak table.",
                        "oak table",
                    ),
                )
                for (
                    session_id,
                    occurred_at,
                    event_start,
                    source,
                    claim,
                    obj,
                ) in fixtures:
                    _, ids = self._materialize(
                        engine,
                        session_id=session_id,
                        occurred_at=occurred_at,
                        messages=[{"role": "user", "content": source}],
                        claims=[
                            {
                                "kind": "event",
                                "text": claim,
                                "subject": "user",
                                "predicate": "restored",
                                "object_text": obj,
                                "event_start": event_start,
                            }
                        ],
                    )
                    source_ids.append(ids[0])

                query = "How many furniture pieces did I restore in total?"
                with patch("narratordb.engine.time.time", return_value=1_000.0):
                    first = engine.search_memory_blocks(query, limit=20).blocks[0]
                with patch("narratordb.engine.time.time", return_value=1_000_000_000.0):
                    second = engine.search_memory_blocks(query, limit=20).blocks[0]
                self.assertEqual(first, second)
                self.assertEqual(first.kind, "evidence_pack")
                self.assertTrue(first.composite_id.startswith("evidence-pack:"))
                self.assertIn("Possible retellings R1", first.text)
                self.assertIn("do not count automatically", first.text)
                self.assertEqual(set(first.message_ids), set(source_ids))
                self.assertIn("walnut chair", first.text)
                self.assertIn("oak table", first.text)
                self.assertLess(
                    first.text.index(f"message:{source_ids[0]}"),
                    first.text.index(f"message:{source_ids[2]}"),
                )

    def test_independent_acquisitions_survive_structured_and_memory_key_matching(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with Engine(
                str(Path(directory) / "memory.db"),
                user_id="independent-acquisitions",
                semantic_dedup=False,
                local_only=True,
            ) as engine:
                fixtures = (
                    (
                        "brass-locket",
                        "I bought a brass locket for my keepsakes registry.",
                        "The user bought a brass locket keepsake on Tuesday.",
                        "fact",
                        "user",
                        "bought",
                        "brass locket",
                        "user.keepsakes.acquisitions.brass_locket",
                    ),
                    (
                        "cedar-brooch",
                        "I purchased a cedar brooch as a keepsake.",
                        "The user purchased a cedar brooch keepsake on Wednesday.",
                        "fact",
                        "user",
                        "purchased",
                        "cedar brooch keepsake",
                        "",
                    ),
                    (
                        "linen-pouch",
                        "I received a linen pouch for my keepsakes registry.",
                        "The user received a linen pouch keepsake on Thursday.",
                        "fact",
                        "user",
                        "received",
                        "linen pouch",
                        "user.keepsakes.acquisitions.linen_pouch",
                    ),
                    (
                        "opal-earrings",
                        "I acquired a pair of opal earrings as one keepsake.",
                        "The user acquired a pair of opal earrings as one keepsake.",
                        "event",
                        "user",
                        "acquired",
                        "a pair of opal earrings",
                        "user.keepsakes.acquisitions.opal_earrings",
                    ),
                )
                source_ids = []
                for index, (
                    session_id,
                    source,
                    text,
                    kind,
                    subject,
                    predicate,
                    object_text,
                    memory_key,
                ) in enumerate(fixtures, start=1):
                    _, ids = self._materialize(
                        engine,
                        session_id=session_id,
                        occurred_at=float(index * 100),
                        messages=[{"role": "user", "content": source}],
                        claims=[
                            {
                                "kind": kind,
                                "text": text,
                                "subject": subject,
                                "predicate": predicate,
                                "object_text": object_text,
                                "memory_key": memory_key,
                                "event_start": float(index * 90),
                            }
                        ],
                    )
                    source_ids.append(ids[0])

                for index, (session_id, source, text, predicate, memory_key) in enumerate(
                    (
                        (
                            "registry-state",
                            "My keepsakes registry was last cleaned recently.",
                            "The user's keepsakes registry was last cleaned recently.",
                            "last_cleaned",
                            "user.keepsakes.last_cleaned_interval",
                        ),
                        (
                            "negated-acquisition",
                            "I have not yet acquired the glass token keepsake.",
                            "The user has not yet acquired the glass token keepsake.",
                            "acquired",
                            "user.keepsakes.glass_token.not_acquired",
                        ),
                        (
                            "future-acquisition",
                            "I plan to acquire the maple charm keepsake.",
                            "The user plans to acquire the maple charm keepsake.",
                            "plans to acquire",
                            "user.keepsakes.maple_charm.planned",
                        ),
                    ),
                    start=10,
                ):
                    self._materialize(
                        engine,
                        session_id=session_id,
                        occurred_at=float(index * 100),
                        messages=[{"role": "user", "content": source}],
                        claims=[
                            {
                                "kind": "fact" if session_id == "registry-state" else "event",
                                "text": text,
                                "subject": "user",
                                "predicate": predicate,
                                "object_text": text,
                                "memory_key": memory_key,
                            }
                        ],
                    )

                pack = engine.search_memory_blocks(
                    "How many keepsakes did I acquire in total?",
                    limit=20,
                ).blocks[0]
                self.assertEqual(pack.kind, "evidence_pack")
                self.assertEqual(set(pack.message_ids), set(source_ids))
                self.assertIn("brass locket", pack.text)
                self.assertIn("cedar brooch", pack.text)
                self.assertIn("linen pouch", pack.text)
                self.assertIn("pair of opal earrings", pack.text)
                self.assertEqual(pack.text.count("pair of opal earrings"), 1)
                self.assertNotIn("last cleaned", pack.text)
                self.assertNotIn("glass token", pack.text)
                self.assertNotIn("maple charm", pack.text)
                self.assertNotIn("groups represented", pack.text)

                bought_pack = engine.search_memory_blocks(
                    "How many keepsakes did I buy in total?",
                    limit=20,
                ).blocks[0]
                self.assertIn("brass locket", bought_pack.text)
                self.assertIn("cedar brooch", bought_pack.text)
                self.assertNotIn("linen pouch", bought_pack.text)
                self.assertNotIn("opal earrings", bought_pack.text)

    def test_focused_action_recall_excludes_generic_window_noise(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with Engine(
                str(Path(directory) / "memory.db"),
                user_id="focused-action-recall",
                semantic_dedup=False,
                local_only=True,
            ) as engine:
                completed_ids = []
                for session_id, occurred_at, source, object_text in (
                    (
                        "rye-loaf",
                        100.0,
                        "I baked a rye loaf for the neighborhood picnic.",
                        "rye loaf",
                    ),
                    (
                        "almond-biscuits",
                        200.0,
                        "I baked almond biscuits for a family visit.",
                        "almond biscuits",
                    ),
                ):
                    _, message_ids = self._materialize(
                        engine,
                        session_id=session_id,
                        occurred_at=occurred_at,
                        messages=[{"role": "user", "content": source}],
                        claims=[
                            {
                                "kind": "event",
                                "text": f"The user baked {object_text}.",
                                "subject": "user",
                                "predicate": "baked",
                                "object_text": object_text,
                                "event_start": occurred_at - 10.0,
                            }
                        ],
                    )
                    completed_ids.append(message_ids[0])

                _, fact_ids = self._materialize(
                    engine,
                    session_id="notebook-category",
                    occurred_at=300.0,
                    messages=[
                        {
                            "role": "user",
                            "content": "My notebook has a category called baked goods.",
                        }
                    ],
                    claims=[
                        {
                            "kind": "fact",
                            "text": "The user's notebook category is baked goods.",
                            "subject": "user notebook",
                            "predicate": "baked",
                            "object_text": "goods category",
                        }
                    ],
                )
                _, plan_ids = self._materialize(
                    engine,
                    session_id="future-tart",
                    occurred_at=400.0,
                    messages=[
                        {
                            "role": "user",
                            "content": "I plan to bake a pear tart for a future party.",
                        }
                    ],
                    claims=[
                        {
                            "kind": "event",
                            "text": "The user plans to bake a pear tart.",
                            "subject": "user",
                            "predicate": "plans to bake",
                            "object_text": "pear tart",
                        }
                    ],
                )
                _, advice_ids = self._materialize(
                    engine,
                    session_id="assistant-advice",
                    occurred_at=500.0,
                    messages=[
                        {
                            "role": "assistant",
                            "content": "You should bake a plum cake next season.",
                        }
                    ],
                    claims=[
                        {
                            "kind": "event",
                            "text": "The assistant recommends baking a plum cake.",
                            "subject": "assistant",
                            "predicate": "recommends baking",
                            "object_text": "plum cake",
                        }
                    ],
                )
                _, user_advice_ids = self._materialize(
                    engine,
                    session_id="friend-advice",
                    occurred_at=550.0,
                    messages=[
                        {
                            "role": "user",
                            "content": "A friend advised me to bake a plum cake.",
                        }
                    ],
                    claims=[
                        {
                            "kind": "event",
                            "text": "A friend advised the user to bake a plum cake.",
                            "subject": "user friend",
                            "predicate": "advised bake",
                            "object_text": "plum cake",
                        }
                    ],
                )
                _, generic_window_ids = self._materialize(
                    engine,
                    session_id="seasonal-food-stall",
                    occurred_at=600.0,
                    messages=[
                        {
                            "role": "user",
                            "content": (
                                "I opened a food stall in early spring and closed "
                                "it in late autumn."
                            ),
                        }
                    ],
                    claims=[
                        {
                            "kind": "event",
                            "text": (
                                "The user opened a food stall in early spring and "
                                "closed it in late autumn."
                            ),
                            "subject": "user",
                            "predicate": "opened and closed",
                            "object_text": "seasonal food stall",
                        }
                    ],
                )

                pack = engine.search_memory_blocks(
                    "How many different types of food did I bake between early "
                    "spring and late autumn?",
                    limit=20,
                ).blocks[0]
                self.assertEqual(pack.kind, "evidence_pack")
                self.assertEqual(set(pack.message_ids), set(completed_ids))
                self.assertIn("rye loaf", pack.text)
                self.assertIn("almond biscuits", pack.text)
                excluded_ids = {
                    fact_ids[0],
                    plan_ids[0],
                    advice_ids[0],
                    user_advice_ids[0],
                    generic_window_ids[0],
                }
                self.assertTrue(excluded_ids.isdisjoint(pack.message_ids))
                self.assertNotIn("pear tart", pack.text)
                self.assertNotIn("food stall", pack.text)

    def test_action_only_focus_expands_events_and_groups_retellings(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with Engine(
                str(Path(directory) / "memory.db"),
                user_id="action-only-session-expansion",
                semantic_dedup=False,
                local_only=True,
            ) as engine:
                _, session_ids = self._materialize(
                    engine,
                    session_id="baking-notes",
                    occurred_at=100.0,
                    messages=[
                        {
                            "role": "user",
                            "content": "I baked almond biscuits for a family visit.",
                        },
                        {
                            "role": "user",
                            "content": "I made a seeded baguette for lunch.",
                        },
                        {
                            "role": "user",
                            "content": (
                                "My previous attempt at making sourdough bread "
                                "didn't turn out well and came out dense."
                            ),
                        },
                        {
                            "role": "user",
                            "content": "I made a wooden serving tray for the table.",
                        },
                        {
                            "role": "user",
                            "content": (
                                "I didn't bake a walnut pie because the oven failed."
                            ),
                        },
                    ],
                    claims=[
                        {
                            "kind": "event",
                            "text": "The user baked almond biscuits for a family visit.",
                            "subject": "user",
                            "predicate": "baked",
                            "object_text": "almond biscuits for a family visit",
                            "event_start": 90.0,
                        },
                        {
                            "kind": "event",
                            "text": "The user made a seeded baguette for lunch.",
                            "subject": "user",
                            "predicate": "made",
                            "object_text": "seeded baguette for lunch",
                            "source_index": 1,
                            "event_start": 80.0,
                        },
                        {
                            "kind": "event",
                            "text": (
                                "The user reported that a previous attempt at "
                                "making sourdough bread didn't turn out well "
                                "and came out dense."
                            ),
                            "subject": "user",
                            "predicate": "reported",
                            "object_text": (
                                "previous attempt at making sourdough bread "
                                "that didn't turn out well"
                            ),
                            "source_index": 2,
                            "event_start": 70.0,
                        },
                        {
                            "kind": "event",
                            "text": "The user made a wooden serving tray.",
                            "subject": "user",
                            "predicate": "made",
                            "object_text": "wooden serving tray",
                            "source_index": 3,
                            "event_start": 60.0,
                        },
                        {
                            "kind": "event",
                            "text": (
                                "The user didn't bake a walnut pie because the "
                                "oven failed."
                            ),
                            "subject": "user",
                            "predicate": "didn't bake",
                            "object_text": "walnut pie",
                            "source_index": 4,
                            "event_start": 50.0,
                        },
                    ],
                )
                _, retelling_ids = self._materialize(
                    engine,
                    session_id="biscuit-retelling",
                    occurred_at=200.0,
                    messages=[
                        {
                            "role": "user",
                            "content": (
                                "I baked almond biscuits for a family visit "
                                "using the same recipe."
                            ),
                        }
                    ],
                    claims=[
                        {
                            "kind": "event",
                            "text": (
                                "The user baked almond biscuits for a family "
                                "visit using the same recipe."
                            ),
                            "subject": "user",
                            "predicate": "baked_biscuit_result",
                            "object_text": (
                                "almond biscuits for a family visit using the "
                                "same recipe"
                            ),
                            "event_start": 90.0,
                        }
                    ],
                )

                pack = engine.search_memory_blocks(
                    "How many times did I bake anything during the previous month?",
                    limit=20,
                ).blocks[0]
                self.assertEqual(pack.kind, "evidence_pack")
                self.assertEqual(
                    set(pack.message_ids),
                    {session_ids[0], session_ids[1], session_ids[2], retelling_ids[0]},
                )
                self.assertIn("seeded baguette", pack.text)
                self.assertIn("sourdough bread", pack.text)
                self.assertIn("Possible retellings R1", pack.text)
                self.assertNotIn("wooden serving tray", pack.text)
                self.assertNotIn("walnut pie", pack.text)

    def test_present_perfect_cumulative_snapshot_keeps_ordinary_ranking(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with Engine(
                str(Path(directory) / "memory.db"),
                user_id="cumulative-snapshot-parity",
                semantic_dedup=False,
                local_only=True,
            ) as engine:
                for index, count in enumerate(("two", "four"), start=1):
                    self._materialize(
                        engine,
                        session_id=f"compass-{index}",
                        occurred_at=float(index * 100),
                        messages=[
                            {
                                "role": "user",
                                "content": (
                                    f"I have polished my brass compass {count} times."
                                ),
                            }
                        ],
                        claims=[
                            {
                                "kind": "fact",
                                "text": (
                                    "The user has polished their brass compass "
                                    f"{count} times."
                                ),
                                "subject": "user brass compass",
                                "predicate": "polished count",
                                "object_text": f"{count} times",
                                "memory_key": "user.tools.brass_compass.polish_count",
                            }
                        ],
                    )

                query = "How many times have I polished my brass compass?"
                raw = engine.search(
                    query,
                    limit=20,
                    max_context=20,
                    full_context_threshold=0,
                    minimum_results=20,
                )
                expected = engine._memory.rank_memories(
                    query,
                    raw.messages,
                    limit=20,
                    include_claims=True,
                    max_chars=1200,
                    include_session_siblings=True,
                )
                actual = engine.search_memory_blocks(query, limit=20).blocks
                self.assertEqual(actual, expected)
                self.assertFalse(
                    any(block.kind == "evidence_pack" for block in actual)
                )

    def test_relative_event_distance_keeps_ordinary_ranking(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with Engine(
                str(Path(directory) / "memory.db"),
                user_id="relative-event-distance",
                semantic_dedup=False,
                local_only=True,
            ) as engine:
                engine.store_session(
                    [
                        {
                            "role": "user",
                            "content": "I watched the championship game today.",
                        }
                    ],
                    session_id="championship",
                    occurred_at=300.0,
                )
                engine.store_session(
                    [
                        {
                            "role": "user",
                            "content": "I attended the safety workshop on Monday.",
                        },
                        {
                            "role": "user",
                            "content": "The team meeting was the following Monday.",
                        },
                    ],
                    session_id="workshop",
                    occurred_at=400.0,
                )

                for query in (
                    "How many days ago did I watch the championship game?",
                    "How many days earlier did I watch the championship game?",
                    "How many days before the team meeting did I attend the "
                    "safety workshop?",
                    "How many business days before the team meeting did I "
                    "attend the safety workshop?",
                    "How much time before the team meeting did I attend the "
                    "safety workshop?",
                    "What was the total number of days between the safety "
                    "workshop and the team meeting?",
                    "What was the total time elapsed between the safety "
                    "workshop and the team meeting?",
                    "What was the total elapsed time between the safety "
                    "workshop and the team meeting?",
                    "What was the combined number of days between the safety "
                    "workshop and the team meeting?",
                ):
                    raw = engine.search(
                        query,
                        limit=20,
                        max_context=20,
                        full_context_threshold=0,
                        minimum_results=20,
                    )
                    expected = engine._memory.rank_memories(
                        query,
                        raw.messages,
                        limit=20,
                        include_claims=True,
                        max_chars=1200,
                        include_session_siblings=True,
                    )
                    actual = engine.search_memory_blocks(query, limit=20).blocks
                    self.assertEqual(actual, expected)
                    self.assertFalse(
                        any(block.kind == "evidence_pack" for block in actual)
                    )

    def test_semantic_source_fallback_survives_a_vocabulary_gap(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with Engine(
                str(Path(directory) / "memory.db"),
                user_id="aggregation-vocabulary-gap",
                semantic_dedup=False,
                local_only=True,
            ) as engine:
                _, message_ids = self._materialize(
                    engine,
                    session_id="sedan-arrival",
                    occurred_at=100.0,
                    messages=[
                        {
                            "role": "user",
                            "content": "The sedan joined my collection on Tuesday.",
                        }
                    ],
                    claims=[
                        {
                            "kind": "event",
                            "text": "The user acquired a sedan on Tuesday.",
                            "subject": "user",
                            "predicate": "acquired",
                            "object_text": "sedan",
                            "event_start": 90.0,
                        }
                    ],
                )

                # Dense retrieval can bridge vocabulary that the conservative
                # lexical/structured gate cannot. Supplying that source as the
                # fallback must retain it when no query-backed claim exists.
                pack = engine._memory._build_aggregation_pack(
                    "How many conveyances did I obtain in total?",
                    message_ids,
                    max_chars=1200,
                    fallback_source_message_ids=set(message_ids),
                )
                self.assertIsNotNone(pack)
                assert pack is not None
                self.assertIn("acquired a sedan", pack.block.text)
                self.assertEqual(set(pack.block.message_ids), set(message_ids))

    def test_current_state_and_non_aggregation_queries_keep_existing_ranking(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with Engine(
                str(Path(directory) / "memory.db"),
                user_id="parity",
                semantic_dedup=False,
                local_only=True,
            ) as engine:
                self._materialize(
                    engine,
                    session_id="inventory",
                    occurred_at=100.0,
                    messages=[
                        {"role": "user", "content": "I currently own 3 blue lanterns."}
                    ],
                    claims=[
                        {
                            "kind": "fact",
                            "text": "The user currently owns 3 blue lanterns.",
                            "subject": "user",
                            "predicate": "owns",
                            "object_text": "3 blue lanterns",
                        }
                    ],
                )

                for query in (
                    "How many blue lanterns do I have now?",
                    "Which lanterns are blue?",
                ):
                    raw = engine.search(
                        query,
                        limit=20,
                        max_context=20,
                        full_context_threshold=0,
                        minimum_results=20,
                    )
                    expected = engine._memory.rank_memories(
                        query,
                        raw.messages,
                        limit=20,
                        include_claims=True,
                        max_chars=1200,
                        include_session_siblings=True,
                    )
                    actual = engine.search_memory_blocks(query, limit=20).blocks
                    self.assertEqual(actual, expected)
                    self.assertFalse(
                        any(block.kind == "evidence_pack" for block in actual)
                    )

    def test_filters_scope_raw_claims_and_pack_sources(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = str(Path(directory) / "memory.db")
            with Engine(
                db_path,
                user_id="filter-owner",
                semantic_dedup=False,
                local_only=True,
            ) as engine:
                _, allowed_ids = self._materialize(
                    engine,
                    session_id="allowed-workspace",
                    occurred_at=100.0,
                    messages=[
                        {
                            "role": "user",
                            "content": "I completed the cobalt beacon repair.",
                            "provenance": {"workspace_id": "allowed"},
                        }
                    ],
                    claims=[
                        {
                            "kind": "event",
                            "text": "The user completed the cobalt beacon repair.",
                            "subject": "user",
                            "predicate": "completed repair",
                            "object_text": "cobalt beacon",
                        }
                    ],
                )
                _, blocked_ids = self._materialize(
                    engine,
                    session_id="blocked-workspace",
                    occurred_at=200.0,
                    messages=[
                        {
                            "role": "user",
                            "content": "I completed the amber beacon repair.",
                            "provenance": {"workspace_id": "blocked"},
                        }
                    ],
                    claims=[
                        {
                            "kind": "event",
                            "text": "The user completed the amber beacon repair.",
                            "subject": "user",
                            "predicate": "completed repair",
                            "object_text": "amber beacon",
                        }
                    ],
                )

                result = engine.search_memory_blocks(
                    "How many beacon repairs did I complete in total?",
                    limit=20,
                    filters={"workspace_id": "allowed"},
                )
                self.assertEqual(result.blocks[0].kind, "evidence_pack")
                self.assertIn("cobalt beacon", result.blocks[0].text)
                self.assertNotIn("amber beacon", result.blocks[0].text)
                self.assertTrue(
                    all(
                        set(block.message_ids) <= set(allowed_ids)
                        for block in result.blocks
                        if block.message_ids
                    )
                )
                self.assertTrue(
                    set(blocked_ids).isdisjoint(result.blocks[0].message_ids)
                )

                ordinary = engine.search_memory_blocks(
                    "Which beacon repair was completed?",
                    limit=20,
                    filters={"workspace_id": "allowed"},
                )
                self.assertFalse(
                    any("amber beacon" in block.text for block in ordinary.blocks)
                )
                self.assertTrue(
                    all(
                        set(block.message_ids) <= set(allowed_ids)
                        for block in ordinary.blocks
                        if block.message_ids
                    )
                )

            with Engine(
                db_path,
                user_id="different-user",
                semantic_dedup=False,
                local_only=True,
            ) as other:
                isolated = other.search_memory_blocks(
                    "How many beacon repairs did I complete in total?", limit=20
                )
                self.assertEqual(isolated.blocks, [])


if __name__ == "__main__":
    unittest.main()
