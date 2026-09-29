// Radar tab: a north-up map around the boat with live radar and satellite images (downloaded
// and cached by the weather server) and the forecast's wind, clouds and rain, which work
// offline. One timeline runs from the recent radar loop into the forecast.

import { api, fetchImage } from "./api.js";
import { $, ago, clear, compass, dayLabel, h, hourLabel, store, whenLabel } from "./dom.js";
import * as U from "./units.js";

const TILE = 256;
const RANGES_NM = [5, 10, 25, 50, 100, 200, 400];
const MIN_RANGE_NM = 3;
const MAX_RANGE_NM = 600;
const FORECAST_HOURS = 48;
const IMAGERY_REFRESH_MS = 5 * 60 * 1000;
const FIELDS_REFRESH_MS = 10 * 60 * 1000;
const MAX_TILES = 240;           // decoded images kept in memory (~64 MB)
const PARALLEL_LOADS = 6;
const WIND_SPACING_PX = 76;
const BUOY_CURRENT_S = 5400;     // buoy reports are shown at full strength within 1.5 h of the map time
const DEG = Math.PI / 180;

// Rain colours by mm/h, matched to the radar images (Marshall-Palmer: 10 dBZ ~ 0.15 mm/h ...
// 55 dBZ ~ 100 mm/h), so observed radar and forecast rain read the same way.
const RAIN_RAMP = [
  [0.15, [136, 221, 238, 0.62]], [0.5, [0, 170, 221, 0.72]], [1.3, [0, 119, 204, 0.78]],
  [3.6, [0, 68, 153, 0.82]], [8.7, [255, 238, 0, 0.85]], [21, [255, 170, 0, 0.88]],
  [49, [255, 68, 0, 0.9]], [100, [193, 0, 0, 0.92]],
];
const RAIN_LEGEND = [0.15, 1.3, 8.7, 49];

const R = {
  getters: null, active: false, built: false,
  canvas: null, ctx: null, w: 0, h: 0, dpr: 1,
  center: null, follow: true, range: store.get("shw.radar.range", 50),
  layers: { rain: true, clouds: true, wind: true, buoys: true, ...store.get("shw.radar.layers", {}) },
  land: null, landLoading: false,
  fields: null, grid: null, imagery: null, boat: null,
  steps: [], index: 0, pinnedTime: null, playing: false, playTimer: null,
  tiles: new Map(), queue: [], loading: 0, notes: {},
  timers: [], drawPending: false, colors: {}, inspect: null,
};

// ------------------------------------------------------------------ projection (Web Mercator)

const clampLat = (lat) => Math.max(-85.05, Math.min(85.05, lat));
const wrapLon = (lon) => ((((lon + 180) % 360) + 360) % 360) - 180;

function worldXY(lat, lon, z) {
  const size = TILE * 2 ** z;
  const s = Math.sin(clampLat(lat) * DEG);
  return [((lon + 180) / 360) * size, (0.5 - Math.log((1 + s) / (1 - s)) / (4 * Math.PI)) * size];
}

function worldToLatLon(x, y, z) {
  const size = TILE * 2 ** z;
  const lon = (x / size) * 360 - 180;
  const n = Math.PI - (2 * Math.PI * y) / size;
  return [Math.atan(Math.sinh(n)) / DEG, wrapLon(lon)];
}

function view() {
  const c = R.center;
  const metersPerPx = (R.range * 1852) / (Math.min(R.w, R.h) / 2 - 12);
  const z = Math.log2((156543.03392 * Math.cos(clampLat(c.lat) * DEG)) / metersPerPx);
  const [cx, cy] = worldXY(c.lat, c.lon, z);
  return { z, cx, cy, world: TILE * 2 ** z, clon: c.lon };
}

function toScreen(v, lat, lon) {
  const [x, y] = worldXY(lat, lon, v.z);
  let dx = x - v.cx;
  if (dx > v.world / 2) dx -= v.world;
  if (dx < -v.world / 2) dx += v.world;
  return [R.w / 2 + dx, R.h / 2 + (y - v.cy)];
}

const fromScreen = (v, px, py) => worldToLatLon(v.cx + px - R.w / 2, v.cy + py - R.h / 2, v.z);

/** Visible area; longitudes continuous around the map centre (may pass +-180). */
function viewBox(v) {
  const [n, w] = fromScreen(v, 0, 0);
  const [s, e] = fromScreen(v, R.w, R.h);
  return { s, n, w: v.clon + wrapLon(w - v.clon), e: v.clon + wrapLon(e - v.clon) };
}

function distanceNm(a, b) {
  const dLat = (b.lat - a.lat) * DEG;
  const dLon = wrapLon(b.lon - a.lon) * DEG;
  const x = Math.sin(dLat / 2) ** 2 + Math.cos(a.lat * DEG) * Math.cos(b.lat * DEG) * Math.sin(dLon / 2) ** 2;
  return 2 * 3440.065 * Math.asin(Math.min(1, Math.sqrt(x)));
}

function bearingDeg(a, b) {
  const y = Math.sin(wrapLon(b.lon - a.lon) * DEG) * Math.cos(b.lat * DEG);
  const x = Math.cos(a.lat * DEG) * Math.sin(b.lat * DEG) - Math.sin(a.lat * DEG) * Math.cos(b.lat * DEG) * Math.cos(wrapLon(b.lon - a.lon) * DEG);
  return (Math.atan2(y, x) / DEG + 360) % 360;
}

// ------------------------------------------------------------------ colours (follow the theme)

function readColors() {
  const cs = getComputedStyle(document.documentElement);
  const v = (name) => cs.getPropertyValue(name).trim();
  R.colors = {
    water: v("--map-water"), land: v("--map-land"), coast: v("--map-coast"), grid: v("--map-grid"),
    cloud: v("--map-cloud") || "120, 130, 145", halo: v("--map-halo"), ink: v("--ink-1"), ink2: v("--ink-2"),
    wind: v("--map-wind"), reef: v("--warning"), caution: v("--serious"), nogo: v("--critical"),
    obs: v("--series-3"), boat: v("--accent"), boatInk: v("--accent-ink"),
  };
}

// ------------------------------------------------------------------ offline land map

async function loadLand() {
  if (R.land || R.landLoading) return;
  R.landLoading = true;
  try {
    const res = await fetch("data/basemap.json");
    const data = await res.json();
    const q = data.q;
    const decode = (poly) => {
      const rings = poly.slice(4).map((enc) => {
        const pts = new Float32Array(enc.length);
        let x = 0; let y = 0;
        for (let i = 0; i < enc.length; i += 2) {
          x += enc[i]; y += enc[i + 1];
          pts[i] = x / q; pts[i + 1] = y / q;
        }
        return pts;
      });
      return { w: poly[0] / q, s: poly[1] / q, e: poly[2] / q, n: poly[3] / q, rings };
    };
    R.land = { land: data.land.map(decode), lakes: data.lakes.map(decode) };
    requestDraw();
  } catch {
    R.notes.land = "The offline map did not load.";
  } finally {
    R.landLoading = false;
  }
}

/** Whole turns to add to a shape's longitudes for the copy nearest the map centre. */
function copyShift(centerLon, w, e) {
  return Math.round((centerLon - (w + e) / 2) / 360) * 360 || 0;   // no -0
}

