"""
Writes finalized session result dict(s) to report.json and a Lighthouse-styled
report.html.

The HTML now covers the Lighthouse-parity dimensions this tool implements:
  - Core Web Vitals (FCP, Speed Index, LCP, TBT, CLS, INP)
  - Audit categories (Performance, Accessibility, Best Practices, SEO, PWA)
  - Root-cause diagnostics (render-blocking resources, TTFB, DOM size, LCP element)
  - Actionable guidance (Opportunities & estimated savings)
  - Visual analysis (load filmstrip + final screenshot with LCP element highlighted)
"""

from __future__ import annotations

import json
import os
import time
from html import escape

from .scoring import compute_performance_score, rating_for_score, score_metric


# --------------------------------------------------------------------------- #
# Formatting helpers
# --------------------------------------------------------------------------- #
def write_json_report(result: dict, output_dir: str, filename: str = "report.json") -> str:
    os.makedirs(output_dir, exist_ok=True)
    path = os.path.join(output_dir, filename)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, default=str)
    return path


def _score_color(score) -> str:
    if score is None:
        return "#9aa0a6"
    if score >= 90:
        return "#0cce6b"
    if score >= 50:
        return "#ffa400"
    return "#ff4e42"


def _metric_color(rating: str) -> str:
    return {"good": "#0cce6b", "needs-improvement": "#ffa400", "poor": "#ff4e42"}.get(rating, "#9aa0a6")


def _fmt_ms(value) -> str:
    if value is None:
        return "N/A"
    return f"{value / 1000:.2f} s" if value >= 1000 else f"{value:.0f} ms"


def _fmt_bytes(n) -> str:
    n = n or 0
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def _metric_card(display: str, value_str: str, rating: str) -> str:
    color = _metric_color(rating)
    return f"""
    <div class="metric-card" style="border-left-color:{color}">
      <div class="metric-name">{escape(display)}</div>
      <div class="metric-value" style="color:{color}">{escape(value_str)}</div>
      <div class="metric-rating">{escape(rating.replace('-', ' '))}</div>
    </div>"""


# --------------------------------------------------------------------------- #
# Section renderers (reused by single-session AND consolidated reports)
# --------------------------------------------------------------------------- #
_CATEGORY_LABELS = {
    "accessibility": "Accessibility",
    "best-practices": "Best Practices",
    "seo": "SEO",
    "pwa": "PWA",
}


def _donut(label: str, score) -> str:
    color = _score_color(score)
    val = score if score is not None else "N/A"
    return f"""
    <div class="cat">
      <div class="cat-circle" style="border-color:{color};color:{color}">{val}</div>
      <div class="cat-label">{escape(label)}</div>
    </div>"""


def _category_row(result: dict) -> str:
    cats = result.get("categories", {}) or {}
    donuts = [_donut("Performance", result.get("performance_score"))]
    for key, label in _CATEGORY_LABELS.items():
        c = cats.get(key)
        donuts.append(_donut(label, c.get("score") if c else None))
    return f'<div class="cat-row">{"".join(donuts)}</div>'


def _opportunities_section(result: dict) -> str:
    opps = result.get("opportunities", [])
    if not opps:
        return '<p class="small">No major opportunities detected.</p>'
    blocks = ""
    for o in opps:
        if o.get("savings_ms"):
            saving = "~ " + _fmt_ms(o["savings_ms"])
        elif o.get("savings_bytes"):
            saving = "~ " + _fmt_bytes(o["savings_bytes"])
        else:
            saving = ""
        items = "".join(
            f"<li class='mono small'>{escape(str(i.get('url') or ''))} "
            f"<span class='muted'>{escape(str(i.get('note') or ''))}</span></li>"
            for i in o.get("items", [])
        )
        items_html = f"<ul class='opp-items'>{items}</ul>" if items else ""
        blocks += f"""
        <div class="opp">
          <div class="opp-head">
            <span class="opp-title">{escape(o.get('title', ''))}</span>
            <span class="opp-save">{escape(saving)}</span>
          </div>
          <div class="small">{escape(o.get('detail', ''))}</div>
          {items_html}
        </div>"""
    return blocks


def _diagnostics_section(result: dict) -> str:
    items = result.get("diagnostics", [])
    if not items:
        return '<p class="small">No diagnostics captured.</p>'
    rows = ""
    for it in items:
        color = "#ffa400" if it.get("severity") == "warn" else "#0cce6b"
        rows += f"""
        <tr>
          <td><span class="dot" style="background:{color}"></span>{escape(it.get('title', ''))}</td>
          <td class="mono small">{escape(str(it.get('value', '')))}</td>
        </tr>"""
    return f"<table><tbody>{rows}</tbody></table>"


