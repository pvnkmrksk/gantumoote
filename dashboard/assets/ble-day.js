// SPDX-License-Identifier: AGPL-3.0-or-later
"use strict";

const chart = document.querySelector("[data-report-events]");
if (chart) {
    fetch(chart.dataset.reportEvents, {credentials: "same-origin"})
        .then(response => response.ok ? response.json() : null)
        .then(data => { if (data?.series) drawDay(chart, data); })
        .catch(() => { /* Keep the PNG fallback if the interactive chart fails. */ });
}

const METRICS = [
    {key: "hits", title: "BLE receptions", unit: "per 5 min", step: 5},
    {key: "gaps", title: "Packet gaps", unit: "seconds · log scale", step: 1},
    {key: "rssi", title: "Signal strength", unit: "dBm", step: 5},
    {key: "temp", title: "Temperature", unit: "°C", step: 0.5},
];
const MIN_SPAN = 30000;
const DARK_COLORS = ["#7ec5ff", "#ffae75", "#d9a2ef", "#64d7aa", "#f38eae", "#d5d786"];
const clamp = (n, low, high) => Math.min(high, Math.max(low, n));
const isoTime = value => value.slice(11).match(/^[\d:.]+/)?.[0] || value;
const offsetText = offset => (offset > 0 ? "+" : "") + offset.toFixed(3) + " s";

function scaleMetric(metric, points) {
    const transform = metric.key === "gaps" ? Math.log2 : value => value;
    let low = metric.key === "hits" ? 0 : Infinity;
    let high = -Infinity;
    for (const [, value] of points) {
        const y = transform(value);
        if (!Number.isFinite(y)) continue;
        low = Math.min(low, y);
        high = Math.max(high, y);
    }
    if (!Number.isFinite(high)) return null;
    const step = metric.step * Math.max(1, Math.ceil((high - low) / (3 * metric.step)));
    low = Math.floor(low / step) * step;
    high = Math.max(low + step, Math.ceil(high / step) * step);
    const ticks = [];
    for (let n = low; n <= high; n += step) {
        ticks.push([n, metric.key === "gaps" ? +Math.pow(2, n).toPrecision(2) : +n.toFixed(1)]);
    }
    return {transform, low, high, ticks};
}