function polygonPath(v, box, polys) {
  const path = new Path2D();
  let any = false;
  for (const p of polys) {
    if (p.n < box.s || p.s > box.n) continue;
    const shift = copyShift(v.clon, p.w, p.e);
    if (p.e + shift < box.w || p.w + shift > box.e) continue;
    for (const ring of p.rings) {
      for (let i = 0; i < ring.length; i += 2) {
        const [x, y] = worldXY(ring[i + 1], ring[i] + shift, v.z);
        const sx = R.w / 2 + x - v.cx;
        const sy = R.h / 2 + y - v.cy;
        if (i === 0) path.moveTo(sx, sy); else path.lineTo(sx, sy);
      }
      path.closePath();
      any = true;
    }
  }
  return any ? path : null;
}

function drawLand(v, box, { fill = true } = {}) {
  if (!R.land) return;
  const ctx = R.ctx;
  // The polygons are projected relative to the view centre's world copy.
  const land = polygonPath(v, box, R.land.land);
  const lakes = polygonPath(v, box, R.land.lakes);
  if (fill) {
    if (land) { ctx.fillStyle = R.colors.land; ctx.fill(land, "evenodd"); }
    if (lakes) { ctx.fillStyle = R.colors.water; ctx.fill(lakes, "evenodd"); }
  }
  ctx.strokeStyle = R.colors.coast;
  ctx.lineWidth = fill ? 1 : 1.2;
  if (land) ctx.stroke(land);
  if (lakes) ctx.stroke(lakes);
}

// ------------------------------------------------------------------ image tiles

function tileEntry(url) {
  let t = R.tiles.get(url);
  if (!t) {
    t = { url, state: "queued", img: null, used: 0 };
    R.tiles.set(url, t);
    R.queue.push(t);
    pumpQueue();
  }
  t.used = performance.now();
  return t;
}

function pumpQueue() {
  while (R.loading < PARALLEL_LOADS && R.queue.length) {
    const t = R.queue.shift();
    if (t.state !== "queued") continue;
    t.state = "loading";
    R.loading++;
    fetchImage(t.url)
      .then((img) => { t.img = t.url.includes("/satellite/") ? cloudMask(img) : img; t.state = "ok"; })
      .catch((e) => {
        t.state = e.status === 404 || e.status === 403 ? "missing" : "error";
        t.error = e;
        if (e.status === 503 || e.status === 0) {
          const layer = t.url.split("/")[2];
          R.notes[layer] = e.message;
          t.retryAt = performance.now() + 60000;
        }
        renderStatus();
      })
      .finally(() => { R.loading--; pumpQueue(); requestDraw(); });
  }
  if (R.tiles.size > MAX_TILES) {
    const done = [...R.tiles.values()].filter((x) => x.state !== "loading" && x.state !== "queued")
      .sort((a, b) => a.used - b.used);
    for (const x of done.slice(0, R.tiles.size - MAX_TILES)) {
      x.img?.close?.();                                        // ImageBitmap
      if (x.img && "width" in x.img && !x.img.close) { x.img.width = 0; x.img.height = 0; }   // canvas (iOS memory)
      R.tiles.delete(x.url);
    }
  }
}

/**
 * Cloud opacity for one pixel of an infrared satellite image. NASA GIBS uses an enhanced
 * scale: grey that brightens as it gets colder (warm ground dark, low and mid cloud
 * lighter), then colours for the coldest cloud tops (tall showers and storms). Plain grey
 * infrared images work too. Low, warm cloud and fog look like clear sky in infrared.
 */
function irAlpha(r, g, b) {
  if (Math.max(r, g, b) - Math.min(r, g, b) > 40) return 0.92;   // coloured: cold cloud tops
  const lum = 0.3 * r + 0.59 * g + 0.11 * b;
  return Math.max(0, Math.min(1, (lum - 115) / 85)) * 0.85;
}

/** Turn an infrared tile into a cloud mask, so clear sky shows the map beneath and clouds
 * can be tinted to suit the theme. */
function cloudMask(img) {
  const c = makeCanvas(img.width, img.height);
  const x = c.getContext("2d", { willReadFrequently: true });
  x.drawImage(img, 0, 0);
  const d = x.getImageData(0, 0, c.width, c.height);
  const p = d.data;
  for (let i = 0; i < p.length; i += 4) {
    const a = irAlpha(p[i], p[i + 1], p[i + 2]);
    p[i] = 255; p[i + 1] = 255; p[i + 2] = 255;
    p[i + 3] = Math.round(p[i + 3] * a);
  }
  x.putImageData(d, 0, 0);
  img.close?.();
  return c;
}

function tileUrl(layer, frame, z, x, y) {
  const key = R.imagery?.layers?.[layer]?.key;
  return `api/tiles/${layer}/${frame}/${z}/${x}/${y}.png${key ? `?k=${encodeURIComponent(key)}` : ""}`;
}

function visibleTiles(v, maxZoom) {
  const zt = Math.max(0, Math.min(maxZoom, Math.round(v.z)));
  const f = 2 ** (v.z - zt);
  const size = TILE * f;
  const n = 2 ** zt;
  const cx = v.cx / f;
  const cy = v.cy / f;
  const x0 = Math.floor((cx - R.w / 2 / f) / TILE);
  const x1 = Math.floor((cx + R.w / 2 / f) / TILE);
  const y0 = Math.max(0, Math.floor((cy - R.h / 2 / f) / TILE));
  const y1 = Math.min(n - 1, Math.floor((cy + R.h / 2 / f) / TILE));
  const out = [];
  for (let ty = y0; ty <= y1; ty++) {
    for (let tx = x0; tx <= x1; tx++) {
      out.push({ z: zt, x: ((tx % n) + n) % n, y: ty, sx: R.w / 2 + (tx * TILE - cx) * f, sy: R.h / 2 + (ty * TILE - cy) * f, size });
    }
  }
  return out;
}

/** Draw a layer's tiles for a frame; where a tile isn't loaded yet, hold the previous frame's. */
function drawTiles(v, layer, frame, alpha, ctx = R.ctx) {
  const info = R.imagery?.layers?.[layer];
  if (!info) return { drawn: 0, total: 0 };
  const frames = info.frames.map((f) => f.time);
  const k = frames.indexOf(frame);
  ctx.save();
  ctx.globalAlpha = alpha;
  ctx.imageSmoothingEnabled = true;
  let drawn = 0;
  const tiles = visibleTiles(v, info.max_zoom);
  for (const t of tiles) {
    let img = null;
    for (let j = k; j >= Math.max(0, k - 3) && !img; j--) {
      const e = tileEntry(tileUrl(layer, frames[j], t.z, t.x, t.y));
      if (e.state === "error" && e.retryAt && performance.now() > e.retryAt) { e.state = "queued"; R.queue.push(e); pumpQueue(); }
      if (e.state === "ok") img = e.img;
      if (j === k && e.state === "ok") drawn++;
    }
    if (img) ctx.drawImage(img, t.sx, t.sy, t.size + 0.5, t.size + 0.5);
  }
  ctx.restore();
  return { drawn, total: tiles.length };
}

