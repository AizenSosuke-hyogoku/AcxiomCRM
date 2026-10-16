from __future__ import annotations
import os, re, json, secrets, hmac, difflib, calendar
from datetime import datetime, date, timedelta
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any
import httpx
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, VerificationError
from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent / ".env")
from fastapi import FastAPI, Request, Depends, HTTPException, status
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from sqlalchemy import select, func, or_, and_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from pydantic import BaseModel, Field, ValidationError
from .db import Base, engine, get_db, SessionLocal
from .models import User, Customer, Lead, Opportunity, FollowUp, Activity, AuditLog
from .anonymize import outbound_summary_signals

ROOT = Path(__file__).resolve().parent.parent
APP_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("DATA_DIR", str(ROOT / "data")))
DATA_DIR.mkdir(parents=True, exist_ok=True)
if os.getenv("DATABASE_URL") is None:
    os.environ["DATABASE_URL"] = f"sqlite:///{DATA_DIR / 'acxiomcrm.db'}"
# db.py resolves DATABASE_URL at import time; use the default configured there for local runs.
SECRET = os.getenv("SESSION_SECRET", "local-demo-secret-change-before-deployment-2026")
APP_ENV = os.getenv("APP_ENV", "development")
AI_URL = os.getenv("AI_SERVICE_URL", "http://127.0.0.1:8001").rstrip("/")
ph = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=2)
app = FastAPI(title="AcxiomCRM | Sahyadri Cloudworks", version="1.0.0", description="Role-based CRM with privacy-aware lead intelligence")
app.add_middleware(SessionMiddleware, secret_key=SECRET, session_cookie="acxiomcrm_session", same_site="lax", https_only=(APP_ENV == "production"), max_age=60*60*8)
app.mount("/static", StaticFiles(directory=str(APP_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(APP_DIR / "templates"))

ROLES = ["Admin", "Manager", "Sales Executive"]
LEAD_STATUSES = ["New", "Contacted", "Qualified", "Unqualified", "Converted", "Lost"]
OPP_STAGES = ["Qualification", "Proposal", "Negotiation", "Won", "Lost"]
OPP_STATUSES = ["Open", "Won", "Lost"]
FOLLOWUP_STATUSES = ["Planned", "Completed", "Missed", "Cancelled"]
ACTIVITY_TYPES = ["Call", "Meeting", "Email", "Task"]
INDUSTRIES = ["Fintech", "Healthcare", "Retail", "Logistics", "EdTech", "Technology"]
SOURCES = ["Website", "Referral", "LinkedIn", "Event", "Cold Outreach", "Partner"]
SUMMARY_CACHE: dict[str, tuple[datetime, dict[str, Any]]] = {}
AUTH_ATTEMPTS: dict[str, list[datetime]] = {}

# -- Small, explicit validation helpers keep business rules visible to reviewers. --
def valid_email(value: str) -> bool:
    return bool(re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value or ""))

def valid_phone(value: str) -> bool:
    return bool(re.fullmatch(r"[6-9]\d{9}", value or ""))

def valid_password(value: str) -> bool:
    return len(value) >= 12 and bool(re.search(r"[A-Z]", value)) and bool(re.search(r"[a-z]", value)) and bool(re.search(r"\d", value)) and bool(re.search(r"[^A-Za-z0-9]", value))

def csrf_for(request: Request) -> str:
    token = request.session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        request.session["csrf_token"] = token
    return token

def check_csrf(request: Request, supplied: str | None):
    expected = request.session.get("csrf_token", "")
    if not expected or not supplied or not hmac.compare_digest(str(expected), str(supplied)):
        raise HTTPException(status_code=403, detail="Security token expired. Refresh the page and try again.")

def get_user(request: Request, db: Session) -> User | None:
    user_id = request.session.get("user_id")
    if not user_id:
        return None
    user = db.get(User, int(user_id))
    if not user or not user.is_active:
        request.session.clear()
        return None
    return user

def browser_user(request: Request, db: Session) -> User | RedirectResponse:
    user = get_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    return user

def api_user(request: Request, db: Session) -> User:
    user = get_user(request, db)
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")
    return user

def ip_for(request: Request) -> str:
    # Trust forwarded IP only when reverse-proxy deployment is intentionally configured.
    return (request.client.host if request.client else "unknown")[:60]

def audit(db: Session, request: Request, user: User | None, action: str, entity: str, record_id: Any = "", old: Any = "", new: Any = "", result: str = "Success"):
    def compact(value):
        if not value: return ""
        if isinstance(value, dict): return json.dumps(value, ensure_ascii=False, default=str)[:2500]
        return str(value)[:2500]
    db.add(AuditLog(user_id=user.id if user else None, role=user.role if user else "Anonymous", action=action, entity_name=entity, record_id=str(record_id or ""), old_value=compact(old), new_value=compact(new), status=result, ip_address=ip_for(request)))

def snapshot(obj: Any) -> dict[str, Any]:
    # Audit values intentionally omit direct contact details.
    result = {}
    for key in ("name", "company", "status", "stage", "amount", "probability", "expected_value", "assigned_to", "follow_up_date", "subject", "role", "is_active"):
        if hasattr(obj, key): result[key] = getattr(obj, key)
    return result

def scoped_query(db: Session, model, user: User):
    q = db.query(model)
    if model is Customer:
        q = q.filter(Customer.is_deleted.is_(False))
    if user.role == "Sales Executive" and hasattr(model, "assigned_to"):
        q = q.filter(getattr(model, "assigned_to") == user.id)
    return q

def can_manage_users(user: User) -> bool:
    return user.role == "Admin"

def safe_phone(phone: str, full: bool) -> str:
    digits = re.sub(r"\D", "", phone or "")
    if full or len(digits) < 4: return phone or ""
    return "+91 ******" + digits[-4:]

def safe_email(email: str, full: bool) -> str:
    if full or "@" not in (email or ""): return email or ""
    local, domain = email.split("@", 1)
    return (local[:1] + "***@" + domain) if local else "***@" + domain

def display_customer(c: Customer, user: User) -> dict[str, Any]:
    full = user.role != "Sales Executive" or c.assigned_to == user.id
    owner = ""
    return {"id": c.id, "code": c.code, "name": c.name, "email": safe_email(c.email, full), "phone": safe_phone(c.phone, full), "company": c.company, "industry": c.industry, "city": c.city, "state": c.state, "status": c.status, "assigned_to": c.assigned_to, "created_at": c.created_at}

def money(value: float | int | None) -> str:
    value = float(value or 0)
    if value >= 10_000_000: return f"₹{value/10_000_000:.2f} Cr"
    if value >= 100_000: return f"₹{value/100_000:.2f} L"
    return "₹" + f"{value:,.0f}"

templates.env.filters["money"] = money

# -- AI client: fail closed to transparent deterministic logic if the sidecar is unavailable. --
def fallback_score(lead: Lead | dict[str, Any]) -> dict[str, Any]:
    get = (lambda k, d=0: getattr(lead, k, d)) if not isinstance(lead, dict) else (lambda k, d=0: lead.get(k, d))
    lc = get("last_contact_date")
    days = max(0, (date.today() - lc).days) if lc else 30
    raw = 35 + min(int(get("interaction_count", 0) or 0), 12) * 2.6 + min(float(get("expected_value", 0) or 0)/100_000, 15)
    raw += 18 if get("status", "New") == "Qualified" else 8 if get("status", "New") == "Contacted" else 0
    raw += 8 if get("source", "Website") == "Referral" else 4 if get("source", "Website") == "LinkedIn" else 0
    raw -= min(days, 60) * 0.75
    score = round(max(2, min(97, raw)), 1)
    return {"score": score, "band": "Hot" if score > 70 else "Warm" if score >= 40 else "Cold", "reasons": ["recent engagement supports momentum" if days <= 7 else "contact is going quiet" if days > 21 else "contact cadence is moderate", "qualification signals are positive" if get("status", "New") == "Qualified" else "more qualification evidence is needed", "multiple interactions provide useful signal" if int(get("interaction_count", 0) or 0) >= 4 else "log another meaningful customer interaction"], "model": "web_fallback"}

def call_ai(path: str, payload: dict[str, Any], timeout: float = 1.2) -> dict[str, Any] | None:
    try:
        with httpx.Client(timeout=timeout) as client:
            r = client.post(f"{AI_URL}{path}", json=payload)
            r.raise_for_status()
            return r.json()
    except Exception:
        return None

def ai_is_available() -> bool:
    try:
        with httpx.Client(timeout=0.25) as client:
            response=client.get(f"{AI_URL}/health")
            return response.is_success
    except Exception:
        return False

def score_and_store(lead: Lead) -> dict[str, Any]:
    days = max(0, (date.today() - lead.last_contact_date).days) if lead.last_contact_date else 30
    payload = {"interaction_count": lead.interaction_count or 0, "expected_value": lead.expected_value or 0, "industry": lead.industry, "status": lead.status, "days_since_last_contact": days, "source": lead.source}
    scored = call_ai("/ai/score", payload) or fallback_score(lead)
    lead.ai_score = float(scored["score"])
    lead.ai_band = str(scored["band"])
    lead.ai_reasons = json.dumps(scored.get("reasons", []))
    return scored

def auto_assign(db: Session, industry: str) -> tuple[int, str]:
    people = db.query(User).filter(User.role == "Sales Executive", User.is_active.is_(True)).all()
    candidates = []
    for person in people:
        workload = db.query(Lead).filter(Lead.assigned_to == person.id, Lead.status.notin_(["Converted", "Lost", "Unqualified"])).count()
        wins = db.query(Opportunity).filter(Opportunity.assigned_to == person.id, Opportunity.stage == "Won").count()
        # This lightweight demo treats overall won deals as transferable experience, not ground truth.
        candidates.append({"user_id": person.id, "name": person.name, "open_workload": workload, "industry_wins": wins if industry else 0, "capacity": 25})
    result = call_ai("/ai/assign", {"industry": industry, "candidates": candidates})
    if result and result.get("user_id"):
        return int(result["user_id"]), str(result.get("reason", "Lowest workload"))
    viable = [x for x in candidates if x["open_workload"] < x["capacity"]]
    if not viable: return (people[0].id if people else 1, "All executives at capacity; manager review recommended")
    chosen = min(viable, key=lambda x: (x["open_workload"] - min(x["industry_wins"], 4)*0.75, x["user_id"]))
    return chosen["user_id"], f"{chosen['open_workload']} open items; workload-based fallback"

# -- Bootstrap synthetic data, for a self-contained first-run demo. --
def seed_data():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        if db.query(User).count() > 0: return
        users = [
            User(name="Aarav Kulkarni", email="admin@acxiomcrm.demo", password_hash=ph.hash("Admin@12345!"), role="Admin"),
            User(name="Neha Deshmukh", email="manager@acxiomcrm.demo", password_hash=ph.hash("Manager@12345!"), role="Manager"),
            User(name="Rohan Patil", email="sales@acxiomcrm.demo", password_hash=ph.hash("Sales@12345!"), role="Sales Executive"),
        ]
        db.add_all(users); db.flush()
        executives = users[2:]
        # Create a few additional reps so workload-aware assignment and manager views are meaningful.
        for i, (name, email) in enumerate([("Isha Shah", "isha@acxiomcrm.demo"), ("Kabir Joshi", "kabir@acxiomcrm.demo")]):
            u = User(name=name, email=email, password_hash=ph.hash("DemoRep@12345!"), role="Sales Executive")
            db.add(u); executives.append(u)
        db.flush()
        companies = ["BlueKite Systems", "MangoTree Fintech", "Sahyadri Health", "Orbit Retail Labs", "Monsoon Logistics", "Pioneer EdTech", "CopperLeaf Digital", "Asterion Analytics", "NimblePay", "CedarWorks"]
        cities = [("Pune","Maharashtra"),("Mumbai","Maharashtra"),("Nashik","Maharashtra"),("Bengaluru","Karnataka"),("Hyderabad","Telangana"),("Ahmedabad","Gujarat"),("Indore","Madhya Pradesh"),("Nagpur","Maharashtra")]
        industries = INDUSTRIES = ["Fintech", "Healthcare", "Retail", "Logistics", "EdTech", "Technology"]
        rnd = __import__("random").Random(73)
        customers=[]
        for i in range(40):
            company = companies[i % len(companies)] + (f" {i//len(companies)+1}" if i >= len(companies) else "")
            first = ["Aditi","Arjun","Meera","Vihaan","Ananya","Reyansh","Saanvi","Ishaan","Kavya","Advait"][i%10]
            last = ["Kulkarni","Shah","Deshmukh","Patil","Joshi","Iyer","Mehta","Rao"][i%8]
            city,state = cities[i%len(cities)]
            owner=executives[i%len(executives)]
            c = Customer(code=f"CUS-{1001+i}", name=f"{first} {last}", email=f"contact{i+1}@{company.lower().replace(' ','').replace('-','')}.example", phone=f"{6+(i%4)}{(770000000+i):09d}"[-10:], company=company, industry=industries[i%len(industries)], address=f"{10+i} Business Park Road", city=city, state=state, gst_number=f"27ABCDE{i:04d}F1Z{(i%9)+1}", status="Active" if i%7 else "Prospect", assigned_to=owner.id, notes="Seeded demo account — replace with your own scenario.", created_by=users[0].id, created_at=datetime.utcnow()-timedelta(days=rnd.randint(5,280)))
            customers.append(c); db.add(c)
        db.flush()
        leads=[]
        for i in range(60):
            owner=executives[i%len(executives)]
            days=rnd.randint(0,45)
            st=["New","Contacted","Qualified","Unqualified","Converted","Lost"][i%6]
            lead=Lead(code=f"LEAD-{2001+i}", name=f"{['Pranav','Riya','Om','Sana','Atharv','Tara'][i%6]} {['Shinde','Bose','Khan','Gokhale','Nair'][i%5]}", email=f"lead{i+1}@example.test", phone=f"{7+(i%3)}{(660000000+i):09d}"[-10:], company=companies[(i+3)%len(companies)], industry=industries[i%len(industries)], source=["Website","Referral","LinkedIn","Event","Cold Outreach"][i%5], status=st, expected_value=float(rnd.choice([65000,125000,275000,450000,850000,1400000])), interaction_count=rnd.randint(0,9), last_contact_date=date.today()-timedelta(days=days), assigned_to=owner.id, created_at=datetime.utcnow()-timedelta(days=rnd.randint(1,120)))
            score=fallback_score(lead); lead.ai_score=score['score']; lead.ai_band=score['band']; lead.ai_reasons=json.dumps(score['reasons'])
            leads.append(lead); db.add(lead)
        db.flush()
        opps=[]
        for i in range(25):
            owner=executives[i%len(executives)]
            st=["Qualification","Proposal","Negotiation","Won","Lost"][i%5]
            status_val="Won" if st=="Won" else "Lost" if st=="Lost" else "Open"
            opp=Opportunity(name=f"{customers[i%len(customers)].company} — {['Platform rollout','Analytics upgrade','Annual services','Data modernisation','Security assessment'][i%5]}", customer_id=customers[i%len(customers)].id, lead_id=leads[i%len(leads)].id if i%2==0 else None, amount=float(rnd.choice([75000,180000,320000,575000,1250000,2500000])), stage=st, probability={"Qualification":20,"Proposal":45,"Negotiation":70,"Won":100,"Lost":0}[st], expected_close_date=date.today()+timedelta(days=rnd.randint(1,75)), status=status_val, source=["Website","Referral","LinkedIn"][i%3], notes="Validate buying group, value and next step.", assigned_to=owner.id, created_at=datetime.utcnow()-timedelta(days=rnd.randint(1,100)))
            opps.append(opp); db.add(opp)
        db.flush()
        for i in range(34):
            owner=executives[i%len(executives)]
            db.add(FollowUp(customer_id=customers[i%40].id if i%2==0 else None, lead_id=leads[i%60].id if i%2 else None, opportunity_id=opps[i%25].id if i%3==0 else None, follow_up_date=date.today()+timedelta(days=(i%10)-2), follow_up_type=["Call","Meeting","Email","Task"][i%4], subject=f"{['Check-in','Proposal review','Discovery call','Renewal planning'][i%4]} — {customers[i%40].company}", remarks="Confirm the next buyer milestone; update outcome after the touchpoint.", status="Planned" if i%4 else "Completed", assigned_to=owner.id))
        for i in range(48):
            owner=executives[i%len(executives)]
            words=["positive response and interested in demo", "budget concern needs clarification", "great meeting; next steps confirmed", "no response after two attempts", "requested pricing and implementation plan"]
            desc=words[i%len(words)]
            sentiment="Positive" if any(w in desc for w in ["positive","great","interested"]) else "Negative" if any(w in desc for w in ["concern","no response"]) else "Neutral"
            db.add(Activity(activity_type=ACTIVITY_TYPES[i%4], subject=f"{['Discovery call','Solution demo','Pricing email','Internal follow-up'][i%4]}", description=desc, activity_date=date.today()-timedelta(days=i%28), customer_id=customers[i%40].id if i%2==0 else None, lead_id=leads[i%60].id if i%2 else None, assigned_to=owner.id, status="Completed" if i%5 else "Planned", sentiment=sentiment))
        audit(db, type("R", (), {"client": type("C", (), {"host":"127.0.0.1"})()})(), users[0], "SEED", "System", "", "", {"customers":40,"leads":60,"opportunities":25,"followups":34,"activities":48})
        db.commit()
    except Exception:
        db.rollback(); raise
    finally:
        db.close()

@app.on_event("startup")
def startup():
    seed_data()

# -- Public pages and authentication --
@app.get("/health")
def web_health():
    return {"status":"ok", "service":"web", "database":"configured", "ai_service":AI_URL}

@app.get("/", response_class=HTMLResponse)
def home(request: Request, db: Session = Depends(get_db)):
    user = get_user(request, db)
    return RedirectResponse("/dashboard" if user else "/login", status_code=303)

@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return templates.TemplateResponse(request, "auth.html", {"request":request,"csrf_token":csrf_for(request),"mode":"login","error":request.query_params.get("error", ""),"msg":request.query_params.get("msg", "")})

@app.post("/login")
async def login(request: Request, db: Session = Depends(get_db)):
    form=await request.form(); check_csrf(request, form.get("csrf_token"))
    email=str(form.get("email", "")).strip().lower(); password=str(form.get("password", ""))
    user=db.query(User).filter(func.lower(User.email)==email).first()
    now=datetime.utcnow()
    if user and user.lockout_end and user.lockout_end > now:
        audit(db, request, user, "LOGIN_BLOCKED", "User", user.id, "", "Account lockout active", "Blocked"); db.commit()
        return RedirectResponse("/login?error=Account+temporarily+locked.+Try+again+after+15+minutes.",status_code=303)
    valid=False
    if user and user.is_active:
        try: valid=ph.verify(user.password_hash,password)
        except (VerifyMismatchError, VerificationError, Exception): valid=False
    if not valid:
        if user:
            user.failed_login_count += 1
            if user.failed_login_count >= 5:
                user.lockout_end=now+timedelta(minutes=15); user.failed_login_count=0
                audit(db, request, user, "LOCKOUT", "User", user.id, "", "5 failed attempts; locked 15 minutes", "Blocked")
            audit(db, request, user, "LOGIN_FAILED", "User", user.id, "", "Credentials rejected", "Failed")
        else:
            audit(db, request, None, "LOGIN_FAILED", "User", email[:80], "", "Unknown account", "Failed")
        db.commit()
        return RedirectResponse("/login?error=Invalid+credentials+or+inactive+account.",status_code=303)
    user.failed_login_count=0; user.lockout_end=None
    request.session.clear(); request.session["user_id"]=user.id; request.session["csrf_token"]=secrets.token_urlsafe(32)
    audit(db, request, user, "LOGIN_SUCCESS", "User", user.id, "", "Login successful"); db.commit()
    return RedirectResponse("/dashboard",status_code=303)

@app.get("/register", response_class=HTMLResponse)
def register_page(request: Request):
    return templates.TemplateResponse(request,"auth.html",{"request":request,"csrf_token":csrf_for(request),"mode":"register","error":request.query_params.get("error", ""),"msg":"Self-registration is restricted to the Sales Executive role."})

@app.post("/register")
async def register(request: Request, db: Session = Depends(get_db)):
    form=await request.form(); check_csrf(request,form.get("csrf_token"))
    name=str(form.get("name", "")).strip(); email=str(form.get("email", "")).strip().lower(); password=str(form.get("password", ""))
    if not name or len(name)>100: return RedirectResponse("/register?error=Enter+a+name+of+1%E2%80%93100+characters.",status_code=303)
    if not valid_email(email): return RedirectResponse("/register?error=Enter+a+valid+email+address.",status_code=303)
    if not valid_password(password): return RedirectResponse("/register?error=Password+needs+12%2B+characters%2C+uppercase%2C+lowercase%2C+number+and+symbol.",status_code=303)
    if db.query(User).filter(func.lower(User.email)==email).first(): return RedirectResponse("/register?error=An+account+with+this+email+already+exists.",status_code=303)
    user=User(name=name,email=email,password_hash=ph.hash(password),role="Sales Executive")
    db.add(user); db.flush(); audit(db,request,user,"REGISTER","User",user.id,"","Sales Executive registration"); db.commit()
    return RedirectResponse("/login?msg=Account+created.+Please+sign+in.",status_code=303)

@app.post("/logout")
async def logout(request: Request, db: Session = Depends(get_db)):
    form=await request.form(); check_csrf(request,form.get("csrf_token")); user=get_user(request,db)
    audit(db,request,user,"LOGOUT","User",user.id if user else "","","Session terminated"); db.commit(); request.session.clear()
    return RedirectResponse("/login?msg=You+have+signed+out.",status_code=303)

@app.get("/api/csrf")
def api_csrf(request: Request, db: Session = Depends(get_db)):
    api_user(request,db); return {"csrf_token":csrf_for(request)}

# -- Dashboard and reports --
def scoped_all(db: Session, model, user: User):
    return scoped_query(db,model,user).all()

def build_dashboard(db: Session,user: User):
    customers=scoped_all(db,Customer,user); leads=scoped_all(db,Lead,user); opps=scoped_all(db,Opportunity,user); followups=scoped_all(db,FollowUp,user); activities=scoped_all(db,Activity,user)
    open_leads=[x for x in leads if x.status not in ("Converted","Lost","Unqualified")]
    open_opps=[x for x in opps if x.status=="Open"]
    won=[x for x in opps if x.status=="Won"]
    lost=[x for x in opps if x.status=="Lost"]
    today=date.today()
    overdue=[f for f in followups if f.status=="Planned" and f.follow_up_date<today]
    upcoming=sorted([f for f in followups if f.status=="Planned" and f.follow_up_date>=today],key=lambda f:f.follow_up_date)[:6]
    lead_counts={s:sum(1 for x in leads if x.status==s) for s in ["New","Contacted","Qualified","Lost","Converted","Unqualified"]}
    opp_counts={s:sum(1 for x in opps if x.stage==s) for s in OPP_STAGES}
    monthly=[]
    for back in reversed(range(6)):
        idx=today.year*12+(today.month-1)-back; y,m=idx//12,idx%12+1
        sales=sum(o.amount for o in won if o.created_at.year==y and o.created_at.month==m)
        monthly.append({"label":calendar.month_abbr[m],"value":round(sales,2)})
    top_hot=sorted([l for l in leads if l.ai_score>=70 and l.status not in ("Lost","Converted")],key=lambda x:x.ai_score,reverse=True)[:3]
    risk_opps=[]
    for o in open_opps:
        activity_clauses=[]
        if o.customer_id: activity_clauses.append(Activity.customer_id==o.customer_id)
        if o.lead_id: activity_clauses.append(Activity.lead_id==o.lead_id)
        last_activity=db.query(Activity).filter(or_(*activity_clauses)).order_by(Activity.activity_date.desc()).first() if activity_clauses else None
        days_since_activity=(today-last_activity.activity_date).days if last_activity else 999
        close_days=(o.expected_close_date-today).days
        if days_since_activity>=14 or o.expected_close_date<today or (close_days<=7 and o.probability<40):
            risk_opps.append(o)
    risk_opps=sorted(risk_opps,key=lambda o:(o.expected_close_date,o.probability))[:3]
    brief=[]
    brief.append(f"{len(top_hot)} hot lead(s) are ready for focused outreach.")
    brief.append(f"{len(risk_opps)} deal(s) need risk review: low probability near close or overdue close date.")
    brief.append(f"{len(overdue)} planned follow-up(s) are overdue.")
    brief.append(f"Open pipeline is {money(sum(o.amount for o in open_opps))}; weighted open pipeline is {money(sum(o.amount*o.probability/100 for o in open_opps))}.")
    brief.append("Prioritise a dated next step on every active opportunity before the day ends.")
    owner_names={u.id:u.name for u in db.query(User).all()}
    cust_names={c.id:c.company for c in customers}
    return {"cards":[("Total Customers",len(customers),"customers", "users"),("Total Leads",len(leads),"leads","target"),("Open Leads",len(open_leads),"active prospects","activity"),("Total Opportunities",len(opps),"deals tracked","briefcase"),("Open Opportunities",len(open_opps),"in the pipeline","trending-up"),("Won",len(won),"closed successfully","check-circle"),("Lost",len(lost),"closed / not won","x-circle"),("Total Pipeline Value",money(sum(o.amount for o in open_opps)),"open deal value","indian-rupee")],"lead_labels":list(lead_counts.keys()),"lead_values":list(lead_counts.values()),"opp_labels":list(opp_counts.keys()),"opp_values":list(opp_counts.values()),"month_labels":[x["label"] for x in monthly],"month_values":[x["value"] for x in monthly],"brief":brief,"hot_leads":top_hot,"risk_opps":risk_opps,"overdue":overdue,"upcoming":upcoming,"owner_names":owner_names,"cust_names":cust_names,"weighted_pipeline":sum(o.amount*o.probability/100 for o in open_opps),"followups_pending":sum(1 for f in followups if f.status=="Planned"),"activity_count":len(activities)}

@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request, db: Session=Depends(get_db)):
    user=browser_user(request,db)
    if isinstance(user,RedirectResponse): return user
    data=build_dashboard(db,user)
    return templates.TemplateResponse(request,"dashboard.html",{"request":request,"user":user,"csrf_token":csrf_for(request),"data":data,"active":"dashboard"})

