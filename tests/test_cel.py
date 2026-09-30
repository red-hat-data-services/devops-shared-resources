"""Tests for CEL expression parsing, decomposition, and transformation."""

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

from lib.cel import (
    build_stable_pr_cel,
    decompose_push_cel,
    flatten_and,
    parse_label_list,
    path_conditions_to_text,
    replace_tekton_filename,
)


# --- decompose_push_cel ---


class TestDecomposePushCel:
    """Tests for decomposing push CEL expressions into structural parts."""

    def test_broad_filter(self):
        """Shape A: broad files.all.exists filter."""
        cel = (
            'event == "push" && target_branch == "rhoai-3.6"'
            " && ( files.all.exists(p, !p.matches('^\\\\.tekton/'))"
            ' || ".tekton/odh-dashboard-v3-6-push.yaml".pathChanged() )'
        )
        parts = decompose_push_cel(cel)
        assert 'event' in parts["event"]
        assert '"push"' in parts["event"]
        assert 'target_branch' in parts["branch"]
        assert '"rhoai-3.6"' in parts["branch"]
        assert parts["negative_guards"] == []
        assert "files.all.exists" in parts["path_conditions"]
        assert "pathChanged" in parts["path_conditions"]
        assert parts["path_conditions_tree"] is not None

    def test_fine_grained_with_negative_guard(self):
        """Shape B: fine-grained pathChanged with negative guard."""
        cel = (
            'event == "push" && target_branch == "rhoai-3.6"'
            ' && !("manifests/rhoai/base/params-latest.env".pathChanged())'
            ' && ( ".tekton/odh-workbench-v3-6-push.yaml".pathChanged()'
            ' || "jupyter/utils/**".pathChanged()'
            ' || "jupyter/minimal/ubi9-python-3.12/**".pathChanged() )'
        )
        parts = decompose_push_cel(cel)
        assert '"push"' in parts["event"]
        assert '"rhoai-3.6"' in parts["branch"]
        assert len(parts["negative_guards"]) == 1
        assert "manifests/rhoai/base/params-latest.env" in parts["negative_guards"][0]
        assert "odh-workbench" in parts["path_conditions"]
        assert "jupyter/utils" in parts["path_conditions"]

    def test_custom_path_glob(self):
        """Shape C: custom path glob for helm charts."""
        cel = (
            'event == "push" && target_branch == "rhoai-3.5-ea.2"'
            ' && ( "helm/rhai-on-openshift-chart/**".pathChanged()'
            ' || ".tekton/rhai-on-openshift-chart-v3-5-ea-2-push.yaml".pathChanged() )'
        )
        parts = decompose_push_cel(cel)
        assert '"rhoai-3.5-ea.2"' in parts["branch"]
        assert "helm/rhai-on-openshift-chart" in parts["path_conditions"]

    def test_invalid_cel_raises(self):
        with pytest.raises(ValueError, match="failed to parse"):
            decompose_push_cel("this is not valid CEL &&& !!!")

    def test_missing_event_raises(self):
        with pytest.raises(ValueError, match="no event clause"):
            decompose_push_cel('target_branch == "rhoai-3.6"')

    def test_missing_paths_raises(self):
        with pytest.raises(ValueError, match="no path conditions"):
            decompose_push_cel('event == "push" && target_branch == "rhoai-3.6"')


# --- replace_tekton_filename ---


class TestReplaceTektonFilename:
    def test_replaces_push_filename(self):
        import celpy
        import copy

        env = celpy.Environment()
        cel = '".tekton/odh-dashboard-v3-6-push.yaml".pathChanged()'
        ast = env.compile(cel)
        tree = copy.deepcopy(ast)
        replace_tekton_filename(tree, "odh-dashboard-pull-request.yaml")
        result = path_conditions_to_text(
            tree.children[0].children[0]
        )
        assert "odh-dashboard-pull-request.yaml" in result
        assert "v3-6-push" not in result

    def test_preserves_non_tekton_paths(self):
        import celpy
        import copy

        env = celpy.Environment()
        cel = (
            '( ".tekton/x-v3-6-push.yaml".pathChanged()'
            ' || "jupyter/utils/**".pathChanged() )'
        )
        ast = env.compile(cel)
        tree = copy.deepcopy(ast)
        replace_tekton_filename(tree, "x-pull-request.yaml")
        result = path_conditions_to_text(
            tree.children[0].children[0]
        )
        assert "x-pull-request.yaml" in result
        assert '"jupyter/utils/**"' in result

    def test_preserves_matches_pattern(self):
        """Ensure the ^\\.tekton/ regex in files.all.exists is NOT replaced."""
        import celpy
        import copy

        env = celpy.Environment()
        cel = (
            "( files.all.exists(p, !p.matches('^\\\\.tekton/'))"
            ' || ".tekton/foo-v3-6-push.yaml".pathChanged() )'
        )
        ast = env.compile(cel)
        tree = copy.deepcopy(ast)
        replace_tekton_filename(tree, "foo-pull-request.yaml")
        result = path_conditions_to_text(
            tree.children[0].children[0]
        )
        assert "foo-pull-request.yaml" in result
        # tree_dump preserves the regex — it may add extra escaping
        assert ".tekton/" in result  # regex pattern preserved
        assert "files.all.exists" in result


# --- parse_label_list ---


