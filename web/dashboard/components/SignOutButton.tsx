"use client";

/**
 * Botón de cierre de sesión. Sólo se muestra cuando hay un usuario autenticado (estado del
 * Authenticator). `signOut` limpia los tokens (cookies) y `useAuthenticator` re-renderiza la app
 * a estado no autenticado; las páginas protegidas redirigirán a /login en el siguiente request.
 */
import { useAuthenticator } from "@aws-amplify/ui-react";

export function SignOutButton() {
  const { authStatus, signOut } = useAuthenticator((ctx) => [
    ctx.authStatus,
    ctx.signOut,
  ]);

  if (authStatus !== "authenticated") {
    return null;
  }

  return (
    <button
      type="button"
      onClick={signOut}
      className="rounded border border-gray-300 px-3 py-1 text-gray-700 hover:bg-gray-100"
    >
      Salir
    </button>
  );
}
