from __future__ import annotations

import base64
import json

import pytest

from lib.gap_component_map import (
    COMPONENT_MAP_ENDPOINT,
    component_is_allowed,
    fetch_component_repo_map,
    normalize_component_name,
    repository_components,
)
from lib.github_check_runs import CheckQueryError


def contents(data):
    return json.dumps({"encoding": "base64", "content": base64.b64encode(json.dumps(data).encode()).decode()})


def test_fetch_validates_and_keeps_repository_scope():
    calls = []

    def runner(args):
        calls.append(args)
        return contents({"eval-hub": {"odh-eval-hub": "rhoai/eval"}, "other": {"odh-other": "rhoai/other"}})

    mapping = fetch_component_repo_map(runner)
    assert calls == [["api", COMPONENT_MAP_ENDPOINT]]
    allowed = repository_components(mapping, "eval-hub")
    assert allowed == frozenset({"odh-eval-hub"})
    assert not component_is_allowed("odh-other", allowed)
    assert repository_components(mapping, "missing") == frozenset()
    assert repository_components(mapping, "eval-hub-branch") == frozenset()


@pytest.mark.parametrize("name, expected", [
    ("odh-eval-hub", True),
    ("odh-eval-hub-v3-6", True),
    ("odh-eval-hub-v3-6-1", True),
    ("odh-eval-hub-extra", False),
    ("odh-eval-hub-v3-6-extra", False),
    ("other-odh-eval-hub", False),
])
def test_component_identity(name, expected):
    assert bool(component_is_allowed(name, frozenset({"odh-eval-hub"}))) is expected


@pytest.mark.parametrize("name, expected", [
    ("odh-eval-hub", "odh-eval-hub"),
    ("odh-eval-hub-v3-6", "odh-eval-hub"),
    ("odh-eval-hub-v3-6-1", "odh-eval-hub"),
    ("odh-eval-hub-extra", "odh-eval-hub-extra"),
    ("odh-eval-hub-v3-6-extra", "odh-eval-hub-v3-6-extra"),
    ("odh-eval-hub-v3", "odh-eval-hub-v3"),
    ("other-odh-eval-hub-v3-6", "other-odh-eval-hub"),
    ("", ""),
])
def test_normalize_component_name(name, expected):
    assert normalize_component_name(name) == expected


@pytest.mark.parametrize("data", [None, [], {}, {"repo": []}, {"repo": {"": "image"}}, {"repo": {"component": None}}])
def test_invalid_mapping_is_not_an_empty_allowlist(data):
    with pytest.raises(CheckQueryError):
        fetch_component_repo_map(lambda args: contents(data))


@pytest.mark.parametrize("response", ["not json", "{}", '{"encoding":"base64","content":"@@@"}'])
def test_bad_contents_response_is_an_error(response):
    with pytest.raises(CheckQueryError):
        fetch_component_repo_map(lambda args: response)

def test_exact_and_ea_build_component_allowlisting():
    assert component_is_allowed("odh-foo-v3-6", frozenset({"odh-foo-v3-6"}))
    assert component_is_allowed("odh-foo-v3-7-ea-1", frozenset({"odh-foo"}))
    assert not component_is_allowed("odh-other-v3-7-ea-1", frozenset({"odh-foo"}))
