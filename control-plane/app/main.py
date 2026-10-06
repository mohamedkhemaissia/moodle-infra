import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import httpx
from fastapi import FastAPI, HTTPException, Request

from .models import MetricsSummary
from .prometheus import (
    PrometheusClient,
    PrometheusInvalidResponse,
    PrometheusUnavailable,
)


def create_app(
    prometheus_url: str | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    url = prometheus_url or os.getenv("PROMETHEUS_URL", "http://127.0.0.1:9090")
    max_age = float(os.getenv("MAX_SCRAPE_AGE_SECONDS", "60"))

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        async with httpx.AsyncClient(
            base_url=url.rstrip("/") + "/",
            timeout=5,
            trust_env=False,
            transport=transport,
        ) as client:
            application.state.prometheus = PrometheusClient(client, max_age)
            yield

    application = FastAPI(
        title="Pilotage Moodle",
        description="Lecture des metriques de supervision du pilote Moodle.",
        version="0.1.0",
        lifespan=lifespan,
    )

    @application.get("/api/v1/health", tags=["Sante"])
    async def health():
        return {"status": "ok", "service": "moodle-control-plane", "version": "0.1.0"}

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

    return application


app = create_app()
