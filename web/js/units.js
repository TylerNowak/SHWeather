// Display units. The API always speaks canonical units (knots, metres, hPa, °C, mm,
// nautical miles for distances, metres for visibility); conversion happens only here.
//
// A device picks a unit *system* (metric or imperial), optionally "nautical" (knots and
// nautical miles for wind, current and distance, the way marine forecasts are written in
// either system), and can then override any single quantity ("custom").
// Until a device chooses, it follows the server's default (display.units in config.yaml)
// or, when that is "auto", the device's region (imperial in the US).

import { store } from "./dom.js";

const KEY = "shw.units.v2";

export const QUANTITIES = ["speed", "height", "temp", "pressure", "precip", "distance", "visibility"];

export const LABELS = {
  speed: "Wind and current", height: "Waves and tides", temp: "Temperature", pressure: "Pressure",
  precip: "Rain", distance: "Distance", visibility: "Visibility",
};

export const OPTIONS = {
  speed: [["kn", "knots"], ["kmh", "km/h"], ["ms", "m/s"], ["mph", "mph"]],
  height: [["m", "metres"], ["ft", "feet"]],
  temp: [["C", "°C"], ["F", "°F"]],
  pressure: [["hPa", "hPa (mbar)"], ["inHg", "inHg"]],
  precip: [["mm", "millimetres"], ["in", "inches"]],
  distance: [["nm", "nautical miles"], ["km", "kilometres"], ["mi", "miles"]],
  visibility: [["nm", "nautical miles"], ["km", "kilometres"], ["mi", "miles"]],
};

const SYSTEMS = {
  metric: { speed: "kmh", height: "m", temp: "C", pressure: "hPa", precip: "mm", distance: "km", visibility: "km" },
  imperial: { speed: "mph", height: "ft", temp: "F", pressure: "inHg", precip: "in", distance: "mi", visibility: "mi" },
};
const NAUTICAL = { speed: "kn", distance: "nm", visibility: "nm" };

// canonical value -> display value (multiply), and display label
const FACTORS = {
  speed: { kn: [1, "kn"], kmh: [1.852, "km/h"], ms: [0.514444, "m/s"], mph: [1.150779, "mph"] },
  height: { m: [1, "m"], ft: [3.28084, "ft"] },
  pressure: { hPa: [1, "hPa"], inHg: [0.0295300, "inHg"] },
  precip: { mm: [1, "mm"], in: [1 / 25.4, "in"] },
  distance: { nm: [1, "nm"], km: [1.852, "km"], mi: [1.150779, "mi"] },
  visibility: { nm: [1 / 1852, "nm"], km: [1 / 1000, "km"], mi: [1 / 1609.344, "mi"] },
};

export function preset(system, nautical) {
  return { ...SYSTEMS[system], ...(nautical ? NAUTICAL : {}) };
}

export function localeSystem(lang = (typeof navigator !== "undefined" && navigator.language) || "en-US") {
  let region = "";
  try { region = new Intl.Locale(lang).maximize().region || ""; } catch { region = (lang.split("-")[1] || "").toUpperCase(); }
  return ["US", "LR", "MM"].includes(region) ? "imperial" : "metric";
}

let serverDefault = { units: "auto", nautical: true };
let chosen = store.get(KEY, null); // { units: {...} } once the user has picked on this device
let units = resolve();

function resolve() {
  if (chosen?.units) return { ...preset("metric", true), ...chosen.units };
  const system = serverDefault.units === "auto" ? localeSystem() : serverDefault.units;
  return preset(system in SYSTEMS ? system : "metric", serverDefault.nautical !== false);
}

// ---- state ----

/** Apply the server's default (from /api/status). Returns true when displayed units changed. */
export function setServerDefault(display) {
  if (!display) return false;
  const before = JSON.stringify(units);
  serverDefault = { units: display.units || "auto", nautical: display.nautical !== false };
  units = resolve();
  return JSON.stringify(units) !== before;
}

export const getUnits = () => ({ ...units });
export const isDeviceChoice = () => Boolean(chosen?.units);

/** {system: "metric"|"imperial"|"custom", nautical: bool} describing the current units. */
export function getSystem() {
  for (const system of Object.keys(SYSTEMS)) {
    for (const nautical of [true, false]) {
      const p = preset(system, nautical);
      if (QUANTITIES.every((q) => p[q] === units[q])) return { system, nautical };
    }
  }
  return { system: "custom", nautical: units.speed === "kn" };
}

export function setSystem(system, nautical = getSystem().nautical) {
  units = preset(system, nautical);
  chosen = { units: { ...units } };
  store.set(KEY, chosen);
}

export function setUnit(quantity, value) {
  if (!FACTORS[quantity]?.[value] && !(quantity === "temp" && ["C", "F"].includes(value))) return;
  units = { ...units, [quantity]: value };
  chosen = { units: { ...units } };
  store.set(KEY, chosen);
}

export function resetToDefault() {
  chosen = null;
  store.set(KEY, null);
  units = resolve();
}

// ---- conversion ----

function conv(q, v) {
  if (v === null || v === undefined || Number.isNaN(v)) return null;
  if (q === "temp") return units.temp === "F" ? v * 9 / 5 + 32 : v;
  return v * FACTORS[q][units[q]][0];
}

