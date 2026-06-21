// Cliente API tipado (fetch same-origin). Sin CORS: las rutas son relativas a
// /api del MISMO origen que sirve la SPA. El gate opcional de token de escritura
// se envía por la cabecera X-API-Token cuando está presente en localStorage.

import type {
  Camera,
  Counters,
  CrossingEvent,
  DeviceInfo,
  Health,
  LineConfig,
  LineConfigUpdate,
} from "./types";

/** Error de concurrencia optimista en PUT config (HTTP 409). */
export class ConfigConflictError extends Error {
  readonly expected: number;
  readonly current: number;
  constructor(expected: number, current: number) {
    super(`config_version desactualizado: esperado=${expected}, actual=${current}`);
    this.name = "ConfigConflictError";
    this.expected = expected;
    this.current = current;
  }
}

/** Error genérico de API con el status HTTP. */
export class ApiError extends Error {
  readonly status: number;
  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

const TOKEN_KEY = "camcounter_api_token";

function writeHeaders(): HeadersInit {
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  const token = localStorage.getItem(TOKEN_KEY);
  if (token) headers["X-API-Token"] = token;
  return headers;
}

async function getJson<T>(path: string): Promise<T> {
  const resp = await fetch(path, { headers: { Accept: "application/json" } });
  if (!resp.ok) throw new ApiError(resp.status, `GET ${path} -> ${resp.status}`);
  return (await resp.json()) as T;
}

export const api = {
  device: (): Promise<DeviceInfo> => getJson<DeviceInfo>("/api/device"),

  health: (): Promise<Health> => getJson<Health>("/api/health"),

  cameras: (): Promise<Camera[]> => getJson<Camera[]>("/api/cameras"),

  camera: (id: string): Promise<Camera> =>
    getJson<Camera>(`/api/cameras/${encodeURIComponent(id)}`),

  getConfig: (id: string): Promise<LineConfig> =>
    getJson<LineConfig>(`/api/cameras/${encodeURIComponent(id)}/config`),

  putConfig: async (id: string, update: LineConfigUpdate): Promise<LineConfig> => {
    const resp = await fetch(`/api/cameras/${encodeURIComponent(id)}/config`, {
      method: "PUT",
      headers: writeHeaders(),
      body: JSON.stringify(update),
    });
    if (resp.status === 409) {
      const body = (await resp.json()) as {
        detail?: { expected?: number; current?: number };
      };
      const expected = body.detail?.expected ?? update.expected_config_version;
      const current = body.detail?.current ?? update.expected_config_version;
      throw new ConfigConflictError(expected, current);
    }
    if (!resp.ok) throw new ApiError(resp.status, `PUT config -> ${resp.status}`);
    return (await resp.json()) as LineConfig;
  },

  counters: (id: string): Promise<Counters> =>
    getJson<Counters>(`/api/cameras/${encodeURIComponent(id)}/counters`),

  resetCounters: async (id: string): Promise<Counters> => {
    const resp = await fetch(
      `/api/cameras/${encodeURIComponent(id)}/counters/reset`,
      { method: "POST", headers: writeHeaders() },
    );
    if (!resp.ok) throw new ApiError(resp.status, `reset -> ${resp.status}`);
    return (await resp.json()) as Counters;
  },

  events: (id: string, limit = 50, offset = 0): Promise<CrossingEvent[]> =>
    getJson<CrossingEvent[]>(
      `/api/cameras/${encodeURIComponent(id)}/events?limit=${limit}&offset=${offset}`,
    ),

  /** URL del stream MJPEG (primitivo de vídeo en vivo) para un `<img>`. */
  streamUrl: (id: string): string =>
    `/api/cameras/${encodeURIComponent(id)}/stream.mjpg`,
};

/** URL absoluta del WebSocket del hub, same-origin (ws:// o wss://). */
export function wsUrl(): string {
  const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${proto}//${window.location.host}/api/ws`;
}
