import { ApiError, listDevices } from "@/lib/api-client";
import { requireServerIdToken } from "@/lib/require-auth";
import { FleetTable } from "@/components/FleetTable";
import { ErrorNotice } from "@/components/ErrorNotice";

// SSR dinámico: la lista de flota se resuelve por request con el JWT del operador (no se prerenderiza
// en build, no se cachea). Cumple "lista de flota server-side con JWT".
export const dynamic = "force-dynamic";

export default async function FleetPage() {
  const idToken = await requireServerIdToken("/fleet");

  try {
    const { devices, next_cursor } = await listDevices({ idToken, limit: 100 });
    return (
      <section>
        <div className="mb-4 flex items-baseline justify-between">
          <h1 className="text-xl font-semibold">Flota</h1>
          <span className="text-sm text-gray-500">{devices.length} dispositivos</span>
        </div>
        <FleetTable devices={devices} />
        {next_cursor && (
          <p className="mt-3 text-sm text-gray-500">
            Hay más dispositivos (lista paginada; se muestran los primeros {devices.length}).
          </p>
        )}
      </section>
    );
  } catch (err) {
    const message =
      err instanceof ApiError ? `${err.message} (HTTP ${err.status})` : String(err);
    return (
      <section>
        <h1 className="mb-4 text-xl font-semibold">Flota</h1>
        <ErrorNotice message={message} />
      </section>
    );
  }
}
