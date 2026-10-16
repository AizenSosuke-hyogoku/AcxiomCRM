# AcxiomCRM — Pulse Intelligence

**A role-aware CRM for Sahyadri Cloudworks, a fictional Pune-based B2B firm.** Built for the ACXIOM technical assessment with a focus on a dependable end-to-end flow, practical security, explainable ML, and a visible personal point of view.

> Demo data is fictional. Scores are trained on synthetic examples and are for prioritisation only, not validated real-world conversion probabilities.

> **Stack note:** the detailed evaluator document names ASP.NET Core Identity/Razor in a few sections, while the attached master-build note explicitly allows an all-Python implementation when .NET 8 is unavailable. This submission uses FastAPI/SQLAlchemy with the same role, validation, CSRF, lockout, audit, API and workflow behaviours. If the evaluator treats ASP.NET Core as a strict technology gate rather than a suggested implementation, this is a stack deviation to disclose honestly.

## Run it with Docker (recommended)

1. Install Docker Desktop and start it.
2. From this directory, copy `.env.example` to `.env`; replace `SESSION_SECRET` with a long random value. On PowerShell: `Copy-Item .env.example .env`.
3. Run `docker compose up --build` and open **http://localhost:8080**.

The web app stores its SQLite file in a named Docker volume. The AI service is available only on the internal Compose network. To stop, run `docker compose down`; to reset the seeded database, run `docker compose down -v` (this deletes the local demo database).

## Run locally without Docker

Use Python 3.12 (recommended). On Windows PowerShell, the included `run-local.ps1` creates a virtual environment, installs the dependencies, starts both services in separate windows, and prints the local URL. If PowerShell blocks scripts, run `Set-ExecutionPolicy -Scope Process Bypass` in that terminal, then `./run-local.ps1`.

Manual steps: use Python 3.11+ (Python 3.12 is the recommended container runtime). In a terminal at the project root:

```powershell
py -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
pip install -r ai_service/requirements.txt
Copy-Item .env.example .env
```

Start the AI service in terminal 1:

```powershell
cd ai_service
python -m uvicorn main:app --reload --port 8001
```

Start the web app in terminal 2 from the project root:

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload --port 8080
```

Then open **http://localhost:8080**. The local app reads `.env`; the default AI URL is `http://127.0.0.1:8001`. The first startup creates the database and synthetic seed records automatically.

## Demo logins

| Role | Email | Password |
|---|---|---|
| Admin | `admin@acxiomcrm.demo` | `Admin@12345!` |
| Manager | `manager@acxiomcrm.demo` | `Manager@12345!` |
| Sales Executive | `sales@acxiomcrm.demo` | `Sales@12345!` |
| Additional Sales Executive | `isha@acxiomcrm.demo` | `DemoRep@12345!` |
| Additional Sales Executive | `kabir@acxiomcrm.demo` | `DemoRep@12345!` |

Change/remove demo accounts and rotate the session secret before any deployment. Do not expose this demo instance to the public internet with known demo credentials.

## Implemented features

- **Authentication:** login, sales-only self-registration, logout, Argon2id password hashes, mixed-case/number/symbol 12-character policy, five-failure/15-minute lockout, session cookie and CSRF checks.
- **Roles:** Admin full access; Manager team-wide CRM/report access but no user administration; Sales Executive sees owned records. Assignment is checked server-side, not only in navigation.
- **CRM workflows:** create/edit/deactivate customer, lead, opportunity, follow-up and activity records; qualified lead conversion into a customer plus opportunity; search and status/stage/owner filters.
- **Validation:** customer email/mobile validation and uniqueness, duplicate warning, numeric ranges, permitted status sets, opportunity amount/probability/date rules and follow-up-date rules are checked server-side.
- **Dashboard:** eight role-scoped KPI cards, Chart.js lead-status, opportunity-stage and six-month won-sales charts, daily brief and Deal Risk Radar.
- **REST API:** DTO-only customer GET/GET by id/POST/PUT/DELETE, lead GET/POST, opportunity GET/POST, authentication checks, CSRF-protected writes and relevant status codes.
- **Audit:** successful/failed login, lockout, logout, CRUD/deactivation, role changes, user status changes, conversion, registration, customer export/anonymisation and AI auto-assignment events. Audit page is Admin-only.
- **AI/ML sidecar:** synthetic data training and Random Forest scoring, human-readable reasons, Hot/Warm/Cold band, workload-based assignment, privacy-aware hover summary, ten-minute summary cache and deterministic fallback if the sidecar or optional LLM is unavailable.
- **Local-first privacy:** AI summary requests use HMAC-pseudonymous account aliases and numeric/status signals, not contact details or free-text activity notes. Customer JSON export and Admin-only right-to-be-forgotten anonymisation are included; linked financial records remain attached.
- **Indian context / product identity:** Pune/Maharashtra seed locations, GSTIN-shaped placeholders, +91 mobile presentation and INR/L/Cr formatting.

## Important endpoints

