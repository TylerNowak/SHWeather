// Settings dialog: units and theme (stored on this device), position and this device's GPS,
// the secure (HTTPS) address, the boat's limits and the bandwidth limits (stored on the
// weather server, shared by every device aboard).

import { api } from "./api.js";
import { $, ago, clear, h, store } from "./dom.js";
import * as G from "./gps.js";
import * as U from "./units.js";

const THEMES = [["auto", "Automatic"], ["light", "Day (bright sun)"], ["dark", "Dark (cabin)"], ["night", "Night (red, keeps night vision)"]];

function select(name, label, options, value) {
  return h("label", { class: "field" }, label,
    h("select", { name }, options.map(([v, text]) => h("option", { value: v, selected: v === value, text }))));
}

function input(name, label, value, attrs = {}) {
  return h("label", { class: "field" }, label,
    h("input", { name, value: value ?? "", inputmode: "decimal", type: "number", step: "any", ...attrs }));
}

const numOrNull = (v) => (v === "" || v === null || v === undefined ? null : Number(v));

function msg(form, text) {
  const el = form.querySelector(".form-msg");
  if (el) el.textContent = text;
}

function unitsSection(onChange) {
  const { system, nautical } = U.getSystem();
  const u = U.getUnits();
  // Rebuild the whole dialog: every form below shows values in these units.
  const rerender = () => { onChange(); openSettings(onChange); };
  const sysButton = (value, label) => h("button", { type: "button", "aria-pressed": String(system === value), text: label,
    onclick: () => { U.setSystem(value, nautical); rerender(); } });
  const form = h("form", {},
    h("fieldset", {}, h("legend", { text: "Units (this device)" }),
      h("div", { class: "seg units-seg", role: "group", "aria-label": "Unit system" },
        sysButton("metric", "Metric"), sysButton("imperial", "Imperial")),
      h("label", { class: "check" },
        h("input", { type: "checkbox", name: "nautical", checked: nautical,
          onchange: (e) => { U.setSystem(system === "custom" ? "metric" : system, e.target.checked); rerender(); } }),
        "Nautical: knots and nautical miles for wind, current and distance"),
      h("p", { class: "help", text: system === "custom"
        ? "Custom mix of units. Pick Metric or Imperial to reset them all."
        : (U.isDeviceChoice() ? "Chosen on this device." : "Following the server's default for this boat.") }),
      h("details", { class: "unit-details", open: system === "custom" },
        h("summary", { text: "Individual units" }),
        h("div", { class: "field-row" },
          U.QUANTITIES.map((q) => {
            const el = select(q, U.LABELS[q], U.OPTIONS[q], u[q]);
            el.querySelector("select").addEventListener("change", (e) => { U.setUnit(q, e.target.value); rerender(); });
            return el;
          })))));
  form.addEventListener("submit", (e) => e.preventDefault());
  return form;
}

function displaySection() {
  let theme = "auto";
  try { theme = localStorage.getItem("shw.theme") || "auto"; } catch { /* ignore */ }
  const form = h("form", {},
    h("fieldset", {}, h("legend", { text: "Display (this device)" }),
      select("theme", "Theme", THEMES, theme),
      h("label", { class: "field" }, "Access token (only if one is set on the server)",
        h("input", { name: "token", type: "password", autocomplete: "off", value: store.get("shw.token", "") || "",
          onchange: (e) => store.set("shw.token", e.target.value || null) }))));
  form.querySelector("select[name=theme]").addEventListener("change", (e) => setTheme(e.target.value));
  form.addEventListener("submit", (e) => e.preventDefault());
  return form;
}

export function setTheme(t) {
  if (t === "auto") delete document.documentElement.dataset.theme;
  else document.documentElement.dataset.theme = t;
  try { localStorage.setItem("shw.theme", t); } catch { /* ignore */ }
  const btn = $("#theme-btn");
  if (btn) btn.title = `Theme: ${t}`;
}