@app.get("/reports", response_class=HTMLResponse)
def reports(request: Request, kind: str="pipeline", db: Session=Depends(get_db)):
    user=browser_user(request,db)
    if isinstance(user,RedirectResponse): return user
    if kind not in ("pipeline","conversion","followups","customers","leads","audit"): kind="pipeline"
    data={"kind":kind,"rows":[]}
    if kind=="pipeline":
        opps=scoped_all(db,Opportunity,user); grouped=defaultdict(lambda:{"count":0,"value":0.0,"weighted":0.0})
        for o in opps:
            g=grouped[o.stage];g["count"]+=1;g["value"]+=o.amount;g["weighted"]+=o.amount*o.probability/100
        data["rows"]=[{"label":k,"count":v["count"],"value":money(v["value"]),"weighted":money(v["weighted"])} for k,v in grouped.items()]
    elif kind=="conversion":
        leads=scoped_all(db,Lead,user); data["rows"]=[{"label":s,"count":sum(l.status==s for l in leads)} for s in LEAD_STATUSES];data["total"]=len(leads);data["converted"]=sum(l.status=="Converted" for l in leads)
    elif kind=="followups":
        fs=scoped_all(db,FollowUp,user); data["rows"]=[{"label":s,"count":sum(f.status==s for f in fs)} for s in FOLLOWUP_STATUSES];data["overdue"]=sum(f.status=="Planned" and f.follow_up_date<date.today() for f in fs)
    elif kind=="customers":
        data["rows"]=[{"label":c.company,"status":c.status,"city":c.city,"owner":db.get(User,c.assigned_to).name if db.get(User,c.assigned_to) else "—","date":c.created_at.date().isoformat()} for c in scoped_all(db,Customer,user)]
    elif kind=="leads":
        data["rows"]=[{"label":l.name,"company":l.company,"source":l.source,"status":l.status,"score":l.ai_score,"owner":db.get(User,l.assigned_to).name if db.get(User,l.assigned_to) else "—"} for l in scoped_all(db,Lead,user)]
    elif kind=="audit":
        if user.role!="Admin": raise HTTPException(status_code=403, detail="Admin only")
        data["rows"]=[{"label":a.entity_name,"action":a.action,"record":a.record_id,"role":a.role,"result":a.status,"date":a.created_at.strftime("%Y-%m-%d %H:%M"),"ip":a.ip_address} for a in db.query(AuditLog).order_by(AuditLog.created_at.desc()).limit(150).all()]
    return templates.TemplateResponse(request,"reports.html",{"request":request,"user":user,"csrf_token":csrf_for(request),"data":data,"active":"reports"})

