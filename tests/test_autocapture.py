"""Focused safety and behavior tests for deterministic personal auto-capture."""

from __future__ import annotations

import unittest

from narratordb.autocapture import (
    MAX_AUTOCAPTURE_CHARS,
    AutoCaptureCandidate,
    classify_prompt,
)


class AutoCapturePositiveTests(unittest.TestCase):
    def test_real_compound_favorite_phrase(self) -> None:
        candidates = classify_prompt(
            "i like porsche its truly my favorite car and dream car"
        )
        self.assertEqual(
            candidates,
            (
                AutoCaptureCandidate(
                    kind="favorite",
                    key="favorite:car",
                    value="Porsche",
                    canonical_text="The user's favorite and dream car is Porsche.",
                    rule_id="favorite.compound.v1",
                ),
            ),
        )

    def test_real_model_year_phrase(self) -> None:
        candidate = classify_prompt("i like the 911 turbo s from 2013")[0]
        self.assertEqual(candidate.kind, "preference")
        self.assertEqual(candidate.key, "preference:like:2013-911-turbo-s")
        self.assertEqual(candidate.value, "2013 911 Turbo S")
        self.assertEqual(
            candidate.canonical_text,
            "The user likes 2013 911 Turbo S.",
        )

    def test_real_porsche_911_phrase(self) -> None:
        candidate = classify_prompt("I really like Porsche 911")[0]
        self.assertEqual(candidate.key, "preference:like:porsche-911")
        self.assertEqual(candidate.canonical_text, "The user likes Porsche 911.")

    def test_conversational_prefix_preserves_personal_preference(self) -> None:
        for prompt in (
            "and i like mercedes",
            "Also I love jazz",
            "By the way, my favorite color is green",
        ):
            with self.subTest(prompt=prompt):
                self.assertEqual(len(classify_prompt(prompt)), 1)

        candidate = classify_prompt("and i like mercedes")[0]
        self.assertEqual(candidate.kind, "preference")
        self.assertEqual(candidate.key, "preference:like:mercedes-benz")
        self.assertEqual(candidate.canonical_text, "The user likes Mercedes-Benz.")

    def test_real_friday_routine_phrase(self) -> None:
        candidate = classify_prompt(
            "Fridays I like to drink coffee at 4 am in the park"
        )[0]
        self.assertEqual(candidate.kind, "routine")
        self.assertEqual(candidate.key, "routine:fridays:drink-coffee")
        self.assertEqual(
            candidate.value,
            "drink coffee at 4 a.m. in the park on Fridays",
        )
        self.assertEqual(
            candidate.canonical_text,
            "The user likes to drink coffee at 4 a.m. in the park on Fridays.",
        )

    def test_explicit_favorites_both_word_orders(self) -> None:
        forward = classify_prompt("My favorite programming language is rust")[0]
        reverse = classify_prompt("Porsche is my favourite car")[0]
        self.assertEqual(forward.key, "favorite:programming-language")
        self.assertEqual(forward.value, "Rust")
        self.assertEqual(
            forward.canonical_text,
            "The user's favorite programming language is Rust.",
        )
        self.assertEqual(reverse.key, "favorite:car")
        self.assertEqual(reverse.value, "Porsche")

    def test_dream_favorite(self) -> None:
        candidate = classify_prompt("My dream car is Porsche 911")[0]
        self.assertEqual(candidate.key, "dream:car")
        self.assertEqual(
            candidate.canonical_text,
            "The user's dream car is Porsche 911.",
        )

    def test_durable_routines(self) -> None:
        morning = classify_prompt("I usually go running every morning")[0]
        sunday = classify_prompt("On Sundays I cook breakfast")[0]
        self.assertEqual(
            morning.canonical_text,
            "The user's recurring routine is to go running every morning.",
        )
        self.assertEqual(
            sunday.canonical_text,
            "The user's recurring routine is to cook breakfast on Sundays.",
        )

    def test_assistant_response_preferences(self) -> None:
        cases = {
            "I prefer concise answers": (
                "assistant_response:verbosity",
                "The user prefers concise assistant responses.",
            ),
            "I prefer answers with bullet points": (
                "assistant_response:format",
                "The user prefers assistant responses with bullet points.",
            ),
            "Please always respond in plain language": (
                "assistant_response:style",
                "The user prefers assistant responses in plain language.",
            ),
            "Do not use emojis in your answers": (
                "assistant_response:emoji",
                "The user prefers assistant responses without emojis.",
            ),
        }
        for text, (key, canonical) in cases.items():
            with self.subTest(text=text):
                candidate = classify_prompt(text)[0]
                self.assertEqual(candidate.kind, "response_preference")
                self.assertEqual(candidate.key, key)
                self.assertEqual(candidate.canonical_text, canonical)

    def test_multiple_independent_durable_sentences(self) -> None:
        candidates = classify_prompt(
            "I love jazz. My favorite color is green. I usually walk every morning."
        )
        self.assertEqual(len(candidates), 3)
        self.assertEqual(
            [candidate.kind for candidate in candidates],
            ["preference", "favorite", "routine"],
        )

    def test_keys_are_stable_across_case_and_spacing(self) -> None:
        first = classify_prompt("I like Porsche 911")[0]
        second = classify_prompt("  i   like   porsche 911. ")[0]
        self.assertEqual(first.key, second.key)


