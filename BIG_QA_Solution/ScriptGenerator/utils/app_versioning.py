import os
import re
import subprocess
from pathlib import Path
from typing import Optional, Tuple
from dotenv import load_dotenv


UNAVAILABLE_VERSION = "Unavailable"
SEMVER_FULL_RE = re.compile(
    r"^v?(0|[1-9]\d*)(?:\.(0|[1-9]\d*))?(?:\.(0|[1-9]\d*))?(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$",
    re.IGNORECASE,
)


def refresh_version_env(app_root_dir: Path) -> None:
    env_path = Path(app_root_dir) / ".env"
    load_dotenv(dotenv_path=env_path, override=True)


def get_configured_app_version() -> str:
    return (os.environ.get("APP_VERSION", "") or "").strip()


def get_release_download_url() -> str:
    return (
        (os.environ.get("APP_RELEASE_URL", "") or "").strip()
        or (os.environ.get("RELEASE_DOWNLOAD_URL", "") or "").strip()
    )


def get_app_version_label(app_root_dir: Path, allow_git_fallback: bool = True) -> str:
    refresh_version_env(app_root_dir)
    configured_version = get_configured_app_version()
    if configured_version:
        return configured_version

    if allow_git_fallback:
        try:
            commit = subprocess.check_output(
                ["git", "-C", str(app_root_dir), "rev-parse", "--short", "HEAD"],
                text=True,
                stderr=subprocess.DEVNULL,
                timeout=2,
            ).strip()
            if commit:
                return f"build-{commit}"
        except Exception:
            pass

    return UNAVAILABLE_VERSION


def parse_version_tuple(value: str) -> Optional[Tuple[int, int, int]]:
    normalized = (value or "").strip()
    if not normalized:
        return None

    match = SEMVER_FULL_RE.fullmatch(normalized)
    if not match:
        return None

    major = int(match.group(1))
    minor = int(match.group(2) or 0)
    patch = int(match.group(3) or 0)
    return (major, minor, patch)


def build_release_status(
    current_version: str,
    latest_version: str,
    release_url: str = "",
    mandatory_update: bool = False,
) -> dict:
    current = (current_version or "").strip() or UNAVAILABLE_VERSION
    latest = (latest_version or "").strip() or UNAVAILABLE_VERSION
    parsed_current = parse_version_tuple(current)
    parsed_latest = parse_version_tuple(latest)

    payload = {
        "current_version": current,
        "latest_version": latest,
        "release_url": release_url or "",
        "requires_download": False,
        "update_available": False,
        "severity": "warning",
        "status": "unavailable",
        "badge_value": "Unavailable",
        "message": "Version information is unavailable.",
        "title": "Version information is unavailable.",
        "mandatory_update": bool(mandatory_update),
    }

    if current == UNAVAILABLE_VERSION:
        payload.update({
            "status": "current_unavailable",
            "badge_value": "Set APP_VERSION",
            "message": "This app instance does not have APP_VERSION configured yet.",
            "title": "Set APP_VERSION in .env so this app can compare itself with the backend release.",
        })
        return payload

    if latest == UNAVAILABLE_VERSION:
        payload.update({
            "status": "latest_unavailable",
            "badge_value": current,
            "message": f"Current version is {current}, but the backend release version is unavailable.",
            "title": f"Current version: {current}. Backend release version is unavailable.",
        })
        return payload

    if parsed_current is None:
        payload.update({
            "severity": "error",
            "status": "invalid_current_version",
            "badge_value": current,
            "message": f"The local APP_VERSION '{current}' is not a valid full version string.",
            "title": f"Invalid local APP_VERSION: {current}. Use a full version like 1.0.1.",
        })
        return payload

    if parsed_latest is None:
        payload.update({
            "severity": "error",
            "status": "invalid_latest_version",
            "badge_value": current,
            "message": f"The release API returned an invalid version string: '{latest}'.",
            "title": f"Invalid release version from server: {latest}. Expected a full version like 1.0.1.",
        })
        return payload

    if parsed_latest == parsed_current:
        payload.update({
            "severity": "healthy",
            "status": "up_to_date",
            "badge_value": current,
            "message": f"Current version {current} matches the backend release.",
            "title": f"Current version: {current}. Backend release version: {latest}.",
        })
        return payload

    if parsed_latest > parsed_current:
        major_changed = parsed_latest[0] > parsed_current[0]
        forced_download = bool(mandatory_update) or major_changed
        payload.update({
            "severity": "error" if forced_download else "warning",
            "status": "update_required" if forced_download else "update_available",
            "badge_value": current if forced_download else f"{latest} Available",
            "message": (
                f"Mandatory update required. Current version {current} is behind backend release {latest}."
                if forced_download
                else f"Update available. Current version {current}; backend release {latest}."
            ),
            "title": (
                f"Mandatory update required: current {current}, latest {latest}. "
                "Download the latest version before continuing."
                if forced_download
                else f"Update available: current {current}, latest {latest}."
            ),
            "requires_download": forced_download,
            "update_available": True,
            "mandatory_update": bool(mandatory_update),
        })
        return payload

    payload.update({
        "severity": "healthy",
        "status": "ahead_of_backend",
        "badge_value": current,
        "message": f"Current version {current} is newer than backend release {latest}.",
        "title": f"Current version: {current}. Backend release version: {latest}.",
    })
    return payload
