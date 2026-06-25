import { notFound } from "next/navigation";

import { ApiError, getDevice, listEvents } from "@/lib/api-client";
import { requireServerIdToken } from "@/lib/require-auth";
import { DeviceHeader } from "@/components/DeviceHeader";
import { EventsTable } from "@/components/EventsTable";
import { ErrorNotice } from "@/components/ErrorNotice";

export const dynamic = "force-dynamic";

export default async function DevicePage({
  params,
}: {
  params: { deviceId: string };
}) {
  const deviceId = decodeURIComponent(params.deviceId);
  const idToken = await requireServerIdToken(`/devices/${deviceId}`);

  let device;
  try {
    ({ device } = await getDevice(deviceId, { idToken }));
  } catch (err) {
    if (err instanceof ApiError && err.status === 404) {
      notFound();
    }
    const message =
      err instanceof ApiError ? `${err.message} (HTTP ${err.status})` : String(err);
    return <ErrorNotice message={message} />;
  }

  // Primera página de eventos en SSR (cámara por defecto = la elegida por la API). La paginación
  // y el cambio de cámara siguen en cliente (EventsTable).
  const cameraIds = device.camera_ids ?? [];
  let initialEvents: Awaited<ReturnType<typeof listEvents>> | null = null;
  let eventsError: string | null = null;
  if (cameraIds.length > 0) {
    try {
      initialEvents = await listEvents(deviceId, { idToken, limit: 50 });
    } catch (err) {
      eventsError =
        err instanceof ApiError ? `${err.message} (HTTP ${err.status})` : String(err);
    }
  }

  return (
    <div>
      <DeviceHeader device={device} />
      {eventsError && (
        <div className="mt-6">
          <ErrorNotice message={eventsError} />
        </div>
      )}
      {initialEvents && (
        <EventsTable
          deviceId={deviceId}
          cameraIds={cameraIds}
          initialCamera={initialEvents.camera_id}
          initialEvents={initialEvents.events}
          initialCursor={initialEvents.next_cursor}
        />
      )}
      {cameraIds.length === 0 && (
        <p className="mt-6 rounded border border-gray-200 bg-white p-4 text-sm text-gray-500">
          El dispositivo no tiene cámaras registradas.
        </p>
      )}
    </div>
  );
}
