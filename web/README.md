# codeflow-web

Next.js 14 frontend for Code Flow. Talks to the FastAPI backend over
REST for fetches and WebSocket for live build/audit events.

## Local dev

```bash
cd web
npm install
NEXT_PUBLIC_API_URL=https://codeflow-production-27c6.up.railway.app npm run dev
```

Opens on http://localhost:3000.

If the API URL is missing, all pages will render an error — the frontend
won't fall back to a guess.

## Deploy on Railway

1. In the Railway project, create a new service from the same GitHub
   repo as the FastAPI service.
2. **Root directory:** `/web` (so this Dockerfile and railway.toml are used).
3. Set environment variable: `NEXT_PUBLIC_API_URL` to the FastAPI
   public URL (e.g. `https://codeflow-production-27c6.up.railway.app`).
4. Deploy. Railway gives the service its own public URL on the same
   project.

The two services are independent: redeploys of either don't affect the
other. Logs are separate. They share Redis (the backend uses pub/sub
for live events; the frontend doesn't touch Redis directly).

## Structure

```
src/
├── app/                  # Next.js app router
│   ├── page.tsx          # / — project list + new build form
│   ├── projects/[id]/    # /projects/<id> — live build view
│   ├── layout.tsx
│   └── globals.css
├── components/
│   ├── ui/               # shadcn primitives
│   └── *.tsx             # higher-level components
└── lib/
    ├── api.ts            # REST client
    ├── ws.ts             # WebSocket hook
    ├── types.ts          # TS interfaces mirroring FastAPI responses
    ├── format.ts         # money, time, token formatters
    └── utils.ts          # cn() helper
```

## Styling

Tailwind + shadcn/ui, dark only. CSS variables live in `globals.css`.
If you want to add a light theme, follow standard shadcn approach: copy
the light variant alongside `:root` and toggle a class on `<html>`.
