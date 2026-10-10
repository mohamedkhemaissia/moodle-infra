"""Administration humaine des politiques ; aucun appel Docker depuis l'API."""
import asyncio
from datetime import datetime, timezone
from typing import Literal

from fastapi import HTTPException, Request, Response, Query
from pydantic import BaseModel, ConfigDict, Field

from pilotage.auth import AuthDenied
from pilotage.operating_policy import PolicyStorageError, PolicyConflict, PolicyBlocked
from .decisions import summarize
from .incidents import IncidentStorageError


class Input(BaseModel):
    model_config=ConfigDict(extra="forbid",strict=True)


class PolicyUpdate(Input):
    revision: int=Field(ge=1,lt=2147483647)
    mode: Literal["observation","assisted","automatic"]
    suspended: bool
    evaluation_context: bool
    reason: str=Field(min_length=15,max_length=500)
    confirm_automatic: bool=False


class Approval(Input):
    revision: int=Field(ge=1,lt=2147483647)
    incident_id: int=Field(ge=1,le=9223372036854775807)
    reason: str=Field(min_length=15,max_length=500)
    client_key: str=Field(pattern=r"^[0-9a-f]{32}$")


async def call(function,*args,**kwargs):
    try: return await asyncio.to_thread(function,*args,**kwargs)
    except AuthDenied as exc: raise HTTPException(403,detail=str(exc)) from None
    except (PolicyConflict,PolicyBlocked) as exc: raise HTTPException(409,detail=str(exc)) from None
    except ValueError as exc: raise HTTPException(422,detail=str(exc)) from None
    except (PolicyStorageError,IncidentStorageError):
        raise HTTPException(503,detail="Les politiques ou leurs observations sont indisponibles.") from None


def install(application):
    def store(request,response,*,human=False):
        response.headers["Cache-Control"]="no-store"
        principal=getattr(request.state,"principal",None)
        if human and (not principal or principal.get("kind") != "human"):
            raise HTTPException(403,detail="Une session humaine est requise.")
        value=request.app.state.operating_policy
        if value is None: raise HTTPException(503,detail="Les politiques ne sont pas configurees.")
        return value,principal

    @application.get("/api/v1/policies",tags=["Politiques"])
    async def view(request:Request,response:Response):
        service,_=store(request,response)
        return await call(service.view)

    @application.post("/api/v1/policies",tags=["Politiques"])
    async def update(payload:PolicyUpdate,request:Request,response:Response):
        service,principal=store(request,response,human=True)
        policy,changed=await call(service.update,principal,**payload.model_dump())
        return {"status":"available","policy":policy,"changed":changed}

    @application.get("/api/v1/policies/events",tags=["Politiques"])
    async def history(request:Request,response:Response,limit:int=Query(10,ge=1,le=50),offset:int=Query(0,ge=0,le=10000)):
        service,_=store(request,response,human=True)
        return {"status":"available",**await call(service.history,limit=limit,offset=offset)}

    @application.post("/api/v1/policies/approvals",tags=["Politiques"],status_code=201)
    async def approve(payload:Approval,request:Request,response:Response):
        service,principal=store(request,response,human=True)
        incidents=request.app.state.incidents
        if incidents is None or not request.app.state.decisions_enabled:
            raise HTTPException(503,detail="Le moteur de decision n'est pas configure.")
        document=await call(summarize,incidents,datetime.now(timezone.utc).timestamp(),payload.incident_id)
        if document is None: raise HTTPException(404,detail="Incident inconnu.")
        approval,created=await call(service.approve,principal,document,**payload.model_dump())
        response.status_code=201 if created else 200
        return {"status":"available","approval":approval,"created":created,"action_dispatched":False}
