"""Previsions experimentales : artefacts JSON verifies et historique Prometheus."""
import argparse
import asyncio
import hashlib
import json
import math
from datetime import datetime
from pathlib import Path

from .resource_history import (
    FEATURE_NAMES, METRICS, current_collection, feature_vector, read_resource_history,
    strict_json, utc_iso, window_statistics,
)
HORIZONS = (5, 10, 15)
MAX_JSON_BYTES = 2 * 1024 * 1024


class ForecastBundleError(ValueError):
    pass


def number(value, *, positive=False, nonnegative=False):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ForecastBundleError("Nombre fini attendu.")
    if positive and value <= 0 or nonnegative and value < 0:
        raise ForecastBundleError("Domaine numerique invalide.")
    return float(value)


def vector(value, length, **kwargs):
    if not isinstance(value, list) or len(value) != length:
        raise ForecastBundleError("Dimensions du modele invalides.")
    return [number(item, **kwargs) for item in value]


def preferred_predictor(ridge_mae, persistence_mae, trend_mae):
    """Rejouer exactement la selection faite sur la partition de validation."""
    preferred, best = "persistence", persistence_mae
    if trend_mae + 1e-9 < best:
        preferred, best = "linear_trend", trend_mae
    if ridge_mae + 1e-9 < best:
        preferred = "ridge"
    return preferred


