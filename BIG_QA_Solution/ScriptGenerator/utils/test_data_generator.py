"""
test_data_generator.py
----------------------
Back end of the Performance Test page's "Generate Test Data" dialog: reads a
performance script, works out the data it consumes, and produces any number of
synthetic records for it as JSON, XML or CSV.

Three independent stages:

  1. `analyze_script()` builds the record schema from the script itself:

     Locust (.py)
       * the leaves of `json=` / `data=` / `params=` dict literals (a name bound
         to a dict literal is followed), the query string of literal URLs and
         the values typed by a Functional journey's `ui.type(...)`;
       * the keys read from a test-data record (`record["amount"]`,
         `row.get("email")`) - a record being a variable filled from a loader,
         `next()` / `random.choice()`, or a function parameter named like one;
       * the placeholders of f-string URLs (`f"/users/{user_id}"`) that the
         script does not compute itself.
     JMeter (.jmx)
       * the arguments, raw JSON / XML body leaves and query string of every
         HTTP sampler;
       * the columns of every CSV Data Set (its `variableNames`, in order);
       * every `${variable}` the plan reads that nothing in the plan defines -
         extractors, User Defined Variables, counters and the `vars.put(...)`
         of JSR223 scripts (inline or in their script file) are excluded.
     Both
       * any CSV / JSON / XML data file the script names that exists in the
         project: its fields join the schema and its records become samples,
         which drive the type, format, range and choices of every field.

  2. `generate_records()` fills the schema. The type of a field comes from its
     samples first (email, UUID, number range, date format, a small set of
     choices, a code like `ORD-1001`) and from its name otherwise. Values that
     belong together are generated together per record: first / last / full
     name, username and email; city, state, country, postal code and phone
     prefix; `password` / `confirmPassword`; start <= end dates; created <=
     updated; age from date of birth; total = quantity x price. Fields that
     identify a record (ids, codes, emails, usernames) are unique.

  3. `serialize()` writes the records as JSON, XML or CSV and `validate()`
     parses the result back and counts the records before it is offered for
     download.

Generated values are synthetic: names from fixed lists, emails on the reserved
example.com / example.org domains, card numbers from the test-card ranges.
"""

import ast
import csv
import io
import json
import os
import random
import re
import string
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl, urlsplit
from pathlib import Path

from utils import perf_project_layout as layout
from utils import payload_parameterizer as locust_payload
from utils import jmeter_payload
from utils.payload_parameterizer import PayloadError

FORMATS = {"json": ".json", "xml": ".xml", "csv": ".csv"}
MIME_TYPES = {"json": "application/json", "xml": "application/xml", "csv": "text/csv"}
DEFAULT_RECORDS = 100
DEFAULT_MAX_RECORDS = 50_000
PREVIEW_PAGE_SIZE = 50

# How many records of a data file the script already reads are used as samples.
_SAMPLE_RECORDS = 200
_MAX_DATA_FILES = 10
_DATA_EXTENSIONS = {".csv": "csv", ".json": "json", ".xml": "xml"}
# Share of an optional field's values left empty.
_OPTIONAL_BLANK_RATE = 0.1

_JMETER_VAR = re.compile(r"\$\{([A-Za-z_][\w.\-]*)\}")
_JMETER_PROPERTY_DEFAULT = re.compile(r"\$\{__P\(\s*[^,()]+\s*,\s*([^()]*?)\s*\)\}")
_VARS_PUT = re.compile(r"""vars\.put(?:Object)?\(\s*["']([^"']+)["']""")
_DATA_FILE_NAME = re.compile(r"[\w.\-/\\${}():]+\.(?:csv|json|xml)\b", re.IGNORECASE)


class TestDataError(Exception):
    """The script cannot be analysed or its data generated; the message is shown to the tester."""


def max_records():
    """The largest record count one request may ask for (TEST_DATA_MAX_RECORDS in .env)."""
    raw = (os.getenv("TEST_DATA_MAX_RECORDS", "") or "").strip()
    try:
        value = int(float(raw))
    except ValueError:
        return DEFAULT_MAX_RECORDS
    return value if value > 0 else DEFAULT_MAX_RECORDS


def validate_request(output_format, count):
    """Return (format, count) or raise TestDataError with the reason."""
    output_format = (output_format or "").strip().lower()
    if output_format not in FORMATS:
        raise TestDataError("Choose an output format of XML, JSON or CSV.")
    try:
        number = int(str(count).strip())
        if str(number) != str(count).strip().lstrip("+"):
            raise ValueError
    except (TypeError, ValueError):
        raise TestDataError("The number of records must be a whole number.")
    if number < 1:
        raise TestDataError("The number of records must be at least 1.")
    limit = max_records()
    if number > limit:
        raise TestDataError(f"The number of records cannot exceed {limit:,}.")
    return output_format, number


# ─────────────────────────────────────────────────────────────────────────────
# 1. Script analysis
# ─────────────────────────────────────────────────────────────────────────────

class _Schema:
    """Fields keyed by path, in the order they were found."""

    def __init__(self):
        self.fields = {}
        self.record_tag = ""
        self.data_files = []

    def add(self, path, origin, samples=(), optional=False, column=False, typed=False):
        """
        Record a field. `typed` marks samples whose Python / JSON type is the one
        the script sends (a literal, a JSON body or JSON data file), as opposed
        to text from a form, CSV or XML where every value is a string.
        """
        path = _clean_path(path)
        if not path:
            return None
        field = self.fields.get(path)
        if field is None:
            field = {"path": path, "origins": [], "samples": [], "optional": optional, "column": column,
                     "typed": typed}
            self.fields[path] = field
        else:
            field["optional"] = field["optional"] and optional
            field["column"] = field["column"] or column
            field["typed"] = field["typed"] or typed
        if origin and origin not in field["origins"]:
            field["origins"].append(origin)
        for sample in samples:
            if len(field["samples"]) < _SAMPLE_RECORDS:
                field["samples"].append(sample)
        return field


def _clean_path(path):
    parts = [p.strip() for p in str(path or "").split(".") if p.strip()]
    return ".".join(parts)


def _is_template_value(value):
    """A value the script fills in at run time rather than data: `${var}`, `{{var}}`, `{placeholder}`."""
    return isinstance(value, str) and bool(re.search(r"\$\{[^}]+\}|\{\{[^}]+\}\}", value))


def analyze_script(script_path, tool, perf_dir=""):
    """
    Return the record schema of a script:

        {"script": file name, "tool", "record_tag", "data_files": [names],
         "fields": [{"path", "origins", "samples", "optional", "column"}]}

    Raises TestDataError when the script cannot be read or names no input.
    """
    if not script_path or not os.path.isfile(script_path):
        raise TestDataError("The script file was not found. Refresh the list and try again.")
    try:
        with open(script_path, "r", encoding="utf-8-sig") as handle:
            source = handle.read()
    except UnicodeDecodeError:
        raise TestDataError(f"{os.path.basename(script_path)} is not UTF-8 text, so it cannot be analysed.")
    except OSError as error:
        raise TestDataError(f"The script could not be read: {error}")

    schema = _Schema()
    extension = Path(script_path).suffix.lower()
    if extension == ".jmx":
        file_refs = _analyze_jmeter(source, script_path, perf_dir, schema)
    elif extension == ".py":
        file_refs = _analyze_locust(source, script_path, schema)
    else:
        raise TestDataError(f"{Path(script_path).name} is not a Locust script (.py) or JMeter plan (.jmx).")

    _read_data_files(file_refs, script_path, perf_dir, schema)
    _drop_shadowed_leaves(schema)

    if not schema.fields:
        raise TestDataError(
            f"No input parameters were found in {Path(script_path).name}. Test data is generated for the "
            "request fields, query parameters, typed values and data-file columns a script uses; this "
            "script sends none of them."
        )
    return {
        "script": Path(script_path).name,
        "tool": tool,
        "record_tag": schema.record_tag,
        "data_files": schema.data_files,
        "fields": list(schema.fields.values()),
    }


def _drop_shadowed_leaves(schema):
    """A path that is both a value and the parent of other fields keeps its children."""
    paths = set(schema.fields)
    for path in list(schema.fields):
        if any(other.startswith(path + ".") for other in paths):
            schema.fields.pop(path, None)


# ── JMeter ──────────────────────────────────────────────────────────────────

