"""Build a clean disposable source fixture for precommit CLI integration tests.

The fixture commit is test data, never a production source pin. No dependencies,
credentials, remotes, pushes or modifications to the source worktree are used.
"""
import argparse
import json
from pathlib import Path
import shutil
import subprocess
import tempfile


def create_candidate_source(destination=None):
    source = Path(__file__).resolve().parents[1]
    root = Path(destination) if destination else Path(tempfile.mkdtemp(prefix="tekton-candidate-")) / "source"
    root.mkdir(parents=True, exist_ok=False)
    shutil.copytree(source / "lib", root / "lib", ignore=shutil.ignore_patterns("__pycache__"))
    (root / "scripts").mkdir()
    shutil.copy2(source / "scripts/validate-tekton", root / "scripts/validate-tekton")
    (root / ".gitignore").write_text("__pycache__/\n*.pyc\n")
    def git(*args):
        return subprocess.check_output(["git", *args], cwd=root, stderr=subprocess.PIPE).decode().strip()
    git("init", "-q")
    git("config", "user.name", "CLI fixture")
    git("config", "user.email", "fixture@example.com")
    git("add", ".")
    git("commit", "-qm", "disposable candidate source fixture, not production pin")
    return {"path": str(root), "fixture_sha": git("rev-parse", "HEAD")}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", type=Path)
    args = parser.parse_args()
    print(json.dumps(create_candidate_source(args.destination)))
