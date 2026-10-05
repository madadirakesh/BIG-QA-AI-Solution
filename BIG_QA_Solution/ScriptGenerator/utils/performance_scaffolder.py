"""
Scaffold a Locust or JMeter performance project from the bundled framework templates.

A scaffolded performance project lives in `<project path>/<project name>_perf`
and is a copy of `scripts/templates/Locust_framework` (Locust) or
`scripts/templates/jmeter-framework` (JMeter) with the project's own
application URL and load defaults written into its configuration:
the Locust suite YAMLs, or the JMeter `pom.xml` properties.

A project can also point at a framework the user already has ("Framework
Exists"). Then the project path *is* the framework root, nothing is copied,
and `detect_framework_tools` is used to check that the folder really holds a
framework for the selected tool.

Scaffolding also provisions the project's dependencies, mirroring what the
Script Developer page does for an automation project built from scratch: the
same pre-flight system check, then the same EnvironmentSetup install
pipeline, pointed at the performance project's root folder:
  - Locust: Python/Pip, then Locust and the rest of requirements.txt land in `<perf project>/.venv`.
  - JMeter: JDK 17+/Maven, then Maven resolves the plugins and downloads JMeter.
"""

import logging
import os
import re
import shutil
import subprocess
from math import ceil
from pathlib import Path
from urllib.parse import urlparse

from ProjectBootstrapper.environment_setup import EnvironmentSetup

LOCUST_TOOL = "Locust"
JMETER_TOOL = "Jmeter"
PERFORMANCE_TOOLS = (LOCUST_TOOL, JMETER_TOOL)

TEMPLATES_ROOT = Path(__file__).resolve().parent.parent / "scripts" / "templates"
TEMPLATE_DIRS = {
    LOCUST_TOOL: TEMPLATES_ROOT / "Locust_framework",
    JMETER_TOOL: TEMPLATES_ROOT / "jmeter-framework",
}
TEMPLATE_DIR = TEMPLATE_DIRS[LOCUST_TOOL]
PERF_DIR_SUFFIX = "_perf"
TEST_CONFIG_SUBDIR = Path("config") / "test_configs"
POM_FILE = "pom.xml"

# The performance runner (performance_runner.resolve_python) looks for `.venv`
# inside the project root, so that is the folder the install must create. The
# automation templates use plain `venv`; the difference is why the installer
# takes the folder name as a parameter.
VENV_DIR_NAME = ".venv"
REQUIREMENTS_FILE = "requirements.txt"

# The JMeter template compiles with <maven.compiler.release>17</maven.compiler.release>.
JMETER_MIN_JAVA = "17"

# The steady-state suite mirrors the project's default users / spawn rate /
# duration. The smoke, spike, stress and soak suites keep their own deliberate
# load shapes and only get the application URL.
BASELINE_CONFIG = "load_test.yaml"

_INVALID_DIR_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')
_WHITESPACE = re.compile(r"\s+")

# Framework detection walks a user's folder, so keep it shallow and skip
# environments/build output that can be huge and never decide the tool.
_DETECT_MAX_DEPTH = 4
_DETECT_MAX_PY_FILES = 200
_DETECT_SKIP_DIRS = {
    ".venv", "venv", "env", ".git", ".idea", ".vscode", "__pycache__",
    "node_modules", "target", "results", "Results", "build", "dist",
}
_LOCUST_IMPORT = re.compile(r"^\s*(?:from\s+locust\b|import\s+locust\b)", re.MULTILINE)
# A requirement line (`locust>=2.28`) or a quoted dependency (`"locust>=2.28"` in pyproject).
_LOCUST_REQUIREMENT = re.compile(r"""^\s*["']?locust\b""", re.IGNORECASE | re.MULTILINE)


def normalize_tool(value):
    """Return the canonical tool name ('Locust' / 'Jmeter') for `value`, or '' if unknown."""
    text = (value or "").strip().lower()
    for tool in PERFORMANCE_TOOLS:
        if text == tool.lower():
            return tool
    return ""


def performance_dir_name(project_name):
    """Return the `<project name>_perf` folder name, safe for the filesystem."""
    name = _INVALID_DIR_CHARS.sub("_", (project_name or "").strip())
    name = _WHITESPACE.sub("_", name).strip("._")
    if not name:
        name = "performance"
    return f"{name}{PERF_DIR_SUFFIX}"


