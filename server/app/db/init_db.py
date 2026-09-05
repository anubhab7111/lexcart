"""
Initialize the database: create tables from schema.sql if they are missing,
then seed demo lawyers, upsell addons, and a demo AI-buyer API key.
Idempotent — safe to run repeatedly.

Usage: python -m app.db.init_db
"""

import hashlib
import secrets
from pathlib import Path

import bcrypt
from sqlalchemy import inspect
from sqlmodel import Session

from app.db.engine import get_engine
from app.db.migrations import run_migrations
from app.db.models import AgentApiKey, Lawyer, ServiceAddon, User

SCHEMA_FILE = Path(__file__).parent / "schema.sql"

# Legacy mock ids '1'..'5' kept as primary keys so the client's lawyer
# routes keep working unchanged. Rates are INR per consultation.
SEED_LAWYERS = [
    Lawyer(
        id="1",
        name="Priya Raghunathan",
        specialty="Criminal Defense",
        experience=15,
        rating=4.9,
        hourly_rate=3500,
        location="Mumbai, Maharashtra",
        bio="Senior criminal defense advocate with a proven record in bail matters, white-collar crime, and trials under the BNS/IPC before the Bombay High Court.",
        cases=450,
        success_rate=92,
        education="NLSIU Bangalore, BA LLB (Hons)",
        languages=["English", "Hindi", "Marathi"],
        availability="Available this week",
    ),
    Lawyer(
        id="2",
        name="Arjun Mehta",
        specialty="Family Law",
        experience=12,
        rating=4.8,
        hourly_rate=2500,
        location="Delhi NCR",
        bio="Compassionate family lawyer specializing in divorce, child custody, maintenance, and adoption, with a strong focus on mediation-first resolution.",
        cases=380,
        success_rate=88,
        education="Faculty of Law, Delhi University, LLB",
        languages=["English", "Hindi", "Punjabi"],
        availability="Available next week",
    ),
    Lawyer(
        id="3",
        name="Kavya Krishnan",
        specialty="Business & Corporate Law",
        experience=18,
        rating=4.9,
        hourly_rate=5000,
        location="Bengaluru, Karnataka",
        bio="Corporate counsel for startups and SMEs: incorporation, founder agreements, fundraising, M&A, and Companies Act compliance.",
        cases=520,
        success_rate=95,
        education="NALSAR Hyderabad, BA LLB (Hons)",
        languages=["English", "Kannada", "Tamil"],
        availability="Available in 2 weeks",
    ),
    Lawyer(
        id="4",
        name="Rohan Deshpande",
        specialty="Personal Injury",
        experience=10,
        rating=4.7,
        hourly_rate=2000,
        location="Pune, Maharashtra",
        bio="Motor accident claims (MACT), workplace injury, and insurance dispute specialist fighting for maximum compensation for accident victims.",
        cases=340,
        success_rate=90,
        education="ILS Law College Pune, LLB",
        languages=["English", "Hindi", "Marathi"],
        availability="Available this week",
    ),
    Lawyer(
        id="5",
        name="Sneha Iyer",
        specialty="Real Estate Law",
        experience=14,
        rating=4.8,
        hourly_rate=3000,
        location="Chennai, Tamil Nadu",
        bio="Property lawyer handling sale deeds, title verification, RERA complaints, tenancy disputes, and ancestral property partition.",
        cases=410,
        success_rate=93,
        education="Government Law College Chennai, LLB",
        languages=["English", "Tamil", "Hindi"],
        availability="Available next week",
    ),
    # Priced above the agent per-order cap (₹25,000) on purpose: lets
    # demo/ai_buyer.py --over-budget trip the guardrail on its first
    # attempt instead of spending down the daily cap first. See
    # app/commerce/guardrails.py: check_order_bounds.
    Lawyer(
        id="6",
        name="Vikram Nair",
        specialty="Business & Corporate Law",
        experience=22,
        rating=4.9,
        hourly_rate=30000,
        location="Mumbai, Maharashtra",
        bio="Senior counsel offering a full-day retainer package: M&A due diligence, board advisory, and complex cross-border structuring for large enterprises.",
        cases=290,
        success_rate=97,
        education="NLSIU Bangalore, BA LLB (Hons); LLM, Harvard Law School",
        languages=["English", "Hindi"],
        availability="Available by appointment",
    ),
    Lawyer(
        id="7",
        name="Ananya Bhattacharya",
        specialty="Consumer Protection Law",
        experience=9,
        rating=4.7,
        hourly_rate=1800,
        location="Kolkata, West Bengal",
        bio="Consumer-forum specialist handling defective-product, e-commerce, insurance, and deficiency-of-service complaints under the Consumer Protection Act, 2019.",
        cases=260,
        success_rate=89,
        education="WBNUJS Kolkata, BA LLB (Hons)",
        languages=["English", "Hindi", "Bengali"],
        availability="Available this week",
    ),
    Lawyer(
        id="8",
        name="Rahul Kapoor",
        specialty="Intellectual Property Law",
        experience=13,
        rating=4.8,
        hourly_rate=4000,
        location="New Delhi, Delhi",
        bio="IP counsel for trademark, copyright, and design registration, oppositions, and infringement enforcement before the Trademark Registry and IP Division of the Delhi High Court.",
        cases=310,
        success_rate=91,
        education="National Law University, Delhi, BA LLB (Hons)",
        languages=["English", "Hindi"],
        availability="Available next week",
    ),
    Lawyer(
        id="9",
        name="Meera Pillai",
        specialty="Labour & Employment Law",
        experience=11,
        rating=4.7,
        hourly_rate=2200,
        location="Hyderabad, Telangana",
        bio="Employment-rights advocate for wrongful termination, gratuity and PF disputes, workplace harassment complaints, and negotiating severance settlements.",
        cases=295,
        success_rate=90,
        education="NALSAR Hyderabad, BA LLB (Hons)",
        languages=["English", "Hindi", "Telugu"],
        availability="Available this week",
    ),
    Lawyer(
        id="10",
        name="Devansh Trivedi",
        specialty="Banking & Cheque Bounce Law",
        experience=16,
        rating=4.8,
        hourly_rate=2600,
        location="Ahmedabad, Gujarat",
        bio="Section 138 (Negotiable Instruments Act) cheque-dishonour litigation and loan-recovery disputes, from statutory demand notice through trial.",
        cases=430,
        success_rate=94,
        education="Gujarat National Law University, BA LLB (Hons)",
        languages=["English", "Hindi", "Gujarati"],
        availability="Available in 2 weeks",
    ),
    Lawyer(
        id="11",
        name="Ishaan Bose",
        specialty="Tax Law",
        experience=17,
        rating=4.9,
        hourly_rate=4500,
        location="Jaipur, Rajasthan",
        bio="Direct-tax and GST disputes: assessment appeals, notices under the Income Tax Act, and representation before the Income Tax Appellate Tribunal.",
        cases=250,
        success_rate=93,
        education="National Law University, Jodhpur, BA LLB (Hons)",
        languages=["English", "Hindi"],
        availability="Available by appointment",
    ),
]

