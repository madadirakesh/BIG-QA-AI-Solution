"""
jmeter_payload.py
-----------------
"Configure Payload" for JMeter test plans - the .jmx counterpart of
payload_parameterizer.py (which handles Locust scripts).

  * `extract_parameters()` lists what a payload can drive in a plan: the
    arguments of every HTTP sampler (form / query fields), the leaves of a raw
    JSON body, and the query string of the sampler path.
  * `extract_requests()` lists the samplers and transactions, for the
    response-time threshold rows.
  * `write_parameterized_plan()` writes `_bigqa_param_<stem>.jmx` next to the
    plan: each mapped value becomes `${bigqa_N}`, fed by a CSV Data Set that
    reads `_bigqa_param_<stem>.csv` (the mapped nodes of every payload record,
    whatever the payload format), and every sampler gets a Duration Assertion
    for its threshold. The runner executes that copy; the plan itself is never
    edited.

Parameter ids and request ids use the same shapes as the Locust side, so the
dialog, the validation and the stored configuration are shared.
"""

import csv
import json
import os
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import parse_qsl, quote, urlsplit

from utils import perf_project_layout as layout
from utils.payload_parameterizer import (
    GENERAL_THRESHOLD,
    GENERATED_PREFIX,
    MAX_LIST_NODES,
    MAX_NODE_DEPTH,
    PayloadError,
    SOURCE_LABELS,
    _sample_text,
    parameter_id,
    payload_records,
    validate_mappings,
    validate_thresholds,
)

VAR_PREFIX = "bigqa_"
_BODY_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
_SENTINEL = "__BIGQA_VALUE_{}__"


def generated_plan_name(script_file):
    return f"{GENERATED_PREFIX}{Path(script_file).stem}.jmx"


def generated_csv_name(script_file):
    return f"{GENERATED_PREFIX}{Path(script_file).stem}.csv"


# ─────────────────────────────────────────────────────────────────────────────
# Reading the plan
# ─────────────────────────────────────────────────────────────────────────────

def _parse(script_path):
    try:
        return ET.parse(script_path)
    except (ET.ParseError, OSError) as error:
        raise PayloadError(f"The test plan could not be read: {error}")


def _prop(element, name):
    for child in element:
        if child.get("name") == name:
            return child
    return None


def _prop_text(element, name):
    child = _prop(element, name)
    return (child.text or "") if child is not None else ""


def _pairs(hash_tree):
    """(element, its child hashTree) pairs of one hashTree level."""
    children = list(hash_tree)
    pairs = []
    for index, child in enumerate(children):
        if child.tag == "hashTree":
            continue
        following = children[index + 1] if index + 1 < len(children) else None
        pairs.append((child, following if following is not None and following.tag == "hashTree" else None))
    return pairs


def _walk_tree(hash_tree):
    if hash_tree.tag != "hashTree":
        # The <jmeterTestPlan> root: its tree starts at the hashTree inside it.
        for child in hash_tree.findall("hashTree"):
            yield from _walk_tree(child)
        return
    for element, sub in _pairs(hash_tree):
        yield element, sub, hash_tree
        if sub is not None:
            yield from _walk_tree(sub)


def _is_sampler(element):
    return element.tag.endswith("Sampler") or element.tag.endswith("SamplerProxy")


def _arguments(sampler):
    holder = _prop(sampler, "HTTPsampler.Arguments")
    collection = _prop(holder, "Arguments.arguments") if holder is not None else None
    return list(collection) if collection is not None else []


def _json_leaves(node, prefix, out, depth=0):
    if depth > MAX_NODE_DEPTH:
        return
    if isinstance(node, dict):
        for key, value in node.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            if isinstance(value, (dict, list)):
                _json_leaves(value, path, out, depth + 1)
            else:
                out.append((path, value))
    elif isinstance(node, list):
        for index, value in enumerate(node[:MAX_LIST_NODES]):
            path = f"{prefix}.{index}" if prefix else str(index)
            if isinstance(value, (dict, list)):
                _json_leaves(value, path, out, depth + 1)
            else:
                out.append((path, value))


def _variable(value):
    return isinstance(value, str) and "${" in value


