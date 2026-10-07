"""
payload_parameterizer.py
------------------------
Back end for the Performance Test page's "Configure Payload" dialog.

Four jobs:

  1. `parse_payload()` reads an uploaded CSV / JSON / XML file and reports the
     nodes a tester can map onto - column headings, JSON keys, XML child tags.
     Parsing deliberately mirrors `core/payload_loader.PayloadLoader` (the
     framework class the generated script uses at runtime) so the nodes offered
     in the dialog are exactly the keys that will exist during the run.

  2. `extract_parameters()` scans a Locust script for the fields a payload can
     drive: request body keys (`json=` / `data=` dict literals), `params=`
     entries, the query string of the request URL itself, and - in a
     Functional (Selenium) journey - the values typed or selected by
     `ui.type(...)` / `ui.select(...)`.
     `extract_requests()` scans the same calls for the requests themselves, which
     is what the response-time threshold dropdown is populated from.

  3. `build_parameterized_script()` rewrites the script so every mapped field
     reads from the payload at runtime instead of the recorded literal.

  4. The same rewrite injects the response-time thresholds (the NFR limit per
     request, with a `General` row covering everything else) so a response
     slower than its limit is reported to Locust as a failure.

The rewrite never touches the recorded script. It produces a sibling
`_bigqa_param_<stem>.py` in the script's own folder - the leading underscore keeps it
out of the Performance Test grid (`performance_runner.list_scripts` skips
private modules) - and the runner executes that copy instead. The original stays
the reviewable artefact, and regenerating after an edit is always safe because
mappings are keyed by *what* the parameter is (request + field path), never by
line number.

Parameter identity
------------------
A parameter id looks like `body::POST /api/orders::customer_id`:

    <source>::<method> <request name>::<field path>

Two occurrences that share an id are the same logical field (the same key of
the same call, e.g. inside a loop), so mapping it once parameterises both.
"""

import ast
import codecs
import csv
import io
import json
import os
import re
import shutil
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qsl, quote_plus, urlsplit

from utils import perf_project_layout as layout

DATA_DIRNAME = "data"
CORE_DIRNAME = "core"
GENERATED_PREFIX = "_bigqa_param_"
PAYLOAD_COPY_PREFIX = "bigqa_"

PAYLOAD_TYPES = ("csv", "json", "xml")
PAYLOAD_EXTENSIONS = {"csv": ".csv", "json": ".json", "xml": ".xml"}

# An uploaded payload is held in memory while the dialog is open, so it is
# capped well below "a database dump someone dragged in by mistake".
MAX_PAYLOAD_BYTES = 5 * 1024 * 1024
# Records inspected when collecting the node list. A payload with thousands of
# rows describes its shape in the first few.
NODE_SCAN_RECORDS = 50
# Depth guard for nested JSON and for dict literals inside a script.
MAX_NODE_DEPTH = 6
# Only the first few list entries become addressable nodes; a 500-item array
# would otherwise flood the dropdown.
MAX_LIST_NODES = 5

# Names used by the generated preamble.
ROW_VAR = "_bigqa_row"
LOADER_VAR = "_bigqa_payload"
VALUE_FUNC = "_bigqa_value"
PAYLOAD_FILE_CONST = "_BIGQA_PAYLOAD_FILE"

# The threshold row that covers every request without one of its own.
GENERAL_THRESHOLD = "__general__"
GENERAL_THRESHOLD_LABEL = "General"
# A threshold is a wall-clock NFR, so it is capped at something a tester could
# plausibly mean rather than left open to a typo'd 99999.
MAX_THRESHOLD_SECONDS = 3600

# Locust's client methods, plus the generic `request(method, url, ...)` form.
CLIENT_METHODS = {"get", "post", "put", "patch", "delete", "head", "options", "request"}
# Call keyword -> the parameter "source" it produces.
BODY_KEYWORDS = {"json": "body", "data": "body", "params": "query"}

SOURCE_LABELS = {"body": "request body", "query": "query parameter", "input": "typed value"}
# Functional journeys: `ui.type(by, locator, text)` / `ui.select(by, locator, option)`.
UI_VALUE_METHODS = {"type": 2, "select": 2}
UI_METHOD = "UI"
# Folders a stored payload copy may be deleted from.
_DATA_FOLDER_NAMES = {"data", "payloads", "test_data", "testdata", "csv", "json", "xml"}

_UNSAFE_FILENAME = re.compile(r"[^A-Za-z0-9._-]+")
_SAMPLE_MAX_CHARS = 60
# `LOADER_VERSION = <n>` as declared at the top of core/payload_loader.py.
_LOADER_VERSION_RE = re.compile(r"^LOADER_VERSION\s*=\s*(\d+)", re.MULTILINE)


class PayloadError(ValueError):
    """A payload the user needs to fix: wrong format, malformed, or empty."""


# ─────────────────────────────────────────────────────────────────────────────
# Payload parsing
# ─────────────────────────────────────────────────────────────────────────────

def normalize_payload_type(value):
    payload_type = (value or "").strip().lower()
    if payload_type not in PAYLOAD_TYPES:
        raise PayloadError("Choose a payload type of XML, JSON or CSV.")
    return payload_type


def _decode(raw):
    if isinstance(raw, str):
        return raw
    if len(raw) > MAX_PAYLOAD_BYTES:
        limit_mb = MAX_PAYLOAD_BYTES // (1024 * 1024)
        raise PayloadError(f"The payload file is larger than {limit_mb} MB.")
    try:
        # utf-8-sig strips the BOM Excel writes into exported CSVs, which would
        # otherwise become part of the first column heading.
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise PayloadError("The payload file is not valid UTF-8 text.")


def _reject_wrong_format(text, payload_type):
    """
    Catch the common mis-selection (a JSON file uploaded as CSV, say) before the
    format-specific parser fails with a message that reads like a stack trace.
    """
    head = text.lstrip()[:1]
    looks_like = {"{": "JSON", "[": "JSON", "<": "XML"}.get(head, "")
    if looks_like and looks_like.lower() != payload_type:
        raise PayloadError(
            f"This file looks like {looks_like}, but the payload type is set to "
            f"{payload_type.upper()}. Change the payload type or choose another file."
        )


