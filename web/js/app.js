// SHWeather PWA: polls the weather server (a Pi or a PC aboard) and renders the cockpit
// view. No framework, no build step.

import { api } from "./api.js";
import { lineChart } from "./charts.js";
import { $, ago, clear, compass, dayLongLabel, dirArrow, h, hourShort, isoToEpoch, LEVELS, levelIcon, store, whenLabel } from "./dom.js";
import { forecastMissing, forecastNow, forecastStatus, renderForecast } from "./forecast.js";
import { gpsState, gpsStatusText, initGps, onGpsChange, setGpsEnabled } from "./gps.js";
import { initRadar, radarUnitsChanged, radarUpdate, showRadar } from "./radar.js";
import { gpsFormat, openSettings, secureUrl, setTheme } from "./settings.js";
import * as T from "./text.js";
import * as U from "./units.js";

const state = {
  now: null, forecast: null, obs: null, status: null,
  hours: store.get("shw.hours", 48),
  lastContact: null, error: null,
  tides: null, buoys: null, marineText: null,
};

const SOURCE_NAMES = {
  open_meteo: "Open-Meteo forecast", open_meteo_marine: "Open-Meteo marine", nws_grid: "NWS gridpoint",
  nws_alerts: "NWS alerts", nws_marine_text: "NWS marine text", ndbc: "NDBC buoys", coops: "NOAA tides",
};

// ------------------------------------------------------------------ helpers

function tile(label, value, unit, sub, extra) {
  return h("div", { class: "tile" },
    h("div", { class: "tile-label", text: label }),
    h("div", { class: "tile-value" }, value, unit ? h("small", { text: ` ${unit}` }) : null),
    sub ? h("div", { class: "tile-sub" }, sub) : null, extra || null);
}

const srcLabel = (x) => (x?.source === "instrument" ? "on board" : x?.source === "forecast" ? "forecast" : "");

function nightBands(series) {
  const bands = [];
  const t = series.time;
  const d = series.is_day || [];
  let start = null;
  for (let i = 0; i < t.length; i++) {
    const night = d[i] === 0;
    if (night && start === null) start = t[i] - 1800;
    if (!night && start !== null) { bands.push([start, t[i] - 1800]); start = null; }
  }
  if (start !== null) bands.push([start, t[t.length - 1] + 1800]);
  return bands;
}

const pts = (times, values, conv) => times.map((t, i) => [t, values?.[i] == null ? null : conv(values[i])]);

function windClass(kn) {
  if (kn == null) return "";
  const b = [5, 10, 15, 20, 25, 30].findIndex((x) => kn < x);
  return `w${b === -1 ? 7 : b + 1}`;
}

// ------------------------------------------------------------------ position line + this device's GPS

const SOURCE_LABEL = { gps: "Boat GPS", phone: "Phone GPS", manual: "Set by hand", cli: "Set by hand", home: "Home position" };

function renderPosition() {
  const p = state.now?.position;
  if (!p) return;
  const g = gpsState();
  // The server's position is this device's latest fix: say so.
  const mine = p.source === "phone" && g.enabled && g.fix
    && Math.abs(g.fix.lat - p.lat) < 1e-7 && Math.abs(g.fix.lon - p.lon) < 1e-7;
  const parts = [U.fmtLatLon(p.lat, p.lon), mine ? "This device's GPS" : SOURCE_LABEL[p.source] || p.source];
  if (p.age_s != null && p.age_s >= 15 * 60) parts.push(ago(p.age_s));
  const el = $("#position");
  el.textContent = parts.join(" · ");
  el.title = g.enabled ? gpsStatusText(g, gpsFormat) : "";
}

function renderGpsBanner(st = gpsState()) {
  const b = $("#gps-banner");
  const show = st.enabled && st.status === "denied";
  b.hidden = !show;
  if (!show) return;
  clear(b).append(
    h("span", { text: "Location is blocked for this page, so this device can't send its GPS position to the weather server." }),
    h("button", { class: "btn", type: "button", text: "Settings", onclick: showSettings }),
    h("button", { class: "btn", type: "button", text: "Turn off", onclick: () => setGpsEnabled(false) }));
}

// ------------------------------------------------------------------ connection badge

