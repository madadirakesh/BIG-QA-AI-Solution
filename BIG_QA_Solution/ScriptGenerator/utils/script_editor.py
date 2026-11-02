"""
script_editor.py
----------------
Back end of the Edit Script dialog on the Performance Test page: reading and
writing a script's text, checking it still parses, describing what a save
changed, and turning a free-form instruction ("send this body as JSON", "add a
logout step", "set an Authorization header") into an edit made by the AI.

The AI does not send the script back. The configured providers cap a reply at
about 4096 tokens, which a recorded JMeter plan exceeds many times over, so the
AI answers with find/replace edits against the script it was shown and those
are applied here. A reply whose edits do not apply, or that leaves the script
unparseable, is sent back to the AI once with the reason.

A prompt can carry CSV / JSON / XML data files ("use the User column of the
attached file as the username"). The AI is shown each file's columns or nodes,
a few sample records and the path the script must read it from; the file
itself is written into the project's data folder when the script is saved.
"""

import ast
import codecs
import csv
import difflib
import io
import json
import os
import re
import xml.etree.ElementTree as ET
from pathlib import Path

from utils import payload_parameterizer
from utils.perf_project_layout import JMETER_TOOL
from utils.payload_parameterizer import PayloadError

# Bigger than any recorded plan seen so far, small enough to stay well inside
# every provider's input window together with the instructions.
MAX_AI_SCRIPT_CHARS = 400_000

ATTACHMENT_TYPES = {".csv": "csv", ".json": "json", ".xml": "xml"}
MAX_ATTACHMENTS = 5
MAX_ATTACHMENT_BYTES = payload_parameterizer.MAX_PAYLOAD_BYTES
# What the AI sees of a data file: enough to know its shape, not the data set.
_SAMPLE_RECORDS = 3
_MAX_LISTED_NODES = 150
_UNSAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


class ScriptEditError(Exception):
    """A script could not be read, edited or written; the message is shown to the tester."""


# ─────────────────────────────── File access ────────────────────────────────

def read_script(path):
    """
    Return (text with '\\n' line endings, newline the file uses).

    The editor works in '\\n'; the file's own line ending is put back on write
    so a save does not rewrite every line of a CRLF script.
    """
    with open(path, "rb") as fh:
        raw = fh.read()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as e:
        raise ScriptEditError(
            f"{os.path.basename(path)} is not UTF-8 text, so it cannot be edited here."
        ) from e
    newline = "\r\n" if "\r\n" in text else "\n"
    return text.replace("\r\n", "\n"), newline


def write_script(path, content, newline="\n"):
    text = normalize(content)
    if newline != "\n":
        text = text.replace("\n", newline)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)


def normalize(content):
    return (content or "").replace("\r\n", "\n").replace("\r", "\n")


# ──────────────────────────────── Validation ────────────────────────────────

def validate(file_name, content):
    """Return '' when the script parses for its type, else a one-line reason."""
    extension = os.path.splitext(file_name or "")[1].lower()
    if not (content or "").strip():
        return "The script is empty."
    if extension == ".py":
        try:
            ast.parse(content, filename=file_name)
        except SyntaxError as e:
            return f"Python syntax error on line {e.lineno}: {e.msg}"
    elif extension == ".jmx":
        try:
            ET.fromstring(content.encode("utf-8"))
        except ET.ParseError as e:
            line, column = getattr(e, "position", (None, None))
            where = f" on line {line}, column {column}" if line else ""
            return f"The test plan is not well-formed XML{where}: {e}"
    return ""


# ──────────────────────────────── Change log ────────────────────────────────

def describe_changes(old, new):
    """A short description of a save that the tester did not describe."""
    added = removed = 0
    for line in difflib.unified_diff(normalize(old).split("\n"), normalize(new).split("\n"), lineterm="", n=0):
        if line.startswith("+") and not line.startswith("+++"):
            added += 1
        elif line.startswith("-") and not line.startswith("---"):
            removed += 1
    if not added and not removed:
        return "No content changes"
    return f"Manual edit: {added} line(s) added, {removed} line(s) removed"


# ─────────────────────────────── Data files ─────────────────────────────────

def data_file_name(script_file, file_name, extension):
    """
    The name a prompt's data file is stored under: `<script>__<file><ext>`.

    Deliberately without the payload configuration's `bigqa_` prefix, whose
    files that feature deletes and rewrites as its own.
    """
    script_stem = _UNSAFE_NAME.sub("_", Path(script_file or "script").stem).strip("._") or "script"
    stem = _UNSAFE_NAME.sub("_", Path(file_name or "data").stem).strip("._") or "data"
    return f"{script_stem}__{stem}{extension}"


