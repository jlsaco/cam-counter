import { redirect } from "next/navigation";

import { getServerIdToken } from "./auth-server";

/**
 * Guard server-side de las páginas protegidas: devuelve el idToken o redirige a /login
 * (preservando el destino en `?from=`). Centraliza el patrón "sin sesión -> login" para que cada
 * Server Component sólo escriba `const idToken = await requireServerIdToken(path)`.
 */
export async function requireServerIdToken(fromPath: string): Promise<string> {
  const idToken = await getServerIdToken();
  if (!idToken) {
    redirect(`/login?from=${encodeURIComponent(fromPath)}`);
  }
  return idToken;
}