/** Satellite clouds: draw the masks on a scratch layer, tint them, then lay that over the map. */
function drawSatellite(v, frame) {
  if (!R.layer || R.layer.width !== R.canvas.width || R.layer.height !== R.canvas.height) {
    R.layer = makeCanvas(R.canvas.width, R.canvas.height);
  }
  const lctx = R.layer.getContext("2d");
  lctx.setTransform(1, 0, 0, 1, 0, 0);
  lctx.clearRect(0, 0, R.layer.width, R.layer.height);
  lctx.setTransform(R.dpr, 0, 0, R.dpr, 0, 0);
  drawTiles(v, "satellite", frame, 1, lctx);
  lctx.globalCompositeOperation = "source-in";
  lctx.fillStyle = `rgb(${R.colors.cloud})`;
  lctx.fillRect(0, 0, R.w, R.h);
  lctx.globalCompositeOperation = "source-over";
  R.ctx.save();
  R.ctx.setTransform(1, 0, 0, 1, 0, 0);
  R.ctx.globalAlpha = 0.8;
  R.ctx.drawImage(R.layer, 0, 0);
  R.ctx.restore();
}

function preloadFrames(v, layer) {
  const info = R.imagery?.layers?.[layer];
  if (!info) return;
  const tiles = visibleTiles(v, info.max_zoom);
  // Newest first, so the latest picture appears before the loop fills in; only as many
  // frames as fit in memory next to the other layer (a big tablet shows many tiles).
  const frames = [...info.frames].reverse().slice(0, Math.max(1, Math.floor(MAX_TILES / 2.5 / Math.max(1, tiles.length))));
  for (const f of frames) for (const t of tiles) tileEntry(tileUrl(layer, f.time, t.z, t.x, t.y));
}

// ------------------------------------------------------------------ forecast fields

function buildGrid(fields) {
  const pts = fields.points;
  const c = fields.run.center;
  const lats = [...new Set(pts.map((p) => p[0]))].sort((a, b) => a - b);
  const unwrap = (lon) => c[1] + wrapLon(lon - c[1]);
  const lons = [...new Set(pts.map((p) => unwrap(p[1])))].sort((a, b) => a - b);
  const idx = new Map(pts.map((p, i) => [`${p[0]},${unwrap(p[1])}`, i]));
  const at = [];
  for (const la of lats) at.push(lons.map((lo) => idx.get(`${la},${lo}`) ?? -1));
  const regular = at.every((row) => row.every((i) => i >= 0));
  const F = fields.fields;
  // Wind as vectors, so directions interpolate correctly (NE and NW average to N, not S).
  const u = F.wind_speed_kn.map((ser, i) => ser.map((spd, k) => {
    const d = F.wind_dir_deg[i][k];
    return spd == null || d == null ? null : -spd * Math.sin(d * DEG);
  }));
  const vv = F.wind_speed_kn.map((ser, i) => ser.map((spd, k) => {
    const d = F.wind_dir_deg[i][k];
    return spd == null || d == null ? null : -spd * Math.cos(d * DEG);
  }));
  return { lats, lons, at, regular, time: fields.time, series: { ...F, u, v: vv }, unwrap, pts };
}

function timeWeights(g, t) {
  const T = g.time;
  if (!T.length) return null;
  if (t <= T[0]) return [0, 0, 0];
  if (t >= T[T.length - 1]) return [T.length - 1, T.length - 1, 0];
  let k = 0;
  while (T[k + 1] < t) k++;
  return [k, k + 1, (t - T[k]) / (T[k + 1] - T[k])];
}

/** Model value at a position and time (bilinear in space, linear in time); null outside the grid. */
function sample(g, field, lat, lon, tw) {
  if (!tw) return null;
  const ser = g.series[field];
  const [k0, k1, f] = tw;
  const val = (i) => {
    const a = ser[i]?.[k0];
    const b = ser[i]?.[k1];
    if (a == null || b == null) return a ?? b ?? null;
    return a + (b - a) * f;
  };
  lon = g.unwrap(lon);
  const { lats, lons } = g;
  if (!g.regular) {
    // Inverse-distance fallback for an irregular set of points.
    let num = 0; let den = 0;
    g.pts.forEach((p, i) => {
      const d = Math.hypot(p[0] - lat, (g.unwrap(p[1]) - lon) * Math.cos(lat * DEG)) + 1e-6;
      const x = val(i);
      if (x != null) { num += x / d ** 2; den += 1 / d ** 2; }
    });
    return den ? num / den : null;
  }
  const half = (lats.length > 1 ? lats[1] - lats[0] : 0.25) / 2;
  const halfLon = (lons.length > 1 ? lons[1] - lons[0] : 0.25) / 2;
  if (lat < lats[0] - half || lat > lats[lats.length - 1] + half || lon < lons[0] - halfLon || lon > lons[lons.length - 1] + halfLon) return null;
  const locate = (arr, x) => {
    if (arr.length === 1 || x <= arr[0]) return [0, 0, 0];
    if (x >= arr[arr.length - 1]) return [arr.length - 1, arr.length - 1, 0];
    let i = 0;
    while (arr[i + 1] < x) i++;
    return [i, i + 1, (x - arr[i]) / (arr[i + 1] - arr[i])];
  };
  const [r0, r1, fy] = locate(lats, lat);
  const [c0, c1, fx] = locate(lons, lon);
  const q = [val(g.at[r0][c0]), val(g.at[r0][c1]), val(g.at[r1][c0]), val(g.at[r1][c1])];
  if (q.some((x) => x == null)) return q.find((x) => x != null) ?? null;
  const top = q[0] + (q[1] - q[0]) * fx;
  const bot = q[2] + (q[3] - q[2]) * fx;
  return top + (bot - top) * fy;
}

function windAt(g, lat, lon, tw) {
  const u = sample(g, "u", lat, lon, tw);
  const v = sample(g, "v", lat, lon, tw);
  if (u == null || v == null) return null;
  return { speed: Math.hypot(u, v), dir: (Math.atan2(-u, -v) / DEG + 360) % 360, gust: sample(g, "wind_gust_kn", lat, lon, tw) };
}

function gridBounds(g) {
  const hl = (g.lats.length > 1 ? g.lats[1] - g.lats[0] : 0.25) / 2;
  const hn = (g.lons.length > 1 ? g.lons[1] - g.lons[0] : 0.25) / 2;
  return { s: g.lats[0] - hl, n: g.lats[g.lats.length - 1] + hl, w: g.lons[0] - hn, e: g.lons[g.lons.length - 1] + hn };
}

function rainColor(mm) {
  let c = null;
  for (const [lim, col] of RAIN_RAMP) { if (mm >= lim) c = col; else break; }
  return c;   // [r, g, b, alpha]
}