# -- CRUD screens share one metadata-driven form, but each resource has explicit validation. --
MODULES = {
 "customers": {"model":Customer,"title":"Customers","singular":"Customer","fields":[("name","Customer name","text",True),("email","Business email","email",True),("phone","Indian mobile (10 digits)","tel",True),("company","Company","text",True),("industry","Industry","select",True),("city","City","text",True), ("state","State","text",True),("gst_number","GSTIN (optional)","text",False),("status","Status","select",True),("assigned_to","Owner","select",True),("notes","Account notes","textarea",False)],"columns":["code","name","company","email","phone","industry","city","status","assigned_to"],"filters":["search","status"]},
 "leads": {"model":Lead,"title":"Leads","singular":"Lead","fields":[("name","Lead name","text",True),("email","Email (optional)","email",False),("phone","Phone (optional)","tel",False),("company","Company","text",True),("industry","Industry","select",True),("source","Lead source","select",True),("status","Status","select",True),("expected_value","Expected value (₹)","number",True),("interaction_count","Interactions","number",True),("last_contact_date","Last contact date","date",False),("assigned_to","Owner / auto-assign","select",False)],"columns":["code","name","company","industry","source","status","expected_value","ai_score","ai_band","assigned_to"],"filters":["search","status","assigned_to"]},
 "opportunities": {"model":Opportunity,"title":"Opportunities","singular":"Opportunity","fields":[("name","Opportunity name","text",True),("customer_id","Customer","select",False),("lead_id","Related lead","select",False),("amount","Amount (₹)","number",True),("stage","Stage","select",True),("probability","Probability (0–100)","number",True),("expected_close_date","Expected close date","date",True),("status","Status","select",True),("assigned_to","Owner","select",True),("source","Source","select",False),("notes","Deal notes","textarea",False)],"columns":["name","customer_id","amount","stage","probability","expected_close_date","status","assigned_to"],"filters":["search","status","stage"]},
 "followups": {"model":FollowUp,"title":"Follow-ups","singular":"Follow-up","fields":[("customer_id","Related customer","select",False),("lead_id","Related lead","select",False),("opportunity_id","Related opportunity","select",False),("follow_up_date","Date","date",True),("follow_up_type","Type","select",True),("subject","Subject","text",True),("remarks","Notes","textarea",False),("status","Status","select",True),("assigned_to","Assigned to","select",True)],"columns":["subject","follow_up_date","follow_up_type","status","customer_id","lead_id","assigned_to"],"filters":["search","status","assigned_to"]},
 "activities": {"model":Activity,"title":"Activities","singular":"Activity","fields":[("activity_type","Activity type","select",True),("subject","Subject","text",True),("description","Activity notes","textarea",False),("activity_date","Date","date",True),("customer_id","Related customer","select",False),("lead_id","Related lead","select",False),("assigned_to","Assigned to","select",True),("status","Status","select",True)],"columns":["activity_type","subject","activity_date","status","sentiment","customer_id","lead_id","assigned_to"],"filters":["search","status","assigned_to"]}
}

