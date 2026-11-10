import pytest
from lib.gap_notifications.ownership import OwnershipPolicy


def policy():
    return OwnershipPolicy({'_default': 'DevOps', 'mappings': {'api': ['API', 'Serving'], 'api-v2': 'V2'}},
                           {'components': {'default': {'slack_team_handle': 'devops'},
                                           'api': {'slack_team_handle': 'api'}}})


@pytest.mark.parametrize('name,expected', [('api', ['API', 'Serving']), ('api-v3-4', ['API', 'Serving']),
                                          ('api-v2', ['V2']), ('unknown', ['DevOps']), (None, ['DevOps'])])
def test_components(name, expected):
    assert policy().extra_fields({'component': name}) == {'components': [{'name': n} for n in expected]}


@pytest.mark.parametrize('handle', ['here', 'HERE', 'channel', 'Everyone', '<!here>', '@team', 'a b', ''])
@pytest.mark.parametrize('entry', ['default', 'api'])
def test_reject_broadcast_and_malformed_handles(handle, entry):
    teams = {'default': {'slack_team_handle': 'devops'}, 'api': {'slack_team_handle': 'api'}}
    teams[entry]['slack_team_handle'] = handle
    with pytest.raises(ValueError):
        OwnershipPolicy({'mappings': {}}, {'components': teams})


def test_blocks_isolate_handle_and_bound_body():
    p = policy()
    body = "@everyone @here " + "x" * 6001
    blocks = p.blocks({"component": "api-v3-4", "status": "failure"}, body, root=True)
    assert blocks[1]["text"] == {"type": "mrkdwn", "text": "*Owning team:* @api\n", "verbatim": False}
    assert "".join(b["text"]["text"] for b in blocks[2:-1]) == body
    assert all(b["text"]["verbatim"] and len(b["text"]["text"]) <= 3000 for b in blocks[2:-1])
    assert blocks[-1]["type"] == "context"
    for status, root in [("failure", False), ("success", True), ("success", False)]:
        assert all(b["text"]["verbatim"] for b in p.blocks({"status": status}, body, root=root))


def test_failure_summary_integrates_team_and_links_to_both_mapping_files():
    body = ('🔴 *GAP · Build failed — staging test*\n'
            '*Component:* `api-v3-4`\n'
            '*Repository:* `example/repo`\nReported reason: @everyone @here')
    blocks = policy().blocks({'component': 'api-v3-4', 'status': 'failure'}, body, root=True)
    summary, team = blocks[0]['text'], blocks[1]['text']
    assert summary == {'type': 'mrkdwn', 'text': body.split('\n*Repository:*')[0] + '\n', 'verbatim': True}
    assert team == {'type': 'mrkdwn', 'text': '*Owning team:* @api\n', 'verbatim': False}
    assert blocks[2]['text']['text'] == '*Repository:* `example/repo`\nReported reason: @everyone @here'
    assert blocks[2]['text']['verbatim'] is True
    assert all('fields' not in block for block in blocks)
    footer = blocks[-1]
    assert footer['type'] == 'context'
    assert footer['elements'] == [{
        'type': 'mrkdwn', 'verbatim': True,
        'text': 'Ownership mappings: '
                '<https://gitlab.cee.redhat.com/wznoinsk/rhoai-monitoring/-/blob/main/data/rhoai-component-data.yaml|Slack teams> · '
                '<https://gitlab.cee.redhat.com/wznoinsk/rhoai-monitoring/-/blob/main/data/konflux-to-jira-component-map.yaml|Jira components>',
    }]


@pytest.mark.parametrize('body', ['@everyone', '', 'heading\n' + '*Component:* ' + 'x' * 3100])
def test_summary_does_not_parse_untrusted_text_or_exceed_section_limits(body):
    blocks = policy().blocks({'status': 'failure'}, body, root=True)
    assert blocks[0]['text']['verbatim'] is True
    assert all(0 < len(block['text']['text']) <= 3000 for block in blocks[:-1])
    assert all(b['text']['verbatim'] for b in blocks[2:-1])
    if '\n' in body:
        assert ''.join(b['text']['text'] for b in blocks[2:-1]) == body.split('\n', 1)[1]