def _flatten_record(node, prefix, out, depth=0):
    """Collect the dotted paths of every scalar leaf in a parsed record."""
    if depth > MAX_NODE_DEPTH:
        return
    if isinstance(node, dict):
        for key, value in node.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            _flatten_record(value, path, out, depth + 1)
    elif isinstance(node, list):
        for index, value in enumerate(node[:MAX_LIST_NODES]):
            path = f"{prefix}.{index}" if prefix else str(index)
            _flatten_record(value, path, out, depth + 1)
    elif prefix:
        out.setdefault(prefix, node)


def _nodes_and_sample(records):
    """Return (ordered node paths, first-record sample values keyed by path)."""
    nodes = {}
    for record in records[:NODE_SCAN_RECORDS]:
        _flatten_record(record, "", nodes)
    sample = {}
    if records:
        _flatten_record(records[0], "", sample)
    return list(nodes.keys()), {key: _sample_text(value) for key, value in sample.items()}


def _sample_text(value):
    text = "" if value is None else str(value)
    text = " ".join(text.split())
    return text if len(text) <= _SAMPLE_MAX_CHARS else text[:_SAMPLE_MAX_CHARS - 1] + "…"


def _parse_csv(text):
    if not text.strip():
        raise PayloadError("The CSV file is empty.")
    reader = csv.DictReader(io.StringIO(text))
    headers = [name.strip() for name in (reader.fieldnames or []) if name and name.strip()]
    if not headers:
        raise PayloadError("The CSV file has no column headings on its first row.")
    if len(headers) != len(set(headers)):
        duplicates = sorted({name for name in headers if headers.count(name) > 1})
        raise PayloadError(f"The CSV file has duplicate column headings: {', '.join(duplicates)}.")
    rows = [{key: value for key, value in row.items() if key} for row in reader]
    if not rows:
        raise PayloadError("The CSV file has column headings but no data rows.")
    return headers, rows


def json_record_list_keys(data):
    """
    The keys of a JSON object whose values are lists of objects.

    A payload that wraps its records under a name of its own -
    `{"users": [{...}, {...}]}` - is the common export shape, so the wrapped
    list is the record list. Without this the object is read as a single
    record and every element of the list becomes an addressable node of its
    own (`users.0.username`, `users.1.username`, ...) instead of the fields
    those records share.
    """
    return [key for key, value in data.items()
            if isinstance(value, list) and value
            and all(isinstance(item, dict) for item in value)]


def _parse_json(text):
    try:
        data = json.loads(text)
    except ValueError as error:
        raise PayloadError(f"The JSON file could not be parsed: {error}")

    # Mirrors PayloadLoader._load_json: a list of records, {"records": [...]},
    # a single wrapped list of objects, or a lone object treated as one record.
    # A "records" key that is not a list is rejected rather than reinterpreted,
    # because the runtime loader would hand that value straight to
    # itertools.cycle.
    if isinstance(data, list):
        records = data
    elif isinstance(data, dict) and "records" in data:
        if not isinstance(data["records"], list):
            raise PayloadError('The "records" key of the JSON payload must hold a list of objects.')
        records = data["records"]
    elif isinstance(data, dict):
        wrappers = json_record_list_keys(data)
        if len(wrappers) > 1:
            # Guessing here would silently test the wrong data, and the runtime
            # loader has to make the same choice from the same file.
            raise PayloadError(
                "The JSON payload holds more than one list of objects "
                f"({', '.join(wrappers[:4])}). Keep the records to test in the file, "
                'or move them under a "records" key.'
            )
        records = data[wrappers[0]] if wrappers else [data]
    else:
        raise PayloadError("The JSON payload must be an object or a list of objects.")

    records = [record for record in records if isinstance(record, dict)]
    if not records:
        raise PayloadError("The JSON payload contains no objects to use as records.")
    return records


def _xml_record_tag(root):
    """
    The tag of the element that holds one record.

    The record is the repeating element, which is not always a direct child of
    the root: `<root><users><user/><user/></users></root>` keeps its records in
    `<user>`, not in the `<users>` wrapper. Taking the wrapper instead would
    offer the record tag itself as the only mappable node. PayloadLoader looks
    records up with `.//<tag>`, so how deep the tag sits does not matter to the
    generated script.

    Returns "" when no element repeats and nothing looks like a record.
    """
    # Breadth-first, so the shallowest repeating tag wins over a field name that
    # happens to repeat inside one record.
    queue = [(root, 0)]
    # Elements whose children are all leaves: the shape of a record in a file
    # that happens to carry only one.
    single = ""
    while queue:
        element, depth = queue.pop(0)
        children = list(element)
        if not children or depth > MAX_NODE_DEPTH:
            continue
        tag, count = Counter(child.tag for child in children).most_common(1)[0]
        if count > 1:
            return tag
        if not single and element is not root and all(len(child) == 0 for child in children):
            single = element.tag
        queue.extend((child, depth + 1) for child in children)
    return single


def _parse_xml(text):
    try:
        root = ET.fromstring(text)
    except ET.ParseError as error:
        raise PayloadError(f"The XML file could not be parsed: {error}")

    if not len(root):
        raise PayloadError(
            f"<{root.tag}> has no child elements, so the XML file holds no records."
        )

    record_tag = _xml_record_tag(root)
    if not record_tag:
        raise PayloadError(
            f"No repeating record element was found under <{root.tag}>. Wrap each "
            "record in its own element, e.g. <records><record>…</record></records>."
        )
    records = []
    for element in root.findall(f".//{record_tag}"):
        record = {child.tag: child.text for child in element}
        record.update(element.attrib)
        records.append(record)
    if not records:
        raise PayloadError(f"No <{record_tag}> records were found in the XML file.")
    return record_tag, records