def _audits_section(result: dict) -> str:
    cats = result.get("categories", {}) or {}
    blocks = ""
    for key, label in _CATEGORY_LABELS.items():
        c = cats.get(key)
        if not c:
            continue
        rows = ""
        for a in c.get("failed", []):
            rows += (f"<tr class='fail'><td class='mark'>&#10007;</td>"
                     f"<td>{escape(a.get('title', ''))}</td></tr>")
        for a in c.get("passed", []):
            rows += (f"<tr class='pass'><td class='mark'>&#10003;</td>"
                     f"<td>{escape(a.get('title', ''))}</td></tr>")
        score = c.get("score")
        blocks += f"""
        <div class="audit-cat">
          <h3><span style="color:{_score_color(score)}">{score if score is not None else 'N/A'}</span>
              &middot; {escape(label)}</h3>
          <table><tbody>{rows}</tbody></table>
        </div>"""
    return f'<div class="audit-grid">{blocks}</div>' if blocks else \
        '<p class="small">No category audits captured.</p>'


def _visual_section(result: dict) -> str:
    frames = result.get("filmstrip", [])
    shot = result.get("screenshot")
    html = ""
    if frames:
        imgs = "".join(
            f"<figure class='frame'><img src='data:image/jpeg;base64,{f.get('data', '')}' "
            f"alt='frame'><figcaption>{f.get('offset_ms', 0)} ms</figcaption></figure>"
            for f in frames
        )
        html += f"<div class='film-label small'>Load filmstrip</div><div class='filmstrip'>{imgs}</div>"
    if shot:
        overlay = ""
        lcp = result.get("lcp_element")
        vp = result.get("viewport")
        if lcp and lcp.get("rect") and vp and vp.get("w") and vp.get("h"):
            r = lcp["rect"]
            left = max(0.0, min(100.0, r.get("x", 0) / vp["w"] * 100))
            top = max(0.0, min(100.0, r.get("y", 0) / vp["h"] * 100))
            w = max(0.0, min(100.0 - left, r.get("width", 0) / vp["w"] * 100))
            h = max(0.0, min(100.0 - top, r.get("height", 0) / vp["h"] * 100))
            overlay = (f"<div class='lcp-box' style='left:{left:.1f}%;top:{top:.1f}%;"
                       f"width:{w:.1f}%;height:{h:.1f}%'></div>")
        cap = ""
        if lcp:
            sel = escape(str(lcp.get("tag") or "element"))
            cap = (f"<figcaption class='small'>Final screenshot &middot; "
                   f"<span class='lcp-key'></span> Largest Contentful Paint element "
                   f"(<span class='mono'>{sel}</span>)</figcaption>")
        html += (f"<figure class='screenshot'><div class='shot-wrap'>"
                 f"<img src='data:image/jpeg;base64,{shot}' alt='screenshot'>{overlay}</div>{cap}</figure>")
    if not html:
        return ('<p class="small">No screenshots captured. The filmstrip and screenshot '
                'are captured on the watcher/Selenium (raw CDP) path.</p>')
    return html


def _resource_console_sections(result: dict) -> str:
    resource_rows = ""
    for rtype, info in sorted(result.get("resource_summary", {}).items(), key=lambda x: -x[1]["bytes"]):
        resource_rows += (f"<tr><td>{escape(rtype)}</td><td>{info['count']}</td>"
                          f"<td>{_fmt_bytes(info['bytes'])}</td></tr>")
    if not resource_rows:
        resource_rows = "<tr><td colspan='3'>No network activity recorded</td></tr>"

    console_rows = ""
    for msg in result.get("console_messages", [])[:200]:
        level_color = "#ff4e42" if msg.get("level") == "error" else "#ffa400"
        console_rows += f"""
        <tr>
          <td><span class="badge" style="background:{level_color}">{escape(str(msg.get('level', '')))}</span></td>
          <td class="mono">{escape(str(msg.get('text', '')) or '')[:300]}</td>
          <td class="mono small">{escape(str(msg.get('url') or ''))}{(':' + str(msg['line'])) if msg.get('line') is not None else ''}</td>
        </tr>"""
    if not console_rows:
        console_rows = "<tr><td colspan='3'>No console errors or warnings captured</td></tr>"

    failed_rows = ""
    for req in result.get("failed_requests", []):
        failed_rows += (f"<tr><td>{escape(str(req.get('status')))}</td>"
                        f"<td class='mono small'>{escape(str(req.get('url') or ''))}</td>"
                        f"<td>{escape(str(req.get('type', '')))}</td></tr>")
    if not failed_rows:
        failed_rows = "<tr><td colspan='3'>No failed network requests</td></tr>"

    return f"""
  <section>
    <h2>Resource Breakdown</h2>
    <table><thead><tr><th>Type</th><th>Requests</th><th>Transferred</th></tr></thead>
      <tbody>{resource_rows}</tbody></table>
  </section>
  <section>
    <h2>Console Errors &amp; Warnings ({len(result.get('console_messages', []))})</h2>
    <table><thead><tr><th>Level</th><th>Message</th><th>Source</th></tr></thead>
      <tbody>{console_rows}</tbody></table>
  </section>
  <section>
    <h2>Failed Network Requests ({len(result.get('failed_requests', []))})</h2>
    <table><thead><tr><th>Status</th><th>URL</th><th>Type</th></tr></thead>
      <tbody>{failed_rows}</tbody></table>
  </section>"""


