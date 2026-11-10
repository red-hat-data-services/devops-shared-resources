"""Offline report tests: no publication, credentials, or network access."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from lib import tekton_run_report as report


class RunReportTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.jobs = {"jobs": []}

    def add(self, name, status="ready", publication=None, conclusion="success", changes=None):
        self.jobs["jobs"].append({
            "name": f"reconcile ({name}, abc)", "conclusion": conclusion,
            "steps": [{"name": "checkout", "conclusion": "failure"}] if conclusion == "failure" else [],
        })
        directory = self.root / f"plr-enforce-main-{name}" / "enforcement-report"
        directory.mkdir(parents=True)
        result = {"status": status, "changes": changes or []}
        (directory / "result.json").write_text(json.dumps(result))
        if publication:
            (directory / "publication.json").write_text(json.dumps(publication))
        return directory

    def render(self, dry_run=False):
        rows = report.collect(self.root, [self.jobs], "plr-enforce-main-", "example", dry_run)
        return report.render(rows, "https://github.com/example/policy/actions/runs/123", dry_run)

    def test_created_existing_no_change_and_failed_publication(self):
        self.add("created", publication={"status": "published", "url": "https://github.com/example/created/pull/1"})
        self.add("existing", publication={"status": "existing-pr", "url": "https://github.com/example/existing/pull/2"})
        self.add("unchanged", status="no-changes", publication={"status": "no-changes"})
        directory = self.add("failed", conclusion="failure")
        (directory.parent / "enforcement.log").write_text("error: publication denied\n")
        full, summary = self.render()
        for text in ("PR created", "existing PR", "no changes", "job failure", "/pull/1", "/pull/2"):
            self.assertIn(text, summary)
        self.assertIn("publication denied", full)
        self.assertIn("Failed steps", full)

    def test_preview_and_partial_are_not_created_prs(self):
        self.add("preview")
        self.add("partial", status="partial")
        _, summary = self.render(dry_run=True)
        self.assertIn("would create/report PR", summary)
        self.assertNotIn("PR created", summary)
        self.assertIn("partial", summary)

    def test_deletions_listed_but_only_modification_diffs_embedded(self):
        directory = self.add("changed", changes=[
            {"path": ".tekton/deleted.yaml", "status": "D"},
            {"path": ".tekton/modified.yaml", "status": "M"},
        ])
        (directory / "enforcement.patch").write_text(
            "diff --git a/deleted b/deleted\ndeleted file mode 100644\n-SECRET_DELETED_CONTENT\n"
            "diff --git a/modified b/modified\n--- a/modified\n+++ b/modified\n-old\n+new\n")
        full, _ = self.render()
        self.assertIn(".tekton/deleted.yaml", full)
        self.assertIn("Files modified", full)
        self.assertIn("-old\n+new", full)
        self.assertNotIn("SECRET_DELETED_CONTENT", full)
        self.assertIn("<details>", full)

    def test_untrusted_diff_paths_and_urls_cannot_inject_markup(self):
        directory = self.add("malicious", changes=[{"path": "</code><script>evil</script>", "status": "M"}],
                             publication={"status": "published", "url": "https://evil.example/pull/1"})
        (directory / "enforcement.patch").write_text(
            "diff --git a/a b/a\n+</pre></details><script>evil</script>\n")
        full, summary = self.render()
        self.assertNotIn("<script>", full)
        self.assertIn("&lt;script&gt;", full)
        self.assertNotIn("evil.example", summary)
        self.assertEqual(report.pr_link("https://github.com/example/other/pull/1", "example", "malicious"), "—")

    def test_missing_malformed_artifacts_and_checkout_failure(self):
        directory = self.add("checkout", conclusion="failure")
        (directory / "result.json").write_text("not JSON")
        full, summary = self.render()
        self.assertIn("job failure", summary)
        self.assertIn("No result report", full)
        (directory / "result.json").write_text('{"changes": [null]}')
        self.assertIn("No result report", self.render()[0])

    def test_missing_patch_and_no_jobs(self):
        self.add("missing-patch", changes=[{"status": "M", "path": "a"}])
        self.assertIn("Diff unavailable", self.render()[0])
        self.jobs = {"jobs": []}
        self.assertIn("inventory may have failed", self.render()[1])
        with self.assertRaises(ValueError):
            report.collect(self.root, {"message": "API failure"}, "prefix-", "example", False)

    def test_summary_budget_does_not_truncate_full_report(self):
        self.add("one", changes=[{"status": "D", "path": "a"}])
        with patch.object(report, "SUMMARY_LIMIT", 100):
            full, summary = self.render()
        self.assertIn("Summary exceeds", summary)
        self.assertIn("Files deleted", full)

    def test_cli_end_to_end_and_summary_append(self):
        self.add("no-change", status="no-changes")
        jobs = self.root / "jobs.json"
        jobs.write_text(json.dumps([self.jobs]))
        summary = self.root / "summary.md"
        summary.write_text("Existing summary\n")
        destination = self.root / "report.md"
        script = Path(__file__).resolve().parents[1] / "scripts" / "report-tekton-run"
        subprocess.run([sys.executable, "-I", str(script), "--artifacts", str(self.root),
                        "--jobs", str(jobs), "--artifact-prefix", "plr-enforce-main-",
                        "--owner", "example", "--dry-run", "true", "--run-url",
                        "https://github.com/example/policy/actions/runs/123", "--output",
                        str(destination), "--summary", str(summary)], check=True)
        self.assertIn("no changes", destination.read_text())
        self.assertTrue(summary.read_text().startswith("Existing summary\n"))


if __name__ == "__main__":
    unittest.main()
