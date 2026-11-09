from __future__ import annotations

import hashlib
import json
import re
from typing import Any

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def early_gate_generation(payload: dict[str, Any]) -> str | None:
    entries = payload.get("pull-requests")
    if not isinstance(entries, list) or not entries:
        return None

    children: list[tuple[str, str]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            return None
        url = str(entry.get("pr-url") or "").strip()
        sha = str(entry.get("head-sha") or "").strip().lower()
        if not url or not _SHA_RE.fullmatch(sha):
            return None
        children.append((url, sha))

    encoded = json.dumps(sorted(children), separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def has_child_sha_tracking(payload: dict[str, Any]) -> bool:
    entries = payload.get("pull-requests")
    return isinstance(entries, list) and any(
        isinstance(entry, dict) and "head-sha" in entry for entry in entries
    )
