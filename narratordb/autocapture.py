"""Conservative, deterministic extraction of durable personal memories.

This module deliberately handles a small set of high-confidence statements.  It
does not try to understand arbitrary conversation: callers should use an
explicit remember action or an opt-in model-backed extractor for that job.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from typing import Literal


MAX_AUTOCAPTURE_CHARS = 600
MAX_AUTOCAPTURE_CANDIDATES = 3

CandidateKind = Literal[
    "favorite",
    "preference",
    "routine",
    "response_preference",
]


@dataclass(frozen=True, slots=True)
class AutoCaptureCandidate:
    """A high-confidence personal-memory value ready for durable storage."""

    kind: CandidateKind
    key: str
    value: str
    canonical_text: str
    rule_id: str


_SPACE_RE = re.compile(r"\s+")
_URL_RE = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)
_EMAIL_RE = re.compile(r"\b[^\s@]+@[^\s@]+\.[^\s@]+\b")
_ASSIGNMENT_RE = re.compile(r"\b[A-Za-z_]\w*\s*(?::=|\+=|-=|\*=|/=|=)\s*\S")
_SECRET_RE = re.compile(
    r"(?:"
    r"\b(?:api[-_ ]?key|access[-_ ]?token|refresh[-_ ]?token|private[-_ ]?key|"
    r"password|passwd|client[-_ ]?secret|authorization|bearer)\b|"
    r"\b(?:sk|rk|pk)-(?:proj-)?[A-Za-z0-9_-]{12,}|"
    r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b|"
    r"\b(?:ghp|gho|ghu|ghs|github_pat)_[A-Za-z0-9_]{16,}\b|"
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----|"
    r"\[REDACTED\]"
    r")",
    re.IGNORECASE,
)
_LONG_NUMBER_RE = re.compile(r"(?<!\d)(?:\d[ -]?){13,19}(?!\d)")
_PHONE_RE = re.compile(r"(?<!\d)(?:\+?\d[ .()-]?){8,}(?!\d)")
_SENSITIVE_RE = re.compile(
    r"\b(?:"
    r"social security|ssn|passport(?: number)?|driver'?s licen[cs]e|"
    r"bank account|routing number|credit card|debit card|home address|"
    r"street address|phone number|email address|"
    r"medical condition|health condition|diagnos(?:is|ed)|medication|"
    r"prescription|psych(?:iatry|iatrist|ological)|therapy|therapist|"
    r"pregnan(?:t|cy)|disabilit(?:y|ies)|"
    r"religion|religious beliefs?|political (?:party|beliefs?|affiliation)|"
    r"sexual orientation|gender identity|racial identity|ethnicity"
    r")\b",
    re.IGNORECASE,
)
_HYPOTHETICAL_RE = re.compile(
    r"\b(?:if|maybe|perhaps|hypothetically|suppose|supposing|imagine|"
    r"would|could|might|wish(?:ed)?)\b",
    re.IGNORECASE,
)
_QUESTION_START_RE = re.compile(
    r"^(?:who|what|when|where|why|how|can|could|would|should|do|does|did|"
    r"is|are|am|will|may)\b",
    re.IGNORECASE,
)
_CODE_RE = re.compile(
    r"(?:```|`[^`]+`|^\s*(?:def|class|function|SELECT|INSERT|UPDATE|DELETE|"
    r"import|from\s+\S+\s+import|npm|pip|git|curl|sudo)\b|"
    r"\b(?:print|console\.log)\s*\(|[{}\[\]]|(?:&&|\|\|))",
    re.IGNORECASE | re.MULTILINE,
)
_QUOTED_RE = re.compile(r"[\"“”]|(?:^|[\s(])'[^'\n]{2,}'(?:$|[\s).,!?])")
_IMPERATIVE_RE = re.compile(
    r"^(?:please\s+)?(?:remember|save|store|forget|delete|run|execute|install|"
    r"deploy|build|fix|write|read|open|create|add|remove|send|call|set|tell|"
    r"show|explain|make|change|update|use)\b",
    re.IGNORECASE,
)
_CONTEXTUAL_RE = re.compile(
    r"\b(?:this|these|those|here|now|today|tonight|tomorrow|currently|"
    r"lately|recently|right now|at the moment|for now|this week|this month|"
    r"project|repo(?:sitory)?|codebase|task|ticket|pull request|conversation|"
    r"chat|session|workspace)\b",
    re.IGNORECASE,
)
_UNRESOLVED_PRONOUN_RE = re.compile(
    r"\b(?:it|this|that|these|those|you|your|yours|we|our|ours|my|mine)\b",
    re.IGNORECASE,
)
_CLAUSE_RE = re.compile(
    r"\b(?:because|although|unless|except|but|however|which|who|whose)\b",
    re.IGNORECASE,
)
_CONVERSATIONAL_PREFIX_RE = re.compile(
    r"^(?:(?:and|also|oh|well)\s+|(?:by\s+the\s+way|btw)\s*,?\s+)(?=i\b|my\b)",
    re.IGNORECASE,
)

_DAY = r"(?:mondays?|tuesdays?|wednesdays?|thursdays?|fridays?|saturdays?|sundays?)"
_CADENCE_PREFIX = rf"(?:on\s+)?{_DAY}|(?:every|each)\s+(?:{_DAY}|day|morning|afternoon|evening|night|weekday|weekend|week|month)"
_CADENCE_SUFFIX = rf"(?:on\s+{_DAY}|(?:every|each)\s+(?:{_DAY}|day|morning|afternoon|evening|night|weekday|weekend|week|month))"

_COMPOUND_FAVORITE_RE = re.compile(
    r"^i\s+(?:(?:really|truly|absolutely)\s+)?(?:like|love|adore)\s+"
    r"(?P<value>.+?)\s+it'?s\s+(?:truly\s+)?my\s+"
    r"(?:favorite|favourite)\s+(?P<category>[a-z][a-z -]{0,38})\s+and\s+"
    r"(?:my\s+)?dream\s+(?P<dream_category>[a-z][a-z -]{0,38})$",
    re.IGNORECASE,
)
_FAVORITE_RE = re.compile(
    r"^my\s+(?:favorite|favourite)\s+(?P<category>[a-z][a-z -]{0,38})\s+"
    r"is\s+(?P<value>.+)$",
    re.IGNORECASE,
)
_REVERSE_FAVORITE_RE = re.compile(
    r"^(?P<value>.+?)\s+is\s+my\s+(?:favorite|favourite)\s+"
    r"(?P<category>[a-z][a-z -]{0,38})$",
    re.IGNORECASE,
)
_DREAM_RE = re.compile(
    r"^my\s+dream\s+(?P<category>[a-z][a-z -]{0,38})\s+is\s+"
    r"(?P<value>.+)$",
    re.IGNORECASE,
)
_ROUTINE_PREFIX_RE = re.compile(
    rf"^(?P<cadence>{_CADENCE_PREFIX})\s*,?\s+i\s+"
    r"(?:(?P<marker>like to|love to|prefer to|usually|always|typically|regularly)\s+)?"
    r"(?P<activity>.+)$",
    re.IGNORECASE,
)
_ROUTINE_USUAL_RE = re.compile(
    r"^i\s+(?P<marker>usually|always|typically|regularly)\s+"
    r"(?P<activity>.+)$",
    re.IGNORECASE,
)
_ROUTINE_SUFFIX_RE = re.compile(
    rf"^i\s+(?:(?P<marker>like to|love to|prefer to)\s+)?"
    rf"(?P<activity>.+?)\s+(?P<cadence>{_CADENCE_SUFFIX})$",
    re.IGNORECASE,
)
_LIKE_RE = re.compile(
    r"^i\s+(?:(?:really|truly|absolutely)\s+)?"
    r"(?P<verb>like|love|enjoy|adore|prefer)\s+(?P<value>.+)$",
    re.IGNORECASE,
)

_RESPONSE_PATTERNS = (
    re.compile(
        r"^i\s+prefer\s+(?P<style>[a-z][a-z -]{1,50})\s+"
        r"(?:answers|responses|replies)$",
        re.IGNORECASE,
    ),
    re.compile(
        r"^i\s+prefer\s+(?:answers|responses|replies)\s+"
        r"(?:(?:that are|to be|with|using|in)\s+)?(?P<style>.+)$",
        re.IGNORECASE,
    ),
    re.compile(
        r"^i\s+prefer\s+(?:you|the assistant)\s+to\s+"
        r"(?:answer|respond|reply)\s+(?:(?:in|with|using)\s+)?(?P<style>.+)$",
        re.IGNORECASE,
    ),
    re.compile(
        r"^please\s+always\s+(?:answer|respond|reply)\s+"
        r"(?:(?:in|with|using)\s+)?(?P<style>.+)$",
        re.IGNORECASE,
    ),
    re.compile(
        r"^please\s+always\s+keep\s+(?:your\s+)?"
        r"(?:answers|responses|replies)\s+(?P<style>.+)$",
        re.IGNORECASE,
    ),
    re.compile(
        r"^always\s+use\s+(?P<style>.+?)\s+in\s+(?:your\s+)?"
        r"(?:answers|responses|replies)$",
        re.IGNORECASE,
    ),
)
_NO_RESPONSE_FEATURE_RE = re.compile(
    r"^(?:please\s+)?(?:do not|don't|never)\s+use\s+(?P<style>.+?)\s+in\s+"
    r"(?:your\s+)?(?:answers|responses|replies)$",
    re.IGNORECASE,
)
_DIRECT_RESPONSE_STYLE_RE = re.compile(
    r"^i\s+prefer\s+(?P<style>concise|brief|short|detailed|thorough|"
    r"comprehensive|direct|clear|friendly|formal|casual|technical|"
    r"structured|actionable)\s+(?:answers|responses|replies)?$",
    re.IGNORECASE,
)

_RESPONSE_DIMENSIONS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "verbosity",
        ("concise", "brief", "short", "detailed", "thorough", "comprehensive"),
    ),
    ("format", ("bullet", "list", "table", "markdown", "heading", "structured")),
    ("citations", ("citation", "source", "evidence")),
    ("emoji", ("emoji", "emojis")),
    ("code_format", ("indentation", "code block", "code blocks")),
    (
        "style",
        (
            "direct",
            "clear",
            "friendly",
            "formal",
            "casual",
            "technical",
            "plain language",
            "simple language",
            "actionable",
        ),
    ),
)


def classify_prompt(
    text: str,
    *,
    redaction_changed: bool = False,
) -> tuple[AutoCaptureCandidate, ...]:
    """Return bounded, high-confidence memories from one current user prompt.

    ``redaction_changed`` must be set when a caller's secret scrubber changed the
    prompt.  Rejecting the whole prompt avoids storing a misleading partial fact.
    """

    if redaction_changed or not isinstance(text, str):
        return ()
    normalized = _normalize_prompt(text)
    if not _fundamentally_safe(normalized):
        return ()

    direct = _strip_conversational_prefix(normalized)

    response = _classify_response_preference(direct)
    if response is not None:
        return (response,)

    compound = _classify_compound_favorite(direct)
    if compound is not None:
        return (compound,)

    sentences = _sentences(normalized)
    if not sentences or len(sentences) > MAX_AUTOCAPTURE_CANDIDATES:
        return ()
    if any(_IMPERATIVE_RE.match(sentence) for sentence in sentences):
        return ()
    if _CONTEXTUAL_RE.search(normalized):
        return ()

    candidates: list[AutoCaptureCandidate] = []
    for sentence in sentences:
        sentence = _strip_conversational_prefix(sentence)
        candidate = (
            _classify_favorite(sentence)
            or _classify_response_preference(sentence)
            or _classify_routine(sentence)
            or _classify_like(sentence)
        )
        if candidate is None:
            return ()
        if not any(
            existing.key == candidate.key
            and existing.value.casefold() == candidate.value.casefold()
            for existing in candidates
        ):
            candidates.append(candidate)
    return tuple(candidates[:MAX_AUTOCAPTURE_CANDIDATES])


def _normalize_prompt(text: str) -> str:
    value = unicodedata.normalize("NFKC", text).strip()
    value = value.replace("\u2018", "'").replace("\u2019", "'")
    return value


def _fundamentally_safe(text: str) -> bool:
    if not text or len(text) > MAX_AUTOCAPTURE_CHARS:
        return False
    if "\n" in text or "\r" in text:
        return False
    if any(ord(character) < 32 and character not in "\t" for character in text):
        return False
    question_start = _QUESTION_START_RE.match(text)
    negative_directive = re.match(r"^(?:do not|don't|never)\b", text, re.IGNORECASE)
    if "?" in text or (question_start and not negative_directive):
        return False
    if _URL_RE.search(text) or _EMAIL_RE.search(text):
        return False
    if _ASSIGNMENT_RE.search(text) or _CODE_RE.search(text) or _QUOTED_RE.search(text):
        return False
    if (
        _SECRET_RE.search(text)
        or _LONG_NUMBER_RE.search(text)
        or _PHONE_RE.search(text)
    ):
        return False
    if _SENSITIVE_RE.search(text) or _HYPOTHETICAL_RE.search(text):
        return False
    return True


def _sentences(text: str) -> tuple[str, ...]:
    parts = re.split(r"(?<=[.!])\s+", text)
    cleaned = tuple(_clean_fragment(part) for part in parts if _clean_fragment(part))
    return cleaned


def _clean_fragment(value: str) -> str:
    return _SPACE_RE.sub(" ", value).strip(" \t.,!;:")


def _strip_conversational_prefix(value: str) -> str:
    """Remove one harmless discourse marker before a first-person claim."""

    return _CONVERSATIONAL_PREFIX_RE.sub("", value, count=1)


def _safe_field(value: str, *, max_words: int = 20) -> bool:
    if not 2 <= len(value) <= 180 or len(value.split()) > max_words:
        return False
    if _UNRESOLVED_PRONOUN_RE.search(value) or _CLAUSE_RE.search(value):
        return False
    if _SENSITIVE_RE.search(value) or _SECRET_RE.search(value):
        return False
    if re.search(r"[;:{}<>]", value):
        return False
    return True


def _classify_compound_favorite(text: str) -> AutoCaptureCandidate | None:
    match = _COMPOUND_FAVORITE_RE.fullmatch(_clean_fragment(text))
    if match is None:
        return None
    category = _clean_fragment(match.group("category")).lower()
    dream_category = _clean_fragment(match.group("dream_category")).lower()
    value = _display_value(match.group("value"))
    if category != dream_category or not _safe_field(category, max_words=4):
        return None
    if not _safe_field(value):
        return None
    return AutoCaptureCandidate(
        kind="favorite",
        key=f"favorite:{_slug(category)}",
        value=value,
        canonical_text=f"The user's favorite and dream {category} is {value}.",
        rule_id="favorite.compound.v1",
    )


def _classify_favorite(text: str) -> AutoCaptureCandidate | None:
    cleaned = _clean_fragment(text)
    match = _FAVORITE_RE.fullmatch(cleaned) or _REVERSE_FAVORITE_RE.fullmatch(cleaned)
    dream = False
    if match is None:
        match = _DREAM_RE.fullmatch(cleaned)
        dream = match is not None
    if match is None:
        return None
    category = _clean_fragment(match.group("category")).lower()
    value = _display_value(match.group("value"))
    if not _safe_field(category, max_words=4) or not _safe_field(value):
        return None
    label = f"dream {category}" if dream else f"favorite {category}"
    return AutoCaptureCandidate(
        kind="favorite",
        key=f"{'dream' if dream else 'favorite'}:{_slug(category)}",
        value=value,
        canonical_text=f"The user's {label} is {value}.",
        rule_id="favorite.dream.v1" if dream else "favorite.explicit.v1",
    )


def _classify_response_preference(text: str) -> AutoCaptureCandidate | None:
    cleaned = _clean_fragment(text)
    negative = _NO_RESPONSE_FEATURE_RE.fullmatch(cleaned)
    if negative is not None:
        style = _clean_fragment(negative.group("style")).lower()
        return _response_candidate(style, negative=True)

    direct = _DIRECT_RESPONSE_STYLE_RE.fullmatch(cleaned)
    if direct is not None:
        return _response_candidate(_clean_fragment(direct.group("style")).lower())

    for pattern in _RESPONSE_PATTERNS:
        match = pattern.fullmatch(cleaned)
        if match is None:
            continue
        style = _clean_fragment(match.group("style")).lower()
        return _response_candidate(style)
    return None


def _response_candidate(
    style: str,
    *,
    negative: bool = False,
) -> AutoCaptureCandidate | None:
    if not _safe_field(style, max_words=7):
        return None
    dimension = ""
    for candidate_dimension, needles in _RESPONSE_DIMENSIONS:
        if any(needle in style for needle in needles):
            dimension = candidate_dimension
            break
    if not dimension:
        return None

    if negative:
        value = f"without {style}"
        canonical = f"The user prefers assistant responses without {style}."
    elif dimension == "verbosity" or style in {
        "direct",
        "clear",
        "friendly",
        "formal",
        "casual",
        "technical",
        "structured",
        "actionable",
    }:
        value = style
        canonical = f"The user prefers {style} assistant responses."
    elif style in {"plain language", "simple language"}:
        value = f"in {style}"
        canonical = f"The user prefers assistant responses in {style}."
    else:
        value = f"with {style}"
        canonical = f"The user prefers assistant responses with {style}."
    return AutoCaptureCandidate(
        kind="response_preference",
        key=f"assistant_response:{dimension}",
        value=value,
        canonical_text=canonical,
        rule_id="response_preference.explicit.v1",
    )


def _classify_routine(text: str) -> AutoCaptureCandidate | None:
    cleaned = _clean_fragment(text)
    match = _ROUTINE_PREFIX_RE.fullmatch(cleaned)
    if match is None:
        match = _ROUTINE_USUAL_RE.fullmatch(cleaned)
    if match is None:
        match = _ROUTINE_SUFFIX_RE.fullmatch(cleaned)
    if match is None:
        return None

    marker = (match.groupdict().get("marker") or "").lower()
    activity = _clean_fragment(match.group("activity"))
    cadence = _clean_fragment(match.groupdict().get("cadence") or "")
    if not _safe_field(activity) or (cadence and not _safe_field(cadence, max_words=4)):
        return None
    if re.match(
        r"^(?:ask|tell|make|have|get)\s+(?:you|the assistant)\b",
        activity,
        re.IGNORECASE,
    ):
        return None

    activity = _normalize_times(activity)
    cadence_suffix = _cadence_suffix(cadence)
    value = f"{activity}{' ' + cadence_suffix if cadence_suffix else ''}"
    if marker in {"like to", "love to", "prefer to"}:
        verb = {
            "like to": "likes to",
            "love to": "loves to",
            "prefer to": "prefers to",
        }[marker]
        canonical = f"The user {verb} {value}."
    else:
        canonical = f"The user's recurring routine is to {value}."

    cadence_key = _slug(cadence or marker or "usual")
    activity_key = _activity_key(activity)
    return AutoCaptureCandidate(
        kind="routine",
        key=f"routine:{cadence_key}:{activity_key}",
        value=value,
        canonical_text=canonical,
        rule_id="routine.explicit.v1",
    )


def _classify_like(text: str) -> AutoCaptureCandidate | None:
    match = _LIKE_RE.fullmatch(_clean_fragment(text))
    if match is None:
        return None
    verb = match.group("verb").lower()
    value = _display_value(match.group("value"))
    if not _safe_field(value):
        return None
    if re.search(r"\b(?:my favorite|my favourite|dream car)\b", value, re.IGNORECASE):
        return None

    predicate = {
        "like": "likes",
        "love": "loves",
        "enjoy": "enjoys",
        "adore": "adores",
        "prefer": "prefers",
    }[verb]
    return AutoCaptureCandidate(
        kind="preference",
        key=f"preference:{verb}:{_slug(value)}",
        value=value,
        canonical_text=f"The user {predicate} {value}.",
        rule_id=f"preference.{verb}.v1",
    )


def _display_value(value: str) -> str:
    cleaned = _clean_fragment(value)
    cleaned = re.sub(r"^the\s+", "", cleaned, flags=re.IGNORECASE)
    year = re.fullmatch(r"(.+?)\s+from\s+((?:19|20)\d{2})", cleaned, re.IGNORECASE)
    if year is not None:
        cleaned = f"{year.group(2)} {year.group(1)}"
    cleaned = _normalize_times(cleaned)
    replacements = {
        r"\bporsche\b": "Porsche",
        r"\bmercedes(?:-benz)?\b": "Mercedes-Benz",
        r"\bturbo s\b": "Turbo S",
        r"\bpython\b": "Python",
        r"\bjavascript\b": "JavaScript",
        r"\btypescript\b": "TypeScript",
        r"\brust\b": "Rust",
    }
    for pattern, replacement in replacements.items():
        cleaned = re.sub(pattern, replacement, cleaned, flags=re.IGNORECASE)
    return cleaned


def _normalize_times(value: str) -> str:
    def replace(match: re.Match[str]) -> str:
        hour = str(int(match.group("hour")))
        minute = match.group("minute")
        meridiem = match.group("meridiem").lower().replace(".", "").replace(" ", "")
        clock = f"{hour}:{minute}" if minute else hour
        return f"{clock} {meridiem[0]}.m."

    return re.sub(
        r"\b(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*"
        r"(?P<meridiem>a\.?\s*m\.?|p\.?\s*m\.?)\b",
        replace,
        value,
        flags=re.IGNORECASE,
    )


def _cadence_suffix(cadence: str) -> str:
    value = _clean_fragment(cadence)
    if not value:
        return ""
    lowered = value.lower()
    day_match = re.fullmatch(rf"(?:on\s+)?({_DAY})", lowered, re.IGNORECASE)
    if day_match is not None:
        day = day_match.group(1).lower()
        if not day.endswith("s"):
            day += "s"
        return f"on {day.capitalize()}"
    return lowered


def _activity_key(activity: str) -> str:
    ignored = {"a", "an", "the", "to", "at", "in", "on", "with", "for", "of"}
    words = [
        word
        for word in re.findall(r"[a-z0-9]+", activity.casefold())
        if word not in ignored and not word.isdigit()
    ]
    return _slug("-".join(words[:2]) or activity)


def _slug(value: str) -> str:
    ascii_value = (
        unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    )
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_value.casefold()).strip("-")
    if not slug:
        slug = "value"
    if len(slug) <= 80:
        return slug
    digest = hashlib.sha256(slug.encode("utf-8")).hexdigest()[:10]
    return f"{slug[:69].rstrip('-')}-{digest}"


__all__ = [
    "AutoCaptureCandidate",
    "CandidateKind",
    "MAX_AUTOCAPTURE_CANDIDATES",
    "MAX_AUTOCAPTURE_CHARS",
    "classify_prompt",
]
