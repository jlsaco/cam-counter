"use client";

import { useAuthenticator } from "@aws-amplify/ui-react";

/** Botón de cierre de sesión (usa el contexto del <Authenticator>). */
export function SignOutButton() {
  const { signOut, user } = useAuthenticator((ctx) => [ctx.user]);
  const label = user?.signInDetails?.loginId ?? user?.username ?? "operador";
  return (
    <div className="row" style={{ gap: 8 }}>
      <span className="muted mono">{label}</span>
      <button className="btn" onClick={signOut}>
        Salir
      </button>
    </div>
  );
}
