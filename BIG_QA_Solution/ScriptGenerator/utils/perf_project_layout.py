"""
perf_project_layout.py
----------------------
Work out where a performance project keeps its scripts, instead of assuming
the folder names of the bundled templates.

A project scaffolded by this application uses `locustfiles/` (Locust) or
`TestScripts/<api|cli|functional>/` (JMeter), but an existing framework that
was onboarded in place can keep its scripts anywhere: `tests/perf/`,
`locust/`, `src/test/jmeter/`, a flat folder of `.jmx` plans... Every part of
the Performance Test page - listing, running, payload configuration, deleting,
and choosing where a new script is written - asks this module instead of
hard-coding a path.

Detection is by content, not by name:

  Locust script        a .py module that imports locust and declares a User
                       subclass (HttpUser, FastHttpUser, User, ...)
  Selenium journey     a pytest module (test_*.py) that drives a `ui` fixture -
                       the format older recordings were written in
  JMeter plan          any *.jmx file; the plans root comes from the pom's
                       jmeter-maven-plugin <testFilesDirectory> when there is one

When a project has no script yet, the conventional folder is used if it
exists, else the template's folder name is created.
"""

import os
import re
import threading
import time
from collections import Counter
from pathlib import Path

LOCUST_TOOL = "Locust"
JMETER_TOOL = "Jmeter"

# Template defaults, used only when the project has nothing better to offer.
DEFAULT_LOCUST_DIR = "locustfiles"
DEFAULT_JMETER_ROOT = "TestScripts"
# jmeter-maven-plugin's own default <testFilesDirectory>.
MAVEN_JMETER_ROOT = "src/test/jmeter"
JMETER_TYPE_FOLDERS = ("api", "cli", "functional")

# Folder names, in order of preference, for a project that has no script yet.
_LOCUST_DIR_CANDIDATES = ("locustfiles", "locust", "locust_tests", "locusttests",
                          "perf", "performance", "load_tests", "loadtests")
_JMETER_ROOT_CANDIDATES = ("TestScripts", "testscripts", "test_scripts", "src/test/jmeter",
                           "jmeter", "jmx", "testplans", "test_plans", "plans")
_RESULTS_CANDIDATES = ("results", "Results", "reports", "Reports")
_DATA_CANDIDATES = {
    LOCUST_TOOL: ("data", "Data", "payloads", "Payloads", "test_data", "testdata"),
    JMETER_TOOL: ("Payloads", "payloads", "data", "Data", "test_data", "testdata"),
}

SKIP_DIRS = {
    ".venv", "venv", "env", ".env", ".git", ".hg", ".svn", "__pycache__", "node_modules",
    "site-packages", ".idea", ".vscode", ".pytest_cache", ".mypy_cache", ".tox",
    "build", "dist", "target", "out", "bin", "obj",
    "results", "reports", "logs", "screenshots",
}
MAX_DEPTH = 6
MAX_FILES_READ = 3000
_HEAD_BYTES = 20_000

_LOCUST_IMPORT = re.compile(r"^\s*(?:from\s+locust\b|import\s+locust\b)", re.MULTILINE)
# class X(HttpUser), class X(FastHttpUser, Mixin), class X(locust.User), class X(BaseUser) ...
_USER_CLASS = re.compile(r"^\s*class\s+\w+\s*\([^)]*?\w*User\s*[,)]", re.MULTILINE)
_UI_JOURNEY = re.compile(r"^\s*def\s+test_\w*\s*\(\s*ui\b", re.MULTILINE)
_POM_TEST_FILES = re.compile(r"<testFilesDirectory>\s*([^<]+?)\s*</testFilesDirectory>")

# Generated helpers sit beside the scripts but are never scripts themselves.
GENERATED_PREFIX = "_bigqa_"
CHECK_HOOK = "locust_check_hook.py"

_CACHE_SECONDS = 3.0
_cache = {}
_cache_lock = threading.Lock()


def _read_head(path, limit=_HEAD_BYTES):
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as handle:
            return handle.read(limit)
    except OSError:
        return ""


def _skip_dir(name):
    return name.lower() in SKIP_DIRS or name.startswith(".")


def walk(root):
    """os.walk over a project, pruned of virtualenvs, build output and results."""
    root_depth = len(Path(root).parts)
    for current, dirs, files in os.walk(root):
        depth = len(Path(current).parts) - root_depth
        dirs[:] = sorted(d for d in dirs if not _skip_dir(d))
        if depth >= MAX_DEPTH:
            dirs[:] = []
        yield current, files


def _relative(perf_dir, path):
    rel = os.path.relpath(path, perf_dir).replace("\\", "/")
    return "" if rel == "." else rel


def is_locust_source(text):
    return bool(_LOCUST_IMPORT.search(text) and _USER_CLASS.search(text))