def render_session_detail(result: dict, heading: str = None) -> str:
    """The full set of per-session sections, reused by both report types."""
    notes = "".join(f"<li>{escape(n)}</li>" for n in result.get("notes", []))
    title_html = f'<h2 class="detail-heading">{escape(heading)}</h2>' if heading else ""
    # A session's own scores describe the page that was open when it finalized;
    # say so, since the averaged per-page section is the authoritative view.
    page_results = result.get("page_results") or []
    scope_note = ""
    if len(page_results) > 1:
        last_url = page_results[-1].get("url") or ""
        scope_note = (f'<div class="small page-note">Last page measured in this session: '
                      f'<span class="mono">{escape(str(last_url))}</span>. '
                      f'{len(page_results)} page load(s) were measured - see the averaged '
                      f'per-page section for the full picture.</div>')
    return f"""
  {title_html}
  <section>
    <h2>Categories</h2>
    {scope_note}
    {_category_row(result)}
  </section>

  <section>
    <h2>Core Web Vitals</h2>
    <div class="metrics-grid">{_metrics_grid_cards(result)}</div>
  </section>

  <section>
    <h2>Visual Analysis</h2>
    {_visual_section(result)}
  </section>

  <section>
    <h2>Opportunities &amp; Estimated Savings</h2>
    {_opportunities_section(result)}
  </section>

  <section>
    <h2>Diagnostics</h2>
    {_diagnostics_section(result)}
  </section>

  <section>
    <h2>Accessibility, Best Practices, SEO &amp; PWA Audits</h2>
    {_audits_section(result)}
  </section>
  {_resource_console_sections(result)}
  <section>
    <h2>Notes &amp; Limitations</h2>
    <ul class="notes">{notes}</ul>
  </section>"""


def _metrics_grid_cards(result: dict) -> str:
    """Just the metric cards (without the wrapping div, since callers wrap)."""
    m = result.get("metrics", {})
    ms = result.get("metric_scores", {})

    def rating(k):
        return ms.get(k, {}).get("rating", "unknown")

    cls_val = m.get("cumulative_layout_shift")
    si = m.get("speed_index_ms")
    inp = m.get("interaction_to_next_paint_ms")
    return "".join([
        _metric_card("First Contentful Paint", _fmt_ms(m.get("first_contentful_paint_ms")), rating("fcp")),
        _metric_card("Speed Index", _fmt_ms(si) if si is not None else "Not measured", rating("si")),
        _metric_card("Largest Contentful Paint", _fmt_ms(m.get("largest_contentful_paint_ms")), rating("lcp")),
        _metric_card("Total Blocking Time", _fmt_ms(m.get("total_blocking_time_ms")), rating("tbt")),
        _metric_card("Cumulative Layout Shift",
                     "N/A" if cls_val is None else f"{cls_val:.3f}", rating("cls")),
        _metric_card("Interaction to Next Paint",
                     _fmt_ms(inp) if inp is not None else "No interactions", rating("inp")),
    ])


# --------------------------------------------------------------------------- #
# Per-page aggregation (averages every load of the same URL)
# --------------------------------------------------------------------------- #
# (metric_scores key, metrics key) pairs, in display order.
_METRIC_KEYS = (
    ("fcp", "first_contentful_paint_ms"),
    ("si", "speed_index_ms"),
    ("lcp", "largest_contentful_paint_ms"),
    ("tbt", "total_blocking_time_ms"),
    ("cls", "cumulative_layout_shift"),
    ("inp", "interaction_to_next_paint_ms"),
)