function renderConn() {
  const b = $("#conn-badge");
  b.className = "badge";
  if (state.error && !state.now) { b.classList.add("down"); b.textContent = "Server unreachable"; return; }
  if (state.error) {
    b.classList.add("down");
    const since = state.now.__cached ? state.now.time * 1000 : state.lastContact;
    b.textContent = `Server unreachable · saved data from ${ago((Date.now() - since) / 1000)}`;
    return;
  }
  const age = state.now?.forecast?.run?.age_s;
  if (age == null) { b.classList.add("old"); b.textContent = "No forecast yet"; return; }
  b.classList.add(age < 6 * 3600 ? "ok" : age < 24 * 3600 ? "stale" : "old");
  b.textContent = `${state.now.online ? "" : "Offline · "}Forecast ${ago(age)}`;
}

// ------------------------------------------------------------------ now

function renderAlerts(now) {
  const box = clear($("#alerts"));
  const items = [];
  for (const w of now.tendency?.warnings || []) {
    items.push(h("div", { class: `alert ${w.level === "danger" ? "critical" : w.level === "warning" ? "critical" : "warning"}` },
      h("div", { style: { display: "flex", gap: "10px", alignItems: "flex-start" } },
        levelIcon(w.level === "caution" ? "reef" : "nogo", 20),
        h("div", {}, h("div", { class: "alert-title", text: "Barometer" }), h("div", { text: T.warning(w) })))));
  }
  for (const a of now.alerts || []) {
    const onset = isoToEpoch(a.onset);
    const ends = isoToEpoch(a.ends);
    const when = [onset && onset > Date.now() / 1000 ? `from ${whenLabel(onset)}` : "in effect", ends ? `until ${whenLabel(ends)}` : ""].join(" ");
    const sev = ["Extreme", "Severe"].includes(a.severity) ? "critical" : a.marine ? "" : "warning";
    items.push(h("details", { class: `alert ${sev}` },
      h("summary", {}, levelIcon(sev === "critical" ? "nogo" : "caution", 20),
        h("div", {}, h("div", { class: "alert-title", text: a.event }), h("div", { class: "alert-when", text: when }))),
      h("pre", { text: [a.headline, a.description, a.instruction].filter(Boolean).join("\n\n") })));
  }
  box.append(...items);
}

