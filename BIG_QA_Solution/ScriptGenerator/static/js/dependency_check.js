// Shared pre-flight dependency check. Callers pass their own stack inputs + target elements, so
// the new-project modal and the existing-project section reuse one implementation.
// opts: { tool, language, framework, versionProfile, requiredVersions, rowEl, listEl, onResult }

const _DEP_STYLE = {
    ok:       { icon: 'fa-check-circle',         color: '#2ecc71' },
    missing:  { icon: 'fa-times-circle',         color: '#e74c3c' },
    mismatch: { icon: 'fa-exclamation-triangle', color: '#f1c40f' }
};

function runDependencyCheck(opts) {
    const rowEl = opts.rowEl;
    const listEl = opts.listEl;
    if (!rowEl || !listEl) return;

    // Per-container token: a stale response must not overwrite a newer one. Kept on the element
    // so independent panels don't share a counter.
    const seq = (rowEl._depSeq = (rowEl._depSeq || 0) + 1);

    rowEl.style.display = '';
    listEl.innerHTML = '<span class="text-secondary" style="font-size:0.85rem;">'
        + '<i class="fas fa-circle-notch fa-spin"></i> Checking installed dependencies...</span>';

    fetch('/api/preflight-dependencies', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            tool: opts.tool,
            language: opts.language,
            framework: opts.framework,
            versionProfile: opts.versionProfile || '',
            requiredVersions: opts.requiredVersions || {}
        })
    })
    .then(r => r.json())
    .then(data => {
        if (seq !== rowEl._depSeq) return;
        const deps = data.dependencies || [];
        renderDependencyCheck(deps, rowEl, listEl);
        if (typeof opts.onResult === 'function') opts.onResult(deps);
    })
    .catch(() => {
        if (seq !== rowEl._depSeq) return;
        // Advisory check: on failure hide the panel rather than alarm the user.
        rowEl.style.display = 'none';
        if (typeof opts.onResult === 'function') opts.onResult([]);
    });
}

// Detect a project's stack + inferred versions from its path, then render the pre-check panel.
// Used by any page that selects an existing project (Script Runner, Test Case Generator, ...).
function checkProjectDependencies(path, rowEl, listEl) {
    if (!rowEl || !listEl) return;
    fetch('/api/detect-project', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path: path })
    })
    .then(r => r.json())
    .then(data => {
        if (data && data.language && data.tool) {
            runDependencyCheck({
                tool: data.tool,
                language: data.language,
                framework: data.framework,
                requiredVersions: data.required_versions || {},
                rowEl: rowEl,
                listEl: listEl
            });
        } else {
            rowEl.style.display = 'none';
        }
    })
    .catch(() => { rowEl.style.display = 'none'; });
}

function renderDependencyCheck(deps, rowEl, listEl) {
    if (!deps.length) { rowEl.style.display = 'none'; return; }

    listEl.innerHTML = '';

    // At-a-glance summary so a mismatch is noticeable without scanning every row.
    const unmet = deps.filter(d => d.status !== 'ok').length;
    const sum = unmet ? _DEP_STYLE.mismatch : _DEP_STYLE.ok;
    const summary = document.createElement('div');
    summary.style.cssText = 'display:flex; align-items:center; gap:8px; font-size:0.85rem; font-weight:600; margin-bottom:4px;';
    summary.innerHTML =
        '<i class="fas ' + sum.icon + '" style="color:' + sum.color + ';"></i>'
        + '<span>' + (unmet
            ? (unmet === 1 ? '1 prerequisite needs attention'
                           : unmet + ' prerequisites need attention')
            : 'All prerequisites met') + '</span>';
    listEl.appendChild(summary);

    deps.forEach(d => {
        const s = _DEP_STYLE[d.status] || _DEP_STYLE.missing;
        let detail;
        if (d.status === 'ok') {
            detail = d.detected ? ('v' + d.detected + ' detected') : 'Installed';
        } else if (d.status === 'mismatch') {
            detail = 'Needs ' + d.required + ' or newer, found v' + (d.detected || '?') + ' — ' + d.hint;
        } else {
            detail = 'Not found' + (d.required ? (' (needs ' + d.required + ')') : '') + ' — ' + d.hint;
        }
        const item = document.createElement('div');
        item.style.cssText = 'display:flex; align-items:flex-start; gap:8px; font-size:0.85rem;';
        item.innerHTML =
            '<i class="fas ' + s.icon + '" style="color:' + s.color + '; margin-top:2px;"></i>'
            + '<span><b>' + d.name + '</b>'
            + (d.required ? (' <span style="opacity:0.7;">(requires ' + d.required + '+)</span>') : '')
            + '<br><span style="opacity:0.8;">' + detail + '</span></span>';
        listEl.appendChild(item);
    });
}
