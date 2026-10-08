"""Acces humains par session ; identite locale limitee aux lectures d'exploitation."""
import asyncio
from dataclasses import dataclass
import hmac
import json
import os
from urllib.parse import urlsplit

from fastapi import HTTPException, Request
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ConfigDict, Field
from starlette.responses import JSONResponse

from pilotage.auth import AuthStore, AuthStorageError, AuthDenied, AuthConflict, AuthThrottled, SESSION_SECONDS
from pilotage.worker_auth import READ_PATH, read_secret_file, token_fingerprint

COOKIE = "moodle_pilotage_session"
PUBLIC = {"/api/v1/health", "/api/v1/storage/health", "/api/v1/auth/status"}


def boolean(value):
    if value not in ("true", "false"):
        raise ValueError("Parametre d'authentification invalide.")
    return value == "true"


@dataclass(frozen=True)
class Settings:
    enabled: bool
    origins: tuple
    secure: bool
    worker_hash: str | None

    @classmethod
    def load(cls, enabled=None, origins=None, secure=None, worker_hash_file=None):
        enabled = boolean(os.getenv("AUTH_ENABLED", "false")) if enabled is None else enabled
        if type(enabled) is not bool:
            raise ValueError("Activation de l'authentification invalide.")
        if not enabled:
            return cls(False, (), True, None)
        secure = boolean(os.getenv("AUTH_COOKIE_SECURE", "true")) if secure is None else secure
        if type(secure) is not bool:
            raise ValueError("Configuration du cookie invalide.")
        origins = json.loads(os.getenv("AUTH_ALLOWED_ORIGINS", "[]")) if origins is None else origins
        if not isinstance(origins, (list, tuple)) or not 1 <= len(origins) <= 10:
            raise ValueError("Configurer l'origine du tableau de bord.")
        values = []
        for value in origins:
            if not isinstance(value, str):
                raise ValueError("Origine invalide.")
            parsed = urlsplit(value)
            if (parsed.scheme not in ("http", "https") or not parsed.hostname
                    or parsed.username or parsed.password or parsed.path not in ("", "/")
                    or parsed.query or parsed.fragment):
                raise ValueError("Origine invalide.")
            if parsed.scheme != "https" and (secure or parsed.hostname not in ("127.0.0.1", "localhost", "::1")):
                raise ValueError("HTTPS requis en dehors de l'acces local au laboratoire.")
            values.append(value.rstrip("/"))
        path = worker_hash_file or os.getenv("AUTH_WORKER_HASH_FILE", "")
        fingerprint = read_secret_file(path) if path else None
        return cls(True, tuple(values), secure, fingerprint)


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Login(Input):
    username: str = Field(min_length=3, max_length=32)
    password: str = Field(min_length=1, max_length=128, repr=False)


class NewUser(Input):
    username: str = Field(min_length=3, max_length=32)
    display_name: str = Field(min_length=1, max_length=80)
    role: str
    password: str = Field(min_length=15, max_length=128, repr=False)


class UserUpdate(Input):
    role: str | None = None
    active: bool | None = Field(default=None, strict=True)


class PasswordChange(Input):
    old_password: str = Field(min_length=1, max_length=128, repr=False)
    new_password: str = Field(min_length=15, max_length=128, repr=False)


