import hashlib
import json
import logging
import os
import platform
import re
import socket
import sys
import urllib.request
import uuid
from datetime import datetime
from pathlib import Path

import requests
from dotenv import load_dotenv

from db.app_db import fetch_data, insert_data, update_data
from utils.app_versioning import get_app_version_label
from utils.crypto_util import decrypt_for_app, encrypt_for_app


LICENSE_PRODUCT_NAME = "BIG AI QA Solution"
LICENSE_SERVICE_UNAVAILABLE_MESSAGE = (
    "We couldn't verify your license because the license service is unavailable. "
    "Please check that the license server is running, then try again."
)
LICENSE_CACHE_SECONDS = 300
APP_LICENSE_ROW_ID = 1
APP_ROOT_DIR = Path(__file__).resolve().parent.parent
ENV_PATH = APP_ROOT_DIR / ".env"
SEMVER_FULL_RE = re.compile(
    r"^v?(0|[1-9]\d*)(?:\.(0|[1-9]\d*))?(?:\.(0|[1-9]\d*))?(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$",
    re.IGNORECASE,
)
logger = logging.getLogger(__name__)


def utcnow_iso():
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def get_license_api_base_url():
    load_dotenv(dotenv_path=ENV_PATH, override=True)
    return (os.environ.get("LICENSE_API_BASE_URL") or "").strip()


def get_license_api_token():
    load_dotenv(dotenv_path=ENV_PATH, override=True)
    return (os.environ.get("LICENSE_API_TOKEN") or "").strip()


def normalize_release_platform_name(os_name):
    normalized = (os_name or "").strip().lower()
    if not normalized:
        return ""
    if normalized.startswith("win"):
        return "windows"
    if normalized in {"darwin", "mac", "macos", "osx"}:
        return "mac"
    if normalized.startswith("lin"):
        return "linux"
    return normalized


def machine_fingerprint():
    raw = "|".join([
        socket.gethostname(),
        str(uuid.getnode()),
        sys.platform,
        str(APP_ROOT_DIR),
    ])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def resolve_license_verify_urls():
    candidates = []
    license_api_base_url = get_license_api_base_url()
    if license_api_base_url:
        base = license_api_base_url.rstrip("/")
        candidates.extend([
            f"{base}/api/v1/license-validations",
            f"{base}/api/licenses/validate",
        ])

    deduped = []
    seen = set()
    for url in candidates:
        if url and url not in seen:
            deduped.append(url)
            seen.add(url)
    return deduped


def resolve_release_urls(os_name=""):
    candidates = []
    license_api_base_url = get_license_api_base_url()
    if license_api_base_url:
        base = license_api_base_url.rstrip("/")
        normalized_os = normalize_release_platform_name(os_name)
        query_suffix = f"?os_name={normalized_os}" if normalized_os else ""
        candidates.extend([
            f"{base}/api/v1/releases/latest{query_suffix}",
            f"{base}/api/releases/latest{query_suffix}",
        ])

    deduped = []
    seen = set()
    for url in candidates:
        if url and url not in seen:
            deduped.append(url)
            seen.add(url)
    return deduped


def mask_license_key(license_key):
    key = (license_key or "").strip()
    if len(key) <= 8:
        return key
    return f"{key[:4]}{'*' * max(len(key) - 8, 4)}{key[-4:]}"


def parse_iso_datetime(value):
    if not value:
        return None
    normalized = str(value).strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        return None


def extract_license_payload(payload):
    if not isinstance(payload, dict):
        return {}
    for key in ("data", "result", "license", "payload"):
        nested = payload.get(key)
        if isinstance(nested, dict):
            return nested
    return payload


def extract_service_payload(payload):
    if not isinstance(payload, dict):
        return {}
    data = payload.get("data")
    if isinstance(data, dict):
        return data
    return extract_license_payload(payload)


def parse_release_version_tuple(value):
    normalized = (value or "").strip()
    if not normalized:
        return ()
    match = SEMVER_FULL_RE.fullmatch(normalized)
    if not match:
        return ()
    return (
        int(match.group(1)),
        int(match.group(2) or 0),
        int(match.group(3) or 0),
    )


def parse_release_bool(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "yes", "1", "active", "latest", "mandatory", "required"}:
            return True
        if normalized in {"false", "no", "0", "inactive", "optional"}:
            return False
    return False