const fmtClock = new Intl.DateTimeFormat(undefined, { hour: "2-digit", minute: "2-digit", second: "2-digit" });
export const gpsFormat = { accuracy: U.fmtAccuracy, time: (ms) => fmtClock.format(new Date(ms)) };

const SOURCE_TEXT = {
  gps: "from the boat's GPS", phone: "from a phone's GPS", manual: "typed in by hand",
  home: "the home position in config.yaml", cli: "set on the command line",
};

/** "Now using 41.9000, -87.5000, from a phone's GPS (±8 m), 2 min ago." */
export function positionText(pos) {
  if (!pos) return "No position yet.";
  const acc = pos.accuracy_m != null ? ` (±${U.fmtAccuracy(pos.accuracy_m)})` : "";
  const age = pos.age_s != null && pos.age_s >= 90 ? `, ${ago(pos.age_s)}` : "";
  return `Now using ${pos.lat.toFixed(4)}, ${pos.lon.toFixed(4)}, ${SOURCE_TEXT[pos.source] || pos.source}${acc}${age}.`;
}

/** The same page on the server's HTTPS port, if the server offers one. */
export function secureUrl(status, loc = location) {
  const https = status?.https;
  if (!https?.running || !https.port) return null;
  return `https://${loc.hostname}:${https.port}${loc.pathname}${loc.hash}`;
}

// Keep the status line in an open Settings dialog current as fixes come and go.
G.onGpsChange((st) => {
  const el = document.getElementById("gps-status");
  if (el) el.textContent = G.gpsStatusText(st, gpsFormat);
});

function gpsControls(status) {
  const st = G.gpsState();
  const usable = st.support === "ok";
  const rate = select("gps_rate", "Send its position every", G.RATES.map((r) => [String(r), G.rateLabel(r)]), String(st.rate));
  const rateSelect = rate.querySelector("select");
  rateSelect.disabled = !usable || !st.enabled;
  rateSelect.addEventListener("change", (e) => G.setGpsRate(Number(e.target.value)));
  const toggle = h("input", { type: "checkbox", name: "gps", checked: usable && st.enabled, disabled: !usable,
    onchange: (e) => { G.setGpsEnabled(e.target.checked); rateSelect.disabled = !e.target.checked; } });
  const url = secureUrl(status);
  return h("div", { class: "gps-box" },
    h("label", { class: "check" }, toggle, "Use this device's location"),
    rate,
    h("p", { class: "help gps-status", id: "gps-status", role: "status", "aria-live": "polite", text: G.gpsStatusText(st, gpsFormat) }),
    st.support === "insecure"
      ? h("p", { class: "help" }, "This page was opened over plain HTTP. ",
        url ? ["Open the secure address instead: ", h("a", { href: url, text: url }), " (see Secure connection below)."]
          : "See Secure connection below.")
      : usable ? h("p", { class: "help", text: "Works while SHWeather is open on screen; browsers don't share location from the background. A GPS on the boat's network always comes first." })
        : null);
}

function positionSection(status, onChange) {
  const pos = status?.position;
  const form = h("form", {},
    h("fieldset", {}, h("legend", { text: "Position" }),
      h("p", { class: "help", text: positionText(pos) }),
      gpsControls(status),
      h("div", { class: "sub-legend", text: "Or set it by hand" }),
      h("div", { class: "field-row" },
        input("lat", "Latitude (decimal, N positive)", pos?.lat?.toFixed(4), { min: -90, max: 90 }),
        input("lon", "Longitude (decimal, E positive)", pos?.lon?.toFixed(4), { min: -180, max: 180 })),
      h("div", { class: "btn-row" }, h("button", { class: "btn primary", type: "submit", text: "Set position" })),
      h("div", { class: "form-msg", role: "status" })));
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = new FormData(form);
    // This device's location goes off first: a fix sent meanwhile would replace what was typed in.
    const wasOn = G.gpsState().enabled;
    if (wasOn) {
      G.setGpsEnabled(false);
      form.querySelector("input[name=gps]").checked = false;
      form.querySelector("select[name=gps_rate]").disabled = true;
    }
    try {
      await api.setPosition(Number(f.get("lat")), Number(f.get("lon")), { source: "manual" });
      let note = "Saved. The forecast follows when the server is online.";
      if (wasOn) note += " This device's location is now off, so it won't overwrite it.";
      msg(form, note);
      onChange();
    } catch (err) { msg(form, err.message); }
  });
  return form;
}

