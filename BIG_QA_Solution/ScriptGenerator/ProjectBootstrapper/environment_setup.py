import subprocess
import platform
import os
import logging

class EnvironmentSetup:
    @staticmethod
    def is_windows():
        return platform.system().lower() == "windows"

    @staticmethod
    def is_mac():
        return platform.system().lower() == "darwin"

    @staticmethod
    def check_system_dependency(dep_name, check_cmd):
        try:
            subprocess.run(check_cmd, shell=True, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            return True
        except (subprocess.CalledProcessError, FileNotFoundError):
            return False
            
    @classmethod
    def verify_environment(cls, language):
        missing = []
        if language == "Java":
            if not cls.check_system_dependency("Java", "java -version"): missing.append("Java")
            if not cls.check_system_dependency("Maven", "mvn -version"): missing.append("Maven")
        elif language == "Python":
            py_cmd = "python --version" if cls.is_windows() else "python3.12 --version"
            pip_cmd = "pip --version" if cls.is_windows() else "pip3.12 --version"
            if not cls.check_system_dependency("Python", py_cmd): missing.append("Python 3.12")
            if not cls.check_system_dependency("Pip", pip_cmd): missing.append("Pip 3.12")
        elif language in ["JS / TS", "JavaScript", "TypeScript"]:
            if not cls.check_system_dependency("Node", "node -v"): missing.append("Node.js")
            if not cls.check_system_dependency("NPM", "npm -v"): missing.append("NPM")
        elif language == "C#":
            if not cls.check_system_dependency("Dotnet", "dotnet --version"): missing.append("Dotnet CLI")
            
        if missing:
            return False, missing
        return True, []

    @staticmethod
    def _run_command(cmd, cwd, timeout=1800):
        try:
            proc = subprocess.Popen(
                cmd,
                cwd=cwd,
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                universal_newlines=True
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
            ok, output = EnvironmentSetup._run_command(cmd, project_path)
            if not ok:
                return False, f"{label}\n{output}"

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
                    "Downloading Playwright browser binaries (~250 MB, ~2–3 min)...",
                    'mvn exec:java -Dexec.mainClass="com.microsoft.playwright.CLI"'
                    ' -Dexec.args="install" -Dexec.classpathScope=compile',
                ))
            return phases

        if "Pip" in package_manager:
            if EnvironmentSetup.is_windows():
                phases = [
                    ("Creating Python virtual environment...", "python -m venv venv"),
                    ("Installing Python packages from requirements.txt...",
                     "venv\\Scripts\\pip install -r requirements.txt"),
                ]
                if tool == "Playwright":
                    phases.append((
                        "Downloading Playwright browser binaries (~250 MB)...",
                        "venv\\Scripts\\python -m playwright install",
                    ))
            else:
                phases = [
                    ("Creating Python virtual environment...", "python3.12 -m venv venv"),
                    ("Upgrading pip / setuptools / wheel...",
                     "venv/bin/pip install --upgrade pip setuptools wheel"),
                    ("Installing Python packages from requirements.txt...",
                     "venv/bin/pip install -r requirements.txt"),
                ]
                if tool == "Playwright":
                    phases.append((
                        "Downloading Playwright browser binaries (~250 MB)...",
                        "venv/bin/python3.12 -m playwright install",
                    ))
            return phases

        if "NPM" in package_manager:
            phases = [("Installing npm packages from package.json...", "npm install")]
            if tool == "Playwright":
                phases.append((
                    "Downloading Playwright browser binaries (~250 MB)...",
                    "npx playwright install",
                ))
            return phases

        if "NuGet" in package_manager:
            phases = [("Restoring NuGet packages...", "dotnet restore")]
            if tool == "Playwright":
                phases.append((
                    "Downloading Playwright browser binaries (~250 MB)...",
                    "pwsh bin/Debug/net6.0/playwright.ps1 install",
                ))
            return phases

        return []