class ForecastBundle:
    def __init__(self, directory):
        try:
            contents = {}
            for name in ("models.json", "report.json", "features_metadata.json"):
                path = Path(directory) / name
                if path.stat().st_size > MAX_JSON_BYTES:
                    raise ForecastBundleError("Artefact trop volumineux.")
                contents[name] = path.read_bytes()
            models, report, metadata = (strict_json(contents[name]) for name in contents)
            if (report["files"]["models.json"]["sha256"] != hashlib.sha256(contents["models.json"]).hexdigest()
                    or report["source_metadata_sha256"] != hashlib.sha256(contents["features_metadata.json"]).hexdigest()
                    or report["source_input_sha256"] != metadata["input_sha256"]
                    or models["input_sha256"] != report["input_sha256"]
                    or report["source_range"] != metadata["source_range"]):
                raise ForecastBundleError("Empreintes ou provenance des artefacts incoherentes.")
            if (type(models["schema_version"]) is not int or models["schema_version"] != 1
                    or type(report["schema_version"]) is not int or report["schema_version"] != 1
                    or type(metadata["schema_version"]) is not int or metadata["schema_version"] != 1
                    or report["status"] != "trained_experimental"
                    or models["model_type"] != "standard_scaler_then_multioutput_ridge"
                    or models["feature_names"] != FEATURE_NAMES or metadata["feature_names"] != FEATURE_NAMES
                    or models["target_metrics"] != list(METRICS) or models["clip_range"] != [0, 100]
                    or metadata["horizons_minutes"] != list(HORIZONS)
                    or set(models["horizons"]) != {str(h) for h in HORIZONS}
                    or set(report["horizons"]) != {str(h) for h in HORIZONS}):
                raise ForecastBundleError("Schema ou ordre des variables incompatible.")
            self.step = metadata["step_seconds"]
            self.lookback = metadata["lookback_seconds"]
            self.context = metadata["source_context_seconds"]
            if (any(type(v) is not int for v in (self.step, self.lookback, self.context))
                    or not 15 <= self.step <= 120 or 120 % self.step
                    or not 60 <= self.lookback <= 3600 or self.lookback % 60 or self.lookback % self.step
                    or self.lookback < self.step or self.context != 120
                    or metadata["window_points_including_endpoints"] != self.lookback // self.step + 1
                    or any(h * 60 % self.step for h in HORIZONS)):
                raise ForecastBundleError("Fenetre temporelle incompatible.")
            self.models, self.evaluations = {}, {}
            for horizon in HORIZONS:
                key = str(horizon)
                record, evaluation = models["horizons"][key], report["horizons"][key]
                if (type(record["horizon_minutes"]) is not int or record["horizon_minutes"] != horizon
                        or number(record["alpha"], positive=True) != number(evaluation["selected_alpha"], positive=True)
                        or set(record["preferred_on_validation"]) != set(METRICS)):
                    raise ForecastBundleError("Configuration de l'horizon invalide.")
                vector(record["scaler_mean"], 21)
                vector(record["scaler_scale"], 21, positive=True)
                vector(record["intercept"], 3)
                if not isinstance(record["coefficients"], list) or len(record["coefficients"]) != 3:
                    raise ForecastBundleError("Matrice de coefficients invalide.")
                for coefficients in record["coefficients"]:
                    vector(coefficients, 21)
                scores = {}
                for metric in METRICS:
                    validation = evaluation["validation"][metric]
                    test = evaluation["test"][metric]
                    ridge_mae = number(validation["ridge"]["mae"], nonnegative=True)
                    baseline_mae = number(validation["persistence"]["mae"], nonnegative=True)
                    trend_mae = number(validation["linear_trend"]["mae"], nonnegative=True)
                    preferred = preferred_predictor(ridge_mae, baseline_mae, trend_mae)
                    if (record["preferred_on_validation"][metric] != preferred
                            or validation["preferred_on_validation"] != preferred
                            or test["preferred_on_validation"] != preferred):
                        raise ForecastBundleError("Choix du predicteur different de la validation.")
                    scores[metric] = {
                        "ridge_mae_points": number(test["ridge"]["mae"], nonnegative=True),
                        "persistence_mae_points": number(test["persistence"]["mae"], nonnegative=True),
                        "linear_trend_mae_points": number(test["linear_trend"]["mae"], nonnegative=True),
                    }
                self.models[key], self.evaluations[key] = record, scores
            # Les dates sont seulement descriptives, jamais utilisees comme donnees d'entree.
            self.identifier = models["input_sha256"]
            self.created_at = report["created_at"]
            self.source_range = report["source_range"]
            for value in (self.created_at, self.source_range["start"], self.source_range["end"]):
                if not isinstance(value, str) or not value.endswith("Z"):
                    raise ForecastBundleError("Date UTC d'artefact invalide.")
                datetime.fromisoformat(value.replace("Z", "+00:00"))
            if not isinstance(self.identifier, str) or len(self.identifier) != 64:
                raise ForecastBundleError("Identifiant du modele invalide.")
            self.model_sha256 = hashlib.sha256(contents["models.json"]).hexdigest()
        except (OSError, ValueError, TypeError, KeyError, OverflowError) as exc:
            raise ForecastBundleError("Dossier de previsions incomplet, incompatible ou corrompu.") from exc

    def predict(self, rows):
        try:
            values = feature_vector(rows, self.step, self.lookback)
        except (ValueError, KeyError, TypeError) as exc:
            raise ForecastBundleError("Fenetre de variables inexploitable.") from exc
        window = rows[-(self.lookback // self.step + 1):]
        predictions = {}
        for key, model in self.models.items():
            standardized = [(value - mean) / scale for value, mean, scale in zip(
                values, model["scaler_mean"], model["scaler_scale"])]
            if not all(math.isfinite(value) for value in standardized):
                raise ForecastBundleError("Normalisation non finie.")
            try:
                raw = [math.fsum(c * x for c, x in zip(coefficients, standardized)) + intercept
                       for coefficients, intercept in zip(model["coefficients"], model["intercept"])]
            except (ValueError, OverflowError) as exc:
                raise ForecastBundleError("Calcul numerique inexploitable.") from exc
            if not all(math.isfinite(value) for value in raw):
                raise ForecastBundleError("Calcul de prevision non fini.")
            predictions[key] = {}
            for index, metric in enumerate(METRICS):
                ridge = min(100.0, max(0.0, raw[index]))
                persistence = window[-1][metric]
                slope = values[FEATURE_NAMES.index(f"{metric}__slope_per_minute")]
                raw_trend = persistence + slope * int(key)
                linear_trend = min(100.0, max(0.0, raw_trend))
                method = model["preferred_on_validation"][metric]
                selected = {"ridge": ridge, "persistence": persistence, "linear_trend": linear_trend}
                predictions[key][metric] = {
                    "value": round(selected[method], 3),
                    "method": method,
                    "ridge_value": round(ridge, 3),
                    "persistence_value": round(persistence, 3),
                    "linear_trend_value": round(linear_trend, 3),
                    "ridge_clipped": raw[index] < 0 or raw[index] > 100,
                    "linear_trend_clipped": raw_trend < 0 or raw_trend > 100,
                    "test_evaluation": self.evaluations[key][metric],
                }
        return predictions


class ForecastService:
    def __init__(self, prometheus, bundle):
        self.prometheus, self.bundle = prometheus, bundle
        self.lock = asyncio.Lock()
        self.cached_end, self.cached = None, None

    async def summary(self, now):
        end = int(now.timestamp()) // self.bundle.step * self.bundle.step
        async with self.lock:
            # Seules les previsions disponibles sont cachees sur la grille de 30 s.
            # Une verification instantanee detecte une collecte arretee depuis le calcul.
            if self.cached_end == end and self.cached is not None:
                status = await current_collection(self.prometheus, now)
                if status.status == "up":
                    return {**self.cached, "evaluated_at": utc_iso(now.timestamp()),
                            "collection": {"linux": status.model_dump()}, "cached": True}
                return self.unavailable(now, end, "linux_collection_unavailable", [], status)
            return await self.calculate(now, end)

    def unavailable(self, now, end, reason, invalid_points, status=None):
        return {"status": "unavailable", "experimental": True,
                "evaluated_at": utc_iso(now.timestamp()), "input_evaluated_at": utc_iso(end),
                "reason": reason, "required_history_seconds": self.bundle.lookback + self.bundle.context,
                "invalid_points": invalid_points, "forecasts": {},
                "collection": {"linux": status.model_dump()} if status is not None else {},
                "model_id": self.bundle.identifier}

    async def calculate(self, now, end):
        history = await read_resource_history(self.prometheus, now, step=self.bundle.step,
                                              lookback=self.bundle.lookback, context=self.bundle.context)
        if history.reason:
            return self.unavailable(now, end, history.reason, history.invalid_points, history.collection)
        start, rows, status = history.start, history.rows, history.collection
        predictions = self.bundle.predict(rows)
        response = {"status": "available", "experimental": True, "evaluated_at": utc_iso(now.timestamp()),
                    "input_evaluated_at": utc_iso(end), "history_start": utc_iso(start),
                    "feature_window_start": utc_iso(end - self.bundle.lookback), "step_seconds": self.bundle.step,
                    "required_history_seconds": self.bundle.lookback + self.bundle.context,
                    "model_id": self.bundle.identifier, "model_sha256": self.bundle.model_sha256,
                    "model_created_at": self.bundle.created_at, "training_range": self.bundle.source_range,
                    "selection": "validation", "cached": False, "collection": {"linux": status.model_dump()},
                    "current_metrics": rows[-1], "forecasts": {},
                    "semantics": {"target": "Valeur a t+h ; CPU moyenne sur deux minutes ; disque disponible.",
                                  "units": "Pourcentages ; erreurs de test en points de pourcentage.",
                                  "scope": "Experience sur le pilote ; aucune probabilite de panne ni action automatique."}}
        for horizon in HORIZONS:
            response["forecasts"][str(horizon)] = {"target_evaluated_at": utc_iso(end + horizon * 60),
                                                   "metrics": predictions[str(horizon)]}
        self.cached_end, self.cached = end, response
        return response


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Verifier les artefacts des previsions.")
    parser.add_argument("--check-bundle", required=True)
    args = parser.parse_args()
    try:
        bundle = ForecastBundle(args.check_bundle)
    except ForecastBundleError as exc:
        parser.exit(1, f"Verification echouee : {exc}\n")
    print(json.dumps({"status": "validated", "model_id": bundle.identifier, "horizons_minutes": list(HORIZONS),
                      "step_seconds": bundle.step, "required_history_seconds": bundle.lookback + bundle.context}))
