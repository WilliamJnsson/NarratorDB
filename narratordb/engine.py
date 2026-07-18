"""NarratorDB retrieval engine.

Canonical SQLite/FTS5 + semantic retrieval engine.
New integrations should import this module through :mod:`narratordb`.

API:
    engine = Engine(db_path, user_id)
    engine.store(speaker, text, timestamp?)
    engine.search(query, limit?, after?, before?)
    engine.delete(message_id)
    engine.stats()
    engine.cleanup(max_age_days?, max_messages?)
"""

import sqlite3
import hashlib
import io
import json
import math
import os
import re
import time
import threading
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Optional, Sequence

from .config import default_db_path
from .intelligence import (
    CLAIM_FTS_FORMAT_VERSION,
    DERIVED_SCHEMA_VERSION,
    ContextBlock,
    ContextBundle,
    DerivedMemoryStore,
    _aggregation_query_action_surface_terms,
    _aggregation_query_action_tokens,
    _aggregation_query_focus_terms,
    _aggregation_query_uses_embedded_action,
)


ENGINE_NAME = "NarratorDB"
DEFAULT_EMBEDDING_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
FTS_FORMAT_VERSION = 2
ARTIFACT_FTS_FORMAT_VERSION = 1
CODE_CHUNK_FTS_FORMAT_VERSION = 1
SCHEMA_VERSION = 3
NORMALIZED_TERMS_VERSION = 1

_EMBEDDING_CACHE: dict[str, object] = {}
_EMBEDDING_CACHE_LOCK = threading.Lock()
# The shared SentenceTransformer forward pass is not thread-safe on every
# torch backend (concurrent encodes on Apple MPS can segfault the process).
# All encode calls serialize on this process-wide lock; the pass itself is
# milliseconds, so contention stays negligible.
_ENCODE_LOCK = threading.Lock()


def _encode_texts(model, texts, **kwargs):
    with _ENCODE_LOCK:
        return model.encode(texts, **kwargs)


def _candidate_embedding_paths() -> list[str]:
    explicit = os.getenv("NARRATORDB_EMBEDDING_MODEL_DIR")
    candidates: list[str] = []
    if explicit:
        candidates.append(os.path.expanduser(explicit))

    home = Path.home()
    sentence_transformers_cache = (
        home
        / ".cache"
        / "torch"
        / "sentence_transformers"
        / "sentence-transformers_all-MiniLM-L6-v2"
    )
    if sentence_transformers_cache.exists():
        candidates.append(str(sentence_transformers_cache))

    hf_snapshot_root = (
        home
        / ".cache"
        / "huggingface"
        / "hub"
        / "models--sentence-transformers--all-MiniLM-L6-v2"
        / "snapshots"
    )
    if hf_snapshot_root.exists():
        for snapshot in sorted(hf_snapshot_root.iterdir(), reverse=True):
            if snapshot.is_dir():
                candidates.append(str(snapshot))

    return candidates


def _load_sentence_transformer(*, local_only: bool = False):
    try:
        from sentence_transformers import SentenceTransformer
    except ImportError:
        return None, None

    for candidate in _candidate_embedding_paths():
        try:
            with _EMBEDDING_CACHE_LOCK:
                cached = _EMBEDDING_CACHE.get(candidate)
                if cached is None:
                    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                        cached = SentenceTransformer(candidate)
                    _EMBEDDING_CACHE[candidate] = cached
            return cached, candidate
        except Exception:
            continue

    env_local_only = os.getenv("NARRATORDB_LOCAL_ONLY", "")
    if local_only or env_local_only.lower() in {"1", "true", "yes"}:
        return None, None

    model_id = os.getenv("NARRATORDB_EMBEDDING_MODEL", DEFAULT_EMBEDDING_MODEL)
    try:
        with _EMBEDDING_CACHE_LOCK:
            cached = _EMBEDDING_CACHE.get(model_id)
            if cached is None:
                with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    cached = SentenceTransformer(model_id)
                _EMBEDDING_CACHE[model_id] = cached
        return cached, model_id
    except Exception:
        return None, None


# ── Snowball-lite stemmer (covers English well, extensible) ──
# For production: replace with PyStemmer (Snowball) which covers 20+ languages

_IRREGULAR = {
    "ran": "run",
    "ate": "eat",
    "went": "go",
    "gone": "go",
    "bought": "buy",
    "built": "build",
    "came": "come",
    "did": "do",
    "done": "do",
    "drew": "draw",
    "fell": "fall",
    "felt": "feel",
    "found": "find",
    "gave": "give",
    "got": "get",
    "grew": "grow",
    "had": "have",
    "heard": "hear",
    "held": "hold",
    "knew": "know",
    "led": "lead",
    "left": "leave",
    "lost": "lose",
    "made": "make",
    "met": "meet",
    "paid": "pay",
    "put": "put",
    "read": "read",
    "said": "say",
    "saw": "see",
    "sent": "send",
    "set": "set",
    "sat": "sit",
    "sold": "sell",
    "spoke": "speak",
    "spent": "spend",
    "stood": "stand",
    "took": "take",
    "taught": "teach",
    "told": "tell",
    "thought": "think",
    "threw": "throw",
    "understood": "understand",
    "won": "win",
    "wore": "wear",
    "wrote": "write",
    "broken": "break",
    "chosen": "choose",
    "driven": "drive",
    "eaten": "eat",
    "fallen": "fall",
    "frozen": "freeze",
    "given": "give",
    "hidden": "hide",
    "known": "know",
    "spoken": "speak",
    "stolen": "steal",
    "taken": "take",
    "written": "write",
    "children": "child",
    "women": "woman",
    "men": "man",
    "people": "person",
    "mice": "mouse",
    "teeth": "tooth",
    "feet": "foot",
}


@lru_cache(maxsize=131_072)
def stem(word: str) -> str:
    w = word.lower().strip("'\".,!?;:()")
    if not w or len(w) < 3:
        return w
    if w in _IRREGULAR:
        return _IRREGULAR[w]
    # Suffix rules (order matters — longest first)
    for suffix, repl in [
        ("ational", "ate"),
        ("tional", "tion"),
        ("enci", "ence"),
        ("anci", "ance"),
        ("izer", "ize"),
        ("ating", "ate"),
        ("iting", "ite"),
        ("uting", "ute"),
        ("bling", "ble"),
        ("ling", "le"),
        ("ving", "ve"),
        ("fulness", "ful"),
        ("ousness", "ous"),
        ("iveness", "ive"),
        ("ment", ""),
        ("ness", ""),
        ("tion", "t"),
        ("sion", "s"),
        ("ible", ""),
        ("able", ""),
        ("ries", "ry"),
        ("ies", "y"),
        ("ied", "y"),
        ("ying", "y"),
        ("ting", "t"),
        ("ning", "n"),
        ("ping", "p"),
        ("bing", "b"),
        ("ding", "d"),
        ("ging", "g"),
        ("ming", "m"),
        ("lled", "ll"),
        ("rred", "r"),
        ("tted", "t"),
        ("pped", "p"),
        ("nned", "n"),
        ("gged", "g"),
        ("mmed", "m"),
        ("dded", "d"),
        ("ing", ""),
        ("ings", ""),
        ("ated", "ate"),
        ("ited", "ite"),
        ("uted", "ute"),
        ("ed", ""),
        ("er", ""),
        ("es", ""),
        ("ly", ""),
        ("s", ""),
    ]:
        if w.endswith(suffix) and len(w) - len(suffix) + len(repl) >= 3:
            return w[: -len(suffix)] + repl
    return w


# ── Stop words (English default, override per language) ──
STOP_WORDS_EN = frozenset(
    "a about after all also am an and any are as at be been before being between both but by "
    "can could did do does doing down during each few for from further get got had has have "
    "having he her here hers herself him himself his how i if in into is it its itself just "
    "let like may me might more most my myself no nor not now of off on once only or other "
    "our ours ourselves out over own re s same she should so some still such t than that the "
    "their theirs them themselves then there these they this those through to too under until "
    "up very was we were what when where which while who whom why will with would you your "
    "yours yourself yourselves been being go going gone went has have had did does do was were "
    "is are am".split()
)


# ── Query-intent and evidence patterns (compiled once at import) ──
# Query-side patterns run against query.lower(); evidence-side patterns keep
# their original case handling from the rerank block they were hoisted out of.
_QUANTITY_QUERY_RE = re.compile(
    r"\b(how many|number of|total|count(?:ed|ing)?|combined)\b"
)
_AGGREGATION_EXPLICIT_RE = re.compile(
    r"\b(?:total|combined|altogether|in\s+all|overall|cumulative|"
    r"sum(?:med|ming)?|add(?:ed|ing)?\s+up|aggregate(?:d|s|ing)?)\b"
)
_AGGREGATION_COUNT_RE = re.compile(r"\b(?:how\s+many|number\s+of|count(?:ed|ing)?)\b")
_AGGREGATION_PAST_ACTION_RE = re.compile(
    r"\b(?:did\s+(?:i|we)|have\s+(?:i|we)|had\s+(?:i|we)|"
    r"times?|events?|sessions?|to\s+date|so\s+far)\b"
)
_RELATIVE_TIME_UNIT = (
    r"(?:(?:business|calendar|working)\s+)?"
    r"(?:seconds?|minutes?|hours?|days?|weeks?|months?|quarters?|years?)"
)
_RELATIVE_EVENT_DISTANCE_RE = re.compile(
    rf"(?:\bhow\s+many\s+{_RELATIVE_TIME_UNIT}\s+"
    r"(?:ago|earlier|later|before|after|between|since)\b|"
    r"\bhow\s+much\s+(?:time|duration)\s+"
    r"(?:ago|earlier|later|before|after|between|since)\b|"
    rf"\b(?:total(?:\s+number\s+of)?|combined(?:\s+number\s+of)?|"
    rf"number\s+of)\s+{_RELATIVE_TIME_UNIT}\s+"
    r"(?:between|from|since|before|after)\b|"
    r"\b(?:total\s+)?(?:elapsed\s+(?:time|duration)|"
    r"(?:time|duration)\s+elapsed)\s+(?:between|from|since)\b|"
    r"\b(?:total\s+)?(?:time|duration)\s+(?:between|from)\b)"
)
_HOW_MUCH_QUERY_RE = re.compile(r"\bhow\s+much\b")
_PENDING_OBLIGATION_COUNT_RE = re.compile(
    r"\bhow\s+many\b.*(?:"
    r"\b(?:do|does)\s+(?:i|we|the\s+user)\s+"
    r"(?:still\s+)?(?:need|have)\s+to\b|"
    r"\b(?:need|needs|required)\s+to\s+be\b"
    r")"
)
_CUMULATIVE_SNAPSHOT_COUNT_RE = re.compile(r"\bhow\s+many\s+times\s+have\s+(?:i|we)\b")
_CURRENT_STATE_COUNT_RE = re.compile(
    r"\b(?:do\s+(?:i|we)\s+have|(?:is|are)\s+there|remaining|left|"
    r"(?:i|we)\s+(?:have|own)|user\s+(?:has|owns))\b"
)
_AGGREGATION_SOURCE_OVERFETCH = 96
_MEASUREMENT_QUANTITY_RE = re.compile(
    r"\b(?:seconds?|minutes?|hours?|days?|weeks?|months?|years?|"
    r"pages?|amount|money|dollars?|cost|price|miles?|kilometers?)\b"
)
_MONEY_INTENT_RE = re.compile(
    r"\b(cost|price|pricing|charge|charged|pay|paid|dollar|monthly|per month)\b"
)
_CURRENT_INTENT_RE = re.compile(r"\b(now|current|currently|latest|today)\b")
_HISTORICAL_MARKER_RE = re.compile(r"\b(before|previous|prior|earlier)\b")
_PAST_TARGET_RE = re.compile(
    r"^\s*(?:what|which|who|where|how)\s+(?:was|were|did|had)\b"
)
_CODE_LOCATOR_WH_RE = re.compile(r"\b(where|which|what)\b")
_CODE_LOCATOR_NOUN_RE = re.compile(
    r"\b(module|file|function|component|service|class|route|handler|method|symbol)\b"
)
_CODE_LOCATOR_VERB_RE = re.compile(
    r"\b(implemented|defined|handles|handle|deals with|deal with|renders?|rendering|wired?|patched?)\b"
)
_AUDIT_INTENT_RE = re.compile(
    r"\b(review|verdict|approval|approve|approved|failed|failure|risk|security|issue|audit)\b"
)
_MONEY_EVIDENCE_RE = re.compile(
    r"\$[\d,.]+|\b\d+\s+dollars?\b|\b(price|pricing|charge|charged|monthly|per month)\b",
    re.IGNORECASE,
)
_QUANTITY_FACT_RE = re.compile(
    r"\b(?:currently\s+has|currently\s+have|i(?:'ve|\s+have)|we(?:'ve|\s+have)|"
    r"there\s+(?:is|are)|contains?|which\s+has|that\s+has)\b|"
    r"\b(?:one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|"
    r"\d+)\s+times?\s+(?:now|so\s+far|to\s+date)\b",
    re.IGNORECASE,
)
_DATE_EVIDENCE_RE = re.compile(
    r"\b(?:\d{1,2}\s+(?:january|february|march|april|may|june|july|august|september|october|november|december)|"
    r"(?:january|february|march|april|may|june|july|august|september|october|november|december)\s+\d{1,2}|20\d{2})\b",
    re.IGNORECASE,
)
_CODE_EVIDENCE_RE = re.compile(
    r"[A-Za-z0-9_]+\.(?:ts|tsx|js|jsx|py|rs|go|java|kt|json|yaml|yml|toml|md)\b|"
    r"\b[a-z]+[A-Z][A-Za-z0-9]+\b|"
    r"\b(component|service|handler|middleware|module|function|class|route|render|popout|dialog|modal|patch(?:ed)?|implement(?:ed)?)\b",
    re.IGNORECASE,
)
_AUDIT_EVIDENCE_RE = re.compile(
    r"\b(review|reviewer|verdict|fail|failed|approval|security|risk|issue|audit|csp)\b",
    re.IGNORECASE,
)
_NUMERIC_MENTION_RE = re.compile(
    r"\b(?:\d+(?:[.,]\d+)?|one|two|three|four|five|six|seven|"
    r"eight|nine|ten|eleven|twelve)\b"
)


def _aggregation_query_mode(query: str) -> str | None:
    """Conservatively identify cumulative evidence requests.

    Current-state counts deliberately remain on the established retrieval
    path. Count wording without an explicit aggregate marker also needs a
    past/action signal, which avoids changing ordinary inventory questions.
    """

    normalized = str(query or "").casefold()
    if _PENDING_OBLIGATION_COUNT_RE.search(normalized):
        return "pending"
    if (
        _CURRENT_INTENT_RE.search(normalized)
        or _CURRENT_STATE_COUNT_RE.search(normalized)
        or _CUMULATIVE_SNAPSHOT_COUNT_RE.search(normalized)
        or _RELATIVE_EVENT_DISTANCE_RE.search(normalized)
    ):
        return None
    if _AGGREGATION_EXPLICIT_RE.search(normalized):
        return "completed"
    if (
        _HOW_MUCH_QUERY_RE.search(normalized)
        and _AGGREGATION_PAST_ACTION_RE.search(normalized)
        and _aggregation_query_uses_embedded_action(normalized)
    ):
        return "completed"
    if _AGGREGATION_COUNT_RE.search(normalized) and _AGGREGATION_PAST_ACTION_RE.search(
        normalized
    ):
        return "completed"
    return None


def _aggregation_query_intent(query: str) -> bool:
    """Backward-compatible boolean view of the aggregation query planner."""

    return _aggregation_query_mode(query) is not None


def _aggregation_lexical_surface_terms(query: str) -> tuple[str, ...]:
    """Build bounded inflections for aggregation-only lexical overfetch."""

    focused_terms = _aggregation_query_focus_terms(query)
    variants: list[str] = []
    seen: set[str] = set()

    def add(value: str) -> None:
        normalized = str(value or "").casefold()
        if normalized.isalnum() and len(normalized) >= 2 and normalized not in seen:
            seen.add(normalized)
            variants.append(normalized)

    for term in focused_terms[:12]:
        add(term)
        base = stem(term)
        add(base)
        for irregular, normalized in _IRREGULAR.items():
            if normalized == term or normalized == base:
                add(irregular)
        for root in (term, base):
            if not root.isalpha() or len(root) < 3:
                continue
            if root.endswith("e"):
                add(root + "d")
                add(root[:-1] + "ing")
                add(root + "s")
            else:
                add(root + "ed")
                add(root + "ing")
                add(root + "s")
    for term in _aggregation_query_action_surface_terms(query):
        add(term)
        add(stem(term))
    return tuple(variants[:64])


@dataclass
class Message:
    id: int
    speaker: str
    text: str
    timestamp: float
    position: int
    provenance: dict = field(default_factory=dict)


@dataclass
class SearchResult:
    messages: list[Message]
    query_ms: float
    total_matches: int
    direct_hits: list[Message] = field(default_factory=list)
    context_messages: list[Message] = field(default_factory=list)
    timings_ms: dict[str, float] = field(default_factory=dict)
    # Fused rerank score per direct hit (aligned with direct_hits). Unlike
    # positional adapter scores, these are the engine's real confidences.
    scores: list[float] = field(default_factory=list)


@dataclass
class MemoryBlockSearchResult:
    """Budgetless ranked memory candidates for top-k retrieval adapters."""

    blocks: list[ContextBlock]
    query_ms: float
    total_matches: int
    timings_ms: dict[str, float] = field(default_factory=dict)


@dataclass
class Artifact:
    id: int
    kind: str
    title: str
    content: str
    summary: str
    timestamp: float
    tags: list[str] = field(default_factory=list)
    provenance: dict = field(default_factory=dict)


@dataclass
class ArtifactSearchResult:
    artifacts: list[Artifact]
    query_ms: float
    total_matches: int


@dataclass
class CodeChunk:
    id: int
    path: str
    language: str
    kind: str
    symbol: str
    content: str
    summary: str
    start_line: int | None
    end_line: int | None
    timestamp: float
    tags: list[str] = field(default_factory=list)
    provenance: dict = field(default_factory=dict)


@dataclass
class CodeChunkSearchResult:
    chunks: list[CodeChunk]
    query_ms: float
    total_matches: int


@dataclass
class Relation:
    id: int
    source_type: str
    source_id: int
    target_type: str
    target_id: int
    relation_type: str
    weight: float
    timestamp: float
    metadata: dict = field(default_factory=dict)


