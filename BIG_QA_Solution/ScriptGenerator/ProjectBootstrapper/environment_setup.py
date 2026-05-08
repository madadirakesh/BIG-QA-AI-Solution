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
    def install_project_dependencies(project_path, package_manager, tool=""):
        if "Maven" in package_manager:
            cmd = "mvn install -DskipTests"
        elif "Pip" in package_manager:
            if EnvironmentSetup.is_windows():
                cmd = "python -m venv venv && venv\\Scripts\\pip install -r requirements.txt"
                if tool == "Playwright":
                    cmd += " && venv\\Scripts\\python -m playwright install"
            else:
                cmd = "python3.12 -m venv venv && venv/bin/pip install --upgrade pip setuptools wheel && venv/bin/pip install -r requirements.txt"
                if tool == "Playwright":
                    cmd += " && venv/bin/python3.12 -m playwright install"
        elif "NPM" in package_manager:
            cmd = "npm install"
            if tool == "Playwright":
                cmd += " && npx playwright install"
        elif "NuGet" in package_manager:
            cmd = "dotnet restore"
            if tool == "Playwright":
                cmd += " && pwsh bin/Debug/net6.0/playwright.ps1 install"
        else:
            return False, f"Unknown package manager {package_manager}"

        if not os.path.exists(project_path):
            return False, f"Project path {project_path} does not exist."

        logging.info(f"Running dependency installation: {cmd} in {project_path}")
        return EnvironmentSetup._run_command(cmd, project_path)
