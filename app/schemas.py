from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

SourceType = Literal["csv", "excel", "sql", "json"]


class ColumnSchema(BaseModel):
    name: str
    dtype: str  # friendly type: integer | float | string | boolean | datetime | other


class DatasetMeta(BaseModel):
    id: str
    name: str
    source_type: SourceType
    category: str = "uncategorized"  # loan | bank_statement | payment_due | ...
    group_id: Optional[str] = None  # links tables ingested from one source together
    table_name: Optional[str] = None  # sheet / DB table / JSON key this table came from
    original_filename: Optional[str] = None
    row_count: int
    column_count: int
    columns: list[ColumnSchema]
    size_bytes: int
    created_at: datetime
    extra: dict[str, Any] = Field(default_factory=dict)  # sheet name, query, table, etc.


class IngestResult(BaseModel):
    """Result of one ingestion call, which may produce one or many tables."""

    group_id: str
    source_type: SourceType
    category: str
    count: int
    datasets: list[DatasetMeta]


class DatasetList(BaseModel):
    count: int
    datasets: list[DatasetMeta]


class PreviewResponse(BaseModel):
    id: str
    total_rows: int
    offset: int
    limit: int
    returned: int
    columns: list[str]
    rows: list[dict[str, Any]]


class SQLIngestRequest(BaseModel):
    connection_string: str = Field(
        ...,
        description="SQLAlchemy URL, e.g. postgresql+psycopg://user:pass@host/db or sqlite:///path.db",
    )
    query: Optional[str] = Field(
        None, description="SQL SELECT to run. Provide one of: query, table, tables, all_tables."
    )
    table: Optional[str] = Field(
        None, description="Single table name to read in full."
    )
    tables: Optional[list[str]] = Field(
        None, description="Several table names; each becomes its own dataset/table."
    )
    all_tables: bool = Field(
        False, description="Reflect and ingest every table in the database."
    )
    name: Optional[str] = Field(None, description="Friendly name / group label.")
    category: Optional[str] = Field(None, description="Category for the ingested tables.")


class DeleteResponse(BaseModel):
    deleted: bool
    id: str


# ---------------------------------------------------------------------------
# Normalized domain entities (produced by the Organiser agent)
# ---------------------------------------------------------------------------
class Obligation(BaseModel):
    id: str
    user_id: str
    creditor: Optional[str] = None
    kind: str = "other"  # credit_card | loan | bnpl | overdraft | mortgage | other
    balance: Optional[float] = None
    apr: Optional[float] = None
    min_payment: Optional[float] = None
    due_day: Optional[int] = None
    currency: str = "EUR"
    source: str  # dataset id this was normalized from
    source_category: Optional[str] = None
    created_at: datetime


class Transaction(BaseModel):
    id: str
    user_id: str
    date: Optional[str] = None  # ISO date
    amount: Optional[float] = None
    category: Optional[str] = None
    counterparty: Optional[str] = None
    source: str
    created_at: datetime


TargetEntity = Literal["obligation", "transaction"]


class NormaliseRequest(BaseModel):
    target: Optional[TargetEntity] = Field(
        None, description="Force 'obligation' or 'transaction'. Default: inferred from category/columns."
    )
    use_llm: bool = Field(True, description="Refine the column mapping with the local LLM.")
    mapping_override: Optional[dict[str, Optional[str]]] = Field(
        None, description="Manually pin canonical_field -> raw_column (wins over heuristic + LLM)."
    )


class NormaliseResult(BaseModel):
    dataset_id: str
    target: TargetEntity
    category: Optional[str] = None
    mapping: dict[str, Optional[str]]  # canonical_field -> raw column (or null)
    unmapped_fields: list[str]
    record_count: int
    mapping_source: str  # "heuristic" | "llm" | "override"
    llm_model: Optional[str] = None
    sample: list[dict[str, Any]]


class ObligationList(BaseModel):
    count: int
    obligations: list[Obligation]


# ---------------------------------------------------------------------------
# Watcher agent (Risk Monitor) — advisory only
# ---------------------------------------------------------------------------
class RiskDriver(BaseModel):
    factor: str  # projected_shortfall | cashflow_coverage | debt_to_income | low_buffer
    detail: str
    impact: float  # contribution to the 0-100 score


class Alert(BaseModel):
    id: str
    type: str  # projected_shortfall | negative_cashflow | high_debt_burden | low_buffer | no_income_data
    severity: str  # info | warning | critical
    title: str
    explanation: str
    suggested_actions: list[str] = Field(default_factory=list)
    advisory: bool = True  # always advisory — never an automated decision (GDPR Art.22)
    created_at: datetime


class CashflowPoint(BaseModel):
    date: str
    projected_balance: float


class CashflowSummary(BaseModel):
    monthly_income: float
    monthly_expenses: float  # non-obligation outgoings
    monthly_obligations: float  # sum of obligation minimum payments
    monthly_net: float
    months_of_history: float
    starting_balance: float
    starting_balance_assumed: bool  # True if we estimated it (no value supplied)


