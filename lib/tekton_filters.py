"""Jinja2 filters for Tekton template rendering and validation.

CONTRIBUTING FILTERS
====================

Mark new filters with the @jinja_filter decorator. The function name
becomes the filter name in templates.

There are three kinds of filters:

1. TRANSFORM filters — modify the value:

    @jinja_filter
    def my_transform(value):
        return value.replace("old", "new")

   Usage: {{ _ | my_transform }}

2. VALIDATION filters — check the value, raise ValidationError if wrong:

    @jinja_filter
    def must_be_positive(value):
        if int(value) < 0:
            raise ValidationError(f"expected positive, got {value}")
        return value

   Usage: {{ _ | must_be_positive }}

3. COMBINED transform+validation — fix and validate in one step:

    @jinja_filter
    def to_private_quay(value):
        if not re.search(r"quay\\.io/rhoai-private/", value):
            raise ValidationError(
                f"should use quay.io/rhoai-private/, got '{value}'",
                re.sub(r"quay\\.io/rhoai/", "quay.io/rhoai-private/", value),
            )
        return value

   The second argument to ValidationError is the fixed value. In fix mode
   the wrapper returns the fixed value; in validate mode it returns the
   original.

   Usage: {{ _ | to_private_quay }}

Filters can accept arguments from the template:

    @jinja_filter
    def must_be_one_of(value, allowed):
        ...

   Usage: {{ _ | must_be_one_of(approved_pipeline_repos) }}

TESTING FILTERS
===============

Add tests in tests/test_tekton_filters.py. Each filter should have tests
for the pass case, fail case, and (if applicable) the fix case. Example:

    from lib.tekton_filters import to_private_quay, ValidationError

    def test_to_private_quay_already_correct():
        assert to_private_quay("quay.io/rhoai-private/foo") == "quay.io/rhoai-private/foo"

    def test_to_private_quay_needs_fix():
        with pytest.raises(ValidationError) as exc:
            to_private_quay("quay.io/rhoai/foo")
        assert exc.value.fixed_value == "quay.io/rhoai-private/foo"
"""

import re


def jinja_filter(fn):
    """Mark a function for registration as a Jinja filter."""
    fn._is_jinja_filter = True
    return fn


class ValidationError(Exception):
    """Raised by filters when validation fails.

    Args:
        message: Human-readable error description.
        fixed_value: The corrected value (used in fix mode). If None,
                     the original value is kept.
    """
    def __init__(self, message, fixed_value=None):
        super().__init__(message)
        self.fixed_value = fixed_value


class DeleteFileError(ValidationError):
    """Raised by filters to signal the entire file should be deleted."""


class SkipValidation(str):
    """Sentinel string subclass that tells _wrap_filter to skip validation."""
    pass


DELETE_FILE_PREFIX = "DELETE: "


# =============================================================================
# Transform filters
# =============================================================================


@jinja_filter
def repo_name(url: str) -> str:
    """Extract repo name from a git URL.

    >>> repo_name("https://github.com/org/my-repo.git")
    'my-repo'
    """
    return url.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git").split("?")[0]


@jinja_filter
def strip_prefix(value: str, prefix: str = "odh-") -> str:
    """Strip a prefix from a string.

    >>> strip_prefix("odh-dashboard")
    'dashboard'
    """
    if value.startswith(prefix):
        return value[len(prefix):]
    return value


@jinja_filter
def strip_version(value: str) -> str:
    """Strip version suffix.

    >>> strip_version("odh-dashboard-v3-5")
    'odh-dashboard'
    """
    return re.sub(r"-v\d+-\d+.*$", "", value)


@jinja_filter
def split_tag(value: str) -> str:
    """Extract image base from image:tag.

    >>> split_tag("quay.io/org/repo:v1.0")
    'quay.io/org/repo'
    """
    return value.rsplit(":", 1)[0] if ":" in value else value


