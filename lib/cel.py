"""CEL expression parsing, decomposition, and transformation for Tekton PipelineRuns.

Uses cel-python (cloud-custodian) to parse CEL expressions into Lark ASTs,
decompose them into structural components, and reassemble them into new forms.

The primary use case is transforming push pipeline CEL triggers into
consolidated pull-request CEL triggers with label and stable-branch arms.
"""

import copy
import re

import celpy
from celpy.celparser import tree_dump
from lark import Token, Tree


def _cel_env():
    return celpy.Environment()


def flatten_and(node):
    """Collect all operands from nested conditionaland nodes.

    CEL's && is left-associative, so ``a && b && c`` parses as::

        conditionaland
          conditionaland
            a
            b
          c

    This function flattens that into ``[a, b, c]``.
    """
    if isinstance(node, Tree) and node.data == "conditionaland":
        results = []
        for child in node.children:
            results.extend(flatten_and(child))
        return results
    return [node]


def classify_clause(clause_text):
    """Classify a single top-level && clause by its content.

    Returns one of:
        "event"    — e.g. ``event == "push"``
        "branch"   — e.g. ``target_branch == "rhoai-3.6"``
        "negative" — e.g. ``!("manifests/...".pathChanged())``
        "paths"    — everything else (path conditions, files.all.exists, etc.)
    """
    stripped = clause_text.strip()
    if stripped.startswith("event"):
        return "event"
    if stripped.startswith("target_branch"):
        return "branch"
    # Negative guards: !(...) or !files.all.all(...)
    if stripped.startswith("!") or stripped.startswith("! "):
        return "negative"
    return "paths"


def decompose_push_cel(cel_expr):
    """Parse a push CEL expression and decompose into structural parts.

    Args:
        cel_expr: A push pipeline's on-cel-expression string.

    Returns:
        dict with keys:
            event: str — the event clause text (e.g. ``event ==  "push"``)
            branch: str — the branch clause text
            negative_guards: list[str] — negative guard clause texts
            path_conditions: str — the path condition clause text
            path_conditions_tree: lark.Tree — the AST subtree for path conditions

    Raises:
        ValueError: If the CEL cannot be parsed or has unexpected structure.
    """
    env = _cel_env()
    try:
        ast = env.compile(cel_expr)
    except Exception as e:
        raise ValueError(f"failed to parse CEL expression: {e}") from e

    # Structure: expr -> conditionalor -> conditionaland (nested)
    if not isinstance(ast, Tree) or ast.data != "expr":
        raise ValueError(f"unexpected CEL AST root: {ast.data if isinstance(ast, Tree) else type(ast)}")

    cor = ast.children[0]
    if not isinstance(cor, Tree) or cor.data != "conditionalor":
        raise ValueError(f"expected conditionalor, got {cor.data}")

    cand = cor.children[0]
    clauses = flatten_and(cand)

    result = {
        "event": None,
        "branch": None,
        "negative_guards": [],
        "path_conditions": None,
        "path_conditions_tree": None,
    }

    for clause in clauses:
        # Wrap clause in full expr structure for tree_dump
        wrapped = Tree("expr", [Tree("conditionalor", [Tree("conditionaland", [clause])])])
        text = tree_dump(wrapped)

        kind = classify_clause(text)
        if kind == "event":
            result["event"] = text
        elif kind == "branch":
            result["branch"] = text
        elif kind == "negative":
            result["negative_guards"].append(text)
        else:
            result["path_conditions"] = text
            result["path_conditions_tree"] = copy.deepcopy(clause)

    if result["event"] is None:
        raise ValueError("CEL expression has no event clause")
    if result["path_conditions"] is None:
        raise ValueError("CEL expression has no path conditions")

    return result


def replace_tekton_filename(tree, new_filename):
    """Walk a Lark AST and replace .tekton/*.yaml string literals.

    Modifies the tree in place. Only replaces string literals that look like
    ``.tekton/<something>.yaml`` (used in pathChanged() calls).

    Args:
        tree: A Lark Tree (modified in place).
        new_filename: The new filename (without .tekton/ prefix),
                      e.g. ``"odh-dashboard-pull-request.yaml"``.
    """
    if isinstance(tree, Tree):
        if tree.data == "literal":
            for i, tok in enumerate(tree.children):
                if isinstance(tok, Token):
                    val = str(tok)
                    if val.startswith('"') and val.endswith('"'):
                        inner = val[1:-1]
                        if inner.startswith(".tekton/") and inner.endswith(".yaml"):
                            tree.children[i] = Token(tok.type, f'".tekton/{new_filename}"')
        for child in tree.children:
            if isinstance(child, Tree):
                replace_tekton_filename(child, new_filename)


def path_conditions_to_text(tree):
    """Convert a path conditions AST subtree back to CEL text.

    Args:
        tree: A Lark Tree representing the path conditions clause.

    Returns:
        str: The CEL text for the path conditions.
    """
    wrapped = Tree("expr", [Tree("conditionalor", [Tree("conditionaland", [tree])])])
    return tree_dump(wrapped)


def parse_label_list(labels_str):
    """Parse a PaC on-label annotation value into a list of label names.

    Args:
        labels_str: e.g. ``"[kfbuild-all, kfbuild-dashboard]"``

    Returns:
        list[str]: e.g. ``["kfbuild-all", "kfbuild-dashboard"]``
    """
    if not labels_str:
        return []
    # Strip brackets and split on commas
    inner = labels_str.strip().strip("[]")
    if not inner:
        return []
    return [label.strip() for label in inner.split(",") if label.strip()]


def build_stable_pr_cel(path_conditions_text, negative_guards=None, labels=None,
                        target_branch=None):
    """Build a PR CEL expression with pathChanged conditions, optionally scoped to a branch.

    Args:
        path_conditions_text: str — CEL text for path conditions (from push pipeline).
        negative_guards: list[str] | None — negative guard CEL clauses.
        labels: list[str] | None — label names for an optional label trigger arm.
            When provided, the CEL has two arms joined by ||: labels OR paths.
        target_branch: str | None — when set, adds a target_branch == "..." check
            to the path conditions arm.

    Returns:
        str: The complete PR CEL expression, formatted for readability.
    """
    # Build the path arm
    path_parts = []
    if target_branch:
        path_parts.append(f'target_branch == "{target_branch}"')
    if negative_guards:
        path_parts.extend(negative_guards)
    path_parts.append(path_conditions_text)
    path_arm = "\n&& ".join(path_parts)

    if labels:
        # Two-arm: labels || paths
        labels_quoted = ", ".join(f'"{l}"' for l in labels)
        label_arm = (
            "has(body.pull_request.labels)\n"
            f"    && body.pull_request.labels.exists(l, l.name in [{labels_quoted}])"
        )
        if target_branch:
            path_arm_indented = "\n    && ".join(path_parts)
        else:
            path_arm_indented = "\n    && ".join(path_parts)
        cel = (
            'event == "pull_request"\n'
            "&& (\n"
            f"  (\n    {label_arm}\n  )\n"
            "  ||\n"
            f"  (\n    {path_arm_indented}\n  )\n"
            ")"
        )
    else:
        # Single arm: paths only
        cel = (
            'event == "pull_request"\n'
            f"&& {path_arm}"
        )
    return cel
