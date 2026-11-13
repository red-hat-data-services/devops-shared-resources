"""CEL parsing, condition extraction, and AST utilities for Tekton filters."""

import ast as python_ast
import re

import celpy
from celpy.celparser import DumpAST
from lark import Token, Tree


class _CELDumpAST(DumpAST):
    """Avoid celpy's extra separator after comparison operators."""

    def relation(self, tree):
        if len(tree.children) == 1:
            return
        right = self.stack.pop()
        left = self.stack.pop()
        # relation_eq and its siblings already append the operator's space.
        # Join AST fragments, not source text: literal whitespace stays intact.
        self.stack.append(f"{left}{right}")


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


def ensure_cel_selectors(expression, desired):
    """Reconcile named string selectors; None requests their absence.

    Only standalone equality conjuncts can be changed. A valid, compliant
    expression is returned verbatim. On rebuild, retained predicates reuse
    source layout; selector order and inter-conjunct separators are normalized.
    Standalone negations retain the legacy `! ` spelling for template callers.
    Unknown predicate shapes are never weakened. Callers supply policy; this
    function knows no event or cluster.
    """
    import json

    if not isinstance(desired, dict) or any(
        not isinstance(name, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", name)
        or (value is not None and not isinstance(value, str))
        for name, value in desired.items()
    ):
        raise ValueError("selectors must map simple CEL identifiers to strings or absence")
    try:
        tree = celpy.Environment().compile(expression)
    except Exception as exc:
        raise ValueError(f"failed to parse CEL expression: {exc}") from exc

    def unwrap(node):
        while isinstance(node, Tree) and len(node.children) == 1 and isinstance(node.children[0], Tree):
            node = node.children[0]
        return node

    def conjuncts(node):
        original = node
        node = unwrap(node)
        if isinstance(node, Tree) and node.data == "conditionaland":
            if grouped(original) and not any(
                str(ident.children[0]) in desired for ident in original.find_data("ident")
            ):
                return [original]
            return [part for child in node.children for part in conjuncts(child)]
        return [original]

    def grouped(node):
        while isinstance(node, Tree) and len(node.children) == 1 and isinstance(node.children[0], Tree):
            if node.data == "paren_expr":
                return True
            node = node.children[0]
        return False

    kept, found = [], {}
    for clause in conjuncts(tree):
        references = [ident for ident in clause.find_data("ident") if str(ident.children[0]) in desired]
        if not references:
            node = unwrap(clause)
            # Parser spans include grouping punctuation, unlike token bounds.
            # Reuse source text so retained predicates keep their layout,
            # literal quoting, and spacing instead of being pretty-printed.
            text = expression[clause.meta.start_pos:clause.meta.end_pos]
            if node.data == "unary" and node.children[0].data == "unary_not":
                # Existing notebook templates match standalone `! (...)` guards.
                # Keep that operator spelling without reprinting its operand.
                operator_end = node.meta.start_pos + 1
                operand_start = node.children[1].meta.start_pos
                gap = expression[operator_end:operand_start]
                if not gap.strip():
                    start = operator_end - clause.meta.start_pos
                    end = operand_start - clause.meta.start_pos
                    text = text[:start] + " " + text[end:]
            if node.data == "conditionalor" and len(node.children) > 1 and not grouped(clause):
                text = f"({text})"
            kept.append(text)
            continue
        node = unwrap(clause)
        if node.data == "relation" and len(node.children) == 2 and node.children[0].data == "relation_eq":
            left, right = unwrap(node.children[0]), unwrap(node.children[1])
            if right.data == "ident":
                left, right = right, left
            if (left.data == "ident" and str(left.children[0]) in desired
                    and right.data == "literal" and len(references) == 1):
                literal = str(right.children[0])
                if literal.startswith(('"', "'")):
                    found.setdefault(str(left.children[0]), []).append(python_ast.literal_eval(literal))
                    continue
        raise ValueError("selected identifiers must be standalone string equalities")
    for name, values in found.items():
        if len(set(values)) != 1:
            raise ValueError(f"conflicting values for selector {name!r}")
    if all(found.get(name, []) == ([] if value is None else [value]) for name, value in desired.items()):
        return expression
    selectors = [f"{name} == {json.dumps(value)}" for name, value in desired.items() if value is not None]
    result = "\n&& ".join(selectors + kept) or "true"
    # Preserve the scalar's final newline(s), including YAML chomping behavior.
    return result + expression[tree.meta.end_pos:]


def ensure_cel_tekton_filename(expression, filename):
    """Reconcile pathChanged YAML receivers to the desired filename.

    Compliant references and all unrelated expression text stay byte-identical.
    Template comparison supplies validation/autofix against the child value.
    """
    import json

    if not isinstance(filename, str) or not re.fullmatch(r"[^/\\\\]+\.ya?ml", filename):
        raise ValueError("filename must be a YAML basename")
    try:
        tree = celpy.Environment().compile(expression)
    except Exception as exc:
        raise ValueError(f"failed to parse CEL expression: {exc}") from exc
    replacements = []
    for call in tree.find_data("member_dot_arg"):
        if len(call.children) != 2 or str(call.children[1]) != "pathChanged":
            continue
        receiver = call.children[0]
        while isinstance(receiver, Tree) and len(receiver.children) == 1 and isinstance(receiver.children[0], Tree):
            receiver = receiver.children[0]
        if not isinstance(receiver, Tree) or receiver.data != "literal":
            continue
        token = receiver.children[0]
        literal = str(token)
        if not literal.startswith(('"', "'")):
            continue
        path = python_ast.literal_eval(literal)
        if (isinstance(path, str) and path.startswith(".tekton/")
                and path.endswith((".yaml", ".yml")) and path != f".tekton/{filename}"):
            replacements.append((token.start_pos, token.end_pos, json.dumps(f".tekton/{filename}")))
    for start, end, replacement in sorted(replacements, reverse=True):
        expression = expression[:start] + replacement + expression[end:]
    return expression


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

    Multiple path clauses are retained as a conjunction; individual clauses
    may contain parenthesized ORs. A top-level OR cannot be safely split:

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
            path_tree = clause if path_tree is None else Tree("conditionaland", [path_tree, clause])
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
    return _CELDumpAST.display(wrapped)