/**
 * Display value back to canonical (for forms). Pass the units the form was rendered with
 * (from getUnits()) so a unit change while the form is open can't rescale what was typed.
 */
export function toCanonical(q, v, rendered = units) {
  if (v === null || v === undefined || v === "" || Number.isNaN(Number(v))) return null;
  const n = Number(v);
  if (q === "temp") return rendered.temp === "F" ? (n - 32) * 5 / 9 : n;
  return n / FACTORS[q][rendered[q]][0];
}

export const speed = (kn) => conv("speed", kn);
export const height = (m) => conv("height", m);
export const temp = (c) => conv("temp", c);
export const pressure = (hpa) => conv("pressure", hpa);
export const precip = (mm) => conv("precip", mm);
export const distance = (nm) => conv("distance", nm);
export const visibility = (m) => conv("visibility", m);
export const pressureDelta = pressure;

export const unit = (q, rendered = units) => (q === "temp" ? (rendered.temp === "F" ? "°F" : "°C") : FACTORS[q][rendered[q]][1]);

/** Canonical -> display value using a specific set of units (defaults to the current ones). */
export function toDisplay(q, v, rendered = units) {
  if (v === null || v === undefined || Number.isNaN(v)) return null;
  if (q === "temp") return rendered.temp === "F" ? v * 9 / 5 + 32 : v;
  return v * FACTORS[q][rendered[q]][0];
}
export const speedUnit = () => unit("speed");
export const heightUnit = () => unit("height");
export const tempUnit = () => unit("temp");
export const pressureUnit = () => unit("pressure");
export const precipUnit = () => unit("precip");
export const distanceUnit = () => unit("distance");
export const visibilityUnit = () => unit("visibility");

// ---- formatting ----

export function num(v, digits = 0) {
  if (v === null || v === undefined || Number.isNaN(v)) return "–";
  return v.toLocaleString(undefined, { minimumFractionDigits: digits, maximumFractionDigits: digits });
}

/** Sensible decimals for an already-converted display value of a quantity. */
export function digits(q, v = 0) {
  const u = units[q];
  const a = Math.abs(v ?? 0);
  switch (q) {
    case "speed": return u === "ms" ? 1 : 0;
    case "height": return u === "ft" ? (a < 10 ? 1 : 0) : (a < 10 ? 1 : 0);
    case "pressure": return u === "inHg" ? 2 : 0;
    case "precip": return u === "in" ? 2 : 1;
    case "distance": case "visibility": return a < 10 ? 1 : 0;
    default: return 0;
  }
}

/** Format an already-converted display value. */
export const fmtDisplay = (q, v) => num(v, digits(q, v));

export const fmtSpeed = (kn, d) => num(speed(kn), d ?? digits("speed"));
export const fmtHeight = (m) => fmtDisplay("height", height(m));
export const fmtTemp = (c) => num(temp(c), 0);
export const fmtPressure = (hpa) => fmtDisplay("pressure", pressure(hpa));
export const fmtDistance = (nm) => fmtDisplay("distance", distance(nm));
export function fmtPrecip(mm) {
  const v = precip(mm);
  if (v == null) return "–";
  return v < (units.precip === "in" ? 0.005 : 0.1) ? "" : fmtDisplay("precip", v);
}
export function fmtPressureDelta(hpa) {
  if (hpa == null) return "–";
  const v = pressure(hpa);
  const d = units.pressure === "inHg" ? 2 : 1;
  return (v > 0 ? "+" : v < 0 ? "−" : "±") + num(Math.abs(v), d);
}
export function fmtVisibility(m) {
  const v = visibility(m);
  if (v == null) return "–";
  const cap = units.visibility === "km" ? 20 : 10;
  return v >= cap ? `${cap}+` : fmtDisplay("visibility", v);
}

/**
 * Format a canonical value next to limits without rounding making it look like it
 * crossed one (24.6 kn against a 25 kn limit must not read "25 kn"). Adds a decimal
 * when needed. Returns "value unit".
 */
export function fmtNear(q, canonical, ...limits) {
  const v = conv(q, canonical);
  let d = digits(q, v);
  const lims = limits.map((l) => conv(q, l));
  const crosses = (dd) => lims.some((l) => {
    const r = Number(v.toFixed(dd));
    return (r >= l) !== (v >= l) || (r === l && v !== l); // crossed, or rounded onto the limit
  });
  while (d < 2 && crosses(d)) d++;
  return `${num(v, d)} ${unit(q)}`;
}

/** Format a limit value in display units, dropping needless decimals ("25 kn", "6.6 ft"). */
export function fmtLimit(q, canonical) {
  const v = conv(q, canonical);
  const d = Math.abs(v - Math.round(v)) < 0.05 ? 0 : 1;
  return `${num(v, d)} ${unit(q)}`;
}

export function fmtLatLon(lat, lon) {
  const f = (v, pos, neg) => {
    const a = Math.abs(v);
    const d = Math.floor(a);
    const m = (a - d) * 60;
    return `${d}°${m.toFixed(2).padStart(5, "0")}′${v >= 0 ? pos : neg}`;
  };
  return `${f(lat, "N", "S")} ${f(lon, "E", "W")}`;
}