def normalize_release_entry(candidate):
    if not isinstance(candidate, dict):
        return None

    version = ""
    for key in ("version", "latest_version", "tag_name", "release_version", "name"):
        value = candidate.get(key)
        if isinstance(value, str) and parse_release_version_tuple(value):
            version = value.strip()
            break

    if not version:
        return None

    platforms = []
    raw_platforms = (
        candidate.get("supported_platforms")
        or candidate.get("platforms")
        or candidate.get("platform")
        or candidate.get("os_name")
    )
    if isinstance(raw_platforms, list):
        platforms = [normalize_release_platform_name(item) for item in raw_platforms if str(item).strip()]
    elif isinstance(raw_platforms, str) and raw_platforms.strip():
        parts = [item.strip() for item in raw_platforms.split(",") if item.strip()]
        platforms = [normalize_release_platform_name(item) for item in parts]

    download_url = ""
    for key in ("download_url", "downloadUrl", "url", "asset_url", "assetUrl"):
        value = candidate.get(key)
        if isinstance(value, str) and value.strip():
            download_url = value.strip()
            break

    update_mode = str(candidate.get("update_mode") or candidate.get("updateMode") or "").strip().lower()
    mandatory_update = any([
        parse_release_bool(candidate.get("mandatory_update")),
        parse_release_bool(candidate.get("is_mandatory")),
        parse_release_bool(candidate.get("mandatory")),
        parse_release_bool(candidate.get("force_update")),
        update_mode in {"mandatory", "required", "force"},
    ])

    return {
        "available": parse_release_bool(candidate.get("available")) if "available" in candidate else True,
        "version": version,
        "download_url": download_url,
        "mandatory_update": mandatory_update,
        "title": (candidate.get("title") or candidate.get("name") or "").strip() if isinstance(candidate.get("title") or candidate.get("name"), str) else "",
        "changelog": (candidate.get("changelog") or candidate.get("notes") or "").strip() if isinstance(candidate.get("changelog") or candidate.get("notes"), str) else "",
        "release_date": (candidate.get("release_date") or candidate.get("releaseDate") or candidate.get("published_at") or "").strip() if isinstance(candidate.get("release_date") or candidate.get("releaseDate") or candidate.get("published_at"), str) else "",
        "supported_platforms": [item for item in platforms if item],
        "latest": any([
            parse_release_bool(candidate.get("latest")),
            parse_release_bool(candidate.get("is_latest")),
        ]),
        "active": not str(candidate.get("status") or "").strip() or str(candidate.get("status")).strip().lower() == "active",
        "raw": candidate,
    }


def extract_release_candidates(payload):
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []

    data = payload.get("data")
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("releases", "items", "results"):
            nested = data.get(key)
            if isinstance(nested, list):
                return nested
    for key in ("releases", "items", "results"):
        nested = payload.get(key)
        if isinstance(nested, list):
            return nested
    return []


def select_latest_release(payload, os_name=""):
    normalized_os = normalize_release_platform_name(os_name)
    candidates = []

    direct_candidate = normalize_release_entry(extract_service_payload(payload))
    if direct_candidate:
        candidates.append(direct_candidate)

    for item in extract_release_candidates(payload):
        normalized = normalize_release_entry(item)
        if normalized:
            candidates.append(normalized)

    if not candidates:
        return None

    active_candidates = [item for item in candidates if item.get("active", True)] or candidates
    if normalized_os:
        platform_candidates = [
            item for item in active_candidates
            if not item.get("supported_platforms") or normalized_os in item.get("supported_platforms", [])
        ]
    else:
        platform_candidates = active_candidates

    if not platform_candidates:
        return None

    latest_candidates = [item for item in platform_candidates if item.get("latest")]
    ranked = latest_candidates or platform_candidates
    ranked.sort(
        key=lambda item: (
            parse_release_bool(item.get("mandatory_update")),
            parse_release_version_tuple(item.get("version", "")),
            item.get("release_date", ""),
        ),
        reverse=True,
    )
    return ranked[0]