@jinja_filter
def default_comment(value):
    """Return default on-comment value if empty.

    >>> default_comment("")
    '^/build-konflux'
    >>> default_comment("^/custom")
    '^/custom'
    """
    if not value:
        return "^/build-konflux"
    return value


@jinja_filter
def default_label(value, component=""):
    """Return default on-label value if empty, derived from component name.

    >>> default_label("", "odh-dashboard-v3-5")
    '[kfbuild-all, kfbuild-dashboard]'
    >>> default_label("[existing]", "odh-dashboard-v3-5")
    '[existing]'
    """
    if not value:
        short = strip_version(strip_prefix(component))
        return f"[kfbuild-all, kfbuild-{short}]"
    return value


# =============================================================================
# Combined transform + validation filters
# =============================================================================


@jinja_filter
def to_private_quay(value: str) -> str:
    """Ensure quay.io/rhoai-private/ prefix. Fixes quay.io/rhoai/ → quay.io/rhoai-private/.

    >>> to_private_quay("quay.io/rhoai-private/foo")
    'quay.io/rhoai-private/foo'
    """
    expected = r"quay\.io/rhoai-private/"
    if not re.search(expected, value):
        fix_attempt = re.sub(r"quay\.io/(?:repository/)?rhoai/", "quay.io/rhoai-private/", value)
        fixed = fix_attempt if re.search(expected, fix_attempt) else None
        raise ValidationError(
            f"should use quay.io/rhoai-private/, got '{value}'",
            fixed,
        )
    return value


@jinja_filter
def to_private_gitlab(value: str) -> str:
    """Ensure gitlab.cee.redhat.com/rhoai/private/ URL. Fixes GitHub URLs.

    >>> to_private_gitlab("https://gitlab.cee.redhat.com/rhoai/private/foo")
    'https://gitlab.cee.redhat.com/rhoai/private/foo'
    """
    expected = r"gitlab\.cee\.redhat\.com/rhoai/private/"
    if not re.search(expected, value):
        fix_attempt = re.sub(r"https://github\.com/red-hat-data-services/", "https://gitlab.cee.redhat.com/rhoai/private/", value)
        fixed = fix_attempt if re.search(expected, fix_attempt) else None
        raise ValidationError(
            f"should use gitlab.cee.redhat.com/rhoai/private/, got '{value}'",
            fixed,
        )
    return value


# =============================================================================
# Validation-only filters
# =============================================================================


@jinja_filter
def must_equal(value, expected):
    """Value must equal expected.

    >>> must_equal("foo", "foo")
    'foo'
    """
    if str(value) != str(expected):
        raise ValidationError(
            f"expected '{expected}', got '{value}'",
            expected,
        )
    return value


@jinja_filter
def must_match(value, pattern):
    """Value must match regex pattern (search, not anchored).

    >>> must_match("quay.io/rhoai-private/foo", "rhoai-private")
    'quay.io/rhoai-private/foo'
    """
    if not re.search(pattern, str(value)):
        raise ValidationError(f"'{value}' does not match pattern '{pattern}'")
    return value


@jinja_filter
def must_be_version(value, expected=None):
    """Value must be a version: X.Y.Z or X.Y.Z-ea.N. If expected is given, must match exactly.

    >>> must_be_version("3.5.0")
    '3.5.0'
    >>> must_be_version("3.5.0-ea.1")
    '3.5.0-ea.1'
    """
    version_re = r"\d+\.\d+\.\d+(-ea\.\d+)?$"
    # Malformed value (e.g. "-2.25.10") — try to extract a fixable version
    if not re.match(version_re, str(value)):
        fixed = None
        if expected and re.match(version_re, str(expected)):
            fixed = str(expected)
        else:
            m = re.search(version_re, str(value))
            if m:
                fixed = m.group(0)
        raise ValidationError(
            f"'{value}' is not a valid version (expected X.Y.Z or X.Y.Z-ea.N)",
            fixed,
        )
    # Valid format but wrong version — pin to expected
    if expected and str(value) != str(expected):
        raise ValidationError(
            f"expected version '{expected}', got '{value}'",
            str(expected),
        )
    return value


