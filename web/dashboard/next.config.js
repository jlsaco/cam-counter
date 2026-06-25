/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  // La consola es SOLO LECTURA y no embebe credenciales: toda la config sensible
  // (endpoints, Cognito) llega por variables NEXT_PUBLIC_* en build/runtime de Amplify.
  // No exponemos `env` aquí para no hornear valores; Next inlina NEXT_PUBLIC_* solo.
};

module.exports = nextConfig;