def install(application, settings):
    def error(status, detail):
        return JSONResponse({"detail": detail}, status_code=status, headers={"Cache-Control": "no-store"})

    @application.middleware("http")
    async def authorization(request, call_next):
        if not settings.enabled:
            return await call_next(request)
        path = request.url.path
        method = request.method
        if method == "GET" and path in PUBLIC:
            result = await call_next(request)
            result.headers["Cache-Control"] = "no-store"
            return result
        write = method not in ("GET", "HEAD")
        if write:
            if request.headers.get("origin") not in settings.origins:
                return error(403, "Origine de la requete refusee.")
            if method not in ("POST", "PATCH") or request.headers.get("content-type", "").split(";")[0] != "application/json":
                return error(415, "Une requete JSON est requise.")
            length = request.headers.get("content-length", "")
            if not length.isdigit() or int(length) > 4096:
                return error(413, "Requete trop volumineuse ou taille non fournie.")
        if path == "/api/v1/auth/login" and method == "POST":
            return await call_next(request)
        try:
            authorization_headers = request.headers.getlist("authorization")
            if authorization_headers:
                if len(authorization_headers) != 1:
                    raise AuthDenied("Connexion requise.")
                header = authorization_headers[0]
                token = header[7:] if header.startswith("Bearer ") else ""
                if (settings.worker_hash is None or len(token) != 64
                        or any(c not in "0123456789abcdef" for c in token)
                        or not hmac.compare_digest(token_fingerprint(token), settings.worker_hash)
                        or request.client is None or request.client.host not in ("127.0.0.1", "::1")):
                    raise AuthDenied("Connexion requise.")
                if method != "GET" or not READ_PATH.fullmatch(path):
                    return error(403, "Cette identite est limitee aux lectures d'exploitation.")
                request.state.principal = {"kind": "worker"}
            else:
                cookie = request.cookies.get(COOKIE, "")
                if sum(part.strip().startswith(COOKIE + "=")
                       for raw in request.headers.getlist("cookie") for part in raw.split(";")) != 1:
                    raise AuthDenied("Connexion requise.")
                principal = await asyncio.to_thread(request.app.state.auth_store.authenticate, cookie)
                principal["kind"] = "human"
                request.state.principal = principal
                if write and not hmac.compare_digest(request.headers.get("x-csrf-token", ""), principal["csrf_token"]):
                    return error(403, "La verification de la session a echoue.")
                if (path.startswith("/api/v1/users") or path == "/api/v1/auth/events"
                        or (write and path not in ("/api/v1/auth/logout", "/api/v1/auth/password"))):
                    if principal["user"]["role"] != "admin":
                        return error(403, "Droits administrateur requis.")
        except AuthDenied as exc:
            result = error(401, str(exc))
            result.delete_cookie(COOKIE, path="/", secure=settings.secure, httponly=True, samesite="strict")
            return result
        except AuthStorageError:
            return error(503, "Le service de connexion est indisponible.")
        result = await call_next(request)
        result.headers["Cache-Control"] = "no-store"
        result.headers["X-Content-Type-Options"] = "nosniff"
        return result

    @application.exception_handler(RequestValidationError)
    async def invalid_input(request, exc):
        # Ne jamais renvoyer le mot de passe dans une erreur de validation.
        return error(422, "Verifier les champs de la requete.")

    def store(request):
        value = request.app.state.auth_store
        if value is None:
            raise HTTPException(503, detail="L'authentification n'est pas activee.")
        return value

    def human(request):
        principal = getattr(request.state, "principal", None)
        if principal is None or principal.get("kind") != "human":
            raise HTTPException(403, detail="Session utilisateur requise.")
        return principal

    async def call(request, method, *args, denied_status=403, **kwargs):
        try:
            return await asyncio.to_thread(getattr(store(request), method), *args, **kwargs)
        except AuthStorageError:
            raise HTTPException(503, detail="Le stockage des comptes est indisponible.") from None
        except AuthThrottled as exc:
            raise HTTPException(429, detail=str(exc), headers={"Retry-After": "300"}) from None
        except AuthConflict as exc:
            raise HTTPException(409, detail=str(exc)) from None
        except AuthDenied as exc:
            raise HTTPException(denied_status, detail=str(exc)) from None
        except ValueError as exc:
            raise HTTPException(400, detail=str(exc)) from None

    async def password_call(request, method, *args, **kwargs):
        semaphore = request.app.state.auth_password_slots
        try:
            await asyncio.wait_for(semaphore.acquire(), timeout=1)
        except asyncio.TimeoutError:
            raise HTTPException(429, detail="Service de connexion occupe. Reessayer.") from None
        task = asyncio.create_task(call(request, method, *args, **kwargs))
        def completed(done):
            semaphore.release()
            if not done.cancelled():
                done.exception()
        task.add_done_callback(completed)
        # Une deconnexion du navigateur n'autorise pas d'autres hachages simultanes.
        return await asyncio.shield(task)

    def login_response(value):
        body = {key: value[key] for key in ("user", "csrf_token", "expires_at")}
        body["enabled"] = True
        result = JSONResponse(body, headers={"Cache-Control": "no-store"})
        result.set_cookie(COOKIE, value["token"], max_age=SESSION_SECONDS, path="/",
                          secure=settings.secure, httponly=True, samesite="strict")
        return result

    @application.get("/api/v1/auth/status", tags=["Comptes"])
    async def status():
        return {"enabled": settings.enabled}

    @application.get("/api/v1/auth/session", tags=["Comptes"])
    async def session(request: Request):
        if not settings.enabled:
            return {"enabled": False, "user": None}
        principal = human(request)
        return {"enabled": True, "user": principal["user"], "csrf_token": principal["csrf_token"]}

    @application.post("/api/v1/auth/login", tags=["Comptes"])
    async def login(body: Login, request: Request):
        value = await password_call(request, "login", body.username, body.password, denied_status=401)
        return login_response(value)

    @application.post("/api/v1/auth/logout", tags=["Comptes"])
    async def logout(request: Request):
        await call(request, "logout", human(request))
        result = JSONResponse({"status": "logged_out"})
        result.delete_cookie(COOKIE, path="/", secure=settings.secure, httponly=True, samesite="strict")
        return result

    @application.post("/api/v1/auth/password", tags=["Comptes"])
    async def password(body: PasswordChange, request: Request):
        value = await password_call(request, "change_password", human(request),
                                    body.old_password, body.new_password, denied_status=400)
        return login_response(value)

    @application.get("/api/v1/users", tags=["Comptes"])
    async def users(request: Request):
        items = await call(request, "users", actor_id=human(request)["user"]["id"])
        return {"items": items}

    @application.post("/api/v1/users", tags=["Comptes"], status_code=201)
    async def add_user(body: NewUser, request: Request):
        return await password_call(request, "add_user", body.username, body.display_name,
                                   body.role, body.password, actor_id=human(request)["user"]["id"])

    @application.patch("/api/v1/users/{identifier}", tags=["Comptes"])
    async def update_user(identifier: int, body: UserUpdate, request: Request):
        return await call(request, "update_user", identifier, role=body.role, active=body.active,
                          actor_id=human(request)["user"]["id"])

    @application.get("/api/v1/auth/events", tags=["Comptes"])
    async def events(request: Request):
        return {"items": await call(request, "events", actor_id=human(request)["user"]["id"])}
