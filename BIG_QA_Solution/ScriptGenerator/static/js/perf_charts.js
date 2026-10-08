/*
 * perf_charts.js — the chart marks of the Performance Dashboard.
 *
 * Plain SVG on purpose: the app runs on machines that may be offline or behind
 * a proxy, so a chart library from a CDN would be the one thing on the page
 * that fails to load. Two forms cover the dashboard — a multi-series line
 * chart and a column chart — and both are built from the same axis, tooltip
 * and table-view plumbing below.
 *
 * Conventions that every chart here keeps to:
 *   • Series colours come from CSS custom properties (--viz-1…), applied through
 *     inline `style` rather than SVG presentation attributes, so the light/dark
 *     theme toggle re-colours a rendered chart with no redraw.
 *   • Marks are thin: 2px lines, ≤24px columns with a 4px rounded data end,
 *     hairline solid gridlines, and a 2px surface ring on any dot that can
 *     overlap another.
 *   • Reading a value never depends on hovering. Every chart renders a
 *     "Table view" twin underneath it, and the crosshair is reachable from the
 *     keyboard with the arrow keys.
 */

(function (global) {
    'use strict';

    const SVG_NS = 'http://www.w3.org/2000/svg';

    // Room for the axis labels. A chart's box has to include its x-axis band,
    // otherwise the card grows a private scrollbar around the labels.
    const PAD = { top: 16, right: 18, bottom: 30, left: 54 };
    const MAX_COLUMN_WIDTH = 24;   // marks stay thin
    const COLUMN_GAP = 2;          // the surface gap that separates neighbours
    // A column never fills its band. At 30 samples a band is ~20px wide, and a
    // column that takes all of it turns the series into one saturated block.
    const COLUMN_BAND_SHARE = 0.62;
    const MIN_HIT_WIDTH = 24;      // a hit target is never just the painted pixels

    function svg(name, attrs, parent) {
        const node = document.createElementNS(SVG_NS, name);
        for (const key in attrs) {
            if (attrs[key] === null || attrs[key] === undefined) continue;
            node.setAttribute(key, attrs[key]);
        }
        if (parent) parent.appendChild(node);
        return node;
    }

    function html(tag, className, parent) {
        const node = document.createElement(tag);
        if (className) node.className = className;
        if (parent) parent.appendChild(node);
        return node;
    }

    // ── Formatters ──────────────────────────────────────────────────────────
    const fmt = {
        compact(value) {
            if (value === null || value === undefined || isNaN(value)) return '—';
            const abs = Math.abs(value);
            if (abs >= 1e9) return (value / 1e9).toFixed(abs >= 1e10 ? 0 : 1) + 'B';
            if (abs >= 1e6) return (value / 1e6).toFixed(abs >= 1e7 ? 0 : 1) + 'M';
            if (abs >= 1e4) return (value / 1e3).toFixed(0) + 'K';
            return fmt.number(value);
        },
        number(value, digits) {
            if (value === null || value === undefined || isNaN(value)) return '—';
            const places = digits === undefined ? (Math.abs(value) < 10 && value % 1 !== 0 ? 1 : 0) : digits;
            return Number(value).toLocaleString(undefined, {
                minimumFractionDigits: places, maximumFractionDigits: places
            });
        },
        ms(value) {
            if (value === null || value === undefined || isNaN(value)) return '—';
            if (value >= 10000) return fmt.number(value / 1000, 1) + ' s';
            return fmt.number(value, value < 10 ? 1 : 0) + ' ms';
        },
        rate(value) {
            if (value === null || value === undefined || isNaN(value)) return '—';
            return fmt.number(value, value < 10 ? 2 : 1) + '/s';
        },
        pct(value, digits) {
            if (value === null || value === undefined || isNaN(value)) return '—';
            return fmt.number(value, digits === undefined ? 2 : digits) + '%';
        },
        signedPct(value) {
            if (value === null || value === undefined || isNaN(value)) return '—';
            const sign = value > 0 ? '+' : '';
            return sign + fmt.number(value, 1) + '%';
        },
        bytes(value) {
            if (value === null || value === undefined || isNaN(value)) return '—';
            if (value >= 1048576) return fmt.number(value / 1048576, 2) + ' MB';
            if (value >= 1024) return fmt.number(value / 1024, 1) + ' KB';
            return fmt.number(value, 0) + ' B';
        },
        clock(seconds) {
            if (seconds === null || seconds === undefined || isNaN(seconds)) return '—';
            const total = Math.max(0, Math.round(seconds));
            const mins = Math.floor(total / 60);
            const secs = total % 60;
            return mins + ':' + String(secs).padStart(2, '0');
        }
    };

    // ── Scales & ticks ──────────────────────────────────────────────────────

    /** Round tick steps to 1 / 2 / 5 × 10ⁿ so the axis reads in clean numbers. */
    function niceTicks(min, max, count) {
        if (!isFinite(min) || !isFinite(max)) return [0, 1];
        if (min === max) {
            if (min === 0) return [0, 1];
            const pad = Math.abs(min) * 0.5;
            min -= pad;
            max += pad;
        }
        const raw = (max - min) / Math.max(1, count);
        const magnitude = Math.pow(10, Math.floor(Math.log10(raw)));
        const normalized = raw / magnitude;
        const step = (normalized <= 1 ? 1 : normalized <= 2 ? 2 : normalized <= 5 ? 5 : 10) * magnitude;
        const start = Math.floor(min / step) * step;
        const end = Math.ceil(max / step) * step;
        const ticks = [];
        // Guard the loop against a step that floating point made non-advancing.
        for (let value = start; value <= end + step / 2 && ticks.length < 40; value += step) {
            ticks.push(Math.abs(value) < step / 1e6 ? 0 : value);
        }
        return ticks;
    }

    function plotBox(width, height) {
        return {
            x: PAD.left,
            y: PAD.top,
            width: Math.max(10, width - PAD.left - PAD.right),
            height: Math.max(10, height - PAD.top - PAD.bottom)
        };
    }

    function drawFrame(root, box, yTicks, yFormat, xEntries) {
        // Gridlines and the axis rule are hairline, solid and one step off the
        // surface — present enough to read a value against, quiet enough to
        // stay behind the data.
        const span = yTicks[yTicks.length - 1] - yTicks[0] || 1;
        yTicks.forEach(tick => {
            const y = box.y + box.height - ((tick - yTicks[0]) / span) * box.height;
            svg('line', {
                x1: box.x, x2: box.x + box.width, y1: y, y2: y,
                'stroke-width': 1, 'shape-rendering': 'crispEdges',
                style: 'stroke:var(--viz-grid)'
            }, root);
            const label = svg('text', {
                x: box.x - 8, y: y + 4, 'text-anchor': 'end',
                style: 'fill:var(--viz-muted);font-size:11px;font-variant-numeric:tabular-nums'
            }, root);
            label.textContent = yFormat(tick);
        });
        svg('line', {
            x1: box.x, x2: box.x + box.width,
            y1: box.y + box.height, y2: box.y + box.height,
            'stroke-width': 1, 'shape-rendering': 'crispEdges',
            style: 'stroke:var(--viz-axis)'
        }, root);

        (xEntries || []).forEach(entry => {
            const label = svg('text', {
                x: entry.x, y: box.y + box.height + 18, 'text-anchor': 'middle',
                style: 'fill:var(--viz-muted);font-size:11px;font-variant-numeric:tabular-nums'
            }, root);
            label.textContent = entry.label;
        });
    }

    // ── Chrome shared by both forms ─────────────────────────────────────────

    function prepare(container) {
        container.innerHTML = '';
        container.classList.add('viz-root');
        const stage = html('div', 'viz-stage', container);
        const tooltip = html('div', 'viz-tooltip', stage);
        tooltip.setAttribute('role', 'status');
        return { stage, tooltip };
    }

    /**
     * A legend, always present from two series up. One series needs none — the
     * card's own title already says what is plotted.
     */
    function drawLegend(container, series) {
        if (series.length < 2) return;
        const legend = html('div', 'viz-legend', container);
        series.forEach(item => {
            const entry = html('span', 'viz-legend-item', legend);
            const key = html('span', 'viz-legend-key', entry);
            key.style.background = item.color;
            const text = html('span', null, entry);
            text.textContent = item.name;
        });
    }

    /**
     * The table twin. Every value a tooltip can show is also here, so nothing
     * on the page is reachable only by hovering.
     */
    function drawTableView(container, headers, rows) {
        const wrap = html('div', 'viz-table-wrap', container);
        const toggle = html('button', 'viz-table-toggle', wrap);
        toggle.type = 'button';
        toggle.setAttribute('aria-expanded', 'false');
        toggle.innerHTML = '<i class="fas fa-table-list"></i> Table view';

        const holder = html('div', 'viz-table', wrap);
        holder.hidden = true;
        const table = html('table', 'data-table viz-data-table', holder);
        const head = html('tr', null, html('thead', null, table));
        headers.forEach(header => {
            const cell = html('th', null, head);
            cell.textContent = header;
        });
        const body = html('tbody', null, table);
        rows.forEach(row => {
            const line = html('tr', null, body);
            row.forEach((value, index) => {
                const cell = html('td', index ? 'viz-num' : null, line);
                cell.textContent = value;
            });
        });

        toggle.addEventListener('click', () => {
            holder.hidden = !holder.hidden;
            toggle.setAttribute('aria-expanded', String(!holder.hidden));
            toggle.innerHTML = holder.hidden
                ? '<i class="fas fa-table-list"></i> Table view'
                : '<i class="fas fa-xmark"></i> Hide table';
        });
    }

    function emptyState(container, message) {
        container.innerHTML = '';
        container.classList.add('viz-root');
        const empty = html('div', 'viz-empty', container);
        empty.innerHTML = '<i class="fas fa-chart-line"></i>';
        const text = html('p', null, empty);
        text.textContent = message;
    }

    function showTooltip(tooltip, stage, x, y) {
        tooltip.classList.add('visible');
        const bounds = stage.getBoundingClientRect();
        const box = tooltip.getBoundingClientRect();
        // Keep the readout inside the card rather than letting it hang over the
        // next panel.
        let left = x + 14;
        if (left + box.width > bounds.width) left = Math.max(4, x - box.width - 14);
        let top = y - box.height - 12;
        if (top < 0) top = y + 16;
        tooltip.style.left = left + 'px';
        tooltip.style.top = top + 'px';
    }

    function tooltipRows(tooltip, title, rows) {
        tooltip.innerHTML = '';
        const heading = html('div', 'viz-tip-title', tooltip);
        heading.textContent = title;
        rows.forEach(row => {
            const line = html('div', 'viz-tip-row', tooltip);
            const key = html('span', 'viz-tip-key', line);
            if (row.color) key.style.background = row.color;
            else key.style.visibility = 'hidden';
            // Values lead: the reader already knows the series and wants the number.
            const value = html('strong', 'viz-tip-value', line);
            value.textContent = row.value;
            const name = html('span', 'viz-tip-name', line);
            name.textContent = row.name;
        });
    }

    // ── Line chart ──────────────────────────────────────────────────────────
    /*
     * config = {
     *   series:   [{ name, color, values: [number|null] }],
     *   labels:   [string]            // one per x position (tick + tooltip title)
     *   xTickEvery: number            // thin out the x labels on a long run
     *   yFormat / valueFormat: fn
     *   endLabels: boolean            // direct-label the last point of each series
     *   empty:    string              // shown when there is nothing to plot
     * }
     */
    function lineChart(container, config) {
        const labels = config.labels || [];
        const series = (config.series || []).filter(s => (s.values || []).some(v => v !== null && v !== undefined));
        if (!labels.length || !series.length) {
            emptyState(container, config.empty || 'No data to plot yet.');
            return;
        }

        const { stage, tooltip } = prepare(container);
        const width = Math.max(320, stage.clientWidth || container.clientWidth || 640);
        const height = Math.max(200, config.height || 280);
        const box = plotBox(width, height);
        const yFormat = config.yFormat || fmt.compact;
        const valueFormat = config.valueFormat || yFormat;

        const flat = [];
        series.forEach(s => (s.values || []).forEach(v => {
            if (v !== null && v !== undefined && !isNaN(v)) flat.push(v);
        }));
        const lowest = Math.min(...flat);
        const highest = Math.max(...flat);
        // Response times and rates are read against zero, so the axis starts
        // there unless the data itself is negative (an indexed series can be).
        const yTicks = niceTicks(Math.min(0, lowest), highest, 4);
        const yFloor = yTicks[0];
        const ySpan = yTicks[yTicks.length - 1] - yFloor || 1;

        const root = svg('svg', {
            width: '100%', height: height, viewBox: `0 0 ${width} ${height}`,
            role: 'img', tabindex: '0', class: 'viz-svg'
        }, stage);
        if (config.title) {
            const title = svg('title', {}, root);
            title.textContent = config.title;
        }

        const step = labels.length > 1 ? box.width / (labels.length - 1) : 0;
        const xAt = index => labels.length > 1 ? box.x + index * step : box.x + box.width / 2;
        const yAt = value => box.y + box.height - ((value - yFloor) / ySpan) * box.height;

        const tickEvery = config.xTickEvery || Math.max(1, Math.ceil(labels.length / 7));
        const xEntries = labels.reduce((acc, label, index) => {
            if (index % tickEvery === 0 || index === labels.length - 1) {
                acc.push({ x: xAt(index), label: label });
            }
            return acc;
        }, []);
        drawFrame(root, box, yTicks, yFormat, xEntries);

        const crosshair = svg('line', {
            x1: 0, x2: 0, y1: box.y, y2: box.y + box.height,
            'stroke-width': 1, visibility: 'hidden',
            style: 'stroke:var(--viz-axis)'
        }, root);

        // Where two series converge at the right edge, their end labels would
        // sit on top of each other. Nudging them apart detaches a label from
        // its line, so the second one is dropped instead - the legend and the
        // tooltip still carry that series, and the table view its value.
        const placedLabels = [];
        const labelFits = y => {
            if (placedLabels.some(other => Math.abs(other - y) < 14)) return false;
            placedLabels.push(y);
            return true;
        };

        series.forEach(item => {
            // A gap in the data breaks the line rather than being bridged — a
            // drawn segment would assert a measurement that was never taken.
            let path = '';
            let open = false;
            (item.values || []).forEach((value, index) => {
                if (value === null || value === undefined || isNaN(value)) { open = false; return; }
                path += (open ? ' L' : ' M') + xAt(index) + ',' + yAt(value);
                open = true;
            });
            svg('path', {
                d: path.trim(), fill: 'none', 'stroke-width': 2,
                'stroke-linejoin': 'round', 'stroke-linecap': 'round',
                style: 'stroke:' + item.color
            }, root);

            const lastIndex = (item.values || []).reduce(
                (found, value, index) => (value === null || value === undefined || isNaN(value)) ? found : index, -1);
            if (lastIndex >= 0) {
                // The end dot carries a 2px ring in the surface colour so it
                // stays legible where two series cross.
                svg('circle', {
                    cx: xAt(lastIndex), cy: yAt(item.values[lastIndex]), r: 4,
                    'stroke-width': 2,
                    style: 'fill:' + item.color + ';stroke:var(--viz-surface)'
                }, root);
                if (config.endLabels && series.length <= 3
                        && labelFits(yAt(item.values[lastIndex]))) {
                    const label = svg('text', {
                        x: Math.min(xAt(lastIndex) + 9, box.x + box.width),
                        y: yAt(item.values[lastIndex]) - 9,
                        'text-anchor': xAt(lastIndex) > box.x + box.width - 44 ? 'end' : 'start',
                        style: 'fill:var(--viz-ink);font-size:11px;font-weight:600'
                    }, root);
                    label.textContent = valueFormat(item.values[lastIndex]);
                }
            }
        });

        const markers = series.map(item => svg('circle', {
            cx: 0, cy: 0, r: 4, visibility: 'hidden', 'stroke-width': 2,
            style: 'fill:' + item.color + ';stroke:var(--viz-surface)'
        }, root));

        function readAt(index) {
            crosshair.setAttribute('x1', xAt(index));
            crosshair.setAttribute('x2', xAt(index));
            crosshair.setAttribute('visibility', 'visible');
            const rows = [];
            series.forEach((item, position) => {
                const value = (item.values || [])[index];
                const marker = markers[position];
                if (value === null || value === undefined || isNaN(value)) {
                    marker.setAttribute('visibility', 'hidden');
                } else {
                    marker.setAttribute('cx', xAt(index));
                    marker.setAttribute('cy', yAt(value));
                    marker.setAttribute('visibility', 'visible');
                }
                rows.push({
                    color: item.color, name: item.name,
                    value: value === null || value === undefined || isNaN(value) ? '—' : valueFormat(value)
                });
            });
            // One tooltip lists every series at that x, so the pointer never
            // has to land on a 2px line to get a number.
            tooltipRows(tooltip, labels[index], rows);
            showTooltip(tooltip, stage, xAt(index), yAt(yTicks[yTicks.length - 1]));
        }

        function hide() {
            crosshair.setAttribute('visibility', 'hidden');
            markers.forEach(marker => marker.setAttribute('visibility', 'hidden'));
            tooltip.classList.remove('visible');
        }

        function indexFromEvent(event) {
            const bounds = root.getBoundingClientRect();
            const scale = bounds.width ? width / bounds.width : 1;
            const x = (event.clientX - bounds.left) * scale;
            if (labels.length === 1) return 0;
            return Math.min(labels.length - 1, Math.max(0, Math.round((x - box.x) / step)));
        }

        let focusIndex = labels.length - 1;
        root.addEventListener('pointermove', event => {
            focusIndex = indexFromEvent(event);
            readAt(focusIndex);
        });
        root.addEventListener('pointerleave', hide);
        root.addEventListener('focus', () => readAt(focusIndex));
        root.addEventListener('blur', hide);
        root.addEventListener('keydown', event => {
            // Keyboard reads the same crosshair the pointer does.
            if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') return;
            event.preventDefault();
            focusIndex = Math.min(labels.length - 1, Math.max(0,
                focusIndex + (event.key === 'ArrowRight' ? 1 : -1)));
            readAt(focusIndex);
        });

        drawLegend(container, series);
        drawTableView(container,
            [config.xHeader || 'Point'].concat(series.map(s => s.name)),
            labels.map((label, index) => [label].concat(series.map(s => {
                const value = (s.values || [])[index];
                return value === null || value === undefined || isNaN(value) ? '—' : valueFormat(value);
            }))));
    }

    // ── Column chart ────────────────────────────────────────────────────────
    /*
     * config = { labels, values, color, yFormat, valueFormat, xHeader,
     *            valueHeader, height, empty, align }
     *
     * `align: 'points'` centres each column on the x position a line chart of
     * the same labels would use. That is what lets a column chart sit directly
     * under a line chart and be read against the same x — the honest
     * alternative to plotting two different measures on two y-scales in one
     * frame, where the chosen alignment of the scales invents a correlation.
     */
    function columnChart(container, config) {
        const labels = config.labels || [];
        const values = config.values || [];
        if (!labels.length || !values.some(v => v !== null && v !== undefined && !isNaN(v))) {
            emptyState(container, config.empty || 'No data to plot yet.');
            return;
        }

        const { stage, tooltip } = prepare(container);
        const width = Math.max(320, stage.clientWidth || container.clientWidth || 640);
        const height = Math.max(160, config.height || 200);
        const box = plotBox(width, height);
        const yFormat = config.yFormat || fmt.compact;
        const valueFormat = config.valueFormat || yFormat;

        const present = values.filter(v => v !== null && v !== undefined && !isNaN(v));
        const yTicks = niceTicks(0, Math.max(...present), 3);
        const ySpan = yTicks[yTicks.length - 1] - yTicks[0] || 1;
        const baseline = box.y + box.height;

        const root = svg('svg', {
            width: '100%', height: height, viewBox: `0 0 ${width} ${height}`,
            role: 'img', class: 'viz-svg'
        }, stage);
        if (config.title) {
            const title = svg('title', {}, root);
            title.textContent = config.title;
        }

        const onPoints = config.align === 'points' && labels.length > 1;
        const band = onPoints ? box.width / (labels.length - 1) : box.width / labels.length;
        const centreAt = index => onPoints
            ? box.x + band * index
            : box.x + band * index + band / 2;
        const barWidth = Math.max(2, Math.min(MAX_COLUMN_WIDTH, band - COLUMN_GAP, band * COLUMN_BAND_SHARE));
        const tickEvery = config.xTickEvery || Math.max(1, Math.ceil(labels.length / 7));
        const xEntries = labels.reduce((acc, label, index) => {
            if (index % tickEvery === 0 || index === labels.length - 1) {
                acc.push({ x: centreAt(index), label: label });
            }
            return acc;
        }, []);
        drawFrame(root, box, yTicks, yFormat, xEntries);

        labels.forEach((label, index) => {
            const value = values[index];
            const centre = centreAt(index);
            const drawn = (value === null || value === undefined || isNaN(value)) ? 0 : value;
            const barHeight = Math.max(0, (drawn / ySpan) * box.height);
            const top = baseline - barHeight;

            if (barHeight > 0) {
                const radius = Math.min(4, barWidth / 2, barHeight);
                // A 4px rounded data end, square where it meets the baseline.
                svg('path', {
                    d: `M${centre - barWidth / 2},${baseline}`
                        + ` V${top + radius}`
                        + ` Q${centre - barWidth / 2},${top} ${centre - barWidth / 2 + radius},${top}`
                        + ` H${centre + barWidth / 2 - radius}`
                        + ` Q${centre + barWidth / 2},${top} ${centre + barWidth / 2},${top + radius}`
                        + ` V${baseline} Z`,
                    style: 'fill:' + config.color
                }, root);
            }

            // The hit area is wider than the mark and covers the whole band, so
            // a zero-height column is still hoverable.
            const hit = svg('rect', {
                x: centre - Math.max(barWidth, MIN_HIT_WIDTH) / 2, y: box.y,
                width: Math.max(barWidth, MIN_HIT_WIDTH), height: box.height,
                tabindex: '0', class: 'viz-hit',
                style: 'fill:transparent'
            }, root);
            const lift = svg('rect', {
                x: centre - barWidth / 2, y: top, width: barWidth,
                height: Math.max(barHeight, 1), visibility: 'hidden',
                style: 'fill:var(--viz-ink);opacity:0.14'
            }, root);

            function read() {
                lift.setAttribute('visibility', 'visible');
                tooltipRows(tooltip, label, [{
                    color: config.color,
                    name: config.valueHeader || 'Value',
                    value: (value === null || value === undefined || isNaN(value)) ? '—' : valueFormat(value)
                }]);
                showTooltip(tooltip, stage, centre, top);
            }
            function clear() {
                lift.setAttribute('visibility', 'hidden');
                tooltip.classList.remove('visible');
            }
            hit.addEventListener('pointermove', read);
            hit.addEventListener('pointerleave', clear);
            hit.addEventListener('focus', read);
            hit.addEventListener('blur', clear);
        });

        drawTableView(container,
            [config.xHeader || 'Point', config.valueHeader || 'Value'],
            labels.map((label, index) => {
                const value = values[index];
                return [label, (value === null || value === undefined || isNaN(value)) ? '—' : valueFormat(value)];
            }));
    }

    /**
     * Re-render on resize, keeping the frame while it happens.
     *
     * The charts are sized in pixels from their container, so a window or
     * sidebar resize has to redraw them. Renders are coalesced to one per
     * frame — a drag across the window edge fires this continuously.
     */
    function responsive(container, render) {
        // A chart is redrawn on every filter change, so the previous watcher is
        // dropped first - otherwise each reload leaves another observer behind
        // and one resize redraws the same chart many times over.
        if (container.vizObserver) {
            container.vizObserver.disconnect();
            container.vizObserver = null;
        }
        let frame = null;
        let lastWidth = 0;
        const run = () => {
            frame = null;
            lastWidth = container.clientWidth;
            render();
        };
        run();
        if (typeof ResizeObserver === 'undefined') return;
        const observer = new ResizeObserver(() => {
            if (!container.clientWidth || container.clientWidth === lastWidth) return;
            if (frame) cancelAnimationFrame(frame);
            frame = requestAnimationFrame(run);
        });
        observer.observe(container);
        container.vizObserver = observer;
    }

    global.PerfCharts = { line: lineChart, columns: columnChart, fmt, responsive, emptyState };
})(window);
