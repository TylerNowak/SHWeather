// Forecast tab logic (conditions, days, wording, sun). Run: npm test. No browser needed.
import assert from "node:assert/strict";
import { test } from "node:test";

// Wording uses the device's clock: pin it (UTC-5, like CAL below) before the modules load.
process.env.TZ = "Etc/GMT+5";
const D = await import("../../web/js/daily.js");
const U = await import("../../web/js/units.js");
U.setSystem("metric", true);   // °C, knots, mm

const close = (a, b, eps) => assert.ok(Math.abs(a - b) <= eps, `${a} != ${b} (±${eps})`);
const HOUR = 3600;

// A calendar 5 hours behind UTC (US Central daylight time), independent of the machine.
const OFFSET = -5 * HOUR;
const CAL = {
  key: (t) => new Date((t + OFFSET) * 1000).toISOString().slice(0, 10),
  hour: (t) => new Date((t + OFFSET) * 1000).getUTCHours(),
};
const LOCAL_MIDNIGHT = Date.UTC(2026, 8, 29, 5) / 1000;   // 2026-09-29 00:00 at UTC-5

/** Hourly series from a function of the local hour index (0 = today's midnight). */
function series(hours, fn, start = LOCAL_MIDNIGHT) {
  const s = { time: [], temp_c: [], weather_code: [], cloud_pct: [], precip_mm: [], precip_prob_pct: [],
    is_day: [], wind_speed_kn: [], wind_gust_kn: [], wind_dir_deg: [], humidity_pct: [] };
  for (let i = 0; i < hours; i++) {
    const hod = ((i % 24) + 24) % 24;
    const r = { temp: 10 + 8 * Math.sin(((hod - 9) / 24) * 2 * Math.PI), code: 1, cloud: 30, precip: 0, prob: 5,
      isDay: hod >= 7 && hod < 19 ? 1 : 0, wind: 10, gust: 14, dir: 220, rh: 70, ...fn(i, hod) };
    s.time.push(start + i * HOUR);
    s.temp_c.push(r.temp); s.weather_code.push(r.code); s.cloud_pct.push(r.cloud); s.precip_mm.push(r.precip);
    s.precip_prob_pct.push(r.prob); s.is_day.push(r.isDay); s.wind_speed_kn.push(r.wind); s.wind_gust_kn.push(r.gust);
    s.wind_dir_deg.push(r.dir); s.humidity_pct.push(r.rh);
  }
  return s;
}
const rowsOf = (s, assessment) => D.hourRows(s, assessment, CAL);

test("hour conditions from WMO codes and cloud cover", () => {
  assert.equal(D.hourCondition({ code: 95, isDay: 1 }).text, "Thunderstorms");
  assert.equal(D.hourCondition({ code: 99, isDay: 1 }).icon, "hail");
  assert.equal(D.hourCondition({ code: 65, isDay: 0 }).icon, "heavy-rain");
  assert.equal(D.hourCondition({ code: 45 }).text, "Fog");
  // Sky codes are refined by the cloud cover, with night wording.
  assert.deepEqual([10, 30, 60, 80, 95].map((c) => D.hourCondition({ code: 2, cloud: c, isDay: 1 }).text),
    ["Sunny", "Mostly sunny", "Partly cloudy", "Mostly cloudy", "Cloudy"]);
  const night = D.hourCondition({ code: 0, cloud: 5, isDay: 0 });
  assert.equal(night.text, "Clear");
  assert.equal(night.night, true);
  // No code at all: rain from the amount, else sky from the cloud cover.
  assert.equal(D.hourCondition({ code: null, precip: 2, cloud: 90 }).text, "Light rain");
  assert.equal(D.hourCondition({ code: null, precip: 0, cloud: 90 }).text, "Cloudy");
});

test("chance wording follows the NWS probability bands", () => {
  assert.deepEqual([10, 30, 60, 90, null].map(D.chanceWord), ["slight", "chance", "likely", "", ""]);
});

