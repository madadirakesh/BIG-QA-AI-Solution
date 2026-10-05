"""
api_script_generator.py
-----------------------
Turn an API document into a runnable Locust or JMeter API test script.

Supported documents:

  * OpenAPI 3 / Swagger 2   (.json, .yaml, .yml - or a .txt holding either)
  * Postman collection v2.x (.json)
  * Free-form API documents (.txt, .md, .docx, .doc) - the endpoints are
    extracted by the configured AI provider into the same structure the
    structured parsers produce.

Every document is reduced to one normalised shape before rendering:

    {"base_path": "/v1", "auth": {"type": "bearer" | "apikey" | "basic" | "", "name": ...},
     "endpoints": [{"name", "method", "path", "query", "headers", "body_type", "body"}]}

so the renderers never care where an endpoint came from, and the AI only ever
has to fill in data - the code itself is always generated deterministically.

Scripts are written into the framework's own folders:
  Locust -> locustfiles/<name>.py        (one sequential `api_journey` task)
  JMeter -> TestScripts/api/<name>.jmx   (one sampler per endpoint)

Both carry a `Test Case:` title and a `Script Type: API` marker, which is what
the Performance Test grid reads back for its Test Case and Type columns.
"""

import io
import json
import os
import re
import zipfile
from datetime import datetime
from pprint import pformat
from urllib.parse import parse_qsl, urlencode, urlsplit
from xml.sax.saxutils import escape as _xml_escape

LOCUST_TOOL = "Locust"
JMETER_TOOL = "Jmeter"

SCRIPT_TYPE_API = "API"
SCRIPT_TYPE_MARKER = "Script Type"

MAX_DOCUMENT_BYTES = 10 * 1024 * 1024
MAX_ENDPOINTS = 150
# Enough for a long API reference while keeping the AI prompt within the
# context window of every supported provider.
MAX_AI_DOCUMENT_CHARS = 60_000
_SCHEMA_DEPTH = 6

SUPPORTED_EXTENSIONS = (".json", ".yaml", ".yml", ".txt", ".md", ".docx", ".doc")
HTTP_METHODS = ("get", "post", "put", "patch", "delete", "head", "options")

TOKEN_ENV_VAR = "PERF_API_TOKEN"
API_KEY_ENV_VAR = "PERF_API_KEY"
USERNAME_ENV_VAR = "PERF_API_USERNAME"
PASSWORD_ENV_VAR = "PERF_API_PASSWORD"

_NON_ALNUM = re.compile(r"[^A-Za-z0-9]+")
_POSTMAN_VAR = re.compile(r"\{\{\s*([^}]+?)\s*\}\}")


class ApiDocumentError(ValueError):
    """The document could not be read or holds no usable endpoints."""


# ---------------------------------------------------------------------------
# Naming
# ---------------------------------------------------------------------------

def sanitize_title(raw):
    """Collapse a title to one line that is safe inside a docstring / XML attribute."""
    single_line = " ".join((raw or "").split())
    return single_line.replace("\\", "/").replace('"""', "'''").strip()


def script_file_name(raw, tool):
    """`My Orders API` -> `My_Orders_API.py` (Locust) / `.jmx` (JMeter); '' when unusable."""
    base = os.path.basename((raw or "").strip().replace("\\", "/"))
    base = re.sub(r"\.(py|jmx)$", "", base, flags=re.IGNORECASE)
    stem = _NON_ALNUM.sub("_", base).strip("_")
    if not stem:
        return ""
    # A leading digit is not a valid module name, and the script list treats a
    # leading underscore as "helper, not a runnable script".
    if stem[0].isdigit():
        stem = f"api_{stem}"
    return f"{stem}{'.jmx' if tool == JMETER_TOOL else '.py'}"


def _identifier(text, fallback="endpoint"):
    ident = _NON_ALNUM.sub("_", text or "").strip("_").lower()
    if not ident:
        ident = fallback
    return f"_{ident}" if ident[0].isdigit() else ident


def _class_name(title):
    parts = [p for p in _NON_ALNUM.split(title or "") if p]
    name = "".join(part[:1].upper() + part[1:] for part in parts) or "Api"
    if name[0].isdigit():
        name = f"Api{name}"
    return f"{name}User"


# ---------------------------------------------------------------------------
# Document reading
# ---------------------------------------------------------------------------

def _decode_text(raw):
    for encoding in ("utf-8-sig", "utf-16", "cp1252"):
        try:
            text = raw.decode(encoding)
            if encoding == "utf-16" and "\x00" in text:
                continue
            return text
        except (UnicodeDecodeError, UnicodeError):
            continue
    return raw.decode("latin-1", errors="ignore")