@jinja_filter
def must_be_one_of(value, allowed):
    """Value must be in allowed list.

    >>> must_be_one_of("a", ["a", "b"])
    'a'
    """
    if isinstance(allowed, str):
        allowed = [allowed]
    if str(value) not in [str(a) for a in allowed]:
        raise ValidationError(f"'{value}' not in allowed list: {allowed}")
    return value


APPROVED_PIPELINE_REPOS = [
    "https://github.com/red-hat-data-services/konflux-central.git",
]


@jinja_filter
def must_be_approved_pipeline(value):
    """Pipeline repo URL must be in the approved list.

    >>> must_be_approved_pipeline("https://github.com/red-hat-data-services/konflux-central.git")
    'https://github.com/red-hat-data-services/konflux-central.git'
    """
    if str(value) not in APPROVED_PIPELINE_REPOS:
        raise ValidationError(f"'{value}' not in approved pipeline repos: {APPROVED_PIPELINE_REPOS}")
    return value


@jinja_filter
def delete_if(value, match):
    """Signal that the file should be deleted if value matches.

    >>> delete_if("rhoai-tenant", "open-data-hub-tenant")
    'rhoai-tenant'
    """
    if str(value) == str(match):
        raise DeleteFileError(f"file should be deleted (matched '{match}')")
    return value


@jinja_filter
def delete_if_contains(value, substring):
    """Signal that the file should be deleted if value contains substring.

    >>> delete_if_contains("https://github.com/red-hat-data-services/foo", "opendatahub-io")
    'https://github.com/red-hat-data-services/foo'
    """
    if substring in str(value):
        raise DeleteFileError(f"file should be deleted (contains '{substring}')")
    return value


@jinja_filter
def validate_cel(value, branch="", filename=""):
    """Validate CEL expression references the target branch and its own filename.

    >>> validate_cel('target_branch == "rhoai-3.5" && ".tekton/x.yaml".pathChanged()', "rhoai-3.5", "x.yaml")
    'target_branch == "rhoai-3.5" && ".tekton/x.yaml".pathChanged()'
    """
    v = str(value)
    normalized = " ".join(v.split())
    if branch and f'target_branch == "{branch}"' not in normalized:
        fixed = re.sub(r'target_branch(\s*==\s*)"[^"]*"', rf'target_branch\1"{branch}"', v)
        has_target_branch = fixed != v
        raise ValidationError(
            f"CEL expression does not reference branch '{branch}'",
            fixed if has_target_branch else None,
        )
    if filename and f'".tekton/{filename}".pathChanged()' not in v:
        fixed = re.sub(r'"\.tekton/[^"]+\.yaml"\.pathChanged\(\)',
                       f'".tekton/{filename}".pathChanged()', v)
        if fixed != v:
            return fixed
        raise ValidationError(
            f"CEL expression does not reference '.tekton/{filename}'.pathChanged()",
        )
    return value

@jinja_filter
def validate_cel_tekton_ignore(value):
    """Validate that the CEL expression is not using 

    >>> !".tekton/**".pathChanged()

    This CEL expression will prevent pipeline triggers if the .tekton folder is modified,
    even if other files were modified that would warrant a pipeline.

    This filter detects this issue, and replaces instances of this expression with

    >>> files.all.exists(p, !p.matches('^\\.tekton/'))
    """
    v = str(value)
    bad = '!".tekton/**".pathChanged()'
    good = 'files.all.exists(p, !p.matches(\'^\\\\.tekton/\'))'
    fixed = v.replace(bad, good)
    if fixed != v:
        raise ValidationError(
            f"CEL expression uses bad expression {bad}, need to replace with {good}",
            fixed,
        )
    return fixed