def _avg(values):
    vals = [v for v in values if v is not None]
    if not vals:
        return None
    return sum(vals) / len(vals)


def _normalize_url(url: str) -> str:
    """
    Group key for "the same page". The fragment is dropped (it never changes
    what the server sent) and a bare trailing slash is ignored; the query string
    is KEPT, since different parameters generally mean a different page.
    """
    u = (url or "").split("#", 1)[0]
    if u.endswith("/") and "?" not in u and u.count("/") > 2:
        u = u[:-1]
    return u


def _page_results_of(result: dict) -> list:
    """
    Per-page-load records for one session. Sessions produced before per-page
    capture existed (or by the Playwright/Selenium hooks, which don't poll)
    fall back to a single record built from the session-level metrics.
    """
    pages = result.get("page_results")
    if pages:
        return [dict(p, session=p.get("session") or result.get("label")) for p in pages]

    navs = result.get("navigations") or []
    url = navs[-1].get("url") if navs else None
    if not url:
        loads = result.get("page_loads") or []
        url = loads[0].get("url") if loads else None
    if not url:
        return []
    load = next((p for p in (result.get("page_loads") or [])
                 if _normalize_url(p.get("url")) == _normalize_url(url)), {})
    return [{
        "url": url,
        "session": result.get("label"),
        "performance_score": result.get("performance_score"),
        "metrics": result.get("metrics", {}),
        "metric_scores": result.get("metric_scores", {}),
        "categories": result.get("categories", {}),
        "load_time_ms": load.get("load_time_ms"),
        "dom_content_loaded_ms": load.get("dom_content_loaded_ms"),
    }]


def build_page_aggregates(results: list) -> list:
    """
    Group every measured page load (across all sessions) by URL and average its
    metrics, so each page appears exactly once with a single set of numbers.

    The headline scores are recomputed from the AVERAGED metric values using the
    same Lighthouse curves as a single run, rather than averaging the per-run
    scores - that keeps each page card internally consistent (its score always
    matches the metrics shown next to it).
    """
    groups: dict[str, list] = {}
    for r in results:
        for page in _page_results_of(r):
            groups.setdefault(_normalize_url(page.get("url")), []).append(page)

    aggregates = []
    for url, runs in groups.items():
        metrics: dict = {}
        for short, mkey in _METRIC_KEYS:
            value = _avg([(p.get("metrics") or {}).get(mkey) for p in runs])
            if value is not None:
                value = round(value, 3) if short == "cls" else round(value, 1)
            metrics[mkey] = value

        metric_scores = {short: score_metric(short, metrics[mkey])
                         for short, mkey in _METRIC_KEYS}

        # Category scores are averaged too; the pass/fail audit lists come from
        # the most recent load (they describe the page's markup, not its timing).
        categories: dict = {}
        latest_cats = next((p.get("categories") for p in reversed(runs) if p.get("categories")), {}) or {}
        for key in _CATEGORY_LABELS:
            avg_score = _avg([((p.get("categories") or {}).get(key) or {}).get("score") for p in runs])
            source = latest_cats.get(key) or {}
            if avg_score is None and not source:
                continue
            categories[key] = {
                "score": round(avg_score) if avg_score is not None else None,
                "passed": source.get("passed", []),
                "failed": source.get("failed", []),
            }

        run_scores = [p.get("performance_score") for p in runs if p.get("performance_score") is not None]
        aggregates.append({
            "url": url,
            "samples": len(runs),
            "sessions": sorted({p.get("session") for p in runs if p.get("session")}),
            "performance_score": compute_performance_score(metric_scores),
            "score_min": min(run_scores) if run_scores else None,
            "score_max": max(run_scores) if run_scores else None,
            "metrics": metrics,
            "metric_scores": {k: {"score": v, "rating": rating_for_score(v)}
                              for k, v in metric_scores.items()},
            "categories": categories,
            "load_time_ms": _round_or_none(_avg([p.get("load_time_ms") for p in runs])),
            "dom_content_loaded_ms": _round_or_none(_avg([p.get("dom_content_loaded_ms") for p in runs])),
            "ttfb_ms": _round_or_none(_avg([p.get("ttfb_ms") for p in runs])),
            "dom_nodes": _round_or_none(_avg([p.get("dom_nodes") for p in runs])),
            "runs": runs,
        })

    # Worst pages first - that's what a reader needs to act on.
    aggregates.sort(key=lambda a: (a["performance_score"] is None, a["performance_score"] or 0))
    return aggregates