def _analyze_jmeter(source, script_path, perf_dir, schema):
    """Add a plan's inputs to `schema`; returns the data file names it refers to."""
    try:
        root = ET.fromstring(source.encode("utf-8"))
    except ET.ParseError as error:
        raise TestDataError(f"The test plan is not well-formed XML: {error}")

    defined = set()
    csv_columns = []
    file_refs = []
    referenced = {}

    # Pass 1 - what the plan defines, and where it reads data from.
    for element, _sub, _parent in jmeter_payload._walk_tree(root):
        if element.get("enabled") == "false":
            continue
        tag = element.tag
        if tag == "CSVDataSet":
            file_name = _resolve_jmeter_text(_prop_text(element, "filename"))
            names = [n.strip() for n in _prop_text(element, "variableNames").split(",") if n.strip()]
            delimiter = _prop_text(element, "delimiter") or ","
            csv_columns.append({"file": file_name, "names": names, "delimiter": delimiter,
                                "header": _prop_text(element, "ignoreFirstLine").lower() == "true"})
            continue
        if tag in ("Arguments",) or element.get("testclass") == "Arguments":
            defined.update(_argument_names(element))
        if tag == "TestPlan":
            holder = jmeter_payload._prop(element, "TestPlan.user_defined_variables")
            if holder is not None:
                defined.update(_argument_names(holder))
        for prop_name in ("RegexExtractor.refname", "refname", "JSONPostProcessor.referenceNames",
                          "BoundaryExtractor.refname", "XPathExtractor.refname", "XPath2Extractor.refname",
                          "HtmlExtractor.refname", "CounterConfig.name", "variableName",
                          "JSONExtractor.referenceNames"):
            for name in _prop_text(element, prop_name).split(";"):
                if name.strip():
                    defined.add(name.strip())
        if tag.startswith("JSR223") or tag.startswith("BeanShell"):
            defined.update(_VARS_PUT.findall(_prop_text(element, "script")))
            script_file = _resolve_jmeter_text(_prop_text(element, "filename"))
            if script_file:
                defined.update(_VARS_PUT.findall(_read_companion(script_file, script_path, perf_dir)))
        for text in _element_texts(element):
            for match in _DATA_FILE_NAME.findall(_resolve_jmeter_text(text)):
                if "${" not in match:
                    file_refs.append(match)

    # Pass 2 - HTTP request fields with literal values.
    for element, _sub, _parent in jmeter_payload._walk_tree(root):
        if element.tag != "HTTPSamplerProxy" or element.get("enabled") == "false":
            continue
        method = (_prop_text(element, "HTTPSampler.method") or "GET").upper()
        request = element.get("testname") or _prop_text(element, "HTTPSampler.path") or "request"
        for argument in jmeter_payload._arguments(element):
            value = _prop_text(argument, "Argument.value")
            if value.lstrip().startswith("<"):
                _add_xml_body(value, f"{method} {request} · request body", schema)
    for parameter in _jmeter_literal_parameters(root):
        schema.add(parameter["path"], f"{parameter['method']} {parameter['request']} · {parameter['source_label']}",
                   [parameter["value"]], typed=parameter["typed"])

    # Pass 3 - variables read but not defined: the plan's data inputs.
    for element, _sub, _parent in jmeter_payload._walk_tree(root):
        if element.get("enabled") == "false":
            continue
        where = element.get("testname") or element.tag
        for text in _element_texts(element):
            for name in _JMETER_VAR.findall(text):
                referenced.setdefault(name, where)

    for dataset in csv_columns:
        origin = f"CSV Data Set ({Path(dataset['file']).name or 'data file'})"
        for name in dataset["names"]:
            schema.add(name, origin, column=True)
        _read_csv_dataset(dataset, script_path, perf_dir, schema)
    column_names = {n for d in csv_columns for n in d["names"]}
    for name, where in referenced.items():
        if name in defined or name in column_names or name.startswith("__") or name.startswith("_bigqa"):
            continue
        schema.add(name, f"${{{name}}} in {where}")
    return file_refs


def _prop_text(element, name):
    return jmeter_payload._prop_text(element, name) if element is not None else ""


def _argument_names(element):
    names = set()
    for argument in element.iter("elementProp"):
        name = _prop_text(argument, "Argument.name") or ""
        if name and argument.get("elementType") == "Argument":
            names.add(name)
    return names


def _element_texts(element):
    """Every own property text of an element (not of its children in the tree)."""
    texts = []
    for node in element.iter():
        if node.text and node.text.strip():
            texts.append(node.text)
    return texts


def _resolve_jmeter_text(text):
    """`${__P(dir,Payloads)}/x.csv` -> `Payloads/x.csv`: properties take their default."""
    return _JMETER_PROPERTY_DEFAULT.sub(lambda m: m.group(1), (text or "").strip())


def _jmeter_literal_parameters(root):
    """The literal fields of the plan's HTTP samplers, with their recorded value."""
    found = []
    for key, parameter in jmeter_payload._collect(root).items():
        kind, owner, extra = parameter["occurrences"][0]
        value = parameter["sample"]
        if kind == "json":
            try:
                body = json.loads(_prop_text(owner, "Argument.value"))
                leaves = []
                jmeter_payload._json_leaves(body, "", leaves)
                value = dict(leaves).get(extra, value)
            except ValueError:
                pass
        elif kind == "argument":
            value = _prop_text(owner, "Argument.value")
        if _is_template_value(value):
            continue
        found.append({**parameter, "value": value, "typed": kind == "json",
                      "source_label": locust_payload.SOURCE_LABELS.get(parameter["source"], parameter["source"])})
    return found


def _add_xml_body(text, origin, schema):
    try:
        body = ET.fromstring(text.strip())
    except ET.ParseError:
        return
    if not schema.record_tag:
        schema.record_tag = body.tag

    def walk(element, prefix, depth=0):
        if depth > locust_payload.MAX_NODE_DEPTH:
            return
        for name, value in element.attrib.items():
            if not _is_template_value(value):
                schema.add(f"{prefix}.@{name}" if prefix else f"@{name}", origin, [value])
        children = list(element)
        if not children:
            value = (element.text or "").strip()
            if prefix and not _is_template_value(value):
                schema.add(prefix, origin, [value] if value else [])
            return
        for child in children:
            walk(child, f"{prefix}.{child.tag}" if prefix else child.tag, depth + 1)

    walk(body, "")


def _read_companion(file_name, script_path, perf_dir):
    path = _find_file(file_name, script_path, perf_dir)
    if not path:
        return ""
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as handle:
            return handle.read(500_000)
    except OSError:
        return ""


def _read_csv_dataset(dataset, script_path, perf_dir, schema):
    """Samples for a CSV Data Set's columns, read from its file when it exists."""
    path = _find_file(dataset["file"], script_path, perf_dir)
    if not path:
        return
    try:
        with open(path, "r", encoding="utf-8-sig", errors="replace", newline="") as handle:
            rows = list(csv.reader(handle, delimiter=dataset["delimiter"][:1] or ","))
    except OSError:
        return
    if not rows:
        return
    names = dataset["names"]
    if not names:
        names = [n.strip() for n in rows[0]]
        for name in names:
            schema.add(name, f"CSV Data Set ({Path(path).name})", column=True)
        dataset["names"] = names
    if dataset["header"] or [c.strip() for c in rows[0]] == names:
        rows = rows[1:]
    for row in rows[:_SAMPLE_RECORDS]:
        for name, value in zip(names, row):
            field = schema.fields.get(name)
            if field is not None and len(field["samples"]) < _SAMPLE_RECORDS:
                field["samples"].append(value)
    _note_data_file(schema, path)


# ── Locust ──────────────────────────────────────────────────────────────────

_RECORD_NAMES = {"record", "rec", "row", "item", "entry", "payload", "data_row", "test_data", "testdata",
                 "user_data", "user", "customer", "account", "line", "data"}
_LOADER_CALLS = {"next", "choice", "get_next", "next_record", "next_row", "random", "pop", "get_record",
                 "sample_record", "pick"}
_RESPONSE_HINTS = ("json(", "response", "resp.", ".text", ".headers", ".cookies", "status_code")


