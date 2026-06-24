/**
 * idToken del lado CLIENTE (navegador) para los componentes interactivos
 * (EventsTable "cargar más", ClipPlayer). Lo lee de la sesión Amplify del navegador.
 * Complementa a lib/auth-server.ts (que lo obtiene de las cookies SSR).
 */
import { fetchAuthSession } from "aws-amplify/auth";

export async function getClientIdToken(): Promise<string | null> {
  try {
    const session = await fetchAuthSession();
    return session.tokens?.idToken?.toString() ?? null;
  } catch {
    return null;
  }
}