def _docx_text(raw):
    """Paragraph and table-cell text of a .docx (it is a zip of WordprocessingML)."""
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            xml = archive.read("word/document.xml").decode("utf-8", errors="ignore")
    except (zipfile.BadZipFile, KeyError) as e:
        raise ApiDocumentError(f"The Word document could not be opened: {e}") from e
    xml = re.sub(r"</w:p>", "\n", xml)
    xml = re.sub(r"<w:tab/>|</w:tc>", "\t", xml)
    xml = re.sub(r"<w:br[^>]*/>", "\n", xml)
    text = re.sub(r"<[^>]+>", "", xml)
    for entity, char in (("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"'), ("&apos;", "'"), ("&amp;", "&")):
        text = text.replace(entity, char)
    return text


def _legacy_doc_text(raw):
    """
    Best-effort text of a legacy binary .doc. Word 97-2003 stores the body as
    UTF-16LE or cp1252 runs inside an OLE container; pulling out the longer
    printable runs recovers the prose without an extra dependency.
    """
    runs = []
    for match in re.finditer(rb"(?:[\x20-\x7e\t\r\n]\x00){6,}", raw):
        runs.append(match.group().decode("utf-16le", errors="ignore"))
    if sum(len(r) for r in runs) < 200:
        runs = [m.group().decode("cp1252", errors="ignore")
                for m in re.finditer(rb"[\x20-\x7e\t\r\n]{8,}", raw)]
    text = "\n".join(r.strip() for r in runs if r.strip())
    if len(text) < 80:
        raise ApiDocumentError(
            "Could not read text from this .doc file. Save it as .docx or .txt and upload it again."
        )
    return text


def _load_structured(text):
    """Parse text as JSON, then YAML (when PyYAML is available). None when neither fits."""
    stripped = text.strip()
    if stripped[:1] in ("{", "["):
        try:
            return json.loads(stripped)
        except ValueError:
            pass
    if re.search(r"^\s*(openapi|swagger)\s*:", stripped, re.MULTILINE):
        try:
            import yaml
        except ImportError:
            return None
        try:
            return yaml.safe_load(stripped)
        except Exception:
            return None
    return None


def read_document(file_name, raw):
    """
    Classify and read an uploaded API document.

    Returns (kind, content): kind is "openapi" | "swagger" | "postman" with the
    parsed dict, or "text" with the document's plain text (for AI extraction).
    """
    if not raw:
        raise ApiDocumentError("The uploaded document is empty.")
    if len(raw) > MAX_DOCUMENT_BYTES:
        raise ApiDocumentError(f"The document is larger than {MAX_DOCUMENT_BYTES // (1024 * 1024)} MB.")

    extension = os.path.splitext(file_name or "")[1].lower()
    if extension not in SUPPORTED_EXTENSIONS:
        raise ApiDocumentError(
            f"Unsupported document type '{extension or file_name}'. "
            f"Upload one of: {', '.join(SUPPORTED_EXTENSIONS)}."
        )

    if extension == ".docx":
        return "text", _docx_text(raw)
    if extension == ".doc":
        # Some ".doc" files are really .docx or RTF/HTML saved with the old extension.
        if raw[:2] == b"PK":
            return "text", _docx_text(raw)
        if raw[:1] in (b"{", b"<"):
            return "text", _decode_text(raw)
        return "text", _legacy_doc_text(raw)

    text = _decode_text(raw)
    data = _load_structured(text)
    if isinstance(data, dict):
        if str(data.get("openapi", "")).startswith("3"):
            return "openapi", data
        if str(data.get("swagger", "")).startswith("2"):
            return "swagger", data
        if isinstance(data.get("item"), list):
            return "postman", data
    if extension == ".json":
        if data is None:
            raise ApiDocumentError("The JSON document could not be parsed. Check that it is valid JSON.")
        raise ApiDocumentError(
            "The JSON document is neither an OpenAPI/Swagger specification nor a Postman collection."
        )
    # Plain text, or YAML that is not OpenAPI (or PyYAML is missing) - the AI reads it.
    return "text", text


# ---------------------------------------------------------------------------
# Sample values
# ---------------------------------------------------------------------------

def _resolve_ref(spec, node, seen=None):
    """Follow a local `$ref` (`#/components/schemas/X`); cycles resolve to {}."""
    seen = seen or set()
    while isinstance(node, dict) and "$ref" in node:
        ref = node["$ref"]
        if not isinstance(ref, str) or not ref.startswith("#/") or ref in seen:
            return {}
        seen.add(ref)
        target = spec
        for part in ref[2:].split("/"):
            part = part.replace("~1", "/").replace("~0", "~")
            target = target.get(part, {}) if isinstance(target, dict) else {}
        node = target
    return node if isinstance(node, dict) else {}


def _sample_from_schema(spec, schema, depth=0, expanding=frozenset()):
    """
    Build a sample value for a JSON schema. `expanding` holds the `$ref`s on the
    current path, so a self-referencing schema (Order.parent -> Order) stops at
    the loop instead of nesting until the depth limit.
    """
    ref = schema.get("$ref") if isinstance(schema, dict) else None
    if ref in expanding:
        return None
    if ref:
        expanding = expanding | {ref}
    schema = _resolve_ref(spec, schema)
    if not schema or depth > _SCHEMA_DEPTH:
        return None
    for key in ("example", "default"):
        if key in schema:
            return schema[key]
    if isinstance(schema.get("examples"), list) and schema["examples"]:
        return schema["examples"][0]
    if isinstance(schema.get("enum"), list) and schema["enum"]:
        return schema["enum"][0]
    for combiner in ("allOf", "oneOf", "anyOf"):
        options = schema.get(combiner)
        if isinstance(options, list) and options:
            if combiner == "allOf":
                merged = {}
                for option in options:
                    sample = _sample_from_schema(spec, option, depth + 1, expanding)
                    if isinstance(sample, dict):
                        merged.update(sample)
                return merged or None
            return _sample_from_schema(spec, options[0], depth + 1, expanding)

    kind = schema.get("type")
    if isinstance(kind, list):
        kind = next((k for k in kind if k != "null"), None)
    if kind == "object" or (kind is None and "properties" in schema):
        sample = {}
        for name, prop in (schema.get("properties") or {}).items():
            value = _sample_from_schema(spec, prop, depth + 1, expanding)
            if value is not None:
                sample[name] = value
        return sample
    if kind == "array":
        item = _sample_from_schema(spec, schema.get("items") or {}, depth + 1, expanding)
        return [item] if item is not None else []
    if kind == "integer":
        return 1
    if kind == "number":
        return 1.0
    if kind == "boolean":
        return True
    fmt = schema.get("format", "")
    return {
        "date": "2024-01-01", "date-time": "2024-01-01T00:00:00Z", "email": "user@example.com",
        "uuid": "00000000-0000-0000-0000-000000000001", "uri": "https://example.com",
    }.get(fmt, "string")


def _param_sample(spec, param):
    for key in ("example", "default"):
        if key in param:
            return param[key]
    examples = param.get("examples")
    if isinstance(examples, dict) and examples:
        first = _resolve_ref(spec, next(iter(examples.values())))
        if "value" in first:
            return first["value"]
    schema = param.get("schema") or {k: v for k, v in param.items() if k in ("type", "format", "enum", "items")}
    value = _sample_from_schema(spec, schema)
    return 1 if value is None else value


# ---------------------------------------------------------------------------
# OpenAPI / Swagger
# ---------------------------------------------------------------------------

def _openapi_auth(spec, kind):
    schemes = (spec.get("components", {}) or {}).get("securitySchemes", {}) if kind == "openapi" \
        else spec.get("securityDefinitions", {})
    for scheme in (schemes or {}).values():
        scheme = _resolve_ref(spec, scheme)
        stype = (scheme.get("type") or "").lower()
        if stype == "http" and (scheme.get("scheme") or "").lower() == "basic" or stype == "basic":
            return {"type": "basic"}
        if stype in ("http", "oauth2", "openidconnect"):
            return {"type": "bearer"}
        if stype == "apikey" and (scheme.get("in") or "header") == "header":
            return {"type": "apikey", "name": scheme.get("name") or "X-API-Key"}
    return {"type": ""}


def _openapi_base_path(spec, kind):
    if kind == "swagger":
        return (spec.get("basePath") or "").rstrip("/")
    servers = spec.get("servers") or []
    url = servers[0].get("url", "") if servers and isinstance(servers[0], dict) else ""
    return (urlsplit(url).path if "://" in url else url).rstrip("/")


def _openapi_body(spec, operation, kind, params):
    """Return (body_type, body) for an operation's request body."""
    if kind == "swagger":
        body_param = next((p for p in params if p.get("in") == "body"), None)
        if body_param:
            return "json", _sample_from_schema(spec, body_param.get("schema") or {})
        form = {p["name"]: _param_sample(spec, p) for p in params if p.get("in") == "formData" and p.get("name")}
        return ("form", form) if form else ("", None)

    request_body = _resolve_ref(spec, operation.get("requestBody") or {})
    content = request_body.get("content") or {}
    for media, body_type in (("application/json", "json"), ("+json", "json"),
                             ("application/x-www-form-urlencoded", "form"),
                             ("multipart/form-data", "form"), ("xml", "xml"), ("text/", "text")):
        match = next((c for c in content if media in c), None)
        if not match:
            continue
        media_obj = content[match] or {}
        sample = media_obj.get("example")
        if sample is None and isinstance(media_obj.get("examples"), dict) and media_obj["examples"]:
            sample = _resolve_ref(spec, next(iter(media_obj["examples"].values()))).get("value")
        if sample is None:
            sample = _sample_from_schema(spec, media_obj.get("schema") or {})
        if body_type == "xml" and not isinstance(sample, str):
            sample = "<request/>"
        if body_type == "text" and not isinstance(sample, str):
            sample = json.dumps(sample) if sample is not None else ""
        return body_type, sample
    return "", None


def parse_openapi(spec, kind):
    endpoints = []
    for raw_path, path_item in (spec.get("paths") or {}).items():
        path_item = _resolve_ref(spec, path_item)
        shared = [_resolve_ref(spec, p) for p in path_item.get("parameters") or []]
        for method in HTTP_METHODS:
            operation = path_item.get(method)
            if not isinstance(operation, dict):
                continue
            params = {(p.get("in"), p.get("name")): p for p in shared}
            for p in operation.get("parameters") or []:
                p = _resolve_ref(spec, p)
                params[(p.get("in"), p.get("name"))] = p
            params = [p for p in params.values() if p.get("name")]

            path = raw_path
            query, headers = {}, {}
            for p in params:
                location = p.get("in")
                if location == "query" and (p.get("required") or "example" in p or "default" in p):
                    query[p["name"]] = _param_sample(spec, p)
                elif location == "header" and p["name"].lower() not in ("authorization", "content-type", "accept"):
                    headers[p["name"]] = str(_param_sample(spec, p))
            body_type, body = _openapi_body(spec, operation, kind, params)
            endpoints.append({
                "name": operation.get("summary") or operation.get("operationId") or f"{method.upper()} {path}",
                "method": method.upper(),
                "path": path,
                "path_samples": {p["name"]: _param_sample(spec, p) for p in params if p.get("in") == "path"},
                "query": query,
                "headers": headers,
                "body_type": body_type,
                "body": body,
            })
    return {"base_path": _openapi_base_path(spec, kind), "auth": _openapi_auth(spec, kind),
            "endpoints": endpoints}


# ---------------------------------------------------------------------------
# Postman
# ---------------------------------------------------------------------------

def _postman_vars(collection):
    return {v.get("key"): v.get("value") for v in collection.get("variable") or []
            if isinstance(v, dict) and v.get("key")}


def _postman_auth(auth):
    if not isinstance(auth, dict):
        return {"type": ""}
    atype = (auth.get("type") or "").lower()
    if atype in ("bearer", "oauth2", "jwt"):
        return {"type": "bearer"}
    if atype == "basic":
        return {"type": "basic"}
    if atype == "apikey":
        entries = {e.get("key"): e.get("value") for e in auth.get("apikey") or [] if isinstance(e, dict)}
        if (entries.get("in") or "header") == "header":
            return {"type": "apikey", "name": entries.get("key") or "X-API-Key"}
    return {"type": ""}


def _postman_url(url, variables):
    """Return (path, query) for a Postman url (string or object), dropping the host part."""
    raw = url.get("raw", "") if isinstance(url, dict) else str(url or "")
    # Resolve variables that are plain values; a leading {{baseUrl}} style host is dropped below.
    def substitute(match):
        value = variables.get(match.group(1))
        return str(value) if value not in (None, "") and "://" not in str(value) else match.group(0)
    raw = _POSTMAN_VAR.sub(substitute, raw)

    if isinstance(url, dict) and isinstance(url.get("path"), list):
        path = "/" + "/".join(str(seg.get("value", "") if isinstance(seg, dict) else seg) for seg in url["path"])
        query = {q.get("key"): q.get("value", "") for q in url.get("query") or []
                 if isinstance(q, dict) and q.get("key") and not q.get("disabled")}
    else:
        without_host = re.sub(r"^\{\{[^}]+\}\}", "", raw)
        parts = urlsplit(without_host if "://" in without_host else "http://placeholder" +
                         (without_host if without_host.startswith("/") else "/" + without_host))
        path = parts.path or "/"
        query = dict(parse_qsl(parts.query, keep_blank_values=True))

    path = _POSTMAN_VAR.sub(lambda m: "{" + m.group(1) + "}", path)
    path = re.sub(r"/:([A-Za-z_]\w*)", r"/{\1}", path)
    return path.replace("//", "/") or "/", query


def _postman_body(body, variables):
    if not isinstance(body, dict):
        return "", None
    mode = body.get("mode")
    if mode == "raw":
        # Collection variables with a value are substituted; unknown ones keep
        # their name so the body still parses and shows what to fill in.
        raw = _POSTMAN_VAR.sub(lambda m: str(variables.get(m.group(1), m.group(1))), body.get("raw") or "")
        language = ((body.get("options") or {}).get("raw") or {}).get("language", "")
        if language == "json" or raw.lstrip()[:1] in ("{", "["):
            try:
                return "json", json.loads(raw)
            except ValueError:
                return "text", raw
        if language == "xml" or raw.lstrip().startswith("<"):
            return "xml", raw
        return ("text", raw) if raw else ("", None)
    if mode in ("urlencoded", "formdata"):
        form = {f.get("key"): f.get("value", "") for f in body.get(mode) or []
                if isinstance(f, dict) and f.get("key") and not f.get("disabled") and f.get("type") != "file"}
        return ("form", form) if form else ("", None)
    return "", None


def parse_postman(collection):
    variables = _postman_vars(collection)
    auth = _postman_auth(collection.get("auth"))
    endpoints = []

    def walk(items, folder_auth):
        nonlocal auth
        for item in items or []:
            if not isinstance(item, dict):
                continue
            if isinstance(item.get("item"), list):
                walk(item["item"], _postman_auth(item.get("auth")) if item.get("auth") else folder_auth)
                continue
            request = item.get("request")
            if isinstance(request, str):
                request = {"method": "GET", "url": request}
            if not isinstance(request, dict):
                continue
            path, query = _postman_url(request.get("url"), variables)
            headers = {h.get("key"): h.get("value", "") for h in request.get("header") or []
                       if isinstance(h, dict) and h.get("key") and not h.get("disabled")
                       and h["key"].lower() not in ("authorization", "content-type", "accept")}
            body_type, body = _postman_body(request.get("body"), variables)
            if not auth.get("type"):
                auth = _postman_auth(request.get("auth")) if request.get("auth") else folder_auth
            endpoints.append({
                "name": item.get("name") or f"{request.get('method', 'GET')} {path}",
                "method": (request.get("method") or "GET").upper(),
                "path": path,
                "path_samples": {},
                "query": query,
                "headers": headers,
                "body_type": body_type,
                "body": body,
            })

    walk(collection.get("item"), auth)
    return {"base_path": "", "auth": auth, "endpoints": endpoints}


# ---------------------------------------------------------------------------
# Free-form documents (AI extraction)
# ---------------------------------------------------------------------------

AI_EXTRACTION_PROMPT = """You extract HTTP API endpoints from API documentation so a performance test can be generated.

Return ONLY a JSON object, no prose and no markdown fences, with exactly this shape:
{{
  "base_path": "common path prefix shared by every endpoint, e.g. /api/v1, or empty string",
  "auth": {{"type": "bearer" | "apikey" | "basic" | "", "name": "header name when type is apikey"}},
  "endpoints": [
    {{
      "name": "short human description, e.g. Create order",
      "method": "GET | POST | PUT | PATCH | DELETE",
      "path": "/path/relative/to/base_path with {{param}} placeholders for path parameters",
      "path_samples": {{"param": "realistic sample value"}},
      "query": {{"name": "sample value"}},
      "headers": {{"Header-Name": "value"}},
      "body_type": "json | form | xml | text | empty string when there is no body",
      "body": <sample request body: a JSON object/array for json, an object for form, a string for xml/text, null otherwise>
    }}
  ]
}}

Rules:
- Include every distinct endpoint the document describes, in the order it describes them.
- Use sample values that appear in the document; otherwise invent realistic ones matching the described types.
- Do not put Authorization, Content-Type or Accept in headers - authentication goes in "auth".
- Paths must start with "/". Never include the scheme or host in a path.
- If the document describes no HTTP endpoints, return {{"base_path": "", "auth": {{"type": ""}}, "endpoints": []}}.

API documentation:
<<<
{document}
>>>
"""


def _extract_json_object(text):
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ApiDocumentError("The AI response did not contain the endpoint list.")
    try:
        return json.loads(text[start:end + 1], strict=False)
    except ValueError as e:
        raise ApiDocumentError(f"The AI response could not be parsed as JSON: {e}") from e


def extract_with_ai(document_text, ai_call):
    """
    Ask the configured AI provider for the document's endpoints.

    `ai_call(prompt) -> str` is injected by the caller (the Flask app wires in
    api.backend.call_ai) so this module stays free of provider plumbing.
    """
    text = (document_text or "").strip()
    if len(text) < 20:
        raise ApiDocumentError("The document has no readable text.")
    truncated = len(text) > MAX_AI_DOCUMENT_CHARS
    prompt = AI_EXTRACTION_PROMPT.format(document=text[:MAX_AI_DOCUMENT_CHARS])
    data = _extract_json_object(ai_call(prompt))
    if not isinstance(data, dict):
        raise ApiDocumentError("The AI response was not a JSON object.")
    data.setdefault("base_path", "")
    data.setdefault("auth", {"type": ""})
    data["truncated"] = truncated
    return data


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------

def _clean_endpoint(raw):
    if not isinstance(raw, dict):
        return None
    method = str(raw.get("method") or "GET").upper().strip()
    if method.lower() not in HTTP_METHODS:
        return None
    path = str(raw.get("path") or "").strip()
    if "://" in path:
        parts = urlsplit(path)
        path = parts.path + (f"?{parts.query}" if parts.query else "")
    if "?" in path:
        path, _, query_text = path.partition("?")
        query = dict(parse_qsl(query_text, keep_blank_values=True))
    else:
        query = {}
    if not path.startswith("/"):
        path = "/" + path
    query.update({str(k): v for k, v in (raw.get("query") or {}).items()} if isinstance(raw.get("query"), dict) else {})

    body_type = str(raw.get("body_type") or "").lower()
    body = raw.get("body")
    if body_type not in ("json", "form", "xml", "text"):
        body_type = "json" if isinstance(body, (dict, list)) else ""
    if body_type == "form" and not isinstance(body, dict):
        body_type = "text" if isinstance(body, str) and body else ""
    if body_type in ("xml", "text") and not isinstance(body, str):
        body = json.dumps(body) if body is not None else ""
    if body_type and body in (None, "", {}, []) and body_type != "json":
        body_type = ""
    if method in ("GET", "HEAD", "OPTIONS", "DELETE") and body_type and body in (None, {}):
        body_type = ""

    headers = raw.get("headers") if isinstance(raw.get("headers"), dict) else {}
    samples = raw.get("path_samples") if isinstance(raw.get("path_samples"), dict) else {}
    return {
        "name": sanitize_title(str(raw.get("name") or f"{method} {path}"))[:120],
        "method": method,
        "path": path,
        "path_samples": {str(k): v for k, v in samples.items()},
        "query": query,
        "headers": {str(k): str(v) for k, v in headers.items()},
        "body_type": body_type,
        "body": body if body_type else None,
    }


def normalize_api_model(model):
    endpoints = [e for e in (_clean_endpoint(raw) for raw in model.get("endpoints") or []) if e]
    if not endpoints:
        raise ApiDocumentError("No HTTP endpoints were found in the document.")
    auth = model.get("auth") if isinstance(model.get("auth"), dict) else {}
    auth_type = str(auth.get("type") or "").lower()
    base_path = str(model.get("base_path") or "").strip()
    if "://" in base_path:
        base_path = urlsplit(base_path).path
    base_path = ("/" + base_path.strip("/")) if base_path.strip("/") else ""
    if base_path:
        # An endpoint given as a full URL already carries the base path.
        for endpoint in endpoints:
            if endpoint["path"] == base_path or endpoint["path"].startswith(base_path + "/"):
                endpoint["path"] = endpoint["path"][len(base_path):] or "/"
    return {
        "base_path": base_path,
        "auth": {"type": auth_type if auth_type in ("bearer", "apikey", "basic") else "",
                 "name": str(auth.get("name") or "X-API-Key")},
        "endpoints": endpoints[:MAX_ENDPOINTS],
        "dropped": max(0, len(endpoints) - MAX_ENDPOINTS),
        "truncated": bool(model.get("truncated")),
    }


def build_api_model(kind, content, ai_call=None):
    """Turn a read document into the normalised endpoint model."""
    if kind in ("openapi", "swagger"):
        model = parse_openapi(content, kind)
    elif kind == "postman":
        model = parse_postman(content)
    else:
        if ai_call is None:
            raise ApiDocumentError("Reading this document needs the AI provider, which is not available.")
        model = extract_with_ai(content, ai_call)
    return normalize_api_model(model)


# ---------------------------------------------------------------------------
# Rendering helpers
# ---------------------------------------------------------------------------

def _request_path(endpoint, prefix):
    """(concrete path with samples, templated stats name) under `prefix`."""
    templated = f"{prefix}{endpoint['path']}" if prefix else endpoint["path"]
    samples = endpoint.get("path_samples") or {}

    def fill(match):
        value = samples.get(match.group(1), 1)
        if value in (None, "", "string"):
            # A schema placeholder makes a poor URL segment; 1 is accepted by most ids.
            value = 1
        return str(value).replace("/", "%2F").replace(" ", "%20")
    concrete = re.sub(r"\{([^}/]+)\}", fill, templated)
    return concrete, templated


def _effective_prefix(base_path, application_url):
    """Doc base path, unless the Application URL already ends with it."""
    app_path = urlsplit(application_url or "").path.rstrip("/")
    if not base_path or (app_path and app_path.endswith(base_path)):
        return ""
    return base_path


def _query_string(query):
    items = []
    for key, value in (query or {}).items():
        if isinstance(value, (dict, list)):
            value = json.dumps(value, separators=(",", ":"))
        elif isinstance(value, bool):
            value = str(value).lower()
        items.append((key, "" if value is None else value))
    return urlencode(items)


def _origin(application_url):
    parts = urlsplit((application_url or "").strip())
    return f"{parts.scheme}://{parts.netloc}" if parts.scheme and parts.netloc else ""


def _header_lines(model, generated_from, source_label, title, file_name, tool):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    notes = ["Sample values come from the document's examples and schemas - parameterise them "
             "before load testing."]
    auth = model["auth"]["type"]
    if auth == "bearer":
        notes.append(f"Bearer token is read from the {TOKEN_ENV_VAR} " +
                     ("environment variable." if tool == LOCUST_TOOL else "JMeter property (-Japi.token=...)."))
    elif auth == "apikey":
        notes.append(f"API key header '{model['auth']['name']}' is read from the {API_KEY_ENV_VAR} " +
                     ("environment variable." if tool == LOCUST_TOOL else "JMeter property (-Japi.key=...)."))
    elif auth == "basic":
        notes.append(f"Basic auth credentials are read from {USERNAME_ENV_VAR} / {PASSWORD_ENV_VAR}" +
                     ("." if tool == LOCUST_TOOL else " (-Japi.username / -Japi.password)."))
    if model.get("dropped"):
        notes.append(f"{model['dropped']} endpoint(s) beyond the first {MAX_ENDPOINTS} were not included.")
    if model.get("truncated"):
        notes.append(f"Only the first {MAX_AI_DOCUMENT_CHARS} characters of the document were analysed.")
    return {
        "title": title,
        "lines": [
            f"Test Case: {title}",
            f"{SCRIPT_TYPE_MARKER}: {SCRIPT_TYPE_API}",
            "",
            f"Generated from : {generated_from} ({source_label})",
            f"Generated at   : {now}",
            f"Endpoints      : {len(model['endpoints'])}",
        ],
        "notes": notes,
    }


# ---------------------------------------------------------------------------
# Locust
# ---------------------------------------------------------------------------

def _literal(value, indent):
    text = pformat(value, width=88, sort_dicts=False)
    return text.replace("\n", "\n" + " " * indent)


def build_locust_script(model, title, file_name, application_url, generated_from, source_label):
    header = _header_lines(model, generated_from, source_label, title, file_name, LOCUST_TOOL)
    origin = _origin(application_url)
    prefix = _effective_prefix(model["base_path"], application_url)
    auth = model["auth"]

    lines = ['"""', file_name, "-" * len(file_name)] + header["lines"] + [
        "", "Generated by the BIG QA API script generator. Review before load testing:"]
    lines += [f"  * {note}" for note in header["notes"]]
    lines += ["", "Run standalone (from the perf project root):",
              f"    locust -f locustfiles/{file_name}" + (f" --host {origin}" if origin else ""),
              '"""', ""]

    if auth["type"]:
        lines += ["import os", ""]
    lines += ["from locust import HttpUser, task, between", "", ""]
    lines += [f"class {_class_name(title)}(HttpUser):"]
    lines += [f'    host = "{origin}"' if origin else "    # host comes from --host / the suite config",
              "    wait_time = between(1, 3)", ""]

    if auth["type"]:
        lines += ["    def on_start(self):"]
        if auth["type"] == "bearer":
            lines += [f'        token = os.getenv("{TOKEN_ENV_VAR}", "")',
                      "        if token:",
                      '            self.client.headers["Authorization"] = f"Bearer {token}"']
        elif auth["type"] == "apikey":
            lines += [f'        api_key = os.getenv("{API_KEY_ENV_VAR}", "")',
                      "        if api_key:",
                      f"            self.client.headers[{auth['name']!r}] = api_key"]
        else:
            lines += [f'        username = os.getenv("{USERNAME_ENV_VAR}", "")',
                      "        if username:",
                      f'            self.client.auth = (username, os.getenv("{PASSWORD_ENV_VAR}", ""))']
        lines += [""]

    lines += ["    @task", "    def api_journey(self):"]
    for index, endpoint in enumerate(model["endpoints"]):
        concrete, templated = _request_path(endpoint, prefix)
        query = _query_string(endpoint["query"])
        target = f"{concrete}?{query}" if query else concrete
        args = [repr(target)]
        body_type, body = endpoint["body_type"], endpoint["body"]
        if body_type == "json":
            args.append(f"json={_literal(body, 12)}")
        elif body_type == "form":
            args.append(f"data={_literal(body, 12)}")
        elif body_type in ("xml", "text"):
            args.append(f"data={_literal(body, 12)}")
        headers = dict(endpoint["headers"])
        if body_type == "xml":
            headers.setdefault("Content-Type", "application/xml")
        elif body_type == "text":
            headers.setdefault("Content-Type", "text/plain")
        if headers:
            args.append(f"headers={_literal(headers, 12)}")
        args.append(f"name={templated!r}")

        if index:
            lines.append("")
        lines.append(f"        # {index + 1}. {endpoint['method']} {templated} - {endpoint['name']}")
        method = endpoint["method"].lower()
        single = f"        self.client.{method}({', '.join(args)})"
        if len(single) <= 110 and "\n" not in single:
            lines.append(single)
        else:
            lines.append(f"        self.client.{method}(")
            lines += [f"            {arg}," for arg in args]
            lines.append("        )")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# JMeter
# ---------------------------------------------------------------------------

def _x(value):
    return _xml_escape(str(value), {'"': "&quot;"})


def _prop(name, value):
    return f'<stringProp name="{name}">{_x(value)}</stringProp>'


def _header_manager(name, headers, indent):
    pad = " " * indent
    rows = "".join(
        f'\n{pad}    <elementProp name="" elementType="Header">{_prop("Header.name", k)}{_prop("Header.value", v)}</elementProp>'
        for k, v in headers.items()
    )
    return (f'{pad}<HeaderManager guiclass="HeaderPanel" testclass="HeaderManager" testname="{_x(name)}" enabled="true">\n'
            f'{pad}  <collectionProp name="HeaderManager.headers">{rows}\n{pad}  </collectionProp>\n'
            f'{pad}</HeaderManager>\n{pad}<hashTree/>\n')


def _sampler(endpoint, prefix, indent):
    pad = " " * indent
    concrete, templated = _request_path(endpoint, prefix)
    body_type, body = endpoint["body_type"], endpoint["body"]
    raw_body = body_type in ("json", "xml", "text")
    path = concrete
    query = _query_string(endpoint["query"])

    arguments = []
    if raw_body:
        text = json.dumps(body, indent=2) if body_type == "json" else str(body)
        arguments.append(
            f'{pad}      <elementProp name="" elementType="HTTPArgument">\n'
            f'{pad}        <boolProp name="HTTPArgument.always_encode">false</boolProp>\n'
            f'{pad}        {_prop("Argument.value", text)}\n'
            f'{pad}        {_prop("Argument.metadata", "=")}\n'
            f'{pad}      </elementProp>')
        if query:
            path = f"{path}?{query}"
    else:
        pairs = list((endpoint["body"] or {}).items()) if body_type == "form" else []
        if body_type == "form" and query:
            path = f"{path}?{query}"
        elif query:
            pairs = parse_qsl(query, keep_blank_values=True)
        for key, value in pairs:
            if isinstance(value, (dict, list)):
                value = json.dumps(value)
            arguments.append(
                f'{pad}      <elementProp name="{_x(key)}" elementType="HTTPArgument">\n'
                f'{pad}        <boolProp name="HTTPArgument.always_encode">true</boolProp>\n'
                f'{pad}        {_prop("Argument.name", key)}\n'
                f'{pad}        {_prop("Argument.value", "" if value is None else value)}\n'
                f'{pad}        {_prop("Argument.metadata", "=")}\n'
                f'{pad}        <boolProp name="HTTPArgument.use_equals">true</boolProp>\n'
                f'{pad}      </elementProp>')

    args_xml = ("\n" + "\n".join(arguments) + f"\n{pad}    ") if arguments else ""
    label = f"{endpoint['method']} {templated} - {endpoint['name']}"
    xml = (
        f'{pad}<HTTPSamplerProxy guiclass="HttpTestSampleGui" testclass="HTTPSamplerProxy" testname="{_x(label)}" enabled="true">\n'
        f'{pad}  <boolProp name="HTTPSampler.postBodyRaw">{"true" if raw_body else "false"}</boolProp>\n'
        f'{pad}  <elementProp name="HTTPsampler.Arguments" elementType="Arguments">\n'
        f'{pad}    <collectionProp name="Arguments.arguments">{args_xml}</collectionProp>\n'
        f'{pad}  </elementProp>\n'
        f'{pad}  {_prop("HTTPSampler.domain", "")}\n'
        f'{pad}  {_prop("HTTPSampler.port", "")}\n'
        f'{pad}  {_prop("HTTPSampler.protocol", "")}\n'
        f'{pad}  {_prop("HTTPSampler.path", path)}\n'
        f'{pad}  {_prop("HTTPSampler.method", endpoint["method"])}\n'
        f'{pad}  <boolProp name="HTTPSampler.follow_redirects">true</boolProp>\n'
        f'{pad}  <boolProp name="HTTPSampler.auto_redirects">false</boolProp>\n'
        f'{pad}  <boolProp name="HTTPSampler.use_keepalive">true</boolProp>\n'
        f'{pad}  <boolProp name="HTTPSampler.DO_MULTIPART_POST">false</boolProp>\n'
        f'{pad}</HTTPSamplerProxy>\n'
    )
    headers = dict(endpoint["headers"])
    if body_type == "json":
        headers.setdefault("Content-Type", "application/json")
    elif body_type == "xml":
        headers.setdefault("Content-Type", "application/xml")
    elif body_type == "text":
        headers.setdefault("Content-Type", "text/plain")
    elif body_type == "form":
        headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
    if headers:
        xml += f"{pad}<hashTree>\n" + _header_manager("Request headers", headers, indent + 2) + f"{pad}</hashTree>\n"
    else:
        xml += f"{pad}<hashTree/>\n"
    return xml


def build_jmeter_script(model, title, file_name, application_url, generated_from, source_label):
    header = _header_lines(model, generated_from, source_label, title, file_name, JMETER_TOOL)
    comments = "\n".join(header["lines"] + [""] + [f"* {note}" for note in header["notes"]])
    parts = urlsplit(application_url or "")
    host = parts.hostname or "localhost"
    protocol = (parts.scheme or "https").lower()
    port = str(parts.port or "")
    prefix = _effective_prefix(model["base_path"], application_url)
    app_path = parts.path.rstrip("/")
    if app_path:
        # JMeter's defaults element has no base-path field, so the Application
        # URL's own path is folded into every sampler.
        prefix = f"{app_path}{prefix}"

    auth = model["auth"]
    common_headers = {"Accept": "application/json"}
    if auth["type"] == "bearer":
        common_headers["Authorization"] = "Bearer ${__P(api.token,)}"
    elif auth["type"] == "apikey":
        common_headers[auth["name"]] = "${__P(api.key,)}"
    elif auth["type"] == "basic":
        common_headers["Authorization"] = "Basic ${__base64Encode(${__P(api.username,)}:${__P(api.password,)})}"

    samplers = "".join(_sampler(e, prefix, 8) for e in model["endpoints"])
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<jmeterTestPlan version="1.2" properties="5.0" jmeter="5.6.3">
  <hashTree>
    <TestPlan guiclass="TestPlanGui" testclass="TestPlan" testname="{_x(title)}" enabled="true">
      {_prop("TestPlan.comments", comments)}
      <boolProp name="TestPlan.functional_mode">false</boolProp>
      <boolProp name="TestPlan.serialize_threadgroups">false</boolProp>
      <elementProp name="TestPlan.user_defined_variables" elementType="Arguments" guiclass="ArgumentsPanel" testclass="Arguments" testname="User Defined Variables" enabled="true">
        <collectionProp name="Arguments.arguments"/>
      </elementProp>
    </TestPlan>
    <hashTree>
      <ConfigTestElement guiclass="HttpDefaultsGui" testclass="ConfigTestElement" testname="HTTP Request Defaults" enabled="true">
        <elementProp name="HTTPsampler.Arguments" elementType="Arguments" guiclass="HTTPArgumentsPanel" testclass="Arguments" testname="User Defined Variables" enabled="true">
          <collectionProp name="Arguments.arguments"/>
        </elementProp>
        {_prop("HTTPSampler.domain", "${__P(api.host," + host + ")}")}
        {_prop("HTTPSampler.port", "${__P(api.port," + port + ")}" if port else "")}
        {_prop("HTTPSampler.protocol", "${__P(api.protocol," + protocol + ")}")}
        {_prop("HTTPSampler.contentEncoding", "UTF-8")}
        {_prop("HTTPSampler.path", "")}
        {_prop("HTTPSampler.connect_timeout", "10000")}
        {_prop("HTTPSampler.response_timeout", "30000")}
      </ConfigTestElement>
      <hashTree/>
      <ThreadGroup guiclass="ThreadGroupGui" testclass="ThreadGroup" testname="API Users" enabled="true">
        {_prop("ThreadGroup.on_sample_error", "continue")}
        <elementProp name="ThreadGroup.main_controller" elementType="LoopController" guiclass="LoopControlPanel" testclass="LoopController" testname="Loop Controller" enabled="true">
          <boolProp name="LoopController.continue_forever">false</boolProp>
          {_prop("LoopController.loops", "-1")}
        </elementProp>
        {_prop("ThreadGroup.num_threads", "${__P(threads,5)}")}
        {_prop("ThreadGroup.ramp_time", "${__P(rampup,5)}")}
        <boolProp name="ThreadGroup.scheduler">true</boolProp>
        {_prop("ThreadGroup.duration", "${__P(duration,30)}")}
        {_prop("ThreadGroup.delay", "")}
        <boolProp name="ThreadGroup.same_user_on_next_iteration">true</boolProp>
      </ThreadGroup>
      <hashTree>
{_header_manager("Common headers", common_headers, 8)}{samplers}      </hashTree>
    </hashTree>
  </hashTree>
</jmeterTestPlan>
"""


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------

SOURCE_LABELS = {
    "openapi": "OpenAPI 3",
    "swagger": "Swagger 2",
    "postman": "Postman collection",
    "text": "API document, endpoints extracted by AI",
}


def target_directory(perf_dir, tool):
    if tool == JMETER_TOOL:
        return os.path.join(perf_dir, "TestScripts", "api")
    return os.path.join(perf_dir, "locustfiles")


def _name_taken(perf_dir, tool, directory, file_name):
    if os.path.exists(os.path.join(directory, file_name)):
        return True
    if tool == JMETER_TOOL:
        # JMeter scripts are addressed by base name across all TestScripts folders.
        root = os.path.join(perf_dir, "TestScripts")
        for _current, _dirs, files in os.walk(root):
            if file_name in files:
                return True
    return False


def write_api_script(perf_dir, tool, model, title, file_name, application_url, generated_from, kind):
    """
    Render and write the script; a taken name gets a numeric suffix.

    Returns (absolute_path, file_name, relative_path).
    """
    directory = target_directory(perf_dir, tool)
    os.makedirs(directory, exist_ok=True)
    stem, extension = os.path.splitext(file_name)
    candidate, suffix = file_name, 2
    while _name_taken(perf_dir, tool, directory, candidate):
        candidate = f"{stem}_{suffix}{extension}"
        suffix += 1

    builder = build_jmeter_script if tool == JMETER_TOOL else build_locust_script
    source = builder(model, title, candidate, application_url, generated_from, SOURCE_LABELS.get(kind, kind))
    if tool == LOCUST_TOOL:
        # Never write a script that cannot even be imported.
        compile(source, candidate, "exec")
    path = os.path.join(directory, candidate)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(source)
    relative = os.path.relpath(path, perf_dir).replace("\\", "/")
    return path, candidate, relative