def _collect(root):
    """Ordered dict of parameter id -> {public fields..., occurrences: [(kind, element, extra)]}."""
    parameters = {}

    def record(source, method, request, path, sample, occurrence):
        key = parameter_id(source, method, request, path)
        entry = parameters.setdefault(key, {
            "id": key, "name": path.split(".")[-1], "path": path, "source": source,
            "method": method, "request": request, "sample": _sample_text(sample), "occurrences": [],
        })
        entry["occurrences"].append(occurrence)

    for element, _sub, _parent in _walk_tree(root):
        if element.tag != "HTTPSamplerProxy" or element.get("enabled") == "false":
            continue
        method = (_prop_text(element, "HTTPSampler.method") or "GET").upper()
        request = element.get("testname") or _prop_text(element, "HTTPSampler.path") or "request"
        raw_body = _prop_text(element, "HTTPSampler.postBodyRaw").strip().lower() == "true"
        for argument in _arguments(element):
            value_prop = _prop(argument, "Argument.value")
            value = (value_prop.text or "") if value_prop is not None else ""
            if raw_body:
                try:
                    body = json.loads(value)
                except ValueError:
                    continue
                leaves = []
                _json_leaves(body, "", leaves)
                for path, leaf in leaves:
                    if not _variable(leaf):
                        record("body", method, request, path, leaf, ("json", argument, path))
                continue
            name = _prop_text(argument, "Argument.name") or argument.get("name") or ""
            if name and not _variable(value):
                source = "body" if method in _BODY_METHODS else "query"
                record(source, method, request, name, value, ("argument", argument, name))
        path_value = _prop_text(element, "HTTPSampler.path")
        if "?" in path_value:
            seen = set()
            for key, value in parse_qsl(urlsplit(path_value).query, keep_blank_values=True):
                if key in seen or _variable(value):
                    continue
                seen.add(key)
                record("query", method, request, key, value, ("query", element, key))
    return parameters


def extract_parameters(script_path):
    parameters = _collect(_parse(script_path).getroot())
    return [{
        "id": p["id"], "name": p["name"], "path": p["path"], "source": p["source"],
        "source_label": SOURCE_LABELS.get(p["source"], p["source"]), "method": p["method"],
        "request": p["request"], "sample": p["sample"], "occurrences": len(p["occurrences"]),
    } for p in parameters.values()]


def _request_method(element):
    if element.tag == "HTTPSamplerProxy":
        return (_prop_text(element, "HTTPSampler.method") or "GET").upper()
    if element.tag == "TransactionController":
        return "TX"
    return element.tag.replace("SamplerProxy", "").replace("Sampler", "").upper() or "SAMPLER"


def _timed(element):
    return (_is_sampler(element) or element.tag == "TransactionController") and element.get("enabled") != "false"


def extract_requests(script_path):
    """The plan's samplers and transactions; the id is the label JMeter reports them under."""
    requests = {}
    for element, _sub, _parent in _walk_tree(_parse(script_path).getroot()):
        if not _timed(element):
            continue
        label = element.get("testname") or element.tag
        entry = requests.get(label)
        if entry:
            entry["occurrences"] += 1
        else:
            requests[label] = {"id": label, "method": _request_method(element), "name": label, "occurrences": 1}
    return list(requests.values())


# ─────────────────────────────────────────────────────────────────────────────
# Writing the parameterised copy
# ─────────────────────────────────────────────────────────────────────────────

def _string_prop(name, value):
    element = ET.Element("stringProp", {"name": name})
    element.text = str(value)
    return element


def _bool_prop(name, value):
    element = ET.Element("boolProp", {"name": name})
    element.text = "true" if value else "false"
    return element


def _csv_data_set(file_name, variables):
    element = ET.Element("CSVDataSet", {"guiclass": "TestBeanGUI", "testclass": "CSVDataSet",
                                        "testname": "BIG QA payload", "enabled": "true"})
    for child in (
        _string_prop("filename", file_name), _string_prop("fileEncoding", "UTF-8"),
        _string_prop("variableNames", ",".join(variables)), _bool_prop("ignoreFirstLine", True),
        _string_prop("delimiter", ","), _bool_prop("quotedData", True), _bool_prop("recycle", True),
        _bool_prop("stopThread", False), _string_prop("shareMode", "shareMode.all"),
    ):
        element.append(child)
    return element


def _duration_assertion(limit_ms):
    element = ET.Element("DurationAssertion", {"guiclass": "DurationAssertionGui", "testclass": "DurationAssertion",
                                               "testname": "BIG QA response-time threshold", "enabled": "true"})
    element.append(_string_prop("DurationAssertion.duration", int(round(limit_ms))))
    return element


def _set_json_leaf(body, path, value):
    parts = path.split(".")
    current = body
    for part in parts[:-1]:
        current = current[int(part)] if isinstance(current, list) else current[part]
    last = parts[-1]
    if isinstance(current, list):
        current[int(last)] = value
    else:
        current[last] = value


def _rewrite_json(text, replacements):
    """Swap JSON leaves for `${var}` - quoted when the recorded value was a string."""
    body = json.loads(text)
    originals = {}
    for index, (path, variable) in enumerate(replacements):
        leaves = dict(_flat_leaves(body))
        originals[index] = (leaves.get(path), variable)
        _set_json_leaf(body, path, _SENTINEL.format(index))
    rendered = json.dumps(body, indent=2, ensure_ascii=False)
    for index, (original, variable) in originals.items():
        token = json.dumps(_SENTINEL.format(index))
        rendered = rendered.replace(token, f'"${{{variable}}}"' if isinstance(original, str) else f"${{{variable}}}")
    return rendered


def _flat_leaves(body):
    out = []
    _json_leaves(body, "", out)
    return out


