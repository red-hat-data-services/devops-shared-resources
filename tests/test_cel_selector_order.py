"""Mapping order is selector policy; unrelated CEL is not globally formatted."""
import pytest
from lib.cel import ensure_cel_selectors
from lib.tekton import ABSENT
from lib.tekton_filters import ensure_cel_selectors as filter_selectors

@pytest.mark.parametrize("keys", [("event", "route"), ("route", "event")])
def test_correct_values_wrong_order_follow_mapping(keys):
    values = {"event": "pull_request", "route": "p02"}
    desired = {name: values[name] for name in keys}
    wrong = list(reversed(keys))
    body = '( "keep  ==  spaces" == message || "src/**".pathChanged() )'
    expression = " && ".join(f'{k} == "{values[k]}"' for k in wrong) + " && " + body + "\n"
    expected = "\n&& ".join([f'{k} == "{values[k]}"' for k in keys] + [body]) + "\n"
    assert ensure_cel_selectors(expression, desired) == expected
    assert ensure_cel_selectors(expected, desired) == expected

def test_ordered_compliant_input_is_verbatim_even_with_unowned_clauses_between():
    expression = 'skip == false && (event == \'pull_request\')\n && guard\n && "p02" == route\n'
    assert ensure_cel_selectors(expression, {"event": "pull_request", "route": "p02"}) == expression

@pytest.mark.parametrize("desired", [
    {"gone": ABSENT, "event": "pull_request", "route": "p02"},
    {"event": "pull_request", "gone": ABSENT, "route": "p02"},
    {"event": "pull_request", "route": "p02", "gone": ABSENT},
])
def test_absent_keys_do_not_affect_order(desired):
    expression = 'route == "p02" && gone == "remove" && event == "pull_request" && guard'
    expected = 'event == "pull_request"\n&& route == "p02"\n&& guard'
    assert filter_selectors(expression, desired) == expected
    assert filter_selectors(expected, desired) == expected

def test_partial_policy_preserves_other_selector_values_and_guards():
    expression = 'event == "push" && guard && route == "p02"'
    assert ensure_cel_selectors(expression, {"route": "p02"}) == expression
    changed = ensure_cel_selectors(expression, {"route": "p01"})
    assert changed == 'route == "p01"\n&& event == "push"\n&& guard'

def test_ordering_does_not_rearrange_or_arms_or_nested_branch_conditions():
    body = '( (source_branch == "main" && target_branch == "stable") || ("src/**".pathChanged() && !skip) )'
    expression = 'route == "p02" && event == "pull_request" && ' + body
    result = ensure_cel_selectors(expression, {"event": "pull_request", "route": "p02"})
    assert result == 'event == "pull_request"\n&& route == "p02"\n&& ' + body

@pytest.mark.parametrize("expression", [
    'event == "pull_request" || route == "p02"',
    'event == "pull_request" && (route == "p02" || bypass)',
    'event == "pull_request" && route == "p02" && route == "p01"',
    'event != "push" && route == "p02"',
])
def test_ordering_retains_fail_closed_selector_contract(expression):
    with pytest.raises(ValueError):
        ensure_cel_selectors(expression, {"event": "pull_request", "route": "p02"})
