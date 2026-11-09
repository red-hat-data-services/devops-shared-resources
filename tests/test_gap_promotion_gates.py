"""Unit tests for GAP promotion-gates helpers (SoT checklist)."""

from __future__ import annotations

import pytest

from lib.gap_promotion_gates import (
    GATE_COMPONENT_BUILDS,
    GATE_EARLY_GATE_BUILD,
    GATE_EARLY_GATE_TEST,
    GATE_STATUS_FAILURE,
    GATE_STATUS_PENDING,
    GATE_STATUS_SUCCESS,
    LABEL_EG_BUILD_FAILED,
    LABEL_EG_BUILD_SUCCESS,
    LABEL_EG_TEST_FAILED,
    LABEL_EG_TEST_SUCCESS,
    apply_component_builds_from_children,
    both_early_gate_gates_terminal,
    children_ready_for_success_status,
    derive_legacy_overall_status,
    early_gate_pair_overall,
    ensure_promotion_gates,
    gate_status_from_labels,
    get_gate_required,
    get_gate_status,
    migrate_legacy_overall_status,
    required_gates,
    set_gate_required,
    set_gate_status,
    should_green_leader_for_build_failure,
    should_trigger_early_gate,
    strip_legacy_overall_status,
)


def test_ensure_creates_defaults() -> None:
    payload: dict = {}
    gates = ensure_promotion_gates(payload)
    assert [g["id"] for g in gates] == [
        "component-builds",
        "early-gate-build",
        "early-gate-test",
    ]
    assert all(g["status"] == GATE_STATUS_PENDING for g in gates)
    assert get_gate_required(payload, GATE_COMPONENT_BUILDS) is True
    assert get_gate_required(payload, GATE_EARLY_GATE_BUILD) is False
    assert get_gate_required(payload, GATE_EARLY_GATE_TEST) is False
    assert payload["promotion-gates"] == gates


def test_ensure_merges_partial_existing() -> None:
    payload = {
        "promotion-gates": [
            {"id": "early-gate-build", "status": "success", "required": False},
        ]
    }
    gates = ensure_promotion_gates(payload)
    assert get_gate_status(payload, GATE_EARLY_GATE_BUILD) == GATE_STATUS_SUCCESS
    assert get_gate_status(payload, GATE_EARLY_GATE_TEST) == GATE_STATUS_PENDING
    assert len(gates) == 3


def test_set_and_pair_overall() -> None:
    payload: dict = {}
    set_gate_status(payload, GATE_EARLY_GATE_BUILD, GATE_STATUS_SUCCESS)
    assert both_early_gate_gates_terminal(payload) is False
    assert early_gate_pair_overall(payload) is None

    set_gate_status(payload, GATE_EARLY_GATE_TEST, GATE_STATUS_FAILURE)
    assert both_early_gate_gates_terminal(payload) is True
    assert early_gate_pair_overall(payload) == GATE_STATUS_FAILURE

    set_gate_status(payload, GATE_EARLY_GATE_TEST, GATE_STATUS_SUCCESS)
    assert early_gate_pair_overall(payload) == GATE_STATUS_SUCCESS


def test_gate_status_from_labels_prefers_failed() -> None:
    assert (
        gate_status_from_labels(
            [LABEL_EG_BUILD_SUCCESS, LABEL_EG_BUILD_FAILED],
            success_label=LABEL_EG_BUILD_SUCCESS,
            failed_label=LABEL_EG_BUILD_FAILED,
        )
        == GATE_STATUS_FAILURE
    )
    assert (
        gate_status_from_labels(
            [LABEL_EG_TEST_SUCCESS],
            success_label=LABEL_EG_TEST_SUCCESS,
            failed_label=LABEL_EG_TEST_FAILED,
        )
        == GATE_STATUS_SUCCESS
    )
    assert (
        gate_status_from_labels(
            [],
            success_label=LABEL_EG_TEST_SUCCESS,
            failed_label=LABEL_EG_TEST_FAILED,
        )
        is None
    )


def test_children_remain_blocked_after_eg_failure() -> None:
    """A finished failure still blocks child approval."""
    from lib.gap_promotion_gates import early_gate_concluded, waiting_for_early_gate

    payload: dict = {}
    assert children_ready_for_success_status(payload) is False

    set_gate_status(payload, GATE_COMPONENT_BUILDS, GATE_STATUS_SUCCESS)
    assert should_trigger_early_gate(payload) is True
    assert waiting_for_early_gate(payload) is True
    assert children_ready_for_success_status(payload) is False

    set_gate_status(payload, GATE_EARLY_GATE_BUILD, GATE_STATUS_SUCCESS)
    set_gate_status(payload, GATE_EARLY_GATE_TEST, GATE_STATUS_FAILURE)
    assert early_gate_concluded(payload) is True
    assert children_ready_for_success_status(payload) is False


def test_children_blocked_on_eg_build_failure_without_test() -> None:
    from lib.gap_promotion_gates import mark_eg_test_skipped_after_build_failure

    payload: dict = {}
    set_gate_status(payload, GATE_COMPONENT_BUILDS, GATE_STATUS_SUCCESS)
    set_gate_status(payload, GATE_EARLY_GATE_BUILD, GATE_STATUS_FAILURE)
    assert children_ready_for_success_status(payload) is False
    assert mark_eg_test_skipped_after_build_failure(payload) is False  # pair evaluation marked it skipped
    assert get_gate_status(payload, GATE_EARLY_GATE_TEST) == GATE_STATUS_FAILURE