function renderNow() {
  const now = state.now;
  if (!now) return;
  renderPosition();

  const w = now.wind;
  const d = now.wind_dir;
  $("#wind-speed").textContent = w ? U.fmtSpeed(w.value) : "–";
  $("#wind-unit").textContent = U.speedUnit();
  $("#wind-dir").textContent = d ? `from ${compass(d.value)} ${Math.round(d.value)}°` : "Direction unknown";
  $("#wind-arrow").style.transform = `rotate(${((d?.value ?? 0) + 180) % 360}deg)`;
  $("#wind-arrow").style.visibility = d ? "visible" : "hidden";
  const meta = [];
  if (now.gust) meta.push(`Gusts ${U.fmtSpeed(now.gust.value)} ${U.speedUnit()}`);
  if (w?.beaufort) meta.push(`Force ${w.beaufort.force}, ${w.beaufort.description.toLowerCase()}`);
  if (w) meta.push(srcLabel(w));
  if (now.__cached) meta.push(`saved ${ago(Date.now() / 1000 - now.time)}`);
  $("#wind-meta").textContent = meta.join(" · ");

  const chip = $("#assessment");
  const lvl = now.assessment?.level || "unknown";
  const why = T.firstReason(now.assessment);
  clear(chip).append(levelIcon(lvl, 16), `${LEVELS[lvl].label}${why ? ` · ${why}` : ""}`);
  chip.hidden = false;

  // tiles
  const tiles = clear($("#tiles"));
  const pr = now.pressure;
  const td = now.tendency;
  if (pr) {
    const arrow = td ? (td.change_hpa > 0.1 ? "↑ " : td.change_hpa < -0.1 ? "↓ " : "→ ") : "";
    tiles.append(tile("Pressure", U.fmtPressure(pr.value), U.pressureUnit(),
      td ? `${arrow}${td.text}, ${U.fmtPressureDelta(td.change_hpa)} ${U.pressureUnit()} in 3 h${td.estimated ? " (est.)" : ""}`
        : srcLabel(pr)));
  }
  const wv = now.waves;
  if (wv?.wave_height_m != null) {
    tiles.append(tile("Waves", U.fmtHeight(wv.wave_height_m), U.heightUnit(),
      [wv.wave_period_s != null ? `${Math.round(wv.wave_period_s)} s` : null, wv.wave_dir_deg != null ? `from ${compass(wv.wave_dir_deg)}` : null].filter(Boolean).join(" · ")));
  }
  if (wv?.swell_height_m != null && wv.swell_height_m > 0.05) {
    tiles.append(tile("Swell", U.fmtHeight(wv.swell_height_m), U.heightUnit(),
      [wv.swell_period_s != null ? `${Math.round(wv.swell_period_s)} s` : null, wv.swell_dir_deg != null ? `from ${compass(wv.swell_dir_deg)}` : null].filter(Boolean).join(" · ")));
  }
  const water = now.sea?.water_temp_c;
  if (water) tiles.append(tile("Water", U.fmtTemp(water.value), U.tempUnit(), srcLabel(water)));
  const air = now.air?.temp_c;
  if (air) tiles.append(tile("Air", U.fmtTemp(air.value), U.tempUnit(), srcLabel(air)));
  if (now.air?.visibility_m != null) {
    tiles.append(tile("Visibility", U.fmtVisibility(now.air.visibility_m), U.visibilityUnit(), "forecast"));
  }
  const rain = now.air?.precip_mm != null ? U.fmtPrecip(now.air.precip_mm) : "";
  if (rain) tiles.append(tile("Rain", rain, U.precipUnit(), "this hour, forecast"));
  if (now.sea?.current_speed_kn != null && now.sea.current_speed_kn > 0.05) {
    tiles.append(tile("Current", U.fmtSpeed(now.sea.current_speed_kn, 1), U.speedUnit(),
      now.sea.current_dir_deg != null ? `setting ${compass(now.sea.current_dir_deg)}` : ""));
  }

  // local correction note
  const lc = now.forecast?.local_correction;
  const note = $("#local-note");
  if (lc?.active) {
    const parts = [];
    if (lc.wind_ratio != null && Math.abs(lc.wind_ratio - 1) >= 0.05) {
      parts.push(`${Math.round(Math.abs(lc.wind_ratio - 1) * 100)}% ${lc.wind_ratio > 1 ? "more" : "less"} wind`);
    }
    if (lc.dir_offset_deg != null && Math.abs(lc.dir_offset_deg) >= 5) {
      parts.push(`${Math.round(Math.abs(lc.dir_offset_deg))}° ${lc.dir_offset_deg > 0 ? "veered" : "backed"}`);
    }
    if (lc.pressure_offset_hpa != null && Math.abs(lc.pressure_offset_hpa) >= 0.5) {
      parts.push(`pressure ${U.fmtPressureDelta(lc.pressure_offset_hpa)} ${U.pressureUnit()}`);
    }
    note.textContent = parts.length
      ? `Local correction: over the last ${lc.lookback_h} h your instruments measured ${parts.join(", ")} compared with the model. The next few hours are adjusted, fading back to the model over the day.`
      : "Local correction: your instruments agree with the model.";
    note.hidden = false;
  } else note.hidden = true;

  renderAlerts(now);
  renderOutlook(now);
  renderZambretti(now);
}

function renderOutlook(now) {
  const box = clear($("#outlook"));
  const o = now.outlook_24h;
  if (!o) { box.append(h("p", { class: "muted", text: "No forecast downloaded yet." })); return; }
  if (o.worst) {
    const l = o.worst.level;
    box.append(tile("Worst", h("span", { style: { display: "inline-flex", alignItems: "center", gap: "6px" } }, levelIcon(l, 18), LEVELS[l].label), "",
      `${whenLabel(o.worst.time)}${T.firstReason(o.worst) ? ` · ${T.firstReason(o.worst)}` : ""}`));
  }
  if (o.max_wind) box.append(tile("Strongest wind", U.fmtSpeed(o.max_wind.value), U.speedUnit(), whenLabel(o.max_wind.time)));
  if (o.max_gust) box.append(tile("Strongest gust", U.fmtSpeed(o.max_gust.value), U.speedUnit(), whenLabel(o.max_gust.time)));
  if (o.max_wave) box.append(tile("Biggest waves", U.fmtHeight(o.max_wave.value), U.heightUnit(), whenLabel(o.max_wave.time)));
}

function renderZambretti(now) {
  const card = $("#zambretti-card");
  const z = now.zambretti;
  card.hidden = !z;
  if (!z) return;
  clear($("#zambretti")).append(
    h("div", { class: "zam" }, h("div", { class: "zam-letter", text: z.letter }),
      h("div", {}, h("div", { style: { fontWeight: 650, fontSize: "18px" }, text: z.text }),
        h("div", { class: "muted", text: `From ${U.fmtPressure(now.pressure.value)} ${U.pressureUnit()} (${srcLabel(now.pressure)}) `
          + `and a ${z.trend} barometer; Zambretti counts changes under ${U.num(U.pressure(1.6), U.getUnits().pressure === "inHg" ? 2 : 1)} `
          + `${U.pressureUnit()} in 3 h as steady. ${z.note}` }))));
}

