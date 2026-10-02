from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

from lib.tekton_matchers import (
    alias_match,
    component_starts_with,
    exact_component_match,
    is_pr_pipeline,
    is_push_pipeline,
    is_push_pipeline_by_cel,
    is_scheduled_pipeline,
    make_parent_matchers,
    make_pipeline_matchers,
    odh_prefix_match,
)


def _make_data(name, annotations=None):
    """Build a minimal PipelineRun data dict."""
    data = {"metadata": {"name": name}}
    if annotations:
        data["metadata"]["annotations"] = annotations
    return data


# --- Pipeline matchers ---


class TestIsPushPipeline:
    def test_matches_on_push_suffix(self):
        assert is_push_pipeline("odh-dashboard-v3-5-on-push", _make_data("odh-dashboard-v3-5-on-push"))

    def test_rejects_pull_request(self):
        assert not is_push_pipeline("odh-dashboard-on-pull-request", _make_data("odh-dashboard-on-pull-request"))

    def test_rejects_schedule(self):
        assert not is_push_pipeline("odh-operator-v2-25-on-schedule", _make_data("odh-operator-v2-25-on-schedule"))

    def test_rejects_arbitrary_name(self):
        assert not is_push_pipeline("something-else", _make_data("something-else"))


class TestIsPushPipelineByCel:
    def test_matches_push_cel(self):
        data = _make_data(
            "odh-dashboard-v3-6",
            {"pipelinesascode.tekton.dev/on-cel-expression": 'event == "push" && target_branch == "rhoai-3.6"'},
        )
        assert is_push_pipeline_by_cel("odh-dashboard-v3-6", data)

    def test_rejects_pr_cel(self):
        data = _make_data(
            "odh-dashboard-pull-request",
            {"pipelinesascode.tekton.dev/on-cel-expression": 'event == "pull_request" && target_branch == "rhoai-3.6"'},
        )
        assert not is_push_pipeline_by_cel("odh-dashboard-pull-request", data)

    def test_rejects_no_cel(self):
        data = _make_data("odh-dashboard-v3-6")
        assert not is_push_pipeline_by_cel("odh-dashboard-v3-6", data)

    def test_rejects_empty_cel(self):
        data = _make_data(
            "odh-dashboard-v3-6",
            {"pipelinesascode.tekton.dev/on-cel-expression": ""},
        )
        assert not is_push_pipeline_by_cel("odh-dashboard-v3-6", data)


class TestIsPrPipeline:
    def test_matches_by_annotation(self):
        data = _make_data("some-pipeline", {"pipelinesascode.tekton.dev/on-event": "[pull_request]"})
        assert is_pr_pipeline("some-pipeline", data)

    def test_matches_by_name(self):
        assert is_pr_pipeline("odh-dashboard-on-pull-request", _make_data("odh-dashboard-on-pull-request"))

    def test_rejects_push(self):
        assert not is_pr_pipeline("odh-dashboard-v3-5-on-push", _make_data("odh-dashboard-v3-5-on-push"))


class TestIsScheduledPipeline:
    def test_matches_schedule_name(self):
        assert is_scheduled_pipeline("odh-operator-v2-25-on-schedule", _make_data("odh-operator-v2-25-on-schedule"))

    def test_rejects_push(self):
        assert not is_scheduled_pipeline("odh-dashboard-v3-5-on-push", _make_data("odh-dashboard-v3-5-on-push"))


# --- Parent matchers ---


class TestExactComponentMatch:
    def test_matching_prefixes(self):
        parent = _make_data("odh-dashboard-v3-5-on-push")
        child = _make_data("odh-dashboard-on-pull-request")
        assert exact_component_match(parent, child)

    def test_different_prefixes(self):
        parent = _make_data("odh-dashboard-v3-5-on-push")
        child = _make_data("odh-operator-on-pull-request")
        assert not exact_component_match(parent, child)

    def test_no_prefix_extractable(self):
        parent = _make_data("something")
        child = _make_data("something-else")
        assert not exact_component_match(parent, child)


