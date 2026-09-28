// Tiny DOM helpers. Text always goes in via textContent: API strings (alert text,
// station names) are untrusted data.

const SVG_NS = "http://www.w3.org/2000/svg";

export function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  setAttrs(el, attrs);
  append(el, children);
  return el;
}

export function s(tag, attrs = {}, ...children) {
  const el = document.createElementNS(SVG_NS, tag);
  setAttrs(el, attrs);
  append(el, children);
  return el;
}

function setAttrs(el, attrs) {
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.setAttribute("class", v);
    else if (k === "text") el.textContent = v;
    else if (k === "style" && typeof v === "object") Object.assign(el.style, v);
    else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? "" : String(v));
  }
}

function append(el, children) {
  for (const c of children.flat(Infinity)) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
}

export function clear(el) {
  while (el.firstChild) el.removeChild(el.firstChild);
  return el;
}

export const $ = (sel, root = document) => root.querySelector(sel);

// Storage can be unavailable (private mode, blocked site data): never let it break the app.
export const store = {
  get(key, fallback = null) {
    try { const v = localStorage.getItem(key); return v === null ? fallback : JSON.parse(v); } catch { return fallback; }
  },
  set(key, value) {
    try { localStorage.setItem(key, JSON.stringify(value)); } catch { /* ignore */ }
  },
};

// ---- time formatting (device local time) ----
const fmtHour = new Intl.DateTimeFormat(undefined, { hour: "2-digit", minute: "2-digit" });
const fmtHourShort = new Intl.DateTimeFormat(undefined, { hour: "numeric" });
const fmtDay = new Intl.DateTimeFormat(undefined, { weekday: "short" });
const fmtDayLong = new Intl.DateTimeFormat(undefined, { weekday: "short", day: "numeric", month: "short" });

export const hourLabel = (t) => fmtHour.format(new Date(t * 1000));
export const hourShort = (t) => fmtHourShort.format(new Date(t * 1000));
export const dayLabel = (t) => fmtDay.format(new Date(t * 1000));
export const dayLongLabel = (t) => fmtDayLong.format(new Date(t * 1000));
export const whenLabel = (t) => `${dayLabel(t)} ${hourLabel(t)}`;

export function ago(seconds) {
  if (seconds === null || seconds === undefined) return "unknown age";
  const s = Math.max(0, Math.round(seconds));
  if (s < 90) return `${s} s ago`;
  if (s < 5400) return `${Math.round(s / 60)} min ago`;
  if (s < 172800) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} days ago`;
}

export function isoToEpoch(iso) {
  const t = Date.parse(iso);
  return Number.isNaN(t) ? null : t / 1000;
}

export const COMPASS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"];
export const compass = (deg) => (deg === null || deg === undefined ? "–" : COMPASS[Math.round(((deg % 360) + 360) % 360 / 22.5) % 16]);

// Arrow showing where the wind/waves are GOING (from + 180), as sailors read a wind barb.
export function dirArrow(deg, size = 14) {
  const rot = ((deg ?? 0) + 180) % 360;
  return s("svg", { class: "arrow", viewBox: "0 0 14 14", width: size, height: size, "aria-hidden": "true" },
    s("path", { d: "M7 1 L11 8 L8 7 L8 13 L6 13 L6 7 L3 8 Z", transform: `rotate(${rot} 7 7)` }));
}

export const LEVELS = {
  good: { label: "Good", cls: "lvl-good", color: "var(--good)" },
  reef: { label: "Reef", cls: "lvl-reef", color: "var(--warning)" },
  caution: { label: "Caution", cls: "lvl-caution", color: "var(--serious)" },
  nogo: { label: "No-go", cls: "lvl-nogo", color: "var(--critical)" },
  unknown: { label: "Unknown", cls: "lvl-unknown", color: "var(--axis)" },
};

// Status icons: shape differs per level so meaning never rests on colour alone.
export function levelIcon(level, size = 16) {
  const c = LEVELS[level]?.color || "var(--axis)";
  const common = { class: "status-icon", viewBox: "0 0 20 20", width: size, height: size, "aria-hidden": "true" };
  if (level === "good") return s("svg", common, s("circle", { cx: 10, cy: 10, r: 9, fill: c }), s("path", { d: "M5.5 10.5l3 3 6-6.5", fill: "none", stroke: "#fff", "stroke-width": 2, "stroke-linecap": "round", "stroke-linejoin": "round" }));
  if (level === "reef") return s("svg", common, s("path", { d: "M10 1.5 L19 18 H1 Z", fill: c }), s("path", { d: "M10 7v5M10 14.5v.5", stroke: "#111", "stroke-width": 2, "stroke-linecap": "round" }));
  if (level === "caution") return s("svg", common, s("path", { d: "M10 1 L19 10 L10 19 L1 10 Z", fill: c }), s("path", { d: "M10 6v5M10 13.5v.5", stroke: "#111", "stroke-width": 2, "stroke-linecap": "round" }));
  if (level === "nogo") return s("svg", common, s("path", { d: "M6 1h8l5 5v8l-5 5H6l-5-5V6z", fill: c }), s("path", { d: "M6 10h8", stroke: "#fff", "stroke-width": 2.4, "stroke-linecap": "round" }));
  return s("svg", common, s("circle", { cx: 10, cy: 10, r: 9, fill: c }));
}
