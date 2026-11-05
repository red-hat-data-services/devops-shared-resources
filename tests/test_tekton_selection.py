"""Selection plumbing and strict/ordinary engine compatibility (no publication)."""
from copy import deepcopy
import json
from pathlib import Path
import types

import pytest
from ruamel.yaml import YAML

from lib.tekton import _create_jinja_env, render_and_merge
from lib.tekton_config import load_config
from lib.tekton_filters import unique_item
from lib.tekton_reconciliation import context_for, load_policy, scan_phase
from lib.tekton_validation import selection

ROOT = Path(__file__).resolve().parents[1]


def validator():
    module = types.ModuleType("test_validator_engine")
    module.__file__ = str(ROOT / "scripts/validate-tekton")
    exec(compile(Path(module.__file__).read_bytes(), module.__file__, "exec"), module.__dict__)
    return module


def setup(tmp_path, selector='{"status":"applicable","reason":""}'):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "config.yaml").write_text('tekton_layout: nested\nselection_template: selection.json.j2\ncontext_paths: [pipelines/base.yaml]\ntemplates:\n  - name: push\n    file: common.yaml.j2\n    match_fn: is_push_pipeline\n')
    (config_dir / "selection.json.j2").write_text(selector)
    (config_dir / "common.yaml.j2").write_text('spec:\n  taskRunSpecs:\n    - pipelineTaskName: scan\n      computeResources: "{{ ABSENT }}"\n      stepSpecs:\n        - name: scan\n          computeResources:\n            requests: {cpu: 500m, memory: 1Gi}\n            limits: {cpu: 500m, memory: 1Gi}\n')
    return load_config(str(config_dir))


def document(root, component="one", suffix=""):
    path = root / f"pipelineruns/{component}/.tekton/example-push.yaml"
    path.parent.mkdir(parents=True)
    path.write_text('# keep header\nkind: PipelineRun\nmetadata:\n  name: example-on-push\n  labels: null\nspec:\n  params: [] # keep this comment\n' + suffix)
    return path


@pytest.mark.parametrize("status", ["applicable", "skipped", "manual", "unresolved"])
def test_selection_status_context_and_isolation(tmp_path, status):
    config = setup(tmp_path, '{{ {"status": "' + status + '", "reason": context.branch} | tojson }}')
    ctx = {"branch": "release", "documents": {"x": {"value": 1}}}
    before = deepcopy(ctx)
    assert selection(config, {"metadata": {"labels": None}}, "x", "release", ctx) == {"status": status, "reason": "release"}
    assert ctx == before


@pytest.mark.parametrize("value", ['[]', '{}', '{"status":"bad","reason":""}', '{"status":[],"reason":""}', '{"status":"applicable","reason":3}', '{"status":"applicable","reason":"","extra":1}', '{"status":"applicable","status":"skipped","reason":""}', json.dumps({"status": "applicable", "reason": "x" * 4097}), 'x' * 16385])
def test_selection_rejects_invalid_schema(tmp_path, value):
    config = setup(tmp_path, value)
    with pytest.raises((ValueError, json.JSONDecodeError)):
        selection(config, {}, "x", "release", {})


def test_generic_helpers_and_filter_isolation():
    with pytest.raises(ValueError, match="duplicate"):
        unique_item([{"name": "x"}, {"name": "x"}], "name", "x")
    assert unique_item(None, "name", "x") == {}
    first = _create_jinja_env(".", extra_filters={"custom_local": lambda x: x})
    second = _create_jinja_env(".")
    assert "custom_local" in first.filters and "custom_local" not in second.filters
    with pytest.raises(ValueError, match="malformed context"):
        second.from_string('{{ fail("malformed context") }}').render()