def test_long_component_and_owner_remain_separate_when_copied():
    component = 'odh-pipeline-runtime-pytorch-rocm-py312-v3-6'
    body = ('🔴 GAP · Build failed — staging test\n'
            f'*Component:* `{component}`\n'
            '*Repository:* `red-hat-data-services/notebooks`\nBuild failed at: 2026-11-09 19:54:41 UTC')
    blocks = policy().blocks({'component': component, 'status': 'failure'}, body, root=True)
    copied = ''.join(block['text']['text'] for block in blocks[:-1])
    assert f'*Component:* `{component}`\n*Owning team:* @devops\n*Repository:*' in copied
    assert all('fields' not in block for block in blocks)


@pytest.mark.parametrize('body', ['!!python/object/apply:os.system [false]', '[]', '_default: []', 'api: 123'])
def test_unsafe_or_malformed(body):
    with pytest.raises(Exception):
        OwnershipPolicy.load(lambda name: body)


def test_fetch_error():
    def fail(name):
        raise OSError('offline')
    with pytest.raises(OSError):
        OwnershipPolicy.load(fail)


def test_fetch_once_each():
    calls = []
    def fetch(name):
        calls.append(name)
        if name == 'automerge_team_mapping.yaml':
            return 'repo_to_component: {}\nrepo_to_slack_handle: {}'
        return '_default: DevOps\nmappings: {}' if name.startswith('konflux') else 'components: {default: {slack_team_handle: devops}}'
    OwnershipPolicy.load(fetch)
    assert len(calls) == len(set(calls)) == 3


def test_representative_upstream_schema_ignores_task_routing():
    jira = '''
_metadata:
  target_project: RHAI
_default: DevOps
_conforma_skip: ['*']
_component_team_tasks: [build-images]
mappings:
  api: [API, Serving]
  api-v2: V2
'''
    teams = '''
components:
  default:
    slack_team_handle: devops
  api:
    slack_team_handle: unknown
    slack_team_channel: ignored
'''
    p = OwnershipPolicy.load(lambda name: 'repo_to_component: {}\nrepo_to_slack_handle: {}' if name == 'automerge_team_mapping.yaml' else jira if name.startswith('konflux') else teams)
    assert p.extra_fields({'component': 'api-v3-4', 'failed_task': 'clone-repository'}) == {
        'components': [{'name': 'API'}, {'name': 'Serving'}]}
    assert p.extra_fields({'component': 'api-v2'}) == {'components': [{'name': 'V2'}]}
    assert p.blocks({'component': 'api', 'status': 'failure'}, 'body', root=True)[1]['text']['text'] == '*Owning team:* @devops\n'
    assert p.extra_fields({}) == {'components': [{'name': 'DevOps'}]}


@pytest.mark.parametrize('jira,teams', [
    ({'mappings': {'': 'API'}}, {'components': {'default': {'slack_team_handle': 'devops'}}}),
    ({'mappings': {None: 'API'}}, {'components': {'default': {'slack_team_handle': 'devops'}}}),
    ({'mappings': {}}, {'components': {'default': None}}),
    ({'mappings': {}}, {'components': {'default': 'devops'}}),
    ({'mappings': {}}, {'components': {'default': {'slack_team_handle': 'unknown'}}}),
    ({'mappings': {}}, {'components': {'default': {'slack_team_handle': 'devops'}, ' ': {}}}),
])
def test_invalid_schema_rejected_safely(jira, teams):
    with pytest.raises(ValueError):
        OwnershipPolicy(jira, teams)

@pytest.mark.parametrize('category', ['conflict', 'feasibility'])
@pytest.mark.parametrize('repo,mapped', [('red-hat-data-services/notebooks', True), ('https://github.com/red-hat-data-services/notebooks', True), ('other/notebooks', False), ('https://example.com/red-hat-data-services/notebooks', False)])
def test_repository_ownership(category, repo, mapped):
    p = OwnershipPolicy({'mappings': {'api': 'API'}},
                        {'components': {'default': {'slack_team_handle': 'devops'}}},
                        {'repo_to_component': {'notebooks': ['Images', 'Server']},
                         'repo_to_slack_handle': {'notebooks': 'redhat-ai-notebooks'}})
    obs = dict(category=category, repository=repo, component='api', status='failure')
    assert p.extra_fields(obs)['components'] == [{'name': n} for n in (['Images', 'Server'] if mapped else ['DevOps'])]
    assert ('@redhat-ai-notebooks' if mapped else '@devops') in p.blocks(obs, 'body', root=True)[1]['text']['text']
    assert 'Repository ownership' in p.blocks(obs, 'body', root=True)[-1]['elements'][0]['text']
    assert p.extra_fields(dict(obs, category='build')) == {'components': [{'name': 'API'}]}

