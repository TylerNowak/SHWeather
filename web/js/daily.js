// Forecast-tab logic: turns the hourly forecast (/api/forecast) into conditions ("Partly
// cloudy", "Rain likely"), calendar days with highs and lows, day / night periods worded
// like a traditional forecast, and sunrise / sunset. Pure functions with no DOM, so they
// run (and are tested) in Node. Days are the phone's local calendar days.

import { compass } from "./dom.js";
import * as U from "./units.js";

// ------------------------------------------------------------------ WMO weather codes

// Open-Meteo reports WMO code 4677 "present weather" in a reduced set.
// [text, icon, family, intensity 0-2]
const CODES = {
  45: ["Fog", "fog", "fog", 0], 48: ["Freezing fog", "fog", "fog", 1],
  51: ["Light drizzle", "drizzle", "drizzle", 0], 53: ["Drizzle", "drizzle", "drizzle", 1], 55: ["Heavy drizzle", "drizzle", "drizzle", 2],
  56: ["Freezing drizzle", "sleet", "freezing", 0], 57: ["Freezing drizzle", "sleet", "freezing", 2],
  61: ["Light rain", "rain", "rain", 0], 63: ["Rain", "rain", "rain", 1], 65: ["Heavy rain", "heavy-rain", "rain", 2],
  66: ["Freezing rain", "sleet", "freezing", 0], 67: ["Freezing rain", "sleet", "freezing", 2],
  71: ["Light snow", "snow", "snow", 0], 73: ["Snow", "snow", "snow", 1], 75: ["Heavy snow", "snow", "snow", 2],
  77: ["Snow grains", "snow", "snow", 0],
  80: ["Light showers", "showers", "showers", 0], 81: ["Showers", "showers", "showers", 1], 82: ["Heavy showers", "showers", "showers", 2],
  85: ["Snow showers", "snow-showers", "snow-showers", 0], 86: ["Heavy snow showers", "snow-showers", "snow-showers", 2],
  95: ["Thunderstorms", "thunder", "thunder", 1], 96: ["Thunderstorms with hail", "hail", "thunder", 2],
  99: ["Thunderstorms with hail", "hail", "thunder", 2],
};

// Words for "a chance of ..." phrases, per family.
const NOUN = {
  drizzle: "drizzle", freezing: "freezing rain", rain: "rain", showers: "showers", snow: "snow",
  "snow-showers": "snow showers", thunder: "thunderstorms",
};
// Which family wins a tie in hours: the more disruptive one.
const FAMILY_RANK = { drizzle: 1, showers: 2, rain: 3, "snow-showers": 4, snow: 5, freezing: 6, thunder: 7 };

// Sky cover, NWS wording. Thresholds are total cloud cover in %.
const SKY = [
  { max: 20, icon: "clear", day: "Sunny", night: "Clear" },
  { max: 45, icon: "mostly-clear", day: "Mostly sunny", night: "Mostly clear" },
  { max: 70, icon: "partly-cloudy", day: "Partly cloudy", night: "Partly cloudy" },
  { max: 88, icon: "mostly-cloudy", day: "Mostly cloudy", night: "Mostly cloudy" },
  { max: Infinity, icon: "cloudy", day: "Cloudy", night: "Cloudy" },
];
const SKY_FROM_CODE = { 0: 0, 1: 1, 2: 2, 3: 4 };

export const isWet = (code) => code != null && code >= 51;
export const isFog = (code) => code === 45 || code === 48;

function skyLevel(cloud, code) {
  if (cloud != null) return SKY.findIndex((x) => cloud < x.max);
  return SKY_FROM_CODE[code] ?? null;
}

function skyCondition(level, night) {
  const k = SKY[level ?? 2];
  return { icon: k.icon, text: night ? k.night : k.day, night, family: "sky", sky: level ?? 2 };
}

function codeCondition(code, night) {
  const c = CODES[code];
  return { icon: c[1], text: c[0], night, family: c[2], intensity: c[3], code };
}

