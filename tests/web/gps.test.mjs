// This device's GPS polling (web/js/gps.js), with fake geolocation, timers and page.
import assert from "node:assert/strict";
import { test } from "node:test";

import * as G from "../../web/js/gps.js";

const settle = () => new Promise((r) => setImmediate(r));

function fakeEnv({ secure = true } = {}) {
  let now = 1_000_000;
  const timers = [];
  const env = {
    win: { navigator: { geolocation: {} }, isSecureContext: secure },
    doc: { hidden: false, listeners: {}, addEventListener(type, fn) { this.listeners[type] = fn; } },
    geolocation: { requests: [], getCurrentPosition(ok, err, opts) { this.requests.push({ ok, err, opts }); } },
    sent: [],
    sendFails: false,
    serverPos: null,
    async send(fix) {
      if (env.sendFails) throw new Error("Cannot reach the weather server");
      env.sent.push(fix);
      return { source: "phone", lat: fix.lat, lon: fix.lon };
    },
    serverPosition: () => env.serverPos,
    onSent: () => {},
    now: () => now,
    setTimeout(fn, ms) { timers.push({ fn, at: now + ms, done: false }); return timers.length - 1; },
    clearTimeout(id) { if (timers[id]) timers[id].done = true; },
    /** Run every timer due within `ms`, in order. */
    advance(ms) {
      const end = now + ms;
      for (;;) {
        const due = timers.filter((t) => !t.done && t.at <= end).sort((a, b) => a.at - b.at)[0];
        if (!due) break;
        now = due.at;
        due.done = true;
        due.fn();
      }
      now = end;
    },
    get last() { return this.geolocation.requests.at(-1); },
    setVisible(visible) { this.doc.hidden = !visible; this.doc.listeners.visibilitychange(); },
  };
  return env;
}

const fix = (lat, lon, accuracy = 8) => ({ coords: { latitude: lat, longitude: lon, accuracy }, timestamp: Date.now() });

function start(opts) {
  G._reset();
  const env = fakeEnv(opts);
  G.initGps(env);
  return env;
}

test("rates and their labels", () => {
  assert.ok(G.RATES.includes(G.DEFAULT_RATE));
  assert.equal(G.rateLabel(10), "10 seconds");
  assert.equal(G.rateLabel(60), "1 minute");
  assert.equal(G.rateLabel(300), "5 minutes");
  assert.equal(G.rateLabel(3600), "1 hour");
  assert.equal(G.everyLabel(60), "every minute");
  assert.equal(G.everyLabel(30), "every 30 seconds");
});

test("only secure pages with geolocation can share location", () => {
  assert.equal(G.support({ navigator: {}, isSecureContext: true }), "unsupported");
  assert.equal(G.support({ navigator: { geolocation: {} }, isSecureContext: false }), "insecure");
  assert.equal(G.support({ navigator: { geolocation: {} }, isSecureContext: true }), "ok");
});

test("rough fixes and a working boat GPS keep a fix from being sent", () => {
  const good = { lat: 41.9, lon: -87.5, accuracy: 15 };
  assert.equal(G.verdict(good, null), "send");
  assert.equal(G.verdict({ ...good, accuracy: 25_000 }, null), "rough");
  assert.equal(G.verdict({ ...good, accuracy: undefined }, null), "rough");
  assert.equal(G.verdict(good, { source: "gps", age_s: 3 }), "boat-gps");
  assert.equal(G.verdict(good, { source: "gps", age_s: 3600 }), "send");   // the boat's GPS went quiet
  assert.equal(G.verdict(good, { source: "phone", age_s: 3 }), "send");
});

test("fix options suit the polling rate", () => {
  assert.deepEqual(G.fixOptions(10), { enableHighAccuracy: true, timeout: 15000, maximumAge: 5000 });
  assert.deepEqual(G.fixOptions(60), { enableHighAccuracy: true, timeout: 60000, maximumAge: 30000 });
  assert.deepEqual(G.fixOptions(3600), { enableHighAccuracy: true, timeout: 60000, maximumAge: 30000 });
});