/** Paint a model field (cloud or rain) over the forecast area. */
function drawField(v, kind, t) {
  const g = R.grid;
  if (!g) return;
  const tw = timeWeights(g, t);
  const b = gridBounds(g);
  const [x0, y0] = toScreen(v, b.n, b.w);
  const [x1, y1] = toScreen(v, b.s, b.e);
  const wpx = Math.max(2, Math.min(160, Math.round((x1 - x0) / 6)));
  const hpx = Math.max(2, Math.min(160, Math.round((y1 - y0) / 6)));
  const off = makeCanvas(wpx, hpx);
  const octx = off.getContext("2d");
  const img = octx.createImageData(wpx, hpx);
  const cloudRgb = R.colors.cloud.split(",").map(Number);
  // Fade out over the outer half grid cell, so the forecast area has soft edges.
  const fadeX = 0.5 / Math.max(1, g.lons.length);
  const fadeY = 0.5 / Math.max(1, g.lats.length);
  for (let j = 0; j < hpx; j++) {
    const [lat] = fromScreen(v, x0, y0 + ((j + 0.5) / hpx) * (y1 - y0));
    const fy = (j + 0.5) / hpx;
    const edgeY = Math.min(1, Math.min(fy, 1 - fy) / fadeY);
    for (let i = 0; i < wpx; i++) {
      const lon = b.w + ((i + 0.5) / wpx) * (b.e - b.w);
      const fx = (i + 0.5) / wpx;
      const edge = Math.min(edgeY, Math.min(1, Math.min(fx, 1 - fx) / fadeX));
      const o = (j * wpx + i) * 4;
      if (kind === "clouds") {
        const c = sample(g, "cloud_pct", lat, lon, tw);
        if (c == null) continue;
        img.data[o] = cloudRgb[0]; img.data[o + 1] = cloudRgb[1]; img.data[o + 2] = cloudRgb[2];
        img.data[o + 3] = Math.round(255 * 0.5 * edge * Math.max(0, Math.min(1, (c - 10) / 90)));
      } else {
        const mm = sample(g, "precip_mm", lat, lon, tw);
        const col = mm == null ? null : rainColor(mm);
        if (!col) continue;
        img.data[o] = col[0]; img.data[o + 1] = col[1]; img.data[o + 2] = col[2];
        img.data[o + 3] = Math.round(255 * col[3] * edge);
      }
    }
  }
  octx.putImageData(img, 0, 0);
  const ctx = R.ctx;
  ctx.save();
  ctx.imageSmoothingEnabled = true;
  ctx.drawImage(off, x0, y0, x1 - x0, y1 - y0);
  ctx.restore();
}

// A small scratch canvas (OffscreenCanvas is missing on older iOS).
function makeCanvas(w, h) {
  const c = document.createElement("canvas");
  c.width = w; c.height = h;
  return c;
}

function drawForecastArea(v) {
  const g = R.grid;
  if (!g) return;
  const b = gridBounds(g);
  const [x0, y0] = toScreen(v, b.n, b.w);
  const [x1, y1] = toScreen(v, b.s, b.e);
  const ctx = R.ctx;
  ctx.save();
  ctx.setLineDash([5, 5]);
  ctx.strokeStyle = R.colors.ink2;
  ctx.globalAlpha = 0.55;
  ctx.lineWidth = 1;
  ctx.strokeRect(x0, y0, x1 - x0, y1 - y0);
  ctx.restore();
  if (y0 > 18 && x0 > -40 && x0 < R.w - 60) haloText("Forecast area", x0 + 4, y0 - 6, { size: 11, align: "left", color: R.colors.ink2 });
}

function windColor(kn) {
  const b = R.boat;
  if (!b || kn == null) return R.colors.wind;
  if (kn >= b.max_wind_kn) return R.colors.nogo;
  if (kn >= b.reef2_kn) return R.colors.caution;
  if (kn >= b.reef1_kn) return R.colors.reef;
  return R.colors.wind;
}

function drawArrow(x, y, dirFrom, len, color, width = 2.2) {
  // Points where the wind is going, like the arrows on the Wind tab.
  const a = (dirFrom + 180) * DEG;
  const dx = Math.sin(a) * len / 2;
  const dy = -Math.cos(a) * len / 2;
  const ctx = R.ctx;
  const head = Math.min(8, len * 0.38);
  const hx = x + dx;
  const hy = y + dy;
  ctx.save();
  ctx.lineCap = "round";
  ctx.lineJoin = "round";
  for (const [stroke, w] of [[R.colors.halo, width + 3], [color, width]]) {
    ctx.strokeStyle = stroke;
    ctx.lineWidth = w;
    ctx.beginPath();
    ctx.moveTo(x - dx, y - dy);
    ctx.lineTo(hx, hy);
    ctx.moveTo(hx - Math.sin(a - 0.5) * head, hy + Math.cos(a - 0.5) * head);
    ctx.lineTo(hx, hy);
    ctx.lineTo(hx - Math.sin(a + 0.5) * head, hy + Math.cos(a + 0.5) * head);
    ctx.stroke();
  }
  ctx.restore();
}

function haloText(text, x, y, { size = 12, weight = 650, align = "center", color = R.colors.ink } = {}) {
  const ctx = R.ctx;
  ctx.save();
  ctx.font = `${weight} ${size}px system-ui, -apple-system, "Segoe UI", Roboto, sans-serif`;
  ctx.textAlign = align;
  ctx.textBaseline = "middle";
  ctx.lineJoin = "round";
  ctx.strokeStyle = R.colors.halo;
  ctx.lineWidth = 3.5;
  ctx.strokeText(text, x, y);
  ctx.fillStyle = color;
  ctx.fillText(text, x, y);
  ctx.restore();
}

/** Screen areas the wind arrows keep clear of: the boat, ring labels, and current buoy reports. */
function obstacles(v, t) {
  const out = [];
  const p = R.getters.now()?.position;
  if (p) {
    const [x, y] = toScreen(v, p.lat, p.lon);
    out.push([x, y, 14, 14]);
    const pxPerNm = (Math.min(R.w, R.h) / 2 - 12) / R.range;
    for (const f of [0.5, 1]) {   // range-ring labels
      const r = R.range * f * pxPerNm;
      out.push([x + r * Math.SQRT1_2, y + r * Math.SQRT1_2, 34, 10]);
    }
  }
  if (R.layers.buoys) {
    for (const b of R.getters.buoys()?.data?.stations || []) {
      if (Math.abs(t - b.time) > BUOY_CURRENT_S) continue;   // faded: the forecast wind matters more
      const [x, y] = toScreen(v, b.lat, b.lon);
      out.push([x + 6, y + 6, 42, 16]);   // marker, observed-wind arrow and label below
    }
  }
  return out;
}

/** The map's own buttons and labels, as rectangles in canvas coordinates. */
function overlayRects() {
  const base = R.canvas.getBoundingClientRect();
  return [...document.querySelectorAll("#view-radar .map-stamp > *, #view-radar .map-tools, #view-radar .map-scale")]
    .map((el) => el.getBoundingClientRect())
    .filter((r) => r.width && r.height)
    .map((r) => [r.left - base.left, r.top - base.top, r.right - base.left, r.bottom - base.top]);
}

function drawWind(v, t) {
  const g = R.grid;
  if (!g) return;
  const tw = timeWeights(g, t);
  const avoid = obstacles(v, t);
  const rects = overlayRects();
  const b = gridBounds(g);
  const [x0, y0] = toScreen(v, b.n, b.w);
  const [x1, y1] = toScreen(v, b.s, b.e);
  const sp = WIND_SPACING_PX;
  // Lattice anchored to the grid's corner so arrows don't swim while panning.
  const startX = x0 + ((((sp / 2 - x0) % sp) + sp) % sp);
  const startY = y0 + ((((sp / 2 - y0) % sp) + sp) % sp);
  for (let y = Math.max(startY, 14); y < Math.min(y1, R.h - 8); y += sp) {
    for (let x = Math.max(startX, 14); x < Math.min(x1, R.w - 8); x += sp) {
      // The arrow and its number fill roughly x +-16, y - 22 .. y + 25.
      const hit = ([l, tp, r, b]) => x + 16 > l && x - 16 < r && y + 25 > tp && y - 22 < b;
      if (avoid.some(([ox, oy, rx, ry]) => hit([ox - rx, oy - ry, ox + rx, oy + ry])) || rects.some(hit)) continue;
      const [lat, lon] = fromScreen(v, x, y);
      const w = windAt(g, lat, lon, tw);
      if (!w) continue;
      const color = windColor(w.speed);
      drawArrow(x, y - 6, w.dir, 30, color);
      haloText(U.fmtSpeed(w.speed), x, y + 17, { size: 12, color });
    }
  }
}

