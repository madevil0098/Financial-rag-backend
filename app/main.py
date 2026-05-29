from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from . import config
from .routers import agents, datasets, domain, ingest, organiser, pipeline, planner, watcher

app = FastAPI(
    title="Debt OS — Data Ingestion API",
    version="0.1.0",
    description="Ingest financial data from CSV, Excel, SQL and JSON into a uniform dataset model.",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=config.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(ingest.router, prefix=config.API_PREFIX)
app.include_router(datasets.router, prefix=config.API_PREFIX)
app.include_router(organiser.router, prefix=config.API_PREFIX)
app.include_router(domain.router, prefix=config.API_PREFIX)
app.include_router(watcher.router, prefix=config.API_PREFIX)
app.include_router(planner.router, prefix=config.API_PREFIX)
app.include_router(pipeline.router, prefix=config.API_PREFIX)
app.include_router(agents.router, prefix=config.API_PREFIX)


@app.get("/health", tags=["meta"])
def health() -> dict[str, str]:
    return {"status": "ok", "service": "debt-os-ingestion"}