/** Condition for one hour: {icon, text, night, family, ...}. */
export function hourCondition(row) {
  const night = row.isDay === 0;
  if (row.code != null && CODES[row.code]) return codeCondition(row.code, night);
  // No weather code (or a sky code): sky cover from cloud %, else rain from the amount.
  if (row.code == null && row.precip != null && row.precip >= 0.3) return codeCondition(row.precip >= 4 ? 65 : 61, night);
  return skyCondition(skyLevel(row.cloud, row.code), night);
}

/** Chance wording from a probability of precipitation (NWS usage). */
export function chanceWord(prob) {
  if (prob == null) return "";
  if (prob < 25) return "slight";
  if (prob < 55) return "chance";
  if (prob < 75) return "likely";
  return "";
}

const cap = (t) => t.charAt(0).toUpperCase() + t.slice(1);

function precipPhrase(family, code, prob, short = false) {
  const noun = code === 56 || code === 57 ? "freezing drizzle" : NOUN[family];
  const w = chanceWord(prob);
  if (w === "slight") return short ? `Slight chance of ${noun}` : `A slight chance of ${noun}`;
  if (w === "chance") return short ? `Chance of ${noun}` : `A chance of ${noun}`;
  if (w === "likely") return `${cap(noun)} likely`;
  return CODES[code][0];
}

/**
 * Representative condition of several hours (a day, a night, a part of a day).
 * Thunder anywhere wins (it is what a sailor must know), then precipitation lasting two
 * hours or more, then fog lasting a quarter of the time, else the average sky cover of the
 * daylight hours. `night` picks moon icons and night wording.
 */
export function periodCondition(rows, { night = false } = {}) {
  if (!rows.length) return null;
  const prob = maxOf(rows, "prob");
  const thunder = rows.filter((r) => r.code >= 95 && CODES[r.code]);
  if (thunder.length) {
    const code = Math.max(...thunder.map((r) => r.code));
    return { ...codeCondition(code, night), text: precipPhrase("thunder", code, prob, true), prob, wetHours: thunder.length };
  }
  const wet = rows.filter((r) => isWet(r.code) && CODES[r.code]);
  const total = sumOf(rows, "precip");
  if (wet.length >= 2 || (wet.length && total >= 1)) {
    const hours = {};
    for (const r of wet) hours[CODES[r.code][2]] = (hours[CODES[r.code][2]] || 0) + 1;
    const family = Object.keys(hours).sort((a, b) => hours[b] - hours[a] || FAMILY_RANK[b] - FAMILY_RANK[a])[0];
    const code = Math.max(...wet.filter((r) => CODES[r.code][2] === family).map((r) => r.code));
    return { ...codeCondition(code, night), text: precipPhrase(family, code, prob, true), prob, wetHours: wet.length };
  }
  const fog = rows.filter((r) => isFog(r.code));
  if (fog.length >= Math.min(3, Math.max(2, rows.length / 4))) {
    const cond = codeCondition(fog.some((r) => r.code === 48) ? 48 : 45, night);
    const clear = rows.filter((r) => !isFog(r.code));
    const morning = !night && fog.every((r) => r.hour != null && r.hour < 12) && clear.some((r) => r.hour >= 12);
    if (morning) cond.text = `Morning fog, then ${skyOf(clear, night).text.toLowerCase()}`;
    return { ...cond, prob };
  }
  return { ...skyOf(rows, night), prob };
}

// Sky cover of some hours: the average cloud cover of the daylight ones (all of them at night).
function skyOf(rows, night) {
  const lit = night ? rows : rows.filter((r) => r.isDay !== 0);
  const use = lit.length ? lit : rows;
  const clouds = use.map((r) => r.cloud).filter((v) => v != null);
  const level = clouds.length ? skyLevel(clouds.reduce((a, b) => a + b, 0) / clouds.length, null)
    : skyLevel(null, mode(use.map((r) => r.code).filter((c) => c != null && c <= 3)));
  return skyCondition(level, night);
}

// ------------------------------------------------------------------ hourly rows

const localKey = (t) => {
  const d = new Date(t * 1000);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
};
const localHour = (t) => new Date(t * 1000).getHours();

/** Device-local calendar: the day each hour belongs to, and its hour of day. */
export const LOCAL = { key: localKey, hour: localHour };

