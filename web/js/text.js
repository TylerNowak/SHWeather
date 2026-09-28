// Human wording for structured server messages, in the user's chosen units.
// The server also sends ready-made English strings in canonical units; those are the
// fallback for anything this file doesn't know.

import * as U from "./units.js";

// Every limit a number sits next to, so rounding never makes it look equal to one.
const lims = (d) => (Array.isArray(d.limits_kn) ? d.limits_kn : [d.limit_kn]).filter((x) => x != null);
const sp = (v, d) => U.fmtNear("speed", v, ...lims(d));
const ht = (v, ...l) => U.fmtNear("height", v, ...l);

const REASONS = {
  wind_over_limit: (d) => `Wind ${sp(d.wind_kn, d)} is above your ${U.fmtLimit("speed", d.limit_kn)} limit`,
  second_reef: (d) => `Wind ${sp(d.wind_kn, d)}: second reef`,
  first_reef: (d) => `Wind ${sp(d.wind_kn, d)}: first reef`,
  light_air: () => "Light air: expect to motor",
  gust_over_limit: (d) => `Gusts ${sp(d.gust_kn, d)} are above your ${U.fmtLimit("speed", d.limit_kn)} limit`,
  gust_near_limit: (d) => `Gusts ${sp(d.gust_kn, d)} near your ${U.fmtLimit("speed", d.limit_kn)} limit`,
  gusty_reef_early: (d) => `Gusty (${sp(d.gust_kn, d)}): consider reefing early`,
  waves_over_limit: (d) => `Waves ${ht(d.wave_m, d.limit_m)} exceed your ${U.fmtLimit("height", d.limit_m)} limit`,
  waves_near_limit: (d) => `Waves ${ht(d.wave_m, d.limit_m)} near your limit`,
  steep_seas: (d) => `Short, steep seas (${ht(d.wave_m)} @ ${Math.round(d.period_s)} s)`,
  poor_visibility: (d) => `Poor visibility (${U.fmtVisibility(d.visibility_m)} ${U.visibilityUnit()})`,
  thunderstorms: () => "Thunderstorms forecast",
  unstable_air: () => "Unstable air: squalls and thunderstorms possible",
  no_wind: () => "No wind data",
};

/** Reasons of an assessment ({level, reasons, details}) worded in display units, worst first. */
export function reasons(assessment) {
  if (!assessment) return [];
  if (Array.isArray(assessment.details) && assessment.details.length) {
    return assessment.details.map((d, i) => (REASONS[d.code] ? REASONS[d.code](d) : assessment.reasons?.[i] ?? ""));
  }
  return assessment.reasons || [];
}

export const firstReason = (assessment) => reasons(assessment)[0] || "";

/** A barometer warning ({code, text, pressure_hpa}) in display units. */
export function warning(w) {
  if (w?.code === "deep_low" && w.pressure_hpa != null) {
    return `Deep low (${U.fmtPressure(w.pressure_hpa)} ${U.pressureUnit()}) and still falling.`;
  }
  return w?.text || "";
}
