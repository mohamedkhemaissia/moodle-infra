"""Entrainer Isolation Forest sur les variables Moodle deja preparees.

Apprentissage et calibration sur train uniquement ; validation et test en
observation. Une anomalie statistique ne constitue pas un incident confirme.
"""

from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
import hashlib
from importlib.metadata import version as package_version
import json
import math
import os
from pathlib import Path
import platform
import shutil
import sys
import tempfile
import time

import numpy as np
from sklearn.ensemble import IsolationForest
from threadpoolctl import threadpool_limits

from ml import export_metrics as exporter
from ml import prepare_dataset as preparation
from ml import prepare_features as features
from ml import isolation_model as numeric


VERSION = "0.1.0"
MIN_ROWS = {"train": 50, "validation": 10, "test": 10}
TRAIN_QUANTILE = 0.98
RULE_THRESHOLDS = {"cpu_usage_percent": 85.0, "memory_usage_percent": 90.0, "disk_available_percent": 10.0}
AnomalyError = exporter.ExportError
SCORE_FIELDS = (
    "evaluated_at", "split", "resource_segment_id", *exporter.RESOURCE_METRICS,
    "anomaly_score", "anomaly_threshold", "statistical_anomaly",
    "operational_rule_alert", "operational_rule_reasons",
)


@dataclass
class Inputs:
    directory: Path
    metadata: dict
    metadata_sha256: str
    rows: list[dict]


