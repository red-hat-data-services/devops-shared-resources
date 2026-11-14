"""Container-specific routing and guard-only execution need no selector sidecar."""
from copy import deepcopy
from types import SimpleNamespace

import pytest
from ruamel.yaml import YAML

from lib.tekton import classify_pipeline
from lib.tekton_config import load_config
from lib.tekton_matchers import (is_container_build_push, is_container_build_pull,
                                is_container_build_scheduled, make_pipeline_matchers)
from lib.tekton_reconciliation import scan_phase

MATCHERS = {'push': is_container_build_push, 'pull': is_container_build_pull,
            'scheduled': is_container_build_scheduled}


def document(event='push', path='pipelines/container-build.yaml'):
    name = {'push': 'example-on-push', 'pull': 'example-pull-request',
            'scheduled': 'example-on-schedule'}[event]
    return {'kind': 'PipelineRun', 'metadata': {'name': name},
            'spec': {'pipelineRef': {'resolver': 'git', 'params': [
                {'name': 'pathInRepo', 'value': path}]}}}


@pytest.mark.parametrize('event', MATCHERS)
@pytest.mark.parametrize('path', ['pipelines/container-build.yaml', 'pipelines/multi-arch-container-build.yaml'])
def test_container_reference_and_event(event, path):
    data = document(event, path)
    before = deepcopy(data)
    for kind, matcher in MATCHERS.items():
        assert make_pipeline_matchers()[matcher.__name__] is matcher
        assert matcher(data['metadata']['name'], data) is (kind == event)
    assert data == before


@pytest.mark.parametrize('event', MATCHERS)
@pytest.mark.parametrize('path', ['pipelines/helm-chart-build.yaml', 'pipelines/fbc-fragment-build.yaml', 'other.yaml'])
def test_other_families_cannot_receive_container_policy(event, path):
    data = document(event, path)
    assert not MATCHERS[event](data['metadata']['name'], data)


@pytest.mark.parametrize('event', MATCHERS)
@pytest.mark.parametrize('location', ['tasks', 'finally'])
@pytest.mark.parametrize('scan', [False, True])
def test_inline_requires_real_scan_target(event, location, scan):
    data = document(event)
    data['spec'] = {'pipelineSpec': {location: [{'name': 'rpms-signature-scan' if scan else 'build'}]}}
    assert MATCHERS[event](data['metadata']['name'], data) is scan


@pytest.mark.parametrize('problem', ['duplicate-ref', 'inline-shape', 'tasks-shape', 'duplicate-task'])
def test_ambiguous_or_malformed_evidence_fails(problem):
    data = document()
    if problem == 'duplicate-ref':
        data['spec']['pipelineRef']['params'] *= 2
    else:
        data['spec'] = {'pipelineSpec': 'bad' if problem == 'inline-shape' else {
            'tasks': 'bad' if problem == 'tasks-shape' else [{'name': 'rpms-signature-scan'}],
            'finally': [{'name': 'rpms-signature-scan'}]}}
    with pytest.raises(ValueError):
        is_container_build_push(data['metadata']['name'], data)


def configuration(tmp_path, exclusions=None, fallback=True):
    (tmp_path/'policy.j2').write_text('{}\n')
    entries = [{'name': 'pr', 'file': 'policy.j2', 'match_fn': 'is_pr_pipeline'}]
    if exclusions is not None:
        entries[0]['exclude_branches'] = exclusions
    if fallback:
        entries.append({'name': 'default', 'file': 'policy.j2', 'match_fn': 'is_pipeline_run'})
    with (tmp_path/'config.yaml').open('w') as stream:
        YAML().dump({'tekton_layout': 'nested', 'guard_only': True, 'templates': entries}, stream)
    return load_config(str(tmp_path))


@pytest.mark.parametrize('bad', ['rhoai-*', [None], ['*'], ['main', 'main'], ['rhoai-**'], ['../main'], ['a//b']])
def test_exclusion_config_rejects_bad_patterns(tmp_path, bad):
    with pytest.raises(ValueError, match='exclude_branches'):
        configuration(tmp_path, bad)


