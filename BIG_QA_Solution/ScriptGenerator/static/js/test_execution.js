/* Test Execution page.
 * Everything that comes from a workbook, Jira or Zephyr is untrusted: it is only
 * ever inserted through esc() or textContent. */
(function () {
    'use strict';

    const API = '/api/test-execution';
    const $ = (id) => document.getElementById(id);
    const FIELD_LABELS = {
        id: 'Test Case ID', title: 'Title', description: 'Description', preconditions: 'Preconditions',
        steps: 'Test Steps / Step Action *', step_number: 'Step No.', step_expected: 'Step Expected Result',
        expected: 'Expected Result (overall)', test_data: 'Test Data', actual: 'Actual Result (write-back)',
        status: 'Status (write-back)', evidence: 'Evidence (write-back)', defect: 'Defect ID', comments: 'Comments',
    };
    const STATUS_BADGE = { 'Passed': 'badge-success', 'Failed': 'badge-error', 'Blocked': 'badge-warning',
        'Not Executed': '', 'In Progress': 'badge-info' };
    const PUB_BADGE = { 'Published': 'badge-success', 'Failed': 'badge-error', 'Pending': 'badge-info',
        'Skipped': '', 'Unsupported': 'badge-warning' };

    const state = {
        source: null, upload: null, previewCases: [], selected: new Set(), ref: '', meta: null,
        runId: null, since: 0, pollTimer: null, results: [], expanded: new Set(), status: {},
        shotMode: 'final',
    };

    function esc(v) {
        return String(v == null ? '' : v).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
    }
    function toast(msg, type) { if (window.showToast) window.showToast(esc(msg), type || 'info'); }
    function fmtMs(ms) { if (!ms) return '—'; const s = ms / 1000; return s < 60 ? s.toFixed(1) + 's' : Math.floor(s / 60) + 'm ' + Math.round(s % 60) + 's'; }
    function badge(text, cls) { return `<span class="badge ${cls || ''}">${esc(text)}</span>`; }

    async function api(path, opts) {
        opts = opts || {};
        const init = { method: opts.method || 'GET', headers: {} };
        if (opts.json !== undefined) { init.headers['Content-Type'] = 'application/json'; init.body = JSON.stringify(opts.json); }
        if (opts.form) init.body = opts.form;
        const res = await fetch(API + path, init);
        let data = {};
        try { data = await res.json(); } catch (e) { data = { status: 'error', message: `Unexpected response (${res.status})` }; }
        if (!res.ok || data.status !== 'success') throw new Error(data.message || `Request failed (${res.status})`);
        return data;
    }

    function busy(btn, on, label) {
        if (!btn) return;
        if (on) { btn.dataset.label = btn.innerHTML; btn.disabled = true; btn.innerHTML = `<i class="fas fa-spinner fa-spin"></i> ${esc(label || 'Working…')}`; }
        else { btn.disabled = false; if (btn.dataset.label) btn.innerHTML = btn.dataset.label; }
    }

    // ── status chips ───────────────────────────────────────────────────
    async function loadStatus() {
        try {
            const s = await api('/status');
            state.status = s;
            const chip = (ok, label, tip) => `<span class="badge ${ok ? 'badge-success' : 'badge-warning'}" title="${esc(tip)}"><i class="fas ${ok ? 'fa-check' : 'fa-triangle-exclamation'}"></i> ${esc(label)}</span>`;
            $('teStatusChips').innerHTML =
                chip(s.node_available, `Playwright MCP (${s.playwright_mcp})`, s.node_available ? 'Node.js/npx found' : 'Install Node.js 18+ to run Playwright MCP') +
                chip(s.jira_configured, 'Jira', s.jira_configured ? 'Configured via Configure Jira' : 'Not configured') +
                chip(s.zephyr_configured, 'Zephyr Scale', s.zephyr_configured ? 'API token configured' : 'Not configured') +
                chip(s.ai_configured, 'AI provider', s.ai_configured ? 'Used only for steps the parser cannot read' : 'Not configured: unparsed steps will be Blocked');
        } catch (e) { /* page still usable */ }
    }

    // ── source selection ───────────────────────────────────────────────
    document.querySelectorAll('#teSourceCard .te-option').forEach((b) => b.addEventListener('click', () => {
        state.source = b.dataset.source;
        document.querySelectorAll('#teSourceCard .te-option').forEach((o) => {
            o.classList.toggle('selected', o === b); o.setAttribute('aria-checked', o === b ? 'true' : 'false');
        });
        ['excel', 'jira', 'zephyr'].forEach((s) => $('tePanel-' + s).classList.toggle('active', s === state.source));
        document.querySelectorAll('.te-excel-only').forEach((el) => el.classList.toggle('te-hidden', state.source !== 'excel'));
        document.querySelectorAll('.te-remote-only').forEach((el) => el.classList.toggle('te-hidden', state.source === 'excel'));
        $('teZephyrCycleRow').classList.toggle('te-hidden', !state.status.zephyr_configured || state.source === 'excel');
    }));

    // ── Excel ──────────────────────────────────────────────────────────
    $('teFile').addEventListener('change', async (ev) => {
        const f = ev.target.files[0];
        if (!f) return;
        const form = new FormData();
        form.append('file', f);
        const t = window.showToast ? window.showToast('Reading workbook…', 'loading', 0) : null;
        try {
            const d = await api('/upload', { method: 'POST', form });
            state.upload = d;
            $('teSheet').innerHTML = d.sheets.map((s, i) => `<option value="${i}">${esc(s.name)} (${s.row_count} rows)</option>`).join('');
            const best = d.sheets.findIndex((s) => s.suggested_mapping.steps !== null);
            $('teSheet').value = String(best >= 0 ? best : 0);
            renderMapping();
            $('teSheetBlock').classList.remove('te-hidden');
            toast('Workbook loaded. Check the column mapping, then preview.', 'success');
        } catch (e) { toast(e.message, 'error'); ev.target.value = ''; }
        finally { if (t) t.remove(); }
    });
    $('teSheet').addEventListener('change', renderMapping);

    function renderMapping() {
        const sheet = state.upload.sheets[Number($('teSheet').value)];
        const opts = (sel) => `<option value="">— not mapped —</option>` + sheet.headers.map((h, i) =>
            h ? `<option value="${i}" ${sel === i ? 'selected' : ''}>${esc(h)}</option>` : '').join('');
        $('teMapping').innerHTML = state.upload.fields.map((f) => `
            <div class="form-group" style="margin:0;">
                <label for="teMap-${f}">${esc(FIELD_LABELS[f] || f)}</label>
                <select id="teMap-${f}" data-field="${f}">${opts(sheet.suggested_mapping[f])}</select>
            </div>`).join('');
    }

    function currentMapping() {
        const m = {};
        document.querySelectorAll('#teMapping select').forEach((s) => { m[s.dataset.field] = s.value === '' ? null : Number(s.value); });
        return m;
    }

    $('tePreviewExcel').addEventListener('click', async (ev) => {
        const btn = ev.currentTarget;
        busy(btn, true, 'Parsing…');
        try {
            const sheet = state.upload.sheets[Number($('teSheet').value)];
            const d = await api('/excel/preview', { method: 'POST', json: {
                upload_id: state.upload.upload_id, sheet: sheet.name, mapping: currentMapping(), project_id: $('teProject').value } });
            state.meta = { upload_id: state.upload.upload_id, sheet: sheet.name, mapping: currentMapping() };
            showPreview(d, []);
        } catch (e) { toast(e.message, 'error'); }
        finally { busy(btn, false); }
    });

    // ── Jira ───────────────────────────────────────────────────────────
    $('teJiraSearch').addEventListener('click', async (ev) => {
        const q = $('teJiraQuery').value.trim();
        if (!q) { toast('Enter an issue key or JQL.', 'warning'); return; }
        const btn = ev.currentTarget;
        busy(btn, true, 'Searching…');
        try {
            const d = await api('/jira/search?q=' + encodeURIComponent(q));
            const list = $('teJiraResults');
            list.classList.remove('te-hidden');
            list.innerHTML = d.issues.length ? d.issues.map((i) => `
                <button type="button" data-key="${esc(i.key)}"><span class="te-key">${esc(i.key)}</span>
                <span style="flex:1;">${esc(i.summary)}</span>${badge(i.type)} ${badge(i.status)}</button>`).join('')
                : '<div class="te-hint" style="padding:10px;">No issues found.</div>';
            list.querySelectorAll('button').forEach((b) => b.addEventListener('click', () => loadRemote('jira', b.dataset.key, b)));
        } catch (e) { toast(e.message, 'error'); }
        finally { busy(btn, false); }
    });
    $('teJiraQuery').addEventListener('keydown', (e) => { if (e.key === 'Enter') $('teJiraSearch').click(); });

    // ── Zephyr ─────────────────────────────────────────────────────────
    $('teZephyrLoad').addEventListener('click', (ev) => {
        const ref = $('teZephyrRef').value.trim();
        if (!ref) { toast('Enter a Zephyr test case key, issue key or project key.', 'warning'); return; }
        loadRemote('zephyr', ref, ev.currentTarget);
    });
    $('teZephyrConfigBtn').addEventListener('click', async () => {
        try { const d = await api('/zephyr/config'); $('teZephyrBase').value = d.base_url || ''; } catch (e) { /* ignore */ }
        $('teZephyrToken').value = '';
        $('teZephyrModal').classList.add('active');
    });
    $('teZephyrCancel').addEventListener('click', () => $('teZephyrModal').classList.remove('active'));
    $('teZephyrSave').addEventListener('click', async () => {
        try {
            await api('/zephyr/config', { method: 'POST', json: { api_token: $('teZephyrToken').value, base_url: $('teZephyrBase').value } });
            $('teZephyrModal').classList.remove('active');
            toast('Zephyr configuration saved.', 'success');
            loadStatus();
        } catch (e) { toast(e.message, 'error'); }
    });

    async function loadRemote(source, ref, btn) {
        busy(btn, true, 'Loading…');
        try {
            const d = await api('/remote/preview', { method: 'POST', json: { source, ref, project_id: $('teProject').value } });
            state.ref = ref;
            showPreview(d, d.notes || []);
            loadCycles(ref);
        } catch (e) { toast(e.message, 'error'); }
        finally { busy(btn, false); }
    }

    async function loadCycles(ref) {
        if (!state.status.zephyr_configured) return;
        const project = (ref.match(/^([A-Z][A-Z0-9_]+)/i) || [])[1];
        if (!project) return;
        try {
            const d = await api('/zephyr/cycles?project=' + encodeURIComponent(project.toUpperCase()));
            $('teZephyrCycle').innerHTML = '<option value="">— not publishing to Zephyr —</option>' +
                d.cycles.map((c) => `<option value="${esc(c.key)}">${esc(c.key)} · ${esc(c.name)}</option>`).join('');
        } catch (e) { /* cycles are optional */ }
    }

    // ── preview ────────────────────────────────────────────────────────
    function showPreview(d, notes) {
        state.previewCases = d.cases;
        state.selected = new Set(d.cases.filter((c) => c.valid).map((c) => c.key));
        const s = d.stats;
        $('tePreviewStats').innerHTML = [
            ['Test cases', s.total, ''], ['Valid', s.valid, 'ok'], ['Invalid', s.invalid, s.invalid ? 'bad' : ''],
            ['Steps', s.steps, ''], ['Parsed (no AI)', s.steps_deterministic, 'ok'], ['Need AI', s.steps_needing_ai, s.steps_needing_ai ? 'warn' : ''],
        ].map(([l, v, c]) => `<div class="te-stat ${c}"><div class="v">${esc(v)}</div><div class="l">${esc(l)}</div></div>`).join('');
        $('tePreviewNotes').innerHTML = notes.map((n) => `<div class="te-warn">${esc(n)}</div>`).join('');
        renderPreviewTable();
        $('tePreviewCard').classList.remove('te-hidden');
        $('teConfigCard').classList.remove('te-hidden');
        updateEstimate();
        $('tePreviewCard').scrollIntoView({ behavior: 'smooth', block: 'start' });
    }

    function renderPreviewTable() {
        $('tePreviewBody').innerHTML = state.previewCases.map((c, idx) => {
            const ai = c.steps.filter((s) => s.plan_source === 'unresolved').length;
            const interp = c.steps.length ? (ai ? badge(`${ai} need AI`, 'badge-warning') + ' ' : '') +
                badge(`${c.steps.length - ai} parser`, 'badge-success') : '—';
            const valid = c.valid ? badge('Valid', 'badge-success') : c.issues.map((i) => badge(i, 'badge-error')).join(' ');
            const steps = c.steps.map((s) => `<tr><td>${s.number}</td><td>${esc(s.action_text)}</td><td>${esc(s.expected)}</td>
                <td>${s.plan_source === 'unresolved' ? badge('needs AI', 'badge-warning') : badge(s.plan_source || '—', 'badge-success')}
                ${s.actions && s.actions.length ? '<br><small class="te-hint">' + esc(s.actions.map((a) => a.type).join(', ')) + '</small>' : ''}</td></tr>`).join('');
            return `<tr>
                <td><input type="checkbox" data-idx="${idx}" ${state.selected.has(c.key) ? 'checked' : ''} ${c.valid ? '' : 'disabled'} aria-label="Select ${esc(c.key)}"></td>
                <td><strong>${esc(c.key)}</strong></td>
                <td>${esc(c.title)}<details style="margin-top:4px;"><summary class="te-hint" style="cursor:pointer;">${c.steps.length} step(s)</summary>
                    <table class="te-steps"><thead><tr><th>#</th><th>Action</th><th>Expected</th><th>Plan</th></tr></thead><tbody>${steps}</tbody></table></details></td>
                <td>${c.steps.length}</td><td>${interp}</td><td>${valid}</td></tr>`;
        }).join('') || '<tr><td colspan="6" class="te-hint">No test cases found.</td></tr>';
        $('tePreviewBody').querySelectorAll('input[type=checkbox]').forEach((cb) => cb.addEventListener('change', () => {
            const key = state.previewCases[Number(cb.dataset.idx)].key;
            if (cb.checked) state.selected.add(key); else state.selected.delete(key);
            updateEstimate();
        }));
    }
    $('teSelectAll').addEventListener('click', () => { state.selected = new Set(state.previewCases.filter((c) => c.valid).map((c) => c.key)); renderPreviewTable(); updateEstimate(); });
    $('teSelectNone').addEventListener('click', () => { state.selected = new Set(); renderPreviewTable(); updateEstimate(); });

    // ── configuration ──────────────────────────────────────────────────
    document.querySelectorAll('#teShotModes .te-option').forEach((b) => b.addEventListener('click', () => {
        state.shotMode = b.dataset.mode;
        document.querySelectorAll('#teShotModes .te-option').forEach((o) => { o.classList.toggle('selected', o === b); o.setAttribute('aria-checked', o === b ? 'true' : 'false'); });
        updateEstimate();
    }));

    function updateEstimate() {
        const chosen = state.previewCases.filter((c) => state.selected.has(c.key));
        const steps = chosen.reduce((n, c) => n + c.steps.length, 0);
        const n = { none: 0, every_step: steps, final: chosen.length, failure: null }[state.shotMode];
        const text = n === null ? `Up to one screenshot per failing or blocked step (at most ${steps}).`
            : n === 0 ? 'No screenshots will be captured.'
            : `About ${n} screenshot(s), roughly ${Math.max(1, Math.round(n * 0.12))} MB of evidence.`;
        $('teShotEstimate').textContent = text + (state.shotMode === 'every_step' && steps > 200 ? ' This is a lot of evidence — consider "Final result only".' : '');
        $('teSelectedCount').textContent = `${chosen.length} test case(s) selected`;
    }

    async function loadProject() {
        const pid = $('teProject').value;
        if (!pid) { $('teCredHint').textContent = 'No project selected: enter a base URL. Steps that need a username/password will be Blocked.'; return; }
        try {
            const d = await api('/project/' + encodeURIComponent(pid));
            if (!$('teBaseUrl').value || $('teBaseUrl').dataset.fromProject === '1') { $('teBaseUrl').value = d.base_url || ''; $('teBaseUrl').dataset.fromProject = '1'; }
            $('teCredHint').textContent = `Project credentials: username ${d.has_username ? 'configured' : 'missing'}, password ${d.has_password ? 'configured' : 'missing'}.`;
        } catch (e) { $('teCredHint').textContent = e.message; }
    }
    $('teProject').addEventListener('change', loadProject);
    $('teBaseUrl').addEventListener('input', () => { $('teBaseUrl').dataset.fromProject = '0'; });

    function runConfig() {
        const num = (id) => Number($(id).value) || undefined;
        return {
            screenshot_mode: state.shotMode, embed_screenshots: $('teEmbed').checked, publish_results: $('tePublish').checked,
            isolate_tests: $('teIsolate').checked, use_ai_planner: $('teUseAi').checked, use_ai_locator_recovery: $('teUseAiLocator').checked,
            headless: $('teHeadless').checked, concurrency: num('teConcurrency'), action_timeout_ms: num('teActionTimeout'),
            test_timeout_s: num('teTestTimeout'), max_infra_retries: Number($('teRetries').value),
            mask_selectors: $('teMask').value.split(',').map((s) => s.trim()).filter(Boolean),
            allowed_hosts: $('teAllowedHosts').value.split(',').map((s) => s.trim()).filter(Boolean),
            zephyr_test_cycle: $('teZephyrCycle').value, confirm_production: $('teConfirmProd').checked,
        };
    }

    $('teStart').addEventListener('click', async (ev) => {
        if (!state.selected.size) { toast('Select at least one valid test case.', 'warning'); return; }
        const btn = ev.currentTarget;
        busy(btn, true, 'Starting…');
        const body = { source: state.source, selected_keys: [...state.selected], config: runConfig(),
            project_id: $('teProject').value, base_url: $('teBaseUrl').value.trim() };
        if (state.source === 'excel') Object.assign(body, state.meta); else body.ref = state.ref;
        try {
            const d = await api('/runs', { method: 'POST', json: body });
            $('teProdWarn').classList.add('te-hidden');
            openRun(d.run_id);
            toast('Execution started.', 'success');
        } catch (e) {
            if (/production/i.test(e.message)) $('teProdWarn').classList.remove('te-hidden');
            toast(e.message, 'error');
        } finally { busy(btn, false); }
    });

    // ── run view ───────────────────────────────────────────────────────
    function openRun(runId) {
        state.runId = runId; state.since = 0; state.expanded = new Set();
        $('teLog').innerHTML = '';
        $('teRunCard').classList.remove('te-hidden');
        $('teRunId').textContent = runId;
        $('teExportCsv').href = `${API}/runs/${encodeURIComponent(runId)}/export?format=csv`;
        $('teExportJson').href = `${API}/runs/${encodeURIComponent(runId)}/export?format=json`;
        $('teDownloadExcel').href = `${API}/runs/${encodeURIComponent(runId)}/excel`;
        $('teRunCard').scrollIntoView({ behavior: 'smooth', block: 'start' });
        poll();
    }

    async function poll() {
        clearTimeout(state.pollTimer);
        if (!state.runId) return;
        let d;
        try { d = await api(`/runs/${encodeURIComponent(state.runId)}?since=${state.since}`); }
        catch (e) { toast(e.message, 'error'); return; }
        renderRun(d);
        if (d.active) state.pollTimer = setTimeout(poll, 1500);
        else loadRuns();
    }

    const PHASE_LABEL = { queued: 'Queued', planning: 'Interpreting steps', running: 'Executing', publishing: 'Publishing results',
        completed: 'Completed', cancelled: 'Cancelled', failed: 'Failed', interrupted: 'Interrupted' };

    function renderRun(d) {
        const run = d.run, p = d.progress;
        $('teRunStatus').textContent = PHASE_LABEL[run.status] || run.status;
        $('teRunStatus').className = 'badge ' + ({ completed: 'badge-success', failed: 'badge-error', cancelled: 'badge-warning', interrupted: 'badge-warning' }[run.status] || 'badge-info');
        $('teProgressBlock').classList.toggle('te-hidden', !d.active && !d.events.length && !$('teLog').childElementCount);
        $('teCancel').classList.toggle('te-hidden', !d.active);
        if (p) {
            const pct = p.total ? Math.round(100 * p.done / p.total) : 0;
            $('teProgressBar').style.width = (d.active ? pct : 100) + '%';
            $('teProgressText').textContent = d.active ? `${PHASE_LABEL[p.phase] || p.phase}: ${p.done}/${p.total}${p.current ? ' — ' + p.current : ''}` : PHASE_LABEL[run.status];
        } else if (!d.active) { $('teProgressBar').style.width = '100%'; $('teProgressText').textContent = PHASE_LABEL[run.status] || run.status; }
        d.events.forEach((e) => {
            state.since = Math.max(state.since, e.seq);
            const line = document.createElement('div');
            line.textContent = `${e.ts.slice(11, 19)}  ${e.type}${e.test_key ? ' ' + e.test_key : ''}${e.step ? ' step ' + e.step : ''}${e.status ? ' → ' + e.status : ''}${e.phase ? ' ' + e.phase : ''}${e.message ? ' ' + e.message : ''}${e.reason ? ' ' + e.reason : ''}`;
            $('teLog').appendChild(line);
        });
        $('teLog').scrollTop = $('teLog').scrollHeight;

        const s = d.summary || {};
        const pass = s.pass_percentage == null ? '—' : s.pass_percentage + '%';
        $('teSummary').innerHTML = [
            ['Total', s.total, ''], ['Passed', s.passed, 'ok'], ['Failed', s.failed, s.failed ? 'bad' : ''],
            ['Blocked', s.blocked, s.blocked ? 'warn' : ''], ['Not Executed', s.not_executed, ''],
            ...(s.in_progress ? [['In Progress', s.in_progress, '']] : []),
            ['Pass rate', pass, ''], ['Duration', fmtMs(s.total_duration_ms), ''], ['Screenshots', s.screenshot_count, ''],
            ['Published', s.published, 'ok'], ['Publish pending', s.publication_pending, ''], ['Publish failed', s.publication_failed, s.publication_failed ? 'bad' : ''],
        ].map(([l, v, c]) => `<div class="te-stat ${c}"><div class="v">${esc(v == null ? 0 : v)}</div><div class="l">${esc(l)}</div></div>`).join('');
        $('tePassFormula').textContent = 'Pass rate = Passed ÷ (Passed + Failed). Blocked and Not Executed tests are excluded because they say nothing about the application.';

        const u = d.usage || {};
        $('teUsage').innerHTML = [
            ['Steps parsed (no AI)', u.steps_parsed_deterministically], ['AI-planned steps', u.steps_planned_by_ai],
            ['Plan cache hits', u.plan_cache_hits], ['Model calls', u.model_calls],
            ['Est. tokens in / out', `${u.est_input_tokens || 0} / ${u.est_output_tokens || 0}`],
            ['Browser operations', u.browser_operations], ['Infra retries', u.retries],
            ['Evidence size', ((u.evidence_bytes || 0) / 1048576).toFixed(1) + ' MB'],
        ].map(([l, v]) => `<div class="te-stat"><div class="v" style="font-size:1.1rem;">${esc(v == null ? 0 : v)}</div><div class="l">${esc(l)}</div></div>`).join('');
        const notes = [...((d.summary || {}).excel_notes || [])];
        if ((d.summary || {}).excel_error) notes.push('Excel output failed: ' + d.summary.excel_error);
        notes.push('Token counts are estimates (tiktoken) — the AI providers\' exact usage is not exposed through the app\'s AI client. No cost is shown for that reason.');
        $('teNotes').innerHTML = notes.map((n) => `<p class="te-hint">${esc(n)}</p>`).join('');

        $('teDownloadExcel').classList.toggle('te-hidden', !d.has_excel);
        $('teRetryFailed').disabled = d.active || !d.results.some((r) => ['Failed', 'Blocked', 'Not Executed'].includes(r.status));
        $('teRetryPub').disabled = d.active || !d.results.some((r) => Object.values(r.publication || {}).some((x) => ['Failed', 'Pending'].includes(x.status)));
        state.results = d.results;
        renderResults();
    }

    function renderResults() {
        const q = $('teSearch').value.trim().toLowerCase(), fs = $('teFilterStatus').value, fsrc = $('teFilterSource').value;
        const rows = state.results.filter((r) => (!fs || r.status === fs) && (!fsrc || r.source === fsrc) &&
            (!q || (r.test_key || '').toLowerCase().includes(q) || (r.title || '').toLowerCase().includes(q)));
        const evUrl = (ev) => `${API}/runs/${encodeURIComponent(state.runId)}/evidence/${encodeURIComponent(ev.evidence_id)}`;
        $('teResultsBody').innerHTML = rows.map((r) => {
            const pubs = Object.entries(r.publication || {}).map(([k, v]) => `<span title="${esc(v.message || '')}">${badge(k + ': ' + v.status, PUB_BADGE[v.status])}</span>`).join(' ') || '—';
            const open = state.expanded.has(r.test_key);
            const plans = Object.fromEntries((r.plan || []).map((p) => [p.number, p]));
            const steps = (r.steps || []).map((s) => {
                const shots = (r.evidence || []).filter((e) => (s.evidence_ids || []).includes(e.evidence_id));
                const plan = plans[s.number] || {};
                return `<tr><td>${s.number}</td><td>${esc(s.action_text)}</td><td>${esc(s.expected)}</td>
                    <td>${badge(s.status, STATUS_BADGE[s.status])}${s.failure_category ? '<br><small class="te-hint">' + esc(s.failure_category) + '</small>' : ''}</td>
                    <td>${esc(s.actual || s.error)}${plan.plan_source ? '<br><small class="te-hint">plan: ' + esc(plan.plan_source) + '</small>' : ''}</td>
                    <td>${fmtMs(s.duration_ms)}</td>
                    <td>${shots.map((e) => `<img src="${evUrl(e)}" alt="${esc(e.label)}" data-full="${evUrl(e)}" loading="lazy" style="width:110px;border-radius:4px;cursor:zoom-in;">`).join('')}</td></tr>`;
            }).join('');
            const finals = (r.evidence || []).filter((e) => e.step_number == null);
            const detail = open ? `<tr class="te-detail"><td colspan="10">
                ${r.failure_reason ? `<p><strong>Failure:</strong> ${esc(r.failure_reason)}</p>` : ''}
                <p class="te-hint">Expected: ${esc(r.expected || '—')} · Actual: ${esc(r.actual || '—')} · Started ${esc(r.started_at || '—')} · Retries ${esc(r.retry_count || 0)}</p>
                <table class="te-steps"><thead><tr><th>#</th><th>Action</th><th>Expected</th><th>Status</th><th>Actual / Error</th><th>Time</th><th>Evidence</th></tr></thead><tbody>${steps}</tbody></table>
                ${finals.length ? `<div class="te-thumbs">${finals.map((e) => `<img src="${evUrl(e)}" alt="${esc(e.label)}" data-full="${evUrl(e)}" loading="lazy">`).join('')}</div>` : ''}
                ${Object.entries(r.publication || {}).map(([k, v]) => `<p class="te-hint">${esc(k)}: ${esc(v.status)} ${esc(v.ref || '')} — ${esc(v.message || '')}</p>`).join('')}
            </td></tr>` : '';
            return `<tr>
                <td><strong>${esc(r.test_key)}</strong></td><td>${esc(r.title)}</td><td>${esc(r.source)}</td>
                <td>${badge(r.status, STATUS_BADGE[r.status])}</td><td>${fmtMs(r.duration_ms)}</td>
                <td>${esc(r.failed_step || '—')}</td><td>${esc(r.failure_reason || '—')}</td>
                <td>${(r.evidence || []).length ? `<a href="#" data-toggle="${esc(r.test_key)}">${(r.evidence || []).length} <i class="fas fa-image"></i></a>` : '—'}</td>
                <td>${pubs}</td>
                <td><button type="button" class="btn btn-ghost btn-sm" data-toggle="${esc(r.test_key)}" aria-expanded="${open}">${open ? 'Hide' : 'Steps'}</button></td>
            </tr>${detail}`;
        }).join('') || '<tr><td colspan="10" class="te-hint">No results match the filters.</td></tr>';
        $('teResultsBody').querySelectorAll('[data-toggle]').forEach((b) => b.addEventListener('click', (e) => {
            e.preventDefault();
            const k = b.dataset.toggle;
            if (state.expanded.has(k)) state.expanded.delete(k); else state.expanded.add(k);
            renderResults();
        }));
        $('teResultsBody').querySelectorAll('img[data-full]').forEach((img) => img.addEventListener('click', () => {
            $('teLightboxImg').src = img.dataset.full; $('teLightbox').classList.add('open');
        }));
    }
    ['teSearch', 'teFilterStatus', 'teFilterSource'].forEach((id) => $(id).addEventListener('input', renderResults));
    $('teLightbox').addEventListener('click', () => $('teLightbox').classList.remove('open'));
    document.addEventListener('keydown', (e) => { if (e.key === 'Escape') { $('teLightbox').classList.remove('open'); $('teZephyrModal').classList.remove('active'); } });

    $('teCancel').addEventListener('click', async () => {
        try { await api(`/runs/${encodeURIComponent(state.runId)}/cancel`, { method: 'POST', json: {} }); toast('Cancelling after the current step…', 'info'); }
        catch (e) { toast(e.message, 'error'); }
    });
    $('teRetryFailed').addEventListener('click', async () => {
        try { const d = await api(`/runs/${encodeURIComponent(state.runId)}/retry-failed`, { method: 'POST', json: {} }); toast(`Retrying ${d.count} test(s).`, 'info'); state.since = 0; $('teLog').innerHTML = ''; poll(); }
        catch (e) { toast(e.message, 'error'); }
    });
    $('teRetryPub').addEventListener('click', async () => {
        try { await api(`/runs/${encodeURIComponent(state.runId)}/retry-publications`, { method: 'POST', json: {} }); toast('Retrying publications.', 'info'); poll(); }
        catch (e) { toast(e.message, 'error'); }
    });

    // ── recent runs ────────────────────────────────────────────────────
    async function loadRuns() {
        try {
            const d = await api('/runs');
            $('teRunsBody').innerHTML = d.runs.map((r) => `<tr>
                <td>${esc(r.run_id)}</td><td>${esc(r.source)}</td><td>${esc(r.status)}</td>
                <td>${esc((r.summary || {}).passed ?? '—')}</td><td>${esc((r.summary || {}).failed ?? '—')}</td><td>${esc((r.summary || {}).blocked ?? '—')}</td>
                <td>${esc((r.created_at || '').replace('T', ' ').slice(0, 19))}</td>
                <td><button type="button" class="btn btn-ghost btn-sm" data-run="${esc(r.run_id)}">Open</button></td></tr>`).join('')
                || '<tr><td colspan="8" class="te-hint">No runs yet.</td></tr>';
            $('teRunsBody').querySelectorAll('[data-run]').forEach((b) => b.addEventListener('click', () => openRun(b.dataset.run)));
        } catch (e) { $('teRunsBody').innerHTML = `<tr><td colspan="8" class="te-hint">${esc(e.message)}</td></tr>`; }
    }

    loadStatus();
    loadProject();
    loadRuns();
})();