function secureSection(status) {
  const https = status?.https || {};
  const secure = location.protocol === "https:";
  const url = secureUrl(status);
  const parts = [];
  if (secure) {
    parts.push(h("p", { class: "help", text: "This page uses a secure connection: this device's GPS and the app's offline copy are available." }));
  } else if (window.isSecureContext) {
    parts.push(h("p", { class: "help", text: "Opened on the weather server itself (localhost), which browsers treat as secure." }));
  } else {
    parts.push(h("p", { class: "help", text: "This page uses plain HTTP, so the browser blocks this device's GPS and won't keep an offline copy of the app." }));
    if (url) {
      parts.push(h("div", { class: "btn-row" }, h("a", { class: "btn primary", href: url, text: "Open the secure address" })),
        h("p", { class: "help", text: "Browsers keep this device's settings (units, theme, token) per address, so set them "
          + "again there. If SHWeather is on your home screen, add it again from the secure address." }));
    } else {
      parts.push(h("p", { class: "help", text: https.enabled === false ? "HTTPS is off on the server (https_port: 0 in config.yaml)."
        : https.error ? `HTTPS isn't running on the server: ${https.error}` : "HTTPS is still starting on the server…" }));
    }
  }
  if (https.own_certificate && !(secure && window.isSecureContext && navigator.serviceWorker?.controller)) {
    parts.push(
      h("p", { class: "help", text: "The weather server made its certificate itself (it needs no internet), so the first visit shows a "
        + "warning such as \"Your connection is not private\". Continue anyway (Advanced, then Proceed; on an iPhone, Show Details, "
        + "then visit this website), or install the server's certificate on this device once: no more warnings, and the app "
        + "keeps an offline copy." }),
      h("div", { class: "btn-row" }, h("a", { class: "btn", href: "api/tls/ca.crt", text: "Download the certificate" })),
      h("details", { class: "unit-details" }, h("summary", { text: "How to install it" }),
        h("ul", { class: "help steps" },
          h("li", { text: "iPhone, iPad: download it in Safari and tap Allow. Open Settings, tap Profile Downloaded, then Install. "
            + "Then Settings > General > About > Certificate Trust Settings: switch on full trust for \"SHWeather CA\"." }),
          h("li", { text: "Android: download it, then open Settings and search for \"CA certificate\" (Security > Encryption & "
            + "credentials > Install a certificate > CA certificate) and pick SHWeather-CA.crt." }),
          h("li", {}, "Windows: open the downloaded file > Install Certificate > Local Machine > Trusted Root Certification "
            + "Authorities. Mac: open it in Keychain Access and set it to Always Trust. Firefox keeps its own list "
            + "(Settings > Certificates > Import; ", h("a", { href: "api/tls/ca.crt?format=pem", text: "PEM file" }), ")."),
          h("li", { text: "It can only vouch for private network addresses and local names (.local, .lan), "
            + "never for websites on the internet." })),
        https.ca_fingerprint ? h("p", { class: "help" }, "SHA-256 fingerprint: ", h("span", { class: "mono fp", text: https.ca_fingerprint })) : null));
  }
  if (!parts.length) return null;
  return h("form", {}, h("fieldset", {}, h("legend", { text: "Secure connection (HTTPS)" }), ...parts));
}