function drawBuoys(v, t) {
  const list = R.getters.buoys()?.data?.stations || [];
  const ctx = R.ctx;
  for (const b of list) {
    const [x, y] = toScreen(v, b.lat, b.lon);
    if (x < -20 || y < -20 || x > R.w + 20 || y > R.h + 20) continue;
    // Latest reports only: faded when the map shows a time hours away from them.
    ctx.save();
    ctx.globalAlpha = Math.abs(t - b.time) > BUOY_CURRENT_S ? 0.35 : 1;
    ctx.save();
    ctx.fillStyle = R.colors.obs;
    ctx.strokeStyle = R.colors.halo;
    ctx.lineWidth = 2;
    ctx.beginPath();
    ctx.rect(x - 5, y - 5, 10, 10);
    ctx.fill();
    ctx.stroke();
    ctx.restore();
    if (b.wind_dir_deg != null) drawArrow(x + 20, y, b.wind_dir_deg, 22, R.colors.obs, 2);
    const label = b.wind_speed_kn != null ? `${b.station} · ${U.fmtSpeed(b.wind_speed_kn)}` : String(b.station);
    haloText(label, x, y + 15, { size: 11, color: R.colors.obs });
    ctx.restore();
  }
}

function drawBoat(v) {
  const now = R.getters.now();
  const p = now?.position;
  if (!p) return;
  const [x, y] = toScreen(v, p.lat, p.lon);
  const inst = now.instruments || {};
  const hdg = inst.heading_true_deg?.value ?? inst.cog_deg?.value;
  const ctx = R.ctx;
  ctx.save();
  ctx.translate(x, y);
  ctx.fillStyle = R.colors.boat;
  ctx.strokeStyle = R.colors.halo;
  ctx.lineWidth = 2.5;
  ctx.beginPath();
  if (hdg != null) {
    ctx.rotate(hdg * DEG);
    ctx.moveTo(0, -11); ctx.lineTo(7, 8); ctx.lineTo(0, 4); ctx.lineTo(-7, 8); ctx.closePath();
  } else {
    ctx.arc(0, 0, 7, 0, 2 * Math.PI);
  }
  ctx.stroke();
  ctx.fill();
  ctx.restore();
}

/** "5 nm", "2.5 nm", "46 km": no needless ".0". */
function rangeText(nm) {
  const val = U.distance(nm);
  const d = Math.abs(val - Math.round(val)) < 0.05 ? 0 : 1;
  return `${U.num(val, d)} ${U.distanceUnit()}`;
}

function drawRings(v) {
  const p = R.getters.now()?.position;
  if (!p) return;
  const [x, y] = toScreen(v, p.lat, p.lon);
  const pxPerNm = (Math.min(R.w, R.h) / 2 - 12) / R.range;
  const ctx = R.ctx;
  ctx.save();
  ctx.strokeStyle = R.colors.grid;
  ctx.lineWidth = 1;
  ctx.setLineDash([2, 4]);
  for (const f of [0.5, 1]) {
    const r = R.range * f * pxPerNm;
    ctx.beginPath();
    ctx.arc(x, y, r, 0, 2 * Math.PI);
    ctx.stroke();
  }
  ctx.restore();
  for (const f of [0.5, 1]) {
    const r = R.range * f * pxPerNm;
    const lx = x + r * Math.SQRT1_2;
    const ly = y + r * Math.SQRT1_2;
    if (lx < R.w - 30 && ly < R.h - 34) haloText(rangeText(R.range * f), lx, ly, { size: 11, weight: 600, color: R.colors.ink2 });
  }
}

// ------------------------------------------------------------------ timeline

function nearestFrame(layer, t, maxAge) {
  const frames = R.imagery?.layers?.[layer]?.frames || [];
  let best = null;
  for (const f of frames) if (f.time <= t + 60 && t - f.time <= maxAge && (!best || f.time > best.time)) best = f;
  return best;
}

function buildSteps() {
  const now = Date.now() / 1000;
  const obs = new Set();
  const ahead = new Set();
  for (const layer of ["radar", "satellite"]) {
    for (const f of R.imagery?.layers?.[layer]?.frames || []) (f.forecast ? ahead : obs).add(f.time);
  }
  const obsTimes = [...obs].sort((a, b) => a - b);
  const lastObs = obsTimes.length ? obsTimes[obsTimes.length - 1] : null;
  const steps = obsTimes.map((t) => ({ t, kind: "obs" }));
  for (const t of [...ahead].sort((a, b) => a - b)) if (!lastObs || t > lastObs) steps.push({ t, kind: "nowcast" });
  const model = R.fields?.time || [];
  const from = steps.length ? Math.min(steps[steps.length - 1].t + 1800, now - 1800) : now - 3 * 3600;
  for (const t of model) if (t >= from && t <= now + FORECAST_HOURS * 3600) steps.push({ t, kind: "model" });
  if (!steps.length) steps.push({ t: now, kind: "model" });
  R.steps = steps;
  // "Now": the newest observed frame if it is recent, else the step closest to the present
  // (old radar pictures stay on the timeline under their own times).
  let nowIdx = lastObs != null && now - lastObs < 30 * 60 ? obsTimes.length - 1 : -1;
  if (nowIdx < 0) nowIdx = steps.reduce((best, s, i) => (Math.abs(s.t - now) < Math.abs(steps[best].t - now) ? i : best), 0);
  R.nowIndex = nowIdx;
  if (R.pinnedTime != null) {
    R.index = steps.reduce((best, s, i) => (Math.abs(s.t - R.pinnedTime) < Math.abs(steps[best].t - R.pinnedTime) ? i : best), 0);
  } else {
    R.index = nowIdx;
  }
  renderTimeline();
}

function renderTimeline() {
  const input = $("#map-time");
  input.max = String(Math.max(0, R.steps.length - 1));
  input.value = String(R.index);
  input.setAttribute("aria-valuetext", stepLabel(R.steps[R.index]));
  const n = R.steps.length - 1 || 1;
  const labels = clear($("#map-time-labels"));
  const now = R.steps[R.nowIndex]?.t ?? Date.now() / 1000;
  const marks = [[0, R.steps[0] ? hourLabel(R.steps[0].t) : ""], [R.nowIndex, "Now"]];
  for (const hrs of [12, 24, 36, 48]) {
    const i = R.steps.findIndex((s) => s.t >= now + hrs * 3600 - 3600);
    if (i > 0) marks.push([i, `+${hrs} h`]);
  }
  const used = [];
  const width = $("#map-time-track").clientWidth || 300;
  // Sort so "Now" wins when labels would overlap.
  marks.sort((a, b) => (b[0] === R.nowIndex) - (a[0] === R.nowIndex));
  for (const [i, text] of marks) {
    const frac = i / n;
    const px = 10 + (width - 20) * frac;
    if (used.some((u) => Math.abs(u - px) < 30 + text.length * 3.2)) continue;
    used.push(px);
    // Match the range thumb, whose centre travels from 10px to (width - 10px).
    const span = h("span", { class: i === R.nowIndex ? "now" : null, text });
    span.style.left = `calc(10px + (100% - 20px) * ${frac})`;
    if (frac === 0) span.style.transform = "translateX(-10px)";
    labels.append(span);
  }
  $("#map-time-track").style.setProperty("--now-frac", String(R.nowIndex / n));
}

