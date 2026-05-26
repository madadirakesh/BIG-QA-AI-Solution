import os
import sys
import subprocess
import platform
from pathlib import Path

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
        studio = locator_studio.LocatorStudio()
        studio.show()
        sys.exit(app.exec())
    except Exception as e:
        print(f"Failed to launch: {e}")

if __name__ == "__main__":
    main()
