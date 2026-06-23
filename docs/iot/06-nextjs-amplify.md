I have enough grounding. The schemas confirm the DynamoDB keys, `clip_key` convention, device registry fields (`status`, `last_seen_at`, `reported_version`, `release_channel`). Now I'll write the design document.

# Dashboard de flota (Next.js + Amplify)

Consola web de operadores para **cam-counter**. App Next.js (App Router) en **AWS Amplify Hosting** que lee la flota desde `cam-counter-devices` + IoT/shadow, lista eventos por dispositivo desde `cam-counter-events`, y reproduce el clip MP4 de cada evento via presigned GET. Cuenta `950639281773`, region `us-east-1`, monorepo `github.com/jlsaco/cam-counter`. La app vive en `web/dashboard/`.

Nombres alineados al estandar (SPEC naming/security/iot): Amplify app `cam-counter-fleet-console`, API `cam-counter-fleet-api`, User Pool `cam-counter-fleet-users`, Lambdas `cam-counter-fleet-api`/`cam-counter-clip-presign`, modulos terraform nuevos `terraform/modules/{fleet-api,cognito-fleet,amplify-fleet-console}`. Todo **aditivo/monotono**: no toca ningun recurso existente.

---

## 0. Decision de capa de datos: API Gateway HTTP + Lambda (REST/JSON) — recomendada

**Recomendacion: API Gateway HTTP API (v2) + Lambdas + authorizer JWT Cognito.** NO AppSync/GraphQL.

| Criterio | **API Gateway HTTP + Lambda** (elegida) | AppSync / GraphQL |
|---|---|---|
| Forma de los datos | El dashboard hace 3 accesos fijos y conocidos: lista devices, lista eventos paginada por device, presign de un clip. Es REST puro, no un grafo. | Sobra: no hay relaciones profundas que justificar un grafo. |
| Paginacion DynamoDB | `LastEvaluatedKey` se mapea 1:1 a un cursor opaco en query string; trivial. | Hay que modelar `@connection`/cursors en el schema GraphQL. |
| Presigned URL | Un resolver Lambda devuelve un string; encaja natural en REST `GET /clips/{event_id}/url`. | Forzar un campo "computed" con resolver Lambda; mas ceremonia. |
| Coste / superficie | 2 Lambdas + 1 HTTP API + authorizer. Minimo. | VTL/JS resolvers, schema, data sources; mas piezas que mantener. |
| Consistencia con el repo | El proyecto ya es Lambda-first (`cam-counter-ingest-events`); reusa patrones, IAM least-privilege por funcion y terraform por servicio. | Introduce un paradigma nuevo (SDL, resolvers) sin necesidad. |
| Tiempo real | El "contadores en vivo" (opcional) se cubre con polling cada 5-10 s o, si se quiere push, IoT Core ya tiene los topics; no requiere AppSync subscriptions. | Su unica ventaja (subscriptions) ya la cubre IoT Core. |

**Conclusion:** las consultas son pocas, fijas y tabulares; AppSync aporta complejidad (SDL, resolvers, suscripciones) que no se necesita. HTTP API + Lambda es mas barato, mas simple, coherente con la arquitectura Lambda-first existente, y el push en vivo (si se hace) se resuelve con IoT Core MQTT-over-WSS usando las credenciales del Identity Pool, no con AppSync.

El **frontend Next.js nunca habla DynamoDB/S3 directo** (regla de seguridad seccion 6 del modelo): todo via la API autenticada con JWT Cognito.

---

## 1. Estructura de paginas / rutas Next.js (App Router)

