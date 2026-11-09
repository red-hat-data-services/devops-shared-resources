"""Leader-level promotion gate checklist (source of truth for GAP merge).

``promotion-gates`` in state.json is the only persisted conclusion checklist.
``overall-status`` is legacy: migrated once into gates, then stripped. Callers
that still want a single string (logs / MonitorResult) should use
``derive_legacy_overall_status`` — do not write it back.

Child approval requires successful component builds and Early Gate build/tests.
Failed batches remain active until replaced by a verified newer Leader.
The persisted required flags are retained for compatibility.
"""

from __future__ import annotations

from typing import Any

from lib.leader_lifecycle import retired_outcome

GATE_COMPONENT_BUILDS = "component-builds"
GATE_EARLY_GATE_BUILD = "early-gate-build"
GATE_EARLY_GATE_TEST = "early-gate-test"

GATE_STATUS_PENDING = "pending"
GATE_STATUS_SUCCESS = "success"
GATE_STATUS_FAILURE = "failure"

TERMINAL_GATE_STATUSES = frozenset({GATE_STATUS_SUCCESS, GATE_STATUS_FAILURE})

# Early Gate Leader label contract (konflux-central).
LABEL_EG_BUILD_SUCCESS = "early-gate-build-successful"
LABEL_EG_BUILD_FAILED = "early-gate-build-failed"
LABEL_EG_TEST_SUCCESS = "early-gate-tests-successful"
LABEL_EG_TEST_FAILED = "early-gate-tests-failed"

EG_WAKE_LABELS = frozenset(
    {
        LABEL_EG_BUILD_SUCCESS,
        LABEL_EG_BUILD_FAILED,
        LABEL_EG_TEST_SUCCESS,
        LABEL_EG_TEST_FAILED,
    }
)

# Child pr-status values that mean the component gate passed.
_CHILD_PASS = frozenset({"success", "build-success"})
_CHILD_TERMINAL = frozenset(
    {"success", "build-success", "merge-failure", "build-failure"}
)

# Legacy overall-status strings (read/migrate only).
LEGACY_OVERALL_SUCCESS = "success"
LEGACY_OVERALL_FAILURE = "failure"
LEGACY_OVERALL_EARLY_GATE_TRIGGERED = "early-gate-triggered"

DEFAULT_PROMOTION_GATES: tuple[dict[str, Any], ...] = (
    {
        "id": GATE_COMPONENT_BUILDS,
        "required": True,
        "status": GATE_STATUS_PENDING,
    },
    {
        "id": GATE_EARLY_GATE_BUILD,
        "required": False,
        "status": GATE_STATUS_PENDING,
    },
    {
        "id": GATE_EARLY_GATE_TEST,
        "required": False,
        "status": GATE_STATUS_PENDING,
    },
)


