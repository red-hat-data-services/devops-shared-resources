"""Local regressions for PR66 correctness boundaries; no live adapters."""
import copy
import io
import json
import pytest
from gap_notification_fakes import make_runner, observation, state, run, LEADER_URL
from lib.gap_notifications.analyze_leader_state import collect_build_observations
from scripts.gap_notify import main


# Explicit invalid receipts must fail before factories, ledger writes, or ordinary dispatch.
@pytest.mark.parametrize('value', [None, False, 0, '', {}, [], [1], 'text', {'receipt': {}}, {'task_id': 2, 'receipt': {}}])
def test_invalid_receipt_before_side_effects(tmp_path, value):
    source = tmp_path / 'obs.json'
    source.write_text('[]')
    receipt = tmp_path / 'receipt.json'
    receipt.write_text(json.dumps(value))
    calls = []
    def factory(*a, **kw):
        calls.append('client')
        return object()
    result = main(['--observations', str(source), '--ledger', str(tmp_path / 'ledger.json'),
                   '--resolve-receipt', str(receipt), '--write'],
                  environ={'JIRA_USER_EMAIL': 'fake', 'JIRA_API_TOKEN': 'fake', 'SLACK_TOKEN': 'fake'},
                  jira_factory=factory, slack_factory=factory, stdout=io.StringIO())
    assert result == 1 and not calls
    assert not (tmp_path / 'ledger.json').exists()
    assert not (tmp_path / 'ledger.json.lock').exists()


# Direct callers cannot bypass CLI validation or conflate supplied null with absence.
@pytest.mark.parametrize('value', [None, False, 0, '', {}, [], [1], 'text'])
def test_dispatch_invalid_receipt_before_ingest(tmp_path, value):
    runner, jira, slack = make_runner(tmp_path)
    with pytest.raises(ValueError):
        runner().run([observation()], receipt=value)
    assert not jira.calls and not slack.calls and not list(tmp_path.iterdir())


# A distinct positive event after recovery cannot send another recovery notice.
def test_unseen_success_after_recovery_is_noop(tmp_path):
    runner, jira, slack = make_runner(tmp_path)
    runner().run([observation()])
    runner().run([observation('success', 'ok')])
    before = copy.deepcopy((jira.properties, jira.calls, slack.calls))
    runner('fresh').run([observation('success', 'different-ok')])
    assert (jira.properties, jira.calls, slack.calls) == before


# An uncertain or definitely rejected create retries only on a later due run after reconciliation.
@pytest.mark.parametrize('error', [TimeoutError, ValueError])
def test_create_retry_after_no_match(tmp_path, monkeypatch, error):
    runner, jira, slack = make_runner(tmp_path)
    clock = [1000.0]
    monkeypatch.setattr('lib.gap_notifications.dispatch_incidents.time.time', lambda: clock[0])
    original = jira.create_bug
    def reject(*a, **kw):
        jira.calls.append('create')
        raise error('fixture')
    jira.create_bug = reject
    runner().run([observation()])
    assert jira.calls == ['create']
    jira.create_bug = original
    runner().run([observation()])
    assert jira.calls == ['create']
    clock[0] += 60
    result = runner().run([observation()])
    assert jira.calls.count('create') == 2 and len(jira.issues) == 1
    audit = result['creates'][next(iter(result['creates']))]['attempts']
    assert len(audit) == 2
    assert audit[0]['started_at'] == 1000 and audit[0]['next_retry_at'] == 1060
    assert audit[0]['state'] == 'reconciliation_required'
    assert audit[1]['started_at'] == 1060 and audit[1]['next_retry_at'] == 1180


def pipeline_state(statuses):
    payload = state(expected_components=['api'], runs=[dict(run('api', s, uid=p), pipeline_name=p) for p, s in statuses])
    receipt = payload['pull-requests'][0]['build-observations']
    receipt['required-pipelines'] = {'schema': 1, 'source': 'revision-pinned-tekton',
        'repository': receipt['repository'], 'pr': receipt['pr'], 'head-sha': receipt['head-sha'],
        'pipelines': [{'component': 'api', 'pipeline_name': p} for p in ['build', 'test']]}
    return payload


def collect(payload):
    return collect_build_observations(payload, environment='stage-redhat.atlassian.net/RHAI/C0883L8FARE',
        leader_url=LEADER_URL, state_url='https://github.com/acme/demo/blob/main/state.json')[0]


