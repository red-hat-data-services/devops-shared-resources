"""Tests for lookup_tools responsibilities."""
from __future__ import annotations

import json
from lib.gap_notifications.lookup_tools import inspect_issue
from lib.gap_notifications.notify_tools import section
from gap_notification_fakes import make_runner
import copy
import pytest
from lib.gap_notifications.dispatch_incidents import Dispatcher, Ledger
from lib.gap_notifications.dispatch_incidents import incident_key
from lib.gap_notifications.lookup_tools import project_tracking, tracking_cell
from gap_notification_fakes import Jira, Slack, observation


# Links

PR = 'https://github.com/acme/repo/pull/42'


@pytest.fixture
def populated(tmp_path):
    jira, slack = Jira(), Slack()
    observations = [observation(component=component, oid=component,
                    evidence={'pr_url': PR}, pipeline='pull-request') for component in ('cpu', 'gpu')]
    Dispatcher(Ledger(tmp_path / 'journal'), jira, slack, trial_id='trial').run(observations)
    return jira, slack, observations


# Read-only tracking must pair each component's verified Jira and Slack links without mutation.
def test_reused_component_paired_links_and_read_only(populated, monkeypatch):
    jira, slack, observations = populated
    before = copy.deepcopy((jira.issues, jira.properties, jira.calls, slack.calls))
    def forbidden(*args, **kwargs):
        raise AssertionError('write is forbidden')
    for method in ('create_bug', 'update_issue', 'put_notification_state', 'transition_to_done'):
        monkeypatch.setattr(jira, method, forbidden)
    projection = project_tracking(observations, jira, trial_id='trial')
    cell = tracking_cell(projection, PR)
    assert 'cpu / pull\\-request: [RHAI-1]' in cell
    assert 'gpu / pull\\-request: [RHAI-2]' in cell
    assert cell.count('[Slack thread]') == 2 and '<br>' in cell
    assert (jira.issues, jira.properties, jira.calls, slack.calls) == before
    assert project_tracking(observations, jira, trial_id='other')['rows'][PR][0]['status'] == 'not_found'


# Closed-only history must be labeled closed, not misrepresented as pending delivery.
def test_closed_only_fallback_is_explicit_and_not_pending(populated):
    jira, _, observations = populated
    jira.close('RHAI-1')
    original_delivery = copy.deepcopy(jira.properties['RHAI-1']['delivery'])
    jira.properties['RHAI-1']['delivery'].update(root=None, last_event=None)
    projected = project_tracking(observations[:1], jira, trial_id='trial')
    entry = projected['rows'][PR][0]
    assert entry['closed'] is True and entry['status'] == 'closed'
    cell = tracking_cell(projected, PR)
    assert '(Closed)' in cell and 'No confirmed Slack thread' in cell
    assert 'pending' not in cell and '[Slack thread]' not in cell
    jira.properties['RHAI-1']['delivery'] = original_delivery
    projected = project_tracking(observations[:1], jira, trial_id='trial')
    assert '(Closed)' in tracking_cell(projected, PR) and '[Slack thread]' in tracking_cell(projected, PR)


# Prefer an open incident; otherwise show the latest valid closed episode.
def test_open_preferred_over_newer_closed_episode(populated):
    jira, _, observations = populated
    newer = copy.deepcopy(jira.issues['RHAI-1'])
    newer['key'] = 'RHAI-3'
    newer['fields']['status']['statusCategory']['key'] = 'done'
    state = copy.deepcopy(jira.properties['RHAI-1'])
    state['episode_id'] = '11111111-1111-4111-8111-111111111111'
    newer['fields']['labels'].append('gap-episode-11111111111141118111111111111111')
    jira.issues['RHAI-3'], jira.properties['RHAI-3'] = newer, state
    projected = project_tracking(observations[:1], jira, trial_id='trial')
    assert projected['rows'][PR][0]['jira_key'] == 'RHAI-1'
    assert projected['rows'][PR][0]['closed'] is False
    assert '(Closed)' not in tracking_cell(projected, PR)
    jira.close('RHAI-1')
    projected = project_tracking(observations[:1], jira, trial_id='trial')
    assert projected['rows'][PR][0]['jira_key'] == 'RHAI-3'
    assert projected['rows'][PR][0]['closed'] is True


