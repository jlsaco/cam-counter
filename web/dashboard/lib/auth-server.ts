/**
 * Puente de Amplify Auth en el SERVIDOR (App Router / Server Components).
 *
 * `createServerRunner` ejecuta operaciones de Amplify en el contexto del request leyendo los
 * tokens de las COOKIES que el cliente (configurado con `{ ssr: true }`) escribió tras el login.
 * Así los Server Components listan la flota SERVER-SIDE con el JWT del operador (criterio de
 * aceptación de WP12) sin exponer credenciales AWS al navegador.
 *
 * `getServerIdToken()` devuelve el idToken (string) para `Authorization: Bearer ...` hacia la API
 * de WP11, o `null` si no hay sesión válida (las páginas protegidas redirigen a /login).
 */
import { createServerRunner } from "@aws-amplify/adapter-nextjs";
import { fetchAuthSession } from "aws-amplify/auth/server";
import { cookies } from "next/headers";

import { amplifyConfig } from "./amplify-config";

export const { runWithAmplifyServerContext } = createServerRunner({
  config: amplifyConfig,
});

/** idToken de la sesión actual (SSR) o null si no hay sesión / no se pudo resolver. */
export async function getServerIdToken(): Promise<string | null> {
  try {
    return await runWithAmplifyServerContext({
      nextServerContext: { cookies },
      operation: async (contextSpec) => {
        const session = await fetchAuthSession(contextSpec);
        return session.tokens?.idToken?.toString() ?? null;
      },
    });
  } catch {
    // Sin sesión (o cookies ausentes/expiradas): se trata como no autenticado.
    return null;
  }
}