def test_children_and_leader_blocked_on_component_failure() -> None:
    payload: dict = {}
    set_gate_status(payload, GATE_COMPONENT_BUILDS, GATE_STATUS_FAILURE)
    assert children_ready_for_success_status(payload) is False
    assert should_trigger_early_gate(payload) is False
    assert should_green_leader_for_build_failure(payload) is False


def test_making_eg_required_demands_eg_success() -> None:
    payload: dict = {}
    set_gate_status(payload, GATE_COMPONENT_BUILDS, GATE_STATUS_SUCCESS)
    set_gate_required(payload, GATE_EARLY_GATE_BUILD, True)
    set_gate_required(payload, GATE_EARLY_GATE_TEST, True)
    assert children_ready_for_success_status(payload) is False

    set_gate_status(payload, GATE_EARLY_GATE_BUILD, GATE_STATUS_FAILURE)
    set_gate_status(payload, GATE_EARLY_GATE_TEST, GATE_STATUS_SUCCESS)
    # required=true ⇒ EG failure does not green children
    assert children_ready_for_success_status(payload) is False

    set_gate_status(payload, GATE_EARLY_GATE_BUILD, GATE_STATUS_SUCCESS)
    set_gate_status(payload, GATE_EARLY_GATE_TEST, GATE_STATUS_SUCCESS)
    assert children_ready_for_success_status(payload) is True


def test_apply_component_builds_from_children_all_success() -> None:
    payload = {
        "pull-requests": [
            {"pr-url": "https://ex/a/1", "pr-status": "success"},
            {"pr-url": "https://ex/b/2", "pr-status": "build-success"},
        ]
    }
    assert apply_component_builds_from_children(payload) == GATE_STATUS_SUCCESS
    assert get_gate_status(payload, GATE_COMPONENT_BUILDS) == GATE_STATUS_SUCCESS


def test_apply_component_builds_from_children_any_failure() -> None:
    payload = {
        "pull-requests": [
            {"pr-url": "https://ex/a/1", "pr-status": "success"},
            {"pr-url": "https://ex/b/2", "pr-status": "build-failure"},
        ]
    }
    assert apply_component_builds_from_children(payload) == GATE_STATUS_FAILURE


def test_apply_component_builds_pending_while_open() -> None:
    payload = {
        "pull-requests": [
            {"pr-url": "https://ex/a/1", "pr-status": "success"},
            {"pr-url": "https://ex/b/2", "pr-status": "build-pending"},
        ]
    }
    assert apply_component_builds_from_children(payload) == GATE_STATUS_PENDING


def test_migrate_legacy_early_gate_triggered() -> None:
    payload = {"overall-status": "early-gate-triggered"}
    assert migrate_legacy_overall_status(payload) is True
    assert "overall-status" not in payload
    assert get_gate_status(payload, GATE_COMPONENT_BUILDS) == GATE_STATUS_SUCCESS
    assert get_gate_status(payload, GATE_EARLY_GATE_BUILD) == GATE_STATUS_PENDING


def test_migrate_legacy_success_fills_eg() -> None:
    payload = {"overall-status": "success"}
    migrate_legacy_overall_status(payload)
    assert get_gate_status(payload, GATE_COMPONENT_BUILDS) == GATE_STATUS_SUCCESS
    assert get_gate_status(payload, GATE_EARLY_GATE_BUILD) == GATE_STATUS_SUCCESS
    assert get_gate_status(payload, GATE_EARLY_GATE_TEST) == GATE_STATUS_SUCCESS
    assert "overall-status" not in payload


def test_migrate_legacy_failure_without_eg_is_build_failure() -> None:
    payload = {"overall-status": "failure"}
    migrate_legacy_overall_status(payload)
    assert get_gate_status(payload, GATE_COMPONENT_BUILDS) == GATE_STATUS_FAILURE


def test_migrate_legacy_failure_with_eg_terminal_keeps_builds_success() -> None:
    payload = {
        "overall-status": "failure",
        "promotion-gates": [
            {"id": GATE_EARLY_GATE_BUILD, "status": "failure", "required": False},
            {"id": GATE_EARLY_GATE_TEST, "status": "success", "required": False},
        ],
    }
    migrate_legacy_overall_status(payload)
    assert get_gate_status(payload, GATE_COMPONENT_BUILDS) == GATE_STATUS_SUCCESS


def test_derive_legacy_overall_status_matrix() -> None:
    payload: dict = {}
    assert derive_legacy_overall_status(payload) is None

    set_gate_status(payload, GATE_COMPONENT_BUILDS, GATE_STATUS_FAILURE)
    assert derive_legacy_overall_status(payload) == "failure"

    set_gate_status(payload, GATE_COMPONENT_BUILDS, GATE_STATUS_SUCCESS)
    assert derive_legacy_overall_status(payload) == "early-gate-triggered"

    set_gate_status(payload, GATE_EARLY_GATE_BUILD, GATE_STATUS_SUCCESS)
    set_gate_status(payload, GATE_EARLY_GATE_TEST, GATE_STATUS_SUCCESS)
    assert derive_legacy_overall_status(payload) == "success"

    set_gate_status(payload, GATE_EARLY_GATE_TEST, GATE_STATUS_FAILURE)
    assert derive_legacy_overall_status(payload) == "failure"


def test_strip_legacy_overall_status() -> None:
    payload = {"overall-status": "success", "x": 1}
    strip_legacy_overall_status(payload)
    assert "overall-status" not in payload
    assert payload["x"] == 1


def test_set_gate_status_rejects_invalid() -> None:
    with pytest.raises(ValueError, match="Invalid gate status"):
        set_gate_status({}, GATE_COMPONENT_BUILDS, "nope")