class TestParseLabelList:
    def test_standard_format(self):
        assert parse_label_list("[kfbuild-all, kfbuild-dashboard]") == [
            "kfbuild-all",
            "kfbuild-dashboard",
        ]

    def test_many_labels(self):
        result = parse_label_list(
            "[kfbuild-all, kfbuild-workbench, kfbuild-cpu, kfbuild-datascience]"
        )
        assert len(result) == 4
        assert result[0] == "kfbuild-all"
        assert result[3] == "kfbuild-datascience"

    def test_empty_string(self):
        assert parse_label_list("") == []

    def test_none(self):
        assert parse_label_list(None) == []

    def test_empty_brackets(self):
        assert parse_label_list("[]") == []


# --- build_stable_pr_cel ---


class TestBuildStablePrCel:
    def test_basic_no_labels_no_branch(self):
        result = build_stable_pr_cel(
            path_conditions_text='".tekton/odh-dashboard-pull-request.yaml".pathChanged()',
        )
        assert 'event == "pull_request"' in result
        assert "odh-dashboard-pull-request.yaml" in result
        assert "has(body.pull_request.labels)" not in result
        assert "target_branch" not in result

    def test_with_target_branch(self):
        result = build_stable_pr_cel(
            path_conditions_text='".tekton/odh-dashboard-pull-request.yaml".pathChanged()',
            target_branch="stable",
        )
        assert 'event == "pull_request"' in result
        assert 'target_branch == "stable"' in result
        assert "odh-dashboard-pull-request.yaml" in result

    def test_with_labels(self):
        result = build_stable_pr_cel(
            path_conditions_text='".tekton/odh-dashboard-pull-request.yaml".pathChanged()',
            labels=["kfbuild-all", "kfbuild-dashboard"],
        )
        assert 'event == "pull_request"' in result
        assert "has(body.pull_request.labels)" in result
        assert '"kfbuild-all"' in result
        assert '"kfbuild-dashboard"' in result
        assert "||" in result

    def test_with_labels_and_branch(self):
        result = build_stable_pr_cel(
            path_conditions_text='".tekton/foo.yaml".pathChanged()',
            labels=["kfbuild-all"],
            target_branch="stable",
        )
        label_pos = result.index("has(body.pull_request.labels)")
        stable_pos = result.index('target_branch == "stable"')
        assert label_pos < stable_pos
        assert "||" in result

    def test_with_negative_guards(self):
        result = build_stable_pr_cel(
            path_conditions_text='".tekton/x.yaml".pathChanged()',
            negative_guards=['!("manifests/rhoai/base/params-latest.env".pathChanged())'],
        )
        assert "manifests/rhoai/base/params-latest.env" in result
        assert "x.yaml" in result


# --- to_stable_pr_cel filter (end-to-end) ---


class TestToStablePrCelFilter:
    """End-to-end tests for the to_stable_pr_cel Jinja filter."""

    def test_broad_filter_no_labels(self):
        from lib.tekton_filters import to_stable_pr_cel

        push_cel = (
            'event == "push" && target_branch == "rhoai-3.6"'
            " && ( files.all.exists(p, !p.matches('^\\\\.tekton/'))"
            ' || ".tekton/odh-dashboard-v3-6-push.yaml".pathChanged() )'
        )
        result = to_stable_pr_cel(
            push_cel,
            "odh-dashboard-pull-request.yaml",
            "",
        )
        assert 'event == "pull_request"' in result
        assert 'event == "push"' not in result
        assert "target_branch" not in result
        assert "odh-dashboard-pull-request.yaml" in result
        assert "v3-6-push" not in result
        assert "files.all.exists" in result
        assert "has(body.pull_request.labels)" not in result

    def test_broad_filter_with_labels(self):
        from lib.tekton_filters import to_stable_pr_cel

        push_cel = (
            'event == "push" && target_branch == "rhoai-3.6"'
            " && ( files.all.exists(p, !p.matches('^\\\\.tekton/'))"
            ' || ".tekton/odh-dashboard-v3-6-push.yaml".pathChanged() )'
        )
        result = to_stable_pr_cel(
            push_cel,
            "odh-dashboard-pull-request.yaml",
            "[kfbuild-all, kfbuild-dashboard]",
        )
        assert 'event == "pull_request"' in result
        assert '"kfbuild-all"' in result
        assert '"kfbuild-dashboard"' in result
        assert "||" in result

    def test_fine_grained_with_negative_guard(self):
        from lib.tekton_filters import to_stable_pr_cel

        push_cel = (
            'event == "push" && target_branch == "rhoai-3.6"'
            ' && !("manifests/rhoai/base/params-latest.env".pathChanged())'
            ' && ( ".tekton/odh-workbench-v3-6-push.yaml".pathChanged()'
            ' || "jupyter/utils/**".pathChanged() )'
        )
        result = to_stable_pr_cel(
            push_cel,
            "odh-workbench-pull-request.yaml",
            "",
        )
        assert 'event == "pull_request"' in result
        assert "manifests/rhoai/base/params-latest.env" in result
        assert "odh-workbench-pull-request.yaml" in result
        assert '"jupyter/utils/**"' in result
        assert "has(body.pull_request.labels)" not in result

    def test_empty_push_cel_raises(self):
        from lib.tekton_filters import ValidationError, to_stable_pr_cel

        with pytest.raises(ValidationError, match="empty"):
            to_stable_pr_cel("", "foo.yaml", "")