def prepare_attachment(raw, file_name, script_file, tool=""):
    """
    Parse an uploaded data file for a prompt.

    Returns {file_name, payload_type, stored_name, content (bytes to write),
    nodes, row_count, record_tag, sample}. JMeter reads test data through a
    CSV Data Set Config, which cannot walk JSON or XML, so for a JMeter plan
    those are flattened into a CSV whose columns are the node paths - the same
    conversion the Payload Configuration dialog uses.
    """
    extension = Path(file_name or "").suffix.lower()
    payload_type = ATTACHMENT_TYPES.get(extension)
    if not payload_type:
        raise ScriptEditError("Attach a .csv, .json or .xml file.")
    if not raw:
        raise ScriptEditError(f"{file_name} is empty.")
    if len(raw) > MAX_ATTACHMENT_BYTES:
        raise ScriptEditError(f"{file_name} is larger than {MAX_ATTACHMENT_BYTES // (1024 * 1024)} MB.")
    if raw.startswith(codecs.BOM_UTF8):
        raw = raw[len(codecs.BOM_UTF8):]
    try:
        parsed = payload_parameterizer.parse_payload(raw, payload_type, file_name)
        records = payload_parameterizer.payload_records(raw, payload_type)
    except PayloadError as e:
        raise ScriptEditError(f"{file_name}: {e}") from e

    content, stored_type = raw, payload_type
    if tool == JMETER_TOOL and payload_type != "csv":
        buffer = io.StringIO()
        writer = csv.writer(buffer, quoting=csv.QUOTE_MINIMAL, lineterminator="\n")
        writer.writerow(parsed["nodes"])
        for record in records:
            writer.writerow(["" if record.get(node) is None else str(record.get(node)) for node in parsed["nodes"]])
        content, stored_type = buffer.getvalue().encode("utf-8"), "csv"

    return {
        "file_name": os.path.basename(file_name),
        "payload_type": payload_type,
        "stored_type": stored_type,
        "stored_name": data_file_name(script_file, file_name, "." + stored_type),
        "content": content,
        "nodes": parsed["nodes"],
        "row_count": parsed["row_count"],
        "record_tag": parsed.get("record_tag") or "",
        "sample": records[:_SAMPLE_RECORDS],
    }


def store_attachment(data_dir, attachment):
    """Write a data file into the project's data folder; returns its path."""
    os.makedirs(data_dir, exist_ok=True)
    target = os.path.join(data_dir, attachment["stored_name"])
    with open(target, "wb") as fh:
        fh.write(attachment["content"])
    return target


def _describe_attachment(attachment):
    nodes = attachment["nodes"]
    listed = ", ".join(nodes[:_MAX_LISTED_NODES]) + (" ..." if len(nodes) > _MAX_LISTED_NODES else "")
    if attachment["stored_type"] != attachment["payload_type"]:
        shape = (f"Uploaded as {attachment['payload_type'].upper()}, provided to the script as a CSV with a header "
                 f"row whose columns are the node paths below (one row per record).")
    elif attachment["payload_type"] == "csv":
        shape = "CSV with a header row."
    elif attachment["payload_type"] == "json":
        shape = "JSON; the records are the objects of its top-level list (or of the list it holds)."
    else:
        shape = f"XML; each <{attachment['record_tag'] or 'record'}> element is one record."
    sample = json.dumps(attachment["sample"], ensure_ascii=False)
    if len(sample) > 3000:
        sample = sample[:3000] + " ..."
    return (f"- Name given by the tester: {attachment['file_name']}\n"
            f"  Path to read it from, relative to the script's own folder: {attachment['relative_path']}\n"
            f"  Format: {shape}\n"
            f"  Records: {attachment['row_count']}\n"
            f"  Columns / nodes (dotted paths are nested JSON/XML nodes): {listed}\n"
            f"  First records, flattened: {sample}")


_DATA_FILE_GUIDANCE = {
    ".py": """Reading data files in this Python script:
- Resolve the path from the script's folder, never the working directory:
  os.path.join(os.path.dirname(os.path.abspath(__file__)), "<relative path>").
- Load the file once at module level (csv.DictReader / json.load / xml.etree.ElementTree), not per request.
- Hand records to users with itertools.cycle (or an index modulo the record count) so every user gets the next record;
  take a record in on_start for per-user data (e.g. credentials) or per task for per-iteration data, as the request says.
- Nested JSON/XML nodes are addressed by their path in the original structure (e.g. user.name -> record["user"]["name"]).""",
    ".jmx": """Reading data files in this JMeter plan:
- Add a CSV Data Set Config (testclass="CSVDataSet", guiclass="TestBeanGUI") with filename set to the relative path
  (JMeter resolves it against the plan's folder), ignoreFirstLine=true, variableNames listing every column in file order
  (use simple names without dots, e.g. user_name for user.name), delimiter ",", recycle=true, stopThread=false,
  shareMode="shareMode.all", and quotedData=true.
- Place it in the thread group's hashTree before the samplers (followed by its own <hashTree/>), then use ${variable}
  wherever the value is needed.""",
}


