"""Build-family matchers use the existing registry and first-match ordering."""
import pytest
from ruamel.yaml import YAML

from lib.tekton import classify_pipeline
from lib.tekton_config import load_config
from lib.tekton_matchers import is_chart, is_fbc, make_pipeline_matchers


@pytest.fixture(params=[('charts', is_chart, 'helm-chart-build'), ('fbc', is_fbc, 'fbc-fragment-build')])
def family(request):
    name, matcher, pipeline = request.param
    data = {'kind': 'PipelineRun', 'metadata': {'name': 'example-on-push', 'annotations': {
        'pipelinesascode.tekton.dev/on-cel-expression': 'event == "push"',
    }}, 'spec': {'pipelineRef': {'resolver': 'git', 'params': [
        {'name': 'url', 'value': 'https://example.org/repo'},
        {'name': 'pathInRepo', 'value': f'pipelines/{pipeline}.yaml'},
        {'name': 'revision', 'value': 'main'},
    ]}}}
    return name, matcher, data


@pytest.mark.parametrize('scheduled', [False, True])
def test_family_registry_and_event_matching(family, scheduled):
    name, matcher, data = family
    if scheduled:
        data['metadata']['name'] = 'example-on-schedule'
    assert make_pipeline_matchers()[matcher.__name__] is matcher
    assert matcher(data['metadata']['name'], data)
    other = is_fbc if matcher is is_chart else is_chart
    assert not other(data['metadata']['name'], data)


@pytest.mark.parametrize('mutation', ['path', 'resolver', 'inline', 'kind', 'missing'])
def test_family_reference_shape(family, mutation):
    name, matcher, data = family
    if mutation == 'path':
        data['spec']['pipelineRef']['params'][1]['value'] += '.other'
    elif mutation == 'resolver':
        data['spec']['pipelineRef']['resolver'] = 'bundles'
    elif mutation == 'inline':
        data['spec']['pipelineSpec'] = {'tasks': []}
    elif mutation == 'kind':
        data['kind'] = 'TaskRun'
    else:
        data['spec'].pop('pipelineRef')
    assert not matcher(data['metadata']['name'], data)


@pytest.mark.parametrize('name,event', [
    ('example-pull-request', None),
    ('example-on-push', '[pull_request]'),
    ('example-on-schedule', '[pull_request]'),
])
def test_family_excludes_prs(family, name, event):
    _, matcher, data = family
    data['metadata']['name'] = name
    if event:
        data['metadata']['annotations']['pipelinesascode.tekton.dev/on-event'] = event
    assert not matcher(name, data)


def test_family_requires_build_event(family):
    _, matcher, data = family
    data['metadata']['name'] = 'other'
    data['metadata']['annotations'] = {}
    assert not matcher('other', data)


def test_duplicate_reference_params_fail_closed(family):
    _, matcher, data = family
    data['spec']['pipelineRef']['params'].append({'name': 'pathInRepo', 'value': 'other'})
    with pytest.raises(ValueError, match='duplicate pipeline reference parameter'):
        matcher(data['metadata']['name'], data)


def test_url_and_revision_remain_selection_policy_responsibility(family):
    _, matcher, data = family
    data['spec']['pipelineRef']['params'][0]['value'] = 'other-repo'
    data['spec']['pipelineRef']['params'][2]['value'] = 'other-revision'
    assert matcher(data['metadata']['name'], data)


def test_classification_keeps_normal_first_match_order(tmp_path, family):
    name, matcher, data = family
    (tmp_path/'policy.j2').write_text('spec: {}\n')
    entries = [
        {'name': name, 'file': 'policy.j2', 'match_fn': matcher.__name__},
        {'name': 'generic', 'file': 'policy.j2', 'match_fn': 'is_pipeline_run'},
    ]
    with (tmp_path/'config.yaml').open('w') as stream:
        YAML().dump({'templates': entries}, stream)
    config = load_config(str(tmp_path))
    assert not hasattr(config.templates[0], 'pipeline_ref')
    assert classify_pipeline('', data, config) == name
    config.templates.reverse()
    assert classify_pipeline('', data, config) == 'generic'
