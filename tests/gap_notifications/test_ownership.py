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
    assert blocks[0]["text"] == {"type": "mrkdwn", "text": "@api", "verbatim": False}
    assert "".join(b["text"]["text"] for b in blocks[1:]) == body
    assert all(b["text"]["verbatim"] and len(b["text"]["text"]) <= 3000 for b in blocks[1:])
    for status, root in [("failure", False), ("success", True), ("success", False)]:
        assert all(b["text"]["verbatim"] for b in p.blocks({"status": status}, body, root=root))


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
        return '_default: DevOps\nmappings: {}' if name.startswith('konflux') else 'components: {default: {slack_team_handle: devops}}'
    OwnershipPolicy.load(fetch)
    assert len(calls) == len(set(calls)) == 2


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
    p = OwnershipPolicy.load(lambda name: jira if name.startswith('konflux') else teams)
    assert p.extra_fields({'component': 'api-v3-4', 'failed_task': 'clone-repository'}) == {
        'components': [{'name': 'API'}, {'name': 'Serving'}]}
    assert p.extra_fields({'component': 'api-v2'}) == {'components': [{'name': 'V2'}]}
    assert p.blocks({'component': 'api', 'status': 'failure'}, 'body', root=True)[0]['text']['text'] == '@devops'
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
