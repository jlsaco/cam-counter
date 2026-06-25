import Link from "next/link";

import { deviceStatusClass } from "@/lib/format";
import type { DeviceRegistryItem } from "@/lib/types";

/**
 * Tabla de la flota. Server Component puro (sin estado): cada fila enlaza al detalle del
 * dispositivo. Muestra campos VERBATIM del registro (status, release_channel, reported/desired
 * version, last_seen_at). `desired_version` es ESPEJO escrito por la nube; `reported_version` lo
 * reporta la Pi.
 */
export function FleetTable({ devices }: { devices: DeviceRegistryItem[] }) {
  if (devices.length === 0) {
    return (
      <p className="rounded border border-gray-200 bg-white p-4 text-sm text-gray-500">
        No hay dispositivos registrados.
      </p>
    );
  }

  return (
    <div className="overflow-x-auto rounded border border-gray-200 bg-white">
      <table className="min-w-full divide-y divide-gray-200 text-sm">
        <thead className="bg-gray-50 text-left text-xs uppercase tracking-wide text-gray-500">
          <tr>
            <th className="px-4 py-2">device_id</th>
            <th className="px-4 py-2">site_id</th>
            <th className="px-4 py-2">canal</th>
            <th className="px-4 py-2">estado</th>
            <th className="px-4 py-2">reported</th>
            <th className="px-4 py-2">desired</th>
            <th className="px-4 py-2">last_seen_at</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-gray-100">
          {devices.map((d) => (
            <tr key={d.device_id} className="hover:bg-gray-50">
              <td className="px-4 py-2 font-medium">
                <Link
                  href={`/devices/${encodeURIComponent(d.device_id)}`}
                  className="text-blue-700 hover:underline"
                >
                  {d.device_id}
                </Link>
              </td>
              <td className="px-4 py-2 text-gray-700">{d.site_id}</td>
              <td className="px-4 py-2 text-gray-700">{d.release_channel}</td>
              <td className="px-4 py-2">
                <span
                  className={`inline-block rounded px-2 py-0.5 text-xs font-medium ${deviceStatusClass(d.status)}`}
                >
                  {d.status ?? "—"}
                </span>
              </td>
              <td className="px-4 py-2 text-gray-700">{d.reported_version ?? "—"}</td>
              <td className="px-4 py-2 text-gray-700">{d.desired_version ?? "—"}</td>
              <td className="px-4 py-2 text-gray-500">{d.last_seen_at ?? "—"}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