def test_strict_engine_nested_identity_and_all_guards_before_writes(tmp_path):
    config = setup(tmp_path)
    target = tmp_path / "target"
    a, b = document(target), document(target, "two")
    before = {p: p.read_bytes() for p in (a, b)}
    calls = []
    def reject_second(original, candidate, context):
        calls.append(candidate)
        assert isinstance(candidate, dict)
        if len(calls) == 2:
            raise ValueError("guard rejected")
        return True
    with pytest.raises(ValueError, match="guard rejected"):
        scan_phase(target, "fix", config, types.SimpleNamespace(check=reject_second), "release", "pipelineruns", "nested")
    assert {p: p.read_bytes() for p in (a, b)} == before
    result = scan_phase(target, "fix", config, None, "release", "pipelineruns", "nested")
    assert result["counts"] == {"planned": 2}
    assert len({row["path"] for row in result["files"]}) == 2
    assert all(b"params: [] # keep this comment" in p.read_bytes() for p in (a, b))
    fixed = {p: p.read_bytes() for p in (a, b)}
    for phase in ("validation", "idempotence"):
        assert scan_phase(target, phase, config, None, "release", "pipelineruns", "nested")["counts"] == {"conformant": 2}
    assert {p: p.read_bytes() for p in (a, b)} == fixed


def test_strict_skip_and_context_mutation_do_not_leak(tmp_path):
    config = setup(tmp_path, '{"status":"unresolved","reason":"deferred"}')
    target = tmp_path / "target"
    path = document(target)
    before = path.read_bytes()
    assert scan_phase(target, "fix", config, None, "release", "pipelineruns", "nested")["counts"] == {"unresolved": 1}
    assert path.read_bytes() == before


def test_ordinary_writer_retains_anchor_support_strict_rejects(tmp_path):
    config = setup(tmp_path)
    config.selection_template = None
    target = tmp_path / "target"
    path = document(target, suffix='  workspaces: &spaces []\n  extra: *spaces\n')
    source = path.read_text()
    engine = validator()
    fixed, _ = engine.fix_tekton_dir(target, "release", None, False, config=config, layout="nested")
    assert fixed
    parsed = YAML(typ="safe").load(path.read_text())
    assert parsed["spec"]["workspaces"] == parsed["spec"]["extra"] == []
    # A changed strict source still fails closed on anchors, as documented.
    config.selection_template = "selection.json.j2"
    path.write_text(source)
    before = path.read_bytes()
    with pytest.raises(ValueError, match="anchor|alias"):
        scan_phase(target, "fix", config, None, "release", "pipelineruns", "nested")
    assert path.read_bytes() == before


def test_declared_context_is_detached_and_duplicate_paths_rejected(tmp_path):
    config = setup(tmp_path)
    target = tmp_path / "target"
    (target / "pipelines").mkdir(parents=True)
    (target / "pipelines/base.yaml").write_text('kind: Pipeline\nspec: {tasks: []}\n')
    context = context_for(target, None, "release", config)
    assert context["documents"]["pipelines/base.yaml"]["kind"] == "Pipeline"
    with pytest.raises(ValueError, match="duplicate"):
        context_for(target, types.SimpleNamespace(CONTEXT_PATHS=config.context_paths), "release", config)


@pytest.mark.parametrize("status", ["skipped", "manual", "unresolved"])
def test_ordinary_optional_selection_preserves_default_output_contract(tmp_path, status):
    config = setup(tmp_path, json.dumps({"status": status, "reason": "not selected"}))
    target = tmp_path / "target"
    path = document(target)
    before = path.read_bytes()
    engine = validator()
    results = engine.validate_tekton_dir(target, "release", None, config=config, layout="nested")
    assert results[path.name] == {"errors": [], "warnings": [], "status": status, "reason": "not selected"}
    assert engine.fix_tekton_dir(target, "release", None, False, config=config, layout="nested")[0] == []
    assert path.read_bytes() == before


def test_context_not_overridden_by_cli_vars(tmp_path):
    config = setup(tmp_path)
    template = Path(config.config_dir) / "context.yaml.j2"
    template.write_text('metadata:\n  name: "{{ context.branch }}"\n')
    result = render_and_merge(config.config_dir, template.name, {}, "x", cli_vars={"context": {"branch": "attack"}}, template_context={"branch": "trusted"})
    assert result["metadata"]["name"] == "trusted"
