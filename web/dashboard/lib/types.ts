/**
 * Tipos espejo VERBATIM de los contratos compartidos (`contracts/`), NO una reinvención.
 *
 * Reglas (CLAUDE.md §8 + notas del revisor de WP12):
 *  - snake_case EXACTO de los schemas; `schema_version = 1`.
 *  - CrossingEvent usa `line_version`, `clip_key`, `clip_status`, `crossing_seq`, `track_id`.
 *    NO existen `count_delta` ni `line_config_version` (no inventar campos).
 *  - DeviceRegistryItem: `desired_version` lo escribe la NUBE (espejo); `reported_version` la Pi.
 *
 * La API de WP11 (read-only) sanea los items: elimina los atributos internos de clave
 * (`PK`/`SK`/`GSI1PK`/`GSI1SK`) y convierte los Decimals de DynamoDB a number, de modo que el
 * JSON recibido casa con estos tipos. Los campos marcados opcionales (`?`) reflejan que NO están
 * en la lista `required` del schema (la nube puede no haberlos materializado todavía).
 */

// ───────────────────────── CrossingEvent (contracts/crossing_event.schema.json) ─────────────────────────

/** Único valor de cable/almacenado del sentido del cruce (los términos humanos van en *_label). */
export type CrossingDirection = "in" | "out";

/** Estado de subida del media del evento. */
export type ClipStatus = "pending" | "uploading" | "uploaded" | "failed";

/** Evento de cruce de línea (snake_case, schema_version=1). */
export interface CrossingEvent {
  /** sha1 hex-minúscula determinista de 'site|device|camera|track|crossing_seq' (dedup, NO cripto). */
  event_id: string;
  site_id: string;
  device_id: string;
  /** Slug global único '{device_id}-cam{N}'. */
  camera_id: string;
  track_id: string;
  /** Contador MONÓTONO PERSISTIDO POR CÁMARA (no por track, no reiniciable). */
  crossing_seq: number;
  direction: CrossingDirection;
  /** Etiqueta humana del sentido positivo, p.ej. 'subieron'. */
  positive_label?: string;
  /** Etiqueta humana del sentido negativo, p.ej. 'bajaron'. */
  negative_label?: string;
  /** Etiqueta humana resuelta en el momento del evento (positive/negative según direction). */
  label?: string;
  /** Versión de la config de línea en vigor al contar el cruce (config_version de line_config). */
  line_version?: number;
  /** Timestamp del evento en epoch ms UTC. Autoritativo. */
  ts_event_ms: number;
  /** Timestamp del evento en ISO-8601 UTC (espejo legible de ts_event_ms). */
  ts_event_iso: string;
  confidence?: number;
  /** Clave S3 del media o null si aún no aplica. media/{site}/{device}/{camera}/{yyyy}/{mm}/{dd}/{event_id}.{ext}. */
  clip_key?: string | null;
  clip_status?: ClipStatus;
  /** const 1: cualquier rename de campo es BREAKING. */
  schema_version: 1;
  /** Flag SÓLO-LOCAL (SQLite): no se persiste en la nube; no debería llegar por la API. */
  synced?: 0 | 1;
  created_at?: string;
}

// ───────────────── DeviceRegistryItem (contracts/device_registry_item.schema.json) ─────────────────

/** Canal de release al que está suscrito el dispositivo. */
export type ReleaseChannel = "canary" | "stable";

/** Estado de la última operación de actualización OTA. */
export type LastUpdateStatus =
  | "idle"
  | "downloading"
  | "verifying"
  | "activating"
  | "healthy"
  | "rolled_back"
  | "failed";

/** Estado operativo del dispositivo. */
export type DeviceStatus = "online" | "offline" | "updating" | "degraded";

/** Información de hardware del dispositivo. */
export interface DeviceHardware {
  /** Modelo del Pi, p.ej. 'Raspberry Pi 5'. */
  model: string;
  /** Versión de firmware del acelerador Hailo. */
  hailo_fw: string;
}

/** Item del registro de dispositivos (DynamoDB 'cam-counter-devices'). */
export interface DeviceRegistryItem {
  device_id: string;
  site_id: string;
  /** Cada camera_id es un slug global único '{device_id}-cam{N}'. */
  camera_ids?: string[];
  release_channel: ReleaseChannel;
  /** SemVer. Lo escribe la NUBE = versión del manifiesto del canal. ESPEJO de observabilidad. */
  desired_version?: string;
  /** SemVer de la versión instalada, reportada por la Pi. */
  reported_version?: string;
  /** SemVer de la última versión que pasó el health-check (objetivo de auto-rollback). */
  last_good_version?: string;
  last_update_status?: LastUpdateStatus;
  last_update_error?: string | null;
  /** Último heartbeat del dispositivo (ISO-8601 UTC). */
  last_seen_at?: string;
  /** Versión del update-agent que corre en la Pi. */
  agent_version?: string;
  status?: DeviceStatus;
  hardware?: DeviceHardware;
  /** const 1: cualquier rename de campo es BREAKING. */
  schema_version: 1;
}

// ───────────────────────── Envelopes de la API de flota (WP11) ─────────────────────────

/** GET /devices[?channel=&limit=&cursor=] */
export interface ListDevicesResponse {
  devices: DeviceRegistryItem[];
  /** Cursor OPACO base64-url para la siguiente página, o null si no hay más. */
  next_cursor: string | null;
}

/** GET /devices/{deviceId} */
export interface GetDeviceResponse {
  device: DeviceRegistryItem;
}

/** GET /devices/{deviceId}/events[?camera=&limit=&cursor=] */
export interface ListEventsResponse {
  events: CrossingEvent[];
  next_cursor: string | null;
  /** Cámara efectivamente consultada (la de `?camera=` o la primera de camera_ids). */
  camera_id: string;
}

/** GET /clips/url?key=... -> presigned GET de corta vida (TTL del lado servidor). */
export interface ClipUrlResponse {
  url: string;
  key: string;
  expires_in: number;
}

/** Cuerpo de error uniforme de la API: `{ "error": "..." }`. */
export interface ApiErrorBody {
  error: string;
}
