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
            py_cmd = "python --version" if cls.is_windows() else "python3 --version"
            pip_cmd = "pip --version" if cls.is_windows() else "pip3 --version"
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
                cmd = "python3 -m venv venv && venv/bin/pip install -r requirements.txt"
                if tool == "Playwright":
                    cmd += " && venv/bin/python3 -m playwright install"
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

        try:
            logging.info(f"Running dependency installation: {cmd} in {project_path}")
            # Ensure folder exists
            if not os.path.exists(project_path):
                return False, f"Project path {project_path} does not exist."
                
            result = subprocess.run(cmd, cwd=project_path, shell=True, check=True, capture_output=True, text=True)
            return True, result.stdout
        except subprocess.CalledProcessError as e:
            return False, e.stderr