# Required evidence aggregates once per component; partial success cannot recover.
@pytest.mark.parametrize('statuses,expected', [([('build', 'failure'), ('test', 'success')], 'failure'),
    ([('test', 'success'), ('build', 'failure')], 'failure'), ([('build', 'success')], 'unknown'),
    ([('build', 'failure')], 'failure'), ([('build', 'success'), ('test', 'success')], 'success'),
    ([('build', 'success'), ('test', 'unknown')], 'unknown')])
def test_required_pipeline_coverage(statuses, expected):
    result = collect(pipeline_state(statuses))
    assert result['status'] == expected and result['component'] == 'api' and 'pipeline' not in result


# Present invalid inventory must not silently use the legacy success path.
@pytest.mark.parametrize('mutation', ['head', 'blank', 'duplicate', 'null'])
def test_invalid_inventory_never_recovers(mutation):
    payload = pipeline_state([('build', 'success')])
    inv = payload['pull-requests'][0]['build-observations']['required-pipelines']
    if mutation == 'head':
        inv['head-sha'] = 'b' * 40
    elif mutation == 'blank':
        inv['pipelines'][0]['pipeline_name'] = ''
    elif mutation == 'duplicate':
        inv['pipelines'].append(copy.deepcopy(inv['pipelines'][0]))
    else:
        payload['pull-requests'][0]['build-observations']['required-pipelines'] = None
    assert collect(payload)['status'] == 'unknown'


# Leader overlap must not disable notifications or alter the configured routing.
@pytest.mark.parametrize('count', [1, 2, 3])
@pytest.mark.parametrize('mode', ['write', 'preview', 'disabled'])
def test_scheduled_leaders_preserve_notification_config(tmp_path, monkeypatch, count, mode):
    from types import SimpleNamespace
    from scripts import gap_pr_monitor as monitor
    from lib.gap_notifications.monitor_hook import NotificationConfig
    leaders = [SimpleNamespace(number=n, url=f'https://github.com/a/b/pull/{n}', head_ref=f'leader-{n}', labels=[], base_ref='main') for n in range(count)]
    monkeypatch.delenv('GAP_RETIRE_PREVIOUS_LEADERS', raising=False)
    monkeypatch.setattr(monitor, 'list_open_leader_prs', lambda *a, **k: leaders)
    monkeypatch.setattr(monitor, 'checkout_leader_branch', lambda *a, **k: None)
    monkeypatch.setattr(monitor, 'resolve_state_file', lambda **k: tmp_path / 'state.json')
    configs = []
    def invoke(*a, **kw):
        configs.append(kw['notification_config'])
        return monitor.MonitorResult(tmp_path / 'state.json', [], [], [], [], False)
    monkeypatch.setattr(monitor, 'run_monitor_publish_and_signal', invoke)
    config = NotificationConfig(mode=mode, ledger=str(tmp_path / 'journal.json'),
                                trial_id='overlapping-leaders', jira_environment='production',
                                channel='C07RGA06207')
    assert monitor.run_scheduled_monitors(tmp_path, leader_repo='a/b', notification_config=config) == 0
    assert len(configs) == count
    assert all(actual is config for actual in configs)