function stepLabel(step) {
  if (!step) return "";
  const now = Date.now() / 1000;
  const rel = (step.t - now) / 3600;
  const relText = step.kind === "model" && Math.abs(rel) >= 1 ? ` (${rel > 0 ? "+" : "−"}${Math.round(Math.abs(rel))} h)` : "";
  return `${dayLabel(step.t)} ${hourLabel(step.t)}${relText}`;
}

// ------------------------------------------------------------------ drawing

function requestDraw() {
  if (R.drawPending || !R.active) return;
  R.drawPending = true;
  requestAnimationFrame(() => { R.drawPending = false; draw(); });
}

function draw() {
  const ctx = R.ctx;
  if (!ctx || !R.center || !R.w) return;
  const v = view();
  const box = viewBox(v);
  const step = R.steps[R.index] || { t: Date.now() / 1000, kind: "model" };
  ctx.setTransform(R.dpr, 0, 0, R.dpr, 0, 0);
  ctx.fillStyle = R.colors.water;
  ctx.fillRect(0, 0, R.w, R.h);
  if (R.imagery?.layers?.basemap?.frames?.length) drawTiles(v, "basemap", 0, 1);
  else drawLand(v, box);

  const live = step.kind !== "model";
  const used = { clouds: null, rain: null };
  if (R.layers.clouds) {
    const sat = live ? nearestFrame("satellite", step.t, 3600) : null;
    if (sat) { drawSatellite(v, sat.time); used.clouds = { src: "satellite", t: sat.time }; }
    else if (R.grid) { drawField(v, "clouds", step.t); used.clouds = { src: "model" }; }
  }
  if (R.imagery?.layers?.basemap?.frames?.length || R.layers.clouds) drawLand(v, box, { fill: false });
  if (R.layers.rain) {
    const radar = step.kind !== "model" ? nearestFrame("radar", step.t, 900) : null;
    if (radar) { drawTiles(v, "radar", radar.time, 0.9); used.rain = { src: "radar", t: radar.time }; }
    else if (R.grid) { drawField(v, "rain", step.t); used.rain = { src: "model" }; }
  }
  drawForecastArea(v);
  drawRings(v);
  if (R.layers.buoys) drawBuoys(v, step.t);
  if (R.layers.wind) drawWind(v, step.t);
  drawBoat(v);
  R.used = used;
  renderStamp(step, used);
}

function renderStamp(step, used) {
  const parts = [];
  if (step.kind !== "model") {
    if (used.rain) parts.push(used.rain.src === "radar" ? `radar ${hourLabel(used.rain.t)}` : "rain: forecast");
    if (used.clouds) parts.push(used.clouds.src === "satellite" ? `satellite ${hourLabel(used.clouds.t)}` : "clouds: forecast");
  }
  const stamp = $("#map-stamp");
  clear(stamp).append(
    h("strong", { text: stepLabel(step) }),
    h("span", { class: `kind ${step.kind === "model" ? "fc" : "obs"}`, text: step.kind === "model" ? "Forecast" : step.kind === "nowcast" ? "Radar nowcast" : "Observed" }),
    h("span", { class: "src", text: parts.join(" · ") }));
}

// ------------------------------------------------------------------ status, legend, attribution

function renderStatus() {
  const box = clear($("#map-status"));
  const msgs = [];
  const im = R.imagery;
  if (R.notes.server) msgs.push(R.notes.server);
  else if (im) {
    if (!im.enabled) msgs.push("Live radar and satellite images are turned off on the server (imagery.enabled).");
    else if (im.paused) msgs.push(`Live images paused: ${im.paused}. Forecast layers still work.`);
    else {
      for (const [layer, name] of [["radar", "Radar"], ["satellite", "Satellite"]]) {
        const info = im.layers[layer];
        const note = R.notes[layer];
        const text = info?.error || note;
        if (text) msgs.push(/^Live /.test(text) ? `${text}.` : `${name}: ${text}.`);
      }
      if (im.saver) msgs.push("Data saver: latest images only, no loop.");
    }
  }
  if (!R.fields && R.notes.fields) msgs.push(R.notes.fields);
  if (R.notes.land) msgs.push(R.notes.land);
  for (const m of msgs) box.append(h("p", { text: m }));
  box.hidden = !msgs.length;
}

function renderLegend() {
  const box = clear($("#map-legend"));
  if (R.layers.rain) {
    box.append(h("div", { class: "legend-row" }, h("span", { class: "legend-name", text: `Rain (${U.precipUnit()}/h)` }),
      h("span", { class: "ramp" }, RAIN_RAMP.map(([, c]) => h("i", { style: { background: `rgba(${c[0]},${c[1]},${c[2]},${c[3]})` } }))),
      h("span", { class: "ramp-labels" }, RAIN_LEGEND.map((mm) => {
        const i = RAIN_RAMP.findIndex(([lim]) => lim === mm);
        const val = U.precip(mm);
        const d = U.precipUnit() === "in" ? (val < 0.1 ? 2 : 1) : val < 10 ? 1 : 0;
        return h("span", { style: { left: `${(i / RAIN_RAMP.length) * 100}%` }, text: U.num(val, d) });
      }))));
  }
  if (R.layers.clouds) {
    box.append(h("div", { class: "legend-row" }, h("span", { class: "legend-name", text: "Clouds" }),
      h("span", { class: "ramp cloud-ramp" }), h("span", { class: "ramp-labels two" }, h("span", { text: "clear" }), h("span", { text: "overcast" }))));
  }
  if (R.layers.wind && R.boat) {
    const b = R.boat;
    const lim = (kn) => U.fmtLimit("speed", kn);
    box.append(h("div", { class: "legend-row" }, h("span", { class: "legend-name", text: "Wind (forecast)" }),
      h("span", { class: "keys" },
        h("span", {}, h("i", { class: "k-wind" }), `under ${lim(b.reef1_kn)}`),
        h("span", {}, h("i", { class: "k-reef" }), `reef ${lim(b.reef1_kn)}+`),
        h("span", {}, h("i", { class: "k-caution" }), `2nd reef ${lim(b.reef2_kn)}+`),
        h("span", {}, h("i", { class: "k-nogo" }), `over limit ${lim(b.max_wind_kn)}`))));
  }
  if (R.layers.buoys) {
    box.append(h("div", { class: "legend-row" }, h("span", { class: "legend-name", text: "Observed" }),
      h("span", { class: "keys" }, h("span", {}, h("i", { class: "k-obs" }), "buoys and coastal stations, latest report"))));
  }
}

function renderAttribution() {
  const parts = [];
  for (const layer of ["radar", "satellite", "basemap"]) {
    const info = R.imagery?.layers?.[layer];
    if (!info?.attribution) continue;
    parts.push(info.link ? h("a", { href: info.link, target: "_blank", rel: "noopener", text: info.attribution }) : info.attribution);
  }
  parts.push("Forecast: Open-Meteo", "Map: Natural Earth");
  const box = clear($("#map-attrib"));
  parts.forEach((p, i) => { if (i) box.append(" · "); box.append(p); });
}

// ------------------------------------------------------------------ inspector (tap the map)