/** One object per forecast hour, with the fields the Forecast tab uses. */
export function hourRows(series, assessment = [], cal = LOCAL) {
  const s = series || {};
  const at = (f, i) => (s[f] ? s[f][i] ?? null : null);
  return (s.time || []).map((t, i) => ({
    t, key: cal.key(t), hour: cal.hour(t),
    temp: at("temp_c", i), code: at("weather_code", i), cloud: at("cloud_pct", i),
    precip: at("precip_mm", i), prob: at("precip_prob_pct", i), isDay: at("is_day", i),
    wind: at("wind_speed_kn", i), gust: at("wind_gust_kn", i), dir: at("wind_dir_deg", i),
    vis: at("visibility_m", i), rh: at("humidity_pct", i), dew: at("dewpoint_c", i),
    assessment: assessment[i] || null,
  }));
}

function maxOf(rows, f) {
  let best = null;
  for (const r of rows) if (r[f] != null && (best === null || r[f] > best)) best = r[f];
  return best;
}
function sumOf(rows, f) {
  let sum = null;
  for (const r of rows) if (r[f] != null) sum = (sum || 0) + r[f];
  return sum;
}
function extreme(rows, f, sign) {
  let best = null;
  for (const r of rows) if (r[f] != null && (best === null || sign * (r[f] - best.value) > 0)) best = { value: r[f], t: r.t };
  return best;
}
function mode(values) {
  const n = {};
  let best = null;
  for (const v of values) { n[v] = (n[v] || 0) + 1; if (best === null || n[v] > n[best]) best = v; }
  return best === null ? null : Number(best);
}

/** Mean wind direction (from), weighted by speed, so 350 and 010 average to 000. */
export function meanDirection(rows) {
  let x = 0;
  let y = 0;
  for (const r of rows) {
    if (r.dir == null) continue;
    const w = r.wind ?? 1;
    x += w * Math.sin((r.dir * Math.PI) / 180);
    y += w * Math.cos((r.dir * Math.PI) / 180);
  }
  if (Math.abs(x) < 1e-9 && Math.abs(y) < 1e-9) return null;
  return ((Math.atan2(x, y) * 180) / Math.PI + 360) % 360;
}

const LEVEL_ORDER = ["good", "reef", "caution", "nogo"];

/** Figures for a set of hours: high, low, rain, wind, worst sailing level. */
export function summarize(rows, { night = false } = {}) {
  const levels = rows.map((r) => r.assessment).filter((a) => a && LEVEL_ORDER.includes(a.level));
  const worst = levels.length ? levels.reduce((a, b) => (LEVEL_ORDER.indexOf(b.level) > LEVEL_ORDER.indexOf(a.level) ? b : a)) : null;
  const third = Math.max(1, Math.round(rows.length / 3));
  return {
    hi: extreme(rows, "temp", 1), lo: extreme(rows, "temp", -1),
    prob: maxOf(rows, "prob"), precip: sumOf(rows, "precip"),
    wetHours: rows.filter((r) => isWet(r.code)).length,
    wind: {
      min: extreme(rows, "wind", -1)?.value ?? null, max: extreme(rows, "wind", 1)?.value ?? null,
      gust: maxOf(rows, "gust"), dir: meanDirection(rows),
      // for "becoming": the first and the last third of the period
      dirStart: rows.length >= 3 ? meanDirection(rows.slice(0, third)) : null,
      dirEnd: rows.length >= 3 ? meanDirection(rows.slice(-third)) : null,
    },
    visMin: extreme(rows, "vis", -1)?.value ?? null,
    rh: { min: extreme(rows, "rh", -1)?.value ?? null, max: extreme(rows, "rh", 1)?.value ?? null },
    worst,
    cond: periodCondition(rows, { night }),
  };
}

const PARTS = [
  { name: "Overnight", from: 0, to: 6, night: true },
  { name: "Morning", from: 6, to: 12, night: false },
  { name: "Afternoon", from: 12, to: 18, night: false },
  { name: "Evening", from: 18, to: 24, night: true },
];

