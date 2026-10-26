"""
Flask routes for the Test Execution page.

Login is enforced by app.require_authentication and CSRF by the global
CSRFProtect; this blueprint adds the QA/Admin role check that the other
testing pages use. Credentials for the application under test are read from
the selected project's configuration on the server and never sent by the browser.
"""
from __future__ import annotations

import io
import os
import shutil
from typing import Callable

from flask import Blueprint, flash, jsonify, redirect, render_template, request, send_file, session, url_for

from .integrations import IntegrationError
from .models import RunConfig, TestCase
from .playwright_mcp import PLAYWRIGHT_MCP_PACKAGE
from .security import PolicyError
from .service import TestExecutionService


def create_blueprint(service: TestExecutionService, project_settings: Callable[[int], dict], env_path: str) -> Blueprint:
    bp = Blueprint("test_execution", __name__)

    def _role_ok() -> bool:
        return session.get("user_role", "").lower() in ("qa", "admin")

    @bp.before_request
    def _guard():
        if "user_id" not in session:
            return None   # app-level guard answers with 401/redirect
        if not _role_ok():
            if request.path.startswith("/api/"):
                return jsonify({"status": "error", "message": "Not authorized"}), 403
            flash("Not authorized", "error")
            return redirect(url_for("home"))
        return None

    def _user() -> str:
        return str(session.get("user_name") or session.get("user_id") or "unknown")

    def _ok(**data):
        return jsonify({"status": "success", **data})

    def _err(msg, code=400):
        return jsonify({"status": "error", "message": str(msg)}), code

    def _project(project_id) -> dict:
        try:
            pid = int(project_id)
        except (TypeError, ValueError):
            return {}
        return project_settings(pid) or {}

    def _base_and_creds(data: dict) -> tuple[str, dict]:
        proj = _project(data.get("project_id"))
        base = (data.get("base_url") or "").strip() or proj.get("baseurl", "")
        creds = {"username": proj.get("username", ""), "password": proj.get("password", "")}
        return base, creds

    @bp.route("/qa/test-execution", methods=["GET"])
    def page():
        return render_template("test_execution.html")

    @bp.route("/api/test-execution/status", methods=["GET"])
    def status():
        jira_ok = all(os.environ.get(k, "").strip().strip('"') for k in ("JIRA_SERVER", "JIRA_EMAIL", "JIRA_API_TOKEN"))
        return _ok(
            jira_configured=jira_ok,
            zephyr_configured=service.zephyr_configured(),
            node_available=bool(shutil.which("npx") or shutil.which("npx.cmd")),
            playwright_mcp=PLAYWRIGHT_MCP_PACKAGE,
            ai_configured=bool(os.environ.get("API_KEY", "").strip()),
        )

    @bp.route("/api/test-execution/project/<int:project_id>", methods=["GET"])
    def project_info(project_id):
        proj = _project(project_id)
        return _ok(base_url=proj.get("baseurl", ""), has_username=bool(proj.get("username")),
                   has_password=bool(proj.get("password")))

    # ── Excel ──────────────────────────────────────────────────────────
    @bp.route("/api/test-execution/upload", methods=["POST"])
    def upload():
        f = request.files.get("file")
        if f is None:
            return _err("No file uploaded.")
        try:
            return _ok(**service.save_upload(_user(), f.filename or "", f.read()))
        except PolicyError as exc:
            return _err(exc)

    @bp.route("/api/test-execution/excel/preview", methods=["POST"])
    def excel_preview():
        data = request.get_json(silent=True) or {}
        try:
            cases, meta = service.excel_cases(data.get("upload_id", ""), data.get("sheet", ""), data.get("mapping") or {})
            # A run cannot start without a base URL, so plan as if one is set (execution does the same).
            _, creds = _base_and_creds(data)
            return _ok(**service.preview(cases, True, bool(creds["username"] or creds["password"])), meta=meta)
        except PolicyError as exc:
            return _err(exc)

    # ── Jira / Zephyr ──────────────────────────────────────────────────
    @bp.route("/api/test-execution/jira/search", methods=["GET"])
    def jira_search():
        try:
            return _ok(issues=service.jira().search(request.args.get("q", "")))
        except IntegrationError as exc:
            return _err(exc, 502)

    @bp.route("/api/test-execution/remote/preview", methods=["POST"])
    def remote_preview():
        data = request.get_json(silent=True) or {}
        try:
            cases, notes = service.remote_cases(data.get("source", ""), data.get("ref", ""))
            # A run cannot start without a base URL, so plan as if one is set (execution does the same).
            _, creds = _base_and_creds(data)
            return _ok(**service.preview(cases, True, bool(creds["username"] or creds["password"])), notes=notes)
        except (IntegrationError, PolicyError) as exc:
            return _err(exc, 502 if isinstance(exc, IntegrationError) else 400)

    @bp.route("/api/test-execution/zephyr/cycles", methods=["GET"])
    def zephyr_cycles():
        project = (request.args.get("project") or "").strip().upper()
        if not project.replace("_", "").isalnum():
            return _err("Enter a Jira project key.")
        try:
            cycles = service.zephyr().client.test_cycles(project)
            return _ok(cycles=[{"key": c.get("key"), "name": c.get("name")} for c in cycles])
        except IntegrationError as exc:
            return _err(exc, 502)

    @bp.route("/api/test-execution/zephyr/config", methods=["GET", "POST"])
    def zephyr_config():
        if request.method == "GET":
            return _ok(configured=service.zephyr_configured(), base_url=os.environ.get("ZEPHYR_BASE_URL", ""))
        data = request.get_json(silent=True) or {}
        token = (data.get("api_token") or "").strip()
        base = (data.get("base_url") or "").strip()
        if base and not base.startswith("https://"):
            return _err("Zephyr base URL must use https.")
        updates = {"ZEPHYR_BASE_URL": base}
        if token:
            updates["ZEPHYR_API_TOKEN"] = token
        try:
            _update_env(env_path, updates)
        except PolicyError as exc:
            return _err(exc)
        return _ok(configured=service.zephyr_configured())

    # ── runs ───────────────────────────────────────────────────────────
    @bp.route("/api/test-execution/runs", methods=["GET", "POST"])
    def runs():
        if request.method == "GET":
            return _ok(runs=service.list_runs())
        data = request.get_json(silent=True) or {}
        source = data.get("source")
        selected = set(data.get("selected_keys") or [])
        base, creds = _base_and_creds(data)
        cfg = RunConfig.from_dict({**(data.get("config") or {}), "base_url": base})
        try:
            # Test cases are re-derived on the server; the browser only sends the selection.
            if source == "excel":
                cases, meta = service.excel_cases(data.get("upload_id", ""), data.get("sheet", ""), data.get("mapping") or {})
            elif source in ("jira", "zephyr"):
                cases, _ = service.remote_cases(source, data.get("ref", ""))
                meta = {"ref": data.get("ref", "")}
            else:
                return _err("Choose a test case source.")
            chosen: list[TestCase] = [c for c in cases if c.key in selected] if selected else cases
            meta["project_id"] = data.get("project_id")
            run_id = service.start_run(_user(), source, chosen, cfg, meta, creds)
            return _ok(run_id=run_id)
        except IntegrationError as exc:
            return _err(exc, 502)
        except PolicyError as exc:
            return _err(exc)

    @bp.route("/api/test-execution/runs/<run_id>", methods=["GET"])
    def run_detail(run_id):
        try:
            return _ok(**service.run_view(run_id, since=request.args.get("since", 0, type=int)))
        except PolicyError as exc:
            return _err(exc, 404)

    @bp.route("/api/test-execution/runs/<run_id>/cancel", methods=["POST"])
    def run_cancel(run_id):
        return _ok(cancelled=service.cancel(run_id, _user()))

    @bp.route("/api/test-execution/runs/<run_id>/retry-failed", methods=["POST"])
    def run_retry(run_id):
        try:
            run = service.store.get_run(run_id) or {}
            _, creds = _base_and_creds({"project_id": (run.get("source_meta") or {}).get("project_id")})
            return _ok(count=service.retry_failed(run_id, _user(), creds))
        except PolicyError as exc:
            return _err(exc)

    @bp.route("/api/test-execution/runs/<run_id>/retry-publications", methods=["POST"])
    def run_retry_pub(run_id):
        try:
            service.retry_publications(run_id, _user())
            return _ok()
        except PolicyError as exc:
            return _err(exc)

    @bp.route("/api/test-execution/runs/<run_id>/excel", methods=["GET"])
    def run_excel(run_id):
        try:
            service._require_run(run_id)
        except PolicyError as exc:
            return _err(exc, 404)
        path = service.output_path(run_id)
        if not os.path.exists(path):
            return _err("No updated workbook for this run.", 404)
        return send_file(path, as_attachment=True, download_name=f"{run_id}-results.xlsx",
                         mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

    @bp.route("/api/test-execution/runs/<run_id>/export", methods=["GET"])
    def run_export(run_id):
        fmt = "json" if request.args.get("format") == "json" else "csv"
        try:
            data, mime, name = service.export_summary(run_id, fmt)
        except PolicyError as exc:
            return _err(exc, 404)
        return send_file(io.BytesIO(data), as_attachment=True, download_name=name, mimetype=mime)

    @bp.route("/api/test-execution/runs/<run_id>/evidence/<evidence_id>", methods=["GET"])
    def run_evidence(run_id, evidence_id):
        if not evidence_id.isalnum():
            return _err("Invalid evidence id.", 404)
        try:
            data = service.evidence_bytes(run_id, evidence_id)
        except PolicyError as exc:
            return _err(exc, 404)
        if data is None:
            return _err("Evidence not found.", 404)
        return send_file(io.BytesIO(data), mimetype="image/png", download_name=f"{evidence_id}.png")

    return bp


def _update_env(env_path: str, updates: dict):
    """Same .env mechanism Configure Jira / Configure AI use (the app's approved secret store)."""
    for v in updates.values():
        if any(ch in v for ch in ('"', "\n", "\r")):
            raise PolicyError("Configuration values cannot contain quotes or line breaks.")
    lines = []
    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    seen = set()
    out = []
    for line in lines:
        key = line.split("=", 1)[0].strip()
        if key in updates:
            out.append(f'{key} = "{updates[key]}"\n')
            seen.add(key)
        else:
            out.append(line)
    for k, v in updates.items():
        if k not in seen:
            out.append(f'{k} = "{v}"\n')
    with open(env_path, "w", encoding="utf-8") as f:
        f.writelines(out)
    for k, v in updates.items():
        os.environ[k] = v