class TestOdhPrefixMatch:
    def test_child_without_odh_matches_parent_with_odh(self):
        parent = _make_data("odh-caikit-nlp-v3-5-on-push")
        child = _make_data("caikit-nlp-on-pull-request")
        assert odh_prefix_match(parent, child)

    def test_child_already_has_odh(self):
        parent = _make_data("odh-dashboard-v3-5-on-push")
        child = _make_data("odh-dashboard-on-pull-request")
        assert not odh_prefix_match(parent, child)

    def test_no_match(self):
        parent = _make_data("odh-dashboard-v3-5-on-push")
        child = _make_data("caikit-nlp-on-pull-request")
        assert not odh_prefix_match(parent, child)


class TestComponentStartsWith:
    def test_parent_starts_with_child(self):
        parent = _make_data("odh-dashboard-extra-v3-5-on-push")
        child = _make_data("odh-dashboard-on-pull-request")
        assert component_starts_with(parent, child)

    def test_no_prefix_overlap(self):
        parent = _make_data("odh-operator-v2-25-on-push")
        child = _make_data("odh-dashboard-on-pull-request")
        assert not component_starts_with(parent, child)


class TestAliasMatch:
    def test_alias_matches_short_to_full(self):
        parent = _make_data("odh-guardrails-detector-huggingface-runtime-v3-3-on-push")
        child = _make_data("odh-guardrails-detector-hf-runtime-on-pull-request")
        aliases = {"odh-guardrails-detector-hf-runtime": "odh-guardrails-detector-huggingface-runtime"}
        assert alias_match(parent, child, aliases=aliases)

    def test_alias_matches_full_to_short(self):
        parent = _make_data("odh-guardrails-detector-huggingface-runtime-v3-3-on-push")
        child = _make_data("odh-guardrails-detector-hf-runtime-on-pull-request")
        aliases = {"odh-guardrails-detector-huggingface-runtime": "odh-guardrails-detector-hf-runtime"}
        assert alias_match(parent, child, aliases=aliases)

    def test_no_alias_no_match(self):
        parent = _make_data("odh-guardrails-detector-huggingface-runtime-v3-3-on-push")
        child = _make_data("odh-guardrails-detector-hf-runtime-on-pull-request")
        assert not alias_match(parent, child)

    def test_no_aliases_config(self):
        parent = _make_data("odh-guardrails-detector-huggingface-runtime-v3-3-on-push")
        child = _make_data("odh-guardrails-detector-hf-runtime-on-pull-request")
        assert not alias_match(parent, child, aliases=None)

    def test_unrelated_alias_no_match(self):
        parent = _make_data("odh-dashboard-v3-5-on-push")
        child = _make_data("odh-guardrails-detector-hf-runtime-on-pull-request")
        aliases = {"odh-guardrails-detector-hf-runtime": "odh-guardrails-detector-huggingface-runtime"}
        assert not alias_match(parent, child, aliases=aliases)


# --- Registry discovery ---


class TestMakeMatchers:
    def test_pipeline_matchers_discovered(self):
        matchers = make_pipeline_matchers()
        assert "is_push_pipeline" in matchers
        assert "is_push_pipeline_by_cel" in matchers
        assert "is_pr_pipeline" in matchers
        assert "is_scheduled_pipeline" in matchers
        assert callable(matchers["is_push_pipeline"])

    def test_parent_matchers_discovered(self):
        matchers = make_parent_matchers()
        assert "exact_component_match" in matchers
        assert "odh_prefix_match" in matchers
        assert "component_starts_with" in matchers
        assert "alias_match" in matchers
        assert callable(matchers["exact_component_match"])

    def test_no_cross_contamination(self):
        pm = make_pipeline_matchers()
        par = make_parent_matchers()
        assert "is_push_pipeline" not in par
        assert "exact_component_match" not in pm
