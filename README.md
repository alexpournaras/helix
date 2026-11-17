# Helix

Admin panel for a biomedical research platform. Early development.

## Stack

- Frontend: React, Vite
- Backend: Python 3.11+, FastAPI

## Getting started

Requires Node.js 20+ and Python 3.11+.

```bash
npm run setup   # npm install + backend/.venv + pip install
npm run dev     # API on :8000, web on :5173
```

Open http://127.0.0.1:5173.

API keys go in `backend/.env`; see `backend/.env.example`.

## Scripts

| Command           | What it does                               |
| ----------------- | ------------------------------------------ |
| `npm run setup`   | Install Node and Python dependencies       |
| `npm run dev`     | Run API and web dev servers together       |
| `npm run dev:web` | Frontend only                              |
| `npm run dev:api` | Backend only                               |
| `npm run build`   | Build the frontend into `dist/`            |
| `npm start`       | Build, then serve the app from the backend |
