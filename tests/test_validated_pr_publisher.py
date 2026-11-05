"""Real Git manifests/history with all remote writes and GitHub calls mocked."""
import json
from pathlib import Path
import subprocess
import sys

import pytest

from lib.validated_pr_publisher import command, digest, publish, verify


def git(root, *args, input=None):
    """Execute Git only in a disposable fixture checkout."""
    return command(['git', *args], root, input=input)


@pytest.fixture
def case(tmp_path):
    """Create a real committed base and an independently planned modification."""
    git(tmp_path, 'init', '-q')
    git(tmp_path, 'config', 'user.name', 'Publisher Test')
    git(tmp_path, 'config', 'user.email', 'test@example.com')
    git(tmp_path, 'remote', 'add', 'origin', 'https://github.com/example/repository.git')
    path = tmp_path / 'pipelines/run.yaml'
    path.parent.mkdir()
    path.write_bytes(b'original\n\n')
    git(tmp_path, 'add', '.')
    git(tmp_path, 'commit', '-qm', 'fixture')
    base = git(tmp_path, 'rev-parse', 'HEAD')
    path.write_bytes(b'validated\n\n')
    request = {'version': 1, 'repository': 'example/repository', 'base': 'release-1',
               'base_oid': base, 'head': 'automation/example/release-1',
               'lifecycle': 'create-or-report', 'marker': 'Generated-Example: true',
               'title': 'Apply validated changes', 'body': 'Policy checked separately.',
               'provenance': {'validator': 'recorded-revision'},
               'changes': [{'path': 'pipelines/run.yaml', 'status': 'M', 'mode': '100644',
                            'before_sha256': digest(b'original\n\n'),
                            'after_sha256': digest(b'validated\n\n')}]}
    return tmp_path, request


class Remote:
    """Use real local Git but replace remote Git/GitHub operations deterministically."""
    def __init__(self, request, old=None, prs=None, fail_push=False):
        """Configure remote branch/PR responses without touching any real remote."""
        self.request, self.old, self.prs = request, old, prs or []
        self.fail_push, self.calls = fail_push, []
        self.base = request['base_oid']

    def __call__(self, args, cwd, **kwargs):
        """Intercept every network command and record mutation attempts."""
        self.calls.append(args)
        if args[:2] == ['git', 'ls-remote']:
            oid = self.base if args[-1] == 'refs/heads/' + self.request['base'] else self.old
            return f'{oid}\t{args[-1]}' if oid else ''
        if args[:2] == ['git', 'fetch']:
            return ''
        if args == ['git', 'rev-parse', 'FETCH_HEAD']:
            return self.old
        if args[:2] == ['git', 'push']:
            if self.fail_push:
                raise RuntimeError('concurrent update rejected')
            return ''
        if args[:3] == ['gh', 'pr', 'list']:
            return json.dumps(self.prs)
        if args[:2] == ['gh', 'pr']:
            assert args[2] in ('create', 'edit')
            return 'https://example.com/pr/1'
        return command(args, cwd, **kwargs)


def test_create_real_commit_without_remote_writes(case):
    """Commit only validated bytes, with DCO/provenance and no force-push."""
    root, request = case
    remote = Remote(request)
    receipt = publish(root, request, remote)
    assert receipt['status'] == 'published'
    assert receipt['provenance'] == request['provenance']
    assert git(root, 'show', receipt['commit'] + ':pipelines/run.yaml') == 'validated'
    assert 'Signed-off-by: Publisher Test <test@example.com>' in git(root, 'show', '-s', '--format=%B', receipt['commit'])
    assert all('--force' not in arg and '--force-with-lease' not in arg for call in remote.calls for arg in call)
    assert git(root, 'rev-parse', 'HEAD') == request['base_oid']


def test_explicit_deletion(case):
    """Publish a deletion only when it exactly matches the validated manifest."""
    root, request = case
    (root / 'pipelines/run.yaml').unlink()
    request['changes'][0].update(status='D', after_sha256=None)
    result = publish(root, request, Remote(request))
    assert not git(root, 'ls-tree', result['commit'], '--', 'pipelines/run.yaml')


@pytest.mark.parametrize('attack', ['content', 'before-hash', 'after-hash', 'mode', 'untracked', 'staged', 'added', 'deleted', 'base', 'origin', 'symlink'])
def test_invalid_checkout_never_reaches_network(case, attack):
    """Reject mismatched bytes, modes, scope, or identities before remote reads."""
    root, request = case
    path = root / 'pipelines/run.yaml'
    if attack == 'content': path.write_text('unexpected')
    elif attack == 'before-hash': request['changes'][0]['before_sha256'] = '0' * 64
    elif attack == 'after-hash': request['changes'][0]['after_sha256'] = '0' * 64
    elif attack == 'mode': path.chmod(0o755)
    elif attack == 'untracked': (root / 'untracked').write_text('unrelated')
    elif attack == 'staged': git(root, 'add', '.')
    elif attack == 'added': request['changes'][0]['status'] = 'A'
    elif attack == 'deleted': path.unlink()
    elif attack == 'base': request['base_oid'] = '0' * 40
    elif attack == 'origin': request['repository'] = 'another/repository'
    elif attack == 'symlink':
        path.unlink()
        path.symlink_to(root / '.git/HEAD')
    remote = Remote(request)
    with pytest.raises(ValueError):
        publish(root, request, remote)
    assert not any(call[0] == 'gh' or call[:2] in (['git', 'push'], ['git', 'ls-remote']) for call in remote.calls)


