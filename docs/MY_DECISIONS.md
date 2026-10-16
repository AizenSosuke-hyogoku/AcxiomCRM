# My Decisions — AcxiomCRM / Pulse Intelligence

## The product point of view

I designed this as a CRM for **Sahyadri Cloudworks**, a fictional Pune-based B2B technology and services company. I did not want the project to feel like a generic customer table with a chart on top. The product question I chose is: *what should a sales person do next, and can they explain why?*

The working UI calls the decision-support layer **Pulse Intelligence**. The UI uses deep indigo for the workspace, teal for positive signals, and warm coral for risk. Seed data uses Indian cities/states, Indian mobile-number validation, INR formatting (including lakh and crore), and a GSTIN-shaped demo field. All names, accounts, contacts and tax identifiers in the demo are fictional placeholders.

## The feature I consider my signature

### Quiet Account Alarm

A lead/customer name can show a small on-hover account summary. It uses days since contact, overdue follow-ups, open pipeline and the stored lead score to produce a risk level and exactly one next action. The browser waits briefly before requesting it, the result is cached for ten minutes, and the AI request uses an alias like `Account 12` instead of a customer's name, email or phone. The summary still works if the AI service or external LLM is unavailable: a deterministic template produces the result.

I chose this because it connects the CRM data model to a concrete sales behaviour instead of adding AI as a disconnected chat box.

## AI/ML choices

- **Lead scoring:** a Random Forest pipeline is trained on 2,400 synthetic examples using one-hot encoding for categories and standard scaling for numeric features. The generator deliberately uses a documented rule plus noise; therefore the score is a demo prioritisation signal, not a claim about real conversion probability. The API returns a Hot/Warm/Cold band and three human-readable reasons.
- **Fallback:** if training or the service call fails, the web app switches to an understandable weighted score. No external API key is required for the core demo.
- **Smart assignment:** available sales executives are ranked using open workload, a simple prior-win proxy, and capacity. The API returns a reason with the decision; the assignment is audited. A production design should use territory, skills, fairness checks and actual outcomes.
- **Deal Risk Radar:** open deals with an overdue close date or a near close date and low probability are surfaced for a human review.
- **Sentiment tags:** activity sentiment is derived from a small, readable keyword rule. I kept it intentionally modest rather than calling it a trained language model.

## Architecture decision I can defend

I put lead scoring, summaries and assignment behind a **separate FastAPI AI service**. The web app owns authentication, authorisation, business rules, data and HTML; the AI service owns model inference and summary logic. A short network timeout plus a deterministic web-side fallback means an AI failure does not block normal CRM CRUD. This makes the boundary easy to explain and allows model retraining without redeploying the web layer.

SQLite keeps first-run setup simple. SQLAlchemy models, helper/service functions, HTML templates, DTO endpoints and validation are separated so the project can move to PostgreSQL and a more formal repository/service setup later.

## Security choices

- Passwords use Argon2id via the maintained Argon2 library; no password or hash is placed in an audit event or DTO.
- Five failed logins trigger a fifteen-minute lockout; authentication attempts and lockouts are audited.
- A signed cookie is used for this assessment-sized web app. State-changing form/API requests require a CSRF token.
- Sales Executive queries are filtered by owner in the server-side query helper; ownership cannot be reassigned by a forged sales-user form. User/role management and audit logs are Admin-only.
- Customer email and phone are unique at the database level and checked before saving; customer deletion is a soft-deactivation. Audit records are append-oriented and there is no audit mutation UI.
- Direct contact information and free-text notes are not sent to the AI summary service. Summary inputs use pseudonymous account aliases and numeric/status signals only.

## What I simplified under the deadline

This is a working, demo-oriented build, not a claim of production readiness. It uses cookie authentication for the REST endpoints instead of the stronger JWT access/refresh-token flow in the long-form brief. Audit writes are synchronous rather than a durable background queue. Records are deactivated instead of hard-deleted. List views cap results at 200 and do not yet provide database-backed pagination. The executive assignment heuristic is intentionally transparent but basic. There is no email/SMS reminder delivery, no real customer-data import, and no live deployment bundled with the code.

Before real use, I would add PostgreSQL, migrations, a durable audit queue, explicit service/repository boundaries, stronger API token separation, rate limiting, secure proxy/TLS configuration, backup/restore, operational monitoring, and tests for every role/resource combination.

## How to explain the project in an interview

1. The CRM has a trust boundary: the web service owns user identity and record-level access; the AI service receives only compact, pseudonymised signals.
2. The score is a prioritisation heuristic trained on synthetic labels, not a validated sales forecast. I expose reasons and a fallback instead of presenting it as certainty.
3. Auto-assignment balances workload and capacity, and the reason is recorded so a manager can challenge or improve the rule.
4. The best AI feature is tied to an existing sales workflow: the hover summary helps a rep notice silence/overdue work and select a next action.
