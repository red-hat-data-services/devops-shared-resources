"""Localized YAML emission for existing-file reconciliation, not general formatting."""
from io import StringIO

from ruamel.yaml import YAML
from ruamel.yaml.nodes import MappingNode, SequenceNode


def localized_yaml(source, candidate):
    """Splice changed values/fields into source; verify the entire semantic result.

    Untouched spans are copied verbatim. Flow collections and changed list shapes
    may require emission of the affected collection, never a whole-file fallback.
    Anchors are rejected: alias nodes do not have independent source locations.
    """
    if isinstance(source, bytes):
        source = source.decode("utf-8")
    parser = YAML()
    parser.preserve_quotes = True
    before = parser.load(source)
    tree = parser.compose(source)
    edits = []
    newline = "\r\n" if "\r\n" in source else "\n"

    def check_nodes(node):
        if node.anchor:
            raise ValueError("localized YAML editing does not support anchors/aliases")
        children = [child for pair in node.value for child in pair] if isinstance(node, MappingNode) else node.value if isinstance(node, SequenceNode) else []
        for child in children:
            check_nodes(child)
    check_nodes(tree)

    def line_start(index):
        return source.rfind("\n", 0, index) + 1

    def content_end(node):
        if isinstance(node, MappingNode) and node.value and not node.flow_style:
            return content_end(node.value[-1][1])
        if isinstance(node, SequenceNode) and node.value and not node.flow_style:
            return content_end(node.value[-1])
        return node.end_mark.index

    def line_end(index):
        if index and source[index - 1:index] == "\n":
            return index
        end = source.find("\n", index)
        return len(source) if end < 0 else end + 1

    def emit(value):
        yaml = YAML()
        yaml.preserve_quotes, yaml.width = True, 4096
        stream = StringIO()
        yaml.dump(value, stream)
        return stream.getvalue()

    def insert(node, value, indent):
        position = line_start(node.end_mark.index)
        if node.end_mark.index == len(source):
            position = len(source)
        text = "".join(" " * indent + line if line.strip() else line for line in emit(value).splitlines(keepends=True))
        if position and source[position - 1] != "\n":
            text = "\n" + text
        if position == len(source) and not source.endswith("\n"):
            text = text.rstrip("\n")
        edits.append((position, position, text.replace("\n", newline)))

    def remove(node, start):
        edits.append((line_start(start), line_end(content_end(node)), ""))

    def replace(node, value, root, scalar_indent=None):
        if root:
            raise ValueError("localized YAML edit would require whole-document emission")
        if ((isinstance(node, MappingNode) != isinstance(value, dict))
                or (isinstance(node, SequenceNode) != isinstance(value, list))):
            raise ValueError("localized YAML replacement cannot change collection shape")
        if isinstance(node, (MappingNode, SequenceNode)) and node.flow_style:
            if not hasattr(value, "fa") or value.fa.flow_style() is not True:
                raise ValueError("localized YAML flow replacement requires preserved collection style")
        text = emit(value)
        if not isinstance(node, (MappingNode, SequenceNode)):
            emitted = YAML().compose(text)
            text = text[emitted.start_mark.index:emitted.end_mark.index]
            end = node.end_mark.index
            if source[end - 1:end] == "\n" and not text.endswith("\n"):
                text += "\n"
            elif emitted.style in ("|", ">") and text.endswith("\n") and source[end:].startswith(newline):
                # Inline scalars exclude their line break from the node span;
                # reuse that existing break instead of inserting a blank line.
                text = text[:-1]
        else:
            end = content_end(node)
            if not node.flow_style:
                end = line_end(end)
                text = text.rstrip("\n") + ("\n" if source[end - 1:end] == "\n" else "")
            else:
                emitted = YAML().compose(text)
                text = text[emitted.start_mark.index:emitted.end_mark.index]
            if isinstance(node, SequenceNode) and not node.flow_style and not value:
                text = "  " + text
        parts = text.splitlines(keepends=True)
        # Block bodies belong below the key/dash, not below the value marker.
        # Root-scalar emission normally has zero body indent, but may declare
        # an explicit indicator (e.g. |2-) for leading spaces or blank lines.
        indent = node.start_mark.column
        if (scalar_indent is not None and not isinstance(node, (MappingNode, SequenceNode))
                and emitted.style in ("|", ">")):
            emitted_indent = next((int(char) for char in parts[0] if char in "123456789"), 0)
            indent = scalar_indent + 2 - emitted_indent
        text = parts[0] + "".join(" " * indent + line for line in parts[1:])
        edits.append((node.start_mark.index, end, text.replace("\n", newline)))

    def walk(node, old, new, root=False, scalar_indent=None):
        if old == new:
            return
        if isinstance(node, (MappingNode, SequenceNode)) and isinstance(new, (dict, list)) and not new:
            replace(node, new, root)
            return
        if isinstance(node, MappingNode) and isinstance(old, dict) and isinstance(new, dict) and not node.flow_style:
            pairs = {key.value: (key, value) for key, value in node.value}
            # Removing the first key after a sequence dash needs collection emission.
            if any(source[line_start(pairs[key][0].start_mark.index):pairs[key][0].start_mark.index].strip()
                   for key in old if key not in new):
                replace(node, new, root)
                return
            for key, value in old.items():
                key_node, value_node = pairs[key]
                if key not in new:
                    remove(value_node, key_node.start_mark.index)
                else:
                    walk(value_node, value, new[key], scalar_indent=key_node.start_mark.column)
            added = {key: value for key, value in new.items() if key not in old}
            if added:
                insert(node, added, node.start_mark.column)
        elif isinstance(node, SequenceNode) and isinstance(old, list) and isinstance(new, list) and not node.flow_style:
            identity = next((key for key in ("pipelineTaskName", "name")
                             if old and all(isinstance(item, dict) and key in item for item in [*old, *new])), None)
            if identity:
                old_ids, new_ids = [item[identity] for item in old], [item[identity] for item in new]
                retained = [name for name in old_ids if name in new_ids]
                if len(set(old_ids)) != len(old_ids) or len(set(new_ids)) != len(new_ids) or new_ids[:len(retained)] != retained:
                    replace(node, new, root)
                    return
                by_id = {item[identity]: item for item in new}
                for child, item in zip(node.value, old):
                    if item[identity] in by_id:
                        walk(child, item, by_id[item[identity]], scalar_indent=node.start_mark.column)
                    else:
                        remove(child, child.start_mark.index)
                tail = new[len(retained):]
            else:
                for index in range(min(len(old), len(new))):
                    walk(node.value[index], old[index], new[index], scalar_indent=node.start_mark.column)
                for child in node.value[len(new):]:
                    remove(child, child.start_mark.index)
                tail = new[len(old):]
            if tail:
                insert(node, tail, node.start_mark.column)
        else:
            replace(node, new, root, scalar_indent)

    walk(tree, before, candidate, root=True)
    edits.sort(key=lambda edit: (edit[0], edit[1]))
    previous_end = 0
    for start, end, _ in edits:
        if not 0 <= previous_end <= start <= end <= len(source):
            raise ValueError("overlapping or invalid localized YAML edits")
        previous_end = end
    result = source
    for start, end, text in reversed(edits):
        result = result[:start] + text + result[end:]
    if YAML(typ="safe", pure=True).load(result) != YAML(typ="safe", pure=True).load(emit(candidate)):
        raise ValueError("localized YAML result differs from candidate")
    return result