# Notifications follow applicable signals; retained failures never emit a Leader conclusion.
# A synthetic relocated publisher result also guards against loading the obsolete active path.
@pytest.mark.parametrize('signal', ['early', 'retained-failure'])
@pytest.mark.parametrize('broken', [False, True])
@pytest.mark.parametrize('relocated', [False, True])
def test_hook_after_publish_and_applicable_signal(tmp_path, monkeypatch, signal, broken, relocated):
    from scripts import gap_pr_monitor as monitor
    from lib.gap_notifications.monitor_hook import NotificationConfig
    path = tmp_path / 'state.json'
    payload = state(expected_components=['api'], runs=[run('api', 'failure')])
    monitor.save_state(path, payload)
    accepted = (payload['pull-requests'][0]['pr-url'],)
    result = monitor.MonitorResult(path, [], [], [], [], False, notification_urls=accepted)
    monkeypatch.setattr(monitor, 'run_monitor', lambda *a, **kw: result)
    monkeypatch.setattr(monitor, 'sync_leader_body_omit_ignored', lambda *a, **k: False)
    order = []
    signal_shas = []
    published_path = tmp_path / 'GAP History/unsuccessful/fixture/state.json' if relocated else path
    def publish(*a, **kw):
        assert kw['archive'] is False  # Actual failed-batch policy remains active.
        order.append('publish')
        if relocated:
            published_path.parent.mkdir(parents=True)
            published_path.write_bytes(path.read_bytes())
            path.unlink()
        return monitor.PublishResult('b' * 40, True, state_path=published_path)
    monkeypatch.setattr(monitor, 'publish_state', publish)
    monkeypatch.setattr(monitor, 'should_trigger_early_gate', lambda *a: signal == 'early')
    monkeypatch.setattr(monitor, 'early_gate_concluded', lambda *a: False)
    def signal_callback(*a, **kw):
        # The coordinator now uses the semantic child batch and comment
        # history for idempotency; it does not pass a bookkeeping commit.
        signal_shas.append(kw.get('leader_commit'))
        assert 'leader_commit' not in kw
        order.append('signal')
        return True
    monkeypatch.setattr(monitor, 'post_early_gate_trigger_comment', signal_callback)
    def forbidden(*a, **kw):
        raise AssertionError('retained failure must not conclude/green the Leader')
    monkeypatch.setattr(monitor, 'post_leader_conclusion', forbidden)
    def notify(saved, **kw):
        assert kw['state_path'] == published_path
        assert saved == monitor.load_state(published_path)
        assert kw['accepted_urls'] == accepted and kw['published_sha'] == 'b' * 40
        assert path.exists() is (not relocated)
        assert signal_shas == ([None] if signal == 'early' else [])
        order.append('notify')
        if broken:
            raise RuntimeError('fixture')
        return {'status': 'preview'}
    monkeypatch.setattr('lib.gap_notifications.monitor_hook.notify_after_publish', notify)
    monkeypatch.setattr(monitor, '_post_leader_failure_summary', lambda **kw: order.append('summary'))
    output = monitor.run_monitor_publish_and_signal(path, repo_root=tmp_path, publish=True,
        head_ref='leader', leader_pr_url=LEADER_URL, notification_config=NotificationConfig('preview'))
    assert output.state_path == published_path and not output.leader_status_posted
    assert order == ['publish'] + (['signal'] if signal == 'early' else []) + ['notify', 'summary']


# A known key or discoverable episode is adopted even before the retry deadline.
@pytest.mark.parametrize('known', [False, True])
def test_lost_create_response_adopts_without_duplicate(tmp_path, known):
    runner, jira, slack = make_runner(tmp_path)
    original = jira.create_bug
    def lost(*a, **kw):
        result = original(*a, **kw)
        exc = TimeoutError('lost')
        if known:
            exc.key = result['key']
        raise exc
    jira.create_bug = lost
    runner().run([observation()])
    jira.create_bug = original
    if known:
        jira.find_by_label = lambda label: []
    runner().run([observation()])
    assert jira.calls.count('create') == 1 and len(slack.calls) == 1


# Cooldown delays POSTs, not positive reconciliation of a newly visible prior create.
def test_create_cooldown_allows_positive_reconciliation_without_new_events(tmp_path):
    runner, jira, slack = make_runner(tmp_path)
    original = jira.create_bug
    search = jira.find_by_label
    def lost(*a, **kw):
        original(*a, **kw)
        raise TimeoutError('lost')
    jira.create_bug = lost
    jira.find_by_label = lambda label: []
    runner().run([observation()])
    runner().run([])  # No match yet; POST remains in cooldown.
    jira.find_by_label = search
    jira.create_bug = original
    runner().run([])  # Prior issue appears before the cooldown deadline.
    assert jira.calls.count('create') == 1 and len(slack.calls) == 1


# Successful no-match may duplicate under indexing delay; later multiple matches fail closed.
def test_delayed_index_duplicate_preference_and_later_hold(tmp_path, monkeypatch):
    runner, jira, slack = make_runner(tmp_path)
    clock = [1000.0]
    monkeypatch.setattr('lib.gap_notifications.dispatch_incidents.time.time', lambda: clock[0])
    original = jira.create_bug
    def lost(*a, **kw):
        original(*a, **kw)
        raise TimeoutError('lost')
    jira.create_bug = lost
    runner().run([observation()])
    jira.create_bug = original
    search = jira.find_by_label
    jira.find_by_label = lambda label: []
    clock[0] += 60
    runner().run([observation()])
    assert len(jira.issues) == 2
    before = copy.deepcopy((jira.calls, slack.calls))
    jira.find_by_label = search
    result = runner().run([observation()])
    assert result['outbox'][0]['state'] == 'reconciliation_required'
    assert (jira.calls, slack.calls) == before


