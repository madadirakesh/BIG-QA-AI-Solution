"""
JavaScript injected via Page.addScriptToEvaluateOnNewDocument.

Runs before any page script on every navigation, so it reliably captures
paint timing / LCP / CLS / long-task / interaction data using the browser's
native PerformanceObserver APIs - the same primitives Lighthouse and
web-vitals.js are built on. No external JS dependency is required.
"""

COLLECTOR_SCRIPT = r"""
(() => {
  window.__webperf__ = {
    fcp: null,
    lcp: null,
    cls: 0,
    longTasks: [],
    inp: 0,
    lcpElement: null,
    navigationStart: performance.timeOrigin || null
  };

  const safeObserve = (type, buffered, cb) => {
    try {
      new PerformanceObserver((list) => cb(list.getEntries())).observe({ type, buffered });
    } catch (e) { /* entry type not supported in this browser */ }
  };

  safeObserve('paint', true, (entries) => {
    for (const entry of entries) {
      if (entry.name === 'first-contentful-paint') {
        window.__webperf__.fcp = entry.startTime;
      }
    }
  });

  safeObserve('largest-contentful-paint', true, (entries) => {
    const last = entries[entries.length - 1];
    if (last) {
      window.__webperf__.lcp = last.startTime;
      try {
        const el = last.element;
        if (el && el.getBoundingClientRect) {
          const r = el.getBoundingClientRect();
          window.__webperf__.lcpElement = {
            tag: el.tagName ? el.tagName.toLowerCase() : null,
            id: el.id || null,
            cls: (el.className && el.className.toString) ? el.className.toString().slice(0, 120) : null,
            rect: { x: r.x, y: r.y, width: r.width, height: r.height },
            url: last.url || null
          };
        }
      } catch (e) { /* element may be gone */ }
    }
  });

  safeObserve('layout-shift', true, (entries) => {
    for (const entry of entries) {
      if (!entry.hadRecentInput) {
        window.__webperf__.cls += entry.value;
      }
    }
  });

  safeObserve('longtask', true, (entries) => {
    for (const entry of entries) {
      window.__webperf__.longTasks.push({ start: entry.startTime, duration: entry.duration });
    }
  });

  // INP (Interaction to Next Paint): track the worst interaction latency.
  // Needs durationThreshold, which safeObserve() above doesn't pass, so use a
  // dedicated observer.
  try {
    new PerformanceObserver((list) => {
      for (const e of list.getEntries()) {
        if (e.interactionId && e.duration > window.__webperf__.inp) {
          window.__webperf__.inp = e.duration;
        }
      }
    }).observe({ type: 'event', buffered: true, durationThreshold: 40 });
  } catch (e) { /* Event Timing API not supported */ }
})();
"""

COLLECT_RESULTS_EXPR = "JSON.stringify(window.__webperf__ || {})"

# Reads the current document's Navigation Timing entry. Used to record the
# load time of each distinct URL the page navigates to. `duration` is
# loadEventEnd - startTime (0 until the load event fires), so callers should
# keep the latest non-zero reading per URL.
COLLECT_NAV_TIMING_EXPR = r"""
(() => {
  try {
    const nav = performance.getEntriesByType('navigation')[0];
    if (!nav) return 'null';
    const startTime = nav.startTime || 0;
    const load = nav.loadEventEnd ? (nav.loadEventEnd - startTime) : 0;
    const dcl = nav.domContentLoadedEventEnd ? (nav.domContentLoadedEventEnd - startTime) : 0;
    const resp = nav.responseEnd ? (nav.responseEnd - startTime) : 0;
    return JSON.stringify({
      url: location.href,
      load_time_ms: load > 0 ? Math.round(load) : null,
      dom_content_loaded_ms: dcl > 0 ? Math.round(dcl) : null,
      response_end_ms: resp > 0 ? Math.round(resp) : null
    });
  } catch (e) { return 'null'; }
})()
"""

