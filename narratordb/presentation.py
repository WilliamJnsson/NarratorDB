"""Compact assistant-facing rendering for internally retrieved memory."""

from __future__ import annotations

import re
from typing import Any


_CONTEXT_PROVENANCE_LABEL_RE = re.compile(
    r"\[[^\]\n]*(?:\bmessage:\d|\bclaim:\d|\bsessions?:)[^\]\n]*\]\s*",
    re.IGNORECASE,
)
_CONTEXT_INLINE_PROVENANCE_RE = re.compile(
    r"\b(?:message|claim):\d+(?:,\d+)*(?:\s*->)?\s*",
    re.IGNORECASE,
)


def assistant_facing_context(text: str) -> str:
    """Remove internal provenance labels from memory shown to an assistant."""

    rendered = str(text or "")
    rendered = rendered.replace(
        "Canonical message IDs identify supporting evidence.",
        "Internal provenance is available for diagnostics.",
    )
    rendered = rendered.replace(
        "Cite message IDs when answering.",
        "Do not mention internal memory identifiers or provenance unless the user "
        "explicitly asks how the memory was sourced.",
    )
    rendered = _CONTEXT_PROVENANCE_LABEL_RE.sub("", rendered)
    rendered = _CONTEXT_INLINE_PROVENANCE_RE.sub("", rendered)
    return rendered.strip()


def bundle_facts(bundle: Any) -> str:
    """Render only recalled facts, omitting the diagnostic envelope."""

    if bundle is None or not bundle.blocks:
        return ""
    facts = [assistant_facing_context(block.text) for block in bundle.blocks]
    return "\n".join(fact for fact in facts if fact).strip()
