import { FleetTable } from "@/components/FleetTable";
import { ApiError, getDevices } from "@/lib/api-client";
import { getServerIdToken } from "@/lib/auth-server";
import type { ReleaseChannel } from "@/lib/types";

// Datos en vivo: nunca cachear la lista de flota.
export const dynamic = "force-dynamic";

const CHANNELS: ReleaseChannel[] = ["canary", "stable"];

function isChannel(v: string | undefined): v is ReleaseChannel {
  return v === "canary" || v === "stable";
}

/** Vista de flota — server-side con JWT (GET /devices, Query GSI1 por canal en la API). */
export default async function FleetPage({
  searchParams,
}: {
  searchParams: { channel?: string };
}) {
  const channel = isChannel(searchParams.channel) ? searchParams.channel : undefined;

  let body: React.ReactNode;
  try {
    const token = await getServerIdToken();
    const { devices, count } = await getDevices(token, channel);
    body = (
      <>
        <p className="muted">
          {count} dispositivo{count === 1 ? "" : "s"}
          {channel ? ` · canal ${channel}` : ""}
        </p>
        <FleetTable devices={devices} />
      </>
    );
  } catch (err) {
    const msg = err instanceof ApiError ? `${err.status} · ${err.message}` : "error desconocido";
    body = <div className="error">No se pudo cargar la flota: {msg}</div>;
  }

  return (
    <>
      <div className="row">
        <h1>Flota</h1>
        <span className="spacer" />
        <nav className="row" style={{ gap: 8 }}>
          <a className="btn" href="/fleet">
            Todos
          </a>
          {CHANNELS.map((ch) => (
            <a key={ch} className="btn" href={`/fleet?channel=${ch}`}>
              {ch}
            </a>
          ))}
        </nav>
      </div>
      {body}
    </>
  );
}
