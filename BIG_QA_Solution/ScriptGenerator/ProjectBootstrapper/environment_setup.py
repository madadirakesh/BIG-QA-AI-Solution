import subprocess
import platform
import os
import re
import logging
import shlex
from pathlib import Path
import shutil

class EnvironmentSetup:
    @staticmethod
    def is_windows():
        return platform.system().lower() == "windows"

    @staticmethod
    def is_mac():
        return platform.system().lower() == "darwin"

    @classmethod
    def _augmented_probe_env(cls, check_cmd, base_env=None):
        """Mirror common interactive shell PATH setup for dependency probes.

        The app can be launched from a GUI, service, IDE, or an older shell session whose PATH
        does not match a freshly opened terminal. That is most noticeable with Maven: `mvn -v`
        works in the user's terminal, but the app reports "Not found". To narrow that gap we
        supplement PATH with well-known tool-home variables and standard install locations across
        Windows, macOS, and Linux before running the probe.
        """
        env = dict(base_env or os.environ.copy())

        tool = (check_cmd or "").strip().split()[0].lower()
        candidate_dirs = []

        def add_dir(path_str, include_bin=True):
            if not path_str:
                return
            path = Path(path_str)
            if include_bin and path.name.lower() != "bin":
                path = path / "bin"
            try:
                if path.is_dir():
                    resolved = str(path.resolve())
                    if resolved not in candidate_dirs:
                        candidate_dirs.append(resolved)
            except OSError:
                return

        def add_globbed_dirs(root_str, patterns, include_bin=True):
            if not root_str:
                return
            root_path = Path(root_str)
            for pattern in patterns:
                try:
                    for match in root_path.glob(pattern):
                        add_dir(str(match), include_bin=include_bin)
                except OSError:
                    continue

        def add_common_unix_bins():
            for known_bin in (
                "/opt/homebrew/bin",
                "/usr/local/bin",
                "/usr/bin",
                "/opt/bin",
                "/snap/bin",
            ):
                add_dir(known_bin, include_bin=False)

        if tool == "mvn":
            for env_key in ("MAVEN_HOME", "M2_HOME", "M2"):
                add_dir(env.get(env_key))

            # SDKMAN is a common source of "works in terminal, missing in app" on macOS/Linux
            # because shell startup files export it, but GUI-launched apps never source them.
            add_dir(env.get("SDKMAN_CANDIDATES_DIR") and str(Path(env["SDKMAN_CANDIDATES_DIR"]) / "maven" / "current"))
            add_dir(str(Path.home() / ".sdkman" / "candidates" / "maven" / "current"))

            if cls.is_windows():
                program_roots = [
                    env.get("ProgramFiles"),
                    env.get("ProgramFiles(x86)"),
                    env.get("ProgramW6432"),
                    "C:\\",
                    str(Path.home()),
                ]
                for root in filter(None, program_roots):
                    add_globbed_dirs(root, ("apache-maven*", "maven*"))
            else:
                add_common_unix_bins()

                for root in (
                    "/opt",
                    "/usr/local",
                    "/usr/share",
                    str(Path.home()),
                ):
                    add_globbed_dirs(root, ("apache-maven*", "maven*", "apache-maven-*"))

        if tool == "java":
            add_dir(env.get("JAVA_HOME"))
            if cls.is_windows():
                program_roots = [
                    env.get("ProgramFiles"),
                    env.get("ProgramFiles(x86)"),
                    env.get("ProgramW6432"),
                    "C:\\",
                    str(Path.home()),
                ]
                for root in filter(None, program_roots):
                    add_globbed_dirs(root, ("jdk*", "java*", "temurin*", "zulu*", "adoptopenjdk*"))
            else:
                add_common_unix_bins()

        if tool in ("python", "python3", "pip", "pip3", "py"):
            for env_key in ("PYENV_ROOT",):
                root = env.get(env_key)
                if root:
                    add_dir(str(Path(root) / "shims"), include_bin=False)
                    add_dir(str(Path(root) / "bin"), include_bin=False)
            add_dir(str(Path.home() / ".pyenv" / "shims"), include_bin=False)
            add_dir(str(Path.home() / ".pyenv" / "bin"), include_bin=False)
            add_dir(str(Path.home() / ".asdf" / "shims"), include_bin=False)

            conda_prefix = env.get("CONDA_PREFIX")
            if conda_prefix:
                add_dir(conda_prefix, include_bin=not cls.is_windows())
                if cls.is_windows():
                    add_dir(str(Path(conda_prefix) / "Scripts"), include_bin=False)

            if cls.is_windows():
                local_appdata = env.get("LOCALAPPDATA")
                appdata = env.get("APPDATA")
                for root in filter(None, (local_appdata, appdata)):
                    add_globbed_dirs(root, ("Programs/Python/Python*", "Python/Python*"), include_bin=False)
                add_dir(str(Path.home() / "AppData" / "Local" / "Programs" / "Python"), include_bin=False)
            else:
                add_common_unix_bins()
                add_dir(str(Path.home() / ".local" / "bin"), include_bin=False)

        if tool in ("node", "npm", "npx"):
            for env_key in ("NVM_BIN", "NVM_SYMLINK"):
                add_dir(env.get(env_key), include_bin=False)
            volta_home = env.get("VOLTA_HOME")
            if volta_home:
                add_dir(str(Path(volta_home) / "bin"), include_bin=False)
            fnm_dir = env.get("FNM_DIR")
            if fnm_dir:
                add_globbed_dirs(str(Path(fnm_dir) / "aliases"), ("default/bin", "*/bin"))
            add_dir(str(Path.home() / ".nvm" / "versions" / "node"), include_bin=False)
            add_globbed_dirs(str(Path.home() / ".nvm" / "versions" / "node"), ("*/bin",))
            add_dir(str(Path.home() / ".volta" / "bin"), include_bin=False)
            add_dir(str(Path.home() / ".asdf" / "shims"), include_bin=False)
            if cls.is_windows():
                appdata = env.get("APPDATA")
                local_appdata = env.get("LOCALAPPDATA")
                for root in filter(None, (appdata, local_appdata)):
                    add_dir(str(Path(root) / "npm"), include_bin=False)
                    add_globbed_dirs(root, ("nvm/*", "Programs/nodejs"), include_bin=False)
                add_dir(str(Path.home() / "AppData" / "Roaming" / "npm"), include_bin=False)
            else:
                add_common_unix_bins()
                add_dir(str(Path.home() / ".local" / "bin"), include_bin=False)

        if tool == "dotnet":
            for env_key in ("DOTNET_ROOT", "DOTNET_ROOT(x86)"):
                add_dir(env.get(env_key), include_bin=False)
            if cls.is_windows():
                for root in filter(None, (env.get("ProgramFiles"), env.get("ProgramFiles(x86)"), env.get("ProgramW6432"))):
                    add_dir(str(Path(root) / "dotnet"), include_bin=False)
                add_dir(str(Path.home() / "AppData" / "Local" / "Microsoft" / "dotnet"), include_bin=False)
            else:
                add_common_unix_bins()
                add_dir(str(Path.home() / ".dotnet"), include_bin=False)
                add_dir("/usr/share/dotnet", include_bin=False)
                add_dir("/usr/local/share/dotnet", include_bin=False)

        if candidate_dirs:
            existing_path = env.get("PATH", "")
            merged = candidate_dirs + ([existing_path] if existing_path else [])
            env["PATH"] = os.pathsep.join(merged)
        return env

    @staticmethod
    def _home_from_executable(executable_path):
        if not executable_path:
            return None
        path = Path(executable_path)
        parent = path.parent
        if parent.name.lower() == "bin":
            return str(parent.parent)
        return str(parent)

    @staticmethod
    def _is_valid_java_home(java_home):
        if not java_home:
            return False
        root = Path(java_home)
        for name in ("java", "java.exe"):
            if (root / "bin" / name).exists():
                return True
        return False

    @classmethod
    def prepare_runtime_env(cls, command, base_env=None):
        """Build a subprocess environment that can actually launch the requested tool.

        The runtime executor differs from the preflight probe in one important way: wrappers such
        as `mvn` consult JAVA_HOME directly and fail hard when it points at a stale install, even
        if `java` itself is discoverable on PATH. Reuse the same path augmentation logic we use
        for dependency detection, then normalise JAVA_HOME / MAVEN_HOME from the resolved
        executable paths when they are missing or invalid.
        """
        env = cls._augmented_probe_env(command, base_env=base_env)
        java_env = cls._augmented_probe_env("java -version", base_env=env)
        env["PATH"] = java_env.get("PATH", env.get("PATH", ""))

        java_path = cls._resolve_command_path("java -version", env)
        if java_path and not cls._is_valid_java_home(env.get("JAVA_HOME")):
            env["JAVA_HOME"] = cls._home_from_executable(java_path)

        mvn_path = cls._resolve_command_path("mvn -version", env)
        if mvn_path:
            mvn_home = cls._home_from_executable(mvn_path)
            if mvn_home and not env.get("MAVEN_HOME"):
                env["MAVEN_HOME"] = mvn_home
            if mvn_home and not env.get("M2_HOME"):
                env["M2_HOME"] = mvn_home

        # Stale JAVA_HOME is worse than no JAVA_HOME for Maven/Gradle wrappers. If we still
        # couldn't resolve a valid JDK, remove the broken value so downstream tools can fall back
        # to PATH or surface a cleaner "java not found" error.
        if env.get("JAVA_HOME") and not cls._is_valid_java_home(env.get("JAVA_HOME")):
            env.pop("JAVA_HOME", None)

        return env

    @classmethod
    def _command_visible_in_env(cls, check_cmd, env):
        tool = (check_cmd or "").strip().split()[0]
        # `python -m pip`/`py -m pip` require a module in addition to the launcher itself; the
        # presence of `python` alone must not be mistaken for a valid pip installation.
        if " -m " in f" {check_cmd} ":
            return False
        return bool(tool and shutil.which(tool, path=env.get("PATH")))

    @classmethod
    def _resolve_command_path(cls, check_cmd, env):
        tool = (check_cmd or "").strip().split()[0]
        if not tool:
            return None
        resolved = shutil.which(tool, path=env.get("PATH"))
        return str(Path(resolved).resolve()) if resolved else None

    @classmethod
    def _python_check_commands(cls):
        if cls.is_windows():
            return [
                "py -3.14 --version",
                "py -3.13 --version",
                "py -3.12 --version",
                "py -3 --version",
                "python3.14 --version",
                "python3 --version",
                "python --version",
            ]
        return [
            "python3.14 --version",
            "python3.13 --version",
            "python3.12 --version",
            "python3 --version",
            "python --version",
        ]

    @classmethod
    def _pip_check_commands(cls):
        if cls.is_windows():
            return [
                "py -3.14 -m pip --version",
                "py -3.13 -m pip --version",
                "py -3.12 -m pip --version",
                "py -3 -m pip --version",
                "python -m pip --version",
                "pip --version",
            ]
        return [
            "python3.14 -m pip --version",
            "python3.13 -m pip --version",
            "python3.12 -m pip --version",
            "python3 -m pip --version",
            "python -m pip --version",
            "pip3 --version",
            "pip --version",
        ]

    @classmethod
    def _python_launcher_command(cls):
        installed, _, executable_path, command_used = cls._probe(cls._python_check_commands())
        if executable_path:
            return f'"{executable_path}"' if cls.is_windows() else shlex.quote(executable_path)
        if command_used:
            return command_used.strip().split()[0]
        return "python" if cls.is_windows() else "python3"

    @classmethod
    def _try_check_commands(cls, check_cmds):
        commands = check_cmds if isinstance(check_cmds, (list, tuple)) else [check_cmds]
        saw_visible_binary = False
        for cmd in commands:
            env = cls._augmented_probe_env(cmd)
            try:
                subprocess.run(
                    cmd,
                    shell=True,
                    check=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env=env,
                )
                return True
            except (subprocess.CalledProcessError, FileNotFoundError):
                saw_visible_binary = saw_visible_binary or cls._command_visible_in_env(cmd, env)
        return saw_visible_binary

    @staticmethod
    def check_system_dependency(dep_name, check_cmd):
        return EnvironmentSetup._try_check_commands(check_cmd)
            
    @classmethod
    def verify_environment(cls, language):
        missing = []
        if language == "Java":
            if not cls.check_system_dependency("Java", "java -version"): missing.append("Java")
            if not cls.check_system_dependency("Maven", "mvn -version"): missing.append("Maven")
        elif language == "Python":
            py_cmd = cls._python_check_commands()
            pip_cmd = cls._pip_check_commands()
            if not cls.check_system_dependency("Python", py_cmd): missing.append("Python")
            if not cls.check_system_dependency("Pip", pip_cmd): missing.append("Pip")
        elif language in ["JS / TS", "JavaScript", "TypeScript"]:
            if not cls.check_system_dependency("Node", "node -v"): missing.append("Node.js")
            if not cls.check_system_dependency("NPM", "npm -v"): missing.append("NPM")
        elif language == "C#":
            if not cls.check_system_dependency("Dotnet", "dotnet --version"): missing.append("Dotnet CLI")
            
        if missing:
            return False, missing
        return True, []

    # ── Pre-flight dependency inspection ────────────────────────────────────────────────────
    # Why this block exists: verify_environment() above answers only the yes/no question
    # "is the tool on PATH?" and it runs *after* the create-project form is submitted — so a
    # missing JDK or a wrong Java major only surfaces as a late, mid-install error. The methods
    # below power a *pre-flight* panel rendered in the form itself. For the selected stack +
    # version profile they report, per tool: the version we require, the version actually
    # installed, and whether the two agree. The user can then fix anything flagged before
    # clicking Generate. This is advisory only — nothing here blocks project creation.

    @classmethod
    def _probe(cls, check_cmd):
        """Run a version-probe command and return probe details.

        `installed` is True when the command exists and exits 0. We merge stdout+stderr because
        several tools (notably `java -version`) print their version banner to stderr, and we want
        the version string regardless of which stream it lands on. On failure we still return any
        partial output the process produced so the caller can attempt to parse a version from it.
        """
        commands = check_cmd if isinstance(check_cmd, (list, tuple)) else [check_cmd]
        last_output = ""
        saw_visible_binary = False
        last_path = None
        last_cmd = commands[-1] if commands else None
        for cmd in commands:
            env = cls._augmented_probe_env(cmd)
            resolved_path = cls._resolve_command_path(cmd, env)
            if resolved_path:
                last_path = resolved_path
                last_cmd = cmd
            try:
                res = subprocess.run(
                    cmd,
                    shell=True,
                    check=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    env=env,
                )
                return True, (res.stdout or b"").decode("utf-8", "ignore"), resolved_path, cmd
            except subprocess.CalledProcessError as e:
                out = e.output or b""
                saw_visible_binary = saw_visible_binary or cls._command_visible_in_env(cmd, env)
                last_output = out.decode("utf-8", "ignore") if isinstance(out, (bytes, bytearray)) else str(out)
            except FileNotFoundError:
                # Only reachable if a future caller passes shell=False; with shell=True a missing
                # binary surfaces as a non-zero exit (CalledProcessError) instead.
                saw_visible_binary = saw_visible_binary or cls._command_visible_in_env(cmd, env)
        return saw_visible_binary, last_output, last_path, last_cmd

    @staticmethod
    def _java_major(version):
        """Map a Java version string to its major number for compatibility comparison.

        JDK compatibility is keyed on the major version, but the version string format changed
        across releases: modern JDKs report "17.0.1" / "11.0.20" (major = first segment) while
        legacy JDKs report "1.8.0_xyz" (major = second segment, i.e. 8). Returns None when no
        leading number can be parsed.
        """
        if not version:
            return None
        parts = re.findall(r"\d+", version)
        if not parts:
            return None
        first = int(parts[0])
        # The "1.x" scheme (1.8 == Java 8) only applies to the pre-9 line.
        if first == 1 and len(parts) > 1:
            return int(parts[1])
        return first

    @staticmethod
    def infer_required_versions(filename, content, out):
        # content is lower-cased; keys (java/python/dotnet/node) match the spec profile_keys.
        if filename == 'pom.xml':
            m = re.search(r'<(?:java\.version|maven\.compiler\.(?:release|source|target))>\s*([\d._]+)', content)
            if m:
                out['java'] = m.group(1)
        elif filename.endswith('.csproj'):
            m = re.search(r'<targetframework>[^<]*?net(?:coreapp)?([\d]+\.[\d]+)', content)
            if m:
                out['dotnet'] = m.group(1)
        elif filename == 'pyproject.toml':
            m = re.search(r'requires-python\s*=\s*["\'][^"\']*?([\d]+\.[\d]+)', content)
            if m:
                out['python'] = m.group(1)
        elif filename == 'runtime.txt':
            m = re.search(r'python-([\d]+\.[\d]+)', content)
            if m:
                out['python'] = m.group(1)
        elif filename == 'package.json':
            m = re.search(r'"node"\s*:\s*"[^"]*?([\d]+(?:\.[\d]+)*)', content)
            if m:
                out['node'] = m.group(1)

    @staticmethod
    def _version_tuple(version):
        """Numeric segments of a version string as an int tuple, for ordered comparison.

        Strips any comparator/range prefix (">=3.12", "^18.0.0", "net8.0", "v20.11.1"). Returns
        () when nothing parses, so the caller falls back to a presence-only check.
        """
        if not version:
            return ()
        return tuple(int(p) for p in re.findall(r"\d+", version))

    @classmethod
    def _dependency_specs(cls, language):
        """Describe the system tools the developer must have on PATH for the given language.

        Package-manager-installed libraries (Playwright browsers, npm/pip/NuGet packages) are
        downloaded automatically during setup, so they are deliberately NOT listed here — only
        the prerequisites the user has to install themselves.

        Each spec:
          key         : stable id for the frontend
          name        : human label shown in the panel
          check_cmd   : command whose success means "installed" and whose output carries the version
          version_re  : regex with one capture group extracting the version from check_cmd output
          profile_key : if set, the required version is read from the resolved version profile
                        under this key (e.g. "java" -> JDK 11 vs 17); None -> no version requirement
          compare     : how to compare detected vs required ("java_major"); None -> presence only
          hint        : install / fix guidance, shown when the tool is missing or mismatched
        """
        if language == "Java":
            return [
                {"key": "java", "name": "Java (JDK)", "check_cmd": "java -version",
                 "version_re": r'version "([\d._]+)"', "profile_key": "java",
                 "compare": "java_major",
                 "hint": "Install a matching JDK from https://adoptium.net and ensure 'java' is on PATH."},
                {"key": "maven", "name": "Apache Maven", "check_cmd": "mvn -version",
                 "version_re": r"Apache Maven ([\d.]+)", "profile_key": None, "compare": None,
                 "hint": "Install Maven from https://maven.apache.org/install.html and add 'mvn' to PATH."},
            ]
        if language == "Python":
            py_cmd = cls._python_check_commands()
            pip_cmd = cls._pip_check_commands()
            return [
                {"key": "python", "name": "Python", "check_cmd": py_cmd,
                 "version_re": r"Python ([\d.]+)", "profile_key": "python", "compare": "min_version",
                 "hint": "Install Python from https://www.python.org/downloads/."},
                {"key": "pip", "name": "Pip", "check_cmd": pip_cmd,
                 "version_re": r"pip ([\d.]+)", "profile_key": None, "compare": None,
                 "hint": "Ships with Python; if missing run 'python -m ensurepip --upgrade'."},
            ]
        if language in ["JS / TS", "JavaScript", "TypeScript", "Typescript"]:
            return [
                {"key": "node", "name": "Node.js", "check_cmd": "node -v",
                 "version_re": r"v?([\d.]+)", "profile_key": "node", "compare": "min_version",
                 "hint": "Install Node.js LTS from https://nodejs.org (npm is bundled with it)."},
                {"key": "npm", "name": "npm", "check_cmd": "npm -v",
                 "version_re": r"([\d.]+)", "profile_key": None, "compare": None,
                 "hint": "Bundled with Node.js — reinstall Node.js if 'npm' is not found."},
            ]
        if language == "C#":
            return [
                {"key": "dotnet", "name": ".NET SDK", "check_cmd": "dotnet --version",
                 "version_re": r"([\d.]+)", "profile_key": "dotnet", "compare": "min_version",
                 "hint": "Install the .NET SDK from https://dotnet.microsoft.com/download."},
            ]
        return []

    @classmethod
    def required_dependencies(cls, tool, language, framework, profile_versions=None):
        """Build the pre-flight dependency report for the selected stack.

        Returns a list of dicts (one per required system tool), each carrying the required vs
        detected version and a status of:
          ok       — installed, and (where a version is required) the version matches
          missing  — not installed / not on PATH
          mismatch — installed, but a different version than the chosen profile requires

        `profile_versions` is the resolved version-profile dict from versions_catalog (e.g.
        {"java": "17", ...}); it supplies the required version for any spec with a profile_key.
        `tool`/`framework` are accepted for forward flexibility (e.g. tool-specific prerequisites)
        even though the current specs key only off language.
        """
        profile_versions = profile_versions or {}
        results = []
        for spec in cls._dependency_specs(language):
            installed, output, executable_path, command_used = cls._probe(spec["check_cmd"])

            detected = None
            if installed and spec.get("version_re"):
                match = re.search(spec["version_re"], output)
                if match:
                    detected = match.group(1)

            # A version is only "required" when the chosen profile actually pins this tool.
            required = None
            pkey = spec.get("profile_key")
            if pkey and profile_versions.get(pkey):
                required = profile_versions[pkey]

            compare = spec.get("compare")
            if not installed:
                status = "missing"
            elif required and compare == "java_major" and detected:
                # Required version is a minimum; a newer JDK is fine. Flag only if older.
                det_major = cls._java_major(detected)
                req_major = cls._java_major(required)
                if det_major is not None and req_major is not None and det_major < req_major:
                    status = "mismatch"
                else:
                    status = "ok"
            elif required and compare == "min_version" and detected:
                # Generic minimum-version gate; required is a floor, newer is fine.
                det_tuple = cls._version_tuple(detected)
                req_tuple = cls._version_tuple(required)
                if det_tuple and req_tuple and det_tuple < req_tuple:
                    status = "mismatch"
                else:
                    status = "ok"
            else:
                status = "ok"

            results.append({
                "key": spec["key"],
                "name": spec["name"],
                "required": required,
                "detected": detected,
                "installed": installed,
                "status": status,
                "hint": spec["hint"],
                "executable_path": executable_path,
                "command_used": command_used,
            })

        # Playwright on Linux also needs a small set of host browser libraries in addition to the
        # language/runtime toolchain. The most common missing package on our Ubuntu hosts is
        # libwoff1, which triggers Playwright's "Host system is missing dependencies" warning even
        # though the tests may still limp along. Surface it in the same pre-check panel so users
        # see the actionable fix before execution rather than only in the test logs.
        if tool == "Playwright" and platform.system().lower() == "linux":
            lib_check = subprocess.run(
                ["dpkg-query", "-W", "-f=${Status} ${Version}", "libwoff1"],
                capture_output=True,
                text=True,
                timeout=15,
            )
            installed = lib_check.returncode == 0 and "install ok installed" in (lib_check.stdout or "")
            detected = None
            if installed:
                parts = (lib_check.stdout or "").strip().split()
                if parts:
                    detected = parts[-1]
            results.append({
                "key": "playwright_linux_libwoff1",
                "name": "Playwright Linux host lib (libwoff1)",
                "required": None,
                "detected": detected,
                "installed": installed,
                "status": "ok" if installed else "missing",
                "hint": "Install with: sudo apt-get update && sudo apt-get install -y libwoff1",
                "executable_path": "/var/lib/dpkg/status" if installed else "",
                "command_used": "dpkg-query -W -f=${Status} ${Version} libwoff1",
            })
        return results

    @staticmethod
    def _download_env():
        """
        Build the environment dict used for the install subprocesses, adding the TLS/CA
        settings Playwright's browser downloader needs when the machine sits behind a
        corporate proxy that does TLS interception (Zscaler, Netskope, an internal root CA,
        etc.).

        Why this lives here and applies to every package manager: Playwright's Node, Python,
        Java *and* .NET distributions all fetch their browser binaries through a bundled
        Node.js driver. That means the NODE_* TLS variables below are the single lever that
        fixes the download for *all* of the stacks _build_install_phases emits — we don't
        need a per-language tweak.

        Configuration (all read from the Flask process's own environment so operators can set
        it once on the server / in a .env, instead of every developer exporting it by hand in
        PowerShell before each run):

          * Default — NODE_USE_SYSTEM_CA=1: tells Node to trust the operating system's
            certificate store, which is where a corporate root CA is normally installed.
            Certificate verification stays ON; we just teach Node about the company CA. This
            is the safe fix and why it's the default. (Requires Node >= 22.15 / 23.5.)

          * BIG_QA_CA_CERTS=<path> — also exports NODE_EXTRA_CA_CERTS so Node trusts an
            explicit CA bundle (.pem). Use this when the machine's Node is too old for
            NODE_USE_SYSTEM_CA, or the corporate CA isn't in the OS trust store.

          * BIG_QA_INSECURE_TLS=1 — escape hatch that disables TLS verification entirely
            (NODE_TLS_REJECT_UNAUTHORIZED=0). This is insecure (accepts ANY certificate, so
            it's vulnerable to MITM); it exists only as a last resort and logs a loud warning
            so it can't be enabled silently. Prefer the two options above.
        """
        env = os.environ.copy()

        # setdefault, not assignment: if the operator already exported NODE_USE_SYSTEM_CA on
        # the Flask process (e.g. to "0" to deliberately opt out), respect their choice.
        env.setdefault("NODE_USE_SYSTEM_CA", "1")

        ca_certs = os.environ.get("BIG_QA_CA_CERTS")
        if ca_certs:
            env["NODE_EXTRA_CA_CERTS"] = ca_certs

        # .NET CLI hardening for non-interactive use. We run `dotnet restore` / `dotnet test`
        # through a subprocess that captures stdout, and in that mode the default .NET behaviour
        # can make the command *appear to hang*:
        #   - MSBUILDDISABLENODEREUSE=1: MSBuild normally leaves persistent worker nodes running
        #     after a build for reuse. Those nodes inherit the captured stdout pipe and don't exit,
        #     so the parent's communicate() blocks forever even though `dotnet restore` itself has
        #     finished. Disabling node reuse makes the workers exit, closing the pipe.
        #   - DOTNET_CLI_TELEMETRY_OPTOUT / NOLOGO / SKIP_FIRST_TIME_EXPERIENCE: skip the one-time
        #     first-run experience and banners, which add latency and can stall on a captured stdin.
        # These are harmless no-ops for the npm/pip/maven phases. setdefault so an operator can
        # override any of them on the Flask process.
        env.setdefault("MSBUILDDISABLENODEREUSE", "1")
        env.setdefault("DOTNET_CLI_TELEMETRY_OPTOUT", "1")
        env.setdefault("DOTNET_NOLOGO", "1")
        env.setdefault("DOTNET_SKIP_FIRST_TIME_EXPERIENCE", "1")
        # Force .NET sockets to IPv4. On machines that advertise IPv6 (a router-supplied default
        # route) but have no working IPv6 path to the internet, .NET's HttpClient tries IPv6 first
        # and hangs until timeout — NuGet then retries the service index several times, so a
        # `dotnet restore` takes ~10 minutes and fails with NU1301 even though IPv4 works instantly
        # (curl/Node succeed because they do Happy-Eyeballs IPv4 fallback). Disabling IPv6 in .NET
        # makes restore use the working IPv4 path. Harmless where IPv6 actually works.
        env.setdefault("DOTNET_SYSTEM_NET_DISABLEIPV6", "1")

        # Same broken-IPv6 hazard for the other ecosystems (Maven Central / npm / PyPI all publish
        # AAAA records). The JVM is the worst offender: Maven prefers IPv6 and will hang on a dead
        # route, so force IPv4 for it. Node 18+ already does Happy-Eyeballs fallback, but
        # dns-result-order=ipv4first skips the initial IPv6 stall. (pip/urllib3 fall back on their
        # own; there is no clean per-process knob, so it is covered by the OS-level fix in the docs.)
        # Append rather than overwrite so an operator's existing MAVEN_OPTS / NODE_OPTIONS survive.
        maven_opts = env.get("MAVEN_OPTS", "")
        if "preferIPv4Stack" not in maven_opts:
            env["MAVEN_OPTS"] = (maven_opts + " -Djava.net.preferIPv4Stack=true").strip()
        node_opts = env.get("NODE_OPTIONS", "")
        if "dns-result-order" not in node_opts:
            env["NODE_OPTIONS"] = (node_opts + " --dns-result-order=ipv4first").strip()

        if os.environ.get("BIG_QA_INSECURE_TLS") == "1":
            env["NODE_TLS_REJECT_UNAUTHORIZED"] = "0"
            logging.warning(
                "BIG_QA_INSECURE_TLS=1 is set — disabling TLS certificate verification "
                "(NODE_TLS_REJECT_UNAUTHORIZED=0) for dependency installs. This accepts any "
                "certificate and is vulnerable to MITM. Prefer NODE_USE_SYSTEM_CA or "
                "BIG_QA_CA_CERTS=<path-to-corporate-ca.pem> instead."
            )

        return env

    # Substrings that show up in a failed install's output when the cause is the network/
    # proxy rather than a real dependency problem. Split into two buckets because the fix is
    # different for each, and we want to point the user at the *right* one instead of a
    # generic "check your connection".
    _TLS_ERROR_MARKERS = (
        "unable to get local issuer",
        "self-signed certificate",
        "self signed certificate",
        "SELF_SIGNED_CERT_IN_CHAIN",
        "unable to verify the first certificate",
        "UNABLE_TO_VERIFY_LEAF_SIGNATURE",
        "CERT_",
        "certificate has expired",
        "SSL certificate problem",
    )
    _BLOCKED_ERROR_MARKERS = (
        "ETIMEDOUT",
        "ECONNREFUSED",
        "ECONNRESET",
        "ENOTFOUND",
        "getaddrinfo",
        "Could not resolve host",
        "Failed to connect",
        "Connection timed out",
        "network timeout",
        "tunneling socket could not be established",
        "407 Proxy Authentication",
    )

    @staticmethod
    def _format_hint(lines):
        """
        Render message lines inside a bordered "Hint" box appended to an error message.

        Centralised so every hint looks identical and the box-drawing isn't duplicated at each
        call site. Returns a leading-blank-line-separated block so it reads as a distinct
        section below the raw command output.
        """
        border = "-" * 70
        body = "\n".join(lines)
        return f"\n\n{border}\nHint:\n{body}\n{border}"

    @staticmethod
    def _diagnose_network_failure(output):
        """
        Turn a raw install failure into a short, actionable hint when the output looks like a
        network/proxy problem — otherwise return "" (no hint, so real dependency errors aren't
        buried under proxy boilerplate).

        Why this exists: behind a corporate firewall the failure the user actually sees is a
        wall of Node/pip/Maven stack trace ending in something like "self-signed certificate
        in certificate chain" or "ETIMEDOUT". Those are network issues, not bugs in the
        generated project, but they read like a crash. This maps the two common shapes to the
        two fixes documented in SETUP-PROXY.md so the user knows which knob to turn.
        """
        text = output or ""

        # TLS interception: the connection went through but presented a corporate certificate
        # Node didn't trust. NODE_USE_SYSTEM_CA (already on by default) usually fixes it; if
        # not, the operator points us at the CA bundle.
        if any(m in text for m in EnvironmentSetup._TLS_ERROR_MARKERS):
            return EnvironmentSetup._format_hint([
                "This looks like a TLS/certificate problem from a corporate proxy, not a",
                "problem with your project. The app already sets NODE_USE_SYSTEM_CA=1; if it",
                "still fails, point it at your company CA bundle:",
                "    set BIG_QA_CA_CERTS=C:\\path\\to\\corporate-ca.pem   (then restart the app)",
                "See SETUP-PROXY.md (section 'TLS / certificate errors') for details.",
            ])

        # Host blocked / unreachable: no certificate was even exchanged. No CA setting can fix
        # this — the user needs a proxy, an internal mirror, or pre-cached browsers.
        if any(m in text for m in EnvironmentSetup._BLOCKED_ERROR_MARKERS):
            return EnvironmentSetup._format_hint([
                "This looks like the firewall is blocking the download host (no connection",
                "could be made). A certificate setting will NOT help here. Options:",
                "  * Route through your corporate proxy:  set HTTPS_PROXY=http://proxy:8080",
                "  * Use an internal mirror:              set PLAYWRIGHT_DOWNLOAD_HOST=...",
                "  * Use pre-downloaded browsers offline: set PLAYWRIGHT_BROWSERS_PATH=...",
                "Set these on the app's environment, then restart it.",
                "See SETUP-PROXY.md (section 'Firewall blocks the download') for details.",
            ])

        return ""

    @staticmethod
    def _run_command(cmd, cwd, timeout=1800, env=None):
        try:
            proc = subprocess.Popen(
                cmd,
                cwd=cwd,
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                universal_newlines=True,
                # env=None makes Popen inherit the parent environment unchanged (the original
                # behaviour); callers that need the Playwright TLS/CA vars pass _download_env().
                env=env,
            )
            stdout, _ = proc.communicate(timeout=timeout)
            if proc.returncode != 0:
                return False, f"Command failed (exit {proc.returncode}): {cmd}\nOutput:\n{stdout}"
            return True, stdout
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout, _ = proc.communicate()
            return False, f"Command timed out after {timeout}s: {cmd}\nOutput until timeout:\n{stdout}"
        except Exception as e:
            try:
                proc.kill()
            except Exception:
                pass
            return False, str(e)

    @staticmethod
    def install_project_dependencies(project_path, package_manager, tool="", status_cb=None):
        """
        Run the package-manager install + (for Playwright) browser-binary download for a
        scaffolded project.

        The work is split into discrete *phases* (e.g. "Resolving Maven dependencies" vs
        "Downloading Playwright browsers") so the UI can show what is actually happening
        during the multi-minute install — previously the user saw a single static "Installing
        dependencies..." message for the whole duration, which felt like a hang.

        Parameters
        ----------
        project_path : str
            Absolute path to the scaffolded project (cwd for the install commands).
        package_manager : str
            "Maven", "Pip", "NPM", or "NuGet" (substring match — matches the values the
            modal sends).
        tool : str
            "Playwright" or "Selenium". When Playwright, an extra browser-install phase is
            appended after the package manager phase.
        status_cb : callable, optional
            If provided, called with a human-readable string before each phase starts.
            Used by the Flask worker to update the polling endpoint's status message so the
            frontend's loading panel reflects the current phase.

        Returns
        -------
        (success: bool, message: str)
        """
        if not os.path.exists(project_path):
            return False, f"Project path {project_path} does not exist."

        phases = EnvironmentSetup._build_install_phases(package_manager, tool)
        if not phases:
            return False, f"Unknown package manager {package_manager}"

        # Build the install environment once (adds the Playwright TLS/CA settings needed
        # behind a corporate proxy) and reuse it for every phase. Computing it here rather
        # than inside _run_command keeps the warning-on-insecure-TLS log to one line per
        # install instead of one per phase.
        run_env = EnvironmentSetup._download_env()

        # Run each phase in order, surfacing its label before kicking off the subprocess.
        # We bail on the first failure so the UI gets a meaningful "this exact step failed"
        # error rather than a wall of multi-step shell output.
        for label, cmd in phases:
            if status_cb:
                try:
                    status_cb(label)
                except Exception:
                    # A buggy status callback must never abort the install; just log and continue.
                    logging.exception("status_cb raised while announcing phase %r", label)
            logging.info(f"Install phase '{label}' in {project_path}: {cmd}")
            ok, output = EnvironmentSetup._run_command(cmd, project_path, env=run_env)
            if not ok:
                # Append a proxy/firewall hint when the failure looks network-related, so the
                # user gets "here's the fix" instead of just a raw stack trace. Returns "" for
                # genuine dependency errors, leaving those untouched.
                hint = EnvironmentSetup._diagnose_network_failure(output)
                return False, f"{label}\n{output}{hint}"

        return True, "All dependencies installed."

    @staticmethod
    def _build_install_phases(package_manager, tool):
        """
        Return an ordered list of (status_message, shell_command) tuples for the requested
        package manager + tool combo.

        Each tuple is run as its own subprocess via _run_command. The split exists for UX
        reasons (per-phase status updates) — there is no functional difference vs the
        previous "&&"-chained one-shot command, except that on failure we now know which
        phase broke and can surface that to the user.

        Why the messages include duration hints: on a cold cache Maven and Playwright each
        pull hundreds of megabytes; users without that hint sometimes assume the install has
        hung. The first-run hint disappears on subsequent runs because the caches make it
        near-instant.
        """
        if "Maven" in package_manager:
            phases = [(
                "Resolving Maven dependencies (~1–2 min on first run)...",
                "mvn install -DskipTests",
            )]
            if tool == "Playwright":
                # exec:java is invoked directly (no plugin block in pom.xml) so the template
                # stays minimal. classpathScope=compile is required because the
                # com.microsoft.playwright.CLI class lives in the compile-scope playwright
                # artifact, not test-scope.
                phases.append((
                    "Downloading Playwright Chromium browser (~130 MB)...",
                    # Only Chromium is installed — every Playwright template defaults to it. This
                    # roughly thirds the download vs `install` (which pulls Chromium + Firefox +
                    # WebKit, ~1 GB). To use another browser, run the CLI `install firefox`/`webkit`.
                    'mvn exec:java -Dexec.mainClass="com.microsoft.playwright.CLI"'
                    ' -Dexec.args="install chromium" -Dexec.classpathScope=compile',
                ))
            return phases

        if "Pip" in package_manager:
            python_cmd = EnvironmentSetup._python_launcher_command()
            if EnvironmentSetup.is_windows():
                phases = [
                    ("Creating Python virtual environment...", f"{python_cmd} -m venv venv"),
                    ("Installing Python packages from requirements.txt...",
                     "venv\\Scripts\\pip install -r requirements.txt"),
                ]
                if tool == "Playwright":
                    phases.append((
                        "Downloading Playwright Chromium browser (~130 MB)...",
                        "venv\\Scripts\\python -m playwright install chromium",
                    ))
            else:
                phases = [
                    ("Creating Python virtual environment...", f"{python_cmd} -m venv venv"),
                    ("Upgrading pip / setuptools / wheel...",
                     "venv/bin/pip install --upgrade pip setuptools wheel"),
                    ("Installing Python packages from requirements.txt...",
                     "venv/bin/pip install -r requirements.txt"),
                ]
                if tool == "Playwright":
                    phases.append((
                        "Downloading Playwright Chromium browser (~130 MB)...",
                        "venv/bin/python -m playwright install chromium",
                    ))
            return phases

        if "NPM" in package_manager:
            phases = [("Installing npm packages from package.json...", "npm install")]
            if tool == "Playwright":
                phases.append((
                    "Downloading Playwright Chromium browser (~130 MB)...",
                    "npx playwright install chromium",
                ))
            return phases

        if "NuGet" in package_manager:
            phases = [("Restoring NuGet packages...", "dotnet restore")]
            if tool == "Playwright":
                phases.append((
                    "Downloading Playwright Chromium browser (~130 MB)...",
                    "pwsh bin/Debug/net6.0/playwright.ps1 install chromium",
                ))
            return phases

        return []