def _analyze_locust(source, script_path, schema):
    """Add a Locust script's inputs to `schema`; returns the data file names it refers to."""
    try:
        tree = ast.parse(source, filename=str(script_path))
    except SyntaxError as error:
        raise TestDataError(
            f"The script has a syntax error on line {error.lineno}, so its parameters cannot be read: {error.msg}"
        )

    assignments = _assignments(tree)
    record_vars = _record_variables(tree, assignments)

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if locust_payload._is_ui_value_call(node):
            _add_ui_value(node, schema)
            continue
        if not locust_payload._is_client_call(node):
            continue
        method = locust_payload._call_method(node)
        request = locust_payload._request_label(node)
        for keyword in node.keywords:
            source_kind = locust_payload.BODY_KEYWORDS.get(keyword.arg or "")
            if not source_kind:
                continue
            origin = f"{method} {request} · {locust_payload.SOURCE_LABELS.get(source_kind, source_kind)}"
            value = keyword.value
            if isinstance(value, ast.Name) and isinstance(assignments.get(value.id), (ast.Dict, ast.List)):
                value = assignments[value.id]
            if isinstance(value, ast.Constant) and isinstance(value.value, str) \
                    and value.value.lstrip().startswith("<"):
                _add_xml_body(value.value, origin, schema)
                continue
            if isinstance(value, (ast.Dict, ast.List, ast.Tuple)):
                leaves = []
                locust_payload._walk_literal(value, "", leaves)
                for path, leaf in leaves:
                    if isinstance(leaf, ast.Constant) and not _is_template_value(leaf.value):
                        schema.add(path, origin, [leaf.value], typed=True)
                    elif isinstance(leaf, (ast.Name, ast.Attribute)) and _is_input_name(leaf, assignments):
                        schema.add(path, origin, _constant_samples(leaf, assignments))
        url = locust_payload._url_node(node)
        origin = f"{method} {request}"
        if isinstance(url, ast.Constant) and isinstance(url.value, str) and "?" in url.value:
            for key, value in parse_qsl(urlsplit(url.value).query, keep_blank_values=True):
                schema.add(key, f"{origin} · query parameter", [value])
        elif isinstance(url, ast.JoinedStr):
            for part in url.values:
                if isinstance(part, ast.FormattedValue) and isinstance(part.value, (ast.Name, ast.Attribute)) \
                        and _is_input_name(part.value, assignments):
                    schema.add(_short_name(part.value), f"{origin} · URL path",
                               _constant_samples(part.value, assignments))

    _add_record_reads(tree, record_vars, schema)
    return [n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
            and Path(n.value.strip()).suffix.lower() in _DATA_EXTENSIONS and len(n.value) < 400]


def _assignments(tree):
    """Name (or `self.attr`) -> the last expression assigned to it anywhere in the script."""
    found = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                key = _target_key(target)
                if key:
                    found[key] = node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            key = _target_key(node.target)
            if key:
                found[key] = node.value
    return found


def _target_key(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
        return f"{node.value.id}.{node.attr}"
    return ""


def _short_name(node):
    return node.id if isinstance(node, ast.Name) else node.attr


def _is_response_expression(node):
    text = locust_payload._safe_unparse(node)
    return any(hint in text for hint in _RESPONSE_HINTS)


def _is_input_name(node, assignments):
    """
    True for a name whose value is test input: never assigned (a parameter) or
    assigned a constant. Computed and correlated values (taken from a
    response), and UPPER_CASE configuration constants, are not.
    """
    key = _target_key(node)
    if not key or _short_name(node).isupper() or _short_name(node) in ("self", "host", "base_url"):
        return False
    value = assignments.get(key)
    return value is None or isinstance(value, ast.Constant)


def _constant_samples(node, assignments):
    value = assignments.get(_target_key(node))
    return [value.value] if isinstance(value, ast.Constant) and value.value is not None else []


def _record_variables(tree, assignments):
    """Names (or `self.attr`) that hold one test-data record."""
    found = set()
    for key, value in assignments.items():
        if _is_response_expression(value):
            continue
        if isinstance(value, ast.Call):
            func = value.func
            name = func.attr if isinstance(func, ast.Attribute) else func.id if isinstance(func, ast.Name) else ""
            if name in _LOADER_CALLS:
                found.add(key)
        elif isinstance(value, ast.Subscript) and not _is_response_expression(value):
            # rows[i % len(rows)]
            found.add(key)
    for node in ast.walk(tree):
        if isinstance(node, ast.For) and _target_key(node.target) and not _is_response_expression(node.iter):
            found.add(_target_key(node.target))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for arg in node.args.args:
                if arg.arg.lower() in _RECORD_NAMES and arg.arg not in assignments:
                    found.add(arg.arg)
    return {key for key in found
            if not _is_response_expression(assignments.get(key, ast.Constant(value=None)))}


def _record_read(node, record_vars):
    """(path, optional) for `record["a"]["b"]` / `record.get("a")`, else None."""
    keys = []
    current = node
    optional = False
    while True:
        if isinstance(current, ast.Subscript) and isinstance(current.slice, ast.Constant) \
                and isinstance(current.slice.value, (str, int)):
            keys.append(str(current.slice.value))
            current = current.value
        elif isinstance(current, ast.Call) and isinstance(current.func, ast.Attribute) \
                and current.func.attr == "get" and current.args \
                and isinstance(current.args[0], ast.Constant) and isinstance(current.args[0].value, str):
            keys.append(current.args[0].value)
            optional = True
            current = current.func.value
        else:
            break
    if keys and _target_key(current) in record_vars:
        return ".".join(reversed(keys)), optional
    return None


def _add_record_reads(tree, record_vars, schema):
    if not record_vars:
        return
    seen = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Subscript, ast.Call)):
            continue
        read = _record_read(node, record_vars)
        if not read:
            continue
        path, optional = read
        # The outer read of a chain covers the inner ones (record["a"]["b"] over record["a"]).
        if any(other.startswith(path + ".") for other in seen):
            continue
        seen.add(path)
        schema.add(path, "test-data record", optional=optional)


def _add_ui_value(node, schema):
    min_args = locust_payload.UI_VALUE_METHODS[node.func.attr]
    value = node.args[min_args] if len(node.args) > min_args else next(
        (kw.value for kw in node.keywords if kw.arg in ("value", "text", "option")), None)
    locator = node.args[1] if len(node.args) > 1 else None
    if value is None or not (isinstance(locator, ast.Constant) and isinstance(locator.value, str)):
        return
    if isinstance(value, ast.Constant):
        schema.add(_locator_field(locator.value), f"UI {node.func.attr} {locator.value}", [value.value])


def _locator_field(locator):
    """A field name for a UI locator: `#user-name` -> user-name, `input[name='email']` -> email."""
    match = re.search(r"""(?:name|id|placeholder|aria-label|data-test(?:id)?)\s*=\s*["']?([\w\- ]+)""", locator) \
        or re.search(r"#([\w\-]+)", locator)
    text = match.group(1) if match else re.sub(r"[^\w]+", "_", locator).strip("_")
    return re.sub(r"\s+", "_", text.strip())[:60] or "field"


# ── Data files the script reads ─────────────────────────────────────────────

def _find_file(name, script_path, perf_dir):
    """Resolve a file name from a script against the script folder, then the project."""
    name = (name or "").strip().strip("'\"")
    if not name or "${" in name:
        return ""
    candidates = [name] if os.path.isabs(name) else []
    base = os.path.dirname(script_path)
    candidates += [os.path.join(base, name), os.path.join(base, "..", name)]
    if perf_dir:
        candidates.append(os.path.join(perf_dir, name))
    for candidate in candidates:
        if os.path.isfile(candidate):
            return os.path.normpath(candidate)
    if not perf_dir or not os.path.isdir(perf_dir):
        return ""
    wanted = Path(name.replace("\\", "/")).name.lower()
    tail = name.replace("\\", "/").lower().lstrip("./")
    best = ""
    for current, files in layout.walk(perf_dir):
        for file_name in files:
            if file_name.lower() != wanted:
                continue
            path = os.path.join(current, file_name)
            if path.replace("\\", "/").lower().endswith(tail):
                return path
            best = best or path
    return best


def _note_data_file(schema, path):
    name = Path(path).name
    if name not in schema.data_files:
        schema.data_files.append(name)


def _read_data_files(names, script_path, perf_dir, schema):
    seen = set()
    for name in names:
        if len(schema.data_files) >= _MAX_DATA_FILES:
            break
        path = _find_file(name, script_path, perf_dir)
        if not path or path in seen or Path(path).name.startswith(layout.GENERATED_PREFIX):
            continue
        seen.add(path)
        payload_type = _DATA_EXTENSIONS.get(Path(path).suffix.lower())
        try:
            if os.path.getsize(path) > locust_payload.MAX_PAYLOAD_BYTES:
                continue
            with open(path, "rb") as handle:
                raw = handle.read()
            parsed = locust_payload.parse_payload(raw, payload_type, path)
            records = locust_payload.payload_records(raw, payload_type)
        except (OSError, PayloadError):
            continue
        origin = f"data file {Path(path).name}"
        added = False
        for node in parsed["nodes"]:
            values = [r.get(node) for r in records[:_SAMPLE_RECORDS]]
            present = [v for v in values if v not in (None, "")]
            # A request template ({"user": "${username}"}) is not data; its variables are fields already.
            if present and all(_is_template_value(v) for v in present):
                continue
            schema.add(node, origin, present, optional=len(present) < len(values), typed=payload_type == "json")
            added = True
        if added:
            if payload_type == "xml" and parsed.get("record_tag") and not schema.record_tag:
                schema.record_tag = parsed["record_tag"]
            _note_data_file(schema, path)


