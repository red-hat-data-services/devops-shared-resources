"""Strict callbacks receive caller-owned overlay identity, never metadata hints."""
from copy import deepcopy
from types import SimpleNamespace

import pytest
from ruamel.yaml import YAML

from lib.tekton import resolve_overlay
from lib.tekton_config import load_config
from lib.tekton_validation import StrictProfile


@pytest.mark.parametrize('component,expected', [('vllm-cpu', 'pz-only/pr.yaml.j2'), ('other', None)])
def test_strict_callback_receives_actual_overlay_and_path(tmp_path, component, expected):
    config_dir = tmp_path/'config'
    (config_dir/'pz-only').mkdir(parents=True)
    (config_dir/'pr.yaml.j2').write_text('metadata:\n  annotations:\n    route: default\n')
    (config_dir/'pz-only/pr.yaml.j2').write_text('metadata:\n  annotations:\n    route: exception\n')
    yaml = YAML()
    with (config_dir/'config.yaml').open('w') as stream:
        yaml.dump({'tekton_layout': 'nested', 'template_overlays': [{
            'pipeline_dir': 'pipelineruns/vllm-cpu', 'template_dir': 'pz-only'}],
            'templates': [{'name': 'pr', 'file': 'pr.yaml.j2', 'match_fn': 'is_pipeline_run'}]}, stream)
    config = load_config(str(config_dir))
    root = tmp_path/'repo'
    relative = f'pipelineruns/{component}/.tekton/same-pull-request.yaml'
    path = root/relative
    path.parent.mkdir(parents=True)
    data = {'kind': 'PipelineRun', 'metadata': {'name': 'same-pull-request', 'annotations': {
        'template_overlay': 'pz-only/pr.yaml.j2', 'pipeline_path': 'pipelineruns/vllm-cpu/forged.yaml'}}}
    with path.open('w') as stream:
        yaml.dump(data, stream)
    context = {'branch': 'main', 'documents': {}, 'template_overlay': 'forged', 'pipeline_path': 'forged'}
    before = deepcopy(context)
    seen = []
    def check(original, candidate, ctx):
        seen.append(ctx)
        assert candidate['metadata']['annotations']['route'] == ('exception' if expected else 'default')
        return True
    policy = SimpleNamespace(check=check, decision=lambda *args: {'status': 'applicable', 'reason': ''})
    profile = StrictProfile(root, 'fix', config, 'main', {relative: data}, context, policy)
    profile.load()
    profile.current_path = path
    overlay = resolve_overlay(config_dir, 'pr.yaml.j2', None, None,
                              pipeline_path=path, repository_root=root, mappings=config.template_overlays)
    profile.render(str(config_dir), 'pr.yaml.j2', data, path.name, branch='main', overlay_path=overlay)
    assert seen[0]['pipeline_path'] == relative
    assert seen[0]['template_overlay'] == expected
    assert context == before
    assert profile.context == before
