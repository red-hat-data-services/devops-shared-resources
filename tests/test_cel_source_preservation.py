"""Selector edits must not pretty-print unrelated CEL predicates."""

import celpy
import pytest

from lib.cel import ensure_cel_selectors, ensure_cel_tekton_filename


@pytest.mark.parametrize("retained", [
    '( files.all.exists(p, !p.matches("^docker/"))\n'
    '    || ".tekton/build.yaml".pathChanged()\n'
    '    || "docker/Dockerfile".pathChanged() )',
    '(( source\n       || other ))',
    '( source\n    && other )',
    'message  ==  "keep  ==  spaces"',
    "message == '日本語  spaces'",
    '( source\r\n       || other )',
])
@pytest.mark.parametrize("destination", [None, "new"])
def test_retained_predicate_is_exact_source(retained, destination):
    original = f'event == "push"\n&& route == "old"\n&& {retained}'
    result = ensure_cel_selectors(original, {"route": destination})
    expected = ('route == "new"\n&& ' if destination else '') + f'event == "push"\n&& {retained}'
    assert result == expected
    assert ensure_cel_selectors(result, {"route": destination}) == result
    assert celpy.Environment().compile(result) == celpy.Environment().compile(expected)


@pytest.mark.parametrize("ending", ["", "\n", "\n\n", "\r\n"])
def test_added_guard_preserves_scalar_final_newlines(ending):
    original = 'event == "push"\n&& ( source\n     || other )' + ending
    expected = 'route == "new"\n&& ' + original
    assert ensure_cel_selectors(original, {"route": "new"}) == expected


def test_ungrouped_or_gets_mandatory_outer_guard_without_reprinting():
    original = 'source\n  || other'
    result = ensure_cel_selectors(original, {"route": "new"})
    assert result == 'route == "new"\n&& (source\n  || other)'
    env = celpy.Environment()
    program = env.program(env.compile(result))
    for route in ["old", "new"]:
        assert bool(program.evaluate(celpy.json_to_cel({"route": route, "source": True, "other": True}))) == (route == "new")


def test_selector_inside_parenthesized_conjunction_keeps_nested_or_guarded():
    original = '(route == "old" && ( source\n  || other )) && enabled'
    result = ensure_cel_selectors(original, {"route": "new"})
    assert result == 'route == "new"\n&& ( source\n  || other )\n&& enabled'
    env = celpy.Environment()
    program = env.program(env.compile(result))
    for route in ["old", "new"]:
        for enabled in [False, True]:
            assert bool(program.evaluate(celpy.json_to_cel({
                "route": route, "source": True, "other": True, "enabled": enabled,
            }))) == (route == "new" and enabled)


def test_filename_rewrite_composes_with_multiline_source_preservation():
    original = '( "src/**".pathChanged()\n    || ".tekton/old.yaml".pathChanged() )'
    result = ensure_cel_tekton_filename(ensure_cel_selectors(original, {"route": "new"}), "new.yaml")
    assert result == 'route == "new"\n&& ' + original.replace('".tekton/old.yaml"', '".tekton/new.yaml"')


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_yaml_annotation_changes_only_added_guard(newline):
    from ruamel.yaml import YAML
    from ruamel.yaml.scalarstring import LiteralScalarString
    from lib.tekton_yaml import localized_yaml

    source = (
        'metadata:\n  annotations:\n'
        '    pipelinesascode.tekton.dev/on-cel-expression: |\n'
        '      event == "push"\n'
        '      && target_branch == "rhoai-3.7-ea.1"\n'
        '      && ( "src/**".pathChanged()\n'
        '        || ".tekton/build.yaml".pathChanged() )\n'
        'spec: {} # untouched\n'
    ).replace("\n", newline)
    candidate = YAML().load(source)
    key = "pipelinesascode.tekton.dev/on-cel-expression"
    before = candidate["metadata"]["annotations"][key]
    candidate["metadata"]["annotations"][key] = LiteralScalarString(
        ensure_cel_selectors(before, {"konflux_cluster": "stone-prod-p02"})
    )
    output = localized_yaml(source, candidate)
    assert output == source.replace(
        '      event == "push"',
        '      konflux_cluster == "stone-prod-p02"' + newline + '      && event == "push"',
    )
    assert localized_yaml(output, candidate) == output


@pytest.mark.parametrize("gap", ["", " ", "  ", "\n  "])
def test_standalone_negation_keeps_legacy_template_spelling(gap):
    operand = '("manifests/rhoai/base/params-latest.env".pathChanged())'
    original = 'route == "old" && !' + gap + operand
    assert ensure_cel_selectors(original, {"route": None}) == '! ' + operand


def test_standalone_negation_preserves_multiline_operand():
    operand = '( source\n    || other )'
    assert ensure_cel_selectors('route == "old" && !' + operand, {"route": None}) == '! ' + operand


def test_compliant_expression_stays_byte_identical():
    original = ' ( route  ==  "new" )\n  && ( source\n       || other )\n'
    assert ensure_cel_selectors(original, {"route": "new"}) == original
