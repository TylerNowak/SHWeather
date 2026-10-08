// API client. Every call has a timeout: a phone on the edge of the boat's Wi-Fi must
// fall back to cached data quickly instead of hanging.

const TIMEOUT_MS = 8000;
const REFRESH_TIMEOUT_MS = 10 * 60 * 1000; // passage downloads over a slow link take a while

function token() {
  try { return JSON.parse(localStorage.getItem("shw.token") || "null"); } catch { return null; }
}

export class ApiError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
}

async function request(method, path, body, timeoutMs = TIMEOUT_MS) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  const headers = { "X-SHW-Client": "pwa" };
  if (body) headers["Content-Type"] = "application/json";
  const t = token();
  if (t) headers["X-SHW-Token"] = t;
  try {
    const res = await fetch(path, {
      method,
      signal: ctrl.signal,
      headers,
      body: body ? JSON.stringify(body) : undefined,
      cache: "no-store",
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      const detail = typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail ?? data);
      throw new ApiError(res.status, detail || res.statusText);
    }
    // The service worker marks responses it served from its cache.
    if (res.headers.get("X-SHW-Cached")) data.__cached = true;
    return data;
  } catch (err) {
    if (err instanceof ApiError) throw err;
    throw new ApiError(0, err.name === "AbortError" ? "The weather server did not answer in time" : "Cannot reach the weather server");
  } finally {
    clearTimeout(timer);
  }
}

/**
 * A map tile as something drawImage accepts. Tiles are fetched with the app's headers so
 * the server may download ones it hasn't cached yet; its error text explains refusals
 * (data cap reached, no internet).
 */
export async function fetchImage(path, timeoutMs = 20000) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  const headers = { "X-SHW-Client": "pwa" };
  const t = token();
  if (t) headers["X-SHW-Token"] = t;
  let res;
  try {
    res = await fetch(path, { headers, signal: ctrl.signal });
  } catch (err) {
    throw new ApiError(0, err.name === "AbortError" ? "The weather server did not answer in time" : "Cannot reach the weather server");
  } finally {
    clearTimeout(timer);
  }
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new ApiError(res.status, typeof data.detail === "string" ? data.detail : res.statusText);
  }
  const blob = await res.blob();
  if (typeof createImageBitmap === "function") {
    try { return await createImageBitmap(blob); } catch { /* fall back below */ }
  }
  const url = URL.createObjectURL(blob);
  try {
    const img = new Image();
    img.src = url;
    await img.decode();
    return img;
  } finally {
    URL.revokeObjectURL(url);
  }
}

export const api = {
  status: () => request("GET", "api/status"),
  now: () => request("GET", "api/now"),
  forecast: (hours = 96, pastHours = 6) => request("GET", `api/forecast?hours=${hours}&past_hours=${pastHours}`),
  observations: (metrics, hours) => request("GET", `api/observations?metrics=${encodeURIComponent(metrics)}&hours=${hours}`),
  tides: () => request("GET", "api/tides"),
  buoys: () => request("GET", "api/buoys"),
  marineText: () => request("GET", "api/marine-text"),
  map: (pastHours = 3, hours = 48) => request("GET", `api/map?past_hours=${pastHours}&hours=${hours}`),
  imagery: () => request("GET", "api/imagery", null, 30000),
  boat: () => request("GET", "api/boat"),
  saveBoat: (b) => request("PUT", "api/boat", b),
  bandwidth: () => request("GET", "api/bandwidth"),
  saveBandwidth: (b) => request("PUT", "api/bandwidth", b),
  // source "phone": a fix from this device's GPS (sent repeatedly); "manual": typed in
  setPosition: (lat, lon, { source = "manual", accuracy = null } = {}) =>
    request("POST", "api/position", { lat, lon, source, accuracy_m: accuracy }),
  refresh: (radiusNm) => request("POST", radiusNm ? `api/refresh?radius_nm=${radiusNm}` : "api/refresh", null, REFRESH_TIMEOUT_MS),
};
