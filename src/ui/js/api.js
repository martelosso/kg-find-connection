// HTTP + Server-Sent Events client for the plan §7 API.

import { EVENT_TYPES, TERMINAL } from "./investigation.js";

export class ApiError extends Error {
  constructor(status, code, message) { super(message); this.status = status; this.code = code; }
}

async function request(method, url, body) {
  let res;
  try {
    res = await fetch(url, {
      method,
      headers: body !== undefined ? { "Content-Type": "application/json" } : undefined,
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
  } catch (err) {
    throw new ApiError(0, "offline", "The workbench server is unreachable.");
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new ApiError(res.status, data.error?.code || "http_error", data.error?.message || `HTTP ${res.status}`);
  return data;
}

export const api = {
  health: () => request("GET", "/api/health"),
  ontology: () => request("GET", "/api/people-ontology"),
  demoCases: () => request("GET", "/api/demo-cases"),
  searchPeople: (q, limit = 8) => request("GET", `/api/people/search?q=${encodeURIComponent(q)}&limit=${limit}`),
  start: (body) => request("POST", "/api/investigations", body),
  get: (id) => request("GET", `/api/investigations/${id}`),
  cancel: (id) => request("POST", `/api/investigations/${id}/cancel`),
  turtle: (id, edgeIds) => request("POST", `/api/investigations/${id}/turtle`, { edge_ids: edgeIds ?? null }),
  overlay: (id) => request("GET", `/api/investigations/${id}/overlay`),
  setOverlay: (id, text, shownEdgeIds) => request("POST", `/api/investigations/${id}/overlay`, { text, shown_edge_ids: shownEdgeIds ?? null }),
  clearOverlay: (id) => request("DELETE", `/api/investigations/${id}/overlay`),
  recordSuggestion: (id) => request("GET", `/api/investigations/${id}/record`),
  record: (id, caseId, title) => request("POST", `/api/investigations/${id}/record`, { case_id: caseId, title }),
};

/**
 * Stream events for an investigation. EventSource resumes with Last-Event-ID after network blips;
 * we close it ourselves after the terminal event so it does not reconnect to a finished stream.
 */
export function streamEvents(id, onEvent, onError) {
  const es = new EventSource(`/api/investigations/${id}/events`);
  const handler = (msg) => {
    const ev = JSON.parse(msg.data);
    onEvent(ev);
    if (TERMINAL.has(ev.type)) es.close();
  };
  for (const t of EVENT_TYPES) es.addEventListener(t, handler);
  es.onerror = () => { if (es.readyState === EventSource.CLOSED) onError?.(); };
  return es;
}
