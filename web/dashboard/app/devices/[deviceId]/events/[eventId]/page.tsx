import { ClipPlayer } from "@/components/ClipPlayer";
import { ApiError, getEvents, type ListEventsOpts } from "@/lib/api-client";
import { getServerIdToken } from "@/lib/auth-server";
import type { CrossingEvent } from "@/lib/types";

export const dynamic = "force-dynamic";

// La fleet-api (WP11) expone eventos solo como lista paginada (no hay GET por event_id).
// Para el detalle, recorremos las páginas recientes-primero buscando el event_id. Cota de
// páginas para no barrer la tabla entera (los eventos abiertos desde la tabla son recientes).
const MAX_LOOKUP_PAGES = 5;

async function findEvent(
  token: string | null,
  deviceId: string,
  eventId: string,
  opts: ListEventsOpts,
): Promise<CrossingEvent | null> {
  let cursor: string | null | undefined = undefined;
  for (let page = 0; page < MAX_LOOKUP_PAGES; page++) {
    const resp = await getEvents(token, deviceId, { ...opts, limit: 100, cursor });
    const hit = resp.events.find((e) => e.event_id === eventId);
    if (hit) return hit;
    if (!resp.next_cursor) break;
    cursor = resp.next_cursor;
  }
  return null;
}

/** Detalle de un evento de cruce + reproducción del clip por presigned URL. */
export default async function EventPage({
  params,
  searchParams,
}: {
  params: { deviceId: string; eventId: string };
  searchParams: { site_id?: string; camera_id?: string };
}) {
  const deviceId = decodeURIComponent(params.deviceId);
  const eventId = decodeURIComponent(params.eventId);
  // Pistas de ruteo desde la tabla (la API resuelve la partición de eventos por cámara).
  const opts: ListEventsOpts = {
    site_id: searchParams.site_id,
    camera_id: searchParams.camera_id,
  };

  const back = (
    <p>
      <a href={`/devices/${encodeURIComponent(deviceId)}`}>← {deviceId}</a>
    </p>
  );

  let event: CrossingEvent | null = null;
  let error: string | null = null;
  try {
    const token = await getServerIdToken();
    event = await findEvent(token, deviceId, eventId, opts);
  } catch (err) {
    error = err instanceof ApiError ? `${err.status} · ${err.message}` : "error desconocido";
  }

  if (error) {
    return (
      <>
        {back}
        <div className="error">No se pudo cargar el evento: {error}</div>
      </>
    );
  }
  if (!event) {
    return (
      <>
        {back}
        <div className="error">
          Evento <span className="mono">{eventId}</span> no encontrado en las páginas recientes.
          Ábrelo desde la tabla de eventos del dispositivo.
        </div>
      </>
    );
  }

  return (
    <>
      {back}
      <h1>Evento de cruce</h1>
      <div className="card">
        <dl className="kv">
          <dt>event_id</dt>
          <dd className="mono" style={{ wordBreak: "break-all" }}>
            {event.event_id}
          </dd>

          <dt>Cuándo</dt>
          <dd className="mono">
            {event.ts_event_iso} ({event.ts_event_ms} ms)
          </dd>

          <dt>Cámara</dt>
          <dd className="mono">{event.camera_id}</dd>

          <dt>Sitio / device</dt>
          <dd className="mono">
            {event.site_id} / {event.device_id}
          </dd>

          <dt>Sentido</dt>
          <dd>
            <span className="badge">{event.direction}</span>
            {event.label ? ` · ${event.label}` : ""}
          </dd>

          <dt>track_id</dt>
          <dd className="mono">{event.track_id}</dd>

          <dt>crossing_seq</dt>
          <dd className="mono">{event.crossing_seq}</dd>

          <dt>line_version</dt>
          <dd className="mono">{event.line_version ?? "—"}</dd>

          <dt>confidence</dt>
          <dd className="mono">{event.confidence ?? "—"}</dd>

          <dt>clip_status</dt>
          <dd className="mono">{event.clip_status ?? "—"}</dd>

          <dt>clip_key</dt>
          <dd className="mono" style={{ wordBreak: "break-all" }}>
            {event.clip_key ?? "—"}
          </dd>
        </dl>
      </div>

      {event.clip_key ? (
        <div style={{ marginTop: 16 }}>
          <ClipPlayer clipKey={event.clip_key} />
        </div>
      ) : (
        <p className="muted" style={{ marginTop: 16 }}>
          Este evento no tiene clip asociado.
        </p>
      )}
    </>
  );
}