def parse_payload(raw, payload_type, file_name=""):
    """
    Parse an uploaded payload and describe it for the mapping dropdowns.

    Returns {"payload_type", "file_name", "nodes", "sample", "row_count",
    "record_tag"}. Raises PayloadError with a message meant for the user.
    """
    payload_type = normalize_payload_type(payload_type)
    expected = PAYLOAD_EXTENSIONS[payload_type]
    if file_name and Path(file_name).suffix.lower() != expected:
        raise PayloadError(
            f"The payload type is set to {payload_type.upper()}, so the file must be "
            f"a {expected} file."
        )

    text = _decode(raw)
    _reject_wrong_format(text, payload_type)

    record_tag = ""
    if payload_type == "csv":
        headers, records = _parse_csv(text)
        nodes, sample = headers, _nodes_and_sample(records)[1]
    elif payload_type == "json":
        records = _parse_json(text)
        nodes, sample = _nodes_and_sample(records)
    else:
        record_tag, records = _parse_xml(text)
        nodes, sample = _nodes_and_sample(records)

    if not nodes:
        raise PayloadError("No usable nodes were found in the payload file.")

    return {
        "payload_type": payload_type,
        "file_name": os.path.basename(file_name or ""),
        "nodes": nodes,
        "sample": sample,
        "row_count": len(records),
        "record_tag": record_tag,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Script scanning
# ─────────────────────────────────────────────────────────────────────────────

def _safe_unparse(node):
    if node is None:
        return ""
    try:
        return ast.unparse(node)
    except Exception:
        return ""


def _is_client_call(call):
    """True for `self.client.post(...)`, `client.get(...)` and friends."""
    func = call.func
    if not isinstance(func, ast.Attribute) or func.attr not in CLIENT_METHODS:
        return False
    owner = func.value
    if isinstance(owner, ast.Attribute):
        return owner.attr == "client"
    return isinstance(owner, ast.Name) and owner.id == "client"


def _is_ui_owner(node):
    """`ui` or `self.ui` - the UiSession of a Functional journey."""
    if isinstance(node, ast.Name):
        return node.id == "ui"
    return isinstance(node, ast.Attribute) and node.attr == "ui"


def _is_ui_value_call(call):
    func = call.func
    return (isinstance(func, ast.Attribute) and func.attr in UI_VALUE_METHODS
            and _is_ui_owner(func.value) and len(call.args) > UI_VALUE_METHODS[func.attr])


def _ui_step_label(item):
    """The label of `with ui.step("...")`, or None for any other context manager."""
    call = item.context_expr
    if (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
            and call.func.attr == "step" and _is_ui_owner(call.func.value) and call.args
            and isinstance(call.args[0], ast.Constant) and isinstance(call.args[0].value, str)):
        return call.args[0].value
    return None


def _ui_step_index(tree):
    """Map every node to the label of the `ui.step(...)` block it sits in."""
    index = {}

    def visit(node, label):
        if isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                label = _ui_step_label(item) or label
        index[id(node)] = label
        for child in ast.iter_child_nodes(node):
            visit(child, label)

    visit(tree, None)
    return index


def _call_method(call):
    attr = call.func.attr
    if attr != "request":
        return attr.upper()
    if call.args and isinstance(call.args[0], ast.Constant) and isinstance(call.args[0].value, str):
        return call.args[0].value.upper()
    for keyword in call.keywords:
        if keyword.arg == "method" and isinstance(keyword.value, ast.Constant):
            return str(keyword.value.value).upper()
    return "REQUEST"


def _url_node(call):
    """The node holding the request URL, whichever calling form was used."""
    positional = 1 if call.func.attr == "request" else 0
    if len(call.args) > positional:
        return call.args[positional]
    for keyword in call.keywords:
        if keyword.arg in ("url", "path"):
            return keyword.value
    return None


def _request_label(call):
    """
    The label the parameter is filed under. Locust's `name=` wins because that
    is what the tester sees in the report; otherwise the URL path is used, with
    the query string dropped so `/search?q=a` and `/search?q=b` agree.
    """
    for keyword in call.keywords:
        if keyword.arg == "name" and isinstance(keyword.value, ast.Constant) \
                and isinstance(keyword.value.value, str):
            return keyword.value.value
    url = _url_node(call)
    if isinstance(url, ast.Constant) and isinstance(url.value, str):
        return urlsplit(url.value).path or url.value
    return _safe_unparse(url) or "request"


def _function_index(tree):
    """Map every node to its innermost enclosing function (None at module level)."""
    index = {}

    def visit(node, current):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            current = node
        index[id(node)] = current
        for child in ast.iter_child_nodes(node):
            visit(child, current)

    visit(tree, None)
    return index


def _walk_literal(node, prefix, out, depth=0):
    """Collect (path, value node) for every leaf of a dict/list literal."""
    if depth > MAX_NODE_DEPTH:
        return
    if isinstance(node, ast.Dict):
        for key, value in zip(node.keys, node.values):
            if not (isinstance(key, ast.Constant) and isinstance(key.value, str)):
                continue
            path = f"{prefix}.{key.value}" if prefix else key.value
            if isinstance(value, (ast.Dict, ast.List, ast.Tuple)):
                _walk_literal(value, path, out, depth + 1)
            else:
                out.append((path, value))
    elif isinstance(node, (ast.List, ast.Tuple)):
        for position, value in enumerate(node.elts[:MAX_LIST_NODES]):
            path = f"{prefix}.{position}" if prefix else str(position)
            if isinstance(value, (ast.Dict, ast.List, ast.Tuple)):
                _walk_literal(value, path, out, depth + 1)
            else:
                out.append((path, value))


def parameter_id(source, method, request, path):
    return f"{source}::{method} {request}::{path}"


def _offset_resolver(source):
    """
    Return a `(lineno, col_offset) -> index into source` function.

    `ast` reports columns as UTF-8 byte offsets, so a non-ASCII character
    earlier on the line would otherwise shift every edit on that line.
    """
    lines = source.splitlines(keepends=True)
    starts = []
    running = 0
    for line in lines:
        starts.append(running)
        running += len(line)

    def index(lineno, col_offset):
        if not (0 < lineno <= len(lines)):
            return len(source)
        line = lines[lineno - 1]
        prefix = line.encode("utf-8")[:col_offset].decode("utf-8", errors="ignore")
        return starts[lineno - 1] + len(prefix)

    return index, starts


def _collect_parameters(tree, source):
    """
    Walk the script and return an ordered dict of parameter id -> parameter.

    Each parameter carries the occurrences that have to be rewritten:
      * kind 'node'      - replace the value expression itself
      * kind 'url_query' - rebuild the URL string literal around the value
    """
    resolve, _ = _offset_resolver(source)
    functions = _function_index(tree)
    parameters = {}

    def record(source_kind, method, request, path, value_node, function, kind="node",
               query_key="", url_node=None, sample=""):
        key = parameter_id(source_kind, method, request, path)
        parameter = parameters.get(key)
        if parameter is None:
            parameter = {
                "id": key,
                "name": path.split(".")[-1],
                "path": path,
                "source": source_kind,
                "method": method,
                "request": request,
                "sample": sample,
                "occurrences": [],
            }
            parameters[key] = parameter
        occurrence = {
            "kind": kind,
            "function": function,
            "query_key": query_key,
            "url_node": url_node,
        }
        if kind == "node":
            occurrence["start"] = resolve(value_node.lineno, value_node.col_offset)
            occurrence["end"] = resolve(value_node.end_lineno, value_node.end_col_offset)
        else:
            occurrence["start"] = resolve(url_node.lineno, url_node.col_offset)
            occurrence["end"] = resolve(url_node.end_lineno, url_node.end_col_offset)
        parameter["occurrences"].append(occurrence)

    steps = _ui_step_index(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _is_ui_value_call(node):
            value_node = node.args[UI_VALUE_METHODS[node.func.attr]]
            locator = node.args[1]
            field = (locator.value if isinstance(locator, ast.Constant) and isinstance(locator.value, str)
                     else _safe_unparse(locator) or "field")
            sample = (value_node.value if isinstance(value_node, ast.Constant)
                      else _safe_unparse(value_node))
            record("input", UI_METHOD, steps.get(id(node)) or node.func.attr, field, value_node,
                   functions.get(id(node)), sample=_sample_text(sample))
            continue
        if not isinstance(node, ast.Call) or not _is_client_call(node):
            continue

        method = _call_method(node)
        request = _request_label(node)
        function = functions.get(id(node))

        for keyword in node.keywords:
            source_kind = BODY_KEYWORDS.get(keyword.arg or "")
            if not source_kind or not isinstance(keyword.value, (ast.Dict, ast.List, ast.Tuple)):
                continue
            leaves = []
            _walk_literal(keyword.value, "", leaves)
            for path, value_node in leaves:
                record(source_kind, method, request, path, value_node, function,
                       sample=_sample_text(_safe_unparse(value_node)))

        # The query string of a literal URL is parameterisable too; an f-string
        # URL is skipped because its pieces are already computed at runtime.
        url = _url_node(node)
        if isinstance(url, ast.Constant) and isinstance(url.value, str) and "?" in url.value:
            seen = set()
            for key, value in parse_qsl(urlsplit(url.value).query, keep_blank_values=True):
                if key in seen:
                    continue
                seen.add(key)
                record("query", method, request, key, url, function,
                       kind="url_query", query_key=key, url_node=url,
                       sample=_sample_text(value))

    return parameters


def public_parameters(parameters):
    """Strip the AST bookkeeping so the parameter list can be sent as JSON."""
    result = []
    for parameter in parameters.values():
        result.append({
            "id": parameter["id"],
            "name": parameter["name"],
            "path": parameter["path"],
            "source": parameter["source"],
            "source_label": SOURCE_LABELS.get(parameter["source"], parameter["source"]),
            "method": parameter["method"],
            "request": parameter["request"],
            "sample": parameter["sample"],
            "occurrences": len(parameter["occurrences"]),
        })
    return result


def extract_parameters(script_path):
    """
    List the parameters of a Locust script for the mapping dropdown.

    Raises PayloadError when the file cannot be read or does not parse, so the
    dialog can say why instead of showing an empty dropdown.
    """
    source = _read_script(script_path)
    tree = _parse_script(source, script_path)
    return public_parameters(_collect_parameters(tree, source))


def request_id(method, request):
    return f"{method} {request}"


def _collect_requests(tree):
    """
    List the distinct requests a script makes, in the order they appear.

    The id is `<METHOD> <request label>` - the same label Locust files the
    request under in its report, so a threshold saved against it can be matched
    back to the live request at run time.
    """
    requests = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.With, ast.AsyncWith)):
            # A Functional journey's steps are its "requests": each is one UI
            # request in the Locust report.
            for item in node.items:
                label = _ui_step_label(item)
                if label is None:
                    continue
                key = request_id(UI_METHOD, label)
                if key in requests:
                    requests[key]["occurrences"] += 1
                else:
                    requests[key] = {"id": key, "method": UI_METHOD, "name": label, "occurrences": 1}
            continue
        if not isinstance(node, ast.Call) or not _is_client_call(node):
            continue
        method = _call_method(node)
        label = _request_label(node)
        key = request_id(method, label)
        entry = requests.get(key)
        if entry is None:
            requests[key] = {"id": key, "method": method, "name": label, "occurrences": 1}
        else:
            entry["occurrences"] += 1
    return list(requests.values())


