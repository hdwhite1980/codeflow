/** @type {import('next').NextConfig} */
const nextConfig = {
  // Standalone output makes the Railway Docker image smaller — only
  // what's actually used is bundled into the runtime layer. Without
  // this, the production image carries the whole node_modules tree
  // (~300MB) instead of the trace-pruned ~80MB.
  output: "standalone",

  // The frontend hits the FastAPI service over public HTTPS, not via
  // a Next.js rewrite. We could rewrite /api -> backend to keep things
  // same-origin (avoids CORS), but the backend already has CORS open
  // and the cleaner separation is to keep the API URL explicit in
  // client code. NEXT_PUBLIC_API_URL is the contract.

  // No image domains needed — we don't fetch remote images.
  reactStrictMode: true,
};

export default nextConfig;