class AutoCaptureSafetyTests(unittest.TestCase):
    def assertRejected(self, *prompts: str) -> None:  # noqa: N802 - unittest idiom
        for prompt in prompts:
            with self.subTest(prompt=prompt):
                self.assertEqual(classify_prompt(prompt), ())

    def test_questions_and_hypotheticals_are_rejected(self) -> None:
        self.assertRejected(
            "Do I like Porsche?",
            "I like Porsche?",
            "If I liked Porsche, it would be my favorite car",
            "Maybe I like Porsche",
            "I would like Porsche",
            "Suppose my favorite car is Porsche",
        )

    def test_quotes_code_urls_and_assignments_are_rejected(self) -> None:
        self.assertRejected(
            'He said "I like Porsche"',
            "'I like Porsche'",
            '`print("I like Porsche")`',
            "print(I like Porsche)",
            "I like https://example.com/porsche",
            "I like Porsche and preference=turbo",
            "I like [Porsche]",
        )

    def test_commands_are_rejected(self) -> None:
        self.assertRejected(
            "Remember I like Porsche",
            "Save my favorite car as Porsche",
            "Store that my favorite color is green",
            "Delete files. I like Porsche.",
            "Use Porsche for this task",
        )

    def test_transient_project_and_deictic_statements_are_rejected(self) -> None:
        self.assertRejected(
            "I like this",
            "I like that car",
            "I like Porsche right now",
            "Today I like Porsche",
            "I currently prefer tea",
            "I like working on this project",
            "I like the current codebase",
            "I prefer concise answers for this session",
        )

    def test_sensitive_and_secret_bearing_statements_are_rejected(self) -> None:
        self.assertRejected(
            "My favorite political party is Example Party",
            "My favorite medication is ExampleMed",
            "I like therapy",
            "I like Porsche and my api key is sk-proj-abcdefghijklmnop",
            "I like 4111 1111 1111 1111",
            "I like Porsche and my phone number is +1 212 555 0199",
            "I like jane@example.com",
            "I like [REDACTED]",
        )
        self.assertEqual(
            classify_prompt("I like Porsche", redaction_changed=True),
            (),
        )

    def test_multiline_paste_and_oversized_input_are_rejected(self) -> None:
        self.assertRejected(
            "I like Porsche.\n\nHere is a pasted document.",
            "I like Porsche\nMy favorite color is green",
            "I like " + ("Porsche " * MAX_AUTOCAPTURE_CHARS),
        )

    def test_unresolved_or_compound_claims_are_rejected(self) -> None:
        self.assertRejected(
            "I like it",
            "I like spending time with my family",
            "I like Porsche because it is fast",
            "I like Porsche but dislike the seats",
            "I like Porsche and my favorite color is green",
            "My favorite car is Porsche which is very fast",
        )

    def test_non_personal_and_unsupported_statements_are_rejected(self) -> None:
        self.assertRejected(
            "Porsche makes sports cars",
            "William likes Porsche",
            "I bought a Porsche yesterday",
            "The sky is blue",
            "Okay",
            "And Porsche makes sports cars",
            "Also remember I like Porsche",
            "",
        )


if __name__ == "__main__":
    unittest.main()
