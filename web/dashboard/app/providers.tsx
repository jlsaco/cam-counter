"use client";

/**
 * Providers de cliente: inicializa Amplify (con almacenamiento en cookies para SSR) y
 * envuelve la app en el <Authenticator> como GUARD de autenticación.
 *
 * - `Amplify.configure(config, { ssr: true })`: persiste los tokens en cookies para que los
 *   Server Components (lib/auth-server.ts) lean el idToken y hagan fetch server-side con JWT.
 * - <Authenticator>: en DESARROLLO LOCAL usa el flujo usuario/clave (USER_SRP_AUTH), único
 *   flujo de password habilitado en el app client web (cognito WP10). El login OAuth/PKCE por
 *   Hosted UI se valida end-to-end en WP13.
 * - Si falta config de Cognito (NEXT_PUBLIC_* sin rellenar), no bloqueamos el render con un
 *   guard a medias: mostramos un aviso (útil en build/preview sin entorno).
 */
import { Authenticator } from "@aws-amplify/ui-react";
import "@aws-amplify/ui-react/styles.css";
import { Amplify } from "aws-amplify";
import { useEffect, useState } from "react";

import { amplifyConfig, isAmplifyConfigured } from "@/lib/amplify-config";

if (isAmplifyConfigured) {
  Amplify.configure(amplifyConfig, { ssr: true });
}

export function Providers({ children }: { children: React.ReactNode }) {
  // Evita un mismatch de hidratación: el guard depende de estado de cliente.
  const [mounted, setMounted] = useState(false);
  useEffect(() => setMounted(true), []);

  if (!isAmplifyConfigured) {
    return (
      <div className="auth-screen">
        <div className="card" style={{ maxWidth: 480 }}>
          <h1>Configuración de Cognito ausente</h1>
          <p className="muted">
            Rellena <span className="mono">NEXT_PUBLIC_COGNITO_USER_POOL_ID</span> y{" "}
            <span className="mono">NEXT_PUBLIC_COGNITO_USER_POOL_CLIENT_ID</span> (outputs de
            Terraform del módulo cognito, WP10) en <span className="mono">.env.local</span>.
          </p>
        </div>
      </div>
    );
  }

  if (!mounted) {
    return null;
  }

  return (
    <Authenticator.Provider>
      <Authenticator hideSignUp className="auth-screen">
        {() => <>{children}</>}
      </Authenticator>
    </Authenticator.Provider>
  );
}