def read_inputs(directory: Path) -> Inputs:
    """Verifier le fichier utilise et les contextes temporels disjoints."""
    try:
        metadata_path = directory / "metadata.json"
        path = directory / "features.csv"
        if metadata_path.stat().st_size > 1024 * 1024 or path.stat().st_size > 32 * 1024 * 1024:
            raise AnomalyError("Fichier d'entree trop grand.")
        raw_metadata = metadata_path.read_bytes()
        metadata = features.strict_json(raw_metadata)
        keys = ("tool_version", "schema_version", "source_dataset_sha256", "source_metadata_sha256",
                "step_seconds", "lookback_seconds", "window_points_including_endpoints", "source_context_seconds",
                "feature_names", "split_policy")
        description = {key: metadata[key] for key in keys}
        fingerprint = hashlib.sha256(json.dumps(description, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        if (type(metadata["schema_version"]) is not int or metadata["schema_version"] != features.SCHEMA_VERSION
                or metadata["input_sha256"] != fingerprint
                or metadata["feature_names"] != list(features.FEATURE_NAMES)
                or metadata["source_context_seconds"] != features.SOURCE_CONTEXT_SECONDS
                or metadata["split_policy"] != "chronological_duration_70_15_15_with_disjoint_contexts"):
            raise AnomalyError("Schema ou configuration des variables incoherents.")
        step, lookback = metadata["step_seconds"], metadata["lookback_seconds"]
        if type(step) is not int or step < 15 or type(lookback) is not int or lookback % 60:
            raise AnomalyError("Pas ou fenetre invalide.")
        if features.validate_settings(step, lookback // 60) != metadata["window_points_including_endpoints"]:
            raise AnomalyError("Taille de fenetre incoherente.")
        bounds = metadata["source_range"]
        first, last = preparation.parse_utc(bounds["start"]), preparation.parse_utc(bounds["end"])
        if first > last or first % step or last % step or bounds["step_seconds"] != step:
            raise AnomalyError("Periode source invalide.")
        periods = metadata["split_periods"]
        if not isinstance(periods, list) or [p["split"] for p in periods] != list(features.SPLITS):
            raise AnomalyError("Trois partitions temporelles ordonnees sont necessaires.")
        by_split, previous_end = {}, None
        for period in periods:
            start, end = preparation.parse_utc(period["start"]), preparation.parse_utc(period["end_exclusive"])
            if (start >= end or start % step or end % step or start < first or end > last + step
                    or (previous_end is not None and start != previous_end)):
                raise AnomalyError("Partitions temporelles invalides ou chevauchantes.")
            by_split[period["split"]], previous_end = (start, end), end
        manifest = metadata["files"]["features.csv"]
        if exporter.file_sha256(path) != manifest["sha256"]:
            raise AnomalyError("Empreinte du features.csv invalide.")
        rows, previous_time, previous_split, previous_segment = [], -1, None, 0
        with path.open(encoding="utf-8", newline="") as stream:
            reader = csv.DictReader(stream)
            if reader.fieldnames != list(features.FEATURE_FIELDS):
                raise AnomalyError("Colonnes du CSV non prises en charge.")
            for index, raw in enumerate(reader):
                if index >= features.MAX_INPUT_ROWS or any(not isinstance(v, str) for v in raw.values()):
                    raise AnomalyError("CSV trop grand ou ligne incomplete.")
                timestamp = preparation.parse_utc(raw["evaluated_at"])
                start = preparation.parse_utc(raw["window_start"])
                context = preparation.parse_utc(raw["input_context_start"])
                if (timestamp <= previous_time or timestamp % step or start != timestamp - lookback
                        or context != start - features.SOURCE_CONTEXT_SECONDS or timestamp > last):
                    raise AnomalyError("Chronologie ou contexte invalide.")
                split = raw["split"]
                split_start, split_end = by_split[split]
                if context < split_start or timestamp >= split_end:
                    raise AnomalyError("Une fenetre traverse une frontiere de partition.")
                segment = int(raw["resource_segment_id"])
                if segment <= 0 or str(segment) != raw["resource_segment_id"] or segment < previous_segment:
                    raise AnomalyError("Identifiant de segment invalide.")
                if (segment == previous_segment and split == previous_split
                        and timestamp - previous_time != step):
                    raise AnomalyError("Interruption de grille dans un segment declare complet.")
                previous_time, previous_split, previous_segment = timestamp, split, segment
                row = {"evaluated_at": exporter.utc_iso(timestamp), "split": split, "resource_segment_id": segment}
                for name in features.FEATURE_NAMES:
                    value = float(raw[name])
                    stat = name.split("__")[1]
                    if (not math.isfinite(value)
                            or (stat in ("current", "mean", "min", "max") and not 0 <= value <= 100)
                            or (stat == "std" and not 0 <= value <= 50.000001)
                            or (stat == "delta" and not -100 <= value <= 100)):
                        raise AnomalyError("Variable absente, non finie ou hors domaine.")
                    row[name] = value
                for name in exporter.RESOURCE_METRICS:
                    minimum, maximum = row[f"{name}__min"], row[f"{name}__max"]
                    if (not minimum <= row[f"{name}__current"] <= maximum
                            or not minimum <= row[f"{name}__mean"] <= maximum):
                        raise AnomalyError("Statistiques de fenetre incoherentes.")
                rows.append(row)
        counts = {name: sum(row["split"] == name for row in rows) for name in features.SPLITS}
        summary = metadata["summary"]
        if (type(manifest["rows"]) is not int or manifest["rows"] != len(rows)
                or summary["feature_rows"] != len(rows) or summary["rows_by_split"] != counts
                or summary["feature_count"] != len(features.FEATURE_NAMES)
                or exporter.file_sha256(path) != manifest["sha256"]
                or metadata_path.read_bytes() != raw_metadata):
            raise AnomalyError("Entrees modifiees ou bilan de variables incoherent.")
        for name, minimum in MIN_ROWS.items():
            if counts[name] < minimum:
                raise AnomalyError(f"Au moins {minimum} lignes {name} necessaires ; {counts[name]} disponibles.")
        return Inputs(directory.resolve(), metadata, hashlib.sha256(raw_metadata).hexdigest(), rows)
    except (OSError, ValueError, TypeError, KeyError, AttributeError, csv.Error) as exc:
        raise AnomalyError("Variables d'apprentissage illisibles ou invalides.") from exc


def operational_reasons(row: dict) -> list[str]:
    reasons = []
    for name, threshold in RULE_THRESHOLDS.items():
        value = row[f"{name}__current"]
        triggered = value <= threshold if name == "disk_available_percent" else value >= threshold
        if triggered:
            reasons.append(name)
    return reasons


def fit_and_score(inputs: Inputs) -> tuple[dict, dict, list[dict]]:
    train = [row for row in inputs.rows if row["split"] == "train"]
    train_matrix = np.asarray([[row[name] for name in features.FEATURE_NAMES] for row in train], dtype=float)
    all_matrix = np.asarray([[row[name] for name in features.FEATURE_NAMES] for row in inputs.rows], dtype=float)
    forest = IsolationForest(n_estimators=100, max_samples=min(256, len(train)),
                             contamination="auto", random_state=42, n_jobs=1)
    with threadpool_limits(limits=1):
        forest.fit(train_matrix)
        train_scores = -forest.score_samples(train_matrix)
        threshold = float(np.quantile(train_scores, TRAIN_QUANTILE, method="higher"))
        expected = -forest.score_samples(all_matrix)
    model = numeric.export_forest(forest, list(features.FEATURE_NAMES), threshold)
    replayed = np.asarray(numeric.score_rows(model, all_matrix.tolist()))
    maximum_difference = float(np.max(np.abs(replayed - expected)))
    if maximum_difference > 1e-10:
        raise AnomalyError("Les arbres JSON ne reproduisent pas les scores Scikit-learn.")
    # Calibrer avec le meme moteur de score que celui qui sera deploye :
    # un ecart d'arrondi ne doit pas signaler toutes les donnees constantes.
    train_mask = np.asarray([row["split"] == "train" for row in inputs.rows])
    threshold = float(np.quantile(replayed[train_mask], TRAIN_QUANTILE, method="higher"))
    model["anomaly_threshold"] = threshold
    records = []
    for row, score in zip(inputs.rows, replayed):
        reasons = operational_reasons(row)
        records.append({
            "evaluated_at": row["evaluated_at"], "split": row["split"],
            "resource_segment_id": row["resource_segment_id"],
            **{name: row[f"{name}__current"] for name in exporter.RESOURCE_METRICS},
            "anomaly_score": float(score), "anomaly_threshold": threshold,
            "statistical_anomaly": int(score > threshold),
            "operational_rule_alert": int(bool(reasons)),
            "operational_rule_reasons": "|".join(reasons),
        })
    by_split = {}
    for split in features.SPLITS:
        subset = [r for r in records if r["split"] == split]
        atypical = sum(r["statistical_anomaly"] for r in subset)
        by_split[split] = {
            "rows": len(subset), "statistical_anomalies": atypical,
            "statistical_anomaly_rate_percent": 100 * atypical / len(subset),
            "operational_rule_alerts": sum(r["operational_rule_alert"] for r in subset),
            "both_flags": sum(r["statistical_anomaly"] and r["operational_rule_alert"] for r in subset),
            "score_min": min(r["anomaly_score"] for r in subset),
            "score_median": float(np.median([r["anomaly_score"] for r in subset])),
            "score_max": max(r["anomaly_score"] for r in subset),
        }
    details = {
        "anomaly_threshold": threshold, "by_split": by_split,
        "constant_training_features": [name for index, name in enumerate(features.FEATURE_NAMES)
                                       if np.ptp(train_matrix[:, index]) == 0],
        "json_replay_max_absolute_difference": maximum_difference,
        "detection_quality": {
            "status": "not_evaluated_no_incident_labels",
            "precision": None, "recall": None, "false_positive_rate": None,
        },
    }
    return model, details, records


def perform_training(inputs: Inputs, output_dir: Path) -> tuple[dict, Path, bool]:
    settings = {
        "tool_version": VERSION, "schema_version": 1,
        "source_metadata_sha256": inputs.metadata_sha256,
        "source_input_sha256": inputs.metadata["input_sha256"],
        "source_features_sha256": inputs.metadata["files"]["features.csv"]["sha256"],
        "environment": {"python": platform.python_version(),
                        "packages": {name: package_version(name) for name in
                                     ("numpy", "scipy", "scikit-learn", "joblib", "threadpoolctl")}},
        "forest_parameters": {"n_estimators": 100, "max_samples": min(256, sum(r["split"] == "train" for r in inputs.rows)),
                              "random_state": 42, "threads": 1, "max_features": 1.0, "bootstrap": False},
        "threshold_calibration": {"partition": "train", "quantile": TRAIN_QUANTILE, "method": "higher", "comparison": "strictly_greater"},
        "minimum_rows": MIN_ROWS, "operational_rule_thresholds": RULE_THRESHOLDS,
    }
    fingerprint = hashlib.sha256(json.dumps(settings, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    end = inputs.metadata["source_range"]["end"].replace("-", "").replace(":", "")
    directory = output_dir.resolve() / f"moodle_anomalies_{end}_{fingerprint[:12]}"
    if directory.exists():
        try:
            existing = features.strict_json((directory / "report.json").read_bytes())
            if (existing["input_sha256"] != fingerprint or existing["status"] != "trained_experimental"
                    or any(existing[key] != value for key, value in settings.items())
                    or set(existing["files"]) != {"model.json", "scores.csv"}):
                raise AnomalyError("Resultats existants incoherents.")
            for name in existing["files"]:
                if exporter.file_sha256(directory / name) != existing["files"][name]["sha256"]:
                    raise AnomalyError("Resultats existants corrompus ; ils ne seront pas ecrases.")
            numeric.load_model(directory / "model.json", list(features.FEATURE_NAMES))
            return existing, directory, True
        except (OSError, ValueError, TypeError, KeyError) as exc:
            raise AnomalyError("Dossier d'experience existant incomplet ou invalide.") from exc
    model, details, records = fit_and_score(inputs)
    report = {
        **settings, **details, "input_sha256": fingerprint,
        "created_at": exporter.utc_iso(time.time()), "status": "trained_experimental",
        "source_features_directory": inputs.directory.name,
        "source_range": inputs.metadata["source_range"], "split_periods": inputs.metadata["split_periods"],
        "semantics": {
            "score": "Oppose de score_samples de Scikit-learn ; score eleve = observation atypique, pas probabilite de panne.",
            "fit": "Arbres et seuil appris sur train uniquement ; validation et test ne modifient pas le modele.",
            "threshold": "Quantile 98% du train, avec comparaison stricte ; budget experimental de signalement, pas taux de fausses alertes.",
            "training_data": "Les lignes completes ne sont pas presumees normales ; incidents non annotes possiblement presents.",
            "baseline": "Seuils operationnels illustratifs a valider avec BeeCoders ; nombres de signalements compares, pas exactitude.",
            "labels": "Precision, rappel et faux positifs non mesurables sans etiquettes d'incidents independantes.",
            "constant_features": "Variables constantes identifiees dans le rapport ; la representativite devra etre enrichie.",
            "mode": "Observation experimentale ; un score seul n'autorise aucune intervention.",
            "model_json": "Arbres numeriques rejoues et compares aux scores du modele entraine.",
            "scope": "Ressources serveur CPU, RAM et disque ; ne qualifie pas seul la disponibilite ni la cause d'une panne Moodle.",
        },
    }
    directory.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".anomalies-", dir=directory.parent))
    try:
        (temporary / "model.json").write_text(json.dumps(model, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")
        with (temporary / "scores.csv").open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=SCORE_FIELDS)
            writer.writeheader()
            writer.writerows(records)
        report["files"] = {name: {"sha256": exporter.file_sha256(temporary / name)}
                           for name in ("model.json", "scores.csv")}
        report["files"]["scores.csv"]["rows"] = len(records)
        (temporary / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")
        os.rename(temporary, directory)
        return report, directory, False
    except FileExistsError as exc:
        raise AnomalyError("Une autre execution a deja publie cette experience.") from exc
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


def compact_summary(report: dict, directory: Path, reused: bool) -> dict:
    return {
        "status": "already_trained_experimental" if reused else report["status"],
        "report_file": str(directory / "report.json"), "model_file": str(directory / "model.json"),
        "scores_file": str(directory / "scores.csv"),
        "anomaly_threshold": report["anomaly_threshold"],
        "rows_by_split": {name: values["rows"] for name, values in report["by_split"].items()},
        "by_split": {name: {key: value for key, value in values.items() if not key.startswith("score_")}
                     for name, values in report["by_split"].items()},
        "constant_training_feature_count": len(report["constant_training_features"]),
        "detection_quality": report["detection_quality"],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--features-dir", required=True, type=Path)
    parser.add_argument("--output-dir", type=Path, default=Path(__file__).resolve().parent / "data/models")
    arguments = parser.parse_args(argv)
    try:
        inputs = read_inputs(arguments.features_dir)
        print("Entrainement Isolation Forest : train uniquement ; observation des trois partitions.", file=sys.stderr, flush=True)
        report, directory, reused = perform_training(inputs, arguments.output_dir)
        print(json.dumps(compact_summary(report, directory, reused), indent=2, ensure_ascii=False, allow_nan=False))
        return 0
    except (AnomalyError, numeric.ModelError, OSError, ValueError) as exc:
        print(f"Entrainement interrompu : {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
