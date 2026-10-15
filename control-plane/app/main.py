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
from .container_metrics import summary as container_metrics_summary, parse_targets, DEFAULT_TARGETS
from .auth import Settings as AuthSettings, install as install_auth
from .diagnostic_requests import install as install_diagnostic_requests
from .course_operations import CourseOperationError, CourseOperationStore, install as install_course_operations
from .site_operations import SiteOperationError, SiteOperationStore, install as install_site_operations
from .recovery_requests import install as install_recovery_requests
from .operating_policy import install as install_operating_policy
from .php_profiles import read_status as read_php_profiles
from .backup_catalog_status import select_point
from .backups import read_summary as read_backup_summary, read_pilotage_summary, read_catalog
from pilotage.auth import AuthStore, AuthStorageError
from pilotage.recovery import RecoveryStore, RecoveryStorageError
from pilotage.recovery_execution import RecoveryExecutionStore
from pilotage.operating_policy import PolicyStore, PolicyStorageError
from pilotage.decision_journal import DecisionJournal
from pilotage.diagnostic_requests import DiagnosticRequestStore, DiagnosticRequestStorageError
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
    diagnostic_requests_enabled: bool | None = None,
    diagnostic_request_store=None,
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
    recovery_requests_enabled: bool | None = None,
    recovery_store=None,
    recovery_execution_enabled: bool | None = None,
    policies_enabled: bool | None = None,
    policy_store=None,
    container_metrics_enabled: bool | None = None,
    decision_journal_enabled: bool | None = None,
    course_operations_enabled: bool | None = None,
    course_operations_dir: str | None = None,
    site_operations_enabled: bool | None = None,
    site_operations_dir: str | None = None,
) -> FastAPI:
    authentication = AuthSettings.load(auth_enabled, auth_origins, auth_secure_cookie, auth_worker_hash_file)
    backup_file = backup_status_file if backup_status_file is not None else os.getenv("BACKUP_STATUS_FILE", "")
    pilotage_backup_file = (pilotage_backup_status_file if pilotage_backup_status_file is not None
                           else os.getenv("PILOTAGE_BACKUP_STATUS_FILE", ""))
    catalog_file = backup_catalog_file if backup_catalog_file is not None else os.getenv("BACKUP_CATALOG_FILE", "")
    if recovery_requests_enabled is None:
        setting = os.getenv("RECOVERY_REQUESTS_ENABLED", "false")
        if setting not in ("true", "false"):
            raise ValueError("RECOVERY_REQUESTS_ENABLED doit valoir true ou false.")
        recovery_requests_enabled = setting == "true"
    if type(recovery_requests_enabled) is not bool or (recovery_requests_enabled and (not authentication.enabled or not catalog_file)):
        raise ValueError("Les demandes exigent l'authentification et un catalogue configure.")
    if recovery_execution_enabled is None:
        setting = os.getenv("RECOVERY_EXECUTION_ENABLED", "false")
        if setting not in ("true", "false"):
            raise ValueError("RECOVERY_EXECUTION_ENABLED doit valoir true ou false.")
        recovery_execution_enabled = setting == "true"
    if type(recovery_execution_enabled) is not bool or (recovery_execution_enabled and not recovery_requests_enabled):
        raise ValueError("L execution isolee exige le parcours de demandes active.")
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
    if container_metrics_enabled is None:
        setting = os.getenv("CONTAINER_METRICS_ENABLED", "false")
        if setting not in ("true", "false"):
            raise ValueError("CONTAINER_METRICS_ENABLED doit valoir true ou false.")
        container_metrics_enabled = setting == "true"
    if type(container_metrics_enabled) is not bool:
        raise ValueError("Activation des mesures de conteneurs invalide.")
    container_targets = parse_targets(os.getenv("CONTAINER_METRICS_TARGETS", DEFAULT_TARGETS))
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
    if diagnostic_requests_enabled is None:
        setting = os.getenv("DIAGNOSTIC_REQUESTS_ENABLED", "false")
        if setting not in ("true", "false"):
            raise ValueError("DIAGNOSTIC_REQUESTS_ENABLED doit valoir true ou false.")
        diagnostic_requests_enabled = setting == "true"
    if type(diagnostic_requests_enabled) is not bool or (diagnostic_requests_enabled and (
            not authentication.enabled or not decisions_enabled or not incident_directory or not diagnostic_directory)):
        raise ValueError("Les demandes de diagnostic exigent les comptes, les incidents, les decisions et le journal de diagnostics.")
    if decision_journal_enabled is None:
        setting = os.getenv("DECISION_JOURNAL_ENABLED", "false")
        if setting not in ("true", "false"):
            raise ValueError("DECISION_JOURNAL_ENABLED doit valoir true ou false.")
        decision_journal_enabled = setting == "true"
    if type(decision_journal_enabled) is not bool or (decision_journal_enabled and (not decisions_enabled or not incident_directory)):
        raise ValueError("Le journal exige les incidents et les decisions.")
    if policies_enabled is None:
        setting=os.getenv("POLICIES_ENABLED","false")
        if setting not in ("true","false"):
            raise ValueError("POLICIES_ENABLED doit valoir true ou false.")
        policies_enabled=setting == "true"
    if type(policies_enabled) is not bool or (policies_enabled and (not authentication.enabled or not decisions_enabled or not incident_directory)):
        raise ValueError("Les politiques exigent les comptes, les incidents et les decisions.")
    if course_operations_enabled is None:
        setting = os.getenv("COURSE_OPERATIONS_ENABLED", "false")
        if setting not in ("true", "false"):
            raise ValueError("COURSE_OPERATIONS_ENABLED doit valoir true ou false.")
        course_operations_enabled = setting == "true"
    course_operations_directory = (course_operations_dir if course_operations_dir is not None
                                   else os.getenv("COURSE_OPERATIONS_DIR", ""))
    if type(course_operations_enabled) is not bool or (course_operations_enabled and (
            not authentication.enabled or not course_operations_directory)):
        raise ValueError("Les sauvegardes de cours exigent les comptes et un stockage dedie.")
    if site_operations_enabled is None:
        setting = os.getenv("SITE_OPERATIONS_ENABLED", "false")
        if setting not in ("true", "false"):
            raise ValueError("SITE_OPERATIONS_ENABLED doit valoir true ou false.")
        site_operations_enabled = setting == "true"
    site_operations_directory = (site_operations_dir if site_operations_dir is not None
                                 else os.getenv("SITE_OPERATIONS_DIR", ""))
    if type(site_operations_enabled) is not bool or (site_operations_enabled and (
            not authentication.enabled or not site_operations_directory)):
        raise ValueError("La sauvegarde complete exige les comptes et un stockage dedie.")

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        application.state.auth_store = (auth_store or AuthStore()) if authentication.enabled else None
        application.state.auth_password_slots = asyncio.Semaphore(2)
        if authentication.enabled:
            await asyncio.to_thread(application.state.auth_store.check)
        application.state.diagnostic_requests = None
        if diagnostic_requests_enabled:
            application.state.diagnostic_requests = diagnostic_request_store or DiagnosticRequestStore()
            await asyncio.to_thread(application.state.diagnostic_requests.check)
        application.state.operating_policy=(policy_store or PolicyStore()) if policies_enabled else None
        if application.state.operating_policy is not None:
            await asyncio.to_thread(application.state.operating_policy.check)
        application.state.course_operations = None
        if course_operations_enabled:
            application.state.course_operations = CourseOperationStore(course_operations_directory)
            await asyncio.to_thread(application.state.course_operations.check)
        application.state.site_operations = None
        if site_operations_enabled:
            application.state.site_operations = SiteOperationStore(site_operations_directory)
            await asyncio.to_thread(application.state.site_operations.check)
        application.state.recovery_execution_enabled = recovery_execution_enabled
        application.state.recovery_requests = None
        if recovery_requests_enabled:
            application.state.recovery_requests = recovery_store or (RecoveryExecutionStore if recovery_execution_enabled else RecoveryStore)(
                selector=lambda scope, snapshot, now: select_point(catalog_file, now, scope, snapshot))
            await asyncio.to_thread(application.state.recovery_requests.check)
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
            application.state.decision_journal = DecisionJournal(application.state.incidents) if decision_journal_enabled else None
            application.state.remediations = RemediationStore(remediation_directory) if remediation_directory else None
            application.state.diagnostics = DiagnosticStore(diagnostic_directory) if diagnostic_directory else None
            yield

    application = FastAPI(
        title="Pilotage Moodle",
        description="Supervision, analyses experimentales et historique des incidents observes du pilote Moodle.",
        version="0.17.0",
        lifespan=lifespan,
    )
    install_auth(application, authentication)
    install_diagnostic_requests(application)
    install_recovery_requests(application)
    install_operating_policy(application)
    install_course_operations(application)
    install_site_operations(application)

    @application.get("/api/v1/health", tags=["Sante"])
    async def health():
        return {"status": "ok", "service": "moodle-control-plane", "version": "0.17.0"}

    @application.get("/api/v1/metrics/containers", tags=["Supervision"])
    async def container_metrics(request: Request, response: Response):
        response.headers["Cache-Control"] = "no-store"
        if not container_metrics_enabled:
            raise HTTPException(503, detail="Le suivi des conteneurs n'est pas configure.")
        try:
            return await container_metrics_summary(request.app.state.prometheus, datetime.now(timezone.utc), container_targets)
        except PrometheusUnavailable as exc:
            raise HTTPException(503, detail="Prometheus est indisponible.") from exc
        except PrometheusInvalidResponse as exc:
            raise HTTPException(502, detail="Les mesures de conteneurs sont inexploitables.") from exc

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
            if request.app.state.recovery_requests is not None:
                request.app.state.recovery_requests.check()
                result["recovery_requests"] = {"status": "available", "schema_version": 1, "backend": "postgresql"}
            if request.app.state.operating_policy is not None:
                request.app.state.operating_policy.check()
                result["policies"]={"status":"available","schema_version":1,"backend":"postgresql"}
            if request.app.state.decision_journal is not None:
                with request.app.state.incidents.connection() as connection:
                    request.app.state.decision_journal.check(connection)
                result["decision_journal"]={"status":"available","schema_version":1,"backend":"postgresql"}
            if request.app.state.diagnostic_requests is not None:
                request.app.state.diagnostic_requests.check()
                result["diagnostic_requests"] = {"status": "available", "schema_version": 1,
                                                  "backend": "postgresql", "mode": "read_only"}
            if request.app.state.course_operations is not None:
                request.app.state.course_operations.check()
                result["course_operations"] = {"status": "available", "schema_version": 1,
                                                "backend": "filesystem", "mode": "course_only"}
            if request.app.state.site_operations is not None:
                request.app.state.site_operations.check()
                result["site_operations"] = {"status": "available", "schema_version": 1,
                                              "backend": "filesystem", "mode": "site_full"}
            return result
        try:
            stores = await asyncio.to_thread(inspect_storage)
        except (IncidentStorageError, DiagnosticStorageError, DiagnosticRequestStorageError,
                AuthStorageError, RecoveryStorageError, PolicyStorageError, CourseOperationError,
                SiteOperationError) as exc:
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

    def journal_store(request):
        store = request.app.state.decision_journal
        if store is None:
            raise HTTPException(503, detail="Le journal des decisions n'est pas configure.")
        return store

    @application.get("/api/v1/decisions/history", tags=["Decisions"])
    async def decision_history(request: Request, response: Response,
                               limit: int = Query(50, ge=1, le=100),
                               before_id: int | None = Query(None, ge=1, le=9223372036854775807),
                               status: Literal["recommendation", "blocked", "monitoring", "closed"] | None = None,
                               incident_id: int | None = Query(None, ge=1, le=9223372036854775807)):
        response.headers["Cache-Control"] = "no-store"
        return await read_incidents(journal_store(request).list, datetime.now(timezone.utc).timestamp(),
                                    limit=limit, before_id=before_id, status=status, incident_id=incident_id)

    @application.get("/api/v1/decisions/history/{entry_id}", tags=["Decisions"])
    async def decision_history_detail(request: Request, response: Response,
                                      entry_id: int = ApiPath(..., ge=1, le=9223372036854775807)):
        response.headers["Cache-Control"] = "no-store"
        result = await read_incidents(journal_store(request).detail, entry_id)
        if result is None:
            raise HTTPException(404, detail="Decision historisee inconnue.")
        return result

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
        result = await read_remediations(request, response, "summary")
        policy_store = request.app.state.operating_policy
        if policy_store is not None:
            try:
                control = await asyncio.to_thread(policy_store.view)
            except PolicyStorageError as exc:
                raise HTTPException(503, detail="Les politiques de correction sont indisponibles.") from exc
            policy = control["policy"]
            result = {**result, "mode": "policy_guarded", "operating_policy": policy,
                      "automatic_worker": control["worker"],
                      "automatic_execution_enabled": policy["mode"] == "automatic" and not policy["suspended"],
                      "semantics": {**result.get("semantics", {}),
                          "action": "Remise en service de PHP arrete ou profil PHP-FPM prevalide selon les autorisations et les controles techniques.",
                          "evaluation": "Une evaluation active exige une approbation humaine valide pour cet incident."}}
        return result

    @application.get("/api/v1/php-fpm/profiles", tags=["Remediation"])
    async def php_profiles(response: Response):
        response.headers["Cache-Control"] = "no-store"
        return await asyncio.to_thread(read_php_profiles, remediation_directory, datetime.now(timezone.utc).timestamp())

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