def is_ui_journey_source(text):
    """A Selenium pytest journey (`def test_x(ui):`), the pre-Locust recording format."""
    return bool(_UI_JOURNEY.search(text)) and not _LOCUST_IMPORT.search(text)


def _prefer(counter, preferred_names):
    """The folder holding most scripts; ties go to a conventional name, then the shallower path."""
    def rank(item):
        folder, count = item
        name = folder.rsplit("/", 1)[-1].lower()
        conventional = name in [p.lower() for p in preferred_names]
        return (-count, not conventional, folder.count("/"), folder)
    return sorted(counter.items(), key=rank)[0][0] if counter else None


def _first_existing(perf_dir, candidates):
    """The first candidate folder that exists, spelled the way it is on disk."""
    try:
        on_disk = {name.lower(): name for name in os.listdir(perf_dir)}
    except OSError:
        on_disk = {}
    for candidate in candidates:
        if os.path.isdir(os.path.join(perf_dir, candidate)):
            head, _, rest = candidate.partition("/")
            return on_disk.get(head.lower(), head) + (f"/{rest}" if rest else "")
    return None


def _scan_locust(perf_dir):
    scripts, journeys = [], []
    read = 0
    for current, files in walk(perf_dir):
        for name in sorted(files):
            if not name.endswith(".py") or name.startswith("_") or name == CHECK_HOOK:
                continue
            if name in ("conftest.py", "setup.py") or read >= MAX_FILES_READ:
                continue
            read += 1
            path = os.path.join(current, name)
            text = _read_head(path)
            if is_locust_source(text):
                scripts.append(path)
            elif name.startswith("test_") and is_ui_journey_source(text):
                journeys.append(path)
    return scripts, journeys


def _pom_plans_root(perf_dir):
    pom = os.path.join(perf_dir, "pom.xml")
    if not os.path.isfile(pom):
        return None
    match = _POM_TEST_FILES.search(_read_head(pom, 400_000))
    if not match:
        return MAVEN_JMETER_ROOT if "jmeter-maven-plugin" in _read_head(pom, 400_000) else None
    value = match.group(1).replace("${project.basedir}", "").replace("${basedir}", "")
    value = value.strip().lstrip("/\\")
    return value.replace("\\", "/").rstrip("/") or ""


def _scan_jmeter(perf_dir):
    plans = []
    for current, files in walk(perf_dir):
        for name in sorted(files):
            if name.lower().endswith(".jmx") and not name.startswith(GENERATED_PREFIX):
                plans.append(os.path.join(current, name))
    return plans


def _common_folder(perf_dir, paths):
    folders = [os.path.dirname(p) for p in paths]
    try:
        common = os.path.commonpath(folders)
    except ValueError:
        return ""
    return _relative(perf_dir, common)


def _type_folders(perf_dir, plans_root):
    """api/cli/functional sub-folders of the plans root, matched case-insensitively."""
    root = os.path.join(perf_dir, plans_root)
    existing = {}
    if os.path.isdir(root):
        for entry in os.listdir(root):
            if os.path.isdir(os.path.join(root, entry)) and entry.lower() in JMETER_TYPE_FOLDERS:
                existing[entry.lower()] = entry
    folders = {}
    for kind in JMETER_TYPE_FOLDERS:
        sub = existing.get(kind)
        if sub:
            folders[kind] = f"{plans_root}/{sub}".strip("/")
        elif existing or not os.path.isdir(root) or plans_root == DEFAULT_JMETER_ROOT:
            # A typed layout (or a brand new one): give this type its own folder.
            folders[kind] = f"{plans_root}/{kind}".strip("/")
        else:
            # A flat folder of plans: new plans go next to the existing ones.
            folders[kind] = plans_root
    return folders


def _detect(perf_dir, tool):
    layout = {"perf_dir": perf_dir, "tool": tool}
    layout["results_dir"] = _first_existing(perf_dir, _RESULTS_CANDIDATES) or (
        "Results" if tool == JMETER_TOOL else "results")
    layout["data_dir"] = _first_existing(perf_dir, _DATA_CANDIDATES[tool]) or (
        "Payloads" if tool == JMETER_TOOL else "data")

    if tool == JMETER_TOOL:
        plans = _scan_jmeter(perf_dir)
        configured = _pom_plans_root(perf_dir)
        if configured is not None and os.path.isdir(os.path.join(perf_dir, configured)):
            root = configured
        elif plans:
            root = _common_folder(perf_dir, plans)
            # Plans that sit directly in api/ cli/ functional/ belong to their parent.
            if root.rsplit("/", 1)[-1].lower() in JMETER_TYPE_FOLDERS:
                root = root.rsplit("/", 1)[0] if "/" in root else ""
        else:
            root = configured if configured is not None else (
                _first_existing(perf_dir, _JMETER_ROOT_CANDIDATES) or DEFAULT_JMETER_ROOT)
        layout.update({
            "plans_root": root,
            "plans": plans,
            "type_dirs": _type_folders(perf_dir, root),
            "script_dirs": sorted({_relative(perf_dir, os.path.dirname(p)) for p in plans}),
        })
        return layout

    scripts, journeys = _scan_locust(perf_dir)
    counts = Counter(_relative(perf_dir, os.path.dirname(p)) for p in scripts)
    script_dir = _prefer(counts, _LOCUST_DIR_CANDIDATES)
    if script_dir is None:
        script_dir = _first_existing(perf_dir, _LOCUST_DIR_CANDIDATES) or DEFAULT_LOCUST_DIR
    layout.update({
        "script_dir": script_dir,
        "scripts": scripts,
        "journeys": journeys,
        "script_dirs": sorted(set(counts) | {_relative(perf_dir, os.path.dirname(p)) for p in journeys}),
    })
    return layout


