// SPDX-License-Identifier: AGPL-3.0-or-later
"use strict";

const frame = document.querySelector("[data-report-events]");
if (frame) {
    fetch(frame.dataset.reportEvents, {credentials: "same-origin"})
        .then(response => response.ok ? response.json() : null)
        .then(data => {
            if (data?.events.length) inspectReports(frame, data);
        })
        .catch(() => {
            // The PNG and its download link still work if the inspector is unavailable.
        });
}

function clockTime(iso) {
    return iso.match(/T([\d:.]+)/)?.[1] || iso;
}

function offsetText(offset) {
    return `${offset > 0 ? "+" : ""}${offset.toFixed(3)} s`;
}

function inspectReports(plot, data) {
    const target = plot.querySelector(".day-plot-target");
    const cursor = target.querySelector(".day-report-cursor");
    const tooltip = target.querySelector(".day-report-tooltip");
    const inspector = document.querySelector(".day-report-inspector");
    const select = inspector.querySelector("select");
    const result = inspector.querySelector(".day-report-result");

    const start = Date.parse(data.start);
    const duration = Date.parse(data.end) - start;
    const grouped = new Map();
    for (const event of data.events) {
        const observed = Date.parse(event.observed);
        if (!grouped.has(observed)) {
            grouped.set(observed, {observed: event.observed, position: (observed - start) / duration, events: []});
        }
        grouped.get(observed).events.push(event);
    }
    const reports = [...grouped.values()].sort((a, b) => a.position - b.position);
    Object.assign(target.style, {
        left: `${100 * data.axes.left}%`,
        right: `${100 * (1 - data.axes.right)}%`,
        top: `${100 * (1 - data.axes.top)}%`,
        bottom: `${100 * data.axes.bottom}%`,
    });
    for (const [index, report] of reports.entries()) {
        const tags = report.events.map(event => event.tag).join(", ");
        select.add(new Option(`${clockTime(report.observed)} · ${tags}`, String(index)));
    }

    let pinned = -1;
    let shown = -1;
    select.selectedIndex = -1;
    select.setAttribute("aria-label", "Find My observation");
    target.hidden = false;
    inspector.hidden = false;
    document.querySelector(".day-report-hint").hidden = false;

    function display(index) {
        if (index === shown) return;
        shown = index;
        cursor.hidden = index < 0;
        select.selectedIndex = index;
        result.replaceChildren();
        if (index < 0) return;

        const report = reports[index];
        cursor.style.left = `${report.position * 100}%`;
        const details = document.createElement("dl");
        for (const event of report.events) {
            const tag = document.createElement("dt");
            tag.textContent = event.tag;
            const readings = document.createElement("dd");
            if (event.candidates.length) {
                const list = document.createElement("ul");
                for (const candidate of event.candidates) {
                    const value = document.createElement("li");
                    value.textContent = [
                        `${clockTime(candidate.received)} (${offsetText(candidate.offset)})`,
                        candidate.rssi === null ? null : `${candidate.rssi} dBm`,
                        candidate.temp === null ? null : `${candidate.temp} °C`,
                    ].filter(part => part !== null).join(" · ");
                    list.append(value);
                }
                readings.append(list);
            } else {
                readings.textContent = `No Pi advert within ±${data.window} s`;
            }
            details.append(tag, readings);
        }
        result.append(details);
    }

    function closest(event) {
        const box = target.getBoundingClientRect();
        const position = (event.clientX - box.left) / box.width;
        let lo = 0;
        let hi = reports.length;
        while (lo < hi) {
            const mid = Math.floor((lo + hi) / 2);
            if (reports[mid].position < position) lo = mid + 1;
            else hi = mid;
        }
        const before = lo - 1;
        const delta = index => Math.abs((reports[index]?.position ?? Infinity) - position);
        const index = delta(before) <= delta(lo) ? before : lo;
        return delta(index) * box.width <= 10 ? index : -1;
    }

    target.addEventListener("pointermove", event => {
        if (event.pointerType === "touch") return;
        const index = closest(event);
        display(index < 0 ? pinned : index);
        tooltip.hidden = index < 0;
        if (index < 0) return;
        const report = reports[index];
        const summary = report.events.map(item => {
            const n = item.candidates.length;
            if (n === 1) {
                const candidate = item.candidates[0];
                return `${item.tag} · ${clockTime(candidate.received)} (${offsetText(candidate.offset)})`;
            }
            return `${item.tag} · ${n} candidates`;
        });
        tooltip.textContent = [clockTime(report.observed), ...summary].join("  |  ");
        const box = target.getBoundingClientRect();
        const left = Math.min(box.width - tooltip.offsetWidth, event.clientX - box.left + 12);
        const top = Math.min(box.height - tooltip.offsetHeight, event.clientY - box.top + 10);
        tooltip.style.left = `${Math.max(0, left)}px`;
        tooltip.style.top = `${Math.max(0, top)}px`;
    });
    target.addEventListener("pointerleave", () => {
        tooltip.hidden = true;
        display(pinned);
    });
    target.addEventListener("click", event => {
        pinned = closest(event);
        display(pinned);
    });
    select.addEventListener("change", () => {
        pinned = select.selectedIndex;
        display(pinned);
    });
}
