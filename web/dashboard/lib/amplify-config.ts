/**
 * Configuración de Amplify (Auth = Cognito) para la consola de flota.
 *
 * Todo viene de variables NEXT_PUBLIC_* (identificadores PÚBLICOS, jamás secretos AWS):
 * User Pool ID + Web Client ID del módulo `cognito` (WP10), y opcionalmente el Hosted UI
 * domain para el login OAuth/PKCE real (se valida end-to-end en WP13).
 *
 * Para DESARROLLO LOCAL no hace falta el Hosted UI: el <Authenticator> usa el flujo
 * usuario/clave (USER_SRP_AUTH), que es el único flujo de password habilitado en el app
 * client web (ver terraform/modules/cognito/main.tf).
 *
 * `cookieStorage` + `ssr: true` permiten que los Server Components lean el idToken de las
 * cookies (vía lib/auth-server.ts) para hacer fetch server-side con JWT.
 */
import type { ResourcesConfig } from "aws-amplify";

const userPoolId = process.env.NEXT_PUBLIC_COGNITO_USER_POOL_ID ?? "";
const userPoolClientId = process.env.NEXT_PUBLIC_COGNITO_USER_POOL_CLIENT_ID ?? "";
const hostedUiDomain = process.env.NEXT_PUBLIC_COGNITO_HOSTED_UI_DOMAIN ?? "";
const region = process.env.NEXT_PUBLIC_AWS_REGION ?? "us-east-1";
const redirectSignIn = process.env.NEXT_PUBLIC_COGNITO_REDIRECT_SIGN_IN ?? "";
const redirectSignOut = process.env.NEXT_PUBLIC_COGNITO_REDIRECT_SIGN_OUT ?? "";

// El bloque oauth (Hosted UI) SOLO se incluye si hay domain configurado (WP13). Sin él,
// el login local funciona igual con usuario/clave (SRP).
const oauth = hostedUiDomain
  ? {
      domain: `${hostedUiDomain}.auth.${region}.amazoncognito.com`,
      scopes: ["openid", "email", "profile"],
      redirectSignIn: redirectSignIn ? [redirectSignIn] : [],
      redirectSignOut: redirectSignOut ? [redirectSignOut] : [],
      responseType: "code" as const, // Authorization Code + PKCE (único flujo permitido).
    }
  : undefined;

export const amplifyConfig: ResourcesConfig = {
  Auth: {
    Cognito: {
      userPoolId,
      userPoolClientId,
      ...(oauth ? { loginWith: { oauth } } : {}),
    },
  },
};

/** True si la config mínima de Cognito está presente (evita inicializar Amplify a medias). */
export const isAmplifyConfigured = Boolean(userPoolId && userPoolClientId);