def performance_dir_path(project_name, project_path, framework_exists=False):
    """
    Return the absolute path of the performance project folder, or '' if no base path.

    A scaffolded project lives in `<project path>/<project name>_perf`; an
    existing framework is used in place, so its path is the project path itself.
    """
    base = (project_path or "").strip()
    if not base:
        return ""
    if framework_exists:
        return os.path.normpath(base)
    return os.path.normpath(os.path.join(base, performance_dir_name(project_name)))


# ---------------------------------------------------------------------------
# Framework detection
# ---------------------------------------------------------------------------

def _read_text(path, limit=200_000):
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as handle:
            return handle.read(limit)
    except OSError:
        return ""


def detect_framework_tools(folder):
    """
    Return the performance tools whose framework markers are present in `folder`,
    in PERFORMANCE_TOOLS order (usually one entry; empty when nothing is recognised).

    JMeter: a `*.jmx` test plan, or a pom.xml / build.gradle using JMeter.
    Locust: a requirements/pyproject entry for locust, a locustfile, or a Python
            module that imports locust.
    """
    root = Path(folder)
    if not root.is_dir():
        return []

    found = set()
    py_files_read = 0
    root_depth = len(root.parts)

    for current, dirs, files in os.walk(root):
        depth = len(Path(current).parts) - root_depth
        dirs[:] = [d for d in dirs if d not in _DETECT_SKIP_DIRS and not d.startswith(".")]
        if depth >= _DETECT_MAX_DEPTH:
            dirs[:] = []

        for name in files:
            lower = name.lower()
            path = os.path.join(current, name)

            if JMETER_TOOL not in found:
                if lower.endswith(".jmx"):
                    found.add(JMETER_TOOL)
                elif lower in ("pom.xml", "build.gradle", "build.gradle.kts") and \
                        "jmeter" in _read_text(path).lower():
                    found.add(JMETER_TOOL)

            if LOCUST_TOOL not in found:
                if lower.startswith("locustfile") and lower.endswith(".py"):
                    found.add(LOCUST_TOOL)
                elif (lower.startswith("requirements") and lower.endswith(".txt")) or \
                        lower in ("pyproject.toml", "pipfile", "setup.py", "setup.cfg"):
                    if _LOCUST_REQUIREMENT.search(_read_text(path)):
                        found.add(LOCUST_TOOL)
                elif lower.endswith(".py") and py_files_read < _DETECT_MAX_PY_FILES:
                    py_files_read += 1
                    if _LOCUST_IMPORT.search(_read_text(path, limit=20_000)):
                        found.add(LOCUST_TOOL)

        if len(found) == len(PERFORMANCE_TOOLS):
            break

    return [tool for tool in PERFORMANCE_TOOLS if tool in found]


# ---------------------------------------------------------------------------
# System prerequisites
# ---------------------------------------------------------------------------

def check_performance_dependencies(tool=LOCUST_TOOL):
    """
    Pre-flight the system tools the selected performance framework needs, using
    the same report the Script Developer page renders before building a
    framework from scratch. Only prerequisites the user has to install
    themselves are listed - Locust (a pip package) and JMeter (downloaded by
    Maven) are provisioned by the install step.

      Locust: Python, Pip
      JMeter: Java (JDK 17+), Apache Maven

    Returns (ok: bool, report: list[dict]) where each report entry carries
    name / required / detected / status / hint.
    """
    if normalize_tool(tool) == JMETER_TOOL:
        report = EnvironmentSetup.required_dependencies(
            tool="", language="Java", framework="", profile_versions={"java": JMETER_MIN_JAVA}
        )
    else:
        report = EnvironmentSetup.required_dependencies(tool="", language="Python", framework="")
    ok = all(entry.get("status") not in ("missing", "mismatch") for entry in report)
    return ok, report


def describe_dependency_problems(report):
    """One readable line per missing / wrong-version prerequisite in a dependency report."""
    problems = []
    for entry in report:
        status = entry.get("status")
        if status == "missing":
            problems.append(f"{entry['name']} is not installed or not on PATH - {entry['hint']}")
        elif status == "mismatch":
            problems.append(
                f"{entry['name']} {entry.get('detected') or '(unknown version)'} found, "
                f"{entry.get('required')}+ required - {entry['hint']}"
            )
    return "; ".join(problems)