def field_options(db: Session, user: User, name: str):
    if name=="industry": return [(x,x) for x in INDUSTRIES]
    if name=="source": return [(x,x) for x in SOURCES]
    if name=="status":
        return [(x,x) for x in ( ["Active","Prospect","Inactive"] if getattr(field_options,"_module","" )=="customers" else LEAD_STATUSES if getattr(field_options,"_module","")=="leads" else OPP_STATUSES if getattr(field_options,"_module","")=="opportunities" else FOLLOWUP_STATUSES if getattr(field_options,"_module","")=="followups" else ["Planned","Completed","Cancelled"]) ]
    if name=="stage": return [(x,x) for x in OPP_STAGES]
    if name=="follow_up_type" or name=="activity_type": return [(x,x) for x in (ACTIVITY_TYPES if name=="activity_type" else ["Call","Meeting","Email","Task","Demo","Renewal"])]
    if name=="probability": return [(str(x),f"{x}%") for x in (0,10,20,30,40,50,60,70,80,90,100)]
    if name=="assigned_to":
        people=db.query(User).filter(User.is_active.is_(True),User.role=="Sales Executive").order_by(User.name).all()
        if user.role=="Sales Executive": people=[p for p in people if p.id==user.id]
        return [(str(p.id),p.name) for p in people]
    if name=="customer_id": return [(str(x.id),f"{x.company} · {x.name}") for x in scoped_query(db,Customer,user).order_by(Customer.company).all()]
    if name=="lead_id": return [(str(x.id),f"{x.company} · {x.name}") for x in scoped_query(db,Lead,user).order_by(Lead.name).all()]
    if name=="opportunity_id": return [(str(x.id),x.name) for x in scoped_query(db,Opportunity,user).order_by(Opportunity.name).all()]
    return []

def build_fields(db: Session,user: User,module: str, record: Any=None):
    spec=MODULES[module]; built=[]
    for key,label,kind,required in spec["fields"]:
        if key=="assigned_to" and user.role=="Sales Executive" and record is None:
            value=str(user.id); hidden=True
        else:
            raw=getattr(record,key,None) if record is not None else None
            value=raw.isoformat() if isinstance(raw,(date,datetime)) else ("" if raw is None else str(raw))
            hidden=False
        options=[]
        if kind=="select":
            field_options._module=module
            options=field_options(db,user,key)
            if key in ("customer_id","lead_id","opportunity_id"):
                options=[("", "— No linked record —")]+options
        built.append({"key":key,"label":label,"kind":kind,"required":required,"value":value,"options":options,"hidden":hidden})
    return built

def row_display(db: Session,user: User,module: str,obj: Any):
    cols=[]
    for key in MODULES[module]["columns"]:
        val=getattr(obj,key,None)
        if key=="assigned_to": val=db.get(User,val).name if val and db.get(User,val) else "—"
        elif key=="customer_id":
            c=db.get(Customer,val) if val else None; val=c.company if c else "—"
        elif key=="lead_id":
            l=db.get(Lead,val) if val else None; val=l.name if l else "—"
        elif key=="phone": val=("+91 "+str(val)) if val and val!="—" else val
        elif key=="ai_score": val=f"{val:.0f}/100"
        elif key=="expected_value" or key=="amount": val=money(val)
        elif isinstance(val,(datetime,date)): val=val.strftime("%d %b %Y")
        elif val is None or val=="": val="—"
        cols.append({"key":key,"value":val})
    return {"id":obj.id,"cells":cols,"name":getattr(obj,"name",getattr(obj,"subject","Record")),"status":getattr(obj,"status","")}