// ------------------------------------------------------------------ charts + table

function renderCharts() {
  const fc = state.forecast;
  if (!fc) return;
  const s = fc.series;
  const now = Date.now() / 1000;
  const past = 6 * 3600;
  const dom = [now - past, now + state.hours * 3600];
  const bands = nightBands(s);
  const levels = fc.assessment.map((a, i) => ({ t: s.time[i], level: a.level }));
  const obs = state.obs?.series || {};
  const sp = (v) => U.speed(v);

  lineChart($("#wind-chart"), {
    ariaLabel: `Wind forecast for the next ${state.hours} hours in ${U.speedUnit()}; values also in the hourly table`,
    xDomain: dom, now, height: 250, yMin: 0, minSpan: sp(10), yUnit: U.speedUnit(),
    fmt: (v) => U.fmtDisplay("speed", v),
    nightBands: bands, strip: levels,
    arrows: s.time.map((t, i) => ({ t, deg: s.wind_dir_deg?.[i] })),
    hoverTimes: s.time,
    series: [
      { name: "Wind", short: "wind", color: "var(--series-1)", area: true, points: pts(s.time, s.wind_speed_kn, sp) },
      { name: "Gusts", short: "gusts", color: "var(--series-2)", points: pts(s.time, s.wind_gust_kn, sp) },
      { name: "Measured on board", short: "measured", color: "var(--series-3)", endLabel: false,
        points: (obs.tws_kn || []).filter(([t]) => t >= dom[0]).map(([t, v]) => [t, sp(v)]) },
    ],
    tooltipExtra: (t) => {
      const i = s.time.indexOf(t);
      if (i < 0) return [];
      const deg = s.wind_dir_deg?.[i];
      return [
        ["Direction", deg == null ? null : `${compass(deg)} ${Math.round(deg)}°`],
        ["Conditions", fc.assessment[i] ? LEVELS[fc.assessment[i].level].label : null],
      ];
    },
  });

  const hasWaves = (s.wave_height_m || []).some((v) => v != null);
  $("#wave-card").hidden = !hasWaves;
  if (hasWaves) {
    const hasSwell = (s.swell_height_m || []).some((v) => v != null && v > 0.05);
    lineChart($("#wave-chart"), {
      ariaLabel: `Wave height forecast in ${U.heightUnit()}`,
      xDomain: dom, now, height: 190, yMin: 0, minSpan: U.height(1), yUnit: U.heightUnit(),
      fmt: (v) => U.fmtDisplay("height", v), nightBands: bands, hoverTimes: s.time,
      arrows: s.time.map((t, i) => ({ t, deg: s.wave_dir_deg?.[i] })),
      series: [
        { name: "Wave height", short: "waves", color: "var(--series-1)", area: true, points: pts(s.time, s.wave_height_m, U.height) },
        hasSwell ? { name: "Swell", short: "swell", color: "var(--series-2)", points: pts(s.time, s.swell_height_m, U.height) } : null,
      ].filter(Boolean),
      tooltipExtra: (t) => {
        const i = s.time.indexOf(t);
        if (i < 0) return [];
        return [["Period", s.wave_period_s?.[i] == null ? null : `${Math.round(s.wave_period_s[i])} s`],
          ["From", s.wave_dir_deg?.[i] == null ? null : compass(s.wave_dir_deg[i])],
          ["Swell period", s.swell_period_s?.[i] == null ? null : `${Math.round(s.swell_period_s[i])} s`]];
      },
    });
  }

  const baroDom = [now - 24 * 3600, now + state.hours * 3600];
  lineChart($("#baro-chart"), {
    ariaLabel: `Barometer: last 24 hours measured on board and forecast, in ${U.pressureUnit()}`,
    xDomain: baroDom, now, height: 200, minSpan: U.pressure(10), yUnit: U.pressureUnit(),
    fmt: (v) => U.fmtDisplay("pressure", v),
    fmtTick: (v) => U.fmtDisplay("pressure", v),
    series: [
      { name: "Forecast", short: "forecast", color: "var(--series-1)", points: pts(s.time, s.pressure_hpa, U.pressure) },
      { name: "Measured on board", short: "measured", color: "var(--series-3)",
        points: (obs.pressure_hpa || []).map(([t, v]) => [t, U.pressure(v)]) },
    ],
    emptyText: "No pressure data yet.",
  });

  renderTable();
}

