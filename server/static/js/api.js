// HTTP helpers with basic error handling.

/**
 * POST JSON to a URL. Returns { ok: bool, data: object }.
 */
export async function post(url, body) {
  try {
    const res = await fetch(url, {
      method: "POST",
      headers: body !== undefined ? { "Content-Type": "application/json" } : {},
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
    let data;
    try { data = await res.json(); } catch { data = {}; }
    return { ok: res.ok, status: res.status, data };
  } catch (err) {
    return { ok: false, status: 0, data: { detail: String(err) } };
  }
}

/**
 * GET JSON from a URL. Returns the parsed body or null on error.
 */
export async function get(url) {
  try {
    const res = await fetch(url);
    if (!res.ok) return null;
    return await res.json();
  } catch {
    return null;
  }
}
