#!/usr/bin/env python3
"""Fail if a Community wheel contains hosted implementation or dependencies."""

from __future__ import annotations

from pathlib import Path
import sys
import zipfile


wheel = Path(sys.argv[1])
with zipfile.ZipFile(wheel) as archive:
    names = archive.namelist()
    assert not any(name.startswith("narratordb/cloud/") for name in names)
    metadata_name = next(name for name in names if name.endswith(".dist-info/METADATA"))
    metadata = archive.read(metadata_name).decode("utf-8").casefold()

for forbidden in ("psycopg", "pyjwt", "boto3", "terraform", "narratordb-cloud"):
    assert forbidden not in metadata, forbidden
print("NarratorDB Community wheel boundary passed")
