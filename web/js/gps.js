// This device's location. While the app is open, the phone's GPS is read at the rate chosen
// in Settings and each fix goes to the weather server, which uses it whenever the boat has
// no GPS of its own. Browsers share location only with secure pages (HTTPS or localhost) and
// only while the page is on screen, so polling pauses while the app is hidden and catches
// up as soon as it is back.
//
// The browser parts (geolocation, timers, the page's visibility) can be swapped out, so the
// logic runs under Node's test runner.

import { api } from "./api.js";
import { store } from "./dom.js";

/** Polling rates offered in Settings, in seconds. */
export const RATES = [10, 30, 60, 120, 300, 600, 900, 1800, 3600];
export const DEFAULT_RATE = 60;
/** Fixes rougher than this (a guess from the IP address, say) are never sent. */
export const MAX_ACCURACY_M = 10000;
/** The boat's own GPS counts as working while its fix is younger than this. */
const BOAT_GPS_FRESH_S = 120;

const KEY_ON = "shw.gps";
const KEY_RATE = "shw.gpsRate";

const normRate = (r) => (RATES.includes(Number(r)) ? Number(r) : DEFAULT_RATE);
let prefs = { enabled: store.get(KEY_ON, false) === true, rate: normRate(store.get(KEY_RATE, DEFAULT_RATE)) };

/** "30 seconds", "1 minute", "1 hour" */
export function rateLabel(s) {
  const [n, unit] = s < 60 ? [s, "second"] : s < 3600 ? [s / 60, "minute"] : [s / 3600, "hour"];
  return `${n} ${unit}${n === 1 ? "" : "s"}`;
}

/** "every 30 seconds", "every minute" */
export const everyLabel = (s) => `every ${rateLabel(s).replace(/^1 /, "")}`;

/** "ok", or why this browser can't share its location: "insecure" (plain HTTP) or "unsupported". */
export function support(win = globalThis) {
  if (!win?.navigator?.geolocation) return "unsupported";
  if (!win.isSecureContext) return "insecure";
  return "ok";
}

/** Whether a fix should go to the server: "send", or "rough" / "boat-gps" when it shouldn't. */
export function verdict(fix, serverPos) {
  if (!(fix.accuracy <= MAX_ACCURACY_M)) return "rough";
  if (serverPos?.source === "gps" && serverPos.age_s != null && serverPos.age_s < BOAT_GPS_FRESH_S) return "boat-gps";
  return "send";
}

/** Geolocation options for a polling rate: real GPS, a fresh-enough fix, and a time limit. */
export function fixOptions(rate) {
  return {
    enableHighAccuracy: true,                                  // satellites, not Wi-Fi guesses: there is no Wi-Fi offshore
    timeout: Math.min(60, Math.max(15, rate)) * 1000,          // a cold GPS can take a while
    maximumAge: Math.min(rate / 2, 30) * 1000,
  };
}

// ---------------------------------------------------------------- state

const S = {
  status: "off",   // off | unsupported | insecure | waiting | ok | rough | boat-gps | denied | nofix | send-failed
  fix: null,       // { lat, lon, accuracy, time } of the latest fix
  sentAt: null,    // when a fix last reached the server (ms)
  error: null,
  lastAttempt: 0,
  busy: false,
};
let timer = null;
let attempt = 0;
const listeners = new Set();

function browserEnv() {
  const w = typeof window !== "undefined" ? window : globalThis;
  return {
    win: w,
    doc: typeof document !== "undefined" ? document : null,
    geolocation: w.navigator?.geolocation,
    send: (fix) => api.setPosition(fix.lat, fix.lon, { source: "phone", accuracy: fix.accuracy }),
    serverPosition: () => null,
    onSent: () => {},
    now: () => Date.now(),
    setTimeout: (fn, ms) => setTimeout(fn, ms),
    clearTimeout: (id) => clearTimeout(id),
  };
}
let env = browserEnv();

export function gpsState() {
  return { ...prefs, support: support(env.win), status: S.status, fix: S.fix, sentAt: S.sentAt, error: S.error };
}