# ──────────────────────────────── AI editing ────────────────────────────────

_TOOL_GUIDANCE = {
    ".py": """This is a Python performance script (Locust, or a Selenium journey run through Locust).
- Keep the existing imports, the User / TaskSet classes and the order of the steps unless asked otherwise.
- Requests go through self.client (get/post/put/patch/delete). Give every request a stable name= so results group correctly.
- Headers: pass headers={...} on the request, or set shared ones once in on_start with self.client.headers.update({...}).
- Payloads: JSON bodies use json={...}; form bodies use data={...}. Test data may come from a CSV/JSON file read once at module level and cycled per user.
- Checks: use `with self.client.<method>(..., catch_response=True) as response:` and call response.failure(...) / response.success().
- Correlation: read values from a response (response.json(), regex) and store them on self for later steps.
- Think time: use wait_time = between(a, b) or constant(n) on the User class.
- Add any import the change needs at the top of the file.""",
    ".jmx": """This is an Apache JMeter test plan (.jmx XML).
- Every element inside a <hashTree> is immediately followed by its own <hashTree> (empty: <hashTree/>) holding its children. Keep that pairing intact when adding or removing elements.
- Headers: an HeaderManager (guiclass="HeaderPanel") with a collectionProp name="HeaderManager.headers" of elementProp entries (Header.name / Header.value), placed in the hashTree of the sampler (one request) or of the thread group (all requests).
- Raw bodies (JSON/XML): on the HTTPSamplerProxy set <boolProp name="HTTPSampler.postBodyRaw">true</boolProp> and put the body in the Arguments collection as a single HTTPArgument with an empty name and the body as Argument.value (XML-escaped).
- Test data: CSV Data Set Config (guiclass="TestBeanGUI" testclass="CSVDataSet") with filename, variableNames, delimiter, recycle, stopThread, shareMode; reference values as ${variable}.
- Correlation: JSONPostProcessor / RegexExtractor in the sampler's hashTree; checks: ResponseAssertion / DurationAssertion.
- Think time: ConstantTimer / UniformRandomTimer. Loops and conditions: LoopController, IfController, TransactionController.
- Give new elements a descriptive testname and enabled="true". Escape &, < and > inside attribute and text values.""",
}


def _guidance(file_name):
    return _TOOL_GUIDANCE.get(os.path.splitext(file_name or "")[1].lower(),
                              "Keep the script's existing structure and conventions.")


def build_prompt(instruction, content, context, feedback=""):
    """The prompt sent to the AI for one change request."""
    retry = ""
    if feedback:
        retry = (
            "\nYOUR PREVIOUS ANSWER COULD NOT BE APPLIED:\n"
            f"{feedback}\n"
            "Answer again. Copy every `find` text character for character from the script below.\n"
        )
    data_files = ""
    attachments = context.get("attachments") or []
    if attachments:
        extension = os.path.splitext(context.get("file_name") or "")[1].lower()
        data_files = (
            "\nData files attached to the request (they will be saved at the given paths with the script):\n"
            + "\n".join(_describe_attachment(a) for a in attachments)
            + "\n" + _DATA_FILE_GUIDANCE.get(extension, "")
            + "\nWhen the request maps a script field to a column or node, replace the field's hard-coded value"
              " with the value from the current record. Match column/node names case-insensitively if needed.\n"
        )
    return f"""You are a senior performance test engineer editing an existing performance test script.

Tool: {context.get('tool') or 'unknown'}
Script type: {context.get('script_type') or 'unknown'}
File: {context.get('file_name')}
Application URL: {context.get('application_url') or 'not configured'}

{_guidance(context.get('file_name'))}

The tester's request:
<<<
{instruction}
>>>
{data_files}{retry}
Make exactly the change the tester asked for, completely and correctly, and nothing else. Do not reformat or
reorder untouched code. If the request is ambiguous, pick the most sensible interpretation for a performance test
and say what you assumed in the summary. If the request cannot be done on this script, return no edits and explain
why in the summary.

Answer with a JSON object only:
{{
  "summary": "One or two sentences, past tense, describing what was changed (used as the version note).",
  "edits": [
    {{"find": "exact text copied from the script", "replace": "the text that replaces it", "all": false}}
  ]
}}

Rules for edits:
- `find` must be copied verbatim from the script (same indentation and spacing) and must match exactly one place
  unless "all" is true. Include enough surrounding lines to make it unique, but keep it short.
- To insert new code, `find` an anchor (for example the line before the insertion point) and `replace` it with the
  anchor followed by the new code.
- To delete code, `replace` with an empty string.
- Edits are applied in order, each to the result of the previous one. Edits must not overlap.
- Use "\\n" for new lines inside the JSON strings.

The current script:
<<<SCRIPT
{content}
SCRIPT>>>
"""