test("a period's condition: thunder wins, then lasting rain, then fog, then the daylight sky", () => {
  const base = { cloud: 10, isDay: 1, prob: 10 };
  const rows = (list) => list.map((r, i) => ({ ...base, hour: 6 + i, t: i * HOUR, ...r }));
  // One thunderstorm hour among showers
  let c = D.periodCondition(rows([{ code: 80, precip: 1, prob: 60 }, { code: 95, precip: 3, prob: 90 }, { code: 1 }]));
  assert.equal(c.icon, "thunder");
  assert.equal(c.text, "Thunderstorms");
  // A single light-rain hour is not a rainy day...
  c = D.periodCondition(rows([{ code: 61, precip: 0.2 }, { code: 1 }, { code: 1 }, { code: 1 }]));
  assert.equal(c.family, "sky");
  // ...but two are, worded by the chance of precipitation.
  c = D.periodCondition(rows([{ code: 61, precip: 0.3, prob: 40 }, { code: 63, precip: 1.2, prob: 45 }, { code: 1 }]));
  assert.equal(c.text, "Chance of rain");
  assert.equal(c.icon, "rain");
  c = D.periodCondition(rows([{ code: 80, prob: 65 }, { code: 81, prob: 70 }, { code: 61, prob: 70 }]));
  assert.equal(c.text, "Showers likely");      // showers outnumber rain
  c = D.periodCondition(rows([{ code: 80, prob: 90 }, { code: 82, prob: 95 }]));
  assert.equal(c.text, "Heavy showers");       // near certain: the heaviest hour's wording
  // Morning fog, then the sky of the rest of the day.
  c = D.periodCondition(rows([45, 45, 45, 0, 0, 0, 0, 0, 0, 0, 0, 0].map((code) => ({ code, cloud: 5 }))));
  assert.equal(c.icon, "fog");
  assert.equal(c.text, "Morning fog, then sunny");
  // Sky: the average cloud of the daylight hours; the night hours don't count.
  c = D.periodCondition(rows([{ cloud: 90, isDay: 0 }, { cloud: 10 }, { cloud: 20 }, { cloud: 30 }]));
  assert.equal(c.text, "Mostly sunny");
});

test("days: local calendar, today keeps its past hours, partial last day", () => {
  // 24 h of yesterday, then 4 days and 19 hours (a forecast ending at 7 PM local).
  const s = series(24 + 4 * 24 + 19, (i, hod) => (i >= 24 + 24 + 14 && i < 24 + 24 + 20 ? { code: 63, precip: 2, prob: 90 } : {}),
    LOCAL_MIDNIGHT - 24 * HOUR);
  const now = LOCAL_MIDNIGHT + 15.5 * HOUR;               // today 3:30 PM
  const days = D.buildDays(rowsOf(s), { now, cal: CAL });
  assert.deepEqual(days.map((d) => d.key), ["2026-09-29", "2026-09-30", "2026-10-01", "2026-10-02", "2026-10-03"]);
  assert.equal(days[0].today, true);
  assert.equal(days[0].rows.length, 24);                  // midnight to midnight, past hours included
  assert.equal(days[0].partial, false);
  assert.equal(days[4].partial, true);
  assert.equal(days[4].rows.length, 19);
  assert.ok(days[4].day && !days[4].night);              // one hour of its night: not worded
  close(days[0].hi.value, 18, 0.01);                      // 10 + 8 sin(...) peaks at 3 PM
  assert.equal(CAL.hour(days[0].hi.t), 15);
  close(days[0].lo.value, 2, 0.01);
  // Rain from 2 PM to 8 PM tomorrow: a rainy day, and in words.
  assert.equal(days[1].cond.text, "Rain");
  assert.equal(days[1].wetHours, 6);
  assert.equal(days[1].precip, 12);
  const at = (h) => D.hourText(LOCAL_MIDNIGHT + h * HOUR);   // "2 PM" or "14:00", as the device writes it
  assert.equal(D.periodText(days[1].day),
    `Rain, mainly after ${at(24 + 14)}. Otherwise mostly sunny. High 18°C. Wind SW 10 kn. Rain about 8 mm.`);
  // The night runs 6 PM to 6 AM into the next day.
  assert.equal(days[1].night.rows.length, 12);
  assert.equal(CAL.hour(days[1].night.rows[0].t), 18);
  assert.equal(CAL.key(days[1].night.rows[11].t), "2026-10-01");
  assert.deepEqual(days[1].parts.map((p) => p.name), ["Overnight", "Morning", "Afternoon", "Evening"]);
  // A day whose afternoon the forecast doesn't reach is left out.
  const short = D.buildDays(rowsOf(series(24 + 12, () => ({}))), { now: LOCAL_MIDNIGHT + 1, cal: CAL });
  assert.equal(short.length, 1);
});

test("forecast wording: chances, timing, wind turning and gusts, in display units", () => {
  U.setSystem("metric", true);   // °C, knots, mm
  const s = series(24, (i, hod) => ({
    code: hod >= 12 && hod < 16 ? 80 : 2, cloud: 60, prob: hod >= 12 && hod < 16 ? 40 : 10, precip: hod >= 12 && hod < 16 ? 0.6 : 0,
    wind: hod < 12 ? 8 : 16, gust: hod < 12 ? 11 : 27, dir: hod < 12 ? 200 : 300,
  }));
  const [day] = D.buildDays(rowsOf(s), { now: LOCAL_MIDNIGHT + 7 * HOUR, cal: CAL });
  const at = (h) => D.hourText(LOCAL_MIDNIGHT + h * HOUR);
  assert.equal(D.periodText(day.day), `A chance of showers between ${at(12)} and ${at(16)}. Otherwise partly cloudy. `
    + "High 18°C. Wind SSW 8 to 16 kn, becoming WNW, gusts to 27 kn. Rain about 2.4 mm.");
  U.setSystem("imperial", false); // °F, mph
  assert.match(D.periodText(day.day), /High 64°F\. Wind SSW 9 to 18 mph, becoming WNW, gusts to 31 mph\./);
  U.setSystem("metric", true);
  const calm = D.summarize(rowsOf(series(6, () => ({ wind: 3, gust: 5 }))));
  assert.equal(D.windPhrase(calm), "Light winds");
});

