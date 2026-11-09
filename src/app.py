"""Public HTTP service for the Larkfield Home support agent."""
from __future__ import annotations

import os
import subprocess
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import List, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse

from .data import ARTIFACT, get_data
from .models import Decision, LedgerEntry, RunState, Ticket
from .service import decide, get_run, ledger, process_ticket

ROOT = Path(__file__).resolve().parent.parent


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Deploy-time safety net: build the normalised artifact if it is missing.
    if not ARTIFACT.exists():
        subprocess.run(
            [sys.executable, str(ROOT / "prepare.py")],
            cwd=str(ROOT),
            check=True,
        )
    get_data()
    yield


app = FastAPI(
    title="Larkfield Home Customer Care Agent",
    version="1.0.0",
    description="Policy-controlled support-email agent. It proposes; it never executes.",
    lifespan=lifespan,
)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/")
def index() -> dict:
    return {
        "service": "larkfield-home-support-agent",
        "status": "ok",
        "endpoints": ["/health", "/tickets", "/runs/{run_id}", "/runs/{run_id}/decision", "/ledger"],
    }


@app.post("/tickets", response_model=RunState)
def create_ticket(ticket: Ticket) -> RunState:
    try:
        return RunState(**process_ticket(ticket))
    except Exception as exc:  # keep the contract alive even on an internal error
        raise HTTPException(status_code=500, detail=f"ticket processing failed: {exc}") from exc


@app.get("/runs/{run_id}", response_model=RunState)
def read_run(run_id: str) -> RunState:
    state = get_run(run_id)
    if not state:
        raise HTTPException(status_code=404, detail="run not found")
    return RunState(**state)


@app.post("/runs/{run_id}/decision", response_model=RunState)
def record_decision(run_id: str, decision: Decision) -> RunState:
    state = decide(run_id, decision.decision, decision.approver)
    if not state:
        raise HTTPException(status_code=404, detail="run not found")
    return RunState(**state)


@app.get("/ledger", response_model=List[LedgerEntry])
def read_ledger() -> List[LedgerEntry]:
    return [LedgerEntry(**x) for x in ledger()]


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "src.app:app",
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8000")),
        log_level="info",
    )