```
web/dashboard/
├── app/
│   ├── layout.tsx                      # AmplifyProvider + Authenticator guard global
│   ├── globals.css                     # Tailwind
│   ├── page.tsx                        # / -> redirect a /fleet
│   ├── login/page.tsx                  # Hosted-UI redirect / Authenticator embebido
│   ├── fleet/
│   │   ├── page.tsx                    # (a) LISTA DE FLOTA: todos los devices, estado, last_seen, version, sitio
│   │   └── loading.tsx
│   ├── devices/
│   │   └── [deviceId]/
│   │       ├── page.tsx                # (b) detalle device: contadores + tabla EVENTOS paginada
│   │       ├── live/page.tsx           # (d) opcional: contadores en vivo (MQTT/poll)
│   │       └── events/
│   │           └── [eventId]/page.tsx  # (c) detalle evento + <video> con presigned URL
│   └── api/                            # BFF route handlers (opcional, ver 2.3)
│       └── health/route.ts
├── components/
│   ├── FleetTable.tsx                  # online/offline badge, last_seen relativo, version, site
│   ├── DeviceHeader.tsx                # contadores agregados (in/out) del device
│   ├── EventsTable.tsx                 # paginada por cursor, click -> evento
│   ├── ClipPlayer.tsx                  # <video controls src={presignedUrl}>
│   └── LiveCounters.tsx               # suscripcion MQTT-WSS (opcional)
├── lib/
│   ├── amplify-config.ts               # Auth (Cognito) + custom API endpoint
│   ├── api-client.ts                   # fetch con Authorization: Bearer <idToken>
│   ├── auth-server.ts                  # runWithAmplifyServerContext (SSR token)
│   └── types.ts                        # DeviceItem, CrossingEvent (espejo de los contracts)
├── amplify.yml                         # CI build spec (en la raiz de la app)
├── next.config.js
├── package.json
└── tsconfig.json
```

**Rendering:** la lista de flota y la de eventos se renderizan **server-side** (RSC) llamando a la API con el `idToken` del request (cookies via `@aws-amplify/adapter-nextjs`), para no exponer datos ni hacer waterfalls en cliente. El `ClipPlayer` y `LiveCounters` son **client components** (necesitan `<video>` interactivo / WebSocket). El detalle de evento pide la presigned URL **on-demand al hacer click / al montar**, nunca en build (la URL caduca).

---

## 2. Capa de datos (API Gateway HTTP + Lambda)

### 2.1 Recursos

- **HTTP API** `cam-counter-fleet-api`, stage `prod`, autorizador **JWT** `cam-counter-fleet-cognito-authorizer` apuntando al User Pool `cam-counter-fleet-users` (issuer + audience = app client). Todas las rutas exigen JWT valido salvo `OPTIONS`/health.
- **Lambda `cam-counter-fleet-api`** (Node 20 o Python 3.12): maneja `GET /devices`, `GET /devices/{deviceId}`, `GET /devices/{deviceId}/events`. Solo **lectura**.
- **Lambda `cam-counter-clip-presign`**: `GET /clips/url?key=...` -> presigned GET (TTL 300 s).
- CORS: `allow_origins = [https://main.<appid>.amplifyapp.com, https://fleet.cam-counter.<dominio>]`, `allow_methods=[GET]`, `allow_headers=[authorization]`.

### 2.2 Endpoints

| Metodo / ruta | Hace | Acceso DynamoDB / S3 |
|---|---|---|
| `GET /devices` | Lista TODA la flota. Opcional `?channel=stable` (usa GSI1 `CHANNEL#{release_channel}`) o `?site=`. | `Query` GSI1 / `Scan` acotado de `cam-counter-devices`. Devuelve `device_id, site_id, status, last_seen_at, reported_version, release_channel, camera_ids, online` (online derivado: `status=='online' && now-last_seen_at < 90s`). |
| `GET /devices/{deviceId}` | Detalle + contadores agregados (in/out hoy / total). | `GetItem` device + `Query` de hoy en `cam-counter-events` por las PKs `CAM#{site}#{device}#{cam}` de sus `camera_ids`. |
| `GET /devices/{deviceId}/events?camera=&limit=50&cursor=<b64>` | EVENTOS paginados, mas recientes primero. | `Query` `cam-counter-events` `PK=CAM#{site}#{device}#{camera}`, `ScanIndexForward=false`, `Limit`, `ExclusiveStartKey` = cursor decodificado. Devuelve `{items, next_cursor}`. |
| `GET /clips/url?key={clip_key}` | Presigned GET del clip. | Valida que `key` empieza por `media/` y matchea `^media/[a-z0-9-]+/[a-z0-9-]+/...\.(mp4\|jpg\|gif)$`; `s3:GetObject` presign sobre `cam-counter-media-950639281773`. |

