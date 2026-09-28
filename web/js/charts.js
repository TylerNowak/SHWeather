// Dependency-free SVG time-series chart tuned for a cockpit: thin 2px lines, hairline
// grid, direct end labels, night-hour shading, a crosshair tooltip that lists every
// series at the hovered hour, keyboard access, and an optional go/no-go status strip.
// The hourly table is the chart's table-view twin.

import { $, clear, dayLabel, h, hourShort, LEVELS, levelIcon, s, whenLabel } from "./dom.js";

const M = { left: 44, right: 64, top: 12, bottom: 30 };
const UNIT_ROW = 14;
const ARROW_ROW = 20;
const STRIP_H = 6;

const registry = new WeakMap();

function niceStep(range, target = 4) {
  const raw = range / target;
  const mag = 10 ** Math.floor(Math.log10(raw));
  const norm = raw / mag;
  const step = norm <= 1 ? 1 : norm <= 2 ? 2 : norm <= 2.5 ? 2.5 : norm <= 5 ? 5 : 10;
  return step * mag;
}

function decimalsOf(step) {
  for (let d = 0; d <= 4; d++) if (Math.abs(Number(step.toFixed(d)) - step) < 1e-9) return d;
  return 4;
}

function yDomain(series, opts) {
  let lo = Infinity;
  let hi = -Infinity;
  for (const sr of series) for (const [, v] of sr.points) if (v != null) { lo = Math.min(lo, v); hi = Math.max(hi, v); }
  if (!Number.isFinite(lo)) return null;
  if (opts.yMin !== undefined && opts.yMin !== null) lo = Math.min(lo, opts.yMin);
  if (opts.minSpan && hi - lo < opts.minSpan) {
    const mid = (hi + lo) / 2;
    lo = opts.yMin != null ? Math.max(opts.yMin, mid - opts.minSpan / 2) : mid - opts.minSpan / 2;
    hi = lo + opts.minSpan;
  }
  if (hi === lo) hi = lo + 1;
  const step = niceStep(hi - lo);
  return { lo: Math.floor(lo / step) * step, hi: Math.ceil(hi / step) * step, step };
}

function linePath(points, x, y) {
  let d = "";
  let pen = false;
  for (const [t, v] of points) {
    if (v == null) { pen = false; continue; }
    d += `${pen ? "L" : "M"}${x(t).toFixed(1)},${y(v).toFixed(1)}`;
    pen = true;
  }
  return d;
}

function areaPath(points, x, y, base) {
  const segs = [];
  let cur = [];
  for (const p of points) {
    if (p[1] == null) { if (cur.length) segs.push(cur); cur = []; } else cur.push(p);
  }
  if (cur.length) segs.push(cur);
  return segs.map((seg) => {
    const top = seg.map(([t, v], i) => `${i ? "L" : "M"}${x(t).toFixed(1)},${y(v).toFixed(1)}`).join("");
    return `${top}L${x(seg[seg.length - 1][0]).toFixed(1)},${base}L${x(seg[0][0]).toFixed(1)},${base}Z`;
  }).join("");
}

function nearestIndex(times, t) {
  let lo = 0;
  let hi = times.length - 1;
  while (hi - lo > 1) {
    const mid = (lo + hi) >> 1;
    if (times[mid] < t) lo = mid; else hi = mid;
  }
  return Math.abs(times[lo] - t) <= Math.abs(times[hi] - t) ? lo : hi;
}

function valueAt(points, t) {
  // points are hourly or minute data; take the closest within 45 minutes
  if (!points.length) return null;
  const times = points.map((p) => p[0]);
  const i = nearestIndex(times, t);
  return Math.abs(points[i][0] - t) <= 2700 ? points[i][1] : null;
}

export function lineChart(container, opts) {
  registry.set(container, opts);
  if (!container.__shwObserved) {
    container.__shwObserved = true;
    let raf = 0;
    let lastW = container.clientWidth;
    new ResizeObserver(() => {
      if (Math.abs(container.clientWidth - lastW) < 4) return;
      lastW = container.clientWidth;
      cancelAnimationFrame(raf);
      raf = requestAnimationFrame(() => render(container, registry.get(container)));
    }).observe(container);
  }
  render(container, opts);
}

