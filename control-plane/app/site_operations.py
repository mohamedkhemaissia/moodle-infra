"""File minimale de sauvegarde complete et restauration directe du pilote."""
import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import stat
import threading
import time
import uuid

from fastapi import HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field


IDENTIFIER = re.compile(r"^[0-9a-f]{32}$")
SNAPSHOT = re.compile(r"^[0-9a-f]{64}$")
STATES = {"queued", "running", "completed", "failed", "interrupted"}


class SiteOperationError(RuntimeError):
    pass


class SiteOperationConflict(ValueError):
    pass


def utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class SiteOperationStore:
    def __init__(self, directory):
        self.root = Path(directory)
        self.requests = self.root / "requests"
        self.operations = self.root / "operations"
        self.processing = self.root / "processing"
        self.lock = threading.Lock()

    def check(self):
        for path in (self.root, self.requests, self.operations, self.processing):
            try:
                info = path.stat(follow_symlinks=False)
            except OSError as exc:
                raise SiteOperationError("Stockage des sauvegardes indisponible.") from exc
            if not stat.S_ISDIR(info.st_mode) or path.is_symlink():
                raise SiteOperationError("Stockage des sauvegardes invalide.")

    @staticmethod
    def _read(path, maximum=131072):
        try:
            info = path.stat(follow_symlinks=False)
            if path.is_symlink() or not stat.S_ISREG(info.st_mode) or info.st_size > maximum:
                raise SiteOperationError("Fichier de sauvegarde invalide.")
            value = json.loads(path.read_text(encoding="utf-8"))
        except SiteOperationError:
            raise
        except (OSError, ValueError, TypeError) as exc:
            raise SiteOperationError("Fichier de sauvegarde illisible.") from exc
        if not isinstance(value, dict):
            raise SiteOperationError("Fichier de sauvegarde invalide.")
        return value

    @staticmethod
    def _write_new(path, value):
        data = (json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o660)
            with os.fdopen(descriptor, "wb") as output:
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
        except FileExistsError as exc:
            raise SiteOperationConflict("Cette demande existe deja.") from exc
        except OSError as exc:
            raise SiteOperationError("Impossible d'enregistrer la demande.") from exc

    @staticmethod
    def _operation(value):
        if (not IDENTIFIER.fullmatch(str(value.get("id", ""))) or value.get("kind") not in ("backup", "restore")
                or value.get("status") not in STATES or not isinstance(value.get("requested_by"), dict)):
            raise SiteOperationError("Operation de sauvegarde invalide.")
        return value

    def _points(self):
        value = self._read(self.root / "points.json")
        if (value.get("status") != "available" or not isinstance(value.get("points"), list)
                or not isinstance(value.get("generated_at"), str)):
            raise SiteOperationError("Catalogue des sauvegardes invalide.")
        try:
            age = time.time() - datetime.fromisoformat(value["generated_at"].replace("Z", "+00:00")).timestamp()
        except (ValueError, OverflowError) as exc:
            raise SiteOperationError("Catalogue des sauvegardes invalide.") from exc
        if age < -60 or age > 7200:
            raise SiteOperationError("Catalogue des sauvegardes ancien.")
        points = []
        for point in value["points"]:
            if (not isinstance(point, dict) or not SNAPSHOT.fullmatch(str(point.get("id", "")))
                    or not isinstance(point.get("created_at"), str)):
                raise SiteOperationError("Point de reprise invalide.")
            points.append({"id": point["id"], "created_at": point["created_at"]})
        latest = value.get("latest_success")
        completed = latest.get("completed_at_utc") if isinstance(latest, dict) else None
        return {"latest_success_at": completed if isinstance(completed, str) else None, "points": points}

    def listing(self):
        values = []
        try:
            paths = list(self.operations.glob("*.json"))
        except OSError as exc:
            raise SiteOperationError("Historique indisponible.") from exc
        for path in paths:
            if IDENTIFIER.fullmatch(path.stem):
                values.append(self._operation(self._read(path)))
        values.sort(key=lambda row: (row.get("created_at", ""), row["id"]), reverse=True)
        catalog = self._points()
        return {**catalog, "operations": values[:20]}

    def _existing(self, actor, client_key):
        return next((row for row in self.listing()["operations"]
                     if row.get("client_key") == client_key and row.get("requested_by", {}).get("id") == actor), None)

    def create(self, principal, kind, client_key, snapshot_id=None):
        with self.lock:
            existing = self._existing(principal["user"]["id"], client_key)
            if existing:
                if existing["kind"] != kind or existing.get("snapshot_id") != snapshot_id:
                    raise SiteOperationConflict("La cle de demande est deja utilisee.")
                return existing, False
            current = self.listing()
            if any(row["status"] in ("queued", "running") for row in current["operations"]):
                raise SiteOperationConflict("Une sauvegarde ou une restauration est deja en cours.")
            if kind == "restore" and snapshot_id not in {point["id"] for point in current["points"]}:
                raise SiteOperationConflict("Ce point de reprise n'est plus disponible.")
            identifier = uuid.uuid4().hex
            stamp = utcnow()
            operation = {"id": identifier, "client_key": client_key, "kind": kind, "status": "queued",
                         "requested_by": {"id": principal["user"]["id"], "username": principal["user"]["username"]},
                         "snapshot_id": snapshot_id, "created_at": stamp, "updated_at": stamp,
                         "started_at": None, "finished_at": None, "reason_code": None, "message": None,
                         "duration_seconds": None}
            self._write_new(self.operations / f"{identifier}.json", operation)
            try:
                self._write_new(self.requests / f"{identifier}.json", operation)
            except Exception:
                (self.operations / f"{identifier}.json").unlink(missing_ok=True)
                raise
            return operation, True


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class NewBackup(Input):
    client_key: str = Field(pattern=r"^[0-9a-f]{32}$")


