# Repository Guidelines

Enterprise AI Agent platform: FastAPI + AgentScope 2.0 backend, React + TypeScript frontend, SQLite storage, served behind nginx.

## Project Structure
- `backend/` – FastAPI service. `app/api/v1/` holds routers, `app/models/` DB models, `app/schemas/` Pydantic validation, `app/services/` business logic, `app/core/` auth/security, `app/agents/` AgentScope integration, `app/main.py` entrypoint. Runtime data lives in `knowledge/` (skill packages), `uploads/`, `outputs/`, `traces/`.
- `frontend/` – React 18 + Ant Design + Zustand, built with Vite. One folder per page under `src/pages/<Feature>/index.tsx` (e.g. `Chat`, `Admin`, `SkillSquare`), API calls in `src/api/`, state in `src/store/`.
- `test/` – Playwright E2E scripts (`_test_*.mjs`, `_diag_*.mjs`, `_probe_*.js`).
- `nginx/nginx.conf` – reverse-proxy config for deployment.

## Build, Test, and Development Commands
Backend (Python, conda env `agentscope`):
- `cd backend && python init_db.py` – create tables and the admin account.
- `python -m uvicorn app.main:app --reload --port 8000` – run the API; Swagger docs at `http://localhost:8000/docs`.

Frontend:
- `cd frontend && npm install` – install dependencies.
- `npm run dev` – Vite dev server on port 5173; proxies `/api` and `/api/v1/ws` to `localhost:8000`.
- `npm run build` – type-check (`tsc`) then build to `dist/`.

Tests: run Playwright scripts directly, e.g. `node test/_test_skills2.mjs` (backend and frontend must be running).

## Coding Style & Naming
- Python: PEP 8, snake_case; Pydantic v2 for request/response schemas. No autoformatter is configured – match surrounding code.
- TypeScript/React: 2-space indent, PascalCase component folders with `index.tsx` plus a colocated `index.css`; shared helpers in `src/utils/`.
- DB schema changes ship as numbered `backend/migrate_*.py` scripts; run them in order after pulling.

## Testing Guidelines
There is no unit-test framework. Verification uses the Playwright scripts in `test/` and `backend/_e2e_v2_test.py`. Name new scripts `_test_<feature>.mjs` and keep them runnable with plain `node`. No coverage target; prioritize flows that touch chat, skills, and permissions.

## Commit & Pull Request Guidelines
- Commits use type prefixes observed in history: `feat:`, `fix:`, optional scope (`feat(skill): ...`); descriptions may be in Chinese. Reverts use `Revert "..."`.
- Keep each commit focused on one change and name the affected area (frontend/backend) in the message.
- PRs: describe what changed and why, attach screenshots for UI changes, confirm `npm run build` passes, and list the Playwright scripts used to verify.

## Configuration & Secrets
- Backend config is `backend/.env` (requires `DASHSCOPE_API_KEY`); never commit `.env` or `*.db` files.
- Runtime directories (`backend/uploads`, `outputs`, `knowledge`, `traces`) hold user/agent data, not source – do not hand-edit their contents.