function render(container, opts) {
  clear(container);
  const series = opts.series.filter((sr) => sr.points.some((p) => p[1] != null));
  if (!series.length) {
    container.append(h("p", { class: "chart-empty", text: opts.emptyText || "No data yet." }));
    return;
  }
  if (series.length >= 2 || opts.forceLegend) {
    container.append(h("div", { class: "legend" }, series.map((sr) =>
      h("span", { class: "legend-item" }, h("span", { class: "legend-line", style: { background: sr.color } }), sr.name))));
  }

  const W = Math.max(280, container.clientWidth || 600);
  const H = opts.height || 220;
  const top = M.top + (opts.yUnit ? UNIT_ROW : 0) + (opts.arrows ? ARROW_ROW : 0);
  const stripSpace = opts.strip ? STRIP_H + 6 : 0;
  const plotH = H - top - M.bottom - stripSpace;
  const [t0, t1] = opts.xDomain;
  const x = (t) => M.left + ((t - t0) / (t1 - t0)) * (W - M.left - M.right);
  const dom = yDomain(series, opts);
  const y = (v) => top + plotH - ((v - dom.lo) / (dom.hi - dom.lo)) * plotH;
  const baseY = top + plotH;

  const svg = s("svg", {
    viewBox: `0 0 ${W} ${H}`, width: W, height: H, role: "img", tabindex: 0,
    "aria-label": opts.ariaLabel || "Chart",
  });

  // Night shading first, under everything.
  for (const [a, b] of opts.nightBands || []) {
    const xa = Math.max(M.left, x(a));
    const xb = Math.min(W - M.right, x(b));
    if (xb > xa) svg.append(s("rect", { class: "night-band", x: xa, y: top, width: xb - xa, height: plotH }));
  }

  // Y grid + ticks
  for (let v = dom.lo; v <= dom.hi + 1e-9; v += dom.step) {
    const yy = Math.round(y(v)) + 0.5;
    svg.append(s("line", { class: v === dom.lo ? "base-line" : "grid-line", x1: M.left, x2: W - M.right, y1: yy, y2: yy }));
    const stepDec = decimalsOf(dom.step);
    const label = stepDec > 0
      ? v.toLocaleString(undefined, { minimumFractionDigits: stepDec, maximumFractionDigits: stepDec })
      : (opts.fmtTick || opts.fmt)(v);
    svg.append(s("text", { class: "axis-text", x: M.left - 6, y: yy + 4, "text-anchor": "end", text: label }));
  }
  if (opts.yUnit) svg.append(s("text", { class: "axis-text", x: M.left - 6, y: M.top + 4, "text-anchor": "end", text: opts.yUnit }));

  // X ticks at local-hour multiples, day names at midnight.
  const pxPerHour = (W - M.left - M.right) / ((t1 - t0) / 3600);
  // Space ticks by the widest label this locale produces ("9 AM" vs "09").
  const labelW = Math.max(hourShort(t0).length, hourShort(t0 + 13 * 3600).length, dayLabel(t0).length) * 6.5 + 12;
  const every = [3, 6, 12, 24].find((n) => n * pxPerHour >= labelW) || 24;
  const xLabelY = baseY + stripSpace + 16;
  for (let t = Math.ceil(t0 / 3600) * 3600; t <= t1; t += 3600) {
    const d = new Date(t * 1000);
    const hr = d.getHours();
    if (hr % every !== 0 || d.getMinutes() !== 0) continue;
    const xx = x(t);
    if (xx < M.left + 12 || xx > W - M.right - 8) continue;
    const midnight = hr === 0;
    svg.append(s("text", { class: `axis-text${midnight ? " day" : ""}`, x: xx, y: xLabelY, "text-anchor": "middle",
      text: midnight ? dayLabel(t) : hourShort(t) }));
    if (midnight) svg.append(s("line", { class: "grid-line", x1: Math.round(xx) + 0.5, x2: Math.round(xx) + 0.5, y1: top, y2: baseY }));
  }

  // Now marker
  if (opts.now && opts.now > t0 && opts.now < t1) {
    const xn = Math.round(x(opts.now)) + 0.5;
    svg.append(s("line", { class: "now-line", x1: xn, x2: xn, y1: top, y2: baseY }));
    svg.append(s("text", { class: "now-label", x: xn + 4, y: baseY - 4, text: "Now" }));
  }

  // Direction arrows (where the wind is going), spaced to stay legible.
  if (opts.arrows) {
    let lastX = -Infinity;
    for (const { t, deg } of opts.arrows) {
      if (deg == null || t < t0 || t > t1) continue;
      const xx = x(t);
      if (xx - lastX < 22 || xx < M.left || xx > W - M.right) continue;
      lastX = xx;
      svg.append(s("path", { class: "dir-arrow", d: "M0,-7 L4.5,2 L1.2,0.8 L1.2,7 L-1.2,7 L-1.2,0.8 L-4.5,2 Z",
        transform: `translate(${xx.toFixed(1)},${top - ARROW_ROW / 2}) rotate(${(deg + 180) % 360})` }));
    }
  }

  // Series: areas, then lines, clipped to the plot; then end dots + labels.
  const clipId = `clip-${Math.random().toString(36).slice(2, 9)}`;
  svg.append(s("clipPath", { id: clipId }, s("rect", { x: M.left, y: top - 4, width: W - M.left - M.right, height: plotH + 8 })));
  const plot = s("g", { "clip-path": `url(#${clipId})` });
  svg.append(plot);
  for (const sr of series) if (sr.area) plot.append(s("path", { class: "series-area", d: areaPath(sr.points, x, y, baseY), fill: sr.color }));
  for (const sr of series) plot.append(s("path", { class: "series-line", d: linePath(sr.points, x, y), stroke: sr.color }));
  const labelYs = [];
  for (const sr of series) {
    if (sr.endLabel === false) continue;
    const last = [...sr.points].reverse().find((p) => p[1] != null && p[0] <= t1);
    if (!last) continue;
    const lx = x(last[0]);
    const ly = y(last[1]);
    svg.append(s("circle", { class: "end-dot", cx: lx, cy: ly, r: 4, fill: sr.color }));
    if (labelYs.some((v) => Math.abs(v - ly) < 26)) continue; // don't stack colliding labels; legend + tooltip carry it
    labelYs.push(ly);
    svg.append(s("text", { class: "end-label", x: lx + 8, y: ly - 1, text: opts.fmt(last[1]) }));
    svg.append(s("text", { class: "end-label sub", x: lx + 8, y: ly + 12, text: sr.short || sr.name }));
  }

  // Status strip (go / reef / caution / no-go per hour).
  if (opts.strip) {
    const cells = opts.strip.filter((c) => c.t >= t0 && c.t < t1);
    const cw = pxPerHour;
    const gap = cw > 6 ? 2 : 0;
    for (const c of cells) {
      svg.append(s("rect", { x: x(c.t) - cw / 2 + gap / 2, y: baseY + 5, width: Math.max(1, cw - gap), height: STRIP_H, rx: 2,
        fill: LEVELS[c.level]?.color || "var(--axis)" }));
    }
  }

  // Hover layer
  const hover = s("g", { visibility: "hidden" });
  const cross = s("line", { class: "crosshair", y1: top, y2: baseY });
  hover.append(cross);
  const dots = series.map((sr) => { const c = s("circle", { class: "hover-dot", r: 5, fill: sr.color }); hover.append(c); return c; });
  svg.append(hover);
  const hit = s("rect", { x: M.left, y: 0, width: W - M.left - M.right, height: H, fill: "transparent" });
  svg.append(hit);

  const times = opts.hoverTimes || [...new Set(series.flatMap((sr) => sr.points.map((p) => p[0])))].sort((a, b) => a - b);
  let idx = -1;
  const tip = $("#tooltip");

  function show(i, clientX, clientY) {
    if (i < 0 || i >= times.length) return;
    idx = i;
    const t = times[i];
    const xx = x(t);
    cross.setAttribute("x1", xx);
    cross.setAttribute("x2", xx);
    const rows = [];
    series.forEach((sr, k) => {
      const v = valueAt(sr.points, t);
      if (v == null) { dots[k].setAttribute("visibility", "hidden"); return; }
      dots[k].setAttribute("visibility", "visible");
      dots[k].setAttribute("cx", xx);
      dots[k].setAttribute("cy", y(v));
      rows.push(h("div", { class: "tt-row" },
        h("span", { class: "k" }, h("span", { class: "tt-key", style: { background: sr.color } }), sr.name),
        h("span", { class: "v", text: `${opts.fmt(v)} ${opts.yUnit || ""}`.trim() })));
    });
    for (const [label, value] of (opts.tooltipExtra ? opts.tooltipExtra(t) : [])) {
      if (value == null) continue;
      rows.push(h("div", { class: "tt-row" }, h("span", { class: "k", text: label }),
        value instanceof Node ? h("span", { class: "v" }, value) : h("span", { class: "v", text: value })));
    }
    hover.setAttribute("visibility", "visible");
    clear(tip).append(h("div", { class: "tt-time", text: whenLabel(t) }), ...rows);
    tip.hidden = false;
    const rect = svg.getBoundingClientRect();
    const px = clientX ?? rect.left + (xx / W) * rect.width;
    const py = clientY ?? rect.top + 20;
    const tw = tip.offsetWidth;
    const th = tip.offsetHeight;
    let left = px + 14;
    if (left + tw > window.innerWidth - 8) left = px - tw - 14;
    let topPx = py - th - 12;
    if (topPx < 8) topPx = py + 16;
    tip.style.left = `${Math.max(8, left)}px`;
    tip.style.top = `${Math.max(8, topPx)}px`;
  }

  function hide() {
    hover.setAttribute("visibility", "hidden");
    tip.hidden = true;
  }

  function fromEvent(ev) {
    const rect = svg.getBoundingClientRect();
    const sx = ((ev.clientX - rect.left) / rect.width) * W;
    const t = t0 + ((sx - M.left) / (W - M.left - M.right)) * (t1 - t0);
    show(nearestIndex(times, t), ev.clientX, ev.clientY);
  }

  hit.addEventListener("pointermove", fromEvent);
  hit.addEventListener("pointerdown", fromEvent);
  hit.addEventListener("pointerleave", hide);
  svg.addEventListener("blur", hide);
  svg.addEventListener("focus", () => {
    const start = opts.now ? nearestIndex(times, opts.now) : 0;
    show(start);
  });
  svg.addEventListener("keydown", (ev) => {
    if (ev.key === "ArrowRight") { show(Math.min(times.length - 1, idx + 1)); ev.preventDefault(); }
    else if (ev.key === "ArrowLeft") { show(Math.max(0, idx - 1)); ev.preventDefault(); }
    else if (ev.key === "Escape") hide();
  });

  container.append(svg);

  if (opts.strip) {
    container.append(h("div", { class: "level-legend", "aria-hidden": "true" },
      ["good", "reef", "caution", "nogo"].map((l) => h("span", {}, levelIcon(l, 14), LEVELS[l].label))));
  }
}