@pytest.mark.parametrize('path', ['../outside', '/absolute', 'a/../b', './file', '.git/config', 'a\\b', 'a\nb'])
def test_unsafe_manifest_paths(case, path):
    """Reject ambiguous paths before any filesystem mutation."""
    root, request = case
    request['changes'][0]['path'] = path
    with pytest.raises(ValueError): verify(root, request)


def test_stale_remote_base(case):
    """Stop before staging if the release advanced after planning."""
    root, request = case
    remote = Remote(request)
    remote.base = '0' * 40
    with pytest.raises(ValueError, match='remote base advanced'):
        publish(root, request, remote)
    assert not git(root, 'diff', '--cached', '--name-only')


def test_existing_pr_is_reported_without_mutations(case):
    """Retain create-or-report semantics even when the policy head changed."""
    root, request = case
    pr = {'state': 'OPEN', 'headRefName': 'another-policy-revision', 'body': request['marker'], 'url': 'https://example.com/existing', 'number': 1}
    remote = Remote(request, prs=[pr])
    assert publish(root, request, remote)['status'] == 'existing-pr'
    assert not git(root, 'diff', '--cached', '--name-only')
    assert not any(call[:2] == ['git', 'push'] or call[:3] == ['gh', 'pr', 'edit'] for call in remote.calls)


def test_closed_pr_is_not_resurrected(case):
    """Require disposition of a prior PR instead of silently reopening it."""
    root, request = case
    remote = Remote(request, prs=[{'state': 'CLOSED', 'headRefName': request['head'], 'body': '', 'number': 1}])
    with pytest.raises(ValueError, match='disposition'): publish(root, request, remote)


def test_append_only_update_preserves_previous_tip(case):
    """Update through a two-parent commit without rewriting published history."""
    root, request = case
    request['lifecycle'] = 'create-or-update'
    old = git(root, 'commit-tree', 'HEAD^{tree}', '-p', 'HEAD', input='old\n\n' + request['marker'] + '\n')
    pr = {'state': 'OPEN', 'headRefName': request['head'], 'body': request['marker'], 'number': 1, 'url': 'https://example.com/pr/1'}
    result = publish(root, request, Remote(request, old=old, prs=[pr]))
    assert git(root, 'show', '-s', '--format=%P', result['commit']).split() == [request['base_oid'], old]


def test_unmarked_branch_cannot_be_adopted(case):
    """Do not update a branch whose prior commit lacks the generated marker."""
    root, request = case
    request['lifecycle'] = 'create-or-update'
    with pytest.raises(ValueError, match='marker'):
        publish(root, request, Remote(request, old=request['base_oid']))


def test_push_failure_does_not_create_pr(case):
    """A rejected non-force push must stop before PR writes."""
    root, request = case
    remote = Remote(request, fail_push=True)
    with pytest.raises(RuntimeError, match='concurrent'):
        publish(root, request, remote)
    assert not any(call[:3] == ['gh', 'pr', 'create'] for call in remote.calls)


def test_no_changes_never_queries_github(case):
    """An empty checked plan is a no-op, not an empty PR."""
    root, request = case
    (root / 'pipelines/run.yaml').write_bytes(b'original\n\n')
    request['changes'] = []
    assert publish(root, request, Remote(request)) == {'status': 'no-changes'}


def test_identical_tree_reuses_append_only_commit(case):
    """An identical validated tree/base must not create or push another commit."""
    root, request = case
    request['lifecycle'] = 'create-or-update'
    git(root, 'add', '.')
    tree = git(root, 'write-tree')
    git(root, 'read-tree', 'HEAD')
    message = request['marker'] + '\nValidated-Publication-Base: ' + request['base_oid'] + '\n'
    old = git(root, 'commit-tree', tree, '-p', 'HEAD', input=message)
    remote = Remote(request, old=old)
    assert publish(root, request, remote)['commit'] == old
    assert not any(call[:2] == ['git', 'push'] for call in remote.calls)


def test_index_transformation_cannot_change_published_bytes(case):
    """Detect staging filters/other index changes, not just working-tree hashes."""
    root, request = case
    remote = Remote(request)
    def altered(args, cwd, **kwargs):
        """Inject a different staged blob after real git add in a fixture only."""
        result = remote(args, cwd, **kwargs)
        if args[:2] == ['git', 'add']:
            blob = git(root, 'hash-object', '-w', '--stdin', input='not validated')
            git(root, 'update-index', '--cacheinfo', '100644', blob, 'pipelines/run.yaml')
        return result
    with pytest.raises(ValueError, match='staged bytes'):
        publish(root, request, altered)
    assert not any(call[:2] == ['git', 'push'] for call in remote.calls)


@pytest.mark.parametrize('count', [2, 1000])
def test_ambiguous_or_truncated_pr_inventory_blocks_publication(case, count):
    """Do not choose a PR arbitrarily or silently accept incomplete pagination."""
    root, request = case
    pr = {'state': 'OPEN', 'headRefName': request['head'], 'body': request['marker'], 'number': 1, 'url': 'https://example.com/pr'}
    with pytest.raises(ValueError):
        publish(root, request, Remote(request, prs=[pr] * count))
    assert not git(root, 'diff', '--cached', '--name-only')


def test_import_has_no_validator_dependency():
    """Publisher import must not import template or validator execution code."""
    script = Path(__file__).resolve().parents[1] / 'scripts/publish_validated_pr.py'
    result = subprocess.run([sys.executable, '-S', str(script), '--help'],
                            capture_output=True, text=True, check=True)
    assert '--publish' in result.stdout