def extract_requests(script_path):
    """List a Locust script's requests for the response-time threshold dropdown."""
    source = _read_script(script_path)
    tree = _parse_script(source, script_path)
    return _collect_requests(tree)


def _read_script(script_path):
    try:
        with open(script_path, "r", encoding="utf-8") as handle:
            return handle.read()
    except OSError as error:
        raise PayloadError(f"The test script could not be read: {error}")


def _parse_script(source, script_path):
    try:
        return ast.parse(source, filename=str(script_path))
    except SyntaxError as error:
        raise PayloadError(
            f"The test script has a syntax error on line {error.lineno}, so its "
            f"parameters cannot be read: {error.msg}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Mapping validation
# ─────────────────────────────────────────────────────────────────────────────

def validate_mappings(mappings, parameters, nodes=None):
    """
    Check a mapping list against the script's parameters and the payload's nodes.

    Returns (clean mappings, errors, unmapped parameter ids). `errors` is empty
    when the configuration is safe to save; unmapped parameters are only a
    warning - a payload does not have to drive every field of a script.

    `nodes` is the node list of the uploaded payload. Pass None when the payload
    is not being re-parsed (a regeneration at run time, say) to skip that check.
    """
    node_set = None if nodes is None else set(nodes)
    clean = []
    errors = []
    used = set()

    for entry in mappings or []:
        parameter_key = (entry.get("parameter") or "").strip()
        node = (entry.get("node") or "").strip()
        if not parameter_key or not node:
            errors.append("Every mapping row needs both a script parameter and a payload node.")
            continue
        if parameter_key not in parameters:
            errors.append(
                f"'{_short(parameter_key)}' is no longer a parameter of this script. "
                "Remove that row and map the field again."
            )
            continue
        if node_set is not None and node not in node_set:
            errors.append(f"'{node}' is not a node of the uploaded payload file.")
            continue
        if parameter_key in used:
            label = parameters[parameter_key]["path"]
            errors.append(f"'{label}' is mapped more than once. Map each script parameter to one payload node.")
            continue
        used.add(parameter_key)
        clean.append({"parameter": parameter_key, "node": node})

    unmapped = [key for key in parameters if key not in used]
    return clean, errors, unmapped


def _short(parameter_key):
    return parameter_key.split("::")[-1] or parameter_key


def threshold_label(request):
    return GENERAL_THRESHOLD_LABEL if request == GENERAL_THRESHOLD else request


def validate_thresholds(thresholds, requests=None):
    """
    Check the response-time threshold rows against the script's requests.

    Returns (clean thresholds, errors). A row is `{"request", "seconds"}`, where
    `request` is a request id from `extract_requests()` or `__general__` for the
    row that covers every request without one of its own.

    `requests` is the script's request list; pass None to skip that check (a
    regeneration at run time, where the rows were already validated on save).
    """
    known = None if requests is None else {entry["id"] for entry in requests}
    clean = []
    errors = []
    seen = set()

    for entry in thresholds or []:
        request = (entry.get("request") or "").strip()
        raw_seconds = entry.get("seconds")
        if not request:
            errors.append("Every threshold row needs a request, or General for all of them.")
            continue

        label = threshold_label(request)
        try:
            seconds = float(str(raw_seconds).strip())
        except (TypeError, ValueError):
            errors.append(f"The threshold for '{label}' must be a number of seconds.")
            continue
        if seconds <= 0:
            errors.append(f"The threshold for '{label}' must be greater than zero seconds.")
            continue
        if seconds > MAX_THRESHOLD_SECONDS:
            errors.append(f"The threshold for '{label}' must be {MAX_THRESHOLD_SECONDS} seconds or less.")
            continue
        if request != GENERAL_THRESHOLD and known is not None and request not in known:
            errors.append(
                f"'{label}' is no longer a request in this script. Remove that row and "
                "set the threshold again."
            )
            continue
        if request in seen:
            errors.append(f"'{label}' has more than one threshold. Keep a single row per request.")
            continue

        seen.add(request)
        clean.append({"request": request, "seconds": round(seconds, 3)})

    return clean, errors


def describe_thresholds(thresholds):
    """One-line summary of the saved thresholds, for the execution console."""
    return ", ".join(
        f"{threshold_label(entry['request'])} {float(entry['seconds']):g}s"
        for entry in thresholds or []
    )


# ─────────────────────────────────────────────────────────────────────────────
# Script rewriting
# ─────────────────────────────────────────────────────────────────────────────

def _row_binding_anchor(function):
    """
    The statement the per-iteration row binding is inserted in front of, or None
    when it cannot be inserted (module level, or a body written on the `def`
    line). Callers fall back to reading a fresh record inline.
    """
    if function is None:
        return None
    body = function.body
    first = body[0]
    if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) \
            and isinstance(first.value.value, str):
        if len(body) < 2:
            return None
        first = body[1]
    if first.lineno == function.lineno:
        return None  # `def f(self): ...` - nothing to indent against
    return first