/**
 * Calendar days from today on. Today keeps its past hours (a day's high and low cover the
 * whole day). A later day is listed only if the forecast covers its afternoon and at least
 * half of it; `partial` marks a day with some hours missing.
 * Each day has: key, t (first hour), today, rows, summary figures, `day` and `night`
 * periods (06-18 and 18-06 into the next morning) and four parts.
 */
export function buildDays(rows, { now, maxDays = 10, cal = LOCAL } = {}) {
  const todayKey = cal.key(now);
  const groups = [];
  for (const r of rows) {
    if (r.key < todayKey) continue;
    const g = groups[groups.length - 1];
    if (g && g.key === r.key) g.rows.push(r);
    else groups.push({ key: r.key, rows: [r] });
  }
  const days = [];
  groups.forEach((g, i) => {
    const today = g.key === todayKey;
    const hours = new Set(g.rows.map((r) => r.hour));
    if (!today && !(g.rows.length >= 12 && hours.has(14))) return;
    const next = groups[i + 1]?.key === nextKey(g, cal) ? groups[i + 1].rows : [];
    const dayRows = g.rows.filter((r) => r.hour >= 6 && r.hour < 18);
    const nightRows = [...g.rows.filter((r) => r.hour >= 18), ...next.filter((r) => r.hour < 6)];
    days.push({
      key: g.key, t: g.rows[0].t, today, rows: g.rows,
      partial: !(hours.has(0) && hours.has(23)),
      firstHour: g.rows[0].hour, lastHour: g.rows[g.rows.length - 1].hour,
      ...summarize(g.rows),
      // A period is worded only when the forecast covers half of it or more.
      day: dayRows.length >= 6 ? { rows: dayRows, ...summarize(dayRows) } : null,
      night: nightRows.length >= 6 ? { rows: nightRows, ...summarize(nightRows, { night: true }) } : null,
      parts: PARTS.map((p) => {
        const pr = g.rows.filter((r) => r.hour >= p.from && r.hour < p.to);
        if (!pr.length) return null;
        const mid = pr[Math.floor(pr.length / 2)];
        return { ...p, rows: pr, t: pr[0].t, end: pr[pr.length - 1].t + 3600, temp: mid.temp,
          ...summarize(pr, { night: p.night }) };
      }).filter(Boolean),
    });
  });
  return days.slice(0, maxDays);
}

// The key of the calendar day after g (hours 0-5 of it complete g's night).
function nextKey(g, cal) {
  const last = g.rows[g.rows.length - 1];
  return cal.key(last.t + (24 - last.hour) * 3600 + 1800);
}

// ------------------------------------------------------------------ words

const CLOCK = new Intl.DateTimeFormat(undefined, { hour: "numeric", minute: "2-digit" });
// Whole hours in a sentence: "2 PM" on a 12-hour clock, "14:00" on a 24-hour one ("14" alone
// reads badly in "between 14 and 18").
const HOUR = (() => {
  const f = new Intl.DateTimeFormat(undefined, { hour: "numeric" });
  const cycle = f.resolvedOptions().hourCycle;
  return cycle === "h23" || cycle === "h24" ? CLOCK : f;
})();
export const fmtClock = (t) => CLOCK.format(new Date(t * 1000));
export const hourText = (t) => HOUR.format(new Date(t * 1000));

const tempText = (c) => `${U.fmtTemp(c)}${U.tempUnit()}`;

// A rain total for words: "about 8 mm", "about 2.4 mm", "about 0.35 in".
function aboutPrecip(mm) {
  const v = U.precip(mm);
  if (U.precipUnit() === "mm") return v >= 5 ? String(Math.round(v)) : v.toFixed(1);
  return v >= 1 ? v.toFixed(1) : v.toFixed(2);
}

