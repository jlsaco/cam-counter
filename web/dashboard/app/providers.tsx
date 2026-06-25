"use client";

/**
 * Configura Amplify EN EL CLIENTE con `{ ssr: true }` (tokens en cookies) y provee el contexto
 * del Authenticator a toda la app. Es un Client Component montado por el root layout: la
 * configuración corre una sola vez en el navegador.
 *
 * `{ ssr: true }` es lo que permite que los Server Components lean la sesión vía
 * `getServerIdToken()` (cookies compartidas), cumpliendo "lista de flota server-side con JWT".
 */
import { Authenticator } from "@aws-amplify/ui-react";
import { Amplify } from "aws-amplify";

import { amplifyConfig } from "@/lib/amplify-config";

Amplify.configure(amplifyConfig, { ssr: true });

export function Providers({ children }: { children: React.ReactNode }) {
  return <Authenticator.Provider>{children}</Authenticator.Provider>;
}