def validate_and_save(db: Session, request: Request, user: User, module: str, form: Any):
    model=MODULES[module]["model"]
    rid=str(form.get("entity_id", "")).strip()
    obj=None
    if rid:
        try: obj=scoped_query(db,model,user).filter(model.id==int(rid)).first()
        except ValueError: obj=None
        if obj is None: raise ValueError("Record not found or you do not have permission to edit it.")
    old=snapshot(obj) if obj else {}
    data={k:str(form.get(k,"")).strip() for k,_,_,_ in MODULES[module]["fields"]}
    # Sales executives cannot change ownership to another user, even with a forged request.
    if user.role=="Sales Executive": data["assigned_to"]=str(user.id)
    if module=="customers":
        if not data.get("assigned_to"): data["assigned_to"]=str(user.id if user.role=="Sales Executive" else auto_assign(db, data.get("industry", "Technology"))[0])
        selected_owner=db.get(User,int(data["assigned_to"])) if data.get("assigned_to") else None
        if not selected_owner or not selected_owner.is_active or selected_owner.role!="Sales Executive": raise ValueError("Select an active Sales Executive as owner.")
        if not data["name"]: raise ValueError("Customer Name is required.")
        if len(data["name"])>120: raise ValueError("Customer Name must be 120 characters or fewer.")
        if not valid_email(data["email"]): raise ValueError("Enter a valid email address.")
        if not valid_phone(data["phone"]): raise ValueError("Enter a valid phone number.")
        if not data["company"]: raise ValueError("Company is required.")
        dup_email=db.query(Customer).filter(func.lower(Customer.email)==data["email"].lower(),Customer.is_deleted.is_(False))
        dup_phone=db.query(Customer).filter(Customer.phone==re.sub(r"\D","",data["phone"]),Customer.is_deleted.is_(False))
        if obj: dup_email=dup_email.filter(Customer.id!=obj.id); dup_phone=dup_phone.filter(Customer.id!=obj.id)
        if dup_email.first() or dup_phone.first(): raise ValueError("A customer with this email or phone already exists.")
        # Fuzzy duplicate warning is not a hard block; hard uniqueness is above.
        possible=[]
        for c in scoped_query(db,Customer,user).all():
            ratio=max(difflib.SequenceMatcher(None,data["company"].lower(),c.company.lower()).ratio(),difflib.SequenceMatcher(None,data["name"].lower(),c.name.lower()).ratio())
            if ratio>=0.88 and (not obj or c.id!=obj.id): possible.append(c.company)
        data["phone"]=re.sub(r"\D","",data["phone"])
        data["gst_number"]=data["gst_number"][:20]
        data["notes"]=data["notes"][:2000]
        if possible: request.session["notice"]="Possible duplicate: "+", ".join(possible[:2])+". Check before creating another account."
    elif module=="leads":
        if data.get("assigned_to"):
            selected_owner=db.get(User,int(data["assigned_to"]))
            if not selected_owner or not selected_owner.is_active or selected_owner.role!="Sales Executive": raise ValueError("Select an active Sales Executive as owner.")
        if not data["name"]: raise ValueError("Lead Name is required.")
        if not data["company"]: raise ValueError("Company is required.")
        if data["email"] and not valid_email(data["email"]): raise ValueError("Enter a valid email address.")
        if data["phone"] and not valid_phone(data["phone"]): raise ValueError("Enter a valid phone number.")
        if data["status"] not in LEAD_STATUSES: raise ValueError("Select a valid lead status.")
        try: val=float(data["expected_value"])
        except: raise ValueError("Expected value must be numeric.")
        if val<0 or val>100_000_000: raise ValueError("Expected value must be between 0 and 100000000.")
        try: interactions=int(data["interaction_count"] or 0)
        except: raise ValueError("Interactions must be a whole number.")
        if interactions<0 or interactions>10000: raise ValueError("Interactions must be between 0 and 10000.")
    elif module=="opportunities":
        if not data.get("assigned_to"): data["assigned_to"]=str(user.id if user.role=="Sales Executive" else auto_assign(db, "Technology")[0])
        selected_owner=db.get(User,int(data["assigned_to"])) if data.get("assigned_to") else None
        if not selected_owner or not selected_owner.is_active or selected_owner.role!="Sales Executive": raise ValueError("Select an active Sales Executive as owner.")
        if not data["name"]: raise ValueError("Opportunity Name is required.")
        try: amount=float(data["amount"])
        except: raise ValueError("Opportunity Amount must be numeric.")
        if data["status"]=="Open" and amount<=0: raise ValueError("Opportunity Amount must be greater than 0.")
        if amount<0: raise ValueError("Opportunity Amount cannot be negative.")
        try: prob=int(data["probability"])
        except: raise ValueError("Probability must be a whole number.")
        if prob<0 or prob>100: raise ValueError("Probability must be between 0 and 100.")
        if data["stage"] not in OPP_STAGES or data["status"] not in OPP_STATUSES: raise ValueError("Select a valid opportunity stage and status.")
        try: close=date.fromisoformat(data["expected_close_date"])
        except: raise ValueError("Enter a valid expected close date.")
        if data["status"]=="Open" and close<date.today(): raise ValueError("Expected Close Date cannot be in the past.")
        if data["customer_id"] and not scoped_query(db,Customer,user).filter(Customer.id==int(data["customer_id"])).first(): raise ValueError("Select a customer you are allowed to access.")
        if data["lead_id"] and not scoped_query(db,Lead,user).filter(Lead.id==int(data["lead_id"])).first(): raise ValueError("Select a lead you are allowed to access.")
    elif module=="followups":
        if not data.get("assigned_to"): data["assigned_to"]=str(user.id if user.role=="Sales Executive" else auto_assign(db, "Technology")[0])
        selected_owner=db.get(User,int(data["assigned_to"])) if data.get("assigned_to") else None
        if not selected_owner or not selected_owner.is_active or selected_owner.role!="Sales Executive": raise ValueError("Select an active Sales Executive as assignee.")
        if not data["subject"]: raise ValueError("Follow-up subject is required.")
        try: d=date.fromisoformat(data["follow_up_date"])
        except: raise ValueError("Enter a valid follow-up date.")
        if data["status"]=="Planned" and d<date.today(): raise ValueError("Follow-up date cannot be earlier than today.")
        if data["status"] not in FOLLOWUP_STATUSES: raise ValueError("Select a valid follow-up status.")
        if data.get("customer_id") and not scoped_query(db,Customer,user).filter(Customer.id==int(data["customer_id"])).first(): raise ValueError("Select a customer you are allowed to access.")
        if data.get("lead_id") and not scoped_query(db,Lead,user).filter(Lead.id==int(data["lead_id"])).first(): raise ValueError("Select a lead you are allowed to access.")
        if data.get("opportunity_id") and not scoped_query(db,Opportunity,user).filter(Opportunity.id==int(data["opportunity_id"])).first(): raise ValueError("Select an opportunity you are allowed to access.")
        if not any(data.get(k) for k in ("customer_id","lead_id","opportunity_id")): raise ValueError("Link the follow-up to a customer, lead or opportunity.")
    elif module=="activities":
        if not data.get("assigned_to"): data["assigned_to"]=str(user.id if user.role=="Sales Executive" else auto_assign(db, "Technology")[0])
        selected_owner=db.get(User,int(data["assigned_to"])) if data.get("assigned_to") else None
        if not selected_owner or not selected_owner.is_active or selected_owner.role!="Sales Executive": raise ValueError("Select an active Sales Executive as assignee.")
        if not data["subject"]: raise ValueError("Activity subject is required.")
        if data["activity_type"] not in ACTIVITY_TYPES: raise ValueError("Select a valid activity type.")
        try: date.fromisoformat(data["activity_date"])
        except: raise ValueError("Enter a valid activity date.")
        if data.get("customer_id") and not scoped_query(db,Customer,user).filter(Customer.id==int(data["customer_id"])).first(): raise ValueError("Select a customer you are allowed to access.")
        if data.get("lead_id") and not scoped_query(db,Lead,user).filter(Lead.id==int(data["lead_id"])).first(): raise ValueError("Select a lead you are allowed to access.")
    if obj is None:
        obj=model()
        if module=="customers": obj.code=f"CUS-{secrets.token_hex(4).upper()}"; obj.created_by=user.id
        if module=="leads": obj.code=f"LEAD-{secrets.token_hex(4).upper()}"; obj.interaction_count=0
        db.add(obj)
    # Map form fields to typed model attributes; fields not supplied are not mass-assigned.
    for key,value in data.items():
        if key in ("name","email","phone","company","industry","city","state","gst_number","status","notes","source","stage","subject","remarks","follow_up_type","activity_type","description"): setattr(obj,key,value)
        elif key in ("assigned_to","customer_id","lead_id","opportunity_id"):
            if key=="assigned_to" and not value and module=="leads": continue
            setattr(obj,key, int(value) if value else None)
        elif key in ("expected_value","amount"): setattr(obj,key,float(value or 0))
        elif key in ("interaction_count","probability"): setattr(obj,key,int(value or 0))
        elif key in ("last_contact_date","expected_close_date","follow_up_date","activity_date"): setattr(obj,key,date.fromisoformat(value) if value else None)
    if module=="leads":
        if not rid and not data.get("assigned_to"):
            assigned, reason=auto_assign(db,obj.industry); obj.assigned_to=assigned
            audit(db,request,user,"AI_AUTO_ASSIGN","Lead",obj.code,"",{"assigned_to":assigned,"reason":reason})
        scored=score_and_store(obj)
        # Audit score changes only as metadata, not customer contact details.
        obj.ai_reasons=json.dumps(scored.get("reasons",[]))
    if module=="activities":
        text_blob=(str(getattr(obj,"subject", ""))+" "+str(getattr(obj,"description", ""))).lower()
        positive=("positive", "great", "interested", "excellent", "approved", "happy", "confirmed", "success")
        negative=("concern", "blocked", "delay", "no response", "unhappy", "risk", "complaint", "cancel")
        obj.sentiment="Positive" if any(word in text_blob for word in positive) else "Negative" if any(word in text_blob for word in negative) else "Neutral"
    db.flush()
    audit(db,request,user,"UPDATE" if rid else "CREATE",MODULES[module]["singular"],obj.id,old,snapshot(obj))
    try: db.commit()
    except IntegrityError:
        db.rollback(); raise ValueError("A unique field is already in use. Check email, phone and record code.")
    return obj

@app.get("/{module}", response_class=HTMLResponse)
def module_page(module: str, request: Request, search: str="", status_filter: str="", stage: str="", assigned_to: str="", record_date: str="", related_id: str="", activity_type: str="", edit: int|None=None, db: Session=Depends(get_db)):
    # Keep one-segment literal pages reachable despite this compact metadata route.
    if module == "users": return users_page(request, db)
    if module == "audit": return audit_page(request, db)
    if module not in MODULES: raise HTTPException(status_code=404,detail="Page not found")
    user=browser_user(request,db)
    if isinstance(user,RedirectResponse): return user
    spec=MODULES[module]; q=scoped_query(db,spec["model"],user)
    if search:
        cols={"customers":[Customer.name,Customer.email,Customer.phone,Customer.company],"leads":[Lead.name,Lead.company,Lead.email],"opportunities":[Opportunity.name],"followups":[FollowUp.subject,FollowUp.remarks],"activities":[Activity.subject,Activity.description]}[module]
        clauses=[c.ilike(f"%{search[:100]}%") for c in cols]
        if module=="opportunities": clauses.append(Opportunity.customer_id.in_(db.query(Customer.id).filter(Customer.company.ilike(f"%{search[:100]}%"))))
        if module=="followups":
            clauses.extend([FollowUp.customer_id.in_(db.query(Customer.id).filter(or_(Customer.company.ilike(f"%{search[:100]}%"),Customer.name.ilike(f"%{search[:100]}%")))), FollowUp.lead_id.in_(db.query(Lead.id).filter(or_(Lead.company.ilike(f"%{search[:100]}%"),Lead.name.ilike(f"%{search[:100]}%"))))])
        if module=="activities":
            clauses.extend([Activity.customer_id.in_(db.query(Customer.id).filter(or_(Customer.company.ilike(f"%{search[:100]}%"),Customer.name.ilike(f"%{search[:100]}%")))), Activity.lead_id.in_(db.query(Lead.id).filter(or_(Lead.company.ilike(f"%{search[:100]}%"),Lead.name.ilike(f"%{search[:100]}%"))))])
        q=q.filter(or_(*clauses))
    if status_filter and hasattr(spec["model"],"status"): q=q.filter(spec["model"].status==status_filter)
    if stage and module=="opportunities": q=q.filter(Opportunity.stage==stage)
    if record_date and module in ("followups","activities"):
        try: q=q.filter((FollowUp.follow_up_date if module=="followups" else Activity.activity_date)==date.fromisoformat(record_date))
        except ValueError: pass
    if activity_type and module=="activities" and activity_type in ACTIVITY_TYPES: q=q.filter(Activity.activity_type==activity_type)
    if related_id and module=="followups":
        try:
            kind, raw_id=related_id.split(":",1); raw_id=int(raw_id)
            if kind=="customer": q=q.filter(FollowUp.customer_id==raw_id)
            elif kind=="lead": q=q.filter(FollowUp.lead_id==raw_id)
            elif kind=="opportunity": q=q.filter(FollowUp.opportunity_id==raw_id)
        except (ValueError, TypeError): pass
    if assigned_to and user.role!="Sales Executive" and hasattr(spec["model"],"assigned_to"):
        try:q=q.filter(spec["model"].assigned_to==int(assigned_to))
        except:pass
    q=q.order_by(spec["model"].id.desc())
    total=q.count(); records=q.limit(200).all()
    rows=[row_display(db,user,module,obj) for obj in records]
    selected=scoped_query(db,spec["model"],user).filter(spec["model"].id==edit).first() if edit else None
    form_fields=build_fields(db,user,module,selected)
    message=request.query_params.get("msg", "")
    notice=request.session.pop("notice", "")
    error=request.query_params.get("error", "")
    return templates.TemplateResponse(request,"module.html",{"request":request,"user":user,"csrf_token":csrf_for(request),"active":module,"module":module,"spec":spec,"fields":form_fields,"rows":rows,"search":search,"status_filter":status_filter,"stage":stage,"assigned_filter":assigned_to,"total":total,"editing":selected is not None,"entity_id":selected.id if selected else "","msg":message,"error":error,"notice":notice,"users":db.query(User).filter(User.is_active.is_(True)).all(),"status_options":{"customers":["Active","Prospect","Inactive"],"leads":LEAD_STATUSES,"opportunities":OPP_STATUSES,"followups":FOLLOWUP_STATUSES,"activities":["Planned","Completed","Cancelled"]}[module],"record_date":record_date,"related_id":related_id,"activity_type":activity_type,"activity_types":ACTIVITY_TYPES,"related_options":[(f"customer:{c.id}",f"{c.company} · customer") for c in scoped_query(db,Customer,user).order_by(Customer.company).all()]+[(f"lead:{l.id}",f"{l.company} · lead") for l in scoped_query(db,Lead,user).order_by(Lead.company).all()]+[(f"opportunity:{o.id}",f"{o.name} · opportunity") for o in scoped_query(db,Opportunity,user).order_by(Opportunity.name).all()]})

