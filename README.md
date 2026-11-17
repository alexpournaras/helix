# Helix

Admin panel for a biomedical research platform. Named after the DNA double helix, which
animates on the dashboard.

The panel is an empty shell for now. Projects, literature, datasets and AI runs are
placeholders. What works today:

- **Dashboard** with the animated helix, workspace counters, a setup checklist and backend status
- **Integrations**: the planned AI models (Ollama, OpenAI, Claude, Gemini) and data sources
  (PubMed, ClinVar, cBioPortal, UniProt, Ensembl, ClinicalTrials.gov), showing which API keys are present
- **Settings**: workspace details and model defaults, saved on the backend
- Dark theme, responsive down to 300px

## Stack

| Part     | Tech                                    |
| -------- | --------------------------------------- |
| Frontend | React 18, Vite, Sass, lucide-react      |
| Backend  | Python 3.11+, FastAPI, Uvicorn, Pydantic |

Python was chosen for the backend because the planned integrations live there: official SDKs for
Ollama, OpenAI and Anthropic, plus Biopython for NCBI/PubMed.

## Getting started

Requires Node.js 20+ and Python 3.11+.

```bash
git clone https://github.com/alexpournaras/helix.git
cd helix
npm run setup   # npm install + backend/.venv + pip install
npm run dev     # API on :8000, web on :5173
```

Open http://127.0.0.1:5173. API docs are at http://127.0.0.1:8000/docs.

`npm run dev` restarts the API whenever a file in `backend/app` changes. Vite hot-reloads the frontend.

### API keys

Copy `backend/.env.example` to `backend/.env` and fill in the providers you use. Keys are read by the
backend only; the frontend just sees whether each one is present.

### Production

```bash
npm start       # builds the frontend into dist/ and serves everything from FastAPI on :8000
```

## Scripts

| Command           | What it does                                  |
| ----------------- | --------------------------------------------- |
| `npm run setup`   | Install Node and Python dependencies          |
| `npm run dev`     | Run API and web dev servers together          |
| `npm run dev:web` | Frontend only                                 |
| `npm run dev:api` | Backend only                                  |
| `npm run build`   | Build the frontend into `dist/`               |
| `npm start`       | Build, then serve the app from the backend    |

## Project layout

```
src/
  App.jsx               shell: sidebar, top bar, client-side routing
  routes.js             navigation entries
  components/
    DnaHelix.jsx        the animated helix (styles/helix.scss)
    Logo.jsx            sidebar mark, same drawing as public/favicon.svg
  views/                Dashboard, Integrations, Settings, placeholders
  styles/app.css        theme and layout
backend/
  run.py                entry point
  app/main.py           routes: /api/health, /overview, /integrations, /ollama, /settings
  app/integrations.py   catalog of planned integrations
  app/store.py          workspace settings, saved to backend/data/settings.json
scripts/
  dev.mjs               runs both servers, restarts the API on change
  py.mjs                runs the venv's Python on any OS
```

`backend/data/` holds runtime data and is not committed.