class Engine:
    def __init__(
        self,
        db_path: str | None = None,
        user_id: str = "default",
        stop_words: set = None,
        context_window: int = 5,
        semantic_dedup: bool = True,
        dedup_threshold: float = 0.82,
        dedup_window: int = 100,
        search_dedup_jaccard: float = 0.90,
        session_cap: int = 4,
        diversify_top_n: int = 20,
        temporal_cluster_select: bool = True,
        score_floor_ratio: float = 0.25,
        score_floor_min_results: int = 20,
        semantic_search_mode: str = "fallback_only",
        local_only: bool = False,
    ):
        self.db_path = os.path.expanduser(db_path or default_db_path())
        self.user_id = user_id
        self.stop_words = stop_words or STOP_WORDS_EN
        self._use_persisted_terms = frozenset(self.stop_words) == STOP_WORDS_EN
        self.context_window = context_window
        self.semantic_dedup = semantic_dedup
        self.dedup_threshold = dedup_threshold
        self.dedup_window = dedup_window
        # Retrieval-time result shaping (context precision; generic defaults,
        # no dataset-specific behavior)
        self.search_dedup_jaccard = search_dedup_jaccard
        self.session_cap = session_cap
        self.diversify_top_n = diversify_top_n
        self.temporal_cluster_select = temporal_cluster_select
        self.score_floor_ratio = score_floor_ratio
        self.score_floor_min_results = score_floor_min_results
        if semantic_search_mode not in {"fallback_only", "hybrid", "disabled"}:
            raise ValueError(
                "semantic_search_mode must be fallback_only, hybrid, or disabled"
            )
        self.semantic_search_mode = semantic_search_mode
        self.local_only = bool(local_only)
        self.engine_name = ENGINE_NAME
        self.embedding_source: Optional[str] = None
        # Per-user FTS5 table — each user gets an isolated FTS5 index so
        # scans are bounded by that user's corpus, not total DB size.
        # Table name: fts_u{16 hex chars} — alphanumeric only, safe in SQL identifiers.
        self._user_key = "u" + hashlib.md5(user_id.encode()).hexdigest()[:16]
        self._fts_table = f"fts_{self._user_key}"
        self._artifact_fts_table = f"artifact_fts_{self._user_key}"
        self._code_fts_table = f"code_fts_{self._user_key}"
        self._hashes: set = set()
        # Cache of recent message token sets for fast Jaccard similarity
        # Each entry: (stemmed_token_frozenset,)
        self._recent_tokens: list = []
        self._embedding_index_dirty = True
        self._embedding_matrix = None
        self._embedding_row_meta: list[tuple[int, int, float]] = []
        self._query_embedding_cache: dict = {}
        self._db_lock = threading.RLock()

        # Load NarratorDB's canonical embedding backend once per process.
        self._sbert = None
        if semantic_dedup:
            try:
                import numpy as np

                self._sbert, self.embedding_source = _load_sentence_transformer(
                    local_only=self.local_only
                )
                self._sbert_embeddings: list = []  # recent embeddings cache
                self._np = np
            except ImportError:
                self._sbert = None
                self.embedding_source = None
                pass  # Fall back to Jaccard

        parent = os.path.dirname(self.db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._conn = sqlite3.connect(
            self.db_path, timeout=30.0, check_same_thread=False
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA busy_timeout=30000")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA secure_delete=ON")
        # Memory-map the database file so read-heavy multi-scope workloads
        # (one engine per user against one large file) share the OS page
        # cache instead of paying per-connection page reads. Advisory only:
        # SQLite silently degrades where mmap is unavailable.
        self._conn.execute("PRAGMA mmap_size=2147483648")
        self._init_tables()
        self._memory = DerivedMemoryStore(self._conn, self.user_id, self._user_key)
        self._load_hashes()
        self._ensure_message_positions()
        self._load_recent_tokens()

    def _init_tables(self):
        # Drop old shared-FTS5 triggers — replaced by explicit per-user inserts
        self._conn.executescript("""
            DROP TRIGGER IF EXISTS msg_ai;
            DROP TRIGGER IF EXISTS msg_ad;

            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                speaker TEXT NOT NULL,
                text TEXT NOT NULL,
                normalized_terms TEXT,
                hash TEXT NOT NULL,
                timestamp REAL NOT NULL,
                position INTEGER NOT NULL,
                created_at TEXT DEFAULT (datetime('now'))
            );

            CREATE INDEX IF NOT EXISTS idx_msg_user ON messages(user_id);
            CREATE INDEX IF NOT EXISTS idx_msg_user_ts ON messages(user_id, timestamp);
            CREATE INDEX IF NOT EXISTS idx_msg_user_pos ON messages(user_id, position);
            CREATE INDEX IF NOT EXISTS idx_msg_hash ON messages(user_id, hash);

            CREATE TABLE IF NOT EXISTS embeddings (
                message_id INTEGER PRIMARY KEY,
                user_id TEXT NOT NULL,
                embedding BLOB NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_emb_user ON embeddings(user_id);

            CREATE TABLE IF NOT EXISTS message_provenance (
                message_id INTEGER PRIMARY KEY,
                user_id TEXT NOT NULL,
                provider TEXT,
                model_id TEXT,
                agent_id TEXT,
                run_id TEXT,
                workspace_id TEXT,
                tool_used TEXT,
                response_id TEXT,
                metadata_json TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_message_prov_user ON message_provenance(user_id);
            CREATE INDEX IF NOT EXISTS idx_message_prov_model ON message_provenance(user_id, model_id);
            CREATE INDEX IF NOT EXISTS idx_message_prov_agent ON message_provenance(user_id, agent_id);

            CREATE TABLE IF NOT EXISTS automatic_memories (
                user_id TEXT NOT NULL,
                memory_key TEXT NOT NULL,
                memory_value TEXT NOT NULL,
                message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
                rule_id TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now')),
                PRIMARY KEY (user_id, memory_key)
            );
            CREATE INDEX IF NOT EXISTS idx_automatic_memories_message
                ON automatic_memories(message_id);

            CREATE TABLE IF NOT EXISTS artifacts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                title TEXT NOT NULL,
                content TEXT NOT NULL,
                summary TEXT,
                tags_json TEXT,
                hash TEXT NOT NULL,
                timestamp REAL NOT NULL,
                provider TEXT,
                model_id TEXT,
                agent_id TEXT,
                run_id TEXT,
                workspace_id TEXT,
                tool_used TEXT,
                response_id TEXT,
                metadata_json TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_artifacts_user ON artifacts(user_id);
            CREATE INDEX IF NOT EXISTS idx_artifacts_user_kind ON artifacts(user_id, kind);
            CREATE INDEX IF NOT EXISTS idx_artifacts_user_ts ON artifacts(user_id, timestamp);
            CREATE INDEX IF NOT EXISTS idx_artifacts_user_model ON artifacts(user_id, model_id);
            CREATE INDEX IF NOT EXISTS idx_artifacts_user_agent ON artifacts(user_id, agent_id);

            CREATE TABLE IF NOT EXISTS code_chunks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                path TEXT NOT NULL,
                language TEXT,
                kind TEXT NOT NULL,
                symbol TEXT,
                content TEXT NOT NULL,
                summary TEXT,
                start_line INTEGER,
                end_line INTEGER,
                tags_json TEXT,
                hash TEXT NOT NULL,
                timestamp REAL NOT NULL,
                provider TEXT,
                model_id TEXT,
                agent_id TEXT,
                run_id TEXT,
                workspace_id TEXT,
                tool_used TEXT,
                response_id TEXT,
                metadata_json TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_code_chunks_user ON code_chunks(user_id);
            CREATE INDEX IF NOT EXISTS idx_code_chunks_path ON code_chunks(user_id, path);
            CREATE INDEX IF NOT EXISTS idx_code_chunks_symbol ON code_chunks(user_id, symbol);
            CREATE INDEX IF NOT EXISTS idx_code_chunks_kind ON code_chunks(user_id, kind);
            CREATE INDEX IF NOT EXISTS idx_code_chunks_ts ON code_chunks(user_id, timestamp);

            CREATE TABLE IF NOT EXISTS relations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                source_type TEXT NOT NULL,
                source_id INTEGER NOT NULL,
                target_type TEXT NOT NULL,
                target_id INTEGER NOT NULL,
                relation_type TEXT NOT NULL,
                weight REAL NOT NULL DEFAULT 1.0,
                timestamp REAL NOT NULL,
                metadata_json TEXT,
                hash TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_relations_user ON relations(user_id);
            CREATE INDEX IF NOT EXISTS idx_relations_source ON relations(user_id, source_type, source_id);
            CREATE INDEX IF NOT EXISTS idx_relations_target ON relations(user_id, target_type, target_id);
            CREATE INDEX IF NOT EXISTS idx_relations_type ON relations(user_id, relation_type);

            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
        """)

        # Schema v2 persists the exact default-tokenizer terms used during
        # reranking. Older databases are upgraded in place and backfilled one
        # user scope at a time, avoiding a whole-database startup migration.
        message_columns = {
            str(row[1])
            for row in self._conn.execute("PRAGMA table_info(messages)").fetchall()
        }
        if "normalized_terms" not in message_columns:
            self._conn.execute("ALTER TABLE messages ADD COLUMN normalized_terms TEXT")

        if self._use_persisted_terms:
            terms_version_key = f"{self._user_key}_normalized_terms_version"
            version_row = self._conn.execute(
                "SELECT value FROM metadata WHERE key = ?",
                (terms_version_key,),
            ).fetchone()
            current_terms_version = int(version_row[0]) if version_row else 0
            if current_terms_version < NORMALIZED_TERMS_VERSION:
                term_rows = self._conn.execute(
                    "SELECT id, text FROM messages WHERE user_id = ? ORDER BY id",
                    (self.user_id,),
                ).fetchall()
            else:
                term_rows = self._conn.execute(
                    """
                    SELECT id, text FROM messages
                    WHERE user_id = ? AND normalized_terms IS NULL
                    ORDER BY id
                    """,
                    (self.user_id,),
                ).fetchall()
            if term_rows:
                self._conn.executemany(
                    "UPDATE messages SET normalized_terms = ? WHERE id = ? AND user_id = ?",
                    [
                        (" ".join(self._normalized_terms(row[1])), row[0], self.user_id)
                        for row in term_rows
                    ],
                )
            self._conn.execute(
                """
                INSERT INTO metadata(key, value) VALUES(?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (terms_version_key, str(NORMALIZED_TERMS_VERSION)),
            )
            self._conn.commit()

        # Per-user FTS5 table — isolated from all other users, no cross-user scan
        self._conn.execute(f"""
            CREATE VIRTUAL TABLE IF NOT EXISTS {self._fts_table} USING fts5(
                text, speaker
            )
        """)
        self._conn.execute(f"""
            CREATE VIRTUAL TABLE IF NOT EXISTS {self._artifact_fts_table} USING fts5(
                text, kind, title
            )
        """)
        self._conn.execute(f"""
            CREATE VIRTUAL TABLE IF NOT EXISTS {self._code_fts_table} USING fts5(
                text, path, symbol, language, kind
            )
        """)
        self._conn.commit()

        # Migration: if per-user FTS5 is empty or on an old format, rebuild it
        fts_count = self._conn.execute(
            f"SELECT COUNT(*) FROM {self._fts_table}"
        ).fetchone()[0]
        version_key = f"{self._fts_table}_version"
        version_row = self._conn.execute(
            "SELECT value FROM metadata WHERE key = ?",
            (version_key,),
        ).fetchone()
        current_version = int(version_row[0]) if version_row else 0
        if fts_count == 0:
            msg_count = self._conn.execute(
                "SELECT COUNT(*) FROM messages WHERE user_id = ?", (self.user_id,)
            ).fetchone()[0]
            if msg_count > 0:
                rows = self._conn.execute(
                    """
                    SELECT id, text, speaker, normalized_terms
                    FROM messages WHERE user_id = ? ORDER BY id
                    """,
                    (self.user_id,),
                ).fetchall()
                provenance_map = self._load_provenance_map([row[0] for row in rows])
                for row in rows:
                    self._conn.execute(
                        f"INSERT INTO {self._fts_table}(rowid, text, speaker) VALUES (?,?,?)",
                        (
                            row[0],
                            self._searchable_text(
                                row[2],
                                row[1],
                                provenance_map.get(row[0]),
                                str(row[3]).split()
                                if self._use_persisted_terms and row[3]
                                else None,
                            ),
                            row[2],
                        ),
                    )
                self._conn.commit()
        elif current_version < FTS_FORMAT_VERSION:
            self._conn.execute(f"DELETE FROM {self._fts_table}")
            rows = self._conn.execute(
                """
                SELECT id, text, speaker, normalized_terms
                FROM messages WHERE user_id = ? ORDER BY id
                """,
                (self.user_id,),
            ).fetchall()
            provenance_map = self._load_provenance_map([row[0] for row in rows])
            for row in rows:
                self._conn.execute(
                    f"INSERT INTO {self._fts_table}(rowid, text, speaker) VALUES (?,?,?)",
                    (
                        row[0],
                        self._searchable_text(
                            row[2],
                            row[1],
                            provenance_map.get(row[0]),
                            str(row[3]).split()
                            if self._use_persisted_terms and row[3]
                            else None,
                        ),
                        row[2],
                    ),
                )
            self._conn.commit()

        self._conn.execute(
            "INSERT INTO metadata(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (version_key, str(FTS_FORMAT_VERSION)),
        )

        artifact_fts_count = self._conn.execute(
            f"SELECT COUNT(*) FROM {self._artifact_fts_table}"
        ).fetchone()[0]
        artifact_version_key = f"{self._artifact_fts_table}_version"
        artifact_version_row = self._conn.execute(
            "SELECT value FROM metadata WHERE key = ?",
            (artifact_version_key,),
        ).fetchone()
        current_artifact_version = (
            int(artifact_version_row[0]) if artifact_version_row else 0
        )
        if (
            artifact_fts_count == 0
            or current_artifact_version < ARTIFACT_FTS_FORMAT_VERSION
        ):
            if artifact_fts_count > 0:
                self._conn.execute(f"DELETE FROM {self._artifact_fts_table}")
            artifact_rows = self._conn.execute(
                """
                SELECT id, kind, title, content, summary, tags_json, provider, model_id, agent_id, run_id,
                       workspace_id, tool_used, response_id, metadata_json
                FROM artifacts
                WHERE user_id = ?
                ORDER BY id
                """,
                (self.user_id,),
            ).fetchall()
            for row in artifact_rows:
                provenance = self._artifact_row_provenance(row)
                tags = self._decode_json_list(row[5])
                self._conn.execute(
                    f"INSERT INTO {self._artifact_fts_table}(rowid, text, kind, title) VALUES (?,?,?,?)",
                    (
                        row[0],
                        self._artifact_searchable_text(
                            row[1], row[2], row[3], row[4], tags, provenance
                        ),
                        row[1],
                        row[2],
                    ),
                )
        self._conn.execute(
            "INSERT INTO metadata(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (artifact_version_key, str(ARTIFACT_FTS_FORMAT_VERSION)),
        )
        code_fts_count = self._conn.execute(
            f"SELECT COUNT(*) FROM {self._code_fts_table}"
        ).fetchone()[0]
        code_version_key = f"{self._code_fts_table}_version"
        code_version_row = self._conn.execute(
            "SELECT value FROM metadata WHERE key = ?",
            (code_version_key,),
        ).fetchone()
        current_code_version = int(code_version_row[0]) if code_version_row else 0
        if code_fts_count == 0 or current_code_version < CODE_CHUNK_FTS_FORMAT_VERSION:
            if code_fts_count > 0:
                self._conn.execute(f"DELETE FROM {self._code_fts_table}")
            code_rows = self._conn.execute(
                """
                SELECT id, path, language, kind, symbol, content, summary, tags_json,
                       provider, model_id, agent_id, run_id, workspace_id, tool_used, response_id, metadata_json
                FROM code_chunks
                WHERE user_id = ?
                ORDER BY id
                """,
                (self.user_id,),
            ).fetchall()
            for row in code_rows:
                provenance = self._code_chunk_row_provenance(row, offset=8)
                tags = self._decode_json_list(row[7])
                self._conn.execute(
                    f"INSERT INTO {self._code_fts_table}(rowid, text, path, symbol, language, kind) VALUES (?,?,?,?,?,?)",
                    (
                        row[0],
                        self._code_chunk_searchable_text(
                            path=row[1],
                            language=row[2] or "",
                            kind=row[3],
                            symbol=row[4] or "",
                            content=row[5],
                            summary=row[6] or "",
                            tags=tags,
                            provenance=provenance,
                        ),
                        row[1],
                        row[4] or "",
                        row[2] or "",
                        row[3],
                    ),
                )
        self._conn.execute(
            "INSERT INTO metadata(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (code_version_key, str(CODE_CHUNK_FTS_FORMAT_VERSION)),
        )
        self._conn.execute(
            "INSERT INTO metadata(key, value) VALUES('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (str(SCHEMA_VERSION),),
        )
        self._conn.commit()

    def _load_hashes(self):
        """Load existing message hashes for dedup."""
        rows = self._conn.execute(
            "SELECT hash FROM messages WHERE user_id = ?", (self.user_id,)
        ).fetchall()
        self._hashes = {r[0] for r in rows}

    def _next_message_position(self) -> int:
        row = self._conn.execute(
            "SELECT MAX(position) FROM messages WHERE user_id = ?",
            (self.user_id,),
        ).fetchone()
        max_position = row[0] if row and row[0] is not None else -1
        return int(max_position) + 1

    def _ensure_message_positions(self):
        row = self._conn.execute(
            """
            SELECT COUNT(*), COUNT(DISTINCT position), MIN(position), MAX(position)
            FROM messages
            WHERE user_id = ?
            """,
            (self.user_id,),
        ).fetchone()
        total = int(row[0] or 0)
        if total <= 1:
            return

        distinct_positions = int(row[1] or 0)
        min_position = row[2]
        max_position = row[3]
        positions_are_dense = (
            distinct_positions == total
            and min_position == 0
            and max_position == total - 1
        )
        if positions_are_dense:
            return

        rows = self._conn.execute(
            """
            SELECT id
            FROM messages
            WHERE user_id = ?
            ORDER BY timestamp, id
            """,
            (self.user_id,),
        ).fetchall()
        self._conn.executemany(
            "UPDATE messages SET position = ? WHERE id = ?",
            [(index, row[0]) for index, row in enumerate(rows)],
        )
        self._conn.commit()
        self._invalidate_embedding_index()

    def _load_recent_tokens(self):
        """Load recent message token sets for semantic dedup cache."""
        if not self.semantic_dedup:
            return
        rows = self._conn.execute(
            """
            SELECT text, normalized_terms
            FROM messages WHERE user_id = ? ORDER BY position DESC LIMIT ?
            """,
            (self.user_id, self.dedup_window),
        ).fetchall()
        self._recent_tokens = [
            frozenset(str(row[1]).split())
            if self._use_persisted_terms and row[1]
            else self._tokenize(row[0])
            for row in reversed(rows)
        ]
        if self._sbert and rows:
            texts = [r[0] for r in reversed(rows)]
            self._sbert_embeddings = list(
                _encode_texts(self._sbert, texts, show_progress_bar=False)
            )
        elif self._sbert:
            self._sbert_embeddings = []

    def _normalize_provenance(self, provenance: Optional[dict]) -> dict:
        if not provenance:
            return {}

        normalized = {}
        metadata = provenance.get("metadata")
        for key in (
            "provider",
            "model_id",
            "agent_id",
            "run_id",
            "workspace_id",
            "tool_used",
            "response_id",
        ):
            value = provenance.get(key)
            if value is None:
                continue
            text = str(value).strip()
            if text:
                normalized[key] = text

        if isinstance(metadata, dict) and metadata:
            normalized["metadata"] = {
                str(k): str(v)
                for k, v in metadata.items()
                if v is not None and str(v).strip()
            }

        return normalized

    def _provenance_signature(self, provenance: Optional[dict]) -> str:
        normalized = self._normalize_provenance(provenance)
        if not normalized:
            return ""
        return json.dumps(normalized, sort_keys=True, separators=(",", ":"))

    def _normalize_tags(self, tags: Optional[list | tuple | set]) -> list[str]:
        if not tags:
            return []
        seen = set()
        normalized = []
        for tag in tags:
            text = str(tag).strip()
            if not text:
                continue
            lowered = text.lower()
            if lowered in seen:
                continue
            seen.add(lowered)
            normalized.append(text)
        return normalized

    def _decode_json_list(self, raw: Optional[str]) -> list[str]:
        if not raw:
            return []
        try:
            data = json.loads(raw)
        except Exception:
            return []
        if not isinstance(data, list):
            return []
        return [str(item) for item in data if str(item).strip()]

    def _artifact_row_provenance(self, row, offset: int = 6) -> dict:
        metadata = {}
        metadata_index = offset + 7
        if row[metadata_index]:
            try:
                parsed = json.loads(row[metadata_index])
                if isinstance(parsed, dict):
                    metadata = parsed
            except Exception:
                metadata = {}
        provenance = {
            key: value
            for key, value in {
                "provider": row[offset],
                "model_id": row[offset + 1],
                "agent_id": row[offset + 2],
                "run_id": row[offset + 3],
                "workspace_id": row[offset + 4],
                "tool_used": row[offset + 5],
                "response_id": row[offset + 6],
            }.items()
            if value
        }
        if metadata:
            provenance["metadata"] = metadata
        return provenance

    def _code_chunk_row_provenance(self, row, offset: int = 11) -> dict:
        metadata = {}
        metadata_index = offset + 7
        if row[metadata_index]:
            try:
                parsed = json.loads(row[metadata_index])
                if isinstance(parsed, dict):
                    metadata = parsed
            except Exception:
                metadata = {}
        provenance = {
            key: value
            for key, value in {
                "provider": row[offset],
                "model_id": row[offset + 1],
                "agent_id": row[offset + 2],
                "run_id": row[offset + 3],
                "workspace_id": row[offset + 4],
                "tool_used": row[offset + 5],
                "response_id": row[offset + 6],
            }.items()
            if value
        }
        if metadata:
            provenance["metadata"] = metadata
        return provenance

    def _iter_search_terms(self, text: str):
        for fragment in re.findall(r"[A-Za-z0-9_./:-]+", text):
            cleaned = fragment.strip("._:/-")
            if not cleaned:
                continue

            lowered = cleaned.lower()
            yield lowered

            pieces = re.split(r"[/._:\-]+", cleaned)
            for piece in pieces:
                if not piece:
                    continue
                camel_split = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", piece).split()
                if camel_split:
                    for token in camel_split:
                        lowered_token = token.lower()
                        if lowered_token:
                            yield lowered_token
                else:
                    yield piece.lower()

    def _normalized_terms(self, text: str) -> list[str]:
        seen = set()
        ordered = []
        for candidate in self._iter_search_terms(text):
            if not candidate or candidate.isdigit() or len(candidate) <= 1:
                continue

            if candidate not in self.stop_words:
                if candidate not in seen:
                    seen.add(candidate)
                    ordered.append(candidate)

            if candidate.isalpha():
                stemmed = stem(candidate)
                if (
                    stemmed
                    and stemmed not in self.stop_words
                    and len(stemmed) > 1
                    and stemmed not in seen
                ):
                    seen.add(stemmed)
                    ordered.append(stemmed)

        return ordered

    def _provenance_terms(self, provenance: Optional[dict]) -> list[str]:
        normalized = self._normalize_provenance(provenance)
        if not normalized:
            return []

        terms = []
        for key in (
            "provider",
            "model_id",
            "agent_id",
            "run_id",
            "workspace_id",
            "tool_used",
            "response_id",
        ):
            value = normalized.get(key)
            if value:
                terms.extend(self._normalized_terms(value))

        metadata = normalized.get("metadata") or {}
        if isinstance(metadata, dict):
            for key, value in metadata.items():
                terms.extend(self._normalized_terms(str(key)))
                terms.extend(self._normalized_terms(str(value)))

        seen = set()
        ordered = []
        for term in terms:
            if term not in seen:
                seen.add(term)
                ordered.append(term)
        return ordered

    def _searchable_text(
        self,
        speaker: str,
        text: str,
        provenance: Optional[dict] = None,
        normalized_terms: Optional[list[str]] = None,
    ) -> str:
        tokens = (
            list(normalized_terms)
            if normalized_terms is not None
            else self._normalized_terms(text)
        )
        tokens.extend(self._provenance_terms(provenance))
        if not tokens:
            return f"{speaker}: {text}"
        return f"{speaker}: {text}\n__terms__: {' '.join(tokens)}"

    def _artifact_searchable_text(
        self,
        kind: str,
        title: str,
        content: str,
        summary: str = "",
        tags: Optional[list[str]] = None,
        provenance: Optional[dict] = None,
    ) -> str:
        tokens = []
        tokens.extend(self._normalized_terms(kind))
        tokens.extend(self._normalized_terms(title))
        tokens.extend(self._normalized_terms(summary or ""))
        tokens.extend(self._normalized_terms(content))
        for tag in tags or []:
            tokens.extend(self._normalized_terms(tag))
        tokens.extend(self._provenance_terms(provenance))
        seen = set()
        ordered = []
        for token in tokens:
            if token not in seen:
                seen.add(token)
                ordered.append(token)
        return f"{kind}: {title}\n{summary}\n{content}\n__terms__: {' '.join(ordered)}"

    def _code_chunk_searchable_text(
        self,
        path: str,
        language: str,
        kind: str,
        symbol: str,
        content: str,
        summary: str = "",
        tags: Optional[list[str]] = None,
        provenance: Optional[dict] = None,
    ) -> str:
        tokens = []
        for value in (path, language, kind, symbol, summary, content):
            tokens.extend(self._normalized_terms(value or ""))
        for tag in tags or []:
            tokens.extend(self._normalized_terms(tag))
        tokens.extend(self._provenance_terms(provenance))
        seen = set()
        ordered = []
        for token in tokens:
            if token not in seen:
                seen.add(token)
                ordered.append(token)
        return (
            f"{path}\n{language} {kind} {symbol}\n{summary}\n{content}\n"
            f"__terms__: {' '.join(ordered)}"
        )

    def _infer_language(self, path: str, language: str = "") -> str:
        if language:
            return language.lower()
        suffix = Path(path).suffix.lower()
        return {
            ".ts": "typescript",
            ".tsx": "tsx",
            ".js": "javascript",
            ".jsx": "jsx",
            ".py": "python",
            ".rs": "rust",
            ".go": "go",
            ".java": "java",
            ".kt": "kotlin",
            ".json": "json",
            ".yaml": "yaml",
            ".yml": "yaml",
        }.get(suffix, suffix.lstrip(".") or "text")

    def _code_symbol_patterns(self, language: str) -> list[tuple[str, re.Pattern[str]]]:
        if language in {"typescript", "tsx", "javascript", "jsx"}:
            return [
                (
                    "function",
                    re.compile(
                        r"^\s*(?:export\s+)?(?:async\s+)?function\s+(?P<name>[A-Za-z_][A-Za-z0-9_]*)"
                    ),
                ),
                (
                    "function",
                    re.compile(
                        r"^\s*(?:export\s+)?(?:const|let|var)\s+(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?:async\s*)?(?:\([^)]*\)|[A-Za-z_][A-Za-z0-9_]*)\s*=>"
                    ),
                ),
                (
                    "class",
                    re.compile(
                        r"^\s*(?:export\s+)?class\s+(?P<name>[A-Za-z_][A-Za-z0-9_]*)"
                    ),
                ),
                (
                    "interface",
                    re.compile(
                        r"^\s*(?:export\s+)?interface\s+(?P<name>[A-Za-z_][A-Za-z0-9_]*)"
                    ),
                ),
                (
                    "type",
                    re.compile(
                        r"^\s*(?:export\s+)?type\s+(?P<name>[A-Za-z_][A-Za-z0-9_]*)"
                    ),
                ),
            ]
        if language == "python":
            return [
                ("class", re.compile(r"^\s*class\s+(?P<name>[A-Za-z_][A-Za-z0-9_]*)")),
                (
                    "function",
                    re.compile(
                        r"^\s*(?:async\s+)?def\s+(?P<name>[A-Za-z_][A-Za-z0-9_]*)"
                    ),
                ),
            ]
        if language == "rust":
            return [
                (
                    "function",
                    re.compile(r"^\s*(?:pub\s+)?fn\s+(?P<name>[A-Za-z_][A-Za-z0-9_]*)"),
                ),
                (
                    "struct",
                    re.compile(
                        r"^\s*(?:pub\s+)?struct\s+(?P<name>[A-Za-z_][A-Za-z0-9_]*)"
                    ),
                ),
                (
                    "enum",
                    re.compile(
                        r"^\s*(?:pub\s+)?enum\s+(?P<name>[A-Za-z_][A-Za-z0-9_]*)"
                    ),
                ),
                (
                    "impl",
                    re.compile(
                        r"^\s*impl(?:<[^>]+>)?\s+(?P<name>[A-Za-z_][A-Za-z0-9_:<>]*)"
                    ),
                ),
            ]
        return []

    def _extract_code_chunks(
        self,
        path: str,
        content: str,
        language: str = "",
        chunk_lines: int = 120,
    ) -> list[dict]:
        resolved_language = self._infer_language(path, language)
        lines = content.splitlines()
        if not lines:
            return []

        patterns = self._code_symbol_patterns(resolved_language)
        symbols: list[tuple[int, str, str]] = []
        for index, line in enumerate(lines, start=1):
            for kind, pattern in patterns:
                match = pattern.match(line)
                if match:
                    name = match.group("name")
                    symbols.append((index, kind, name))
                    break

        chunks: list[dict] = []

        def _append_chunk(
            start_line: int, end_line: int, kind: str, symbol: str, summary: str
        ):
            if end_line < start_line:
                return
            snippet = "\n".join(lines[start_line - 1 : end_line]).strip()
            if not snippet:
                return
            chunks.append(
                {
                    "path": path,
                    "language": resolved_language,
                    "kind": kind,
                    "symbol": symbol,
                    "content": snippet,
                    "summary": summary,
                    "start_line": start_line,
                    "end_line": end_line,
                    "tags": [resolved_language, kind] + ([symbol] if symbol else []),
                }
            )

        if symbols:
            first_symbol_line = symbols[0][0]
            if first_symbol_line > 1:
                _append_chunk(
                    1,
                    first_symbol_line - 1,
                    "preamble",
                    Path(path).name,
                    f"Preamble for {Path(path).name}",
                )
            for idx, (start_line, kind, symbol) in enumerate(symbols):
                next_start = (
                    symbols[idx + 1][0] if idx + 1 < len(symbols) else len(lines) + 1
                )
                _append_chunk(
                    start_line,
                    next_start - 1,
                    kind,
                    symbol,
                    f"{kind} {symbol} in {Path(path).name}",
                )
        else:
            for start in range(1, len(lines) + 1, chunk_lines):
                end = min(len(lines), start + chunk_lines - 1)
                ordinal = ((start - 1) // chunk_lines) + 1
                _append_chunk(
                    start,
                    end,
                    "chunk",
                    f"{Path(path).name}#chunk{ordinal}",
                    f"Chunk {ordinal} from {Path(path).name}",
                )

        return chunks

    def _tokenize(self, text: str) -> frozenset:
        """Stem + stop-word filter for Jaccard similarity."""
        return frozenset(self._normalized_terms(text))

    def _stored_message_terms(self, row) -> frozenset:
        """Return persisted default-tokenizer terms, with a compatibility fallback."""
        if self._use_persisted_terms and len(row) > 5 and row[5]:
            return frozenset(str(row[5]).split())
        return self._tokenize(str(row[2]))

    def _jaccard(self, a: frozenset, b: frozenset) -> float:
        if not a or not b:
            return 0.0
        inter = len(a & b)
        if inter == 0:
            return 0.0
        return inter / len(a | b)

    def _shape_results(
        self,
        ranked: list,
        *,
        provenance_map: dict,
        current_intent: bool = False,
        before_intent: bool = False,
        minimum_results: int | None = None,
    ) -> list:
        """Reorder scored direct hits for context precision.

        ranked: (score, row) tuples sorted best-first, row shaped as
        (id, speaker, text, timestamp, position, normalized_terms).

        Near-duplicates of a higher-ranked hit are demoted to the tail
        (never dropped, so deep-cutoff recall is preserved). Under a
        current/before query intent the cluster representative is the
        latest/earliest member rather than the highest-scored one. A
        per-session cap inside the top window lets other sessions
        surface. Finally a relative confidence floor trims the weak tail,
        keeping at least score_floor_min_results.
        """
        if len(ranked) <= 1:
            return list(ranked)

        # 1. Near-duplicate clustering against kept representatives.
        threshold = self.search_dedup_jaccard
        representatives: list[tuple[int, frozenset]] = []  # (kept_index, terms)
        kept: list = []
        clusters: dict[int, list] = {}  # kept_index -> demoted members
        for entry in ranked:
            terms = self._stored_message_terms(entry[1])
            matched = None
            if threshold < 1.0:
                for kept_index, kept_terms in representatives:
                    if self._jaccard(terms, kept_terms) >= threshold:
                        matched = kept_index
                        break
            if matched is None:
                clusters[len(kept)] = []
                representatives.append((len(kept), terms))
                kept.append(entry)
            else:
                clusters[matched].append(entry)

        # 2. Temporal representative selection inside each cluster.
        if self.temporal_cluster_select and (current_intent or before_intent):
            for kept_index, members in clusters.items():
                if not members:
                    continue
                family = [kept[kept_index], *members]
                if current_intent:
                    chosen = max(family, key=lambda e: e[1][3])
                else:
                    chosen = min(family, key=lambda e: e[1][3])
                if chosen is not kept[kept_index]:
                    members = [e for e in family if e is not chosen]
                    kept[kept_index] = chosen
                    clusters[kept_index] = members

        demoted_dups = [entry for members in clusters.values() for entry in members]

        # 3. Per-session cap within the top window.
        cap = self.session_cap
        window = self.diversify_top_n
        shaped: list = []
        overflow: list = []
        if cap > 0 and provenance_map:
            session_counts: dict[str, int] = {}
            for entry in kept:
                run_id = (provenance_map.get(entry[1][0]) or {}).get("run_id")
                if (
                    run_id is not None
                    and len(shaped) < window
                    and session_counts.get(run_id, 0) >= cap
                ):
                    overflow.append(entry)
                    continue
                if run_id is not None:
                    session_counts[run_id] = session_counts.get(run_id, 0) + 1
                shaped.append(entry)
        else:
            shaped = kept
        shaped = shaped + overflow + demoted_dups

        # 4. Relative confidence floor: drop the weak tail, keep a minimum.
        floor_min = self.score_floor_min_results
        if minimum_results is not None:
            floor_min = max(floor_min, max(0, int(minimum_results)))
        ratio = self.score_floor_ratio
        if ratio > 0.0 and len(shaped) > floor_min:
            top_score = shaped[0][0]
            if top_score > 0:
                floor = ratio * top_score
                trimmed = [
                    entry
                    for index, entry in enumerate(shaped)
                    if index < floor_min or entry[0] >= floor
                ]
                shaped = trimmed
        return shaped

    def _is_semantic_duplicate(self, text: str) -> bool:
        """Return True if text is semantically similar to a recent message."""
        if not self.semantic_dedup or not self._recent_tokens:
            return False

        if self._sbert:
            # SBERT cosine similarity
            emb = _encode_texts(self._sbert, [text], show_progress_bar=False)[0]
            for prev_emb in self._sbert_embeddings[-self.dedup_window :]:
                sim = float(
                    self._np.dot(emb, prev_emb)
                    / (
                        self._np.linalg.norm(emb) * self._np.linalg.norm(prev_emb)
                        + 1e-9
                    )
                )
                if sim >= self.dedup_threshold:
                    return True
            return False
        else:
            # Jaccard fallback
            tokens = self._tokenize(text)
            if len(tokens) < 3:
                return False  # Too short to dedup meaningfully
            for prev in self._recent_tokens[-self.dedup_window :]:
                if self._jaccard(tokens, prev) >= self.dedup_threshold:
                    return True
            return False

    def _add_to_dedup_cache(self, text: str, tokens: Optional[frozenset] = None):
        """Add a newly stored message to the semantic dedup cache."""
        if not self.semantic_dedup:
            return
        self._recent_tokens.append(
            tokens if tokens is not None else self._tokenize(text)
        )
        if len(self._recent_tokens) > self.dedup_window * 2:
            self._recent_tokens = self._recent_tokens[-self.dedup_window :]
        if self._sbert:
            emb = _encode_texts(self._sbert, [text], show_progress_bar=False)[0]
            self._sbert_embeddings.append(emb)
            if len(self._sbert_embeddings) > self.dedup_window * 2:
                self._sbert_embeddings = self._sbert_embeddings[-self.dedup_window :]

    def _invalidate_embedding_index(self):
        if self._sbert is None:
            return
        self._embedding_index_dirty = True
        self._embedding_matrix = None
        self._embedding_row_meta = []

    def _append_embedding_index(
        self, message_id: int, position: int, timestamp: float, embedding
    ):
        if self._sbert is None or embedding is None or self._embedding_index_dirty:
            return
        if self._embedding_matrix is None:
            self._embedding_matrix = embedding.reshape(1, -1)
            self._embedding_row_meta = [(message_id, position, timestamp)]
            return
        self._embedding_matrix = self._np.vstack(
            (self._embedding_matrix, embedding.reshape(1, -1))
        )
        self._embedding_row_meta.append((message_id, position, timestamp))

    def _ensure_embedding_index(self) -> bool:
        if self._sbert is None:
            return False
        if not self._embedding_index_dirty and self._embedding_matrix is not None:
            return True

        rows = self._conn.execute(
            """
            SELECT e.message_id, m.position, m.timestamp, e.embedding
            FROM embeddings e
            JOIN messages m ON m.id = e.message_id
            WHERE e.user_id = ?
            ORDER BY m.position, e.message_id
        """,
            (self.user_id,),
        ).fetchall()

        if not rows:
            self._embedding_matrix = None
            self._embedding_row_meta = []
            self._embedding_index_dirty = False
            return False

        emb_dim = len(rows[0][3]) // 4
        self._embedding_matrix = self._np.frombuffer(
            b"".join(row[3] for row in rows),
            dtype=self._np.float32,
        ).reshape(-1, emb_dim)
        self._embedding_row_meta = [(row[0], row[1], row[2]) for row in rows]
        self._embedding_index_dirty = False
        return True

    def _store_provenance(self, message_id: int, provenance: Optional[dict]):
        normalized = self._normalize_provenance(provenance)
        if not normalized:
            return

        metadata = (
            normalized.get("metadata")
            if isinstance(normalized.get("metadata"), dict)
            else None
        )
        self._conn.execute(
            """
            INSERT INTO message_provenance (
                message_id, user_id, provider, model_id, agent_id, run_id,
                workspace_id, tool_used, response_id, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(message_id) DO UPDATE SET
                provider = excluded.provider,
                model_id = excluded.model_id,
                agent_id = excluded.agent_id,
                run_id = excluded.run_id,
                workspace_id = excluded.workspace_id,
                tool_used = excluded.tool_used,
                response_id = excluded.response_id,
                metadata_json = excluded.metadata_json
            """,
            (
                message_id,
                self.user_id,
                normalized.get("provider"),
                normalized.get("model_id"),
                normalized.get("agent_id"),
                normalized.get("run_id"),
                normalized.get("workspace_id"),
                normalized.get("tool_used"),
                normalized.get("response_id"),
                json.dumps(metadata, sort_keys=True) if metadata else None,
            ),
        )

    def _load_provenance_map(self, message_ids: list[int]) -> dict[int, dict]:
        if not message_ids:
            return {}

        placeholders = ",".join("?" for _ in message_ids)
        rows = self._conn.execute(
            f"""
            SELECT message_id, provider, model_id, agent_id, run_id, workspace_id, tool_used, response_id, metadata_json
            FROM message_provenance
            WHERE user_id = ? AND message_id IN ({placeholders})
            """,
            [self.user_id] + message_ids,
        ).fetchall()

        provenance_map: dict[int, dict] = {}
        for row in rows:
            metadata = {}
            if row[8]:
                try:
                    parsed = json.loads(row[8])
                    if isinstance(parsed, dict):
                        metadata = parsed
                except Exception:
                    metadata = {}
            provenance = {
                key: value
                for key, value in {
                    "provider": row[1],
                    "model_id": row[2],
                    "agent_id": row[3],
                    "run_id": row[4],
                    "workspace_id": row[5],
                    "tool_used": row[6],
                    "response_id": row[7],
                }.items()
                if value
            }
            if metadata:
                provenance["metadata"] = metadata
            provenance_map[row[0]] = provenance

        return provenance_map

    def _message_from_row(self, row, provenance_map: dict[int, dict]) -> Message:
        return Message(
            id=row[0],
            speaker=row[1],
            text=row[2],
            timestamp=row[3],
            position=row[4],
            provenance=provenance_map.get(row[0], {}),
        )

    def _hash(self, speaker: str, text: str, provenance: Optional[dict] = None) -> str:
        signature = self._provenance_signature(provenance)
        return hashlib.md5(f"{speaker}:{text}:{signature}".encode()).hexdigest()

    def _artifact_hash(
        self,
        kind: str,
        title: str,
        content: str,
        summary: str = "",
        tags: Optional[list[str]] = None,
        provenance: Optional[dict] = None,
    ) -> str:
        signature = self._provenance_signature(provenance)
        tag_signature = json.dumps(self._normalize_tags(tags), sort_keys=True)
        return hashlib.md5(
            f"{kind}:{title}:{summary}:{content}:{tag_signature}:{signature}".encode()
        ).hexdigest()

    def _code_chunk_hash(
        self,
        path: str,
        language: str,
        kind: str,
        symbol: str,
        content: str,
        summary: str = "",
        start_line: int | None = None,
        end_line: int | None = None,
        tags: Optional[list[str]] = None,
        provenance: Optional[dict] = None,
    ) -> str:
        signature = self._provenance_signature(provenance)
        tag_signature = json.dumps(self._normalize_tags(tags), sort_keys=True)
        return hashlib.md5(
            f"{path}:{language}:{kind}:{symbol}:{start_line}:{end_line}:{summary}:{content}:{tag_signature}:{signature}".encode()
        ).hexdigest()

    def _relation_hash(
        self,
        source_type: str,
        source_id: int,
        target_type: str,
        target_id: int,
        relation_type: str,
        metadata: Optional[dict] = None,
    ) -> str:
        metadata_signature = json.dumps(
            metadata or {}, sort_keys=True, separators=(",", ":")
        )
        return hashlib.md5(
            f"{source_type}:{source_id}:{target_type}:{target_id}:{relation_type}:{metadata_signature}".encode()
        ).hexdigest()

    # ── Store ──

    def remember(
        self,
        text: str,
        speaker: str = "memory",
        timestamp: float = None,
        provenance: Optional[dict] = None,
        *,
        semantic_dedup: bool = True,
    ) -> Optional[int]:
        """Compatibility alias for store()."""
        return self.store(
            speaker=speaker,
            text=text,
            timestamp=timestamp,
            provenance=provenance,
            semantic_dedup=semantic_dedup,
        )

    def automatic_memory(self, memory_key: str) -> dict | None:
        """Return one typed automatic-memory ledger entry."""

        row = self._conn.execute(
            """
            SELECT memory_key, memory_value, message_id, rule_id
            FROM automatic_memories
            WHERE user_id = ? AND memory_key = ?
            """,
            (self.user_id, str(memory_key)),
        ).fetchone()
        if row is None:
            return None
        return {
            "memory_key": str(row[0]),
            "memory_value": str(row[1]),
            "message_id": int(row[2]),
            "rule_id": str(row[3]),
        }

    def exact_message(self, text: str, *, speaker: str) -> dict | None:
        """Return matching raw evidence and its automatic key, if any."""

        row = self._conn.execute(
            """
            SELECT m.id, a.memory_key
            FROM messages AS m
            LEFT JOIN automatic_memories AS a
              ON a.user_id = m.user_id AND a.message_id = m.id
            WHERE m.user_id = ? AND m.speaker = ? AND m.text = ?
            ORDER BY m.id DESC
            LIMIT 1
            """,
            (self.user_id, str(speaker), str(text)),
        ).fetchone()
        if row is None:
            return None
        return {
            "message_id": int(row[0]),
            "memory_key": str(row[1]) if row[1] is not None else None,
        }

    def record_automatic_memory(
        self,
        *,
        memory_key: str,
        memory_value: str,
        message_id: int,
        rule_id: str,
    ) -> int | None:
        """Atomically point a stable automatic key at its current evidence.

        Return the previously referenced message so the caller can remove only
        superseded *automatic* evidence. ``BEGIN IMMEDIATE`` serializes this
        swap across hooks running in separate Codex processes.
        """

        try:
            self._conn.execute("BEGIN IMMEDIATE")
            previous = self._conn.execute(
                """
                SELECT message_id
                FROM automatic_memories
                WHERE user_id = ? AND memory_key = ?
                """,
                (self.user_id, str(memory_key)),
            ).fetchone()
            self._conn.execute(
                """
                INSERT INTO automatic_memories(
                    user_id, memory_key, memory_value, message_id, rule_id
                ) VALUES(?,?,?,?,?)
                ON CONFLICT(user_id, memory_key) DO UPDATE SET
                    memory_value = excluded.memory_value,
                    message_id = excluded.message_id,
                    rule_id = excluded.rule_id,
                    updated_at = datetime('now')
                """,
                (
                    self.user_id,
                    str(memory_key),
                    str(memory_value),
                    int(message_id),
                    str(rule_id),
                ),
            )
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        return int(previous[0]) if previous is not None else None

    def store(
        self,
        speaker: str,
        text: str,
        timestamp: float = None,
        provenance: Optional[dict] = None,
        *,
        semantic_dedup: bool = True,
    ) -> Optional[int]:
        """Store a message. Returns message ID or None if duplicate."""
        normalized_provenance = self._normalize_provenance(provenance)
        h = self._hash(speaker, text, normalized_provenance)
        if h in self._hashes:
            return None  # Exact dedup

        # Semantic dedup — skip if very similar to a recent message
        has_identity_provenance = any(
            normalized_provenance.get(key)
            for key in ("provider", "model_id", "agent_id", "run_id", "response_id")
        )
        if (
            semantic_dedup
            and not has_identity_provenance
            and self._is_semantic_duplicate(text)
        ):
            return None

        if timestamp is None:
            timestamp = time.time()

        normalized_terms = self._normalized_terms(text)
        token_set = frozenset(normalized_terms)

        # Get next position for this user
        pos = self._next_message_position()

        cursor = self._conn.execute(
            """
            INSERT INTO messages (
                user_id, speaker, text, normalized_terms, hash, timestamp, position
            ) VALUES (?,?,?,?,?,?,?)
            """,
            (
                self.user_id,
                speaker,
                text,
                " ".join(normalized_terms),
                h,
                timestamp,
                pos,
            ),
        )
        msg_id = cursor.lastrowid
        self._conn.execute(
            f"INSERT INTO {self._fts_table}(rowid, text, speaker) VALUES (?,?,?)",
            (
                msg_id,
                self._searchable_text(
                    speaker,
                    text,
                    normalized_provenance,
                    normalized_terms,
                ),
                speaker,
            ),
        )
        emb = None
        if self._sbert:
            emb = _encode_texts(
                self._sbert,
                [f"{speaker}: {text}"],
                normalize_embeddings=True,
                show_progress_bar=False,
            )[0]
            self._conn.execute(
                "INSERT INTO embeddings (message_id, user_id, embedding) VALUES (?,?,?)",
                (msg_id, self.user_id, emb.tobytes()),
            )
        self._store_provenance(msg_id, normalized_provenance)
        self._conn.commit()
        self._hashes.add(h)
        if not has_identity_provenance:
            self._add_to_dedup_cache(text, token_set)
        self._append_embedding_index(msg_id, pos, timestamp, emb)
        return msg_id

    def store_batch(self, messages: list[dict]) -> int:
        """Store multiple messages. Each dict: {speaker, text, timestamp?}. Returns count stored."""
        return len(self.store_batch_with_ids(messages))

    def store_batch_with_ids(
        self, messages: list[dict], *, commit: bool = True
    ) -> list[int]:
        """Store a batch and return the IDs inserted by this call.

        ``commit=False`` is reserved for compound engine operations that own
        the surrounding transaction, such as ``store_session``.
        """

        inserted_ids: list[int] = []
        pos = self._next_message_position()

        # Collect messages to store, then batch-encode embeddings
        to_store = []
        for msg in messages:
            provenance = self._normalize_provenance(msg.get("provenance"))
            h = self._hash(msg["speaker"], msg["text"], provenance)
            if h in self._hashes:
                continue
            text = msg["text"]
            has_identity_provenance = any(
                provenance.get(key)
                for key in ("provider", "model_id", "agent_id", "run_id", "response_id")
            )
            if not has_identity_provenance and self._is_semantic_duplicate(text):
                continue
            normalized_terms = self._normalized_terms(text)
            to_store.append(
                (
                    msg,
                    h,
                    text,
                    pos,
                    provenance,
                    normalized_terms,
                    not has_identity_provenance,
                )
            )
            self._hashes.add(h)
            if not has_identity_provenance:
                self._add_to_dedup_cache(text, frozenset(normalized_terms))
            pos += 1

        # Batch encode all embeddings at once (much faster than one-by-one)
        embeddings = None
        if self._sbert and to_store:
            texts = [f"{m[0]['speaker']}: {m[2]}" for m in to_store]
            embeddings = _encode_texts(
                self._sbert,
                texts,
                normalize_embeddings=True,
                batch_size=256,
                show_progress_bar=False,
            )

        for i, (msg, h, text, p, provenance, normalized_terms, _) in enumerate(
            to_store
        ):
            ts = msg.get("timestamp")
            if ts is None:
                ts = time.time()
            cursor = self._conn.execute(
                """
                INSERT INTO messages (
                    user_id, speaker, text, normalized_terms, hash, timestamp, position
                ) VALUES (?,?,?,?,?,?,?)
                """,
                (
                    self.user_id,
                    msg["speaker"],
                    text,
                    " ".join(normalized_terms),
                    h,
                    ts,
                    p,
                ),
            )
            msg_id = cursor.lastrowid
            self._conn.execute(
                f"INSERT INTO {self._fts_table}(rowid, text, speaker) VALUES (?,?,?)",
                (
                    msg_id,
                    self._searchable_text(
                        msg["speaker"],
                        text,
                        provenance,
                        normalized_terms,
                    ),
                    msg["speaker"],
                ),
            )
            if embeddings is not None:
                self._conn.execute(
                    "INSERT INTO embeddings (message_id, user_id, embedding) VALUES (?,?,?)",
                    (msg_id, self.user_id, embeddings[i].tobytes()),
                )
                self._append_embedding_index(msg_id, p, ts, embeddings[i])
            self._store_provenance(msg_id, provenance)
            inserted_ids.append(int(msg_id))
        if commit:
            self._conn.commit()
        return inserted_ids

    def store_session(
        self,
        messages: list[dict],
        *,
        session_id: str,
        occurred_at: float | None = None,
        metadata: Optional[dict] = None,
        append: bool = False,
    ) -> dict:
        """Atomically store and register a conversation session.

        By default, a repeated ``session_id`` replaces the registered source
        membership while retaining canonical raw history. ``append=True``
        preserves the existing registered membership and adds this call's
        current rows, which supports chunked ingestion without a volatile
        application-side buffer.
        """

        external_id = str(session_id or "").strip()
        if not external_id:
            raise ValueError("session_id is required")
        base_metadata = dict(metadata or {})
        base_metadata.setdefault("session_id", external_id)
        rows = []
        for index, message in enumerate(messages):
            speaker = str(message.get("speaker") or message.get("role") or "memory")
            text = str(message.get("text") or message.get("content") or "").strip()
            if not text:
                continue
            provenance = self._normalize_provenance(message.get("provenance"))
            provenance["run_id"] = external_id
            nested_metadata = dict(provenance.get("metadata") or {})
            nested_metadata.update(base_metadata)
            nested_metadata.setdefault("turn_index", index)
            provenance["metadata"] = nested_metadata
            timestamp = message.get("timestamp")
            if timestamp is None and occurred_at is not None:
                timestamp = float(occurred_at) + (index / 1000.0)
            rows.append(
                {
                    "speaker": speaker,
                    "text": text,
                    "timestamp": timestamp,
                    "provenance": provenance,
                }
            )
        # Resolve membership from this call's exact hashes rather than every
        # historical raw row carrying the same run_id. This preserves canonical
        # raw history while making a repeated external session ID a true
        # replacement for compiler and derived-memory purposes.
        row_hashes = [
            self._hash(row["speaker"], row["text"], row.get("provenance"))
            for row in rows
        ]
        with self._db_lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                existing_message_ids: list[int] = []
                if append:
                    existing_session = self._conn.execute(
                        """
                        SELECT id FROM memory_sessions
                        WHERE user_id = ? AND external_id = ?
                        """,
                        (self.user_id, external_id),
                    ).fetchone()
                    if existing_session is not None:
                        existing_message_ids = [
                            int(row[0])
                            for row in self._conn.execute(
                                """
                                SELECT message_id FROM memory_session_messages
                                WHERE session_id = ? ORDER BY ordinal
                                """,
                                (int(existing_session[0]),),
                            ).fetchall()
                        ]
                inserted_ids = self.store_batch_with_ids(rows, commit=False)
                current_message_ids = []
                for row_hash in row_hashes:
                    resolved = self._conn.execute(
                        """
                        SELECT id FROM messages
                        WHERE user_id = ? AND hash = ?
                        ORDER BY id DESC LIMIT 1
                        """,
                        (self.user_id, row_hash),
                    ).fetchone()
                    if resolved is None:
                        raise RuntimeError("session source row was not stored")
                    current_message_ids.append(int(resolved[0]))
                message_ids = list(
                    dict.fromkeys([*existing_message_ids, *current_message_ids])
                )
                session_pk, source_hash = self._memory.register_session(
                    external_id,
                    message_ids,
                    occurred_at=occurred_at,
                    metadata=base_metadata,
                    commit=False,
                )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                # Batch preparation updates these caches optimistically. A
                # failed compound transaction must restore their durable view.
                self._load_hashes()
                self._load_recent_tokens()
                self._invalidate_embedding_index()
                raise
        return {
            "session_pk": session_pk,
            "session_id": external_id,
            "message_ids": message_ids,
            "stored_message_ids": inserted_ids,
            "stored": len(inserted_ids),
            "source_hash": source_hash,
        }

    def enqueue_compilation(
        self, session_pk: int, source_hash: str, compiler_fingerprint: str
    ) -> int:
        return self._memory.enqueue_job(session_pk, source_hash, compiler_fingerprint)

    def pending_compilations(self, limit: int = 100) -> list[dict]:
        with self._db_lock:
            return self._memory.next_jobs(limit=limit)

    def compilation_job_state(self, job_id: int) -> dict | None:
        """Return content-free lifecycle state for one scoped compiler job."""

        with self._db_lock:
            row = self._conn.execute(
                """
                SELECT j.id, j.status, j.attempts, j.last_error, s.external_id,
                       j.next_attempt_at
                FROM memory_compiler_jobs j
                JOIN memory_sessions s ON s.id = j.session_id
                WHERE j.id = ? AND j.user_id = ?
                """,
                (int(job_id), self.user_id),
            ).fetchone()
        if row is None:
            return None
        return {
            "id": int(row[0]),
            "status": str(row[1]),
            "attempts": int(row[2]),
            "last_error": str(row[3]) if row[3] is not None else None,
            "session_id": str(row[4]),
            "next_attempt_at": float(row[5]) if row[5] is not None else None,
        }

    def load_compiler_session(self, session_pk: int) -> dict:
        return self._memory.load_session(session_pk)

    def load_compiler_reference_claims(
        self,
        session_pk: int,
        *,
        limit: int = 8,
    ) -> list[dict]:
        """Return a small deterministic set of active prior claims.

        This is local lexical retrieval with a recent-keyed fallback. The
        records are compiler hints only; evidence validation remains scoped to
        the current session's raw messages.
        """

        bounded_limit = min(8, max(0, int(limit)))
        if bounded_limit == 0:
            return []
        with self._db_lock:
            session = self._memory.load_session(session_pk)
            seen_terms: set[str] = set()
            terms: list[str] = []
            for message in session["messages"]:
                for term in self._normalized_terms(str(message["content"])):
                    if term in seen_terms:
                        continue
                    seen_terms.add(term)
                    terms.append(term)
                    if len(terms) >= 24:
                        break
                if len(terms) >= 24:
                    break

            selected: list = []
            selected_ids: set[int] = set()
            recent_reserve = min(2, bounded_limit)
            lexical_limit = bounded_limit - recent_reserve
            if terms and lexical_limit:
                fts_query = " OR ".join(
                    f'"{term.replace(chr(34), chr(34) * 2)}"' for term in terms
                )
                table = self._memory.fts_table
                try:
                    rows = self._conn.execute(
                        f"""
                        SELECT c.*
                        FROM {table}
                        JOIN memory_claims c ON c.id = {table}.rowid
                        WHERE {table} MATCH ? AND c.user_id = ?
                          AND c.status = 'active' AND c.session_id != ?
                        ORDER BY bm25({table}),
                                 COALESCE(c.document_time, c.created_at) DESC,
                                 c.id DESC
                        LIMIT ?
                        """,
                        (fts_query, self.user_id, int(session_pk), lexical_limit),
                    ).fetchall()
                except sqlite3.OperationalError:
                    rows = []
                for row in rows:
                    claim_id = int(row["id"])
                    if claim_id not in selected_ids:
                        selected.append(row)
                        selected_ids.add(claim_id)

            recent_rows = self._conn.execute(
                """
                SELECT * FROM memory_claims
                WHERE user_id = ? AND status = 'active' AND session_id != ?
                  AND memory_key != ''
                ORDER BY COALESCE(document_time, valid_from, event_start, created_at) DESC,
                         id DESC
                LIMIT ?
                """,
                (self.user_id, int(session_pk), bounded_limit),
            ).fetchall()
            for row in recent_rows:
                claim_id = int(row["id"])
                if claim_id in selected_ids:
                    continue
                selected.append(row)
                selected_ids.add(claim_id)
                if len(selected) >= bounded_limit:
                    break

        return [
            {
                "claim_id": int(row["id"]),
                "memory_key": str(row["memory_key"] or ""),
                "text": str(row["text"]),
                "document_time": row["document_time"],
                "event_start": row["event_start"],
                "event_end": row["event_end"],
                "valid_from": row["valid_from"],
                "valid_to": row["valid_to"],
            }
            for row in selected[:bounded_limit]
        ]

    def mark_compilation_running(
        self,
        job_id: int,
        *,
        stale_after_seconds: float = 300.0,
    ) -> bool:
        with self._db_lock:
            return self._memory.mark_job_running(
                job_id,
                stale_after_seconds=stale_after_seconds,
            )

    def claim_compilation_attempt(
        self,
        job_id: int,
        *,
        stale_after_seconds: float = 300.0,
    ) -> int | None:
        """Atomically claim a compiler job and return its attempt lease token."""

        with self._db_lock:
            return self._memory.claim_job(
                job_id,
                stale_after_seconds=stale_after_seconds,
            )

    def heartbeat_compilation(self, job_id: int, expected_attempt: int) -> bool:
        """Renew a claimed compiler attempt while a long model call is active."""

        with self._db_lock:
            return self._memory.heartbeat_job(job_id, expected_attempt)

    def obsolete_compilations_except(
        self,
        compiler_fingerprint: str | None,
        *,
        all_scopes: bool = False,
    ) -> int:
        """Make queued work incompatible with the active compiler non-actionable."""

        now = time.time()
        clauses = [
            (
                "status != 'obsolete'"
                if compiler_fingerprint is not None
                else "status IN ('pending', 'failed', 'running')"
            )
        ]
        parameters: list = [now, now]
        if not all_scopes:
            clauses.append("user_id = ?")
            parameters.append(self.user_id)
        if compiler_fingerprint is not None:
            fingerprint = str(compiler_fingerprint).strip()
            if not fingerprint:
                raise ValueError("compiler_fingerprint cannot be empty")
            clauses.append("compiler_fingerprint != ?")
            parameters.append(fingerprint)
        cursor = self._conn.execute(
            f"""
            UPDATE memory_compiler_jobs
            SET status = 'obsolete', next_attempt_at = NULL,
                finished_at = ?, updated_at = ?
            WHERE {" AND ".join(clauses)}
            """,
            parameters,
        )
        self._conn.commit()
        return int(cursor.rowcount)

    def mark_compilation_failed(
        self,
        job_id: int,
        error: str,
        retryable: bool = True,
        *,
        expected_attempt: int | None = None,
        retry_after_seconds: float | None = None,
    ) -> str:
        with self._db_lock:
            return self._memory.mark_job_failed(
                job_id,
                error,
                retryable=retryable,
                expected_attempt=expected_attempt,
                retry_after_seconds=retry_after_seconds,
            )

    def record_compiler_usage(self, job_id: int | None, usage: dict) -> None:
        """Persist content-free usage when no compilation result can be applied."""

        with self._db_lock:
            self._memory.record_usage(job_id, usage)
            self._memory.commit()

    def apply_compilation(
        self,
        job_id: int,
        compiled,
        *,
        processor: str,
        processor_version: str,
        prompt_version: str,
        usage: Optional[dict] = None,
        expected_attempt: int | None = None,
        compiler_warnings: Sequence[str] = (),
    ) -> dict:
        with self._db_lock:
            return self._memory.apply_compilation(
                job_id,
                compiled,
                processor=processor,
                processor_version=processor_version,
                prompt_version=prompt_version,
                usage=usage,
                expected_attempt=expected_attempt,
                compiler_warnings=compiler_warnings,
            )

    def recall_context(
        self,
        query: str,
        *,
        token_budget: int = 6000,
        filters: Optional[dict] = None,
        profile: str = "default",
        explain: bool = False,
        include_derived: bool = True,
    ) -> ContextBundle:
        if include_derived:
            ranked = self.search_memory_blocks(
                query,
                limit=80,
                filters=filters,
                profile=profile,
                include_derived=True,
            )
            bundle = self._memory.compose_ranked_context(
                query,
                ranked.blocks,
                token_budget=token_budget,
                include_debug=explain,
            )
            bundle.query_ms += ranked.query_ms
            if explain:
                bundle.debug["ranked_search"] = {
                    "total_matches": ranked.total_matches,
                    "timings_ms": ranked.timings_ms,
                }
            return bundle

        raw = self.search(
            query,
            limit=80,
            max_context=120,
            full_context_threshold=0,
            filters=filters,
            profile=profile,
        )
        bundle = self._memory.compose_context(
            query,
            raw.messages,
            token_budget=token_budget,
            include_debug=explain,
            include_claims=include_derived,
        )
        bundle.query_ms += raw.query_ms
        if explain:
            bundle.debug["raw_search"] = {
                "total_matches": raw.total_matches,
                "timings_ms": raw.timings_ms,
            }
        return bundle

    def search_memory_blocks(
        self,
        query: str,
        *,
        limit: int = 50,
        filters: Optional[dict] = None,
        profile: str = "default",
        include_derived: bool = True,
        max_chars: int = 1200,
    ) -> MemoryBlockSearchResult:
        """Return concise fused memory candidates without a token budget.

        ``recall_context`` renders these fused candidates into one bounded
        prompt; this method preserves up to the requested number of
        independently ranked candidates for applications, benchmark adapters,
        and rerankers.
        """

        started = time.perf_counter()
        candidate_limit = max(1, int(limit))
        raw = self.search(
            query,
            limit=candidate_limit,
            max_context=candidate_limit,
            full_context_threshold=0,
            filters=filters,
            profile=profile,
            minimum_results=candidate_limit,
        )
        allowed_message_ids = self._message_ids_in_scope(filters) if filters else None
        aggregation_source_ids: tuple[int, ...] | None = None
        aggregation_mode = _aggregation_query_mode(query)
        aggregation_ms = 0.0
        if aggregation_mode is not None:
            aggregation_started = time.perf_counter()
            aggregation_source_ids = self._aggregation_source_candidates(
                query,
                raw,
                filters=filters,
                limit=candidate_limit,
            )
            aggregation_ms = (time.perf_counter() - aggregation_started) * 1000
        fusion_started = time.perf_counter()
        blocks = self._memory.rank_memories(
            query,
            raw.messages,
            limit=candidate_limit,
            include_claims=include_derived,
            max_chars=max_chars,
            # Session-neighborhood expansion currently keys off compiled
            # session membership, not arbitrary provenance filters.  Disable
            # it for filtered retrieval so no out-of-filter sibling can leak.
            include_session_siblings=not bool(filters),
            allowed_message_ids=allowed_message_ids,
            aggregation_source_message_ids=aggregation_source_ids,
            aggregation_mode=aggregation_mode,
        )
        fusion_ms = (time.perf_counter() - fusion_started) * 1000
        query_ms = (time.perf_counter() - started) * 1000
        timings_ms = dict(raw.timings_ms)
        if aggregation_source_ids is not None:
            timings_ms["aggregation_sources"] = aggregation_ms
        timings_ms["memory_fusion"] = fusion_ms
        timings_ms["total"] = query_ms
        return MemoryBlockSearchResult(
            blocks=blocks,
            query_ms=query_ms,
            total_matches=max(raw.total_matches, len(blocks)),
            timings_ms=timings_ms,
        )

    def memory_status(self) -> dict:
        return self._memory.status()

    def enrichment_status(self) -> dict:
        """Return compiler queue, claim, entity, token, and cost totals."""

        return self.memory_status()

    def backfill_derived(
        self,
        compiler_config=None,
        compiler_fingerprint: str | None = None,
    ) -> dict:
        """Queue every registered session for a deterministic compiler fingerprint."""

        if compiler_fingerprint is not None:
            fingerprint = str(compiler_fingerprint).strip()
            if not fingerprint:
                raise ValueError("compiler_fingerprint cannot be empty")
        else:
            if compiler_config is None:
                raise ValueError(
                    "compiler_config or compiler_fingerprint is required for intelligence backfill"
                )
            fingerprint_value = getattr(compiler_config, "fingerprint", None)
            if callable(fingerprint_value):
                fingerprint = str(fingerprint_value())
            elif fingerprint_value:
                fingerprint = str(fingerprint_value)
            else:
                if hasattr(compiler_config, "to_dict"):
                    payload = compiler_config.to_dict()
                elif hasattr(compiler_config, "__dict__"):
                    payload = vars(compiler_config)
                else:
                    payload = str(compiler_config)
                fingerprint = hashlib.sha256(
                    json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
                ).hexdigest()
        sessions = self._conn.execute(
            """
            SELECT id, source_hash FROM memory_sessions
            WHERE user_id = ? ORDER BY occurred_at, id
            """,
            (self.user_id,),
        ).fetchall()
        job_ids = [
            self._memory.enqueue_job(int(row[0]), str(row[1]), fingerprint)
            for row in sessions
        ]
        return {"queued": len(job_ids), "job_ids": job_ids, "fingerprint": fingerprint}

    def purge_derived(self) -> int:
        return self._memory.purge()

    # ── Search ──

    def _expand_query(self, query: str) -> str:
        """Build FTS5 AND query: OR within per-term variants, AND across terms.
        AND intersection keeps matching sets small → fast at scale.
        """
        terms = self._normalized_terms(query)

        if not terms:
            return ""

        seen = set()
        groups = []
        for t in terms:
            if t in seen:
                continue
            seen.add(t)
            variants = {t}
            if t.isalpha():
                s = stem(t)
                if s != t and len(s) >= 3:
                    variants.add(s)
            if len(t) >= 3 and t.isalpha():
                for suffix in ("ing", "ed", "s", "er"):
                    variants.add(t + suffix)
            # FTS5 accumulates per-term BM25 contributions in query order.
            # Iterating a set makes the generated query (and, at floating-point
            # precision, deep-tail scores) depend on PYTHONHASHSEED.
            inner = " OR ".join(f'"{v}"' for v in sorted(variants))
            groups.append(f"({inner})" if len(variants) > 1 else f'"{t}"')

        return " AND ".join(groups)

    def _expand_query_or(self, query: str) -> str:
        """Fallback OR query — used when AND returns too few results."""
        terms = self._normalized_terms(query)
        all_terms = set()
        for t in terms:
            all_terms.add(t)
            if t.isalpha():
                s = stem(t)
                if s != t and len(s) >= 3:
                    all_terms.add(s)
            if len(t) >= 3 and t.isalpha():
                for suffix in ("ing", "ed", "s", "er"):
                    all_terms.add(t + suffix)
        return " OR ".join(f'"{x}"' for x in sorted(all_terms) if x)

    def _semantic_search(
        self,
        query: str,
        limit: int = 15,
        *,
        allowed_message_ids: set[int] | None = None,
    ):
        """Embedding-based semantic search using stored embeddings.
        Bridges vocabulary gaps FTS5 can't: "martial arts" → "kickboxing".
        Returns list of tuples: (id, position, timestamp, score).
        """
        np = self._np

        if not self._ensure_embedding_index() or self._embedding_matrix is None:
            return []

        # Encode query (bounded per-engine LRU: repeated or retried queries
        # skip the model forward pass entirely)
        query_emb = self._query_embedding_cache.get(query)
        if query_emb is None:
            query_emb = _encode_texts(self._sbert, [query], normalize_embeddings=True)
            self._query_embedding_cache[query] = query_emb
            if len(self._query_embedding_cache) > 128:
                self._query_embedding_cache.pop(next(iter(self._query_embedding_cache)))

        if allowed_message_ids is not None:
            if not allowed_message_ids:
                return []
            allowed_indices = np.asarray(
                [
                    index
                    for index, metadata in enumerate(self._embedding_row_meta)
                    if int(metadata[0]) in allowed_message_ids
                ],
                dtype=int,
            )
            if allowed_indices.size == 0:
                return []
            candidate_matrix = self._embedding_matrix[allowed_indices]
        else:
            allowed_indices = None
            candidate_matrix = self._embedding_matrix

        # Cosine similarity (embeddings are normalized). Scoped retrieval
        # masks the matrix before top-k so out-of-filter rows cannot crowd the
        # permitted candidates out of the bounded overfetch.
        scores = np.dot(candidate_matrix, query_emb.T).flatten()

        # Get top-k by similarity under a total, input-order-independent key.
        # Dense backends can emit exact ties (or values equal at float32
        # precision); argsort's tie behavior otherwise follows matrix order.
        if allowed_indices is None:
            candidate_metadata = self._embedding_row_meta
        else:
            candidate_metadata = [
                self._embedding_row_meta[int(index)] for index in allowed_indices
            ]
        message_ids = np.asarray(
            [int(metadata[0]) for metadata in candidate_metadata], dtype=np.int64
        )
        positions = np.asarray(
            [int(metadata[1]) for metadata in candidate_metadata], dtype=np.int64
        )
        top_indices = np.lexsort((-message_ids, -positions, -scores))[:limit]

        results = []
        for candidate_index in top_indices:
            if scores[candidate_index] < 0.20:  # minimum similarity threshold
                break
            idx = (
                int(allowed_indices[candidate_index])
                if allowed_indices is not None
                else int(candidate_index)
            )
            message_id, position, timestamp = self._embedding_row_meta[idx]
            results.append(
                (
                    message_id,
                    position,
                    timestamp,
                    -float(scores[candidate_index]),
                )
            )

        return results

    def _has_strong_lexical_hit(self, query: str, rows: list[tuple]) -> bool:
        if not rows:
            return False

        query_terms = self._tokenize(query)
        if not query_terms:
            return bool(rows)

        candidate_ids = [row[0] for row in rows[: min(len(rows), 3)]]
        placeholders = ",".join("?" for _ in candidate_ids)
        candidate_rows = self._conn.execute(
            f"""
            SELECT id, text, normalized_terms
            FROM messages
            WHERE user_id = ? AND id IN ({placeholders})
        """,
            [self.user_id] + candidate_ids,
        ).fetchall()
        candidates_by_id = {row[0]: row for row in candidate_rows}

        min_overlap = 1 if len(query_terms) <= 3 else 2
        query_lower = query.lower()
        for candidate_id in candidate_ids:
            candidate = candidates_by_id.get(candidate_id)
            text = str(candidate[1]) if candidate else ""
            if candidate and self._use_persisted_terms and candidate[2]:
                text_terms = frozenset(str(candidate[2]).split())
            else:
                text_terms = self._tokenize(text)
            overlap = len(query_terms & text_terms)
            if overlap >= min_overlap:
                return True
            if query_lower and query_lower in text.lower():
                return True
        return False

    def _normalize_filter_values(self, value) -> list[str]:
        if value is None:
            return []
        if isinstance(value, (list, tuple, set)):
            values = [str(item).strip() for item in value if str(item).strip()]
        else:
            text = str(value).strip()
            values = [text] if text else []
        return values

    def _message_filter_sql(
        self, filters: Optional[dict]
    ) -> tuple[str, list[str], list]:
        if not filters:
            return "", [], []

        clauses = []
        params: list = []
        needs_provenance = False

        if "speaker" in filters:
            values = self._normalize_filter_values(filters.get("speaker"))
            if values:
                placeholders = ",".join("?" for _ in values)
                clauses.append(f"m.speaker IN ({placeholders})")
                params.extend(values)

        for key in (
            "provider",
            "model_id",
            "agent_id",
            "run_id",
            "workspace_id",
            "tool_used",
            "response_id",
        ):
            values = self._normalize_filter_values(filters.get(key))
            if values:
                needs_provenance = True
                placeholders = ",".join("?" for _ in values)
                clauses.append(f"mp.{key} IN ({placeholders})")
                params.extend(values)

        join_sql = (
            " LEFT JOIN message_provenance mp ON mp.message_id = m.id AND mp.user_id = m.user_id "
            if needs_provenance
            else ""
        )
        return join_sql, clauses, params

    def _message_ids_in_scope(
        self,
        filters: Optional[dict],
        *,
        speaker: str | None = None,
    ) -> set[int]:
        """Return canonical message IDs satisfying the caller's raw scope."""

        join_sql, clauses, params = self._message_filter_sql(filters)
        if speaker is not None:
            clauses.append("LOWER(m.speaker) = ?")
            params.append(str(speaker).casefold())
        clause_sql = f" AND {' AND '.join(clauses)}" if clauses else ""
        rows = self._conn.execute(
            f"""
            SELECT DISTINCT m.id
            FROM messages m {join_sql}
            WHERE m.user_id = ? {clause_sql}
            """,
            [self.user_id, *params],
        ).fetchall()
        return {int(row[0]) for row in rows}

    def _aggregation_source_candidates(
        self,
        query: str,
        raw: SearchResult,
        *,
        filters: Optional[dict],
        limit: int,
    ) -> tuple[int, ...]:
        """Build a bounded, user-speaker source pool for aggregation."""

        user_scope = self._message_ids_in_scope(filters, speaker="user")
        if not user_scope:
            return ()
        overfetch = min(
            _AGGREGATION_SOURCE_OVERFETCH,
            max(24, max(1, int(limit)) * 2),
        )
        has_explicit_action = bool(_aggregation_query_action_tokens(query))
        selected: list[int] = []
        if has_explicit_action:
            selected.extend(
                self._aggregation_focused_lexical_sources(
                    query,
                    filters=filters,
                    limit=overfetch,
                )
            )
        semantic_query = (
            " ".join(_aggregation_query_focus_terms(query))
            if has_explicit_action
            else query
        )
        if self._sbert is not None and self.semantic_search_mode != "disabled":
            try:
                selected.extend(
                    int(hit[0])
                    for hit in self._semantic_search(
                        semantic_query,
                        limit=overfetch,
                        allowed_message_ids=user_scope,
                    )
                )
            except Exception:
                pass

        # Lexical/direct hits make the path functional when embeddings are
        # disabled or unavailable. Claim-FTS sources are added by the derived
        # store before it builds the final pack.
        for message in [*raw.direct_hits, *raw.messages]:
            message_id = int(getattr(message, "id", 0) or 0)
            if (
                message_id in user_scope
                and str(getattr(message, "speaker", "")).casefold() == "user"
            ):
                selected.append(message_id)
        return tuple(dict.fromkeys(selected))[:overfetch]

    def _aggregation_focused_lexical_sources(
        self,
        query: str,
        *,
        filters: Optional[dict],
        limit: int,
    ) -> tuple[int, ...]:
        """Find aggregate sources by focused action/domain inflections.

        This is a bounded secondary FTS pass used only for aggregation. It
        avoids letting generic count objects and date-window words consume the
        ordinary result limit, and it bridges common ``bake``/``baked`` style
        surface differences without changing global search semantics.
        """

        terms = _aggregation_lexical_surface_terms(query)
        if not terms:
            return ()
        fts_query = " OR ".join(f'"{term}"' for term in terms)
        join_sql, filter_clauses, filter_params = self._message_filter_sql(filters)
        filter_clauses.append("LOWER(m.speaker) = 'user'")
        filter_sql = f" AND {' AND '.join(filter_clauses)}"
        try:
            rows = self._conn.execute(
                f"""
                SELECT m.id
                FROM {self._fts_table}
                JOIN messages m ON m.id = {self._fts_table}.rowid
                {join_sql}
                WHERE {self._fts_table} MATCH ? AND m.user_id = ? {filter_sql}
                ORDER BY bm25({self._fts_table}), m.position, m.id
                LIMIT ?
                """,
                [fts_query, self.user_id, *filter_params, max(1, int(limit))],
            ).fetchall()
        except sqlite3.OperationalError:
            return ()
        return tuple(dict.fromkeys(int(row[0]) for row in rows))

    def _filter_semantic_hits(
        self,
        hits: list[tuple],
        *,
        filters: Optional[dict],
        after: float | None,
        before: float | None,
    ) -> list[tuple]:
        """Apply the same scope filters to dense candidates before fusion."""

        if not hits:
            return []
        candidate_ids = [int(hit[0]) for hit in hits]
        placeholders = ",".join("?" for _ in candidate_ids)
        join_sql, clauses, params = self._message_filter_sql(filters)
        clauses.append(f"m.id IN ({placeholders})")
        params.extend(candidate_ids)
        if after is not None:
            clauses.append("m.timestamp >= ?")
            params.append(after)
        if before is not None:
            clauses.append("m.timestamp <= ?")
            params.append(before)
        rows = self._conn.execute(
            f"""
            SELECT m.id FROM messages m {join_sql}
            WHERE m.user_id = ? AND {" AND ".join(clauses)}
            """,
            [self.user_id, *params],
        ).fetchall()
        allowed = {int(row[0]) for row in rows}
        return [hit for hit in hits if int(hit[0]) in allowed]

    def _artifact_filter_sql(
        self, filters: Optional[dict], alias: str = "a"
    ) -> tuple[list[str], list]:
        if not filters:
            return [], []

        clauses = []
        params: list = []
        for key in (
            "provider",
            "model_id",
            "agent_id",
            "run_id",
            "workspace_id",
            "tool_used",
            "response_id",
            "kind",
            "title",
        ):
            values = self._normalize_filter_values(filters.get(key))
            if values:
                placeholders = ",".join("?" for _ in values)
                clauses.append(f"{alias}.{key} IN ({placeholders})")
                params.extend(values)

        tags = self._normalize_filter_values(filters.get("tag"))
        if tags:
            for tag in tags:
                clauses.append(f"{alias}.tags_json LIKE ?")
                params.append(f'%"{tag}"%')

        return clauses, params

    def _code_chunk_filter_sql(
        self, filters: Optional[dict], alias: str = "c"
    ) -> tuple[list[str], list]:
        if not filters:
            return [], []

        clauses = []
        params: list = []
        for key in (
            "provider",
            "model_id",
            "agent_id",
            "run_id",
            "workspace_id",
            "tool_used",
            "response_id",
            "path",
            "language",
            "kind",
            "symbol",
        ):
            values = self._normalize_filter_values(filters.get(key))
            if values:
                placeholders = ",".join("?" for _ in values)
                clauses.append(f"{alias}.{key} IN ({placeholders})")
                params.extend(values)

        tags = self._normalize_filter_values(filters.get("tag"))
        if tags:
            for tag in tags:
                clauses.append(f"{alias}.tags_json LIKE ?")
                params.append(f'%"{tag}"%')

        return clauses, params

    def _profile_name(self, profile: Optional[str]) -> str:
        normalized = str(profile or "default").strip().lower()
        if normalized in {"default", "fact", "code", "timeline", "audit"}:
            return normalized
        return "default"

    def _profile_config(self, profile: Optional[str]) -> dict:
        normalized = self._profile_name(profile)
        configs = {
            "default": {
                "window_delta": 0,
                "relevance_weight": 0.70,
                "recency_weight": 0.30,
                "code_bonus": 0.24,
                "audit_bonus": 0.0,
                "direct_overlap_floor": 0,
                "semantic_enabled": True,
                "or_fallback_enabled": True,
            },
            "fact": {
                "window_delta": -2,
                "relevance_weight": 0.82,
                "recency_weight": 0.18,
                "code_bonus": 0.14,
                "audit_bonus": 0.0,
                "direct_overlap_floor": 1,
                "semantic_enabled": False,
                "or_fallback_enabled": False,
            },
            "code": {
                "window_delta": -2,
                "relevance_weight": 0.86,
                "recency_weight": 0.14,
                "code_bonus": 0.34,
                "audit_bonus": 0.0,
                "direct_overlap_floor": 0,
                "semantic_enabled": True,
                "or_fallback_enabled": True,
            },
            "timeline": {
                "window_delta": 2,
                "relevance_weight": 0.55,
                "recency_weight": 0.45,
                "code_bonus": 0.12,
                "audit_bonus": 0.0,
                "direct_overlap_floor": 0,
                "semantic_enabled": False,
                "or_fallback_enabled": True,
            },
            "audit": {
                "window_delta": 1,
                "relevance_weight": 0.68,
                "recency_weight": 0.32,
                "code_bonus": 0.18,
                "audit_bonus": 0.28,
                "direct_overlap_floor": 0,
                "semantic_enabled": False,
                "or_fallback_enabled": True,
            },
        }
        return configs[normalized]

    def search(
        self,
        query: str,
        limit: int = 15,
        after: float = None,
        before: float = None,
        max_context: int = 120,
        full_context_threshold: int = 750,
        filters: Optional[dict] = None,
        profile: str = "default",
        minimum_results: int | None = None,
    ) -> SearchResult:
        """Search messages by query. Returns messages with context windows.

        max_context: hard cap on returned messages after recency+relevance scoring.
                     Prevents context explosion at scale. Default 60.
        full_context_threshold: if user has fewer messages than this, return ALL
                                messages instead of searching. Eliminates retrieval
                                loss for small corpora that fit in LLM context. Default 750.
        """
        t0 = time.perf_counter()
        timings_ms: dict[str, float] = {}

        def _total_ms() -> float:
            return (time.perf_counter() - t0) * 1000

        def _empty_result() -> SearchResult:
            total_ms = _total_ms()
            timings_ms["total"] = total_ms
            return SearchResult(
                messages=[],
                query_ms=total_ms,
                total_matches=0,
                direct_hits=[],
                context_messages=[],
                timings_ms=dict(timings_ms),
            )

        profile_name = self._profile_name(profile)
        profile_config = self._profile_config(profile)
        quantity_query_intent = bool(_QUANTITY_QUERY_RE.search(query.lower()))
        # A numeric-state boost helps entity/event counts ("how many fish",
        # "how many times") but is harmful for interval and measurement
        # questions, where several separate dated facts must remain visible.
        # Keep those intents distinct so a generic optimization cannot crowd
        # temporal or monetary evidence out of the answer context.
        measurement_quantity_intent = bool(
            _MEASUREMENT_QUANTITY_RE.search(query.lower())
        )
        state_quantity_intent = (
            quantity_query_intent and not measurement_quantity_intent
        )

        # Small corpus optimization: return everything if it fits in context.
        # 750 messages ≈ 22K tokens — fits in any modern LLM context window.
        # This eliminates retrieval loss entirely for typical conversation sizes.
        if full_context_threshold > 0 and not filters:
            # Use MAX(position) as a fast proxy — avoids full COUNT(*) scan at scale.
            # position is 0-indexed sequential, so max_pos+1 ≈ count.
            max_pos = self._conn.execute(
                "SELECT MAX(position) FROM messages WHERE user_id = ?", (self.user_id,)
            ).fetchone()[0]
            if max_pos is not None and max_pos < full_context_threshold:
                time_clauses = ""
                time_params = []
                if after is not None:
                    time_clauses += " AND timestamp >= ?"
                    time_params.append(after)
                if before is not None:
                    time_clauses += " AND timestamp <= ?"
                    time_params.append(before)
                rows = self._conn.execute(
                    f"""
                    SELECT id, speaker, text, timestamp, position
                    FROM messages
                    WHERE user_id = ? {time_clauses}
                    ORDER BY position
                """,
                    [self.user_id] + time_params,
                ).fetchall()
                provenance_map = self._load_provenance_map([r[0] for r in rows])
                all_messages = [self._message_from_row(r, provenance_map) for r in rows]
                messages = all_messages[: max(0, int(max_context))]
                total_ms = _total_ms()
                timings_ms.update({"context_fetch": total_ms, "total": total_ms})
                return SearchResult(
                    messages=messages,
                    query_ms=total_ms,
                    total_matches=len(all_messages),
                    direct_hits=messages,
                    context_messages=[],
                    timings_ms=timings_ms,
                )

        fts_started = time.perf_counter()
        fts_query = self._expand_query(query)
        if not fts_query:
            return _empty_result()

        # BM25 search on per-user FTS5 table — no cross-user scan.
        # Two-phase strategy at scale:
        #   Phase 1: scan only the most recent RECENT_WINDOW messages (fast, covers 99% of queries)
        #   Phase 2: fall back to full-corpus scan if Phase 1 returns too few results
        # This bounds the ORDER BY bm25 sort set regardless of total DB size.
        RECENT_WINDOW = 100_000

        max_rowid = (
            self._conn.execute(f"SELECT MAX(rowid) FROM {self._fts_table}").fetchone()[
                0
            ]
            or 0
        )
        recent_threshold = max(0, max_rowid - RECENT_WINDOW)

        join_sql, filter_clauses, filter_params = self._message_filter_sql(filters)
        filter_sql = f" AND {' AND '.join(filter_clauses)}" if filter_clauses else ""
        range_clauses = []
        range_params = []
        if after is not None:
            range_clauses.append("m.timestamp >= ?")
            range_params.append(after)
        if before is not None:
            range_clauses.append("m.timestamp <= ?")
            range_params.append(before)
        range_sql = f" AND {' AND '.join(range_clauses)}" if range_clauses else ""

        def _run_fts(q, rowid_gt=0):
            if rowid_gt > 0:
                return self._conn.execute(
                    f"""
                    SELECT m.id, m.position, m.timestamp, bm25({self._fts_table}) as score
                    FROM {self._fts_table}
                    JOIN messages m ON m.id = {self._fts_table}.rowid
                    {join_sql}
                    WHERE {self._fts_table} MATCH ? AND {self._fts_table}.rowid > ? {filter_sql} {range_sql}
                    ORDER BY bm25({self._fts_table}), m.position, m.id
                    LIMIT ?
                """,
                    [q, rowid_gt, *filter_params, *range_params, limit],
                ).fetchall()
            else:
                return self._conn.execute(
                    f"""
                    SELECT m.id, m.position, m.timestamp, bm25({self._fts_table}) as score
                    FROM {self._fts_table}
                    JOIN messages m ON m.id = {self._fts_table}.rowid
                    {join_sql}
                    WHERE {self._fts_table} MATCH ? {filter_sql} {range_sql}
                    ORDER BY bm25({self._fts_table}), m.position, m.id
                    LIMIT ?
                """,
                    [q, *filter_params, *range_params, limit],
                ).fetchall()

        try:
            rows = _run_fts(fts_query, recent_threshold)
        except Exception:
            rows = []

        # Phase 2: full-corpus fallback (old memories or very specific queries)
        if len(rows) < limit // 2 and recent_threshold > 0:
            try:
                full_rows = _run_fts(fts_query, 0)
                if len(full_rows) > len(rows):
                    rows = full_rows
            except Exception:
                pass

        # Judge lexical strength before the broader OR fallback. A generic OR
        # match must not suppress semantic retrieval for a true vocabulary gap.
        and_match_count = len(rows)

        # AND returned too few results → try OR (multi-hop / vague queries)
        if profile_config["or_fallback_enabled"] and len(rows) < limit:
            or_query = self._expand_query_or(query)
            if or_query and or_query != fts_query:
                try:
                    or_rows = _run_fts(or_query, recent_threshold)
                    if len(or_rows) > len(rows):
                        rows = or_rows
                    elif len(or_rows) < limit // 2 and recent_threshold > 0:
                        full_or = _run_fts(or_query, 0)
                        if len(full_or) > len(rows):
                            rows = full_or
                except Exception:
                    pass

        strong_lexical_hit = self._has_strong_lexical_hit(query, rows)
        semantic_gap_override = state_quantity_intent and and_match_count == 0

        timings_ms["fts"] = (time.perf_counter() - fts_started) * 1000

        # Phase 3: fuse dense candidates. Private mode keeps the original
        # fallback-only hot path; intelligence mode can opt into always-on
        # hybrid retrieval without introducing a query-time network call.
        semantic_rank: dict[int, int] = {}
        top_semantic_ids: set[int] = set()
        should_run_semantic = (
            profile_config["semantic_enabled"]
            and self._sbert is not None
            and self.semantic_search_mode != "disabled"
            and (
                self.semantic_search_mode == "hybrid"
                or not strong_lexical_hit
                or semantic_gap_override
            )
        )
        semantic_started = time.perf_counter()
        if should_run_semantic:
            try:
                semantic_hits = self._semantic_search(query, limit=limit)
                semantic_hits = self._filter_semantic_hits(
                    semantic_hits,
                    filters=filters,
                    after=after,
                    before=before,
                )
                semantic_rank = {
                    hit[0]: index for index, hit in enumerate(semantic_hits)
                }
                top_semantic_ids = {
                    hit[0]
                    for hit in semantic_hits[: min(10, len(semantic_hits))]
                    if -float(hit[3]) >= 0.40
                }
                # Merge: keep FTS rows, add semantic hits that aren't already in the set
                existing_ids = {r[0] for r in rows}
                for hit in semantic_hits:
                    if hit[0] not in existing_ids:
                        rows.append(hit)
                        existing_ids.add(hit[0])
            except Exception:
                pass
        timings_ms["semantic"] = (time.perf_counter() - semantic_started) * 1000

        if not rows:
            return _empty_result()

        total_matches = len(rows)
        direct_only_profile = profile_name in {"fact", "timeline", "audit"}
        if direct_only_profile:
            rows = rows[:limit]

        # Collect positions with context, tracking which are direct hits vs context
        hit_positions = {
            r[1]: (r[2], r[3]) for r in rows
        }  # pos -> (timestamp, bm25_score)
        time_clauses = ""
        time_params = []
        if after is not None:
            time_clauses += " AND m.timestamp >= ?"
            time_params.append(after)
        if before is not None:
            time_clauses += " AND m.timestamp <= ?"
            time_params.append(before)

        context_started = time.perf_counter()
        if direct_only_profile:
            direct_ids = [r[0] for r in rows]
            if not direct_ids:
                return _empty_result()
            placeholders = ",".join("?" for _ in direct_ids)
            fetched_rows = self._conn.execute(
                f"""
                SELECT m.id, m.speaker, m.text, m.timestamp, m.position, m.normalized_terms
                FROM messages m
                WHERE m.user_id = ? AND m.id IN ({placeholders}) {time_clauses}
                """,
                [self.user_id] + direct_ids + time_params,
            ).fetchall()
            rows_by_id = {row[0]: row for row in fetched_rows}
            result_rows = [
                rows_by_id[row_id] for row_id in direct_ids if row_id in rows_by_id
            ]
        else:
            # Dynamic context window: few matches = wider, many matches = tighter
            if total_matches <= 3:
                window = self.context_window + 3
            elif total_matches >= 10:
                window = max(2, self.context_window - 2)
            else:
                window = self.context_window
            window = max(1, window + profile_config["window_delta"])

            positions = set()
            for pos in hit_positions:
                start = max(0, pos - window)
                end = pos + window
                for p in range(start, end + 1):
                    positions.add(p)

            if not positions:
                return _empty_result()

            placeholders = ",".join("?" for _ in positions)
            result_rows = self._conn.execute(
                f"""
                SELECT m.id, m.speaker, m.text, m.timestamp, m.position, m.normalized_terms
                FROM messages m
                {join_sql}
                WHERE m.user_id = ? AND m.position IN ({placeholders})
                      {filter_sql} {time_clauses}
                ORDER BY position, m.id
            """,
                [self.user_id] + sorted(positions) + filter_params + time_params,
            ).fetchall()

        timings_ms["context_fetch"] = (time.perf_counter() - context_started) * 1000

        if not result_rows:
            return _empty_result()

        # ── Recency + relevance scoring ──
        rerank_started = time.perf_counter()
        # Score = bm25_relevance × recency_decay
        # bm25 scores are negative in SQLite (lower = better match), normalize to [0,1]
        # recency_decay: half-life of 30 days — recent messages score higher
        # Anchor recency to persisted corpus state, not the search wall clock.
        # The old clock-based factor changed the balance between relevance and
        # recency as time passed, which could rotate near-tied deep results even
        # when the database, query, and candidate multiset were byte-identical.
        # Arrival time preserves the production meaning of "recent" for
        # imported historical messages while remaining stable across replays.
        recency_reference_row = self._conn.execute(
            """
            SELECT timestamp, CAST(strftime('%s', created_at) AS REAL)
            FROM messages
            WHERE user_id = ?
            ORDER BY position DESC, id DESC
            LIMIT 1
            """,
            (self.user_id,),
        ).fetchone()
        recency_reference_candidates = [float(row[3]) for row in result_rows]
        if recency_reference_row is not None:
            recency_reference_candidates.extend(
                float(value) for value in recency_reference_row if value is not None
            )
        recency_reference = max(recency_reference_candidates)
        HALF_LIFE_SECS = 30 * 86400  # 30 days
        DECAY_LAMBDA = 0.693 / HALF_LIFE_SECS  # ln(2) / half_life

        # Get max/min BM25 for normalization (scores are negative)
        all_bm25 = [v[1] for v in hit_positions.values()]
        min_bm25 = min(all_bm25)  # most negative = best match
        max_bm25 = max(all_bm25)
        bm25_range = max_bm25 - min_bm25 if max_bm25 != min_bm25 else 1.0

        query_terms = self._tokenize(query)
        query_lower = query.lower()
        money_intent = bool(_MONEY_INTENT_RE.search(query_lower))
        quantity_intent = state_quantity_intent
        current_intent = bool(_CURRENT_INTENT_RE.search(query_lower))
        historical_marker = bool(_HISTORICAL_MARKER_RE.search(query_lower))
        # A historical word can describe an entity instead of the requested
        # answer: "an old colleague from my previous company, currently at X".
        # Prefer the explicit current target unless the interrogative itself is
        # past-directed ("What was ... before the current status?").
        past_target_intent = bool(_PAST_TARGET_RE.search(query_lower))
        before_intent = historical_marker and (not current_intent or past_target_intent)
        code_locator_intent = (
            bool(
                _CODE_LOCATOR_WH_RE.search(query_lower)
                and _CODE_LOCATOR_NOUN_RE.search(query_lower)
            )
            or bool(_CODE_LOCATOR_VERB_RE.search(query_lower))
            or profile_name == "code"
        )
        audit_intent = (
            bool(_AUDIT_INTENT_RE.search(query_lower)) or profile_name == "audit"
        )

        provenance_map = self._load_provenance_map([r[0] for r in result_rows])

        # Single fused pass: base score, intent bonuses, and direct-hit
        # candidacy are computed together so each row's text is lowered,
        # tokenized, and pattern-matched exactly once. Evidence patterns run
        # only when the corresponding query intent consumes their result.
        relevance_weight = profile_config["relevance_weight"]
        recency_weight = profile_config["recency_weight"]
        required_direct_overlap = (1 if len(query_terms) <= 3 else 2) + profile_config[
            "direct_overlap_floor"
        ]
        need_state_markers = (
            current_intent or before_intent or (money_intent and current_intent)
        )
        reranked = []
        for r in result_rows:
            msg_pos = r[4]

            # Relevance: is this a direct hit or context?
            if msg_pos in hit_positions:
                raw_bm25 = hit_positions[msg_pos][1]
                # Normalize: best match (most negative) → 1.0, worst → 0.0
                relevance = 1.0 - (raw_bm25 - min_bm25) / bm25_range
            else:
                relevance = 0.15  # context window messages get a small base score

            # Recency: exponential decay from now
            age_secs = max(0, recency_reference - r[3])
            recency = math.exp(-DECAY_LAMBDA * age_secs)
            score = relevance_weight * relevance + recency_weight * recency

            text_lower = r[2].lower()
            padded = f" {text_lower} "
            message_terms = self._stored_message_terms(r)
            overlap = len(message_terms & query_terms) if query_terms else 0
            bonus = 0.25 * (overlap / max(len(query_terms), 1))
            if overlap >= required_direct_overlap:
                bonus += 0.12
            if query_lower and query_lower in text_lower:
                bonus += 0.25

            has_money = money_intent and bool(_MONEY_EVIDENCE_RE.search(text_lower))
            if has_money:
                bonus += 0.35
            if quantity_intent:
                numeric_mentions = len(_NUMERIC_MENTION_RE.findall(text_lower))
                if numeric_mentions and _QUANTITY_FACT_RE.search(text_lower):
                    bonus += 0.25 + min(0.35, 0.10 * numeric_mentions)
                if " now " in padded or " so far " in padded or " to date " in padded:
                    bonus += 0.35

            has_historical_marker = need_state_markers and " was " in padded
            if current_intent:
                if (
                    "changed to" in text_lower
                    or "current" in text_lower
                    or "latest" in text_lower
                    or "took over" in text_lower
                    or " now " in text_lower
                    or "so far" in text_lower
                    or "to date" in text_lower
                ):
                    bonus += 0.22
                elif has_historical_marker:
                    bonus -= 0.18
                elif _DATE_EVIDENCE_RE.search(text_lower):
                    bonus += 0.12
            # "current"/"currently" both contain "current"; keep the shared
            # marker set for candidacy demotion below.
            is_later_state = need_state_markers and (
                "took over" in text_lower
                or "changed to" in text_lower
                or "latest" in text_lower
                or "current" in text_lower
            )
            if before_intent and is_later_state:
                bonus -= 0.15
            if code_locator_intent and _CODE_EVIDENCE_RE.search(r[2]):
                bonus += profile_config["code_bonus"]
            if audit_intent and _AUDIT_EVIDENCE_RE.search(r[2]):
                bonus += profile_config["audit_bonus"]
            if r[0] in semantic_rank:
                bonus += max(0.0, 0.35 - 0.05 * semantic_rank[r[0]])

            direct_candidate = (
                msg_pos in hit_positions and overlap >= required_direct_overlap
            )
            if should_run_semantic and r[0] in top_semantic_ids:
                direct_candidate = True
            if money_intent and not has_money:
                direct_candidate = False
            if (
                money_intent
                and current_intent
                and has_historical_marker
                and not is_later_state
            ):
                direct_candidate = False
            if before_intent and is_later_state:
                direct_candidate = False

            reranked.append((score + bonus, overlap, r, direct_candidate))

        reranked.sort(key=lambda x: (-x[0], -x[1], x[2][4], x[2][0]))

        direct_rows = []
        context_rows = []
        for rerank_score, overlap, r, direct_candidate in reranked:
            if direct_candidate:
                direct_rows.append((rerank_score, r))
            else:
                context_rows.append((rerank_score, r))

        direct_rows = self._shape_results(
            direct_rows,
            provenance_map=provenance_map,
            current_intent=current_intent,
            before_intent=before_intent,
            minimum_results=minimum_results,
        )

        direct_hits = [
            self._message_from_row(r, provenance_map) for _, r in direct_rows
        ]
        direct_scores = [score for score, _ in direct_rows]
        context_candidates = [
            self._message_from_row(r, provenance_map) for _, r in context_rows
        ]

        if direct_only_profile:
            if direct_hits:
                messages = direct_hits[:max_context]
            else:
                messages = context_candidates[:max_context]
            context_messages = []
        elif len(direct_hits) >= max_context:
            direct_hits = direct_hits[:max_context]
            context_messages = []
            messages = direct_hits
        else:
            context_budget = max_context - len(direct_hits)
            context_messages = context_candidates[:context_budget]
            messages = direct_hits + context_messages

        timings_ms["rerank"] = (time.perf_counter() - rerank_started) * 1000
        total_ms = _total_ms()
        timings_ms["total"] = total_ms
        return SearchResult(
            messages=messages,
            query_ms=total_ms,
            total_matches=total_matches,
            direct_hits=direct_hits,
            context_messages=context_messages,
            timings_ms=timings_ms,
            scores=direct_scores[: len(direct_hits)],
        )

    def recall(
        self,
        query: str,
        limit: int = 15,
        after: float = None,
        before: float = None,
        max_context: int = 120,
        full_context_threshold: int = 750,
        filters: Optional[dict] = None,
        profile: str = "default",
    ) -> SearchResult:
        """Compatibility alias for search()."""
        return self.search(
            query=query,
            limit=limit,
            after=after,
            before=before,
            max_context=max_context,
            full_context_threshold=full_context_threshold,
            filters=filters,
            profile=profile,
        )

    def store_code_chunk(
        self,
        path: str,
        content: str,
        symbol: str = "",
        language: str = "",
        kind: str = "chunk",
        summary: str = "",
        start_line: int | None = None,
        end_line: int | None = None,
        tags: Optional[list | tuple | set] = None,
        timestamp: float = None,
        provenance: Optional[dict] = None,
    ) -> Optional[int]:
        resolved_language = self._infer_language(path, language)
        normalized_provenance = self._normalize_provenance(provenance)
        normalized_tags = self._normalize_tags(tags)
        chunk_hash = self._code_chunk_hash(
            path=path,
            language=resolved_language,
            kind=kind,
            symbol=symbol,
            content=content,
            summary=summary,
            start_line=start_line,
            end_line=end_line,
            tags=normalized_tags,
            provenance=normalized_provenance,
        )
        existing = self._conn.execute(
            "SELECT id FROM code_chunks WHERE user_id = ? AND hash = ?",
            (self.user_id, chunk_hash),
        ).fetchone()
        if existing:
            return None

        if timestamp is None:
            timestamp = time.time()

        cursor = self._conn.execute(
            """
            INSERT INTO code_chunks (
                user_id, path, language, kind, symbol, content, summary, start_line, end_line,
                tags_json, hash, timestamp, provider, model_id, agent_id, run_id,
                workspace_id, tool_used, response_id, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                self.user_id,
                path,
                resolved_language,
                kind,
                symbol,
                content,
                summary,
                start_line,
                end_line,
                json.dumps(normalized_tags, sort_keys=True)
                if normalized_tags
                else None,
                chunk_hash,
                timestamp,
                normalized_provenance.get("provider"),
                normalized_provenance.get("model_id"),
                normalized_provenance.get("agent_id"),
                normalized_provenance.get("run_id"),
                normalized_provenance.get("workspace_id"),
                normalized_provenance.get("tool_used"),
                normalized_provenance.get("response_id"),
                json.dumps(normalized_provenance.get("metadata"), sort_keys=True)
                if normalized_provenance.get("metadata")
                else None,
            ),
        )
        chunk_id = cursor.lastrowid
        self._conn.execute(
            f"INSERT INTO {self._code_fts_table}(rowid, text, path, symbol, language, kind) VALUES (?,?,?,?,?,?)",
            (
                chunk_id,
                self._code_chunk_searchable_text(
                    path=path,
                    language=resolved_language,
                    kind=kind,
                    symbol=symbol,
                    content=content,
                    summary=summary,
                    tags=normalized_tags,
                    provenance=normalized_provenance,
                ),
                path,
                symbol,
                resolved_language,
                kind,
            ),
        )
        self._conn.commit()
        return chunk_id

    def ingest_code_file(
        self,
        path: str,
        content: str,
        language: str = "",
        provenance: Optional[dict] = None,
        timestamp: float = None,
        chunk_lines: int = 120,
    ) -> int:
        count = 0
        for chunk in self._extract_code_chunks(
            path=path, content=content, language=language, chunk_lines=chunk_lines
        ):
            chunk_id = self.store_code_chunk(
                path=chunk["path"],
                content=chunk["content"],
                symbol=chunk.get("symbol", ""),
                language=chunk.get("language", language),
                kind=chunk.get("kind", "chunk"),
                summary=chunk.get("summary", ""),
                start_line=chunk.get("start_line"),
                end_line=chunk.get("end_line"),
                tags=chunk.get("tags"),
                timestamp=timestamp,
                provenance=provenance,
            )
            if chunk_id is not None:
                count += 1
        return count

    def _code_chunk_from_row(self, row) -> CodeChunk:
        return CodeChunk(
            id=row[0],
            path=row[1],
            language=row[2] or "",
            kind=row[3],
            symbol=row[4] or "",
            content=row[5],
            summary=row[6] or "",
            start_line=row[7],
            end_line=row[8],
            timestamp=row[9],
            tags=self._decode_json_list(row[10]),
            provenance=self._code_chunk_row_provenance(row, offset=11),
        )

    def search_code(
        self,
        query: str,
        limit: int = 10,
        after: float = None,
        before: float = None,
        filters: Optional[dict] = None,
        profile: str = "code",
    ) -> CodeChunkSearchResult:
        t0 = time.time()
        fts_query = self._expand_query(query)
        if not fts_query:
            return CodeChunkSearchResult(chunks=[], query_ms=0.0, total_matches=0)

        filter_clauses, filter_params = self._code_chunk_filter_sql(filters, alias="c")
        time_clauses = []
        time_params = []
        if after is not None:
            time_clauses.append("c.timestamp >= ?")
            time_params.append(after)
        if before is not None:
            time_clauses.append("c.timestamp <= ?")
            time_params.append(before)

        where_parts = [f"{self._code_fts_table} MATCH ?"]
        if filter_clauses:
            where_parts.extend(filter_clauses)
        if time_clauses:
            where_parts.extend(time_clauses)
        where_sql = " AND ".join(where_parts)

        rows = self._conn.execute(
            f"""
            SELECT c.id, c.path, c.language, c.kind, c.symbol, c.content, c.summary, c.start_line, c.end_line,
                   c.timestamp, c.tags_json, c.provider, c.model_id, c.agent_id, c.run_id, c.workspace_id,
                   c.tool_used, c.response_id, c.metadata_json, bm25({self._code_fts_table}) as score
            FROM {self._code_fts_table}
            JOIN code_chunks c ON c.id = {self._code_fts_table}.rowid
            WHERE {where_sql} AND c.user_id = ?
            ORDER BY bm25({self._code_fts_table})
            LIMIT ?
            """,
            [fts_query, *filter_params, *time_params, self.user_id, limit],
        ).fetchall()

        if not rows and query:
            or_query = self._expand_query_or(query)
            if or_query and or_query != fts_query:
                rows = self._conn.execute(
                    f"""
                    SELECT c.id, c.path, c.language, c.kind, c.symbol, c.content, c.summary, c.start_line, c.end_line,
                           c.timestamp, c.tags_json, c.provider, c.model_id, c.agent_id, c.run_id, c.workspace_id,
                           c.tool_used, c.response_id, c.metadata_json, bm25({self._code_fts_table}) as score
                    FROM {self._code_fts_table}
                    JOIN code_chunks c ON c.id = {self._code_fts_table}.rowid
                    WHERE {self._code_fts_table} MATCH ? {(" AND " + " AND ".join(filter_clauses)) if filter_clauses else ""} {(" AND " + " AND ".join(time_clauses)) if time_clauses else ""} AND c.user_id = ?
                    ORDER BY bm25({self._code_fts_table})
                    LIMIT ?
                    """,
                    [or_query, *filter_params, *time_params, self.user_id, limit],
                ).fetchall()

        profile_name = self._profile_name(profile)
        query_terms = self._tokenize(query)
        relation_bonus: dict[int, float] = {}
        try:
            artifact_hits = self.search_artifacts(query=query, limit=min(5, limit))
            artifact_rank = {
                artifact.id: idx for idx, artifact in enumerate(artifact_hits.artifacts)
            }
            if artifact_rank:
                placeholders = ",".join("?" for _ in artifact_rank)
                relation_rows = self._conn.execute(
                    f"""
                    SELECT source_type, source_id, target_type, target_id
                    FROM relations
                    WHERE user_id = ?
                      AND (
                        (source_type = 'artifact' AND source_id IN ({placeholders}) AND target_type = 'code_chunk')
                        OR
                        (target_type = 'artifact' AND target_id IN ({placeholders}) AND source_type = 'code_chunk')
                      )
                    """,
                    [self.user_id, *artifact_rank.keys(), *artifact_rank.keys()],
                ).fetchall()
                for relation_row in relation_rows:
                    artifact_id = (
                        relation_row[1]
                        if relation_row[0] == "artifact"
                        else relation_row[3]
                    )
                    chunk_id = (
                        relation_row[3]
                        if relation_row[2] == "code_chunk"
                        else relation_row[1]
                    )
                    bonus = max(
                        0.0,
                        2.40
                        - 0.25 * artifact_rank.get(artifact_id, len(artifact_rank)),
                    )
                    relation_bonus[chunk_id] = max(
                        relation_bonus.get(chunk_id, 0.0), bonus
                    )
        except Exception:
            relation_bonus = {}

        reranked = []
        for row in rows:
            text = "\n".join(
                [
                    row[1] or "",
                    row[2] or "",
                    row[3] or "",
                    row[4] or "",
                    row[5] or "",
                    row[6] or "",
                ]
            )
            text_terms = self._tokenize(text)
            overlap = len(query_terms & text_terms) if query_terms else 0
            bonus = 0.22 * (overlap / max(len(query_terms), 1))
            if row[4]:
                symbol_terms = self._tokenize(row[4])
                symbol_overlap = len(query_terms & symbol_terms) if query_terms else 0
                bonus += 0.30 * symbol_overlap
            if row[1]:
                path_terms = self._tokenize(row[1])
                path_overlap = len(query_terms & path_terms) if query_terms else 0
                bonus += 0.18 * path_overlap
            if profile_name == "code":
                if row[4]:
                    bonus += 0.10
                if row[3] in {
                    "function",
                    "class",
                    "interface",
                    "type",
                    "struct",
                    "enum",
                }:
                    bonus += 0.08
            linked_bonus = relation_bonus.get(row[0], 0.0)
            base_score = -(row[19] or 0.0) + bonus
            reranked.append(
                (1 if linked_bonus > 0 else 0, linked_bonus, base_score, overlap, row)
            )

        reranked.sort(
            key=lambda item: (
                -item[0],
                -item[1],
                -item[2],
                -item[3],
                item[4][7] or 0,
                item[4][0],
            )
        )
        chunks = [
            self._code_chunk_from_row(row) for _, _, _, _, row in reranked[:limit]
        ]
        return CodeChunkSearchResult(
            chunks=chunks,
            query_ms=(time.time() - t0) * 1000,
            total_matches=len(chunks),
        )

    def link_records(
        self,
        source_type: str,
        source_id: int,
        target_type: str,
        target_id: int,
        relation_type: str,
        weight: float = 1.0,
        metadata: Optional[dict] = None,
        timestamp: float = None,
    ) -> Optional[int]:
        normalized_metadata = {
            str(key): value for key, value in (metadata or {}).items()
        }
        relation_hash = self._relation_hash(
            source_type=source_type,
            source_id=source_id,
            target_type=target_type,
            target_id=target_id,
            relation_type=relation_type,
            metadata=normalized_metadata,
        )
        existing = self._conn.execute(
            "SELECT id FROM relations WHERE user_id = ? AND hash = ?",
            (self.user_id, relation_hash),
        ).fetchone()
        if existing:
            return None
        if timestamp is None:
            timestamp = time.time()
        cursor = self._conn.execute(
            """
            INSERT INTO relations (
                user_id, source_type, source_id, target_type, target_id,
                relation_type, weight, timestamp, metadata_json, hash
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                self.user_id,
                source_type,
                source_id,
                target_type,
                target_id,
                relation_type,
                float(weight),
                timestamp,
                json.dumps(normalized_metadata, sort_keys=True)
                if normalized_metadata
                else None,
                relation_hash,
            ),
        )
        self._conn.commit()
        return cursor.lastrowid

    def related_records(
        self,
        record_type: str,
        record_id: int,
        relation_type: str | None = None,
        limit: int = 20,
    ) -> list[Relation]:
        clauses = [
            "user_id = ?",
            "((source_type = ? AND source_id = ?) OR (target_type = ? AND target_id = ?))",
        ]
        params: list = [self.user_id, record_type, record_id, record_type, record_id]
        if relation_type:
            clauses.append("relation_type = ?")
            params.append(relation_type)
        rows = self._conn.execute(
            f"""
            SELECT id, source_type, source_id, target_type, target_id, relation_type, weight, timestamp, metadata_json
            FROM relations
            WHERE {" AND ".join(clauses)}
            ORDER BY weight DESC, timestamp DESC, id DESC
            LIMIT ?
            """,
            [*params, limit],
        ).fetchall()
        relations = []
        for row in rows:
            metadata = {}
            if row[8]:
                try:
                    parsed = json.loads(row[8])
                    if isinstance(parsed, dict):
                        metadata = parsed
                except Exception:
                    metadata = {}
            relations.append(
                Relation(
                    id=row[0],
                    source_type=row[1],
                    source_id=row[2],
                    target_type=row[3],
                    target_id=row[4],
                    relation_type=row[5],
                    weight=row[6],
                    timestamp=row[7],
                    metadata=metadata,
                )
            )
        return relations

    def store_artifact(
        self,
        kind: str,
        title: str,
        content: str,
        summary: str = "",
        tags: Optional[list | tuple | set] = None,
        timestamp: float = None,
        provenance: Optional[dict] = None,
    ) -> Optional[int]:
        normalized_provenance = self._normalize_provenance(provenance)
        normalized_tags = self._normalize_tags(tags)
        artifact_hash = self._artifact_hash(
            kind, title, content, summary, normalized_tags, normalized_provenance
        )

        existing = self._conn.execute(
            "SELECT id FROM artifacts WHERE user_id = ? AND hash = ?",
            (self.user_id, artifact_hash),
        ).fetchone()
        if existing:
            return None

        if timestamp is None:
            timestamp = time.time()

        cursor = self._conn.execute(
            """
            INSERT INTO artifacts (
                user_id, kind, title, content, summary, tags_json, hash, timestamp,
                provider, model_id, agent_id, run_id, workspace_id, tool_used, response_id, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                self.user_id,
                kind,
                title,
                content,
                summary,
                json.dumps(normalized_tags, sort_keys=True)
                if normalized_tags
                else None,
                artifact_hash,
                timestamp,
                normalized_provenance.get("provider"),
                normalized_provenance.get("model_id"),
                normalized_provenance.get("agent_id"),
                normalized_provenance.get("run_id"),
                normalized_provenance.get("workspace_id"),
                normalized_provenance.get("tool_used"),
                normalized_provenance.get("response_id"),
                json.dumps(normalized_provenance.get("metadata"), sort_keys=True)
                if normalized_provenance.get("metadata")
                else None,
            ),
        )
        artifact_id = cursor.lastrowid
        self._conn.execute(
            f"INSERT INTO {self._artifact_fts_table}(rowid, text, kind, title) VALUES (?,?,?,?)",
            (
                artifact_id,
                self._artifact_searchable_text(
                    kind,
                    title,
                    content,
                    summary,
                    normalized_tags,
                    normalized_provenance,
                ),
                kind,
                title,
            ),
        )
        self._conn.commit()
        return artifact_id

    def _artifact_from_row(self, row) -> Artifact:
        return Artifact(
            id=row[0],
            kind=row[1],
            title=row[2],
            content=row[3],
            summary=row[4] or "",
            timestamp=row[5],
            tags=self._decode_json_list(row[6]),
            provenance=self._artifact_row_provenance(row, offset=7),
        )

    def search_artifacts(
        self,
        query: str,
        limit: int = 10,
        after: float = None,
        before: float = None,
        filters: Optional[dict] = None,
    ) -> ArtifactSearchResult:
        t0 = time.time()
        fts_query = self._expand_query(query)
        if not fts_query:
            return ArtifactSearchResult(artifacts=[], query_ms=0.0, total_matches=0)

        filter_clauses, filter_params = self._artifact_filter_sql(filters, alias="a")
        time_clauses = []
        time_params = []
        if after is not None:
            time_clauses.append("a.timestamp >= ?")
            time_params.append(after)
        if before is not None:
            time_clauses.append("a.timestamp <= ?")
            time_params.append(before)

        where_parts = [f"{self._artifact_fts_table} MATCH ?"]
        if filter_clauses:
            where_parts.extend(filter_clauses)
        if time_clauses:
            where_parts.extend(time_clauses)
        where_sql = " AND ".join(where_parts)

        rows = self._conn.execute(
            f"""
            SELECT a.id, a.kind, a.title, a.content, a.summary, a.timestamp, a.tags_json,
                   a.provider, a.model_id, a.agent_id, a.run_id, a.workspace_id, a.tool_used, a.response_id, a.metadata_json,
                   bm25({self._artifact_fts_table}) as score
            FROM {self._artifact_fts_table}
            JOIN artifacts a ON a.id = {self._artifact_fts_table}.rowid
            WHERE {where_sql} AND a.user_id = ?
            ORDER BY bm25({self._artifact_fts_table})
            LIMIT ?
            """,
            [fts_query, *filter_params, *time_params, self.user_id, limit],
        ).fetchall()

        if not rows and query:
            or_query = self._expand_query_or(query)
            if or_query and or_query != fts_query:
                or_where_parts = [f"{self._artifact_fts_table} MATCH ?"]
                if filter_clauses:
                    or_where_parts.extend(filter_clauses)
                if time_clauses:
                    or_where_parts.extend(time_clauses)
                or_where_sql = " AND ".join(or_where_parts)
                rows = self._conn.execute(
                    f"""
                    SELECT a.id, a.kind, a.title, a.content, a.summary, a.timestamp, a.tags_json,
                           a.provider, a.model_id, a.agent_id, a.run_id, a.workspace_id, a.tool_used, a.response_id, a.metadata_json,
                           bm25({self._artifact_fts_table}) as score
                    FROM {self._artifact_fts_table}
                    JOIN artifacts a ON a.id = {self._artifact_fts_table}.rowid
                    WHERE {or_where_sql} AND a.user_id = ?
                    ORDER BY bm25({self._artifact_fts_table})
                    LIMIT ?
                    """,
                    [or_query, *filter_params, *time_params, self.user_id, limit],
                ).fetchall()

        artifacts = [self._artifact_from_row(row) for row in rows]
        return ArtifactSearchResult(
            artifacts=artifacts,
            query_ms=(time.time() - t0) * 1000,
            total_matches=len(artifacts),
        )

    # ── Delete ──

    def delete(self, message_id: int) -> bool:
        """Delete a specific message."""
        cursor = self._conn.execute(
            "DELETE FROM messages WHERE id = ? AND user_id = ?",
            (message_id, self.user_id),
        )
        if cursor.rowcount > 0:
            self._conn.execute(
                f"DELETE FROM {self._fts_table} WHERE rowid = ?", (message_id,)
            )
            self._conn.execute(
                "DELETE FROM embeddings WHERE message_id = ? AND user_id = ?",
                (message_id, self.user_id),
            )
            self._conn.execute(
                "DELETE FROM message_provenance WHERE message_id = ? AND user_id = ?",
                (message_id, self.user_id),
            )
            self._conn.execute(
                "DELETE FROM automatic_memories WHERE message_id = ? AND user_id = ?",
                (message_id, self.user_id),
            )
            self._conn.execute(
                """
                DELETE FROM relations
                WHERE user_id = ?
                  AND ((source_type = 'message' AND source_id = ?) OR (target_type = 'message' AND target_id = ?))
                """,
                (self.user_id, message_id, message_id),
            )
            self._conn.commit()
            self._memory.cleanup_orphans()
            self._load_hashes()
            self._load_recent_tokens()
            self._invalidate_embedding_index()
            return True
        return False

    def forget(self, message_id: int) -> bool:
        """Compatibility alias for delete()."""
        return self.delete(message_id)

    def clear_scope(self) -> int:
        """Delete every record owned by the current user scope.

        The return value remains the number of deleted messages for compatibility.
        """
        self._memory.purge()
        ids = self._conn.execute(
            "SELECT id FROM messages WHERE user_id = ?",
            (self.user_id,),
        ).fetchall()

        for (rid,) in ids:
            self._conn.execute(f"DELETE FROM {self._fts_table} WHERE rowid = ?", (rid,))

        artifact_ids = self._conn.execute(
            "SELECT id FROM artifacts WHERE user_id = ?",
            (self.user_id,),
        ).fetchall()
        for (artifact_id,) in artifact_ids:
            self._conn.execute(
                f"DELETE FROM {self._artifact_fts_table} WHERE rowid = ?",
                (artifact_id,),
            )

        code_chunk_ids = self._conn.execute(
            "SELECT id FROM code_chunks WHERE user_id = ?",
            (self.user_id,),
        ).fetchall()
        for (chunk_id,) in code_chunk_ids:
            self._conn.execute(
                f"DELETE FROM {self._code_fts_table} WHERE rowid = ?", (chunk_id,)
            )

        self._conn.execute("DELETE FROM embeddings WHERE user_id = ?", (self.user_id,))
        self._conn.execute(
            "DELETE FROM message_provenance WHERE user_id = ?", (self.user_id,)
        )
        self._conn.execute("DELETE FROM artifacts WHERE user_id = ?", (self.user_id,))
        self._conn.execute("DELETE FROM code_chunks WHERE user_id = ?", (self.user_id,))
        self._conn.execute("DELETE FROM relations WHERE user_id = ?", (self.user_id,))
        cursor = self._conn.execute(
            "DELETE FROM messages WHERE user_id = ?",
            (self.user_id,),
        )
        deleted = cursor.rowcount
        self._conn.commit()
        self._hashes.clear()
        self._recent_tokens = []
        if hasattr(self, "_sbert_embeddings"):
            self._sbert_embeddings = []
        self._invalidate_embedding_index()
        return deleted

    # ── Cleanup ──

    def cleanup(self, max_age_days: int = None, max_messages: int = None) -> int:
        """Remove old messages. Returns count deleted."""
        deleted = 0

        if max_age_days is not None:
            cutoff = time.time() - (max_age_days * 86400)
            ids = self._conn.execute(
                "SELECT id FROM messages WHERE user_id = ? AND timestamp < ?",
                (self.user_id, cutoff),
            ).fetchall()
            for (rid,) in ids:
                self._conn.execute(
                    f"DELETE FROM {self._fts_table} WHERE rowid = ?", (rid,)
                )
                self._conn.execute(
                    "DELETE FROM relations WHERE user_id = ? AND "
                    "((source_type = 'message' AND source_id = ?) OR "
                    "(target_type = 'message' AND target_id = ?))",
                    (self.user_id, rid, rid),
                )
            self._conn.execute(
                "DELETE FROM embeddings WHERE user_id = ? AND message_id IN "
                "(SELECT id FROM messages WHERE user_id = ? AND timestamp < ?)",
                (self.user_id, self.user_id, cutoff),
            )
            self._conn.execute(
                "DELETE FROM message_provenance WHERE user_id = ? AND message_id IN (SELECT id FROM messages WHERE user_id = ? AND timestamp < ?)",
                (self.user_id, self.user_id, cutoff),
            )
            artifact_ids = self._conn.execute(
                "SELECT id FROM artifacts WHERE user_id = ? AND timestamp < ?",
                (self.user_id, cutoff),
            ).fetchall()
            for (artifact_id,) in artifact_ids:
                self._conn.execute(
                    f"DELETE FROM {self._artifact_fts_table} WHERE rowid = ?",
                    (artifact_id,),
                )
                self._conn.execute(
                    "DELETE FROM relations WHERE user_id = ? AND "
                    "((source_type = 'artifact' AND source_id = ?) OR "
                    "(target_type = 'artifact' AND target_id = ?))",
                    (self.user_id, artifact_id, artifact_id),
                )
            code_chunk_ids = self._conn.execute(
                "SELECT id FROM code_chunks WHERE user_id = ? AND timestamp < ?",
                (self.user_id, cutoff),
            ).fetchall()
            for (chunk_id,) in code_chunk_ids:
                self._conn.execute(
                    f"DELETE FROM {self._code_fts_table} WHERE rowid = ?", (chunk_id,)
                )
                self._conn.execute(
                    "DELETE FROM relations WHERE user_id = ? AND "
                    "((source_type = 'code_chunk' AND source_id = ?) OR "
                    "(target_type = 'code_chunk' AND target_id = ?))",
                    (self.user_id, chunk_id, chunk_id),
                )
            self._conn.execute(
                "DELETE FROM artifacts WHERE user_id = ? AND timestamp < ?",
                (self.user_id, cutoff),
            )
            self._conn.execute(
                "DELETE FROM code_chunks WHERE user_id = ? AND timestamp < ?",
                (self.user_id, cutoff),
            )
            self._conn.execute(
                "DELETE FROM relations WHERE user_id = ? AND timestamp < ?",
                (self.user_id, cutoff),
            )
            cursor = self._conn.execute(
                "DELETE FROM messages WHERE user_id = ? AND timestamp < ?",
                (self.user_id, cutoff),
            )
            deleted += cursor.rowcount

        if max_messages is not None:
            count = self._conn.execute(
                "SELECT COUNT(*) FROM messages WHERE user_id = ?", (self.user_id,)
            ).fetchone()[0]
            if count > max_messages:
                excess = count - max_messages
                ids = self._conn.execute(
                    "SELECT id FROM messages WHERE user_id = ? ORDER BY timestamp ASC LIMIT ?",
                    (self.user_id, excess),
                ).fetchall()
                for (rid,) in ids:
                    self._conn.execute(
                        f"DELETE FROM {self._fts_table} WHERE rowid = ?", (rid,)
                    )
                    self._conn.execute(
                        "DELETE FROM relations WHERE user_id = ? AND "
                        "((source_type = 'message' AND source_id = ?) OR "
                        "(target_type = 'message' AND target_id = ?))",
                        (self.user_id, rid, rid),
                    )
                    self._conn.execute(
                        "DELETE FROM embeddings WHERE user_id = ? AND message_id = ?",
                        (self.user_id, rid),
                    )
                self._conn.execute(
                    """
                    DELETE FROM message_provenance WHERE message_id IN (
                        SELECT id FROM messages WHERE user_id = ?
                        ORDER BY timestamp ASC LIMIT ?
                    ) AND user_id = ?
                """,
                    (self.user_id, excess, self.user_id),
                )
                self._conn.execute(
                    """
                    DELETE FROM messages WHERE id IN (
                        SELECT id FROM messages WHERE user_id = ?
                        ORDER BY timestamp ASC LIMIT ?
                    )
                """,
                    (self.user_id, excess),
                )
                deleted += excess

        if deleted > 0:
            self._conn.commit()
            self._memory.cleanup_orphans()
            self._load_hashes()  # Refresh hash cache
            self._load_recent_tokens()
            self._invalidate_embedding_index()
        return deleted

    # ── Stats ──

    def stats(self) -> dict:
        """Return storage stats."""
        row = self._conn.execute(
            """
            SELECT COUNT(*), MIN(timestamp), MAX(timestamp)
            FROM messages WHERE user_id = ?
        """,
            (self.user_id,),
        ).fetchone()
        provenance_count = self._conn.execute(
            "SELECT COUNT(*) FROM message_provenance WHERE user_id = ?",
            (self.user_id,),
        ).fetchone()[0]
        artifact_count = self._conn.execute(
            "SELECT COUNT(*) FROM artifacts WHERE user_id = ?",
            (self.user_id,),
        ).fetchone()[0]
        code_chunk_count = self._conn.execute(
            "SELECT COUNT(*) FROM code_chunks WHERE user_id = ?",
            (self.user_id,),
        ).fetchone()[0]
        relation_count = self._conn.execute(
            "SELECT COUNT(*) FROM relations WHERE user_id = ?",
            (self.user_id,),
        ).fetchone()[0]
        terms_version_row = self._conn.execute(
            "SELECT value FROM metadata WHERE key = ?",
            (f"{self._user_key}_normalized_terms_version",),
        ).fetchone()
        normalized_terms_version = (
            int(terms_version_row[0])
            if terms_version_row and str(terms_version_row[0]).isdigit()
            else None
        )
        memory_status = self._memory.status()
        return {
            "engine_name": self.engine_name,
            "message_count": row[0],
            "provenance_count": provenance_count,
            "artifact_count": artifact_count,
            "code_chunk_count": code_chunk_count,
            "relation_count": relation_count,
            "oldest_timestamp": row[1],
            "newest_timestamp": row[2],
            "db_size_bytes": os.path.getsize(self.db_path)
            if os.path.exists(self.db_path)
            else 0,
            "semantic_dedup": self.semantic_dedup,
            "dedup_backend": "sbert" if self._sbert else "jaccard",
            "dedup_threshold": self.dedup_threshold,
            "embedding_source": self.embedding_source,
            "semantic_search_mode": self.semantic_search_mode,
            "local_only": self.local_only,
            "memory_session_count": memory_status["session_count"],
            "memory_claim_count": memory_status["claim_count"],
            "memory_entity_count": memory_status["entity_count"],
            "enrichment_jobs": memory_status["jobs"],
            "compiler_usage": memory_status["usage"],
            "schema_version": SCHEMA_VERSION,
            "derived_schema_version": DERIVED_SCHEMA_VERSION,
            "normalized_terms_version": normalized_terms_version,
            "fts_format_version": FTS_FORMAT_VERSION,
            "artifact_fts_format_version": ARTIFACT_FTS_FORMAT_VERSION,
            "code_chunk_fts_format_version": CODE_CHUNK_FTS_FORMAT_VERSION,
        }

    def message_counts(
        self,
        *,
        logical_user_id: str,
        selected_scope_key: str,
    ) -> dict[str, int]:
        """Count the selected scope separately from user and database totals."""

        workspace_prefix = f"{logical_user_id}::workspace::"
        row = self._conn.execute(
            """
            SELECT
                COUNT(*),
                COALESCE(SUM(CASE WHEN user_id = ? THEN 1 ELSE 0 END), 0),
                COALESCE(SUM(CASE WHEN user_id = ? THEN 1 ELSE 0 END), 0),
                COALESCE(SUM(
                    CASE
                        WHEN user_id = ? OR INSTR(user_id, ?) = 1 THEN 1
                        ELSE 0
                    END
                ), 0)
            FROM messages
            """,
            (
                selected_scope_key,
                logical_user_id,
                logical_user_id,
                workspace_prefix,
            ),
        ).fetchone()
        return {
            "selected_scope": int(row[1]),
            "global": int(row[2]),
            "user_total": int(row[3]),
            "database_total": int(row[0]),
        }

    def health_check(self, full: bool = False) -> dict:
        """Check SQLite and current-scope index consistency without changing data."""

        pragma = "integrity_check" if full else "quick_check"
        diagnostic = self._conn
        main_database = next(
            (
                str(row[2])
                for row in self._conn.execute("PRAGMA database_list").fetchall()
                if str(row[1]) == "main"
            ),
            "",
        )
        owns_diagnostic = bool(main_database)
        if owns_diagnostic:
            # A fresh snapshot avoids stale FTS5 checksum state on a long-lived
            # foreground connection immediately after a background WAL writer
            # commits. The live connection supplies the canonical path so a
            # later working-directory change cannot redirect this check.
            diagnostic = sqlite3.connect(main_database, timeout=30.0)
            diagnostic.execute("PRAGMA busy_timeout=30000")
        try:
            sqlite_rows = diagnostic.execute(f"PRAGMA {pragma}").fetchall()
            sqlite_messages = [str(row[0]) for row in sqlite_rows]
            sqlite_ok = sqlite_messages == ["ok"]
            foreign_key_violations = len(
                diagnostic.execute("PRAGMA foreign_key_check").fetchall()
            )
        finally:
            if owns_diagnostic:
                diagnostic.close()

        expected_messages = self._conn.execute(
            "SELECT COUNT(*) FROM messages WHERE user_id = ?", (self.user_id,)
        ).fetchone()[0]
        expected_artifacts = self._conn.execute(
            "SELECT COUNT(*) FROM artifacts WHERE user_id = ?", (self.user_id,)
        ).fetchone()[0]
        expected_code_chunks = self._conn.execute(
            "SELECT COUNT(*) FROM code_chunks WHERE user_id = ?", (self.user_id,)
        ).fetchone()[0]
        indexed_messages = self._conn.execute(
            f"SELECT COUNT(*) FROM {self._fts_table}"
        ).fetchone()[0]
        indexed_artifacts = self._conn.execute(
            f"SELECT COUNT(*) FROM {self._artifact_fts_table}"
        ).fetchone()[0]
        indexed_code_chunks = self._conn.execute(
            f"SELECT COUNT(*) FROM {self._code_fts_table}"
        ).fetchone()[0]
        orphan_embeddings = self._conn.execute(
            """
            SELECT COUNT(*) FROM embeddings e
            LEFT JOIN messages m ON m.id = e.message_id AND m.user_id = e.user_id
            WHERE e.user_id = ? AND m.id IS NULL
            """,
            (self.user_id,),
        ).fetchone()[0]
        orphan_provenance = self._conn.execute(
            """
            SELECT COUNT(*) FROM message_provenance p
            LEFT JOIN messages m ON m.id = p.message_id AND m.user_id = p.user_id
            WHERE p.user_id = ? AND m.id IS NULL
            """,
            (self.user_id,),
        ).fetchone()[0]
        missing_normalized_terms = (
            self._conn.execute(
                """
                SELECT COUNT(*) FROM messages
                WHERE user_id = ? AND normalized_terms IS NULL
                """,
                (self.user_id,),
            ).fetchone()[0]
            if self._use_persisted_terms
            else 0
        )

        def orphan_relation_count(endpoint: str) -> int:
            type_column = f"{endpoint}_type"
            id_column = f"{endpoint}_id"
            return self._conn.execute(
                f"""
                SELECT COUNT(*)
                FROM relations r
                WHERE r.user_id = ? AND NOT (
                    ({type_column} = 'message' AND EXISTS (
                        SELECT 1 FROM messages m
                        WHERE m.id = r.{id_column} AND m.user_id = r.user_id
                    )) OR
                    ({type_column} = 'artifact' AND EXISTS (
                        SELECT 1 FROM artifacts a
                        WHERE a.id = r.{id_column} AND a.user_id = r.user_id
                    )) OR
                    ({type_column} = 'code_chunk' AND EXISTS (
                        SELECT 1 FROM code_chunks c
                        WHERE c.id = r.{id_column} AND c.user_id = r.user_id
                    ))
                )
                """,
                (self.user_id,),
            ).fetchone()[0]

        orphan_relations = {
            "sources": orphan_relation_count("source"),
            "targets": orphan_relation_count("target"),
        }
        version_keys = {
            "schema": ("schema_version", SCHEMA_VERSION),
            "messages_fts": (f"{self._fts_table}_version", FTS_FORMAT_VERSION),
            "artifacts_fts": (
                f"{self._artifact_fts_table}_version",
                ARTIFACT_FTS_FORMAT_VERSION,
            ),
            "code_fts": (
                f"{self._code_fts_table}_version",
                CODE_CHUNK_FTS_FORMAT_VERSION,
            ),
            "derived_schema": ("derived_schema_version", DERIVED_SCHEMA_VERSION),
            "claims_fts": (
                f"{self._memory.fts_table}_version",
                CLAIM_FTS_FORMAT_VERSION,
            ),
        }
        if self._use_persisted_terms:
            version_keys["normalized_terms"] = (
                f"{self._user_key}_normalized_terms_version",
                NORMALIZED_TERMS_VERSION,
            )
        metadata_versions = {}
        versions_ok = True
        for label, (key, expected) in version_keys.items():
            row = self._conn.execute(
                "SELECT value FROM metadata WHERE key = ?", (key,)
            ).fetchone()
            actual = int(row[0]) if row and str(row[0]).isdigit() else None
            metadata_versions[label] = {"expected": expected, "actual": actual}
            versions_ok = versions_ok and actual == expected

        journal_mode = str(
            self._conn.execute("PRAGMA journal_mode").fetchone()[0]
        ).lower()

        index_counts = {
            "messages": {"records": expected_messages, "indexed": indexed_messages},
            "artifacts": {"records": expected_artifacts, "indexed": indexed_artifacts},
            "code_chunks": {
                "records": expected_code_chunks,
                "indexed": indexed_code_chunks,
            },
        }
        derived_health = self._memory.health()
        index_counts["memory_claims"] = {
            "records": derived_health["claim_count"],
            "indexed": derived_health["indexed_claim_count"],
        }
        indexes_ok = all(
            value["records"] == value["indexed"] for value in index_counts.values()
        )
        healthy = (
            sqlite_ok
            and foreign_key_violations == 0
            and orphan_embeddings == 0
            and orphan_provenance == 0
            and missing_normalized_terms == 0
            and orphan_relations["sources"] == 0
            and orphan_relations["targets"] == 0
            and indexes_ok
            and versions_ok
            and derived_health["ok"]
            and journal_mode == "wal"
        )
        return {
            "ok": healthy,
            "engine_name": ENGINE_NAME,
            "db_path": self.db_path,
            "schema_version": SCHEMA_VERSION,
            "sqlite_check": pragma,
            "sqlite_messages": sqlite_messages,
            "foreign_key_violations": foreign_key_violations,
            "orphan_embeddings": orphan_embeddings,
            "orphan_provenance": orphan_provenance,
            "missing_normalized_terms": missing_normalized_terms,
            "orphan_relations": orphan_relations,
            "index_counts": index_counts,
            "metadata_versions": metadata_versions,
            "derived_memory": derived_health,
            "journal_mode": journal_mode,
        }

    def backup(self, destination: str) -> dict:
        """Create and verify a transactionally consistent SQLite backup."""

        destination_path = os.path.abspath(os.path.expanduser(destination))
        source_path = os.path.abspath(self.db_path)
        if destination_path == source_path:
            raise ValueError("backup destination must differ from the active database")
        parent = os.path.dirname(destination_path)
        if parent:
            os.makedirs(parent, exist_ok=True)

        target = sqlite3.connect(destination_path, timeout=30.0)
        try:
            self._conn.backup(target)
            target.commit()
            integrity = [
                str(row[0])
                for row in target.execute("PRAGMA integrity_check").fetchall()
            ]
        finally:
            target.close()
        if integrity != ["ok"]:
            raise RuntimeError(f"backup integrity check failed: {integrity}")
        return {
            "path": destination_path,
            "size_bytes": os.path.getsize(destination_path),
            "integrity_check": integrity,
        }

    def get_stats(self) -> dict:
        """Compatibility alias for stats()."""
        return self.stats()

    def __enter__(self):
        return self

    def close(self):
        self._conn.close()

    def __exit__(self, *args):
        self.close()