function renderTable() {
  const fc = state.forecast;
  const s = fc.series;
  const now = Date.now() / 1000;
  const step = state.hours > 48 ? 3 : 1;
  const idx = s.time.map((t, i) => i).filter((i) => s.time[i] >= now - 3 * 3600 && s.time[i] <= now + state.hours * 3600)
    .filter((i, k) => k % step === 0);
  const table = clear($("#hourly"));
  const days = [];
  for (const i of idx) {
    const key = new Date(s.time[i] * 1000).toDateString();
    if (!days.length || days[days.length - 1].key !== key) days.push({ key, t: s.time[i], n: 0 });
    days[days.length - 1].n++;
  }
  const past = (i) => s.time[i] + 3600 <= now;
  const row = (label, cell) => h("tr", {}, h("th", { scope: "row", text: label }), idx.map((i) => {
    const c = cell(i);
    return h("td", { class: [c.cls, past(i) ? "past" : ""].filter(Boolean).join(" ") || null }, c.v);
  }));
  const val = (v, f) => ({ v: v == null ? "–" : f(v) });
  const hasWaves = (s.wave_height_m || []).some((v) => v != null);
  table.append(
    h("thead", {},
      h("tr", {}, h("th", {}), days.map((d) => h("th", { class: "day", colspan: d.n, text: dayLongLabel(d.t) }))),
      h("tr", {}, h("th", { scope: "col", text: "Time" }), idx.map((i) => h("th", { scope: "col", text: hourShort(s.time[i]) })))),
    h("tbody", {},
      row(`Wind (${U.speedUnit()})`, (i) => ({ v: U.fmtSpeed(s.wind_speed_kn?.[i]), cls: windClass(s.wind_speed_kn?.[i]) })),
      row(`Gusts (${U.speedUnit()})`, (i) => ({ v: U.fmtSpeed(s.wind_gust_kn?.[i]), cls: windClass(s.wind_gust_kn?.[i]) })),
      row("From", (i) => (s.wind_dir_deg?.[i] == null ? { v: "–" } : { v: [dirArrow(s.wind_dir_deg[i]), " ", compass(s.wind_dir_deg[i])] })),
      hasWaves ? row(`Waves (${U.heightUnit()})`, (i) => val(s.wave_height_m?.[i], U.fmtHeight)) : null,
      hasWaves ? row("Period (s)", (i) => val(s.wave_period_s?.[i], (v) => Math.round(v))) : null,
      row(`Pressure (${U.pressureUnit()})`, (i) => val(s.pressure_hpa?.[i], U.fmtPressure)),
      row(`Rain (${U.precipUnit()})`, (i) => val(s.precip_mm?.[i], U.fmtPrecip)),
      row(`Vis (${U.visibilityUnit()})`, (i) => val(s.visibility_m?.[i], U.fmtVisibility)),
      row(`Air (${U.tempUnit()})`, (i) => val(s.temp_c?.[i], U.fmtTemp)),
      row("Sailing", (i) => {
        const a = fc.assessment[i];
        if (!a) return { v: "–" };
        const why = T.reasons(a);
        const span = h("span", { title: `${LEVELS[a.level].label}${why.length ? `: ${why.join("; ")}` : ""}` }, levelIcon(a.level, 16));
        return { v: span };
      })));
}

// ------------------------------------------------------------------ side cards

