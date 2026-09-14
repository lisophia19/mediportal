# Mediportal Call Review Dashboard

Minimal React (Vite) dashboard for reviewing Vogent calls: login, call list,
call detail. See `docs/design/spec-ortho-baseline-demo.md` §9 for the spec.

## Run standalone

```bash
npm install
npm run dev
```

Opens at `http://localhost:5173`. By default it calls the backend at
`/api/v1` — set `VITE_API_BASE_URL` (e.g. in a local `.env`) to point at a
different backend host, such as `http://localhost:5000/api/v1`.

## Run via Docker Compose

From the repo root:

```bash
docker-compose up
```

## Build for production

```bash
npm run build
```

Outputs static files to `dist/`.
