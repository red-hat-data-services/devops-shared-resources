"""CEL parsing, condition extraction, and AST utilities for Tekton filters."""

import celpy
from celpy.celparser import tree_dump
from lark import Token, Tree


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


def extract_push_conditions(expression):
    """Return negative guards and the path subtree, dropping push selectors.

    Returns ``(guards, path_tree)``: guards are CEL strings, while path_tree is
    a Lark subtree (or None). Use path_conditions_to_text to render the subtree.
    Event and target_branch selectors are not included in either result.

    A trigger without guards or paths has nothing left after selector removal:

    >>> extract_push_conditions('event == "push" && target_branch == "rhoai-3.6"')
    ([], None)

    Negative guards stay separate from the parenthesized path expression:

    >>> guards, paths = extract_push_conditions(
    ...     'event == "push" && target_branch == "rhoai-3.6"'
    ...     ' && !skip_build'
    ...     ' && ("src/**".pathChanged() || ".tekton/push.yaml".pathChanged())'
    ... )
    >>> guards
    ['! skip_build']
    >>> path_conditions_to_text(paths)
    '("src/**".pathChanged() || ".tekton/push.yaml".pathChanged())'

    The supported shape is a top-level conjunction with a single path clause,
    which may itself contain ORs. A top-level OR cannot be safely split:

    >>> extract_push_conditions('event == "push" || skip_build')
    Traceback (most recent call last):
        ...
    ValueError: top-level OR expressions cannot be decomposed safely
    """
    try:
        ast = celpy.Environment().compile(expression)
    except Exception as e:
        raise ValueError(f"failed to parse CEL expression: {e}") from e

    if not isinstance(ast, Tree) or ast.data != "expr":
        raise ValueError(f"unexpected CEL AST root: {ast.data if isinstance(ast, Tree) else type(ast)}")
    top_level_or = ast.children[0]
    if not isinstance(top_level_or, Tree) or top_level_or.data != "conditionalor":
        raise ValueError(f"expected conditionalor, got {top_level_or.data}")
    if len(top_level_or.children) != 1:
        raise ValueError("top-level OR expressions cannot be decomposed safely")

    has_event = False
    guards = []
    path_tree = None
    for clause in flatten_and(top_level_or.children[0]):
        text = path_conditions_to_text(clause)
        if text.startswith("event"):
            has_event = True
        elif text.startswith("target_branch"):
            continue
        elif text.startswith("!"):
            guards.append(text)
        else:
            path_tree = clause
    if not has_event:
        raise ValueError("CEL expression has no event clause")
    return guards, path_tree


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