@app.post("/{module}/save")
async def module_save(module: str, request: Request, db: Session=Depends(get_db)):
    if module not in MODULES: raise HTTPException(status_code=404,detail="Page not found")
    user=browser_user(request,db)
    if isinstance(user,RedirectResponse): return user
    form=await request.form(); check_csrf(request,form.get("csrf_token"))
    try: obj=validate_and_save(db,request,user,module,form)
    except ValueError as exc:
        return RedirectResponse(f"/{module}?error={__import__('urllib.parse').parse.quote(str(exc))}",status_code=303)
    return RedirectResponse(f"/{module}?msg={__import__('urllib.parse').parse.quote(MODULES[module]['singular']+' saved successfully.')}",status_code=303)

@app.post("/{module}/delete")
async def module_delete(module: str, request: Request, db: Session=Depends(get_db)):
    if module not in MODULES: raise HTTPException(status_code=404,detail="Page not found")
    user=browser_user(request,db)
    if isinstance(user,RedirectResponse): return user
    form=await request.form(); check_csrf(request,form.get("csrf_token"))
    try: rid=int(form.get("entity_id", "0"))
    except: rid=0
    model=MODULES[module]["model"]; obj=scoped_query(db,model,user).filter(model.id==rid).first()
    if not obj: return RedirectResponse(f"/{module}?error=Record+not+found+or+access+denied.",status_code=303)
    old=snapshot(obj)
    if module=="customers": obj.is_deleted=True; obj.status="Inactive"
    elif module=="leads": obj.status="Lost"
    elif module=="opportunities": obj.status="Lost"; obj.stage="Lost"; obj.probability=0
    elif module=="followups": obj.status="Cancelled"
    elif module=="activities": obj.status="Cancelled"
    audit(db,request,user,"DELETE_OR_DEACTIVATE",MODULES[module]["singular"],obj.id,old,snapshot(obj)); db.commit()
    return RedirectResponse(f"/{module}?msg=Record+deactivated+and+audit+logged.",status_code=303)


@app.get("/customers/{customer_id}/export")
def customer_export(customer_id:int,request:Request,db:Session=Depends(get_db)):
    user=browser_user(request,db)
    if isinstance(user,RedirectResponse): return user
    customer=scoped_query(db,Customer,user).filter(Customer.id==customer_id).first()
    if not customer: raise HTTPException(status_code=404,detail="Customer not found or access denied")
    related_activities=scoped_query(db,Activity,user).filter(Activity.customer_id==customer.id).order_by(Activity.activity_date.desc()).all()
    related_followups=scoped_query(db,FollowUp,user).filter(FollowUp.customer_id==customer.id).order_by(FollowUp.follow_up_date.desc()).all()
    related_opps=scoped_query(db,Opportunity,user).filter(Opportunity.customer_id==customer.id).all()
    export={"exported_at_utc":datetime.utcnow().isoformat()+"Z","customer":{"id":customer.id,"code":customer.code,"name":customer.name,"email":customer.email,"phone":"+91 "+customer.phone,"company":customer.company,"industry":customer.industry,"address":customer.address,"city":customer.city,"state":customer.state,"gst_number":customer.gst_number,"status":customer.status,"created_at":customer.created_at.isoformat()},"activities":[{"id":x.id,"type":x.activity_type,"subject":x.subject,"date":x.activity_date.isoformat(),"status":x.status,"sentiment":x.sentiment} for x in related_activities],"followups":[{"id":x.id,"subject":x.subject,"date":x.follow_up_date.isoformat(),"status":x.status,"type":x.follow_up_type} for x in related_followups],"opportunities":[{"id":x.id,"name":x.name,"amount":x.amount,"stage":x.stage,"probability":x.probability,"status":x.status} for x in related_opps]}
    audit(db,request,user,"DATA_EXPORT","Customer",customer.id,"","Customer JSON export generated");db.commit()
    return JSONResponse(export,headers={"Content-Disposition":f'attachment; filename="customer-{customer.id}-export.json"'})

@app.post("/customers/{customer_id}/forget")
async def customer_forget(customer_id:int,request:Request,db:Session=Depends(get_db)):
    user=browser_user(request,db)
    if isinstance(user,RedirectResponse):return user
    if user.role!="Admin":raise HTTPException(status_code=403,detail="Admin role required")
    form=await request.form();check_csrf(request,form.get("csrf_token"))
    customer=db.query(Customer).filter(Customer.id==customer_id,Customer.is_deleted.is_(False)).first()
    if not customer:return RedirectResponse("/customers?error=Customer+not+found.",status_code=303)
    customer_id_value=customer.id
    customer.name=f"Anonymized Customer {customer_id_value}"
    customer.email=f"forgotten-{customer_id_value}@privacy.invalid"
    candidate=f"9{(customer_id_value*7919)%1_000_000_000:09d}"
    while db.query(Customer).filter(Customer.phone==candidate,Customer.id!=customer_id_value).first():
        candidate=f"9{(int(candidate[1:])+1)%1_000_000_000:09d}"
    customer.phone=candidate
    customer.company=f"Anonymized Account {customer_id_value}";customer.address="";customer.gst_number="";customer.notes="";customer.is_deleted=True;customer.status="Inactive"
    # Intentionally no PII appears in this audit payload; related financial record IDs/totals remain linked.
    audit(db,request,user,"RIGHT_TO_BE_FORGOTTEN","Customer",customer_id_value,"",{"anonymized":True,"financial_relationships_retained":True})
    db.commit()
    return RedirectResponse("/customers?msg=Customer+anonymized%3B+linked+financial+records+retained.",status_code=303)

@app.post("/leads/{lead_id}/convert")
async def convert_lead(lead_id:int, request: Request, db:Session=Depends(get_db)):
    user=browser_user(request,db)
    if isinstance(user,RedirectResponse): return user
    form=await request.form(); check_csrf(request,form.get("csrf_token"))
    lead=scoped_query(db,Lead,user).filter(Lead.id==lead_id).first()
    if not lead: return RedirectResponse("/leads?error=Lead+not+found+or+access+denied.",status_code=303)
    if lead.status!="Qualified": return RedirectResponse("/leads?error=Only+Qualified+leads+can+be+converted.",status_code=303)
    email=lead.email if valid_email(lead.email) else f"converted-{lead.id}@example.invalid"
    phone=lead.phone if valid_phone(lead.phone) else f"9{lead.id:09d}"[-10:]
    if db.query(Customer).filter(or_(func.lower(Customer.email)==email.lower(),Customer.phone==phone),Customer.is_deleted.is_(False)).first():
        return RedirectResponse("/leads?error=Customer+email+or+phone+already+exists.+Review+the+customer+before+conversion.",status_code=303)
    customer=Customer(code=f"CUS-CNV-{lead.id}",name=lead.name,email=email,phone=phone,company=lead.company,industry=lead.industry,city="Pune",state="Maharashtra",assigned_to=lead.assigned_to,created_by=user.id,notes="Created from qualified lead conversion.")
    db.add(customer); db.flush()
    value=max(1,lead.expected_value or 50000)
    opp=Opportunity(name=f"{lead.company} — converted opportunity",customer_id=customer.id,lead_id=lead.id,amount=value,stage="Qualification",probability=35,expected_close_date=date.today()+timedelta(days=30),status="Open",source=lead.source,assigned_to=lead.assigned_to,notes="Created automatically during lead conversion.")
    db.add(opp); lead.status="Converted"; lead.converted_customer_id=customer.id
    audit(db,request,user,"CONVERT","Lead",lead.id,{"status":"Qualified"},{"status":"Converted","customer_id":customer.id,"opportunity_id":opp.id})
    audit(db,request,user,"CREATE","Customer",customer.id,"",snapshot(customer)); db.commit()
    return RedirectResponse("/leads?msg=Lead+converted+to+customer+and+opportunity.",status_code=303)

# -- User and role administration --
@app.get("/users",response_class=HTMLResponse)
def users_page(request:Request,db:Session=Depends(get_db)):
    user=browser_user(request,db)
    if isinstance(user,RedirectResponse): return user
    if user.role!="Admin": raise HTTPException(status_code=403,detail="Admin role required")
    users=db.query(User).order_by(User.id).all()
    return templates.TemplateResponse(request,"users.html",{"request":request,"user":user,"csrf_token":csrf_for(request),"active":"users","users":users,"now":datetime.utcnow(),"msg":request.query_params.get("msg",""),"error":request.query_params.get("error","")})

@app.post("/users/create")
async def users_create(request:Request,db:Session=Depends(get_db)):
    user=browser_user(request,db)
    if isinstance(user,RedirectResponse): return user
    if user.role!="Admin": raise HTTPException(status_code=403,detail="Admin role required")
    form=await request.form();check_csrf(request,form.get("csrf_token"))
    name=str(form.get("name","")).strip(); email=str(form.get("email","")).strip().lower(); password=str(form.get("password", "")); role=str(form.get("role", ""))
    if not name or not valid_email(email) or role not in ROLES or not valid_password(password): return RedirectResponse("/users?error=Check+name%2C+email%2C+role+and+12-character+password+policy.",status_code=303)
    if db.query(User).filter(func.lower(User.email)==email).first(): return RedirectResponse("/users?error=Email+already+exists.",status_code=303)
    new=User(name=name,email=email,password_hash=ph.hash(password),role=role);db.add(new);db.flush();audit(db,request,user,"CREATE","User",new.id,"",{"name":name,"role":role});db.commit()
    return RedirectResponse("/users?msg=User+created.",status_code=303)