def _round_or_none(value):
    return None if value is None else round(value)


def _page_runs_table(page: dict) -> str:
    rows = ""
    for i, run in enumerate(page.get("runs", []), start=1):
        m = run.get("metrics") or {}
        cls_val = m.get("cumulative_layout_shift")
        score = run.get("performance_score")
        rows += f"""
        <tr>
          <td>{i}</td>
          <td style="color:{_score_color(score)};font-weight:700">{score if score is not None else 'N/A'}</td>
          <td>{_fmt_ms(m.get('first_contentful_paint_ms'))}</td>
          <td>{_fmt_ms(m.get('largest_contentful_paint_ms'))}</td>
          <td>{_fmt_ms(m.get('total_blocking_time_ms'))}</td>
          <td>{'N/A' if cls_val is None else f'{cls_val:.3f}'}</td>
          <td>{_fmt_ms(run.get('load_time_ms'))}</td>
          <td class="small">{escape(str(run.get('session') or ''))}</td>
        </tr>"""
    return f"""
    <table>
      <thead><tr><th>#</th><th>Perf</th><th>FCP</th><th>LCP</th><th>TBT</th><th>CLS</th>
        <th>Load</th><th>Session</th></tr></thead>
      <tbody>{rows}</tbody>
    </table>"""


def _page_cards(pages: list) -> str:
    if not pages:
        return '<p class="small">No page loads with metrics were captured.</p>'
    cards = ""
    for page in pages:
        n = page.get("samples", 0)
        spread = ""
        if n > 1 and page.get("score_min") is not None and page["score_min"] != page.get("score_max"):
            spread = (f" &middot; individual loads scored "
                      f"{page['score_min']}&ndash;{page['score_max']}")
        extras = [
            f"Avg load time {_fmt_ms(page.get('load_time_ms'))}",
            f"Avg DOMContentLoaded {_fmt_ms(page.get('dom_content_loaded_ms'))}",
            f"Avg TTFB {_fmt_ms(page.get('ttfb_ms'))}",
            f"Avg DOM nodes {page.get('dom_nodes') if page.get('dom_nodes') is not None else 'N/A'}",
        ]
        cards += f"""
      <div class="page-card">
        <div class="page-head">
          <span class="page-url mono">{escape(str(page.get('url') or ''))}</span>
          <span class="page-count">{n} load(s) averaged</span>
        </div>
        <div class="small page-note">Scores below are computed from the averaged
          metric values{spread}.</div>
        {_category_row(page)}
        <div class="metrics-grid" style="margin-top:18px">{_metrics_grid_cards(page)}</div>
        <div class="small page-extra">{' &middot; '.join(extras)}</div>
        <details class="page-runs">
          <summary class="small">Individual loads ({n})</summary>
          {_page_runs_table(page)}
        </details>
      </div>"""
    return cards


def render_pages_section(pages: list) -> str:
    return f"""
  <section>
    <h2>Pages (averaged across every load of the same URL)</h2>
    {_page_cards(pages)}
  </section>"""


