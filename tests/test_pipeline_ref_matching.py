"""Generic reference qualifiers refine event matching without policy literals."""
from copy import deepcopy

import pytest
from ruamel.yaml import YAML

from lib.tekton import classify_pipeline
from lib.tekton_config import load_config


def configuration(tmp_path, qualifier=None):
    (tmp_path / 'policy.j2').write_text('spec: {}\n')
    ref = {'resolver': 'git', 'params': {'url': 'https://example.org/repo', 'pathInRepo': 'pipelines/special.yaml'}}
    entries = [
        {'name': 'generic', 'file': 'policy.j2', 'match_fn': 'is_pipeline_run'},
        {'name': 'special', 'file': 'policy.j2', 'match_fn': ['is_push_pipeline_by_cel', 'is_scheduled_pipeline'], 'pipeline_ref': ref if qualifier is None else qualifier},
    ]
    with (tmp_path / 'config.yaml').open('w') as stream:
        YAML().dump({'templates': entries}, stream)
    return load_config(str(tmp_path))


def document():
    return {'kind': 'PipelineRun', 'metadata': {'name': 'example-on-push', 'annotations': {
        'pipelinesascode.tekton.dev/on-cel-expression': 'event == "push"',
    }}, 'spec': {'pipelineRef': {'resolver': 'git', 'params': [
        {'name': 'url', 'value': 'https://example.org/repo'},
        {'name': 'pathInRepo', 'value': 'pipelines/special.yaml'},
        {'name': 'revision', 'value': 'main'},
    ]}}}


@pytest.mark.parametrize('scheduled', [False, True])
def test_qualified_match_precedes_legacy_fallback(tmp_path, scheduled):
    config, data = configuration(tmp_path), document()
    if scheduled:
        data['metadata']['name'] = 'example-on-schedule'
    assert classify_pipeline('', data, config) == 'special'
    config.templates.reverse()
    assert classify_pipeline('', data, config) == 'special'


@pytest.mark.parametrize('field,value', [('url', 'other'), ('pathInRepo', 'other')])
def test_qualifiers_require_exact_parameter_values(tmp_path, field, value):
    config, data = configuration(tmp_path), document()
    next(p for p in data['spec']['pipelineRef']['params'] if p['name'] == field)['value'] = value
    assert classify_pipeline('', data, config) == 'generic'


def test_qualifier_does_not_override_event_or_inline_pipeline(tmp_path):
    config, data = configuration(tmp_path), document()
    data['metadata']['name'] = 'example-pull-request'
    assert classify_pipeline('', data, config) == 'generic'
    data = document()
    data['spec']['pipelineSpec'] = {'tasks': []}
    assert classify_pipeline('', data, config) == 'generic'


def test_ambiguous_qualified_matches_and_duplicate_params_fail(tmp_path):
    config, data = configuration(tmp_path), document()
    config.templates.append(deepcopy(config.templates[1]))
    with pytest.raises(ValueError, match='multiple reference-qualified'):
        classify_pipeline('', data, config)
    config.templates.pop()
    data['spec']['pipelineRef']['params'].append({'name': 'url', 'value': 'other'})
    with pytest.raises(ValueError, match='duplicate pipeline reference'):
        classify_pipeline('', data, config)


@pytest.mark.parametrize('qualifier', [{}, {'resolver': 'git'}, {'resolver': 'git', 'params': {}},
    {'resolver': 1, 'params': {'x': 'y'}}, {'resolver': 'git', 'params': {'x': 3}},
    {'resolver': 'git', 'params': {'x': 'y'}, 'extra': 'no'}])
def test_invalid_qualifiers_fail_at_load(tmp_path, qualifier):
    with pytest.raises(ValueError, match='pipeline_ref requires'):
        configuration(tmp_path, qualifier)
