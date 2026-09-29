// Forecast tab: a traditional weather forecast for the boat's position. Today at a glance
// (current conditions, high and low, today / tonight in words), the next 24 hours, and a
// daily list with a temperature range bar that opens into day / night detail.
// Everything comes from the hourly forecast the Wind tab already loads, so it works offline.

import * as D from "./daily.js";
import { $, ago, clear, compass, dirArrow, h, hourShort, LEVELS, levelIcon, s } from "./dom.js";
import * as T from "./text.js";
import * as U from "./units.js";

const fmtWeekday = new Intl.DateTimeFormat(undefined, { weekday: "short" });
const fmtWeekdayLong = new Intl.DateTimeFormat(undefined, { weekday: "long" });
const fmtDate = new Intl.DateTimeFormat(undefined, { day: "numeric", month: "short" });

const F = { forecast: null, now: null, status: null, open: new Set(), heroKey: "" };

// ------------------------------------------------------------------ icons

// Weather pictograms on a 48 x 48 grid, coloured by CSS (--wx-* tokens per theme). Overlapping
// shapes are separated by a ring in the surface colour, never by an outline.

const CLOUD = [["circle", { cx: 14, cy: 30, r: 7 }], ["circle", { cx: 24, cy: 23, r: 10 }],
  ["circle", { cx: 34, cy: 29, r: 8 }], ["rect", { x: 14, y: 27, width: 20, height: 10 }]];

function cloud(tx, ty, sc, dark = false) {
  const shapes = (cls) => CLOUD.map(([tag, a]) => s(tag, { ...a, class: cls }));
  return s("g", { transform: `translate(${tx} ${ty}) scale(${sc})` },
    s("g", { class: "wx-ring", "stroke-width": 5 / sc }, shapes(null)),
    s("g", { class: dark ? "wx-cloud-dark" : "wx-cloud" }, shapes(null)));
}

function sun(cx, cy, r) {
  const rays = [];
  for (let a = 0; a < 360; a += 45) {
    const rad = (a * Math.PI) / 180;
    rays.push(s("line", { x1: cx + Math.sin(rad) * (r + 3.5), y1: cy - Math.cos(rad) * (r + 3.5),
      x2: cx + Math.sin(rad) * (r + 7), y2: cy - Math.cos(rad) * (r + 7) }));
  }
  return s("g", {}, s("g", { class: "wx-ray" }, rays), s("circle", { class: "wx-sun", cx, cy, r }));
}

// A crescent: the disc of radius r minus a disc shifted up and right.
function moon(cx, cy, r) {
  const ox = cx + r * 0.55;
  const oy = cy - r * 0.45;
  const r2 = r * 0.9;
  const d = Math.hypot(ox - cx, oy - cy);
  const a = (r * r - r2 * r2 + d * d) / (2 * d);
  const hh = Math.sqrt(Math.max(0, r * r - a * a));
  const mx = cx + (a * (ox - cx)) / d;
  const my = cy + (a * (oy - cy)) / d;
  const p1 = [mx + (hh * (oy - cy)) / d, my - (hh * (ox - cx)) / d];
  const p2 = [mx - (hh * (oy - cy)) / d, my + (hh * (ox - cx)) / d];
  const f = (p) => `${p[0].toFixed(2)} ${p[1].toFixed(2)}`;
  return s("path", { class: "wx-moon", d: `M${f(p1)} A${r} ${r} 0 1 0 ${f(p2)} A${r2} ${r2} 0 0 1 ${f(p1)} Z` });
}

const light = (night, cx, cy, r) => (night ? moon(cx, cy, r * 1.25) : sun(cx, cy, r));
const drops = (xs, y1, y2) => s("g", { class: "wx-drop" }, xs.map((x) => s("line", { x1: x, y1, x2: x - 2.2, y2 })));
const flake = (x, y) => s("g", { class: "wx-flake" },
  [0, 60, 120].map((a) => {
    const r = (a * Math.PI) / 180;
    return s("line", { x1: x - Math.sin(r) * 3.2, y1: y - Math.cos(r) * 3.2, x2: x + Math.sin(r) * 3.2, y2: y + Math.cos(r) * 3.2 });
  }));