function inspect(px, py) {
  const v = view();
  const [lat, lon] = fromScreen(v, px, py);
  const step = R.steps[R.index];
  const rows = [];
  const buoy = (R.layers.buoys ? R.getters.buoys()?.data?.stations || [] : []).find((b) => {
    const [x, y] = toScreen(v, b.lat, b.lon);
    return Math.hypot(x - px, y - py) < 22;
  });
  if (buoy) {
    const age = ago(Date.now() / 1000 - buoy.time);
    rows.push(["Station", `${buoy.station} (reported ${age})`]);
    if (buoy.wind_speed_kn != null) {
      rows.push(["Wind", `${U.fmtSpeed(buoy.wind_speed_kn)}${buoy.wind_gust_kn != null ? `–${U.fmtSpeed(buoy.wind_gust_kn)}` : ""} ${U.speedUnit()} from ${compass(buoy.wind_dir_deg)}`]);
    }
    if (buoy.wave_height_m != null) rows.push(["Waves", `${U.fmtHeight(buoy.wave_height_m)} ${U.heightUnit()}${buoy.dominant_period_s ? ` @ ${Math.round(buoy.dominant_period_s)} s` : ""}`]);
    if (buoy.pressure_hpa != null) rows.push(["Pressure", `${U.fmtPressure(buoy.pressure_hpa)} ${U.pressureUnit()}`]);
  }
  if (R.grid && step) {
    const tw = timeWeights(R.grid, step.t);
    const w = windAt(R.grid, lat, lon, tw);
    if (w) {
      rows.push(["Wind (forecast)", `${U.fmtSpeed(w.speed)} ${U.speedUnit()} from ${compass(w.dir)} ${Math.round(w.dir)}°${w.gust != null ? `, gusts ${U.fmtSpeed(w.gust)}` : ""}`]);
      const c = sample(R.grid, "cloud_pct", lat, lon, tw);
      if (c != null) rows.push(["Cloud (forecast)", `${Math.round(c)} %`]);
      const mm = sample(R.grid, "precip_mm", lat, lon, tw);
      if (mm != null) rows.push(["Rain (forecast)", mm < 0.1 ? "none" : `${U.fmtDisplay("precip", U.precip(mm))} ${U.precipUnit()}/h`]);
    } else rows.push(["Forecast", "outside the downloaded forecast area (Settings > download a passage area)"]);
  }
  const boatPos = R.getters.now()?.position;
  let where = U.fmtLatLon(lat, lon);
  if (boatPos) {
    const here = { lat, lon };
    const d = distanceNm(boatPos, here);
    if (d >= 0.1) where += ` · ${U.fmtDistance(d)} ${U.distanceUnit()} ${compass(bearingDeg(boatPos, here))} of the boat`;
  }
  const box = $("#map-inspect");
  clear(box).append(
    h("div", { class: "inspect-head" }, h("strong", { text: stepLabel(step) }),
      h("button", { type: "button", class: "icon-btn small", "aria-label": "Close", onclick: () => { box.hidden = true; } }, "×")),
    h("div", { class: "muted", text: where }),
    h("dl", {}, rows.map(([k, val]) => [h("dt", { text: k }), h("dd", { text: val })])));
  box.hidden = false;
  const bw = box.offsetWidth;
  const bh = box.offsetHeight;
  box.style.left = `${Math.max(8, Math.min(R.w - bw - 8, px - bw / 2))}px`;
  box.style.top = `${py + 16 + bh < R.h ? py + 16 : Math.max(8, py - bh - 16)}px`;
}

// ------------------------------------------------------------------ interaction

function setRange(nm, anchor) {
  const before = anchor && view();
  const [alat, alon] = anchor ? fromScreen(before, anchor[0], anchor[1]) : [];
  R.range = Math.max(MIN_RANGE_NM, Math.min(MAX_RANGE_NM, nm));
  store.set("shw.radar.range", Math.round(R.range));
  if (anchor && !R.follow) {
    // Keep the point under the fingers/cursor where it is.
    const v = view();
    const [ax, ay] = worldXY(alat, alon, v.z);
    const [cx, cy] = [ax - (anchor[0] - R.w / 2), ay - (anchor[1] - R.h / 2)];
    const [lat, lon] = worldToLatLon(cx, cy, v.z);
    R.center = { lat, lon };
  }
  $("#map-range").textContent = rangeText(R.range);
  requestDraw();
}

/** Zoom to the next preset range: dir -1 = in (smaller range), +1 = out. */
function stepRange(dir) {
  const next = dir < 0
    ? [...RANGES_NM].reverse().find((r) => r < R.range - 0.01) ?? RANGES_NM[0]
    : RANGES_NM.find((r) => r > R.range + 0.01) ?? RANGES_NM[RANGES_NM.length - 1];
  setRange(next);
}

function followBoat() {
  const p = R.getters.now()?.position;
  if (p) R.center = { lat: p.lat, lon: p.lon };
  R.follow = true;
  $("#map-center").setAttribute("aria-pressed", "true");
  requestDraw();
}

function bindPointer() {
  const cv = R.canvas;
  const ptrs = new Map();
  let gesture = null;
  cv.addEventListener("pointerdown", (e) => {
    cv.setPointerCapture(e.pointerId);
    ptrs.set(e.pointerId, [e.offsetX, e.offsetY]);
    const pts = [...ptrs.values()];
    if (pts.length === 1) gesture = { start: pts[0], last: pts[0], moved: false, t: performance.now() };
    else if (pts.length === 2) gesture = { pinch: Math.hypot(pts[0][0] - pts[1][0], pts[0][1] - pts[1][1]), range: R.range, moved: true };
  });
  cv.addEventListener("pointermove", (e) => {
    if (!ptrs.has(e.pointerId) || !gesture) return;
    ptrs.set(e.pointerId, [e.offsetX, e.offsetY]);
    const pts = [...ptrs.values()];
    if (pts.length >= 2 && gesture.pinch) {
      const d = Math.hypot(pts[0][0] - pts[1][0], pts[0][1] - pts[1][1]);
      setRange(gesture.range * (gesture.pinch / Math.max(d, 1)), [(pts[0][0] + pts[1][0]) / 2, (pts[0][1] + pts[1][1]) / 2]);
      return;
    }
    const [x, y] = pts[0];
    if (!gesture.moved && Math.hypot(x - gesture.start[0], y - gesture.start[1]) < 6) return;
    gesture.moved = true;
    const v = view();
    const [lat, lon] = worldToLatLon(v.cx - (x - gesture.last[0]), v.cy - (y - gesture.last[1]), v.z);
    R.center = { lat, lon };
    gesture.last = [x, y];
    if (R.follow) { R.follow = false; $("#map-center").setAttribute("aria-pressed", "false"); }
    requestDraw();
  });
  const end = (e) => {
    if (!ptrs.has(e.pointerId)) return;
    ptrs.delete(e.pointerId);
    if (gesture && !gesture.moved && !gesture.pinch && ptrs.size === 0 && performance.now() - gesture.t < 600) inspect(e.offsetX, e.offsetY);
    if (ptrs.size === 0) gesture = null;
    else if (ptrs.size === 1) { const p = [...ptrs.values()][0]; gesture = { start: p, last: p, moved: true }; }
    if (!R.follow) preloadVisible();
  };
  cv.addEventListener("pointerup", end);
  cv.addEventListener("pointercancel", end);
  cv.addEventListener("wheel", (e) => {
    e.preventDefault();
    setRange(R.range * Math.exp(e.deltaY * 0.0015), [e.offsetX, e.offsetY]);
  }, { passive: false });
  cv.addEventListener("keydown", (e) => {
    const v = view();
    const move = { ArrowLeft: [-60, 0], ArrowRight: [60, 0], ArrowUp: [0, -60], ArrowDown: [0, 60] }[e.key];
    if (move) {
      e.preventDefault();
      const [lat, lon] = worldToLatLon(v.cx + move[0], v.cy + move[1], v.z);
      R.center = { lat, lon };
      R.follow = false;
      $("#map-center").setAttribute("aria-pressed", "false");
      requestDraw();
    } else if (e.key === "+" || e.key === "=") stepRange(-1);
    else if (e.key === "-") stepRange(1);
  });
}

