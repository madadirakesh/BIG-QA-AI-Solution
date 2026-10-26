"""
Run manager: owns uploads, background runs, progress events, retries,
publication and exports. Routes stay thin and call into this.
"""
from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
import os
import re
import shutil
import sys
import threading
import uuid
from collections import deque
from typing import Callable, Optional

from . import excel_io
from .ai_planner import AIPlanner
from .engine import ExecutionEngine
from .evidence import EvidenceStore, LocalEvidenceStore
from .integrations import IntegrationError, JiraAdapter, ZephyrAdapter, ZephyrScaleClient
from .models import (
    TestCase, TestCaseResult, RunConfig, UsageMetrics, compute_summary, utcnow_iso,
    FAILED, BLOCKED, NOT_EXECUTED, IN_PROGRESS,
    PUB_PENDING, PUB_PUBLISHED, PUB_FAILED, PUB_SKIPPED,
)
from .security import NavigationPolicy, PolicyError, Redactor, is_production_url, sanitize_filename, safe_join, validate_upload
from .step_parser import plan_test_case
from .store import ExecutionStore

log = logging.getLogger("test_execution")

RUN_QUEUED, RUN_PLANNING, RUN_RUNNING, RUN_PUBLISHING = "queued", "planning", "running", "publishing"
RUN_COMPLETED, RUN_CANCELLED, RUN_FAILED, RUN_INTERRUPTED = "completed", "cancelled", "failed", "interrupted"
ACTIVE_STATES = {RUN_QUEUED, RUN_PLANNING, RUN_RUNNING, RUN_PUBLISHING}
MAX_ACTIVE_RUNS = int(os.environ.get("TEST_EXECUTION_MAX_ACTIVE_RUNS") or "2")
MAX_CASES_PER_RUN = int(os.environ.get("TEST_EXECUTION_MAX_CASES") or "500")
UPLOAD_ID = re.compile(r"^[a-f0-9]{32}$")
RUN_ID = re.compile(r"^TE-\d{8}-[a-f0-9]{8}$")


def build_evidence_store(root: str) -> EvidenceStore:
    """Extension point: return an object-storage EvidenceStore here when one is configured."""
    return LocalEvidenceStore(os.path.join(root, "evidence"))


class _ActiveRun:
    def __init__(self, run_id: str):
        self.run_id = run_id
        self.cancel = threading.Event()
        self.events: deque = deque(maxlen=500)
        self.seq = 0
        self.lock = threading.Lock()
        self.thread: Optional[threading.Thread] = None
        self.progress = {"phase": RUN_QUEUED, "done": 0, "total": 0, "current": ""}

    def emit(self, event: dict):
        with self.lock:
            self.seq += 1
            self.events.append({"seq": self.seq, "ts": utcnow_iso(), **event})