class NewRestore(Input):
    snapshot_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    confirmation: str = Field(pattern=r"^RESTAURER$")
    client_key: str = Field(pattern=r"^[0-9a-f]{32}$")


def install(application):
    def service(request, response, write=False):
        response.headers["Cache-Control"] = "no-store"
        principal = getattr(request.state, "principal", None)
        if not principal or principal.get("kind") != "human":
            raise HTTPException(403, detail="Une session utilisateur est requise.")
        if write and principal.get("user", {}).get("role") != "admin":
            raise HTTPException(403, detail="Droits administrateur requis.")
        if request.app.state.site_operations is None:
            raise HTTPException(503, detail="La sauvegarde complete n'est pas configuree.")
        return request.app.state.site_operations, principal

    async def call(function, *args):
        try:
            return await asyncio.to_thread(function, *args)
        except SiteOperationConflict as exc:
            raise HTTPException(409, detail=str(exc)) from None
        except SiteOperationError:
            raise HTTPException(503, detail="Le service de sauvegarde complete est indisponible.") from None

    def envelope(value):
        return {"status": "available", "mode": "site_full", "evaluated_at": utcnow(), **value}

    @application.get("/api/v1/site-operations", tags=["Sauvegarde du site"])
    async def listing(request: Request, response: Response):
        store, _ = service(request, response)
        return envelope(await call(store.listing))

    @application.post("/api/v1/site-operations/backups", tags=["Sauvegarde du site"], status_code=201)
    async def backup(payload: NewBackup, request: Request, response: Response):
        store, principal = service(request, response, True)
        operation, created = await call(store.create, principal, "backup", payload.client_key, None)
        response.status_code = 201 if created else 200
        return envelope({"operation": operation, "created": created})

    @application.post("/api/v1/site-operations/restores", tags=["Sauvegarde du site"], status_code=201)
    async def restore(payload: NewRestore, request: Request, response: Response):
        store, principal = service(request, response, True)
        operation, created = await call(store.create, principal, "restore", payload.client_key, payload.snapshot_id)
        response.status_code = 201 if created else 200
        return envelope({"operation": operation, "created": created})
