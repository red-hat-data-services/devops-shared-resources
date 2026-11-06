"""Generic bounded-name filter, independent of any consumer's PR naming policy."""
import hashlib

import pytest

from lib.tekton_filters import ValidationError, make_filters, shorten_name


def test_names_within_limit_are_unchanged():
    """Avoid renaming existing identities that already satisfy the chosen budget."""
    for name in ("widget", "x" * 36):
        assert shorten_name(name, 36) == name


@pytest.mark.parametrize("limit", [10, 36, 63])
def test_long_names_have_readable_prefix_and_stable_digest(limit):
    """Make deterministic, bounded names and preserve them on a second pass."""
    name = "odh-data-science-pipelines-argo-workflowcontroller" * 2
    result = shorten_name(name, limit)
    assert result == name[:limit - 9].rstrip("-") + "-" + hashlib.sha256(name.encode()).hexdigest()[:8]
    assert len(result) <= limit
    assert shorten_name(result, limit) == result


def test_similar_long_prefixes_remain_distinct():
    """Do not collapse different identities merely because their prefixes match."""
    prefix = "a" * 60
    assert shorten_name(prefix + "-cpu", 36) != shorten_name(prefix + "-gpu", 36)


def test_truncation_does_not_leave_a_double_separator():
    """Strip a trailing prefix separator before adding the digest separator."""
    result = shorten_name("a" * 26 + "-" + "b" * 30, 36)
    assert "--" not in result and len(result) == 35


@pytest.mark.parametrize("value, limit", [(None, 36), (123, 36), ("abc", 9), ("abc", True), ("abc", "36")])
def test_invalid_inputs_are_rejected(value, limit):
    """Reject invalid budgets and values rather than emitting unsafe identities."""
    with pytest.raises(ValidationError):
        shorten_name(value, limit)


@pytest.mark.parametrize("parent, child, expected", [
    ("widget-v3-6", "widget-v3-6", True),
    ("widget-v3-6", "widget-v3-7", False),
    ("widget-v3-6", None, False), (None, None, False), ("", "", False),
])
def test_label_match_does_not_infer_identity_from_shortened_names(parent, child, expected):
    """Use exact nonempty component labels and keep different releases distinct."""
    from lib.tekton_matchers import component_label_match
    def pipeline(component, name):
        """Build a minimal named PipelineRun with one component label."""
        return {"metadata": {"name": name, "labels": {"appstudio.openshift.io/component": component}}}
    assert component_label_match(pipeline(parent, "long-name-on-push"),
                                 pipeline(child, "short-hash-on-pull-request")) is expected


def test_filter_is_registered():
    """Expose shortening through the existing Jinja filter interface."""
    filters = make_filters("fix", [])
    assert filters["shorten_name"]("widget", 36) == "widget"
