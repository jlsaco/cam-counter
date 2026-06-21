// Tipos TS COHERENTES con los modelos Pydantic de v1/api/schemas.py (que a su
// vez reflejan contracts/). snake_case por fidelidad con el JSON del cable.
// Geometría SIEMPRE en floats normalizados 0..1 (origen arriba-izquierda).

export type Direction = "in" | "out";
export type ClipStatus = "pending" | "uploading" | "uploaded" | "failed";
export type PositiveSide = -1 | 1;

export interface Point2D {
  x: number;
  y: number;
}

export interface LineGeom {
  a: Point2D;
  b: Point2D;
}

export interface LineConfig {
  site_id: string;
  device_id: string;
  camera_id: string;
  config_version: number;
  line: LineGeom;
  positive_side: PositiveSide;
  positive_label: string | null;
  negative_label: string | null;
  updated_at: string | null;
  schema_version: number;
}

export interface LineConfigUpdate {
  line: LineGeom;
  positive_side: PositiveSide;
  positive_label?: string | null;
  negative_label?: string | null;
  expected_config_version: number;
}

export interface Camera {
  camera_id: string;
  site_id: string;
  device_id: string;
  config_version: number;
  has_config: boolean;
  frames_processed: number;
  online: boolean;
}

export interface DeviceInfo {
  device_id: string;
  site_id: string;
  app_version: string;
  git_sha: string;
  camera_ids: string[];
  db_schema_version: number;
  fake_source: boolean;
}

export interface CounterDay {
  day_utc: string;
  direction: Direction;
  count: number;
}

export interface Counters {
  camera_id: string;
  in_count: number;
  out_count: number;
  net: number;
  days: CounterDay[];
}

export interface CrossingEvent {
  event_id: string;
  site_id: string;
  device_id: string;
  camera_id: string;
  track_id: string;
  crossing_seq: number;
  direction: Direction;
  positive_label: string | null;
  negative_label: string | null;
  label: string | null;
  line_version: number | null;
  ts_event_ms: number;
  ts_event_iso: string;
  confidence: number | null;
  clip_key: string | null;
  clip_status: ClipStatus | null;
  schema_version: number;
  synced: number;
  created_at: string | null;
}

export interface CameraHealth {
  camera_id: string;
  frames_processed: number;
  last_inference_ts: number | null;
  hailo_inference_ok: boolean | null;
  config_version: number;
}

export interface Health {
  status: "ok" | "degraded";
  app_version: string;
  db_schema_version: number;
  fake_source: boolean;
  frames_flowing: boolean;
  cameras: CameraHealth[];
}

export type WsType =
  | "counter_update"
  | "camera_status"
  | "config_changed"
  | "crossing";

export interface WsEnvelope {
  type: WsType;
  camera_id: string;
  ts_ms: number;
  data: Record<string, unknown>;
}
