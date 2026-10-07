import os
import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import httpx
from fastapi import FastAPI, HTTPException, Request, Query, Path as ApiPath
from typing import Literal

from .models import MetricsSummary
from .incidents import IncidentStore, IncidentStorageError
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
) -> FastAPI:
    url = prometheus_url or os.getenv("PROMETHEUS_URL", "http://127.0.0.1:9090")
    max_age = float(os.getenv("MAX_SCRAPE_AGE_SECONDS", "60"))
    model_directory = forecasts_dir if forecasts_dir is not None else os.getenv("FORECASTS_DIR", "")

    anomaly_directory = anomalies_dir if anomalies_dir is not None else os.getenv("ANOMALIES_DIR", "")

    incident_directory = incidents_dir if incidents_dir is not None else os.getenv("INCIDENTS_DIR", "")

    @asynccontextmanager
    async def lifespan(application: FastAPI):
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
            yield

    application = FastAPI(
        title="Pilotage Moodle",
        description="Supervision, analyses experimentales et historique des incidents observes du pilote Moodle.",
        version="0.4.0",
        lifespan=lifespan,
    )

    @application.get("/api/v1/health", tags=["Sante"])
    async def health():
        return {"status": "ok", "service": "moodle-control-plane", "version": "0.4.0"}

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

    return application


app = create_app()
