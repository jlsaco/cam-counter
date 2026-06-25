/**
 * Next.js config de la consola de flota (App Router).
 *
 * La SPA es READ-ONLY y NUNCA habla DynamoDB/S3 directo: toda la lectura va por la API de WP11
 * (HTTP API + authorizer JWT Cognito). Toda la configuración pública entra por `NEXT_PUBLIC_*`
 * (endpoint de la API, IDs de Cognito); ningún secreto AWS vive aquí ni se commitea.
 *
 * `output: 'standalone'` produce un servidor Node autónomo apto para el hosting de Amplify
 * (SSR/Server Components) — ver `amplify.yml`.
 */
/** @type {import('next').NextConfig} */
const nextConfig = {
  output: "standalone",
  reactStrictMode: true,
};

module.exports = nextConfig;