| Method | Endpoint | Purpose |
|---|---|---|
| GET | `/health` | Web health |
| POST | `/api/auth/login` | Session-based API login; returns role and CSRF token |
| POST | `/api/auth/logout` | CSRF-protected session logout |
| GET | `/api/csrf` | Get CSRF token for authenticated API writes |
| GET/POST/PUT/DELETE | `/api/customers` and `/api/customers/{id}` | Customer DTO API |
| GET/POST | `/api/leads` | Lead DTO API |
| GET/POST | `/api/opportunities` | Opportunity DTO API |
| GET | `/api/ai/summary?entity_type=lead&entity_id=1` | Scoped summary for hover card |
| GET | `/docs` | FastAPI OpenAPI UI |
| GET | `http://localhost:8080` | CRM web app |

Protected APIs use the signed session cookie in this assessment build; write requests also require `X-CSRF-Token` from `/api/csrf`. This is intentionally not documented as a JWT implementation.

## AI/ML design

1. The sidecar generates 2,400 synthetic lead records and labels them with a documented rule plus controlled noise.
2. A scikit-learn pipeline uses categorical one-hot encoding, numeric standardisation and a Random Forest classifier. It is saved with joblib and pre-trained during the AI Docker image build; local first run trains it if the artifact is missing.
3. The scoring endpoint returns score, band, short reasons and which inference path was used.
4. If the model cannot train or the sidecar cannot be reached, deterministic weighted scoring remains available.
5. A summary endpoint uses factual templates by default. An optional `ANTHROPIC_API_KEY` enables a short, time-limited Claude request; the UI does not require it.
6. Assignment minimises open workload with a small prior-win tie-breaker and respects a demo capacity cap.

To inspect the sidecar when using Docker, `docker compose logs ai-service`; it does not expose a host port by default. In local mode it responds at `http://127.0.0.1:8001/health`.

## Acceptance test checklist

1. Open `/customers` while logged out: the app redirects to login.
2. Sign in as each role and compare the dashboard and visible records.
3. Try an invalid customer email or mobile number: the server rejects it and displays a message.
4. Create a customer, then try to reuse the same email/phone: duplicate creation is blocked.
5. Create an active opportunity with amount `0`, probability above `100`, or a past close date: the server rejects it.
6. Create a planned follow-up dated yesterday: the server rejects it.
7. Sign in as Sales Executive and attempt a forged `assigned_to`: server-side ownership checks retain that user's scope.
8. Sign in as Admin, change a role, and inspect `/audit`.
9. Make five incorrect login attempts for a demo user and verify the lockout event appears in the audit trail.
10. Create a lead without selecting an owner as an Admin/Manager: auto-assignment chooses a rep and writes an audit reason.
11. Hover a lead/customer name: a summary and next action should appear; it must still work if the AI sidecar is stopped.
12. Sign in as Admin and call `/api/customers` from `/docs` after authenticating through the same browser session.
13. Verify all three charts render and the executive brief shows five signals.

## Tests

Install the development requirement set with `pip install -r requirements-dev.txt`, then run `python -m pytest -q`. The tests use FastAPI's TestClient and an isolated, seeded SQLite demo DB. For a clean acceptance pass, start with a fresh DB (remove `data/acxiomcrm.db` after stopping the app, or run `docker compose down -v`).

## Screenshots

The submission includes five UI snapshots in [`docs/screenshots/`](docs/screenshots/): sign-in, dashboard, leads, customers and audit trail. They are illustrative snapshots of the rendered templates with seeded demo data.

## Architecture & decisions

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) and [`docs/MY_DECISIONS.md`](docs/MY_DECISIONS.md). The latter explains the design tradeoffs in first person, including why the local-first hover summary is the signature feature.

## Known limitations (honest scope)

- This deadline-oriented build uses signed cookie sessions for protected APIs rather than JWT access/refresh tokens.
- Audit writes are synchronous instead of the brief's queued background writer.
- No Alembic migrations; SQLite tables are created automatically on first run.
- The record list is capped at 200 per view, and explicit page-number pagination is not yet implemented.
- The assignment policy is a simple explainable heuristic; it needs validation on real sales outcomes before production use.
- Optional external LLM use requires an API key and internet access; all core AI features have no-key fallbacks.
- No real message/email reminders, production monitoring, account recovery flow, or live cloud deployment is included.

## AWS deployment outline (not executed)

Use a small Ubuntu EC2 or Lightsail instance, install Docker Engine and Compose, clone/copy the project, set a strong `.env` secret, and run `docker compose up -d --build`. Put Nginx or an AWS load balancer with HTTPS in front; only expose ports 80/443 through the security group. Do not expose the AI sidecar publicly. For a production scale-up, replace SQLite with PostgreSQL/RDS, add Redis if cache sharing is needed, introduce migrations and durable background audit processing, and deploy containers using ECS or a managed container platform.

The authenticated AI scoring endpoint `/api/ai/rescore-all` is available to Admin/Manager sessions and requires the current CSRF token in `X-CSRF-Token`; it recalculates the caller's authorised lead scope and audits the batch.