def detect_layout(perf_dir, tool=LOCUST_TOOL, fresh=False):
    """
    Describe where `perf_dir` keeps its scripts. Cached for a few seconds, as
    one page action resolves the same project several times.

    Locust keys : script_dir, scripts, journeys, script_dirs, results_dir, data_dir
    JMeter keys : plans_root, plans, type_dirs, script_dirs, results_dir, data_dir
    (folders are relative to perf_dir with '/' separators; '' is the root itself;
    scripts / journeys / plans are absolute paths)
    """
    tool = JMETER_TOOL if tool == JMETER_TOOL else LOCUST_TOOL
    key = (os.path.normcase(os.path.abspath(perf_dir or "")), tool)
    now = time.monotonic()
    with _cache_lock:
        cached = _cache.get(key)
        if cached and not fresh and now - cached[0] < _CACHE_SECONDS:
            return cached[1]
    layout = _detect(perf_dir, tool) if perf_dir and os.path.isdir(perf_dir) else {
        "perf_dir": perf_dir, "tool": tool, "script_dir": DEFAULT_LOCUST_DIR, "scripts": [],
        "journeys": [], "plans_root": DEFAULT_JMETER_ROOT, "plans": [],
        "type_dirs": {k: f"{DEFAULT_JMETER_ROOT}/{k}" for k in JMETER_TYPE_FOLDERS},
        "script_dirs": [], "results_dir": "results", "data_dir": "data"}
    with _cache_lock:
        _cache[key] = (now, layout)
    return layout


def invalidate(perf_dir=None):
    """Forget cached layouts - call after a script is written or deleted."""
    with _cache_lock:
        if perf_dir is None:
            _cache.clear()
            return
        target = os.path.normcase(os.path.abspath(perf_dir))
        for key in [k for k in _cache if k[0] == target]:
            _cache.pop(key, None)


def abs_dir(perf_dir, relative):
    return os.path.normpath(os.path.join(perf_dir, *[p for p in (relative or "").split("/") if p]))


def locust_script_dir(perf_dir):
    """Absolute folder new Locust scripts (API, CLI and Functional) are written to."""
    return abs_dir(perf_dir, detect_layout(perf_dir, LOCUST_TOOL)["script_dir"])


def jmeter_type_dir(perf_dir, script_type):
    """Absolute folder a new JMeter plan of `script_type` (API / CLI / Functional) goes to."""
    layout = detect_layout(perf_dir, JMETER_TOOL)
    return abs_dir(perf_dir, layout["type_dirs"].get(str(script_type or "").lower(), layout["plans_root"]))


def jmeter_plans_root(perf_dir):
    return abs_dir(perf_dir, detect_layout(perf_dir, JMETER_TOOL)["plans_root"])


def results_dir(perf_dir, tool=LOCUST_TOOL):
    return abs_dir(perf_dir, detect_layout(perf_dir, tool)["results_dir"])


def data_dir(perf_dir, tool=LOCUST_TOOL):
    return abs_dir(perf_dir, detect_layout(perf_dir, tool)["data_dir"])


def relative_folder(perf_dir, folder):
    """`folder` relative to the project with '/' separators ('' for the root)."""
    return _relative(perf_dir, folder)


def depth_below_root(perf_dir, folder):
    """How many folders `folder` sits below the project root (0 for the root itself)."""
    rel = _relative(perf_dir, folder)
    return len([p for p in rel.split("/") if p and p != "."])


def all_script_paths(perf_dir, tool=LOCUST_TOOL):
    """Every runnable script of the project (Locust scripts + UI journeys, or JMeter plans)."""
    layout = detect_layout(perf_dir, tool)
    if tool == JMETER_TOOL:
        return list(layout["plans"])
    return list(layout["scripts"]) + list(layout["journeys"])
