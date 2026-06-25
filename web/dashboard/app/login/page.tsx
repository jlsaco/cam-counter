"use client";

/**
 * Página de login: el `<Authenticator>` de Amplify (UI guard) maneja sign-in/sign-up/MFA contra el
 * User Pool de operadores (WP10). Al autenticar, Amplify (`{ ssr: true }`) escribe los tokens en
 * cookies y redirigimos a /fleet, donde el Server Component ya puede leer el JWT en SSR.
 *
 * Nota (revisor [MEDIA]): el login web real PKCE con dominio Amplify (callback válido) se valida
 * end-to-end en WP13. En local se usa el flujo usuario/contraseña soportado por el web client.
 */
import { Authenticator, useAuthenticator } from "@aws-amplify/ui-react";
import { useRouter, useSearchParams } from "next/navigation";
import { useEffect } from "react";

import { isAuthConfigured } from "@/lib/amplify-config";

function RedirectOnAuth() {
  const router = useRouter();
  const params = useSearchParams();
  const { authStatus } = useAuthenticator((ctx) => [ctx.authStatus]);

  useEffect(() => {
    if (authStatus === "authenticated") {
      // `from` permite volver a la página protegida que disparó la redirección a /login.
      const from = params.get("from");
      router.replace(from && from.startsWith("/") ? from : "/fleet");
    }
  }, [authStatus, params, router]);

  return null;
}

export default function LoginPage() {
  return (
    <div className="mx-auto max-w-md py-8">
      <h1 className="mb-6 text-center text-xl font-semibold">Acceso de operadores</h1>
      {!isAuthConfigured() && (
        <p className="mb-4 rounded border border-yellow-300 bg-yellow-50 p-3 text-sm text-yellow-800">
          Cognito no está configurado (faltan <code>NEXT_PUBLIC_COGNITO_*</code>). Rellena el
          entorno para poder iniciar sesión.
        </p>
      )}
      <Authenticator hideSignUp>
        {() => <RedirectOnAuth />}
      </Authenticator>
    </div>
  );
}
