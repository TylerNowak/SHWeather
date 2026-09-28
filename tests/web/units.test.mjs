// Unit-system tests for the PWA (run: node --test tests/web/). No browser needed.
import assert from "node:assert/strict";
import { test } from "node:test";

import * as T from "../../web/js/text.js";
import * as U from "../../web/js/units.js";

const close = (a, b, eps = 0.01) => assert.ok(Math.abs(a - b) < eps, `${a} != ${b}`);

test("server default decides until the device chooses", () => {
  U.resetToDefault();
  U.setServerDefault({ units: "imperial", nautical: true });
  assert.deepEqual(U.getSystem(), { system: "imperial", nautical: true });
  assert.equal(U.speedUnit(), "kn");
  assert.equal(U.heightUnit(), "ft");
  U.setServerDefault({ units: "metric", nautical: false });
  assert.deepEqual(U.getSystem(), { system: "metric", nautical: false });
  assert.equal(U.speedUnit(), "km/h");
  assert.equal(U.isDeviceChoice(), false);
});

test("locale picks imperial only for the US and friends", () => {
  assert.equal(U.localeSystem("en-US"), "imperial");
  assert.equal(U.localeSystem("en-GB"), "metric");
  assert.equal(U.localeSystem("fr-CA"), "metric");
  assert.equal(U.localeSystem("de"), "metric");
});

test("imperial conversions", () => {
  U.setSystem("imperial", false);
  close(U.speed(10), 11.51);
  close(U.height(1), 3.281);
  assert.equal(U.temp(0), 32);
  close(U.pressure(1013.25), 29.92);
  close(U.precip(25.4), 1);
  close(U.distance(1), 1.151);
  close(U.visibility(1852), 1.151);
  assert.equal(U.fmtTemp(20), "68");
  assert.equal(U.fmtPressure(1013.25), "29.92");
  assert.equal(U.visibilityUnit(), "mi");
  assert.equal(U.fmtVisibility(30000), "10+");
});

test("metric and nautical conversions", () => {
  U.setSystem("metric", false);
  close(U.speed(10), 18.52);
  assert.equal(U.fmtVisibility(30000), "20+");
  assert.equal(U.fmtPrecip(0.05), "");
  assert.equal(U.fmtPrecip(1.24), "1.2");
  U.setSystem("metric", true);
  assert.equal(U.speedUnit(), "kn");
  assert.equal(U.distanceUnit(), "nm");
  assert.equal(U.heightUnit(), "m");
});

test("custom mix is detected and round-trips through forms", () => {
  U.setSystem("imperial", true);
  U.setUnit("pressure", "hPa");
  assert.equal(U.getSystem().system, "custom");
  U.setSystem("imperial", false);
  close(U.toCanonical("speed", U.speed(17.3)), 17.3, 1e-9);
  close(U.toCanonical("height", U.height(1.7)), 1.7, 1e-9);
  close(U.toCanonical("distance", U.distance(100)), 100, 1e-9);
  close(U.toCanonical("temp", U.temp(12.5)), 12.5, 1e-9);
});

test("near-limit values never round across the limit", () => {
  U.setSystem("metric", true);
  assert.equal(U.fmtNear("speed", 24.6, 25), "24.6 kn");
  assert.equal(U.fmtNear("speed", 22.2, 25), "22 kn");
  assert.equal(U.fmtNear("speed", 25.3, 25), "25.3 kn"); // not "25 kn is above your 25 kn limit"
  assert.equal(U.fmtNear("speed", 25, 25), "25 kn");
  U.setSystem("imperial", false); // 24.6 kn = 28.3 mph, limit 28.8 mph
  assert.equal(U.fmtNear("speed", 24.6, 25), "28 mph");
});

test("assessment reasons are worded in display units", () => {
  const a = { level: "nogo", reasons: ["Waves 2.1 m exceed your 2 m limit"],
    details: [{ level: "nogo", code: "waves_over_limit", wave_m: 2.1, limit_m: 2 }] };
  U.setSystem("imperial", true);
  assert.equal(T.firstReason(a), "Waves 6.9 ft exceed your 6.6 ft limit");
  U.setSystem("metric", true);
  assert.equal(T.firstReason(a), "Waves 2.1 m exceed your 2 m limit");
  const g = { details: [{ code: "gust_over_limit", gust_kn: 36.4, limit_kn: 32 }] };
  U.setSystem("imperial", false);
  assert.equal(T.firstReason(g), "Gusts 42 mph are above your 36.8 mph limit");
  // unknown codes fall back to the server's text
  assert.equal(T.firstReason({ reasons: ["Something new"], details: [{ code: "future_code" }] }), "Something new");
});

test("reef reasons respect every limit (24.6 kn next to a 25 kn max)", () => {
  U.setSystem("metric", true);
  const d = { code: "second_reef", wind_kn: 24.6, limit_kn: 20, limits_kn: [15, 20, 25] };
  assert.equal(T.firstReason({ details: [d] }), "Wind 24.6 kn: second reef");
  const w = { code: "waves_near_limit", wave_m: 1.96, limit_m: 2 };
  assert.equal(T.firstReason({ details: [w] }), "Waves 1.96 m near your limit");
});

test("forms convert with the units they were rendered in", () => {
  U.setSystem("imperial", true);
  const rendered = U.getUnits();
  U.setSystem("metric", true); // switched while the form was open
  close(U.toCanonical("height", 6.6, rendered), 2.01);
  assert.equal(U.unit("height", rendered), "ft");
  close(U.toDisplay("height", 2, rendered), 6.56);
});

test("barometer warnings convert the deep-low pressure", () => {
  U.setSystem("imperial", true);
  assert.equal(T.warning({ code: "deep_low", pressure_hpa: 985, text: "Deep low (985 hPa) and still falling." }),
    "Deep low (29.09 inHg) and still falling.");
  assert.equal(T.warning({ code: "falling", text: "Barometer falling." }), "Barometer falling.");
});