const DRAW = {
  clear: (n) => [n ? moon(24, 24, 13) : sun(24, 24, 8.5)],
  "mostly-clear": (n) => [light(n, 20, 19, 7.5), cloud(17, 18, 0.66)],
  "partly-cloudy": (n) => [light(n, 17, 16, 7), cloud(6, 7, 0.9)],
  "mostly-cloudy": (n) => [light(n, 31, 14, 6), cloud(0, 6, 1)],
  cloudy: () => [cloud(12, -3, 0.72, true), cloud(0, 5, 1)],
  fog: () => [s("g", { class: "wx-fog" },
    s("line", { x1: 9, y1: 15, x2: 39, y2: 15 }), s("line", { x1: 5, y1: 22, x2: 43, y2: 22 }),
    s("line", { x1: 9, y1: 29, x2: 39, y2: 29 }), s("line", { x1: 13, y1: 36, x2: 35, y2: 36 }))],
  drizzle: () => [cloud(0, -5, 1), s("g", { class: "wx-dot" },
    [[16, 39], [24, 41], [32, 39], [20, 45], [28, 45]].map(([cx, cy]) => s("circle", { cx, cy, r: 1.7 })))],
  rain: () => [cloud(0, -5, 1, true), drops([18, 26, 34], 37, 44)],
  "heavy-rain": () => [cloud(0, -5, 1, true), drops([14, 21, 28, 35], 36, 46)],
  showers: (n) => [light(n, 14, 12, 6), cloud(2, -2, 0.95), drops([20, 29], 38, 45)],
  sleet: () => [cloud(0, -5, 1), drops([17, 33], 37, 44), flake(25, 41)],
  snow: () => [cloud(0, -5, 1), flake(16, 40), flake(26, 43), flake(35, 39)],
  "snow-showers": (n) => [light(n, 14, 12, 6), cloud(2, -2, 0.95), flake(20, 41), flake(30, 42)],
  thunder: () => [cloud(0, -6, 1, true), drops([13, 37], 35, 41),
    s("path", { class: "wx-bolt", d: "M26 31 L19 40 L24 40 L21 47 L31 36 L26 36 L29 31 Z" })],
  hail: () => [cloud(0, -6, 1, true),
    s("path", { class: "wx-bolt", d: "M26 31 L19 40 L24 40 L21 47 L31 36 L26 36 L29 31 Z" }),
    s("g", { class: "wx-hail" }, s("circle", { cx: 13, cy: 39, r: 2.2 }), s("circle", { cx: 37, cy: 38, r: 2.2 }), s("circle", { cx: 35, cy: 45, r: 2.2 }))],
};

/** An inline SVG weather icon for a condition ({icon, night, text}). */
export function wxIcon(cond, size = 32, label = true) {
  const draw = DRAW[cond?.icon] || DRAW["partly-cloudy"];
  const attrs = { class: "wx-icon", viewBox: "0 0 48 48", width: size, height: size };
  if (label && cond?.text) Object.assign(attrs, { role: "img", "aria-label": cond.text });
  else attrs["aria-hidden"] = "true";
  return s("svg", attrs, draw(Boolean(cond?.night)));
}

function sunEventIcon(kind, size = 32) {
  const rays = [-60, -30, 0, 30, 60].map((a) => {
    const r = (a * Math.PI) / 180;
    return s("line", { x1: 24 + Math.sin(r) * 11, y1: 32 - Math.cos(r) * 11, x2: 24 + Math.sin(r) * 14.5, y2: 32 - Math.cos(r) * 14.5 });
  });
  const arrow = kind === "rise" ? "M20 11 L24 6 L28 11" : "M20 6 L24 11 L28 6";
  return s("svg", { class: "wx-icon", viewBox: "0 0 48 48", width: size, height: size, "aria-hidden": "true" },
    s("g", { class: "wx-ray" }, rays),
    s("path", { class: "wx-sun", d: "M16 32 A8 8 0 0 1 32 32 Z" }),
    s("line", { class: "wx-horizon", x1: 7, y1: 35, x2: 41, y2: 35 }),
    s("path", { class: "wx-arrow", d: arrow }));
}

function dropIcon(size = 12) {
  return s("svg", { class: "drop-icon", viewBox: "0 0 12 12", width: size, height: size, "aria-hidden": "true" },
    s("path", { d: "M6 1 C6 1 2 5.6 2 7.8 A4 4 0 0 0 10 7.8 C10 5.6 6 1 6 1 Z" }));
}

const srOnly = (text) => h("span", { class: "sr-only", text });

// ------------------------------------------------------------------ formatting