def _accessor(node, bindable):
    literal = json.dumps(node)
    if bindable:
        return f"{VALUE_FUNC}({ROW_VAR}, {literal})"
    return f"{VALUE_FUNC}({LOADER_VAR}.next(), {literal})"


def _rewrite_url(url_value, mapped_keys, bindable):
    """
    Rebuild a URL string literal so the mapped query parameters read from the
    payload: `'/search?q=shoes&p=1'` becomes
    `'/search?q=' + quote_plus(str(_bigqa_value(row, "term"))) + '&p=1'`.

    Values that are not mapped are re-encoded rather than pasted back verbatim,
    so a recorded `%20` survives the round trip through parse_qsl.
    """
    parts = urlsplit(url_value)
    pairs = parse_qsl(parts.query, keep_blank_values=True)
    if not pairs:
        return ""

    pieces = []          # ("literal", text) / ("expression", source)
    literal = url_value.split("?", 1)[0] + "?"
    for position, (key, value) in enumerate(pairs):
        if position:
            literal += "&"
        literal += f"{quote_plus(key)}="
        if key in mapped_keys:
            pieces.append(("literal", literal))
            literal = ""
            pieces.append(("expression",
                           f"quote_plus(str({_accessor(mapped_keys[key], bindable)}))"))
        else:
            literal += quote_plus(value)
    if parts.fragment:
        literal += f"#{parts.fragment}"
    pieces.append(("literal", literal))

    return " + ".join(
        json.dumps(text) if kind == "literal" else text
        for kind, text in pieces if kind == "expression" or text
    )


def _root_expr(levels, path_name="Path"):
    """Source for the project root, `levels` folders above the generated script."""
    return f"{path_name}(__file__).resolve()" + ".parent" * (levels + 1)