def parse_ai_reply(text):
    """Return (summary, edits) from the AI's reply."""
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ScriptEditError("The AI reply did not contain any edits.")
    try:
        data = json.loads(text[start:end + 1], strict=False)
    except ValueError as e:
        raise ScriptEditError(f"The AI reply could not be read as JSON: {e}") from e
    edits = data.get("edits") or []
    if not isinstance(edits, list):
        raise ScriptEditError("The AI reply listed its edits in an unexpected format.")
    cleaned = []
    for edit in edits:
        if not isinstance(edit, dict) or not isinstance(edit.get("find"), str) or not edit["find"]:
            raise ScriptEditError("The AI reply contained an edit without the text to find.")
        cleaned.append({
            "find": normalize(edit["find"]),
            "replace": normalize(edit.get("replace") if isinstance(edit.get("replace"), str) else ""),
            "all": bool(edit.get("all")),
        })
    return str(data.get("summary") or "").strip(), cleaned


def _loose_match(content, find):
    """
    (start, end) of the lines of `content` that equal the lines of `find` once
    surrounding whitespace is ignored, or None. Covers the AI re-indenting
    the text it copied, which is its most common slip.
    """
    want = [line.strip() for line in find.strip("\n").split("\n")]
    if not any(want):
        return None
    lines = content.split("\n")
    offsets, position = [], 0
    for line in lines:
        offsets.append(position)
        position += len(line) + 1
    matches = []
    for i in range(len(lines) - len(want) + 1):
        if all(lines[i + j].strip() == want[j] for j in range(len(want))):
            matches.append(i)
    if len(matches) != 1:
        return None
    first, last = matches[0], matches[0] + len(want) - 1
    return offsets[first], offsets[last] + len(lines[last])


def apply_edits(content, edits):
    """Apply the AI's edits in order; raise ScriptEditError naming the edit that does not fit."""
    result = content
    for number, edit in enumerate(edits, start=1):
        find, replace = edit["find"], edit["replace"]
        count = result.count(find)
        if count and (count == 1 or edit["all"]):
            result = result.replace(find, replace) if edit["all"] else result.replace(find, replace, 1)
            continue
        if count > 1:
            raise ScriptEditError(
                f"Edit {number}: the text to find appears {count} times; include more surrounding lines "
                f"so it matches one place:\n{find[:300]}"
            )
        span = _loose_match(result, find)
        if not span:
            raise ScriptEditError(f"Edit {number}: this text was not found in the script:\n{find[:300]}")
        # The matched span is whole lines, so the replacement takes their place
        # without the surrounding blank lines the AI may have wrapped it in.
        start, end = span
        result = result[:start] + replace.strip("\n") + result[end:]
    return result


def ai_edit(instruction, content, context, ai_call, attempts=2):
    """
    Return (new content, summary) for one change request.

    `ai_call(prompt) -> str` is the configured provider. A reply that does not
    apply cleanly, or breaks the script, is retried once with the reason; a
    reply with no edits is the AI declining and is reported as it is.
    """
    content = normalize(content)
    if len(content) > MAX_AI_SCRIPT_CHARS:
        raise ScriptEditError(
            f"The script is {len(content):,} characters long - too large to send to the AI "
            f"(limit {MAX_AI_SCRIPT_CHARS:,}). Edit it by hand instead."
        )
    feedback = ""
    for attempt in range(1, attempts + 1):
        summary, edits = parse_ai_reply(ai_call(build_prompt(instruction, content, context, feedback)))
        if not edits:
            raise ScriptEditError(summary or "The AI did not suggest any change for this request.")
        try:
            updated = apply_edits(content, edits)
            if updated == content:
                raise ScriptEditError("The AI's edits did not change the script.")
            problem = validate(context.get("file_name"), updated)
            if problem:
                raise ScriptEditError(f"The edited script no longer parses. {problem}")
            return updated, summary or "Updated the script with AI."
        except ScriptEditError as e:
            if attempt >= attempts:
                raise
            feedback = str(e)
    raise ScriptEditError("The AI could not make this change.")
