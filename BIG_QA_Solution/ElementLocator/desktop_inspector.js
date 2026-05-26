/* desktop_inspector.js — Robust Locator Engine v2 */
(function() {
    if (window.location.href.includes('dashboard.html')) {
        return; // Prevent hijacking the dashboard's QWebChannel
    }

    // Initialize QWebChannel for communication with Python backend
    if (typeof qt !== 'undefined' && typeof QWebChannel !== 'undefined') {
        new QWebChannel(qt.webChannelTransport, function (channel) {
            window.pybridge = channel.objects.pybridge;
            console.log("QWebChannel connected.");
        });
    } else {
        console.error("QWebChannel or qt is not defined. The bridge will not work.");
    }

    if (!window.desktopInspectorInitialized) {
        window.desktopInspectorInitialized = true;
    window.desktopInspectorActive = false;
    window.currentHighlightedElement = null;

    // ─── Highlight Box ────────────────────────────────────────────────────────
    let highlightBox = document.getElementById('pyqt-highlight-box');
    if (!highlightBox) {
        highlightBox = document.createElement('div');
        highlightBox.id = 'pyqt-highlight-box';
        highlightBox.style.setProperty('position', 'fixed', 'important');
        highlightBox.style.setProperty('border', '3px solid #ff0055', 'important');
        highlightBox.style.setProperty('background-color', 'rgba(255, 0, 85, 0.2)', 'important');
        highlightBox.style.setProperty('pointer-events', 'none', 'important');
        highlightBox.style.setProperty('z-index', '9999999', 'important');
        highlightBox.style.setProperty('display', 'none', 'important');
        highlightBox.style.setProperty('box-sizing', 'border-box', 'important');
        highlightBox.style.setProperty('margin', '0', 'important');
        (document.body || document.documentElement).appendChild(highlightBox);
    }

    // ─── Inspector Controls ───────────────────────────────────────────────────
    window.activateDesktopInspector = function () {
        if (!window.desktopInspectorActive) {
            document.addEventListener('mouseover', handleMouseOver, true);
            document.addEventListener('mouseout', handleMouseOut, true);
            window.addEventListener('scroll', handleScroll, true);
            const EVENTS = ['click', 'mousedown', 'mouseup'];
            EVENTS.forEach(evt => document.addEventListener(evt, handleMouseCapture, true));
            window.desktopInspectorActive = true;
        }
        if (window === window.top) {
            broadcastToAllFrames({ type: 'ACTIVATE_INSPECTOR' });
        }
    };

    window.deactivateDesktopInspector = function () {
        if (window.desktopInspectorActive) {
            document.removeEventListener('mouseover', handleMouseOver, true);
            document.removeEventListener('mouseout', handleMouseOut, true);
            window.removeEventListener('scroll', handleScroll, true);
            const EVENTS = ['click', 'mousedown', 'mouseup'];
            EVENTS.forEach(evt => document.removeEventListener(evt, handleMouseCapture, true));
            highlightBox.style.setProperty('display', 'none', 'important');
            window.desktopInspectorActive = false;
        }
        if (window === window.top) {
            broadcastToAllFrames({ type: 'DEACTIVATE_INSPECTOR' });
        }
    };

    // ─── Recursive frame broadcaster ─────────────────────────────────────────
    function broadcastToAllFrames(msg) {
        function broadcast(win) {
            try { win.postMessage(msg, '*'); } catch(e) {}
            for (let i = 0; i < win.frames.length; i++) {
                try { broadcast(win.frames[i]); } catch(e) {}
            }
        }
        // Broadcast to all descendants of top
        for (let i = 0; i < window.top.frames.length; i++) {
            try { broadcast(window.top.frames[i]); } catch(e) {}
        }
    }

    window.addEventListener('message', function(e) {
        if (e.data && e.data.type === 'ACTIVATE_INSPECTOR') window.activateDesktopInspector();
        if (e.data && e.data.type === 'DEACTIVATE_INSPECTOR') window.deactivateDesktopInspector();
        if (e.data && e.data.type === 'HIGHLIGHT_INSPECTOR') window.highlightElementByLocator(e.data.locatorType, e.data.locatorValue);
    });

    function handleMouseOver(e) {
        if (!window.desktopInspectorActive) return;
        const target = e.target;
        if (target === highlightBox) return;
        
        window.currentHighlightedElement = target;
        const rect = target.getBoundingClientRect();
        highlightBox.style.setProperty('display', 'block', 'important');
        highlightBox.style.setProperty('top', rect.top + 'px', 'important');
        highlightBox.style.setProperty('left', rect.left + 'px', 'important');
        highlightBox.style.setProperty('width', rect.width + 'px', 'important');
        highlightBox.style.setProperty('height', rect.height + 'px', 'important');
    }

    function handleMouseOut(e) {
        if (!window.desktopInspectorActive) return;
        if (!e.relatedTarget) {
            highlightBox.style.setProperty('display', 'none', 'important');
        }
    }

    function handleScroll(e) {
        if (!window.desktopInspectorActive || !window.currentHighlightedElement) return;
        const rect = window.currentHighlightedElement.getBoundingClientRect();
        highlightBox.style.setProperty('top', rect.top + 'px', 'important');
        highlightBox.style.setProperty('left', rect.left + 'px', 'important');
        highlightBox.style.setProperty('width', rect.width + 'px', 'important');
        highlightBox.style.setProperty('height', rect.height + 'px', 'important');
    }

    // ─── Unbreakable Bridge Queue ─────────────────────────────────────────────
    window.messageQueue = [];
    window.isSending = false;
    window._captureBuffer = window._captureBuffer || [];

    function sendPayload(payload) {
        window._captureBuffer.push(payload);
        window.messageQueue.push(payload);
        processQueue();
    }

    window._drainCaptureBuffer = function() {
        const batch = window._captureBuffer.slice();
        window._captureBuffer = [];
        return JSON.stringify(batch);
    };

    function processQueue() {
        if (window.isSending || window.messageQueue.length === 0) return;
        window.isSending = true;
        const payload = window.messageQueue.shift();
        if (window.pybridge && typeof window.pybridge.receive_payload === 'function') {
            try {
                window.pybridge.receive_payload(JSON.stringify(payload));
                setTimeout(() => { window.isSending = false; processQueue(); }, 50);
            } catch (e) {
                console.error("Bridge send failed:", e);
                window.isSending = false;
            }
        } else if (window !== window.top) {
            try { window.top.postMessage({ type: 'FORWARD_PAYLOAD', payload: payload }, '*'); } catch(e) {}
            setTimeout(() => { window.isSending = false; processQueue(); }, 50);
        } else {
            console.warn("Bridge not ready, retrying in 500ms...");
            window.messageQueue.unshift(payload);
            window.isSending = false;
            setTimeout(processQueue, 500);
        }
    }

    function resolveInteractiveTarget(el) {
        if (!el) return el;
        const tag = el.tagName.toLowerCase();
        const INTERACTIVE = ['input', 'textarea', 'select', 'button', 'a'];
        if (INTERACTIVE.includes(tag)) return el;
        const role = el.getAttribute('role');
        if (role && ['combobox', 'textbox', 'checkbox', 'button', 'link'].includes(role.toLowerCase())) return el;
        
        if (tag === 'label') {
            const forAttr = el.getAttribute('for') || el.htmlFor;
            if (forAttr) {
                const target = document.getElementById(forAttr);
                if (target) return target;
            }
            const nested = el.querySelector('input, textarea, select, button');
            if (nested) return nested;
        }
        let parent = el.parentElement;
        let depth = 0;
        while (parent && depth < 5) {
            const ptag = parent.tagName.toLowerCase();
            if (INTERACTIVE.includes(ptag)) return parent;
            const prole = parent.getAttribute('role');
            if (prole && ['combobox', 'textbox', 'checkbox', 'button', 'link'].includes(prole.toLowerCase())) return parent;
            parent = parent.parentElement;
            depth++;
        }
        return el;
    }

    let _lastCaptureTime = 0;

    const isDynamic = (v) => {
        if (!v) return true;
        // UUIDs / GUIDs
        if (/^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/i.test(v)) return true;
        // Long pure numbers
        if (/^\\d{5,}$/.test(v)) return true;
        // Framework-generated prefixes: ember, ng, __xmlid, vf-, pega-ui-, sf-, Appian acnid/__ap_
        if (/^(ember|ng-|__|vf-|pega-ui-|sf-|lwc-|ap-node-|__ap_|acnid-)/.test(v)) return true;
        // Salesforce dynamic record IDs (15/18-char alphanumeric starting with 3 uppercase)
        if (/^[A-Z0-9]{15,18}$/.test(v)) return true;
        // Trailing 4+ digit suffixes on otherwise stable names
        if (/[-_]\\d{4,}$/.test(v)) return true;
        // Appian internal component node IDs (long numeric strings)
        if (/^\\d{6,}$/.test(v)) return true;
        return false;
    };

    function showCaptureFeedback(msg, color) {
        const t = document.createElement('div');
        t.innerText = msg;
        t.style.cssText = `position:fixed;top:20px;left:50%;transform:translateX(-50%);background:${color};color:white;padding:10px 20px;border-radius:8px;z-index:999999;font-weight:bold;font-family:sans-serif;box-shadow:0 4px 12px rgba(0,0,0,0.3);`;
        document.body.appendChild(t);
        setTimeout(() => t.remove(), 2500);
    }

    function handleMouseCapture(e) {
        if (!window.desktopInspectorActive) return;
        e.preventDefault();
        e.stopPropagation();
        e.stopImmediatePropagation && e.stopImmediatePropagation();

        const now = Date.now();
        if (now - _lastCaptureTime < 1500) return;
        _lastCaptureTime = now;

        const raw = window.currentHighlightedElement || e.target;
        const el = resolveInteractiveTarget(raw);
        if (!el) return;

        showCaptureFeedback('[+] Captured: ' + (el.id || el.name || el.tagName), '#10b981');
        
        try {
            const locators = generateLocators(el);
            const tag = el.tagName.toLowerCase();
            const id = el.getAttribute('id');
            const nameAttr = el.getAttribute('name');
            const testId = el.getAttribute('data-testid') || el.getAttribute('data-test');
            const ph = el.getAttribute('placeholder');
            const text = (el.textContent || '').trim().substring(0, 30);
            
            let rawName = testId || id || nameAttr || ph || text;
            if (!rawName || /^\d+$/.test(rawName)) rawName = tag;

            let cleanName = rawName.replace(/[^a-zA-Z0-9 ]/g, ' ').split(' ').filter(w => w.length > 0).map((w, i) => i === 0 ? w.toLowerCase() : w.charAt(0).toUpperCase() + w.slice(1).toLowerCase()).join('').substring(0, 40);
            let suffix = "";
            if (tag === 'button' || el.getAttribute('type') === 'submit' || el.classList.contains('btn')) suffix = "Btn";
            else if (tag === 'input' || tag === 'textarea') suffix = "Input";
            else if (tag === 'select') suffix = "Dropdown";
            else if (tag === 'a') suffix = "Link";
            else if (tag === 'img') suffix = "Img";
            if (suffix && !cleanName.toLowerCase().endsWith(suffix.toLowerCase())) cleanName += suffix;

            const nameHint = cleanName || "element";
            const outerHtml = (el.outerHTML || '').substring(0, 5000);
            const pTitle = document.title || 'MyPage';
            const hasUnique = locators.some(l => l.count === 1);
            const inIframe = window !== window.top;
            const frameName = window.name || '';
            const cid = Date.now() + '-' + Math.floor(Math.random() * 1000000);
            
            const payload = locators.map(l => ({ ...l, nameHint, outerHtml, pageTitle: pTitle, needsAI: !hasUnique, inIframe, frameName, cid }));

            console.log("[Inspector] Locators:", payload.length, "| needsAI:", !hasUnique, "| iframe:", inIframe);
            try { sendPayload(payload); } catch(err){}
        } catch (fatalError) {
            console.error("[Inspector] FATAL ERROR:", fatalError);
            window._captureBuffer = window._captureBuffer || [];
            window._captureBuffer.push([{ type: 'JS_ERROR', value: String(fatalError.stack || fatalError.message || fatalError), count: 1, nameHint: 'errorDbg', outerHtml: '<error>', pageTitle: 'Error', cid: 'ERROR-' + Date.now() }]);
        }
    }

    // =========================================================================
    // ROBUST LOCATOR ENGINE
    // =========================================================================

    /**
     * Safely escape a string for use in an XPath predicate.
     * Handles strings with single quotes, double quotes, or both.
     */
    function escapeXPath(str) {
        if (!str) return "''";
        if (!str.includes("'")) return `'${str}'`;
        if (!str.includes('"')) return `"${str}"`;
        // Mixed quotes — use XPath concat()
        const parts = str.split("'").map(p => `'${p}'`).join(", \"'\", ");
        return `concat(${parts})`;
    }

    /**
     * Safely count DOM nodes matching an XPath expression.
     * Returns 0 on any error.
     */
    function xpathCount(expr, rootNode) {
        const root = rootNode || document;
        try {
            const r = document.evaluate(expr, root, null, XPathResult.ORDERED_NODE_SNAPSHOT_TYPE, null);
            return r.snapshotLength;
        } catch (e) { return 0; }
    }

    /**
     * Safely count DOM nodes matching a CSS selector.
     * Returns 0 on any error.
     */
    function cssCount(sel, rootNode) {
        const root = rootNode || document;
        try { return root.querySelectorAll(sel).length; }
        catch (e) { return 0; }
    }

    /**
     * Build sibling-position-indexed XPath anchored to the nearest stable ancestor.
     * e.g.  //ul[@id='nav']/li[2]/a
     */
    function buildSiblingIndexedXPath(element) {
        const segments = [];
        let el = element;

        while (el && el !== document.body && el.nodeType === 1) {
            const tag = el.tagName.toLowerCase();
            const id  = el.getAttribute('id');
            const tid = el.getAttribute('data-testid');

            if (id && !/^\d/.test(id)) {
                segments.unshift(`//${tag}[@id=${escapeXPath(id)}]`);
                break;
            }
            if (tid) {
                segments.unshift(`//${tag}[@data-testid=${escapeXPath(tid)}]`);
                break;
            }

            // Count same-tag siblings before this element
            let idx = 1;
            let sib = el.previousSibling;
            while (sib) {
                if (sib.nodeType === 1 && sib.tagName === el.tagName) idx++;
                sib = sib.previousSibling;
            }
            segments.unshift(idx > 1 ? `${tag}[${idx}]` : tag);
            el = el.parentNode;
        }

        if (!segments.length) return null;
        // Join: first segment is already // prefixed; rest are relative
        const first = segments[0];
        const rest  = segments.slice(1).join('/');
        return rest ? `${first}/${rest}` : first;
    }

    /**
     * Build a relative XPath from the nearest stable ancestor that has
     * an id, data-testid, role, or name attribute.
     * e.g.  //form[@id='login']//button[normalize-space()='Submit']
     */
    function buildAncestorRelativeXPath(element) {
        const tag    = element.tagName.toLowerCase();
        const elText = element.textContent.trim();

        // Walk up to find a stable anchor
        let ancestor = element.parentNode;
        while (ancestor && ancestor !== document.body && ancestor.nodeType === 1) {
            const aTag  = ancestor.tagName.toLowerCase();
            const aId   = ancestor.getAttribute('id');
            const aTid  = ancestor.getAttribute('data-testid');
            const aName = ancestor.getAttribute('name');
            const aRole = ancestor.getAttribute('role');

            let anchorXPath = null;
            if (aId  && !/^\d/.test(aId))  anchorXPath = `//${aTag}[@id=${escapeXPath(aId)}]`;
            else if (aTid)                  anchorXPath = `//${aTag}[@data-testid=${escapeXPath(aTid)}]`;
            else if (aName)                 anchorXPath = `//${aTag}[@name=${escapeXPath(aName)}]`;
            else if (aRole)                 anchorXPath = `//${aTag}[@role=${escapeXPath(aRole)}]`;

            if (anchorXPath) {
                // Build target predicate
                let targetXPath;
                if (elText && elText.length < 50) {
                    targetXPath = `${anchorXPath}//${tag}[normalize-space()=${escapeXPath(elText)}]`;
                    if (xpathCount(targetXPath) === 1) return targetXPath;
                }
                // Fallback: just tag under anchor
                targetXPath = `${anchorXPath}//${tag}`;
                if (xpathCount(targetXPath) === 1) return targetXPath;
            }
            ancestor = ancestor.parentNode;
        }
        return null;
    }

    /**
     * Main XPath strategy engine — returns array of XPath candidates
     * sorted by uniqueness (count=1 first), worst-case last.
     */
    function buildSmartXPaths(el) {
        const tag  = el.tagName.toLowerCase();
        const text = el.textContent.trim();
        const candidates = [];

        // Helper: add candidate if non-empty — also validates XPath syntax
        const addXP = (expr, priority) => {
            if (!expr || !expr.trim()) return;
            // XPath syntax validation: reject expressions that throw parse errors
            try {
                document.evaluate(expr, document, null, XPathResult.ORDERED_NODE_SNAPSHOT_TYPE, null);
            } catch(syntaxErr) {
                // Invalid XPath — skip silently
                console.warn('[Inspector] Invalid XPath discarded:', expr, syntaxErr.message);
                return;
            }
            const count = xpathCount(expr);
            if (count > 0) {
                candidates.push({ type: 'XPath', value: expr, count, priority });
            }
        };

        // ── Strategy 1: Stable own attributes ───────────────────────────────
        // Enterprise-aware: includes Pega (pyid, data-automation-id),
        // Salesforce LWC (data-key, data-value), Creatio (data-cid, data-item-id),
        // Appian (data-tempo-name, data-ap-comp, data-ap-field, ap-field-name)
        const stableAttrs = [
            'data-testid', 'data-test', 'data-cy', 'data-automation-id',
            // Pega specifics
            'data-ctl-name', 'data-reference', 'pyid',
            // Salesforce / LWC specifics
            'data-key', 'data-value', 'data-record-id', 'data-component-id',
            // Creatio specifics
            'data-cid', 'data-item-id', 'data-model-item-name',
            // Appian specifics (Tempo runtime + Appian 23.x+)
            'data-tempo-name', 'data-ap-comp', 'data-ap-field', 'ap-field-name',
            'data-ap-name', 'data-ap-id',
            // Standard
            'id', 'name', 'placeholder', 'aria-label', 'title', 'role', 'alt', 'for'
        ];



        for (const attr of stableAttrs) {
            const v = el.getAttribute(attr);
            if (v && v.length < 100) {
                if ((attr === 'id' || attr === 'data-record-id') && isDynamic(v)) continue;
                addXP(`//${tag}[@${attr}=${escapeXPath(v)}]`, 1);
            }
        }

        // ── Strategy 2: Text match ──────────────────────────────────────────
        // Collapse all internal whitespace (handles multi-line text like 'Monitorin\ng' → 'Monitoring')
        const normText = text.replace(/\s+/g, ' ').trim();
        if (normText && normText.length > 0 && normText.length < 60) {
            addXP(`//${tag}[normalize-space()=${escapeXPath(normText)}]`, 2);
            // Also try contains() for partial matches on longer text
            if (normText.length > 4) {
                addXP(`//${tag}[contains(normalize-space(), ${escapeXPath(normText)})]`, 2);
            }
        }

        // ── Strategy 3: Ancestor-anchored (enterprise: also search for Pega/SF component roots) ──
        let ancestor = el.parentNode;
        let pathParts = [];
        while (ancestor && ancestor !== document.body && ancestor.nodeType === 1) {
            const aTag = ancestor.tagName.toLowerCase();
            const aId = ancestor.getAttribute('id');
            const aTid = ancestor.getAttribute('data-testid') || ancestor.getAttribute('data-automation-id');
            const aCid = ancestor.getAttribute('data-cid') || ancestor.getAttribute('data-ctl-name');
            // Appian: check for tempo-name or ap-comp on ancestor containers
            const aAppian = ancestor.getAttribute('data-tempo-name') || ancestor.getAttribute('data-ap-comp') || ancestor.getAttribute('data-ap-name');
            const aRole = ancestor.getAttribute('role');

            let anchor = null;
            if (aTid && !isDynamic(aTid)) anchor = `//${aTag}[@data-testid=${escapeXPath(aTid)}]`;
            else if (aCid && !isDynamic(aCid)) anchor = `//${aTag}[@data-cid=${escapeXPath(aCid)}]`;
            else if (aAppian && !isDynamic(aAppian)) {
                const appianAttr = ancestor.getAttribute('data-tempo-name') ? 'data-tempo-name'
                                 : ancestor.getAttribute('data-ap-comp')    ? 'data-ap-comp'
                                 : 'data-ap-name';
                anchor = `//${aTag}[@${appianAttr}=${escapeXPath(aAppian)}]`;
            }
            else if (aId && !isDynamic(aId)) anchor = `//${aTag}[@id=${escapeXPath(aId)}]`;
            else if (aRole && ['dialog','form','grid','table','listbox','combobox','navigation','main'].includes(aRole)) {
                anchor = `//${aTag}[@role=${escapeXPath(aRole)}]`;
            }

            if (anchor) {
                const subPath = pathParts.length > 0 ? '/' + pathParts.join('/') : '';
                addXP(`${anchor}/${tag}${subPath}`, 3);
                if (text && text.length < 60) {
                    addXP(`${anchor}//${tag}[normalize-space()=${escapeXPath(text)}]`, 3);
                }
                break;
            }

            let idx = 1;
            let sib = ancestor.previousSibling;
            while (sib) {
                if (sib.nodeType === 1 && sib.tagName === ancestor.tagName) idx++;
                sib = sib.previousSibling;
            }
            pathParts.unshift(idx > 1 ? `${aTag}[${idx}]` : aTag);
            ancestor = ancestor.parentNode;
        }

        // ── Strategy 4: Sibling-indexed (last resort) ─────────────────────
        const sibXP = buildSiblingIndexedXPath(el);
        if (sibXP) addXP(sibXP, 4);

        // Sort: unique (count=1) → strategy priority → shorter expression
        return candidates.sort((a, b) => {
            if (a.count === 1 && b.count !== 1) return -1;
            if (b.count === 1 && a.count !== 1) return 1;
            if (a.priority !== b.priority) return a.priority - b.priority;
            return a.value.length - b.value.length;
        });
    }

    /**
     * Walk shadow roots recursively to find an element matching a selector.
     * Needed for Salesforce LWC, Creatio web components, and Pega Cosmos.
     */
    function queryShadowDeep(root, selector, isXpath) {
        const results = [];
        function walk(node) {
            if (!node) return;
            try {
                if (isXpath) {
                    const snap = node.evaluate ? node.evaluate(selector, node, null, XPathResult.ORDERED_NODE_SNAPSHOT_TYPE, null) : null;
                    if (snap) for (let i = 0; i < snap.snapshotLength; i++) results.push(snap.snapshotItem(i));
                } else {
                    const found = node.querySelectorAll ? node.querySelectorAll(selector) : [];
                    for (const el of found) results.push(el);
                }
            } catch(e){}
            // Walk children for shadow roots
            const children = node.querySelectorAll ? node.querySelectorAll('*') : [];
            for (const child of children) {
                if (child.shadowRoot) walk(child.shadowRoot);
            }
        }
        walk(root);
        return results;
    }

    /**
     * Build a piercing CSS selector path for shadow-DOM elements.
     * Used as a fallback for Salesforce/Creatio web components.
     */
    function buildShadowCSSPath(el) {
        const parts = [];
        let node = el;
        while (node && node.nodeType === 1) {
            const host = node.getRootNode && node.getRootNode();
            const tag = node.tagName.toLowerCase();
            const id = node.getAttribute('id');
            const tid = node.getAttribute('data-testid');
            if (tid) { parts.unshift(`[data-testid="${tid}"]`); break; }
            if (id && !isDynamic(id)) { parts.unshift(`#${id}`); break; }
            // Count same-tag siblings
            let idx = 1;
            let sib = node.previousElementSibling;
            while (sib) { if (sib.tagName === node.tagName) idx++; sib = sib.previousElementSibling; }
            parts.unshift(idx > 1 ? `${tag}:nth-of-type(${idx})` : tag);
            node = node.parentElement || (host instanceof ShadowRoot ? host.host : null);
        }
        return parts.join(' >>> ');
    }


    /**
     * Generate ALL locator types for an element.
     * XPaths from `buildSmartXPaths`; CSS, ID, Name, Playwright from own attributes.
     */
    function generateLocators(el) {
        const list = [];
        const tag  = el.tagName.toLowerCase();

        const id      = el.getAttribute('id');
        const name    = el.getAttribute('name');
        const testId  = el.getAttribute('data-testid') || el.getAttribute('data-test') || el.getAttribute('data-cy') || el.getAttribute('data-automation-id');
        const aria    = el.getAttribute('aria-label');
        const ph      = el.getAttribute('placeholder');
        const alt     = el.getAttribute('alt');
        const titleAt = el.getAttribute('title');
        const role    = el.getAttribute('role') ||
                        (tag === 'button' ? 'button' : tag === 'a' ? 'link' : null);
        const text    = el.textContent.trim();

        // Enterprise-specific
        const pyId    = el.getAttribute('pyid');               // Pega
        const dataCid = el.getAttribute('data-cid');           // Creatio
        const dataCtl = el.getAttribute('data-ctl-name');      // Pega Cosmos
        const dataRef = el.getAttribute('data-reference');     // Pega
        const dataKey = el.getAttribute('data-key');           // Salesforce
        const dataMod = el.getAttribute('data-model-item-name'); // Creatio
        // Appian-specific stable attributes
        const apTempoName = el.getAttribute('data-tempo-name');  // Appian Tempo field name (most stable)
        const apComp      = el.getAttribute('data-ap-comp');     // Appian component type
        const apField     = el.getAttribute('data-ap-field') || el.getAttribute('ap-field-name'); // Appian form field
        const apName      = el.getAttribute('data-ap-name');     // Appian named component
        const apId        = el.getAttribute('data-ap-id');       // Appian component ID (check if dynamic)
        const acnId       = el.getAttribute('acnid');            // Appian Component Node ID (usually dynamic, skip if so)

        // Detect if element is inside a shadow root
        const rootNode = el.getRootNode && el.getRootNode();
        const isInShadow = rootNode instanceof ShadowRoot;
        const searchRoot = isInShadow ? rootNode : document;

        function escapeCSSString(str) {
            if (!str) return "";
            return str.replace(/\\/g, "\\\\").replace(/"/g, "\\\"");
        }

        // ── Enterprise stable attributes first ──────────────────────────────
        if (pyId) {
            list.push({ type: 'XPath', value: `//*[@pyid=${escapeXPath(pyId)}]`, count: xpathCount(`//*[@pyid=${escapeXPath(pyId)}]`, searchRoot), rating: 'Best' });
        }
        if (dataCid && !isDynamic(dataCid)) {
            list.push({ type: 'CSS', value: `[data-cid="${escapeCSSString(dataCid)}"]`, count: cssCount(`[data-cid="${escapeCSSString(dataCid)}"]`, searchRoot), rating: 'Best' });
        }
        if (dataCtl) {
            list.push({ type: 'XPath', value: `//*[@data-ctl-name=${escapeXPath(dataCtl)}]`, count: xpathCount(`//*[@data-ctl-name=${escapeXPath(dataCtl)}]`, searchRoot), rating: 'Best' });
        }
        if (dataRef) {
            list.push({ type: 'XPath', value: `//*[@data-reference=${escapeXPath(dataRef)}]`, count: xpathCount(`//*[@data-reference=${escapeXPath(dataRef)}]`, searchRoot), rating: 'Best' });
        }
        if (dataKey && !isDynamic(dataKey)) {
            list.push({ type: 'CSS', value: `[data-key="${escapeCSSString(dataKey)}"]`, count: cssCount(`[data-key="${escapeCSSString(dataKey)}"]`, searchRoot), rating: 'Best' });
        }
        if (dataMod) {
            list.push({ type: 'CSS', value: `[data-model-item-name="${escapeCSSString(dataMod)}"]`, count: cssCount(`[data-model-item-name="${escapeCSSString(dataMod)}"]`, searchRoot), rating: 'Best' });
        }

        // ── Appian-specific locators ─────────────────────────────────────────
        // data-tempo-name is the most stable Appian attribute — survives app upgrades
        if (apTempoName && !isDynamic(apTempoName)) {
            const xp = `//*[@data-tempo-name=${escapeXPath(apTempoName)}]`;
            list.push({ type: 'XPath', value: xp, count: xpathCount(xp, searchRoot), rating: 'Best', note: 'Appian Tempo' });
            list.push({ type: 'CSS', value: `[data-tempo-name="${escapeCSSString(apTempoName)}"]`, count: cssCount(`[data-tempo-name="${escapeCSSString(apTempoName)}"]`, searchRoot), rating: 'Best', note: 'Appian Tempo' });
        }
        if (apField && !isDynamic(apField)) {
            const attrName = el.getAttribute('data-ap-field') ? 'data-ap-field' : 'ap-field-name';
            list.push({ type: 'CSS', value: `[${attrName}="${escapeCSSString(apField)}"]`, count: cssCount(`[${attrName}="${escapeCSSString(apField)}"]`, searchRoot), rating: 'Best', note: 'Appian Field' });
            list.push({ type: 'XPath', value: `//*[@${attrName}=${escapeXPath(apField)}]`, count: xpathCount(`//*[@${attrName}=${escapeXPath(apField)}]`, searchRoot), rating: 'Best', note: 'Appian Field' });
        }
        if (apComp && !isDynamic(apComp)) {
            // ap-comp is a component type (e.g., 'button', 'textField') — combine with text/aria for uniqueness
            const baseXp = `//*[@data-ap-comp=${escapeXPath(apComp)}]`;
            const cnt = xpathCount(baseXp, searchRoot);
            if (cnt === 1) {
                list.push({ type: 'XPath', value: baseXp, count: 1, rating: 'Best', note: 'Appian Component' });
            } else if (text && text.length < 50) {
                const refinedXp = `//*[@data-ap-comp=${escapeXPath(apComp)} and normalize-space()=${escapeXPath(text)}]`;
                list.push({ type: 'XPath', value: refinedXp, count: xpathCount(refinedXp, searchRoot), rating: 'Good', note: 'Appian Component+Text' });
            }
        }
        if (apName && !isDynamic(apName)) {
            list.push({ type: 'CSS', value: `[data-ap-name="${escapeCSSString(apName)}"]`, count: cssCount(`[data-ap-name="${escapeCSSString(apName)}"]`, searchRoot), rating: 'Best', note: 'Appian Name' });
        }
        // acnid is usually dynamic but include as last resort if it looks stable (short, non-numeric)
        if (acnId && !isDynamic(acnId) && acnId.length < 20 && !/^\d+$/.test(acnId)) {
            list.push({ type: 'CSS', value: `[acnid="${escapeCSSString(acnId)}"]`, count: cssCount(`[acnid="${escapeCSSString(acnId)}"]`, searchRoot), rating: 'Ok', note: 'Appian Node ID' });
        }

        // Shadow DOM path (for Salesforce LWC / Creatio / Appian web components)
        if (isInShadow) {
            const shadowPath = buildShadowCSSPath(el);
            if (shadowPath) {
                list.push({ type: 'CSS', value: shadowPath, count: 1, rating: 'Best', note: 'Shadow DOM path' });
            }
        }

        // ── Standard locators ────────────────────────────────────────────────
        if (testId) {
            const escapedTid = escapeCSSString(testId);
            list.push({ type: 'Test ID',     value: testId, count: cssCount(`[data-testid="${escapedTid}"],[data-test="${escapedTid}"],[data-automation-id="${escapedTid}"]`, searchRoot) });
            list.push({ type: 'getByTestId', value: testId, count: cssCount(`[data-testid="${escapedTid}"],[data-test="${escapedTid}"]`, searchRoot) });
        }
        if (id && !isDynamic(id)) {
            const c = cssCount(`#${CSS.escape(id)}`, searchRoot);
            list.push({ type: 'ID',  value: id,       count: c });
            list.push({ type: 'CSS', value: `#${id}`, count: c });
        }
        if (name) {
            list.push({ type: 'Name', value: name, count: cssCount(`[name="${escapeCSSString(name)}"]`, searchRoot) });
        }
        if (ph) {
            list.push({ type: 'getByPlaceholder', value: ph, count: cssCount(`[placeholder="${escapeCSSString(ph)}"]`, searchRoot) });
        }
        if (aria) {
            list.push({ type: 'getByLabel', value: aria, count: cssCount(`[aria-label="${escapeCSSString(aria)}"]`, searchRoot) });
        }
        if (alt) {
            list.push({ type: 'getByAltText', value: alt, count: cssCount(`[alt="${escapeCSSString(alt)}"]`, searchRoot) });
        }
        if (titleAt) {
            list.push({ type: 'getByTitle', value: titleAt, count: cssCount(`[title="${escapeCSSString(titleAt)}"]`, searchRoot) });
        }

        // ── Playwright semantic role ─────────────────────────────────────────
        if (role && text && text.length < 50) {
            list.push({ type: 'getByRole', value: `${role}||${text}`, count: 1 });
        } else if (role) {
            list.push({ type: 'getByRole', value: role, count: cssCount(`[role='${role}']`, searchRoot) });
        }

        // Text-based
        if (text && text.length > 0 && text.length < 60) {
            if (tag === 'a') {
                list.push({ type: 'Link Text', value: text, count: xpathCount(`//a[normalize-space()=${escapeXPath(text)}]`, searchRoot) });
            }
            list.push({ type: 'getByText', value: text, count: 1 });
        }

        // ── XPaths from smart engine ─────────────────────────────────────────
        const xpaths = buildSmartXPaths(el);
        for (const xp of xpaths) { xp.count = xpathCount(xp.value, searchRoot); list.push(xp); }

        if (!list.some(l => l.count > 0)) {
            list.push({ type: 'XPath', value: `//${tag}`, count: 1, rating: 'Poor', note: 'Forced Fallback' });
        }

        return list;
    }


    // ─── Page Freeze ──────────────────────────────────────────────────────────
    window.freezePage = function (duration) {
        const overlay = document.createElement('div');
        overlay.style.cssText = `
            position: fixed; top: 10px; right: 10px; padding: 10px 20px;
            background: #6366f1; color: white; border-radius: 8px;
            z-index: 1000000; font-family: sans-serif; font-weight: bold;
            box-shadow: 0 4px 6px -1px rgba(0,0,0,0.1); pointer-events: none;
        `;
        (document.body || document.documentElement).appendChild(overlay);
        let timeLeft = duration / 1000;
        overlay.innerText = `❄️ Frozen: ${timeLeft}s`;
        const timer = setInterval(() => {
            timeLeft--;
            overlay.innerText = `❄️ Frozen: ${timeLeft}s`;
            if (timeLeft <= 0) { clearInterval(timer); overlay.remove(); }
        }, 1000);
        const suppress = ['blur','focusout','mouseleave'];
        const handler  = (e) => { e.stopImmediatePropagation(); e.preventDefault(); };
        suppress.forEach(evt => { window.addEventListener(evt, handler, true); document.addEventListener(evt, handler, true); });
        setTimeout(() => {
            suppress.forEach(evt => { window.removeEventListener(evt, handler, true); document.removeEventListener(evt, handler, true); });
        }, duration);
    };

    // ─── Live Console Verification ───────────────────────────────────────────
    window.verifyLiveLocator = function(type, value) {
        return window._doVerify(type, value);
    };

    window._doVerify = function(type, value) {
        document.querySelectorAll('.live-console-highlight').forEach(el => el.classList.remove('live-console-highlight'));
        if (!document.getElementById('live-console-style')) {
            const style = document.createElement('style');
            style.id = 'live-console-style';
            style.innerHTML = `
                .live-console-highlight {
                    outline: 3px dashed #f59e0b !important;
                    outline-offset: 3px !important;
                    background: rgba(245, 158, 11, 0.15) !important;
                    box-shadow: 0 0 0 4px rgba(245, 158, 11, 0.3) !important;
                }
                @keyframes lc-pulse {
                    0%   { box-shadow: 0 0 0 0 rgba(245,158,11,0.7); }
                    70%  { box-shadow: 0 0 0 10px rgba(245,158,11,0); }
                    100% { box-shadow: 0 0 0 0 rgba(245,158,11,0); }
                }
                .live-console-highlight-pulse {
                    animation: lc-pulse 0.7s ease-out 2;
                }
            `;
            document.head.appendChild(style);
        }

        if (!value) return 0;

        let count = 0;
        const isXpath = type.toLowerCase().includes('xpath');
        const isRole = type.toLowerCase() === 'getbyrole';
        
        try {
            let domResults = [];
            let shadowResults = [];

            if (isXpath) {
                const s = document.evaluate(value, document, null, XPathResult.ORDERED_NODE_SNAPSHOT_TYPE, null);
                for (let i=0; i<s.snapshotLength; i++) domResults.push(s.snapshotItem(i));
                shadowResults = queryShadowDeep(document, value, true);
            } else if (isRole) {
                const [role, name] = value.split('||');
                const tagMap = { 'link': 'a', 'checkbox': 'input[type="checkbox"]', 'textbox': 'input[type="text"],textarea', 'heading': 'h1,h2,h3,h4,h5,h6' };
                const tagSelector = tagMap[role] || role;
                const selector = `[role="${role}"],${tagSelector}`;
                const allNodes = [...Array.from(document.querySelectorAll(selector)), ...queryShadowDeep(document, selector, false)];
                domResults = name 
                    ? allNodes.filter(e => e.textContent.trim().includes(name))
                    : allNodes;
            } else {
                // Regular CSS
                domResults = Array.from(document.querySelectorAll(value));
                shadowResults = queryShadowDeep(document, value, false);
            }

            const all = [...new Set([...domResults, ...shadowResults])].filter(e => e.nodeType === 1);
            count = all.length;
            all.forEach(el => {
                el.classList.add('live-console-highlight');
                el.classList.add('live-console-highlight-pulse');
                setTimeout(() => el.classList.remove('live-console-highlight-pulse'), 1500);
            });

            // Scroll first match into view so user can actually see it
            if (all.length > 0) {
                try {
                    all[0].scrollIntoView({ behavior: 'smooth', block: 'center', inline: 'nearest' });
                } catch(e) {}
            }
        } catch (e) {
            count = 0;
        }
        return count;
    };

    window.highlightElementByLocator = function (type, value) {
        // Recursively broadcast to ALL descendant frames (handles Appian deep iframes)
        if (window === window.top) {
            window._highlightFound = false; // Track success across frames
            broadcastToAllFrames({ type: 'HIGHLIGHT_INSPECTOR', locatorType: type, locatorValue: value });
        }

        function showToast(msg, color) {
            const t = document.createElement('div');
            t.innerText = msg;
            t.style.cssText = `position:fixed;top:20px;left:50%;transform:translateX(-50%);background:${color};color:white;padding:10px 20px;border-radius:8px;z-index:999999;font-weight:bold;font-family:sans-serif;box-shadow:0 4px 12px rgba(0,0,0,0.3);`;
            document.body.appendChild(t);
            setTimeout(() => t.remove(), 2500);
        }

        function applyHighlight(el, source) {
            if (window !== window.top) {
                // Tell top window we found it
                try { window.top._highlightFound = true; } catch(e){}
            } else {
                window._highlightFound = true;
            }

            try { el.scrollIntoView({ behavior: 'smooth', block: 'center' }); } catch(e){}
            const rect = el.getBoundingClientRect();
            const isHidden = rect.width === 0 || rect.height === 0 ||
                window.getComputedStyle(el).display === 'none' ||
                window.getComputedStyle(el).visibility === 'hidden';

            if (isHidden) {
                showToast(`⚠️ Found in ${source} but element is hidden`, '#f59e0b');
            } else {
                showToast(`✅ Highlighted in ${source}`, '#10b981');
            }

            const origBorder = el.style.border;
            const origBg     = el.style.backgroundColor;
            const origOutline = el.style.outline;
            el.style.transition = 'all 0.2s ease';
            el.style.outline = '3px solid #ef4444';
            el.style.border = '2px solid #ef4444';
            el.style.backgroundColor = 'rgba(239, 68, 68, 0.2)';
            setTimeout(() => {
                if (el) {
                    el.style.border = origBorder;
                    el.style.backgroundColor = origBg;
                    el.style.outline = origOutline;
                }
            }, 2200);
        }

        let el = null;
        let source = 'DOM';
        try {
            function escapeCSSString(str) {
                if (!str) return "";
                return str.replace(/\\/g, "\\\\").replace(/"/g, "\\\"");
            }
            const escVal = escapeCSSString(value);
            const uType = (type || "").toUpperCase().replace(/\s/g, '');

            let allNodes = [];

            if      (uType === 'ID')                { const n = document.getElementById(value); if (n) allNodes.push(n); }
            else if (uType === 'NAME')              allNodes = Array.from(document.getElementsByName(value));
            else if (uType === 'CSS')               allNodes = Array.from(document.querySelectorAll(value));
            else if (uType === 'LINKTEXT')          allNodes = Array.from(document.getElementsByTagName('a')).filter(a => a.textContent.trim() === value);
            else if (uType === 'XPATH')             {
                                                        const s = document.evaluate(value, document, null, XPathResult.ORDERED_NODE_SNAPSHOT_TYPE, null);
                                                        for(let i=0; i<s.snapshotLength; i++) allNodes.push(s.snapshotItem(i));
                                                    }
            else if (uType === 'TESTID')            allNodes = Array.from(document.querySelectorAll(`[data-testid="${escVal}"],[data-test="${escVal}"],[data-automation-id="${escVal}"]`));
            else if (uType === 'GETBYTESTID')       allNodes = Array.from(document.querySelectorAll(`[data-testid="${escVal}"],[data-test="${escVal}"]`));
            else if (uType === 'GETBYPLACEHOLDER')  allNodes = Array.from(document.querySelectorAll(`[placeholder="${escVal}"]`));
            else if (uType === 'GETBYLABEL')        allNodes = Array.from(document.querySelectorAll(`[aria-label="${escVal}"]`));
            else if (uType === 'GETBYALTTEXT')      allNodes = Array.from(document.querySelectorAll(`[alt="${escVal}"]`));
            else if (uType === 'GETBYTITLE')        allNodes = Array.from(document.querySelectorAll(`[title="${escVal}"]`));
            else if (uType === 'GETBYTEXT')         allNodes = Array.from(document.querySelectorAll('*')).filter(n => n.children.length === 0 && n.textContent.trim() === value);
            else if (uType === 'GETBYROLE') {
                const [role, name] = value.split('||');
                const tagMap = { 'link': 'a', 'checkbox': 'input[type="checkbox"]', 'textbox': 'input[type="text"],textarea', 'heading': 'h1,h2,h3,h4,h5,h6' };
                const tagSelector = tagMap[role] || role;
                const selector = `[role="${escapeCSSString(role)}"],${tagSelector}`;
                const rawNodes = [...Array.from(document.querySelectorAll(selector)), ...queryShadowDeep(document, selector, false)];
                allNodes = name ? rawNodes.filter(e => e.textContent.trim().includes(name)) : rawNodes;
            }

            // Shadow DOM fallback (Salesforce LWC / Creatio / Appian iframes)
            if (allNodes.length === 0 && uType !== 'GETBYROLE') { // GETBYROLE already handles shadow DOM above
                const isXpath = uType === 'XPATH';
                allNodes = queryShadowDeep(document, value, isXpath);
                if (allNodes.length > 0) source = 'Shadow DOM';
            }

            if (allNodes.length > 0) {
                // Priority 1: Pick the first VISIBLE node
                const visibleNodes = allNodes.filter(n => {
                    const rect = n.getBoundingClientRect();
                    const style = window.getComputedStyle(n);
                    return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
                });
                el = visibleNodes.length > 0 ? visibleNodes[0] : allNodes[0];
            }

            if (el) {
                applyHighlight(el, source);
            } else if (window === window.top) {
                // Only show not found toast from top window after a delay (giving iframes time to applyHighlight)
                setTimeout(() => {
                    if (!window._highlightFound) {
                        showToast('❌ Locator did not match any element', '#ef4444');
                    }
                }, 100);
            }
        } catch (e) {
            console.error('Highlight error:', e);
            if (window === window.top) {
                setTimeout(() => {
                    if (!window._highlightFound) showToast('⚠️ Highlight Error: ' + e.message, '#ef4444');
                }, 100);
            }
        }
    };
}
})();