const deg = (c) => (c == null ? "–" : `${U.fmtTemp(c)}°`);
// A rain amount, or null when it rounds to nothing in the display unit.
const rainText = (mm) => {
  const v = mm == null ? "" : U.fmtPrecip(mm);
  return v && v !== "–" ? `${v} ${U.precipUnit()}` : null;
};

// Dew point from temperature and humidity (Magnus formula), so the tile agrees with the
// humidity it shows whether that came from instruments or the forecast.
function dewPoint(tempC, rh) {
  if (tempC == null || !rh) return null;
  const g = Math.log(rh / 100) + (17.625 * tempC) / (243.04 + tempC);
  return (243.04 * g) / (17.625 - g);
}
const pct = (p) => `${Math.round(p / 10) * 10}%`;
const showPop = (p) => p != null && p >= 20;

function windRange(w) {
  if (w.max == null) return "–";
  const lo = U.fmtSpeed(w.min);
  const hi = U.fmtSpeed(w.max);
  return lo === hi ? hi : `${lo}–${hi}`;
}

const dayName = (day) => (day.today ? "Today" : fmtWeekday.format(new Date(day.t * 1000)));

function periodNames(day) {
  if (day.today) return ["Today", "Tonight"];
  const w = fmtWeekdayLong.format(new Date(day.t * 1000));
  return [w, `${w} night`];
}

function duration(sec) {
  const m = Math.round(sec / 60);
  return `${Math.floor(m / 60)} h ${String(m % 60).padStart(2, "0")} min`;
}

function tile(label, value, unit, sub) {
  return h("div", { class: "tile" },
    h("div", { class: "tile-label", text: label }),
    h("div", { class: "tile-value" }, value, unit ? h("small", { text: ` ${unit}` }) : null),
    sub ? h("div", { class: "tile-sub" }, sub) : null);
}

// Sun times for the day that contains t, at the forecast position (local noon of that day).
function sunFor(t, pos) {
  if (!pos) return null;
  const d = new Date(t * 1000);
  d.setHours(12, 0, 0, 0);
  return D.sunTimes(d.getTime() / 1000, pos.lat, pos.lon);
}

// ------------------------------------------------------------------ model

function model() {
  const fc = F.forecast;
  if (!fc?.series?.time?.length) return null;
  const now = Date.now() / 1000;
  const rows = D.hourRows(fc.series, fc.assessment);
  const days = D.buildDays(rows, { now, maxDays: 16 });
  const cur = rows.find((r) => r.t <= now && now < r.t + 3600) || null;
  const pos = fc.lat != null && fc.lon != null ? { lat: fc.lat, lon: fc.lon } : F.now?.position || null;
  return { fc, now, rows, days, cur, pos };
}

// ------------------------------------------------------------------ today