# ─────────────────────────────────────────────────────────────────────────────
# 2. Field typing
# ─────────────────────────────────────────────────────────────────────────────

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")
_UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_INT = re.compile(r"^[+-]?\d+$")
_DEC = re.compile(r"^[+-]?\d*\.\d+$")
_PHONE = re.compile(r"^\+?[\d\s().\-]{7,20}$")
_URL = re.compile(r"^https?://", re.IGNORECASE)
_IPV4 = re.compile(r"^(\d{1,3}\.){3}\d{1,3}$")
_BOOL_TEXT = {"true", "false"}

_DATE_FORMATS = ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y", "%Y/%m/%d", "%d.%m.%Y", "%d-%b-%Y",
                 "%b %d, %Y", "%d %b %Y")
_TIMESTAMP_FORMATS = ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%S",
                      "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S%z",
                      "%Y-%m-%dT%H:%M:%S.%f%z", "%d/%m/%Y %H:%M:%S", "%m/%d/%Y %H:%M:%S", "%d/%m/%Y %H:%M",
                      "%H:%M:%S", "%H:%M")

_TYPE_LABELS = {
    "first_name": "String (first name)", "last_name": "String (last name)", "full_name": "String (full name)",
    "username": "String (username)", "email": "Email", "phone": "Phone number", "password": "String (password)",
    "uuid": "Unique identifier (UUID)", "sequence": "Integer (unique id)", "code": "String (code)",
    "integer": "Integer", "decimal": "Decimal", "boolean": "Boolean", "date": "Date", "timestamp": "Timestamp",
    "epoch": "Timestamp (epoch)", "choice": "String (one of)", "street": "String (street address)",
    "city": "String (city)", "state": "String (state)", "country": "String (country)",
    "country_code": "String (country code)", "postal_code": "String (postal code)", "company": "String (company)",
    "url": "URL", "ip": "String (IP address)", "currency": "String (currency code)", "text": "String (text)",
    "word": "String", "product": "String (product)", "job_title": "String (job title)",
    "card_number": "String (test card number)", "cvv": "String (CVV)", "expiry": "String (card expiry)",
    "latitude": "Decimal (latitude)", "longitude": "Decimal (longitude)", "pin": "String (PIN / OTP)",
    "pattern": "String (pattern)", "gender": "String (gender)",
}


def _tokens(path):
    """Lower-case words of a field's own name: `customer.firstName` -> ['first', 'name']."""
    segments = [s for s in path.split(".") if not s.isdigit()]
    name = (segments[-1] if segments else path).lstrip("@")
    name = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name)
    name = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", name)
    return [t for t in re.split(r"[^A-Za-z0-9]+", name.lower()) if t]


def _parent(path):
    return path.rsplit(".", 1)[0] if "." in path else ""


def _date_format(values, formats):
    for fmt in formats:
        try:
            for value in values:
                datetime.strptime(value, fmt)
            return fmt
        except ValueError:
            continue
    return ""