test("polls at the chosen rate and sends good fixes", async () => {
  const env = start();
  assert.equal(G.gpsState().status, "off");
  G.setGpsEnabled(true);
  env.advance(0);
  assert.equal(env.geolocation.requests.length, 1);
  assert.equal(G.gpsState().status, "waiting");
  env.last.ok(fix(41.9, -87.5));
  await settle();
  assert.deepEqual(env.sent.map((f) => [f.lat, f.lon, f.accuracy]), [[41.9, -87.5, 8]]);
  assert.equal(G.gpsState().status, "ok");
  env.advance(59_000);
  assert.equal(env.geolocation.requests.length, 1, "not before a minute has passed");
  env.advance(1_000);
  assert.equal(env.geolocation.requests.length, 2);
  env.last.ok(fix(41.91, -87.5));
  await settle();
  assert.equal(env.sent.length, 2);
});

test("a new rate applies at once", async () => {
  const env = start();
  G.setGpsEnabled(true);
  env.advance(0);
  env.last.ok(fix(41.9, -87.5));
  await settle();
  env.advance(5_000);
  G.setGpsRate(10);
  env.advance(4_999);
  assert.equal(env.geolocation.requests.length, 1);
  env.advance(1);
  assert.equal(env.geolocation.requests.length, 2);
  assert.equal(env.last.opts.maximumAge, 5000);
  G.setGpsRate(7);                                   // not offered: falls back to the default
  assert.equal(G.gpsState().rate, G.DEFAULT_RATE);
});

test("pauses while the app is hidden and catches up when it's back", async () => {
  const env = start();
  G.setGpsEnabled(true);
  env.advance(0);
  env.last.ok(fix(41.9, -87.5));
  await settle();
  env.setVisible(false);
  env.advance(10 * 60_000);
  assert.equal(env.geolocation.requests.length, 1);
  env.setVisible(true);
  env.advance(0);
  assert.equal(env.geolocation.requests.length, 2, "overdue: polls straight away");
});

test("the boat's own GPS and rough fixes are not sent, but polling goes on", async () => {
  const env = start();
  env.serverPos = { source: "gps", age_s: 1 };
  G.setGpsEnabled(true);
  env.advance(0);
  env.last.ok(fix(41.9, -87.5));
  await settle();
  assert.equal(G.gpsState().status, "boat-gps");
  env.serverPos = null;
  env.advance(60_000);
  env.last.ok(fix(41.9, -87.5, 40_000));
  await settle();
  assert.equal(G.gpsState().status, "rough");
  assert.equal(env.sent.length, 0);
  env.advance(60_000);
  assert.equal(env.geolocation.requests.length, 3);
});

test("blocked location stops polling until switched off and on", async () => {
  const env = start();
  G.setGpsEnabled(true);
  env.advance(0);
  env.last.err({ code: 1, message: "User denied Geolocation" });
  assert.equal(G.gpsState().status, "denied");
  env.advance(60 * 60_000);
  assert.equal(env.geolocation.requests.length, 1);
  G.setGpsEnabled(false);
  G.setGpsEnabled(true);
  env.advance(0);
  assert.equal(env.geolocation.requests.length, 2);
});

test("no fix and an unreachable server: keeps trying", async () => {
  const env = start();
  G.setGpsEnabled(true);
  env.advance(0);
  env.last.err({ code: 3, message: "Timeout expired" });
  assert.equal(G.gpsState().status, "nofix");
  env.advance(60_000);
  env.sendFails = true;
  env.last.ok(fix(41.9, -87.5));
  await settle();
  assert.equal(G.gpsState().status, "send-failed");
  assert.match(G.gpsState().error, /reach/);
  env.advance(60_000);
  assert.equal(env.geolocation.requests.length, 3);
});

