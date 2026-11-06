"""Complete lifecycle inventory with every remote read/write stubbed."""
import json

import pytest

from lib.validated_pr_publisher import publish
from test_validated_pr_publisher import case, Remote, git


def row(number, request, **values):
    return {"number": number, "url": f"https://example.com/pr/{number}", "state": "OPEN",
            "headRefName": f"unrelated/{number}", "baseRefName": request["base"], "body": "", **values}


def api_calls(remote, history=None):
    calls = [call for call in remote.calls if call[:3] == ["gh", "api", "graphql"]]
    if history is not None:
        calls = [call for call in calls if any(arg.startswith("head=") for arg in call) == history]
    return calls


def no_writes(root, remote):
    assert not git(root, "diff", "--cached", "--name-only")
    assert not any(call[:2] in (["git", "push"], ["git", "add"], ["git", "commit-tree"])
                   or call[:3] in (["gh", "pr", "create"], ["gh", "pr", "edit"]) for call in remote.calls)


@pytest.mark.parametrize("lifecycle", ["create-or-report", "create-or-update"])
def test_more_than_1000_open_prs_complete_before_safe_publication(case, lifecycle):
    root, request = case
    request["lifecycle"] = lifecycle
    remote = Remote(request, prs=[row(n, request) for n in range(1, 1102)])
    result = publish(root, request, remote)
    assert result["status"] == "published"
    assert len(api_calls(remote, False)) == 12
    assert len(api_calls(remote, True)) == (1 if lifecycle == "create-or-report" else 0)
    assert all("headRefName: $head" not in call[4] for call in api_calls(remote, False))
    pushes = [call for call in remote.calls if call[:2] == ["git", "push"]]
    assert pushes == [["git", "push", "origin", result["commit"] + ":refs/heads/" + request["head"]]]


@pytest.mark.parametrize("lifecycle", ["create-or-report", "create-or-update"])
@pytest.mark.parametrize("match", ["marker", "head", "conflict"])
def test_late_matches_and_duplicate_conflicts_never_disappear(case, lifecycle, match):
    root, request = case
    request["lifecycle"] = lifecycle
    rows = [row(n, request) for n in range(1, 1002)]
    rows.append(row(1002, request, headRefName=request["head"] if match == "head" else "fork/policy", body=request["marker"]))
    if match == "conflict":
        rows.append(row(1003, request, headRefName=request["head"]))
    remote = Remote(request, prs=rows)
    if match == "conflict":
        with pytest.raises(ValueError, match="multiple matching"):
            publish(root, request, remote)
    elif lifecycle == "create-or-report":
        assert publish(root, request, remote) == {"status": "existing-pr", "url": "https://example.com/pr/1002"}
    elif match == "marker":
        with pytest.raises(ValueError, match="different policy branch"):
            publish(root, request, remote)
    else:
        assert publish(root, request, remote)["status"] == "published"
        edits = [call for call in remote.calls if call[:3] == ["gh", "pr", "edit"]]
        assert len(edits) == 1 and edits[0][3] == "1002"
        return
    assert len(api_calls(remote, False)) == 11
    assert not api_calls(remote, True)
    no_writes(root, remote)


@pytest.mark.parametrize("state", ["CLOSED", "MERGED"])
@pytest.mark.parametrize("lifecycle", ["create-or-report", "create-or-update"])
def test_history_only_for_report_same_base_and_head(case, state, lifecycle):
    root, request = case
    request["lifecycle"] = lifecycle
    rows = [row(n, request, state="CLOSED") for n in range(1, 1102)]
    rows += [row(1200, request, state=state, headRefName=request["head"]),
             row(1201, request, baseRefName="other-base", headRefName=request["head"], body=request["marker"])]
    remote = Remote(request, prs=rows)
    if lifecycle == "create-or-report":
        with pytest.raises(ValueError, match="disposition"):
            publish(root, request, remote)
        assert len(api_calls(remote, True)) == 1
        no_writes(root, remote)
    else:
        assert publish(root, request, remote)["status"] == "published"
        assert not api_calls(remote, True)


def test_other_base_and_other_head_history_do_not_block_report(case):
    root, request = case
    rows = [row(1, request, state="MERGED", headRefName="different-head"),
            row(2, request, state="CLOSED", baseRefName="other-base", headRefName=request["head"]),
            row(3, request, state="OPEN", baseRefName="other-base", headRefName=request["head"], body=request["marker"]),
            row(4, request, body=None)]
    remote = Remote(request, prs=rows)
    assert publish(root, request, remote)["status"] == "published"
    assert len(api_calls(remote, True)) == 1


@pytest.mark.parametrize("fault", ["command", "json", "graphql", "missing-data", "missing-page", "bad-next", "missing-cursor", "repeat-cursor", "false-repeat-cursor", "empty-next", "wrong-base", "wrong-state", "wrong-head", "missing-number", "duplicate-number", "missing-body", "bad-body", "oversized-page"])
def test_incomplete_or_malformed_pagination_blocks_before_mutations(case, fault):
    root, request = case
    request["lifecycle"] = "create-or-update"
    remote = Remote(request)
    count = 0
    def run(args, cwd, **kwargs):
        nonlocal count
        if args[:3] != ["gh", "api", "graphql"]:
            return remote(args, cwd, **kwargs)
        remote.calls.append(args)
        count += 1
        if fault == "command" and count == 2:
            raise RuntimeError("API failed")
        if fault == "json" and count == 2:
            return "not JSON"
        if fault == "graphql" and count == 2:
            return json.dumps({"errors": [{"message": "API error"}], "data": {}})
        if fault == "missing-data" and count == 2:
            return "{}"
        node = row(count, request)
        page = {"hasNextPage": True, "endCursor": f"cursor:{count}"}
        nodes = [node]
        if count == 2:
            if fault == "missing-page":
                page = None
            elif fault == "bad-next":
                page["hasNextPage"] = "false"
            elif fault == "missing-cursor":
                page["endCursor"] = None
            elif fault in ("repeat-cursor", "false-repeat-cursor"):
                page.update(endCursor="cursor:1", hasNextPage=fault == "repeat-cursor")
            elif fault == "empty-next":
                nodes = []
                page.update(hasNextPage=False, endCursor=None)
            elif fault == "wrong-base":
                node["baseRefName"] = "different"
            elif fault == "wrong-state":
                node["state"] = "CLOSED"
            elif fault == "wrong-head":
                node["headRefName"] = None
            elif fault == "missing-number":
                node.pop("number")
            elif fault == "duplicate-number":
                node["number"] = 1
            elif fault == "missing-body":
                node.pop("body")
            elif fault == "bad-body":
                node["body"] = []
            elif fault == "oversized-page":
                nodes = [row(n, request) for n in range(10, 111)]
        return json.dumps({"data": {"repository": {"pullRequests": {"nodes": nodes, "pageInfo": page}}}})
    with pytest.raises((ValueError, RuntimeError)):
        publish(root, request, run)
    no_writes(root, remote)


def test_history_errors_cannot_be_mistaken_for_absence(case):
    root, request = case
    remote = Remote(request)
    def run(args, cwd, **kwargs):
        if args[:3] == ["gh", "api", "graphql"] and any(arg.startswith("head=") for arg in args):
            remote.calls.append(args)
            return json.dumps({"errors": [{"message": "history unavailable"}]})
        return remote(args, cwd, **kwargs)
    with pytest.raises(ValueError, match="API errors"):
        publish(root, request, run)
    no_writes(root, remote)