@app.post("/users/{uid}/role")
async def users_role(uid:int,request:Request,db:Session=Depends(get_db)):
    user=browser_user(request,db)
    if isinstance(user,RedirectResponse):return user
    if user.role!="Admin":raise HTTPException(status_code=403,detail="Admin role required")
    form=await request.form();check_csrf(request,form.get("csrf_token"));role=str(form.get("role",""))
    target=db.get(User,uid)
    if not target or role not in ROLES:return RedirectResponse("/users?error=User+or+role+invalid.",status_code=303)
    old={"role":target.role};target.role=role;audit(db,request,user,"ROLE_CHANGE","User",uid,old,{"role":role});db.commit()
    return RedirectResponse("/users?msg=Role+updated+and+audited.",status_code=303)

@app.post("/users/{uid}/toggle")
async def users_toggle(uid:int,request:Request,db:Session=Depends(get_db)):
    user=browser_user(request,db)
    if isinstance(user,RedirectResponse):return user
    if user.role!="Admin":raise HTTPException(status_code=403,detail="Admin role required")
    form=await request.form();check_csrf(request,form.get("csrf_token"));target=db.get(User,uid)
    if not target or target.id==user.id:return RedirectResponse("/users?error=Cannot+change+this+user.",status_code=303)
    old={"is_active":target.is_active};target.is_active=not target.is_active
    audit(db,request,user,"USER_STATUS_CHANGE","User",uid,old,{"is_active":target.is_active});db.commit()
    return RedirectResponse("/users?msg=User+status+changed.",status_code=303)


@app.post("/users/{uid}/unlock")
async def users_unlock(uid:int,request:Request,db:Session=Depends(get_db)):
    user=browser_user(request,db)
    if isinstance(user,RedirectResponse):return user
    if user.role!="Admin":raise HTTPException(status_code=403,detail="Admin role required")
    form=await request.form();check_csrf(request,form.get("csrf_token"));target=db.get(User,uid)
    if not target:return RedirectResponse("/users?error=User+not+found.",status_code=303)
    old={"failed_login_count":target.failed_login_count,"locked":bool(target.lockout_end and target.lockout_end>datetime.utcnow())}
    target.failed_login_count=0;target.lockout_end=None
    audit(db,request,user,"ADMIN_UNLOCK","User",uid,old,{"locked":False});db.commit()
    return RedirectResponse("/users?msg=Account+unlocked+and+audited.",status_code=303)

@app.get("/audit",response_class=HTMLResponse)
def audit_page(request:Request,db:Session=Depends(get_db)):
    user=browser_user(request,db)
    if isinstance(user,RedirectResponse):return user
    if user.role!="Admin":raise HTTPException(status_code=403,detail="Admin role required")
    q=db.query(AuditLog).order_by(AuditLog.created_at.desc())
    action=request.query_params.get("action",""); entity=request.query_params.get("entity","")
    if action:q=q.filter(AuditLog.action==action)
    if entity:q=q.filter(AuditLog.entity_name.ilike(f"%{entity[:80]}%"))
    rows=q.limit(300).all()
    return templates.TemplateResponse(request,"audit.html",{"request":request,"user":user,"csrf_token":csrf_for(request),"active":"audit","rows":rows,"action":action,"entity":entity})

# -- Privacy-preserving hover summaries, cached for ten minutes. --
@app.post("/api/ai/rescore-all")
def ai_rescore_all(request:Request,db:Session=Depends(get_db)):
    user=api_user(request,db);require_api_csrf(request)
    if user.role not in ("Admin","Manager"): raise HTTPException(status_code=403,detail="Admin or Manager role required")
    leads=scoped_query(db,Lead,user).all()
    sidecar_ready=ai_is_available()
    for lead in leads:
        if sidecar_ready: score_and_store(lead)
        else:
            score=fallback_score(lead);lead.ai_score=float(score["score"]);lead.ai_band=score["band"];lead.ai_reasons=json.dumps(score["reasons"])
    audit(db,request,user,"AI_RESCORE_ALL","Lead","*","",{"rescored_count":len(leads),"mode":"ai-service" if sidecar_ready else "deterministic fallback"})
    db.commit()
    return {"rescored_count":len(leads),"scope":"company" if user.role=="Admin" else "team","scoring_service":AI_URL}

@app.get("/api/ai/summary")
def ai_summary(request:Request, entity_type:str, entity_id:int, db:Session=Depends(get_db)):
    user=api_user(request,db); key=f"{user.id}:{entity_type}:{entity_id}"
    cached=SUMMARY_CACHE.get(key)
    if cached and datetime.utcnow()-cached[0]<timedelta(minutes=10): return cached[1]
    payload=None
    if entity_type=="lead":
        lead=scoped_query(db,Lead,user).filter(Lead.id==entity_id).first()
        if not lead:raise HTTPException(status_code=404,detail="Lead not found")
        days=max(0,(date.today()-lead.last_contact_date).days) if lead.last_contact_date else 30
        overdue=db.query(FollowUp).filter(FollowUp.lead_id==lead.id,FollowUp.status=="Planned",FollowUp.follow_up_date<date.today()).count()
        open_opps=db.query(Opportunity).filter(Opportunity.lead_id==lead.id,Opportunity.status=="Open").all()
        payload=outbound_summary_signals(entity_type="lead",record_id=lead.id,days_silent=days,overdue_followups=overdue,open_opportunities=len(open_opps),pipeline_value=sum(o.amount for o in open_opps),ai_score=lead.ai_score,status=lead.status,sentiment="Neutral")
    elif entity_type=="customer":
        c=scoped_query(db,Customer,user).filter(Customer.id==entity_id).first()
        if not c:raise HTTPException(status_code=404,detail="Customer not found")
        last=db.query(Activity).filter(Activity.customer_id==c.id).order_by(Activity.activity_date.desc()).first()
        days=(date.today()-last.activity_date).days if last else 30
        overdue=db.query(FollowUp).filter(FollowUp.customer_id==c.id,FollowUp.status=="Planned",FollowUp.follow_up_date<date.today()).count()
        opps=db.query(Opportunity).filter(Opportunity.customer_id==c.id,Opportunity.status=="Open").all()
        payload=outbound_summary_signals(entity_type="customer",record_id=c.id,days_silent=days,overdue_followups=overdue,open_opportunities=len(opps),pipeline_value=sum(o.amount for o in opps),ai_score=0,status=c.status,sentiment=last.sentiment if last else "Neutral")
    else: raise HTTPException(status_code=400,detail="entity_type must be lead or customer")
    result=call_ai("/ai/summary",payload,timeout=3.2) or {"summary":f"Account {entity_id} has limited recent signal. Risk: Medium.","risk_level":"Medium","sentiment":"Neutral","next_action":"Confirm ownership and schedule the next customer touchpoint.","source":"web_fallback"}
    SUMMARY_CACHE[key]=(datetime.utcnow(),result)
    return result

# -- REST authentication uses the same signed session and lockout policy as the web login. --
class ApiLoginPayload(BaseModel):
    email: str
    password: str

@app.post("/api/auth/login")
def api_auth_login(payload:ApiLoginPayload,request:Request,db:Session=Depends(get_db)):
    ip=ip_for(request); now=datetime.utcnow()
    recent=[t for t in AUTH_ATTEMPTS.get(ip,[]) if now-t<timedelta(minutes=1)]
    if len(recent)>=10: raise HTTPException(status_code=429,detail="Too many login attempts. Try again shortly.")
    recent.append(now);AUTH_ATTEMPTS[ip]=recent
    email=payload.email.strip().lower();user=db.query(User).filter(func.lower(User.email)==email).first()
    if user and user.lockout_end and user.lockout_end>now:
        audit(db,request,user,"LOGIN_BLOCKED","User",user.id,"","Account lockout active","Blocked");db.commit()
        raise HTTPException(status_code=401,detail="Account temporarily locked. Try again after 15 minutes.")
    verified=False
    if user and user.is_active:
        try:verified=ph.verify(user.password_hash,payload.password)
        except Exception:verified=False
    if not verified:
        if user:
            user.failed_login_count+=1
            if user.failed_login_count>=5:
                user.failed_login_count=0;user.lockout_end=now+timedelta(minutes=15)
                audit(db,request,user,"LOCKOUT","User",user.id,"","5 failed attempts; locked 15 minutes","Blocked")
            audit(db,request,user,"LOGIN_FAILED","User",user.id,"","API credentials rejected","Failed")
        else: audit(db,request,None,"LOGIN_FAILED","User",email[:80],"","Unknown API account","Failed")
        db.commit();raise HTTPException(status_code=401,detail="Invalid credentials or inactive account")
    user.failed_login_count=0;user.lockout_end=None;request.session.clear();request.session["user_id"]=user.id;request.session["csrf_token"]=secrets.token_urlsafe(32)
    audit(db,request,user,"LOGIN_SUCCESS","User",user.id,"","API login successful");db.commit()
    AUTH_ATTEMPTS[ip]=[]
    return {"user":{"id":user.id,"name":user.name,"email":user.email,"role":user.role},"csrf_token":request.session["csrf_token"]}

@app.post("/api/auth/logout")
def api_auth_logout(request:Request,db:Session=Depends(get_db)):
    user=api_user(request,db);require_api_csrf(request)
    audit(db,request,user,"LOGOUT","User",user.id,"","API session terminated");db.commit();request.session.clear()
    return {"message":"Signed out successfully"}

# -- DTO-only REST API. Cookie-authenticated in this assessment build; write requests also require CSRF. --
def require_api_csrf(request:Request):
    check_csrf(request,request.headers.get("x-csrf-token"))


