/**
 * Tipos del frontend — ESPEJO VERBATIM de los contratos JSON Schema de `contracts/`.
 *
 * Fuente de verdad (NO se inventan campos):
 *   - contracts/crossing_event.schema.json        → CrossingEvent
 *   - contracts/device_registry_item.schema.json  → DeviceRegistryItem
 *
 * Reglas (CLAUDE.md §3/§8, notas del revisor WP12):
 *   - snake_case, `schema_version = 1`.
 *   - Campos REALES del evento: line_version, clip_key, clip_status, crossing_seq, track_id.
 *     NO existen `count_delta` ni `line_config_version` (campos inventados → prohibidos).
 *   - El frontend es SOLO LECTURA: estos tipos describen lo que la fleet-api (WP11) devuelve.
 */

// ───────────────────────── CrossingEvent (contracts/crossing_event.schema.json) ─────────────────────────

/** direction: único valor de cable/almacenado del sentido del cruce. */
export type CrossingDirection = "in" | "out";

/** Estado de subida del media del evento. */
export type ClipStatus = "pending" | "uploading" | "uploaded" | "failed";

export interface CrossingEvent {
  /** sha1 hex-minúscula determinista de 'site_id|device_id|camera_id|track_id|crossing_seq'. */
  event_id: string;
  site_id: string;
  device_id: string;
  /** Forma '{device_id}-cam{N}'. */
  camera_id: string;
  track_id: string;
  /** Contador MONÓTONO por cámara (no por track, no reiniciable). */
  crossing_seq: number;
  direction: CrossingDirection;
  /** Etiqueta humana del sentido positivo (p.ej. 'subieron'). */
  positive_label?: string;
  /** Etiqueta humana del sentido negativo (p.ej. 'bajaron'). */
  negative_label?: string;
  /** Etiqueta humana resuelta según direction. */
  label?: string;
  /** config_version de line_config en vigor cuando se contó el cruce. */
  line_version?: number;
  /** epoch milisegundos UTC (autoritativo). */
  ts_event_ms: number;
  /** ISO-8601 UTC (espejo legible de ts_event_ms). */
  ts_event_iso: string;
  confidence?: number;
  /** Clave S3 del media, o null. media/{site}/{device}/{camera}/{yyyy}/{mm}/{dd}/{event_id}.{ext}. */
  clip_key?: string | null;
  clip_status?: ClipStatus;
  /** const 1. */
  schema_version: number;
  /** Flag SÓLO-LOCAL (SQLite): no llega de la nube, pero se tipa por fidelidad al contrato. */
  synced?: 0 | 1;
  created_at?: string;
}

// ───────────────────────── DeviceRegistryItem (contracts/device_registry_item.schema.json) ─────────────────────────

export type ReleaseChannel = "canary" | "stable";

export type LastUpdateStatus =
  | "idle"
  | "downloading"
  | "verifying"
  | "activating"
  | "healthy"
  | "rolled_back"
  | "failed";

export type DeviceStatus = "online" | "offline" | "updating" | "degraded";

export interface DeviceHardware {
  model: string;
  hailo_fw: string;
}

export interface DeviceRegistryItem {
  device_id: string;
  site_id: string;
  camera_ids?: string[];
  release_channel: ReleaseChannel;
  /** SemVer escrito por la NUBE (espejo de observabilidad). */
  desired_version?: string;
  /** SemVer reportado por la Pi. */
  reported_version?: string;
  last_good_version?: string;
  last_update_status?: LastUpdateStatus;
  last_update_error?: string | null;
  last_seen_at?: string;
  agent_version?: string;
  status?: DeviceStatus;
  hardware?: DeviceHardware;
  /** const 1. */
  schema_version: number;
}

// ───────────────────────── Envolturas de respuesta de la fleet-api (WP11) ─────────────────────────
// Forma de los bodies JSON que devuelve lambdas/fleet_api/handler.py.

/** GET /devices → enumera dispositivos (Query GSI1 por canal; nunca Scan). */
export interface ListDevicesResponse {
  devices: DeviceRegistryItem[];
  count: number;
}

/** GET /devices/{deviceId} → item del registro. */
export interface GetDeviceResponse {
  device: DeviceRegistryItem;
}

/** GET /devices/{deviceId}/events → página de eventos + cursor OPACO base64url. */
export interface ListEventsResponse {
  events: CrossingEvent[];
  count: number;
  /** Cursor opaco para la siguiente página, o null si no hay más. */
  next_cursor: string | null;
}

/** GET /clips/url?key=... → presigned URL GET de corta vida (clip_presign, WP11). */
export interface ClipUrlResponse {
  url: string;
  key: string;
  expires_in: number;
}

/** Body de error uniforme de la API ({ error }). */
export interface ApiErrorBody {
  error: string;
}
