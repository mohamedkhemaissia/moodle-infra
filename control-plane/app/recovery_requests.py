"""Parcours humain de demande et approbation de tests de restauration isoles."""
import asyncio
from datetime import datetime, timezone
from typing import Literal

from fastapi import HTTPException, Query, Request, Response, Path as ApiPath
from pydantic import BaseModel, ConfigDict, Field

from pilotage.auth import AuthDenied
from pilotage.recovery import RecoveryConflict, RecoveryNotFound, RecoveryStorageError


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class NewRequest(Input):
    scope: Literal["moodle", "pilotage"]
    snapshot_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    reason: str = Field(min_length=15, max_length=500)
    client_key: str = Field(pattern=r"^[0-9a-f]{32}$")


class Decision(Input):
    revision: int = Field(ge=1, le=2147483647)
    reason: str = Field(min_length=15, max_length=500)


class Approval(Decision):
    confirmed_snapshot_id: str = Field(pattern=r"^[0-9a-f]{64}$")


class Execution(Input):
    revision: int = Field(ge=1, le=2147483647)
    confirmed_snapshot_id: str = Field(pattern=r"^[0-9a-f]{64}$")


def install(application):
    def store(request, response):
        response.headers["Cache-Control"] = "no-store"
        principal = getattr(request.state, "principal", None)
        if not principal or principal.get("kind") != "human":
            raise HTTPException(403, detail="Une session humaine est requise pour les demandes.")
        value = request.app.state.recovery_requests
        if value is None:
            raise HTTPException(503, detail="Les demandes de restauration ne sont pas configurees.")
        return value, principal

    async def call(function, *args, **kwargs):
        try:
            result = await asyncio.to_thread(function, *args, **kwargs)
        except AuthDenied as exc:
            raise HTTPException(403, detail=str(exc)) from None
        except RecoveryNotFound as exc:
            raise HTTPException(404, detail=str(exc)) from None
        except RecoveryConflict as exc:
            raise HTTPException(409, detail=str(exc)) from None
        except ValueError as exc:
            raise HTTPException(422, detail=str(exc)) from None
        except RecoveryStorageError:
            raise HTTPException(503, detail="Le journal des demandes est indisponible.") from None
        return result

    def envelope(value, service):
        return {"status": "available", "mode": "isolated_test" if getattr(service, "execution_enabled", False) else "approval_only",
            "evaluated_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
            "execution_enabled": getattr(service, "execution_enabled", False), **value}

    @application.get("/api/v1/recovery/requests", tags=["Restauration"])
    async def requests(request: Request, response: Response,
                       status: Literal["pending", "approved", "rejected", "cancelled"] | None = None,
                       limit: int = Query(default=10, ge=1, le=50), offset: int = Query(default=0, ge=0, le=10000)):
        service, _ = store(request, response)
        return envelope(await call(service.listing, status=status, limit=limit, offset=offset), service)

    @application.post("/api/v1/recovery/requests", tags=["Restauration"], status_code=201)
    async def create(payload: NewRequest, request: Request, response: Response):
        service, principal = store(request, response)
        row, created = await call(service.create, principal, **payload.model_dump())
        response.status_code = 201 if created else 200
        return envelope({"request": row, "created": created}, service)

    @application.get("/api/v1/recovery/requests/{request_id}", tags=["Restauration"])
    async def detail(request: Request, response: Response,
                     request_id: str = ApiPath(pattern=r"^[0-9a-f]{32}$")):
        service, _ = store(request, response)
        return envelope(await call(service.detail, request_id), service)

    async def decide(action, request_id, payload, request, response):
        service, principal = store(request, response)
        row = await call(service.transition, principal, request_id, action=action, **payload.model_dump())
        return envelope({"request": row}, service)

    @application.post("/api/v1/recovery/requests/{request_id}/approve", tags=["Restauration"])
    async def approve(payload: Approval, request: Request, response: Response,
                      request_id: str = ApiPath(pattern=r"^[0-9a-f]{32}$")):
        return await decide("approve", request_id, payload, request, response)

    @application.post("/api/v1/recovery/requests/{request_id}/reject", tags=["Restauration"])
    async def reject(payload: Decision, request: Request, response: Response,
                     request_id: str = ApiPath(pattern=r"^[0-9a-f]{32}$")):
        return await decide("reject", request_id, payload, request, response)

    @application.post("/api/v1/recovery/requests/{request_id}/cancel", tags=["Restauration"])
    async def cancel(payload: Decision, request: Request, response: Response,
                     request_id: str = ApiPath(pattern=r"^[0-9a-f]{32}$")):
        return await decide("cancel", request_id, payload, request, response)

    @application.post("/api/v1/recovery/requests/{request_id}/execute", tags=["Restauration"], status_code=202)
    async def execute(payload: Execution, request: Request, response: Response,
                      request_id: str = ApiPath(pattern=r"^[0-9a-f]{32}$")):
        service, principal = store(request, response)
        if not request.app.state.recovery_execution_enabled or not getattr(service, "execution_enabled", False):
            raise HTTPException(503, detail="Les tests isoles ne sont pas actives.")
        row, created = await call(service.enqueue, principal, request_id, **payload.model_dump())
        response.status_code = 202 if created else 200
        return envelope({"request": row, "created": created}, service)