function renderTides(data) {
  const card = $("#tide-card");
  const d = data?.data;
  card.hidden = !(d && (d.tide || d.current));
  if (card.hidden) return;
  const box = clear($("#tides"));
  const now = Date.now() / 1000;
  if (d.tide) {
    const st = d.tide.station;
    box.append(h("p", { class: "muted", text: `${st.name}, ${U.fmtDistance(st.distance_nm)} ${U.distanceUnit()} ${compass(st.bearing_deg)}. Heights above chart datum (MLLW).` }));
    const curve = d.tide.curve || [];
    if (curve.length) {
      const div = h("div", { class: "chart" });
      box.append(div);
      lineChart(div, {
        ariaLabel: "Tide height", xDomain: [now - 3 * 3600, now + 30 * 3600], now, height: 150, yUnit: U.heightUnit(),
        fmt: (v) => U.fmtDisplay("height", v), series: [{ name: "Tide", short: "tide", color: "var(--series-1)", area: true,
          points: curve.map((p) => [p.time, U.height(p.height_m)]) }],
      });
    }
    box.append(h("ul", { class: "list" }, d.tide.hilo.filter((e) => e.time > now - 3600).slice(0, 4).map((e) =>
      h("li", {}, h("span", { class: "name", text: e.type === "high" ? "High water" : "Low water" }),
        h("span", { class: "vals", text: `${U.fmtHeight(e.height_m)} ${U.heightUnit()} · ${whenLabel(e.time)}` })))));
  }
  if (d.current) {
    const st = d.current.station;
    box.append(h("p", { class: "muted", style: { marginTop: "12px" }, text: `Tidal stream: ${st.name}, ${U.fmtDistance(st.distance_nm)} ${U.distanceUnit()}` }));
    box.append(h("ul", { class: "list" }, d.current.events.filter((e) => e.time > now - 1800).slice(0, 5).map((e) =>
      h("li", {}, h("span", { class: "name", text: e.type === "slack" ? "Slack" : e.type === "flood" ? "Max flood" : "Max ebb" }),
        h("span", { class: "vals", text: e.type === "slack" ? whenLabel(e.time)
          : `${U.fmtSpeed(e.speed_kn, 1)} ${U.speedUnit()} → ${Math.round(e.type === "flood" ? e.flood_dir_deg : e.ebb_dir_deg)}° · ${whenLabel(e.time)}` })))));
  }
}

function renderBuoys(data) {
  const card = $("#buoy-card");
  const list = data?.data?.stations || [];
  card.hidden = !list.length;
  if (!list.length) return;
  const now = Date.now() / 1000;
  clear($("#buoys")).append(h("ul", { class: "list" }, list.map((b) => h("li", {},
    h("div", {}, h("div", { class: "name", text: `Station ${b.station}` }),
      h("div", { class: "sub", text: `${U.fmtDistance(b.distance_nm)} ${U.distanceUnit()} ${compass(b.bearing_deg)} · ${ago(now - b.time)}` })),
    h("div", { class: "vals" },
      h("div", { text: b.wind_speed_kn == null ? "wind –" : `${compass(b.wind_dir_deg)} ${U.fmtSpeed(b.wind_speed_kn)}${b.wind_gust_kn != null ? `–${U.fmtSpeed(b.wind_gust_kn)}` : ""} ${U.speedUnit()}` }),
      h("div", { class: "sub", text: [
        b.wave_height_m != null ? `${U.fmtHeight(b.wave_height_m)} ${U.heightUnit()}${b.dominant_period_s ? ` @ ${Math.round(b.dominant_period_s)} s` : ""}` : null,
        b.pressure_hpa != null ? `${U.fmtPressure(b.pressure_hpa)} ${U.pressureUnit()}${b.pressure_tendency_hpa != null ? ` (${U.fmtPressureDelta(b.pressure_tendency_hpa)})` : ""}` : null,
        b.water_temp_c != null ? `water ${U.fmtTemp(b.water_temp_c)}${U.tempUnit()}` : null,
      ].filter(Boolean).join(" · ") }))))));
}

function renderMarineText(data) {
  const card = $("#marine-text-card");
  const d = data?.data;
  card.hidden = !d?.text;
  if (card.hidden) return;
  const issued = isoToEpoch(d.issued);
  clear($("#marine-text")).append(
    h("p", { class: "muted", text: `Zone ${d.zone} · NWS ${d.product} ${d.office}${issued ? ` · issued ${whenLabel(issued)}` : ""} · fetched ${ago(data.age_s)}` }),
    h("pre", { class: "marine-text", text: d.text }));
}

