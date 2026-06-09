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
            if EnvironmentSetup.is_windows():
                phases = [
                    ("Creating Python virtual environment...", "python -m venv venv"),
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
                    ("Creating Python virtual environment...", "python3.12 -m venv venv"),
                    ("Upgrading pip / setuptools / wheel...",
                     "venv/bin/pip install --upgrade pip setuptools wheel"),
                    ("Installing Python packages from requirements.txt...",
                     "venv/bin/pip install -r requirements.txt"),
                ]
                if tool == "Playwright":
                    phases.append((
                        "Downloading Playwright Chromium browser (~130 MB)...",
                        "venv/bin/python3.12 -m playwright install chromium",
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
