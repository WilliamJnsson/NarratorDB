#!/usr/bin/env python3
"""Capture ECB daily XML and content-free USD/EUR metadata without overwrite."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import xml.etree.ElementTree as ET
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from http.client import HTTPException
from pathlib import Path
from typing import Any, Sequence
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


SCHEMA = "narratordb.ecb-usd-eur-observation.v1"
URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"
MAX_XML_BYTES = 1024 * 1024


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        raise HTTPError(req.full_url, code, "redirect refused", headers, fp)


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _write_new(path: Path, payload: bytes) -> None:
    if path.exists() or path.is_symlink():
        raise ValueError(f"refusing to overwrite ECB evidence: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o444)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
    except Exception:
        path.unlink(missing_ok=True)
        raise


def _parse(raw: bytes, *, retrieved: datetime) -> tuple[str, str]:
    if b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
        raise ValueError("ECB XML declarations/entities are forbidden")
    try:
        tree = ET.fromstring(raw)
    except ET.ParseError as error:
        raise ValueError(f"ECB XML is malformed: {error}") from error
    dated = [
        element
        for element in tree.iter()
        if _local_name(element.tag) == "Cube" and "time" in element.attrib
    ]
    if len(dated) != 1 or set(dated[0].attrib) != {"time"}:
        raise ValueError("ECB XML must contain exactly one dated Cube")
    quotes = [
        child
        for child in list(dated[0])
        if _local_name(child.tag) == "Cube" and child.attrib.get("currency") == "USD"
    ]
    if len(quotes) != 1 or set(quotes[0].attrib) != {"currency", "rate"}:
        raise ValueError("ECB dated Cube must contain exactly one USD quote")
    reference_text = dated[0].attrib["time"]
    try:
        reference = date.fromisoformat(reference_text)
        rate = Decimal(quotes[0].attrib["rate"])
    except (ValueError, InvalidOperation) as error:
        raise ValueError("ECB date/rate is invalid") from error
    age = (retrieved.date() - reference).days
    if reference.weekday() >= 5 or age < 0 or age > 7:
        raise ValueError("ECB reference date is not a recent UTC business date")
    if not rate.is_finite() or rate <= 0:
        raise ValueError("ECB USD-per-EUR quote must be positive and finite")
    return reference_text, format(rate, "f")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository-root", type=Path, required=True)
    parser.add_argument("--raw-output", type=Path, required=True)
    parser.add_argument("--metadata-output", type=Path, required=True)
    parser.add_argument("--parser", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=20.0)
    args = parser.parse_args(argv)
    try:
        root = args.repository_root.resolve(strict=True)
        raw_output = args.raw_output if args.raw_output.is_absolute() else root / args.raw_output
        metadata_output = (
            args.metadata_output if args.metadata_output.is_absolute() else root / args.metadata_output
        )
        parser_path = args.parser if args.parser.is_absolute() else root / args.parser
        if not parser_path.is_file() or parser_path.is_symlink():
            raise ValueError("sealed admission parser is missing")
        if raw_output.exists() or metadata_output.exists():
            raise ValueError("ECB output paths must both start absent")
        request = Request(URL, method="GET", headers={"Accept": "application/xml"})
        # Keep the official FX observation on a direct route regardless of
        # shell or OS proxy configuration.
        with build_opener(ProxyHandler({}), _NoRedirect()).open(
            request, timeout=args.timeout
        ) as response:
            if response.status != 200:
                raise ValueError(f"ECB returned HTTP {response.status}")
            raw = response.read(MAX_XML_BYTES + 1)
            status = response.status
        if len(raw) > MAX_XML_BYTES:
            raise ValueError("ECB XML exceeded the byte limit")
        retrieved = datetime.now(timezone.utc).replace(microsecond=0)
        reference_date, rate = _parse(raw, retrieved=retrieved)
        relative_raw = raw_output.resolve().relative_to(root).as_posix()
        metadata: dict[str, Any] = {
            "schema_version": SCHEMA,
            "publisher": "European Central Bank",
            "source_url": URL,
            "http_status": status,
            "retrieved_at_utc": retrieved.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "raw_xml_path": relative_raw,
            "raw_xml_bytes": len(raw),
            "raw_xml_sha256": hashlib.sha256(raw).hexdigest(),
            "reference_date": reference_date,
            "base_currency": "EUR",
            "quote_currency": "USD",
            "usd_per_eur": rate,
            "parser_sha256": hashlib.sha256(parser_path.read_bytes()).hexdigest(),
            "credential_recorded": False,
            "model_content_recorded": False,
        }
        metadata_payload = (json.dumps(metadata, indent=2, sort_keys=True) + "\n").encode(
            "utf-8"
        )
        _write_new(raw_output, raw)
        # If metadata persistence fails, retain the immutable raw response as
        # terminal evidence.  Attempt artifacts are never rolled back.
        _write_new(metadata_output, metadata_payload)
    except (HTTPError, HTTPException, OSError, URLError, ValueError) as error:
        parser.error(str(error))
    sys.stdout.write(
        json.dumps(
            {
                "ok": True,
                "raw_output": str(raw_output),
                "raw_sha256": hashlib.sha256(raw).hexdigest(),
                "metadata_output": str(metadata_output),
                "metadata_sha256": hashlib.sha256(metadata_payload).hexdigest(),
                "credential_recorded": False,
                "model_content_recorded": False,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