function renderStatus() {
  const st = state.status;
  if (!st) return;
  forecastStatus(st);
  if (U.setServerDefault(st.display)) renderAll(); // server's default units changed what we show
  $("#demo-banner").hidden = !st.demo;
  const box = clear($("#sources"));
  for (const [key, v] of Object.entries(st.sources || {})) {
    const cls = v.applicable === false ? "" : v.error ? "err" : "ok";
    const detail = v.applicable === false ? `not available here (${v.note || ""})` : v.error ? `error: ${v.error}` : `updated ${ago(st.time - v.last_success)}`;
    box.append(h("span", { class: `source ${cls}`, title: detail }, h("span", { class: "dot" }),
      `${SOURCE_NAMES[key] || key}${v.error && v.applicable !== false ? " (error)" : ""}`));
  }
  const bw = st.bandwidth;
  if (bw) {
    const cap = bw.today.cap_mb ? ` of ${bw.today.cap_mb} MB` : "";
    box.append(h("span", { class: "source", text: `Data today: ${bw.today.used_mb.toFixed(1)} MB${cap}${bw.saver ? " · saver on" : ""}${bw.max_kbps ? ` · capped at ${bw.max_kbps} kbit/s` : ""}` }));
  }
  for (const [name, sen] of Object.entries(st.sensors || {})) {
    box.append(h("span", { class: `source ${sen.connected ? "ok" : "err"}`, title: sen.error || "" }, h("span", { class: "dot" }), name));
  }
  $("#version").textContent = `SHWeather ${st.version}`;
}

// ------------------------------------------------------------------ loading

let blockingKey = null;

function showBlocking(message, ...extra) {
  const box = clear($("#alerts"));
  box.append(h("div", { class: "alert warning" }, h("div", { class: "alert-title", text: message }), ...extra));
}

/** No position anywhere yet: offer this device's GPS (or the secure address that allows it). */
function showNoPosition() {
  const g = gpsState();
  const url = secureUrl(state.status);
  const key = [g.support, g.enabled, g.status, url].join("|");
  if (key === blockingKey && $("#alerts").childElementCount) return;   // keep focus on the buttons
  blockingKey = key;
  const actions = [];
  if (g.support === "ok" && !g.enabled) {
    actions.push(h("button", { class: "btn primary", type: "button", text: "Use this device's location", onclick: () => setGpsEnabled(true) }));
  } else if (g.support === "insecure" && url) {
    actions.push(h("a", { class: "btn primary", href: url, text: "Open the secure address to use this device's GPS" }));
  }
  actions.push(h("button", { class: "btn", type: "button", text: "Set it in Settings", onclick: showSettings }));
  showBlocking("No position yet.",
    h("p", { class: "help", text: g.enabled ? gpsStatusText(g, gpsFormat)
      : "Connect a GPS to the boat's network, share this device's location, or type the position in Settings." }),
    h("div", { class: "btn-row" }, actions));
}

async function loadNow() {
  try {
    state.now = await api.now();
    blockingKey = null;
    if (state.now.__cached) {
      // Served by the service worker from its cache: the server is not reachable right now.
      state.error = new Error("Showing saved data");
    } else {
      state.error = null;
      state.lastContact = Date.now();
    }
    renderNow();
    try { forecastNow(state.now); } catch (err) { console.error("Forecast tab:", err); }  // never costs the other tabs
    radarUpdate();
  } catch (e) {
    state.error = e;
    if (e.status === 409) showNoPosition();
    if (e.status === 401) showBlocking("This server requires an access token: open Settings and enter it.");
  }
  renderConn();
}

// One download serves both tabs: the Wind tab shows up to 4 days ahead, the Forecast tab
// every day the server has (5 by default, up to 16), and today's high and low need the
// hours since midnight.
async function loadForecast() {
  try {
    const [fc, obs] = await Promise.all([api.forecast(16 * 24, 24), api.observations("tws_kn,pressure_hpa", 30)]);
    state.forecast = fc;
    state.obs = obs;
    renderCharts();
    renderForecast({ forecast: fc, now: state.now });
  } catch (e) {
    if (e.status === 404) {
      const text = "No forecast downloaded yet. It downloads automatically when the server has internet.";
      clear($("#wind-chart")).append(h("p", { class: "chart-empty", text }));
      forecastMissing(text);
    } else if (!state.forecast) {
      forecastMissing("Can't reach the weather server, and this phone has no saved forecast yet.");
    }
  }
}

async function loadExtras() {
  const [status, tides, buoys, text] = await Promise.allSettled([api.status(), api.tides(), api.buoys(), api.marineText()]);
  if (tides.status === "fulfilled") state.tides = tides.value;
  if (buoys.status === "fulfilled") state.buoys = buoys.value;
  if (text.status === "fulfilled") state.marineText = text.value;
  if (status.status === "fulfilled") {
    state.status = status.value;
    renderStatus();
    if (state.error?.status === 409) showNoPosition();   // now it knows the secure address
  }
  renderSide();
  radarUpdate();
}

function renderSide() {
  if (state.tides) renderTides(state.tides);
  if (state.buoys) renderBuoys(state.buoys);
  if (state.marineText) renderMarineText(state.marineText);
}