test("a browser that never answers doesn't stop polling", () => {
  const env = start();
  G.setGpsRate(600);
  G.setGpsEnabled(true);
  env.advance(0);
  env.advance(G.fixOptions(600).timeout + 5_000);
  assert.equal(G.gpsState().status, "nofix");
  env.advance(600_000);
  assert.equal(env.geolocation.requests.length, 2);
  assert.equal(G.gpsState().status, "nofix", "stays until there's news");
});

test("answers that arrive after switching off are ignored", async () => {
  const env = start();
  G.setGpsEnabled(true);
  env.advance(0);
  const pending = env.last;
  G.setGpsEnabled(false);
  pending.ok(fix(41.9, -87.5));
  await settle();
  assert.equal(env.sent.length, 0);
  assert.equal(G.gpsState().status, "off");
  env.advance(10 * 60_000);
  assert.equal(env.geolocation.requests.length, 1);
});

test("nothing is asked for on a plain-HTTP page", () => {
  const env = start({ secure: false });
  G.setGpsEnabled(true);
  env.advance(60_000);
  assert.equal(env.geolocation.requests.length, 0);
  assert.equal(G.gpsState().status, "insecure");
});

test("status in words", async () => {
  const fmt = { accuracy: (m) => `${m} m`, time: () => "12:04:31" };
  const env = start();
  const seen = [];
  G.onGpsChange((st) => seen.push(st.status));
  G.setGpsEnabled(true);
  env.advance(0);
  env.last.ok(fix(41.9, -87.5, 8));
  await settle();
  assert.deepEqual(seen.slice(-2), ["waiting", "ok"]);
  assert.equal(G.gpsStatusText(G.gpsState(), fmt), "Sent to the weather server at 12:04:31 (±8 m). Next update in about 1 minute.");
  assert.match(G.gpsStatusText({ ...G.gpsState(), support: "insecure" }, fmt), /secure connection/);
  assert.match(G.gpsStatusText({ ...G.gpsState(), status: "boat-gps" }, fmt), /boat's own GPS.*every minute/);
});

test("an upload still on its way when switched off changes nothing", async () => {
  const env = start();
  let release;
  env.send = (f) => new Promise((resolve) => { release = () => resolve({ source: "phone", lat: f.lat, lon: f.lon }); });
  let sentCalls = 0;
  env.onSent = () => sentCalls++;
  G.initGps(env);
  G.setGpsEnabled(true);
  env.advance(0);
  env.last.ok(fix(41.9, -87.5));
  await settle();
  G.setGpsEnabled(false);
  release();
  await settle();
  assert.equal(G.gpsState().status, "off");
  assert.equal(sentCalls, 0);
});

test("a slow upload doesn't trip the watchdog", async () => {
  const env = start();
  let release;
  env.send = () => new Promise((resolve) => { release = resolve; });
  G.initGps(env);
  G.setGpsRate(10);
  G.setGpsEnabled(true);
  env.advance(0);
  env.advance(14_000);
  env.last.ok(fix(41.9, -87.5));          // the fix comes just before the GPS time limit
  await settle();
  env.advance(10_000);                     // the upload takes a while
  assert.equal(env.geolocation.requests.length, 1, "no second poll while the first is uploading");
  assert.notEqual(G.gpsState().status, "nofix");
  release({ source: "phone" });
  await settle();
  assert.equal(G.gpsState().status, "ok");
  env.advance(0);
  assert.equal(env.geolocation.requests.length, 2, "overdue: next poll at once");
});

test("a phone clock set back doesn't stall polling", async () => {
  const env = start();
  G.setGpsEnabled(true);
  env.advance(0);
  env.last.ok(fix(41.9, -87.5));
  await settle();
  const now = env.now;
  env.now = () => now() - 2 * 3600_000;   // network time corrects the clock by two hours
  G.initGps(env);                          // reschedules from the new clock
  env.advance(60_000);
  assert.equal(env.geolocation.requests.length, 2);
});
