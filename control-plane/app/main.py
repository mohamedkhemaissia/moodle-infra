import os
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import httpx
from fastapi import FastAPI, HTTPException, Request

from .models import MetricsSummary
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
) -> FastAPI:
    url = prometheus_url or os.getenv("PROMETHEUS_URL", "http://127.0.0.1:9090")
    max_age = float(os.getenv("MAX_SCRAPE_AGE_SECONDS", "60"))
    model_directory = forecasts_dir if forecasts_dir is not None else os.getenv("FORECASTS_DIR", "")

    anomaly_directory = anomalies_dir if anomalies_dir is not None else os.getenv("ANOMALIES_DIR", "")

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
            yield

    application = FastAPI(
        title="Pilotage Moodle",
        description="Supervision, previsions et detection experimentale des anomalies du pilote Moodle.",
        version="0.3.0",
        lifespan=lifespan,
    )

    @application.get("/api/v1/health", tags=["Sante"])
    async def health():
        return {"status": "ok", "service": "moodle-control-plane", "version": "0.3.0"}

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

    return application


app = create_app()
