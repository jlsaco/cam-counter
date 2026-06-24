import type { DeviceRegistryItem, DeviceStatus } from "@/lib/types";

const STATUS_CLASS: Record<DeviceStatus, string> = {
  online: "ok",
  offline: "bad",
  updating: "warn",
  degraded: "warn",
};

function StatusBadge({ status }: { status?: DeviceStatus }) {
  if (!status) return <span className="badge">—</span>;
  return <span className={`badge ${STATUS_CLASS[status]}`}>{status}</span>;
}

/** Tabla de flota (presentacional). Cada fila enlaza al detalle del device. */
export function FleetTable({ devices }: { devices: DeviceRegistryItem[] }) {
  if (devices.length === 0) {
    return <p className="muted">No hay dispositivos registrados.</p>;
  }
  return (
    <table>
      <thead>
        <tr>
          <th>Device</th>
          <th>Sitio</th>
          <th>Canal</th>
          <th>Estado</th>
          <th>Versión (reportada)</th>
          <th>Cámaras</th>
          <th>Último visto</th>
        </tr>
      </thead>
      <tbody>
        {devices.map((d) => (
          <tr key={d.device_id}>
            <td>
              <a href={`/devices/${encodeURIComponent(d.device_id)}`} className="mono">
                {d.device_id}
              </a>
            </td>
            <td className="mono">{d.site_id}</td>
            <td>
              <span className="badge">{d.release_channel}</span>
            </td>
            <td>
              <StatusBadge status={d.status} />
            </td>
            <td className="mono">{d.reported_version ?? "—"}</td>
            <td className="mono">{d.camera_ids?.length ?? 0}</td>
            <td className="muted mono">{d.last_seen_at ?? "—"}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