**Paginacion (cursor opaco):** `LastEvaluatedKey` de DynamoDB -> `base64url(JSON)` en `next_cursor`. El cliente lo reenvia tal cual; la Lambda lo decodifica a `ExclusiveStartKey`. Cursor ausente = primera pagina. Esto da paginacion **forward infinita** consistente con la SK `TS#{ts_event_ms:013d}#{event_id}` (orden temporal estable).

**Idempotencia/seguridad de `clip_key`:** el presign **nunca** acepta un bucket o prefijo arbitrario; solo firma claves bajo `media/` del bucket de clips. El device subio el clip write-only (IoT creds), el dashboard lo lee con presigned GET firmada server-side — el browser nunca ve credenciales S3.

### 2.3 BFF opcional (route handlers Next)

Para SSR limpio, los RSC pueden llamar directo al HTTP API con el `idToken` del usuario (via `@aws-amplify/adapter-nextjs`). Si se quiere ocultar el endpoint y centralizar el `Authorization`, se anade un thin proxy en `app/api/*/route.ts` que reenvia el JWT. Por defecto: **RSC -> HTTP API directo** (menos saltos).

### 2.4 IAM least-privilege

- `cam-counter-lambda-fleet-api-role`: `dynamodb:Query`/`GetItem`/`BatchGetItem` sobre `cam-counter-events`, `cam-counter-devices` y sus GSI; **sin** Put/Update/Delete; logs. (Reusa el patron del modelo de seguridad seccion 6.)
- `cam-counter-lambda-clip-presign-role`: solo `s3:GetObject` sobre `arn:aws:s3:::cam-counter-media-950639281773/media/*`, `aws:SecureTransport=true`; logs. Sin acceso a `fleet-releases`/`tfstate`/`rpi-artifacts`.

---

## 3. Autenticacion Cognito via Amplify Auth

### 3.1 Recursos (terraform, modulo `cognito-fleet`)

| Recurso | Nombre |
|---|---|
| User Pool | `cam-counter-fleet-users` — `AdminCreateUserConfig` (sin self-signup), email como username, MFA TOTP, password fuerte, refresh-token rotado, tokens de vida corta. |
| App client (SPA) | `cam-counter-fleet-web-client` — **sin secret**, Auth Code + PKCE, callback/logout = URLs de Amplify + dominio custom. |
| Identity Pool | `cam-counter-fleet-identity` — federa el pool; rol autenticado **read-only** (solo se usa para el push MQTT-WSS opcional, seccion 5). |
| Domain Hosted-UI | `cam-counter-fleet-950639281773` |
| Grupos | `cam-counter-operators` (lectura flota), `cam-counter-admins`. |

### 3.2 Integracion Amplify en Next.js

- `aws-amplify` + `@aws-amplify/adapter-nextjs`. `lib/amplify-config.ts` configura `Auth.Cognito` (userPoolId, userPoolClientId, region) y el endpoint del custom API.
- **Guard global** en `app/layout.tsx`: `<Authenticator>` (o redirect a Hosted UI). Sin sesion -> `/login`.
- **SSR:** `runWithAmplifyServerContext` lee el `idToken` de las cookies; cada `fetch` server-side a la API lleva `Authorization: Bearer <idToken>`. El JWT authorizer del HTTP API valida firma/issuer/audience/exp — backend stateless, sin sesiones propias.
- **Cliente:** `api-client.ts` adjunta `(await fetchAuthSession()).tokens.idToken` a cada request.
- Config (userPoolId, clientId, identityPoolId, apiUrl) inyectada por **env vars de Amplify** en build (`NEXT_PUBLIC_*`), no hardcodeada.

---

## 4. Reproduccion del video (presigned URL + `<video>`)

Flujo al abrir `/devices/{deviceId}/events/{eventId}`:

1. El evento (ya cargado en la tabla o re-fetched) trae `clip_key` y `clip_status`.
2. Si `clip_status != 'uploaded'` o `clip_key == null` -> placeholder "clip pendiente" (el clip sube async a S3; orden tolerante segun el contrato).
3. Si `uploaded`: client component pide `GET /clips/url?key={clip_key}` -> `{url, expires_in}` (TTL 300 s).
4. `<video controls preload="metadata" src={url} />`. El MP4 (H.264) reproduce nativo; S3 soporta Range requests para seek.
5. **Refresh on-expiry:** si el `<video>` emite `error` o el usuario vuelve tras >5 min, se re-pide la URL (la vieja caduco). Nunca se cachea la presigned URL en estado persistente.

```tsx
// components/ClipPlayer.tsx (client)
'use client';
export function ClipPlayer({ clipKey }: { clipKey: string }) {
  const [url, setUrl] = useState<string | null>(null);
  const load = useCallback(async () => {
    const r = await apiGet(`/clips/url?key=${encodeURIComponent(clipKey)}`);
    setUrl(r.url);
  }, [clipKey]);
  useEffect(() => { load(); }, [load]);
  if (!url) return <Spinner/>;
  return <video controls preload="metadata" src={url} onError={load} className="w-full rounded-lg" />;
}
```

El bucket `cam-counter-media-950639281773` sigue **privado, BlockPublicAccess all, SSE-S3, deny non-TLS**; la unica via de lectura del browser es la presigned GET. No se monta CloudFront (clips se ven 1-a-1, no hay hot-path masivo); si en futuro se quiere caching/edge, se anade un CloudFront con OAC + signed URLs como evolucion.

---

## 5. (Opcional d) Contadores en vivo

Dos opciones, ambas sin AppSync:

- **Simple (recomendada para v1):** polling de `GET /devices/{deviceId}` cada 5-10 s (SWR/`refetchInterval`). El `last_seen_at` + heartbeat ya en `cam-counter-devices` da near-real-time barato.
- **Push real:** `LiveCounters` (client) usa el **Identity Pool** (`cam-counter-fleet-identity`) para obtener credenciales temporales y conectar a **IoT Core via MQTT-over-WSS (SigV4)**, suscribiendose a `cam-counter/{device_id}/telemetry/heartbeat` y `.../events/crossing` (read-only para el rol authenticated). El rol authenticated permite `iot:Connect`/`iot:Subscribe`/`iot:Receive` solo sobre topics de lectura de flota. Esto reusa la topologia IoT ya disenada, sin servidor de sockets propio.

---

## 6. Hosting / CI en Amplify

### 6.1 Amplify App

- App `cam-counter-fleet-console`, plataforma **WEB_COMPUTE** (Next.js SSR/RSC). Branch `main` -> env `prod` (autobuild on push); ramas de feature -> **preview deployments**.
- Dominio custom `fleet.cam-counter.<dominio>` (managed cert ACM via Amplify).
- **Monorepo:** Amplify apunta a la subcarpeta `web/dashboard` (`AMPLIFY_MONOREPO_APP_ROOT=web/dashboard`); el `appRoot` en `amplify.yml` evita rebuilds del resto del repo.

### 6.2 `amplify.yml` (en `web/dashboard/`)

```yaml
version: 1
applications:
  - appRoot: web/dashboard
    frontend:
      phases:
        preBuild:
          commands:
            - npm ci
        build:
          commands:
            - npm run build
      artifacts:
        baseDirectory: .next
        files:
          - '**/*'
      cache:
        paths:
          - node_modules/**/*
          - .next/cache/**/*
```

### 6.3 Conexion al repo + env

- Amplify conectado a `github.com/jlsaco/cam-counter` (GitHub App), watch path `web/dashboard/**` para no disparar builds en cambios de edge/terraform.
- **Env vars** (Amplify console / terraform `environment_variables`, branch `main`):
  - `NEXT_PUBLIC_API_URL=https://<httpapi-id>.execute-api.us-east-1.amazonaws.com/prod`
  - `NEXT_PUBLIC_COGNITO_USER_POOL_ID`, `NEXT_PUBLIC_COGNITO_CLIENT_ID`, `NEXT_PUBLIC_IDENTITY_POOL_ID`
  - `NEXT_PUBLIC_AWS_REGION=us-east-1`, `NEXT_PUBLIC_IOT_ENDPOINT=a3l2e1ervttr3a-ats.iot.us-east-1.amazonaws.com` (solo si push MQTT)
  - Ningun secreto AWS (no access keys). El SSR usa el JWT del usuario, no un rol de servicio con llaves.