// Boat limit fields: [field, quantity, label, canonical -> quantity's canonical, and back]
const BOAT_FIELDS = [
  ["reef1_kn", "speed", "First reef at"],
  ["reef2_kn", "speed", "Second reef at"],
  ["max_wind_kn", "speed", "Max sustained wind"],
  ["max_gust_kn", "speed", "Max gust"],
  ["max_wave_m", "height", "Max wave height"],
  ["min_visibility_nm", "visibility", "Min visibility", (nm) => nm * 1852, (m) => m / 1852],
];

function boatSection(boat, onChange) {
  const rendered = U.getUnits(); // convert back with these, whatever happens meanwhile
  const shown = {};
  const round1 = (v) => Math.round(v * 10) / 10;
  const fields = BOAT_FIELDS.map(([field, q, label, toQ = (x) => x]) => {
    shown[field] = String(round1(U.toDisplay(q, toQ(boat[field]), rendered)));
    return input(field, `${label} (${U.unit(q, rendered)})`, shown[field], { min: 0 });
  });
  const form = h("form", {},
    h("fieldset", {}, h("legend", { text: "Boat limits (shared by all devices)" }),
      h("label", { class: "field" }, "Boat name", h("input", { name: "name", value: boat.name, maxlength: 60 })),
      h("div", { class: "field-row" }, fields),
      h("div", { class: "btn-row" }, h("button", { class: "btn primary", type: "submit", text: "Save limits" })),
      h("div", { class: "form-msg", role: "status" })));
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = new FormData(form);
    const next = { ...boat, name: f.get("name") };
    for (const [field, q, , , fromQ = (x) => x] of BOAT_FIELDS) {
      const typed = String(f.get(field)).trim();
      // Untouched fields keep their exact stored value (no drift from display rounding).
      if (typed === shown[field]) continue;
      const value = U.toCanonical(q, typed, rendered);
      if (value === null || value < 0) { msg(form, "Please enter positive numbers."); return; }
      next[field] = fromQ(value);
    }
    try { await api.saveBoat(next); msg(form, "Saved."); onChange(); } catch (err) { msg(form, err.message); }
  });
  return form;
}

function meterRow(label, u) {
  const pct = u.pct == null ? null : Math.min(100, u.pct);
  const cls = pct == null ? "" : pct >= 100 ? " full" : pct >= 80 ? " warn" : "";
  return h("div", { class: "usage" },
    h("div", { class: "usage-row" }, h("span", { text: label }),
      h("span", { text: u.cap_mb == null ? `${u.used_mb.toFixed(1)} MB (no cap)` : `${u.used_mb.toFixed(1)} of ${u.cap_mb} MB` })),
    u.cap_mb == null ? null : h("div", { class: `meter${cls}`, role: "meter", "aria-valuemin": 0, "aria-valuemax": 100,
      "aria-valuenow": Math.round(pct), "aria-label": `${label} data used` }, h("span", { style: { width: `${pct}%` } })));
}

function bandwidthSection(bw, onChange) {
  const cfg = bw.settings;
  const form = h("form", {},
    h("fieldset", {}, h("legend", { text: "Bandwidth (weather downloads)" }),
      h("p", { class: "help", text: "Limits apply to everything the server downloads. Leave a box empty for no limit." }),
      meterRow("Today (UTC)", bw.usage.today),
      meterRow("This month", bw.usage.month),
      h("div", { class: "field-row" },
        input("max_kbps", "Speed cap (kbit/s)", cfg.max_kbps, { min: 1, placeholder: "unlimited" }),
        input("alert_reserve_pct", "Reserve for alerts (%)", cfg.alert_reserve_pct, { min: 0, max: 50 }),
        input("daily_mb", "Daily cap (MB)", cfg.daily_mb, { min: 0.1, placeholder: "unlimited" }),
        input("monthly_mb", "Monthly cap (MB)", cfg.monthly_mb, { min: 1, placeholder: "unlimited" })),
      h("label", { class: "check" }, h("input", { type: "checkbox", name: "saver", checked: cfg.saver }),
        "Data saver: smaller area, 3-day forecast, fetch less often"),
      h("p", { class: "help", text: "Once a cap is reached, only safety alerts are fetched until the reserve is used up too. "
        + "The speed cap keeps weather downloads from hogging a slow cellular or satellite link." }),
      h("div", { class: "btn-row" }, h("button", { class: "btn primary", type: "submit", text: "Save bandwidth limits" })),
      h("div", { class: "form-msg", role: "status" })));
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = new FormData(form);
    const next = {
      max_kbps: numOrNull(f.get("max_kbps")),
      daily_mb: numOrNull(f.get("daily_mb")),
      monthly_mb: numOrNull(f.get("monthly_mb")),
      alert_reserve_pct: numOrNull(f.get("alert_reserve_pct")) ?? 10,
      saver: f.get("saver") === "on",
    };
    try {
      await api.saveBandwidth(next);
      const fresh = bandwidthSection(await api.bandwidth(), onChange);
      form.replaceWith(fresh);
      msg(fresh, "Saved on the server.");
      onChange();
    } catch (err) { msg(form, err.message); }
  });
  return form;
}

