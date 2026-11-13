"""Tests for CEL condition extraction and AST utilities."""

import pytest

from lib.cel import (
    extract_push_conditions,
    flatten_and,
    path_conditions_to_text,
    replace_tekton_filename,
)


@pytest.mark.parametrize("branch", ["", ' && target_branch == "release"'])
@pytest.mark.parametrize("condition", ["", ' && ("src/**".pathChanged() || "docs/**".pathChanged())'])
def test_extract_push_conditions(branch, condition):
    guards, path_tree = extract_push_conditions(
        f'event == "push"{branch} && !skip_build{condition}',
    )
    assert guards == ["! skip_build"]
    if condition:
        assert path_conditions_to_text(path_tree) == condition.removeprefix(" && ")
    else:
        assert path_tree is None


@pytest.mark.parametrize("expression,message", [
    ("&&&", "failed to parse"),
    ('target_branch == "release"', "no event clause"),
    ('event == "push" || skip_build', "top-level OR"),
])
def test_extract_push_conditions_rejects_unsupported_input(expression, message):
    with pytest.raises(ValueError, match=message):
        extract_push_conditions(expression)


def test_extract_push_conditions_without_guards_or_paths():
    assert extract_push_conditions('event == "push"') == ([], None)


@pytest.mark.parametrize("expression", [
    "x == y", "x != y", "x < y", "x <= y", "x > y", "x >= y", "x in y",
    "(x == y || x != z) && count >= 2",
    'label == "a  ==  b"',
    "label == 'a  ==  b'",
])
def test_path_conditions_comparison_spacing(expression):
    import celpy

    env = celpy.Environment()
    tree = env.compile(expression)
    result = path_conditions_to_text(tree)
    assert result == expression
    assert env.compile(result) == tree


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


def test_flatten_and_preserves_nested_or():
    import celpy

    ast = celpy.Environment().compile('a && (b || c) && d')
    clauses = flatten_and(ast.children[0].children[0])
    assert [path_conditions_to_text(clause) for clause in clauses] == [
        "a", "(b || c)", "d",
    ]
