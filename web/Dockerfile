# Stage 1: install deps and build
FROM node:20-alpine AS builder

WORKDIR /app

# Copy manifest first for better Docker cache hit rate — only re-installs
# when package.json or lockfile changes, not on every source edit.
COPY package.json package-lock.json* ./

# Use ci when a lockfile is present; otherwise fall back to install.
RUN if [ -f package-lock.json ]; then \
      npm ci; \
    else \
      npm install; \
    fi

COPY . .

# next.config.mjs sets output:'standalone', which produces .next/standalone
# with a self-contained server.js plus only the node_modules actually used.
# This drops image size from ~600MB to ~150MB.
RUN npm run build

# Stage 2: runtime
FROM node:20-alpine AS runner

WORKDIR /app

ENV NODE_ENV=production
# Default port; Railway overrides via $PORT. Next.js binds to whatever
# we pass via the -p flag in `next start`.
ENV PORT=3000

# Copy only what's needed to run. The standalone output is
# self-contained — server.js plus the trace-pruned node_modules.
COPY --from=builder /app/.next/standalone ./
# Static assets (CSS, JS chunks) aren't in standalone; copy separately.
COPY --from=builder /app/.next/static ./.next/static
# public/ would also go here if we had public assets — we don't yet,
# so skip that copy.

EXPOSE 3000

# server.js is at the root of the standalone output. It reads PORT from
# the environment, so Railway's port assignment Just Works.
CMD ["node", "server.js"]