function setIndex(i) {
  R.index = Math.max(0, Math.min(R.steps.length - 1, i));
  R.pinnedTime = R.index === R.nowIndex ? null : R.steps[R.index]?.t;
  $("#map-time").value = String(R.index);
  $("#map-time").setAttribute("aria-valuetext", stepLabel(R.steps[R.index]));
  requestDraw();
}

function togglePlay(force) {
  R.playing = force ?? !R.playing;
  const btn = $("#map-play");
  btn.setAttribute("aria-pressed", String(R.playing));
  btn.setAttribute("aria-label", R.playing ? "Pause" : "Play");
  btn.classList.toggle("playing", R.playing);
  clearTimeout(R.playTimer);
  if (!R.playing) return;
  // The observed loop repeats; forecast hours play forward to the end.
  const obsEnd = R.steps.findIndex((s) => s.kind === "model");
  const inObs = obsEnd !== 0 && (obsEnd < 0 || R.index < obsEnd);
  const lo = inObs ? 0 : Math.max(0, obsEnd);
  const hi = inObs ? (obsEnd < 0 ? R.steps.length - 1 : obsEnd - 1) : R.steps.length - 1;
  if (inObs) preloadVisible();
  const tick = () => {
    if (!R.playing) return;
    let next = R.index + 1;
    if (next > hi) {
      if (!inObs) { togglePlay(false); return; }
      next = lo;
    }
    setIndex(next);
    R.playTimer = setTimeout(tick, next === hi ? 1400 : inObs ? 550 : 800);
  };
  R.playTimer = setTimeout(tick, 200);
}

function preloadVisible() {
  if (!R.center || !R.imagery) return;
  const v = view();
  if (R.layers.rain) preloadFrames(v, "radar");
  if (R.layers.clouds) preloadFrames(v, "satellite");
}

function resize() {
  R.dpr = Math.min(window.devicePixelRatio || 1, 2);
  R.w = R.canvas.clientWidth;
  R.h = R.canvas.clientHeight;
  if (!R.w || !R.h) return;
  R.canvas.width = Math.round(R.w * R.dpr);
  R.canvas.height = Math.round(R.h * R.dpr);
  requestDraw();
}

// ------------------------------------------------------------------ loading

async function loadImagery() {
  try {
    R.imagery = await api.imagery();
    R.notes.server = null;
    for (const layer of ["radar", "satellite"]) if (!R.imagery.layers?.[layer]?.error) delete R.notes[layer];
  } catch (e) {
    if (e.status === 0) R.notes.server = "Cannot reach the weather server; showing what this phone already has.";
  }
  buildSteps();
  renderStatus();
  renderAttribution();
  preloadVisible();
  requestDraw();
}

async function loadFields() {
  try {
    R.fields = await api.map(3, FORECAST_HOURS);
    R.grid = buildGrid(R.fields);
    delete R.notes.fields;
  } catch (e) {
    if (e.status === 404) R.notes.fields = "No forecast downloaded yet: wind, cloud and rain forecasts appear once the server has been online.";
  }
  buildSteps();
  renderStatus();
  requestDraw();
}

async function loadBoat() {
  try { R.boat = await api.boat(); } catch { /* keep defaults */ }
  renderLegend();
  requestDraw();
}

// ------------------------------------------------------------------ public

export function initRadar(getters) {
  R.getters = getters;
}

function build() {
  if (R.built) return;
  R.built = true;
  R.canvas = $("#radar-canvas");
  R.ctx = R.canvas.getContext("2d");
  readColors();
  new ResizeObserver(resize).observe(R.canvas);
  bindPointer();
  $("#map-zoom-in").addEventListener("click", () => stepRange(-1));
  $("#map-zoom-out").addEventListener("click", () => stepRange(1));
  $("#map-center").addEventListener("click", followBoat);
  $("#map-play").addEventListener("click", () => togglePlay());
  $("#map-time").addEventListener("input", (e) => { togglePlay(false); setIndex(Number(e.target.value)); });
  $("#map-now").addEventListener("click", () => { togglePlay(false); setIndex(R.nowIndex); });
  for (const b of document.querySelectorAll("#map-layers button")) {
    b.setAttribute("aria-pressed", String(Boolean(R.layers[b.dataset.layer])));
    b.addEventListener("click", () => {
      R.layers[b.dataset.layer] = !R.layers[b.dataset.layer];
      b.setAttribute("aria-pressed", String(R.layers[b.dataset.layer]));
      store.set("shw.radar.layers", R.layers);
      renderLegend();
      preloadVisible();
      requestDraw();
    });
  }
  // Re-read colours when the theme changes.
  new MutationObserver(() => { readColors(); requestDraw(); }).observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
  window.matchMedia?.("(prefers-color-scheme: dark)").addEventListener?.("change", () => { readColors(); requestDraw(); });
  $("#map-range").textContent = rangeText(R.range);
}

function stopTimers() {
  for (const t of R.timers) clearInterval(t);
  R.timers = [];
}

function startTimers() {
  stopTimers();
  R.timers.push(setInterval(loadImagery, IMAGERY_REFRESH_MS), setInterval(loadFields, FIELDS_REFRESH_MS));
}

export function showRadar(active) {
  R.active = active;
  stopTimers();
  if (!active) { togglePlay(false); return; }
  build();
  resize();
  if (!R.center || R.follow) followBoat();
  loadLand();
  loadBoat();
  loadFields();
  loadImagery();
  renderLegend();
  if (!document.hidden) startTimers();
}

// A browser left open on this tab (a nav PC) must not keep downloading pictures unseen.
if (typeof document !== "undefined") document.addEventListener("visibilitychange", () => {
  if (!R.active) return;
  if (document.hidden) {
    stopTimers();
    togglePlay(false);
    R.queue.length = 0;
    for (const [url, t] of R.tiles) if (t.state === "queued") R.tiles.delete(url);
  } else {
    loadImagery();
    loadFields();
    startTimers();
  }
});

/** New data from the app's polling (position, instruments, buoys). */
export function radarUpdate() {
  if (!R.active) return;
  if (R.follow) {
    const p = R.getters.now()?.position;
    if (p && (!R.center || distanceNm(R.center, p) > 0.01)) R.center = { lat: p.lat, lon: p.lon };
  }
  requestDraw();
}

/** Units changed: relabel everything. */
export function radarUnitsChanged() {
  if (!R.built) return;
  $("#map-range").textContent = rangeText(R.range);
  renderLegend();
  requestDraw();
}

// Exposed for tests.
export const _internals = { worldXY, worldToLatLon, buildGrid, sample, timeWeights, windAt, rainColor, distanceNm, bearingDeg, copyShift, irAlpha };