function renderHero(m) {
  const box = clear($("#fc-hero"));
  const today = m.days.find((d) => d.today);
  const n = F.now;
  const air = n?.air?.temp_c;
  const temp = air?.value ?? m.cur?.temp ?? null;
  const cond = m.cur ? D.hourCondition(m.cur) : null;
  const rh = n?.air?.humidity_pct?.value ?? m.cur?.rh ?? null;
  const wind = m.cur?.wind ?? n?.wind?.value ?? null;     // hourly wind: steadier than a gusty anemometer
  const feels = D.apparentTemp(temp, rh, wind);

  const meta = [];
  if (feels != null && temp != null && Math.round(U.temp(feels)) !== Math.round(U.temp(temp))) meta.push(`Feels like ${deg(feels)}`);
  if (today?.hi) meta.push(`High ${deg(today.hi.value)}`);
  if (today?.lo) meta.push(`Low ${deg(today.lo.value)}`);

  box.append(
    h("div", { class: "fc-hero-main" },
      cond ? wxIcon(cond, 88) : null,
      h("div", {},
        h("div", { class: "hero-value" }, temp == null ? "–" : U.fmtTemp(temp), h("span", { class: "hero-unit", text: U.tempUnit() })),
        h("div", { class: "hero-sub", text: cond?.text || "–" }),
        h("div", { class: "hero-meta", text: meta.join(" · ") }),
        air?.source === "instrument" ? h("div", { class: "src", text: "Temperature from your instruments" }) : null)));

  const periods = clear($("#fc-periods"));
  for (const p of D.upcomingPeriods(m.days, m.now)) {
    periods.append(h("p", { class: "fc-period" }, h("b", { text: p.label }), " ", D.periodText(p.period, { night: p.night })));
  }

  const tiles = clear($("#fc-tiles"));
  if (!today) return;
  const rainToday = rainText(today.precip);
  tiles.append(tile("Rain today", today.prob == null ? "–" : pct(today.prob), "", rainToday ? `${rainToday} in total` : "Little or none"));
  tiles.append(tile("Wind today", windRange(today.wind), U.speedUnit(),
    [today.wind.dir == null ? null : compass(today.wind.dir), today.wind.gust == null ? null : `gusts ${U.fmtSpeed(today.wind.gust)}`]
      .filter(Boolean).join(" · ")));
  if (rh != null) {
    const dew = dewPoint(temp, rh);
    tiles.append(tile("Humidity", `${Math.round(rh)}%`, "", dew == null ? "" : `Dew point ${deg(dew)}`));
  }
  const vis = n?.air?.visibility_m ?? m.cur?.vis;
  if (vis != null) {
    tiles.append(tile("Visibility", U.fmtVisibility(vis), U.visibilityUnit(),
      today.visMin != null && today.visMin < 2000 ? `down to ${U.fmtVisibility(today.visMin)} ${U.visibilityUnit()} today` : "forecast"));
  }
  const sunT = sunFor(m.now, m.pos);
  if (sunT?.rise) {
    tiles.append(tile("Sunrise", D.fmtClock(sunT.rise), "", `${duration(sunT.set - sunT.rise)} of daylight`));
    tiles.append(tile("Sunset", D.fmtClock(sunT.set), "", sunT.set > m.now ? `in ${duration(sunT.set - m.now)}` : "The sun has set"));
  } else if (sunT?.polar) {
    tiles.append(tile("Sun", sunT.polar === "day" ? "Up all day" : "Down all day", "", "polar " + sunT.polar));
  }
  if (today.worst) {
    const l = today.worst.level;
    tiles.append(tile("Sailing today", h("span", { class: "lvl-inline" }, levelIcon(l, 18), LEVELS[l].label), "",
      T.firstReason(today.worst) || "within your limits"));
  }
}

// ------------------------------------------------------------------ next 24 hours

function renderHours(m) {
  const list = clear($("#fc-hours"));
  const start = Math.floor(m.now / 3600) * 3600;
  const hours = m.rows.filter((r) => r.t >= start && r.t < start + 24 * 3600);
  const events = [];
  for (const t of [m.now - 86400, m.now, m.now + 86400]) {
    const st = sunFor(t, m.pos);
    if (st?.rise) events.push({ kind: "rise", t: st.rise }, { kind: "set", t: st.set });
  }
  for (const r of hours) {
    const c = D.hourCondition(r);
    const first = r === hours[0];
    const midnight = r.hour === 0 && !first;
    const label = first ? "Now" : midnight ? fmtWeekday.format(new Date(r.t * 1000)) : hourShort(r.t);
    const pop = showPop(r.prob);
    const spoken = [label, c.text, r.temp == null ? null : `${U.fmtTemp(r.temp)}${U.tempUnit()}`,
      pop ? `${pct(r.prob)} chance of precipitation` : null,
      r.wind == null ? null : `wind ${compass(r.dir)} ${U.fmtSpeed(r.wind)} ${U.speedUnit()}`].filter(Boolean).join(", ");
    list.append(h("li", { class: `fc-hour${midnight ? " midnight" : ""}${first ? " now" : ""}`, "aria-label": spoken },
      h("span", { class: "h-time", text: label }),
      wxIcon(c, 32, false),
      h("span", { class: "h-temp", text: deg(r.temp) }),
      h("span", { class: "h-pop" }, pop ? [dropIcon(10), pct(r.prob)] : null),
      h("span", { class: "h-wind" }, r.wind == null ? "–" : [r.dir == null ? null : dirArrow(r.dir, 12), U.fmtSpeed(r.wind)])));
    for (const e of events) {
      if (e.t >= Math.max(r.t, m.now) && e.t < r.t + 3600) {
        const name = e.kind === "rise" ? "Sunrise" : "Sunset";
        list.append(h("li", { class: "fc-hour sun-event", "aria-label": `${name} ${D.fmtClock(e.t)}` },
          h("span", { class: "h-time", text: D.fmtClock(e.t) }), sunEventIcon(e.kind, 32),
          h("span", { class: "h-temp small", text: name }), h("span", { class: "h-pop" }), h("span", { class: "h-wind" })));
      }
    }
  }
  $("#fc-hours-units").textContent = `${U.tempUnit()} · wind in ${U.speedUnit()}`;
}

