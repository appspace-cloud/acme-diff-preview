"""YAML slips in a config value file (COPS-2766). Pure: no I/O.

Helm and ArgoCD read only the first YAML document of a file, so only that one
is checked. Three slips change config and no diff line shows them clearly:
a duplicate key (only the last copy is kept), a bare `key:` (null, so Helm
deletes that chart default) and a `microservices.definitions` that is not a
non-empty map (it deletes every image name the chart ships).
"""
import collections

import yaml

_LOADER = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
_MERGE = yaml.constructor.SafeConstructor()
_NULL = "tag:yaml.org,2002:null"
DEFINITIONS = "appspace.microservices.definitions"


def _get(node, key):
    """(key node, value node) of `key` in a mapping node as a loader keeps it:
    `<<` merge keys resolved, the last copy wins. (None, None) when absent.
    It resolves the merges in place, so call it after the walk."""
    found = (None, None)
    if isinstance(node, yaml.MappingNode):
        _MERGE.flatten_mapping(node)
        for k, v in node.value:
            if isinstance(k, yaml.ScalarNode) and k.value == key:
                found = (k, v)
    return found


def wipes_definitions_node(root):
    """The key node of appspace.microservices.definitions when it is present
    and is not a non-empty map (null, {}, [], "" or any scalar), else None."""
    ms = _get(_get(root, "appspace")[1], "microservices")[1]
    k, v = _get(ms, "definitions")
    return k if k is not None and not (isinstance(v, yaml.MappingNode) and v.value) else None


def slips(body):
    """The slips of the first document of `body`, or None when it does not parse:
    {"dup": Counter, "null": Counter, "wipe": bool, "lines": {(kind, key): [(line, first_line)]}}.

    A key is a dotted path and a list index is `[]`, so moving list items does
    not make an old slip look new. The walk skips `<<` merge keys, and a node
    that an alias reaches twice is walked once. The wipe resolves them, as a
    loader does. An explicit `null` or `~` is not a bare key: it removes a
    chart default on purpose.
    """
    try:
        root = next(yaml.compose_all(body or "", Loader=_LOADER), None)
    except yaml.YAMLError:
        return None
    out = {"dup": collections.Counter(), "null": collections.Counter(), "wipe": False, "lines": {}}

    def add(kind, key, line, first=None):
        out[kind][key] += 1
        out["lines"].setdefault((kind, key), []).append((line, first))

    # A stack, not recursion: a deep file cannot hit the recursion limit. The
    # children go on reversed, so the walk is in document order.
    seen, todo = set(), [(root, "")]
    while todo:
        node, path = todo.pop()
        if id(node) in seen:
            continue
        seen.add(id(node))
        kids = []
        if isinstance(node, yaml.SequenceNode):
            kids = [(n, path + "[]") for n in node.value]
        elif isinstance(node, yaml.MappingNode):
            first = {}
            for k, v in node.value:
                key = k.value if isinstance(k, yaml.ScalarNode) else "?"
                if key == "<<":
                    continue
                p = f"{path}.{key}" if path else key
                line = k.start_mark.line + 1
                if key in first:
                    add("dup", p, line, first[key])
                first.setdefault(key, line)
                if isinstance(v, yaml.ScalarNode) and v.tag == _NULL and v.value == "":
                    add("null", p, line)
                kids.append((v, p))
        todo += reversed(kids)
    try:
        wiped = wipes_definitions_node(root)
    except yaml.YAMLError:
        return None     # a `<<` that is not a map: no loader reads the file
    if wiped is not None:
        # A bare `definitions:` is the wipe, reported once.
        out["null"].pop(DEFINITIONS, None)
        out["wipe"] = True
        out["lines"][("wipe", DEFINITIONS)] = [(wiped.start_mark.line + 1, None)]
    return out


def scalar_text(body, *keys):
    """The text of the plain scalar at `keys` in the first document of `body`,
    as the author wrote it (`2604.10`, where a loader gives 2604.1), or None."""
    try:
        node = next(yaml.compose_all(body or "", Loader=_LOADER), None)
        for key in keys:
            node = _get(node, key)[1]
    except yaml.YAMLError:
        return None
    return node.value if isinstance(node, yaml.ScalarNode) else None