def _preamble(script_file, payload_name, payload_type, record_tag, strategy,
              mapping_count, row_count, needs_quote, levels=1, data_rel=DATA_DIRNAME):
    payload_relative = f"{data_rel}/{payload_name}" if data_rel else payload_name
    header = [
        "# ── BIG QA payload parameterisation (generated — do not edit) ─────────────",
        f"# Source script : {script_file}",
        f"# Payload       : {payload_relative} ({payload_type.upper()}"
        + (f", {row_count} record(s)" if row_count else "") + ")",
        f"# Mapped fields : {mapping_count}",
        f"# Generated     : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "# Regenerated from the source script every time this test runs, using the",
        "# mapping saved in the Payload Configuration dialog. Edit the source script.",
        "import sys",
        "from pathlib import Path",
    ]
    if needs_quote:
        header.append("from urllib.parse import quote_plus")
    header += [
        "",
        "# The framework's core package sits at the project root.",
        f"sys.path.insert(0, str({_root_expr(levels)}))",
        "",
        "from core.payload_loader import PayloadLoader",
        "",
        f"{PAYLOAD_FILE_CONST} = str({_root_expr(levels)}"
        + "".join(f" / {json.dumps(part)}" for part in (data_rel or "").split("/") if part)
        + f" / {json.dumps(payload_name)})",
        f"{LOADER_VAR} = PayloadLoader(",
        f'    {PAYLOAD_FILE_CONST}, strategy="{strategy}", '
        f'xml_record_tag={json.dumps(record_tag or "record")}',
        ")",
        "",
        "",
        f'def {VALUE_FUNC}(row, node, default=""):',
        '    """Read one payload node (a dot path for nested JSON) out of a record."""',
        "    current = row",
        '    for part in str(node).split("."):',
        "        if isinstance(current, dict):",
        "            current = current.get(part)",
        '        elif isinstance(current, list) and part.lstrip("-").isdigit():',
        "            index = int(part)",
        "            current = current[index] if -len(current) <= index < len(current) else None",
        "        else:",
        "            return default",
        "        if current is None:",
        "            return default",
        "    return current",
        "",
        "",
        "# ── end BIG QA payload parameterisation ──────────────────────────────────",
        "",
    ]
    return "\n".join(header)


def _threshold_preamble(thresholds, levels=1, has_ui=False):
    """
    Source for the block that turns the saved NFR limits into pass/fail.

    Every request is issued with `catch_response` so the outcome is decided
    here: Locust's own verdict stands when the call errored or returned an HTTP
    error, and a response that would otherwise have passed is failed when it
    came back slower than its limit. Milliseconds are baked in because that is
    the unit Locust measures in.
    """
    general = 0.0
    per_request = {}
    for entry in thresholds:
        seconds = float(entry["seconds"])
        if entry["request"] == GENERAL_THRESHOLD:
            general = seconds
        else:
            per_request[entry["request"]] = seconds

    described = []
    if general:
        described.append(f"# General       : {general:g}s - every request without a row of its own")
    described += [f"# {request} : {seconds:g}s" for request, seconds in per_request.items()]

    limits = ", ".join(
        f"{json.dumps(request)}: {seconds * 1000:.1f}" for request, seconds in per_request.items()
    )
    lines = [
        "",
        "# ── BIG QA response-time thresholds (generated — do not edit) ─────────────",
        "# A response slower than its threshold is reported to Locust as a failure.",
    ] + described + [
        "from urllib.parse import urlsplit as _bigqa_urlsplit",
        "",
        "from locust.clients import HttpSession as _BigQaHttpSession",
        "",
        "_BIGQA_THRESHOLD_MS = {" + limits + "}",
        f"_BIGQA_DEFAULT_THRESHOLD_MS = {general * 1000:.1f}",
        "",
        "",
        "def _bigqa_threshold_ms(method, name):",
        '    """The limit for one request in ms: its own row, else the General row."""',
        '    label = str(name or "")',
        '    path = _bigqa_urlsplit(label).path or label.split("?", 1)[0]',
        '    for key in (f"{method} {label}", label, f"{method} {path}", path):',
        "        if key in _BIGQA_THRESHOLD_MS:",
        "            return _BIGQA_THRESHOLD_MS[key]",
        "    return _BIGQA_DEFAULT_THRESHOLD_MS",
        "",
        "",
        "def _bigqa_elapsed_ms(response):",
        '    """How long Locust timed the request at, in ms."""',
        '    meta = getattr(response, "request_meta", None) or {}',
        '    elapsed = meta.get("response_time")',
        "    if elapsed is None:",
        '        clock = getattr(response, "elapsed", None)',
        "        elapsed = clock.total_seconds() * 1000 if clock is not None else 0.0",
        "    return float(elapsed)",
        "",
        "",
        "def _bigqa_enforce_thresholds(session_class):",
        '    """Wrap a Locust session so a response over its threshold fails."""',
        "    original = session_class.request",
        "",
        "    def request(self, method, url, name=None, catch_response=False, **kwargs):",
        "        # A call the script already made with catch_response=True owns its",
        "        # own verdict, so it is passed straight through.",
        "        if catch_response:",
        "            return original(self, method, url, name=name, catch_response=True, **kwargs)",
        "        with original(self, method, url, name=name, catch_response=True, **kwargs) as response:",
        "            limit = _bigqa_threshold_ms(str(method).upper(), name if name else url)",
        "            elapsed = _bigqa_elapsed_ms(response)",
        "            if limit and elapsed > limit:",
        "                try:",
        "                    response.raise_for_status()",
        "                except Exception:",
        "                    pass  # already failing; keep Locust's own reason",
        "                else:",
        "                    response.failure(",
        '                        "Response time %.0f ms exceeded the %.0f ms threshold"',
        "                        % (elapsed, limit)",
        "                    )",
        "        return response",
        "",
        "    session_class.request = request",
        "",
        "",
        "_bigqa_enforce_thresholds(_BigQaHttpSession)",
        "",
        "# A FastHttpUser script uses a different session class; older Locust builds",
        "# ship without it, which is not an error here.",
        "try:",
        "    from locust.contrib.fasthttp import FastHttpSession as _BigQaFastHttpSession",
        "except Exception:",
        "    pass",
        "else:",
        "    _bigqa_enforce_thresholds(_BigQaFastHttpSession)",
        "",
    ] + ([
        "# A Functional journey reports its UI steps through core.ui_journey.",
        "import sys as _bigqa_sys",
        "from pathlib import Path as _BigQaPath",
        "",
        f"_bigqa_sys.path.insert(0, str({_root_expr(levels, '_BigQaPath')}))",
        "from core import ui_journey as _bigqa_ui  # noqa: E402",
        "",
        "_bigqa_ui.set_thresholds(_BIGQA_THRESHOLD_MS, _BIGQA_DEFAULT_THRESHOLD_MS)",
        "",
    ] if has_ui else []) + [
        "# ── end BIG QA response-time thresholds ──────────────────────────────────",
        "",
    ]
    return "\n".join(lines)


