import { useCallback, useEffect, useState } from "react";
import { api } from "../api/client";
import type { CrossingEvent } from "../api/types";

interface Props {
  cameraId: string;
  /** Cambia para forzar un refresco (p.ej. al recibir un 'crossing' por WS). */
  refreshKey: number;
}

const PAGE = 25;

/** Histórico paginado de cruces (CrossingEvent) de una cámara. */
export function HistoryTable({ cameraId, refreshKey }: Props) {
  const [events, setEvents] = useState<CrossingEvent[]>([]);
  const [offset, setOffset] = useState(0);
  const [hasMore, setHasMore] = useState(false);

  const load = useCallback(
    async (nextOffset: number) => {
      const rows = await api.events(cameraId, PAGE, nextOffset);
      setEvents(rows);
      setOffset(nextOffset);
      setHasMore(rows.length === PAGE);
    },
    [cameraId],
  );

  useEffect(() => {
    void load(0);
  }, [load, refreshKey]);

  return (
    <div className="space-y-2">
      <h2 className="text-lg font-semibold">Histórico</h2>
      <div className="overflow-x-auto rounded-lg border border-zinc-800">
        <table className="w-full text-left text-sm">
          <thead className="bg-zinc-900 text-zinc-400">
            <tr>
              <th className="px-3 py-2">ts (UTC)</th>
              <th className="px-3 py-2">dirección</th>
              <th className="px-3 py-2">etiqueta</th>
              <th className="px-3 py-2">track</th>
              <th className="px-3 py-2">seq</th>
              <th className="px-3 py-2">clip</th>
            </tr>
          </thead>
          <tbody>
            {events.length === 0 ? (
              <tr>
                <td className="px-3 py-3 text-zinc-500" colSpan={6}>
                  Sin eventos todavía.
                </td>
              </tr>
            ) : (
              events.map((e) => (
                <tr key={e.event_id} className="border-t border-zinc-800">
                  <td className="px-3 py-1.5 font-mono text-xs">{e.ts_event_iso}</td>
                  <td className="px-3 py-1.5">
                    <span
                      className={
                        e.direction === "in" ? "text-emerald-400" : "text-sky-400"
                      }
                    >
                      {e.direction}
                    </span>
                  </td>
                  <td className="px-3 py-1.5">{e.label ?? "—"}</td>
                  <td className="px-3 py-1.5 font-mono text-xs">{e.track_id}</td>
                  <td className="px-3 py-1.5">{e.crossing_seq}</td>
                  <td className="px-3 py-1.5">{e.clip_status ?? "—"}</td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>

      <div className="flex items-center gap-3 text-sm">
        <button
          type="button"
          onClick={() => void load(Math.max(0, offset - PAGE))}
          disabled={offset === 0}
          className="rounded bg-zinc-800 px-3 py-1 hover:bg-zinc-700 disabled:opacity-40"
        >
          ← anteriores
        </button>
        <span className="text-zinc-400">offset {offset}</span>
        <button
          type="button"
          onClick={() => void load(offset + PAGE)}
          disabled={!hasMore}
          className="rounded bg-zinc-800 px-3 py-1 hover:bg-zinc-700 disabled:opacity-40"
        >
          siguientes →
        </button>
      </div>
    </div>
  );
}