_SHARED_CSS = """
  :root { --bg:#f8f9fa; --card:#ffffff; --text:#202124; --muted:#5f6368; }
  * { box-sizing: border-box; }
  body { margin:0; font-family: -apple-system, "Segoe UI", Roboto, Arial, sans-serif; background:var(--bg); color:var(--text); }
  header { padding:32px 40px; background:var(--card); border-bottom:1px solid #e8eaed; }
  header h1 { margin:0 0 4px; font-size:22px; }
  header .sub { color:var(--muted); font-size:13px; word-break:break-all; }
  main { max-width:1100px; margin:0 auto; padding:32px 20px 80px; }
  section { margin-bottom:36px; }
  h2 { font-size:16px; text-transform:uppercase; letter-spacing:.04em; color:var(--muted); margin-bottom:14px; }
  h2.detail-heading { font-size:15px; color:var(--text); border-top:2px solid #e8eaed; padding-top:24px; text-transform:none; letter-spacing:0; }
  .cat-row { display:flex; gap:28px; flex-wrap:wrap; align-items:flex-start; }
  .cat { text-align:center; width:96px; }
  .cat-circle { width:72px; height:72px; margin:0 auto 8px; border-radius:50%; border:4px solid #9aa0a6;
                display:flex; align-items:center; justify-content:center; font-size:22px; font-weight:700; }
  .cat-label { font-size:12px; color:var(--muted); }
  .metrics-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(190px,1fr)); gap:14px; }
  .metric-card { background:var(--card); border:1px solid #e8eaed; border-left:4px solid #9aa0a6; border-radius:6px; padding:14px 16px; }
  .metric-name { font-size:12px; color:var(--muted); margin-bottom:6px; }
  .metric-value { font-size:22px; font-weight:700; }
  .metric-rating { font-size:11px; color:var(--muted); text-transform:capitalize; margin-top:2px; }
  table { width:100%; border-collapse:collapse; background:var(--card); border:1px solid #e8eaed; border-radius:6px; overflow:hidden; }
  th, td { text-align:left; padding:10px 14px; border-bottom:1px solid #f1f3f4; font-size:13px; }
  th { background:#f1f3f4; color:var(--muted); font-weight:600; }
  tr:last-child td { border-bottom:none; }
  .mono { font-family: ui-monospace, Menlo, Consolas, monospace; word-break:break-all; }
  .small { font-size:11px; color:var(--muted); }
  .muted { color:var(--muted); }
  .badge { color:#fff; font-size:11px; padding:2px 8px; border-radius:10px; text-transform:capitalize; }
  .notes { font-size:12px; color:var(--muted); line-height:1.6; }
  .opp { background:var(--card); border:1px solid #e8eaed; border-radius:6px; padding:14px 16px; margin-bottom:10px; }
  .opp-head { display:flex; justify-content:space-between; align-items:baseline; margin-bottom:4px; }
  .opp-title { font-weight:600; font-size:14px; }
  .opp-save { color:#ff4e42; font-weight:700; font-size:14px; white-space:nowrap; margin-left:12px; }
  .opp-items { margin:8px 0 0; padding-left:18px; }
  .dot { display:inline-block; width:9px; height:9px; border-radius:50%; margin-right:8px; }
  .audit-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(240px,1fr)); gap:16px; }
  .audit-cat h3 { font-size:13px; margin:0 0 8px; }
  .audit-cat td { font-size:12px; }
  .audit-cat tr.pass .mark { color:#0cce6b; }
  .audit-cat tr.fail .mark { color:#ff4e42; }
  .audit-cat td.mark { width:22px; text-align:center; font-weight:700; }
  .filmstrip { display:flex; gap:8px; overflow-x:auto; padding-bottom:8px; }
  .film-label { margin-bottom:6px; }
  .frame { margin:0; flex:0 0 auto; text-align:center; }
  .frame img { height:150px; border:1px solid #e8eaed; border-radius:4px; display:block; }
  .frame figcaption { font-size:11px; color:var(--muted); margin-top:4px; }
  .screenshot { margin:16px 0 0; }
  .shot-wrap { position:relative; display:inline-block; max-width:100%; }
  .shot-wrap img { max-width:100%; border:1px solid #e8eaed; border-radius:4px; display:block; }
  .lcp-box { position:absolute; border:3px solid #ff4e42; background:rgba(255,78,66,.12); box-shadow:0 0 0 2px rgba(255,255,255,.5); }
  .lcp-key { display:inline-block; width:10px; height:10px; border:2px solid #ff4e42; vertical-align:middle; }
  .page-card { background:var(--card); border:1px solid #e8eaed; border-radius:8px; padding:18px 20px; margin-bottom:16px; }
  .page-head { display:flex; justify-content:space-between; align-items:baseline; gap:12px; margin-bottom:2px; }
  .page-url { font-size:13px; font-weight:600; }
  .page-count { font-size:12px; color:var(--muted); white-space:nowrap; }
  .page-note { margin-bottom:14px; }
  .page-extra { margin-top:12px; }
  .page-runs { margin-top:12px; }
  .page-runs summary { cursor:pointer; }
  .page-runs table { margin-top:10px; }
  .session-detail > summary { cursor:pointer; font-size:14px; font-weight:600; padding:14px 0;
                             border-top:2px solid #e8eaed; }
  .stats { display:flex; gap:32px; flex-wrap:wrap; }
  .stat .n { font-size:28px; font-weight:700; }
  .stat .l { font-size:12px; color:var(--muted); }
  footer { text-align:center; color:var(--muted); font-size:12px; padding:20px; }
"""


# --------------------------------------------------------------------------- #
# Single-session report
# --------------------------------------------------------------------------- #
def render_html_report(result: dict) -> str:
    generated_at = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(result.get("generated_at", time.time())))
    # With more than one page load in the session, lead with the per-page
    # averages so repeated loads of the same URL read as one result.
    pages = build_page_aggregates([result]) if len(result.get("page_results") or []) > 1 else []
    pages_html = render_pages_section(pages) if pages else ""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Performance Report - {escape(str(result.get('label', '')))}</title>