def choose_api_owner(db:Session,user:User,requested:int|None,industry:str="Technology"):
    if user.role=="Sales Executive": return user.id
    if requested:
        target=db.get(User,requested)
        if not target or not target.is_active or target.role!="Sales Executive": raise HTTPException(status_code=400,detail="Owner must be an active Sales Executive.")
        return target.id
    owner,_reason=auto_assign(db,industry)
    return owner

def customer_dto(c:Customer,user:User):
    full=user.role!="Sales Executive" or c.assigned_to==user.id
    return {"id":c.id,"code":c.code,"name":c.name,"email":safe_email(c.email,full),"phone":safe_phone(c.phone,full),"company":c.company,"industry":c.industry,"city":c.city,"state":c.state,"status":c.status,"assigned_to":c.assigned_to,"created_at":c.created_at.isoformat()}
def lead_dto(l:Lead):return {"id":l.id,"code":l.code,"name":l.name,"email":l.email,"company":l.company,"industry":l.industry,"source":l.source,"status":l.status,"expected_value":l.expected_value,"ai_score":l.ai_score,"ai_band":l.ai_band,"assigned_to":l.assigned_to}
def opp_dto(o:Opportunity):return {"id":o.id,"name":o.name,"customer_id":o.customer_id,"amount":o.amount,"stage":o.stage,"probability":o.probability,"expected_close_date":o.expected_close_date.isoformat(),"status":o.status,"assigned_to":o.assigned_to}

@app.get("/api/customers")
def api_customers(request:Request,search:str="",db:Session=Depends(get_db)):
    user=api_user(request,db);q=scoped_query(db,Customer,user)
    if search:q=q.filter(or_(Customer.name.ilike(f"%{search[:100]}%"),Customer.email.ilike(f"%{search[:100]}%"),Customer.phone.ilike(f"%{search[:100]}%"),Customer.company.ilike(f"%{search[:100]}%")))
    return {"items":[customer_dto(c,user) for c in q.order_by(Customer.id.desc()).limit(200).all()],"count":q.count()}

@app.get("/api/customers/{cid}")
def api_customer(cid:int,request:Request,db:Session=Depends(get_db)):
    user=api_user(request,db);c=scoped_query(db,Customer,user).filter(Customer.id==cid).first()
    if not c:raise HTTPException(status_code=404,detail="Customer not found")
    return customer_dto(c,user)

class CustomerPayload(BaseModel):
    name:str=Field(min_length=1,max_length=120);email:str;phone:str;company:str=Field(min_length=1,max_length=150);industry:str="Technology";city:str="Pune";state:str="Maharashtra";status:str="Active";assigned_to:int|None=None;gst_number:str=""
@app.post("/api/customers",status_code=201)
def api_customer_create(payload:CustomerPayload,request:Request,db:Session=Depends(get_db)):
    user=api_user(request,db);require_api_csrf(request)
    if not valid_email(payload.email):raise HTTPException(status_code=400,detail="Enter a valid email address.")
    if not valid_phone(payload.phone):raise HTTPException(status_code=400,detail="Enter a valid phone number.")
    if payload.status not in ["Active","Prospect","Inactive"]:raise HTTPException(status_code=400,detail="Select a valid customer status.")
    if db.query(Customer).filter(or_(func.lower(Customer.email)==payload.email.lower(),Customer.phone==re.sub(r"\D","",payload.phone)),Customer.is_deleted.is_(False)).first():raise HTTPException(status_code=409,detail="A customer with this email or phone already exists.")
    owner=choose_api_owner(db,user,payload.assigned_to,payload.industry)
    c=Customer(code=f"CUS-{secrets.token_hex(4).upper()}",name=payload.name,email=payload.email.lower(),phone=re.sub(r"\D","",payload.phone),company=payload.company,industry=payload.industry,city=payload.city,state=payload.state,status=payload.status,assigned_to=owner,gst_number=payload.gst_number,created_by=user.id)
    db.add(c);db.flush();audit(db,request,user,"CREATE","Customer",c.id,"",snapshot(c));db.commit();return customer_dto(c,user)

@app.put("/api/customers/{cid}")
def api_customer_update(cid:int,payload:CustomerPayload,request:Request,db:Session=Depends(get_db)):
    user=api_user(request,db);require_api_csrf(request);c=scoped_query(db,Customer,user).filter(Customer.id==cid).first()
    if not c:raise HTTPException(status_code=404,detail="Customer not found")
    if not valid_email(payload.email):raise HTTPException(status_code=400,detail="Enter a valid email address.")
    if not valid_phone(payload.phone):raise HTTPException(status_code=400,detail="Enter a valid phone number.")
    dup=db.query(Customer).filter(Customer.id!=cid,Customer.is_deleted.is_(False),or_(func.lower(Customer.email)==payload.email.lower(),Customer.phone==re.sub(r"\D","",payload.phone))).first()
    if dup:raise HTTPException(status_code=409,detail="A customer with this email or phone already exists.")
    if payload.status not in ["Active","Prospect","Inactive"]:raise HTTPException(status_code=400,detail="Select a valid customer status.")
    if user.role!="Sales Executive" and payload.assigned_to:
        target=db.get(User,payload.assigned_to)
        if not target or not target.is_active or target.role!="Sales Executive":raise HTTPException(status_code=400,detail="Owner must be an active Sales Executive.")
    old=snapshot(c)
    for k,v in payload.model_dump().items():
        if k=="assigned_to" and user.role=="Sales Executive":continue
        if k=="phone":v=re.sub(r"\D","",v)
        setattr(c,k,v)
    audit(db,request,user,"UPDATE","Customer",c.id,old,snapshot(c));db.commit();return customer_dto(c,user)

@app.delete("/api/customers/{cid}",status_code=204)
def api_customer_delete(cid:int,request:Request,db:Session=Depends(get_db)):
    user=api_user(request,db);require_api_csrf(request);c=scoped_query(db,Customer,user).filter(Customer.id==cid).first()
    if not c:raise HTTPException(status_code=404,detail="Customer not found")
    old=snapshot(c);c.is_deleted=True;c.status="Inactive";audit(db,request,user,"DELETE_OR_DEACTIVATE","Customer",cid,old,snapshot(c));db.commit();return None

class LeadPayload(BaseModel):
    name:str=Field(min_length=1,max_length=120);email:str="";phone:str="";company:str=Field(min_length=1,max_length=150);industry:str="Technology";source:str="Website";status:str="New";expected_value:float=Field(ge=0,le=100_000_000);interaction_count:int=Field(ge=0,le=10000,default=0);assigned_to:int|None=None
@app.get("/api/leads")
def api_leads(request:Request,db:Session=Depends(get_db)):
    user=api_user(request,db);leads=scoped_query(db,Lead,user).order_by(Lead.id.desc()).limit(200).all();return {"items":[lead_dto(l) for l in leads],"count":len(leads)}
@app.post("/api/leads",status_code=201)
def api_lead_create(payload:LeadPayload,request:Request,db:Session=Depends(get_db)):
    user=api_user(request,db);require_api_csrf(request)
    if payload.email and not valid_email(payload.email):raise HTTPException(status_code=400,detail="Enter a valid email address.")
    if payload.phone and not valid_phone(payload.phone):raise HTTPException(status_code=400,detail="Enter a valid phone number.")
    if payload.status not in LEAD_STATUSES:raise HTTPException(status_code=400,detail="Select a valid lead status.")
    owner=choose_api_owner(db,user,payload.assigned_to,payload.industry)
    reason="provided by authorised user" if payload.assigned_to else "workload-aware automatic assignment"
    l=Lead(code=f"LEAD-{secrets.token_hex(4).upper()}",name=payload.name,email=payload.email,phone=re.sub(r"\D","",payload.phone),company=payload.company,industry=payload.industry,source=payload.source,status=payload.status,expected_value=payload.expected_value,interaction_count=payload.interaction_count,assigned_to=owner,last_contact_date=date.today())
    scored=score_and_store(l);db.add(l);db.flush();audit(db,request,user,"CREATE","Lead",l.id,"",{"name":l.name,"status":l.status,"ai_score":l.ai_score});audit(db,request,user,"AI_AUTO_ASSIGN","Lead",l.id,"",{"assigned_to":owner,"reason":reason});db.commit();return lead_dto(l)

class OpportunityPayload(BaseModel):
    name:str=Field(min_length=1,max_length=150);customer_id:int|None=None;amount:float;stage:str="Qualification";probability:int=Field(ge=0,le=100);expected_close_date:date;status:str="Open";assigned_to:int|None=None
@app.get("/api/opportunities")
def api_opps(request:Request,db:Session=Depends(get_db)):
    user=api_user(request,db);items=scoped_query(db,Opportunity,user).order_by(Opportunity.id.desc()).limit(200).all();return {"items":[opp_dto(x) for x in items],"count":len(items)}
@app.post("/api/opportunities",status_code=201)
def api_opp_create(payload:OpportunityPayload,request:Request,db:Session=Depends(get_db)):
    user=api_user(request,db);require_api_csrf(request)
    if payload.status=="Open" and payload.amount<=0:raise HTTPException(status_code=400,detail="Opportunity Amount must be greater than 0.")
    if payload.amount<0:raise HTTPException(status_code=400,detail="Opportunity Amount cannot be negative.")
    if payload.status=="Open" and payload.expected_close_date<date.today():raise HTTPException(status_code=400,detail="Expected Close Date cannot be in the past.")
    if payload.stage not in OPP_STAGES or payload.status not in OPP_STATUSES:raise HTTPException(status_code=400,detail="Select a valid opportunity stage and status.")
    if payload.customer_id and not scoped_query(db,Customer,user).filter(Customer.id==payload.customer_id).first():raise HTTPException(status_code=404,detail="Customer not found")
    owner=choose_api_owner(db,user,payload.assigned_to,"Technology")
    o=Opportunity(name=payload.name,customer_id=payload.customer_id,amount=payload.amount,stage=payload.stage,probability=payload.probability,expected_close_date=payload.expected_close_date,status=payload.status,assigned_to=owner)
    db.add(o);db.flush();audit(db,request,user,"CREATE","Opportunity",o.id,"",snapshot(o));db.commit();return opp_dto(o)