# Read failures are not no-match, and repeated events cannot bypass durable backoff.
def test_create_retry_registry_failure_and_backoff(tmp_path, monkeypatch):
    runner, jira, _ = make_runner(tmp_path)
    clock = [1000.0]
    monkeypatch.setattr('lib.gap_notifications.dispatch_incidents.time.time', lambda: clock[0])
    jira.fail_create = True
    for n, delay in enumerate([60, 120, 240, 480, 900, 900], 1):
        result = runner().run([observation(oid=f'event-{n}')])
        assert jira.calls.count('create') == n
        journal = next(iter(result['creates'].values()))
        assert journal['next_retry_at'] == clock[0] + delay
        runner().run([observation(oid=f'event-{n}-new')])
        assert jira.calls.count('create') == n
        clock[0] += delay
    def unavailable(label):
        raise TimeoutError('search unavailable')
    jira.find_by_label = unavailable
    runner().run([observation(oid='after-search-failure')])
    assert jira.calls.count('create') == 6


# An intent/result persistence failure terminates dispatch rather than sending again.
@pytest.mark.parametrize('phase', ['intent', 'result'])
def test_create_audit_persistence_failure_stops_dispatch(tmp_path, phase):
    from lib.gap_notifications.dispatch_incidents import LedgerPersistenceError
    runner, jira, slack = make_runner(tmp_path)
    dispatch = runner()
    save = dispatch.ledger.save
    def failing(data):
        creates = list(data.get('creates', {}).values())
        if creates and (phase == 'intent' or creates[0]['state'] == 'confirmed'):
            raise OSError('disk failure')
        save(data)
    dispatch.ledger.save = failing
    with pytest.raises(LedgerPersistenceError):
        dispatch.run([observation()])
    assert jira.calls.count('create') == (0 if phase == 'intent' else 1)
    assert not slack.calls


# Retry-After is an additional floor, never shortened by the local backoff.
def test_create_retry_after_deadline(tmp_path, monkeypatch):
    from lib.jira_client import JiraRateLimitError
    runner, jira, _ = make_runner(tmp_path)
    monkeypatch.setattr('lib.gap_notifications.dispatch_incidents.time.time', lambda: 1000.0)
    def limited(*a, **kw):
        raise JiraRateLimitError('limited', retry_after=2000)
    jira.create_bug = limited
    result = runner().run([observation()])
    journal = next(iter(result['creates'].values()))
    assert journal['next_retry_at'] == 3000
    assert journal['attempts'][0]['next_retry_at'] == 3000


# Additive single-pipeline metadata must not change accepted event IDs or resend.
def test_single_pipeline_metadata_preserves_event_and_delivery(tmp_path):
    payload = state(expected_components=['api'], runs=[dict(run('api', 'failure'), pipeline_name='build')])
    before = collect(payload)
    receipt = payload['pull-requests'][0]['build-observations']
    receipt['required-pipelines'] = {'schema': 1, 'source': 'revision-pinned-tekton',
        'repository': receipt['repository'], 'pr': receipt['pr'], 'head-sha': receipt['head-sha'],
        'pipelines': [{'component': 'api', 'pipeline_name': 'build'}]}
    after = collect(payload)
    assert before['observation_id'] == after['observation_id']
    runner, jira, slack = make_runner(tmp_path)
    runner().run([before])
    runner().run([after])
    assert jira.calls.count('create') == 1 and len(slack.calls) == 1


# Query failures/skips and mismatched current pipeline identities never recover copied evidence.
@pytest.mark.parametrize('mutation', ['query-error', 'query-skipped', 'pipeline', 'head', 'duplicate'])
def test_pipeline_uncertainty(mutation):
    payload = pipeline_state([('build', 'success'), ('test', 'success')])
    receipt = payload['pull-requests'][0]['build-observations']
    if mutation in {'query-error', 'query-skipped'}:
        receipt[mutation] = 'unavailable'
    elif mutation == 'pipeline':
        receipt['runs'][0]['pipeline_name'] = 'other'
    elif mutation == 'head':
        payload['pull-requests'][0]['head-sha'] = 'b' * 40
    else:
        receipt['runs'].append(copy.deepcopy(receipt['runs'][0]))
    assert collect(payload)['status'] == 'unknown'


