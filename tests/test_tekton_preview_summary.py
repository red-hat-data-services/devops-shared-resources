"""Offline presentation tests; never validate or modify patch semantics."""
import json
from html.parser import HTMLParser

import pytest

from lib import tekton_preview_summary as summary
from lib import tekton_reconciliation as operation


def result(changes=None):
    return {"status": "ready", "changes": changes or [],
            "diagnostics": {"counts": {"manual": 2, "unresolved": 3}}}


def test_readable_counts_paths_diff_and_deferred():
    data = result([{"status": "M", "path": ".tekton/a.yaml"},
                   {"status": "D", "path": ".tekton/b.yaml"}])
    text = summary.render_preview_summary(data, "--- a/file\n+++ b/file\n-old\n+new\n")
    for expected in ("modified: 1; deleted: 1", ".tekton/a.yaml", ".tekton/b.yaml",
                     "-old\n+new", "enforcement.patch", "Artifacts", "Manual: 2; unresolved: 3",
                     "not complete branch alignment"):
        assert expected in text
    assert "truncated" not in text.replace("may be truncated", "")


def test_data_cannot_break_out_of_pre_or_details():
    malicious = '</pre></details><script>alert(1)</script>\n```\n# fake\n&copy;'
    data = result([{"status": malicious, "path": malicious}])
    data["status"] = malicious
    data["diagnostics"]["counts"]["manual"] = malicious
    text = summary.render_preview_summary(data, malicious)
    tags = []
    class Tags(HTMLParser):
        def handle_starttag(self, tag, attrs):
            tags.append(tag)
    Tags().feed(text)
    assert "script" not in tags
    assert tags.count("pre") == 2
    assert tags.count("details") == 2
    assert "&lt;/pre&gt;" in text
    assert "&amp;copy;" in text


@pytest.mark.parametrize("character", ["x", "界", "😀", "&", "<", "\x00"])
def test_byte_limits_and_honest_truncation(character):
    data = result([{"status": "M", "path": character * 60_000} for _ in range(3)])
    text = summary.render_preview_summary(data, character * 200_000)
    assert len(text.encode()) <= summary.SUMMARY_LIMIT
    assert "Paths truncated" in text
    assert "Diff truncated" in text
    assert text.endswith("</details>\n")
    text = summary.render_preview_summary(data, character * 200_000, limit=4096)
    assert len(text.encode()) <= 4096
    assert "Diff truncated" in text


def test_no_changes():
    text = summary.render_preview_summary(result(), "")
    assert "Planned files: **0**" in text
    assert "No planned file changes" in text
    assert "No diff" in text
    assert "Manual: 2; unresolved: 3" in text


def test_append_preserves_existing_summary_and_patch(tmp_path):
    destination = tmp_path / "summary.md"
    destination.write_bytes(b"Existing shared SHA\n")
    patch = tmp_path / "enforcement.patch"
    original = ("+😀<&\n" * 80_000).encode()
    patch.write_bytes(original)
    summary.append_preview_summary(result(), tmp_path, {"GITHUB_STEP_SUMMARY": str(destination)})
    text = destination.read_text()
    assert text.startswith("Existing shared SHA\n")
    assert "Diff truncated" in text
    assert len(destination.read_bytes()) <= summary.FILE_LIMIT
    assert patch.read_bytes() == original


def test_existing_summary_budget_and_optional_failures(tmp_path, capsys):
    destination = tmp_path / "summary.md"
    original = b"x" * (summary.FILE_LIMIT - 4096)
    destination.write_bytes(original)
    (tmp_path / "enforcement.patch").write_text("+" * 200_000)
    env = {"GITHUB_STEP_SUMMARY": str(destination)}
    summary.append_preview_summary(result(), tmp_path, env)
    assert destination.read_bytes().startswith(original)
    assert len(destination.read_bytes()) <= summary.FILE_LIMIT
    previous = destination.read_bytes()
    summary.append_preview_summary(result(), tmp_path, env)
    assert destination.read_bytes() == previous
    assert "warning:" in capsys.readouterr().err
    summary.append_preview_summary(result(), tmp_path, {})
    summary.append_preview_summary({}, tmp_path / "missing", env)


def test_summary_io_and_render_failures_are_warnings(tmp_path, capsys):
    destination = tmp_path / "summary.md"
    destination.write_text("Existing\n")
    env = {"GITHUB_STEP_SUMMARY": str(destination)}
    summary.append_preview_summary(result(), tmp_path, env)  # Missing patch.
    assert "warning:" in capsys.readouterr().err
    (tmp_path / "enforcement.patch").write_text("+data\n")
    summary.append_preview_summary({"changes": None}, tmp_path, env)
    assert "warning:" in capsys.readouterr().err
    assert destination.read_text() == "Existing\n"


def test_inventory_does_not_append_preview(tmp_path, monkeypatch, capsys):
    destination = tmp_path / "summary.md"
    destination.write_text("Prior inventory\n")
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(destination))
    monkeypatch.setattr(operation, "inventory", lambda args: {"include": []})
    assert operation.main(["--inventory-root", str(tmp_path), "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {"include": []}
    assert destination.read_text() == "Prior inventory\n"


@pytest.mark.parametrize("publish", [False, True])
def test_cli_single_plan_stdout_json_and_preview_only_summary(tmp_path, monkeypatch, capsys, publish):
    target = tmp_path / "target"
    report = tmp_path / "report"
    destination = tmp_path / "summary.md"
    destination.write_text("Prior summary\n")
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(destination))
    calls = []
    data = result([{"status": "M", "path": "example.yaml"}])
    def plan(args, internal):
        calls.append(args)
        internal.mkdir()
        (internal / "enforcement.patch").write_text("-old\n+new\n")
        (internal / "result.json").write_text(json.dumps(data))
        return dict(data)
    monkeypatch.setattr(operation, "plan", plan)
    monkeypatch.setattr(operation, "publish_candidate", lambda *a: {"status": "test"})
    args = ["--path", str(target), "--config-dir", str(tmp_path), "--branch", "test",
            "--report-dir", str(report), "--json"] + (["--publish"] if publish else [])
    assert operation.main(args) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["changes"] == data["changes"]
    assert len(calls) == 1
    assert (report / "enforcement.patch").read_text() == "-old\n+new\n"
    text = destination.read_text()
    assert text.startswith("Prior summary\n")
    assert ("Planned diff" in text) is not publish
