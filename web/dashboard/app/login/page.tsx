import { redirect } from "next/navigation";

import { isServerAuthenticated } from "@/lib/auth-server";

/**
 * Página de login. El <Authenticator> guard (app/providers.tsx) ya muestra el formulario de
 * acceso cuando NO hay sesión, por lo que esta ruta sirve sobre todo como destino de
 * `redirectSignOut`. Si ya hay sesión, saltamos a la flota.
 */
export default async function LoginPage() {
  if (await isServerAuthenticated()) {
    redirect("/fleet");
  }
  return (
    <div className="card" style={{ maxWidth: 480, margin: "48px auto" }}>
      <h1>Acceso a la consola de flota</h1>
      <p className="muted">
        Inicia sesión con tu usuario de operador (Cognito). En desarrollo local se usa el flujo
        usuario/clave; el login OAuth/PKCE por dominio Amplify se valida en WP13.
      </p>
    </div>
  );
}