/** "SW 10 to 15 kn, becoming NW, gusts to 25 kn" (or "Light winds"). */
export function windPhrase(sum) {
  const w = sum.wind;
  if (w.max == null) return "";
  if (w.max < 5) return "Light winds";
  const lo = U.fmtSpeed(w.min);
  const hi = U.fmtSpeed(w.max);
  const unit = U.speedUnit();
  let out = `${w.dir == null ? "" : `${compass(w.dir)} `}${lo === hi ? hi : `${lo} to ${hi}`} ${unit}`;
  if (w.dirStart != null && w.dirEnd != null && angleDiff(w.dirStart, w.dirEnd) >= 45
      && compass(w.dirStart) !== compass(w.dirEnd)) {
    out = `${compass(w.dirStart)} ${lo === hi ? hi : `${lo} to ${hi}`} ${unit}, becoming ${compass(w.dirEnd)}`;
  }
  if (w.gust != null && w.gust >= 15 && w.gust >= w.max + 5) out += `, gusts to ${U.fmtSpeed(w.gust)} ${unit}`;
  return `Wind ${out}`;
}

const angleDiff = (a, b) => Math.abs(((a - b + 540) % 360) - 180);

// "after 2 PM", "before 11 AM", "between 2 PM and 6 PM" for the wet (or foggy) hours of a period.
function timing(rows, test) {
  const idx = rows.map((r, i) => (test(r) ? i : -1)).filter((i) => i >= 0);
  if (!idx.length) return "";
  const first = idx[0];
  const last = idx[idx.length - 1];
  if ((last - first + 1) / rows.length >= 0.7) return "";
  if (first === 0) return `, mainly before ${hourText(rows[last].t + 3600)}`;
  if (last === rows.length - 1) return `, mainly after ${hourText(rows[first].t)}`;
  return ` between ${hourText(rows[first].t)} and ${hourText(rows[last].t + 3600)}`;
}

/**
 * A forecast period in words, NWS style:
 * "A chance of showers, mainly after 2 PM. Otherwise mostly sunny. High 21°C. Wind SW 10 to 15 kn."
 */
export function periodText(period, { night = false } = {}) {
  if (!period?.rows?.length) return "";
  const rows = period.rows;
  const c = period.cond;
  const out = [];
  if (c.family !== "sky" && c.family !== "fog") {
    const phrase = precipPhrase(c.family, c.code, c.prob);
    const test = c.family === "thunder" ? (r) => r.code >= 95 : (r) => isWet(r.code);
    out.push(`${phrase}${timing(rows, test)}.`);
    const dry = rows.filter((r) => !isWet(r.code));
    if (dry.length / rows.length >= 0.5) out.push(`Otherwise ${periodCondition(dry, { night }).text.toLowerCase()}.`);
  } else if (c.family === "fog") {
    out.push(`${c.code === 48 ? "Freezing fog" : "Fog"}${timing(rows, (r) => isFog(r.code))}.`);
    const clear = rows.filter((r) => !isFog(r.code));
    const clearsLater = isFog(rows[0].code) && !isFog(rows[rows.length - 1].code);
    if (clear.length) out.push(`${clearsLater ? "Then" : "Otherwise"} ${periodCondition(clear, { night }).text.toLowerCase()}.`);
  } else {
    out.push(`${c.text}.`);
  }
  if (night ? period.lo : period.hi) out.push(night ? `Low ${tempText(period.lo.value)}.` : `High ${tempText(period.hi.value)}.`);
  const wind = windPhrase(period);
  if (wind) out.push(`${wind}.`);
  if (period.precip != null && period.precip >= 1 && c.family !== "sky" && c.family !== "fog") {
    out.push(`${["snow", "snow-showers", "freezing"].includes(c.family) ? "Precipitation" : "Rain"} about ${aboutPrecip(period.precip)} ${U.precipUnit()}.`);
  }
  return out.join(" ");
}

