import { DeviceHeader } from "@/components/DeviceHeader";
import { EventsTable } from "@/components/EventsTable";
import { ApiError, getDevice, getEvents, type ListEventsOpts } from "@/lib/api-client";
import { getServerIdToken } from "@/lib/auth-server";
import type { CrossingEvent } from "@/lib/types";

export const dynamic = "force-dynamic";

/** Detalle de un device: cabecera + primera página de eventos (cursor en cliente). */
export default async function DevicePage({ params }: { params: { deviceId: string } }) {
  const deviceId = decodeURIComponent(params.deviceId);
  const token = await getServerIdToken();

  // 1) El device (si falla, no tiene sentido pedir eventos).
  let header: React.ReactNode;
  let eventsOpts: ListEventsOpts = {};
  try {
    const { device } = await getDevice(token, deviceId);
    header = <DeviceHeader device={device} />;
    // Si el device tiene varias cámaras, la API exige camera_id explícito: tomamos la primera.
    const cams = device.camera_ids ?? [];
    if (cams.length > 1) {
      eventsOpts = { site_id: device.site_id, camera_id: cams[0] };
    }
  } catch (err) {
    const msg = err instanceof ApiError ? `${err.status} · ${err.message}` : "error desconocido";
    return (
      <>
        <p>
          <a href="/fleet">← Flota</a>
        </p>
        <div className="error">No se pudo cargar el device {deviceId}: {msg}</div>
      </>
    );
  }

  // 2) Primera página de eventos (recientes primero). Errores no tumban la cabecera.
  let events: CrossingEvent[] = [];
  let cursor: string | null = null;
  let eventsError: string | null = null;
  try {
    const page = await getEvents(token, deviceId, eventsOpts);
    events = page.events;
    cursor = page.next_cursor;
  } catch (err) {
    eventsError = err instanceof ApiError ? `${err.status} · ${err.message}` : "error desconocido";
  }

  return (
    <>
      <p>
        <a href="/fleet">← Flota</a>
      </p>
      {header}
      <h2>Eventos de cruce</h2>
      {eventsError ? (
        <div className="error">No se pudieron cargar los eventos: {eventsError}</div>
      ) : (
        <EventsTable deviceId={deviceId} initialEvents={events} initialCursor={cursor} />
      )}
    </>
  );
}
