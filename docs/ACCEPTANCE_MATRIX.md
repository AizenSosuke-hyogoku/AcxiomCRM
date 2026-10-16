# Acceptance / reviewer map

| What a reviewer wants to see | Where to demonstrate it |
|---|---|
| Login, logout, role-aware navigation | `/login`, `/logout`; demo credentials in `README.md` |
| Argon2id password hash and lockout | `app/main.py`: authentication endpoints and audit events |
| Customer / lead / opportunity / follow-up / activity workflows | `/customers`, `/leads`, `/opportunities`, `/followups`, `/activities` |
| Lead conversion workflow | Open a Qualified lead and use its conversion action |
| Client and server validation | Form constraints in `app/templates/module.html`; server checks in `validate_and_save`; DTO validation in API routes |
| Server-side owner scoping | `scoped_query`; Sales Executive form owner override; API `choose_api_owner` |
| KPI cards and three charts | `/dashboard`, `app/templates/dashboard.html`, `app/static/app.js` |
| Search and filtering | Module-specific list filters and query parameters |
| REST API and DTOs | `/docs`, `/api/customers`, `/api/leads`, `/api/opportunities` |
| Append-oriented audit trail | `/audit` as Admin; helper `audit` and CRUD action handlers |
| Explainable ML score | `ai_service/main.py` (`POST /ai/score`), `ai_service/train.py` |
| Quiet Account Alarm hover summary | Hover a lead/customer name; `/api/ai/summary` calls the sidecar with compact pseudonymised signals |
| Smart assignment | Create a lead without an owner as Admin/Manager; inspect assignment audit event |
| Deal Risk Radar and daily brief | Dashboard lower panels |
| PII export and forget workflow | `/customers` actions and `/customers/{id}/export` |
| Product ownership and tradeoffs | `docs/MY_DECISIONS.md` |
| Layered architecture | `docs/ARCHITECTURE.md` |
| Tests | `python -m pytest -q` |

This is a deadline-oriented student implementation. Read the **Known limitations** and **Stack note** in the README before presenting it as production ready or if the evaluator requires a literal ASP.NET Core Identity implementation.
