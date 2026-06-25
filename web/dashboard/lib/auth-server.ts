/**
 * Autenticación SSR (Server Components / Route Handlers).
 *
 * `createServerRunner` del adaptador Next de Amplify ejecuta operaciones de Auth en el
 * servidor leyendo las cookies de la request (donde el <Authenticator> cliente, con
 * `ssr: true`, guarda los tokens). Así los Server Components obtienen el idToken para
 * llamar a la fleet-api (WP11) con `Authorization: Bearer` SIN exponer credenciales al
 * navegador ni hablar nunca con DynamoDB/S3 directo.
 */
import "server-only";

import { createServerRunner } from "@aws-amplify/adapter-nextjs";
import { fetchAuthSession } from "aws-amplify/auth/server";
import { cookies } from "next/headers";

import { amplifyConfig } from "./amplify-config";

export const { runWithAmplifyServerContext } = createServerRunner({
  config: amplifyConfig,
});

/**
 * Devuelve el idToken (JWT Cognito) del usuario autenticado en el contexto de la request,
 * o `null` si no hay sesión válida. El idToken es el que valida el authorizer del API
 * Gateway HTTP API (audience = web client id).
 */
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
    // Sin sesión / cookies ausentes: tratamos como no autenticado (no es un 500).
    return null;
  }
}

/** True si hay una sesión autenticada en el servidor para esta request. */
export async function isServerAuthenticated(): Promise<boolean> {
  return (await getServerIdToken()) !== null;
}