@jinja_filter
def to_stable_pr_cel(value, filename="", labels_str=""):
    """Convert a push CEL expression into a PR CEL expression for stable branch.

    Takes the parent push pipeline's on-cel-expression and produces a PR CEL
    that triggers on target_branch == "stable" with the same pathChanged
    conditions. Optionally includes a label trigger arm if labels_str is
    provided.

    The path conditions (pathChanged, files.all.exists) are extracted from the
    push CEL, with the .tekton filename updated to the PR filename.

    Args:
        value: The push pipeline's on-cel-expression string.
        filename: The PR PipelineRun filename (e.g. "odh-dashboard-pull-request.yaml").
        labels_str: The existing on-label annotation value
                    (e.g. "[kfbuild-all, kfbuild-dashboard]"). When empty or
                    not provided, only the stable+pathChanged arm is generated.

    >>> to_stable_pr_cel('event == "push" && target_branch == "rhoai-3.6" && ".tekton/x-v3-6-push.yaml".pathChanged()', "x-pull-request.yaml", "")
    'event == "pull_request"...'
    """
    import copy
    from lib.cel import (
        build_stable_pr_cel,
        decompose_push_cel,
        parse_label_list,
        path_conditions_to_text,
        replace_tekton_filename,
    )

    v = str(value)
    if not v.strip():
        raise ValidationError("push CEL expression is empty")

    try:
        parts = decompose_push_cel(v)
    except ValueError as e:
        raise ValidationError(f"cannot decompose push CEL: {e}")

    # Parse labels from on-label annotation (optional)
    labels = parse_label_list(labels_str) or None

    # Replace .tekton filename in path conditions
    path_tree = copy.deepcopy(parts["path_conditions_tree"])
    if filename:
        replace_tekton_filename(path_tree, filename)
    path_text = path_conditions_to_text(path_tree)

    # Build the CEL expression
    return build_stable_pr_cel(path_text, parts["negative_guards"], labels=labels)


@jinja_filter
def to_pr_cel(value):
    """Replace event == "push" with event == "pull_request" in a CEL expression.

    >>> to_pr_cel('event == "push" && target_branch == "rhoai-3.5"')
    'event == "pull_request" && target_branch == "rhoai-3.5"'
    """
    v = str(value)
    if 'event == "push"' not in v:
        raise ValidationError('CEL expression does not contain \'event == "push"\'')
    return v.replace('event == "push"', 'event == "pull_request"')


# =============================================================================
# Internal — filter wrapping and registration
# =============================================================================


def _wrap_filter(fn, mode: str, errors: list):
    """Wrap a filter to catch ValidationError and handle mode."""
    def wrapped(value, *args, **kwargs):
        if isinstance(value, SkipValidation):
            return value
        try:
            return fn(value, *args, **kwargs)
        except DeleteFileError as e:
            errors.append(DELETE_FILE_PREFIX + str(e))
            return value
        except ValidationError as e:
            if value == "":
                return value
            errors.append(str(e))
            if mode == "fix" and e.fixed_value is not None:
                return e.fixed_value
            return value
    wrapped.__name__ = fn.__name__
    return wrapped


def make_filters(mode: str, errors: list, component: str | None = None, env=None) -> dict:
    """Create all filters wrapped for the given mode and error list.

    Discovers functions marked with @jinja_filter and wraps them.
    """
    import inspect

    current_module = inspect.getmodule(make_filters)
    filters = {}
    for name, fn in inspect.getmembers(current_module, inspect.isfunction):
        if getattr(fn, "_is_jinja_filter", False):
            filters[name] = _wrap_filter(fn, mode, errors)

    def skip_for(value, skip_list):
        if component:
            for pattern in skip_list:
                if pattern in component:
                    if env is not None:
                        env._skipped = True
                    return SkipValidation(value)
        return value
    filters["skip_for"] = skip_for

    return filters
