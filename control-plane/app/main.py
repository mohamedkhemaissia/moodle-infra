import os
import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import httpx
from fastapi import FastAPI, HTTPException, Request, Response, Query, Path as ApiPath
from typing import Literal

from .models import MetricsSummary
from .application_metrics import summary as application_metrics_summary, queries as application_queries
from .auth import Settings as AuthSettings, install as install_auth
from .backups import read_summary as read_backup_summary, read_pilotage_summary, read_catalog
from pilotage.auth import AuthStore, AuthStorageError
from .diagnostics_store import DiagnosticStore, DiagnosticStorageError
from .remediation_store import RemediationStore
from .incidents import IncidentStore, IncidentStorageError
from .decisions import policy_document, summarize as decision_summary
from .anomalies import AnomalyBundle, AnomalyBundleError, AnomalyService
from .forecasts import ForecastBundle, ForecastBundleError, ForecastService
from .prometheus import (
    PrometheusClient,
    PrometheusInvalidResponse,
    PrometheusUnavailable,
)


def create_app(
    prometheus_url: str | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    forecasts_dir: str | None = None,
    anomalies_dir: str | None = None,
    incidents_dir: str | None = None,
    decisions_enabled: bool | None = None,
    diagnostics_dir: str | None = None,
    remediation_dir: str | None = None,
    auth_enabled: bool | None = None,
    auth_store=None,
    auth_origins=None,
    auth_secure_cookie: bool | None = None,
    auth_worker_hash_file: str | None = None,
    backup_status_file: str | None = None,
    pilotage_backup_status_file: str | None = None,
    application_metrics_enabled: bool | None = None,
    backup_catalog_file: str | None = None,
) -> FastAPI:
    authentication = AuthSettings.load(auth_enabled, auth_origins, auth_secure_cookie, auth_worker_hash_file)
    backup_file = backup_status_file if backup_status_file is not None else os.getenv("BACKUP_STATUS_FILE", "")
    pilotage_backup_file = (pilotage_backup_status_file if pilotage_backup_status_file is not None
                           else os.getenv("PILOTAGE_BACKUP_STATUS_FILE", ""))
    catalog_file = backup_catalog_file if backup_catalog_file is not None else os.getenv("BACKUP_CATALOG_FILE", "")
    url = prometheus_url or os.getenv("PROMETHEUS_URL", "http://127.0.0.1:9090")
    max_age = float(os.getenv("MAX_SCRAPE_AGE_SECONDS", "60"))
    if application_metrics_enabled is None:
        setting = os.getenv("APPLICATION_METRICS_ENABLED", "false")
        if setting not in ("true", "false"):
            raise ValueError("APPLICATION_METRICS_ENABLED doit valoir true ou false.")
        application_metrics_enabled = setting == "true"
    if type(application_metrics_enabled) is not bool:
        raise ValueError("Activation des indicateurs applicatifs invalide.")
    application_pool = os.getenv("APPLICATION_PHP_POOL", "www")
    application_database = os.getenv("APPLICATION_PG_DATABASE", "moodle")
    application_queries(application_pool, application_database)
    model_directory = forecasts_dir if forecasts_dir is not None else os.getenv("FORECASTS_DIR", "")

    anomaly_directory = anomalies_dir if anomalies_dir is not None else os.getenv("ANOMALIES_DIR", "")

    remediation_directory = remediation_dir if remediation_dir is not None else os.getenv("REMEDIATION_DIR", "")
    diagnostic_directory = diagnostics_dir if diagnostics_dir is not None else os.getenv("DIAGNOSTICS_DIR", "")

    incident_directory = incidents_dir if incidents_dir is not None else os.getenv("INCIDENTS_DIR", "")
    if decisions_enabled is None:
        setting = os.getenv("DECISIONS_ENABLED", "false").lower()
        if setting not in ("true", "false"):
            raise ValueError("DECISIONS_ENABLED doit valoir true ou false.")
        decisions_enabled = setting == "true"
    if type(decisions_enabled) is not bool:
        raise ValueError("Le mode des decisions doit etre booleen.")

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        application.state.auth_store = (auth_store or AuthStore()) if authentication.enabled else None
        application.state.auth_password_slots = asyncio.Semaphore(2)
        if authentication.enabled:
            await asyncio.to_thread(application.state.auth_store.check)
        async with httpx.AsyncClient(
            base_url=url.rstrip("/") + "/",
            timeout=5,
            trust_env=False,
            transport=transport,
        ) as client:
            application.state.prometheus = PrometheusClient(client, max_age)
            application.state.forecasts = None
            if model_directory:
                try:
                    application.state.forecasts = ForecastService(
                        application.state.prometheus, ForecastBundle(model_directory))
                except ForecastBundleError:
                    logging.getLogger(__name__).error("Artefacts de previsions invalides ; endpoint indisponible.")
            application.state.anomalies = None
            if anomaly_directory:
                try:
                    application.state.anomalies = AnomalyService(
                        application.state.prometheus, AnomalyBundle(anomaly_directory))
                except AnomalyBundleError:
                    logging.getLogger(__name__).error("Artefacts d'anomalies invalides ; endpoint indisponible.")
            application.state.incidents = IncidentStore(incident_directory) if incident_directory else None
            application.state.decisions_enabled = decisions_enabled
            application.state.remediations = RemediationStore(remediation_directory) if remediation_directory else None
            application.state.diagnostics = DiagnosticStore(diagnostic_directory) if diagnostic_directory else None
            yield

    application = FastAPI(
        title="Pilotage Moodle",
        description="Supervision, analyses experimentales et historique des incidents observes du pilote Moodle.",
        version="0.12.0",
        lifespan=lifespan,
    )
    install_auth(application, authentication)

    @application.get("/api/v1/health", tags=["Sante"])
    async def health():
        return {"status": "ok", "service": "moodle-control-plane", "version": "0.12.0"}

    @application.get("/api/v1/metrics/applications", tags=["Supervision"])
    async def application_metrics(request: Request, response: Response):
        response.headers["Cache-Control"] = "no-store"
        if not application_metrics_enabled:
            raise HTTPException(503, detail="Les indicateurs applicatifs ne sont pas configures.")
        try:
            return await application_metrics_summary(request.app.state.prometheus, datetime.now(timezone.utc),
                                                     application_pool, application_database)
        except PrometheusUnavailable as exc:
            raise HTTPException(503, detail="Prometheus est indisponible.") from exc
        except PrometheusInvalidResponse as exc:
            raise HTTPException(502, detail="Les indicateurs applicatifs sont inexploitables.") from exc

    @application.get("/api/v1/backups/summary", tags=["Sauvegardes"])
    async def backup_summary(response: Response):
        response.headers["Cache-Control"] = "no-store"
        if not backup_file:
            raise HTTPException(503, detail="Le suivi des sauvegardes n'est pas configure.")
        return await read_backup_summary(backup_file)

    @application.get("/api/v1/backups/pilotage/summary", tags=["Sauvegardes"])
    async def pilotage_backup_summary(response: Response):
        response.headers["Cache-Control"] = "no-store"
        if not pilotage_backup_file:
            raise HTTPException(503, detail="Le suivi des sauvegardes du pilotage n'est pas configure.")
        return await read_pilotage_summary(pilotage_backup_file)

    @application.get("/api/v1/backups/catalog", tags=["Sauvegardes"])
    async def backup_catalog(response: Response, scope: Literal["moodle", "pilotage"] = "moodle",
                             limit: int = Query(default=20, ge=1, le=50),
                             offset: int = Query(default=0, ge=0, le=200)):
        response.headers["Cache-Control"] = "no-store"
        if not catalog_file:
            raise HTTPException(503, detail="Le catalogue des points de reprise n'est pas configure.")
        return await read_catalog(catalog_file, scope, limit, offset)

    @application.get("/api/v1/storage/health", tags=["Sante"])
    async def storage_health(request: Request, response: Response):
        response.headers["Cache-Control"] = "no-store"
        def inspect_storage():
            result = {}
            for name, store in (("incidents", request.app.state.incidents),
                                ("diagnostics", request.app.state.diagnostics),
                                ("remediations", request.app.state.remediations)):
                if store is None:
                    continue
                context = store.connection() if name == "incidents" else store.connect()
                with context as connection:
                    if name == "incidents":
                        store.check_schema(connection)
                    connection.execute("SELECT 1")
                result[name] = {"status": "available", "schema_version": 1,
                                "backend": "postgresql" if store.database is not None else "sqlite"}
            if request.app.state.auth_store is not None:
                request.app.state.auth_store.check()
                result["accounts"] = {"status": "available", "schema_version": 1, "backend": "postgresql"}
            return result
        try:
            stores = await asyncio.to_thread(inspect_storage)
        except (IncidentStorageError, DiagnosticStorageError, AuthStorageError) as exc:
            raise HTTPException(503, detail="Le stockage du pilotage est indisponible.") from exc
        if not stores:
            raise HTTPException(503, detail="Aucun historique du pilotage n'est configure.")
        return {"status": "available", "stores": stores}

    @application.get(
        "/api/v1/metrics/summary",
        response_model=MetricsSummary,
        tags=["Supervision"],
        responses={
            502: {"description": "Reponse Prometheus inexploitable."},
            503: {"description": "Prometheus indisponible."},
        },
    )
    async def metrics_summary(request: Request):
        try:
            return await request.app.state.prometheus.summary(datetime.now(timezone.utc))
        except PrometheusUnavailable as exc:
            raise HTTPException(503, detail="Prometheus est indisponible.") from exc
        except PrometheusInvalidResponse as exc:
            raise HTTPException(502, detail="La reponse de Prometheus est inexploitable.") from exc

    @application.get("/api/v1/forecasts/summary", tags=["Previsions"], responses={
        502: {"description": "Historique Prometheus inexploitable."},
        503: {"description": "Prometheus ou artefacts des previsions indisponibles."},
    })
    async def forecasts_summary(request: Request):
        service = request.app.state.forecasts
        if service is None:
            raise HTTPException(503, detail="Les previsions ne sont pas configurees ou leurs artefacts sont invalides.")
        try:
            return await service.summary(datetime.now(timezone.utc))
        except PrometheusUnavailable as exc:
            raise HTTPException(503, detail="Prometheus est indisponible.") from exc
        except PrometheusInvalidResponse as exc:
            raise HTTPException(502, detail="L'historique Prometheus est inexploitable.") from exc
        except ForecastBundleError as exc:
            raise HTTPException(503, detail="Le calcul des previsions est indisponible.") from exc

    @application.get("/api/v1/anomalies/model", tags=["Anomalies"], responses={
        503: {"description": "Artefacts des anomalies indisponibles."},
    })
    async def anomaly_model(request: Request):
        service = request.app.state.anomalies
        if service is None:
            raise HTTPException(503, detail="La detection d'anomalies n'est pas configuree ou ses artefacts sont invalides.")
        return service.bundle.description()

    @application.get("/api/v1/anomalies/summary", tags=["Anomalies"], responses={
        502: {"description": "Historique Prometheus inexploitable."},
        503: {"description": "Prometheus ou calcul des anomalies indisponible."},
    })
    async def anomalies_summary(request: Request):
        service = request.app.state.anomalies
        if service is None:
            raise HTTPException(503, detail="La detection d'anomalies n'est pas configuree ou ses artefacts sont invalides.")
        try:
            return await service.summary(datetime.now(timezone.utc))
        except PrometheusUnavailable as exc:
            raise HTTPException(503, detail="Prometheus est indisponible.") from exc
        except PrometheusInvalidResponse as exc:
            raise HTTPException(502, detail="L'historique Prometheus est inexploitable.") from exc
        except AnomalyBundleError as exc:
            raise HTTPException(503, detail="Le calcul du score est indisponible.") from exc

    def incident_store(request):
        store = request.app.state.incidents
        if store is None:
            raise HTTPException(503, detail="La gestion des incidents n'est pas configuree.")
        return store

    async def read_incidents(function, *args, **kwargs):
        try:
            return await asyncio.to_thread(function, *args, **kwargs)
        except IncidentStorageError as exc:
            raise HTTPException(503, detail="Le stockage des incidents est indisponible.") from exc

    @application.get("/api/v1/incidents/summary", tags=["Incidents"])
    async def incident_summary(request: Request):
        store = incident_store(request)
        return await read_incidents(store.summary, datetime.now(timezone.utc).timestamp())

    @application.get("/api/v1/incidents", tags=["Incidents"])
    async def incidents_list(request: Request, status: Literal["open", "resolved"] | None = None,
                             limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0, le=100000)):
        store = incident_store(request)
        return await read_incidents(store.listing, status=status, limit=limit, offset=offset)

    @application.get("/api/v1/incidents/{incident_id}", tags=["Incidents"])
    async def incident_detail(request: Request,
                              incident_id: int = ApiPath(..., ge=1, le=9223372036854775807)):
        store = incident_store(request)
        result = await read_incidents(store.detail, incident_id)
        if result is None:
            raise HTTPException(404, detail="Incident inconnu.")
        return result

    @application.get("/api/v1/incidents/{incident_id}/events", tags=["Incidents"])
    async def incident_events(request: Request,
                              incident_id: int = ApiPath(..., ge=1, le=9223372036854775807),
                              limit: int = Query(50, ge=1, le=100), after_id: int = Query(0, ge=0, le=9223372036854775807)):
        store = incident_store(request)
        result = await read_incidents(store.events, incident_id, limit=limit, after_id=after_id)
        if result is None:
            raise HTTPException(404, detail="Incident inconnu.")
        return result

    def decision_store(request):
        if not request.app.state.decisions_enabled:
            raise HTTPException(503, detail="La simulation des decisions n'est pas activee.")
        return incident_store(request)

    @application.get("/api/v1/decisions/policies", tags=["Decisions"])
    async def decision_policies(request: Request, response: Response):
        decision_store(request)
        response.headers["Cache-Control"] = "no-store"
        return policy_document()

    @application.get("/api/v1/decisions/summary", tags=["Decisions"])
    async def decisions_summary(request: Request, response: Response):
        store = decision_store(request)
        response.headers["Cache-Control"] = "no-store"
        return await read_incidents(decision_summary, store, datetime.now(timezone.utc).timestamp())

    @application.get("/api/v1/decisions/{incident_id}", tags=["Decisions"])
    async def incident_decision(request: Request, response: Response,
                                incident_id: int = ApiPath(..., ge=1, le=9223372036854775807)):
        store = decision_store(request)
        response.headers["Cache-Control"] = "no-store"
        result = await read_incidents(decision_summary, store, datetime.now(timezone.utc).timestamp(), incident_id)
        if result is None:
            raise HTTPException(404, detail="Incident inconnu.")
        return result

    async def read_diagnostics(request, response, method, *args, **kwargs):
        response.headers["Cache-Control"] = "no-store"
        store = request.app.state.diagnostics
        if store is None:
            raise HTTPException(503, detail="Les diagnostics ne sont pas configures.")
        try:
            return await asyncio.to_thread(getattr(store, method), *args, **kwargs)
        except DiagnosticStorageError as exc:
            raise HTTPException(503, detail="Le journal des diagnostics est indisponible.") from exc

    @application.get("/api/v1/diagnostics/summary", tags=["Diagnostics"])
    async def diagnostics_summary(request: Request, response: Response):
        return await read_diagnostics(request, response, "summary")

    @application.get("/api/v1/diagnostics", tags=["Diagnostics"])
    async def diagnostics_list(request: Request, response: Response,
                               incident_id: int | None = Query(None, ge=1, le=9223372036854775807),
                               limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0, le=100000)):
        return await read_diagnostics(request, response, "listing", incident_id=incident_id, limit=limit, offset=offset)

    @application.get("/api/v1/diagnostics/{run_id}", tags=["Diagnostics"])
    async def diagnostic_detail(request: Request, response: Response,
                                run_id: str = ApiPath(..., pattern="^[0-9a-f]{32}$")):
        result = await read_diagnostics(request, response, "detail", run_id)
        if result is None:
            raise HTTPException(404, detail="Diagnostic inconnu.")
        return result

    async def read_remediations(request, response, method, *args, **kwargs):
        response.headers["Cache-Control"] = "no-store"
        store = request.app.state.remediations
        if store is None:
            raise HTTPException(503, detail="La remediation n'est pas configuree.")
        try:
            return await asyncio.to_thread(getattr(store, method), *args, **kwargs)
        except DiagnosticStorageError as exc:
            raise HTTPException(503, detail="Le journal de remediation est indisponible.") from exc

    @application.get("/api/v1/remediations/summary", tags=["Remediation"])
    async def remediations_summary(request: Request, response: Response):
        return await read_remediations(request, response, "summary")

    @application.get("/api/v1/remediations", tags=["Remediation"])
    async def remediations_list(request: Request, response: Response,
                                incident_id: int | None = Query(None, ge=1, le=9223372036854775807),
                                limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0, le=100000)):
        return await read_remediations(request, response, "listing", incident_id=incident_id, limit=limit, offset=offset)

    @application.get("/api/v1/remediations/{run_id}", tags=["Remediation"])
    async def remediation_detail(request: Request, response: Response,
                                 run_id: str = ApiPath(..., pattern="^[0-9a-f]{32}$")):
        result = await read_remediations(request, response, "detail", run_id)
        if result is None:
            raise HTTPException(404, detail="Remediation inconnue.")
        return result

    return application


app = create_app()
