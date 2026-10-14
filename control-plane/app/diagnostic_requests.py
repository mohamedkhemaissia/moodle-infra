# API des demandes de diagnostic, sans execution Ansible
import asyncio
from typing import Literal

from fastapi import HTTPException, Path, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from pilotage.auth import AuthDenied
from pilotage.diagnostic_requests import RequestConflict, RequestStorageError


class NewDiagnostic(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    incident_id: int = Field(ge=1, le=9223372036854775807)
    reason: str = Field(min_length=15, max_length=500)
    client_key: str = Field(pattern="^[0-9a-f]{32}$")


def install(application):
    def service(request, response, *, admin=False):
        response.headers["Cache-Control"] = "no-store"
        principal = getattr(request.state, "principal", None)
        if not principal or principal.get("kind") != "human":
            raise HTTPException(403, detail="Une session humaine est requise.")
        if admin and principal.get("user", {}).get("role") != "admin":
            raise HTTPException(403, detail="Droits administrateur requis.")
        store = getattr(request.app.state, "diagnostic_requests", None)
        if store is None:
            raise HTTPException(503, detail="Les demandes de diagnostic ne sont pas configurees.")
        return store, principal

    async def call(function, *args, **kwargs):
        try:
            return await asyncio.to_thread(function, *args, **kwargs)
        except AuthDenied as exc:
            raise HTTPException(403, detail=str(exc)) from None
        except RequestConflict as exc:
            raise HTTPException(409, detail=str(exc)) from None
        except ValueError as exc:
            raise HTTPException(422, detail=str(exc)) from None
        except RequestStorageError:
            raise HTTPException(503, detail="La file de diagnostics est indisponible.") from None

    @application.post("/api/v1/diagnostic-requests", status_code=201, tags=["Diagnostics"])
    async def create(payload: NewDiagnostic, request: Request, response: Response):
        store, principal = service(request, response, admin=True)
        row, created = await call(store.create, principal, **payload.model_dump())
        response.status_code = 201 if created else 200
        return {"status": "available", "request": row, "created": created}

    @application.get("/api/v1/diagnostic-requests", tags=["Diagnostics"])
    async def listing(request: Request, response: Response,
                      incident_id: int | None = Query(None, ge=1, le=9223372036854775807),
                      status: Literal["pending", "running", "completed", "blocked", "failed", "interrupted"] | None = None,
                      limit: int = Query(10, ge=1, le=50),
                      offset: int = Query(0, ge=0, le=10000)):
        store, _ = service(request, response)
        return {"status": "available", **await call(store.listing, incident_id=incident_id,
                                                     status=status, limit=limit, offset=offset)}

    @application.get("/api/v1/diagnostic-requests/{request_id}", tags=["Diagnostics"])
    async def detail(request: Request, response: Response,
                     request_id: str = Path(pattern="^[0-9a-f]{32}$")):
        store, _ = service(request, response)
        row = await call(store.detail, request_id)
        if row is None:
            raise HTTPException(404, detail="Demande inconnue.")
        return {"status": "available", "request": row}
