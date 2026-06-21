import { api } from "../api/client";
import type { Counters, LineConfig } from "../api/types";
import { useElementSize } from "../hooks/useElementSize";
import { LineOverlay } from "./LineOverlay";

interface Props {
  cameraId: string;
  config: LineConfig | null;
  counters: Counters | null;
  online: boolean;
}

/**
 * Vídeo en vivo (MJPEG via `<img>`) + overlay SVG de la línea (coords
 * normalizadas mapeadas al tamaño renderizado del `<img>`) + contadores en vivo.
 */
export function LiveView({ cameraId, config, counters, online }: Props) {
  const [ref, size] = useElementSize<HTMLDivElement>();

  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between">
        <h2 className="text-lg font-semibold">Vídeo en vivo</h2>
        <span
          className={`text-xs px-2 py-0.5 rounded ${
            online ? "bg-emerald-700" : "bg-zinc-700"
          }`}
        >
          {online ? "online" : "sin señal"}
        </span>
      </div>

      <div
        ref={ref}
        className="relative w-full overflow-hidden rounded-lg bg-black aspect-video"
      >
        <img
          // `key` fuerza recargar el MJPEG al cambiar de cámara.
          key={cameraId}
          src={api.streamUrl(cameraId)}
          alt={`stream ${cameraId}`}
          className="absolute inset-0 h-full w-full object-contain"
        />
        {config && (
          <LineOverlay line={config.line} width={size.width} height={size.height} />
        )}

        <div className="absolute left-2 top-2 rounded bg-black/60 px-2 py-1 text-sm">
          <span className="text-line">
            {config?.positive_label ?? "in"}: {counters?.in_count ?? 0}
          </span>
          <span className="mx-2 text-zinc-400">|</span>
          <span className="text-zinc-300">
            {config?.negative_label ?? "out"}: {counters?.out_count ?? 0}
          </span>
          <span className="mx-2 text-zinc-400">|</span>
          <span>net: {counters?.net ?? 0}</span>
        </div>
      </div>
    </div>
  );
}