<style>{_SHARED_CSS}</style>
</head>
<body>
<header>
  <h1>Web Performance Report</h1>
  <div class="sub">{escape(str(result.get('label', '')))} &middot; generated {generated_at}</div>
</header>
<main>
{pages_html}
{render_session_detail(result)}
</main>
<footer>Generated by webperf_monitor - a CDP-based Python performance monitor</footer>
</body>
</html>"""


def write_html_report(result: dict, output_dir: str, filename: str = "report.html") -> str:
    os.makedirs(output_dir, exist_ok=True)
    path = os.path.join(output_dir, filename)
    with open(path, "w", encoding="utf-8") as f:
        f.write(render_html_report(result))
    return path


def write_reports(result: dict, output_dir: str) -> dict:
    """Convenience: writes both report.json and report.html, returns their paths."""
    pages = build_page_aggregates([result])
    payload = dict(result, pages=pages) if pages else result
    return {
        "json": write_json_report(payload, output_dir),
        "html": write_html_report(result, output_dir),
    }


# --------------------------------------------------------------------------- #
# Consolidated report (all sessions the watcher saw during a test run)
# --------------------------------------------------------------------------- #
def build_consolidated_result(results: list) -> dict:
    """Merge per-session result dicts into a single consolidated result."""
    # Averages over every measured PAGE LOAD, grouped per URL. This is the
    # authoritative view: a URL loaded five times appears once, with its metrics
    # averaged, instead of five unrelated score cards.
    pages = build_page_aggregates(results)

    # Overall score: mean over all page loads (falls back to the per-session
    # scores when no per-page data was captured).
    run_scores = [run.get("performance_score")
                  for page in pages for run in page.get("runs", [])
                  if run.get("performance_score") is not None]
    if not run_scores:
        run_scores = [r.get("performance_score") for r in results
                      if r.get("performance_score") is not None]
    avg_score = round(sum(run_scores) / len(run_scores)) if run_scores else None

    page_loads: list[dict] = []
    for r in results:
        label = r.get("label", "session")
        for pl in r.get("page_loads", []):
            page_loads.append({
                "url": pl.get("url"),
                "load_time_ms": pl.get("load_time_ms"),
                "dom_content_loaded_ms": pl.get("dom_content_loaded_ms"),
                "response_end_ms": pl.get("response_end_ms"),
                "session": label,
            })
    page_loads.sort(key=lambda p: (p.get("load_time_ms") is None, -(p.get("load_time_ms") or 0)))

    # Average category scores over every measured page load (per-session values
    # are the fallback for sessions with no per-page data).
    all_runs = [run for page in pages for run in page.get("runs", [])] or results
    category_averages: dict[str, int] = {}
    for key in _CATEGORY_LABELS:
        vals = [((r.get("categories") or {}).get(key) or {}).get("score")
                for r in all_runs]
        vals = [v for v in vals if v is not None]
        if vals:
            category_averages[key] = round(sum(vals) / len(vals))

    total_console = sum(len(r.get("console_messages", [])) for r in results)
    total_failed = sum(len(r.get("failed_requests", [])) for r in results)

    return {
        "label": f"consolidated - {len(results)} session(s)",
        "generated_at": time.time(),
        "session_count": len(results),
        "performance_score": avg_score,
        "category_averages": category_averages,
        "pages": pages,
        "page_count": len(pages),
        "measured_page_loads": sum(p.get("samples", 0) for p in pages),
        "total_urls": len(page_loads),
        "total_console_messages": total_console,
        "total_failed_requests": total_failed,
        "page_loads": page_loads,
        "sessions": results,
    }


def _consolidated_url_rows(page_loads: list) -> str:
    rows = ""
    for pl in page_loads:
        rows += f"""
        <tr>
          <td class="mono small">{escape(str(pl.get('url') or ''))}</td>
          <td>{_fmt_ms(pl.get('load_time_ms'))}</td>
          <td>{_fmt_ms(pl.get('dom_content_loaded_ms'))}</td>
          <td class="small">{escape(str(pl.get('session') or ''))}</td>
        </tr>"""
    if not rows:
        rows = "<tr><td colspan='4'>No page loads were captured</td></tr>"
    return rows


def _consolidated_session_rows(results: list) -> str:
    rows = ""
    for r in results:
        m = r.get("metrics", {})
        score = r.get("performance_score")
        cls_val = m.get("cumulative_layout_shift")
        rows += f"""
        <tr>
          <td>{escape(str(r.get('label', '')))}</td>
          <td style="color:{_score_color(score)};font-weight:700">{score if score is not None else 'N/A'}</td>
          <td>{_fmt_ms(m.get('first_contentful_paint_ms'))}</td>
          <td>{_fmt_ms(m.get('speed_index_ms')) if m.get('speed_index_ms') is not None else 'N/A'}</td>
          <td>{_fmt_ms(m.get('largest_contentful_paint_ms'))}</td>
          <td>{_fmt_ms(m.get('total_blocking_time_ms'))}</td>
          <td>{'N/A' if cls_val is None else f'{cls_val:.3f}'}</td>
          <td>{_fmt_ms(m.get('interaction_to_next_paint_ms')) if m.get('interaction_to_next_paint_ms') is not None else 'N/A'}</td>
          <td>{len(r.get('page_loads', []))}</td>
        </tr>"""
    if not rows:
        rows = "<tr><td colspan='9'>No sessions captured</td></tr>"
    return rows


def render_consolidated_html(consolidated: dict) -> str:
    score = consolidated.get("performance_score")
    generated_at = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(consolidated.get("generated_at", time.time())))
    url_rows = _consolidated_url_rows(consolidated.get("page_loads", []))
    session_rows = _consolidated_session_rows(consolidated.get("sessions", []))

    cat_avgs = consolidated.get("category_averages", {})
    overview_donuts = [_donut("Performance", score)]
    for key, label in _CATEGORY_LABELS.items():
        overview_donuts.append(_donut(label, cat_avgs.get(key)))

    pages = consolidated.get("pages", [])
    measured = consolidated.get("measured_page_loads", 0)

    # Per-session detail sections, collapsed: the averaged per-page section
    # above is what should be read first.
    details = ""
    for r in consolidated.get("sessions", []):
        details += f"""
  <details class="session-detail">
    <summary>Session detail: {escape(str(r.get('label', '')))}</summary>
    {render_session_detail(r)}
  </details>"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Consolidated Performance Report</title>
