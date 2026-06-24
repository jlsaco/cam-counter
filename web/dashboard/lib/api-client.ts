/**
 * Cliente HTTP de la fleet-api (WP11) — ISOMÓRFICO (server o client).
 *
 * Reglas duras:
 *   - El frontend habla SOLO con esta API autenticada; NUNCA con DynamoDB/S3 directo.
 *   - Cada llamada lleva `Authorization: Bearer <idToken>` (JWT Cognito que valida el
 *     authorizer del API Gateway HTTP API v2).
 *   - El idToken NO se obtiene aquí: lo inyecta quien llama —
 *       · Server Components → lib/auth-server.ts (getServerIdToken, lee cookies SSR).
 *       · Client Components → aws-amplify/auth `fetchAuthSession()` en el navegador.
 *     Así este módulo es importable desde ambos lados (no arrastra `server-only`).
 *
 * Base de la API: NEXT_PUBLIC_API_BASE_URL (FQDN del HTTP API, sin barra final).
 */
import type {
  ClipUrlResponse,
  GetDeviceResponse,
  ListDevicesResponse,
  ListEventsResponse,
  ReleaseChannel,
} from "./types";

const RAW_BASE = process.env.NEXT_PUBLIC_API_BASE_URL ?? "";
/** Base normalizada sin barra final. */
export const API_BASE_URL = RAW_BASE.replace(/\/+$/, "");

/** Error tipado de la API (lleva el status HTTP y el mensaje del body { error }). */
export class ApiError extends Error {
  readonly status: number;
  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

function buildUrl(path: string, query?: Record<string, string | number | undefined | null>): string {
  if (!API_BASE_URL) {
    throw new ApiError(0, "NEXT_PUBLIC_API_BASE_URL no configurada");
  }
  const url = new URL(API_BASE_URL + (path.startsWith("/") ? path : `/${path}`));
  if (query) {
    for (const [k, v] of Object.entries(query)) {
      if (v !== undefined && v !== null && v !== "") {
        url.searchParams.set(k, String(v));
      }
    }
  }
  return url.toString();
}

/**
 * GET autenticado contra la fleet-api. Devuelve el body JSON tipado o lanza ApiError.
 * `no-store`: la consola muestra estado de flota en vivo (sin caché stale).
 */
async function apiGet<T>(
  path: string,
  idToken: string | null,
  query?: Record<string, string | number | undefined | null>,
): Promise<T> {
  if (!idToken) {
    throw new ApiError(401, "no autenticado (idToken ausente)");
  }
  const res = await fetch(buildUrl(path, query), {
    method: "GET",
    headers: {
      Authorization: `Bearer ${idToken}`,
      accept: "application/json",
    },
    cache: "no-store",
  });

  if (!res.ok) {
    let message = `HTTP ${res.status}`;
    try {
      const body = (await res.json()) as { error?: string };
      if (body?.error) message = body.error;
    } catch {
      /* body no-JSON: nos quedamos con el status. */
    }
    throw new ApiError(res.status, message);
  }
  return (await res.json()) as T;
}

// ───────────────────────── Wrappers tipados por endpoint ─────────────────────────

/** GET /devices (filtro opcional por canal). */
export function getDevices(idToken: string | null, channel?: ReleaseChannel): Promise<ListDevicesResponse> {
  return apiGet<ListDevicesResponse>("/devices", idToken, { channel });
}

/** GET /devices/{deviceId}. */
export function getDevice(idToken: string | null, deviceId: string): Promise<GetDeviceResponse> {
  return apiGet<GetDeviceResponse>(`/devices/${encodeURIComponent(deviceId)}`, idToken);
}

/** Opciones de paginación de eventos (cursor OPACO, limit, y override de cámara/sitio). */
export interface ListEventsOpts {
  cursor?: string | null;
  limit?: number;
  /** Necesarios solo si el device tiene varias cámaras (la API exige camera_id explícito). */
  site_id?: string;
  camera_id?: string;
}

/** GET /devices/{deviceId}/events (más recientes primero, cursor opaco). */
export function getEvents(
  idToken: string | null,
  deviceId: string,
  opts: ListEventsOpts = {},
): Promise<ListEventsResponse> {
  return apiGet<ListEventsResponse>(`/devices/${encodeURIComponent(deviceId)}/events`, idToken, {
    cursor: opts.cursor,
    limit: opts.limit,
    site_id: opts.site_id,
    camera_id: opts.camera_id,
  });
}

/** GET /clips/url?key=... → presigned URL GET de corta vida para el media del evento. */
export function getClipUrl(idToken: string | null, key: string): Promise<ClipUrlResponse> {
  return apiGet<ClipUrlResponse>("/clips/url", idToken, { key });
}