def ensure_promotion_gates(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Return mutable promotion-gates list on ``payload``, creating defaults."""
    raw = payload.get("promotion-gates")
    if not isinstance(raw, list) or not raw:
        gates = [dict(item) for item in DEFAULT_PROMOTION_GATES]
        payload["promotion-gates"] = gates
        return gates

    by_id = {
        str(item.get("id") or ""): item
        for item in raw
        if isinstance(item, dict) and item.get("id")
    }
    gates: list[dict[str, Any]] = []
    for default in DEFAULT_PROMOTION_GATES:
        gate_id = str(default["id"])
        existing = by_id.get(gate_id)
        if existing is None:
            gates.append(dict(default))
            continue
        merged = dict(default)
        merged.update(existing)
        merged["id"] = gate_id
        if "required" not in existing:
            merged["required"] = default["required"]
        else:
            merged["required"] = bool(existing.get("required"))
        status = str(merged.get("status") or GATE_STATUS_PENDING).strip().lower()
        merged["status"] = (
            status
            if status in TERMINAL_GATE_STATUSES | {GATE_STATUS_PENDING}
            else GATE_STATUS_PENDING
        )
        gates.append(merged)
    payload["promotion-gates"] = gates
    return gates


def set_gate_status(payload: dict[str, Any], gate_id: str, status: str) -> None:
    """Set one promotion-gate status (pending|success|failure)."""
    if status not in TERMINAL_GATE_STATUSES | {GATE_STATUS_PENDING}:
        raise ValueError(f"Invalid gate status: {status!r}")
    gates = ensure_promotion_gates(payload)
    for gate in gates:
        if str(gate.get("id")) == gate_id:
            gate["status"] = status
            return
    raise KeyError(f"Unknown promotion gate id: {gate_id!r}")


def get_gate_status(payload: dict[str, Any], gate_id: str) -> str:
    """Return status for ``gate_id`` (default pending)."""
    for gate in ensure_promotion_gates(payload):
        if str(gate.get("id")) == gate_id:
            return str(gate.get("status") or GATE_STATUS_PENDING)
    return GATE_STATUS_PENDING


def get_gate_required(payload: dict[str, Any], gate_id: str) -> bool:
    """Return whether ``gate_id`` is required for child green."""
    for gate in ensure_promotion_gates(payload):
        if str(gate.get("id")) == gate_id:
            return bool(gate.get("required"))
    return False


def set_gate_required(payload: dict[str, Any], gate_id: str, required: bool) -> None:
    """Flip ``required`` on a gate (future: make EG mandatory)."""
    gates = ensure_promotion_gates(payload)
    for gate in gates:
        if str(gate.get("id")) == gate_id:
            gate["required"] = bool(required)
            return
    raise KeyError(f"Unknown promotion gate id: {gate_id!r}")


def required_gates(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Return gates marked required."""
    return [g for g in ensure_promotion_gates(payload) if bool(g.get("required"))]


def all_required_gates_terminal(payload: dict[str, Any]) -> bool:
    """True when every required gate is success or failure."""
    req = required_gates(payload)
    if not req:
        return False
    return all(
        str(g.get("status") or GATE_STATUS_PENDING) in TERMINAL_GATE_STATUSES for g in req
    )


def any_required_gate_failed(payload: dict[str, Any]) -> bool:
    """True when a required gate is failure."""
    return any(
        str(g.get("status") or "") == GATE_STATUS_FAILURE for g in required_gates(payload)
    )


def early_gate_concluded(payload: dict[str, Any]) -> bool:
    """True when the EG pair is finished.

    - Both EG build and EG test terminal, or
    - EG build failed (tests are not started — treat pair as done).
    """
    build = get_gate_status(payload, GATE_EARLY_GATE_BUILD)
    if build == GATE_STATUS_FAILURE:
        return True
    test = get_gate_status(payload, GATE_EARLY_GATE_TEST)
    return build in TERMINAL_GATE_STATUSES and test in TERMINAL_GATE_STATUSES


def mark_eg_test_skipped_after_build_failure(payload: dict[str, Any]) -> bool:
    """If EG build failed and test still pending, mark test failure (skipped).

    Returns True when the test gate was updated.
    """
    if get_gate_status(payload, GATE_EARLY_GATE_BUILD) != GATE_STATUS_FAILURE:
        return False
    if get_gate_status(payload, GATE_EARLY_GATE_TEST) != GATE_STATUS_PENDING:
        return False
    set_gate_status(payload, GATE_EARLY_GATE_TEST, GATE_STATUS_FAILURE)
    return True


def children_ready_for_success_status(payload: dict[str, Any]) -> bool:
    """Approve children only after the batch and Early Gate build/tests pass."""
    if retired_outcome(payload):
        return False
    ensure_promotion_gates(payload)
    return (get_gate_status(payload, GATE_COMPONENT_BUILDS) == GATE_STATUS_SUCCESS
            and early_gate_pair_overall(payload) == GATE_STATUS_SUCCESS)


def should_trigger_early_gate(payload: dict[str, Any]) -> bool:
    """True when Stage-1 should comment ``/early-gate`` (all builds passed)."""
    return (
        not retired_outcome(payload)
        and get_gate_status(payload, GATE_COMPONENT_BUILDS) == GATE_STATUS_SUCCESS
    )


def should_green_leader_for_build_failure(payload: dict[str, Any]) -> bool:
    """A failed batch must wait for replacement rather than authorizing its merge."""
    # Failure stays active until a verified newer Leader replaces this batch.
    return False


def waiting_for_early_gate(payload: dict[str, Any]) -> bool:
    """Builds passed but EG has not concluded yet (hold child green)."""
    return (
        get_gate_status(payload, GATE_COMPONENT_BUILDS) == GATE_STATUS_SUCCESS
        and not early_gate_concluded(payload)
    )


def gate_status_from_labels(
    labels: list[str] | tuple[str, ...] | set[str],
    *,
    success_label: str,
    failed_label: str,
) -> str | None:
    """Map Leader labels to success/failure; prefer failed if both present."""
    names = {str(item).strip() for item in labels if str(item).strip()}
    if failed_label in names:
        return GATE_STATUS_FAILURE
    if success_label in names:
        return GATE_STATUS_SUCCESS
    return None


def both_early_gate_gates_terminal(payload: dict[str, Any]) -> bool:
    """True when EG build and EG test gates are each success or failure.

    Also true after ``mark_eg_test_skipped_after_build_failure`` when build
    failed and test was still pending.
    """
    mark_eg_test_skipped_after_build_failure(payload)
    build = get_gate_status(payload, GATE_EARLY_GATE_BUILD)
    test = get_gate_status(payload, GATE_EARLY_GATE_TEST)
    return build in TERMINAL_GATE_STATUSES and test in TERMINAL_GATE_STATUSES


def early_gate_pair_overall(payload: dict[str, Any]) -> str | None:
    """success if both EG gates passed; failure if EG concluded with any fail."""
    if not early_gate_concluded(payload):
        return None
    mark_eg_test_skipped_after_build_failure(payload)
    build = get_gate_status(payload, GATE_EARLY_GATE_BUILD)
    test = get_gate_status(payload, GATE_EARLY_GATE_TEST)
    if build == GATE_STATUS_SUCCESS and test == GATE_STATUS_SUCCESS:
        return GATE_STATUS_SUCCESS
    return GATE_STATUS_FAILURE


def apply_component_builds_from_children(payload: dict[str, Any]) -> str:
    """Set ``component-builds`` from child ``pr-status`` values; return new status.

    - any non-terminal child → pending
    - all terminal and all pass → success
    - all terminal and any fail → failure
    """
    ensure_promotion_gates(payload)
    entries = payload.get("pull-requests") or []
    if not entries:
        set_gate_status(payload, GATE_COMPONENT_BUILDS, GATE_STATUS_PENDING)
        return GATE_STATUS_PENDING

    statuses: list[str] = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ValueError(f"pull-requests[{index}] must be an object.")
        status = str(entry.get("pr-status") or "").strip()
        if status not in _CHILD_TERMINAL:
            set_gate_status(payload, GATE_COMPONENT_BUILDS, GATE_STATUS_PENDING)
            return GATE_STATUS_PENDING
        statuses.append(status)

    if any(status not in _CHILD_PASS for status in statuses):
        set_gate_status(payload, GATE_COMPONENT_BUILDS, GATE_STATUS_FAILURE)
        return GATE_STATUS_FAILURE
    set_gate_status(payload, GATE_COMPONENT_BUILDS, GATE_STATUS_SUCCESS)
    return GATE_STATUS_SUCCESS


def strip_legacy_overall_status(payload: dict[str, Any]) -> None:
    """Remove duplicated ``overall-status`` from state (gates are SoT)."""
    payload.pop("overall-status", None)


def migrate_legacy_overall_status(payload: dict[str, Any]) -> bool:
    """One-shot: map legacy ``overall-status`` into gates, then strip it.

    Returns True when a legacy value was present and applied.
    """
    ensure_promotion_gates(payload)
    raw = payload.get("overall-status")
    if raw is None or not str(raw).strip():
        strip_legacy_overall_status(payload)
        return False
    overall = str(raw).strip()
    builds = get_gate_status(payload, GATE_COMPONENT_BUILDS)
    if overall == LEGACY_OVERALL_EARLY_GATE_TRIGGERED:
        if builds == GATE_STATUS_PENDING:
            set_gate_status(payload, GATE_COMPONENT_BUILDS, GATE_STATUS_SUCCESS)
    elif overall == LEGACY_OVERALL_SUCCESS:
        if builds == GATE_STATUS_PENDING:
            set_gate_status(payload, GATE_COMPONENT_BUILDS, GATE_STATUS_SUCCESS)
        if get_gate_status(payload, GATE_EARLY_GATE_BUILD) == GATE_STATUS_PENDING:
            set_gate_status(payload, GATE_EARLY_GATE_BUILD, GATE_STATUS_SUCCESS)
        if get_gate_status(payload, GATE_EARLY_GATE_TEST) == GATE_STATUS_PENDING:
            set_gate_status(payload, GATE_EARLY_GATE_TEST, GATE_STATUS_SUCCESS)
    elif overall == LEGACY_OVERALL_FAILURE:
        if builds == GATE_STATUS_PENDING:
            # Ambiguous: build failure vs EG failure. Prefer EG if EG already set.
            if both_early_gate_gates_terminal(payload):
                set_gate_status(payload, GATE_COMPONENT_BUILDS, GATE_STATUS_SUCCESS)
            else:
                set_gate_status(payload, GATE_COMPONENT_BUILDS, GATE_STATUS_FAILURE)
    strip_legacy_overall_status(payload)
    return True


def derive_legacy_overall_status(payload: dict[str, Any]) -> str | None:
    """Derive a legacy overall string for logs / MonitorResult (not persisted).

    - builds pending → None
    - builds failure → failure
    - builds success + EG concluded → success|failure from EG pair
    - builds success + EG incomplete → early-gate-triggered
    """
    ensure_promotion_gates(payload)
    builds = get_gate_status(payload, GATE_COMPONENT_BUILDS)
    if builds == GATE_STATUS_PENDING:
        return None
    if builds == GATE_STATUS_FAILURE:
        return LEGACY_OVERALL_FAILURE
    pair = early_gate_pair_overall(payload)
    if pair == GATE_STATUS_SUCCESS:
        return LEGACY_OVERALL_SUCCESS
    if pair == GATE_STATUS_FAILURE:
        return LEGACY_OVERALL_FAILURE
    return LEGACY_OVERALL_EARLY_GATE_TRIGGERED
