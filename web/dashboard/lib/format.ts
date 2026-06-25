/**
 * Helpers de formato/presentación (puros, sin estado). No tocan datos de los contratos: sólo
 * derivan etiquetas legibles para la UI a partir de campos VERBATIM (`ts_event_ms`, `direction`,
 * `clip_status`, ...).
 */
import type { ClipStatus, CrossingEvent, DeviceStatus } from "./types";

/** Epoch ms -> fecha/hora local legible. `ts_event_ms` es el campo autoritativo del contrato. */
export function formatTimestamp(tsEventMs: number): string {
  const d = new Date(tsEventMs);
  if (Number.isNaN(d.getTime())) {
    return "—";
  }
  return d.toLocaleString();
}

/**
 * Etiqueta humana del sentido del cruce. Prefiere `label` (resuelto en el evento); si no, deriva
 * de `direction` + positive_label/negative_label (CLAUDE.md §8). Nunca almacena 'subieron'/'bajaron'
 * por su cuenta: sólo los muestra si vienen en el contrato.
 */
export function directionLabel(ev: CrossingEvent): string {
  if (ev.label) {
    return ev.label;
  }
  if (ev.direction === "in" && ev.positive_label) {
    return ev.positive_label;
  }
  if (ev.direction === "out" && ev.negative_label) {
    return ev.negative_label;
  }
  return ev.direction;
}

/** Clase Tailwind del badge de estado del dispositivo (sólo color; el texto es `status`). */
export function deviceStatusClass(status: DeviceStatus | undefined): string {
  switch (status) {
    case "online":
      return "bg-green-100 text-green-800";
    case "updating":
      return "bg-blue-100 text-blue-800";
    case "degraded":
      return "bg-yellow-100 text-yellow-800";
    case "offline":
      return "bg-red-100 text-red-800";
    default:
      return "bg-gray-100 text-gray-700";
  }
}

/** True si el media del evento es reproducible (subido y con clave S3). */
export function isClipPlayable(
  clipKey: string | null | undefined,
  clipStatus: ClipStatus | undefined,
): clipKey is string {
  return Boolean(clipKey) && clipStatus === "uploaded";
}