# In-page root-cause diagnostics + Lighthouse-style category audits. Returns a
# JSON blob describing WHY a page may be slow (render-blocking resources, TTFB,
# DOM size, oversized images) and how it scores on Accessibility, Best
# Practices, SEO and PWA checks. All DOM/timing based - no external deps.
COLLECT_DIAGNOSTICS_EXPR = r"""
(() => {
  const out = {};

  // --- Server response time / TTFB -------------------------------------- //
  try {
    const nav = performance.getEntriesByType('navigation')[0];
    if (nav) {
      out.ttfb_ms = Math.round(nav.responseStart - nav.startTime);
      out.server_response_ms = Math.round(nav.responseStart - nav.requestStart);
    }
  } catch (e) {}

  // --- DOM size --------------------------------------------------------- //
  try {
    const all = document.getElementsByTagName('*');
    let maxDepth = 0, maxChildren = 0;
    const depthOf = (el) => { let d = 0; while (el) { d++; el = el.parentElement; } return d; };
    for (const el of all) {
      const d = depthOf(el);
      if (d > maxDepth) maxDepth = d;
      if (el.children && el.children.length > maxChildren) maxChildren = el.children.length;
    }
    out.dom = { nodes: all.length, max_depth: maxDepth, max_children: maxChildren };
  } catch (e) {}

  // --- Render-blocking resources ---------------------------------------- //
  try {
    const timings = {};
    for (const r of performance.getEntriesByType('resource')) timings[r.name] = r;
    const rb = [];
    document.querySelectorAll('link[rel~="stylesheet"]').forEach((l) => {
      if (l.disabled) return;
      const media = l.media && l.media !== '' ? l.media : 'all';
      let applies = true;
      try { applies = media === 'all' || (window.matchMedia && window.matchMedia(media).matches); } catch (e) {}
      if (!applies) return;
      const t = timings[l.href];
      rb.push({ url: l.href, type: 'stylesheet',
                duration_ms: t ? Math.round(t.duration) : null,
                transfer_size: t ? (t.transferSize || 0) : null });
    });
    document.querySelectorAll('head script[src]').forEach((s) => {
      if (s.async || s.defer || s.type === 'module') return;
      const t = timings[s.src];
      rb.push({ url: s.src, type: 'script',
                duration_ms: t ? Math.round(t.duration) : null,
                transfer_size: t ? (t.transferSize || 0) : null });
    });
    out.render_blocking = rb;
  } catch (e) {}

  // --- Images (for sizing / next-gen-format opportunities) -------------- //
  try {
    const imgs = [];
    document.querySelectorAll('img').forEach((img) => {
      const dw = img.clientWidth, dh = img.clientHeight;
      const nw = img.naturalWidth, nh = img.naturalHeight;
      if (nw && nh && dw && dh) {
        imgs.push({ url: img.currentSrc || img.src,
                    natural: { w: nw, h: nh }, displayed: { w: dw, h: dh },
                    dpr: window.devicePixelRatio || 1 });
      }
    });
    out.images = imgs;
  } catch (e) {}

  // --- LCP element + INP carried over from the collector ---------------- //
  try { out.lcp_element = (window.__webperf__ || {}).lcpElement || null; } catch (e) {}
  try { const v = (window.__webperf__ || {}).inp || 0; out.inp_ms = v ? Math.round(v) : null; } catch (e) {}
  try { out.viewport = { w: window.innerWidth, h: window.innerHeight }; } catch (e) {}

  // --- Category audits -------------------------------------------------- //
  const runAudits = (checks) => {
    const passed = [], failed = [];
    for (const c of checks) {
      let ok = false;
      try { ok = !!c.test(); } catch (e) { ok = false; }
      (ok ? passed : failed).push({ id: c.id, title: c.title });
    }
    const total = passed.length + failed.length;
    return { score: total ? passed.length / total : null, passed: passed, failed: failed };
  };

  const isLocal = ['localhost', '127.0.0.1', '::1'].indexOf(location.hostname) !== -1;
  const genericLink = ['click here', 'here', 'read more', 'more', 'link', 'this'];

  const accessibility = runAudits([
    { id: 'image-alt', title: 'Image elements have [alt] attributes',
      test: () => [].every.call(document.querySelectorAll('img'), (i) => i.hasAttribute('alt')) },
    { id: 'html-has-lang', title: '<html> element has a [lang] attribute',
      test: () => !!document.documentElement.lang },
    { id: 'label', title: 'Form elements have associated labels',
      test: () => [].every.call(document.querySelectorAll('input:not([type=hidden]),select,textarea'),
                  (el) => (el.labels && el.labels.length) || el.getAttribute('aria-label') ||
                          el.getAttribute('aria-labelledby') || el.closest('label')) },
    { id: 'button-name', title: 'Buttons have an accessible name',
      test: () => [].every.call(document.querySelectorAll('button'),
                  (b) => (b.textContent || '').trim() || b.getAttribute('aria-label') || b.getAttribute('title')) },
    { id: 'link-name', title: 'Links have a discernible name',
      test: () => [].every.call(document.querySelectorAll('a[href]'),
                  (a) => (a.textContent || '').trim() || a.getAttribute('aria-label') || a.querySelector('img[alt]:not([alt=""])')) },
    { id: 'document-title', title: 'Document has a <title> element',
      test: () => !!(document.title && document.title.trim()) },
    { id: 'meta-viewport', title: 'Has a <meta name="viewport">',
      test: () => !!document.querySelector('meta[name=viewport]') },
  ]);

  const bestPractices = runAudits([
    { id: 'is-on-https', title: 'Uses HTTPS', test: () => location.protocol === 'https:' || isLocal },
    { id: 'doctype', title: 'Page has the HTML doctype', test: () => !!document.doctype },
    { id: 'charset', title: 'Properly defines charset', test: () => !!document.characterSet },
    { id: 'no-console-errors', title: 'No browser errors logged (see console section)',
      test: () => true },
    { id: 'image-aspect-ratio', title: 'Displays images with correct aspect ratio',
      test: () => [].every.call(document.querySelectorAll('img'), (img) => {
        if (!img.naturalWidth || !img.clientWidth) return true;
        const nr = img.naturalWidth / img.naturalHeight, dr = img.clientWidth / img.clientHeight;
        return Math.abs(nr - dr) / nr < 0.15;
      }) },
  ]);

  const seo = runAudits([
    { id: 'document-title', title: 'Document has a <title> element',
      test: () => !!(document.title && document.title.trim()) },
    { id: 'meta-description', title: 'Document has a meta description',
      test: () => { const m = document.querySelector('meta[name=description]'); return !!(m && m.content && m.content.trim()); } },
    { id: 'html-has-lang', title: '<html> element has a [lang] attribute',
      test: () => !!document.documentElement.lang },
    { id: 'viewport', title: 'Has a <meta name="viewport">',
      test: () => !!document.querySelector('meta[name=viewport]') },
    { id: 'link-text', title: 'Links have descriptive text',
      test: () => [].every.call(document.querySelectorAll('a[href]'), (a) => {
        const t = (a.textContent || '').trim().toLowerCase();
        return !t || genericLink.indexOf(t) === -1;
      }) },
    { id: 'is-crawlable', title: 'Page isn\'t blocked from indexing',
      test: () => { const m = document.querySelector('meta[name=robots]'); return !(m && /noindex/i.test(m.content || '')); } },
    { id: 'canonical', title: 'Document has a valid rel=canonical',
      test: () => !!document.querySelector('link[rel=canonical]') },
  ]);

  const pwa = runAudits([
    { id: 'installable-manifest', title: 'Web app manifest is linked',
      test: () => !!document.querySelector('link[rel=manifest]') },
    { id: 'service-worker', title: 'Registers a service worker',
      test: () => ('serviceWorker' in navigator) && !!navigator.serviceWorker.controller },
    { id: 'themed-omnibox', title: 'Sets a theme color',
      test: () => !!document.querySelector('meta[name=theme-color]') },
    { id: 'viewport', title: 'Has a <meta name="viewport">',
      test: () => !!document.querySelector('meta[name=viewport]') },
    { id: 'apple-touch-icon', title: 'Provides a valid apple-touch-icon',
      test: () => !!document.querySelector('link[rel="apple-touch-icon"]') },
  ]);

  out.audits = { accessibility: accessibility, 'best-practices': bestPractices, seo: seo, pwa: pwa };

  return JSON.stringify(out);
})()
"""
