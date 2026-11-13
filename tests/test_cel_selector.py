"""Desired-state selector reconciliation and independent filename rewriting."""
import celpy
import pytest

from lib.cel import ensure_cel_selectors, ensure_cel_tekton_filename
from lib.tekton import ABSENT
from lib.tekton_filters import ValidationError, ensure_cel_selectors as reconcile_filter


@pytest.mark.parametrize("selector", ['route == "old"', '"old" == route', '(route == "old")',
                                     'route == "old" && route == \'old\''])
@pytest.mark.parametrize("destination", [None, "old", "new"])
def test_reconcile_preserves_other_conditions(selector, destination):
    expression = f'event == "push" && {selector} && !skip && ("src/**".pathChanged() || other)'
    result = ensure_cel_selectors(expression, {"route": destination})
    assert ensure_cel_selectors(result, {"route": destination}) == result
    env = celpy.Environment()
    program = env.program(env.compile(result), functions={
        "pathChanged": lambda path: celpy.celtypes.BoolType(False),
    })
    for event in ["push", "pull_request"]:
        for route in ["old", "new"]:
            actual = program.evaluate(celpy.json_to_cel({"event": event, "route": route, "skip": False, "other": True}))
            assert bool(actual) == (event == "push" and (destination is None or route == destination))


@pytest.mark.parametrize("expression", ['event == "push"', 'route_extra == "old"',
                                       'body.route == "old"', '"route" == "literal"'])
def test_absent_selector_stays_byte_identical(expression):
    assert ensure_cel_selectors(expression, {"route": None}) == expression


@pytest.mark.parametrize("expression", ['route == "old"', '"old" == route', '(route == "old")'])
def test_compliant_selector_stays_byte_identical(expression):
    assert ensure_cel_selectors(expression, {"route": "old"}) == expression


@pytest.mark.parametrize("spacing", [" ", "  ", "\n  "])
def test_added_cluster_guard_keeps_branch_comparison_spacing(spacing):
    expression = (
        'event == "push" && target_branch ==' + spacing + '"rhoai-3.7-ea.1"'
        ' && message == "keep  ==  spaces"'
    )
    desired = {"konflux_cluster": "stone-prod-p02"}
    result = ensure_cel_selectors(expression, desired)
    assert result == (
        'konflux_cluster == "stone-prod-p02"\n'
        '&& event == "push"\n'
        '&& target_branch == "rhoai-3.7-ea.1"\n'
        '&& message == "keep  ==  spaces"'
    )
    assert ensure_cel_selectors(result, desired) == result
    env = celpy.Environment()
    for branch in ["rhoai-3.7-ea.1", "wrong-branch"]:
        for cluster in ["stone-prod-p02", "other"]:
            actual = env.program(env.compile(result)).evaluate(celpy.json_to_cel({
                "event": "push", "target_branch": branch,
                "konflux_cluster": cluster, "message": "keep  ==  spaces",
            }))
            assert bool(actual) == (branch == "rhoai-3.7-ea.1" and cluster == "stone-prod-p02")


def test_adds_guard_around_entire_or():
    expression = ensure_cel_selectors('source || sync', {"route": "new"})
    env = celpy.Environment()
    program = env.program(env.compile(expression))
    for route in ["old", "new"]:
        assert bool(program.evaluate(celpy.json_to_cel({"route": route, "source": False, "sync": True}))) == (route == "new")


def test_absence_and_multiple_desired_selectors():
    expression = 'event == "push" && target_branch == "release" && route == "old" && event_extra == "keep"'
    result = reconcile_filter(expression, {"event": "pull_request", "target_branch": ABSENT, "route": "new"})
    assert "target_branch" not in result and 'event_extra == "keep"' in result
    assert 'event == "pull_request"' in result and 'route == "new"' in result
    assert ensure_cel_selectors('route == "old"', {"route": None}) == "true"
    assert ensure_cel_selectors('route == "old"', {}) == 'route == "old"'


@pytest.mark.parametrize("expression", ['route != ""', 'has(route)', 'route == other',
    'route == 42', 'route == "old" || allowed',
    '(route == "old" || allowed) && event == "push"',
    '! (route == "old")', 'route.startsWith("old")',
    'route == "old" && route == "different"', 'route.field == "old"', '&&&'])
def test_unsupported_selector_is_not_autofixed(expression):
    with pytest.raises(ValueError):
        ensure_cel_selectors(expression, {"route": "new"})
    with pytest.raises(ValidationError):
        reconcile_filter(expression, {"route": "new"})


@pytest.mark.parametrize("desired", ["route", None, [], {None: "new"}, {"route.field": "new"}, {"route": 3}])
def test_policy_validation(desired):
    with pytest.raises(ValueError):
        ensure_cel_selectors('event == "push"', desired)


def test_filename_rewrite_is_receiver_only_and_preserves_formatting():
    expression = ('event == "push"\n&& (\'.tekton/old.yml\'.pathChanged()'
                  ' || ".tekton/old.yml" == filename'
                  ' || "src/**".pathChanged() || receiver.pathChanged())')
    expected = expression.replace("'.tekton/old.yml'.pathChanged()", '".tekton/new.yaml".pathChanged()')
    assert ensure_cel_tekton_filename(expression, "new.yaml") == expected
    assert ensure_cel_tekton_filename(expected, "new.yaml") == expected
    assert ensure_cel_tekton_filename('event == "push"', "new.yaml") == 'event == "push"'


@pytest.mark.parametrize("quote", ["'", '"'])
def test_compliant_filename_preserves_exact_text(quote):
    expression = f'event == "pull_request"\n && {quote}.tekton/pr.yaml{quote}.pathChanged()'
    assert ensure_cel_tekton_filename(expression, "pr.yaml") == expression


@pytest.mark.parametrize("filename", ["../escape.yaml", "folder/pr.yaml", "pr.txt", "", None])
def test_filename_validation(filename):
    with pytest.raises(ValueError):
        ensure_cel_tekton_filename('".tekton/old.yaml".pathChanged()', filename)


def test_removed_apis_not_registered():
    import lib.tekton_filters as filters
    for old in ["strip_release_push_selectors", "strip_cel_selector", "strip_cel_selectors",
                "rewrite_cel_tekton_filename", "cel_selectors", "cel_tekton_filename"]:
        assert not hasattr(filters, old)