def _infer(field):
    """Set the generator `kind` and its parameters on a schema field."""
    path = field["path"]
    tokens = _tokens(path)
    words = set(tokens)
    joined = "_".join(tokens)
    raw = [s for s in field["samples"] if s is not None and s != ""]
    samples = [str(s).strip() for s in raw]
    field["tokens"] = tokens
    # Numbers the script sends as JSON strings ("amount": "150.00") stay strings.
    quoted = field.get("typed") and any(isinstance(s, str) for s in raw)
    is_id = bool(words & {"id", "uuid", "guid", "key", "code", "ref", "reference", "sku", "no", "num", "number"}) \
        or joined.endswith("id")
    distinct = len(set(samples))

    def kind(name, **params):
        field["kind"] = name
        field.update(params)
        return field

    # ── From the values the script / its data already carry ──
    if samples:
        lowered = {s.lower() for s in samples}
        if all(isinstance(s, bool) for s in raw) or lowered <= _BOOL_TEXT:
            style = "title" if any(s in ("True", "False") for s in raw) else "lower"
            return kind("boolean", bool_style=style)
        if all(_EMAIL.match(s) for s in samples):
            domain = samples[0].split("@", 1)[1].lower()
            reserved = domain.endswith(("example.com", "example.org", "example.net", ".test", ".invalid"))
            return kind("email", domain=domain if reserved else "example.com")
        if all(_UUID.match(s) for s in samples):
            return kind("uuid", upper=samples[0].isupper())
        if all(_URL.match(s) for s in samples):
            return kind("url")
        if all(_IPV4.match(s) for s in samples):
            return kind("ip")
        timestamp_format = _date_format(samples, _TIMESTAMP_FORMATS)
        if timestamp_format and not all(_INT.match(s) for s in samples):
            fraction = re.search(r"\.(\d+)", samples[0])
            return kind("timestamp", fmt=timestamp_format, fraction=len(fraction.group(1)) if fraction else 0,
                        span=_date_span(samples, timestamp_format))
        date_format = _date_format(samples, _DATE_FORMATS)
        if date_format:
            span = _date_span(samples, date_format)
            if words & _BIRTH_WORDS:
                span = _adult_birth_span()
            return kind("date", fmt=date_format, span=span)
        if all(_INT.match(s) for s in samples):
            numbers = [int(s) for s in samples]
            width = len(samples[0])
            if words & {"time", "timestamp", "epoch", "at"} and all(len(s) in (10, 13) for s in samples):
                return kind("epoch", millis=len(samples[0]) == 13)
            if (words & {"phone", "mobile", "tel", "telephone", "cell", "msisdn", "contact"}) and width >= 7:
                return kind("pattern", template=samples[0], unique=False, keep=2)
            if samples[0].startswith("0") and len(set(map(len, samples))) == 1 and width > 1:
                return kind("pattern", template=samples[0], unique=is_id)
            if is_id or (distinct == len(samples) and len(samples) >= 5 and _is_sequence(numbers)):
                return kind("sequence", start=max(numbers) + 1, as_text=quoted)
            if 2 <= distinct <= 6 and len(samples) >= 8 and not words & _QUANTITY_WORDS:
                return kind("choice", choices=sorted(set(samples), key=int) if quoted else sorted(set(numbers)))
            low, high = min(numbers), max(numbers)
            if low == high:
                low, high = _default_int_range(words, low)
            return kind("integer", low=low, high=high, as_text=quoted)
        if all(_DEC.match(s) or _INT.match(s) for s in samples) and any(_DEC.match(s) for s in samples):
            numbers = [float(s) for s in samples]
            scale = max(len(s.split(".", 1)[1]) if "." in s else 0 for s in samples)
            low, high = min(numbers), max(numbers)
            if words & {"lat", "latitude"}:
                return kind("latitude", scale=scale)
            if words & {"lng", "lon", "long", "longitude"}:
                return kind("longitude", scale=scale)
            if low == high:
                low, high = (round(low * 0.5, scale), round(low * 1.5 + 1, scale)) if low >= 0 else (low * 1.5, 0)
            return kind("decimal", low=low, high=high, scale=scale, as_text=quoted)
        if (words & {"phone", "mobile", "tel", "telephone", "cell", "fax", "msisdn"}) and all(
                _PHONE.match(s) for s in samples):
            return kind("pattern", template=samples[0], unique=False, keep=3)
        if is_id and all(_looks_like_code(s) for s in samples):
            # ORD-1001, C-001: new ids keep the format and count on from the highest one seen.
            return kind("pattern", template=max(samples, key=_trailing_number), unique=True)

    by_name = _infer_from_name(tokens, words, joined, is_id)
    if by_name and samples and by_name[0] == "choice":
        # A status / role the script sends must stay one the application accepts.
        return kind("choice", choices=sorted(set(samples)))
    if by_name and not (samples and by_name[0] in _NAME_YIELDS_TO_SAMPLES):
        return kind(by_name[0], **by_name[1])

    if samples:
        # A small closed set of values (status, role, type ...) keeps to that set.
        if distinct <= max(3, len(samples) // 3) and len(samples) >= 3 and max(map(len, samples)) <= 40 \
                and not is_id:
            return kind("choice", choices=sorted(set(samples)))
        if _looks_like_code(samples[0]):
            return kind("pattern", template=max(samples, key=_trailing_number),
                        unique=is_id or distinct == len(samples) > 1)
        if " " in samples[0]:
            return kind("text", words=max(2, min(12, len(samples[0].split()))))
        if not is_id:
            # Free text recorded once or twice (a category, a search term) with nothing
            # better to go on: inventing words could send values the application rejects.
            return kind("choice", choices=sorted(set(samples)))
        return kind("pattern", template=samples[0], unique=True)
    if by_name:
        return kind(by_name[0], **by_name[1])
    return kind("word", case="lower")


# Kinds a field name suggests that recorded values of another shape override.
_NAME_YIELDS_TO_SAMPLES = {"sequence", "integer", "decimal", "boolean", "uuid", "pattern", "word", "date",
                           "timestamp"}


def _trailing_number(text):
    runs = re.findall(r"\d+", str(text))
    return int(runs[-1]) if runs else -1


_QUANTITY_WORDS = {"qty", "quantity", "count", "units", "items", "age", "size", "limit", "page"}


def _is_sequence(numbers):
    ordered = sorted(numbers)
    return all(b - a == 1 for a, b in zip(ordered, ordered[1:]))


def _default_int_range(words, value):
    if words & {"age"}:
        return 18, 80
    if words & {"qty", "quantity", "count", "units", "page"}:
        return 1, 10
    if words & {"year"}:
        return value - 5, value + 1
    if value == 0:
        return 0, 100
    span = max(1, abs(value) // 2)
    low = value - span
    # A positive recorded value (a page, a size) stays positive.
    return (max(1, low) if value > 0 else low), value + span


def _case_of(text):
    if text.isupper():
        return "upper"
    if text[:1].isupper():
        return "title"
    return "lower"


def _looks_like_code(text):
    return bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_\-/#:]*", text)) and any(c.isdigit() for c in text)


def _date_span(samples, fmt):
    try:
        values = [datetime.strptime(s, fmt) for s in samples]
    except ValueError:
        return None
    low, high = min(values), max(values)
    if (high - low).days < 30:
        low, high = low - timedelta(days=180), high + timedelta(days=180)
    return low.replace(tzinfo=None), high.replace(tzinfo=None)


_BIRTH_WORDS = {"dob", "birth", "birthday", "birthdate"}


def _adult_birth_span():
    """Dates of birth of people aged 18 to 75."""
    now = datetime.now().replace(microsecond=0)
    return now - timedelta(days=365 * 75), now - timedelta(days=365 * 18 + 5)


def _infer_from_name(tokens, words, joined, is_id):
    """(kind, params) from a field's name alone, or None."""
    def has(*names):
        return bool(words & set(names))

    if has("confirm", "repeat", "retype", "verify") and has("password", "pwd", "pass", "email"):
        return ("confirm", {})
    if has("email", "mail") or joined.endswith("email"):
        return ("email", {"domain": "example.com"})
    if has("password", "pwd", "passwd", "secret", "passphrase") or joined == "pass":
        return ("password", {})
    if has("uuid", "guid"):
        return ("uuid", {"upper": False})
    if has("phone", "mobile", "tel", "telephone", "cell", "fax", "msisdn"):
        return ("phone", {})
    if has("username", "login", "userid") or joined in ("user", "user_name", "uname", "login_name", "user_id_name"):
        return ("username", {})
    if has("first", "fname", "given", "forename", "firstname") or joined in ("middle_name",):
        return ("first_name", {})
    if has("last", "surname", "lname", "family", "lastname"):
        return ("last_name", {})
    if has("card", "pan") and has("number", "no", "num") or joined in ("card", "card_number", "cc_number"):
        return ("card_number", {})
    if has("cvv", "cvc", "cvv2", "csc"):
        return ("cvv", {})
    if has("expiry", "exp", "expiration") and not has("date", "at", "on"):
        return ("expiry", {})
    if has("otp", "pin", "passcode") or joined in ("verification_code", "auth_code"):
        return ("pin", {"digits": 6 if has("otp", "passcode", "verification", "auth") else 4})
    if words & _BIRTH_WORDS:
        return ("date", {"fmt": "%Y-%m-%d", "span": _adult_birth_span()})
    if has("timestamp", "datetime") or (has("at") and len(tokens) > 1) or has("time") and not has("zone"):
        return ("timestamp", {"fmt": "%Y-%m-%dT%H:%M:%SZ", "fraction": 0, "span": None})
    if has("date", "day") or (has("on") and len(tokens) > 1):
        return ("date", {"fmt": "%Y-%m-%d", "span": None})
    if has("age"):
        return ("integer", {"low": 18, "high": 80})
    if has("year"):
        year = datetime.now().year
        return ("integer", {"low": year - 10, "high": year})
    if has("lat", "latitude"):
        return ("latitude", {"scale": 6})
    if has("lng", "lon", "longitude"):
        return ("longitude", {"scale": 6})
    if has("zip", "postal", "postcode", "pincode", "zipcode"):
        return ("postal_code", {})
    if has("country"):
        return ("country_code", {}) if has("code", "iso") else ("country", {})
    if has("state", "province", "region", "county"):
        return ("state", {})
    if has("city", "town"):
        return ("city", {})
    if has("street", "address", "address1", "address2", "addr", "line1", "line2"):
        return ("street", {})
    if has("company", "organization", "organisation", "org", "employer", "vendor", "merchant", "supplier",
           "business", "firm"):
        return ("company", {})
    if has("currency"):
        return ("currency", {})
    if has("url", "uri", "link", "website", "homepage", "callback"):
        return ("url", {})
    if has("ip") or joined.endswith("ip_address"):
        return ("ip", {})
    if has("gender", "sex"):
        return ("gender", {})
    if has("price", "amount", "total", "cost", "fee", "balance", "salary", "subtotal", "tax", "discount",
           "charge", "payment", "revenue", "income", "rate"):
        return ("decimal", {"low": 1.0, "high": 2500.0, "scale": 2})
    if has("qty", "quantity", "count", "units"):
        return ("integer", {"low": 1, "high": 10})
    if has("is", "has", "can", "should", "enabled", "active", "flag", "verified", "accept", "accepted",
           "agree", "agreed", "subscribed", "opt", "consent", "valid", "deleted", "visible") and tokens[0] in (
            "is", "has", "can", "should", "enabled", "active", "verified", "accept", "accepted", "agree",
            "agreed", "subscribed", "opt", "consent", "valid", "deleted", "visible", "allow", "flag"):
        return ("boolean", {"bool_style": "lower"})
    if has("job", "designation", "position", "occupation"):
        return ("job_title", {})
    if has("name"):
        if has("product", "item", "sku"):
            return ("product", {})
        if has("company", "org", "business", "bank", "brand", "vendor", "shop", "store"):
            return ("company", {})
        if has("file", "host", "domain", "server", "table", "field", "class", "method", "test", "event",
               "tag", "group", "team", "category", "project", "plan", "app", "service"):
            return ("word", {"case": "title"})
        # customerName, payerName, holderName, displayName, name ...: a person.
        return ("full_name", {})
    if has("product", "item"):
        return ("product", {})
    if has("description", "comment", "comments", "notes", "note", "remarks", "message", "text", "body",
           "summary", "reason", "feedback", "review", "bio", "content"):
        return ("text", {"words": 10})
    if has("title", "subject", "headline", "label"):
        return ("text", {"words": 4})
    if has("search", "query", "keyword", "keywords", "term", "q"):
        return ("product", {})
    if has("status", "state"):
        return ("choice", {"choices": ["ACTIVE", "PENDING", "INACTIVE"]})
    if has("role"):
        return ("choice", {"choices": ["user", "admin", "editor", "viewer"]})
    if has("color", "colour"):
        return ("choice", {"choices": ["red", "blue", "green", "black", "white", "silver"]})
    if has("lang", "language", "locale"):
        return ("choice", {"choices": ["en-US", "en-GB", "fr-FR", "de-DE", "es-ES"]})
    if is_id:
        if has("code", "ref", "reference", "sku"):
            prefix = next((t for t in tokens if t not in ("code", "ref", "reference", "no", "num", "number")),
                          tokens[0])
            return ("pattern", {"template": f"{prefix[:3].upper()}-100000", "unique": True})
        # userId, orderId, id: numeric, the most common shape of a path or key id.
        return ("sequence", {"start": 1001, "as_text": False})
    return None


# ─────────────────────────────────────────────────────────────────────────────
# 3. Generation
# ─────────────────────────────────────────────────────────────────────────────

_FIRST_NAMES = (
    "James", "Olivia", "Liam", "Emma", "Noah", "Ava", "Ethan", "Sophia", "Mason", "Isabella", "Lucas", "Mia",
    "Aiden", "Amelia", "Arjun", "Priya", "Rahul", "Ananya", "Wei", "Mei", "Hiroshi", "Yuki", "Carlos", "Lucia",
    "Mateo", "Valentina", "Omar", "Layla", "Daniel", "Grace", "Samuel", "Chloe", "Benjamin", "Zoe", "Elijah",
    "Nora", "Henry", "Hannah", "Jack", "Ella", "Ravi", "Kavya", "Felix", "Clara", "Leo", "Ruby",
)
_LAST_NAMES = (
    "Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller", "Davis", "Martinez", "Wilson",
    "Anderson", "Taylor", "Thomas", "Moore", "Jackson", "Martin", "Lee", "Thompson", "White", "Harris",
    "Clark", "Lewis", "Walker", "Hall", "Young", "King", "Wright", "Scott", "Green", "Baker", "Patel",
    "Sharma", "Reddy", "Nair", "Chen", "Wang", "Tanaka", "Sato", "Silva", "Rossi", "Muller", "Novak",
)
# city, state, state code, country, country code, postal code pattern, phone pattern, currency
_LOCATIONS = (
    ("New York", "New York", "NY", "United States", "US", "100##", "+1 212-555-####", "USD"),
    ("Austin", "Texas", "TX", "United States", "US", "787##", "+1 512-555-####", "USD"),
    ("Seattle", "Washington", "WA", "United States", "US", "981##", "+1 206-555-####", "USD"),
    ("Chicago", "Illinois", "IL", "United States", "US", "606##", "+1 312-555-####", "USD"),
    ("Denver", "Colorado", "CO", "United States", "US", "802##", "+1 303-555-####", "USD"),
    ("Toronto", "Ontario", "ON", "Canada", "CA", "M5V ?#?", "+1 416-555-####", "CAD"),
    ("Vancouver", "British Columbia", "BC", "Canada", "CA", "V6B ?#?", "+1 604-555-####", "CAD"),
    ("London", "England", "ENG", "United Kingdom", "GB", "SW1A #??", "+44 20 7946 0###", "GBP"),
    ("Manchester", "England", "ENG", "United Kingdom", "GB", "M1 #??", "+44 161 496 0###", "GBP"),
    ("Hyderabad", "Telangana", "TS", "India", "IN", "5000##", "+91 98###-#####", "INR"),
    ("Bengaluru", "Karnataka", "KA", "India", "IN", "5600##", "+91 97###-#####", "INR"),
    ("Sydney", "New South Wales", "NSW", "Australia", "AU", "2###", "+61 2 5550 ####", "AUD"),
    ("Berlin", "Berlin", "BE", "Germany", "DE", "10###", "+49 30 5550####", "EUR"),
    ("Paris", "Ile-de-France", "IDF", "France", "FR", "750##", "+33 1 55 50 ## ##", "EUR"),
)
_STREET_NAMES = ("Maple", "Oak", "Cedar", "Pine", "Elm", "Lake", "Hill", "Park", "Church", "Main", "River",
                 "Sunset", "Highland", "Willow", "Station", "Market", "Garden", "Mill", "Spring", "Forest")
_STREET_TYPES = ("Street", "Avenue", "Road", "Lane", "Drive", "Boulevard", "Court", "Way")
_COMPANY_WORDS = ("Northwind", "Bluepeak", "Silverline", "Greenfield", "Brightpath", "Ironwood", "Clearwater",
                  "Summit", "Redstone", "Evergreen", "Horizon", "Pinnacle", "Lakeside", "Oakridge", "Starlight")
_COMPANY_SUFFIXES = ("Inc", "LLC", "Ltd", "Group", "Solutions", "Technologies", "Partners", "Systems")
_PRODUCT_ADJECTIVES = ("Wireless", "Ergonomic", "Compact", "Premium", "Smart", "Portable", "Classic", "Ultra",
                       "Eco", "Deluxe")
_PRODUCT_NOUNS = ("Headphones", "Keyboard", "Backpack", "Water Bottle", "Desk Lamp", "Monitor", "Notebook",
                  "Running Shoes", "Coffee Maker", "Phone Case", "Office Chair", "Speaker", "Jacket", "Watch")
_JOB_TITLES = ("Software Engineer", "Product Manager", "QA Analyst", "Data Scientist", "Account Manager",
               "Business Analyst", "Designer", "Support Specialist", "Operations Lead", "Consultant")
_WORDS = ("order", "service", "account", "delivery", "update", "request", "review", "payment", "customer",
          "support", "quality", "system", "report", "product", "schedule", "invoice", "network", "project",
          "please", "confirm", "details", "standard", "priority", "available", "regular", "complete", "new",
          "team", "process", "online", "store", "plan", "item", "test", "performance", "release")
_CURRENCIES = ("USD", "EUR", "GBP", "INR", "CAD", "AUD", "JPY")
_GENDERS = ("Male", "Female", "Non-binary")
# Card number prefixes from the published test-card ranges, never real accounts.
_TEST_CARD_PREFIXES = ("411111", "400000", "555555", "222300", "378282")


class _Generator:
    def __init__(self, fields, seed):
        self.fields = fields
        self.rng = random.Random(seed)
        self.seen = {f["path"]: set() for f in fields}
        self.today = datetime.now().replace(microsecond=0)

    # ── per-record context: values that belong together ──
    def _context(self):
        rng = self.rng
        first, last = rng.choice(_FIRST_NAMES), rng.choice(_LAST_NAMES)
        return {
            "first": first, "last": last, "location": rng.choice(_LOCATIONS),
            "handle": f"{first}.{last}".lower(), "dates": {}, "values": {},
        }

    def record(self, index):
        ctx = self._context()
        flat = {}
        for field in self.fields:
            if field["optional"] and self.rng.random() < _OPTIONAL_BLANK_RATE and not field.get("unique"):
                flat[field["path"]] = None
                continue
            flat[field["path"]] = self.value(field, index, ctx)
        self._relate(flat, ctx)
        return flat

    def _unique(self, field, make, index, fallback=None):
        """A value of `make()` not produced before for this field; the record index breaks a tie."""
        seen = self.seen[field["path"]]
        value = make()
        attempts = 0
        while value in seen and attempts < 20:
            value = make()
            attempts += 1
        if value in seen:
            value = fallback() if fallback else f"{value}-{index + 1}"
        seen.add(value)
        return value

    def value(self, field, index, ctx):
        rng = self.rng
        kind = field["kind"]
        unique = field.get("unique")
        loc = ctx["location"]

        if kind == "first_name":
            return ctx["first"]
        if kind == "last_name":
            return ctx["last"]
        if kind == "full_name":
            return f"{ctx['first']} {ctx['last']}"
        if kind == "username":
            make = lambda: ctx["handle"].replace(".", rng.choice(("", ".", "_"))) + str(rng.randint(1, 9999))
            return self._unique(field, make, index) if unique else make()
        if kind == "email":
            domain = field.get("domain") or "example.com"
            make = lambda: f"{ctx['handle']}{rng.randint(1, 99999)}@{domain}"
            if not unique:
                return make()
            return self._unique(field, make, index, fallback=lambda: f"{ctx['handle']}.{index + 1}@{domain}")
        if kind == "password":
            body = "".join(rng.choice(string.ascii_letters + string.digits) for _ in range(9))
            return f"{rng.choice(string.ascii_uppercase)}{body}{rng.choice('!@#$%&*')}{rng.randint(0, 9)}"
        if kind == "confirm":
            return None  # filled in from its partner by _relate
        if kind == "phone":
            return _fill_pattern(loc[6], rng)
        if kind == "uuid":
            value = str(uuid.UUID(int=rng.getrandbits(128), version=4))
            return value.upper() if field.get("upper") else value
        if kind == "sequence":
            value = field.get("start", 1001) + index
            return str(value) if field.get("as_text") else value
        if kind == "pattern":
            if unique:
                return self._unique(field, lambda: _mimic_sequence(field["template"], index, rng), index)
            return _mimic(field["template"], rng, field.get("keep", 0))
        if kind == "integer":
            value = rng.randint(int(field["low"]), int(field["high"]))
            if unique:
                value = self._unique(field, lambda: rng.randint(int(field["low"]), int(field["high"]) + index), index)
            return str(value) if field.get("as_text") else value
        if kind == "decimal":
            value = _Fixed(rng.uniform(field["low"], field["high"]), field["scale"])
            return str(value) if field.get("as_text") else value
        if kind == "boolean":
            value = rng.random() < 0.5
            return value
        if kind == "choice":
            return rng.choice(field["choices"])
        if kind in ("date", "timestamp"):
            moment = self._moment(field)
            ctx["dates"][field["path"]] = moment
            return _format_moment(moment, field)
        if kind == "epoch":
            moment = self.today - timedelta(seconds=rng.randint(0, 365 * 86400))
            seconds = int(moment.replace(tzinfo=timezone.utc).timestamp())
            return seconds * 1000 + rng.randint(0, 999) if field.get("millis") else seconds
        if kind == "street":
            return f"{rng.randint(1, 9999)} {rng.choice(_STREET_NAMES)} {rng.choice(_STREET_TYPES)}"
        if kind == "city":
            return loc[0]
        if kind == "state":
            return loc[2] if field.get("samples") and all(len(str(s)) <= 3 for s in field["samples"] if s) else loc[1]
        if kind == "country":
            return loc[3]
        if kind == "country_code":
            return loc[4]
        if kind == "postal_code":
            return _fill_pattern(loc[5], rng)
        if kind == "currency":
            return loc[7]
        if kind == "company":
            return f"{rng.choice(_COMPANY_WORDS)} {rng.choice(_COMPANY_SUFFIXES)}"
        if kind == "url":
            return f"https://www.example.com/{rng.choice(_WORDS)}/{rng.randint(1000, 99999)}"
        if kind == "ip":
            return f"10.{rng.randint(0, 255)}.{rng.randint(0, 255)}.{rng.randint(1, 254)}"
        if kind == "gender":
            return rng.choice(_GENDERS)
        if kind == "product":
            return f"{rng.choice(_PRODUCT_ADJECTIVES)} {rng.choice(_PRODUCT_NOUNS)}"
        if kind == "job_title":
            return rng.choice(_JOB_TITLES)
        if kind == "text":
            count = field.get("words", 8)
            words = [rng.choice(_WORDS) for _ in range(count)]
            sentence = " ".join(words)
            return sentence[:1].upper() + sentence[1:] + ("." if count > 5 else "")
        if kind == "card_number":
            return _luhn_number(rng.choice(_TEST_CARD_PREFIXES), 16, rng)
        if kind == "cvv":
            return f"{rng.randint(0, 999):03d}"
        if kind == "expiry":
            moment = self.today + timedelta(days=rng.randint(60, 365 * 4))
            return moment.strftime("%m/%y")
        if kind == "pin":
            return "".join(rng.choice(string.digits) for _ in range(field.get("digits", 6)))
        if kind == "latitude":
            return round(rng.uniform(-60, 70), field.get("scale", 6))
        if kind == "longitude":
            return round(rng.uniform(-150, 150), field.get("scale", 6))
        # "word": free text with no better clue.
        word = f"{rng.choice(_WORDS)}_{rng.choice(_WORDS)}" if rng.random() < 0.5 else rng.choice(_WORDS)
        if unique:
            word = self._unique(field, lambda: f"{word}{rng.randint(100, 99999)}", index)
        return {"upper": word.upper(), "title": word.replace("_", " ").title()}.get(field.get("case"), word)

    def _moment(self, field):
        span = field.get("span")
        words = set(field.get("tokens", ()))
        if span:
            low, high = span
        elif words & {"expiry", "expiration", "expires", "due", "end", "until", "to", "valid", "checkout"}:
            low, high = self.today, self.today + timedelta(days=365)
        elif words & {"start", "from", "begin", "checkin", "departure", "scheduled"}:
            low, high = self.today - timedelta(days=30), self.today + timedelta(days=180)
        else:
            low, high = self.today - timedelta(days=730), self.today
        seconds = max(0, int((high - low).total_seconds()))
        return low + timedelta(seconds=self.rng.randint(0, seconds))

    # ── relationships between fields of one record ──
    def _relate(self, flat, ctx):
        by_tokens = {f["path"]: f for f in self.fields}
        for field in self.fields:
            path, tokens = field["path"], field.get("tokens", [])
            if field["kind"] == "confirm":
                partner = _partner(field, self.fields, lambda f: f["kind"] in ("password", "email", "pattern",
                                                                                 "word") and f is not field
                                   and set(f.get("tokens", [])) & {"password", "pwd", "pass", "email"})
                flat[path] = flat.get(partner["path"]) if partner else flat.get(path) or "Passw0rd!"
                continue
            words = set(tokens)
            if field["kind"] in ("date", "timestamp") and path in ctx["dates"] and flat.get(path) is not None:
                start = self._range_start(field, ctx)
                if start is not None and ctx["dates"][path] < start:
                    moment = start + timedelta(days=self.rng.randint(1, 30), seconds=self.rng.randint(0, 86399))
                    ctx["dates"][path] = moment
                    flat[path] = _format_moment(moment, field)
            if words & {"age"} and field["kind"] == "integer":
                birth = next((ctx["dates"][p] for p in ctx["dates"]
                              if set(by_tokens[p].get("tokens", [])) & {"dob", "birth", "birthday", "birthdate"}),
                             None)
                if birth is not None:
                    age = self.today.year - birth.year - ((self.today.month, self.today.day) < (birth.month, birth.day))
                    flat[path] = str(age) if field.get("as_text") else age
            if words & {"total", "subtotal", "line"} and field["kind"] == "decimal":
                siblings = [f for f in self.fields if _parent(f["path"]) == _parent(path) and f is not field]
                quantity = next((f for f in siblings if set(f.get("tokens", [])) & {"qty", "quantity", "units"}), None)
                price = next((f for f in siblings if set(f.get("tokens", [])) & {"price", "rate", "cost"}
                              and not set(f.get("tokens", [])) & {"total"}), None)
                if quantity and price and flat.get(quantity["path"]) is not None and flat.get(price["path"]) is not None:
                    try:
                        value = _Fixed(float(flat[quantity["path"]]) * float(flat[price["path"]]), field["scale"])
                        flat[path] = str(value) if field.get("as_text") else value
                    except (TypeError, ValueError):
                        pass

    def _range_start(self, field, ctx):
        """The moment a date field must not precede: its start / created partner in the same record."""
        pairs = (({"end", "to", "until", "checkout", "finish", "return", "arrival"},
                  {"start", "from", "begin", "checkin", "departure"}),
                 ({"updated", "modified", "closed", "completed", "resolved", "shipped", "delivered"},
                  {"created", "opened", "placed", "ordered", "submitted"}))
        tokens = field.get("tokens", [])
        for later, earlier in pairs:
            hits = set(tokens) & later
            if not hits:
                continue
            stem = [t for t in tokens if t not in later and t not in ("date", "time", "at", "on", "datetime")]
            for other in self.fields:
                if other is field or other["path"] not in ctx["dates"]:
                    continue
                other_tokens = other.get("tokens", [])
                other_stem = [t for t in other_tokens if t not in earlier
                              and t not in ("date", "time", "at", "on", "datetime")]
                if set(other_tokens) & earlier and other_stem == stem:
                    return ctx["dates"][other["path"]]
        return None


class _Fixed(float):
    """A decimal that is a number in JSON and keeps its scale as text: 165.0 -> "165.00" in CSV / XML."""

    def __new__(cls, value, scale):
        instance = super().__new__(cls, round(value, scale))
        instance.scale = scale
        return instance

    def __str__(self):
        return f"{float(self):.{self.scale}f}"


def _partner(field, fields, predicate):
    tokens = [t for t in field.get("tokens", []) if t not in ("confirm", "repeat", "retype", "verify")]
    for other in fields:
        if predicate(other) and [t for t in other.get("tokens", [])] == tokens:
            return other
    return next((f for f in fields if predicate(f)), None)


def _format_moment(moment, field):
    fmt = field.get("fmt") or "%Y-%m-%d"
    if "%z" in fmt:
        text = moment.replace(tzinfo=timezone.utc).isoformat(timespec="seconds")
        return text
    text = moment.strftime(fmt)
    fraction = field.get("fraction")
    if "%f" in fmt and fraction is not None:
        text = re.sub(r"\.(\d{6})", lambda m: "." + m.group(1)[:max(1, fraction)], text, count=1)
    return text


def _fill_pattern(pattern, rng):
    """'#' -> digit, '?' -> upper-case letter."""
    return "".join(rng.choice(string.digits) if c == "#" else rng.choice(string.ascii_uppercase) if c == "?"
                   else c for c in pattern)


def _mimic(template, rng, keep=0):
    """A new value shaped like `template`: digits, upper and lower case letters replaced in place
    (the first `keep` characters, a phone number's country / operator prefix, stay)."""
    template = str(template)
    out = [template[:keep]]
    for c in template[keep:]:
        if c.isdigit():
            out.append(rng.choice(string.digits))
        elif c.isupper():
            out.append(rng.choice(string.ascii_uppercase))
        elif c.islower():
            out.append(rng.choice(string.ascii_lowercase))
        else:
            out.append(c)
    return "".join(out)


def _mimic_sequence(template, index, rng):
    """`ORD-1001` -> ORD-1002, ORD-1003 ...: the last run of digits counts up from the sample."""
    template = str(template)
    match = None
    for match in re.finditer(r"\d+", template):
        pass
    if not match:
        return f"{template}-{index + 1}"
    digits = match.group(0)
    number = int(digits) + 1 + index
    return template[:match.start()] + str(number).zfill(len(digits)) + template[match.end():]


def _luhn_number(prefix, length, rng):
    digits = [int(c) for c in prefix] + [rng.randint(0, 9) for _ in range(length - len(prefix) - 1)]
    total = 0
    for position, digit in enumerate(reversed(digits)):
        if position % 2 == 0:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return "".join(map(str, digits)) + str((10 - total % 10) % 10)


def _mark_unique(field):
    kind = field["kind"]
    words = set(field.get("tokens", []))
    if kind in ("uuid", "sequence"):
        return True
    if kind in ("email", "username"):
        return True
    if kind == "pattern":
        return bool(field.get("unique"))
    return bool(words & {"id", "uuid", "guid", "key", "reference", "ref", "sku"}) and kind in ("integer", "word")


def prepare_fields(schema):
    """Type every field of an analysed schema; returns the fields in output order."""
    fields = schema["fields"]
    # CSV Data Set columns first, in the plan's order: JMeter reads them by position.
    fields = [f for f in fields if f.get("column")] + [f for f in fields if not f.get("column")]
    for field in fields:
        _infer(field)
        field["unique"] = _mark_unique(field)
        field["required"] = not field.get("optional")
    return fields


def describe_fields(fields, first_record=None):
    """The schema as shown in the preview: one row per field."""
    rows = []
    for field in fields:
        rows.append({
            "path": field["path"],
            "type": _TYPE_LABELS.get(field["kind"], "String") if field["kind"] != "confirm" else "String (confirmation)",
            "required": field["required"],
            "unique": field["unique"],
            "source": "; ".join(field["origins"][:3]) + (" ..." if len(field["origins"]) > 3 else ""),
            "example": "" if first_record is None or first_record.get(field["path"]) is None
            else str(first_record.get(field["path"]))[:80],
        })
    return rows


def generate_records(fields, count, seed=None):
    """`count` records as flat dicts keyed by field path."""
    generator = _Generator(fields, seed if seed is not None else random.randrange(1 << 30))
    return [generator.record(index) for index in range(count)]


# ─────────────────────────────────────────────────────────────────────────────
# 4. Serialisation
# ─────────────────────────────────────────────────────────────────────────────

def _nest(flat):
    """`{"a.b": 1, "a.c.0.d": 2}` -> `{"a": {"b": 1, "c": [{"d": 2}]}}`."""
    root = {}
    for path, value in flat.items():
        parts = path.split(".")
        node = root
        for position, part in enumerate(parts):
            last = position == len(parts) - 1
            following_is_index = not last and parts[position + 1].isdigit()
            if isinstance(node, list):
                slot = int(part)
                while len(node) <= slot:
                    node.append(None)
                if last:
                    node[slot] = value
                else:
                    if not isinstance(node[slot], (dict, list)):
                        node[slot] = [] if following_is_index else {}
                    node = node[slot]
            else:
                if last:
                    node[part] = value
                else:
                    if not isinstance(node.get(part), (dict, list)):
                        node[part] = [] if following_is_index else {}
                    node = node[part]
    return root


def _text(value):
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _flat_text(flat, fields):
    out = []
    for field in fields:
        value = flat.get(field["path"])
        if isinstance(value, bool) and field.get("bool_style") == "title":
            out.append("True" if value else "False")
        else:
            out.append(_text(value))
    return out


def _csv_columns(fields):
    return [f["path"] for f in fields]


def _xml_name(name, fallback="field"):
    name = re.sub(r"[^\w.\-]", "_", str(name or "").lstrip("@")) or fallback
    if not re.match(r"[A-Za-z_]", name) or name.lower().startswith("xml"):
        name = "_" + name
    return name


def _xml_tags(record_tag):
    record = _xml_name(record_tag or "record", "record")
    root = "records" if record == "record" else (record + "List" if record.endswith("s") else record + "s")
    return root, record


def _xml_fill(element, value):
    if isinstance(value, dict):
        for key, child in value.items():
            if key.startswith("@"):
                element.set(_xml_name(key), _text(child))
                continue
            _xml_fill(ET.SubElement(element, _xml_name(key)), child)
    elif isinstance(value, list):
        for item in value:
            _xml_fill(ET.SubElement(element, "item"), item)
    else:
        element.text = _text(value)


def _xml_records(records, record_tag):
    root_tag, record_name = _xml_tags(record_tag)
    root = ET.Element(root_tag)
    for flat in records:
        _xml_fill(ET.SubElement(root, record_name), _nest(flat))
    ET.indent(root, space="  ")
    return root


def serialize(records, fields, output_format, record_tag=""):
    """The full file content for `records` in `output_format`."""
    if output_format == "json":
        return json.dumps([_nest(r) for r in records], indent=2, ensure_ascii=False) + "\n"
    if output_format == "xml":
        root = _xml_records(records, record_tag)
        return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="unicode") + "\n"
    buffer = io.StringIO()
    writer = csv.writer(buffer, quoting=csv.QUOTE_MINIMAL, lineterminator="\n")
    writer.writerow(_csv_columns(fields))
    for record in records:
        writer.writerow(_flat_text(record, fields))
    return buffer.getvalue()


def validate(content, output_format, expected, fields, record_tag=""):
    """Parse generated content back; raise TestDataError unless it holds `expected` well-formed records."""
    try:
        if output_format == "json":
            data = json.loads(content)
            found = len(data) if isinstance(data, list) else -1
        elif output_format == "xml":
            root = ET.fromstring(content.encode("utf-8"))
            found = len(root.findall(_xml_tags(record_tag)[1]))
        else:
            rows = list(csv.reader(io.StringIO(content)))
            width = len(fields)
            if not rows or rows[0] != _csv_columns(fields) or any(len(row) != width for row in rows[1:]):
                raise TestDataError("The generated CSV has rows with a different number of columns than its header.")
            found = len(rows) - 1
    except (ValueError, ET.ParseError) as error:
        raise TestDataError(f"The generated {output_format.upper()} is not valid: {error}")
    if found != expected:
        raise TestDataError(f"The generated {output_format.upper()} holds {found} records instead of {expected}.")


def preview_page(records, fields, output_format, page, record_tag="", page_size=PREVIEW_PAGE_SIZE):
    """One page of the preview: text for JSON / XML, columns and rows for CSV."""
    total = len(records)
    pages = max(1, -(-total // page_size))
    page = max(1, min(int(page or 1), pages))
    chunk = records[(page - 1) * page_size: page * page_size]
    result = {"page": page, "pages": pages, "page_size": page_size, "first": (page - 1) * page_size + 1,
              "last": (page - 1) * page_size + len(chunk), "total": total}
    if output_format == "csv":
        result["columns"] = _csv_columns(fields)
        result["rows"] = [_flat_text(r, fields) for r in chunk]
    elif output_format == "json":
        result["text"] = json.dumps([_nest(r) for r in chunk], indent=2, ensure_ascii=False)
    else:
        result["text"] = ET.tostring(_xml_records(chunk, record_tag), encoding="unicode")
    return result


def download_name(title, script_file, output_format, moment=None):
    """`Login performance test` -> LoginPerformanceTest_20261103_153000.json"""
    base = title or Path(script_file or "test_data").stem
    words = re.findall(r"[A-Za-z0-9]+", base)
    stem = "".join(w[:1].upper() + w[1:] for w in words) or "TestData"
    stamp = (moment or datetime.now()).strftime("%Y%m%d_%H%M%S")
    return f"{stem[:80]}_{stamp}{FORMATS[output_format]}"


def build(script_path, tool, perf_dir, output_format, count, seed=None, progress=None):
    """
    Analyse, generate, serialise and validate in one go.

    Returns {"records", "fields", "content", "schema", "record_tag", "seed"}.
    `progress(message)` is told what stage is running.
    """
    progress = progress or (lambda message: None)
    progress("Analysing the script...")
    schema = analyze_script(script_path, tool, perf_dir)
    fields = prepare_fields(schema)
    seed = seed if seed is not None else random.randrange(1 << 30)
    progress(f"Generating {count:,} records for {len(fields)} field(s)...")
    try:
        records = generate_records(fields, count, seed)
    except Exception as error:
        raise TestDataError(f"The test data could not be generated: {error}")
    progress(f"Writing {output_format.upper()}...")
    content = serialize(records, fields, output_format, schema["record_tag"])
    progress("Validating the generated file...")
    validate(content, output_format, count, fields, schema["record_tag"])
    return {"records": records, "fields": fields, "content": content, "schema": schema,
            "record_tag": schema["record_tag"], "seed": seed}
