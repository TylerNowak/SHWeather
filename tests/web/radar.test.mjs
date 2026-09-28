// Radar tab maths (projection, forecast-field sampling, wind interpolation). No browser needed.
import assert from "node:assert/strict";
import { test } from "node:test";

import { _internals as R } from "../../web/js/radar.js";

const close = (a, b, eps = 1e-6) => assert.ok(Math.abs(a - b) < eps, `${a} != ${b}`);

test("Web Mercator round trip and tile sizes", () => {
  for (const [lat, lon] of [[41.9, -87.5], [-33.86, 151.2], [0, 0], [60, 179.9]]) {
    const [x, y] = R.worldXY(lat, lon, 7.3);
    const [lat2, lon2] = R.worldToLatLon(x, y, 7.3);
    close(lat2, lat, 1e-6);
    close(lon2, lon, 1e-6);
  }
  const [x0] = R.worldXY(0, -180, 0);
  const [x1] = R.worldXY(0, 180, 0);
  close(x1 - x0, 256);       // one tile spans the world at zoom 0
});

test("distance and bearing", () => {
  close(R.distanceNm({ lat: 41, lon: -87 }, { lat: 42, lon: -87 }), 60, 0.2);
  close(R.bearingDeg({ lat: 41, lon: -87 }, { lat: 42, lon: -87 }), 0, 1e-6);
  close(R.bearingDeg({ lat: 0, lon: 179.5 }, { lat: 0, lon: -179.5 }), 90, 1e-6);   // across the antimeridian
});

function grid() {
  // 3 x 3 points at 0.25 deg, two hours. Wind: west side 10 kn from 350, east side 10 kn from 010.
  const lats = [41.75, 42.0, 42.25];
  const lons = [-87.75, -87.5, -87.25];
  const points = [];
  const f = { wind_speed_kn: [], wind_gust_kn: [], wind_dir_deg: [], cloud_pct: [], precip_mm: [] };
  for (const la of lats) {
    for (const lo of lons) {
      points.push([la, lo]);
      f.wind_speed_kn.push([10, 20]);
      f.wind_gust_kn.push([14, 26]);
      f.wind_dir_deg.push(lo < -87.5 ? [350, 350] : lo > -87.5 ? [10, 10] : [0, 0]);
      f.cloud_pct.push([lo === -87.75 ? 0 : 100, 50]);
      f.precip_mm.push([0, 2]);
    }
  }
  return R.buildGrid({ time: [0, 3600], points, fields: f, run: { center: [42, -87.5] } });
}

test("forecast grid: bilinear in space, linear in time", () => {
  const g = grid();
  assert.equal(g.regular, true);
  const tw = R.timeWeights(g, 1800);
  assert.deepEqual(tw, [0, 1, 0.5]);
  close(R.sample(g, "precip_mm", 42.0, -87.5, tw), 1);
  close(R.sample(g, "cloud_pct", 42.0, -87.625, R.timeWeights(g, 0)), 50);        // halfway between 0 and 100
  assert.equal(R.sample(g, "cloud_pct", 43.5, -87.5, tw), null);                    // outside the grid
  assert.deepEqual(R.timeWeights(g, -99), [0, 0, 0]);
  assert.deepEqual(R.timeWeights(g, 99999), [1, 1, 0]);
});

test("wind directions interpolate as vectors (350 and 010 average to north, not south)", () => {
  const g = grid();
  const w = R.windAt(g, 42.0, -87.5, R.timeWeights(g, 0));
  assert.ok(w.dir < 1 || w.dir > 359, `direction ${w.dir}`);
  close(w.speed, 10, 0.2);
  const mid = R.windAt(g, 42.0, -87.625, R.timeWeights(g, 1800));
  assert.ok(mid.dir > 349 || mid.dir < 1, `direction ${mid.dir}`);
  close(mid.speed, 15, 0.3);
  close(mid.gust, 20);
});

test("forecast grid across the antimeridian", () => {
  const points = [[0, 179.75], [0, -180], [0, -179.75], [0.25, 179.75], [0.25, -180], [0.25, -179.75]];
  const f = { wind_speed_kn: [], wind_gust_kn: [], wind_dir_deg: [], cloud_pct: [], precip_mm: [] };
  points.forEach((p, i) => {
    f.wind_speed_kn.push([10]); f.wind_gust_kn.push([12]); f.wind_dir_deg.push([90]);
    f.cloud_pct.push([i % 3 * 50]); f.precip_mm.push([0]);
  });
  const g = R.buildGrid({ time: [0], points, fields: f, run: { center: [0.1, 180] } });
  assert.equal(g.regular, true);
  close(R.sample(g, "cloud_pct", 0, -179.875, [0, 0, 0]), 75);
  close(R.sample(g, "cloud_pct", 0, 179.875, [0, 0, 0]), 25);
});

test("rain colours match the radar scale", () => {
  assert.equal(R.rainColor(0.05), null);
  assert.deepEqual(R.rainColor(0.2).slice(0, 3), [136, 221, 238]);
  assert.deepEqual(R.rainColor(10).slice(0, 3), [255, 238, 0]);
  assert.deepEqual(R.rainColor(500).slice(0, 3), [193, 0, 0]);
});

test("land near the antimeridian is drawn on the copy next to the map centre", () => {
  assert.equal(R.copyShift(-179.9, -179.8, -179.6), 0);     // island just east of 180, centre east too
  assert.equal(R.copyShift(179.9, -179.8, -179.6), 360);    // centre just west: shift the island east
  assert.equal(R.copyShift(-179.9, 179.5, 179.9), -360);    // centre just east: shift a western island
  assert.equal(R.copyShift(-87.5, -92, -76), 0);
});

test("infrared: coloured cold tops are solid cloud, grey brightens with cold", () => {
  assert.equal(R.irAlpha(60, 210, 235), 0.92);        // cyan
  assert.equal(R.irAlpha(0, 0, 200), 0.92);           // deep blue: coldest tops, not "dark = clear"
  assert.equal(R.irAlpha(40, 200, 70), 0.92);         // green
  assert.equal(R.irAlpha(95, 95, 95), 0);             // warm ground
  assert.equal(R.irAlpha(200, 200, 200), 0.85);       // cold grey cloud
  const mid = R.irAlpha(157, 157, 157);
  assert.ok(mid > 0.3 && mid < 0.55, `mid grey ${mid}`);
});
