/**
 * Helper de Amplify Auth en el CLIENTE (navegador).
 *
 * Los Client Components (p.ej. `EventsTable` al paginar, `ClipPlayer` al pedir la presigned URL)
 * obtienen el idToken de la sesión del navegador con `fetchAuthSession()`. Amplify se configura
 * con `{ ssr: true }` (ver `providers.tsx`), así que el token vive en cookies y es coherente con
 * el que lee el servidor.
 */
import { fetchAuthSession } from "aws-amplify/auth";

/** idToken de la sesión actual (cliente) o null si no hay sesión válida. */
export async function getClientIdToken(): Promise<string | null> {
  try {
    const session = await fetchAuthSession();
    return session.tokens?.idToken?.toString() ?? null;
  } catch {
    return null;
  }
}