// ------------------------------------------------------------------ days

function rangeBar(day, lo, hi, nowTemp) {
  const span = Math.max(hi - lo, 1);
  const x = (v) => ((v - lo) / span) * 100;
  const bar = h("span", { class: "d-bar", "aria-hidden": "true" });
  if (day.lo && day.hi) {
    const a = x(day.lo.value);
    const b = x(day.hi.value);
    bar.append(h("span", { class: "d-fill", style: { left: `${a}%`, width: `${Math.max(b - a, 2)}%` } }));
  }
  if (nowTemp != null) bar.append(h("span", { class: "d-now", style: { left: `${Math.min(100, Math.max(0, x(nowTemp)))}%` } }));
  return bar;
}

function partCell(p, now) {
  const pop = showPop(p.prob);
  return h("div", { class: `d-part${p.end <= now ? " past" : ""}` },
    h("div", { class: "p-name", text: p.name }),
    wxIcon(p.cond, 32),
    h("div", { class: "p-temp", text: deg(p.temp) }),
    h("div", { class: "p-pop" }, pop ? [dropIcon(10), pct(p.prob)] : "–"),
    h("div", { class: "p-wind" }, p.wind.max == null ? "–" : [p.wind.dir == null ? null : dirArrow(p.wind.dir, 12), `${windRange(p.wind)} ${U.speedUnit()}`]));
}

function dayDetail(day, m) {
  const [dayLabel, nightLabel] = periodNames(day);
  const texts = [];
  if (day.day) texts.push(h("p", {}, h("b", { text: dayLabel }), " ", D.periodText(day.day)));
  if (day.night) texts.push(h("p", {}, h("b", { text: nightLabel }), " ", D.periodText(day.night, { night: true })));
  const sunT = sunFor(day.t, m.pos);
  const facts = [];
  const fact = (k, v, wide = false) => { if (v) facts.push(h("div", { class: wide ? "wide" : null }, h("dt", { text: k }), h("dd", {}, v))); };
  if (sunT?.rise) {
    fact("Sunrise", D.fmtClock(sunT.rise));
    fact("Sunset", D.fmtClock(sunT.set));
  }
  if (day.precip != null) {
    const rain = rainText(day.precip);
    fact("Rain", rain ? `${rain}${day.wetHours ? ` over ${day.wetHours} h` : ""}` : "None");
  }
  fact("Strongest gust", day.wind.gust == null ? null : `${U.fmtSpeed(day.wind.gust)} ${U.speedUnit()}`);
  fact("Humidity", day.rh.min == null ? null : `${Math.round(day.rh.min)}–${Math.round(day.rh.max)}%`);
  if (day.worst) {
    fact("Sailing", h("span", { class: "lvl-inline" }, levelIcon(day.worst.level, 16),
      `${LEVELS[day.worst.level].label}${T.firstReason(day.worst) ? `: ${T.firstReason(day.worst)}` : ""}`), true);
  }
  if (day.partial && !day.today) fact("Forecast", `ends ${D.fmtClock(day.rows[day.rows.length - 1].t + 3600)}`);
  return h("div", { class: "d-detail" },
    h("div", { class: "d-texts" }, texts),
    h("div", { class: "d-parts" }, day.parts.map((p) => partCell(p, m.now))),
    h("dl", { class: "d-facts" }, facts));
}