test("fog and freezing drizzle wording", () => {
  const rows = (codes, night = false) => codes.map((code, i) => ({ code, cloud: 5, isDay: night ? 0 : 1, prob: 10,
    hour: (night ? 18 : 6) + i, t: LOCAL_MIDNIGHT + ((night ? 18 : 6) + i) * HOUR, temp: 10, wind: 6, dir: 200 }));
  const period = (r, night) => ({ rows: r, ...D.summarize(r, { night }) });
  const at = (h) => D.hourText(LOCAL_MIDNIGHT + h * HOUR);
  // Fog that clears: "Then"; fog that comes in late: "Otherwise", never "then" out of order.
  assert.equal(D.periodText(period(rows([45, 45, 45, 0, 0, 0, 0, 0, 0, 0, 0, 0]))),
    `Fog, mainly before ${at(9)}. Then sunny. High 10°C. Wind SSW 6 kn.`);
  assert.equal(D.periodText(period(rows([0, 0, 0, 0, 0, 0, 0, 0, 0, 45, 45, 45]))),
    `Fog, mainly after ${at(15)}. Otherwise sunny. High 10°C. Wind SSW 6 kn.`);
  // No "morning fog" in a night period.
  assert.equal(D.periodCondition(rows([0, 0, 0, 0, 0, 0, 0, 0, 45, 45, 45, 45], true), { night: true }).text, "Fog");
  const fd = D.periodCondition(rows([56, 56, 0]).map((r) => ({ ...r, prob: 40, precip: 0.2 })));
  assert.equal(fd.text, "Chance of freezing drizzle");
});

test("mean wind direction is a vector mean", () => {
  close(D.meanDirection([{ dir: 350, wind: 10 }, { dir: 10, wind: 10 }]) % 360, 0, 1e-6);
  close(D.meanDirection([{ dir: 90, wind: 30 }, { dir: 180, wind: 0 }]), 90, 1e-6);
  assert.equal(D.meanDirection([{ dir: null, wind: 5 }]), null);
});

test("upcoming periods are named by the time of day", () => {
  const s = series(48, () => ({}));
  const rows = rowsOf(s);
  const at = (h) => {
    const now = LOCAL_MIDNIGHT + h * HOUR;
    return D.upcomingPeriods(D.buildDays(rows, { now, cal: CAL }), now, CAL).map((p) => p.label);
  };
  assert.deepEqual(at(3), ["Overnight", "Today"]);
  assert.deepEqual(at(9), ["Today", "Tonight"]);
  assert.deepEqual(at(14), ["This afternoon", "Tonight"]);
  assert.deepEqual(at(20), ["Tonight", "Tomorrow"]);
});

test("sunrise and sunset (NOAA equation) within a couple of minutes", () => {
  // Chicago, 29 Sep 2026: 6:46 AM and 6:37 PM CDT
  let st = D.sunTimes(Date.UTC(2026, 8, 29, 17) / 1000, 41.88, -87.63);
  close(st.rise, Date.UTC(2026, 8, 29, 11, 46) / 1000, 150);
  close(st.set, Date.UTC(2026, 8, 29, 23, 37) / 1000, 150);
  // Sydney, 21 Jun 2026: 7:00 AM and 4:53 PM AEST
  st = D.sunTimes(Date.UTC(2026, 5, 21, 2) / 1000, -33.87, 151.21);
  close(st.rise, Date.UTC(2026, 5, 20, 21, 0) / 1000, 150);
  close(st.set, Date.UTC(2026, 5, 21, 6, 53) / 1000, 150);
  // Auckland, 10 Jan 2026 (the date line): 6:12 AM and 8:43 PM NZDT
  st = D.sunTimes(Date.UTC(2026, 0, 9, 23) / 1000, -36.85, 174.76);
  close(st.rise, Date.UTC(2026, 0, 9, 17, 12) / 1000, 150);
  close(st.set, Date.UTC(2026, 0, 10, 7, 43) / 1000, 150);
  assert.equal(D.sunTimes(Date.UTC(2026, 5, 21, 10) / 1000, 69.65, 18.96).polar, "day");
  assert.equal(D.sunTimes(Date.UTC(2026, 11, 21, 10) / 1000, 69.65, 18.96).polar, "night");
});

test("feels-like: NWS wind chill and heat index, else the air temperature", () => {
  close(D.apparentTemp(-10, 50, 15), -19.2, 0.1);          // wind chill table: 14 F, 17 mph -> -2 F
  close(D.apparentTemp(32.2, 70, 5), 41, 0.3);             // heat index table: 90 F, 70% -> 106 F
  assert.equal(D.apparentTemp(18, 60, 20), 18);
  assert.equal(D.apparentTemp(5, 80, 1), 5);               // too little wind for wind chill
  assert.equal(D.apparentTemp(null, 50, 10), null);
});
