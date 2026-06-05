import os
import sys
import subprocess
import platform
from pathlib import Path

# Bypass GPU driver negotiation on Windows (safe — software rasterizer takes over as renderer)
if platform.system() == "Windows":
    os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = "--disable-gpu --disable-gpu-compositing"

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
        subprocess.check_call([sys.executable, "-m", "pip", "install", "-r", str(base_dir / "requirements.txt")])

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

        print(f"[Launcher] Args received - path: {project_path}, lang: {language}, fw: {framework}, tool: {tool}, url: {app_url}", flush=True)

        studio = locator_studio.LocatorStudio(
            project_path=project_path,
            language=language,
            framework=framework,
            tool=tool,
            app_url=app_url
        )
        # Always launch maximized so all panels and buttons are fully visible
        studio.showMaximized()
        sys.exit(app.exec())
    except Exception as e:
        print(f"Failed to launch: {e}")

if __name__ == "__main__":
    main()
