/**
 * Config de Amplify Auth (Cognito) de la consola de flota, derivada de `NEXT_PUBLIC_*`.
 *
 * Auth con el User Pool de operadores de WP10 (módulo `cognito`): el web client SPA (PKCE, sin
 * secret) emite el JWT que la SPA presenta como `Authorization: Bearer <idToken>` a la API de
 * WP11 (authorizer JWT Cognito). La SPA NUNCA habla DynamoDB/S3 directo.
 *
 * Se usa el MISMO objeto en cliente (`Amplify.configure(amplifyConfig, { ssr: true })`) y en
 * servidor (`createServerRunner({ config: amplifyConfig })`, ver `auth-server.ts`), de modo que
 * los tokens viajan por cookie y los Server Components pueden leer la sesión en SSR.
 *
 * CERO secretos: todos los valores son IDs públicos de Cognito + el endpoint de la API. El login
 * PKCE end-to-end con dominio Amplify (callback válido) se cablea/valida en WP13.
 */
import type { ResourcesConfig } from "aws-amplify";

const region = process.env.NEXT_PUBLIC_AWS_REGION ?? "us-east-1";
const userPoolId = process.env.NEXT_PUBLIC_COGNITO_USER_POOL_ID ?? "";
const userPoolClientId = process.env.NEXT_PUBLIC_COGNITO_WEB_CLIENT_ID ?? "";
const identityPoolId = process.env.NEXT_PUBLIC_COGNITO_IDENTITY_POOL_ID;

// Hosted UI (OAuth/PKCE) OPCIONAL: sólo si hay dominio configurado. El `domain` es el FQDN
// `<prefijo>.auth.<region>.amazoncognito.com` (Cognito espera el host completo, sin esquema).
const hostedUiDomainPrefix = process.env.NEXT_PUBLIC_COGNITO_HOSTED_UI_DOMAIN;
const redirectSignIn = process.env.NEXT_PUBLIC_COGNITO_REDIRECT_SIGN_IN;
const redirectSignOut = process.env.NEXT_PUBLIC_COGNITO_REDIRECT_SIGN_OUT;

const oauth =
  hostedUiDomainPrefix && redirectSignIn && redirectSignOut
    ? {
        domain: `${hostedUiDomainPrefix}.auth.${region}.amazoncognito.com`,
        scopes: ["openid", "email", "profile"],
        redirectSignIn: [redirectSignIn],
        redirectSignOut: [redirectSignOut],
        responseType: "code" as const, // Authorization Code + PKCE
      }
    : undefined;

export const amplifyConfig: ResourcesConfig = {
  Auth: {
    Cognito: {
      userPoolId,
      userPoolClientId,
      ...(identityPoolId ? { identityPoolId } : {}),
      loginWith: {
        // Login por usuario/contraseña (SRP) soportado siempre; OAuth/Hosted UI sólo si hay dominio.
        username: true,
        email: true,
        ...(oauth ? { oauth } : {}),
      },
    },
  },
};

/** True si faltan los IDs mínimos de Cognito (entorno no configurado) -> el login no funcionará. */
export function isAuthConfigured(): boolean {
  return Boolean(userPoolId && userPoolClientId);
}
