"""File d'operations Moodle limitee aux sauvegardes et restaurations de cours.

L'API ne lance aucun processus. Elle ecrit une demande bornee dans un repertoire
partage ; le worker Moodle, prive et sans port reseau public, l'execute ensuite.
"""
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

from fastapi import HTTPException, Path as ApiPath, Query, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field


IDENTIFIER = re.compile(r"^[0-9a-f]{32}$")
ACTIVE = {"queued", "running"}
TERMINAL = {"completed", "failed", "interrupted"}
KINDS = {"backup", "restore"}


class CourseOperationError(RuntimeError):
    pass


class CourseOperationNotFound(ValueError):
    pass


class CourseOperationConflict(ValueError):
    pass


def utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def checked_id(value, label="Operation"):
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise ValueError(f"{label} invalide.")
    return value


class CourseOperationStore:
    def __init__(self, directory):
        self.root = Path(directory)
        self.operations = self.root / "operations"
        self.requests = self.root / "requests"
        self.backups = self.root / "backups"
        self.create_lock = threading.Lock()

    def check(self):
        for path in (self.root, self.operations, self.requests, self.backups):
            try:
                info = path.stat(follow_symlinks=False)
            except OSError as exc:
                raise CourseOperationError("Stockage des cours indisponible.") from exc
            if not stat.S_ISDIR(info.st_mode) or path.is_symlink():
                raise CourseOperationError("Stockage des cours invalide.")

    @staticmethod
    def _read(path, *, maximum=65536):
        try:
            info = path.stat(follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode) or info.st_size > maximum or path.is_symlink():
                raise CourseOperationError("Fichier d'operation invalide.")
            value = json.loads(path.read_text(encoding="utf-8"))
        except CourseOperationError:
            raise
        except (OSError, ValueError, TypeError) as exc:
            raise CourseOperationError("Fichier d'operation illisible.") from exc
        if not isinstance(value, dict):
            raise CourseOperationError("Fichier d'operation invalide.")
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
            raise CourseOperationConflict("Cette demande existe deja.") from exc
        except OSError as exc:
            raise CourseOperationError("Impossible d'enregistrer la demande.") from exc

    @staticmethod
    def _validate_catalog(value):
        if value.get("status") != "available" or not isinstance(value.get("generated_at"), str):
            raise CourseOperationError("Catalogue Moodle invalide.")
        courses = value.get("courses")
        categories = value.get("categories")
        if not isinstance(courses, list) or not isinstance(categories, list):
            raise CourseOperationError("Catalogue Moodle invalide.")
        for rows, label in ((courses, "cours"), (categories, "categorie")):
            for row in rows:
                if (not isinstance(row, dict) or type(row.get("id")) is not int or row["id"] < 1
                        or not isinstance(row.get("name"), str) or not row["name"]):
                    raise CourseOperationError(f"Catalogue de {label} invalide.")
        return value

    def catalog(self):
        value = self._validate_catalog(self._read(self.root / "catalog.json"))
        try:
            age = time.time() - datetime.fromisoformat(value["generated_at"].replace("Z", "+00:00")).timestamp()
        except (ValueError, OverflowError) as exc:
            raise CourseOperationError("Date du catalogue Moodle invalide.") from exc
        if age < -60 or age > 120:
            raise CourseOperationError("Le catalogue Moodle n'est pas a jour.")
        return value

    @staticmethod
    def _validate_operation(value):
        checked_id(value.get("id"))
        if value.get("kind") not in KINDS or value.get("status") not in ACTIVE | TERMINAL:
            raise CourseOperationError("Operation Moodle invalide.")
        if not isinstance(value.get("requested_by"), dict):
            raise CourseOperationError("Operation Moodle invalide.")
        return value

    def detail(self, identifier):
        checked_id(identifier)
        path = self.operations / f"{identifier}.json"
        if not path.exists():
            raise CourseOperationNotFound("Operation Moodle introuvable.")
        return self._validate_operation(self._read(path))

    def listing(self, *, kind=None, limit=20, offset=0):
        if kind is not None and kind not in KINDS:
            raise ValueError("Type d'operation invalide.")
        values = []
        try:
            paths = list(self.operations.glob("*.json"))
        except OSError as exc:
            raise CourseOperationError("Historique Moodle indisponible.") from exc
        for path in paths:
            if not IDENTIFIER.fullmatch(path.stem):
                continue
            value = self._validate_operation(self._read(path))
            if kind is None or value["kind"] == kind:
                values.append(value)
        values.sort(key=lambda row: (row.get("created_at", ""), row["id"]), reverse=True)
        return {"items": values[offset:offset + limit], "total": len(values), "limit": limit,
                "offset": offset, "has_more": offset + limit < len(values)}

    def _existing(self, actor_id, client_key):
        for row in self.listing(limit=1000, offset=0)["items"]:
            if row.get("client_key") == client_key and row.get("requested_by", {}).get("id") == actor_id:
                return row
        return None

    def _create(self, principal, client_key, kind, fields):
        with self.create_lock:
            existing = self._existing(principal["user"]["id"], client_key)
            if existing is not None:
                if existing["kind"] != kind or any(existing.get(key) != value for key, value in fields.items()):
                    raise CourseOperationConflict("La cle de demande est deja utilisee.")
                return existing, False
            identifier = uuid.uuid4().hex
            created = utcnow()
            operation = {
                "id": identifier, "client_key": client_key, "kind": kind, "status": "queued",
                "requested_by": {"id": principal["user"]["id"], "username": principal["user"]["username"]},
                "created_at": created, "updated_at": created, "started_at": None, "finished_at": None,
                "reason_code": None, "message": None, **fields,
            }
            self._write_new(self.operations / f"{identifier}.json", operation)
            try:
                self._write_new(self.requests / f"{identifier}.json", operation)
            except Exception:
                try:
                    (self.operations / f"{identifier}.json").unlink()
                except OSError:
                    pass
                raise
            return operation, True

    def create_backup(self, principal, course_id, client_key):
        courses = {row["id"]: row for row in self.catalog()["courses"]}
        if course_id not in courses:
            raise CourseOperationNotFound("Cours Moodle introuvable.")
        return self._create(principal, client_key, "backup", {
            "course_id": course_id, "course": courses[course_id], "backup_id": None,
            "category_id": None, "backup": None, "restored_course": None,
        })

    def create_restore(self, principal, backup_id, category_id, client_key):
        checked_id(backup_id, "Sauvegarde")
        source = self.detail(backup_id)
        if source["kind"] != "backup" or source["status"] != "completed" or not isinstance(source.get("backup"), dict):
            raise CourseOperationConflict("La sauvegarde n'est pas disponible pour une restauration.")
        categories = {row["id"]: row for row in self.catalog()["categories"]}
        if category_id not in categories:
            raise CourseOperationNotFound("Categorie Moodle introuvable.")
        return self._create(principal, client_key, "restore", {
            "course_id": source.get("course_id"), "course": source.get("course"),
            "backup_id": backup_id, "category_id": category_id, "category": categories[category_id],
            "backup": None, "restored_course": None,
        })

    def download(self, identifier):
        operation = self.detail(identifier)
        metadata = operation.get("backup")
        if operation["kind"] != "backup" or operation["status"] != "completed" or not isinstance(metadata, dict):
            raise CourseOperationConflict("Le fichier de sauvegarde n'est pas disponible.")
        path = self.backups / f"{identifier}.mbz"
        try:
            info = path.stat(follow_symlinks=False)
        except OSError as exc:
            raise CourseOperationNotFound("Fichier de sauvegarde introuvable.") from exc
        if path.is_symlink() or not stat.S_ISREG(info.st_mode) or info.st_size != metadata.get("size_bytes"):
            raise CourseOperationError("Fichier de sauvegarde incoherent.")
        return path, metadata.get("filename", f"cours-{identifier}.mbz")


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class NewBackup(Input):
    course_id: int = Field(ge=2, le=9223372036854775807)
    client_key: str = Field(pattern=r"^[0-9a-f]{32}$")


