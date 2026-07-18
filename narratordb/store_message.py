#!/usr/bin/env python3
"""Store one message in NarratorDB.

Usage: echo "message text" | python3 -m narratordb.store_message [speaker]
       python3 -m narratordb.store_message [speaker] "message text"

For Claude Code hooks, use hook_store.py instead (faster, no SBERT).
"""
import sys

import getpass

from .config import default_db_path, default_user_id
from .engine import Engine


def main() -> int:
    speaker = sys.argv[1] if len(sys.argv) > 1 else "assistant"
    text = sys.argv[2] if len(sys.argv) > 2 else sys.stdin.read().strip()

    if not text:
        return 0

    if len(text) > 3000:
        text = text[:3000] + "... [truncated]"

    with Engine(
        db_path=default_db_path(),
        user_id=default_user_id(getpass.getuser()),
        context_window=5,
    ) as engine:
        msg_id = engine.store(speaker=speaker, text=text)
    print(f"Stored id={msg_id}" if msg_id else "Skipped (duplicate)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
