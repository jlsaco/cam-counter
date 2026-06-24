import type { DeviceRegistryItem } from "@/lib/types";

/** Cabecera de detalle de un device (presentacional): identidad + estado OTA + hardware. */
export function DeviceHeader({ device }: { device: DeviceRegistryItem }) {
  return (
    <div className="card">
      <div className="row">
        <h1 className="mono" style={{ margin: 0 }}>
          {device.device_id}
        </h1>
        <span className="badge">{device.release_channel}</span>
        {device.status ? <span className="badge">{device.status}</span> : null}
      </div>
      <dl className="kv" style={{ marginTop: 12 }}>
        <dt>Sitio</dt>
        <dd className="mono">{device.site_id}</dd>

        <dt>Cámaras</dt>
        <dd className="mono">{device.camera_ids?.join(", ") || "—"}</dd>

        <dt>Versión reportada</dt>
        <dd className="mono">{device.reported_version ?? "—"}</dd>

        <dt>Versión deseada (nube)</dt>
        <dd className="mono">{device.desired_version ?? "—"}</dd>

        <dt>Última buena</dt>
        <dd className="mono">{device.last_good_version ?? "—"}</dd>

        <dt>Estado OTA</dt>
        <dd className="mono">{device.last_update_status ?? "—"}</dd>

        {device.last_update_error ? (
          <>
            <dt>Último error</dt>
            <dd className="mono">{device.last_update_error}</dd>
          </>
        ) : null}

        <dt>Agente</dt>
        <dd className="mono">{device.agent_version ?? "—"}</dd>

        <dt>Hardware</dt>
        <dd className="mono">
          {device.hardware ? `${device.hardware.model} · Hailo ${device.hardware.hailo_fw}` : "—"}
        </dd>

        <dt>Último visto</dt>
        <dd className="mono">{device.last_seen_at ?? "—"}</dd>
      </dl>
    </div>
  );
}