function renderDays(m) {
  const box = clear($("#fc-days"));
  $("#fc-days-title").textContent = m.days.length ? `${m.days.length}-day forecast` : "Forecast";
  if (!m.days.length) {
    box.append(h("p", { class: "muted", text: "The saved forecast has run out. A new one downloads when the server is online again." }));
    $("#fc-note").textContent = "";
    return;
  }
  const los = m.days.map((d) => d.lo?.value).filter((v) => v != null);
  const his = m.days.map((d) => d.hi?.value).filter((v) => v != null);
  const lo = Math.min(...los);
  const hi = Math.max(...his);
  const nowTemp = F.now?.air?.temp_c?.value ?? m.cur?.temp ?? null;
  for (const day of m.days) {
    const pop = showPop(day.prob);
    const name = dayName(day);
    const date = fmtDate.format(new Date(day.t * 1000));
    const lvl = day.worst?.level;
    const details = h("details", { class: "fc-day", open: F.open.has(day.key) || null },
      h("summary", { class: "d-row" },
        h("span", { class: "d-name", text: name }),
        h("span", { class: "d-date", text: date }),
        h("span", { class: "d-icon" }, wxIcon(day.cond, 36)),
        h("span", { class: "d-pop" }, pop ? [dropIcon(11), pct(day.prob), srOnly(" chance of precipitation")] : null),
        h("span", { class: "d-lo" }, srOnly("Low "), deg(day.lo?.value)),
        rangeBar(day, lo, hi, day.today ? nowTemp : null),
        h("span", { class: "d-hi" }, srOnly("High "), deg(day.hi?.value)),
        h("span", { class: "d-sub" },
          h("span", { class: "d-cond", text: day.cond?.text || "", title: day.cond?.text || null }),
          h("span", { class: "d-wind" }, day.wind.max == null ? null
            : [day.wind.dir == null ? null : dirArrow(day.wind.dir, 12),
              day.wind.dir == null ? null : h("span", { class: "d-compass", text: `${compass(day.wind.dir)} ` }),
              `${windRange(day.wind)} ${U.speedUnit()}`]),
          h("span", { class: "d-level", title: lvl ? `Sailing: ${LEVELS[lvl].label}` : null },
            lvl ? [levelIcon(lvl, 16), srOnly(`Sailing: ${LEVELS[lvl].label}`)] : null)),
        h("span", { class: "d-chev", "aria-hidden": "true" })),
      dayDetail(day, m));
    details.addEventListener("toggle", () => { if (details.open) F.open.add(day.key); else F.open.delete(day.key); });
    box.append(details);
  }
  const last = m.days[m.days.length - 1];
  const notes = [];
  if (last.partial) notes.push(`The forecast ends ${fmtWeekday.format(new Date(last.t * 1000))} at ${D.fmtClock(last.rows[last.rows.length - 1].t + 3600)}.`);
  if (F.status?.bandwidth?.saver) notes.push("Data saver is on: the server downloads 3 days.");
  notes.push("Days run midnight to midnight on this device. Tap a day for the details.");
  $("#fc-note").textContent = notes.join(" ");
}

// ------------------------------------------------------------------ entry points

function renderStamp(m) {
  const run = m.fc.run;
  $("#fc-stamp").textContent = run ? `${run.model && run.model !== "best_match" ? `${run.model} · ` : ""}updated ${ago(run.age_s)}` : "";
}

/** Draw the whole tab from the latest forecast (and /api/now, /api/status when known). */
export function renderForecast({ forecast, now, status } = {}) {
  if (forecast !== undefined) F.forecast = forecast;
  if (now !== undefined) F.now = now;
  if (status !== undefined) F.status = status;
  const empty = $("#fc-empty");
  const m = model();
  empty.hidden = Boolean(m);
  if (!m && F.forecast) empty.textContent = "The saved forecast has run out. A new one downloads when the server is online again.";
  for (const id of ["#fc-today", "#fc-hours-card", "#fc-days-card"]) $(id).hidden = !m;
  if (!m) return;
  renderStamp(m);
  renderHero(m);
  renderHours(m);
  renderDays(m);
  F.heroKey = heroKey(m);
}

const heroKey = (m) => [m.cur?.t, F.now?.air?.temp_c?.value == null ? "" : U.fmtTemp(F.now.air.temp_c.value),
  F.now?.air?.humidity_pct?.value == null ? "" : Math.round(F.now.air.humidity_pct.value)].join("|");

/** New /api/now: refresh "today" only when what it shows changed (keeps open days open). */
export function forecastNow(now) {
  F.now = now;
  const m = model();
  if (!m) return;
  const key = heroKey(m);
  if (key === F.heroKey) return;
  // A new hour: everything moves along.
  if (m.cur?.t !== Number(F.heroKey.split("|")[0])) { renderForecast(); return; }
  renderHero(m);
  F.heroKey = key;
}

/** Latest /api/status (for the data-saver note); shown at the next render. */
export function forecastStatus(status) {
  F.status = status;
}

/** No forecast downloaded yet (404) or the server can't be reached with nothing saved. */
export function forecastMissing(text) {
  if (F.forecast) return;
  $("#fc-empty").hidden = false;
  $("#fc-empty").textContent = text;
  for (const id of ["#fc-today", "#fc-hours-card", "#fc-days-card"]) $(id).hidden = true;
}
