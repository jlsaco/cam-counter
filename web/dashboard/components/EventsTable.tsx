"use client";

import { useCallback, useState } from "react";

import { ApiError, getEvents } from "@/lib/api-client";
import { getClientIdToken } from "@/lib/auth-client";
import type { CrossingEvent } from "@/lib/types";

/**
 * Tabla de eventos de cruce con paginación por CURSOR OPACO.
 *
 * La primera página llega server-side (props). "Cargar más" pide la siguiente página
 * en el navegador con el idToken de la sesión (cursor opaco base64url que devuelve la API).
 * Cada fila enlaza al detalle del evento, pasando site_id/camera_id como pistas de ruteo
 * (la API resuelve la partición de eventos por cámara).
 */
export function EventsTable({
  deviceId,
  initialEvents,
  initialCursor,
}: {
  deviceId: string;
  initialEvents: CrossingEvent[];
  initialCursor: string | null;
}) {
  const [events, setEvents] = useState<CrossingEvent[]>(initialEvents);
  const [cursor, setCursor] = useState<string | null>(initialCursor);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const loadMore = useCallback(async () => {
    if (!cursor || loading) return;
    setLoading(true);
    setError(null);
    try {
      const token = await getClientIdToken();
      const page = await getEvents(token, deviceId, { cursor });
      setEvents((prev) => [...prev, ...page.events]);
      setCursor(page.next_cursor);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "error cargando más eventos");
    } finally {
      setLoading(false);
    }
  }, [cursor, deviceId, loading]);

  if (events.length === 0) {
    return <p className="muted">Sin eventos de cruce para este dispositivo.</p>;
  }

  return (
    <>
      <table>
        <thead>
          <tr>
            <th>Cuándo (UTC)</th>
            <th>Cámara</th>
            <th>Sentido</th>
            <th>Etiqueta</th>
            <th>Seq</th>
            <th>Línea v.</th>
            <th>Clip</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          {events.map((e) => {
            const href =
              `/devices/${encodeURIComponent(deviceId)}/events/${encodeURIComponent(e.event_id)}` +
              `?site_id=${encodeURIComponent(e.site_id)}&camera_id=${encodeURIComponent(e.camera_id)}`;
            return (
              <tr key={e.event_id}>
                <td className="mono">{e.ts_event_iso}</td>
                <td className="mono">{e.camera_id}</td>
                <td>
                  <span className="badge">{e.direction}</span>
                </td>
                <td>{e.label ?? "—"}</td>
                <td className="mono">{e.crossing_seq}</td>
                <td className="mono">{e.line_version ?? "—"}</td>
                <td className="mono">{e.clip_status ?? (e.clip_key ? "uploaded" : "—")}</td>
                <td>
                  <a href={href}>ver →</a>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>

      <div className="row" style={{ marginTop: 12 }}>
        {error ? <span className="error">{error}</span> : null}
        <span className="spacer" />
        {cursor ? (
          <button className="btn" onClick={loadMore} disabled={loading}>
            {loading ? "Cargando…" : "Cargar más"}
          </button>
        ) : (
          <span className="muted">No hay más eventos.</span>
        )}
      </div>
    </>
  );
}
