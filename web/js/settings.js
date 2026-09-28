// Settings dialog: units and theme (stored on this device), position, the boat's limits and
// the bandwidth limits (stored on the weather server, shared by every device aboard).

import { api } from "./api.js";
import { $, ago, clear, h, store } from "./dom.js";
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

function positionSection(status, onChange) {
  const pos = status?.position;
  const srcText = pos ? `Current: ${pos.lat.toFixed(4)}, ${pos.lon.toFixed(4)} from ${pos.source}` : "No position yet.";
  const form = h("form", {},
    h("fieldset", {}, h("legend", { text: "Position" }),
      h("p", { class: "help", text: `${srcText}. A GPS on the boat's network always takes priority.` }),
      h("div", { class: "field-row" },
        input("lat", "Latitude (decimal, N positive)", pos?.lat?.toFixed(4), { min: -90, max: 90 }),
        input("lon", "Longitude (decimal, E positive)", pos?.lon?.toFixed(4), { min: -180, max: 180 })),
      h("div", { class: "btn-row" },
        h("button", { class: "btn primary", type: "submit", text: "Set position" }),
        "geolocation" in navigator && window.isSecureContext
          ? h("button", { class: "btn", type: "button", text: "Use this phone's GPS", onclick: () => {
            msg(form, "Getting a fix…");
            navigator.geolocation.getCurrentPosition(async (p) => {
              try { await api.setPosition(p.coords.latitude, p.coords.longitude); msg(form, "Position sent to the server."); onChange(); }
              catch (e) { msg(form, e.message); }
            }, (e) => msg(form, `No fix: ${e.message}`), { enableHighAccuracy: true, timeout: 20000 });
          } })
          : null),
      h("div", { class: "form-msg", role: "status" })));
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = new FormData(form);
    try {
      await api.setPosition(Number(f.get("lat")), Number(f.get("lon")));
      msg(form, "Saved. The forecast grid will follow when the server is online.");
      onChange();
    } catch (err) { msg(form, err.message); }
  });
  return form;
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
  if (status.status === "fulfilled") body.append(positionSection(status.value, onChange));
  if (boat.status === "fulfilled") body.append(boatSection(boat.value, onChange));
  if (bw.status === "fulfilled") body.append(bandwidthSection(bw.value, onChange));
  if (status.status === "fulfilled") body.append(downloadSection(status.value, onChange));
  if ([status, boat, bw].every((r) => r.status === "rejected")) {
    body.append(h("p", { class: "help", text: "The server is unreachable, so only this device's settings can be changed right now." }));
  }
}