def preflight_performance_project(tool):
    """
    Check everything that must be in place before a performance project for
    `tool` can be scaffolded and provisioned: a supported tool, its bundled
    template, and the system prerequisites.

    Returns (ok: bool, message: str, report: list[dict]).
    """
    canonical = normalize_tool(tool)
    if not canonical:
        return False, f"Unsupported performance tool '{tool}'. Choose Locust or Jmeter.", []

    template = TEMPLATE_DIRS[canonical]
    if not template.is_dir():
        return False, f"{canonical} framework template not found at {template}.", []

    deps_ok, report = check_performance_dependencies(canonical)
    if not deps_ok:
        return False, (
            f"System prerequisites for the {canonical} framework are not met: "
            f"{describe_dependency_problems(report)}"
        ), report
    return True, "", report


# ---------------------------------------------------------------------------
# Config overrides
# ---------------------------------------------------------------------------

def _quote_if_needed(value):
    text = str(value)
    return f'"{text}"' if _WHITESPACE.search(text) or text.startswith("#") else text


def _apply_yaml_overrides(file_path, overrides):
    """
    Rewrite `key: value` scalars in place, preserving indentation, key order,
    and trailing comments. Only keys present in `overrides` are touched, so the
    template's comments and any unrelated user edits survive.
    """
    if not overrides or not os.path.isfile(file_path):
        return False

    with open(file_path, "r", encoding="utf-8") as handle:
        lines = handle.readlines()

    changed = False
    for index, line in enumerate(lines):
        match = re.match(r"^(\s*(?:-\s+)?)([A-Za-z_][\w-]*):([ \t]*)([^#\n]*?)([ \t]*)(#.*)?(\r?\n?)$", line)
        if not match:
            continue
        indent, key, gap, _old_value, pad, comment, eol = match.groups()
        if key not in overrides:
            continue
        new_line = (
            f"{indent}{key}:{gap or ' '}{_quote_if_needed(overrides[key])}"
            f"{pad if comment else ''}{comment or ''}{eol or ''}"
        )
        if new_line != line:
            lines[index] = new_line
            changed = True

    if changed:
        with open(file_path, "w", encoding="utf-8", newline="") as handle:
            handle.writelines(lines)
    return changed


def _load_overrides(application_url, user_count, spawn_rate, run_duration):
    overrides = {}
    url = (application_url or "").strip().rstrip("/")
    if url:
        overrides["host"] = url
    if user_count:
        overrides["users"] = int(user_count)
    if spawn_rate:
        overrides["spawn_rate"] = int(spawn_rate)
    if run_duration:
        overrides["run_time"] = f"{int(run_duration)}m"
    return overrides


def _jmeter_overrides(application_url, user_count, spawn_rate, run_duration):
    """Map the project settings onto the JMeter template's `perf.*` pom properties."""
    overrides = {}
    url = (application_url or "").strip().rstrip("/")
    if url:
        parsed = urlparse(url)
        if parsed.hostname:
            overrides["perf.apiHost"] = parsed.hostname
        if parsed.scheme:
            overrides["perf.apiProtocol"] = parsed.scheme.lower()
        overrides["perf.pw.baseUrl"] = url
    if user_count:
        overrides["perf.threads"] = int(user_count)
        if spawn_rate:
            # JMeter ramps up over a duration rather than at a rate.
            overrides["perf.rampup"] = max(1, ceil(int(user_count) / int(spawn_rate)))
    if run_duration:
        overrides["perf.duration"] = int(run_duration) * 60
    return overrides


def _apply_pom_overrides(pom_path, overrides):
    """
    Rewrite `<perf.x>value</perf.x>` entries in the pom's top-level <properties>
    block. Only the first occurrence of each property is touched, so profile
    overrides further down the file are left alone.
    """
    if not overrides or not os.path.isfile(pom_path):
        return False

    with open(pom_path, "r", encoding="utf-8") as handle:
        content = handle.read()

    updated = content
    for key, value in overrides.items():
        tag = re.escape(key)
        updated = re.sub(
            rf"(<{tag}>)[^<]*(</{tag}>)",
            lambda m, v=str(value): f"{m.group(1)}{v}{m.group(2)}",
            updated,
            count=1,
        )

    if updated == content:
        return False
    with open(pom_path, "w", encoding="utf-8", newline="") as handle:
        handle.write(updated)
    return True


# ---------------------------------------------------------------------------
# Dependency install
# ---------------------------------------------------------------------------

def venv_python_path(perf_dir):
    """Absolute path of the interpreter inside the performance project's own venv."""
    relative = (
        Path(VENV_DIR_NAME) / "Scripts" / "python.exe" if os.name == "nt"
        else Path(VENV_DIR_NAME) / "bin" / "python"
    )
    return str(Path(perf_dir) / relative)