def extract_license_validity(payload):
    candidates = [extract_service_payload(payload), extract_license_payload(payload), payload]
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        for key in ("valid", "is_valid", "active", "licensed", "success"):
            value = candidate.get(key)
            if isinstance(value, bool):
                return value
            if isinstance(value, str):
                normalized = value.strip().lower()
                if normalized in ("true", "valid", "active", "success", "ok", "licensed"):
                    return True
                if normalized in ("false", "invalid", "inactive", "expired", "error"):
                    return False
        status_value = candidate.get("status")
        if isinstance(status_value, str):
            normalized = status_value.strip().lower()
            if normalized in ("valid", "active", "success", "ok", "licensed"):
                return True
            if normalized in ("invalid", "inactive", "expired", "blocked", "revoked", "error"):
                return False
    return None


def extract_license_message(payload, fallback=None):
    candidates = [extract_service_payload(payload), extract_license_payload(payload), payload]
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        for key in ("message", "detail", "error", "reason", "description"):
            value = candidate.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return fallback or "Unable to validate the license right now."


def extract_licensed_to(payload):
    candidates = [extract_service_payload(payload), extract_license_payload(payload), payload]
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        for key in ("licensed_to", "customer_name", "customer", "company", "account_name", "name"):
            value = candidate.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        license_block = candidate.get("license")
        if isinstance(license_block, dict):
            for key in ("firm_name", "representative_name", "licensed_to", "company", "name"):
                value = license_block.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
    return ""


def extract_license_period(payload):
    candidates = [extract_service_payload(payload), extract_license_payload(payload), payload]
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        license_block = candidate.get("license")
        if isinstance(license_block, dict):
            start_date = (license_block.get("start_date") or "").strip() if isinstance(license_block.get("start_date"), str) else ""
            end_date = (license_block.get("end_date") or "").strip() if isinstance(license_block.get("end_date"), str) else ""
            if start_date or end_date:
                return {
                    "start_date": start_date,
                    "end_date": end_date,
                }
    return {
        "start_date": "",
        "end_date": "",
    }


def fetch_latest_release_info(os_name=""):
    headers = {}
    license_api_token = get_license_api_token()
    if license_api_token:
        headers["X-Dibcase-Api-Token"] = license_api_token

    response = None
    body = {}
    last_error = None
    release_urls = resolve_release_urls(os_name)
    if not release_urls:
        raise requests.RequestException(
            "License API is not configured. Set LICENSE_API_BASE_URL in the environment."
        )

    attempted_urls = []
    for release_url in release_urls:
        attempted_urls.append(release_url)
        try:
            response = requests.get(
                release_url,
                headers=headers,
                timeout=10,
                proxies={"http": "", "https": ""},
            )
            try:
                body = response.json()
            except ValueError:
                body = {"message": (response.text or "").strip()}

            if response.status_code != 404:
                break
        except requests.RequestException as exc:
            last_error = exc
            try:
                request = urllib.request.Request(release_url, headers=headers, method="GET")
                with urllib.request.urlopen(request, timeout=10) as urllib_response:
                    status_code = getattr(urllib_response, "status", 200)
                    raw_text = urllib_response.read().decode("utf-8", errors="replace")
                    try:
                        body = json.loads(raw_text)
                    except ValueError:
                        body = {"message": raw_text.strip()}

                    class _UrlLibResponse:
                        def __init__(self, url, status_code, text, payload):
                            self.url = url
                            self.status_code = status_code
                            self.text = text
                            self._payload = payload

                        def json(self):
                            return self._payload

                    response = _UrlLibResponse(release_url, status_code, raw_text, body)
                    if status_code != 404:
                        break
            except Exception:
                continue
    else:
        if last_error:
            raise last_error
        raise requests.RequestException(
            f"Unable to reach a release endpoint. Tried: {', '.join(attempted_urls)}"
        )

    if response is None:
        raise requests.RequestException("Release endpoint did not return a response.")

    if response.status_code == 404:
        return {
            "available": False,
            "version": "",
            "download_url": "",
            "mandatory_update": False,
            "title": "",
            "changelog": "",
            "release_date": "",
            "supported_platforms": [],
            "message": extract_license_message(body, "No release available for this platform."),
            "raw": body,
            "status_code": response.status_code,
            "endpoint": response.url,
            "not_found": True,
        }

    if response.status_code >= 400:
        return None

    selected_release = select_latest_release(body, os_name)
    if selected_release is None:
        payload = extract_service_payload(body)
        if not isinstance(payload, dict):
            payload = {}
        selected_release = {
            "available": bool(payload.get("available", True)),
            "version": (payload.get("version") or "").strip() if isinstance(payload.get("version"), str) else "",
            "download_url": (payload.get("download_url") or "").strip() if isinstance(payload.get("download_url"), str) else "",
            "mandatory_update": bool(payload.get("mandatory_update")),
            "title": (payload.get("title") or "").strip() if isinstance(payload.get("title"), str) else "",
            "changelog": (payload.get("changelog") or "").strip() if isinstance(payload.get("changelog"), str) else "",
            "release_date": (payload.get("release_date") or "").strip() if isinstance(payload.get("release_date"), str) else "",
            "supported_platforms": payload.get("supported_platforms") if isinstance(payload.get("supported_platforms"), list) else [],
            "raw": payload,
        }

    return {
        "available": bool(selected_release.get("available", True)),
        "version": selected_release.get("version", ""),
        "download_url": selected_release.get("download_url", ""),
        "mandatory_update": bool(selected_release.get("mandatory_update")),
        "title": selected_release.get("title", ""),
        "changelog": selected_release.get("changelog", ""),
        "release_date": selected_release.get("release_date", ""),
        "supported_platforms": selected_release.get("supported_platforms", []),
        "message": extract_license_message(body, "Release information loaded."),
        "raw": body,
        "status_code": response.status_code,
        "endpoint": response.url,
        "not_found": False,
    }