function drawDay(root, data) {
    const canvas = root.querySelector("canvas");
    const context = canvas.getContext("2d");
    if (!context) return;
    const tooltip = root.querySelector(".day-report-tooltip");
    const range = root.querySelector(".day-chart-range");
    const tagButtons = root.querySelector(".day-chart-tags");
    const inspector = root.querySelector(".day-report-inspector");
    const select = inspector.querySelector("select");
    const result = inspector.querySelector(".day-report-result");
    const dark = matchMedia("(prefers-color-scheme: dark)");
    const start = Date.parse(data.start), end = Date.parse(data.end), through = Date.parse(data.through);
    const daySpan = end - start;
    const midnight = Date.parse(data.start.slice(0, 10) + "T00:00:00Z");
    const clock = (at, seconds = false) => at === end ? "24:00"
        : new Date(at + midnight - start).toISOString().slice(11, seconds ? 19 : 16);
    const visible = new Set(data.series.map(track => track.tag));
    const grouped = new Map();
    for (const event of data.events) {
        const at = Date.parse(event.observed);
        if (!grouped.has(at)) grouped.set(at, {at, observed: event.observed, events: []});
        grouped.get(at).events.push(event);
    }
    const reports = [...grouped.values()].sort((a, b) => a.at - b.at);
    const activityTimes = [
        ...reports.map(report => report.at),
        ...data.series.flatMap(track => {
            const points = [track.hits, track.gaps, track.rssi, track.temp];
            return points.filter(series => series.length).flatMap(series => [series[0][0], series.at(-1)[0]]);
        }),
    ];
    const first = activityTimes.length ? Math.min(...activityTimes) : start;
    const last = activityTimes.length ? Math.max(...activityTimes) : end;
    const margin = Math.max(5 * 60000, (last - first) * .05);
    const activitySpan = Math.min(daySpan, Math.max(20 * 60000, last - first + margin * 2));
    const activityLeft = clamp((first + last - activitySpan) / 2, start, end - activitySpan);
    let left = activityLeft, span = activitySpan, pinned = null, hovered = null;
    let size, pending, drag;
    const color = track => dark.matches ? DARK_COLORS[data.series.indexOf(track) % DARK_COLORS.length] : track.color;
    const theme = () => dark.matches
        ? {ink: "#edf2f7", muted: "#a9bbcc", grid: "#344454", shade: "#283745", guide: "#86a2b9"}
        : {ink: "#192638", muted: "#45576b", grid: "#e1e8ee", shade: "#f0f3f6", guide: "#516e84"};

    for (const track of data.series) {
        const button = document.createElement("button");
        button.type = "button";
        const receptions = track.hits.reduce((total, [, count]) => total + count, 0);
        const observations = data.events.filter(event => event.tag === track.tag).length;
        button.title = receptions + " Pi receptions · " + observations + " Find My observations";

        button.setAttribute("aria-pressed", "true");
        const swatch = document.createElement("span");
        swatch.style.backgroundColor = color(track);
        button.append(swatch, track.tag);
        button.addEventListener("click", () => {
            if (visible.has(track.tag)) visible.delete(track.tag);
            else visible.add(track.tag);
            button.setAttribute("aria-pressed", String(visible.has(track.tag)));
            updateChoices();
            redraw();
        });
        dark.addEventListener("change", () => { swatch.style.backgroundColor = color(track); });
        tagButtons.append(button);
    }

    root.hidden = false;
    document.querySelector(".day-static-chart").hidden = true;
    const from = (track, series) => track[series].filter(([at]) => at >= left && at <= left + span);
    const inView = at => at >= left && at <= left + span;
    const onReport = report => report.events.filter(event => visible.has(event.tag));
    function setRange(next, length) {
        span = clamp(length, MIN_SPAN, daySpan);
        left = clamp(next, start, end - span);
        hovered = null;
        tooltip.hidden = true;
        const rangeText = clock(left, span < 5 * 60000) + "–" + clock(left + span, span < 5 * 60000) + " · Pi time";
        if (range.textContent !== rangeText) range.textContent = rangeText;
        const round = (a, b) => Math.abs(a - b) < 1;
        root.querySelector('[data-chart="activity"]').setAttribute("aria-pressed",
            String(round(left, activityLeft) && round(span, activitySpan)));
        root.querySelector('[data-chart="all"]').setAttribute("aria-pressed", String(round(span, daySpan)));
        root.querySelector('[data-chart="in"]').disabled = span <= MIN_SPAN;
        root.querySelector('[data-chart="out"]').disabled = span >= daySpan;
        root.querySelector('[data-chart="earlier"]').disabled = left <= start;
        root.querySelector('[data-chart="later"]').disabled = left + span >= end;
        redraw();
    }
    function zoom(factor, at = .5) {
        const length = clamp(span * factor, MIN_SPAN, daySpan);
        setRange(left + (span - length) * at, length);
    }
    function showReport(report) {
        pinned = report;
        select.value = report ? String(report.at) : "";
        result.replaceChildren();
        if (report) {
            const details = document.createElement("dl");
            for (const event of onReport(report)) {
                const tag = document.createElement("dt");
                tag.textContent = event.tag;
                const readings = document.createElement("dd");
                if (event.candidates.length) {
                    const list = document.createElement("ul");
                    for (const candidate of event.candidates) {
                        const value = document.createElement("li");
                        value.textContent = [
                            isoTime(candidate.received) + " (" + offsetText(candidate.offset) + ")",
                            candidate.rssi === null ? null : candidate.rssi + " dBm",
                            candidate.temp === null ? null : candidate.temp + " °C",
                        ].filter(part => part !== null).join(" · ");
                        list.append(value);
                    }
                    readings.append(list);
                } else readings.textContent = "No Pi candidate within ±" + data.window + " s";
                details.append(tag, readings);
            }
            result.append(details);
        }
        redraw();
    }
    function updateChoices() {
        const available = reports.filter(report => onReport(report).length);
        select.replaceChildren(new Option("Choose a report", ""), ...available.map(report =>
            new Option(isoTime(report.observed) + " · " + onReport(report).map(event => event.tag).join(", "), report.at)));
        inspector.hidden = !available.length;
        showReport(available.includes(pinned) ? pinned : null);
    }

    function line(x1, y1, x2, y2, stroke, weight = 1) {
        context.beginPath();
        context.moveTo(x1, y1);
        context.lineTo(x2, y2);
        context.strokeStyle = stroke;
        context.lineWidth = weight;
        context.stroke();
    }
    function clip(top, bottom, leftEdge, rightEdge) {
        context.save();
        context.beginPath();
        context.rect(leftEdge, top, rightEdge - leftEdge, bottom - top);
        context.clip();
    }

    function paint() {
        const width = Math.floor(canvas.getBoundingClientRect().width);
        if (!width) return;
        const narrow = width < 550, panelHeight = narrow ? 116 : 140, inset = narrow ? 42 : 53;
        const rightEdge = width - 12, plotWidth = rightEdge - inset;
        const tracks = data.series.filter(track => visible.has(track.tag));
        const reportTop = panelHeight * METRICS.length;
        const laneHeight = 17;
        const floor = reportTop + 26 + Math.max(1, tracks.length) * laneHeight;
        const height = floor + 34;
        const dpi = Math.min(2, window.devicePixelRatio || 1);
        if (canvas.width !== Math.round(width * dpi) || canvas.height !== Math.round(height * dpi)) {
            canvas.width = Math.round(width * dpi);
            canvas.height = Math.round(height * dpi);
            canvas.style.height = height + "px";
        }
        size = {inset, plotWidth, width, height};
        context.setTransform(dpi, 0, 0, dpi, 0, 0);
        context.clearRect(0, 0, width, height);
        context.font = "12px system-ui";
        context.textBaseline = "middle";
        const t = theme();
        const x = at => inset + ((at - left) / span) * plotWidth;
        const right = left + span;
        const plotBands = METRICS.map((_, i) => [i * panelHeight + 23, (i + 1) * panelHeight - 13]);
        plotBands.push([reportTop + 18, floor]);
        function vertical(px, stroke, weight) {
            for (const [top, bottom] of plotBands) line(px, top, px, bottom, stroke, weight);
        }
        if (through < right) {
            const shadeAt = x(Math.max(through, left));
            context.fillStyle = t.shade;
            context.fillRect(shadeAt, 21, rightEdge - shadeAt, floor - 21);
        }

        const steps = [15,30,60,120,300,600,900,1800,3600,7200,10800,14400,21600].map(step => step * 1000);
        const tickStep = steps.find(step => span / step <= (narrow ? 4 : 8)) || steps.at(-1);
        for (let at = start + Math.ceil((left - start) / tickStep) * tickStep; at <= right; at += tickStep) {
            const px = x(at);
            vertical(px, t.grid);
            context.fillStyle = t.muted;
            const label = clock(at, tickStep < 60000);
            const half = context.measureText(label).width / 2;
            context.fillText(label, clamp(px - half, 0, width - half * 2), floor + 17);
        }
        const shownReports = reports.filter(report => inView(report.at) && onReport(report).length);
        context.globalAlpha = .14;
        let lastGuide = -Infinity;
        for (const report of shownReports) {
            const px = x(report.at);
            if (px - lastGuide < 3) continue;
            vertical(px, t.guide);
            lastGuide = px;
        }
        context.globalAlpha = 1;

        METRICS.forEach((metric, index) => {
            const top = index * panelHeight + 23;
            const bottom = (index + 1) * panelHeight - 13;
            const samples = tracks.map(track => metric.key === "hits"
                ? track.hits.filter(([at]) => at <= right && at + 300000 >= left)
                : from(track, metric.key));
            const field = metric.key === "gaps" ? "gap" : metric.key;
            const candidates = ["gaps", "rssi"].includes(metric.key)
                ? shownReports.flatMap(report => onReport(report).flatMap(event =>
                    event.candidates.map(item => ({at: Date.parse(item.received), value: item[field],
                        tag: event.tag, unique: event.candidates.length === 1}))))
                    .filter(mark => mark.value != null && inView(mark.at))
                : [];
            const scale = scaleMetric(metric, [...samples.flat(), ...candidates.map(mark => [mark.at, mark.value])]);
            context.font = "600 13px system-ui";
            context.fillStyle = t.ink;
            context.fillText(metric.title, inset, top - 13);
            context.font = "11px system-ui";
            context.fillStyle = t.muted;
            context.fillText(metric.unit, rightEdge - context.measureText(metric.unit).width, top - 13);
            if (!scale) {
                context.fillText("No readings in this range", inset + 8, (top + bottom) / 2);
                return;
            }
            const y = n => bottom - ((n - scale.low) / (scale.high - scale.low)) * (bottom - top);
            for (const [level, label] of scale.ticks) {
                const py = y(level);
                line(inset, py, rightEdge, py, t.grid);
                context.fillStyle = t.muted;
                context.fillText(label, inset - context.measureText(label).width - 7, py);
            }
            clip(top - 3, bottom + 3, inset, rightEdge);
            tracks.forEach((track, i) => {
                const ink = color(track);
                if (metric.key === "hits") {
                    for (const [at, n] of samples[i]) line(x(at), y(n), x(at + 300000), y(n), ink, 1.65);
                } else {
                    context.globalAlpha = .55;
                    context.fillStyle = ink;
                    for (const [at, n] of samples[i]) {
                        const px = x(at), py = y(scale.transform(n));
                        context.fillRect(px - 1, py - 1, 2, 2);
                    }
                    context.globalAlpha = 1;
                    for (const segment of track[metric.key + "Trend"] || []) {
                        context.beginPath();
                        const dataInRange = segment.filter(([at], k) => inView(at)
                           || (at < left && segment[k + 1]?.[0] >= left)
                           || (at > right && segment[k - 1]?.[0] <= right));
                        for (const [k, [at, n]] of dataInRange.entries()) {
                            const px = x(at), py = y(scale.transform(n));
                            if (k) context.lineTo(px, py);
                            else context.moveTo(px, py);
                        }
                        context.strokeStyle = ink;
                        context.lineWidth = 1.2;
                        context.stroke();
                    }
                }
            });
            for (const mark of candidates) {
                const track = tracks.find(item => item.tag === mark.tag);
                const px = x(mark.at), py = y(scale.transform(mark.value)), ink = color(track);
                if (mark.unique) {
                    context.beginPath();
                    context.arc(px, py, 2.2, 0, Math.PI * 2);
                    context.strokeStyle = ink;
                    context.lineWidth = .9;
                    context.stroke();
                } else {
                    line(px - 2, py - 2, px + 2, py + 2, ink, .8);
                    line(px - 2, py + 2, px + 2, py - 2, ink, .8);
                }
            }
            context.restore();
        });

        context.fillStyle = t.ink;
        context.font = "600 13px system-ui";
        context.fillText("Find My", inset, reportTop + 8);
        context.font = "11px system-ui";
        tracks.forEach((track, lane) => {
            const y = reportTop + 26 + lane * laneHeight;
            context.fillStyle = color(track);
            context.fillText(track.tag, 1, y);
            line(inset, y, rightEdge, y, t.grid);
            for (const report of shownReports) {
                if (report.events.some(event => event.tag === track.tag))
                    line(x(report.at), y - 3, x(report.at), y + 3, color(track));
            }
        });
        const selected = hovered || pinned;
        if (selected && inView(selected.at)) {
            context.setLineDash([4,3]);
            vertical(x(selected.at), t.guide, 1.4);
            context.setLineDash([]);
        }
    }

    function redraw() {
        if (pending) return;
        pending = requestAnimationFrame(() => { pending = 0; paint(); });
    }
    function closest(event) {
        if (!size) return null;
        const rect = canvas.getBoundingClientRect();
        const x = event.clientX - rect.left - size.inset;
        if (x < 0 || x > size.plotWidth) return null;
        const at = left + x / size.plotWidth * span;
        const distance = report => Math.abs(report.at - at);
        const nearby = reports.filter(report => inView(report.at) && onReport(report).length
                                     && distance(report) / span * size.plotWidth <= 10);
        return nearby.reduce((best, report) => !best || distance(report) < distance(best) ? report : best, null);
    }
    function hover(event) {
        hovered = closest(event);
        tooltip.hidden = !hovered;
        if (hovered) {
            const summaries = onReport(hovered).map(item => item.candidates.length === 1
                ? item.tag + " · " + offsetText(item.candidates[0].offset)
                : item.tag + " · " + item.candidates.length + " candidates");
            tooltip.textContent = [isoTime(hovered.observed), ...summaries].join("  |  ");
            const box = canvas.getBoundingClientRect();
            tooltip.style.left = clamp(event.clientX - box.left + 12, 0, box.width - tooltip.offsetWidth) + "px";
            tooltip.style.top = clamp(event.clientY - box.top + 10, 0, box.height - tooltip.offsetHeight) + "px";
        }
        redraw();
    }

    root.querySelector(".day-chart-toolbar").addEventListener("click", event => {
        const anchor = pinned && inView(pinned.at) ? (pinned.at - left) / span : .5;
        switch (event.target.closest("button")?.dataset.chart) {
        case "activity": setRange(activityLeft, activitySpan); break;
        case "all": setRange(start, daySpan); break;
        case "in": zoom(.5, anchor); break;
        case "out": zoom(2, anchor); break;
        case "earlier": setRange(left - span * .6, span); break;
        case "later": setRange(left + span * .6, span); break;
        }
    });
    select.addEventListener("change", () => {
        const report = reports.find(item => String(item.at) === select.value);
        if (report && !inView(report.at)) setRange(report.at - span / 2, span);
        showReport(report || null);
    });
    canvas.addEventListener("pointerdown", event => {
        if (!size || !event.isPrimary) return;
        drag = {id:event.pointerId, x:event.clientX, y:event.clientY, left, moved:false};
    });
    canvas.addEventListener("pointermove", event => {
        if (drag?.id === event.pointerId) {
            const dx = event.clientX - drag.x, dy = event.clientY - drag.y;
            if (drag.moved || (Math.abs(dx) >= 8 && Math.abs(dx) > Math.abs(dy))) {
                if (!drag.moved) canvas.setPointerCapture(event.pointerId);
                drag.moved = true;
                setRange(drag.left - dx / size.plotWidth * span, span);
                return;
            }
        }
        if (event.pointerType !== "touch") hover(event);
    });
    canvas.addEventListener("pointerup", event => {
        if (drag?.id !== event.pointerId) return;
        if (!drag.moved) showReport(closest(event));
        drag = null;
    });
    canvas.addEventListener("pointercancel", () => { drag = null; });
    canvas.addEventListener("pointerleave", () => { hovered = null; tooltip.hidden = true; redraw(); });
    canvas.addEventListener("wheel", event => {
        if (!event.ctrlKey || !size) return;
        event.preventDefault();
        const box = canvas.getBoundingClientRect();
        const position = clamp((event.clientX - box.left - size.inset) / size.plotWidth, 0, 1);
        zoom(event.deltaY < 0 ? .8 : 1.25, position);
    }, {passive: false});
    dark.addEventListener("change", redraw);
    new ResizeObserver(redraw).observe(canvas);
    updateChoices();
    setRange(left, span);
}