# A fresh renderer import must not pull in notification service dependencies.
def test_renderer_fresh_import_blocks_all_service_dependencies():
    import subprocess
    import sys
    from pathlib import Path
    script = """
import builtins, importlib.util, sys
original = builtins.__import__
file_only = sys.argv[2] == 'file'
blocked = ('lib.jira_client', 'lib.gap_notifications.notify_tools',
           'lib.gap_notifications.dispatch_incidents')
if file_only:
    blocked += ('requests',)
def guarded(name, *args, **kwargs):
    if name.startswith(blocked):
        raise AssertionError('renderer imported service dependency: ' + name)
    return original(name, *args, **kwargs)
builtins.__import__ = guarded
if file_only:
    spec = importlib.util.spec_from_file_location('pure_tracking', sys.argv[1])
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    tracking_cell = module.tracking_cell
else:
    from lib.gap_notifications.lookup_tools import tracking_cell
assert tracking_cell(None, 'pr') == 'Not checked'
assert tracking_cell({'status': 'unavailable'}, 'pr') == 'Unavailable'
projection = {'status': 'checked', 'rows': {'pr': [
    {'component': 'cpu', 'jira_key': 'RHAI-1', 'closed': True, 'status': 'closed'}]}}
assert '(Closed)' in tracking_cell(projection, 'pr')
"""
    root = Path(__file__).resolve().parents[2]
    # lib/__init__ already imports requests through PRCreator. Do not broaden
    # this feature into package initialization changes: isolate this module for
    # the strongest guard and separately check ordinary package import for Jira.
    for mode in ('file', 'package'):
        subprocess.run([sys.executable, '-c', script, str(root / 'lib/gap_notifications/lookup_tools.py'), mode],
                       cwd=root, check=True)


# An unconfirmed Slack send must not hide a confirmed Jira key or an older confirmed thread.
def test_pending_slack_and_held_do_not_hide_confirmed_jira(populated):
    jira, _, observations = populated
    jira.properties['RHAI-1']['delivery'].update(root=None, last_event=None)
    state = jira.properties['RHAI-2']
    state['jira_intent'] = {'event_id': state['last_event'], 'outcome': 'failure'}
    projected = project_tracking(observations, jira, trial_id='trial')
    cell = tracking_cell(projected, PR)
    assert '[RHAI-1]' in cell and 'Slack pending / unconfirmed' in cell
    assert '[RHAI-2]' in cell and '[Slack thread]' in cell and 'delivery held' in cell


# Damaged registry records must yield unavailable tracking without unsafe links or leaked
# evidence.
@pytest.mark.parametrize('damage', ['trial', 'identity', 'channel', 'origin', 'unconfirmed_root', 'summary', 'episode', 'duplicate', 'two-open', 'error'])
def test_registry_damage_cannot_publish_links(populated, damage, monkeypatch):
    jira, _, observations = populated
    if damage == 'trial':
        jira.properties['RHAI-1']['trial_id'] = 'wrong'
    elif damage == 'identity':
        jira.properties['RHAI-1']['incident_key'] = incident_key(observations[1])
    elif damage == 'channel':
        jira.properties['RHAI-1']['delivery']['root']['channel'] = 'OTHER'
    elif damage == 'origin':
        jira.properties['RHAI-1']['scope'] = 'production.example/RHAI/C0883L8FARE'
    elif damage == 'unconfirmed_root':
        jira.properties['RHAI-1']['delivery']['last_event'] = None
    elif damage == 'summary':
        jira.issues['RHAI-1']['fields']['project']['key'] = 'OTHER'
    elif damage == 'episode':
        jira.properties['RHAI-1']['episode_id'] = 'not-a-uuid'
    elif damage in ('duplicate', 'two-open'):
        original = jira.find_by_label
        def find(label):
            found = original(label)
            if found and found[0]['key'] == 'RHAI-1':
                if damage == 'duplicate':
                    found.append(copy.deepcopy(found[0]))
                else:
                    issue = copy.deepcopy(jira.issues['RHAI-1'])
                    issue['key'] = 'RHAI-3'
                    state = copy.deepcopy(jira.properties['RHAI-1'])
                    state['episode_id'] = '11111111-1111-4111-8111-111111111111'
                    issue['fields']['labels'].append('gap-episode-11111111111141118111111111111111')
                    jira.issues['RHAI-3'], jira.properties['RHAI-3'] = issue, state
                    found.append(issue)
            return found
        monkeypatch.setattr(jira, 'find_by_label', find)
    else:
        def fail(*args):
            raise RuntimeError('SECRET transport data')
        monkeypatch.setattr(jira, 'find_by_label', fail)
    projected = project_tracking(observations[:1], jira, trial_id='trial')
    assert projected['rows'][PR][0]['status'] == 'unavailable'
    cell = tracking_cell(projected, PR)
    assert 'Unavailable' in cell and 'https://' not in cell and 'SECRET' not in cell