class RiskAssessment(BaseModel):
    id: str
    user_id: str
    score: int  # 0-100
    band: str  # low | moderate | elevated | high
    projected_shortfall_date: Optional[str] = None
    horizon_days: int
    summary: str  # plain-language (LLM or template)
    drivers: list[RiskDriver]
    alerts: list[Alert]
    cashflow: CashflowSummary
    projection: list[CashflowPoint]
    llm_used: bool = False
    created_at: datetime


class AlertPreferences(BaseModel):
    user_id: str
    sensitivity: str = "medium"  # low | medium | high
    horizon_days: int = 60
    safety_buffer: float = 0.0  # alert if projected balance dips below this
    channels: list[str] = Field(default_factory=lambda: ["in_app"])


class AssessRequest(BaseModel):
    user_id: Optional[str] = None
    current_balance: Optional[float] = Field(
        None, description="Available cash to start the projection. Estimated if omitted."
    )
    horizon_days: Optional[int] = Field(None, ge=14, le=120)
    use_llm: bool = True


class AlertList(BaseModel):
    count: int
    generated_at: Optional[datetime] = None
    alerts: list[Alert]


# ---------------------------------------------------------------------------
# Planner agent (Strategy) — payoff plans & what-if simulation (advisory, no execution)
# ---------------------------------------------------------------------------
PayoffMethod = Literal["avalanche", "snowball"]


class PlanStep(BaseModel):
    order: int
    obligation_id: str
    creditor: Optional[str] = None
    balance: float
    apr: float
    min_payment: float
    monthly_amount: float  # what this debt gets in month 1 (minimum + any extra)
    payoff_month: Optional[int] = None  # months until this debt is cleared


class PayoffPoint(BaseModel):
    month: int
    date: str
    total_balance: float


class DebtSeries(BaseModel):
    obligation_id: str
    creditor: Optional[str] = None
    balances: list[float]  # balance at each month, aligned with the plan projection


class RepaymentPlan(BaseModel):
    id: str
    user_id: str
    method: PayoffMethod
    monthly_budget: float
    total_minimums: float
    feasible: bool  # False if the budget can't clear the debt within the cap
    months_to_debt_free: Optional[int] = None
    debt_free_date: Optional[str] = None
    total_interest: float
    interest_saved_vs_minimums: float
    baseline_feasible: bool  # whether minimums-only ever clears the debt
    steps: list[PlanStep]
    projection: list[PayoffPoint]
    debt_series: list[DebtSeries] = Field(default_factory=list)  # per-debt balance over time
    summary: str
    llm_used: bool = False
    version: int = 1
    created_at: datetime


class PlanSummary(BaseModel):
    method: PayoffMethod
    months_to_debt_free: Optional[int] = None
    debt_free_date: Optional[str] = None
    total_interest: float
    feasible: bool


class StrategyComparison(BaseModel):
    monthly_budget: float
    avalanche: PlanSummary
    snowball: PlanSummary
    recommended: PayoffMethod
    reason: str


class PlanRequest(BaseModel):
    user_id: Optional[str] = None
    monthly_budget: Optional[float] = Field(
        None, description="Total monthly amount to put toward debts. Defaults to the sum of minimums."
    )
    method: PayoffMethod = "avalanche"
    use_llm: bool = True


class RateChange(BaseModel):
    obligation_id: str
    apr: float


class SimulationRequest(BaseModel):
    user_id: Optional[str] = None
    method: Optional[PayoffMethod] = None  # defaults to current plan's method
    monthly_budget: Optional[float] = None  # defaults to current plan's budget
    extra_payment: float = Field(0, description="Extra added to the monthly budget every month.")
    windfall: float = Field(0, description="One-off lump sum.")
    windfall_month: int = Field(1, ge=1, description="Month the windfall is applied.")
    rate_changes: list[RateChange] = Field(default_factory=list)
    apply: bool = Field(False, description="If true, persist the simulated plan as the live plan.")
    use_llm: bool = False


class ProcessedTable(BaseModel):
    dataset_id: str
    name: str
    target: TargetEntity
    category: str
    record_count: int
    mapping_source: str


class PipelineResult(BaseModel):
    """One-input result: the Supervisor ingested, normalised and ran the agents."""

    group_id: str
    source_type: SourceType
    tables: list[ProcessedTable]
    obligations_count: int
    transactions_count: int
    plan: Optional["RepaymentPlan"] = None
    assessment: Optional["RiskAssessment"] = None
    steps: list[str]  # human-readable log of what the Supervisor did


class SimulationResult(BaseModel):
    baseline: PlanSummary  # the current/live plan
    simulated: PlanSummary  # with the levers applied
    months_sooner: Optional[int] = None
    interest_difference: float  # positive = interest saved vs baseline
    levers: dict[str, Any]
    projection: list[PayoffPoint]  # simulated (with levers applied)
    baseline_projection: list[PayoffPoint] = Field(default_factory=list)  # current plan, for comparison
    applied: bool
    note: str


class TransactionList(BaseModel):
    total: int
    offset: int
    limit: int
    returned: int
    transactions: list[Transaction]
