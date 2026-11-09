from unittest.mock import Mock
from lib.slack_client import SlackClient
from lib.gap_notifications.notify_tools import SlackNotifier
from lib.gap_notifications.ownership import OwnershipPolicy


def test_isolated_blocks_survive_sdk_transport():
    policy = OwnershipPolicy({'mappings': {}}, {'components': {'default': {'slack_team_handle': 'devops'}}})
    body = '@everyone @here arbitrary body'
    blocks = policy.blocks({'status': 'failure'}, body, root=True)
    response = Mock(status_code=200, headers={})
    response.json.return_value = {'ok': True, 'channel': 'C123', 'ts': '1.2'}
    session = Mock(post=Mock(return_value=response))
    notifier = SlackNotifier('fake', 'C123', session=session)
    assert notifier.send(body, blocks=blocks, thread_ts='1.1', client_msg_id='test-id') == {'channel': 'C123', 'ts': '1.2'}
    assert session.post.call_count == 1
    args, kwargs = session.post.call_args
    assert args[0] == 'https://slack.com/api/chat.postMessage'
    payload = kwargs['json']
    assert payload['blocks'] == blocks
    assert payload['text'] == body
    assert not payload.get('link_names')
    assert payload['thread_ts'] == '1.1'
    assert payload['client_msg_id'] == 'test-id'
    assert payload['unfurl_links'] is False and payload['unfurl_media'] is False
    assert blocks[0]['text'] == {'type': 'mrkdwn', 'text': body + '\n', 'verbatim': True}
    assert blocks[1]['text'] == {'type': 'mrkdwn', 'text': '*Owning team:* @devops\n', 'verbatim': False}
    assert all('fields' not in block for block in blocks)
    assert blocks[-1]['type'] == 'context'
    assert blocks[-1]['elements'][0]['verbatim'] is True
    assert not hasattr(SlackClient, 'usergroups')