/** Labels for the next two periods starting now: "This afternoon" / "Tonight", etc. */
export function upcomingPeriods(days, now, cal = LOCAL) {
  const today = days.find((d) => d.today);
  if (!today) return [];
  const h = cal.hour(now);
  const ahead = (p) => p && { ...p, rows: p.rows.filter((r) => r.t + 3600 > now) };
  const list = [];
  const tomorrow = days[days.indexOf(today) + 1];
  if (h < 6) {
    // Before dawn: the rest of the night belongs to yesterday's night period.
    const pre = today.rows.filter((r) => r.hour < 6 && r.t + 3600 > now);
    if (pre.length) list.push({ label: "Overnight", night: true, period: { rows: pre, ...summarize(pre, { night: true }) } });
    if (today.day) list.push({ label: "Today", night: false, period: today.day });
  } else if (h < 18) {
    const rest = ahead(today.day);
    if (rest?.rows.length) list.push({ label: h >= 12 ? "This afternoon" : "Today", night: false, period: { rows: rest.rows, ...summarize(rest.rows) } });
    if (today.night) list.push({ label: "Tonight", night: true, period: today.night });
  } else {
    const rest = ahead(today.night);
    if (rest?.rows.length) list.push({ label: "Tonight", night: true, period: { rows: rest.rows, ...summarize(rest.rows, { night: true }) } });
    if (tomorrow?.day) list.push({ label: "Tomorrow", night: false, period: tomorrow.day });
  }
  return list;
}

// ------------------------------------------------------------------ comfort + sun

/**
 * "Feels like" temperature in °C, the NWS way: wind chill at 10 °C and below (with wind
 * over 4.8 km/h), heat index from 27 °C (Rothfusz regression), else the air temperature.
 */
export function apparentTemp(tempC, rhPct, windKn) {
  if (tempC == null) return null;
  const kmh = (windKn ?? 0) * 1.852;
  if (tempC <= 10 && kmh > 4.8) {
    const v = kmh ** 0.16;
    return Math.min(tempC, 13.12 + 0.6215 * tempC - 11.37 * v + 0.3965 * tempC * v);
  }
  if (tempC >= 26.7 && rhPct != null) {
    const T = tempC * 9 / 5 + 32;
    const R = rhPct;
    let hi = 0.5 * (T + 61 + (T - 68) * 1.2 + R * 0.094);
    if ((hi + T) / 2 >= 80) {
      hi = -42.379 + 2.04901523 * T + 10.14333127 * R - 0.22475541 * T * R - 0.00683783 * T * T
        - 0.05481717 * R * R + 0.00122874 * T * T * R + 0.00085282 * T * R * R - 0.00000199 * T * T * R * R;
      if (R < 13 && T >= 80 && T <= 112) hi -= ((13 - R) / 4) * Math.sqrt((17 - Math.abs(T - 95)) / 17);
      if (R > 85 && T >= 80 && T <= 87) hi += ((R - 85) / 10) * ((87 - T) / 5);
    }
    return Math.max(tempC, (hi - 32) * 5 / 9);
  }
  return tempC;
}

/**
 * Sunrise and sunset (epoch seconds) for the calendar day around `t` at lat/lon, using the
 * NOAA sunrise equation (about a minute accurate). {rise, set} or {polar: "day"|"night"}.
 */
export function sunTimes(t, lat, lon) {
  const rad = Math.PI / 180;
  const jd = t / 86400 + 2440587.5;
  const n = Math.round(jd - 2451545.0 + lon / 360);   // the solar noon nearest to t, in days since J2000
  const jStar = n - lon / 360;
  const M = (357.5291 + 0.98560028 * jStar) % 360;
  const C = 1.9148 * Math.sin(M * rad) + 0.02 * Math.sin(2 * M * rad) + 0.0003 * Math.sin(3 * M * rad);
  const L = (M + C + 180 + 102.9372) % 360;
  const transit = 2451545.0 + jStar + 0.0053 * Math.sin(M * rad) - 0.0069 * Math.sin(2 * L * rad);
  const decl = Math.asin(Math.sin(L * rad) * Math.sin(23.4397 * rad));
  const cosW = (Math.sin(-0.833 * rad) - Math.sin(lat * rad) * Math.sin(decl)) / (Math.cos(lat * rad) * Math.cos(decl));
  const toEpoch = (j) => (j - 2440587.5) * 86400;
  if (cosW < -1) return { polar: "day", noon: toEpoch(transit) };
  if (cosW > 1) return { polar: "night", noon: toEpoch(transit) };
  const w = Math.acos(cosW) / rad;
  return { rise: toEpoch(transit - w / 360), set: toEpoch(transit + w / 360), noon: toEpoch(transit) };
}