function downloadSection(status, onChange) {
  const rendered = U.getUnits();
  const fc = status?.forecast;
  const form = h("form", {},
    h("fieldset", {}, h("legend", { text: "Offline forecast" }),
      h("p", { class: "help", text: fc
        ? `Latest ${fc.kind === "passage" ? "passage" : "local"} forecast downloaded ${ago(fc.age_s)}. `
          + "Before a passage, download a wider area while you still have signal."
        : "No forecast downloaded yet." }),
      h("div", { class: "field-row" }, input("radius", `Passage radius (${U.unit("distance", rendered)})`,
        Math.round(U.distance(100) / 10) * 10, { min: Math.ceil(U.distance(10)), max: Math.floor(U.distance(600)) })),
      h("div", { class: "btn-row" },
        h("button", { class: "btn primary", type: "submit", text: "Download passage area" }),
        h("button", { class: "btn", type: "button", text: "Refresh now", onclick: async () => {
          msg(form, "Refreshing…");
          try { const r = await api.refresh(); msg(form, r.forecast?.error ? `Forecast: ${r.forecast.error}` : "Updated."); onChange(); }
          catch (err) { msg(form, err.message); }
        } })),
      h("div", { class: "form-msg", role: "status" })));
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const r = Math.min(600, Math.max(1, U.toCanonical("distance", Number(new FormData(form).get("radius")), rendered)));
    msg(form, "Downloading… this can take a while on a slow link.");
    try {
      const res = await api.refresh(r);
      msg(form, res.forecast?.error ? `Failed: ${res.forecast.error}` : `Downloaded ${res.forecast.points} points at ${res.forecast.spacing_deg.toFixed(2)}° spacing.`);
      onChange();
    } catch (err) { msg(form, err.message); }
  });
  return form;
}

let buildSeq = 0;

export async function openSettings(onChange) {
  const dlg = $("#settings");
  const seq = ++buildSeq;
  const body = clear($("#settings-body"));
  body.append(unitsSection(onChange), displaySection());
  if (!dlg.open) dlg.showModal();
  const [status, boat, bw] = await Promise.allSettled([api.status(), api.boat(), api.bandwidth()]);
  if (seq !== buildSeq) return; // a newer rebuild (e.g. units changed) owns the dialog now
  if (status.status === "fulfilled") {
    body.append(positionSection(status.value, onChange));
    const secure = secureSection(status.value);
    if (secure) body.append(secure);
  }
  if (boat.status === "fulfilled") body.append(boatSection(boat.value, onChange));
  if (bw.status === "fulfilled") body.append(bandwidthSection(bw.value, onChange));
  if (status.status === "fulfilled") body.append(downloadSection(status.value, onChange));
  if ([status, boat, bw].every((r) => r.status === "rejected")) {
    body.append(h("p", { class: "help", text: "The server is unreachable, so only this device's settings can be changed right now." }));
  }
}
