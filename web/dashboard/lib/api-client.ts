/**
 * Cliente de la API de flota de WP11 (HTTP API detrás del authorizer JWT Cognito).
 *
 * READ-ONLY: la SPA NUNCA habla DynamoDB/S3 directo (CLAUDE.md §2). Cada petición lleva
 * `Authorization: Bearer <idToken>` (idToken de Cognito). Las funciones reciben el token
 * explícitamente para servir TANTO a Server Components (token de `getServerIdToken()`) COMO a
 * Client Components (token de `fetchAuthSession()` del navegador) con el mismo código.
 *
 * El endpoint base entra por `NEXT_PUBLIC_API_BASE_URL` (sin barra final). Sin él, las llamadas
 * fallan con un error claro (no se inventa un host).
 */
import type {
  ClipUrlResponse,
  GetDeviceResponse,
  ListDevicesResponse,
  ListEventsResponse,
  ReleaseChannel,
} from "./types";

const API_BASE_URL = (process.env.NEXT_PUBLIC_API_BASE_URL ?? "").replace(/\/+$/, "");

/** Error HTTP de la API con el código de estado y el mensaje del cuerpo `{ error }` si lo hay. */
export class ApiError extends Error {
  readonly status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

interface RequestOptions {
  /** idToken de Cognito para el header Authorization. */
  idToken: string;
  /** Parámetros de query (se omiten los `undefined`). */
  query?: Record<string, string | number | undefined>;
  /** AbortSignal opcional (cancelación desde el cliente). */
  signal?: AbortSignal;
}

function buildUrl(path: string, query?: RequestOptions["query"]): string {
  if (!API_BASE_URL) {
    throw new ApiError(0, "NEXT_PUBLIC_API_BASE_URL no está configurado");
  }
  const url = new URL(`${API_BASE_URL}${path}`);
  if (query) {
    for (const [key, value] of Object.entries(query)) {
      if (value !== undefined) {
        url.searchParams.set(key, String(value));
      }
    }
  }
  return url.toString();
}

async function apiGet<T>(path: string, opts: RequestOptions): Promise<T> {
  const res = await fetch(buildUrl(path, opts.query), {
    method: "GET",
    headers: {
      Authorization: `Bearer ${opts.idToken}`,
      Accept: "application/json",
    },
    // Datos de flota siempre frescos: nunca cachear la respuesta autenticada.
    cache: "no-store",
    signal: opts.signal,
  });

  if (!res.ok) {
    let message = `HTTP ${res.status}`;
    try {
      const body = (await res.json()) as { error?: string };
      if (body?.error) {
        message = body.error;
      }
    } catch {
      // cuerpo no-JSON: nos quedamos con el "HTTP <status>"
    }
    throw new ApiError(res.status, message);
  }

  return (await res.json()) as T;
}

// ───────────────────────── Endpoints tipados ─────────────────────────

/** GET /devices — lista de flota (opcionalmente filtrada por canal), paginada por cursor opaco. */
export function listDevices(
  opts: RequestOptions & {
    channel?: ReleaseChannel;
    limit?: number;
    cursor?: string;
  },
): Promise<ListDevicesResponse> {
  return apiGet<ListDevicesResponse>("/devices", {
    ...opts,
    query: { channel: opts.channel, limit: opts.limit, cursor: opts.cursor },
  });
}

/** GET /devices/{deviceId} — detalle de un dispositivo del registro. */
export function getDevice(
  deviceId: string,
  opts: RequestOptions,
): Promise<GetDeviceResponse> {
  return apiGet<GetDeviceResponse>(`/devices/${encodeURIComponent(deviceId)}`, opts);
}

/** GET /devices/{deviceId}/events — eventos de una cámara, más recientes primero, paginados. */
export function listEvents(
  deviceId: string,
  opts: RequestOptions & { camera?: string; limit?: number; cursor?: string },
): Promise<ListEventsResponse> {
  return apiGet<ListEventsResponse>(
    `/devices/${encodeURIComponent(deviceId)}/events`,
    {
      ...opts,
      query: { camera: opts.camera, limit: opts.limit, cursor: opts.cursor },
    },
  );
}

/** GET /clips/url?key=... — presigned GET de corta vida para reproducir el media del evento. */
export function getClipUrl(
  key: string,
  opts: RequestOptions,
): Promise<ClipUrlResponse> {
  return apiGet<ClipUrlResponse>("/clips/url", {
    ...opts,
    query: { key },
  });
}
