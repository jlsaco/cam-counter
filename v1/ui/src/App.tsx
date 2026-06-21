import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "./api/client";
import type { Camera, Counters, DeviceInfo, Health, LineConfig } from "./api/types";
import { WsClient } from "./api/ws";
import { CameraSwitcher } from "./components/CameraSwitcher";
import { HistoryTable } from "./components/HistoryTable";
import { LineEditor } from "./components/LineEditor";
import { LiveView } from "./components/LiveView";

export default function App() {
  const [device, setDevice] = useState<DeviceInfo | null>(null);
  const [health, setHealth] = useState<Health | null>(null);
  const [cameras, setCameras] = useState<Camera[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [config, setConfig] = useState<LineConfig | null>(null);
  const [counters, setCounters] = useState<Counters | null>(null);
  const [historyRefresh, setHistoryRefresh] = useState(0);
  const wsRef = useRef<WsClient | null>(null);

  // Carga inicial: device, cámaras y arranque del WS (con reconexión).
  useEffect(() => {
    void (async () => {
      const [dev, cams] = await Promise.all([api.device(), api.cameras()]);
      setDevice(dev);
      setCameras(cams);
      setSelected((cur) => cur ?? cams[0]?.camera_id ?? null);
    })();

    const ws = new WsClient();
    ws.connect();
    wsRef.current = ws;
    return () => ws.close();
  }, []);

  // Poll de salud (cabecera): liveness + frames_flowing del producto.
  useEffect(() => {
    let alive = true;
    const tick = async () => {
      try {
        const h = await api.health();
        if (alive) setHealth(h);
      } catch {
        /* offline transitorio: se reintenta */
      }
    };
    void tick();
    const id = setInterval(() => void tick(), 3000);
    return () => {
      alive = false;
      clearInterval(id);
    };
  }, []);

  const loadCamera = useCallback(async (cameraId: string) => {
    const [cfg, cnt] = await Promise.all([
      api.getConfig(cameraId),
      api.counters(cameraId),
    ]);
    setConfig(cfg);
    setCounters(cnt);
  }, []);

  // Al cambiar de cámara: recarga config + counters y refresca el histórico.
  useEffect(() => {
    if (!selected) return;
    void loadCamera(selected);
    setHistoryRefresh((n) => n + 1);
  }, [selected, loadCamera]);

  // Suscripción WS acotada a la cámara seleccionada (reconexión la maneja WsClient).
  useEffect(() => {
    const ws = wsRef.current;
    if (!ws || !selected) return;
    return ws.subscribe((env) => {
      if (env.camera_id !== selected) return;
      switch (env.type) {
        case "counter_update":
          void api.counters(selected).then(setCounters);
          break;
        case "crossing":
          void api.counters(selected).then(setCounters);
          setHistoryRefresh((n) => n + 1);
          break;
        case "config_changed":
          void api.getConfig(selected).then(setConfig);
          break;
        case "camera_status":
          setCameras((prev) =>
            prev.map((c) =>
              c.camera_id === selected
                ? { ...c, online: env.data.online === true }
                : c,
            ),
          );
          break;
      }
    });
  }, [selected]);

  const selectedCam = cameras.find((c) => c.camera_id === selected) ?? null;

  return (
    <div className="mx-auto max-w-6xl p-4 space-y-4">
      <header className="flex flex-wrap items-center justify-between gap-2 border-b border-zinc-800 pb-3">
        <div>
          <h1 className="text-xl font-bold">cam-counter</h1>
          <p className="text-xs text-zinc-400">
            {device ? `${device.site_id} / ${device.device_id}` : "cargando…"}
            {device && ` · v${device.app_version}`}
            {device?.fake_source && " · fuente falsa"}
          </p>
        </div>
        {health && (
          <div className="text-right text-xs">
            <span
              className={`rounded px-2 py-0.5 ${
                health.status === "ok" ? "bg-emerald-700" : "bg-red-700"
              }`}
            >
              {health.status}
            </span>
            <span className="ml-2 text-zinc-400">
              frames {health.frames_flowing ? "fluyendo" : "0"} · db v
              {health.db_schema_version}
            </span>
          </div>
        )}
      </header>

      <CameraSwitcher cameras={cameras} selected={selected} onSelect={setSelected} />

      {selected ? (
        <>
          <div className="grid gap-4 lg:grid-cols-2">
            <LiveView
              cameraId={selected}
              config={config}
              counters={counters}
              online={selectedCam?.online ?? false}
            />
            {config && (
              <LineEditor cameraId={selected} config={config} onSaved={setConfig} />
            )}
          </div>
          <HistoryTable cameraId={selected} refreshKey={historyRefresh} />
        </>
      ) : (
        <p className="text-zinc-400">Selecciona una cámara.</p>
      )}
    </div>
  );
}