# Single-pipeline metadata preserves detailed legacy diagnostics, event identity and Slack rendering.
@pytest.mark.parametrize('scenario', ['failure', 'success', 'pending', 'missing-uid', 'query-error'])
def test_single_pipeline_preserves_reason_status_and_rendering(scenario):
    from lib.gap_notifications.notify_tools import slack_message
    status = scenario if scenario in {'failure', 'success', 'pending'} else 'failure'
    detail = 'ImagePullBackOff from component dependency' if status == 'failure' else 'Detailed pipeline diagnostic'
    payload = state(expected_components=['api'], runs=[dict(run('api', status, reason=detail), pipeline_name='build')])
    receipt = payload['pull-requests'][0]['build-observations']
    if scenario == 'missing-uid':
        receipt['runs'][0]['uid'] = ''
    elif scenario == 'query-error':
        receipt['query-error'] = 'Detailed archive query unavailable'
    legacy = collect(payload)
    receipt['required-pipelines'] = {'schema': 1, 'source': 'revision-pinned-tekton',
        'repository': receipt['repository'], 'pr': receipt['pr'], 'head-sha': receipt['head-sha'],
        'pipelines': [{'component': 'api', 'pipeline_name': 'build'}]}
    current = collect(payload)
    assert current['status'] == legacy['status']
    assert current['evidence']['reason'] == legacy['evidence']['reason']
    assert current['observation_id'] == legacy['observation_id']
    if current['status'] in {'failure', 'success'}:
        rendered = slack_message(current, jira_url='https://stage-redhat.atlassian.net/browse/RHAI-1')
        assert detail in rendered
        assert rendered == slack_message(legacy, jira_url='https://stage-redhat.atlassian.net/browse/RHAI-1')
    else:
        with pytest.raises(ValueError, match='definitive outcome'):
            slack_message(current, jira_url='https://stage-redhat.atlassian.net/browse/RHAI-1')


# Required aggregate event identity is independent of receipt ordering.
def test_pipeline_aggregate_order_and_component_identity():
    from lib.gap_notifications.dispatch_incidents import incident_key
    first = collect(pipeline_state([('build', 'failure'), ('test', 'success')]))
    second = collect(pipeline_state([('test', 'success'), ('build', 'failure')]))
    assert first['observation_id'] == second['observation_id']
    assert incident_key(first) == incident_key(second)


# Integrated producer records the complete required set before observations, and state/retry preserve it.
def test_producer_inventory_state_retry_roundtrip():
    from types import SimpleNamespace
    from lib.konflux_gate import evaluate_konflux_gate
    from lib.state_file import StatePullRequest
    from lib.gap_build_retries import BuildRetryManager
    expected = [SimpleNamespace(key='api', name_suffix=p) for p in ['build', 'test']]
    class Client:
        def observe(self, **kw):
            pipeline = next(iter(kw['pipeline_names']))
            return SimpleNamespace(status='success', reason='Completed', url=None, name=pipeline,
                to_dict=lambda: dict(run('api', 'success', uid=pipeline), pipeline_name=pipeline))
    gate, receipt = evaluate_konflux_gate(repo='acme/demo', number=42, sha='a' * 40, expected=expected, client=Client())
    assert receipt['required-pipelines']['pipelines'] == [
        {'component': 'api', 'pipeline_name': p} for p in ['build', 'test']]
    child = state()['pull-requests'][0]
    child['build-observations'] = receipt
    restored = StatePullRequest.from_mapping(child, index=0).to_dict()
    assert restored['build-observations'] == receipt
    # Closed PR cannot invoke retry services and retains all additive metadata.
    manager = object.__new__(BuildRetryManager)
    _, retried = manager.apply(SimpleNamespace(state='CLOSED'), gate, receipt)
    assert retried == receipt
    manager.saved = {}
    manager._history = lambda pr: []
    _, retried = manager.apply(SimpleNamespace(state='OPEN', url='fixture', head_sha='a' * 40), gate, receipt)
    assert retried['required-pipelines'] == receipt['required-pipelines']
    assert retried['runs'] == receipt['runs']