# Search-lag hints must be revalidated, and mismatched issue hints must be rejected.
def test_known_key_search_lag_revalidated_and_wrong_hints_rejected(populated, monkeypatch):
    jira, _, observations = populated
    monkeypatch.setattr(jira, 'find_by_label', lambda label: [])
    identity = incident_key(observations[0])
    projection = project_tracking(observations[:1], jira, trial_id='trial', known_keys={identity: ['RHAI-1']})
    assert projection['rows'][PR][0]['jira_key'] == 'RHAI-1'
    invalid = project_tracking(observations[:1], jira, trial_id='trial', known_keys={identity: ['RHAI-2']})
    assert invalid['rows'][PR][0]['status'] == 'unavailable'


# Condense only no-match rows while preserving distinct held, closed and unavailable details.
def test_render_condenses_only_not_found_entries():
    empty = [{'component': f'other-{i}', 'status': 'not_found'} for i in range(16)]
    links = [
        {'component': 'cpu', 'pipeline': 'pr', 'jira_key': 'RHAI-1',
         'status': 'held', 'closed': False},
        {'component': 'gpu', 'jira_key': 'RHAI-2', 'status': 'closed', 'closed': True},
        {'component': 'arm', 'jira_key': 'RHAI-3', 'status': 'confirmed', 'closed': False,
         'slack_url': 'https://redhat-internal.slack.com/archives/C0883L8FARE/p123000001'},
    ]
    projection = {'status': 'checked', 'channel': 'C0883L8FARE', 'rows': {PR: empty + links}}
    cell = tracking_cell(projection, PR)
    assert cell.count('No confirmed registry match') == 1
    assert cell.endswith('No confirmed registry match (16 other components)')
    assert cell.count('<br>') == 3
    assert 'cpu / pr: [RHAI-1]' in cell and 'delivery held' in cell
    assert 'gpu: [RHAI-2]' in cell and '(Closed)' in cell
    assert 'arm: [RHAI-3]' in cell and '[Slack thread]' in cell
    assert 'Slack pending / unconfirmed' in cell
    projection['rows'][PR].append({'component': 'missing', 'status': 'unavailable'})
    cell = tracking_cell(projection, PR)
    assert 'missing: Unavailable' in cell and '(16 other components)' in cell
    projection['rows'][PR] = empty
    assert tracking_cell(projection, PR) == 'No confirmed registry match (16 components)'
    projection['rows'][PR] = empty[:1]
    assert tracking_cell(projection, PR) == 'No confirmed registry match (1 component)'
    projection['rows'][PR] += links[:1]
    assert tracking_cell(projection, PR).endswith('No confirmed registry match (1 other component)')
    assert tracking_cell(None, PR) == 'Not checked'


# An adapter for the wrong origin must not be queried or used to publish links.
def test_wrong_adapter_origin_never_queries_or_publishes(populated, monkeypatch):
    jira, _, observations = populated
    jira.base_url = 'https://production.example'
    def forbidden(*args):
        raise AssertionError('must not query wrong origin')
    monkeypatch.setattr(jira, 'find_by_label', forbidden)
    assert project_tracking(observations, jira, trial_id='trial') == {'status': 'unavailable'}


