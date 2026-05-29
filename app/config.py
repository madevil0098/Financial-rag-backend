import os
from pathlib import Path

# Backend root: .../backend
BASE_DIR = Path(__file__).resolve().parent.parent

# Where ingested datasets and the registry live.
DATA_DIR = BASE_DIR / "data"
DATASETS_DIR = DATA_DIR / "datasets"
REGISTRY_PATH = DATA_DIR / "registry.json"

# Normalized domain entities (Obligation / Transaction) produced by the Organiser.
DOMAIN_DIR = DATA_DIR / "domain"
OBLIGATIONS_PATH = DOMAIN_DIR / "obligations.json"
TRANSACTIONS_PATH = DOMAIN_DIR / "transactions.json"

# Watcher agent: per-user latest RiskAssessment + AlertPreferences.
WATCHER_DIR = DATA_DIR / "watcher"
ASSESSMENTS_PATH = WATCHER_DIR / "assessments.json"
PREFERENCES_PATH = WATCHER_DIR / "preferences.json"

# Planner agent: per-user latest RepaymentPlan.
PLANNER_DIR = DATA_DIR / "planner"
PLANS_PATH = PLANNER_DIR / "plans.json"
# Safety cap so an infeasible (interest > payments) plan can't loop forever.
MAX_PAYOFF_MONTHS = 600

# Cash-flow projection horizon (days) and sensitivity threshold multipliers.
DEFAULT_HORIZON_DAYS = 60
# Higher sensitivity -> lower thresholds -> more alerts.
SENSITIVITY_MULTIPLIERS = {"low": 1.3, "medium": 1.0, "high": 0.75}

# Local Ollama LLM (no cloud API). Override via env.
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
# Normal model for routine structured tasks (Organiser mapping, Watcher summary).
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen3.5:4b")
OLLAMA_TIMEOUT = float(os.environ.get("OLLAMA_TIMEOUT", "120"))
# High-skill model for quality-critical generation (Planner explanation, Negotiator
# drafting). Skill matters more than latency here, so it gets a longer timeout.
OLLAMA_MODEL_SKILL = os.environ.get("OLLAMA_MODEL_SKILL", "qwen3.5:9b")
OLLAMA_TIMEOUT_SKILL = float(os.environ.get("OLLAMA_TIMEOUT_SKILL", "240"))

# Demo runs single-user until auth lands.
DEFAULT_USER_ID = "demo-user"
DEFAULT_CURRENCY = "EUR"

API_PREFIX = "/api/v1"

# Frontend origins allowed by CORS. Local React dev servers plus the deployed
# Vercel frontend. Origins must have NO trailing slash (the browser's Origin
# header never includes one). Add extra origins via the CORS_ORIGINS env var
# (comma-separated) without a code change.
CORS_ORIGINS = [
    "http://localhost:5173",
    "http://localhost:3000",
    "http://127.0.0.1:5173",
    "http://127.0.0.1:3000",
    "https://financial-rag-frontend-seven.vercel.app",
]
_extra_origins = os.environ.get("CORS_ORIGINS", "")
CORS_ORIGINS += [o.strip() for o in _extra_origins.split(",") if o.strip()]

# Cap on rows returned in a single preview page.
MAX_PREVIEW_LIMIT = 500

# Suggested dataset categories. Free-form: callers may send any string; these
# are the well-known financial buckets the product expects.
KNOWN_CATEGORIES = [
    "loan",
    "bank_statement",
    "payment_due",
    "credit_card",
    "bnpl",
    "other",
]
DEFAULT_CATEGORY = "uncategorized"
