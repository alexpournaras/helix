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

### OpenAI usage note

Eligible accounts can receive free daily usage for API traffic shared with OpenAI:

- Up to 250,000 tokens per day across GPT-5.4, GPT-5.2, GPT-5.1, GPT-5, GPT-4.1, GPT-4o, o1, and o3.
- Up to 2.5 million tokens per day across GPT-5.4 mini, GPT-5.4 nano, GPT-5 mini, GPT-5 nano, GPT-4.1 mini, GPT-4.1 nano, GPT-4o mini, o3-mini, and o4-mini.

Usage beyond these limits, and usage of other models, is billed at standard rates. Additional limitations may apply. Eligibility and limits are controlled by OpenAI and may change; check the OpenAI platform for current terms.

### AI literature access

Chat models have read-only tools for the literature saved in Helix. They can search the local library and list recently imported papers. Abstracts are the default reading scope; full text, figures/tables, and references/citations are accessed only when the user explicitly requests them. Figure access currently provides stored captions, image URLs, and table data rather than visual pixel inspection. Answers show the literature records used and models are instructed to cite them as `[Literature #ID]`. Tool calls cannot add, edit, refresh, or delete literature. Chat can also list, search, and read notes, and can create, edit, or delete notes only when the user explicitly requests that exact action. Multiple notes can be created in one transaction. Dedicated tag tools preserve the rest of a note and reject duplicate additions or missing-tag removals. Note tags are independent from literature tags and require no matching paper. Confirmations such as “all of them,” “yes,” “save it,” and “try again” inherit the most recent unresolved note request across drafting turns. Helix injects that resolved request into the model context, while successful mutations clear it to prevent replays.

## Scripts

| Command           | What it does                               |
| ----------------- | ------------------------------------------ |
| `npm run setup`   | Install Node and Python dependencies       |
| `npm run dev`     | Run API and web dev servers together       |
| `npm run dev:web` | Frontend only                              |
| `npm run dev:api` | Backend only                               |
| `npm run build`   | Build the frontend into `dist/`            |
| `npm start`       | Build, then serve the app from the backend |