def _preamble_position(tree, source):
    """Insert point for the preamble: after the module docstring, else line 1."""
    resolve, starts = _offset_resolver(source)
    if not tree.body:
        return len(source)
    first = tree.body[0]
    if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) \
            and isinstance(first.value.value, str):
        end = resolve(first.end_lineno, first.end_col_offset)
        # Skip to the start of the next line so the preamble does not land on
        # the docstring's closing quotes.
        newline = source.find("\n", end)
        return len(source) if newline == -1 else newline + 1
    return starts[first.lineno - 1] if 0 < first.lineno <= len(starts) else 0


def build_parameterized_script(script_path, mappings, payload_name, payload_type,
                               record_tag="record", strategy="round_robin", row_count=0,
                               nodes=None, thresholds=None, levels=1, data_rel=DATA_DIRNAME):
    """
    Return the source of the parameterised copy of `script_path`.

    Either half of the configuration is enough to produce a copy: a payload
    mapping, response-time thresholds, or both.

    Raises PayloadError when the script cannot be parsed or a mapping no longer
    matches anything in it, so a stale configuration fails loudly at save/run
    time rather than silently running the script unparameterised.
    """
    source = _read_script(script_path)
    tree = _parse_script(source, script_path)
    parameters = _collect_parameters(tree, source)
    _, line_starts = _offset_resolver(source)

    clean_thresholds, threshold_errors = validate_thresholds(thresholds, _collect_requests(tree))
    if threshold_errors:
        raise PayloadError(" ".join(dict.fromkeys(threshold_errors)))

    clean = []
    if mappings:
        clean, errors, _ = validate_mappings(mappings, parameters, nodes)
        if errors:
            raise PayloadError(" ".join(dict.fromkeys(errors)))
    if not clean and not clean_thresholds:
        raise PayloadError("Map at least one script parameter to a payload node, "
                           "or set at least one response-time threshold.")

    edits = []                 # (start, end, replacement) - an insertion has start == end
    bindings = {}              # id(function) -> function that needs a row binding
    url_groups = {}            # id(url node) -> every mapped query key of that URL
    needs_quote = False

    for mapping in clean:
        for occurrence in parameters[mapping["parameter"]]["occurrences"]:
            function = occurrence["function"]
            # A row bound at the top of the enclosing function keeps every field
            # of one iteration on the same payload record. Where that binding
            # cannot be inserted (module level, or a one-line body) the accessor
            # pulls its own record instead.
            bindable = _row_binding_anchor(function) is not None
            if bindable:
                bindings[id(function)] = function

            if occurrence["kind"] == "node":
                edits.append((occurrence["start"], occurrence["end"],
                              _accessor(mapping["node"], bindable)))
                continue

            # Several query parameters can share one URL literal, so the mapped
            # keys are collected first and the literal is rebuilt once.
            needs_quote = True
            group = url_groups.setdefault(id(occurrence["url_node"]), {
                "url": occurrence["url_node"].value,
                "start": occurrence["start"],
                "end": occurrence["end"],
                "keys": {},
                "bindable": bindable,
            })
            group["keys"][occurrence["query_key"]] = mapping["node"]

    for group in url_groups.values():
        rewritten = _rewrite_url(group["url"], group["keys"], group["bindable"])
        if rewritten:
            edits.append((group["start"], group["end"], rewritten))

    for function in bindings.values():
        line_start = line_starts[_row_binding_anchor(function).lineno - 1]
        indent = re.match(r"[ \t]*", source[line_start:]).group(0)
        edits.append((line_start, line_start,
                      f"{indent}{ROW_VAR} = {LOADER_VAR}.next()"
                      "  # payload record for this iteration\n"))

    blocks = []
    if clean:
        blocks.append(_preamble(os.path.basename(script_path), payload_name, payload_type,
                                record_tag, strategy, len(clean), row_count, needs_quote,
                                levels=levels, data_rel=data_rel))
    if clean_thresholds:
        has_ui = any(entry["method"] == UI_METHOD for entry in _collect_requests(tree))
        blocks.append(_threshold_preamble(clean_thresholds, levels=levels, has_ui=has_ui))

    preamble_at = _preamble_position(tree, source)
    edits.append((preamble_at, preamble_at, "".join(blocks)))

    # Applied back to front so every recorded offset stays valid.
    rewritten = source
    for start, end, replacement in sorted(edits, key=lambda edit: edit[0], reverse=True):
        rewritten = rewritten[:start] + replacement + rewritten[end:]
    return rewritten


# ─────────────────────────────────────────────────────────────────────────────
# Files on disk
# ─────────────────────────────────────────────────────────────────────────────

def generated_script_name(script_file):
    return f"{GENERATED_PREFIX}{Path(script_file).stem}.py"