def _locust_version(python_exe):
    """Return the Locust version reported by `python_exe`, or '' when it is not installed."""
    if not os.path.isfile(python_exe):
        return ""
    try:
        probe = subprocess.run(
            [python_exe, "-c", "import locust; print(locust.__version__)"],
            capture_output=True, text=True, timeout=120,
        )
    except Exception:
        return ""
    return (probe.stdout or "").strip() if probe.returncode == 0 else ""


def _jmeter_provisioned(perf_dir):
    """True when the jmeter-maven-plugin has already unpacked JMeter under target/."""
    target = Path(perf_dir) / "target"
    if not target.is_dir():
        return False
    return any(target.glob("jmeter/bin/ApacheJMeter*.jar")) or \
        any(target.glob("*/jmeter/bin/ApacheJMeter*.jar"))


def _announcer(status_cb):
    def announce(message):
        if status_cb:
            try:
                status_cb(message)
            except Exception:
                logging.exception("status_cb raised while announcing %r", message)
    return announce


def _block_on_prerequisites(result, tool, announce):
    """Run the system pre-flight; fill `result` and return True when it fails."""
    announce("Checking Java and Maven..." if tool == JMETER_TOOL else "Checking Python and Pip...")
    deps_ok, report = check_performance_dependencies(tool)
    result["dependencies"] = report
    if deps_ok:
        return False
    result.update({
        "status": "blocked",
        "message": f"Cannot install the {tool} dependencies: {describe_dependency_problems(report)}",
    })
    return True


def _install_jmeter_dependencies(perf_dir, announce, force):
    result = {"status": "skipped", "message": "", "tool": JMETER_TOOL, "dependencies": []}

    pom_path = os.path.join(perf_dir, POM_FILE)
    if not os.path.isfile(pom_path):
        result["message"] = f"No {POM_FILE} in {perf_dir}; skipped the JMeter dependency download."
        return result
    if "jmeter-maven-plugin" not in _read_text(pom_path):
        # An existing framework may drive JMeter some other way; there is
        # nothing Maven can provision for it.
        result["message"] = (
            f"{POM_FILE} in {perf_dir} does not use the jmeter-maven-plugin; "
            f"skipped the JMeter dependency download."
        )
        return result

    if not force:
        announce("Checking the performance project's JMeter installation...")
        if _jmeter_provisioned(perf_dir):
            result.update({
                "status": "already_installed",
                "message": "JMeter and the Maven dependencies are already downloaded.",
            })
            return result

    if _block_on_prerequisites(result, JMETER_TOOL, announce):
        return result

    ok, output = EnvironmentSetup.install_project_dependencies(
        perf_dir, "JMeter", tool="", status_cb=announce
    )
    if not ok:
        result.update({"status": "failed", "message": f"JMeter dependency download failed.\n{output}"})
        return result

    result.update({
        "status": "installed",
        "message": f"JMeter and the Maven dependencies are ready in {perf_dir}.",
    })
    return result


def _install_locust_dependencies(perf_dir, announce, force):
    python_exe = venv_python_path(perf_dir)
    result = {"status": "skipped", "message": "", "tool": LOCUST_TOOL, "python": python_exe,
              "locust_version": "", "dependencies": []}

    if not os.path.isfile(os.path.join(perf_dir, REQUIREMENTS_FILE)):
        result["message"] = (
            f"No {REQUIREMENTS_FILE} in {perf_dir}; skipped the Locust install."
        )
        return result

    if not force:
        announce("Checking the performance project's Python environment...")
        existing = _locust_version(python_exe)
        if existing:
            result.update({
                "status": "already_installed",
                "locust_version": existing,
                "message": f"Locust {existing} is already installed in {VENV_DIR_NAME}.",
            })
            return result

    if _block_on_prerequisites(result, LOCUST_TOOL, announce):
        return result

    ok, output = EnvironmentSetup.install_project_dependencies(
        perf_dir, "Pip", tool="", status_cb=announce, venv_dir=VENV_DIR_NAME
    )
    if not ok:
        result.update({"status": "failed", "message": f"Locust install failed.\n{output}"})
        return result

    version = _locust_version(python_exe)
    if not version:
        result.update({
            "status": "failed",
            "message": (
                f"Dependencies installed into {VENV_DIR_NAME}, but Locust could not be "
                f"imported afterwards. Run \"{python_exe} -m pip install -r "
                f"{REQUIREMENTS_FILE}\" inside {perf_dir} to see the error."
            ),
        })
        return result

    result.update({
        "status": "installed",
        "locust_version": version,
        "message": f"Locust {version} installed in {os.path.join(perf_dir, VENV_DIR_NAME)}.",
    })
    return result