class TestExecutionService:
    __test__ = False   # not a pytest test class

    def __init__(self, data_root: str, db_path: str, jira_client_factory: Optional[Callable] = None,
                 ai_call: Optional[Callable] = None, session_factory=None, evidence_store: Optional[EvidenceStore] = None):
        self.root = os.path.realpath(data_root)
        for sub in ("uploads", "runs", "outputs"):
            os.makedirs(os.path.join(self.root, sub), exist_ok=True)
        self.store = ExecutionStore(db_path)
        self.evidence = evidence_store or build_evidence_store(self.root)
        self.jira_client_factory = jira_client_factory
        self.ai_call = ai_call
        self.session_factory = session_factory   # tests inject fakes; None = real Playwright MCP
        self._active: dict[str, _ActiveRun] = {}
        self._lock = threading.Lock()
        prompts_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        if prompts_root not in sys.path:
            sys.path.insert(0, prompts_root)   # BIG_QA_Solution/, home of the prompts package
        self._mark_interrupted_runs()

    def _mark_interrupted_runs(self):
        """Runs that were active when the app stopped keep their partial results."""
        for r in self.store.list_runs(limit=200):
            if r["status"] in ACTIVE_STATES:
                self.store.update_run(r["run_id"], status=RUN_INTERRUPTED, finished_at=utcnow_iso())
                self.store.audit(r["run_id"], "system", "interrupted", "Application restarted during the run")

    # ── Excel upload & preview ──────────────────────────────────────────
    def _upload_paths(self, upload_id: str) -> tuple[str, str]:
        if not UPLOAD_ID.match(upload_id or ""):
            raise PolicyError("Invalid upload id.")
        meta = safe_join(self.root, "uploads", f"{upload_id}.json")
        if not os.path.exists(meta):
            raise PolicyError("Upload not found; please upload the workbook again.")
        with open(meta, encoding="utf-8") as f:
            info = json.load(f)
        return safe_join(self.root, "uploads", f"{upload_id}{info['ext']}"), info

    def save_upload(self, user: str, filename: str, data: bytes) -> dict:
        ext = validate_upload(filename, data)
        upload_id = uuid.uuid4().hex
        try:
            sheets = excel_io.read_workbook(data, ext)
        except Exception as exc:
            raise PolicyError(f"The workbook could not be read: {exc}") from exc
        with open(safe_join(self.root, "uploads", f"{upload_id}{ext}"), "wb") as f:
            f.write(data)
        with open(safe_join(self.root, "uploads", f"{upload_id}.json"), "w", encoding="utf-8") as f:
            json.dump({"filename": sanitize_filename(filename), "ext": ext, "user": user, "uploaded_at": utcnow_iso()}, f)
        return {
            "upload_id": upload_id, "filename": sanitize_filename(filename), "ext": ext,
            "sheets": [{"name": s.name, "headers": s.headers, "header_row": s.header_row, "row_count": len(s.rows),
                        "suggested_mapping": excel_io.suggest_mapping(s.headers)} for s in sheets],
            "fields": list(excel_io.FIELD_SYNONYMS.keys()),
        }

    def excel_cases(self, upload_id: str, sheet_name: str, mapping: dict) -> tuple[list[TestCase], dict]:
        path, info = self._upload_paths(upload_id)
        with open(path, "rb") as f:
            data = f.read()
        sheets = {s.name: s for s in excel_io.read_workbook(data, info["ext"])}
        sheet = sheets.get(sheet_name)
        if sheet is None:
            raise PolicyError(f"Worksheet '{sheet_name}' not found.")
        clean = {}
        for k in excel_io.FIELD_SYNONYMS:
            v = (mapping or {}).get(k)
            clean[k] = int(v) if v is not None and str(v).lstrip("-").isdigit() and 0 <= int(v) < len(sheet.headers) else None
        if clean.get("steps") is None:
            raise PolicyError("Map the Test Steps column before previewing.")
        cases = excel_io.parse_test_cases(sheet, clean)
        meta = {"upload_id": upload_id, "filename": info["filename"], "ext": info["ext"], "sheet": sheet_name,
                "header_row": sheet.header_row, "mapping": clean}
        return cases, meta

    # ── Jira / Zephyr ───────────────────────────────────────────────────
    def jira(self) -> JiraAdapter:
        if self.jira_client_factory is None:
            raise IntegrationError("Jira is not configured.")
        return JiraAdapter(self.jira_client_factory)

    @staticmethod
    def zephyr_configured() -> bool:
        return bool(os.environ.get("ZEPHYR_API_TOKEN", "").strip())

    def zephyr(self, test_cycle: str = "") -> ZephyrAdapter:
        return ZephyrAdapter(ZephyrScaleClient(os.environ.get("ZEPHYR_API_TOKEN", "").strip(),
                                               os.environ.get("ZEPHYR_BASE_URL", "").strip()), test_cycle)

    def remote_cases(self, source: str, ref: str) -> tuple[list[TestCase], list[str]]:
        if source == "jira":
            cases, notes = self.jira().test_cases_for(ref.strip().upper())
            if self.zephyr_configured():
                try:
                    zc, zn = self.zephyr().test_cases_for(ref)
                    cases += zc
                    notes += zn
                except IntegrationError as exc:
                    notes.append(f"Zephyr lookup skipped: {exc}")
            return cases, notes
        if source == "zephyr":
            return self.zephyr().test_cases_for(ref)
        raise PolicyError("Unknown source.")

    # ── preview ─────────────────────────────────────────────────────────
    @staticmethod
    def preview(cases: list[TestCase], has_base_url: bool, has_credentials: bool) -> dict:
        """Deterministic plan only (no AI): shows what will need AI interpretation before anything runs."""
        need_ai = 0
        for tc in cases:
            plan_test_case(tc, has_base_url, has_credentials)
            need_ai += sum(1 for s in tc.steps if s.plan_source == "unresolved")
        total_steps = sum(len(tc.steps) for tc in cases)
        return {
            "cases": [tc.to_dict() for tc in cases],
            "stats": {"total": len(cases), "valid": sum(1 for c in cases if c.valid),
                      "invalid": sum(1 for c in cases if not c.valid), "steps": total_steps,
                      "steps_deterministic": total_steps - need_ai, "steps_needing_ai": need_ai},
        }

    # ── runs ────────────────────────────────────────────────────────────
    def start_run(self, user: str, source: str, cases: list[TestCase], config: RunConfig, source_meta: dict,
                  credentials: Optional[dict] = None) -> str:
        if not cases:
            raise PolicyError("Select at least one test case.")
        if len(cases) > MAX_CASES_PER_RUN:
            raise PolicyError(f"A run is limited to {MAX_CASES_PER_RUN} test cases.")
        if not config.base_url:
            raise PolicyError("A base URL is required (from the selected project or entered for this run).")
        NavigationPolicy(config.base_url, config.allowed_hosts)   # validates the URL
        if is_production_url(config.base_url) and not config.confirm_production:
            raise PolicyError("The base URL matches a production host. Confirm production execution explicitly to continue.")
        with self._lock:
            active = [r for r in self._active.values() if r.thread and r.thread.is_alive()]
            if len(active) >= MAX_ACTIVE_RUNS:
                raise PolicyError(f"{len(active)} runs are already in progress. Wait for one to finish.")
            run_id = f"TE-{utcnow_iso()[:10].replace('-', '')}-{uuid.uuid4().hex[:8]}"
            state = _ActiveRun(run_id)
            self._active[run_id] = state
        self.store.create_run(run_id, user, source, RUN_QUEUED, config.to_dict(), source_meta)
        self.store.save_cases(run_id, [tc.to_dict() for tc in cases])
        self.store.audit(run_id, user, "run_started",
                         f"{len(cases)} test case(s) from {source}; base URL {config.base_url}; screenshots {config.screenshot_mode}")
        state.thread = threading.Thread(target=self._run_thread, name=f"test-exec-{run_id}",
                                        args=(state, cases, config, credentials or {}, user, None), daemon=True)
        state.thread.start()
        return run_id

    def retry_failed(self, run_id: str, user: str, credentials: Optional[dict] = None) -> int:
        run = self._require_run(run_id)
        if self.is_active(run_id):
            raise PolicyError("The run is still in progress.")
        pairs = self.store.load_cases(run_id)
        retry_keys = [c["key"] for c, r in pairs if r is None or r.get("status") in (FAILED, BLOCKED, NOT_EXECUTED)]
        if not retry_keys:
            raise PolicyError("There are no failed, blocked or unexecuted tests to retry.")
        cases = [TestCase.from_dict(c) for c, _ in pairs if c["key"] in retry_keys]
        config = RunConfig.from_dict(run["config"])
        state = _ActiveRun(run_id)
        with self._lock:
            self._active[run_id] = state
        self.store.audit(run_id, user, "retry_failed", ", ".join(retry_keys)[:1500])
        prior = {c["key"]: (r or {}).get("retry_count", 0) for c, r in pairs}
        state.thread = threading.Thread(target=self._run_thread, name=f"test-exec-{run_id}-retry",
                                        args=(state, cases, config, credentials or {}, user, prior), daemon=True)
        state.thread.start()
        return len(cases)

    def retry_publications(self, run_id: str, user: str) -> None:
        run = self._require_run(run_id)
        if self.is_active(run_id):
            raise PolicyError("The run is still in progress.")
        state = _ActiveRun(run_id)
        with self._lock:
            self._active[run_id] = state
        self.store.audit(run_id, user, "retry_publications", "")
        config = RunConfig.from_dict(run["config"])

        def work():
            try:
                self.store.update_run(run_id, status=RUN_PUBLISHING)
                state.progress["phase"] = RUN_PUBLISHING
                pairs = self.store.load_cases(run_id)
                cases = [TestCase.from_dict(c) for c, _ in pairs]
                results = {c["key"]: TestCaseResult.from_dict(r) for c, r in pairs if r}
                self._publish(run_id, run, cases, results, config, state, only_failed=True)
                self._finalize(run_id, run, cases, results, state, RUN_COMPLETED)
            except Exception as exc:
                log.exception("publication retry failed")
                self.store.update_run(run_id, status=RUN_FAILED)
                state.emit({"type": "error", "message": str(exc)[:500]})
        state.thread = threading.Thread(target=work, daemon=True)
        state.thread.start()

    def cancel(self, run_id: str, user: str) -> bool:
        state = self._active.get(run_id)
        if not state or not state.thread or not state.thread.is_alive():
            return False
        state.cancel.set()
        state.emit({"type": "cancel_requested"})
        self.store.audit(run_id, user, "cancel_requested", "")
        return True

    def is_active(self, run_id: str) -> bool:
        s = self._active.get(run_id)
        return bool(s and s.thread and s.thread.is_alive())

    def _require_run(self, run_id: str) -> dict:
        if not RUN_ID.match(run_id or ""):
            raise PolicyError("Invalid run id.")
        run = self.store.get_run(run_id)
        if not run:
            raise PolicyError("Run not found.")
        return run

    # ── the background run ──────────────────────────────────────────────
    def _run_thread(self, state: _ActiveRun, cases: list[TestCase], config: RunConfig, credentials: dict,
                    user: str, prior_retries: Optional[dict]):
        run_id = state.run_id
        try:
            asyncio.run(self._run_async(state, cases, config, credentials, prior_retries))
        except Exception as exc:
            log.exception("test execution run %s failed", run_id)
            self.store.update_run(run_id, status=RUN_FAILED, finished_at=utcnow_iso())
            self.store.audit(run_id, user, "run_failed", str(exc)[:500])
            state.emit({"type": "error", "message": f"Run failed: {str(exc)[:300]}"})

    async def _run_async(self, state: _ActiveRun, cases: list[TestCase], config: RunConfig, credentials: dict,
                         prior_retries: Optional[dict]):
        run_id = state.run_id
        run = self.store.get_run(run_id)
        redactor = Redactor([credentials.get("password", "")])
        usage = UsageMetrics(**{k: v for k, v in (run.get("usage") or {}).items() if k in UsageMetrics.__dataclass_fields__})
        planner = AIPlanner(self._ai_call() if config.use_ai_planner else None, self.store, usage, redactor)

        # 1. plan: deterministic first, AI only for what is left
        self.store.update_run(run_id, status=RUN_PLANNING)
        state.progress.update(phase=RUN_PLANNING, total=len(cases), done=0)
        state.emit({"type": "phase", "phase": RUN_PLANNING})
        has_creds = bool(credentials.get("username") or credentials.get("password"))
        for i, tc in enumerate(cases, start=1):
            if state.cancel.is_set():
                break
            plan_test_case(tc, True, has_creds)
            usage.steps_parsed_deterministically += sum(1 for s in tc.steps if s.plan_source == "parser")
            if tc.valid and any(s.plan_source == "unresolved" for s in tc.steps):
                state.progress["current"] = f"Interpreting steps of {tc.key}"
                await planner.plan_unresolved(tc)
            state.progress["done"] = i
        self.store.save_cases(run_id, [tc.to_dict() for tc in cases])

        # 2. execute
        self.store.update_run(run_id, status=RUN_RUNNING, usage=usage.to_dict())
        state.progress.update(phase=RUN_RUNNING, done=0, total=len(cases), current="")
        state.emit({"type": "phase", "phase": RUN_RUNNING})
        work_root = safe_join(self.root, "runs", run_id)
        policy = NavigationPolicy(config.base_url, config.allowed_hosts)

        def on_result(r: TestCaseResult):
            if prior_retries is not None:
                r.retry_count += prior_retries.get(r.test_key, 0) + 1
            self.store.save_result(run_id, r.test_key, r.to_dict())
            state.progress["done"] += 1
            state.emit({"type": "result", "test_key": r.test_key, "status": r.status})

        def on_event(e: dict):
            if e.get("type") == "test_started":
                state.progress["current"] = f"{e['test_key']}: {e.get('title', '')}"[:200]
            state.emit(e)

        engine = ExecutionEngine(run_id, config, policy, self.evidence, usage, redactor, planner=planner,
                                 credentials=credentials, session_factory=self.session_factory,
                                 on_event=on_event, on_result=on_result, cancel_event=state.cancel, work_root=work_root)
        await engine.run(cases)
        self.store.update_run(run_id, usage=usage.to_dict())

        # 3. publish + Excel output (also on cancel: partial results are still results)
        pairs = self.store.load_cases(run_id)
        all_cases = [TestCase.from_dict(c) for c, _ in pairs]
        results = {c["key"]: TestCaseResult.from_dict(r) for c, r in pairs if r}
        if not state.cancel.is_set():
            self.store.update_run(run_id, status=RUN_PUBLISHING)
            state.progress["phase"] = RUN_PUBLISHING
            state.emit({"type": "phase", "phase": RUN_PUBLISHING})
            self._publish(run_id, run, all_cases, results, config, state, only_failed=False)
        self._finalize(run_id, run, all_cases, results, state, RUN_CANCELLED if state.cancel.is_set() else RUN_COMPLETED)
        shutil.rmtree(work_root, ignore_errors=True)   # MCP scratch (snapshots, logs); evidence lives elsewhere

    def _ai_call(self):
        if self.ai_call is not None:
            return self.ai_call

        async def call(prompt: str) -> str:
            from api import backend
            return await backend.call_ai(prompt, expect_json=True, retries=2)
        return call

    def _publish(self, run_id: str, run: dict, cases: list[TestCase], results: dict, config: RunConfig,
                 state: _ActiveRun, only_failed: bool):
        source = run["source"]
        publishers = []
        if source == "excel":
            for r in results.values():
                r.publication["excel"] = {"status": PUB_PENDING, "message": "Written when the updated workbook is generated"}
        elif config.publish_results:
            try:
                if source == "jira":
                    publishers.append(self.jira())
                if source in ("jira", "zephyr") and self.zephyr_configured():
                    publishers.append(self.zephyr(config.zephyr_test_cycle))
            except IntegrationError as exc:
                state.emit({"type": "error", "message": str(exc)})
                for r in results.values():
                    r.publication[source] = {"status": PUB_FAILED, "message": str(exc)[:300]}
        else:
            for r in results.values():
                r.publication[source] = {"status": PUB_SKIPPED, "message": "Publishing was turned off for this run"}

        for tc in cases:
            r = results.get(tc.key)
            if r is None or r.status == IN_PROGRESS:
                continue
            for pub in publishers:
                target = tc.source_ref.get("zephyr_key") if pub.name == "zephyr" else \
                    (tc.source_ref.get("issue_key") or tc.source_ref.get("jira_issue"))
                if not target:
                    continue
                prev = r.publication.get(pub.name, {})
                if only_failed and prev.get("status") not in (PUB_FAILED, PUB_PENDING, None):
                    continue
                ikey = pub.idempotency_key(tc, r)
                ledger = self.store.get_publication(ikey)
                if ledger and ledger["status"] == PUB_PUBLISHED:
                    r.publication[pub.name] = {"status": PUB_PUBLISHED, "ref": ledger["external_ref"],
                                               "message": "Already published (duplicate prevented)"}
                    continue
                try:
                    status, ref, msg = pub.publish(tc, r, lambda ev: self.evidence.get(run_id, ev.path), run_id)
                except Exception as exc:
                    status, ref, msg = PUB_FAILED, "", f"{exc.__class__.__name__}: {exc}"
                self.store.record_publication(run_id, tc.key, pub.name, ikey, status, ref, msg[:1000])
                self.store.audit(run_id, "system", f"publish_{pub.name}", f"{tc.key} -> {status} {ref} {msg[:300]}")
                r.publication[pub.name] = {"status": status, "ref": ref, "message": msg[:500]}
                state.emit({"type": "published", "test_key": tc.key, "destination": pub.name, "status": status})
            self.store.save_result(run_id, tc.key, r.to_dict())

    def _finalize(self, run_id: str, run: dict, cases: list[TestCase], results: dict, state: _ActiveRun, final: str):
        ordered = [results[c.key] for c in cases if c.key in results]
        fresh = self.store.get_run(run_id) or run
        usage = UsageMetrics(**{k: v for k, v in (fresh.get("usage") or {}).items() if k in UsageMetrics.__dataclass_fields__})
        summary = compute_summary(ordered, usage)
        summary.update(run_id=run_id, started_at=run.get("created_at"), finished_at=utcnow_iso(),
                       not_run=len([c for c in cases if c.key not in results]))
        if run["source"] == "excel":
            try:
                notes = self._write_excel(run_id, run, cases, results, summary)
                summary["excel_notes"] = notes
                for r in ordered:
                    r.publication["excel"] = {"status": PUB_PUBLISHED, "message": "Included in the updated workbook"}
                    self.store.save_result(run_id, r.test_key, r.to_dict())
                summary.update(compute_summary(ordered, usage))   # publication counts changed
            except Exception as exc:
                log.exception("excel output failed")
                summary["excel_error"] = str(exc)[:300]
                for r in ordered:
                    r.publication["excel"] = {"status": PUB_FAILED, "message": f"Workbook generation failed: {exc}"[:300]}
                    self.store.save_result(run_id, r.test_key, r.to_dict())
        self.store.update_run(run_id, status=final, summary=summary, finished_at=utcnow_iso())
        self.store.audit(run_id, "system", f"run_{final}",
                         f"passed={summary['passed']} failed={summary['failed']} blocked={summary['blocked']} not_executed={summary['not_executed']}")
        state.progress.update(phase=final, current="")
        state.emit({"type": "phase", "phase": final})

    def _write_excel(self, run_id, run, cases, results, summary) -> list[str]:
        meta = run["source_meta"]
        path, info = self._upload_paths(meta["upload_id"])
        with open(path, "rb") as f:
            original = f.read()
        base = os.environ.get("TEST_EXECUTION_PUBLIC_BASE_URL", "").rstrip("/")
        data, notes = excel_io.build_result_workbook(
            original, info["ext"], meta["sheet"], meta["header_row"], meta["mapping"], cases, results,
            evidence_reader=lambda ev: self.evidence.get(run_id, ev.path),
            evidence_url=lambda ev: f"{base}/api/test-execution/runs/{run_id}/evidence/{ev.evidence_id}",
            embed=RunConfig.from_dict(run["config"]).embed_screenshots, summary=summary)
        with open(self.output_path(run_id), "wb") as f:
            f.write(data)
        return notes

    def output_path(self, run_id: str) -> str:
        return safe_join(self.root, "outputs", f"{sanitize_filename(run_id)}.xlsx")

    # ── read side ───────────────────────────────────────────────────────
    def run_view(self, run_id: str, since: int = 0) -> dict:
        run = self._require_run(run_id)
        pairs = self.store.load_cases(run_id)
        results = []
        for c, r in pairs:
            if r is None:
                r = {"test_key": c["key"], "title": c["title"], "source": c["source"], "status": NOT_EXECUTED,
                     "steps": [], "evidence": [], "publication": {}, "duration_ms": 0}
            r["plan"] = [{"number": s["number"], "plan_source": s.get("plan_source"), "plan_error": s.get("plan_error", "")}
                         for s in c.get("steps", [])]
            results.append(r)
        ordered = [TestCaseResult.from_dict({k: v for k, v in r.items() if k != "plan"})
                   for r in results if "run_id" in r]
        live = compute_summary(ordered, None)
        live["total"] = len(pairs)
        live["not_executed"] += len(pairs) - len(ordered)
        state = self._active.get(run_id)
        events, progress = [], None
        if state:
            with state.lock:
                events = [e for e in state.events if e["seq"] > since]
            progress = dict(state.progress)
        return {
            "run": {k: run[k] for k in ("run_id", "created_by", "source", "status", "created_at", "finished_at")},
            "config": run["config"], "source_meta": {k: v for k, v in run["source_meta"].items() if k != "mapping"},
            "summary": run["summary"] if run["summary"] and run["status"] not in ACTIVE_STATES else {**live, "usage": run["usage"]},
            "usage": run["usage"], "results": results, "events": events, "progress": progress,
            "active": self.is_active(run_id), "has_excel": os.path.exists(self.output_path(run_id)),
        }

    def list_runs(self) -> list[dict]:
        return self.store.list_runs()

    def evidence_bytes(self, run_id: str, evidence_id: str) -> Optional[bytes]:
        self._require_run(run_id)
        for c, r in self.store.load_cases(run_id):
            for ev in (r or {}).get("evidence", []):
                if ev["evidence_id"] == evidence_id:
                    return self.evidence.get(run_id, ev["path"])
        return None

    def export_summary(self, run_id: str, fmt: str) -> tuple[bytes, str, str]:
        view = self.run_view(run_id)
        if fmt == "json":
            payload = {k: view[k] for k in ("run", "config", "summary", "results")}
            payload["audit"] = self.store.audit_log(run_id)
            payload["publications"] = self.store.list_publications(run_id)
            return json.dumps(payload, indent=2).encode(), "application/json", f"{run_id}-summary.json"
        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(["Test Case ID", "Title", "Source", "Status", "Duration (s)", "Failed Step", "Failure Reason",
                    "Screenshots", "Publication", "Started", "Ended", "Retries"])
        for r in view["results"]:
            pubs = "; ".join(f"{k}: {v.get('status')}" for k, v in (r.get("publication") or {}).items())
            w.writerow([r["test_key"], r.get("title", ""), r.get("source", ""), r["status"],
                        round((r.get("duration_ms") or 0) / 1000.0, 2), r.get("failed_step") or "",
                        r.get("failure_reason", ""), len(r.get("evidence") or []), pubs,
                        r.get("started_at", ""), r.get("ended_at", ""), r.get("retry_count", 0)])
        return out.getvalue().encode("utf-8-sig"), "text/csv", f"{run_id}-summary.csv"
