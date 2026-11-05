"""Detection experimentale en observation ; artefacts numeriques verifies."""
import argparse
import asyncio
import hashlib
import json
import math
from datetime import datetime
from pathlib import Path
import re

from . import isolation_model
from .resource_history import (
    FEATURE_NAMES, METRICS, current_collection, feature_vector, read_resource_history,
    strict_json, utc_iso,
)

SETTINGS_KEYS = (
    "tool_version", "schema_version", "source_metadata_sha256", "source_input_sha256",
    "source_features_sha256", "environment", "forest_parameters", "threshold_calibration",
    "minimum_rows", "operational_rule_thresholds",
)
SPLITS = ("train", "validation", "test")
QUALITY = {"status": "not_evaluated_no_incident_labels", "precision": None,
           "recall": None, "false_positive_rate": None}


class AnomalyBundleError(ValueError):
    pass


def finite(value, low=None, high=None):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise AnomalyBundleError("Nombre fini attendu.")
    if low is not None and value < low or high is not None and value > high:
        raise AnomalyBundleError("Nombre hors du domaine attendu.")
    return float(value)


def utc_timestamp(value):
    if not isinstance(value, str) or not value.endswith("Z"):
        raise AnomalyBundleError("Date UTC attendue.")
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def sha(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise AnomalyBundleError("Empreinte SHA-256 invalide.")
    return value


class AnomalyBundle:
    def __init__(self, directory):
        try:
            contents = {}
            for name, limit in (("model.json", isolation_model.MAX_MODEL_BYTES),
                                ("report.json", 1024 * 1024), ("features_metadata.json", 1024 * 1024)):
                path = Path(directory) / name
                if path.stat().st_size > limit:
                    raise AnomalyBundleError("Artefact trop volumineux.")
                contents[name] = path.read_bytes()
                if len(contents[name]) > limit:
                    raise AnomalyBundleError("Artefact trop volumineux.")
            model, report, metadata = (strict_json(contents[name]) for name in contents)
            isolation_model.validate_model(model, FEATURE_NAMES)
            self.model_sha256 = hashlib.sha256(contents["model.json"]).hexdigest()
            if (sha(report["files"]["model.json"]["sha256"]) != self.model_sha256
                    or sha(report["source_metadata_sha256"]) != hashlib.sha256(contents["features_metadata.json"]).hexdigest()
                    or sha(report["source_input_sha256"]) != metadata["input_sha256"]
                    or sha(report["source_features_sha256"]) != metadata["files"]["features.csv"]["sha256"]
                    or report["source_range"] != metadata["source_range"]
                    or report["split_periods"] != metadata["split_periods"]):
                raise AnomalyBundleError("Empreintes ou provenance incoherentes.")
            settings = {key: report[key] for key in SETTINGS_KEYS}
            fingerprint = hashlib.sha256(json.dumps(settings, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            self.identifier = sha(report["input_sha256"])
            if self.identifier != fingerprint:
                raise AnomalyBundleError("Identifiant de l'experience incoherent.")
            if (report["tool_version"] != "0.1.0" or type(report["schema_version"]) is not int
                    or report["schema_version"] != 1 or type(metadata["schema_version"]) is not int
                    or metadata["schema_version"] not in (1, 2) or report["status"] != "trained_experimental"
                    or metadata["feature_names"] != FEATURE_NAMES
                    or report["threshold_calibration"] != {"partition": "train", "quantile": 0.98,
                        "method": "higher", "comparison": "strictly_greater"}
                    or report["detection_quality"] != QUALITY
                    or report["minimum_rows"] != {"train": 50, "validation": 10, "test": 10}
                    or set(report["files"]) != {"model.json", "scores.csv"}):
                raise AnomalyBundleError("Schema ou protocole experimental incompatible.")
            self.step, self.lookback, self.context = (metadata[key] for key in
                ("step_seconds", "lookback_seconds", "source_context_seconds"))
            if (any(type(value) is not int for value in (self.step, self.lookback, self.context))
                    or not 15 <= self.step <= 120 or 120 % self.step
                    or not 60 <= self.lookback <= 3600 or self.lookback % 60 or self.lookback % self.step
                    or self.lookback < self.step or self.context != 120
                    or type(metadata["window_points_including_endpoints"]) is not int
                    or metadata["window_points_including_endpoints"] != self.lookback // self.step + 1):
                raise AnomalyBundleError("Fenetre temporelle incompatible.")
            self.created_at, self.source_range = report["created_at"], report["source_range"]
            utc_timestamp(self.created_at)
            start, end = (utc_timestamp(self.source_range[key]) for key in ("start", "end"))
            if (start > end or start % self.step or end % self.step
                    or self.source_range["step_seconds"] != self.step):
                raise AnomalyBundleError("Periode source incompatible.")
            self.threshold = finite(model["anomaly_threshold"], 0, 1)
            if self.threshold != finite(report["anomaly_threshold"], 0, 1):
                raise AnomalyBundleError("Seuil different entre le rapport et les arbres.")
            if finite(report["json_replay_max_absolute_difference"], 0) > 1e-10:
                raise AnomalyBundleError("Verification numerique insuffisante.")
            self.rules = report["operational_rule_thresholds"]
            if set(self.rules) != set(METRICS):
                raise AnomalyBundleError("Seuils operationnels incompatibles.")
            for threshold in self.rules.values():
                finite(threshold, 0, 100)
            self.by_split = {}
            if set(report["by_split"]) != set(SPLITS):
                raise AnomalyBundleError("Partitions d'entrainement invalides.")
            for split in SPLITS:
                record = report["by_split"][split]
                keys = ("rows", "statistical_anomalies", "operational_rule_alerts", "both_flags")
                if any(type(record[key]) is not int for key in keys):
                    raise AnomalyBundleError("Effectifs non entiers.")
                rows = record["rows"]
                if (not report["minimum_rows"][split] <= rows <= 50_000
                        or rows != metadata["summary"]["rows_by_split"][split]
                        or any(not 0 <= record[key] <= rows for key in keys[1:])
                        or record["both_flags"] > min(record["statistical_anomalies"], record["operational_rule_alerts"])
                        or abs(finite(record["statistical_anomaly_rate_percent"], 0, 100)
                               - 100 * record["statistical_anomalies"] / rows) > 1e-8):
                    raise AnomalyBundleError("Statistiques des partitions incoherentes.")
                minimum, median, maximum = (finite(record[key], 0, 1) for key in ("score_min", "score_median", "score_max"))
                if not minimum <= median <= maximum:
                    raise AnomalyBundleError("Distribution des scores incoherente.")
                self.by_split[split] = {key: record[key] for key in (*keys, "statistical_anomaly_rate_percent")}
            counts = sum(record["rows"] for record in self.by_split.values())
            if (type(report["files"]["scores.csv"]["rows"]) is not int
                    or report["files"]["scores.csv"]["rows"] != counts):
                raise AnomalyBundleError("Effectif du fichier de scores incoherent.")
            sha(report["files"]["scores.csv"]["sha256"])
            forest = report["forest_parameters"]
            if (type(forest["n_estimators"]) is not int or forest["n_estimators"] != len(model["trees"])
                    or forest["n_estimators"] != 100 or type(forest["max_samples"]) is not int
                    or forest["max_samples"] != model["max_samples"]
                    or forest["max_samples"] != min(256, self.by_split["train"]["rows"])
                    or forest["random_state"] != 42 or forest["threads"] != 1
                    or forest["max_features"] != 1.0 or forest["bootstrap"] is not False):
                raise AnomalyBundleError("Configuration de la foret incoherente.")
            constants = report["constant_training_features"]
            if (not isinstance(constants, list) or any(name not in FEATURE_NAMES for name in constants)
                    or len(set(constants)) != len(constants)):
                raise AnomalyBundleError("Variables constantes invalides.")
            self.constant_features, self.model = constants, model
        except (OSError, ValueError, TypeError, KeyError, IndexError, OverflowError, RecursionError) as exc:
            raise AnomalyBundleError("Dossier d'anomalies incomplet, incompatible ou corrompu.") from exc

    def evaluate(self, rows):
        try:
            values = feature_vector(rows, self.step, self.lookback)
            score = isolation_model.score_rows(self.model, [values])[0]
        except (ValueError, KeyError, TypeError, OverflowError) as exc:
            raise AnomalyBundleError("Calcul du score inexploitable.") from exc
        reasons = []
        for metric in METRICS:
            current = rows[-1][metric]
            threshold = self.rules[metric]
            triggered = current <= threshold if metric == "disk_available_percent" else current >= threshold
            if triggered:
                reasons.append({"metric": metric, "value": current, "threshold": threshold,
                                "comparison": "less_or_equal" if metric == "disk_available_percent" else "greater_or_equal"})
        return {"score": score, "threshold": self.threshold, "statistical_anomaly": score > self.threshold,
                "comparison": "strictly_greater", "operational_rules": {
                    "alert": bool(reasons), "reasons": reasons, "thresholds": dict(self.rules)}}

    def description(self):
        return {"status": "configured", "experimental": True, "mode": "observation",
                "model_id": self.identifier, "model_sha256": self.model_sha256,
                "model_created_at": self.created_at, "training_range": self.source_range,
                "feature_count": len(FEATURE_NAMES), "step_seconds": self.step,
                "required_history_seconds": self.lookback + self.context,
                "threshold": self.threshold, "threshold_calibration": {
                    "partition": "train", "quantile": 0.98, "comparison": "strictly_greater"},
                "training_statistics": self.by_split, "constant_training_features": self.constant_features,
                "detection_quality": dict(QUALITY), "semantics": {
                    "score": "Score eleve = observation atypique ; aucune probabilite de panne.",
                    "statistics": "Comptages historiques des partitions ; aucune mesure de sante actuelle.",
                    "scope": "Ressources CPU, RAM et disque ; un score ne determine pas une cause de panne Moodle.",
                    "operational_rules": "Seuils experimentaux a valider avec BeeCoders.",
                    "action": "Un signalement ne declenche aucune remediation."}}


class AnomalyService:
    def __init__(self, prometheus, bundle):
        self.prometheus, self.bundle = prometheus, bundle
        self.lock = asyncio.Lock()
        self.cached_end, self.cached = None, None

    def unavailable(self, now, end, reason, invalid_points, status):
        return {"status": "unavailable", "experimental": True, "mode": "observation",
                "evaluated_at": utc_iso(now.timestamp()), "input_evaluated_at": utc_iso(end),
                "reason": reason, "required_history_seconds": self.bundle.lookback + self.bundle.context,
                "invalid_points": invalid_points, "anomaly": None,
                "collection": {"linux": status.model_dump()}, "model_id": self.bundle.identifier}

    async def summary(self, now):
        end = int(now.timestamp()) // self.bundle.step * self.bundle.step
        async with self.lock:
            if self.cached_end == end and self.cached is not None:
                status = await current_collection(self.prometheus, now)
                if status.status == "up":
                    return {**self.cached, "evaluated_at": utc_iso(now.timestamp()),
                            "collection": {"linux": status.model_dump()}, "cached": True}
                return self.unavailable(now, end, "linux_collection_unavailable", [], status)
            history = await read_resource_history(self.prometheus, now, step=self.bundle.step,
                lookback=self.bundle.lookback, context=self.bundle.context)
            if history.reason:
                return self.unavailable(now, end, history.reason, history.invalid_points, history.collection)
            anomaly = await asyncio.to_thread(self.bundle.evaluate, history.rows)
            response = {"status": "available", "experimental": True, "mode": "observation",
                        "evaluated_at": utc_iso(now.timestamp()), "input_evaluated_at": utc_iso(end),
                        "history_start": utc_iso(history.start),
                        "feature_window_start": utc_iso(end - self.bundle.lookback),
                        "step_seconds": self.bundle.step, "required_history_seconds": self.bundle.lookback + self.bundle.context,
                        "model_id": self.bundle.identifier, "model_sha256": self.bundle.model_sha256,
                        "model_created_at": self.bundle.created_at, "training_range": self.bundle.source_range,
                        "cached": False, "collection": {"linux": history.collection.model_dump()},
                        "current_metrics": history.rows[-1], "anomaly": anomaly,
                        "detection_quality": dict(QUALITY), "semantics": self.bundle.description()["semantics"]}
            self.cached_end, self.cached = end, response
            return response


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Verifier les artefacts des anomalies.")
    parser.add_argument("--check-bundle", required=True)
    args = parser.parse_args()
    try:
        bundle = AnomalyBundle(args.check_bundle)
    except AnomalyBundleError as exc:
        parser.exit(1, f"Verification echouee : {exc}\n")
    print(json.dumps(bundle.description(), ensure_ascii=False, allow_nan=False))
