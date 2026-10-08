"""
Turns raw diagnostics (in-page DOM/timing data + network metadata) into
Lighthouse-style "Opportunities & Savings" and computes a visual Speed Index
from screencast filmstrip frames.

All heuristics here are approximations - they mirror the *shape* of
Lighthouse's opportunity audits (render-blocking resources, oversized images,
next-gen formats, text compression, slow TTFB, unminified assets) without
running Lighthouse itself. Savings figures are estimates, labelled as such in
the report.
"""

from __future__ import annotations

import base64
import io
from typing import Optional

_COMPRESSIBLE_HINTS = ("text/", "javascript", "json", "xml", "svg", "css")
_ALREADY_COMPRESSED = ("gzip", "br", "deflate", "zstd", "compress")


def build_opportunities(diagnostics: dict, resources: dict) -> list:
    """
    diagnostics: parsed COLLECT_DIAGNOSTICS_EXPR output.
    resources: url -> {"transfer_size", "mime", "content_encoding", "type",
                       "unminified", "minify_savings"} built from network events.
    Returns a list of opportunity dicts sorted by estimated impact.
    """
    opps: list[dict] = []

    # 1. Render-blocking resources ---------------------------------------- #
    rb = diagnostics.get("render_blocking") or []
    if rb:
        blocking_ms = sum((r.get("duration_ms") or 0) for r in rb)
        savings = int(min(blocking_ms, 4000) * 0.5)
        opps.append({
            "id": "render-blocking-resources",
            "title": "Eliminate render-blocking resources",
            "detail": f"{len(rb)} resource(s) block first paint. Inline critical "
                      f"CSS and defer non-critical CSS/JS.",
            "savings_ms": savings,
            "items": [{"url": r.get("url"), "note": _ms(r.get("duration_ms"))} for r in rb[:10]],
        })

    # 2. Properly size images --------------------------------------------- #
    sized_items, sized_bytes = [], 0
    for im in diagnostics.get("images") or []:
        nat, disp = im.get("natural", {}), im.get("displayed", {})
        dpr = im.get("dpr", 1) or 1
        natural_area = (nat.get("w", 0) or 0) * (nat.get("h", 0) or 0)
        displayed_area = (disp.get("w", 0) or 0) * (disp.get("h", 0) or 0) * (dpr * dpr)
        if natural_area > 0 and displayed_area > 0 and natural_area > displayed_area * 1.5:
            size = (resources.get(im.get("url")) or {}).get("transfer_size") or 0
            if size:
                waste = int(size * (1 - displayed_area / natural_area))
                if waste > 2048:
                    sized_bytes += waste
                    sized_items.append({"url": im.get("url"), "note": _bytes(waste)})
    if sized_bytes:
        opps.append({
            "id": "uses-responsive-images",
            "title": "Properly size images",
            "detail": "Serve images no larger than they are displayed to save cellular data.",
            "savings_bytes": sized_bytes,
            "items": sized_items[:10],
        })

    # 3. Next-gen image formats ------------------------------------------- #
    nextgen_items, nextgen_bytes = [], 0
    for url, meta in resources.items():
        mime = (meta.get("mime") or "").lower()
        if mime in ("image/jpeg", "image/jpg", "image/png"):
            size = meta.get("transfer_size") or 0
            if size > 4096:
                waste = int(size * 0.3)
                nextgen_bytes += waste
                nextgen_items.append({"url": url, "note": _bytes(waste)})
    if nextgen_bytes:
        opps.append({
            "id": "modern-image-formats",
            "title": "Serve images in next-gen formats (WebP/AVIF)",
            "detail": "WebP/AVIF typically reduce image transfer size by ~30% over JPEG/PNG.",
            "savings_bytes": nextgen_bytes,
            "items": nextgen_items[:10],
        })

    # 4. Enable text compression ------------------------------------------ #
    comp_items, comp_bytes = [], 0
    for url, meta in resources.items():
        mime = (meta.get("mime") or "").lower()
        enc = (meta.get("content_encoding") or "").lower()
        size = meta.get("transfer_size") or 0
        compressible = any(h in mime for h in _COMPRESSIBLE_HINTS)
        if size > 2048 and compressible and not any(a in enc for a in _ALREADY_COMPRESSED):
            waste = int(size * 0.6)
            comp_bytes += waste
            comp_items.append({"url": url, "note": _bytes(waste)})
    if comp_bytes:
        opps.append({
            "id": "uses-text-compression",
            "title": "Enable text compression",
            "detail": "Serve text-based assets with gzip or brotli to minimize bytes transferred.",
            "savings_bytes": comp_bytes,
            "items": comp_items[:10],
        })

    # 5. Reduce server response time (TTFB) ------------------------------- #
    ttfb = diagnostics.get("ttfb_ms") or diagnostics.get("server_response_ms")
    if ttfb and ttfb > 600:
        opps.append({
            "id": "server-response-time",
            "title": "Reduce initial server response time (TTFB)",
            "detail": f"The root document's time-to-first-byte was {int(ttfb)} ms "
                      f"(target < 600 ms).",
            "savings_ms": int(ttfb - 600),
            "items": [],
        })

    # 6. Minify CSS / JavaScript (best-effort, from response-body sampling) - #
    min_items, min_bytes = [], 0
    for url, meta in resources.items():
        if meta.get("unminified") and (meta.get("minify_savings") or 0) > 2048:
            waste = int(meta["minify_savings"])
            min_bytes += waste
            min_items.append({"url": url, "note": _bytes(waste)})
    if min_bytes:
        opps.append({
            "id": "unminified-assets",
            "title": "Minify CSS & JavaScript",
            "detail": "Removing whitespace and comments from text assets reduces their size.",
            "savings_bytes": min_bytes,
            "items": min_items[:10],
        })

    # Sort by rough impact: 1KB ~= 1ms for ranking purposes.
    opps.sort(key=lambda o: -(o.get("savings_ms", 0) + (o.get("savings_bytes", 0) / 1024)))
    return opps


