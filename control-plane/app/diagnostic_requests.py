"""Routes de demande de diagnostic ; aucune commande n'est executee par l'API."""
import asyncio
from datetime import datetime, timezone

from fastapi import HTTPException, Path as ApiPath, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from pilotage.auth import AuthDenied
from pilotage.diagnostic_requests import (
    DiagnosticRequestConflict,
    DiagnosticRequestNotFound,
    DiagnosticRequestStorageError,
)


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class NewDiagnosticRequest(Input):
    incident_id: int = Field(ge=1, le=9223372036854775807)
    client_key: str = Field(pattern=r"^[0-9a-f]{32}$")


def install(application):
    def service(request, response, *, write=False):
        response.headers["Cache-Control"] = "no-store"
        principal = getattr(request.state, "principal", None)
        if not principal or principal.get("kind") != "human":
            raise HTTPException(403, detail="Une session utilisateur est requise pour les diagnostics.")
        if write and principal.get("user", {}).get("role") != "admin":
            raise HTTPException(403, detail="Droits administrateur requis.")
        value = request.app.state.diagnostic_requests
        if value is None:
            raise HTTPException(503, detail="Les demandes de diagnostic ne sont pas configurees.")
        return value, principal

    async def call(function, *args, **kwargs):
        try:
            return await asyncio.to_thread(function, *args, **kwargs)
        except AuthDenied as exc:
            raise HTTPException(403, detail=str(exc)) from None
        except DiagnosticRequestNotFound as exc:
            raise HTTPException(404, detail=str(exc)) from None
        except DiagnosticRequestConflict as exc:
            raise HTTPException(409, detail=str(exc)) from None
        except ValueError as exc:
            raise HTTPException(422, detail=str(exc)) from None
        except DiagnosticRequestStorageError:
            raise HTTPException(503, detail="La file des diagnostics est indisponible.") from None

    def envelope(value):
        return {
            "status": "available",
            "mode": "read_only",
            "evaluated_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
            **value,
        }

    @application.get("/api/v1/diagnostic-requests", tags=["Diagnostics"])
    async def requests_list(
        request: Request,
        response: Response,
        incident_id: int | None = Query(None, ge=1, le=9223372036854775807),
        limit: int = Query(10, ge=1, le=50),
        offset: int = Query(0, ge=0, le=10000),
    ):
        store, _ = service(request, response)
        return envelope(await call(store.listing, incident_id=incident_id, limit=limit, offset=offset))

    @application.post("/api/v1/diagnostic-requests", tags=["Diagnostics"], status_code=201)
    async def create(payload: NewDiagnosticRequest, request: Request, response: Response):
        store, principal = service(request, response, write=True)
        row, created = await call(store.create, principal, payload.incident_id, payload.client_key)
        response.status_code = 201 if created else 200
        return envelope({"request": row, "created": created})

    @application.get("/api/v1/diagnostic-requests/{request_id}", tags=["Diagnostics"])
    async def request_detail(
        request: Request,
        response: Response,
        request_id: str = ApiPath(pattern=r"^[0-9a-f]{32}$"),
    ):
        store, _ = service(request, response)
        return envelope({"request": await call(store.detail, request_id)})