<style>{_SHARED_CSS}</style>
</head>
<body>
<header>
  <h1>Consolidated Web Performance Report</h1>
  <div class="sub">{consolidated.get('session_count', 0)} browser session(s) &middot; generated {generated_at}</div>
</header>
<main>
  <section>
    <h2>Overview (averages across all measured page loads)</h2>
    <div class="cat-row">{"".join(overview_donuts)}</div>
    <div class="stats" style="margin-top:20px">
      <div class="stat"><div class="n">{len(pages)}</div><div class="l">Distinct pages</div></div>
      <div class="stat"><div class="n">{measured}</div><div class="l">Page loads measured</div></div>
      <div class="stat"><div class="n">{consolidated.get('session_count', 0)}</div><div class="l">Sessions</div></div>
      <div class="stat"><div class="n">{consolidated.get('total_console_messages', 0)}</div><div class="l">Console errors/warnings</div></div>
      <div class="stat"><div class="n">{consolidated.get('total_failed_requests', 0)}</div><div class="l">Failed requests</div></div>
    </div>
  </section>

  {render_pages_section(pages)}

  <section>
    <h2>Sessions</h2>
    <table>
      <thead><tr>
        <th>Session</th><th>Perf</th><th>FCP</th><th>SI</th><th>LCP</th><th>TBT</th><th>CLS</th><th>INP</th><th>URLs</th>
      </tr></thead>
      <tbody>{session_rows}</tbody>
    </table>
  </section>

  <section>
    <h2>All URL Load Times, Per Session ({consolidated.get('total_urls', 0)})</h2>
    <table>
      <thead><tr><th>URL</th><th>Load Time</th><th>DOMContentLoaded</th><th>Session</th></tr></thead>
      <tbody>{url_rows}</tbody>
    </table>
  </section>

  {details}
</main>
<footer>Generated by webperf_monitor - a CDP-based Python performance monitor</footer>
</body>
</html>"""


def write_consolidated_report(results: list, output_dir: str) -> dict:
    """
    Build and write ONE consolidated report (report.json + report.html) covering
    every session seen during the run. Returns the written paths plus the
    consolidated result dict under the "result" key.
    """
    consolidated = build_consolidated_result(results)
    os.makedirs(output_dir, exist_ok=True)
    json_path = os.path.join(output_dir, "report.json")
    html_path = os.path.join(output_dir, "report.html")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(consolidated, f, indent=2, default=str)
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(render_consolidated_html(consolidated))
    return {"json": json_path, "html": html_path, "result": consolidated}