// Re-render everything from the data already loaded (units or theme changed).
function renderAll() {
  renderUnitsToggle();
  renderNow();
  if (state.forecast) renderCharts();
  renderForecast();
  renderSide();
  radarUnitsChanged();
}

// ------------------------------------------------------------------ views (Wind / Forecast / Radar tabs)

const VIEWS = ["wind", "forecast", "radar"];

function currentView() {
  const v = location.hash.slice(1);
  return VIEWS.includes(v) ? v : "wind";     // also old "#weather" bookmarks
}

function applyView() {
  if (location.hash === "#weather") history.replaceState(null, "", "#wind");
  const view = currentView();
  const radar = view === "radar";
  for (const v of VIEWS) $(`#view-${v}`).hidden = v !== view;
  document.body.classList.toggle("radar-open", radar);
  for (const a of document.querySelectorAll(".tabs .tab")) {
    if (a.dataset.view === view) a.setAttribute("aria-current", "page");
    else a.removeAttribute("aria-current");
  }
  window.scrollTo(0, 0);
  showRadar(radar);
}

function renderUnitsToggle() {
  const { system } = U.getSystem();
  const box = $("#units-toggle");
  box.title = system === "custom" ? "Custom units (change them in Settings)" : "Units";
  for (const b of box.querySelectorAll("button")) b.setAttribute("aria-pressed", String(b.dataset.system === system));
}

function refreshEverything() {
  loadNow();
  loadForecast();
  loadExtras();
}

function showSettings() {
  openSettings(() => { renderAll(); loadNow(); loadForecast(); loadExtras(); });
}

// ------------------------------------------------------------------ boot

function init() {
  // Compass ticks
  const ticks = document.querySelector(".compass-ticks");
  for (let a = 0; a < 360; a += 30) {
    const r = (a * Math.PI) / 180;
    const line = document.createElementNS("http://www.w3.org/2000/svg", "line");
    line.setAttribute("x1", 50 + Math.sin(r) * 38); line.setAttribute("y1", 50 - Math.cos(r) * 38);
    line.setAttribute("x2", 50 + Math.sin(r) * 44); line.setAttribute("y2", 50 - Math.cos(r) * 44);
    ticks.append(line);
  }

  for (const b of document.querySelectorAll("#units-toggle button")) {
    b.addEventListener("click", () => {
      U.setSystem(b.dataset.system);
      renderAll();
    });
  }
  renderUnitsToggle();

  for (const b of document.querySelectorAll(".range-seg button")) {
    b.setAttribute("aria-pressed", String(Number(b.dataset.hours) === state.hours));
    b.addEventListener("click", () => {
      state.hours = Number(b.dataset.hours);
      store.set("shw.hours", state.hours);
      for (const o of document.querySelectorAll(".range-seg button")) o.setAttribute("aria-pressed", String(o === b));
      if (state.forecast) renderCharts();
    });
  }

  const order = ["auto", "light", "dark", "night"];
  $("#theme-btn").addEventListener("click", () => {
    let cur = "auto";
    try { cur = localStorage.getItem("shw.theme") || "auto"; } catch { /* ignore */ }
    setTheme(order[(order.indexOf(cur) + 1) % order.length]);
    if (state.forecast) renderCharts();
  });
  $("#settings-btn").addEventListener("click", showSettings);

  initRadar({ now: () => state.now, buoys: () => state.buoys });
  window.addEventListener("hashchange", applyView);
  applyView();

  // This device's GPS: polled at the rate set in Settings while the app is open.
  onGpsChange((st) => {
    renderGpsBanner(st);
    renderPosition();
    if (!state.now && state.error?.status === 409) showNoPosition();
  });
  let catchUp = null;
  initGps({
    serverPosition: () => state.now?.position,
    onSent: () => {
      loadNow();
      // A first position: the server is fetching the forecast for it now.
      if (!state.forecast && !catchUp) catchUp = setTimeout(() => { catchUp = null; loadForecast(); loadExtras(); }, 20000);
    },
  });
  renderGpsBanner();

  refreshEverything();
  setInterval(loadNow, 5000);         // live instruments
  setInterval(loadForecast, 5 * 60 * 1000);
  setInterval(loadExtras, 60 * 1000);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) refreshEverything(); });

  // Offline caching on the phone needs a secure context (HTTPS or localhost).
  if ("serviceWorker" in navigator && window.isSecureContext) {
    navigator.serviceWorker.register("sw.js").catch(() => { /* optional */ });
  }
}

init();