export function onGpsChange(fn) {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

function set(patch) {
  Object.assign(S, patch);
  const st = gpsState();
  for (const fn of listeners) {
    try { fn(st); } catch (err) { console.error(err); }
  }
}

/**
 * Start polling (if this device has it switched on). `options` replaces browser parts:
 * serverPosition() returns the position the server reported last, onSent(position) runs
 * after each fix the server accepted.
 */
export function initGps(options = {}) {
  env = { ...browserEnv(), ...options };
  env.doc?.addEventListener?.("visibilitychange", () => (env.doc.hidden ? pause() : schedule()));
  schedule();
}

export function setGpsEnabled(on) {
  prefs.enabled = Boolean(on);
  store.set(KEY_ON, prefs.enabled);
  pause();
  attempt++;            // an answer still on its way belongs to the old setting: ignore it
  S.busy = false;
  S.lastAttempt = 0;
  if (!prefs.enabled) {
    set({ status: "off", fix: null, sentAt: null, error: null });
    return;
  }
  set({ status: "waiting", error: null });   // also clears "denied": the user is trying again
  schedule();
}

export function setGpsRate(rate) {
  prefs.rate = normRate(rate);
  store.set(KEY_RATE, prefs.rate);
  set({});
  schedule();
}

function pause() {
  if (timer !== null) env.clearTimeout(timer);
  timer = null;
}

function schedule() {
  pause();
  const sup = support(env.win);
  if (!prefs.enabled) { if (S.status !== "off") set({ status: "off" }); return; }
  if (sup !== "ok") { set({ status: sup }); return; }
  if (S.status === "denied" || S.busy || env.doc?.hidden) return;
  // Clamped: a phone clock set back (network time after a passage) must not stall polling.
  const wait = Math.min(prefs.rate * 1000, Math.max(0, S.lastAttempt + prefs.rate * 1000 - env.now()));
  timer = env.setTimeout(poll, wait);
}

function poll() {
  timer = null;
  if (S.busy || !prefs.enabled) return;
  const opts = fixOptions(prefs.rate);
  const mine = ++attempt;
  S.busy = true;
  S.lastAttempt = env.now();
  if (S.status === "off") set({ status: "waiting" });   // first poll since the page opened
  // Still this poll's business? Not once switched off or on again, or once the watchdog gave up.
  const current = () => mine === attempt && S.busy;
  const finish = () => {
    if (!current()) return;
    S.busy = false;
    schedule();
  };
  // Some browsers never answer when the permission prompt is dismissed: don't wait forever.
  const watchdog = env.setTimeout(() => {
    if (!current()) return;
    set({ status: "nofix", error: "the browser did not answer" });
    finish();
  }, opts.timeout + 5000);
  const answered = () => { env.clearTimeout(watchdog); return current(); };   // it times the GPS, not the upload
  try {
    env.geolocation.getCurrentPosition(
      (p) => { if (answered()) onFix(p, mine).finally(finish); },
      (err) => { if (answered()) { onError(err); finish(); } },
      opts);
  } catch (err) {
    env.clearTimeout(watchdog);
    onError({ code: 2, message: err.message });
    finish();
  }
}

async function onFix(p, mine) {
  const c = p.coords;
  const fix = { lat: c.latitude, lon: c.longitude, accuracy: c.accuracy, time: p.timestamp || env.now() };
  S.fix = fix;
  const v = verdict(fix, env.serverPosition());
  if (v !== "send") { set({ status: v, error: null }); return; }
  const stale = () => mine !== attempt || !prefs.enabled;   // switched off while it was on its way
  try {
    const pos = await env.send(fix);
    if (stale()) return;
    set({ status: "ok", sentAt: env.now(), error: null });
    env.onSent(pos);
  } catch (err) {
    if (stale()) return;
    set({ status: "send-failed", error: err.message });
  }
}

function onError(err) {
  // GeolocationPositionError codes: 1 permission denied, 2 position unavailable, 3 timeout
  set({ status: err?.code === 1 ? "denied" : "nofix", error: err?.message || null });
}

// ---------------------------------------------------------------- words

/**
 * The state in a sentence for Settings and the banner. `fmt` formats an accuracy radius
 * (metres) and a time (ms) the way the app shows them.
 */
export function gpsStatusText(st, fmt) {
  const every = everyLabel(st.rate);
  const acc = st.fix ? `±${fmt.accuracy(st.fix.accuracy)}` : "";
  const status = st.support === "ok" ? st.status : st.support;
  switch (status) {
    case "off": return "Off: this device's location isn't used.";
    case "unsupported": return "This browser can't share its location.";
    case "insecure": return "Browsers share location only with pages opened over a secure connection (HTTPS).";
    case "waiting": return "Waiting for a GPS fix… If the browser asks, allow location.";
    case "ok": return `Sent to the weather server at ${fmt.time(st.sentAt)} (${acc}). Next update in about ${rateLabel(st.rate)}.`;
    case "rough": return `This device only knows roughly where it is (${acc}), so nothing was sent. Turn on precise location / GPS for the browser. Trying again ${every}.`;
    case "boat-gps": return `The boat's own GPS is in use, so this device's fixes aren't needed (checking ${every}).`;
    case "denied": return "Location is blocked for this page. Allow it in the browser's site settings (and the phone's privacy settings for the browser), then switch this off and on again.";
    case "nofix": return `No GPS fix right now${st.error ? ` (${st.error})` : ""}. Trying again ${every}; below deck a phone may not see the satellites.`;
    case "send-failed": return `Got a fix (${acc}) but couldn't reach the weather server${st.error ? ` (${st.error})` : ""}. Trying again ${every}.`;
    default: return "";
  }
}

/** For tests: forget everything. */
export function _reset() {
  pause();
  Object.assign(S, { status: "off", fix: null, sentAt: null, error: null, lastAttempt: 0, busy: false });
  prefs = { enabled: false, rate: DEFAULT_RATE };
  listeners.clear();
  attempt++;
}