def extract_license_reason(payload, fallback=None):
    candidates = [extract_service_payload(payload), extract_license_payload(payload), payload]
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        for key in ("reason", "message", "detail", "error", "description"):
            value = candidate.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return fallback or "License validation failed."


def is_transient_license_response(status_code):
    """Return True when the verifier response looks temporarily unavailable."""
    return status_code in (408, 425, 429) or status_code >= 500


def get_license_record():
    rows = fetch_data("SELECT * FROM AppLicense WHERE id = ?", (APP_LICENSE_ROW_ID,))
    if not rows:
        return None
    record = rows[0]
    record["license_key"] = decrypt_for_app(record.get("license_key"))
    record["masked_license_key"] = mask_license_key(record.get("license_key"))
    return record


def save_license_record(license_key, status, message, licensed_to="", checked_at=None):
    checked_at = checked_at or utcnow_iso()
    encrypted_key = encrypt_for_app((license_key or "").strip())
    existing = fetch_data("SELECT id FROM AppLicense WHERE id = ?", (APP_LICENSE_ROW_ID,))
    params = (
        encrypted_key,
        status,
        message,
        licensed_to or "",
        checked_at,
        checked_at,
        APP_LICENSE_ROW_ID,
    )
    if existing:
        update_data(
            """
            UPDATE AppLicense
            SET license_key = ?, status = ?, message = ?, licensed_to = ?, last_checked_at = ?, updated_at = ?
            WHERE id = ?
            """,
            params,
        )
    else:
        insert_data(
            """
            INSERT INTO AppLicense (license_key, status, message, licensed_to, last_checked_at, updated_at, id)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            params,
        )


def call_license_verifier(license_key):
    payload = {
        "license_key": license_key,
        "license": license_key,
        "key": license_key,
        "device_identifier": machine_fingerprint(),
        "machine_name": socket.gethostname(),
        "os_name": platform.system() or sys.platform,
        "os_version": platform.version(),
        "app_version": get_app_version_label(APP_ROOT_DIR, allow_git_fallback=False) or None,
        "metadata": {
            "product_name": LICENSE_PRODUCT_NAME,
            "application_root": str(APP_ROOT_DIR),
            "python_version": platform.python_version(),
        },
        "product_name": LICENSE_PRODUCT_NAME,
        "product": LICENSE_PRODUCT_NAME,
        "application": LICENSE_PRODUCT_NAME,
        "instance_id": machine_fingerprint(),
        "hostname": socket.gethostname(),
    }
    headers = {
        "Content-Type": "application/json",
    }
    license_api_token = get_license_api_token()
    if license_api_token:
        headers["X-Dibcase-Api-Token"] = license_api_token

    last_error = None
    response = None
    body = {}
    verify_urls = resolve_license_verify_urls()
    if not verify_urls:
        raise requests.RequestException(
            "License API is not configured. Set LICENSE_API_BASE_URL in the environment."
        )

    attempted_urls = []
    for verify_url in verify_urls:
        attempted_urls.append(verify_url)
        try:
            response = requests.post(
                verify_url,
                json=payload,
                headers=headers,
                timeout=10,
            )
            try:
                body = response.json()
            except ValueError:
                body = {"message": (response.text or "").strip()}

            # Stop on any non-404 response so business-level invalid licenses still surface cleanly.
            if response.status_code != 404:
                break
        except requests.RequestException as exc:
            last_error = exc
            continue
    else:
        if last_error:
            raise last_error
        raise requests.RequestException(
            f"Unable to reach a license verification endpoint. Tried: {', '.join(attempted_urls)}"
        )

    if response is None:
        raise requests.RequestException("License verification did not return a response.")

    valid = extract_license_validity(body)
    reason = extract_license_reason(body, fallback=f"License verifier returned HTTP {response.status_code}.")
    message = extract_license_message(body, fallback=reason)
    licensed_to = extract_licensed_to(body)
    license_period = extract_license_period(body)

    if valid is None:
        valid = response.ok
        if response.ok and not message:
            message = "License validated successfully."
        elif not response.ok and not message:
            message = "License validation failed."

    if response.status_code >= 400 and valid:
        valid = False

    return {
        "valid": bool(valid),
        "message": reason if not valid and reason else message,
        "detail_message": message,
        "reason": reason,
        "licensed_to": licensed_to,
        "license_period": license_period,
        "raw": body,
        "status_code": response.status_code,
        "endpoint": response.url,
    }


def assess_license_state(force_refresh=False):
    record = get_license_record()
    if not record or not (record.get("license_key") or "").strip():
        return {
            "valid": False,
            "status": "missing",
            "message": "Enter a valid license key to unlock the application.",
            "licensed_to": "",
            "record": record,
        }

    if not force_refresh:
        checked_at = parse_iso_datetime(record.get("last_checked_at"))
        if checked_at and LICENSE_CACHE_SECONDS > 0:
            age_seconds = (datetime.utcnow() - checked_at.replace(tzinfo=None)).total_seconds()
            if age_seconds < LICENSE_CACHE_SECONDS and record.get("status") in ("valid", "invalid", "error"):
                cached_message = record.get("message") or "License status loaded from cache."
                if record.get("status") == "error":
                    cached_message = LICENSE_SERVICE_UNAVAILABLE_MESSAGE
                return {
                    "valid": record.get("status") == "valid",
                    "status": record.get("status"),
                    "message": cached_message,
                    "licensed_to": record.get("licensed_to") or "",
                    "license_period": {
                        "start_date": "",
                        "end_date": "",
                    },
                    "record": record,
                }

    try:
        verification = call_license_verifier(record.get("license_key"))
    except requests.RequestException as exc:
        logger.warning("License verification service request failed: %s", exc)
        message = LICENSE_SERVICE_UNAVAILABLE_MESSAGE
        # Fail closed when the verifier cannot be reached. We preserve the authenticated Flask
        # session in app.require_authentication(), but block protected application routes until
        # the license service recovers and show this explicit service error on the license page.
        save_license_record(
            record.get("license_key"),
            "error",
            message,
            record.get("licensed_to") or "",
        )
        refreshed = get_license_record()
        return {
            "valid": False,
            "status": "error",
            "message": message,
            "licensed_to": refreshed.get("licensed_to") if refreshed else "",
            "license_period": {
                "start_date": "",
                "end_date": "",
            },
            "record": refreshed,
        }

    transient_failure = not verification["valid"] and is_transient_license_response(
        verification.get("status_code", 0)
    )
    status = "valid" if verification["valid"] else "error" if transient_failure else "invalid"
    message = verification["message"]
    if transient_failure:
        logger.warning(
            "License verification service returned HTTP %s: %s",
            verification.get("status_code", 0),
            message,
        )
        message = LICENSE_SERVICE_UNAVAILABLE_MESSAGE

    save_license_record(
        record.get("license_key"),
        status,
        message,
        verification.get("licensed_to") or "",
    )
    refreshed = get_license_record()
    return {
        "valid": verification["valid"],
        "status": status,
        "message": message,
        "licensed_to": verification.get("licensed_to") or "",
        "license_period": verification.get("license_period") or {
            "start_date": "",
            "end_date": "",
        },
        "record": refreshed,
    }
