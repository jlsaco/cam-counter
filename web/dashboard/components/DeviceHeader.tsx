import Link from "next/link";

import { deviceStatusClass } from "@/lib/format";
import type { DeviceRegistryItem } from "@/lib/types";

/**
 * Cabecera del detalle de un dispositivo: identidad + estado OTA + hardware. Campos VERBATIM del
 * contrato device_registry_item (CLAUDE.md §8). Server Component puro.
 */
function Field({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div>
      <dt className="text-xs uppercase tracking-wide text-gray-500">{label}</dt>
      <dd className="text-sm text-gray-900">{value ?? "—"}</dd>
    </div>
  );
}

export function DeviceHeader({ device }: { device: DeviceRegistryItem }) {
  return (
    <div className="rounded border border-gray-200 bg-white p-5">
      <div className="mb-4 flex flex-wrap items-center gap-3">
        <Link href="/fleet" className="text-sm text-blue-700 hover:underline">
          ← Flota
        </Link>
        <h1 className="text-xl font-semibold">{device.device_id}</h1>
        <span
          className={`inline-block rounded px-2 py-0.5 text-xs font-medium ${deviceStatusClass(device.status)}`}
        >
          {device.status ?? "—"}
        </span>
      </div>
      <dl className="grid grid-cols-2 gap-4 sm:grid-cols-3 lg:grid-cols-4">
        <Field label="site_id" value={device.site_id} />
        <Field label="release_channel" value={device.release_channel} />
        <Field label="reported_version" value={device.reported_version} />
        <Field label="desired_version" value={device.desired_version} />
        <Field label="last_good_version" value={device.last_good_version} />
        <Field label="last_update_status" value={device.last_update_status} />
        <Field label="agent_version" value={device.agent_version} />
        <Field label="last_seen_at" value={device.last_seen_at} />
        <Field
          label="camera_ids"
          value={device.camera_ids?.length ? device.camera_ids.join(", ") : "—"}
        />
        {device.hardware && (
          <>
            <Field label="hardware.model" value={device.hardware.model} />
            <Field label="hardware.hailo_fw" value={device.hardware.hailo_fw} />
          </>
        )}
        {device.last_update_error && (
          <Field label="last_update_error" value={device.last_update_error} />
        )}
      </dl>
    </div>
  );
}