def _rewrite_query(path_value, replacements):
    base, _, query = path_value.partition("?")
    pairs = parse_qsl(query, keep_blank_values=True)
    rendered = []
    for key, value in pairs:
        if key in replacements:
            rendered.append(f"{quote(key)}=${{__urlencode(${{{replacements[key]}}})}}")
        else:
            rendered.append(f"{quote(key)}={quote(value, safe='${}()_,')}")
    return f"{base}?{'&'.join(rendered)}"


def build_parameterized_plan(script_path, mappings, csv_name, thresholds=None):
    """Return (plan XML text, [(variable, node)]) for the parameterised copy."""
    tree = _parse(script_path)
    root = tree.getroot()
    parameters = _collect(root)
    clean_thresholds, threshold_errors = validate_thresholds(thresholds, extract_requests(script_path))
    if threshold_errors:
        raise PayloadError(" ".join(dict.fromkeys(threshold_errors)))
    clean = []
    if mappings:
        clean, errors, _ = validate_mappings(mappings, {k: v for k, v in parameters.items()})
        if errors:
            raise PayloadError(" ".join(dict.fromkeys(errors)))
    if not clean and not clean_thresholds:
        raise PayloadError("Map at least one script parameter to a payload node, "
                           "or set at least one response-time threshold.")

    variables = []
    json_edits, query_edits = {}, {}
    for index, mapping in enumerate(clean, start=1):
        variable = f"{VAR_PREFIX}{index}"
        variables.append((variable, mapping["node"]))
        for kind, element, extra in parameters[mapping["parameter"]]["occurrences"]:
            if kind == "argument":
                value = _prop(element, "Argument.value")
                if value is None:
                    value = _string_prop("Argument.value", "")
                    element.append(value)
                value.text = f"${{{variable}}}"
            elif kind == "json":
                json_edits.setdefault(id(element), (element, []))[1].append((extra, variable))
            else:
                query_edits.setdefault(id(element), (element, {}))[1][extra] = variable

    for element, replacements in json_edits.values():
        value = _prop(element, "Argument.value")
        value.text = _rewrite_json(value.text or "", replacements)
    for element, replacements in query_edits.values():
        path_prop = _prop(element, "HTTPSampler.path")
        path_prop.text = _rewrite_query(path_prop.text or "", replacements)

    if clean_thresholds:
        general = 0.0
        limits = {}
        for entry in clean_thresholds:
            if entry["request"] == GENERAL_THRESHOLD:
                general = float(entry["seconds"]) * 1000
            else:
                limits[entry["request"]] = float(entry["seconds"]) * 1000
        for element, sub, parent in list(_walk_tree(root)):
            if not _timed(element):
                continue
            limit = limits.get(element.get("testname") or element.tag, general)
            if not limit:
                continue
            if sub is None:
                sub = ET.Element("hashTree")
                children = list(parent)
                parent.insert(children.index(element) + 1, sub)
            sub.append(_duration_assertion(limit))
            sub.append(ET.Element("hashTree"))

    if variables:
        # The CSV Data Set sits at test-plan level, so every thread group reads it.
        plan_tree = root.find("hashTree")
        test_plan_tree = None
        if plan_tree is not None:
            for element, sub in _pairs(plan_tree):
                if element.tag == "TestPlan":
                    test_plan_tree = sub
                    break
        if test_plan_tree is None:
            raise PayloadError("The test plan has no TestPlan element, so a payload cannot be attached to it.")
        test_plan_tree.insert(0, ET.Element("hashTree"))
        test_plan_tree.insert(0, _csv_data_set(csv_name, [v for v, _ in variables]))

    text = '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="unicode")
    return text, variables


def write_parameterized_plan(perf_dir, script_file, script_path, mappings, raw=None, payload_type="",
                             thresholds=None):
    """
    Write `_bigqa_param_<stem>.jmx` (and its CSV when a payload is mapped) next
    to the plan. Returns (absolute path, file name).
    """
    folder = os.path.dirname(os.path.abspath(script_path))
    csv_name = generated_csv_name(script_file)
    text, variables = build_parameterized_plan(script_path, mappings, csv_name, thresholds)
    if variables:
        if raw is None:
            raise PayloadError("The payload file for this configuration is missing. Upload it again.")
        records = payload_records(raw, payload_type)
        with open(os.path.join(folder, csv_name), "w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle, quoting=csv.QUOTE_MINIMAL)
            writer.writerow([v for v, _ in variables])
            for record in records:
                writer.writerow(["" if record.get(node) is None else str(record.get(node))
                                 for _, node in variables])
    target = os.path.join(folder, generated_plan_name(script_file))
    with open(target, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    return target, os.path.basename(target)


def remove_generated_plan(perf_dir, script_file):
    """Delete the parameterised copy of a plan and its CSV; True when anything was removed."""
    removed = False
    names = {generated_plan_name(script_file), generated_csv_name(script_file)}
    for current, files in layout.walk(perf_dir):
        for name in names & set(files):
            try:
                os.remove(os.path.join(current, name))
                removed = True
            except OSError:
                pass
    return removed