# Upsell catalog for the concierge's cross-sell suggestions.
SEED_ADDONS = [
    ServiceAddon(
        id="addon-doc-review",
        name="Document Review",
        description="Pre-consultation review of up to 20 pages of your documents (agreements, notices, FIRs) so the session starts fully informed.",
        price_inr=499,
        applies_to="",
    ),
    ServiceAddon(
        id="addon-followup-call",
        name="Follow-up Call (15 min)",
        description="A 15-minute follow-up call within 7 days of your consultation to clarify next steps.",
        price_inr=299,
        applies_to="",
    ),
    ServiceAddon(
        id="addon-written-opinion",
        name="Written Legal Opinion",
        description="A signed written opinion summarising the advice, applicable provisions, and recommended course of action.",
        price_inr=999,
        applies_to="Business & Corporate Law,Real Estate Law",
    ),
    ServiceAddon(
        id="addon-notice-draft",
        name="Legal Notice Draft",
        description="Drafting of one legal notice (demand notice, reply, or cease-and-desist) arising from the consultation.",
        price_inr=799,
        applies_to="Family Law,Real Estate Law,Personal Injury",
    ),
]

# Demo AI-buyer key for the agent-to-agent commerce demo (demo/ai_buyer.py).
# Test-mode only: a real deployment would mint keys per partner agent.
DEMO_AGENT_KEY = "lexcart_agent_demo_a7f3e9c1"


def init_db() -> None:
    engine = get_engine()

    if not inspect(engine).has_table("users"):
        sql = SCHEMA_FILE.read_text()
        with engine.begin() as conn:
            conn.exec_driver_sql(sql)
        print("Created tables from schema.sql")
    else:
        print("Tables already exist, skipping schema.sql")

    # Incremental changes for existing dev DBs — see app/db/migrations.py
    # for the convention (there is no Alembic in this project).
    run_migrations(engine)

    with Session(engine) as session:
        seeded = 0
        for lawyer in SEED_LAWYERS:
            if session.get(Lawyer, lawyer.id) is None:
                session.add(lawyer)
                seeded += 1
        session.commit()
        print(f"Seeded {seeded} lawyers ({len(SEED_LAWYERS) - seeded} already present)")

        seeded = 0
        for addon in SEED_ADDONS:
            if session.get(ServiceAddon, addon.id) is None:
                session.add(addon)
                seeded += 1
        session.commit()
        print(f"Seeded {seeded} addons ({len(SEED_ADDONS) - seeded} already present)")

        # Service account that owns bookings made by external AI buyers
        # (bookings.user_id is NOT NULL). Random password — not a login.
        if session.get(User, "agent-buyer") is None:
            session.add(
                User(
                    id="agent-buyer",
                    name="AI Buyer (external agent)",
                    email="agent-buyer@lexcart.local",
                    password=bcrypt.hashpw(
                        secrets.token_urlsafe(24).encode(), bcrypt.gensalt(rounds=10)
                    ).decode(),
                    role="client",
                )
            )
            session.commit()
            print("Seeded agent-buyer service account")

        # Same pattern for whoever pays a shared campaign payment link —
        # that flow has no signed-in user, only a webhook event (see
        # app/routers/webhooks.py).
        if session.get(User, "campaign-buyer") is None:
            session.add(
                User(
                    id="campaign-buyer",
                    name="Campaign Link Buyer (anonymous)",
                    email="campaign-buyer@lexcart.local",
                    password=bcrypt.hashpw(
                        secrets.token_urlsafe(24).encode(), bcrypt.gensalt(rounds=10)
                    ).decode(),
                    role="client",
                )
            )
            session.commit()
            print("Seeded campaign-buyer service account")

        key_hash = hashlib.sha256(DEMO_AGENT_KEY.encode()).hexdigest()
        from sqlmodel import select

        existing = session.exec(
            select(AgentApiKey).where(AgentApiKey.key_hash == key_hash)
        ).first()
        if existing is None:
            session.add(
                AgentApiKey(name="demo-ai-buyer", key_hash=key_hash, daily_limit_inr=50000)
            )
            session.commit()
            print(f"Seeded demo AI-buyer API key: {DEMO_AGENT_KEY}")
        else:
            print("Demo AI-buyer API key already present")


if __name__ == "__main__":
    init_db()
