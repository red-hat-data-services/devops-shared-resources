"""Shared config helpers for embargo-tools scripts."""


def load_release_config(path: str) -> dict:
    """Load release-branch-config.yaml."""
    from ruamel.yaml import YAML
    _yaml = YAML()
    with open(path) as f:
        return _yaml.load(f) or {}


def get_all_repos(rcm: dict) -> list[str]:
    """Sorted unique repo names across all releases."""
    repos = set()
    for info in rcm.values():
        if isinstance(info, dict):
            for repo in info.get("git-repos", {}):
                repos.add(repo)
    return sorted(repos)


def get_repos_for_branch(rcm: dict, branch: str) -> list[str]:
    """Sorted repo names for a specific release branch."""
    info = rcm.get(branch, {})
    if not isinstance(info, dict):
        return []
    return sorted(info.get("git-repos", {}).keys())


def get_version_for_branch(rcm: dict, branch: str) -> str | None:
    """Full version string for a branch, or None."""
    info = rcm.get(branch, {})
    if not isinstance(info, dict):
        return None
    return info.get("full-version")


def get_branches(rcm: dict) -> list[str]:
    """All branch names from the map."""
    return sorted(rcm.keys())


def get_images_for_branch(rcm: dict, branch: str) -> set[str]:
    """Image names (last path segment of the image URI) for a branch."""
    info = rcm.get(branch, {})
    if not isinstance(info, dict):
        return set()
    images = set()
    for repo_data in info.get("git-repos", {}).values():
        for comp in repo_data.get("components", []):
            img = comp.get("image", "")
            if "/" in img:
                images.add(img.rsplit("/", 1)[-1])
    return images
