# Consola de flota cam-counter (`web/dashboard`) — WP12

App **Next.js (App Router)** que muestra la flota, el detalle de un dispositivo, sus eventos
de cruce paginados y la **reproducción del clip MP4** del evento. **SOLO LECTURA**: el
frontend **NUNCA** habla con DynamoDB/S3 directo — todo pasa por la **fleet-api (WP11)**
autenticada con **JWT Cognito** (Amplify Auth).

## Arquitectura

```
Navegador ──(Amplify Auth: idToken)──► fleet-api (API Gateway HTTP API v2 + authorizer JWT)
                                          ├─ GET /devices            (Query GSI1 por canal)
                                          ├─ GET /devices/{id}
                                          ├─ GET /devices/{id}/events (cursor opaco)
                                          └─ GET /clips/url?key=...   (presigned GET, TTL 300s)
```

- **Server Components** (flota, detalle, evento) hacen `fetch` server-side con el idToken
  leído de las cookies SSR (`lib/auth-server.ts` + `@aws-amplify/adapter-nextjs`).
- **Client Components** interactivos:
  - `EventsTable` → "cargar más" con **cursor opaco** (paginación).
  - `ClipPlayer` → pide la **presigned URL on-demand** a `GET /clips/url` y **refresca** la
    URL automáticamente si el `<video>` falla (la URL caduca a los 300 s).
- `lib/types.ts` es **espejo VERBATIM** de `contracts/` (`crossing_event`,
  `device_registry_item`): `line_version`, `clip_key`, `clip_status`, `crossing_seq`,
  `track_id`. **No** existen `count_delta` ni `line_config_version`.

## Configuración (`NEXT_PUBLIC_*`)

Copia la plantilla y rellena con los outputs de Terraform (todos son **identificadores
públicos**, ningún secreto AWS):

```bash
cp .env.example .env.local
# NEXT_PUBLIC_API_BASE_URL              ← FQDN del HTTP API de WP11
# NEXT_PUBLIC_COGNITO_USER_POOL_ID      ← terraform output cognito_user_pool_id
# NEXT_PUBLIC_COGNITO_USER_POOL_CLIENT_ID ← terraform output cognito_web_client_id
```

## Desarrollo local

```bash
npm install
npm run dev      # http://localhost:3000
npm run build    # build de producción
npm run lint
```

El **login local** usa el flujo usuario/clave (USER_SRP_AUTH) del `<Authenticator>`, único
flujo de password habilitado en el app client web (módulo `cognito`, WP10). Crea un operador
con `scripts/cognito-create-admin.sh`. El **login OAuth/PKCE por dominio Amplify** se valida
end-to-end en **WP13** (callback válido).

## Despliegue

`amplify.yml` (`appRoot: web/dashboard`) define el build de Amplify Hosting. El cableado
real (dominio, callbacks, variables de entorno) es WP13.
