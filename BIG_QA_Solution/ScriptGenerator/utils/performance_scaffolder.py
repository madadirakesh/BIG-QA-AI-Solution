"""
Scaffold a Locust performance project from the bundled framework template.

A performance project lives in `<project path>/<project name>_perf` and is a
copy of `scripts/templates/performance_framework` with the project's own
application URL and load defaults written into the suite configs.

Scaffolding also provisions the project's own Python environment, mirroring
what the Script Developer page does for an automation project built from
scratch: the same pre-flight Python/Pip check, then the same
EnvironmentSetup install pipeline, pointed at the performance project's root
folder so Locust and the rest of requirements.txt land in `<perf project>/.venv`.
"""

import logging
import os
import re
import shutil
import subprocess
from pathlib import Path

from ProjectBootstrapper.environment_setup import EnvironmentSetup

TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "scripts" / "templates" / "performance_framework"
PERF_DIR_SUFFIX = "_perf"
TEST_CONFIG_SUBDIR = Path("config") / "test_configs"

# The performance runner (performance_runner.resolve_python) looks for `.venv`
# inside the project root, so that is the folder the install must create. The
# automation templates use plain `venv`; the difference is why the installer
# takes the folder name as a parameter.
VENV_DIR_NAME = ".venv"
REQUIREMENTS_FILE = "requirements.txt"

# The steady-state suite mirrors the project's default users / spawn rate /
# duration. The smoke, spike, stress and soak suites keep their own deliberate
# load shapes and only get the application URL.
BASELINE_CONFIG = "load_test.yaml"

_INVALID_DIR_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')
_WHITESPACE = re.compile(r"\s+")


def performance_dir_name(project_name):
    """Return the `<project name>_perf` folder name, safe for the filesystem."""
    name = _INVALID_DIR_CHARS.sub("_", (project_name or "").strip())
    name = _WHITESPACE.sub("_", name).strip("._")
    if not name:
        name = "performance"
    return f"{name}{PERF_DIR_SUFFIX}"


def performance_dir_path(project_name, project_path):
    """Return the absolute path of the performance project folder, or '' if no base path."""
    base = (project_path or "").strip()
    if not base:
        return ""
    return os.path.normpath(os.path.join(base, performance_dir_name(project_name)))


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


def check_performance_dependencies():
    """
    Pre-flight the system tools the performance framework needs, using the same
    report the Script Developer page renders before building a framework from
    scratch. Locust is a pip package, so it is deliberately not listed here -
    only the prerequisites the user has to install themselves (Python, Pip).

    Returns (ok: bool, report: list[dict]) where each report entry carries
    name / required / detected / status / hint.
    """
    report = EnvironmentSetup.required_dependencies(tool="", language="Python", framework="")
    ok = all(entry.get("status") != "missing" for entry in report)
    return ok, report


def install_performance_dependencies(perf_dir, status_cb=None, force=False):
    """
    Create `<perf_dir>/.venv` and install requirements.txt (Locust and friends)
    into it, reusing EnvironmentSetup's install pipeline so the performance
    project gets the same proxy/TLS handling, per-phase status callbacks and
    network-failure hints as an automation project built from scratch.

    An environment that already has Locust is left alone unless `force` is set,
    so re-saving the configuration of an existing project stays instant.

    Returns a dict:
        {"status": "installed" | "already_installed" | "skipped" | "blocked" | "failed",
         "message": str, "python": str, "locust_version": str, "dependencies": [...]}
    """
    def announce(message):
        if status_cb:
            try:
                status_cb(message)
            except Exception:
                logging.exception("status_cb raised while announcing %r", message)

    python_exe = venv_python_path(perf_dir)
    result = {"status": "skipped", "message": "", "python": python_exe,
              "locust_version": "", "dependencies": []}

    if not os.path.isdir(perf_dir):
        result["message"] = f"Performance project folder does not exist: {perf_dir}"
        return result

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

    announce("Checking Python and Pip...")
    deps_ok, report = check_performance_dependencies()
    result["dependencies"] = report
    if not deps_ok:
        missing = [entry for entry in report if entry.get("status") == "missing"]
        details = "; ".join(f"{entry['name']} - {entry['hint']}" for entry in missing)
        result.update({
            "status": "blocked",
            "message": f"Cannot install Locust: {details}",
        })
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


def scaffold_performance_project(project_name, project_path, application_url,
                                 user_count=None, spawn_rate=None, run_duration=None):
    """
    Create (or refresh) `<project path>/<project name>_perf` from the framework
    template and write the project's settings into the suite configs.

    Returns a dict describing what happened:
        {"scaffolded": bool, "created": bool, "path": str, "updated": [names], "message": str}
    """
    target = performance_dir_path(project_name, project_path)
    if not target:
        return {
            "scaffolded": False,
            "created": False,
            "path": "",
            "updated": [],
            "message": "No Performance Project path provided; skipped framework scaffolding.",
        }

    if not TEMPLATE_DIR.is_dir():
        raise FileNotFoundError(f"Performance framework template not found at {TEMPLATE_DIR}")

    base_dir = os.path.dirname(target)
    if base_dir and not os.path.isdir(base_dir):
        raise NotADirectoryError(f"Performance Project path does not exist: {base_dir}")

    created = not os.path.exists(target)
    if created:
        shutil.copytree(TEMPLATE_DIR, target)
    # An existing folder is never re-copied - locustfiles, core modules and data
    # the user has already tailored must survive. Only the suite configs below
    # are re-synced with the current project settings.

    url_only = _load_overrides(application_url, None, None, None)
    baseline = _load_overrides(application_url, user_count, spawn_rate, run_duration)

    updated = []
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
            f"Performance framework scaffolded at {target}."
            if created else
            f"Existing performance framework at {target} re-synced with these settings."
        ),
    }
