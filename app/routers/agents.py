"""Agent team status — surfaces every agent (spec §6) and what it last did.

Lets the UI show the whole multi-agent "financial team", not just the Planner.
Status is derived from the current state of the system.
"""
from __future__ import annotations

from fastapi import APIRouter

from .. import config, domain_store, planner_store, storage, watcher_store

router = APIRouter(tags=["agents"])


@router.get("/agents", summary="Status of the whole agent team")
def agents(user_id: str = config.DEFAULT_USER_ID) -> dict:
    datasets = storage.list_datasets()
    obs = domain_store.list_obligations(user_id=user_id)
    txn = domain_store.list_transactions(user_id=user_id, limit=1)["total"]
    assessment = watcher_store.get_assessment(user_id)
    plan = planner_store.get_plan(user_id)
    has_data = len(obs) > 0 or txn > 0

    def fmt_eur(n):
        try:
            return "€" + format(round(float(n)), ",")
        except Exception:
            return "—"

    team = []

    # Organiser
    team.append({
        "key": "organiser", "name": "Organiser", "icon": "🧩",
        "role": "Reads your files and normalises everything into one clean model.",
        "status": "Live", "tone": "good", "link": "data",
        "active": len(datasets) > 0,
        "detail": (f"Normalised {len(obs)} debts and {txn} transactions from "
                   f"{len(datasets)} source(s).") if datasets
                  else "Waiting for data — import a file to get started.",
    })

    # Watcher
    if assessment:
        a = assessment
        sf = f" · shortfall {a['projected_shortfall_date']}" if a.get("projected_shortfall_date") else ""
        wdetail = (f"Risk {a['band']} ({a['score']}/100){sf} · "
                   f"{len(a.get('alerts', []))} alert(s).")
    else:
        wdetail = "Waiting for data — will watch your cash-flow for danger zones."
    team.append({
        "key": "watcher", "name": "Watcher", "icon": "🚨",
        "role": "Monitors cash-flow and flags danger zones early (advisory).",
        "status": "Live · advisory", "tone": "blue", "link": "risk",
        "active": assessment is not None, "detail": wdetail,
    })

    # Planner
    if plan:
        pdetail = (f"{plan['method'].title()} plan · debt-free {plan.get('debt_free_date') or '—'} · "
                   f"saves {fmt_eur(plan.get('interest_saved_vs_minimums'))} vs minimums.")
    else:
        pdetail = "Waiting for debts — will map the fastest, cheapest way out."
    team.append({
        "key": "planner", "name": "Planner", "icon": "🎯",
        "role": "Builds your payoff plan and runs what-if simulations (advisory).",
        "status": "Live · advisory", "tone": "blue", "link": "plan",
        "active": plan is not None, "detail": pdetail,
    })

    # Negotiator (not yet built — honest roadmap status)
    team.append({
        "key": "negotiator", "name": "Negotiator", "icon": "✉️",
        "role": "Drafts hardship / lower-rate letters for you to review and send.",
        "status": "Coming soon", "tone": "neutral", "link": None,
        "active": False,
        "detail": "Will draft creditor messages — you always review and send them yourself.",
    })

    # Supervisor + Compliance gate
    team.append({
        "key": "supervisor", "name": "Supervisor + Compliance", "icon": "🛡️",
        "role": "Coordinates the team, gates every action and keeps the audit trail.",
        "status": "Live", "tone": "good", "link": None,
        "active": has_data,
        "detail": ("Coordinated your last import and ran the team. No money moved — by design."
                   if has_data else "Ready to orchestrate your team on the next import."),
    })

    # Payer (dormant by design — no money movement in the MVP)
    team.append({
        "key": "payer", "name": "Payer", "icon": "🏦",
        "role": "Would move money via a licensed partner — disabled in this version.",
        "status": "Dormant · v1", "tone": "neutral", "link": None,
        "active": False,
        "detail": "Off by design. Debt OS never moves your money; you stay fully in control.",
    })

    live = sum(1 for t in team if t["active"])
    return {"count": len(team), "live": live, "agents": team}
