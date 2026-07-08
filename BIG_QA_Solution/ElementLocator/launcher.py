import os
import sys
import hashlib
import subprocess
import platform
from pathlib import Path

# Bypass GPU driver negotiation on Windows (safe — software rasterizer takes over as renderer)
if platform.system() == "Windows":
    os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = "--disable-gpu --disable-gpu-compositing"

def _requirements_signature(req_file: Path):
    return hashlib.sha256(req_file.read_bytes()).hexdigest()

def install_prerequisites(req_file: Path):
    stamp_file = req_file.parent / ".requirements_installed"
    req_signature = _requirements_signature(req_file)
    if stamp_file.exists():
        try:
            if stamp_file.read_text(encoding="utf-8").strip() == req_signature:
                return
        except OSError:
            pass

    env = os.environ.copy()
    env.setdefault("PIP_DISABLE_PIP_VERSION_CHECK", "1")

    def run_cmd(cmd):
        return subprocess.run(cmd, capture_output=True, text=True, env=env)

    pip_check = run_cmd([sys.executable, "-m", "pip", "--version"])
    if pip_check.returncode != 0:
        ensure_pip = run_cmd([sys.executable, "-m", "ensurepip", "--upgrade"])
        if ensure_pip.returncode != 0:
            ensure_error = (ensure_pip.stderr or ensure_pip.stdout or "").strip()
            raise RuntimeError(f"Failed to prepare pip: {ensure_error}")

    install_cmd = [sys.executable, "-m", "pip", "install", "-q", "-r", str(req_file)]
    result = run_cmd(install_cmd)
    error_text = (result.stderr or result.stdout or "").lower()

    if result.returncode != 0 and "externally-managed-environment" in error_text:
        fallback_cmd = install_cmd[:]
        fallback_cmd.insert(-2, "--break-system-packages")
        result = run_cmd(fallback_cmd)
        error_text = (result.stderr or result.stdout or "").lower()

    if result.returncode != 0 and sys.prefix == getattr(sys, "base_prefix", sys.prefix):
        permission_markers = (
            "permission denied",
            "access is denied",
            "not writable",
            "errno 13",
            "winerror 5",
        )
        if any(marker in error_text for marker in permission_markers):
            fallback_cmd = install_cmd[:]
            fallback_cmd.insert(-2, "--user")
            result = run_cmd(fallback_cmd)

    if result.returncode != 0:
        error_text = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(f"Failed to install dependencies: {error_text}")

    try:
        stamp_file.write_text(req_signature, encoding="utf-8")
    except OSError:
        pass

def main():
    print("Initializing Antigravity Locator Studio...")
    
    # Path to the new Hybrid Studio entry point
    base_dir = Path(__file__).resolve().parent
    studio_path = base_dir / "locator_studio.py"
    
    # Check dependencies (simplified check)
    try:
        import PyQt6
        import PyQt6.QtWebEngineWidgets
    except ImportError:
        print("Missing dependencies! Installing requirements...")
        install_prerequisites(base_dir / "requirements.txt")

    # OS Detection for window specific handling (optional)
    current_os = platform.system()
    print(f"System detected: {current_os}")

    # Launch the studio directly in this process to save memory and launch time
    print("Launching Studio UI...")
    try:
        import locator_studio
        # Create the QApplication instance
        app = locator_studio.QApplication(sys.argv)

        # Parse command line args if any
        project_path = sys.argv[1] if len(sys.argv) > 1 else None
        language = sys.argv[2] if len(sys.argv) > 2 else None
        framework = sys.argv[3] if len(sys.argv) > 3 else None
        tool = sys.argv[4] if len(sys.argv) > 4 else None
        app_url = sys.argv[5] if len(sys.argv) > 5 else None
        # Session auth: server URL + token used to heartbeat back and self-close on logout.
        auth_server = sys.argv[6] if len(sys.argv) > 6 else None
        auth_token = sys.argv[7] if len(sys.argv) > 7 else None

        print(f"[Launcher] Args received - path: {project_path}, lang: {language}, fw: {framework}, tool: {tool}, url: {app_url}", flush=True)

        studio = locator_studio.LocatorStudio(
            project_path=project_path,
            language=language,
            framework=framework,
            tool=tool,
            app_url=app_url,
            auth_server=auth_server,
            auth_token=auth_token
        )
        # Always launch maximized so all panels and buttons are fully visible
        studio.showMaximized()
        sys.exit(app.exec())
    except Exception as e:
        print(f"Failed to launch: {e}")

if __name__ == "__main__":
    main()