def install_performance_dependencies(perf_dir, status_cb=None, force=False, tool=LOCUST_TOOL):
    """
    Provision the performance project's dependencies, reusing EnvironmentSetup's
    install pipeline so the project gets the same proxy/TLS handling, per-phase
    status callbacks and network-failure hints as an automation project built
    from scratch.

      Locust: create `<perf_dir>/.venv` and install requirements.txt into it.
      JMeter: `mvn test-compile`, which resolves the Maven plugins and downloads JMeter.

    An environment that is already provisioned is left alone unless `force` is
    set, so re-saving the configuration of an existing project stays instant.

    Returns a dict:
        {"status": "installed" | "already_installed" | "skipped" | "blocked" | "failed",
         "message": str, "tool": str, "dependencies": [...], ...}
    """
    announce = _announcer(status_cb)
    canonical = normalize_tool(tool) or LOCUST_TOOL

    if not os.path.isdir(perf_dir):
        return {"status": "skipped", "tool": canonical, "dependencies": [],
                "message": f"Performance project folder does not exist: {perf_dir}"}

    if canonical == JMETER_TOOL:
        return _install_jmeter_dependencies(perf_dir, announce, force)
    return _install_locust_dependencies(perf_dir, announce, force)


# ---------------------------------------------------------------------------
# Scaffolding
# ---------------------------------------------------------------------------

def scaffold_performance_project(project_name, project_path, application_url,
                                 user_count=None, spawn_rate=None, run_duration=None,
                                 tool=LOCUST_TOOL):
    """
    Create (or refresh) `<project path>/<project name>_perf` from the selected
    tool's framework template and write the project's settings into its config.

    Raises ValueError / FileNotFoundError / NotADirectoryError with a readable
    message when the project cannot be scaffolded.

    Returns a dict describing what happened:
        {"scaffolded": bool, "created": bool, "path": str, "updated": [names], "message": str}
    """
    canonical = normalize_tool(tool)
    if not canonical:
        raise ValueError(f"Unsupported performance tool '{tool}'. Choose Locust or Jmeter.")

    target = performance_dir_path(project_name, project_path)
    if not target:
        return {
            "scaffolded": False,
            "created": False,
            "path": "",
            "updated": [],
            "message": "No Performance Project path provided; skipped framework scaffolding.",
        }

    template = TEMPLATE_DIRS[canonical]
    if not template.is_dir():
        raise FileNotFoundError(f"{canonical} framework template not found at {template}")

    base_dir = os.path.dirname(target)
    if base_dir and not os.path.isdir(base_dir):
        raise NotADirectoryError(f"Performance Project path does not exist: {base_dir}")

    created = not os.path.exists(target)
    if created:
        try:
            shutil.copytree(template, target)
        except Exception:
            # Never leave a half-copied framework behind: the next save would
            # treat it as an existing project and skip the copy.
            shutil.rmtree(target, ignore_errors=True)
            raise
    else:
        if not os.path.isdir(target):
            raise NotADirectoryError(f"{target} exists and is not a folder.")
        # An existing folder is never re-copied - scripts, core modules and data
        # the user has already tailored must survive. It must however hold a
        # framework for the selected tool, or re-syncing would mix the two.
        existing = detect_framework_tools(target)
        if existing and canonical not in existing:
            raise ValueError(
                f"{target} already contains a {' / '.join(existing)} framework, but the "
                f"selected tool is {canonical}. Choose {existing[0]} or use a different "
                f"project name / path."
            )

    updated = []
    if canonical == JMETER_TOOL:
        overrides = _jmeter_overrides(application_url, user_count, spawn_rate, run_duration)
        if _apply_pom_overrides(os.path.join(target, POM_FILE), overrides):
            updated.append(POM_FILE)
    else:
        url_only = _load_overrides(application_url, None, None, None)
        baseline = _load_overrides(application_url, user_count, spawn_rate, run_duration)
        config_dir = Path(target) / TEST_CONFIG_SUBDIR
        for config_file in sorted(config_dir.glob("*.yaml")):
            overrides = baseline if config_file.name == BASELINE_CONFIG else url_only
            if _apply_yaml_overrides(str(config_file), overrides):
                updated.append(config_file.name)

    return {
        "scaffolded": True,
        "created": created,
        "path": target,
        "updated": updated,
        "message": (
            f"{canonical} performance framework scaffolded at {target}."
            if created else
            f"Existing {canonical} performance framework at {target} re-synced with these settings."
        ),
    }