# Distinguish unchecked, unavailable and no-match states while escaping displayed metadata.
def test_no_match_not_checked_unavailable_and_safe_rendering():
    obs = observation(evidence={'pr_url': PR})
    projection = project_tracking([obs], Jira(), trial_id='trial')
    assert 'No confirmed registry match' in tracking_cell(projection, PR)
    assert tracking_cell(None, PR) == 'Not checked'
    assert tracking_cell({'status': 'unavailable'}, PR) == 'Unavailable'
    assert tracking_cell(projection, PR + '0') == 'Not checked'
    projection['rows'][PR][0]['status'] = 'unavailable'
    projection['rows'][PR][0]['component'] = '<script>|[x](https://evil.test)'
    cell = tracking_cell(projection, PR)
    assert '<script>' not in cell and '\\|' in cell and '\\[' in cell
    projection['rows'][PR][0].update(jira_key='OTHER-1', slack_url='https://evil.test')
    assert tracking_cell(projection, PR) == 'Unavailable'


@pytest.fixture
def setup(tmp_path):
    return make_runner(tmp_path)


# Inspection must expose the persisted retry deadline and identify a missing visible link.
def test_inspect_retry_deadline_and_visible_missing(setup):
    runner, jira, _ = setup
    runner().run([observation()])
    state = jira.properties['RHAI-1']
    state['delivery']['intent'] = {'id': state['episode_id'], 'event_id': state['last_event'],
        'outcome': 'failure', 'state': 'rate_limited', 'retry_at': 1900000000}
    nodes = jira.issues['RHAI-1']['fields']['description']['content']
    index = next(i for i, node in enumerate(nodes) if node == section('')[0])
    del nodes[index:index + 2]
    report = inspect_issue(jira, 'RHAI-1')
    assert report['visible_link'] == 'missing'
    assert report['slack_intent']['retry_at'] == 1900000000


# Malformed incident identity fields must not pass registry inspection.
@pytest.mark.parametrize('field,value', [('scope', 'wrong'), ('trial_id', 'different'), ('episode_id', 'not-uuid')])
def test_inspect_rejects_malformed_identity(setup, field, value):
    runner, jira, _ = setup
    runner().run([observation()])
    jira.properties['RHAI-1'][field] = value
    assert inspect_issue(jira, 'RHAI-1')['property'] == 'malformed_or_unavailable'


# Missing or malformed properties must produce safe diagnostics without echoing their contents.
@pytest.mark.parametrize('state', [None, {}, {'trial_id': 'SECRET', 'incident_key': 'SECRET'}])
def test_inspect_missing_malformed_safe(setup, state):
    runner, jira, _ = setup
    runner().run([observation()])
    jira.properties['RHAI-1'] = state
    report = inspect_issue(jira, 'RHAI-1')
    assert report['property'] == ('missing' if state is None else 'malformed_or_unavailable')
    assert 'SECRET' not in json.dumps(report)


# Production tracking and inspection must use production links and reject staging adapters.
def test_environment_scoped_tracking_and_inspection(tmp_path):
    from lib.gap_notifications.notify_tools import routing_target as jira_target
    target = jira_target('production')
    jira, slack = Jira(), Slack()
    jira.base_url = target.url
    obs = observation(environment=target.scope, evidence={'pr_url': PR})
    Dispatcher(Ledger(tmp_path / 'p.json'), jira, slack, trial_id='trial', **target.options).run([obs])
    tracking = project_tracking([obs], jira, trial_id='trial', **target.options)
    cell = tracking_cell(tracking, PR)
    assert target.url + '/browse/RHAI-1' in cell and 'stage-redhat' not in cell
    inspected = inspect_issue(jira, 'RHAI-1', **target.options)
    assert inspected['property'] == 'valid'
    assert inspected['visible_metadata_link'] == 'match'
    assert inspected['metadata_url'].startswith(target.url + '/')
    jira.base_url = 'https://stage-redhat.atlassian.net'
    jira.get_issue = lambda *a: (_ for _ in ()).throw(AssertionError('wrong-origin query'))
    assert project_tracking([obs], jira, trial_id='trial', **target.options) == {'status': 'unavailable'}
    with pytest.raises(ValueError, match='environment'):
        inspect_issue(jira, 'RHAI-1', **target.options)


# A display projection may select only the two known Jira origins, never an arbitrary link host.
def test_tracking_renderer_rejects_untrusted_jira_origin():
    tracking = {'status': 'checked', 'jira_origin': 'https://evil.example',
                'rows': {PR: [{'component': 'cpu', 'status': 'confirmed', 'jira_key': 'RHAI-1'}]}}
    assert tracking_cell(tracking, PR) == 'Unavailable'