def safe_payload_name(script_file, file_name, payload_type):
    """
    A collision-free name for the payload copy stored inside the project's
    `data/` folder. The script stem is part of the name so two scripts driven by
    files that happen to share a name do not overwrite each other.
    """
    stem = _UNSAFE_FILENAME.sub("_", Path(file_name or "payload").stem).strip("._") or "payload"
    script_stem = _UNSAFE_FILENAME.sub("_", Path(script_file).stem).strip("._") or "script"
    return f"{PAYLOAD_COPY_PREFIX}{script_stem}__{stem}{PAYLOAD_EXTENSIONS[payload_type]}"


def remove_payload_copy(path):
    """
    Delete a payload copy this module stored in a project's data folder.

    Only files inside a data folder that carry the generated prefix are touched, so a
    stale database row can never point the delete at the framework's own sample
    data (or anywhere else on disk).
    """
    target = Path(path or "")
    if not target.is_file() or target.parent.name.lower() not in _DATA_FOLDER_NAMES:
        return False
    if not target.name.startswith(PAYLOAD_COPY_PREFIX):
        return False
    try:
        target.unlink()
        return True
    except OSError:
        return False


def store_payload_file(perf_dir, script_file, file_name, raw, payload_type, tool=layout.LOCUST_TOOL):
    """
    Write the uploaded payload into the project's data folder (`data/`, or
    `Payloads/` for JMeter - whichever the project already has). Returns (path, name).

    A UTF-8 BOM is stripped on the way in. This parser reads it with utf-8-sig,
    but PayloadLoader opens the stored file as plain utf-8 at run time, where a
    surviving BOM would corrupt the first CSV heading and break json.load
    outright - the nodes offered here would then not be the nodes that exist
    during the run.
    """
    data_dir = Path(layout.data_dir(perf_dir, tool))
    data_dir.mkdir(parents=True, exist_ok=True)
    payload_name = safe_payload_name(script_file, file_name, payload_type)
    target = data_dir / payload_name

    if isinstance(raw, bytes):
        if raw.startswith(codecs.BOM_UTF8):
            raw = raw[len(codecs.BOM_UTF8):]
        with open(target, "wb") as handle:
            handle.write(raw)
    else:
        with open(target, "w", encoding="utf-8", newline="") as handle:
            handle.write(raw.lstrip("﻿"))
    return str(target), payload_name


def _loader_version(path):
    """The LOADER_VERSION declared by a copy of payload_loader.py (0 if none)."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return -1
    match = _LOADER_VERSION_RE.search(text)
    return int(match.group(1)) if match else 0


def ensure_payload_loader(perf_dir, template_dir=None):
    """
    Make sure `core/payload_loader.py` exists in the project and is current,
    copying it from the bundled framework template when an older scaffold
    predates it or predates the way this module now reads a payload.

    The scaffolder never re-copies framework files into an existing project, so
    a project created before a loader change would keep reading payloads by the
    old rules while the dialog offered nodes parsed by the new ones. Refreshing
    on the declared LOADER_VERSION keeps the generated script and the mapping
    it was built from talking about the same records.
    """
    core_dir = Path(perf_dir) / CORE_DIRNAME
    loader = core_dir / "payload_loader.py"

    template_dir = Path(template_dir) if template_dir else (
        Path(__file__).resolve().parent.parent / "scripts" / "templates" / "Locust_framework"
    )
    source = template_dir / CORE_DIRNAME / "payload_loader.py"
    if not source.is_file():
        # Nothing to copy: an existing loader is better than none.
        return loader.is_file()
    if loader.is_file() and _loader_version(loader) >= _loader_version(source):
        return True

    core_dir.mkdir(parents=True, exist_ok=True)
    init_file = core_dir / "__init__.py"
    if not init_file.exists():
        init_file.write_text("", encoding="utf-8")
    shutil.copy2(source, loader)
    return True


def write_parameterized_script(perf_dir, script_file, script_path, mappings, payload_name,
                               payload_type, record_tag="record", strategy="round_robin",
                               row_count=0, nodes=None, thresholds=None):
    """
    Generate the parameterised copy next to the original and return
    (absolute path, file name). The caller runs this file instead of the source.
    """
    # Only the payload half needs the framework's loader; a thresholds-only copy
    # imports nothing beyond Locust itself.
    if mappings and not ensure_payload_loader(perf_dir):
        raise PayloadError(
            "This performance project has no core/payload_loader.py, so a payload-driven "
            "script cannot be generated. Re-save the project from Configurations > "
            "Performance Configuration to refresh its framework files."
        )

    folder = os.path.dirname(os.path.abspath(script_path))
    data_rel = layout.relative_folder(perf_dir, layout.data_dir(perf_dir, layout.LOCUST_TOOL))
    source = build_parameterized_script(
        script_path, mappings, payload_name, payload_type,
        record_tag=record_tag, strategy=strategy, row_count=row_count, nodes=nodes,
        thresholds=thresholds, levels=layout.depth_below_root(perf_dir, folder), data_rel=data_rel,
    )
    # Fail here rather than inside Locust if the rewrite produced something odd.
    _parse_script(source, "generated script")

    target = Path(folder) / generated_script_name(script_file)
    with open(target, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(source)
    return str(target), target.name


def generated_copies(perf_dir, name):
    """Every file called `name` in the project's folders (the same pruned walk as discovery)."""
    return [Path(current) / name for current, files in layout.walk(perf_dir) if name in files]


def remove_generated_script(perf_dir, script_file):
    """Delete the parameterised copy of a script; returns True when one was removed."""
    removed = False
    for target in generated_copies(perf_dir, generated_script_name(script_file)):
        try:
            target.unlink()
            removed = True
        except OSError:
            pass
    return removed


def payload_records(raw, payload_type):
    """
    The records of a payload as flat dicts keyed by node path - the same paths
    `parse_payload` offers as nodes. A JMeter CSV Data Set is written from these.
    """
    payload_type = normalize_payload_type(payload_type)
    text = _decode(raw)
    if payload_type == "csv":
        _, records = _parse_csv(text)
    elif payload_type == "json":
        records = _parse_json(text)
    else:
        _, records = _parse_xml(text)
    flat = []
    for record in records:
        out = {}
        _flatten_record(record, "", out)
        flat.append(out)
    return flat
