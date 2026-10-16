from datetime import datetime, date
from sqlalchemy import String, Integer, Float, Boolean, DateTime, Date, ForeignKey, Text, Index, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship
from .db import Base

class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    email: Mapped[str] = mapped_column(String(180), unique=True, index=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(30), nullable=False, default="Sales Executive", index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    failed_login_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    lockout_end: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)

class Customer(Base):
    __tablename__ = "customers"
    __table_args__ = (Index("ix_customer_status_owner", "status", "assigned_to"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(20), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    email: Mapped[str] = mapped_column(String(180), unique=True, nullable=False, index=True)
    phone: Mapped[str] = mapped_column(String(16), unique=True, nullable=False, index=True)
    company: Mapped[str] = mapped_column(String(150), nullable=False, index=True)
    industry: Mapped[str] = mapped_column(String(50), default="Technology")
    address: Mapped[str] = mapped_column(String(240), default="")
    city: Mapped[str] = mapped_column(String(80), default="Pune")
    state: Mapped[str] = mapped_column(String(80), default="Maharashtra")
    gst_number: Mapped[str] = mapped_column(String(20), default="")
    status: Mapped[str] = mapped_column(String(30), default="Active", index=True)
    assigned_to: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    notes: Mapped[str] = mapped_column(Text, default="")
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)

class Lead(Base):
    __tablename__ = "leads"
    __table_args__ = (Index("ix_lead_status_owner", "status", "assigned_to"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(20), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    email: Mapped[str] = mapped_column(String(180), default="")
    phone: Mapped[str] = mapped_column(String(16), default="")
    company: Mapped[str] = mapped_column(String(150), nullable=False, index=True)
    industry: Mapped[str] = mapped_column(String(50), default="Technology")
    source: Mapped[str] = mapped_column(String(40), default="Website")
    status: Mapped[str] = mapped_column(String(30), default="New", index=True)
    expected_value: Mapped[float] = mapped_column(Float, default=0)
    interaction_count: Mapped[int] = mapped_column(Integer, default=0)
    last_contact_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    ai_score: Mapped[float] = mapped_column(Float, default=0)
    ai_band: Mapped[str] = mapped_column(String(20), default="Cold")
    ai_reasons: Mapped[str] = mapped_column(Text, default="")
    assigned_to: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    converted_customer_id: Mapped[int | None] = mapped_column(ForeignKey("customers.id"), nullable=True)

class Opportunity(Base):
    __tablename__ = "opportunities"
    __table_args__ = (Index("ix_opportunity_stage_owner", "stage", "assigned_to"), Index("ix_opportunity_close_date", "expected_close_date"))
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(150), nullable=False, index=True)
    customer_id: Mapped[int | None] = mapped_column(ForeignKey("customers.id"), nullable=True)
    lead_id: Mapped[int | None] = mapped_column(ForeignKey("leads.id"), nullable=True)
    amount: Mapped[float] = mapped_column(Float, nullable=False)
    stage: Mapped[str] = mapped_column(String(30), default="Qualification", index=True)
    probability: Mapped[int] = mapped_column(Integer, default=20)
    expected_close_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(20), default="Open", index=True)
    source: Mapped[str] = mapped_column(String(40), default="Website")
    notes: Mapped[str] = mapped_column(Text, default="")
    assigned_to: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)

class FollowUp(Base):
    __tablename__ = "followups"
    __table_args__ = (Index("ix_followup_date_status_owner", "follow_up_date", "status", "assigned_to"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    customer_id: Mapped[int | None] = mapped_column(ForeignKey("customers.id"), nullable=True)
    lead_id: Mapped[int | None] = mapped_column(ForeignKey("leads.id"), nullable=True)
    opportunity_id: Mapped[int | None] = mapped_column(ForeignKey("opportunities.id"), nullable=True)
    follow_up_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    follow_up_type: Mapped[str] = mapped_column(String(30), default="Call")
    subject: Mapped[str] = mapped_column(String(150), nullable=False)
    remarks: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(30), default="Planned", index=True)
    assigned_to: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)

class Activity(Base):
    __tablename__ = "activities"
    __table_args__ = (Index("ix_activity_type_date_owner", "activity_type", "activity_date", "assigned_to"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    activity_type: Mapped[str] = mapped_column(String(30), default="Call", index=True)
    subject: Mapped[str] = mapped_column(String(150), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    activity_date: Mapped[date] = mapped_column(Date, default=date.today, index=True)
    customer_id: Mapped[int | None] = mapped_column(ForeignKey("customers.id"), nullable=True)
    lead_id: Mapped[int | None] = mapped_column(ForeignKey("leads.id"), nullable=True)
    assigned_to: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(30), default="Completed", index=True)
    sentiment: Mapped[str] = mapped_column(String(20), default="Neutral")

class AuditLog(Base):
    __tablename__ = "audit_logs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    role: Mapped[str] = mapped_column(String(30), default="Anonymous")
    action: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    entity_name: Mapped[str] = mapped_column(String(60), nullable=False, index=True)
    record_id: Mapped[str] = mapped_column(String(60), default="")
    old_value: Mapped[str] = mapped_column(Text, default="")
    new_value: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(20), default="Success")
    ip_address: Mapped[str] = mapped_column(String(60), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False, index=True)

# Explicit uniqueness is also enforced by SQLite constraints; indexes make frequent filters cheap.