---

## 7. Infra Terraform / Amplify necesaria

Modulos nuevos en `terraform/modules/`, instanciados desde `terraform/environments/prod/main.tf`. Todo **aditivo**; `default_tags` aplica `Project=cam-counter, Environment=prod, ManagedBy=terraform, Repo=jlsaco/cam-counter, CostCenter=cam-counter`; cada recurso anade `Component=fleet-console|api`.

| Modulo nuevo | Crea |
|---|---|
| `terraform/modules/cognito-fleet` | `aws_cognito_user_pool` (`cam-counter-fleet-users`), user pool client SPA, domain, identity pool, grupos, rol authenticated read-only (+ IoT subscribe si push). |
| `terraform/modules/fleet-api` | `aws_apigatewayv2_api` (HTTP) `cam-counter-fleet-api` + stage `prod` + JWT authorizer; 2 Lambdas (`cam-counter-fleet-api`, `cam-counter-clip-presign`) + sus roles least-privilege; integraciones + rutas + CORS. |
| `terraform/modules/amplify-fleet-console` | `aws_amplify_app` (`cam-counter-fleet-console`, repo, oauth token via secret/SSM, build_spec, `AMPLIFY_MONOREPO_APP_ROOT`), `aws_amplify_branch` (`main`, env vars), `aws_amplify_domain_association`. |

Lectura cruzada via `terraform_remote_state` o data sources sobre los recursos existentes (tablas, bucket de clips) — **solo referencias, sin modificarlos**.

**GHA / MAD:** se extienden los permisos del rol `cam-counter-gha-deploy` (OIDC) para crear/gestionar Apigateway, Lambda, Cognito y Amplify scoped a `cam-counter-*`; CI sigue plan-only y MAD aplica `terraform apply -auto-approve`. El estado es **monotono**: el plan se aborta si intenta destroy/replace de cualquier recurso preexistente.

**Outputs** que consume Amplify (encadenados en `environments/prod/outputs.tf`): `fleet_api_url`, `user_pool_id`, `web_client_id`, `identity_pool_id`, `amplify_app_id`, `amplify_default_domain`.

---

## 8. Orden de PRs apilados (no big-bang)

1. **PR00** (sobre `main`): `terraform/modules/cognito-fleet` + instancia en prod. Crea el User Pool y un usuario operador admin.
2. **PR01** (sobre PR00): `terraform/modules/fleet-api` (HTTP API + 2 Lambdas read-only + presign + authorizer). Validable con `curl` + JWT, sin UI.
3. **PR02** (sobre PR01): `web/dashboard/` (scaffold Next.js: flota, eventos, ClipPlayer) consumiendo la API.
4. **PR03** (sobre PR02): `terraform/modules/amplify-fleet-console` + `amplify.yml` + conexion repo + env + dominio.
5. **PR04** (opcional, sobre PR03): contadores en vivo (Identity Pool IoT-WSS o polling) + dominio custom + hardening (HSTS, CSP).

Merge no-squash; cada PR referencia su issue de GitHub. Ningun PR toca el camino edge ni el ingest MQTT->Lambda->DynamoDB existente; el dashboard es **solo lectura** sobre los datos que ya escribe la Lambda `cam-counter-ingest-events`.

---

Archivos de referencia que fundamentan este diseno (en el repo montado): `/Users/jose.salamanca/Documents/code/personal/cam-counter/contracts/crossing_event.schema.json` (claves `clip_key`, `ts_event_ms`, `direction`, `event_id`), `/Users/jose.salamanca/Documents/code/personal/cam-counter/contracts/device_registry_item.schema.json` (`status`, `last_seen_at`, `reported_version`, `release_channel`, `camera_ids`), `/Users/jose.salamanca/Documents/code/personal/cam-counter/terraform/modules/` (patron de modulos por servicio) y `/Users/jose.salamanca/Documents/code/personal/cam-counter/terraform/environments/prod/` (instanciacion + backend remoto).