@pytest.mark.parametrize('handle', ['here', 'CHANNEL', 'everyone', 'unknown', '@team', '', ['team']])
def test_invalid_repository_handle(handle):
    with pytest.raises(ValueError):
        OwnershipPolicy({'mappings': {}}, {'components': {'default': {'slack_team_handle': 'devops'}}},
                        {'repo_to_component': {}, 'repo_to_slack_handle': {'notebooks': handle}})


def test_repository_missing_slack_and_duplicate_keys():
    args = ({'mappings': {}}, {'components': {'default': {'slack_team_handle': 'devops'}}})
    p = OwnershipPolicy(*args, {'repo_to_component': {'notebooks': 'Images'}, 'repo_to_slack_handle': {}})
    obs = dict(category='conflict', repository='red-hat-data-services/notebooks', status='failure')
    assert p.extra_fields(obs) == {'components': [{'name': 'Images'}]}
    assert '@devops' in p.blocks(obs, 'body', root=True)[1]['text']['text']
    with pytest.raises(ValueError):
        OwnershipPolicy(*args, {'repo_to_component': {'notebooks': 'Images', 'NOTEBOOKS': 'Other'}, 'repo_to_slack_handle': {}})


def test_current_upstream_without_repository_slack_map():
    p = OwnershipPolicy({'mappings': {}}, {'components': {'default': {'slack_team_handle': 'devops'}}},
                        {'repo_to_component': {'notebooks': 'Images'}})
    obs = dict(category='feasibility', repository='red-hat-data-services/notebooks', status='failure')
    assert p.extra_fields(obs) == {'components': [{'name': 'Images'}]}
    assert '@devops' in p.blocks(obs, 'body', root=True)[1]['text']['text']

@pytest.mark.parametrize('body', ['', 'null', '[]'])
def test_fetched_repository_mapping_must_be_mapping(body):
    def fetch(name):
        if name == 'automerge_team_mapping.yaml':
            return body
        return 'mappings: {}' if name.startswith('konflux') else 'components: {default: {slack_team_handle: devops}}'
    with pytest.raises(ValueError):
        OwnershipPolicy.load(fetch)


@pytest.mark.parametrize('status', [200, 302, 401, 403, 404, 500])
def test_github_fetch_transport(monkeypatch, status):
    from lib.gap_notifications import ownership
    class Response:
        status_code = status
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def iter_content(self, size): return iter([b'repo_to_component: {}'])
    class Session:
        closed = False
        def get(self, url, **kwargs):
            assert self.trust_env is False
            assert url == ('https://api.github.com/repos/red-hat-data-services/rhoai-ops/'
                           'contents/config/automerge_team_mapping.yaml?ref=main')
            assert kwargs == dict(timeout=(5, 15), allow_redirects=False, stream=True, verify=True,
                                  headers={'Authorization': 'Bearer fake-github-token',
                                           'Accept': 'application/vnd.github.raw+json'})
            assert not hasattr(self, 'auth')
            return Response()
        def close(self): self.closed = True
    session = Session()
    monkeypatch.setattr(ownership.requests, 'Session', lambda: session)
    if status == 200:
        assert ownership.fetch_mapping('automerge_team_mapping.yaml', ca_bundle='/internal.pem',
                                       github_token='fake-github-token') == b'repo_to_component: {}'
    else:
        with pytest.raises(ValueError, match='^repository ownership mapping unavailable$'):
            ownership.fetch_mapping('automerge_team_mapping.yaml', ca_bundle='/internal.pem',
                                    github_token='fake-github-token')
    assert session.closed
