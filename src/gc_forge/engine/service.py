from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import Literal, Optional, Union

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from gc_forge.config.telemetry import configure_logfire
from gc_forge.contract.schema import Policy
from gc_forge.engine import runtime as gc
from gc_forge.store.versioning import load as load_policy

configure_logfire("guard-iq-engine")

_policy: Optional[Policy] = None
_contract_path: Optional[str] = None


@asynccontextmanager
async def _lifespan(app: FastAPI):
    path = os.environ.get("GC_CONTRACT_PATH")
    if path and os.path.exists(path):
        _load_contract(path)
    yield


app = FastAPI(title="Guardrail Contract Engine", version="0.1.0", lifespan=_lifespan)


class EnforceRequest(BaseModel):
    text: str
    side: Literal["input", "output"]


class DecisionResponse(BaseModel):
    pass_through: bool
    bucket: Optional[str] = None
    message: Optional[str] = None
    route_to: Optional[str] = None
    matched_rule_id: Optional[str] = None


class HealthResponse(BaseModel):
    status: str
    contract_path: Optional[str] = None
    contract_version: Optional[int] = None


def _load_contract(path: str) -> Policy:
    global _policy, _contract_path
    _policy = load_policy(path)
    _contract_path = path
    return _policy


@app.get("/healthz", response_model=HealthResponse)
def healthz() -> HealthResponse:
    return HealthResponse(
        status="ok",
        contract_path=_contract_path,
        contract_version=_policy.version if _policy else None,
    )


@app.post("/enforce", response_model=DecisionResponse)
def enforce_endpoint(body: EnforceRequest) -> DecisionResponse:
    if _policy is None:
        raise HTTPException(status_code=503, detail="No contract loaded. Set GC_CONTRACT_PATH.")

    decision = gc.enforce(_policy, body.text, side=body.side)
    if decision is gc.PASS:
        return DecisionResponse(pass_through=True)

    return DecisionResponse(
        pass_through=False,
        bucket=decision.bucket.value,
        message=decision.message,
        route_to=decision.route_to,
        matched_rule_id=decision.matched_rule_id,
    )


@app.post("/reload")
def reload_contract(path: Optional[str] = None) -> dict[str, Union[str, int]]:
    target = path or os.environ.get("GC_CONTRACT_PATH")
    if not target:
        raise HTTPException(status_code=400, detail="No contract path provided.")
    policy = _load_contract(target)
    return {"status": "reloaded", "path": target, "version": policy.version}


def main() -> None:
    import uvicorn

    from gc_forge.config.models import Settings

    settings = Settings()
    uvicorn.run(
        "gc_forge.engine.service:app",
        host=settings.gc_engine_host,
        port=settings.gc_engine_port,
        reload=False,
    )


if __name__ == "__main__":
    main()