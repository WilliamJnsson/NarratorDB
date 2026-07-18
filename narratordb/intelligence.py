"""Derived, source-linked memory storage and local context composition.

Raw messages remain NarratorDB's canonical record.  This module owns only
rebuildable data produced by an optional write-time compiler.  It deliberately
has no network or model dependencies so recall stays local in every mode.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import math
import re
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Mapping, Sequence


DERIVED_SCHEMA_VERSION = 3
CLAIM_FTS_FORMAT_VERSION = 1
CLAIM_RENDER_FORMAT_VERSION = 3
MEMORY_KEY_FORMAT_VERSION = 2
AGGREGATION_PACK_FORMAT_VERSION = 6
MAX_COMPILER_JOB_ATTEMPTS = 3
MAX_COMPILER_RETRY_DELAY_SECONDS = 24 * 60 * 60
CLAIM_KINDS = frozenset(
    {
        "fact",
        "preference",
        "event",
        "instruction",
        "identity",
        "relationship",
        "status",
        "summary",
        "other",
    }
)
CLAIM_STATUSES = frozenset({"active", "superseded", "retracted"})
CLAIM_RELATIONS = frozenset(
    {"updates", "contradicts", "supports", "extends", "derives"}
)

_WORD_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_'\-]{1,}")
_CURRENT_RE = re.compile(
    r"\b(now|current|currently|latest|today|still)\b", re.IGNORECASE
)
_HISTORY_RE = re.compile(
    r"\b(before|previous|previously|prior|earlier|history|used to)\b", re.IGNORECASE
)
_PAST_TENSE_RE = re.compile(r"\b(did|was|were|had)\b", re.IGNORECASE)
_ORDINAL_QUERY_RE = re.compile(r"\b(\d{1,4})(?:st|nd|rd|th)\b", re.IGNORECASE)
_URL_RE = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)
_URL_INTENT_RE = re.compile(
    r"\b(?:url|website|site|webpage|link|domain)\b", re.IGNORECASE
)
_CONTENT_FREE_REASON_RE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_MEMORY_KEY_PATH_SEPARATOR_RE = re.compile(r"[\\/:.]+")
_MEMORY_KEY_WORD_SEPARATOR_RE = re.compile(r"[\s-]+")
_MEMORY_KEY_UNDERSCORE_RE = re.compile(r"_+")

# Weighted reciprocal-rank fusion is deliberately rank based.  Claim FTS and
# raw hybrid retrieval use different score scales, so comparing their native
# scores directly would make one channel dominate for incidental reasons.
_FUSION_RANK_CONSTANT = 60.0
_RAW_FUSION_WEIGHT = 1.0
_CLAIM_FUSION_WEIGHT = 0.9
_RAW_CLAIM_SUPPORT_WEIGHT = 0.15
_CLAIM_RAW_SUPPORT_WEIGHT = 0.2
_CLAIM_SESSION_LIMIT = 12
_SESSION_SIBLINGS_PER_SESSION = 4
_SESSION_SIBLING_GLOBAL_CAP = 24
_ASSISTANT_SIBLING_FUSION_WEIGHT = 0.95
_OTHER_SIBLING_FUSION_WEIGHT = 0.65
_AGGREGATION_SOURCE_LIMIT = 24
_AGGREGATION_CLAIMS_PER_SOURCE = 12
_AGGREGATION_CANDIDATE_SCAN_LIMIT = _AGGREGATION_SOURCE_LIMIT * 8
_AGGREGATION_HEADER = (
    "Bounded evidence; no answer has been computed; reconcile other memories."
)
_PENDING_AGGREGATION_HEADER = (
    "Open obligations keyed by action and object; verify against source-linked evidence."
)
_CARDINAL_WORD_VALUES = {
    "zero": 0,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
    "twenty": 20,
}
_CARDINAL_SURFACE = r"(?:\d+(?:\.\d+)?|" + "|".join(
    _CARDINAL_WORD_VALUES
) + r")"
_CUMULATIVE_COUNTER_QUERY_RE = re.compile(
    r"\bhow\s+many\s+times\s+have\s+(?:i|we|the\s+user)\b",
    re.IGNORECASE,
)
_CUMULATIVE_COUNTER_VALUE_RE = re.compile(
    rf"\b(?P<value>{_CARDINAL_SURFACE})\s+times?\s+"
    r"(?:already|now|so\s+far|to\s+date)\b",
    re.IGNORECASE,
)
_CUMULATIVE_COUNTER_ANY_VALUE_RE = re.compile(
    rf"\b(?P<value>{_CARDINAL_SURFACE})\s+times?\b",
    re.IGNORECASE,
)
_CUMULATIVE_COUNTER_WINDOW_RE = re.compile(
    r"\b(?:since|during|over|within|between|from|last|past|this|previous|"
    r"today|yesterday)\b|"
    r"\bin\s+(?:the\s+)?(?:last|past|this|previous|january|february|march|"
    r"april|may|june|july|august|september|october|november|december|"
    r"week|month|year|q[1-4]|\d{1,4})\b",
    re.IGNORECASE,
)
_CUMULATIVE_COUNTER_VALUE_WINDOW_RE = re.compile(
    r"^\s+(?:(?:in|during|over|within|since|for)\s+)?(?:the\s+)?"
    r"(?:(?:this|last|past|previous|current)\s+"
    r"(?:day|week|month|quarter|year)s?|"
    r"january|february|march|april|may|june|july|august|september|"
    r"october|november|december|q[1-4]|\d{4})\b",
    re.IGNORECASE,
)
_CUMULATIVE_COUNTER_RESET_RE = re.compile(
    r"\b(?:reset|started?\s+over|from\s+scratch|counter\s+was\s+reset|"
    r"correction|correcting|mistake|actually\s+not)\b",
    re.IGNORECASE,
)
_COMPLETED_COUNT_QUERY_RE = re.compile(r"\bhow\s+many\b", re.IGNORECASE)
_COUNTED_DOMAIN_RE = re.compile(
    r"\bhow\s+many\s+(.+?)\s+(?:did|have|had)\s+(?:i|we|the\s+user)\b",
    re.IGNORECASE,
)
_NON_GROUP_COUNT_QUERY_RE = re.compile(
    r"\bhow\s+many\s+(?:"
    r"dollars?|euros?|yen|pounds?|cents?|hours?|minutes?|seconds?|days?|"
    r"weeks?|months?|years?|pages?|words?|times?|miles?|kilomet(?:er|re)s?|"
    r"meters?|centimet(?:er|re)s?|kilograms?|grams?|lit(?:er|re)s?|"
    r"percent(?:age)?(?:\s+points?)?"
    r")\b",
    re.IGNORECASE,
)
_DURATION_DAY_COUNT_QUERY_RE = re.compile(
    r"\bhow\s+many\s+days?\s+did\s+(?:i|we|the\s+user)\s+"
    r"(?:spend\s+)?(?P<action>[a-z][a-z'-]+)\b",
    re.IGNORECASE,
)
_GENERIC_GROUP_COUNT_NOUN_RE = re.compile(
    r"\b(?:items?|pieces?|pairs?|sets?|groups?|things?|objects?)\b",
    re.IGNORECASE,
)
_NAMED_COLLECTION_RE = re.compile(
    r"\b(?:a|an|one|the)?\s*(?:matching\s+)?(?:pair|set)\s+of\b|"
    r"\b(?:a|an|one|the)\s+[A-Za-z][A-Za-z'-]*\s+(?:pair|set)\b",
    re.IGNORECASE,
)
_COMPOSITE_MEASUREMENT_QUERY_RE = re.compile(
    rf"\b(?:what|which)\s+(?:was|were|is|are)\s+the\s+"
    rf"(?:combined\s+|total\s+)?(?P<measurement>page\s+count|word\s+count)\s+"
    rf"of\s+(?:the\s+)?(?P<count>{_CARDINAL_SURFACE})\b",
    re.IGNORECASE,
)
_MONTH_NAME_RE = re.compile(
    r"\b(?:january|february|march|april|may|june|july|august|september|"
    r"october|november|december)\b",
    re.IGNORECASE,
)
_YEAR_VALUE_RE = re.compile(r"\b(?:19|20)\d{2}\b")
_SCALAR_APPROXIMATION_BEFORE_RE = re.compile(
    r"\b(?:about|around|roughly|approximately|approx\.?|nearly|almost|"
    r"at\s+least|at\s+most|up\s+to|as\s+many\s+as|circa|"
    r"(?:a\s+)?(?:max(?:imum)?|min(?:imum)?)(?:\s+of)?|"
    r"no\s+(?:more|less|fewer)\s+than|"
    r"more\s+than|less\s+than|fewer\s+than|over|under)\s*$",
    re.IGNORECASE,
)
_SCALAR_APPROXIMATION_SYMBOL_BEFORE_RE = re.compile(r"(?:~|≈|≲|≳|≤|≥|<|>)\s*$")
_SCALAR_APPROXIMATION_AFTER_RE = re.compile(
    r"^\s*(?:(?:or\s+(?:so|more|fewer|less))\b|approximately\b|"
    r"roughly\b|at\s+least\b|at\s+most\b|\+|[- ]?ish\b)",
    re.IGNORECASE,
)
_SCALAR_RANGE_RE = re.compile(
    rf"\b(?:between\s+{_CARDINAL_SURFACE}\s+and|"
    rf"from\s+{_CARDINAL_SURFACE}\s+to)\s+{_CARDINAL_SURFACE}\b",
    re.IGNORECASE,
)
_UNSUPPORTED_EXACT_QUANTITY_RE = re.compile(
    r"\b(?:dozens?|scores?|couple|several|few|many|multiple|handfuls?)\b|"
    r"\b(?:twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety)-"
    r"(?:one|two|three|four|five|six|seven|eight|nine)\b",
    re.IGNORECASE,
)
_SCALAR_UNSUPPORTED_TEMPORAL_SCOPE_RE = re.compile(
    r"\b(?:today|yesterday|tomorrow|tonight|this|last|previous|current|past|"
    r"next)\s+(?:day|week|month|quarter|season|year)s?\b|"
    r"\b(?:spring|summer|autumn|fall|winter|q[1-4]|quarter|"
    r"before|after|since|during|between|through|until|within|excluding|except)\b",
    re.IGNORECASE,
)
_SCALAR_UNSUPPORTED_SET_SCOPE_RE = re.compile(
    r"\b(?:excluding|except(?:ing)?|not\s+counting|other\s+than|apart\s+from|"
    r"besides|with\s+the\s+exception\s+of|leaving\s+out)\b",
    re.IGNORECASE,
)
_SCALAR_UNSUPPORTED_TEMPORAL_GRANULARITY_RE = re.compile(
    r"\b(?:january|february|march|april|may|june|july|august|september|"
    r"october|november|december)\s+\d{1,2}(?:st|nd|rd|th)?\b|"
    r"\b(?:early|mid|late)[-\s]+(?:january|february|march|april|may|june|"
    r"july|august|september|october|november|december|month|year)\b|"
    r"\b(?:first|second|third|fourth|final)\s+(?:day|week|fortnight|half)\b",
    re.IGNORECASE,
)
_EXPLICIT_RETELLING_RE = re.compile(
    r"\bas\s+(?:i|we)\s+(?:said|mentioned|recalled|noted)"
    r"(?:\s+(?:before|earlier|previously))?\b|"
    r"\b(?:to\s+repeat|repeating\s+myself|again)\b",
    re.IGNORECASE,
)
_SCALAR_QUANTITY_COMPANION_RE = re.compile(
    r"\b(?:quantity|count|number|total|contained?|included?)\b|"
    r"^\s*there\s+(?:are|were|is|was)\b",
    re.IGNORECASE,
)
_RAW_NEGATED_ACTION_RE = re.compile(
    r"\b(?:do|does|did|have|has|had|was|were|am|is|are)\s+not\b|"
    r"\b(?:don['’]t|doesn['’]t|didn['’]t|haven['’]t|hasn['’]t|"
    r"hadn['’]t|wasn['’]t|weren['’]t)\b|\bnever\b",
    re.IGNORECASE,
)
_RAW_REPORTED_OTHER_ACTION_RE = re.compile(
    r"\b(?:i|we)\s+(?:said|say|heard|hear|think|thought|believe|believed|"
    r"watched|saw|know|knew|reported)\b",
    re.IGNORECASE,
)
_FUTURE_OR_ADVICE_RE = re.compile(
    r"\b(?:plan(?:s|ned|ning)?(?:\s+to)?|intend(?:s|ed|ing)?\s+to|"
    r"intention\s+to|scheduled\s+to|upcoming|going\s+to|"
    r"want(?:s|ed)?\s+to|hope(?:s|d)?\s+to|will|should|"
    r"recommend(?:s|ed|ing)?|could|might|"
    r"advis(?:e|es|ed|ing)|suggest(?:s|ed|ing)?|urge(?:s|d|ing)?|"
    r"think(?:s|ing)?\s+(?:about|of)|consider(?:s|ed|ing)?)\b",
    re.IGNORECASE,
)
_OPEN_OBLIGATION_RE = re.compile(
    r"\b(?:need(?:s|ed)?|required|obligated|have|has|had)\s+to\b|"
    r"\bmust\s+(?!not\b)\w+\b|"
    r"\b(?:has|have|had)\s+not\s+yet\s+\w+\b|"
    r"\b(?:hasn't|haven't|hadn't)\s+yet\s+\w+\b|"
    r"\bstill\s+(?:need(?:s)?|waiting)\b",
    re.IGNORECASE,
)
_CLOSED_OR_NEGATED_OBLIGATION_RE = re.compile(
    r"\b(?:do|does|did)\s+not\s+need\b|\b(?:don['’]t|doesn['’]t|"
    r"didn['’]t)\s+need\b|\bno\s+longer\s+need\b|"
    r"\b(?:do|does|did)\s+not\s+have\s+to\b|"
    r"\b(?:don['’]t|doesn['’]t|didn['’]t)\s+have\s+to\b|"
    r"\b(?:i|we)\s+(?:have|had)\s+no\s+need\s+to\b|"
    r"\b(?:i|we)\s+never\s+need\s+to\b|"
    r"\b(?:need|needs|needed)\s+not\b|"
    r"\bbut\b[^.!?;]{0,120}\b(?:already\s+)?(?:bought|purchased|acquired|"
    r"completed|finished|done|resolved|cancelled|canceled)\b",
    re.IGNORECASE,
)
_RETELLING_NOISE_WORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "at",
        "i",
        "in",
        "of",
        "on",
        "the",
        "to",
        "user",
        "was",
    }
)
_AGGREGATION_QUERY_NOISE_WORDS = frozenset(
    {
        "a",
        "all",
        "altogether",
        "ago",
        "amount",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "before",
        "been",
        "being",
        "by",
        "combined",
        "count",
        "counted",
        "counting",
        "cumulative",
        "day",
        "days",
        "did",
        "do",
        "does",
        "for",
        "from",
        "had",
        "has",
        "have",
        "how",
        "hour",
        "hours",
        "i",
        "in",
        "is",
        "many",
        "me",
        "minute",
        "minutes",
        "money",
        "month",
        "months",
        "my",
        "number",
        "of",
        "our",
        "overall",
        "past",
        "second",
        "seconds",
        "since",
        "start",
        "started",
        "starting",
        "the",
        "times",
        "to",
        "total",
        "user",
        "was",
        "we",
        "week",
        "weeks",
        "were",
        "what",
        "when",
        "where",
        "which",
        "who",
        "year",
        "years",
    }
)
_AGGREGATION_GENERIC_OBJECT_WORDS = frozenset(
    {
        "activities",
        "activity",
        "different",
        "dishes",
        "dish",
        "events",
        "event",
        "foods",
        "food",
        "instances",
        "instance",
        "items",
        "item",
        "kinds",
        "kind",
        "meals",
        "meal",
        "objects",
        "object",
        "occasions",
        "occasion",
        "anything",
        "something",
        "separate",
        "stuff",
        "things",
        "thing",
        "types",
        "type",
        "various",
    }
)
_COUNT_DOMAIN_UNIT_WORDS = frozenset(
    {
        "group",
        "groups",
        "individual",
        "individuals",
        "item",
        "items",
        "pair",
        "pairs",
        "piece",
        "pieces",
        "set",
        "sets",
    }
)
_AGGREGATION_TEMPORAL_MARKER = (
    r"(?:january|february|march|april|may|june|july|august|september|"
    r"october|november|december|spring|summer|autumn|fall|winter|"
    r"today|yesterday|tomorrow|week|weeks|month|months|quarter|quarters|"
    r"year|years|decade|decades|past|last|previous|current|early|late|"
    r"\d{4}|\d{1,2}(?:st|nd|rd|th)?)"
)
_AGGREGATION_TEMPORAL_WINDOW_RES = (
    re.compile(
        rf"\b(?:between|from)\b(?=[^?.,;]*\b{_AGGREGATION_TEMPORAL_MARKER}\b)"
        r"[^?.,;]*?(?:\band\b|\bto\b|\bthrough\b|\buntil\b)"
        r"[^?.,;]*(?=[?.,;]|$)",
        re.IGNORECASE,
    ),
    re.compile(
        rf"\b(?:during|throughout|since|before|after|within|over)\b"
        rf"(?=[^?.,;]*\b{_AGGREGATION_TEMPORAL_MARKER}\b)"
        r"[^?.,;]*(?=[?.,;]|$)",
        re.IGNORECASE,
    ),
    re.compile(
        rf"\b(?:in|on)\s+(?:the\s+)?(?={_AGGREGATION_TEMPORAL_MARKER}\b)"
        r"[^?.,;]*(?=[?.,;]|$)",
        re.IGNORECASE,
    ),
)
_AGGREGATION_QUERY_ACTION_RES = (
    re.compile(
        r"\b(?:did|do|does|have|has|had)\s+(?:i|we|the\s+user)\s+"
        r"(?:(?:ever|actually|already|personally|successfully)\s+)*"
        r"([a-z][a-z'-]+)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:i|we)\s+"
        r"(?:(?:ever|actually|already|personally|successfully)\s+)*"
        r"([a-z][a-z'-]+)\b",
        re.IGNORECASE,
    ),
)
_AGGREGATION_MONETARY_MEASURE_RE = re.compile(
    r"^\s*(?:how\s+much\b|how\s+many\s+(?:dollars?|euros?|pounds?|yen)\b|"
    r"what(?:\s+(?:is|was|were))?\s+(?:(?:the|my)\s+)?"
    r"(?:(?:total|combined|overall)\s+)?(?:amount(?:\s+of\s+money)?|money|"
    r"revenue|income|profit|proceeds|earnings?)\b|"
    r"(?:(?:the\s+)?(?:total|combined|overall)\s+)?"
    r"(?:amount\s+of\s+money|revenue|income|profit|proceeds|earnings?)\b)",
    re.IGNORECASE,
)
_AGGREGATION_EMBEDDED_ACTION_RES = (
    # In amount/time questions the first verb after the subject is often a
    # light measurement verb.  The gerund names the repeated event being
    # aggregated: ``earned from selling`` and ``spent attending``.
    (
        re.compile(
            r"\b(?:earn(?:s|ed|ing)?|mak(?:e|es|ing)|made)\b"
            r"(?:\s+[a-z][a-z'-]+){0,3}?\s+(?:from|by)\s+"
            r"([a-z][a-z'-]+ing)\b",
            re.IGNORECASE,
        ),
        True,
    ),
    (
        re.compile(
            r"\b(?:spend(?:s|ing)?|spent)\b"
            r"(?:\s+(?:time|days?|hours?|weeks?|months?))?\s+"
            r"([a-z][a-z'-]+ing)\b",
            re.IGNORECASE,
        ),
        False,
    ),
)
_AGGREGATION_LIGHT_MEASUREMENT_ACTIONS = frozenset(
    {
        "earn",
        "earned",
        "earning",
        "earns",
        "made",
        "make",
        "makes",
        "making",
        "spend",
        "spending",
        "spends",
        "spent",
    }
)
_AGGREGATION_FIELD_SEPARATOR_RE = re.compile(r"[^A-Za-z0-9]+")
_AGGREGATION_TERM_ALIASES = {
    "acquired": frozenset({"acquire"}),
    "bought": frozenset({"buy"}),
    "got": frozenset({"get"}),
    "purchased": frozenset({"purchase"}),
    "received": frozenset({"receive"}),
    "sale": frozenset({"sell", "sold"}),
    "sell": frozenset({"sale", "sold"}),
    "sold": frozenset({"sale", "sell"}),
}
_AGGREGATION_ACTION_CANONICAL = {
    "acquire": "acquire",
    "acquired": "acquire",
    "bought": "buy",
    "buy": "buy",
    "complete": "complete",
    "completed": "complete",
    "done": "complete",
    "finish": "complete",
    "finished": "complete",
    "get": "get",
    "got": "get",
    "made": "make",
    "make": "make",
    "makes": "make",
    "making": "make",
    "purchase": "buy",
    "purchased": "buy",
    "receive": "receive",
    "received": "receive",
    "sale": "sell",
    "sell": "sell",
    "sold": "sell",
    "ran": "run",
    "run": "run",
    "running": "run",
    "runs": "run",
    "write": "write",
    "writes": "write",
    "writing": "write",
    "written": "write",
    "wrote": "write",
}
_AGGREGATION_QUERY_ACTION_ENTAILMENTS = {
    "acquire": frozenset({"acquire", "buy", "get", "receive"}),
    "buy": frozenset({"buy"}),
    "complete": frozenset({"complete"}),
    "get": frozenset({"get"}),
    "receive": frozenset({"receive"}),
    "sell": frozenset({"sell"}),
}
_NONCOMPLETED_ACTION_RE = re.compile(
    r"\b(?:did\s+not|didn't|do\s+not|does\s+not|don't|doesn't|never|"
    r"failed\s+to|can\s+not|cannot|can't|"
    r"could\s+not|couldn't|won't|wouldn't|"
    r"(?:has|have|had)\s+not(?:\s+yet)?|hasn't(?:\s+yet)?|"
    r"haven't(?:\s+yet)?|hadn't(?:\s+yet)?|was\s+unable\s+to|"
    r"were\s+unable\s+to|wasn't\s+able\s+to|weren't\s+able\s+to|"
    r"not\s+to|without|cancelled|canceled|"
    r"not(?!\s+only\b))\b",
    re.IGNORECASE,
)
_NEGATION_SCOPE_FILLER_WORDS = frozenset(
    {
        "actually",
        "already",
        "an",
        "any",
        "ever",
        "her",
        "him",
        "me",
        "planned",
        "personally",
        "quite",
        "really",
        "still",
        "successfully",
        "that",
        "the",
        "them",
        "they",
        "to",
        "user",
        "we",
        "yet",
    }
)
_NEGATION_SCOPE_CONTROL_WORDS = frozenset(
    {
        "attempt",
        "attempted",
        "choose",
        "chose",
        "chosen",
        "confirm",
        "confirmed",
        "decide",
        "decided",
        "end",
        "ended",
        "finish",
        "finished",
        "get",
        "got",
        "if",
        "know",
        "knew",
        "manage",
        "managed",
        "recall",
        "recalled",
        "remember",
        "remembered",
        "think",
        "thought",
        "try",
        "tried",
        "up",
        "whether",
    }
)
_PENDING_GROUP_NOISE_WORDS = frozenset(
    {
        "a",
        "an",
        "has",
        "have",
        "had",
        "must",
        "need",
        "needs",
        "needed",
        "not",
        "still",
        "the",
        "to",
        "user",
        "yet",
    }
)
_PENDING_GENERIC_ACTION_WORDS = frozenset({"do"})
_AGGREGATION_BAKED_GOOD_WORDS = frozenset(
    {
        "bagel",
        "bagels",
        "baguette",
        "baguettes",
        "biscuit",
        "biscuits",
        "bread",
        "breads",
        "brownie",
        "brownies",
        "cake",
        "cakes",
        "cookie",
        "cookies",
        "croissant",
        "croissants",
        "focaccia",
        "loaf",
        "loaves",
        "muffin",
        "muffins",
        "pastry",
        "pastries",
        "pie",
        "pies",
        "scone",
        "scones",
        "sourdough",
        "tart",
        "tarts",
    }
)
_AGGREGATION_MAKE_ACTION_WORDS = frozenset({"mak", "make", "made", "makes", "making"})
_AGGREGATION_BAKED_GOOD_CLASSES = (
    ("sourdough", frozenset({"sourdough"})),
    ("baguette", frozenset({"baguette", "baguettes"})),
    ("cookie", frozenset({"cookie", "cookies"})),
    ("biscuit", frozenset({"biscuit", "biscuits"})),
    ("cake", frozenset({"cake", "cakes"})),
    ("bread", frozenset({"bread", "breads", "loaf", "loaves"})),
    ("pastry", frozenset({"pastry", "pastries", "croissant", "croissants"})),
    ("pie", frozenset({"pie", "pies"})),
    ("tart", frozenset({"tart", "tarts"})),
    ("muffin", frozenset({"muffin", "muffins"})),
    ("scone", frozenset({"scone", "scones"})),
    ("bagel", frozenset({"bagel", "bagels"})),
    ("brownie", frozenset({"brownie", "brownies"})),
    ("focaccia", frozenset({"focaccia"})),
)
_AGGREGATION_COMPANION_NOISE_WORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "amount",
        "at",
        "cost",
        "credit",
        "credits",
        "dollar",
        "dollars",
        "each",
        "for",
        "from",
        "in",
        "money",
        "of",
        "on",
        "price",
        "the",
        "to",
        "unit",
        "user",
        "was",
    }
)
_AGGREGATION_QUANTITY_RE = re.compile(
    r"(?:[$€£¥]\s*\d|\b\d+(?:[.,]\d+)?\b|"
    r"\b(?:one|two|three|four|five|six|seven|eight|nine|ten|"
    r"eleven|twelve)\b)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ClaimSource:
    message_id: int
    session_id: str
    speaker: str
    quote: str
    start: int | None = None
    end: int | None = None


@dataclass(frozen=True)
class MemoryClaim:
    id: int
    kind: str
    text: str
    status: str
    confidence: float
    subject: str = ""
    predicate: str = ""
    object_text: str = ""
    memory_key: str = ""
    document_time: float | None = None
    event_start: float | None = None
    event_end: float | None = None
    valid_from: float | None = None
    valid_to: float | None = None
    processor: str = ""
    sources: tuple[ClaimSource, ...] = ()
    score: float = 0.0
    channels: tuple[str, ...] = ()


@dataclass(frozen=True)
class ContextBlock:
    kind: str
    text: str
    claim_id: int | None = None
    message_ids: tuple[int, ...] = ()
    session_ids: tuple[str, ...] = ()
    status: str = "active"
    score: float = 0.0
    channels: tuple[str, ...] = ()
    token_count: int = 0
    composite_id: str | None = None


@dataclass
class ContextBundle:
    query: str
    text: str
    blocks: list[ContextBlock] = field(default_factory=list)
    token_count: int = 0
    token_budget: int = 6000
    query_ms: float = 0.0
    total_candidates: int = 0
    mode: str = "private"
    debug: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class _SessionMessage:
    id: int
    speaker: str
    text: str
    timestamp: float
    position: int
    provenance: dict[str, Any]


@dataclass(frozen=True)
class _RawFusionCandidate:
    message: Any
    hybrid_rank: int | None = None
    sibling_claim_rank: int | None = None
    sibling_local_rank: int | None = None


@dataclass(frozen=True)
class _AggregationClaimEvidence:
    claim_id: int
    kind: str
    text: str
    status: str
    subject: str
    predicate: str
    object_text: str
    memory_key: str
    document_time: float | None
    event_start: float | None
    event_end: float | None = None
    quote: str = ""


@dataclass(frozen=True)
class _AggregationSourceEvidence:
    message_id: int
    session_id: str
    source_rank: int
    selection_pass: int
    eligible_claim_count: int
    claims: tuple[_AggregationClaimEvidence, ...]


@dataclass(frozen=True)
class _AggregationPack:
    block: ContextBlock
    claim_ids: frozenset[int]
    represented_message_ids: frozenset[int]


@dataclass(frozen=True)
class _ScalarResolution:
    block: ContextBlock
    claim_ids: frozenset[int] = frozenset()
    represented_message_ids: frozenset[int] = frozenset()


_MAX_EXACT_SCALAR = 10**18


def _parse_cardinal(value: str) -> int | None:
    normalized = str(value or "").casefold()
    if normalized in _CARDINAL_WORD_VALUES:
        return _CARDINAL_WORD_VALUES[normalized]
    if len(normalized) > 32:
        return None
    try:
        parsed = Decimal(normalized)
    except InvalidOperation:
        return None
    if (
        not parsed.is_finite()
        or parsed != parsed.to_integral_value()
        or abs(parsed) > _MAX_EXACT_SCALAR
    ):
        return None
    return int(parsed)


def _format_scalar(value: int) -> str:
    return str(int(value))


def _scalar_match_is_exact(text: str, match: re.Match[str]) -> bool:
    sentence_start = max(
        text.rfind(".", 0, match.start()),
        text.rfind(";", 0, match.start()),
        text.rfind("\n", 0, match.start()),
    )
    sentence_end_candidates = [
        position
        for delimiter in (".", ";", "\n")
        if (position := text.find(delimiter, match.end())) >= 0
    ]
    sentence_end = min(sentence_end_candidates) if sentence_end_candidates else len(text)
    sentence = text[sentence_start + 1 : sentence_end]
    before = text[max(sentence_start + 1, match.start() - 40) : match.start()]
    after = text[match.end() : min(sentence_end, match.end() + 40)]
    return not (
        _SCALAR_APPROXIMATION_BEFORE_RE.search(before)
        or _SCALAR_APPROXIMATION_SYMBOL_BEFORE_RE.search(before)
        or _SCALAR_APPROXIMATION_AFTER_RE.search(after)
        or _SCALAR_RANGE_RE.search(sentence)
    )


def _scalar_subject_is_user(subject: str) -> bool:
    """Accept only the user principal, not possessive/related-person subjects."""

    tokens = _aggregation_field_tokens(subject)
    return "user" in tokens and tokens <= {"the", "user"}


def _exact_domain_quantity_values(
    values: Sequence[str],
    *,
    domain_tokens: frozenset[str],
    domain_token_groups: Sequence[frozenset[str]] = (),
) -> set[int] | None:
    """Parse exact item quantities attached to a queried head noun.

    Up to four adjective/modifier words may separate the quantity and noun.
    Measurement modifiers (``2-day workshop``), prices, years, ranges, and
    unsupported colloquial quantities are deliberately not item counts.
    """

    count_re = re.compile(
        rf"(?<![A-Za-z0-9-])(?P<count>{_CARDINAL_SURFACE})\b",
        re.IGNORECASE,
    )
    found: set[int] = set()
    for raw_value in values:
        if _UNSUPPORTED_EXACT_QUANTITY_RE.search(raw_value):
            return None
        for count_match in count_re.finditer(raw_value):
            surface = count_match.group("count")
            if (
                count_match.start() > 0
                and raw_value[count_match.start() - 1] in "$€£¥"
            ):
                continue
            if re.fullmatch(r"(?:19|20)\d{2}", surface):
                continue
            # A numeric compound describes a property of one object (for
            # example ``three-ring binder`` or ``4-pin connector``), not the
            # number of acquired objects. Duration/measurement compounds are
            # parsed by their dedicated scalar resolvers.
            if re.match(r"\s*-\s*[A-Za-z]", raw_value[count_match.end() :]):
                continue
            classifier_prefix = raw_value[max(0, count_match.start() - 32) : count_match.start()]
            if re.search(
                r"\b(?:type|model|version|series|grade|size|generation|gen|"
                r"mark|class|level)\s*$",
                classifier_prefix,
                re.IGNORECASE,
            ):
                continue
            remainder = raw_value[count_match.end() :]
            if re.match(
                r"\s*-?\s*(?:days?|weeks?|months?|years?|pages?|words?|"
                r"hours?|minutes?|seconds?|dollars?|euros?|pounds?|yen|"
                r"miles?|meters?|kilograms?|grams?|liters?)\b",
                remainder,
                re.IGNORECASE,
            ):
                continue
            attached = False
            matched_groups: set[int] = set()
            cursor = count_match.end()
            for index, word_match in enumerate(
                _WORD_RE.finditer(raw_value, count_match.end())
            ):
                if index >= 5:
                    break
                gap = raw_value[cursor : word_match.start()]
                if re.search(r"[.!?;,]", gap):
                    break
                token = word_match.group(0).casefold()
                if token in {
                    "at",
                    "each",
                    "for",
                    "from",
                    "in",
                    "of",
                    "on",
                    "per",
                    "to",
                }:
                    break
                variants = _aggregation_term_variants(token)
                for group_index, group in enumerate(domain_token_groups):
                    if group & variants:
                        matched_groups.add(group_index)
                if domain_token_groups and variants & domain_token_groups[-1]:
                    attached = len(matched_groups) == len(domain_token_groups)
                    break
                if not domain_token_groups and domain_tokens & variants:
                    attached = True
                    break
                cursor = word_match.end()
            if not attached:
                continue
            if not _scalar_match_is_exact(raw_value, count_match):
                return None
            parsed = _parse_cardinal(surface)
            if parsed is None or parsed <= 0:
                return None
            found.add(parsed)
    return found


def _domain_item_ordinal_present(
    values: Sequence[str],
    *,
    domain_token_groups: Sequence[frozenset[str]],
    category_keyed: bool,
) -> bool:
    """Distinguish object ordinals from dates before singular fallback."""

    temporal_suffix = re.compile(
        r"^\s*(?:of\b|days?\b|weeks?\b|months?\b|years?\b|"
        r"january\b|february\b|march\b|april\b|may\b|june\b|july\b|"
        r"august\b|september\b|october\b|november\b|december\b)",
        re.IGNORECASE,
    )
    for value in values:
        for ordinal_match in _ORDINAL_QUERY_RE.finditer(value):
            suffix = value[ordinal_match.end() : ordinal_match.end() + 100]
            if temporal_suffix.search(suffix):
                continue
            ordinal_phrase = value[
                ordinal_match.start() : ordinal_match.end() + 100
            ]
            if _domain_token_groups_match_noun_phrase(
                domain_token_groups,
                ordinal_phrase,
            ):
                return True
            if category_keyed and _WORD_RE.search(suffix):
                return True
    return False


def _raw_user_completed_action_clauses(
    text: str,
    *,
    action_tokens: frozenset[str],
) -> tuple[str, ...]:
    """Return raw clauses that certify a completed first-person action."""

    normalized = (
        str(text or "")
        .replace("I’ve", "I have")
        .replace("I've", "I have")
        .replace("we’ve", "we have")
        .replace("we've", "we have")
    )
    sentence_parts = re.split(r"(?<=[.!?;])\s+", normalized)
    parts: list[str] = []
    for sentence in sentence_parts:
        parts.extend(
            re.split(
                r"\b(?:and|but|while|whereas)\s+"
                r"(?=(?:[A-Z][a-z]+|he|she|they)\b)",
                sentence,
            )
        )
    certified: list[str] = []
    for clause in parts:
        clause = clause.strip()
        if (
            not clause
            or _RAW_NEGATED_ACTION_RE.search(clause)
            or _FUTURE_OR_ADVICE_RE.search(clause)
            or _RAW_REPORTED_OTHER_ACTION_RE.search(clause)
        ):
            continue
        words = [
            match.group(0).casefold()
            for match in re.finditer(r"[A-Za-z][A-Za-z'-]*", clause)
        ]
        action_positions = [
            index
            for index, token in enumerate(words)
            if action_tokens & _aggregation_action_evidence_tokens(token, token)
        ]
        allowed_between_subject_and_action = {
            "actually",
            "already",
            "also",
            "did",
            "do",
            "finally",
            "had",
            "have",
            "just",
            "now",
            "personally",
            "previously",
            "really",
            "recently",
            "currently",
            "successfully",
        }
        principal_action = False
        for index in action_positions:
            actor_positions = [
                position
                for position in range(max(0, index - 7), index)
                if words[position] in {"i", "we"}
            ]
            if not actor_positions:
                continue
            actor_position = actor_positions[-1]
            if all(
                token in allowed_between_subject_and_action
                for token in words[actor_position + 1 : index]
            ):
                principal_action = True
                break
        if not principal_action:
            continue
        evidence = _AggregationClaimEvidence(
            claim_id=0,
            kind="event",
            text=clause,
            status="active",
            subject="user",
            predicate=clause,
            object_text=clause,
            memory_key="",
            document_time=None,
            event_start=None,
            quote=clause,
        )
        if _aggregation_action_is_uncompleted_plan(
            evidence,
            action_tokens,
        ) or _aggregation_action_is_noncompleted(evidence, action_tokens):
            continue
        certified.append(clause)
    return tuple(dict.fromkeys(certified))


def _build_cumulative_counter_resolution(
    query: str,
    user_messages: Sequence[_SessionMessage],
    claims: Sequence[MemoryClaim],
    *,
    max_chars: int,
) -> _ScalarResolution | None:
    """Resolve an explicitly cumulative ``times`` counter monotonically.

    This is deliberately narrower than generic conflict resolution. It runs
    only for lifetime-style ``how many times have ...`` questions, requires at
    least two source-linked totals for the same query domain, and declines to
    act when a reset/correction or bounded time window is present.
    """

    normalized_query = str(query or "")
    if (
        not _CUMULATIVE_COUNTER_QUERY_RE.search(normalized_query)
        or _CUMULATIVE_COUNTER_WINDOW_RE.search(normalized_query)
        or _SCALAR_UNSUPPORTED_SET_SCOPE_RE.search(normalized_query)
    ):
        return None
    action_tokens = _aggregation_query_action_tokens(normalized_query)
    domain_tokens = _aggregation_query_domain_tokens(
        normalized_query,
        action_tokens,
    )
    messages_by_id = {message.id: message for message in user_messages}
    # A correction/reset can be recorded separately from a numeric total.
    # Scan every domain-relevant user source before considering candidates.
    for message in user_messages:
        evidence_tokens = _aggregation_field_tokens(message.text)
        if domain_tokens and len(domain_tokens & evidence_tokens) < min(
            2, len(domain_tokens)
        ):
            continue
        if _CUMULATIVE_COUNTER_RESET_RE.search(message.text):
            return None

    candidates: list[tuple[int, _SessionMessage, int, frozenset[str]]] = []
    seen_candidates: set[tuple[int, int, int]] = set()
    for claim in claims:
        if not _scalar_subject_is_user(claim.subject):
            continue
        claim_tokens = _aggregation_field_tokens(
            claim.text,
            claim.object_text,
            claim.predicate,
        )
        if domain_tokens and len(domain_tokens & claim_tokens) < min(
            2, len(domain_tokens)
        ):
            continue
        if not (
            action_tokens
            & _aggregation_action_evidence_tokens(claim.predicate, claim.text)
        ):
            continue
        evidence = _AggregationClaimEvidence(
            claim_id=claim.id,
            kind=claim.kind,
            text=claim.text,
            status=claim.status,
            subject=claim.subject,
            predicate=claim.predicate,
            object_text=claim.object_text,
            memory_key=claim.memory_key,
            document_time=claim.document_time,
            event_start=claim.event_start,
        )
        if _aggregation_action_is_uncompleted_plan(
            evidence, action_tokens
        ) or _aggregation_action_is_noncompleted(evidence, action_tokens):
            continue
        claim_matches = [
            (raw_value, value_match)
            for raw_value in (claim.object_text, claim.text)
            for value_match in _CUMULATIVE_COUNTER_ANY_VALUE_RE.finditer(raw_value)
        ]
        if not claim_matches or any(
            not _scalar_match_is_exact(raw_value, value_match)
            for raw_value, value_match in claim_matches
        ):
            continue
        values = {
            parsed
            for _, value_match in claim_matches
            if (parsed := _parse_cardinal(value_match.group("value"))) is not None
            and parsed >= 0
        }
        if len(values) != 1:
            continue
        value = next(iter(values))
        identity_tokens = frozenset(
            token
            for token in (
                _aggregation_field_tokens(
                    claim.text,
                    claim.object_text,
                    remove_query_noise=True,
                )
                - action_tokens
                - domain_tokens
                - {
                    "already",
                    "count",
                    "far",
                    "new",
                    "now",
                    "their",
                    "time",
                    "times",
                }
            )
            if not token.isdigit() and token not in _CARDINAL_WORD_VALUES
        )
        for source in claim.sources:
            if source.speaker.casefold() != "user":
                continue
            message = messages_by_id.get(source.message_id)
            if message is None:
                continue
            certified_clauses = _raw_user_completed_action_clauses(
                source.quote,
                action_tokens=action_tokens,
            )
            if not certified_clauses:
                continue
            source_matches = [
                (clause, quote_match)
                for clause in certified_clauses
                for quote_match in _CUMULATIVE_COUNTER_ANY_VALUE_RE.finditer(clause)
                if _parse_cardinal(quote_match.group("value")) == value
            ]
            cumulative_source_matches = [
                (clause, quote_match)
                for clause in certified_clauses
                for quote_match in _CUMULATIVE_COUNTER_VALUE_RE.finditer(clause)
                if _parse_cardinal(quote_match.group("value")) == value
            ]
            if len(source_matches) != 1 or len(cumulative_source_matches) != 1:
                continue
            if any(
                not _scalar_match_is_exact(clause, quote_match)
                for clause, quote_match in source_matches
            ):
                continue
            for clause, value_match in cumulative_source_matches:
                if (
                    _parse_cardinal(value_match.group("value")) == value
                    and _CUMULATIVE_COUNTER_VALUE_WINDOW_RE.search(
                        clause[value_match.end() : value_match.end() + 80]
                    )
                ):
                    return None
            candidate_key = (value, message.id, claim.id)
            if candidate_key in seen_candidates:
                continue
            seen_candidates.add(candidate_key)
            candidates.append((value, message, claim.id, identity_tokens))
    distinct_values = sorted({value for value, _, _, _ in candidates})
    if len(distinct_values) < 2:
        return None
    counter_identities = {identity for _, _, _, identity in candidates}
    if len(counter_identities) != 1:
        return None
    candidate_message_ids = tuple(
        dict.fromkeys(message.id for _, message, _, _ in candidates)
    )
    if len(candidate_message_ids) < 2:
        return None
    message_ids = candidate_message_ids
    resolved = max(distinct_values)
    rendered_values = ", ".join(_format_scalar(value) for value in distinct_values)
    source_map = "; ".join(
        f"message:{message.id} -> {_format_scalar(value)} times"
        for value, message, _, _ in sorted(
            candidates,
            key=lambda item: (item[1].id, item[2]),
        )
    )
    text = (
        "Resolved cumulative counter from exhaustive user-source evidence: "
        f"{_format_scalar(resolved)} times. Candidate cumulative totals: "
        f"{rendered_values}. Resolution rule: cumulative lifetime counters do not "
        "decrease unless a user source explicitly records a correction or reset. "
        f"Source mapping: {source_map}."
    )
    if len(text) > max_chars:
        return None
    identity_payload = {
        "format": AGGREGATION_PACK_FORMAT_VERSION,
        "mode": "cumulative_counter",
        "message_ids": sorted(message_ids),
        "value": _format_scalar(resolved),
        "candidate_values": [_format_scalar(value) for value in distinct_values],
        "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
    }
    composite_id = (
        "evidence-pack:"
        + hashlib.sha256(_json(identity_payload).encode("utf-8")).hexdigest()[:24]
    )
    session_ids = tuple(
        dict.fromkeys(
            str(
                message.provenance.get("metadata", {}).get("session_id")
                or message.provenance.get("run_id")
                or "unscoped"
            )
            for _, message, _, _ in candidates
        )
    )
    return _ScalarResolution(
        block=ContextBlock(
            kind="evidence_pack",
            text=text,
            message_ids=message_ids,
            session_ids=session_ids,
            score=1.0,
            channels=(
                "cumulative_counter_resolution",
                "source_linked",
                "local_only",
            ),
            token_count=estimate_tokens(text),
            composite_id=composite_id,
        ),
        claim_ids=frozenset(claim_id for _, _, claim_id, _ in candidates),
        represented_message_ids=frozenset(message_ids),
    )


def _build_composite_measurement_resolution(
    query: str,
    claims: Sequence[MemoryClaim],
    *,
    max_chars: int,
) -> _ScalarResolution | None:
    """Sum a fixed number of user-grounded measurement operands.

    Candidate measurements must either express the requested completed action
    themselves or share an exact source message with a claim that does. Values
    carrying an explicit month outside the query are excluded. This keeps
    recommendations and unrelated dated measurements out of the calculation.
    """

    match = _COMPOSITE_MEASUREMENT_QUERY_RE.search(str(query or ""))
    if not match:
        return None
    if (
        _SCALAR_UNSUPPORTED_SET_SCOPE_RE.search(query)
        or _SCALAR_UNSUPPORTED_TEMPORAL_GRANULARITY_RE.search(query)
    ):
        return None
    expected = _parse_cardinal(match.group("count"))
    if (
        expected is None
        or expected <= 0
    ):
        return None
    measurement = match.group("measurement").casefold()
    unit_label = "pages" if measurement.startswith("page") else "words"
    measurement_unit_tokens = frozenset(
        token
        for surface in ("page", "pages", "word", "words", unit_label)
        for token in _aggregation_term_variants(surface)
    )
    value_re = re.compile(
        rf"\b(?P<value>{_CARDINAL_SURFACE})\s*(?:-\s*)?{unit_label}?\b",
        re.IGNORECASE,
    )
    action_tokens = _aggregation_query_action_tokens(query)
    requested_months = {
        value.casefold() for value in _MONTH_NAME_RE.findall(query)
    }
    requested_years = set(_YEAR_VALUE_RE.findall(query))
    if _SCALAR_UNSUPPORTED_TEMPORAL_SCOPE_RE.search(query):
        # Relative, seasonal, quarter, and interval scopes need a full
        # temporal algebra. Until then, declining is safer than mixing sets.
        return None
    claims_by_message: dict[int, list[MemoryClaim]] = {}
    for claim in claims:
        for source in claim.sources:
            claims_by_message.setdefault(source.message_id, []).append(claim)

    def is_completed_action(claim: MemoryClaim) -> bool:
        if not _scalar_subject_is_user(claim.subject):
            return False
        evidence = _AggregationClaimEvidence(
            claim_id=claim.id,
            kind=claim.kind,
            text=claim.text,
            status=claim.status,
            subject=claim.subject,
            predicate=claim.predicate,
            object_text=claim.object_text,
            memory_key=claim.memory_key,
            document_time=claim.document_time,
            event_start=claim.event_start,
        )
        compiled_action = bool(
            action_tokens
            & _aggregation_action_evidence_tokens(claim.predicate, claim.text)
        ) and not (
            _aggregation_action_is_uncompleted_plan(evidence, action_tokens)
            or _aggregation_action_is_noncompleted(evidence, action_tokens)
        )
        return compiled_action and any(
            source.speaker.casefold() == "user"
            and bool(
                _raw_user_completed_action_clauses(
                    source.quote,
                    action_tokens=action_tokens,
                )
            )
            for source in claim.sources
        )

    def entities_are_compatible(
        measurement_tokens: frozenset[str],
        action_entity_tokens: frozenset[str],
    ) -> bool:
        if not measurement_tokens or not action_entity_tokens:
            return False
        shared = measurement_tokens & action_entity_tokens
        if measurement_tokens == action_entity_tokens:
            return True
        if len(shared) >= 2 and (
            len(shared) / min(len(measurement_tokens), len(action_entity_tokens))
            >= 0.6
        ):
            return True
        # A one-token proper/title entity may be embedded in a longer action
        # phrase. Merely sharing one token between two multi-token entities is
        # not enough (for example Cedar report vs Cedar manual).
        smaller = min(
            (measurement_tokens, action_entity_tokens),
            key=lambda value: (len(value), tuple(sorted(value))),
        )
        return len(smaller) == 1 and smaller <= shared

    candidates: list[tuple[int, int, MemoryClaim, tuple[int, ...]]] = []
    seen_logical_events: dict[tuple[str, ...], tuple[int, ...]] = {}
    temporally_conditioned = False
    for claim_rank, claim in enumerate(claims):
        user_sources = tuple(
            source
            for source in claim.sources
            if source.speaker.casefold() == "user"
        )
        user_message_ids = tuple(
            dict.fromkeys(source.message_id for source in user_sources)
        )
        if not user_message_ids:
            continue
        compiled_matches = [
            (raw_value, value_match)
            for raw_value in (claim.object_text, claim.text)
            for value_match in value_re.finditer(raw_value)
        ]
        if not compiled_matches or any(
            not _scalar_match_is_exact(raw_value, value_match)
            for raw_value, value_match in compiled_matches
        ):
            continue
        values = {
            parsed
            for _, value_match in compiled_matches
            if (parsed := _parse_cardinal(value_match.group("value"))) is not None
        }
        if len(values) != 1:
            continue
        value = next(iter(values))
        if value < 0:
            continue
        if any(_EXPLICIT_RETELLING_RE.search(source.quote) for source in user_sources):
            # Explicitly signaled paraphrases are not independent operands,
            # even when their surface entity names or timestamps drift.
            return None
        measurement_entity_tokens = frozenset(
            token
            for token in (
                _aggregation_field_tokens(
                    claim.subject,
                    claim.object_text,
                    claim.text,
                    remove_query_noise=True,
                )
                - _AGGREGATION_GENERIC_OBJECT_WORDS
                - action_tokens
            )
            if not token.isdigit()
            and token not in _CARDINAL_WORD_VALUES
            and token not in measurement_unit_tokens
            and token not in {"has", "was"}
        )
        every_source_is_grounded = True
        for source in user_sources:
            source_clauses = tuple(
                clause.strip()
                for clause in re.split(r"(?<=[.!?;])\s+", source.quote)
                if clause.strip()
            )
            source_matches = [
                (clause_index, clause, value_match)
                for clause_index, clause in enumerate(source_clauses)
                for value_match in value_re.finditer(clause)
            ]
            if not source_matches or any(
                not _scalar_match_is_exact(clause, value_match)
                for _, clause, value_match in source_matches
            ):
                every_source_is_grounded = False
                break
            source_values = {
                parsed
                for _, _, value_match in source_matches
                if (parsed := _parse_cardinal(value_match.group("value")))
                is not None
            }
            if source_values != {value}:
                every_source_is_grounded = False
                break
            for clause_index, clause, _ in source_matches:
                clause_entity_tokens = frozenset(
                    token
                    for token in _aggregation_field_tokens(
                        clause,
                        remove_query_noise=True,
                    )
                    if not token.isdigit()
                    and token not in _CARDINAL_WORD_VALUES
                    and token not in measurement_unit_tokens
                    and token not in {"has", "was"}
                )
                previous_clause = (
                    source_clauses[clause_index - 1]
                    if clause_index > 0
                    else ""
                )
                previous_entity_tokens = frozenset(
                    token
                    for token in _aggregation_field_tokens(
                        previous_clause,
                        remove_query_noise=True,
                    )
                    if not token.isdigit()
                    and token not in _CARDINAL_WORD_VALUES
                )
                safe_anaphor = bool(
                    re.search(r"\b(?:it|its|this|that)\b", clause, re.I)
                    and _raw_user_completed_action_clauses(
                        previous_clause,
                        action_tokens=action_tokens,
                    )
                    and entities_are_compatible(
                        measurement_entity_tokens,
                        previous_entity_tokens,
                    )
                )
                if not safe_anaphor and not entities_are_compatible(
                    measurement_entity_tokens,
                    clause_entity_tokens,
                ):
                    every_source_is_grounded = False
                    break
            if not every_source_is_grounded:
                break
        # Every message advertised by the scalar must independently contain
        # the exact operand; one good citation cannot launder another.
        if not every_source_is_grounded:
            continue
        supporting_actions: list[MemoryClaim] = []
        if is_completed_action(claim):
            supporting_actions.append(claim)
        for message_id in user_message_ids:
            for peer in claims_by_message.get(message_id, []):
                peer_entity_tokens = frozenset(
                    token
                    for token in (
                        _aggregation_field_tokens(
                            peer.subject,
                            peer.object_text,
                            peer.text,
                            remove_query_noise=True,
                        )
                        - _AGGREGATION_GENERIC_OBJECT_WORDS
                        - action_tokens
                    )
                    if not token.isdigit()
                    and token not in _CARDINAL_WORD_VALUES
                    and token not in measurement_unit_tokens
                    and token != "has"
                )
                if (
                    is_completed_action(peer)
                    and entities_are_compatible(
                        measurement_entity_tokens,
                        peer_entity_tokens,
                    )
                ):
                    if all(peer.id != value.id for value in supporting_actions):
                        supporting_actions.append(peer)
        if not supporting_actions:
            continue

        scope_parts = [
            claim.text,
            claim.object_text,
            *(source.quote for source in user_sources),
        ]
        for support in supporting_actions:
            scope_parts.extend((support.text, support.object_text))
            scope_parts.extend(
                source.quote
                for source in support.sources
                if source.speaker.casefold() == "user"
                and source.message_id in user_message_ids
            )
        scope_text = " ".join(scope_parts)
        raw_scope_text = " ".join(
            (
                *(source.quote for source in user_sources),
                *(
                    source.quote
                    for support in supporting_actions
                    for source in support.sources
                    if source.speaker.casefold() == "user"
                    and source.message_id in user_message_ids
                ),
            )
        )
        if _SCALAR_UNSUPPORTED_TEMPORAL_SCOPE_RE.search(raw_scope_text):
            continue
        scope_months = {
            item.casefold() for item in _MONTH_NAME_RE.findall(scope_text)
        }
        scope_years = set(_YEAR_VALUE_RE.findall(scope_text))
        if requested_months and scope_months and not scope_months <= requested_months:
            continue
        if requested_years and scope_years and not scope_years <= requested_years:
            continue
        event_times = tuple(
            sorted(
                {
                    float(candidate.event_start)
                    for candidate in (claim, *supporting_actions)
                    if candidate.event_start is not None
                }
            )
        )
        try:
            event_datetimes = tuple(
                datetime.fromtimestamp(value, tz=timezone.utc)
                for value in event_times
            )
        except (OSError, OverflowError, ValueError):
            continue
        premise_enumerates_temporal_buckets = bool(
            int(expected) > 1
            and len(requested_months) + len(requested_years) == int(expected)
        )
        has_textual_temporal_scope = bool(scope_months or scope_years)
        if (
            requested_months
            and event_datetimes
            and (has_textual_temporal_scope or not premise_enumerates_temporal_buckets)
        ):
            event_months = {
                value.strftime("%B").casefold() for value in event_datetimes
            }
            if not event_months <= requested_months:
                continue
        if (
            requested_years
            and event_datetimes
            and (has_textual_temporal_scope or not premise_enumerates_temporal_buckets)
        ):
            event_years = {str(value.year) for value in event_datetimes}
            if not event_years <= requested_years:
                continue
        if (requested_months and not scope_months) or (
            requested_years and not scope_years
        ):
            if not premise_enumerates_temporal_buckets:
                continue
            temporally_conditioned = True

        identity_tokens = tuple(
            sorted(
                token
                for token in measurement_entity_tokens
                if not token.isdigit()
                and token not in _CARDINAL_WORD_VALUES
                and not _MONTH_NAME_RE.fullmatch(token)
                and not _YEAR_VALUE_RE.fullmatch(token)
                and token not in {"count", "its", "the"}
            )
        )
        if not identity_tokens:
            continue
        logical_event = identity_tokens
        previous_message_ids = seen_logical_events.get(logical_event)
        if previous_message_ids is not None:
            if previous_message_ids != user_message_ids:
                # Same entity and event signature across separate messages is
                # an unresolved retelling, not an additional operand.
                return None
            continue
        seen_logical_events[logical_event] = user_message_ids
        candidates.append((claim_rank, value, claim, user_message_ids))
    if len(candidates) != int(expected):
        return None
    selected = candidates
    operands = [value for _, value, _, _ in selected]
    total = sum(operands)
    rendered_operands = " + ".join(_format_scalar(value) for value in operands)
    source_map = "; ".join(
        f"message:{','.join(str(value) for value in selected_message_ids)} -> "
        f"{_format_scalar(operand)} {unit_label}"
        for _, operand, _, selected_message_ids in selected
    )
    text = (
        "Computed scalar from exhaustive source-linked user measurement operands: "
        f"{_format_scalar(total)} {unit_label} "
        f"({rendered_operands} = {_format_scalar(total)}). "
        f"Operand count required by the query: {int(expected)}. "
        f"Source mapping: {source_map}."
    )
    if temporally_conditioned:
        text += (
            " Temporal scope: the arithmetic is conditioned on the set named "
            "by the query because not every operand source independently states "
            "that time label."
        )
    if len(text) > max_chars:
        return None
    claim_ids = frozenset(claim.id for _, _, claim, _ in selected)
    message_ids = tuple(
        dict.fromkeys(
            message_id
            for _, _, _, selected_message_ids in selected
            for message_id in selected_message_ids
        )
    )
    session_ids = tuple(
        dict.fromkeys(
            source.session_id
            for _, _, claim, _ in selected
            for source in claim.sources
            if source.message_id in message_ids
        )
    )
    identity_payload = {
        "format": AGGREGATION_PACK_FORMAT_VERSION,
        "mode": "composite_measurement",
        "claim_ids": sorted(claim_ids),
        "message_ids": sorted(message_ids),
        "unit": unit_label,
        "operands": [_format_scalar(value) for value in operands],
        "value": _format_scalar(total),
        "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
    }
    composite_id = (
        "evidence-pack:"
        + hashlib.sha256(_json(identity_payload).encode("utf-8")).hexdigest()[:24]
    )
    return _ScalarResolution(
        block=ContextBlock(
            kind="evidence_pack",
            text=text,
            message_ids=message_ids,
            session_ids=session_ids,
            score=1.0,
            channels=(
                "composite_measurement_resolution",
                "source_linked",
                "local_only",
            ),
            token_count=estimate_tokens(text),
            composite_id=composite_id,
        ),
        claim_ids=claim_ids,
        represented_message_ids=frozenset(message_ids),
    )


def estimate_tokens(text: str) -> int:
    """Return a conservative, dependency-free token estimate."""

    if not text:
        return 0
    return max(1, math.ceil(len(text.encode("utf-8")) / 3.6))


def _concise_excerpt(text: str, query: str, max_chars: int) -> str:
    """Return a bounded structural excerpt centered on likely answer evidence."""

    normalized = str(text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    cap = max(1, int(max_chars))
    if len(normalized) <= cap:
        return normalized
    if cap <= 3:
        return normalized[:cap]

    def excerpt_around(start: int, end: int) -> str:
        prefix = "...\n" if start > 0 else ""
        suffix = "\n..." if end < len(normalized) else ""
        available = max(1, cap - len(prefix) - len(suffix))
        span_length = max(1, end - start)
        if span_length >= available:
            window_start = start
        else:
            before = (available - span_length) // 3
            window_start = max(0, start - before)
        window_start = min(window_start, max(0, len(normalized) - available))
        body = normalized[window_start : window_start + available].strip()
        actual_prefix = "...\n" if window_start > 0 else ""
        actual_suffix = "\n..." if window_start + len(body) < len(normalized) else ""
        body_limit = max(1, cap - len(actual_prefix) - len(actual_suffix))
        return f"{actual_prefix}{body[:body_limit]}{actual_suffix}"[:cap]

    ordinal = _ORDINAL_QUERY_RE.search(query)
    if ordinal:
        item_number = re.escape(ordinal.group(1))
        item = re.search(
            rf"(?ms)^[ \t]*{item_number}[.)][ \t]+.*?"
            rf"(?=^[ \t]*\d{{1,4}}[.)][ \t]+|\Z)",
            normalized,
        )
        if item:
            return excerpt_around(item.start(), item.end())

    query_terms = {
        term.casefold() for term in _WORD_RE.findall(query) if len(term) >= 3
    }
    best_line: tuple[float, int, int] | None = None
    offset = 0
    for line in normalized.splitlines(keepends=True):
        line_text = line.rstrip("\n")
        line_terms = {term.casefold() for term in _WORD_RE.findall(line_text)}
        overlap = len(query_terms & line_terms)
        score = float(overlap)
        if query.casefold() in line_text.casefold():
            score += 2.0
        if _URL_RE.search(line_text):
            score += 1.5 if _URL_INTENT_RE.search(query) else 0.2
        candidate = (score, offset, offset + len(line_text))
        if best_line is None or candidate[0] > best_line[0]:
            best_line = candidate
        offset += len(line)
    if best_line is not None and best_line[0] > 0:
        return excerpt_around(best_line[1], best_line[2])

    lowered = normalized.casefold()
    anchors = [lowered.find(term) for term in query_terms if lowered.find(term) >= 0]
    anchor = min(anchors) if anchors else 0
    return excerpt_around(anchor, anchor + 1)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _mapping(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return dict(value)
    if dataclasses.is_dataclass(value):
        return dataclasses.asdict(value)
    if hasattr(value, "to_dict"):
        result = value.to_dict()
        if isinstance(result, Mapping):
            return dict(result)
    if hasattr(value, "__dict__"):
        return dict(vars(value))
    raise TypeError(
        f"expected a mapping-like compiled value, got {type(value).__name__}"
    )


def _list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def _float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        parsed = float(value)
        return parsed if math.isfinite(parsed) else None
    except (TypeError, ValueError):
        if isinstance(value, str):
            try:
                normalized = value.strip().replace("Z", "+00:00")
                parsed = datetime.fromisoformat(normalized)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                timestamp = parsed.timestamp()
                return timestamp if math.isfinite(timestamp) else None
            except ValueError:
                return None
        return None


def _canonical(value: Any) -> str:
    return " ".join(str(value or "").casefold().split())


def _bounded_chunks(values: Sequence[int], size: int = 800) -> Iterable[list[int]]:
    normalized = [int(value) for value in values]
    for start in range(0, len(normalized), max(1, int(size))):
        yield normalized[start : start + size]


def _format_utc_timestamp(value: float | None) -> str:
    if value is None:
        return "unspecified"
    try:
        return (
            datetime.fromtimestamp(float(value), timezone.utc)
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z")
        )
    except (OverflowError, OSError, TypeError, ValueError):
        return "unspecified"


def _looks_like_uncompleted_plan(*texts: str) -> bool:
    # The compiled claim is typed but may legitimately describe a future
    # event. Inspect its own wording rather than a domain-specific action list;
    # source-message speaker scoping separately removes assistant advice.
    primary = str(texts[0] if texts else "")
    return bool(_FUTURE_OR_ADVICE_RE.search(primary))


def _looks_like_open_obligation(*texts: str) -> bool:
    return bool(_OPEN_OBLIGATION_RE.search(" ".join(str(text) for text in texts)))


def _aggregation_term_variants(value: str) -> frozenset[str]:
    """Return small lexical variants for query-to-evidence matching.

    This is intentionally much narrower than a language stemmer. It only
    bridges common inflection differences while retaining the original token,
    so a weak grammatical overlap cannot turn into aggregation evidence by
    itself.
    """

    token = str(value or "").casefold()
    if not token:
        return frozenset()
    variants = {token}
    variants.update(_AGGREGATION_TERM_ALIASES.get(token, ()))
    if len(token) > 7 and token.endswith("ations"):
        variants.add(token[:-6])
    elif len(token) > 6 and token.endswith("ation"):
        variants.add(token[:-5])
    if len(token) > 5 and token.endswith("ing"):
        variants.add(token[:-3])
    if len(token) > 4 and token.endswith("ed"):
        variants.add(token[:-2])
        variants.add(token[:-2] + "e")
    if len(token) > 4 and token.endswith("ies"):
        variants.add(token[:-3] + "y")
    elif len(token) > 4 and token.endswith("es"):
        variants.add(token[:-2])
        variants.add(token[:-1])
    elif len(token) > 3 and token.endswith("s"):
        variants.add(token[:-1])
    return frozenset(variant for variant in variants if len(variant) >= 2)


def _counted_domain_profile(
    surface: str,
) -> tuple[frozenset[str], tuple[frozenset[str], ...], bool]:
    """Return lexical query groups and whether every group must match.

    Concrete plurals can be grounded directly in source wording, so modifiers
    such as ``red`` in ``red pins`` must remain attached to the noun. Broad
    mass categories such as ``clothing`` or ``jewelry`` require ontology-level
    instance matching and therefore retain the existing action-scoped path.
    """

    surface_terms = tuple(
        token
        for token in _WORD_RE.findall(str(surface or "").casefold())
        if token not in _AGGREGATION_QUERY_NOISE_WORDS
    )
    raw_terms = tuple(
        token
        for token in surface_terms
        if token not in _AGGREGATION_GENERIC_OBJECT_WORDS
        and token not in _COUNT_DOMAIN_UNIT_WORDS
    )
    groups = tuple(
        variants
        for term in raw_terms
        if (variants := _aggregation_term_variants(term))
    )
    tokens = frozenset(token for group in groups for token in group)
    generic_plural_unit = any(
        term in _COUNT_DOMAIN_UNIT_WORDS
        and term.endswith("s")
        and not term.endswith("ss")
        for term in surface_terms
    )
    requires_full_match = not bool(re.search(r",", str(surface or ""))) and (
        any(term.endswith("s") and not term.endswith("ss") for term in raw_terms)
        or (generic_plural_unit and len(raw_terms) >= 2)
    )
    return tokens, groups, requires_full_match


def _domain_token_groups_match(
    groups: Sequence[frozenset[str]],
    *values: str,
) -> bool:
    if not groups:
        return False
    tokens = _aggregation_field_tokens(*values)
    return all(bool(group & tokens) for group in groups)


def _domain_token_groups_match_noun_phrase(
    groups: Sequence[frozenset[str]],
    *values: str,
) -> bool:
    """Require modifiers to occur in the local phrase ending at its head noun."""

    if not groups:
        return False
    phrase_boundaries = {
        "after",
        "at",
        "before",
        "beside",
        "by",
        "containing",
        "from",
        "in",
        "inside",
        "into",
        "near",
        "on",
        "over",
        "through",
        "under",
        "with",
        "within",
    }
    for value in values:
        matches = list(_WORD_RE.finditer(str(value or "")))
        for head_index, head_match in enumerate(matches):
            if not (
                groups[-1]
                & _aggregation_term_variants(head_match.group(0).casefold())
            ):
                continue
            phrase_tokens: set[str] = set(
                _aggregation_term_variants(head_match.group(0).casefold())
            )
            cursor = head_match.start()
            for prior_match in reversed(matches[max(0, head_index - 6) : head_index]):
                gap = str(value or "")[prior_match.end() : cursor]
                if re.search(r"[.!?;,]", gap):
                    break
                token = prior_match.group(0).casefold()
                if token in phrase_boundaries:
                    break
                phrase_tokens.update(_aggregation_term_variants(token))
                cursor = prior_match.start()
            if all(bool(group & phrase_tokens) for group in groups):
                return True
    return False


def _aggregation_field_tokens(
    *values: str,
    remove_query_noise: bool = False,
) -> frozenset[str]:
    normalized = _AGGREGATION_FIELD_SEPARATOR_RE.sub(" ", " ".join(values))
    tokens: set[str] = set()
    for raw_token in _WORD_RE.findall(normalized.casefold()):
        if remove_query_noise and raw_token in _AGGREGATION_QUERY_NOISE_WORDS:
            continue
        tokens.update(_aggregation_term_variants(raw_token))
    return frozenset(tokens)


def _aggregation_query_focus_terms(query: str) -> tuple[str, ...]:
    """Return the domain/action terms that should drive aggregate recall.

    Count questions often contain generic objects (``types of food``) and a
    long date window. Those words are useful to the answerer but make a broad
    OR retrieval favor incidental date/object mentions over the repeated
    action. Remove complete temporal clauses and generic count objects only on
    the aggregation path; ordinary retrieval continues to receive the query
    verbatim.
    """

    focused = str(query or "")
    for pattern in _AGGREGATION_TEMPORAL_WINDOW_RES:
        focused = pattern.sub(" ", focused)
    terms: list[str] = []
    seen: set[str] = set()
    for raw_token in _WORD_RE.findall(focused.casefold()):
        if (
            raw_token in _AGGREGATION_QUERY_NOISE_WORDS
            or raw_token in _AGGREGATION_GENERIC_OBJECT_WORDS
            or raw_token in seen
        ):
            continue
        seen.add(raw_token)
        terms.append(raw_token)
    return tuple(terms)


def _aggregation_query_focus_tokens(query: str) -> frozenset[str]:
    tokens: set[str] = set()
    for term in _aggregation_query_focus_terms(query):
        tokens.update(_aggregation_term_variants(term))
    return frozenset(tokens)


def _aggregation_action_evidence_tokens(*values: str) -> frozenset[str]:
    """Normalize action evidence without widening a narrow query intent."""

    tokens: set[str] = set()
    normalized = _AGGREGATION_FIELD_SEPARATOR_RE.sub(
        " ", " ".join(values).casefold()
    )
    for raw_token in _WORD_RE.findall(normalized):
        canonical = _AGGREGATION_ACTION_CANONICAL.get(raw_token)
        if canonical is not None:
            tokens.add(canonical)
        else:
            tokens.update(_aggregation_term_variants(raw_token))
    return frozenset(tokens)


def _aggregation_query_action_resolution(
    query: str,
) -> tuple[frozenset[str], bool]:
    query_text = str(query or "")
    direct_surface = ""
    direct_start: int | None = None
    direct_tokens = frozenset()
    for pattern in _AGGREGATION_QUERY_ACTION_RES:
        match = pattern.search(query_text)
        if not match:
            continue
        direct_surface = match.group(1).casefold()
        direct_start = match.start(1)
        evidence_tokens = _aggregation_action_evidence_tokens(direct_surface)
        if evidence_tokens:
            canonical = min(evidence_tokens, key=lambda value: (len(value), value))
            direct_tokens = _AGGREGATION_QUERY_ACTION_ENTAILMENTS.get(
                canonical,
                evidence_tokens,
            )
        break

    # A later subordinate clause must not override the question's direct
    # action. Only light measurement verbs delegate to their embedded event
    # (``earned from selling`` or ``spent attending``).
    allow_embedded = not direct_tokens or (
        direct_surface in _AGGREGATION_LIGHT_MEASUREMENT_ACTIONS
    )
    monetary_measure = bool(_AGGREGATION_MONETARY_MEASURE_RE.search(query_text))
    if allow_embedded:
        embedded_candidates: list[tuple[int, frozenset[str]]] = []
        for pattern, requires_monetary_measure in _AGGREGATION_EMBEDDED_ACTION_RES:
            if requires_monetary_measure and not monetary_measure:
                continue
            for match in pattern.finditer(query_text):
                if direct_start is not None and match.start() != direct_start:
                    continue
                evidence_tokens = _aggregation_action_evidence_tokens(match.group(1))
                if not evidence_tokens:
                    continue
                canonical = min(
                    evidence_tokens,
                    key=lambda value: (len(value), value),
                )
                embedded_candidates.append(
                    (
                        match.start(),
                        _AGGREGATION_QUERY_ACTION_ENTAILMENTS.get(
                            canonical,
                            evidence_tokens,
                        ),
                    )
                )
        if embedded_candidates:
            return min(embedded_candidates, key=lambda item: item[0])[1], True
    return direct_tokens, False


def _aggregation_query_action_tokens(query: str) -> frozenset[str]:
    return _aggregation_query_action_resolution(query)[0]


def _completed_query_has_coordinated_tail(query: str) -> bool:
    """Decline scalar semantics when multiple post-subject actions are joined."""

    match = re.search(
        r"\b(?:did|have|had)\s+(?:i|we|the\s+user)\s+"
        r"(?:(?:ever|actually|already|personally|successfully)\s+)*"
        r"[a-z][a-z'-]+\b(?P<tail>[^?]*)",
        str(query or ""),
        re.IGNORECASE,
    )
    return bool(match and re.search(r"\b(?:and|or)\b", match.group("tail"), re.I))


def _aggregation_query_uses_embedded_action(query: str) -> bool:
    return _aggregation_query_action_resolution(query)[1]


def _aggregation_query_action_surface_terms(query: str) -> tuple[str, ...]:
    """Return bounded raw spellings that can express the queried action."""

    action_tokens = _aggregation_query_action_tokens(query)
    if not action_tokens:
        return ()
    surfaces = set(action_tokens)
    for surface, canonical in _AGGREGATION_ACTION_CANONICAL.items():
        if canonical in action_tokens:
            surfaces.add(surface)
    return tuple(sorted(surfaces))


def _aggregation_query_domain_tokens(
    query: str,
    action_tokens: frozenset[str],
) -> frozenset[str]:
    """Return non-count, non-action terms that ground an aggregate domain."""

    tokens: set[str] = set()
    for term in _aggregation_query_focus_terms(query):
        variants = _aggregation_term_variants(term)
        if (
            term in _AGGREGATION_GENERIC_OBJECT_WORDS
            or term in {"piece", "pieces"}
            or _aggregation_action_evidence_tokens(term) & action_tokens
        ):
            continue
        tokens.update(variants)
    return frozenset(tokens)


def _aggregation_query_uses_action_only_focus(query: str) -> bool:
    """Whether generic/window removal leaves only the requested action."""

    focus_terms = _aggregation_query_focus_terms(query)
    if len(focus_terms) != 1:
        return False
    return bool(
        _aggregation_action_evidence_tokens(focus_terms[0])
        & _aggregation_query_action_tokens(query)
    )


def _aggregation_query_has_temporal_window(query: str) -> bool:
    return any(
        pattern.search(str(query or "")) for pattern in _AGGREGATION_TEMPORAL_WINDOW_RES
    )


def _aggregation_normalize_scope_text(value: str) -> str:
    normalized = (
        str(value or "")
        .translate(str.maketrans({"‘": "'", "’": "'"}))
        .casefold()
    )
    return re.sub(r"[_-]+", " ", normalized)


def _aggregation_immediate_scope_has_action(
    clause: str,
    query_action_tokens: frozenset[str],
) -> bool:
    """Whether an immediate (possibly ``or``/``nor`` coordinated) VP acts."""

    for segment in re.split(r"\b(?:and|nor|or)\b", clause):
        words = _WORD_RE.findall(
            _AGGREGATION_FIELD_SEPARATOR_RE.sub(" ", segment)
        )[:8]
        raw_segment_tokens = frozenset(
            token.casefold() for token in _WORD_RE.findall(segment)
        )
        for raw_word in words:
            word = raw_word.casefold()
            action_tokens = _aggregation_action_evidence_tokens(word)
            if query_action_tokens & action_tokens:
                return True
            if (
                "bake" in query_action_tokens
                and action_tokens & _AGGREGATION_MAKE_ACTION_WORDS
                and raw_segment_tokens & _AGGREGATION_BAKED_GOOD_WORDS
            ):
                return True
            if word in (
                _NEGATION_SCOPE_FILLER_WORDS | _NEGATION_SCOPE_CONTROL_WORDS
            ):
                continue
            # The first substantive verb/noun belongs to a different VP; a
            # later action mention in this segment is outside the marker's
            # immediate scope.
            break
    return False


def _aggregation_future_marker_scopes_action(
    value: str,
    query_action_tokens: frozenset[str],
) -> bool:
    normalized = _aggregation_normalize_scope_text(value)
    for marker in _FUTURE_OR_ADVICE_RE.finditer(normalized):
        prefix = normalized[: marker.start()]
        if query_action_tokens & _aggregation_action_evidence_tokens(prefix):
            continue
        if "bake" in query_action_tokens:
            prefix_tokens = _aggregation_field_tokens(prefix)
            raw_prefix_tokens = frozenset(
                token.casefold() for token in _WORD_RE.findall(prefix)
            )
            if (
                prefix_tokens & _AGGREGATION_MAKE_ACTION_WORDS
                and raw_prefix_tokens & _AGGREGATION_BAKED_GOOD_WORDS
            ):
                continue
        clause = re.split(
            r"[.!?;,\n]|\b(?:although|but|though|whereas|while)\b",
            normalized[marker.end() :],
            maxsplit=1,
        )[0]
        if _aggregation_immediate_scope_has_action(clause, query_action_tokens):
            return True
    return False


def _aggregation_action_is_uncompleted_plan(
    claim: _AggregationClaimEvidence,
    query_action_tokens: frozenset[str],
) -> bool:
    """Use structured predicate scope before considering narrative plans."""

    normalized_predicate = _aggregation_normalize_scope_text(claim.predicate)
    for marker in _FUTURE_OR_ADVICE_RE.finditer(normalized_predicate):
        prefix = normalized_predicate[: marker.start()]
        if query_action_tokens & _aggregation_action_evidence_tokens(prefix):
            continue
        if "bake" in query_action_tokens:
            prefix_tokens = _aggregation_field_tokens(prefix)
            raw_prefix_tokens = frozenset(
                token.casefold() for token in _WORD_RE.findall(prefix)
            )
            if (
                prefix_tokens & _AGGREGATION_MAKE_ACTION_WORDS
                and raw_prefix_tokens & _AGGREGATION_BAKED_GOOD_WORDS
            ):
                continue
        # A structured predicate whose relation is future/advice is not a
        # completed event even when its surface verb is a vocabulary synonym
        # of the query rather than an exact action token.
        return True
    if _aggregation_future_marker_scopes_action(
        claim.text,
        query_action_tokens,
    ):
        return True

    predicate_actions = _aggregation_action_evidence_tokens(claim.predicate)
    predicate_is_completed = bool(query_action_tokens & predicate_actions)
    predicate_is_completed_bake = bool(
        "bake" in query_action_tokens
        and predicate_actions & _AGGREGATION_MAKE_ACTION_WORDS
        and _aggregation_field_tokens(claim.object_text)
        & _AGGREGATION_BAKED_GOOD_WORDS
    )
    if predicate_is_completed or predicate_is_completed_bake:
        return False

    # When the compiler used a generic relation (for example ``reported``),
    # a future marker still excludes the claim even if its planned action is a
    # vocabulary synonym of the query. A completed queried action before the
    # marker keeps prior-plan narration admissible.
    normalized_text = _aggregation_normalize_scope_text(claim.text)
    for marker in _FUTURE_OR_ADVICE_RE.finditer(normalized_text):
        prefix = normalized_text[: marker.start()]
        if query_action_tokens & _aggregation_action_evidence_tokens(prefix):
            continue
        if "bake" in query_action_tokens:
            prefix_tokens = _aggregation_field_tokens(prefix)
            raw_prefix_tokens = frozenset(
                token.casefold() for token in _WORD_RE.findall(prefix)
            )
            if (
                prefix_tokens & _AGGREGATION_MAKE_ACTION_WORDS
                and raw_prefix_tokens & _AGGREGATION_BAKED_GOOD_WORDS
            ):
                continue
        return True
    return False


def _aggregation_action_is_noncompleted(
    claim: _AggregationClaimEvidence,
    query_action_tokens: frozenset[str],
) -> bool:
    """Reject negation only when it scopes over the queried action.

    A negative outcome does not undo a completed event (for example, ``made
    bread, but it didn't turn out well``).  Search each non-completion marker's
    following clause for the requested action instead of rejecting a claim
    merely because any negative wording appears anywhere in it.
    """

    for value in (claim.predicate, claim.text, claim.object_text):
        normalized = _aggregation_normalize_scope_text(value)
        for marker in _NONCOMPLETED_ACTION_RE.finditer(normalized):
            clause = re.split(
                r"[.!?;,\n]|\b(?:although|but|though|whereas|while)\b",
                normalized[marker.end() :],
                maxsplit=1,
            )[0]
            if _aggregation_immediate_scope_has_action(
                clause,
                query_action_tokens,
            ):
                return True
    return False


def _aggregation_claim_matches_query(
    query_tokens: frozenset[str],
    claim: _AggregationClaimEvidence,
    *,
    query_action_tokens: frozenset[str] = frozenset(),
    query_domain_tokens: frozenset[str] = frozenset(),
) -> bool:
    """Conservatively admit a claim into a cumulative evidence pack.

    A semantic hit identifies a potentially relevant source message, not every
    fact compiled from that message. A memory-key or subject/object match is
    strong structured evidence. Free text and predicates need two independent
    content-term matches; this prevents an incidental word or number from
    pulling an unrelated co-located fact into the bounded pack.
    """

    if not query_tokens:
        return False

    if query_action_tokens:
        if _aggregation_action_is_uncompleted_plan(
            claim,
            query_action_tokens,
        ) or _aggregation_action_is_noncompleted(claim, query_action_tokens):
            return False
        predicate_tokens = _aggregation_action_evidence_tokens(claim.predicate)
        action_matches = bool(query_action_tokens & predicate_tokens)

        # Compilers sometimes classify a completed user action as a fact.
        # Admit that shape only when the structured subject is the user and
        # the action is present in the predicate; this excludes topical facts
        # such as a notebook category named "baked goods".
        if claim.kind.casefold() == "fact":
            if _canonical(claim.subject) not in {"i", "the user", "user", "we"}:
                return False
        elif claim.kind.casefold() != "event":
            return False

        if action_matches:
            if not query_domain_tokens:
                return True
            domain_evidence_tokens = _aggregation_field_tokens(
                claim.text,
                claim.subject,
                claim.object_text,
                claim.memory_key,
            )
            return bool(query_domain_tokens & domain_evidence_tokens)

        # ``make bread`` entails the queried baking action, while ``make a
        # chair`` does not. Keep this bridge event-only and object-grounded.
        if claim.kind.casefold() != "event" or "bake" not in query_action_tokens:
            return False
        action_evidence_tokens = _aggregation_field_tokens(
            claim.predicate,
            claim.object_text,
        )
        raw_content_tokens = {
            token.casefold()
            for token in _WORD_RE.findall(
                " ".join((claim.text, claim.predicate, claim.object_text))
            )
        }
        return bool(
            action_evidence_tokens & _AGGREGATION_MAKE_ACTION_WORDS
            and raw_content_tokens & _AGGREGATION_BAKED_GOOD_WORDS
        )

    memory_key_tokens = _aggregation_field_tokens(claim.memory_key)
    if query_tokens & memory_key_tokens:
        return True
    evidence_tokens = _aggregation_field_tokens(
        claim.text,
        claim.subject,
        claim.predicate,
        claim.object_text,
    )
    if len(query_tokens & evidence_tokens) >= 2:
        return True

    return False


def _aggregation_claim_content_lexemes(
    claim: _AggregationClaimEvidence,
) -> frozenset[str]:
    normalized = _AGGREGATION_FIELD_SEPARATOR_RE.sub(
        " ",
        " ".join((claim.text, claim.subject, claim.predicate, claim.object_text)),
    )
    lexemes: set[str] = set()
    for raw_token in _WORD_RE.findall(normalized.casefold()):
        variants = _aggregation_term_variants(raw_token)
        if not variants:
            continue
        base = min(variants, key=lambda value: (len(value), value))
        if base not in _AGGREGATION_COMPANION_NOISE_WORDS:
            lexemes.add(base)
    return frozenset(lexemes)


def _aggregation_claims_are_quantity_companions(
    left: _AggregationClaimEvidence,
    right: _AggregationClaimEvidence,
) -> bool:
    """Keep one source's event and its numeric fact together.

    A compiler may represent ``sold 20 plants`` and ``$7.50 each`` as two
    claims grounded in the same user message.  Once one claim matches the
    aggregate query, the other is admissible only when at least one of the
    pair is quantitative and they share two nontrivial content terms.  This
    retains the operands while rejecting unrelated co-located budgets.
    """

    if not (
        _AGGREGATION_QUANTITY_RE.search(left.text)
        or _AGGREGATION_QUANTITY_RE.search(left.object_text)
        or _AGGREGATION_QUANTITY_RE.search(right.text)
        or _AGGREGATION_QUANTITY_RE.search(right.object_text)
    ):
        return False
    return (
        len(
            _aggregation_claim_content_lexemes(left)
            & _aggregation_claim_content_lexemes(right)
        )
        >= 2
    )


def _aggregation_tokens(source: _AggregationSourceEvidence) -> frozenset[str]:
    values = []
    for claim in source.claims:
        typed = " ".join(
            value
            for value in (claim.subject, claim.predicate, claim.object_text)
            if value
        )
        values.append(typed or claim.text)
    return frozenset(
        token.casefold()
        for token in _WORD_RE.findall(" ".join(values))
        if token.casefold() not in _RETELLING_NOISE_WORDS
    )


def _aggregation_source_event_class(
    source: _AggregationSourceEvidence,
) -> str | None:
    raw_tokens = {
        token.casefold()
        for claim in source.claims
        for token in _WORD_RE.findall(
            " ".join((claim.text, claim.predicate, claim.object_text))
        )
    }
    for class_name, class_tokens in _AGGREGATION_BAKED_GOOD_CLASSES:
        if raw_tokens & class_tokens:
            return class_name
    return None


def _sources_may_be_retellings(
    left: _AggregationSourceEvidence,
    right: _AggregationSourceEvidence,
    *,
    action_tokens: frozenset[str] = frozenset(),
) -> bool:
    if left.session_id == right.session_id:
        return False
    left_times = sorted(
        claim.event_start for claim in left.claims if claim.event_start is not None
    )
    right_times = sorted(
        claim.event_start for claim in right.claims if claim.event_start is not None
    )
    if left_times and right_times and left_times != right_times:
        return False

    left_typed = sorted(
        _canonical(
            "|".join((claim.kind, claim.subject, claim.predicate, claim.object_text))
        )
        for claim in left.claims
        if claim.subject or claim.predicate or claim.object_text
    )
    right_typed = sorted(
        _canonical(
            "|".join((claim.kind, claim.subject, claim.predicate, claim.object_text))
        )
        for claim in right.claims
        if claim.subject or claim.predicate or claim.object_text
    )
    if left_typed and left_typed == right_typed:
        return True

    left_tokens = _aggregation_tokens(left)
    right_tokens = _aggregation_tokens(right)
    if not left_tokens or not right_tokens:
        return False
    similarity = len(left_tokens & right_tokens) / len(left_tokens | right_tokens)
    if similarity >= 0.82:
        return True
    if not action_tokens:
        return False

    left_predicate_tokens = _aggregation_action_evidence_tokens(
        *(claim.predicate for claim in left.claims)
    )
    right_predicate_tokens = _aggregation_action_evidence_tokens(
        *(claim.predicate for claim in right.claims)
    )
    if not (
        action_tokens & left_predicate_tokens and action_tokens & right_predicate_tokens
    ):
        return False
    left_content = frozenset().union(
        *(_aggregation_claim_content_lexemes(claim) for claim in left.claims)
    )
    right_content = frozenset().union(
        *(_aggregation_claim_content_lexemes(claim) for claim in right.claims)
    )
    shared = left_content & right_content
    return bool(
        len(shared) >= 3
        and len(shared) / min(len(left_content), len(right_content)) >= 0.65
    )


def _pending_group_tokens(value: str) -> frozenset[str]:
    normalized = _AGGREGATION_FIELD_SEPARATOR_RE.sub(
        " ", str(value or "").casefold()
    )
    normalized = re.sub(
        r"\bpick(?:ed|ing)?\s+up\b|\bpickup\b",
        " pickup ",
        normalized,
    )
    tokens: set[str] = set()
    for raw_token in _WORD_RE.findall(normalized):
        if raw_token in _PENDING_GROUP_NOISE_WORDS:
            continue
        variants = _aggregation_term_variants(raw_token)
        if variants:
            tokens.add(min(variants, key=lambda item: (len(item), item)))
    return frozenset(tokens)


def _pending_obligation_action_tokens(
    claim: _AggregationClaimEvidence,
) -> frozenset[str]:
    """Extract the first normalized action after an obligation marker."""

    normalized = _AGGREGATION_FIELD_SEPARATOR_RE.sub(
        " ", str(claim.predicate or claim.text).casefold()
    )
    marker = re.search(
        r"\b(?:still\s+)?(?:need|needs|needed|must|have\s+to|has\s+to|"
        r"had\s+to|has\s+not\s+yet|have\s+not\s+yet|had\s+not\s+yet)\b"
        r"\s+(.*)$",
        normalized,
    )
    action_phrase = marker.group(1) if marker else normalized
    tokens = _pending_group_tokens(action_phrase)
    if not tokens:
        return frozenset()
    ordered = [
        token
        for token in _pending_group_tokens(action_phrase)
        if token not in _PENDING_GENERIC_ACTION_WORDS
    ]
    if not ordered:
        return frozenset()
    # ``_pending_group_tokens`` is a set for object comparisons. Recover the
    # first significant surface token so an object's nouns cannot become part
    # of the action identity.
    normalized_phrase = re.sub(
        r"\bpick(?:ed|ing)?\s+up\b|\bpickup\b",
        " pickup ",
        _AGGREGATION_FIELD_SEPARATOR_RE.sub(" ", action_phrase),
        flags=re.IGNORECASE,
    )
    for raw_token in _WORD_RE.findall(normalized_phrase.casefold()):
        if raw_token in _PENDING_GROUP_NOISE_WORDS:
            continue
        variants = _aggregation_term_variants(raw_token)
        if not variants:
            continue
        token = min(variants, key=lambda item: (len(item), item))
        if token not in _PENDING_GENERIC_ACTION_WORDS:
            return frozenset({token})
    return frozenset()


def _pending_query_action_tokens(query: str) -> frozenset[str]:
    """Extract explicitly enumerated actions from a pending-count query."""

    normalized = _AGGREGATION_FIELD_SEPARATOR_RE.sub(
        " ", str(query or "").casefold()
    )
    marker = re.search(
        r"\b(?:need|needs|have|has)\s+to\s+(.+)$",
        normalized,
    )
    if not marker:
        return frozenset()
    actions: set[str] = set()
    for phrase in re.split(r"\s+(?:or|and)\s+|,", marker.group(1)):
        synthetic = _AggregationClaimEvidence(
            claim_id=0,
            kind="fact",
            text=phrase,
            status="active",
            subject="user",
            predicate=phrase,
            object_text="",
            memory_key="",
            document_time=None,
            event_start=None,
        )
        actions.update(_pending_obligation_action_tokens(synthetic))
    return frozenset(actions - _PENDING_GENERIC_ACTION_WORDS)


def _pending_query_domain_profile(
    query: str,
) -> tuple[frozenset[str], tuple[frozenset[str], ...], bool]:
    """Return a concrete counted noun when surface morphology supports it.

    Broad mass categories (for example, ``clothing``) need ontology knowledge
    to connect them to instances and therefore remain action-filtered. A
    concrete plural such as ``parcels`` can be checked lexically and must match
    the claim object. This favors precision without embedding a domain list.
    """

    normalized = _AGGREGATION_FIELD_SEPARATOR_RE.sub(
        " ", str(query or "").casefold()
    )
    match = re.search(
        r"\bhow\s+many\s+(.+?)\s+(?:do|does)\s+(?:i|we|the\s+user)\b",
        normalized,
    )
    if not match:
        return frozenset(), (), False
    return _counted_domain_profile(match.group(1))


def _pending_query_domain_tokens(query: str) -> tuple[frozenset[str], bool]:
    tokens, _, requires_match = _pending_query_domain_profile(query)
    return tokens, requires_match


def _raw_user_open_obligation_clauses(
    text: str,
    *,
    action_tokens: frozenset[str],
) -> tuple[str, ...]:
    normalized = (
        str(text or "")
        .replace("I haven't", "I have not")
        .replace("I haven’t", "I have not")
        .replace("we haven't", "we have not")
        .replace("we haven’t", "we have not")
    )
    clauses = [
        value.strip()
        for value in re.split(r"(?<=[.!?;])\s+", normalized)
        if value.strip()
    ]
    certified: list[str] = []
    for clause in clauses:
        if _CLOSED_OR_NEGATED_OBLIGATION_RE.search(clause):
            continue
        present_open = bool(
            re.search(
                r"\b(?:i|we)\s+(?:(?:just|really|still)\s+){0,3}"
                r"(?:need|have)\s+to\b|"
                r"\b(?:i|we)\s+(?:(?:just|really|still)\s+){0,3}must\b|"
                r"\b(?:i|we)\s+(?:(?:just|still)\s+){0,3}have\s+not\b"
                r"[^.!?;]{0,100}\byet\b|"
                r"\b(?:i|we)\s+[^.!?;]{1,120}?\band\s+"
                r"(?:(?:just|really|still)\s+){0,3}(?:need|have)\s+to\b",
                clause,
                re.IGNORECASE,
            )
        )
        if not present_open:
            continue
        synthetic = _AggregationClaimEvidence(
            claim_id=0,
            kind="fact",
            text=clause,
            status="active",
            subject="user",
            predicate=clause,
            object_text=clause,
            memory_key="",
            document_time=None,
            event_start=None,
            quote=clause,
        )
        if action_tokens & _pending_obligation_action_tokens(synthetic):
            certified.append(clause)
    return tuple(dict.fromkeys(certified))


def _pending_claim_is_open_for_user(claim: _AggregationClaimEvidence) -> bool:
    if not _scalar_subject_is_user(claim.subject):
        return False
    evidence_text = " ".join(
        (claim.text, claim.predicate, claim.object_text, claim.quote)
    )
    action_tokens = _pending_obligation_action_tokens(claim)
    return bool(
        action_tokens
        and _looks_like_open_obligation(
            claim.text,
            claim.predicate,
            claim.object_text,
            claim.quote,
        )
        and not _CLOSED_OR_NEGATED_OBLIGATION_RE.search(evidence_text)
        and _raw_user_open_obligation_clauses(
            claim.quote,
            action_tokens=action_tokens,
        )
    )


def _pending_claim_matches_query(
    query: str,
    claim: _AggregationClaimEvidence,
) -> bool:
    if _SCALAR_UNSUPPORTED_SET_SCOPE_RE.search(query):
        return False
    if not _pending_claim_is_open_for_user(claim):
        return False
    requested_actions = _pending_query_action_tokens(query)
    if requested_actions and not bool(
        requested_actions & _pending_obligation_action_tokens(claim)
    ):
        return False
    domain_tokens, domain_groups, requires_domain_match = (
        _pending_query_domain_profile(query)
    )
    if requires_domain_match:
        action_tokens = _pending_obligation_action_tokens(claim)
        certified_clauses = _raw_user_open_obligation_clauses(
            claim.quote,
            action_tokens=action_tokens,
        )
        if not (
            _domain_token_groups_match_noun_phrase(
                domain_groups,
                claim.object_text,
                claim.memory_key,
            )
            and _domain_token_groups_match_noun_phrase(
                domain_groups, *certified_clauses
            )
        ):
            return False
    return True


def _pending_obligation_group_count(
    claims: Sequence[_AggregationClaimEvidence],
    *,
    query: str,
) -> tuple[int, bool]:
    """Count rendered open obligations by normalized action and object.

    Duplicate retellings join only when the normalized action is identical and
    the structured objects overlap strongly. Different actions against the
    same object therefore remain separate obligations.
    """

    if _SCALAR_UNSUPPORTED_SET_SCOPE_RE.search(query):
        return 0, False
    domain_tokens, domain_groups, requires_domain_match = (
        _pending_query_domain_profile(query)
    )
    keyed: list[
        tuple[
            frozenset[str],
            frozenset[str],
            frozenset[str],
            float | None,
            int | None,
        ]
    ] = []
    for claim in claims:
        if not _pending_claim_is_open_for_user(claim):
            continue
        action = _pending_obligation_action_tokens(claim)
        obj = _pending_group_tokens(claim.object_text)
        requested_actions = _pending_query_action_tokens(query)
        if (
            action
            and obj
            and (not requested_actions or bool(action & requested_actions))
        ):
            certified_clauses = _raw_user_open_obligation_clauses(
                claim.quote,
                action_tokens=action,
            )
            if not certified_clauses:
                return 0, False
            if requires_domain_match and not (
                _domain_token_groups_match_noun_phrase(
                    domain_groups,
                    claim.object_text,
                    claim.memory_key,
                )
                and _domain_token_groups_match_noun_phrase(
                    domain_groups,
                    *certified_clauses,
                )
            ):
                return 0, False
            explicit_values = _exact_domain_quantity_values(
                (claim.object_text, claim.text),
                domain_tokens=domain_tokens,
                domain_token_groups=(
                    domain_groups if requires_domain_match else ()
                ),
            )
            quote_values = _exact_domain_quantity_values(
                certified_clauses,
                domain_tokens=domain_tokens,
                domain_token_groups=(
                    domain_groups if requires_domain_match else ()
                ),
            )
            if (
                explicit_values is None
                or quote_values is None
                or len(explicit_values) > 1
                or explicit_values != quote_values
            ):
                return 0, False
            if requires_domain_match and not explicit_values:
                def has_singular_article(value: str) -> bool:
                    return any(
                        domain_groups[-1]
                        & _aggregation_term_variants(article_match.group("noun"))
                        and _domain_token_groups_match_noun_phrase(
                            domain_groups,
                            article_match.group(0),
                        )
                        for article_match in re.finditer(
                            r"\b(?:a|an)\s+"
                            r"(?:[A-Za-z][A-Za-z'-]*\s+){0,3}"
                            r"(?P<noun>[A-Za-z][A-Za-z'-]*)\b",
                            value,
                            re.IGNORECASE,
                        )
                    )

                if not (
                    has_singular_article(claim.object_text)
                    and has_singular_article(claim.quote)
                ):
                    return 0, False
            keyed.append(
                (
                    action,
                    obj,
                    _pending_group_tokens(claim.memory_key),
                    claim.event_start,
                    next(iter(explicit_values)) if explicit_values else None,
                )
            )
    parents = list(range(len(keyed)))

    def find(index: int) -> int:
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    def strong_object_overlap(
        left: frozenset[str], right: frozenset[str]
    ) -> bool:
        if left == right:
            return True
        shared = left & right
        return bool(
            len(shared) >= 2
            and len(shared) / min(len(left), len(right)) >= 0.75
        )

    def strong_key_overlap(
        left: frozenset[str], right: frozenset[str]
    ) -> bool:
        if not left or not right:
            return False
        shared = left & right
        return bool(
            len(shared) >= 3
            and len(shared) / min(len(left), len(right)) >= 0.7
        )

    for left in range(len(keyed)):
        for right in range(left + 1, len(keyed)):
            same_durable_event = bool(
                keyed[left][3] is not None
                and keyed[left][3] == keyed[right][3]
            )
            if (
                keyed[left][0] != keyed[right][0]
                or not strong_object_overlap(keyed[left][1], keyed[right][1])
                or not (
                    strong_key_overlap(keyed[left][2], keyed[right][2])
                    or same_durable_event
                )
            ):
                continue
            left_root = find(left)
            right_root = find(right)
            if left_root != right_root:
                parents[right_root] = left_root
    quantities_by_root: dict[int, set[int]] = {}
    for index, entry in enumerate(keyed):
        if entry[4] is not None:
            quantities_by_root.setdefault(find(index), set()).add(entry[4])
    if any(len(values) > 1 for values in quantities_by_root.values()):
        return 0, False
    roots = {find(index) for index in range(len(keyed))}
    used_explicit_quantity = bool(quantities_by_root)
    total = sum(
        next(iter(quantities_by_root[root]))
        if quantities_by_root.get(root)
        else 1
        for root in roots
    )
    return total, used_explicit_quantity


def _canonical_memory_key(value: Any) -> str:
    """Normalize harmless model formatting drift in a semantic state key.

    The public compiler asks for lowercase dotted paths with underscore word
    separators. Hosted models can still vary slash, colon, whitespace, or
    hyphen punctuation across sessions. Those spelling differences must not
    leave two active values for the same state slot.
    """

    normalized = _canonical(value)
    if not normalized:
        return ""
    segments = []
    for raw_segment in _MEMORY_KEY_PATH_SEPARATOR_RE.split(normalized):
        segment = _MEMORY_KEY_WORD_SEPARATOR_RE.sub("_", raw_segment)
        segment = _MEMORY_KEY_UNDERSCORE_RE.sub("_", segment).strip("_")
        if segment:
            segments.append(segment)
    return ".".join(segments)


def _claim_precedence(
    document_time: float | None, session_id: int
) -> tuple[int, float, int]:
    """Return stable current-state precedence for a cross-session claim.

    A dated claim beats an undated claim, then later document time wins. Exact
    ties (including two undated claims) use the durable session row id, so the
    later-registered session wins. Claim ids and compiler completion times are
    intentionally excluded: recompiling a session cannot change the winner.
    """

    if document_time is None or not math.isfinite(document_time):
        return (0, 0.0, int(session_id))
    return (1, float(document_time), int(session_id))


def _normalize_compiled_payload(value: Any) -> dict[str, Any]:
    """Normalize the public compiler dataclasses into the storage wire shape."""

    payload = _mapping(value)
    summary_value = payload.get("summary")
    if summary_value is not None and not isinstance(summary_value, str):
        summary = _mapping(summary_value)
        payload["summary"] = str(summary.get("text") or "")
        payload["summary_evidence"] = _list(summary.get("evidence"))

    entities = [_mapping(item) for item in _list(payload.get("entities"))]
    entity_by_id = {
        str(entity.get("entity_id")): entity
        for entity in entities
        if entity.get("entity_id") is not None
    }
    normalized_entities = []
    for entity in entities:
        normalized_entities.append(
            {
                "canonical_name": entity.get("name") or entity.get("canonical_name"),
                "entity_type": entity.get("entity_type") or entity.get("type") or "",
                "aliases": _list(entity.get("aliases")),
            }
        )
    payload["entities"] = normalized_entities

    claims = [_mapping(item) for item in _list(payload.get("claims"))]
    claim_index: dict[str, int] = {}
    normalized_claims = []
    for index, claim in enumerate(claims):
        claim_id = str(claim.get("claim_id") or index)
        claim_index[claim_id] = index
        claim_entities = [
            entity_by_id[entity_id]
            for entity_id in (str(value) for value in _list(claim.get("entity_ids")))
            if entity_id in entity_by_id
        ]
        normalized_claims.append(
            {
                **claim,
                "kind": claim.get("kind") or "fact",
                "entities": [
                    {
                        "canonical_name": entity.get("name"),
                        "entity_type": entity.get("entity_type") or "",
                        "aliases": _list(entity.get("aliases")),
                    }
                    for entity in claim_entities
                ],
                "subject": claim.get("subject")
                or (str(claim_entities[0].get("name")) if claim_entities else ""),
                "predicate": claim.get("predicate") or claim.get("kind") or "fact",
                "object_text": claim.get("object_text") or claim.get("object") or "",
                "memory_key": claim.get("memory_key") or "",
            }
        )
    payload["claims"] = normalized_claims

    normalized_relations = []
    for item in _list(payload.get("relations")):
        relation = _mapping(item)
        if (
            relation.get("source_index") is not None
            and relation.get("target_index") is not None
        ):
            try:
                source = int(relation["source_index"])
                target = int(relation["target_index"])
            except (TypeError, ValueError):
                continue
        else:
            source = claim_index.get(str(relation.get("source_claim_id")))
            target = claim_index.get(str(relation.get("target_claim_id")))
        if source is None or target is None:
            continue
        normalized_relations.append(
            {
                "source_index": source,
                "target_index": target,
                "type": relation.get("type")
                or relation.get("kind")
                or relation.get("relation_type"),
                "confidence": relation.get("confidence", 1.0),
            }
        )
    payload["relations"] = normalized_relations
    return payload


class DerivedMemoryStore:
    """SQLite-backed rebuildable memory for a single NarratorDB user scope."""

    def __init__(self, connection: sqlite3.Connection, user_id: str, user_key: str):
        self.connection = connection
        self.user_id = user_id
        self.user_key = user_key
        self.fts_table = f"claim_fts_{user_key}"
        self._init_schema()

    def _init_schema(self) -> None:
        metadata_exists = self.connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'metadata'"
        ).fetchone()
        if metadata_exists is not None:
            version_row = self.connection.execute(
                "SELECT value FROM metadata WHERE key = 'derived_schema_version'"
            ).fetchone()
            stored_version = (
                int(version_row[0])
                if version_row and str(version_row[0]).isdigit()
                else None
            )
            if stored_version is not None and stored_version > DERIVED_SCHEMA_VERSION:
                raise RuntimeError(
                    "derived memory schema is newer than this NarratorDB runtime"
                )
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS memory_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                external_id TEXT NOT NULL,
                occurred_at REAL,
                source_hash TEXT NOT NULL,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                UNIQUE(user_id, external_id)
            );
            CREATE INDEX IF NOT EXISTS idx_memory_sessions_user_time
                ON memory_sessions(user_id, occurred_at);

            CREATE TABLE IF NOT EXISTS memory_session_messages (
                session_id INTEGER NOT NULL REFERENCES memory_sessions(id) ON DELETE CASCADE,
                message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
                ordinal INTEGER NOT NULL,
                PRIMARY KEY(session_id, message_id)
            );
            CREATE INDEX IF NOT EXISTS idx_memory_session_messages_message
                ON memory_session_messages(message_id);

            CREATE TABLE IF NOT EXISTS memory_claims (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                session_id INTEGER REFERENCES memory_sessions(id) ON DELETE CASCADE,
                claim_key TEXT NOT NULL,
                kind TEXT NOT NULL,
                text TEXT NOT NULL,
                subject TEXT NOT NULL DEFAULT '',
                predicate TEXT NOT NULL DEFAULT '',
                object_text TEXT NOT NULL DEFAULT '',
                memory_key TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'active',
                confidence REAL NOT NULL DEFAULT 1.0,
                document_time REAL,
                event_start REAL,
                event_end REAL,
                valid_from REAL,
                valid_to REAL,
                processor TEXT NOT NULL,
                processor_version TEXT NOT NULL,
                prompt_version TEXT NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                UNIQUE(user_id, claim_key)
            );
            CREATE INDEX IF NOT EXISTS idx_memory_claims_user_status
                ON memory_claims(user_id, status);
            CREATE INDEX IF NOT EXISTS idx_memory_claims_user_subject
                ON memory_claims(user_id, subject, predicate);
            CREATE INDEX IF NOT EXISTS idx_memory_claims_user_event
                ON memory_claims(user_id, event_start, event_end);

            CREATE TABLE IF NOT EXISTS memory_claim_sources (
                claim_id INTEGER NOT NULL REFERENCES memory_claims(id) ON DELETE CASCADE,
                message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
                quote TEXT NOT NULL,
                span_start INTEGER,
                span_end INTEGER,
                PRIMARY KEY(claim_id, message_id, quote)
            );
            CREATE INDEX IF NOT EXISTS idx_memory_claim_sources_message
                ON memory_claim_sources(message_id);

            CREATE TABLE IF NOT EXISTS memory_entities (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                canonical_key TEXT NOT NULL,
                canonical_name TEXT NOT NULL,
                entity_type TEXT NOT NULL DEFAULT '',
                aliases_json TEXT NOT NULL DEFAULT '[]',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                UNIQUE(user_id, canonical_key)
            );
            CREATE INDEX IF NOT EXISTS idx_memory_entities_user_name
                ON memory_entities(user_id, canonical_name);

            CREATE TABLE IF NOT EXISTS memory_claim_entities (
                claim_id INTEGER NOT NULL REFERENCES memory_claims(id) ON DELETE CASCADE,
                entity_id INTEGER NOT NULL REFERENCES memory_entities(id) ON DELETE CASCADE,
                role TEXT NOT NULL DEFAULT 'mentioned',
                PRIMARY KEY(claim_id, entity_id, role)
            );

            CREATE TABLE IF NOT EXISTS memory_claim_relations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                source_claim_id INTEGER NOT NULL REFERENCES memory_claims(id) ON DELETE CASCADE,
                target_claim_id INTEGER NOT NULL REFERENCES memory_claims(id) ON DELETE CASCADE,
                relation_type TEXT NOT NULL,
                confidence REAL NOT NULL DEFAULT 1.0,
                created_at REAL NOT NULL,
                relation_key TEXT NOT NULL,
                UNIQUE(user_id, relation_key)
            );
            CREATE INDEX IF NOT EXISTS idx_memory_claim_relations_source
                ON memory_claim_relations(user_id, source_claim_id);
            CREATE INDEX IF NOT EXISTS idx_memory_claim_relations_target
                ON memory_claim_relations(user_id, target_claim_id);

            CREATE TABLE IF NOT EXISTS memory_compiler_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                session_id INTEGER NOT NULL REFERENCES memory_sessions(id) ON DELETE CASCADE,
                source_hash TEXT NOT NULL,
                compiler_fingerprint TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                next_attempt_at REAL,
                warning_count INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                started_at REAL,
                finished_at REAL,
                UNIQUE(user_id, session_id, source_hash, compiler_fingerprint)
            );
            CREATE INDEX IF NOT EXISTS idx_memory_compiler_jobs_user_status
                ON memory_compiler_jobs(user_id, status, created_at);

            CREATE TABLE IF NOT EXISTS memory_usage_ledger (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                job_id INTEGER REFERENCES memory_compiler_jobs(id) ON DELETE SET NULL,
                provider TEXT NOT NULL,
                model TEXT NOT NULL,
                input_tokens INTEGER NOT NULL DEFAULT 0,
                output_tokens INTEGER NOT NULL DEFAULT 0,
                reasoning_tokens INTEGER NOT NULL DEFAULT 0,
                cached_tokens INTEGER NOT NULL DEFAULT 0,
                cost_usd REAL NOT NULL DEFAULT 0.0,
                created_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_memory_usage_user
                ON memory_usage_ledger(user_id, created_at);
            """
        )
        claim_columns = {
            str(row[1])
            for row in self.connection.execute(
                "PRAGMA table_info(memory_claims)"
            ).fetchall()
        }
        if "memory_key" not in claim_columns:
            self.connection.execute(
                "ALTER TABLE memory_claims ADD COLUMN memory_key TEXT NOT NULL DEFAULT ''"
            )
        job_columns = {
            str(row[1])
            for row in self.connection.execute(
                "PRAGMA table_info(memory_compiler_jobs)"
            ).fetchall()
        }
        if "next_attempt_at" not in job_columns:
            self.connection.execute(
                "ALTER TABLE memory_compiler_jobs ADD COLUMN next_attempt_at REAL"
            )
        self.connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_memory_compiler_jobs_retry_due "
            "ON memory_compiler_jobs(user_id, status, next_attempt_at, created_at)"
        )
        self.connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_memory_claims_user_memory_key "
            "ON memory_claims(user_id, memory_key, status)"
        )
        self.connection.execute(
            f"""
            CREATE VIRTUAL TABLE IF NOT EXISTS {self.fts_table} USING fts5(
                text, subject, predicate, object_text, kind, status
            )
            """
        )
        self._repair_fts()
        memory_key_version_key = f"{self.fts_table}_memory_key_version"
        memory_key_version_row = self.connection.execute(
            "SELECT value FROM metadata WHERE key = ?", (memory_key_version_key,)
        ).fetchone()
        memory_key_version = (
            int(memory_key_version_row[0])
            if memory_key_version_row and str(memory_key_version_row[0]).isdigit()
            else None
        )
        if memory_key_version is None or memory_key_version < MEMORY_KEY_FORMAT_VERSION:
            self._migrate_memory_keys()
        elif memory_key_version > MEMORY_KEY_FORMAT_VERSION:
            raise RuntimeError(
                "derived memory key format is newer than this NarratorDB runtime"
            )
        self.connection.execute(
            """
            INSERT INTO metadata(key, value) VALUES(?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (memory_key_version_key, str(MEMORY_KEY_FORMAT_VERSION)),
        )
        self.connection.execute(
            """
            INSERT INTO metadata(key, value) VALUES(?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (f"{self.fts_table}_version", str(CLAIM_FTS_FORMAT_VERSION)),
        )
        self.connection.execute(
            """
            INSERT INTO metadata(key, value) VALUES('derived_schema_version', ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (str(DERIVED_SCHEMA_VERSION),),
        )
        self.connection.commit()

    def _repair_fts(self) -> None:
        indexed = int(
            self.connection.execute(
                f"SELECT COUNT(*) FROM {self.fts_table}"
            ).fetchone()[0]
        )
        expected = int(
            self.connection.execute(
                "SELECT COUNT(*) FROM memory_claims WHERE user_id = ?", (self.user_id,)
            ).fetchone()[0]
        )
        if indexed == expected:
            return
        self.connection.execute(f"DELETE FROM {self.fts_table}")
        rows = self.connection.execute(
            """
            SELECT id, text, subject, predicate, object_text, kind, status
            FROM memory_claims WHERE user_id = ? ORDER BY id
            """,
            (self.user_id,),
        ).fetchall()
        self.connection.executemany(
            f"""
            INSERT INTO {self.fts_table}(rowid, text, subject, predicate, object_text, kind, status)
            VALUES(?,?,?,?,?,?,?)
            """,
            [tuple(row) for row in rows],
        )

    def _migrate_memory_keys(self) -> None:
        """Canonicalize this scope's legacy keys and merge active collisions.

        Key formatting is a per-user derived-data format because each user has
        an independent claim FTS table. Keeping its migration marker scoped to
        that table ensures opening one user in a shared database cannot mark
        another user's claims as migrated prematurely.
        """

        rows = self.connection.execute(
            """
            SELECT id, session_id, memory_key, document_time, status
            FROM memory_claims
            WHERE user_id = ? AND memory_key != ''
            ORDER BY id
            """,
            (self.user_id,),
        ).fetchall()
        active_by_key: dict[str, list[Any]] = {}
        for row in rows:
            normalized_key = _canonical_memory_key(row[2])
            if normalized_key != str(row[2]):
                self.connection.execute(
                    "UPDATE memory_claims SET memory_key = ? WHERE id = ? AND user_id = ?",
                    (normalized_key, int(row[0]), self.user_id),
                )
            if normalized_key and str(row[4]) == "active":
                active_by_key.setdefault(normalized_key, []).append(row)

        now = time.time()
        for active_rows in active_by_key.values():
            if len(active_rows) < 2:
                continue
            ordered = sorted(
                active_rows,
                key=lambda row: (
                    *_claim_precedence(row[3], int(row[1])),
                    int(row[0]),
                ),
            )
            for predecessor, successor in zip(ordered, ordered[1:]):
                self._store_relation(
                    int(successor[0]), int(predecessor[0]), "updates", 1.0, now
                )

    def register_session(
        self,
        external_id: str,
        message_ids: Sequence[int],
        *,
        occurred_at: float | None = None,
        metadata: Mapping[str, Any] | None = None,
        commit: bool = True,
    ) -> tuple[int, str]:
        if not str(external_id).strip():
            raise ValueError("session_id is required")
        normalized_ids = list(dict.fromkeys(int(value) for value in message_ids))
        rows_by_id: dict[int, sqlite3.Row] = {}
        if normalized_ids:
            placeholders = ",".join("?" for _ in normalized_ids)
            selected = self.connection.execute(
                f"""
                SELECT id, speaker, text, timestamp FROM messages
                WHERE user_id = ? AND id IN ({placeholders})
                """,
                [self.user_id, *normalized_ids],
            ).fetchall()
            rows_by_id = {int(row[0]): row for row in selected}
            missing = [
                message_id
                for message_id in normalized_ids
                if message_id not in rows_by_id
            ]
            if missing:
                raise ValueError(
                    f"session sources are missing or outside this scope: {missing[:10]}"
                )
        rows = [rows_by_id[message_id] for message_id in normalized_ids]
        source_payload = {
            "document_time": occurred_at,
            "messages": [
                {
                    "id": int(row[0]),
                    "speaker": str(row[1]),
                    "text": str(row[2]),
                    "timestamp": row[3],
                }
                for row in rows
            ],
        }
        source_hash = hashlib.sha256(_json(source_payload).encode("utf-8")).hexdigest()
        now = time.time()
        self.connection.execute(
            """
            INSERT INTO memory_sessions(
                user_id, external_id, occurred_at, source_hash, metadata_json, created_at, updated_at
            ) VALUES(?,?,?,?,?,?,?)
            ON CONFLICT(user_id, external_id) DO UPDATE SET
                occurred_at = excluded.occurred_at,
                source_hash = excluded.source_hash,
                metadata_json = excluded.metadata_json,
                updated_at = excluded.updated_at
            """,
            (
                self.user_id,
                str(external_id),
                occurred_at,
                source_hash,
                _json(dict(metadata or {})),
                now,
                now,
            ),
        )
        session_row = self.connection.execute(
            "SELECT id FROM memory_sessions WHERE user_id = ? AND external_id = ?",
            (self.user_id, str(external_id)),
        ).fetchone()
        session_pk = int(session_row[0])
        self.connection.execute(
            "DELETE FROM memory_session_messages WHERE session_id = ?", (session_pk,)
        )
        self.connection.executemany(
            """
            INSERT INTO memory_session_messages(session_id, message_id, ordinal)
            VALUES(?,?,?)
            """,
            [(session_pk, int(row[0]), index) for index, row in enumerate(rows)],
        )
        if commit:
            self.connection.commit()
        return session_pk, source_hash

    def enqueue_job(
        self, session_id: int, source_hash: str, compiler_fingerprint: str
    ) -> int:
        now = time.time()
        normalized_source_hash = str(source_hash)
        normalized_fingerprint = str(compiler_fingerprint)
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            current = self.connection.execute(
                """
                SELECT source_hash FROM memory_sessions
                WHERE id = ? AND user_id = ?
                """,
                (int(session_id), self.user_id),
            ).fetchone()
            if current is None:
                raise KeyError(f"unknown session {session_id}")
            if str(current[0]) != normalized_source_hash:
                raise ValueError(
                    "compiler job source hash is not current for its session"
                )

            # Exactly one compiler/source lineage can materialize a session at
            # a time. This includes previously completed jobs: switching A to
            # B must obsolete A so switching back to A can deliberately reopen
            # and rematerialize it instead of reusing stale B-derived claims.
            self.connection.execute(
                """
                UPDATE memory_compiler_jobs
                SET status = 'obsolete', next_attempt_at = NULL,
                    finished_at = ?, updated_at = ?
                WHERE user_id = ? AND session_id = ?
                  AND (source_hash != ? OR compiler_fingerprint != ?)
                  AND status != 'obsolete'
                """,
                (
                    now,
                    now,
                    self.user_id,
                    int(session_id),
                    normalized_source_hash,
                    normalized_fingerprint,
                ),
            )
            self.connection.execute(
                """
                INSERT INTO memory_compiler_jobs(
                    user_id, session_id, source_hash, compiler_fingerprint,
                    status, attempts, created_at, updated_at
                ) VALUES(?,?,?,?, 'pending', 0, ?, ?)
                ON CONFLICT(user_id, session_id, source_hash, compiler_fingerprint)
                DO UPDATE SET
                    status = CASE
                        WHEN memory_compiler_jobs.status = 'obsolete' THEN 'pending'
                        WHEN memory_compiler_jobs.status IN (
                            'complete', 'partial', 'running', 'failed',
                            'blocked', 'exhausted'
                        ) THEN memory_compiler_jobs.status
                        ELSE 'pending'
                    END,
                    attempts = CASE
                        WHEN memory_compiler_jobs.status = 'obsolete' THEN 0
                        ELSE memory_compiler_jobs.attempts
                    END,
                    last_error = CASE
                        WHEN memory_compiler_jobs.status = 'obsolete' THEN NULL
                        WHEN memory_compiler_jobs.status IN (
                            'complete', 'partial', 'running', 'failed',
                            'blocked', 'exhausted'
                        ) THEN memory_compiler_jobs.last_error
                        ELSE NULL
                    END,
                    next_attempt_at = CASE
                        WHEN memory_compiler_jobs.status IN (
                            'complete', 'partial', 'running', 'failed',
                            'blocked', 'exhausted'
                        ) THEN memory_compiler_jobs.next_attempt_at
                        ELSE NULL
                    END,
                    warning_count = CASE
                        WHEN memory_compiler_jobs.status = 'obsolete' THEN 0
                        ELSE memory_compiler_jobs.warning_count
                    END,
                    started_at = CASE
                        WHEN memory_compiler_jobs.status = 'obsolete' THEN NULL
                        ELSE memory_compiler_jobs.started_at
                    END,
                    finished_at = CASE
                        WHEN memory_compiler_jobs.status = 'obsolete' THEN NULL
                        ELSE memory_compiler_jobs.finished_at
                    END,
                    updated_at = CASE
                        WHEN memory_compiler_jobs.status = 'obsolete' THEN excluded.updated_at
                        WHEN memory_compiler_jobs.status IN (
                            'complete', 'partial', 'running', 'failed',
                            'blocked', 'exhausted'
                        ) THEN memory_compiler_jobs.updated_at
                        ELSE excluded.updated_at
                    END
                """,
                (
                    self.user_id,
                    session_id,
                    normalized_source_hash,
                    normalized_fingerprint,
                    now,
                    now,
                ),
            )
            row = self.connection.execute(
                """
                SELECT id FROM memory_compiler_jobs
                WHERE user_id = ? AND session_id = ? AND source_hash = ?
                  AND compiler_fingerprint = ?
                """,
                (
                    self.user_id,
                    session_id,
                    normalized_source_hash,
                    normalized_fingerprint,
                ),
            ).fetchone()
            self.connection.commit()
            return int(row[0])
        except Exception:
            self.connection.rollback()
            raise

    def next_jobs(
        self, *, limit: int = 100, stale_after_seconds: float = 300.0
    ) -> list[dict[str, Any]]:
        stale_before = time.time() - max(1.0, float(stale_after_seconds))
        now = time.time()
        # A worker can disappear after claiming its final permitted attempt.
        # Keep the common idle poll read-only; if a candidate exists, the
        # guarded UPDATE rechecks staleness after acquiring SQLite's writer.
        stale_final = self.connection.execute(
            """
            SELECT 1 FROM memory_compiler_jobs
            WHERE user_id = ? AND status = 'running' AND attempts >= ?
              AND updated_at < ? LIMIT 1
            """,
            (self.user_id, MAX_COMPILER_JOB_ATTEMPTS, stale_before),
        ).fetchone()
        if stale_final is not None:
            self.connection.execute(
                """
                UPDATE memory_compiler_jobs
                SET status = 'exhausted',
                    last_error = 'worker_interrupted_after_final_attempt',
                    next_attempt_at = NULL,
                    finished_at = ?, updated_at = ?
                WHERE user_id = ? AND status = 'running' AND attempts >= ?
                  AND updated_at < ?
                """,
                (
                    now,
                    now,
                    self.user_id,
                    MAX_COMPILER_JOB_ATTEMPTS,
                    stale_before,
                ),
            )
            self.connection.commit()
        rows = self.connection.execute(
            """
            SELECT j.id, j.session_id, j.source_hash, j.compiler_fingerprint,
                   j.status, j.attempts, s.external_id, s.occurred_at,
                   s.metadata_json, j.next_attempt_at
            FROM memory_compiler_jobs j
            JOIN memory_sessions s ON s.id = j.session_id
            WHERE j.user_id = ?
              AND (
                  (
                      j.status IN ('pending', 'failed')
                      AND j.attempts < ?
                      AND COALESCE(j.next_attempt_at, 0) <= ?
                  )
                  OR (
                      j.status = 'running'
                      AND j.attempts < ?
                      AND j.updated_at < ?
                  )
              )
            ORDER BY j.created_at, j.id LIMIT ?
            """,
            (
                self.user_id,
                MAX_COMPILER_JOB_ATTEMPTS,
                now,
                MAX_COMPILER_JOB_ATTEMPTS,
                stale_before,
                max(1, int(limit)),
            ),
        ).fetchall()
        return [
            {
                "id": int(row[0]),
                "session_pk": int(row[1]),
                "source_hash": str(row[2]),
                "compiler_fingerprint": str(row[3]),
                "status": str(row[4]),
                "attempts": int(row[5]),
                "session_id": str(row[6]),
                "occurred_at": row[7],
                "metadata": json.loads(row[8] or "{}"),
                "next_attempt_at": float(row[9]) if row[9] is not None else None,
            }
            for row in rows
        ]

    def load_session(self, session_pk: int) -> dict[str, Any]:
        session = self.connection.execute(
            """
            SELECT external_id, occurred_at, source_hash, metadata_json
            FROM memory_sessions WHERE id = ? AND user_id = ?
            """,
            (int(session_pk), self.user_id),
        ).fetchone()
        if session is None:
            raise KeyError(f"unknown session {session_pk}")
        rows = self.connection.execute(
            """
            SELECT m.id, m.speaker, m.text, m.timestamp, sm.ordinal
            FROM memory_session_messages sm
            JOIN messages m ON m.id = sm.message_id
            WHERE sm.session_id = ? AND m.user_id = ?
            ORDER BY sm.ordinal
            """,
            (int(session_pk), self.user_id),
        ).fetchall()
        return {
            "session_pk": int(session_pk),
            "session_id": str(session[0]),
            "occurred_at": session[1],
            "source_hash": str(session[2]),
            "metadata": json.loads(session[3] or "{}"),
            "messages": [
                {
                    "id": int(row[0]),
                    "role": str(row[1]),
                    "content": str(row[2]),
                    "timestamp": float(row[3]),
                    "ordinal": int(row[4]),
                }
                for row in rows
            ],
        }

    def claim_job(
        self,
        job_id: int,
        *,
        stale_after_seconds: float = 300.0,
    ) -> int | None:
        now = time.time()
        stale_before = now - max(1.0, float(stale_after_seconds))
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            cursor = self.connection.execute(
                """
                UPDATE memory_compiler_jobs
                SET status = 'running', attempts = attempts + 1,
                    next_attempt_at = NULL, finished_at = NULL,
                    started_at = ?, updated_at = ?
                WHERE id = ? AND user_id = ?
                  AND attempts < ?
                  AND (
                      (
                          status IN ('pending', 'failed')
                          AND COALESCE(next_attempt_at, 0) <= ?
                      )
                      OR (status = 'running' AND updated_at < ?)
                  )
                """,
                (
                    now,
                    now,
                    int(job_id),
                    self.user_id,
                    MAX_COMPILER_JOB_ATTEMPTS,
                    now,
                    stale_before,
                ),
            )
            attempt = None
            if cursor.rowcount:
                row = self.connection.execute(
                    """
                    SELECT attempts FROM memory_compiler_jobs
                    WHERE id = ? AND user_id = ?
                    """,
                    (int(job_id), self.user_id),
                ).fetchone()
                attempt = int(row[0]) if row is not None else None
            self.connection.commit()
            return attempt
        except Exception:
            self.connection.rollback()
            raise

    def mark_job_running(
        self,
        job_id: int,
        *,
        stale_after_seconds: float = 300.0,
    ) -> bool:
        """Compatibility wrapper for callers that only need claim success."""

        return (
            self.claim_job(
                job_id,
                stale_after_seconds=stale_after_seconds,
            )
            is not None
        )

    def heartbeat_job(self, job_id: int, expected_attempt: int) -> bool:
        """Renew one running attempt lease without changing its attempt count."""

        cursor = self.connection.execute(
            """
            UPDATE memory_compiler_jobs SET updated_at = ?
            WHERE id = ? AND user_id = ? AND status = 'running' AND attempts = ?
            """,
            (time.time(), int(job_id), self.user_id, int(expected_attempt)),
        )
        self.connection.commit()
        return bool(cursor.rowcount)

    def mark_job_failed(
        self,
        job_id: int,
        error: str,
        *,
        retryable: bool = True,
        expected_attempt: int | None = None,
        retry_after_seconds: float | None = None,
    ) -> str:
        now = time.time()
        attempts = expected_attempt
        if attempts is None:
            row = self.connection.execute(
                """
                SELECT attempts FROM memory_compiler_jobs
                WHERE id = ? AND user_id = ? AND status = 'running'
                """,
                (int(job_id), self.user_id),
            ).fetchone()
            if row is None:
                return "stale"
            attempts = int(row[0])
        status = (
            "failed"
            if retryable and attempts < MAX_COMPILER_JOB_ATTEMPTS
            else "exhausted"
            if retryable
            else "blocked"
        )
        next_attempt_at = None
        if status == "failed":
            try:
                delay = float(retry_after_seconds or 0.0)
            except (TypeError, ValueError, OverflowError):
                delay = 0.0
            if not math.isfinite(delay) or delay < 0:
                delay = 0.0
            next_attempt_at = now + min(
                delay,
                float(MAX_COMPILER_RETRY_DELAY_SECONDS),
            )
        cursor = self.connection.execute(
            """
            UPDATE memory_compiler_jobs
            SET status = ?, last_error = ?, next_attempt_at = ?,
                finished_at = ?, updated_at = ?
            WHERE id = ? AND user_id = ? AND status = 'running' AND attempts = ?
            """,
            (
                status,
                str(error)[:4000],
                next_attempt_at,
                now,
                now,
                int(job_id),
                self.user_id,
                int(attempts),
            ),
        )
        self.connection.commit()
        return status if cursor.rowcount else "stale"

    def _resolve_evidence(
        self, evidence: Any, session_message_ids: set[int]
    ) -> tuple[int, str, int | None, int | None] | None:
        item = _mapping(evidence)
        message_id = item.get("message_id", item.get("source_message_id"))
        try:
            message_id = int(message_id)
        except (TypeError, ValueError):
            return None
        if message_id not in session_message_ids:
            return None
        row = self.connection.execute(
            "SELECT text FROM messages WHERE id = ? AND user_id = ?",
            (message_id, self.user_id),
        ).fetchone()
        if row is None:
            return None
        text = str(row[0])
        raw_quote = item.get("quote", item.get("text"))
        quote = "" if raw_quote is None else str(raw_quote)
        if quote == "":
            quote = text
        raw_start = item.get("start", item.get("span_start"))
        raw_end = item.get("end", item.get("span_end"))
        if (raw_start is None) != (raw_end is None):
            return None
        if raw_start is not None:
            if (
                isinstance(raw_start, bool)
                or isinstance(raw_end, bool)
                or not isinstance(raw_start, int)
                or not isinstance(raw_end, int)
            ):
                return None
            if raw_start < 0 or raw_end < raw_start or raw_end > len(text):
                return None
            if text[raw_start:raw_end] != quote:
                return None
            return message_id, quote, raw_start, raw_end
        start = text.find(quote)
        if start < 0:
            return None
        return message_id, quote, start, start + len(quote)

    def apply_compilation(
        self,
        job_id: int,
        compiled: Any,
        *,
        processor: str,
        processor_version: str,
        prompt_version: str,
        usage: Mapping[str, Any] | None = None,
        expected_attempt: int | None = None,
        compiler_warnings: Sequence[str] = (),
    ) -> dict[str, Any]:
        payload = _normalize_compiled_payload(compiled)
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            return self._apply_compilation_locked(
                job_id,
                payload,
                processor=processor,
                processor_version=processor_version,
                prompt_version=prompt_version,
                usage=usage,
                expected_attempt=expected_attempt,
                compiler_warnings=compiler_warnings,
            )
        except Exception:
            self.connection.rollback()
            raise

    def _apply_compilation_locked(
        self,
        job_id: int,
        payload: Mapping[str, Any],
        *,
        processor: str,
        processor_version: str,
        prompt_version: str,
        usage: Mapping[str, Any] | None,
        expected_attempt: int | None,
        compiler_warnings: Sequence[str],
    ) -> dict[str, Any]:
        job = self.connection.execute(
            """
            SELECT session_id, source_hash, status, attempts
            FROM memory_compiler_jobs WHERE id = ? AND user_id = ?
            """,
            (int(job_id), self.user_id),
        ).fetchone()
        if job is None:
            raise KeyError(f"unknown compiler job {job_id}")
        if expected_attempt is not None and (
            str(job[2]) != "running" or int(job[3]) != int(expected_attempt)
        ):
            if usage:
                self.record_usage(job_id, usage)
            self.connection.commit()
            return {
                "status": "stale",
                "claims_stored": 0,
                "warnings": ["compiler result lost its job lease"],
            }
        session_pk = int(job[0])
        session = self.load_session(session_pk)
        if str(job[2]) == "obsolete" or str(job[1]) != str(session["source_hash"]):
            now = time.time()
            self.connection.execute(
                """
                UPDATE memory_compiler_jobs
                SET status = 'obsolete', next_attempt_at = NULL,
                    finished_at = ?, updated_at = ?
                WHERE id = ? AND user_id = ?
                """,
                (now, now, int(job_id), self.user_id),
            )
            if usage:
                self.record_usage(job_id, usage)
            self.connection.commit()
            return {
                "status": "obsolete",
                "claims_stored": 0,
                "warnings": ["source session changed before compilation completed"],
            }
        session_message_ids = {int(message["id"]) for message in session["messages"]}
        now = time.time()

        old_claim_ids = [
            int(row[0])
            for row in self.connection.execute(
                "SELECT id FROM memory_claims WHERE user_id = ? AND session_id = ?",
                (self.user_id, session_pk),
            ).fetchall()
        ]
        for claim_id in old_claim_ids:
            self.connection.execute(
                f"DELETE FROM {self.fts_table} WHERE rowid = ?", (claim_id,)
            )
        self.connection.execute(
            "DELETE FROM memory_claims WHERE user_id = ? AND session_id = ?",
            (self.user_id, session_pk),
        )

        claims_payload = _list(payload.get("claims"))
        summary = str(
            payload.get("summary") or payload.get("session_summary") or ""
        ).strip()
        if summary:
            summary_evidence = _list(payload.get("summary_evidence"))
            claims_payload.append(
                {
                    "kind": "summary",
                    "text": summary,
                    "confidence": 1.0,
                    "evidence": summary_evidence
                    or [
                        {"message_id": message["id"], "quote": message["content"]}
                        for message in session["messages"][:8]
                    ],
                }
            )

        inserted_ids: list[int] = []
        index_to_id: dict[int, int] = {}
        warnings = [
            str(warning)[:200] for warning in compiler_warnings if str(warning).strip()
        ]
        seen_claim_keys: set[str] = set()
        for index, raw_claim in enumerate(claims_payload):
            claim = _mapping(raw_claim)
            kind = str(claim.get("kind") or claim.get("type") or "fact").strip().lower()
            if kind not in CLAIM_KINDS:
                kind = "fact"
            text = str(claim.get("text") or claim.get("memory") or "").strip()
            if not text:
                warnings.append(f"claim {index}: empty text")
                continue
            status = str(claim.get("status") or "active").strip().lower()
            if status not in CLAIM_STATUSES:
                status = "active"
            subject = str(claim.get("subject") or "").strip()
            predicate = str(claim.get("predicate") or "").strip()
            object_text = str(
                claim.get("object_text") or claim.get("object") or ""
            ).strip()
            memory_key = _canonical_memory_key(claim.get("memory_key"))
            evidence_items = _list(claim.get("evidence") or claim.get("sources"))
            evidence = [
                resolved
                for item in evidence_items
                if (resolved := self._resolve_evidence(item, session_message_ids))
                is not None
            ]
            if not evidence:
                warnings.append(f"claim {index}: no valid source evidence")
                continue
            claim_key_payload = {
                "session": session["session_id"],
                "kind": kind,
                "text": _canonical(text),
                "subject": _canonical(subject),
                "predicate": _canonical(predicate),
                "object": _canonical(object_text),
                "memory_key": memory_key,
                "processor": processor,
                "processor_version": processor_version,
                "prompt_version": prompt_version,
            }
            claim_key = hashlib.sha256(
                _json(claim_key_payload).encode("utf-8")
            ).hexdigest()
            if claim_key in seen_claim_keys:
                warnings.append(f"claim {index}: duplicate derived claim")
                continue
            seen_claim_keys.add(claim_key)
            document_time = _float(claim.get("document_time"))
            if document_time is None:
                document_time = _float(session.get("occurred_at"))
            cursor = self.connection.execute(
                """
                INSERT INTO memory_claims(
                    user_id, session_id, claim_key, kind, text, subject, predicate, object_text,
                    memory_key, status, confidence, document_time, event_start, event_end,
                    valid_from, valid_to, processor, processor_version, prompt_version,
                    created_at, updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    self.user_id,
                    session_pk,
                    claim_key,
                    kind,
                    text,
                    subject,
                    predicate,
                    object_text,
                    memory_key,
                    status,
                    min(1.0, max(0.0, float(claim.get("confidence", 1.0)))),
                    document_time,
                    _float(claim.get("event_start")),
                    _float(claim.get("event_end")),
                    _float(claim.get("valid_from")),
                    _float(claim.get("valid_to")),
                    processor,
                    processor_version,
                    prompt_version,
                    now,
                    now,
                ),
            )
            claim_id = int(cursor.lastrowid)
            inserted_ids.append(claim_id)
            index_to_id[index] = claim_id
            self.connection.execute(
                f"""
                INSERT INTO {self.fts_table}(rowid, text, subject, predicate, object_text, kind, status)
                VALUES(?,?,?,?,?,?,?)
                """,
                (claim_id, text, subject, predicate, object_text, kind, status),
            )
            self.connection.executemany(
                """
                INSERT INTO memory_claim_sources(claim_id, message_id, quote, span_start, span_end)
                VALUES(?,?,?,?,?)
                """,
                [(claim_id, *item) for item in evidence],
            )
            entity_values = _list(claim.get("entities"))
            for entity_value in entity_values:
                entity = (
                    _mapping(entity_value)
                    if not isinstance(entity_value, str)
                    else {"name": entity_value}
                )
                entity_id = self._upsert_entity(entity, now)
                if entity_id is not None:
                    self.connection.execute(
                        """
                        INSERT OR IGNORE INTO memory_claim_entities(claim_id, entity_id, role)
                        VALUES(?,?,?)
                        """,
                        (claim_id, entity_id, str(entity.get("role") or "mentioned")),
                    )

            if memory_key and status == "active":
                # The public compiler normally resolves repeated keys within a
                # session before storage. Canonicalization can reveal an
                # additional match when harmless punctuation drift escaped that
                # pass, so preserve payload order and let the later active claim
                # supersede the earlier one deterministically.
                prior_local_rows = self.connection.execute(
                    """
                    SELECT id FROM memory_claims
                    WHERE user_id = ? AND session_id = ? AND memory_key = ?
                      AND status = 'active' AND id != ?
                    ORDER BY id
                    """,
                    (self.user_id, session_pk, memory_key, claim_id),
                ).fetchall()
                for prior_local in prior_local_rows:
                    self._store_relation(
                        claim_id, int(prior_local[0]), "updates", 1.0, now
                    )

                timeline = self.connection.execute(
                    """
                    SELECT id, session_id, document_time
                    FROM memory_claims
                    WHERE user_id = ? AND memory_key = ?
                      AND status IN ('active', 'superseded')
                      AND id != ? AND session_id != ?
                    """,
                    (self.user_id, memory_key, claim_id, session_pk),
                ).fetchall()
                current_order = _claim_precedence(document_time, session_pk)
                older = [
                    row
                    for row in timeline
                    if _claim_precedence(row[2], int(row[1])) < current_order
                ]
                newer = [
                    row
                    for row in timeline
                    if _claim_precedence(row[2], int(row[1])) > current_order
                ]
                predecessor = max(
                    older,
                    key=lambda row: _claim_precedence(row[2], int(row[1])),
                    default=None,
                )
                successor = min(
                    newer,
                    key=lambda row: _claim_precedence(row[2], int(row[1])),
                    default=None,
                )
                if predecessor is not None and successor is not None:
                    self.connection.execute(
                        """
                        DELETE FROM memory_claim_relations
                        WHERE user_id = ? AND source_claim_id = ? AND target_claim_id = ?
                          AND relation_type = 'updates'
                        """,
                        (self.user_id, int(successor[0]), int(predecessor[0])),
                    )
                if predecessor is not None:
                    self._store_relation(
                        claim_id, int(predecessor[0]), "updates", 1.0, now
                    )
                if successor is not None:
                    self._store_relation(
                        int(successor[0]), claim_id, "updates", 1.0, now
                    )

        for entity_value in _list(payload.get("entities")):
            entity = (
                _mapping(entity_value)
                if not isinstance(entity_value, str)
                else {"name": entity_value}
            )
            self._upsert_entity(entity, now)

        for raw_relation in _list(payload.get("relations")):
            relation = _mapping(raw_relation)
            try:
                source_index = int(relation.get("source_index", relation.get("source")))
                target_index = int(relation.get("target_index", relation.get("target")))
            except (TypeError, ValueError):
                warnings.append("relation: invalid claim indexes")
                continue
            source_id = index_to_id.get(source_index)
            target_id = index_to_id.get(target_index)
            relation_type = str(
                relation.get("type") or relation.get("relation_type") or ""
            ).lower()
            if (
                source_id is None
                or target_id is None
                or relation_type not in CLAIM_RELATIONS
            ):
                warnings.append("relation: unresolved claim or invalid type")
                continue
            self._store_relation(
                source_id,
                target_id,
                relation_type,
                float(relation.get("confidence", 1.0)),
                now,
            )

        status = "complete" if not warnings else "partial"
        partial_reason = None
        if warnings:
            candidate_reason = warnings[0]
            partial_reason = (
                candidate_reason
                if _CONTENT_FREE_REASON_RE.fullmatch(candidate_reason)
                else "compiler_warning"
            )
        self.connection.execute(
            """
            UPDATE memory_compiler_jobs
            SET status = ?, warning_count = ?, last_error = ?,
                next_attempt_at = NULL, finished_at = ?, updated_at = ?
            WHERE id = ? AND user_id = ?
            """,
            (
                status,
                len(warnings),
                partial_reason,
                now,
                now,
                int(job_id),
                self.user_id,
            ),
        )
        if usage:
            self.record_usage(job_id, usage)
        self.connection.commit()
        return {
            "status": status,
            "claims_stored": len(inserted_ids),
            "warnings": warnings,
        }

    def _upsert_entity(self, entity: Mapping[str, Any], now: float) -> int | None:
        name = str(entity.get("canonical_name") or entity.get("name") or "").strip()
        if not name:
            return None
        aliases = sorted(
            {
                str(alias).strip()
                for alias in _list(entity.get("aliases"))
                if str(alias).strip() and _canonical(alias) != _canonical(name)
            }
        )
        key = _canonical(name)
        existing = self.connection.execute(
            """
            SELECT id, aliases_json FROM memory_entities WHERE user_id = ? AND canonical_key = ?
            """,
            (self.user_id, key),
        ).fetchone()
        if existing:
            merged = sorted(set(json.loads(existing[1] or "[]")) | set(aliases))
            self.connection.execute(
                """
                UPDATE memory_entities
                SET canonical_name = ?, entity_type = ?, aliases_json = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    name,
                    str(entity.get("entity_type") or entity.get("type") or ""),
                    _json(merged),
                    now,
                    int(existing[0]),
                ),
            )
            return int(existing[0])
        cursor = self.connection.execute(
            """
            INSERT INTO memory_entities(
                user_id, canonical_key, canonical_name, entity_type,
                aliases_json, created_at, updated_at
            ) VALUES(?,?,?,?,?,?,?)
            """,
            (
                self.user_id,
                key,
                name,
                str(entity.get("entity_type") or entity.get("type") or ""),
                _json(aliases),
                now,
                now,
            ),
        )
        return int(cursor.lastrowid)

    def _store_relation(
        self,
        source_id: int,
        target_id: int,
        relation_type: str,
        confidence: float,
        now: float,
    ) -> None:
        relation_key = hashlib.sha256(
            f"{source_id}:{target_id}:{relation_type}".encode("utf-8")
        ).hexdigest()
        self.connection.execute(
            """
            INSERT OR IGNORE INTO memory_claim_relations(
                user_id, source_claim_id, target_claim_id, relation_type,
                confidence, created_at, relation_key
            ) VALUES(?,?,?,?,?,?,?)
            """,
            (
                self.user_id,
                source_id,
                target_id,
                relation_type,
                min(1.0, max(0.0, confidence)),
                now,
                relation_key,
            ),
        )
        if relation_type == "updates":
            source_time = self.connection.execute(
                "SELECT document_time FROM memory_claims WHERE id = ?", (source_id,)
            ).fetchone()
            self.connection.execute(
                """
                UPDATE memory_claims SET status = 'superseded', valid_to = ?, updated_at = ?
                WHERE id = ? AND user_id = ?
                """,
                (source_time[0] if source_time else now, now, target_id, self.user_id),
            )
            self.connection.execute(
                f"UPDATE {self.fts_table} SET status = 'superseded' WHERE rowid = ?",
                (target_id,),
            )

    def record_usage(self, job_id: int | None, usage: Mapping[str, Any]) -> None:
        provider = str(usage.get("provider") or "unknown")
        model = str(usage.get("model") or "unknown")
        self.connection.execute(
            """
            INSERT INTO memory_usage_ledger(
                user_id, job_id, provider, model, input_tokens, output_tokens,
                reasoning_tokens, cached_tokens, cost_usd, created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            (
                self.user_id,
                job_id,
                provider,
                model,
                int(usage.get("input_tokens") or 0),
                int(usage.get("output_tokens") or 0),
                int(usage.get("reasoning_tokens") or 0),
                int(usage.get("cached_tokens") or 0),
                float(usage.get("cost_usd") or 0.0),
                time.time(),
            ),
        )

    def commit(self) -> None:
        """Commit a standalone derived-store mutation such as usage accounting."""

        self.connection.commit()

    def _fts_query(self, query: str) -> str:
        terms = []
        seen = set()
        for match in _WORD_RE.findall(query.casefold()):
            if len(match) < 2 or match in seen:
                continue
            seen.add(match)
            escaped = match.replace('"', '""')
            terms.append(f'"{escaped}"')
        return " OR ".join(terms[:24])

    def _claim_ids_for_source_messages(self, message_ids: Sequence[int]) -> set[int]:
        """Resolve a strict source scope without exceeding SQLite bind limits."""

        allowed_sources = set(int(value) for value in message_ids)
        result: set[int] = set()
        for chunk in _bounded_chunks(sorted(allowed_sources)):
            placeholders = ",".join("?" for _ in chunk)
            rows = self.connection.execute(
                f"""
                SELECT DISTINCT cs.claim_id
                FROM memory_claim_sources cs
                JOIN memory_claims c ON c.id = cs.claim_id
                WHERE c.user_id = ? AND cs.message_id IN ({placeholders})
                """,
                [self.user_id, *chunk],
            ).fetchall()
            result.update(int(row[0]) for row in rows)
        return self._claim_ids_wholly_within_scope(result, allowed_sources)

    def _claim_ids_wholly_within_scope(
        self,
        claim_ids: Iterable[int],
        allowed_message_ids: set[int],
    ) -> set[int]:
        """Reject a multi-source claim if any evidence is outside the filter."""

        normalized_claim_ids = sorted(set(int(value) for value in claim_ids))
        sources_by_claim: dict[int, set[int]] = {}
        for chunk in _bounded_chunks(normalized_claim_ids):
            placeholders = ",".join("?" for _ in chunk)
            rows = self.connection.execute(
                f"""
                SELECT cs.claim_id, cs.message_id
                FROM memory_claim_sources cs
                JOIN memory_claims c ON c.id = cs.claim_id
                WHERE c.user_id = ? AND cs.claim_id IN ({placeholders})
                """,
                [self.user_id, *chunk],
            ).fetchall()
            for row in rows:
                sources_by_claim.setdefault(int(row[0]), set()).add(int(row[1]))
        return {
            claim_id
            for claim_id, source_ids in sources_by_claim.items()
            if source_ids and source_ids <= allowed_message_ids
        }

    def _claim_ids_wholly_user_sourced(
        self,
        claim_ids: Iterable[int],
        *,
        allowed_message_ids: set[int] | None = None,
    ) -> set[int]:
        """Return claims whose complete evidence set is in-scope user text."""

        normalized_claim_ids = sorted(set(int(value) for value in claim_ids))
        speakers_by_claim: dict[int, list[tuple[int, str]]] = {}
        for chunk in _bounded_chunks(normalized_claim_ids):
            placeholders = ",".join("?" for _ in chunk)
            rows = self.connection.execute(
                f"""
                SELECT cs.claim_id, cs.message_id, m.speaker
                FROM memory_claim_sources cs
                JOIN memory_claims c ON c.id = cs.claim_id
                JOIN messages m ON m.id = cs.message_id
                WHERE c.user_id = ? AND cs.claim_id IN ({placeholders})
                ORDER BY cs.claim_id, cs.message_id
                """,
                [self.user_id, *chunk],
            ).fetchall()
            for row in rows:
                speakers_by_claim.setdefault(int(row[0]), []).append(
                    (int(row[1]), str(row[2]).casefold())
                )
        return {
            claim_id
            for claim_id, sources in speakers_by_claim.items()
            if sources
            and all(speaker == "user" for _, speaker in sources)
            and (
                allowed_message_ids is None
                or all(
                    message_id in allowed_message_ids
                    for message_id, _ in sources
                )
            )
        }

    def search_claims(
        self,
        query: str,
        *,
        limit: int = 40,
        allowed_message_ids: set[int] | None = None,
    ) -> list[MemoryClaim]:
        fts_query = self._fts_query(query)
        if not fts_query:
            return []
        requested_limit = max(1, int(limit))
        asks_current = bool(_CURRENT_RE.search(query))
        include_history = bool(_HISTORY_RE.search(query)) or (
            bool(_PAST_TENSE_RE.search(query)) and not asks_current
        )
        status_sql = "" if include_history else " AND c.status = 'active'"
        allowed_claim_ids: set[int] | None = None
        if allowed_message_ids is not None:
            allowed_claim_ids = self._claim_ids_for_source_messages(
                sorted(set(int(value) for value in allowed_message_ids))
            )
            if not allowed_claim_ids:
                return []
        try:
            if allowed_claim_ids is None:
                rows = self.connection.execute(
                    f"""
                    SELECT c.*, bm25({self.fts_table}) AS fts_score
                    FROM {self.fts_table}
                    JOIN memory_claims c ON c.id = {self.fts_table}.rowid
                    WHERE {self.fts_table} MATCH ? AND c.user_id = ? {status_sql}
                    ORDER BY bm25({self.fts_table}),
                             COALESCE(c.document_time, c.created_at) DESC,
                             c.id
                    LIMIT ?
                    """,
                    (fts_query, self.user_id, requested_limit),
                ).fetchall()
            else:
                rows = []
                for chunk in _bounded_chunks(sorted(allowed_claim_ids)):
                    placeholders = ",".join("?" for _ in chunk)
                    rows.extend(
                        self.connection.execute(
                            f"""
                            SELECT c.*, bm25({self.fts_table}) AS fts_score
                            FROM {self.fts_table}
                            JOIN memory_claims c ON c.id = {self.fts_table}.rowid
                            WHERE {self.fts_table} MATCH ? AND c.user_id = ?
                              AND c.id IN ({placeholders}) {status_sql}
                            ORDER BY bm25({self.fts_table}),
                                     COALESCE(c.document_time, c.created_at) DESC,
                                     c.id
                            LIMIT ?
                            """,
                            [
                                fts_query,
                                self.user_id,
                                *chunk,
                                requested_limit,
                            ],
                        ).fetchall()
                    )
                rows.sort(
                    key=lambda row: (
                        float(row["fts_score"] or 0.0),
                        -float(row["document_time"] or row["created_at"] or 0.0),
                        int(row["id"]),
                    )
                )
                rows = rows[:requested_limit]
        except sqlite3.OperationalError:
            return []
        if not rows:
            return []
        sources = self._claim_sources(
            [int(row["id"]) for row in rows],
            allowed_message_ids=allowed_message_ids,
        )
        claims = []
        for rank, row in enumerate(rows):
            fts_score = abs(float(row["fts_score"] or 0.0))
            score = 1.0 / (rank + 1) + min(0.4, fts_score / 20.0)
            if row["status"] == "active":
                score += 0.12
            claims.append(
                MemoryClaim(
                    id=int(row["id"]),
                    kind=str(row["kind"]),
                    text=str(row["text"]),
                    status=str(row["status"]),
                    confidence=float(row["confidence"]),
                    subject=str(row["subject"] or ""),
                    predicate=str(row["predicate"] or ""),
                    object_text=str(row["object_text"] or ""),
                    memory_key=str(row["memory_key"] or ""),
                    document_time=row["document_time"],
                    event_start=row["event_start"],
                    event_end=row["event_end"],
                    valid_from=row["valid_from"],
                    valid_to=row["valid_to"],
                    processor=f"{row['processor']}:{row['processor_version']}",
                    sources=tuple(sources.get(int(row["id"]), [])),
                    score=score,
                    channels=("claim_fts",),
                )
            )
        return self._expand_relations(
            claims,
            limit=requested_limit,
            include_history=include_history,
            allowed_message_ids=allowed_message_ids,
        )

    def _exhaustive_user_grounded_claims(
        self,
        *,
        allowed_message_ids: set[int] | None = None,
    ) -> list[MemoryClaim]:
        """Load the complete structured user-source ledger without a top-k cap."""

        rows = self.connection.execute(
            """
            SELECT DISTINCT c.*
            FROM memory_claims c
            JOIN memory_claim_sources cs ON cs.claim_id = c.id
            JOIN messages m ON m.id = cs.message_id
            WHERE c.user_id = ?
              AND c.kind IN ('event', 'fact')
              AND c.status IN ('active', 'superseded')
              AND m.user_id = ?
              AND LOWER(m.speaker) = 'user'
            ORDER BY c.id
            """,
            (self.user_id, self.user_id),
        ).fetchall()
        if not rows:
            return []
        claim_ids = [int(row["id"]) for row in rows]
        permitted_claim_ids = self._claim_ids_wholly_user_sourced(
            claim_ids,
            allowed_message_ids=allowed_message_ids,
        )
        rows = [row for row in rows if int(row["id"]) in permitted_claim_ids]
        if not rows:
            return []
        source_map: dict[int, list[ClaimSource]] = {}
        claim_ids = [int(row["id"]) for row in rows]
        for chunk in _bounded_chunks(claim_ids):
            source_map.update(self._claim_sources(chunk))
        claims: list[MemoryClaim] = []
        for row in rows:
            claim_id = int(row["id"])
            sources = tuple(source_map.get(claim_id, ()))
            # A scalar may not inherit assistant-authored evidence or a
            # partially filtered multi-source claim.
            if not sources or any(
                source.speaker.casefold() != "user" for source in sources
            ):
                continue
            if allowed_message_ids is not None and any(
                source.message_id not in allowed_message_ids for source in sources
            ):
                continue
            claims.append(
                MemoryClaim(
                    id=claim_id,
                    kind=str(row["kind"]),
                    text=str(row["text"]),
                    status=str(row["status"]),
                    confidence=float(row["confidence"]),
                    subject=str(row["subject"] or ""),
                    predicate=str(row["predicate"] or ""),
                    object_text=str(row["object_text"] or ""),
                    memory_key=str(row["memory_key"] or ""),
                    document_time=row["document_time"],
                    event_start=row["event_start"],
                    event_end=row["event_end"],
                    valid_from=row["valid_from"],
                    valid_to=row["valid_to"],
                    processor=f"{row['processor']}:{row['processor_version']}",
                    sources=sources,
                    score=0.0,
                    channels=("exhaustive_user_ledger",),
                )
            )
        return claims

    def _exhaustive_user_messages(
        self,
        *,
        allowed_message_ids: set[int] | None = None,
    ) -> list[_SessionMessage]:
        """Load every in-scope user message for prefix-stable scalar checks."""

        rows = self.connection.execute(
            """
            SELECT m.id, m.speaker, m.text, m.timestamp, m.position,
                   COALESCE(MIN(s.external_id), 'unscoped') AS session_id
            FROM messages m
            LEFT JOIN memory_session_messages sm ON sm.message_id = m.id
            LEFT JOIN memory_sessions s
              ON s.id = sm.session_id AND s.user_id = m.user_id
            WHERE m.user_id = ? AND LOWER(m.speaker) = 'user'
            GROUP BY m.id, m.speaker, m.text, m.timestamp, m.position
            ORDER BY m.id
            """,
            (self.user_id,),
        ).fetchall()
        result: list[_SessionMessage] = []
        for row in rows:
            message_id = int(row[0])
            if (
                allowed_message_ids is not None
                and message_id not in allowed_message_ids
            ):
                continue
            session_id = str(row[5])
            result.append(
                _SessionMessage(
                    id=message_id,
                    speaker=str(row[1]),
                    text=str(row[2]),
                    timestamp=float(row[3]),
                    position=int(row[4]),
                    provenance={
                        "run_id": session_id,
                        "metadata": {"session_id": session_id},
                    },
                )
            )
        return result

    def _exhaustive_aggregation_source_ids(
        self,
        *,
        allowed_message_ids: set[int] | None = None,
    ) -> list[int]:
        rows = self.connection.execute(
            """
            SELECT DISTINCT cs.message_id
            FROM memory_claim_sources cs
            JOIN memory_claims c ON c.id = cs.claim_id
            JOIN messages m ON m.id = cs.message_id
            WHERE c.user_id = ? AND m.user_id = ?
              AND LOWER(m.speaker) = 'user'
              AND c.kind IN ('event', 'fact')
              AND c.status IN ('active', 'superseded')
            ORDER BY cs.message_id
            """,
            (self.user_id, self.user_id),
        ).fetchall()
        return [
            int(row[0])
            for row in rows
            if allowed_message_ids is None or int(row[0]) in allowed_message_ids
        ]

    def _session_sibling_candidates(
        self,
        claims: Sequence[MemoryClaim],
        query: str,
        *,
        limit: int,
    ) -> list[tuple[_SessionMessage, int, int]]:
        """Load query-anchored response neighborhoods for top claim sessions."""

        session_claim_rank: dict[str, int] = {}
        source_ids_by_session: dict[str, set[int]] = {}
        for claim_rank, claim in enumerate(claims):
            for source in claim.sources:
                session_claim_rank.setdefault(source.session_id, claim_rank)
                source_ids_by_session.setdefault(source.session_id, set()).add(
                    source.message_id
                )
                if len(session_claim_rank) >= _CLAIM_SESSION_LIMIT:
                    break
            if len(session_claim_rank) >= _CLAIM_SESSION_LIMIT:
                break
        if not session_claim_rank:
            return []

        session_ids = list(session_claim_rank)
        placeholders = ",".join("?" for _ in session_ids)
        rows = self.connection.execute(
            f"""
            SELECT s.external_id, m.id, m.speaker, m.text, m.timestamp,
                   m.position, sm.ordinal
            FROM memory_sessions s
            JOIN memory_session_messages sm ON sm.session_id = s.id
            JOIN messages m ON m.id = sm.message_id
            WHERE s.user_id = ? AND s.external_id IN ({placeholders})
            ORDER BY s.id, sm.ordinal, m.id
            """,
            [self.user_id, *session_ids],
        ).fetchall()
        grouped: dict[str, list[Any]] = {}
        for row in rows:
            grouped.setdefault(str(row[0]), []).append(row)

        query_terms = {
            term.casefold() for term in _WORD_RE.findall(query) if len(term) >= 2
        }
        ordinal = _ORDINAL_QUERY_RE.search(query)
        ordinal_marker = (
            re.compile(rf"(?m)^[ \t]*{re.escape(ordinal.group(1))}[.)][ \t]+")
            if ordinal
            else None
        )
        global_cap = min(
            _SESSION_SIBLING_GLOBAL_CAP,
            max(1, int(limit) // 3),
        )
        candidates: list[tuple[_SessionMessage, int, int]] = []
        for session_id in session_ids:
            members = sorted(grouped.get(session_id, []), key=lambda row: int(row[6]))
            if not members:
                continue

            def row_score(row: Any) -> float:
                text = str(row[3])
                terms = {term.casefold() for term in _WORD_RE.findall(text)}
                score = float(len(query_terms & terms))
                if query.casefold() in text.casefold():
                    score += 2.0
                if ordinal_marker is not None and ordinal_marker.search(text):
                    score += 3.0
                if _URL_INTENT_RE.search(query) and _URL_RE.search(text):
                    score += 1.5
                return score

            best_index = max(
                range(len(members)),
                key=lambda index: (row_score(members[index]), -index),
            )
            source_indices = [
                index
                for index, row in enumerate(members)
                if int(row[1]) in source_ids_by_session.get(session_id, set())
            ]
            anchors = [best_index, *source_indices]
            selected_indices: list[int] = []

            def select(index: int) -> None:
                if (
                    0 <= index < len(members)
                    and index not in selected_indices
                    and len(selected_indices) < _SESSION_SIBLINGS_PER_SESSION
                ):
                    selected_indices.append(index)

            for anchor_index in anchors:
                # Conversation evidence most often lives in the assistant turn
                # immediately following the matched user request or cited row.
                if str(members[anchor_index][2]).casefold() == "assistant":
                    select(anchor_index)
                else:
                    next_assistant = next(
                        (
                            index
                            for index in range(anchor_index + 1, len(members))
                            if str(members[index][2]).casefold() == "assistant"
                        ),
                        None,
                    )
                    if next_assistant is not None:
                        select(next_assistant)
                    select(anchor_index)

            fallback_indices = sorted(
                range(len(members)),
                key=lambda index: (
                    0 if str(members[index][2]).casefold() == "assistant" else 1,
                    -row_score(members[index]),
                    index,
                ),
            )
            for index in fallback_indices:
                select(index)

            for local_rank, index in enumerate(selected_indices):
                row = members[index]
                candidates.append(
                    (
                        _SessionMessage(
                            id=int(row[1]),
                            speaker=str(row[2]),
                            text=str(row[3]),
                            timestamp=float(row[4]),
                            position=int(row[5]),
                            provenance={
                                "run_id": session_id,
                                "metadata": {"session_id": session_id},
                            },
                        ),
                        session_claim_rank[session_id],
                        local_rank,
                    )
                )
                if len(candidates) >= global_cap:
                    return candidates
        return candidates

    def _claim_sources(
        self,
        claim_ids: Sequence[int],
        *,
        allowed_message_ids: set[int] | None = None,
    ) -> dict[int, list[ClaimSource]]:
        if not claim_ids:
            return {}
        placeholders = ",".join("?" for _ in claim_ids)
        rows = self.connection.execute(
            f"""
            SELECT cs.claim_id, cs.message_id, s.external_id, m.speaker,
                   cs.quote, cs.span_start, cs.span_end
            FROM memory_claim_sources cs
            JOIN messages m ON m.id = cs.message_id
            JOIN memory_session_messages sm
              ON sm.message_id = cs.message_id
            JOIN memory_sessions s ON s.id = sm.session_id
            WHERE cs.claim_id IN ({placeholders}) AND m.user_id = ?
            ORDER BY cs.claim_id, s.occurred_at, s.id, sm.ordinal, cs.message_id
            """,
            [*claim_ids, self.user_id],
        ).fetchall()
        result: dict[int, list[ClaimSource]] = {}
        for row in rows:
            if (
                allowed_message_ids is not None
                and int(row[1]) not in allowed_message_ids
            ):
                continue
            result.setdefault(int(row[0]), []).append(
                ClaimSource(
                    message_id=int(row[1]),
                    session_id=str(row[2]),
                    speaker=str(row[3]),
                    quote=str(row[4]),
                    start=row[5],
                    end=row[6],
                )
            )
        return result

    def _expand_relations(
        self,
        claims: list[MemoryClaim],
        *,
        limit: int,
        include_history: bool,
        allowed_message_ids: set[int] | None = None,
    ) -> list[MemoryClaim]:
        if not claims:
            return []
        # Relation expansion follows claim-to-claim links, not source scope.
        # Under an explicit raw filter, omitting it is safer than allowing an
        # otherwise related claim whose evidence is outside the caller's scope.
        if allowed_message_ids is not None:
            return claims[:limit]
        ids = [claim.id for claim in claims[: min(len(claims), 20)]]
        placeholders = ",".join("?" for _ in ids)
        related_rows = self.connection.execute(
            f"""
            SELECT DISTINCT CASE
                WHEN source_claim_id IN ({placeholders}) THEN target_claim_id
                ELSE source_claim_id END AS related_id
            FROM memory_claim_relations
            WHERE user_id = ? AND (
                source_claim_id IN ({placeholders}) OR target_claim_id IN ({placeholders})
            )
            ORDER BY related_id
            LIMIT ?
            """,
            [*ids, self.user_id, *ids, *ids, max(0, int(limit) - len(claims))],
        ).fetchall()
        existing = {claim.id for claim in claims}
        related_ids = [
            int(row[0]) for row in related_rows if int(row[0]) not in existing
        ]
        if not related_ids:
            return claims[:limit]
        rel_placeholders = ",".join("?" for _ in related_ids)
        history_sql = "" if include_history else " AND status = 'active'"
        rows = self.connection.execute(
            f"""
            SELECT * FROM memory_claims
            WHERE user_id = ? AND id IN ({rel_placeholders}) {history_sql}
            ORDER BY id
            """,
            [self.user_id, *related_ids],
        ).fetchall()
        sources = self._claim_sources(related_ids)
        for row in rows:
            claims.append(
                MemoryClaim(
                    id=int(row["id"]),
                    kind=str(row["kind"]),
                    text=str(row["text"]),
                    status=str(row["status"]),
                    confidence=float(row["confidence"]),
                    subject=str(row["subject"] or ""),
                    predicate=str(row["predicate"] or ""),
                    object_text=str(row["object_text"] or ""),
                    memory_key=str(row["memory_key"] or ""),
                    document_time=row["document_time"],
                    event_start=row["event_start"],
                    event_end=row["event_end"],
                    valid_from=row["valid_from"],
                    valid_to=row["valid_to"],
                    processor=f"{row['processor']}:{row['processor_version']}",
                    sources=tuple(sources.get(int(row["id"]), [])),
                    score=0.2,
                    channels=("claim_relation",),
                )
            )
        return claims[:limit]

    def _aggregation_session_event_sources(
        self,
        anchor_message_ids: Sequence[int],
        *,
        allowed_message_ids: set[int] | None,
    ) -> list[int]:
        """Append user event sources from sessions reached by focused recall."""

        anchors = list(dict.fromkeys(int(value) for value in anchor_message_ids))
        if not anchors:
            return []
        anchor_rank = {message_id: rank for rank, message_id in enumerate(anchors)}
        session_rank: dict[int, int] = {}
        for chunk in _bounded_chunks(anchors):
            placeholders = ",".join("?" for _ in chunk)
            rows = self.connection.execute(
                f"""
                SELECT sm.session_id, sm.message_id
                FROM memory_session_messages sm
                JOIN memory_sessions s ON s.id = sm.session_id
                WHERE s.user_id = ? AND sm.message_id IN ({placeholders})
                """,
                [self.user_id, *chunk],
            ).fetchall()
            for row in rows:
                session_id = int(row[0])
                message_id = int(row[1])
                session_rank[session_id] = min(
                    session_rank.get(session_id, len(anchors)),
                    anchor_rank[message_id],
                )
        if not session_rank:
            return anchors

        event_rows: list[Any] = []
        for chunk in _bounded_chunks(sorted(session_rank)):
            placeholders = ",".join("?" for _ in chunk)
            event_rows.extend(
                self.connection.execute(
                    f"""
                    SELECT c.session_id, cs.message_id, c.id
                    FROM memory_claims c
                    JOIN memory_claim_sources cs ON cs.claim_id = c.id
                    JOIN messages m ON m.id = cs.message_id
                    WHERE c.user_id = ?
                      AND c.session_id IN ({placeholders})
                      AND c.kind = 'event'
                      AND c.status IN ('active', 'superseded')
                      AND m.user_id = ?
                      AND LOWER(m.speaker) = 'user'
                    """,
                    [self.user_id, *chunk, self.user_id],
                ).fetchall()
            )
        event_rows.sort(
            key=lambda row: (
                session_rank[int(row[0])],
                int(row[2]),
                int(row[1]),
            )
        )
        expanded = list(anchors)
        seen = set(expanded)
        for row in event_rows:
            message_id = int(row[1])
            if message_id in seen or (
                allowed_message_ids is not None
                and message_id not in allowed_message_ids
            ):
                continue
            seen.add(message_id)
            expanded.append(message_id)
            if len(expanded) >= _AGGREGATION_CANDIDATE_SCAN_LIMIT:
                break
        return expanded

    def _aggregation_sources(
        self,
        query: str,
        source_message_ids: Sequence[int],
        *,
        allowed_message_ids: set[int] | None = None,
        fallback_source_message_ids: set[int] | None = None,
        include_uncompleted: bool = False,
        source_limit: int | None = _AGGREGATION_SOURCE_LIMIT,
    ) -> list[_AggregationSourceEvidence]:
        """Load query-relevant event/fact evidence from a source pool.

        ``source_limit=None`` is reserved for the exhaustive scalar ledger. It
        disables retrieval/session expansion and every source-count cap.
        """

        source_ids = list(dict.fromkeys(int(value) for value in source_message_ids))
        if allowed_message_ids is not None:
            source_ids = [
                message_id
                for message_id in source_ids
                if message_id in allowed_message_ids
            ]
        if not source_ids:
            return []
        action_only_focus = _aggregation_query_uses_action_only_focus(query)
        query_action_tokens = (
            frozenset()
            if include_uncompleted
            else _aggregation_query_action_tokens(query)
        )
        exhaustive = source_limit is None
        if query_action_tokens and not exhaustive:
            source_ids = self._aggregation_session_event_sources(
                source_ids,
                allowed_message_ids=allowed_message_ids,
            )
        if not exhaustive:
            source_ids = source_ids[:_AGGREGATION_CANDIDATE_SCAN_LIMIT]
        source_rank = {message_id: rank for rank, message_id in enumerate(source_ids)}
        rows: list[Any] = []
        for chunk in _bounded_chunks(source_ids):
            placeholders = ",".join("?" for _ in chunk)
            rows.extend(
                self.connection.execute(
                    f"""
                    SELECT cs.message_id, s.external_id, cs.quote,
                           c.id, c.kind, c.text, c.status, c.subject,
                           c.predicate, c.object_text, c.memory_key,
                           c.document_time, c.event_start, c.event_end
                    FROM memory_claim_sources cs
                    JOIN memory_claims c ON c.id = cs.claim_id
                    JOIN memory_sessions s ON s.id = c.session_id
                    JOIN messages m ON m.id = cs.message_id
                    WHERE c.user_id = ? AND m.user_id = ?
                      AND LOWER(m.speaker) = 'user'
                      AND c.kind IN ('event', 'fact')
                      AND c.status IN ('active', 'superseded')
                      AND cs.message_id IN ({placeholders})
                    ORDER BY c.id, cs.message_id
                    """,
                    [self.user_id, self.user_id, *chunk],
                ).fetchall()
            )

        if allowed_message_ids is not None and rows:
            permitted_claim_ids = self._claim_ids_wholly_within_scope(
                (int(row[3]) for row in rows),
                allowed_message_ids,
            )
            rows = [row for row in rows if int(row[3]) in permitted_claim_ids]
        if exhaustive and rows:
            permitted_claim_ids = self._claim_ids_wholly_user_sourced(
                (int(row[3]) for row in rows),
                allowed_message_ids=allowed_message_ids,
            )
            rows = [row for row in rows if int(row[3]) in permitted_claim_ids]

        grouped: dict[tuple[int, str], dict[int, _AggregationClaimEvidence]] = {}
        matching: dict[tuple[int, str], dict[int, _AggregationClaimEvidence]] = {}
        query_tokens = (
            _aggregation_query_focus_tokens(query)
            if query_action_tokens or action_only_focus
            else _aggregation_field_tokens(query, remove_query_noise=True)
        )
        query_domain_tokens = (
            _aggregation_query_domain_tokens(query, query_action_tokens)
            if query_action_tokens
            else frozenset()
        )
        for row in rows:
            message_id = int(row[0])
            claim_text = str(row[5])
            claim_fields = (
                claim_text,
                str(row[8] or ""),
                str(row[9] or ""),
                str(row[2]),
            )
            if _looks_like_uncompleted_plan(*claim_fields):
                if include_uncompleted:
                    if not _looks_like_open_obligation(*claim_fields):
                        continue
                elif not query_action_tokens:
                    continue
            claim_id = int(row[3])
            evidence = _AggregationClaimEvidence(
                claim_id=claim_id,
                kind=str(row[4]),
                text=claim_text,
                status=str(row[6]),
                subject=str(row[7] or ""),
                predicate=str(row[8] or ""),
                object_text=str(row[9] or ""),
                memory_key=str(row[10] or ""),
                document_time=row[11],
                event_start=row[12],
                event_end=row[13],
                quote=str(row[2] or ""),
            )
            if (
                not include_uncompleted
                and query_action_tokens
                and (
                    _aggregation_action_is_uncompleted_plan(
                        evidence,
                        query_action_tokens,
                    )
                    or _aggregation_action_is_noncompleted(
                        evidence,
                        query_action_tokens,
                    )
                )
            ):
                continue
            key = (message_id, str(row[1]))
            claims = grouped.setdefault(key, {})
            claims.setdefault(claim_id, evidence)
            matches_query = (
                _pending_claim_matches_query(query, evidence)
                if include_uncompleted
                else _aggregation_claim_matches_query(
                    query_tokens,
                    evidence,
                    query_action_tokens=query_action_tokens,
                    query_domain_tokens=query_domain_tokens,
                )
            )
            if matches_query:
                matching.setdefault(key, {}).setdefault(claim_id, evidence)

        # Exact lexical/structured evidence wins globally. Only when no such
        # evidence exists do we retain the prior semantic/raw source behavior;
        # that fallback protects vocabulary-gap recall without allowing an
        # unrelated co-located fact to crowd a grounded source out of the
        # fixed character budget.
        if matching:
            if exhaustive:
                # The scalar ledger retains every event/fact claim from each
                # matching user source. This lets quantity companions and
                # conflicting same-source values veto a total even when they
                # would not fit the bounded display pack.
                selected_groups = {
                    key: dict(grouped[key]) for key in matching
                }
            else:
                selected_groups = {}
                for key, matched_claims in matching.items():
                    source_claims = grouped[key]
                    expanded = dict(matched_claims)
                    for claim_id, candidate in source_claims.items():
                        if claim_id in expanded:
                            continue
                        if any(
                            _aggregation_claims_are_quantity_companions(
                                candidate, anchor
                            )
                            for anchor in matched_claims.values()
                        ):
                            expanded[claim_id] = candidate
                    selected_groups[key] = expanded
        else:
            fallback_ids = {
                int(value) for value in (fallback_source_message_ids or set())
            }
            selected_groups = {
                key: claims for key, claims in grouped.items() if key[0] in fallback_ids
            }

        candidates = [
            _AggregationSourceEvidence(
                message_id=message_id,
                session_id=session_id,
                source_rank=source_rank[message_id],
                selection_pass=1,
                eligible_claim_count=len(grouped[(message_id, session_id)]),
                claims=tuple(
                    sorted(
                        claims.values(),
                        key=lambda claim: (
                            claim.event_start is None,
                            claim.event_start or 0.0,
                            claim.claim_id,
                        ),
                    )[:_AGGREGATION_CLAIMS_PER_SOURCE]
                ),
            )
            for (message_id, session_id), claims in selected_groups.items()
            if claims
        ]
        candidates.sort(
            key=lambda source: (
                source.source_rank,
                source.session_id,
                source.message_id,
            )
        )

        if exhaustive:
            return [
                dataclasses.replace(source, selection_pass=0)
                for source in candidates
            ]

        if action_only_focus and "bake" in query_action_tokens:
            # A generic bake count can compile as ``baked``, ``made``, or
            # ``reported ... making``. Ensure each dated baked-good class gets
            # one first-pass slot before a retelling consumes the fixed pack
            # budget. For an explicitly bounded time window, an undated new
            # class is too ambiguous to introduce, but it may still accompany
            # a dated representative as a possible retelling.
            bounded_window = _aggregation_query_has_temporal_window(query)
            representatives: dict[str, _AggregationSourceEvidence] = {}
            extras: list[_AggregationSourceEvidence] = []
            undated: list[tuple[str, _AggregationSourceEvidence]] = []
            for source in candidates:
                event_class = _aggregation_source_event_class(source)
                class_key = event_class or f"source:{source.message_id}"
                has_event_time = any(
                    claim.event_start is not None for claim in source.claims
                )
                if bounded_window and not has_event_time:
                    undated.append((class_key, source))
                elif class_key not in representatives:
                    representatives[class_key] = source
                else:
                    extras.append(source)
            for class_key, source in undated:
                if class_key in representatives:
                    extras.append(source)
                elif not bounded_window:
                    representatives[class_key] = source

            primary = [
                dataclasses.replace(source, selection_pass=0)
                for source in representatives.values()
            ]

            def extra_priority(source: _AggregationSourceEvidence) -> tuple[int, int]:
                event_class = _aggregation_source_event_class(source)
                representative = representatives.get(
                    event_class or f"source:{source.message_id}"
                )
                is_retelling = bool(
                    representative is not None
                    and _sources_may_be_retellings(
                        representative,
                        source,
                        action_tokens=query_action_tokens,
                    )
                )
                return (0 if is_retelling else 1, source.source_rank)

            extras.sort(key=extra_priority)
            return [*primary, *extras][: max(1, int(source_limit))]

        # Give every session one slot before any session receives a second.
        first_by_session: dict[str, _AggregationSourceEvidence] = {}
        for source in candidates:
            first_by_session.setdefault(source.session_id, source)
        selected = [
            dataclasses.replace(source, selection_pass=0)
            for source in sorted(
                first_by_session.values(), key=lambda source: source.source_rank
            )
        ]
        selected_ids = {(source.message_id, source.session_id) for source in selected}
        selected.extend(
            source
            for source in candidates
            if (source.message_id, source.session_id) not in selected_ids
        )
        return selected[: max(1, int(source_limit))]

    @staticmethod
    def _aggregation_source_time(source: _AggregationSourceEvidence) -> float:
        event_times = [
            claim.event_start
            for claim in source.claims
            if claim.event_start is not None
        ]
        if event_times:
            return min(event_times)
        document_times = [
            claim.document_time
            for claim in source.claims
            if claim.document_time is not None
        ]
        return min(document_times) if document_times else math.inf

    def _aggregation_units(
        self,
        sources: Sequence[_AggregationSourceEvidence],
        *,
        action_tokens: frozenset[str] = frozenset(),
    ) -> list[tuple[bool, list[_AggregationSourceEvidence]]]:
        """Cluster possible retellings without deleting or counting them."""

        parents = list(range(len(sources)))

        def find(index: int) -> int:
            while parents[index] != index:
                parents[index] = parents[parents[index]]
                index = parents[index]
            return index

        def union(left: int, right: int) -> None:
            left_root = find(left)
            right_root = find(right)
            if left_root != right_root:
                parents[right_root] = left_root

        for left in range(len(sources)):
            for right in range(left + 1, len(sources)):
                if _sources_may_be_retellings(
                    sources[left],
                    sources[right],
                    action_tokens=action_tokens,
                ):
                    union(left, right)

        grouped: dict[int, list[_AggregationSourceEvidence]] = {}
        for index, source in enumerate(sources):
            grouped.setdefault(find(index), []).append(source)
        units = []
        for members in grouped.values():
            members.sort(
                key=lambda source: (
                    self._aggregation_source_time(source),
                    source.source_rank,
                    source.message_id,
                )
            )
            units.append((len(members) > 1, members))
        units.sort(
            key=lambda unit: (
                self._aggregation_source_time(unit[1][0]),
                min(source.source_rank for source in unit[1]),
                unit[1][0].message_id,
            )
        )
        return units

    def _render_aggregation_source(
        self,
        source: _AggregationSourceEvidence,
        *,
        max_chars: int,
        compact: bool = False,
    ) -> tuple[str, tuple[int, ...], bool]:
        document_times = [
            claim.document_time
            for claim in source.claims
            if claim.document_time is not None
        ]
        documented_at = min(document_times) if document_times else None
        if compact:
            heading = f"- session:{source.session_id} | message:{source.message_id}"
        else:
            heading = (
                f"- session:{source.session_id} | message:{source.message_id} | "
                f"document_time={_format_utc_timestamp(documented_at)}"
            )
        lines = [heading]
        rendered_claim_ids = [claim.claim_id for claim in source.claims]
        for claim in source.claims:
            event_label = _format_utc_timestamp(claim.event_start)
            claim_text = _concise_excerpt(claim.text, "", 260).replace("\n", " ")
            if compact:
                line = f"  - [event_time={event_label}] {claim_text}"
            else:
                line = (
                    f"  - [{claim.status} | {claim.kind} | event_time={event_label}] "
                    f"{claim_text}"
                )
            candidate = "\n".join([*lines, line])
            if len(candidate) > max_chars:
                return "", (), False
            lines.append(line)
        if not rendered_claim_ids:
            return "", (), False
        fully_represented = source.eligible_claim_count == len(rendered_claim_ids)
        return "\n".join(lines), tuple(rendered_claim_ids), fully_represented

    @staticmethod
    def _query_count_domain_profile(
        query: str,
    ) -> tuple[frozenset[str], tuple[frozenset[str], ...], bool]:
        match = _COUNTED_DOMAIN_RE.search(str(query or ""))
        if not match:
            return frozenset(), (), False
        return _counted_domain_profile(match.group(1))

    @staticmethod
    def _query_count_domain_tokens(query: str) -> frozenset[str]:
        tokens, _, _ = DerivedMemoryStore._query_count_domain_profile(query)
        return tokens

    @staticmethod
    def _claim_explicit_counts(
        claim: _AggregationClaimEvidence,
        *,
        domain_tokens: frozenset[str],
        domain_token_groups: Sequence[frozenset[str]] = (),
    ) -> tuple[int, ...] | None:
        compiled_values = _exact_domain_quantity_values(
            (claim.object_text, claim.text),
            domain_tokens=domain_tokens,
            domain_token_groups=domain_token_groups,
        )
        quote_values = _exact_domain_quantity_values(
            (claim.quote,),
            domain_tokens=domain_tokens,
            domain_token_groups=domain_token_groups,
        )
        if compiled_values is None or quote_values is None:
            return None
        if not compiled_values:
            return ()
        # A structured claim may select a value from its source, but it cannot
        # invent one or silently choose between conflicting raw quantities.
        if quote_values != compiled_values:
            return None
        return tuple(sorted(compiled_values))

    def _completed_aggregation_scalar(
        self,
        query: str,
        sources: Sequence[_AggregationSourceEvidence],
    ) -> tuple[str, dict[str, Any]] | None:
        """Compute a scalar only from a complete, source-linked evidence pack.

        The calculation is intentionally narrow: an explicit composite page/
        word measurement may be summed, and a ``how many ... did/have`` query
        may count completed action-object groups.  Every operand remains in the
        rendered pack, so the result is auditable without trusting model prose.
        """

        if _SCALAR_UNSUPPORTED_SET_SCOPE_RE.search(query):
            return None

        action_tokens = _aggregation_query_action_tokens(query)
        count_match = _COUNTED_DOMAIN_RE.search(query)
        if count_match is not None and _completed_query_has_coordinated_tail(query):
            return None
        if any(
            _EXPLICIT_RETELLING_RE.search(claim.quote)
            for source in sources
            for claim in source.claims
        ):
            return None
        if (
            count_match is not None
            and action_tokens
            and not _NON_GROUP_COUNT_QUERY_RE.search(query)
            and not _AGGREGATION_MONETARY_MEASURE_RE.search(query)
        ):
            (
                ordinal_domain_tokens,
                ordinal_domain_groups,
                ordinal_requires_full_match,
            ) = self._query_count_domain_profile(query)
            ordinal_candidates: list[
                tuple[float, int, int, int]
            ] = []
            for source in sources:
                source_values: set[int] = set()
                source_claim_ids: set[int] = set()
                for claim in source.claims:
                    if not (
                        _scalar_subject_is_user(claim.subject)
                        and action_tokens
                        & _aggregation_action_evidence_tokens(
                            claim.predicate,
                            claim.text,
                        )
                    ):
                        continue
                    clauses = _raw_user_completed_action_clauses(
                        claim.quote,
                        action_tokens=action_tokens,
                    )
                    if not clauses or any(
                        _CUMULATIVE_COUNTER_RESET_RE.search(clause)
                        for clause in clauses
                    ):
                        continue
                    if ordinal_requires_full_match and not (
                        _domain_token_groups_match_noun_phrase(
                            ordinal_domain_groups,
                            claim.object_text,
                            claim.text,
                        )
                        and _domain_token_groups_match_noun_phrase(
                            ordinal_domain_groups,
                            *clauses,
                        )
                    ):
                        continue
                    cumulative_clauses = tuple(
                        clause
                        for clause in clauses
                        if re.search(
                            r"\b(?:total|so\s+far|to\s+date|since|overall|"
                            r"altogether|in\s+all)\b",
                            clause,
                            re.IGNORECASE,
                        )
                    )
                    if cumulative_clauses:
                        exact_values = _exact_domain_quantity_values(
                            cumulative_clauses,
                            domain_tokens=ordinal_domain_tokens,
                            domain_token_groups=(
                                ordinal_domain_groups
                                if ordinal_requires_full_match
                                else ()
                            ),
                        )
                        if exact_values is None:
                            return None
                        source_values.update(exact_values)
                    for clause in clauses:
                        for ordinal_match in re.finditer(
                            r"(?<![A-Za-z0-9-])(?P<value>\d{1,9})"
                            r"(?:st|nd|rd|th)\s+"
                            r"(?P<noun>[A-Za-z][A-Za-z'-]*)\b",
                            clause,
                            re.IGNORECASE,
                        ):
                            if not (
                                ordinal_domain_tokens
                                & _aggregation_term_variants(
                                    ordinal_match.group("noun")
                                )
                            ):
                                continue
                            parsed = _parse_cardinal(ordinal_match.group("value"))
                            if (
                                parsed is None
                                or parsed <= 0
                                or not _scalar_match_is_exact(
                                    clause,
                                    ordinal_match,
                                )
                            ):
                                return None
                            source_values.add(parsed)
                    if source_values:
                        source_claim_ids.add(claim.claim_id)
                if len(source_values) > 1:
                    return None
                if source_values:
                    ordinal_candidates.append(
                        (
                            self._aggregation_source_time(source),
                            source.message_id,
                            next(iter(source_values)),
                            next(iter(source_claim_ids)),
                        )
                    )
            ordered_candidates = sorted(ordinal_candidates)
            ordered_values = [value for _, _, value, _ in ordered_candidates]
            if (
                len(set(ordered_values)) >= 2
                and ordered_values == sorted(ordered_values)
            ):
                resolved = ordered_values[-1]
                rendered = ", ".join(
                    _format_scalar(value) for value in sorted(set(ordered_values))
                )
                header = (
                    "Resolved completed cumulative/ordinal count from exhaustive "
                    f"user-source evidence: {_format_scalar(resolved)} completed "
                    f"items. Candidate completed totals/ordinals: {rendered}."
                )
                return header, {
                    "kind": "completed_ordinal_count",
                    "operands": [
                        _format_scalar(value) for value in ordered_values
                    ],
                    "value": _format_scalar(resolved),
                    "source_claim_ids": sorted(
                        claim_id for _, _, _, claim_id in ordered_candidates
                    ),
                }

        units = self._aggregation_units(sources, action_tokens=action_tokens)
        # Retelling clusters are deliberately heuristic and rendered as
        # "possible" duplicates. A deterministic scalar must not silently
        # convert that uncertainty into deduplication.
        if any(is_retelling for is_retelling, _ in units):
            return None
        measurement_match = _COMPOSITE_MEASUREMENT_QUERY_RE.search(query)
        if measurement_match:
            # Composite measurements are resolved only by the independent
            # exhaustive resolver. Keeping a second implementation here once
            # let a bounded pack bypass actor, raw-quote, temporal, and
            # retelling safeguards.
            return None

        if _DURATION_DAY_COUNT_QUERY_RE.search(query) and action_tokens:
            duration_re = re.compile(
                rf"\b(?P<value>{_CARDINAL_SURFACE})\s*(?:-\s*)?days?\b",
                re.IGNORECASE,
            )

            duration_domain_tokens = (
                _aggregation_query_domain_tokens(query, action_tokens)
                - action_tokens
                - {"day", "days", "time"}
            )
            duration_requested_months = {
                value.casefold() for value in _MONTH_NAME_RE.findall(query)
            }
            duration_requested_years = set(_YEAR_VALUE_RE.findall(query))
            if (
                _SCALAR_UNSUPPORTED_TEMPORAL_SCOPE_RE.search(query)
                or _SCALAR_UNSUPPORTED_TEMPORAL_GRANULARITY_RE.search(query)
            ):
                return None

            def duration_values(values: Sequence[str]) -> set[int] | None:
                found: set[int] = set()
                for raw_value in values:
                    for value_match in duration_re.finditer(raw_value):
                        if "-" in value_match.group(0):
                            nearby_words = [
                                match.group(0).casefold()
                                for match in _WORD_RE.finditer(
                                    raw_value[value_match.end() :]
                                )
                            ][:4]
                        else:
                            nearby_words = [
                                match.group(0).casefold()
                                for match in _WORD_RE.finditer(
                                    raw_value[: value_match.start()]
                                )
                            ][-5:]
                        if duration_domain_tokens and not any(
                            duration_domain_tokens
                            & _aggregation_term_variants(token)
                            for token in nearby_words
                        ):
                            continue
                        if not _scalar_match_is_exact(raw_value, value_match):
                            return None
                        parsed = _parse_cardinal(value_match.group("value"))
                        if parsed is None or parsed <= 0:
                            return None
                        found.add(parsed)
                return found

            operands: list[int] = []
            used_claim_ids: set[int] = set()
            for _, members in units:
                member_values: list[int] = []
                unit_temporally_excluded = False
                for source in members:
                    relevant_claims = [
                        claim
                        for claim in source.claims
                        if _scalar_subject_is_user(claim.subject)
                        and action_tokens
                        & _aggregation_action_evidence_tokens(
                            claim.predicate,
                            claim.text,
                        )
                        and not _aggregation_action_is_uncompleted_plan(
                            claim,
                            action_tokens,
                        )
                        and not _aggregation_action_is_noncompleted(
                            claim,
                            action_tokens,
                        )
                    ]
                    if not relevant_claims:
                        continue
                    certified_clauses = tuple(
                        clause
                        for claim in relevant_claims
                        for clause in _raw_user_completed_action_clauses(
                            claim.quote,
                            action_tokens=action_tokens,
                        )
                    )
                    if not certified_clauses:
                        return None
                    if duration_requested_months or duration_requested_years:
                        source_scope_text = " ".join(
                            (
                                *certified_clauses,
                                *(claim.text for claim in relevant_claims),
                                *(claim.object_text for claim in relevant_claims),
                            )
                        )
                        source_months = {
                            value.casefold()
                            for value in _MONTH_NAME_RE.findall(source_scope_text)
                        }
                        source_years = set(
                            _YEAR_VALUE_RE.findall(source_scope_text)
                        )
                        try:
                            event_datetimes = tuple(
                                datetime.fromtimestamp(value, tz=timezone.utc)
                                for claim in relevant_claims
                                for value in (claim.event_start, claim.event_end)
                                if value is not None
                            )
                        except (OSError, OverflowError, ValueError):
                            return None
                        if duration_requested_months:
                            event_months = {
                                value.strftime("%B").casefold()
                                for value in event_datetimes
                            }
                            grounded_months = source_months or event_months
                            if not grounded_months:
                                return None
                            if not grounded_months <= duration_requested_months:
                                unit_temporally_excluded = True
                                continue
                        if duration_requested_years:
                            event_years = {
                                str(value.year) for value in event_datetimes
                            }
                            grounded_years = source_years or event_years
                            if not grounded_years:
                                return None
                            if not grounded_years <= duration_requested_years:
                                unit_temporally_excluded = True
                                continue
                    compiled_values = duration_values(
                        tuple(
                            value
                            for claim in source.claims
                            for value in (claim.object_text, claim.text)
                        )
                    )
                    raw_values = duration_values(tuple(dict.fromkeys(certified_clauses)))
                    if (
                        compiled_values is None
                        or raw_values is None
                        or (
                            compiled_values
                            and compiled_values != raw_values
                        )
                    ):
                        return None
                    if not raw_values:
                        return None
                    if len(raw_values) != 1:
                        return None
                    for claim in source.claims:
                        claim_values = duration_values(
                            (claim.object_text, claim.text)
                        )
                        if claim_values is None:
                            return None
                        if claim in relevant_claims or claim_values:
                            used_claim_ids.add(claim.claim_id)
                    member_values.append(next(iter(raw_values)))
                if not member_values:
                    if unit_temporally_excluded:
                        continue
                    return None
                if len(set(member_values)) > 1:
                    return None
                operands.append(member_values[0])
            if not operands:
                return None
            total = sum(operands)
            rendered_operands = " + ".join(
                _format_scalar(value) for value in operands
            )
            header = (
                "Computed scalar from exhaustive source-linked event durations: "
                f"{_format_scalar(total)} days "
                f"({rendered_operands} = {_format_scalar(total)})."
            )
            return header, {
                "kind": "duration_sum",
                "unit": "days",
                "operands": [_format_scalar(value) for value in operands],
                "value": _format_scalar(total),
                "source_claim_ids": sorted(used_claim_ids),
            }

        count_match = _COUNTED_DOMAIN_RE.search(query)
        if (
            not (_COMPLETED_COUNT_QUERY_RE.search(query) and action_tokens)
            or count_match is None
            or _AGGREGATION_MONETARY_MEASURE_RE.search(query)
            or _NON_GROUP_COUNT_QUERY_RE.search(query)
        ):
            return None
        domain_tokens, domain_groups, requires_domain_match = (
            self._query_count_domain_profile(query)
        )
        if not domain_tokens:
            return None
        requested_group_unit = bool(
            _GENERIC_GROUP_COUNT_NOUN_RE.search(count_match.group(1))
        )
        if not requested_group_unit and any(
            _NAMED_COLLECTION_RE.search(claim.object_text)
            or _NAMED_COLLECTION_RE.search(claim.text)
            for source in sources
            for claim in source.claims
        ):
            # ``a pair of cufflinks`` is one acquisition group but two
            # physical cufflinks. Without an explicit group/item unit the
            # requested cardinality is ambiguous, so fail closed.
            return None
        operands: list[int] = []
        used_claim_ids: set[int] = set()
        seen_object_sources: dict[str, int] = {}

        def entities_are_compatible(
            left: frozenset[str], right: frozenset[str]
        ) -> bool:
            if not left or not right:
                return False
            shared = left & right
            if left == right:
                return True
            return bool(
                len(shared) >= 2
                and len(shared) / min(len(left), len(right)) >= 0.6
            )

        def action_clauses(claim: _AggregationClaimEvidence) -> tuple[str, ...]:
            return _raw_user_completed_action_clauses(
                claim.quote,
                action_tokens=action_tokens,
            )

        def has_singular_grounding(
            claim: _AggregationClaimEvidence,
            clauses: Sequence[str],
        ) -> bool:
            if any(
                _NAMED_COLLECTION_RE.search(value)
                for value in (claim.object_text, *clauses)
            ):
                return True
            category_keyed_object = bool(
                not requires_domain_match
                and domain_tokens
                & _aggregation_field_tokens(claim.memory_key)
            )
            for value in (claim.object_text, *clauses):
                for article_match in re.finditer(
                    r"\b(?:a|an|my|our)\b",
                    value,
                    re.I,
                ):
                    cursor = article_match.end()
                    for index, word_match in enumerate(
                        _WORD_RE.finditer(value, article_match.end())
                    ):
                        if index >= 5:
                            break
                        gap = value[cursor : word_match.start()]
                        if re.search(r"[.!?;,]", gap):
                            break
                        token = word_match.group(0).casefold()
                        if (
                            (
                                domain_tokens & _aggregation_term_variants(token)
                                or category_keyed_object
                            )
                            and not (
                                token.endswith("s") and not token.endswith("ss")
                            )
                        ):
                            return True
                        cursor = word_match.end()
            return False

        for _, members in units:
            member_totals: list[int] = []
            unit_domain_excluded = False
            for source in members:
                action_candidates = [
                    claim
                    for claim in source.claims
                    if _scalar_subject_is_user(claim.subject)
                    and action_tokens
                    & _aggregation_action_evidence_tokens(
                        claim.predicate,
                        claim.text,
                    )
                    and not _aggregation_action_is_uncompleted_plan(
                        claim,
                        action_tokens,
                    )
                    and not _aggregation_action_is_noncompleted(
                        claim,
                        action_tokens,
                    )
                ]
                relevant_claims: list[_AggregationClaimEvidence] = []
                for claim in action_candidates:
                    clauses = action_clauses(claim)
                    if not clauses:
                        return None
                    if requires_domain_match and not (
                        _domain_token_groups_match_noun_phrase(
                            domain_groups,
                            claim.object_text,
                            claim.text,
                        )
                        and _domain_token_groups_match_noun_phrase(
                            domain_groups,
                            *clauses,
                        )
                    ):
                        unit_domain_excluded = True
                        continue
                    relevant_claims.append(claim)
                if not relevant_claims:
                    continue

                object_claims: dict[str, list[_AggregationClaimEvidence]] = {}
                object_tokens: dict[str, frozenset[str]] = {}
                clauses_by_claim_id: dict[int, tuple[str, ...]] = {}
                for claim in relevant_claims:
                    clauses = action_clauses(claim)
                    clauses_by_claim_id[claim.claim_id] = clauses
                    quantity_free_object = re.sub(
                        rf"\b{_CARDINAL_SURFACE}\b",
                        " ",
                        claim.object_text or claim.text,
                        flags=re.IGNORECASE,
                    )
                    object_key = " ".join(
                        sorted(
                            _aggregation_field_tokens(
                                quantity_free_object,
                                remove_query_noise=True,
                            )
                            - action_tokens
                        )
                    )
                    if not object_key:
                        return None
                    object_claims.setdefault(object_key, []).append(claim)
                    object_tokens[object_key] = frozenset(object_key.split())
                    used_claim_ids.add(claim.claim_id)

                source_values: set[int] = set()
                quantity_claim_ids: set[int] = set()
                values_by_claim_id: dict[int, tuple[int, ...]] = {}
                for claim in source.claims:
                    values = self._claim_explicit_counts(
                        claim,
                        domain_tokens=domain_tokens,
                        domain_token_groups=(
                            domain_groups if requires_domain_match else ()
                        ),
                    )
                    if values is None or len(values) > 1:
                        return None
                    values_by_claim_id[claim.claim_id] = values
                    if not values:
                        continue
                    if claim not in relevant_claims:
                        subject_tokens = _aggregation_field_tokens(
                            claim.subject,
                            remove_query_noise=True,
                        )
                        actor_or_entity_matches = (
                            _scalar_subject_is_user(claim.subject)
                            and len(object_claims) == 1
                        ) or any(
                            entities_are_compatible(
                                subject_tokens,
                                candidate_tokens,
                            )
                            for candidate_tokens in object_tokens.values()
                        )
                        if not (
                            actor_or_entity_matches
                            and _SCALAR_QUANTITY_COMPANION_RE.search(
                                " ".join((claim.predicate, claim.text))
                            )
                        ):
                            # A same-message number owned by another actor or
                            # object must not become the user's quantity.
                            return None
                    source_values.update(values)
                    quantity_claim_ids.add(claim.claim_id)

                for claim in relevant_claims:
                    direct_values = _exact_domain_quantity_values(
                        clauses_by_claim_id[claim.claim_id],
                        domain_tokens=domain_tokens,
                        domain_token_groups=(
                            domain_groups if requires_domain_match else ()
                        ),
                    )
                    if direct_values is None:
                        return None
                    source_values.update(direct_values)
                if len(source_values) > 1:
                    return None

                quantities_by_object: dict[str, int] = {}
                companion_quantity = (
                    next(iter(source_values)) if source_values else None
                )
                for object_key, claims_for_object in object_claims.items():
                    explicit_values = {
                        value
                        for claim in claims_for_object
                        for value in values_by_claim_id[claim.claim_id]
                    }
                    if len(explicit_values) > 1:
                        return None
                    if explicit_values:
                        quantity = next(iter(explicit_values))
                        if (
                            companion_quantity is not None
                            and companion_quantity != quantity
                        ):
                            return None
                    elif companion_quantity is not None:
                        # A detached quantity companion is safe only when this
                        # source has one logical completed object to attach it
                        # to. Otherwise the allocation is ambiguous.
                        if len(object_claims) != 1:
                            return None
                        quantity = companion_quantity
                    else:
                        if any(
                            _domain_item_ordinal_present(
                                (claim.object_text, *clauses_by_claim_id[claim.claim_id]),
                                domain_token_groups=domain_groups,
                                category_keyed=bool(
                                    not requires_domain_match
                                    and domain_tokens
                                    & _aggregation_field_tokens(claim.memory_key)
                                ),
                            )
                            for claim in claims_for_object
                        ):
                            # A lone ordinal is cumulative evidence, not a
                            # singular item. Without a consistent progression
                            # the ordinal resolver above intentionally declines.
                            return None
                        if not all(
                            has_singular_grounding(
                                claim,
                                clauses_by_claim_id[claim.claim_id],
                            )
                            for claim in claims_for_object
                        ):
                            # An unnumbered plural cannot silently become one
                            # physical item or one completed group.
                            return None
                        quantity = 1
                    quantities_by_object[object_key] = quantity
                    prior_source = seen_object_sources.get(object_key)
                    if prior_source is not None and prior_source != source.message_id:
                        # Same action/object in separate messages may be a
                        # retelling; timestamp drift is not proof of a new item.
                        return None
                    seen_object_sources[object_key] = source.message_id
                used_claim_ids.update(quantity_claim_ids)
                member_totals.append(sum(quantities_by_object.values()))
            if not member_totals:
                if requires_domain_match and unit_domain_excluded:
                    continue
                return None
            # Multiple members in one unit are possible retellings. Preserve
            # the largest fully stated quantity rather than summing duplicates.
            operands.append(max(member_totals))
        if not operands:
            return None
        total = sum(operands)
        rendered_operands = " + ".join(_format_scalar(value) for value in operands)
        header = (
            "Computed scalar from exhaustive source-linked evidence: "
            f"{_format_scalar(total)} completed action-object groups/items "
            f"({rendered_operands} = {_format_scalar(total)}). "
            "Count unit: one matched acquisition/action group; a named pair or set "
            "remains one acquired group. This scalar does not assert the number of "
            "physical components."
        )
        return header, {
            "kind": "completed_group_count",
            "operands": [_format_scalar(value) for value in operands],
            "value": _format_scalar(total),
            "source_claim_ids": sorted(used_claim_ids),
        }

    def _build_aggregation_pack(
        self,
        query: str,
        source_message_ids: Sequence[int],
        *,
        max_chars: int,
        allowed_message_ids: set[int] | None = None,
        fallback_source_message_ids: set[int] | None = None,
        include_uncompleted: bool = False,
    ) -> _AggregationPack | None:
        character_limit = max(1, int(max_chars))
        base_header = (
            _PENDING_AGGREGATION_HEADER if include_uncompleted else _AGGREGATION_HEADER
        )
        if character_limit < len(base_header) + 80:
            return None
        sources = self._aggregation_sources(
            query,
            source_message_ids,
            allowed_message_ids=allowed_message_ids,
            fallback_source_message_ids=fallback_source_message_ids,
            include_uncompleted=include_uncompleted,
        )
        if not sources:
            return None
        exhaustive_sources: list[_AggregationSourceEvidence] = []
        wants_scalar_ledger = bool(
            include_uncompleted
            or _COMPOSITE_MEASUREMENT_QUERY_RE.search(query)
            or _COMPLETED_COUNT_QUERY_RE.search(query)
        )
        if wants_scalar_ledger:
            exhaustive_source_ids = self._exhaustive_aggregation_source_ids(
                allowed_message_ids=allowed_message_ids,
            )
            exhaustive_sources = self._aggregation_sources(
                query,
                exhaustive_source_ids,
                allowed_message_ids=allowed_message_ids,
                fallback_source_message_ids=set(),
                include_uncompleted=include_uncompleted,
                source_limit=None,
            )
        pending_count_reserve = (
            "Distinct open action-object obligation groups represented in this "
            "bounded pack: "
            f"{len(sources) * _AGGREGATION_CLAIMS_PER_SOURCE}"
        )
        header = (
            f"{base_header}\n{pending_count_reserve}"
            if include_uncompleted
            else base_header
        )
        if character_limit < len(header) + 80:
            return None
        action_only_focus = _aggregation_query_uses_action_only_focus(query)
        compact_sources = action_only_focus and not include_uncompleted

        # Reserve capacity in two passes before chronological rendering. This
        # prevents several old messages from one session from consuming the
        # character budget before another relevant session gets one source.
        selected_sources: list[_AggregationSourceEvidence] = []
        remaining_capacity = character_limit - len(header)
        rendered_preview: dict[tuple[int, str], tuple[str, tuple[int, ...], bool]] = {}
        for selection_pass in (0, 1):
            for source in sources:
                if source.selection_pass != selection_pass:
                    continue
                preview = self._render_aggregation_source(
                    source,
                    max_chars=character_limit,
                    compact=compact_sources,
                )
                rendered_preview[(source.message_id, source.session_id)] = preview
                source_text, _, _ = preview
                if not source_text:
                    continue
                # A possible-retelling heading is shared by at least two
                # sources; this small per-source reserve keeps final rendering
                # bounded without materially reducing evidence coverage.
                source_cost = len(source_text) + (32 if compact_sources else 48)
                if source_cost > remaining_capacity:
                    continue
                selected_sources.append(source)
                remaining_capacity -= source_cost
        if not selected_sources:
            # Tiny but valid budgets should still get one atomic source when
            # it fits and no retelling heading is necessary.
            for source in sources:
                preview = rendered_preview.get(
                    (source.message_id, source.session_id)
                ) or self._render_aggregation_source(
                    source,
                    max_chars=character_limit,
                    compact=compact_sources,
                )
                if preview[0] and len(header) + 1 + len(preview[0]) <= character_limit:
                    selected_sources.append(source)
                    break
        if not selected_sources:
            return None

        rendered_parts = [header]
        rendered_sources: list[_AggregationSourceEvidence] = []
        rendered_claim_ids: list[int] = []
        represented_message_ids: list[int] = []
        retelling_index = 0
        retelling_action_tokens = (
            frozenset()
            if include_uncompleted
            else _aggregation_query_action_tokens(query)
        )
        for is_retelling, members in self._aggregation_units(
            selected_sources,
            action_tokens=retelling_action_tokens,
        ):
            label = ""
            if is_retelling:
                retelling_index += 1
                label = (
                    f"Possible retellings R{retelling_index}; preserve every source "
                    "and do not count automatically:"
                )
            unit_parts = [label] if label else []
            unit_sources: list[_AggregationSourceEvidence] = []
            unit_claim_ids: list[int] = []
            unit_represented_message_ids: list[int] = []
            for source in members:
                remaining = (
                    character_limit - len("\n".join(rendered_parts + unit_parts)) - 1
                )
                source_text, claim_ids, fully_represented = (
                    self._render_aggregation_source(
                        source,
                        max_chars=max(0, remaining),
                        compact=compact_sources,
                    )
                )
                if not source_text:
                    continue
                candidate = "\n".join(rendered_parts + unit_parts + [source_text])
                if len(candidate) > character_limit:
                    continue
                unit_parts.append(source_text)
                unit_sources.append(source)
                unit_claim_ids.extend(claim_ids)
                if fully_represented:
                    unit_represented_message_ids.append(source.message_id)
            if not unit_sources:
                continue
            rendered_parts.extend(unit_parts)
            rendered_sources.extend(unit_sources)
            rendered_claim_ids.extend(unit_claim_ids)
            represented_message_ids.extend(unit_represented_message_ids)

        if not rendered_sources:
            return None
        pending_group_count: int | None = None
        computed_scalar_payload: dict[str, Any] | None = None
        if include_uncompleted:
            exhaustive_source_keys = {
                (source.message_id, source.session_id)
                for source in exhaustive_sources
            }
            rendered_source_keys = {
                (source.message_id, source.session_id) for source in rendered_sources
            }
            # The display pack is bounded, so only the independent uncapped
            # structured ledger can prove global completeness.
            exhaustive_sources_complete = bool(exhaustive_sources) and all(
                source.eligible_claim_count == len(source.claims)
                for source in exhaustive_sources
            )
            exhaustive_claims = [
                claim
                for source in exhaustive_sources
                for claim in source.claims
                if _pending_claim_matches_query(query, claim)
            ]
            used_claim_ids = {claim.claim_id for claim in exhaustive_claims}
            rendered_sources_complete = used_claim_ids <= set(rendered_claim_ids)
            if (
                exhaustive_source_keys == rendered_source_keys
                and exhaustive_sources_complete
                and rendered_sources_complete
            ):
                count, used_explicit_quantity = _pending_obligation_group_count(
                    exhaustive_claims,
                    query=query,
                )
                if count:
                    if used_explicit_quantity:
                        computed_header = (
                            "Computed scalar from exhaustive source-linked open "
                            f"obligations: {count} requested items. Count rule: "
                            "use explicit requested-noun quantities when stated; "
                            "otherwise count distinct action-object groups."
                        )
                    else:
                        computed_header = (
                            "Computed scalar from exhaustive source-linked open "
                            f"obligations: {count} distinct action-object groups. "
                            "Count unit: normalized action + object; different actions "
                            "on the same object remain separate."
                        )
                    candidate_parts = [computed_header, *rendered_parts[1:]]
                    scalar_parts = [
                        re.sub(
                            r"Possible retellings R(\d+); preserve every source "
                            r"and do not count automatically:",
                            r"Related source cluster R\1 retained for audit; "
                            r"the scalar uses explicit structured group identities:",
                            part,
                        )
                        for part in candidate_parts
                    ]
                    if len("\n".join(scalar_parts)) <= character_limit:
                        pending_group_count = count
                        rendered_parts = scalar_parts
                    else:
                        rendered_parts[0] = base_header
                else:
                    rendered_parts[0] = base_header
            else:
                rendered_parts[0] = base_header
        else:
            exhaustive_source_keys = {
                (source.message_id, source.session_id)
                for source in exhaustive_sources
            }
            rendered_source_keys = {
                (source.message_id, source.session_id) for source in rendered_sources
            }
            exhaustive_sources_complete = bool(exhaustive_sources) and all(
                source.eligible_claim_count == len(source.claims)
                for source in exhaustive_sources
            )
            computed = (
                self._completed_aggregation_scalar(query, exhaustive_sources)
                if exhaustive_sources_complete
                else None
            )
            used_claim_ids = (
                set(computed[1].get("source_claim_ids", ()))
                if computed is not None
                else set()
            )
            rendered_sources_complete = bool(used_claim_ids) and used_claim_ids <= set(
                rendered_claim_ids
            )
            if (
                exhaustive_source_keys == rendered_source_keys
                and exhaustive_sources_complete
                and rendered_sources_complete
            ):
                if computed is not None:
                    computed_header, payload = computed
                    candidate_parts = [computed_header, *rendered_parts[1:]]
                    scalar_parts = [
                        re.sub(
                            r"Possible retellings R(\d+); preserve every source "
                            r"and do not count automatically:",
                            r"Related source cluster R\1 retained for audit; "
                            r"the scalar uses explicit structured quantities:",
                            part,
                        )
                        for part in candidate_parts
                    ]
                    if len("\n".join(scalar_parts)) <= character_limit:
                        rendered_parts = scalar_parts
                        computed_scalar_payload = payload
        message_ids = tuple(source.message_id for source in rendered_sources)
        session_ids = tuple(
            dict.fromkeys(source.session_id for source in rendered_sources)
        )
        text = "\n".join(rendered_parts)
        identity_payload = {
            "format": AGGREGATION_PACK_FORMAT_VERSION,
            "mode": "pending" if include_uncompleted else "completed",
            "claim_ids": sorted(set(rendered_claim_ids)),
            "message_ids": sorted(set(message_ids)),
            "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        }
        if pending_group_count is not None:
            identity_payload["pending_group_count"] = pending_group_count
        if computed_scalar_payload is not None:
            identity_payload["computed_scalar"] = computed_scalar_payload
        if compact_sources:
            identity_payload["render"] = "compact-action"
        composite_id = (
            "evidence-pack:"
            + hashlib.sha256(_json(identity_payload).encode("utf-8")).hexdigest()[:24]
        )
        block = ContextBlock(
            kind="evidence_pack",
            text=text,
            message_ids=message_ids,
            session_ids=session_ids,
            score=1.0,
            channels=(
                "aggregation_pack",
                "pending_obligations" if include_uncompleted else "completed_events",
                "source_linked",
                "local_only",
            ),
            token_count=estimate_tokens(text),
            composite_id=composite_id,
        )
        return _AggregationPack(
            block=block,
            claim_ids=frozenset(rendered_claim_ids),
            represented_message_ids=frozenset(represented_message_ids),
        )

    def rank_memories(
        self,
        query: str,
        raw_messages: Sequence[Any],
        *,
        limit: int = 50,
        include_claims: bool = True,
        max_chars: int = 1200,
        include_session_siblings: bool = True,
        allowed_message_ids: set[int] | None = None,
        aggregation_source_message_ids: Sequence[int] | None = None,
        aggregation_mode: str | None = None,
    ) -> list[ContextBlock]:
        """Fuse concise compiled and raw candidates without a render budget.

        Claim FTS and raw hybrid search have intentionally different native
        score scales.  Weighted reciprocal-rank fusion combines their ranks
        and adds a small bidirectional boost when a claim cites a raw hit.  A
        slight raw-channel prior keeps exact assistant evidence from falling
        behind an entire page of derived claims.

        Unlike :meth:`compose_context`, this method is bounded by candidate
        count and per-memory characters, not by a single rendered-context
        token budget.  It is therefore suitable for benchmark APIs whose
        ``top_k`` contract is independent of the later answerer's context.
        """

        candidate_limit = max(1, int(limit))
        character_limit = max(1, int(max_chars))
        if not include_claims:
            claims = []
        elif allowed_message_ids is None:
            # Preserve the exact ordinary-call surface for integrations that
            # wrap or instrument claim search.
            claims = self.search_claims(query, limit=candidate_limit)
        else:
            try:
                claims = self.search_claims(
                    query,
                    limit=candidate_limit,
                    allowed_message_ids=allowed_message_ids,
                )
            except TypeError as error:
                if "allowed_message_ids" not in str(error):
                    raise
                # Compatibility for instrumentation wrappers that still
                # expose the pre-scope signature. Production search_claims
                # filters before its top-k; this fallback at least enforces
                # the boundary on the wrapper's returned candidates.
                claims = [
                    claim
                    for claim in self.search_claims(
                        query,
                        limit=candidate_limit,
                    )
                    if claim.sources
                    and all(
                        source.message_id in allowed_message_ids
                        for source in claim.sources
                    )
                ]
        aggregation_pack: _AggregationPack | None = None
        if include_claims and aggregation_source_message_ids is not None:
            # Claim FTS has direct typed evidence, so its user sources must be
            # considered before the broader dense/raw overfetch. The latter
            # remains available as a vocabulary-gap fallback.
            claim_source_ids: list[int] = []
            for claim in claims:
                for source in claim.sources:
                    if source.speaker.casefold() == "user":
                        claim_source_ids.append(source.message_id)
            fallback_source_ids = list(aggregation_source_message_ids)
            source_ids = list(dict.fromkeys([*claim_source_ids, *fallback_source_ids]))
            aggregation_pack = self._build_aggregation_pack(
                query,
                source_ids,
                max_chars=character_limit,
                allowed_message_ids=allowed_message_ids,
                fallback_source_message_ids=set(fallback_source_ids),
                include_uncompleted=aggregation_mode == "pending",
            )

        raw_candidates: list[_RawFusionCandidate] = []
        raw_seen: set[tuple[str, str]] = set()
        raw_rank_by_message_id: dict[int, int] = {}
        raw_index_by_message_id: dict[int, int] = {}
        for message in raw_messages:
            message_text = str(getattr(message, "text", ""))
            if not message_text:
                continue
            speaker = str(getattr(message, "speaker", "memory"))
            normalized = (_canonical(speaker), _canonical(message_text))
            if normalized in raw_seen:
                continue
            raw_seen.add(normalized)
            raw_rank = len(raw_candidates)
            raw_candidates.append(
                _RawFusionCandidate(message=message, hybrid_rank=raw_rank)
            )
            message_id = int(getattr(message, "id", 0) or 0)
            if message_id:
                raw_rank_by_message_id.setdefault(message_id, raw_rank)
                raw_index_by_message_id.setdefault(message_id, raw_rank)
            if len(raw_candidates) >= candidate_limit:
                break

        # A generic compiled claim can identify the right session even when
        # lexical/dense retrieval misses the assistant's exact answer.  Expand
        # only a small assistant-first neighborhood of the highest-ranked
        # claim sessions, then let the same score fusion decide whether those
        # siblings belong in top-k.
        sibling_candidates = (
            self._session_sibling_candidates(
                claims,
                query,
                limit=candidate_limit,
            )
            if include_session_siblings
            else []
        )
        for sibling, sibling_claim_rank, sibling_local_rank in sibling_candidates:
            existing_index = raw_index_by_message_id.get(sibling.id)
            if existing_index is not None:
                existing = raw_candidates[existing_index]
                prior_rank = existing.sibling_claim_rank
                raw_candidates[existing_index] = _RawFusionCandidate(
                    message=existing.message,
                    hybrid_rank=existing.hybrid_rank,
                    sibling_claim_rank=(
                        sibling_claim_rank
                        if prior_rank is None
                        else min(prior_rank, sibling_claim_rank)
                    ),
                    sibling_local_rank=(
                        sibling_local_rank
                        if existing.sibling_local_rank is None
                        else min(existing.sibling_local_rank, sibling_local_rank)
                    ),
                )
                continue
            normalized = (_canonical(sibling.speaker), _canonical(sibling.text))
            if normalized in raw_seen:
                continue
            raw_seen.add(normalized)
            raw_index_by_message_id[sibling.id] = len(raw_candidates)
            raw_candidates.append(
                _RawFusionCandidate(
                    message=sibling,
                    sibling_claim_rank=sibling_claim_rank,
                    sibling_local_rank=sibling_local_rank,
                )
            )

        claim_rank_by_source: dict[int, int] = {}
        for claim_rank, claim in enumerate(claims):
            for source in claim.sources:
                claim_rank_by_source[source.message_id] = min(
                    claim_rank,
                    claim_rank_by_source.get(source.message_id, claim_rank),
                )

        ranked: list[tuple[float, float, int, int, ContextBlock]] = []
        for claim_rank, claim in enumerate(claims):
            source_raw_ranks = [
                raw_rank_by_message_id[source.message_id]
                for source in claim.sources
                if source.message_id in raw_rank_by_message_id
            ]
            fused_score = _CLAIM_FUSION_WEIGHT / (
                _FUSION_RANK_CONSTANT + claim_rank + 1
            )
            channels = [*claim.channels, "score_fusion"]
            if source_raw_ranks:
                fused_score += _CLAIM_RAW_SUPPORT_WEIGHT / (
                    _FUSION_RANK_CONSTANT + min(source_raw_ranks) + 1
                )
                channels.append("raw_support")
            message_ids = tuple(source.message_id for source in claim.sources)
            session_ids = tuple(
                dict.fromkeys(source.session_id for source in claim.sources)
            )
            # Keep the default prompt candidate to the compiler's grounded
            # claim text. Structured fields and source links remain available
            # on MemoryClaim/ContextBlock for programmatic consumers; repeating
            # them here inflates every top-k slot and can crowd out raw context.
            text = _concise_excerpt(
                f"[{claim.status} | {claim.kind}] {claim.text}",
                query,
                character_limit,
            )
            ranked.append(
                (
                    fused_score,
                    claim.score,
                    0,
                    claim_rank,
                    ContextBlock(
                        kind="claim",
                        text=text,
                        claim_id=claim.id,
                        message_ids=message_ids,
                        session_ids=session_ids,
                        status=claim.status,
                        score=fused_score,
                        channels=tuple(dict.fromkeys(channels)),
                        token_count=estimate_tokens(text),
                    ),
                )
            )

        for candidate_index, candidate in enumerate(raw_candidates):
            message = candidate.message
            message_id = int(getattr(message, "id", 0) or 0)
            fused_score = 0.0
            channels = ["score_fusion"]
            if candidate.hybrid_rank is not None:
                fused_score = _RAW_FUSION_WEIGHT / (
                    _FUSION_RANK_CONSTANT + candidate.hybrid_rank + 1
                )
                channels.insert(0, "raw_hybrid")
            if candidate.sibling_claim_rank is not None:
                sibling_weight = (
                    _ASSISTANT_SIBLING_FUSION_WEIGHT
                    if str(getattr(message, "speaker", "")).casefold() == "assistant"
                    else _OTHER_SIBLING_FUSION_WEIGHT
                )
                sibling_score = sibling_weight / (
                    _FUSION_RANK_CONSTANT
                    + candidate.sibling_claim_rank
                    + 4 * int(candidate.sibling_local_rank or 0)
                    + 1
                )
                fused_score = max(fused_score, sibling_score)
                channels.append("claim_session")
                if sibling_weight == _ASSISTANT_SIBLING_FUSION_WEIGHT:
                    channels.append("assistant_sibling")
            if message_id in claim_rank_by_source:
                fused_score += _RAW_CLAIM_SUPPORT_WEIGHT / (
                    _FUSION_RANK_CONSTANT + claim_rank_by_source[message_id] + 1
                )
                channels.append("claim_support")
            speaker = str(getattr(message, "speaker", "memory"))
            text = _concise_excerpt(
                f"[{speaker}] {getattr(message, 'text', '')}",
                query,
                character_limit,
            )
            provenance = getattr(message, "provenance", None) or {}
            metadata = (
                provenance.get("metadata") if isinstance(provenance, Mapping) else {}
            )
            metadata = metadata if isinstance(metadata, Mapping) else {}
            session_id = str(
                metadata.get("session_id")
                or (
                    provenance.get("run_id")
                    if isinstance(provenance, Mapping)
                    else None
                )
                or "unscoped"
            )
            ranked.append(
                (
                    fused_score,
                    0.0,
                    1,
                    candidate.hybrid_rank
                    if candidate.hybrid_rank is not None
                    else candidate_limit + candidate_index,
                    ContextBlock(
                        kind="raw_message",
                        text=text,
                        message_ids=(message_id,) if message_id else (),
                        session_ids=(session_id,),
                        score=fused_score,
                        channels=tuple(channels),
                        token_count=estimate_tokens(text),
                    ),
                )
            )

        # Native channel score is only a deterministic tie-breaker; the fused
        # rank score is the sole cross-channel comparison.  Raw wins an exact
        # tie so verbatim evidence is never displaced by its own abstraction.
        def stable_block_key(block: ContextBlock) -> tuple:
            return (
                block.kind,
                block.claim_id if block.claim_id is not None else -1,
                block.composite_id or "",
                block.message_ids,
                block.session_ids,
                block.text,
            )

        ranked.sort(
            key=lambda item: (
                -item[0],
                -item[1],
                -item[2],
                item[3],
                stable_block_key(item[4]),
            )
        )
        blocks = [item[4] for item in ranked]
        if aggregation_pack is not None:
            blocks = [
                block
                for block in blocks
                if block.claim_id not in aggregation_pack.claim_ids
                and not (
                    block.kind == "raw_message"
                    and bool(
                        aggregation_pack.represented_message_ids
                        & set(block.message_ids)
                    )
                )
            ]
            blocks.insert(0, aggregation_pack.block)
        measurement_resolution = None
        if _COMPOSITE_MEASUREMENT_QUERY_RE.search(query):
            measurement_resolution = _build_composite_measurement_resolution(
                query,
                self._exhaustive_user_grounded_claims(
                    allowed_message_ids=allowed_message_ids,
                ),
                max_chars=character_limit,
            )
        if measurement_resolution is not None:
            blocks = [
                block
                for block in blocks
                if "aggregation_pack" not in block.channels
                and block.claim_id not in measurement_resolution.claim_ids
                and not (
                    block.kind == "raw_message"
                    and bool(
                        measurement_resolution.represented_message_ids
                        & set(block.message_ids)
                    )
                )
            ]
            blocks.insert(0, measurement_resolution.block)
        cumulative_resolution = None
        if _CUMULATIVE_COUNTER_QUERY_RE.search(query):
            cumulative_resolution = _build_cumulative_counter_resolution(
                query,
                self._exhaustive_user_messages(
                    allowed_message_ids=allowed_message_ids,
                ),
                self._exhaustive_user_grounded_claims(
                    allowed_message_ids=allowed_message_ids,
                ),
                max_chars=character_limit,
            )
        if cumulative_resolution is not None:
            blocks = [
                block
                for block in blocks
                if block.claim_id not in cumulative_resolution.claim_ids
                and not bool(
                    cumulative_resolution.represented_message_ids
                    & set(block.message_ids)
                )
            ]
            blocks.insert(0, cumulative_resolution.block)
        return blocks[:candidate_limit]

    def compose_ranked_context(
        self,
        query: str,
        ranked_blocks: Sequence[ContextBlock],
        *,
        token_budget: int = 6000,
        include_debug: bool = False,
    ) -> ContextBundle:
        """Render already-fused memory blocks inside a hard token budget."""

        started = time.perf_counter()
        budget = max(128, int(token_budget))
        header = (
            "<memory>\n"
            f"Query: {query}\n"
            "Use active claims for current-state questions. Keep superseded and "
            "contradictory claims only when the question asks about history. "
            "Canonical message IDs identify supporting evidence.\n"
        )
        footer = "\n</memory>"
        remaining = max(0, budget - estimate_tokens(header + footer))
        blocks: list[ContextBlock] = []

        for block in ranked_blocks:
            message_ids = ",".join(str(value) for value in block.message_ids) or "none"
            session_ids = ",".join(block.session_ids) or "unscoped"
            if block.kind == "claim":
                label = (
                    f"CLAIM | {block.status} | claim:{block.claim_id} | "
                    f"message:{message_ids} | sessions:{session_ids}"
                )
            elif block.kind == "evidence_pack":
                label = (
                    f"EVIDENCE_PACK | {block.composite_id or 'unscoped'} | "
                    f"message:{message_ids} | sessions:{session_ids}"
                )
            else:
                label = f"RAW | message:{message_ids} | sessions:{session_ids}"
            text = f"- [{label}] {block.text}"
            tokens = estimate_tokens(text)
            if tokens > remaining:
                continue
            blocks.append(dataclasses.replace(block, text=text, token_count=tokens))
            remaining -= tokens
            if remaining < 32:
                break

        rendered = header + "\n".join(block.text for block in blocks) + footer
        while blocks and estimate_tokens(rendered) > budget:
            blocks.pop()
            rendered = header + "\n".join(block.text for block in blocks) + footer
        elapsed = (time.perf_counter() - started) * 1000
        return ContextBundle(
            query=query,
            text=rendered,
            blocks=blocks,
            token_count=estimate_tokens(rendered),
            token_budget=budget,
            query_ms=elapsed,
            total_candidates=len(ranked_blocks),
            mode="intelligence",
            debug={
                "ranked_candidates": len(ranked_blocks),
                "claim_blocks": sum(block.kind == "claim" for block in blocks),
                "raw_blocks": sum(block.kind == "raw_message" for block in blocks),
                "evidence_pack_blocks": sum(
                    block.kind == "evidence_pack" for block in blocks
                ),
                "channels": sorted(
                    {channel for block in blocks for channel in block.channels}
                ),
            }
            if include_debug
            else {},
        )

    def compose_context(
        self,
        query: str,
        raw_messages: Iterable[Any],
        *,
        token_budget: int = 6000,
        include_debug: bool = False,
        include_claims: bool = True,
    ) -> ContextBundle:
        started = time.perf_counter()
        budget = max(128, int(token_budget))
        claims = self.search_claims(query, limit=50) if include_claims else []
        blocks: list[ContextBlock] = []
        used_message_ids: set[int] = set()
        remaining = budget

        for claim in claims:
            source_lines = []
            session_ids = []
            for source in claim.sources[:6]:
                used_message_ids.add(source.message_id)
                if source.session_id not in session_ids:
                    session_ids.append(source.session_id)
                source_lines.append(
                    f"  - [{source.session_id} | {source.speaker} | message:{source.message_id}] {source.quote}"
                )
            time_label = claim.event_start or claim.document_time
            labels = [claim.status.upper(), claim.kind]
            if time_label is not None:
                labels.append(f"time={time_label:g}")
            text = f"- [{' | '.join(labels)} | claim:{claim.id}] {claim.text}"
            if source_lines:
                text += "\n" + "\n".join(source_lines)
            tokens = estimate_tokens(text)
            if tokens > remaining:
                continue
            blocks.append(
                ContextBlock(
                    kind="claim",
                    text=text,
                    claim_id=claim.id,
                    message_ids=tuple(source.message_id for source in claim.sources),
                    session_ids=tuple(session_ids),
                    status=claim.status,
                    score=claim.score,
                    channels=claim.channels,
                    token_count=tokens,
                )
            )
            remaining -= tokens
            if remaining < 128:
                break

        raw_seen: set[tuple[str, str]] = set()
        for index, message in enumerate(raw_messages):
            message_id = int(getattr(message, "id", 0) or 0)
            if message_id in used_message_ids:
                continue
            speaker = str(getattr(message, "speaker", "memory"))
            message_text = str(getattr(message, "text", ""))
            normalized = (_canonical(speaker), _canonical(message_text))
            if not message_text or normalized in raw_seen:
                continue
            raw_seen.add(normalized)
            provenance = getattr(message, "provenance", None) or {}
            metadata = (
                provenance.get("metadata") if isinstance(provenance, Mapping) else {}
            )
            metadata = metadata if isinstance(metadata, Mapping) else {}
            session_id = str(
                metadata.get("session_id") or provenance.get("run_id") or "unscoped"
            )
            timestamp = getattr(message, "timestamp", None)
            text = (
                f"- [RAW | {session_id} | {speaker} | message:{message_id} | time={timestamp}] "
                f"{message_text}"
            )
            tokens = estimate_tokens(text)
            if tokens > remaining:
                continue
            blocks.append(
                ContextBlock(
                    kind="raw_message",
                    text=text,
                    message_ids=(message_id,) if message_id else (),
                    session_ids=(session_id,),
                    score=max(0.0, 0.15 - index * 0.001),
                    channels=("raw_hybrid",),
                    token_count=tokens,
                )
            )
            remaining -= tokens
            if remaining < 64:
                break

        header = (
            "<memory>\n"
            f"Query: {query}\n"
            "Use active claims for current-state questions. Keep superseded and contradictory "
            "claims only when the question asks about history. Cite message IDs when answering.\n"
        )
        footer = "\n</memory>"
        text = header + "\n".join(block.text for block in blocks) + footer
        while blocks and estimate_tokens(text) > budget:
            blocks.pop()
            text = header + "\n".join(block.text for block in blocks) + footer
        elapsed = (time.perf_counter() - started) * 1000
        return ContextBundle(
            query=query,
            text=text,
            blocks=blocks,
            token_count=estimate_tokens(text),
            token_budget=budget,
            query_ms=elapsed,
            total_candidates=len(claims),
            mode="intelligence" if include_claims else "private",
            debug={
                "claim_candidates": len(claims),
                "claim_blocks": sum(block.kind == "claim" for block in blocks),
                "raw_blocks": sum(block.kind == "raw_message" for block in blocks),
            }
            if include_debug
            else {},
        )

    def cleanup_orphans(self) -> None:
        orphan_claims = self.connection.execute(
            """
            SELECT c.id FROM memory_claims c
            LEFT JOIN memory_claim_sources cs ON cs.claim_id = c.id
            WHERE c.user_id = ? GROUP BY c.id HAVING COUNT(cs.message_id) = 0
            """,
            (self.user_id,),
        ).fetchall()
        for row in orphan_claims:
            self.connection.execute(
                f"DELETE FROM {self.fts_table} WHERE rowid = ?", (int(row[0]),)
            )
            self.connection.execute(
                "DELETE FROM memory_claims WHERE id = ?", (int(row[0]),)
            )
        self.connection.execute(
            """
            DELETE FROM memory_sessions
            WHERE user_id = ? AND id NOT IN (SELECT DISTINCT session_id FROM memory_session_messages)
            """,
            (self.user_id,),
        )
        self.connection.commit()

    def purge(self) -> int:
        count = int(
            self.connection.execute(
                "SELECT COUNT(*) FROM memory_claims WHERE user_id = ?", (self.user_id,)
            ).fetchone()[0]
        )
        self.connection.execute(f"DELETE FROM {self.fts_table}")
        self.connection.execute(
            "DELETE FROM memory_compiler_jobs WHERE user_id = ?", (self.user_id,)
        )
        self.connection.execute(
            "DELETE FROM memory_usage_ledger WHERE user_id = ?", (self.user_id,)
        )
        self.connection.execute(
            "DELETE FROM memory_claims WHERE user_id = ?", (self.user_id,)
        )
        self.connection.execute(
            "DELETE FROM memory_entities WHERE user_id = ?", (self.user_id,)
        )
        self.connection.execute(
            "DELETE FROM memory_sessions WHERE user_id = ?", (self.user_id,)
        )
        self.connection.commit()
        try:
            self.connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        except sqlite3.OperationalError:
            # Another live scope may briefly hold a reader. secure_delete has
            # already overwritten deleted cells; a later checkpoint can trim
            # the WAL without making purge fail.
            pass
        return count

    def status(self) -> dict[str, Any]:
        job_rows = self.connection.execute(
            """
            SELECT status, COUNT(*) FROM memory_compiler_jobs
            WHERE user_id = ? GROUP BY status
            """,
            (self.user_id,),
        ).fetchall()
        partial_reason_rows = self.connection.execute(
            """
            SELECT COALESCE(last_error, 'unspecified'), COUNT(*)
            FROM memory_compiler_jobs
            WHERE user_id = ? AND status = 'partial'
            GROUP BY COALESCE(last_error, 'unspecified')
            ORDER BY COALESCE(last_error, 'unspecified')
            """,
            (self.user_id,),
        ).fetchall()
        usage = self.connection.execute(
            """
            SELECT COALESCE(SUM(input_tokens), 0), COALESCE(SUM(output_tokens), 0),
                   COALESCE(SUM(reasoning_tokens), 0), COALESCE(SUM(cost_usd), 0)
            FROM memory_usage_ledger WHERE user_id = ?
            """,
            (self.user_id,),
        ).fetchone()
        return {
            "session_count": int(
                self.connection.execute(
                    "SELECT COUNT(*) FROM memory_sessions WHERE user_id = ?",
                    (self.user_id,),
                ).fetchone()[0]
            ),
            "claim_count": int(
                self.connection.execute(
                    "SELECT COUNT(*) FROM memory_claims WHERE user_id = ?",
                    (self.user_id,),
                ).fetchone()[0]
            ),
            "entity_count": int(
                self.connection.execute(
                    "SELECT COUNT(*) FROM memory_entities WHERE user_id = ?",
                    (self.user_id,),
                ).fetchone()[0]
            ),
            "jobs": {str(row[0]): int(row[1]) for row in job_rows},
            "partial_reasons": {
                str(row[0]): int(row[1]) for row in partial_reason_rows
            },
            "usage": {
                "input_tokens": int(usage[0]),
                "output_tokens": int(usage[1]),
                "reasoning_tokens": int(usage[2]),
                "cost_usd": round(float(usage[3]), 6),
            },
            "derived_schema_version": DERIVED_SCHEMA_VERSION,
            "claim_fts_format_version": CLAIM_FTS_FORMAT_VERSION,
            "claim_render_format_version": CLAIM_RENDER_FORMAT_VERSION,
            "memory_key_format_version": MEMORY_KEY_FORMAT_VERSION,
        }

    def health(self) -> dict[str, Any]:
        claim_count = int(
            self.connection.execute(
                "SELECT COUNT(*) FROM memory_claims WHERE user_id = ?", (self.user_id,)
            ).fetchone()[0]
        )
        fts_count = int(
            self.connection.execute(
                f"SELECT COUNT(*) FROM {self.fts_table}"
            ).fetchone()[0]
        )
        orphan_sources = int(
            self.connection.execute(
                """
                SELECT COUNT(*) FROM memory_claim_sources cs
                LEFT JOIN messages m ON m.id = cs.message_id
                JOIN memory_claims c ON c.id = cs.claim_id
                WHERE c.user_id = ? AND m.id IS NULL
                """,
                (self.user_id,),
            ).fetchone()[0]
        )
        memory_key_version_row = self.connection.execute(
            "SELECT value FROM metadata WHERE key = ?",
            (f"{self.fts_table}_memory_key_version",),
        ).fetchone()
        memory_key_version = (
            int(memory_key_version_row[0])
            if memory_key_version_row and str(memory_key_version_row[0]).isdigit()
            else None
        )
        return {
            "ok": claim_count == fts_count
            and orphan_sources == 0
            and memory_key_version == MEMORY_KEY_FORMAT_VERSION,
            "claim_count": claim_count,
            "indexed_claim_count": fts_count,
            "orphan_claim_sources": orphan_sources,
            "memory_key_format_version": {
                "expected": MEMORY_KEY_FORMAT_VERSION,
                "actual": memory_key_version,
            },
        }