# Fresh volatile journals lose prior attempt knowledge; possible duplicates are intentional policy.
def test_volatile_journal_duplicate_risk(tmp_path):
    runner, jira, _ = make_runner(tmp_path)
    original = jira.create_bug
    def lost(*a, **kw):
        original(*a, **kw)
        raise TimeoutError('lost')
    jira.create_bug = lost
    runner('old').run([observation()])
    jira.create_bug = original
    jira.find_by_label = lambda label: []
    runner('fresh').run([observation()])
    assert len(jira.issues) == 2


# Multiple queued events cannot produce multiple create POSTs in one invocation.
def test_one_create_attempt_per_incident_invocation(tmp_path):
    from lib.gap_notifications.dispatch_incidents import ingest
    runner, jira, _ = make_runner(tmp_path)
    dispatch = runner()
    data = ingest({}, [observation(oid='one'), observation(oid='two')], trial_id='trial')
    for task in data['outbox']:
        task['state'] = 'pending'
    dispatch.ledger.save(data)
    jira.fail_create = True
    dispatch.run([])
    assert jira.calls.count('create') == 1


# Malformed durable deadlines/counters fail closed without exponential overflow or a POST.
@pytest.mark.parametrize('field,value', [('next_retry_at', 'not-a-deadline'), ('next_retry_at', -1),
    ('consecutive_attempts', True), ('consecutive_attempts', 10 ** 100)])
def test_malformed_create_retry_metadata(tmp_path, field, value):
    runner, jira, _ = make_runner(tmp_path)
    jira.fail_create = True
    dispatch = runner()
    data = dispatch.run([observation()])
    journal = next(iter(data['creates'].values()))
    journal[field] = value
    # Exercise malformed persisted metadata rather than only the in-memory validator.
    dispatch.ledger.path.write_text(json.dumps(data))
    dispatch.run([observation()])
    assert jira.calls.count('create') == 1


# A malformed receipt document is rejected before even client construction.
def test_malformed_receipt_json(tmp_path):
    source = tmp_path / 'obs.json'
    source.write_text('[]')
    receipt = tmp_path / 'receipt.json'
    receipt.write_text('{invalid')
    def forbidden(*a, **kw):
        raise AssertionError('client constructed')
    assert main(['--observations', str(source), '--ledger', str(tmp_path / 'ledger.json'),
        '--resolve-receipt', str(receipt), '--write'], environ={}, jira_factory=forbidden,
        slack_factory=forbidden, stdout=io.StringIO()) == 1
    assert not (tmp_path / 'ledger.json.lock').exists()


# Ambiguity elsewhere in valid inventory cannot erase a fresh required failure.
def test_required_failure_dominates_ambiguous_other_pair():
    payload = pipeline_state([('build', 'failure'), ('test', 'success')])
    receipt = payload['pull-requests'][0]['build-observations']
    receipt['runs'].append(dict(receipt['runs'][1], uid='another-test'))
    assert collect(payload)['status'] == 'failure'


# Known key read failures and invalid matching properties never authorize duplicate creation.
@pytest.mark.parametrize('fault', ['known-read', 'property'])
def test_attribution_unavailable_blocks_create(tmp_path, monkeypatch, fault):
    runner, jira, _ = make_runner(tmp_path)
    jira.fail_readback = True
    runner().run([observation()])
    monkeypatch.setattr('lib.gap_notifications.dispatch_incidents.time.time', lambda: 10 ** 12)
    if fault == 'known-read':
        jira.find_by_label = lambda label: []
        def unavailable(key):
            raise TimeoutError('GET unavailable')
        jira.get_issue = unavailable
    else:
        jira.properties['RHAI-1'] = None
    runner().run([observation()])
    assert jira.calls.count('create') == 1


# An atomic result save may succeed before acknowledgement fails; later replay adopts without POST.
def test_result_persistence_ack_lost(tmp_path):
    from lib.gap_notifications.dispatch_incidents import LedgerPersistenceError
    runner, jira, slack = make_runner(tmp_path)
    dispatch = runner()
    save = dispatch.ledger.save
    def lost_ack(data):
        save(data)
        if any(j['state'] == 'confirmed' for j in data.get('creates', {}).values()):
            raise OSError('lost fsync acknowledgement')
    dispatch.ledger.save = lost_ack
    with pytest.raises(LedgerPersistenceError):
        dispatch.run([observation()])
    assert not slack.calls
    runner().run([observation()])
    assert jira.calls.count('create') == 1 and len(slack.calls) == 1
