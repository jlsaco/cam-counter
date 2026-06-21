import { useEffect, useState } from "react";
import { api, ConfigConflictError } from "../api/client";
import type { LineConfig, LineGeom, PositiveSide } from "../api/types";
import { useElementSize } from "../hooks/useElementSize";
import { LineOverlay } from "./LineOverlay";

interface Props {
  cameraId: string;
  config: LineConfig;
  onSaved: (config: LineConfig) => void;
}

type SaveState = { kind: "idle" | "saving" | "saved" | "error"; message?: string };

/**
 * Editor de la línea: arrastrar extremos normalizados (sin round-trip por
 * arrastre), toggle "invertir sentido" (mapea a `positive_side`) y guardado con
 * manejo de 409 (recarga la config vigente y deja reintentar).
 */
export function LineEditor({ cameraId, config, onSaved }: Props) {
  const [ref, size] = useElementSize<HTMLDivElement>();
  const [line, setLine] = useState<LineGeom>(config.line);
  const [positiveSide, setPositiveSide] = useState<PositiveSide>(config.positive_side);
  const [baseVersion, setBaseVersion] = useState<number>(config.config_version);
  const [state, setState] = useState<SaveState>({ kind: "idle" });

  // Sincroniza el borrador cuando el padre carga otra config (cámara o reload).
  useEffect(() => {
    setLine(config.line);
    setPositiveSide(config.positive_side);
    setBaseVersion(config.config_version);
    setState({ kind: "idle" });
  }, [config.camera_id, config.config_version]);

  const invert = () => setPositiveSide((s) => (s === 1 ? -1 : 1));

  const save = async () => {
    setState({ kind: "saving" });
    try {
      const saved = await api.putConfig(cameraId, {
        line,
        positive_side: positiveSide,
        positive_label: config.positive_label,
        negative_label: config.negative_label,
        expected_config_version: baseVersion,
      });
      setBaseVersion(saved.config_version);
      onSaved(saved);
      setState({ kind: "saved", message: `Guardado (v${saved.config_version}).` });
    } catch (err) {
      if (err instanceof ConfigConflictError) {
        // 409: la config cambió bajo nuestros pies. Recargamos y dejamos reintentar.
        const fresh = await api.getConfig(cameraId);
        setLine(fresh.line);
        setPositiveSide(fresh.positive_side);
        setBaseVersion(fresh.config_version);
        setState({
          kind: "error",
          message: `Conflicto: la config cambió (ahora v${fresh.config_version}). Recargada — revisa y reintenta.`,
        });
      } else {
        setState({ kind: "error", message: `Error al guardar: ${String(err)}` });
      }
    }
  };

  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between">
        <h2 className="text-lg font-semibold">Editar línea</h2>
        <span className="text-xs text-zinc-400">base v{baseVersion}</span>
      </div>

      <div
        ref={ref}
        className="relative w-full overflow-hidden rounded-lg bg-zinc-900 aspect-video"
      >
        <img
          key={`edit-${cameraId}`}
          src={api.streamUrl(cameraId)}
          alt={`editar ${cameraId}`}
          className="absolute inset-0 h-full w-full object-contain opacity-40"
        />
        <LineOverlay
          line={line}
          width={size.width}
          height={size.height}
          editable
          onChange={setLine}
        />
      </div>

      <div className="flex flex-wrap items-center gap-3">
        <button
          type="button"
          onClick={invert}
          className="rounded bg-zinc-700 px-3 py-1.5 text-sm hover:bg-zinc-600"
        >
          Invertir sentido (positive_side={positiveSide})
        </button>
        <button
          type="button"
          onClick={save}
          disabled={state.kind === "saving"}
          className="rounded bg-amber-500 px-3 py-1.5 text-sm font-medium text-black hover:bg-amber-400 disabled:opacity-50"
        >
          {state.kind === "saving" ? "Guardando…" : "Guardar"}
        </button>
        <span className="text-xs text-zinc-400">
          A=({line.a.x.toFixed(2)},{line.a.y.toFixed(2)}) B=({line.b.x.toFixed(2)},
          {line.b.y.toFixed(2)})
        </span>
      </div>

      {state.message && (
        <p
          className={`text-sm ${
            state.kind === "error" ? "text-red-400" : "text-emerald-400"
          }`}
        >
          {state.message}
        </p>
      )}
    </div>
  );
}
