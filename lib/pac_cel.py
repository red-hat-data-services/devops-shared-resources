"""JSON bridge to the pinned CEL-Go/PaC trigger helper.

No implicit builds or downloads at runtime. Missing helpers and invalid responses
are evaluation errors, never evidence that a pipeline need not run.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
from typing import Any, Sequence


class TriggerParseError(ValueError):
    """A PipelineRun trigger could not be evaluated."""


def evaluate_expressions(
    expressions: Sequence[str], variables: dict[str, Any],
    *, custom_params: dict[str, str] | None = None,
) -> list[bool | TriggerParseError]:
    if not expressions:
        return []
    executable = os.environ.get("PAC_CEL_EVALUATOR") or str(
        Path(__file__).resolve().parents[1] / "bin" / "pac-cel-evaluate"
    )
    try:
        process = subprocess.run(
            [executable],
            input=json.dumps({
                "expressions": list(expressions), "variables": variables,
                "custom_params": custom_params or {},
            }),
            capture_output=True, text=True, timeout=15, check=False,
        )
        if process.returncode:
            raise TriggerParseError("PaC CEL helper failed")
        results = json.loads(process.stdout)
        if not isinstance(results, list) or len(results) != len(expressions):
            raise TriggerParseError("invalid PaC CEL helper response")
        values: list[bool | TriggerParseError] = []
        for item in results:
            if not isinstance(item, dict):
                raise TriggerParseError("invalid PaC CEL result")
            if isinstance(item.get("error"), str) and item["error"]:
                values.append(TriggerParseError(item["error"]))
            elif type(item.get("value")) is bool:
                values.append(item["value"])
            else:
                raise TriggerParseError("PaC CEL result must be boolean or error")
        return values
    except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
        error = TriggerParseError(
            f"PaC CEL evaluation unavailable ({type(exc).__name__}); "
            "build tools/pac-cel or set PAC_CEL_EVALUATOR"
        )
        return [error for _ in expressions]