@pytest.mark.parametrize('branch,excluded', [('main', False), ('rhoai-3.7-ea.1', True), ('rhoai-', False), ('other', False)])
def test_branch_scope_is_terminal_and_guard_only_without_decision(tmp_path, branch, excluded):
    config = configuration(tmp_path, ['rhoai-*'])
    root = tmp_path/'target'
    path = root/'pipelineruns/example/.tekton/pr.yaml'
    path.parent.mkdir(parents=True)
    data = document('pull')
    with path.open('w') as stream:
        stream.write('# preserve formatting\n')
        YAML().dump(data, stream)
    before = path.read_bytes()
    calls = []
    policy = SimpleNamespace(check=lambda original, candidate, context: calls.append(context) or True)
    assert classify_pipeline(path.name, data, config) == 'pr'
    for phase in ('initial', 'preview', 'fix', 'validation', 'idempotence'):
        result = scan_phase(root, phase, config, policy, branch, 'pipelineruns', 'nested', context={})
        assert result['counts'] == {'skipped' if excluded else 'conformant': 1}
        assert path.read_bytes() == before
    assert len(calls) == (0 if excluded else 5)
    if excluded:
        assert result['files'][0]['reason'] == f'Template pr excluded on branch {branch}'


@pytest.mark.parametrize('bad', [None, 'true', [], 1])
def test_guard_only_requires_boolean(tmp_path, bad):
    configuration(tmp_path)
    path = tmp_path/'config.yaml'
    data = YAML().load(path.read_text())
    data['guard_only'] = bad
    with path.open('w') as stream:
        YAML().dump(data, stream)
    with pytest.raises(ValueError, match='guard_only'):
        load_config(str(tmp_path))


def test_guard_only_conflicts_with_selector(tmp_path):
    configuration(tmp_path)
    (tmp_path/'selector.j2').write_text('{}')
    path = tmp_path/'config.yaml'
    data = YAML().load(path.read_text())
    data['selection_template'] = 'selector.j2'
    with path.open('w') as stream:
        YAML().dump(data, stream)
    with pytest.raises(ValueError, match='guard_only'):
        load_config(str(tmp_path))


@pytest.mark.parametrize('branch,excluded', [('main', False), ('rhoai-3.6', True)])
def test_ordinary_validator_and_fixer_honor_scope_without_profile(tmp_path, branch, excluded):
    import runpy
    from pathlib import Path
    engine = runpy.run_path(str(Path(__file__).resolve().parents[1]/'scripts/validate-tekton'), run_name='scope_fixture')
    config = configuration(tmp_path, ['rhoai-*'])
    (tmp_path/'policy.j2').write_text('metadata:\n  labels:\n    owned: "yes"\n')
    root = tmp_path/'target'
    path = root/'pipelineruns/example/.tekton/pr.yaml'
    path.parent.mkdir(parents=True)
    with path.open('w') as stream:
        YAML().dump(document('pull'), stream)
    before = path.read_bytes()
    initial = engine['validate_tekton_dir'](root, branch, None, config=config, layout='nested')
    assert bool(initial['pr.yaml']['errors']) is not excluded
    if excluded:
        assert initial['pr.yaml']['status'] == 'skipped'
    fixed, _ = engine['fix_tekton_dir'](root, branch, None, False, config=config, layout='nested')
    assert bool(fixed) is not excluded
    assert (path.read_bytes() == before) is excluded
    assert not engine['validate_tekton_dir'](root, branch, None, config=config, layout='nested')['pr.yaml']['errors']


def test_guard_only_unmatched_reports_skipped(tmp_path):
    config = configuration(tmp_path, fallback=False)
    root = tmp_path/'target'
    path = root/'pipelineruns/example/.tekton/push.yaml'
    path.parent.mkdir(parents=True)
    with path.open('w') as stream:
        YAML().dump(document(), stream)
    def guard(*args):
        pytest.fail('unmatched file must not reach guard')
    result = scan_phase(root, 'fix', config, SimpleNamespace(check=guard), 'main',
                        'pipelineruns', 'nested', context={})
    assert result['counts'] == {'skipped': 1}
    assert result['files'][0]['reason'] == 'No matching template'
