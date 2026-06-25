"use client";

/**
 * Tabla de eventos de cruce de una cámara, paginada por CURSOR OPACO (no por offset). La primera
 * página llega del Server Component (ya autenticada en SSR); "Cargar más" y el cambio de cámara
 * paginan EN CLIENTE con el idToken del navegador (`getClientIdToken()`), apuntando a la misma API
 * de WP11. Más recientes primero (la API usa ScanIndexForward=False).
 *
 * Cada fila enlaza al detalle del evento, pasando `camera` y `key` (clip_key) por query para que
 * el detalle pueda pedir la presigned URL sin re-listar.
 */
import Link from "next/link";
import { useCallback, useState } from "react";

import { ApiError, listEvents } from "@/lib/api-client";
import { getClientIdToken } from "@/lib/auth-client";
import { directionLabel, formatTimestamp } from "@/lib/format";
import type { CrossingEvent } from "@/lib/types";

interface Props {
  deviceId: string;
  cameraIds: string[];
  initialCamera: string;
  initialEvents: CrossingEvent[];
  initialCursor: string | null;
}

export function EventsTable({
  deviceId,
  cameraIds,
  initialCamera,
  initialEvents,
  initialCursor,
}: Props) {
  const [camera, setCamera] = useState(initialCamera);
  const [events, setEvents] = useState<CrossingEvent[]>(initialEvents);
  const [cursor, setCursor] = useState<string | null>(initialCursor);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const fetchPage = useCallback(
    async (forCamera: string, fromCursor: string | undefined, append: boolean) => {
      setLoading(true);
      setError(null);
      try {
        const idToken = await getClientIdToken();
        if (!idToken) {
          throw new ApiError(401, "sesión no válida; vuelve a iniciar sesión");
        }
        const res = await listEvents(deviceId, {
          idToken,
          camera: forCamera,
          cursor: fromCursor,
          limit: 50,
        });
        setEvents((prev) => (append ? [...prev, ...res.events] : res.events));
        setCursor(res.next_cursor);
      } catch (err) {
        setError(
          err instanceof ApiError ? `${err.message} (HTTP ${err.status})` : String(err),
        );
      } finally {
        setLoading(false);
      }
    },
    [deviceId],
  );

  const onChangeCamera = (next: string) => {
    setCamera(next);
    setEvents([]);
    setCursor(null);
    void fetchPage(next, undefined, false);
  };

  const onLoadMore = () => {
    if (cursor) {
      void fetchPage(camera, cursor, true);
    }
  };

  return (
    <section className="mt-6">
      <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
        <h2 className="text-lg font-semibold">Eventos de cruce</h2>
        {cameraIds.length > 1 && (
          <label className="text-sm text-gray-600">
            Cámara:{" "}
            <select
              value={camera}
              onChange={(e) => onChangeCamera(e.target.value)}
              disabled={loading}
              className="rounded border border-gray-300 px-2 py-1"
            >
              {cameraIds.map((c) => (
                <option key={c} value={c}>
                  {c}
                </option>
              ))}
            </select>
          </label>
        )}
      </div>

      {error && (
        <p className="mb-3 rounded border border-red-300 bg-red-50 p-3 text-sm text-red-800">
          {error}
        </p>
      )}

      <div className="overflow-x-auto rounded border border-gray-200 bg-white">
        <table className="min-w-full divide-y divide-gray-200 text-sm">
          <thead className="bg-gray-50 text-left text-xs uppercase tracking-wide text-gray-500">
            <tr>
              <th className="px-4 py-2">ts_event</th>
              <th className="px-4 py-2">dirección</th>
              <th className="px-4 py-2">crossing_seq</th>
              <th className="px-4 py-2">track_id</th>
              <th className="px-4 py-2">line_version</th>
              <th className="px-4 py-2">clip_status</th>
              <th className="px-4 py-2"></th>
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-100">
            {events.map((ev) => (
              <tr key={ev.event_id} className="hover:bg-gray-50">
                <td className="px-4 py-2 text-gray-700">{formatTimestamp(ev.ts_event_ms)}</td>
                <td className="px-4 py-2 text-gray-900">{directionLabel(ev)}</td>
                <td className="px-4 py-2 text-gray-700">{ev.crossing_seq}</td>
                <td className="px-4 py-2 font-mono text-xs text-gray-600">{ev.track_id}</td>
                <td className="px-4 py-2 text-gray-700">{ev.line_version ?? "—"}</td>
                <td className="px-4 py-2 text-gray-700">{ev.clip_status ?? "—"}</td>
                <td className="px-4 py-2">
                  <Link
                    href={{
                      pathname: `/devices/${encodeURIComponent(deviceId)}/events/${encodeURIComponent(ev.event_id)}`,
                      query: {
                        camera: ev.camera_id,
                        ...(ev.clip_key ? { key: ev.clip_key } : {}),
                      },
                    }}
                    className="text-blue-700 hover:underline"
                  >
                    detalle
                  </Link>
                </td>
              </tr>
            ))}
            {events.length === 0 && !loading && (
              <tr>
                <td colSpan={7} className="px-4 py-6 text-center text-gray-500">
                  Sin eventos para esta cámara.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      <div className="mt-3 flex items-center gap-3">
        {cursor && (
          <button
            type="button"
            onClick={onLoadMore}
            disabled={loading}
            className="rounded border border-gray-300 px-3 py-1 text-sm text-gray-700 hover:bg-gray-100 disabled:opacity-50"
          >
            {loading ? "Cargando…" : "Cargar más"}
          </button>
        )}
        {loading && !cursor && <span className="text-sm text-gray-500">Cargando…</span>}
      </div>
    </section>
  );
}