def build_diagnostic_items(diagnostics: dict) -> list:
    """Non-actionable "here's what's going on" facts (mirrors Lighthouse's
    Diagnostics section): DOM size, TTFB, render-blocking count, LCP element."""
    items = []
    dom = diagnostics.get("dom") or {}
    if dom:
        items.append({
            "id": "dom-size",
            "title": "Avoid an excessive DOM size",
            "value": f"{dom.get('nodes', 0)} nodes, depth {dom.get('max_depth', 0)}, "
                     f"max {dom.get('max_children', 0)} children",
            "severity": "warn" if dom.get("nodes", 0) > 1500 else "ok",
        })
    ttfb = diagnostics.get("ttfb_ms")
    if ttfb is not None:
        items.append({
            "id": "ttfb",
            "title": "Time to First Byte (server response)",
            "value": f"{int(ttfb)} ms",
            "severity": "warn" if ttfb > 600 else "ok",
        })
    rb = diagnostics.get("render_blocking") or []
    items.append({
        "id": "render-blocking",
        "title": "Render-blocking requests",
        "value": f"{len(rb)} resource(s)",
        "severity": "warn" if rb else "ok",
    })
    lcp_el = diagnostics.get("lcp_element")
    if lcp_el:
        sel = lcp_el.get("tag") or "element"
        if lcp_el.get("id"):
            sel += f"#{lcp_el['id']}"
        elif lcp_el.get("cls"):
            sel += "." + ".".join(str(lcp_el["cls"]).split()[:2])
        items.append({
            "id": "lcp-element",
            "title": "Largest Contentful Paint element",
            "value": sel,
            "severity": "ok",
        })
    return items


def compute_speed_index(frames: list) -> Optional[float]:
    """
    Approximate the visual Speed Index (ms) from screencast filmstrip frames.

    frames: list of {"data": <base64 jpeg>, "ts": <epoch seconds>}, in order.

    Speed Index = integral over time of (1 - visualCompleteness). Visual
    completeness of each frame is estimated as its similarity to the final
    frame, using a small greyscale-histogram comparison. Requires Pillow; if it
    isn't installed (or there are too few frames) returns None and the metric
    is reported as "not measured", exactly as before.
    """
    if not frames or len(frames) < 2:
        return None
    try:
        from PIL import Image  # optional dependency
    except Exception:
        return None

    hists = []
    for f in frames:
        try:
            img = Image.open(io.BytesIO(base64.b64decode(f["data"]))).convert("L").resize((64, 64))
            hists.append((f.get("ts", 0), img.histogram()))
        except Exception:
            continue
    if len(hists) < 2:
        return None

    final = hists[-1][1]
    start_hist = hists[0][1]

    def distance(h):
        return sum(abs(a - b) for a, b in zip(h, final))

    baseline = distance(start_hist) or 1

    def completeness(h):
        return max(0.0, min(1.0, 1 - distance(h) / baseline))

    t0 = hists[0][0]
    speed_index = 0.0
    prev_t = t0
    prev_c = completeness(start_hist)
    for ts, h in hists[1:]:
        dt_ms = max(0.0, (ts - prev_t) * 1000.0)
        speed_index += (1 - prev_c) * dt_ms
        prev_t = ts
        prev_c = completeness(h)

    return round(speed_index, 1) if speed_index > 0 else None


def analyze_minification(body: str) -> tuple[bool, float]:
    """
    Cheap heuristic for whether a CSS/JS source looks unminified, plus the
    fraction of bytes that could plausibly be saved by minifying. Returns
    (is_unminified, savings_fraction).
    """
    if not body:
        return False, 0.0
    length = len(body)
    # Collapse runs of whitespace and strip line breaks - a rough proxy for
    # what a minifier removes.
    stripped = " ".join(body.split())
    savings_fraction = (length - len(stripped)) / length if length else 0.0
    newline_density = body.count("\n") / length if length else 0.0
    is_unminified = savings_fraction > 0.12 and newline_density > 0.005
    return is_unminified, savings_fraction


def _bytes(n) -> str:
    n = n or 0
    for unit in ("B", "KB", "MB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def _ms(v) -> str:
    if v is None:
        return "N/A"
    return f"{v / 1000:.2f} s" if v >= 1000 else f"{v:.0f} ms"
