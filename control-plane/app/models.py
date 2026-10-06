from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class SourceStatus(BaseModel):
    status: Literal["up", "down", "stale", "missing"] = Field(
        description="Etat de collecte de la source dans Prometheus."
    )
    scrape_age_seconds: float | None


class Metrics(BaseModel):
    cpu_usage_percent: float | None
    memory_usage_percent: float | None
    disk_available_percent: float | None
    moodle_available: bool | None
    moodle_probe_duration_ms: float | None
    moodle_http_status_code: int | None = Field(
        description="Code observe par la sonde; 0 si aucune reponse HTTP."
    )


class MetricsSummary(BaseModel):
    evaluated_at: datetime
    source: Literal["prometheus"] = "prometheus"
    collection: dict[str, SourceStatus]
    metrics: Metrics
    unavailable_metrics: list[str]