class NewRestore(Input):
    backup_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    category_id: int = Field(ge=1, le=9223372036854775807)
    client_key: str = Field(pattern=r"^[0-9a-f]{32}$")


def install(application):
    def service(request, response, *, write=False):
        response.headers["Cache-Control"] = "no-store"
        principal = getattr(request.state, "principal", None)
        if not principal or principal.get("kind") != "human":
            raise HTTPException(403, detail="Une session utilisateur est requise.")
        if write and principal.get("user", {}).get("role") != "admin":
            raise HTTPException(403, detail="Droits administrateur requis.")
        store = request.app.state.course_operations
        if store is None:
            raise HTTPException(503, detail="Les sauvegardes de cours ne sont pas configurees.")
        return store, principal

    async def call(function, *args, **kwargs):
        try:
            return await asyncio.to_thread(function, *args, **kwargs)
        except CourseOperationNotFound as exc:
            raise HTTPException(404, detail=str(exc)) from None
        except CourseOperationConflict as exc:
            raise HTTPException(409, detail=str(exc)) from None
        except ValueError as exc:
            raise HTTPException(422, detail=str(exc)) from None
        except CourseOperationError:
            raise HTTPException(503, detail="Le service de sauvegarde des cours est indisponible.") from None

    def envelope(value):
        return {"status": "available", "mode": "course_only", "evaluated_at": utcnow(), **value}

    @application.get("/api/v1/course-operations/catalog", tags=["Sauvegardes de cours"])
    async def catalog(request: Request, response: Response):
        store, _ = service(request, response)
        return envelope(await call(store.catalog))

    @application.get("/api/v1/course-operations", tags=["Sauvegardes de cours"])
    async def listing(request: Request, response: Response,
                      kind: str | None = Query(None, pattern=r"^(backup|restore)$"),
                      limit: int = Query(20, ge=1, le=50), offset: int = Query(0, ge=0, le=10000)):
        store, _ = service(request, response)
        return envelope(await call(store.listing, kind=kind, limit=limit, offset=offset))

    @application.post("/api/v1/course-operations/backups", tags=["Sauvegardes de cours"], status_code=201)
    async def create_backup(payload: NewBackup, request: Request, response: Response):
        store, principal = service(request, response, write=True)
        operation, created = await call(store.create_backup, principal, payload.course_id, payload.client_key)
        response.status_code = 201 if created else 200
        return envelope({"operation": operation, "created": created})

    @application.post("/api/v1/course-operations/restores", tags=["Sauvegardes de cours"], status_code=201)
    async def create_restore(payload: NewRestore, request: Request, response: Response):
        store, principal = service(request, response, write=True)
        operation, created = await call(store.create_restore, principal, payload.backup_id,
                                        payload.category_id, payload.client_key)
        response.status_code = 201 if created else 200
        return envelope({"operation": operation, "created": created})

    @application.get("/api/v1/course-operations/{operation_id}", tags=["Sauvegardes de cours"])
    async def detail(request: Request, response: Response,
                     operation_id: str = ApiPath(pattern=r"^[0-9a-f]{32}$")):
        store, _ = service(request, response)
        return envelope({"operation": await call(store.detail, operation_id)})

    @application.get("/api/v1/course-operations/{operation_id}/download", tags=["Sauvegardes de cours"])
    async def download(request: Request, response: Response,
                       operation_id: str = ApiPath(pattern=r"^[0-9a-f]{32}$")):
        store, _ = service(request, response)
        path, filename = await call(store.download, operation_id)
        return FileResponse(path, media_type="application/vnd.moodle.backup", filename=filename,
                            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})
