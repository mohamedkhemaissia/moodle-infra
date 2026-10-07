"""Entrainer des regressions experimentales a 5, 10 et 15 minutes pour Moodle.

Apprentissage et normalisation sur train ; choix du reglage sur validation ;
test reserve a l'evaluation. Comparaison avec la derniere valeur mesuree.
Python 3.10+, dependances dans ml/requirements.txt.
"""

from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
from datetime import datetime, timezone
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
from typing import Any

import numpy as np
from sklearn.linear_model import Ridge
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler
from threadpoolctl import threadpool_limits

from ml import export_metrics as exporter
from ml import prepare_dataset as preparation
from ml import prepare_features as features


VERSION = "0.1.0"
SCHEMA_VERSION = 1
ALPHAS = (0.1, 1.0, 10.0, 100.0)
MIN_ROWS = {"train": 20, "validation": 10, "test": 10}
METRICS = exporter.RESOURCE_METRICS
PREDICTION_FIELDS = (
    "horizon_minutes", "split", "evaluated_at", "target_evaluated_at", "resource_segment_id",
    "metric", "actual", "raw_ridge", "ridge", "persistence", "preferred_on_validation", "preferred_prediction",
)
TrainingError = exporter.ExportError


@dataclass
class Inputs:
    directory: Path
    metadata: dict[str, Any]
    metadata_sha256: str
    forecasts: dict[int, list[dict[str, Any]]]


def finite_number(value: Any) -> float:
    if isinstance(value, bool):
        raise TrainingError("Valeur numerique invalide.")
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise TrainingError("Valeur numerique absente ou invalide.") from exc
    if not math.isfinite(parsed):
        raise TrainingError("Valeur numerique non finie.")
    return parsed


def read_inputs(directory: Path) -> Inputs:
    """Verifier les empreintes, les schemas et la separation temporelle avant fit."""
    try:
        metadata_bytes = (directory / "metadata.json").read_bytes()
        metadata = features.strict_json(metadata_bytes)
        keys = ("tool_version", "schema_version", "source_dataset_sha256", "source_metadata_sha256",
                "step_seconds", "lookback_seconds", "window_points_including_endpoints", "source_context_seconds",
                "horizons_minutes", "feature_names", "target_names", "split_policy")
        settings = {key: metadata[key] for key in keys}
        fingerprint = hashlib.sha256(json.dumps(settings, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        if (type(metadata["schema_version"]) is not int or metadata["schema_version"] != features.SCHEMA_VERSION
                or fingerprint != metadata["input_sha256"] or metadata["feature_names"] != list(features.FEATURE_NAMES)
                or metadata["target_names"] != list(features.TARGET_NAMES)
                or metadata["horizons_minutes"] != list(features.HORIZONS_MINUTES)
                or metadata["source_context_seconds"] != features.SOURCE_CONTEXT_SECONDS):
            raise TrainingError("Configuration des variables incoherente ou non prise en charge.")
        step, lookback = metadata["step_seconds"], metadata["lookback_seconds"]
        if type(step) is not int or step < 15 or type(lookback) is not int or lookback % 60:
            raise TrainingError("Pas temporel ou historique invalide.")
        points = features.validate_settings(step, lookback // 60)
        if metadata["window_points_including_endpoints"] != points:
            raise TrainingError("Taille de fenetre incoherente.")
        bounds = metadata["source_range"]
        source_start, source_end = preparation.parse_utc(bounds["start"]), preparation.parse_utc(bounds["end"])
        if bounds["step_seconds"] != step or source_start > source_end or source_start % step or source_end % step:
            raise TrainingError("Periode source invalide.")
        periods = metadata["split_periods"]
        if not isinstance(periods, list) or [period["split"] for period in periods] != list(features.SPLITS):
            raise TrainingError("Les trois partitions temporelles sont necessaires.")
        period_by_name = {}
        previous_end = None
        for period in periods:
            start, end = preparation.parse_utc(period["start"]), preparation.parse_utc(period["end_exclusive"])
            if (start > end or start % step or end % step or start < source_start or end > source_end + step
                    or (previous_end is not None and start != previous_end)):
                raise TrainingError("Partitions temporelles desordonnees ou chevauchantes.")
            period_by_name[period["split"]] = (start, end)
            previous_end = end
        expected_files = {"features.csv", *(f"forecast_{minutes}m.csv" for minutes in features.HORIZONS_MINUTES)}
        if set(metadata["files"]) != expected_files:
            raise TrainingError("Manifest des fichiers incomplet.")

        def read_table(filename, horizon=0):
            path = directory / filename
            manifest = metadata["files"][filename]
            if exporter.file_sha256(path) != manifest["sha256"]:
                raise TrainingError(f"Empreinte invalide : {filename}.")
            expected_fields = features.FORECAST_FIELDS if horizon else features.FEATURE_FIELDS
            rows, previous_time = [], -1
            with path.open(encoding="utf-8", newline="") as stream:
                reader = csv.DictReader(stream)
                if reader.fieldnames != list(expected_fields):
                    raise TrainingError(f"Schema CSV invalide : {filename}.")
                for index, raw in enumerate(reader):
                    if index >= features.MAX_INPUT_ROWS or any(not isinstance(value, str) for value in raw.values()):
                        raise TrainingError("CSV trop grand ou ligne incomplete.")
                    timestamp = preparation.parse_utc(raw["evaluated_at"])
                    window_start = preparation.parse_utc(raw["window_start"])
                    context_start = preparation.parse_utc(raw["input_context_start"])
                    if (timestamp <= previous_time or timestamp % step or window_start != timestamp - lookback
                            or context_start != window_start - features.SOURCE_CONTEXT_SECONDS
                            or context_start < source_start or timestamp > source_end):
                        raise TrainingError("Chronologie ou contexte de fenetre invalide.")
                    previous_time = timestamp
                    split = raw["split"]
                    if split not in period_by_name:
                        raise TrainingError("Partition inconnue.")
                    start, end = period_by_name[split]
                    if context_start < start or timestamp >= end:
                        raise TrainingError("Une fenetre traverse une frontiere de partition.")
                    segment = int(raw["resource_segment_id"])
                    if segment <= 0 or raw["resource_segment_id"] != str(segment):
                        raise TrainingError("Identifiant de segment invalide.")
                    row = {"evaluated_at": exporter.utc_iso(timestamp), "window_start": exporter.utc_iso(window_start),
                           "input_context_start": exporter.utc_iso(context_start), "resource_segment_id": segment, "split": split}
                    for name in features.FEATURE_NAMES:
                        value = finite_number(raw[name])
                        stat = name.split("__")[1]
                        if (stat in ("current", "mean", "min", "max") and not 0 <= value <= 100
                                or stat == "std" and not 0 <= value <= 50.000001
                                or stat == "delta" and not -100 <= value <= 100):
                            raise TrainingError("Variable hors domaine.")
                        row[name] = value
                    for name in METRICS:
                        minimum, maximum = row[f"{name}__min"], row[f"{name}__max"]
                        if not minimum <= row[f"{name}__current"] <= maximum or not minimum <= row[f"{name}__mean"] <= maximum:
                            raise TrainingError("Extremes et moyenne de fenetre incoherents.")
                    if horizon:
                        target_time = preparation.parse_utc(raw["target_evaluated_at"])
                        if target_time != timestamp + horizon * 60 or target_time >= end or target_time > source_end:
                            raise TrainingError("Cible future incorrecte ou hors de sa partition.")
                        row["target_evaluated_at"] = exporter.utc_iso(target_time)
                        for name in features.TARGET_NAMES:
                            value = finite_number(raw[name])
                            if not 0 <= value <= 100:
                                raise TrainingError("Cible hors domaine.")
                            row[name] = value
                    rows.append(row)
            if type(manifest["rows"]) is not int or len(rows) != manifest["rows"]:
                raise TrainingError("Nombre de lignes incoherent avec le manifest.")
            return rows

        feature_rows = read_table("features.csv")
        summary = metadata["summary"]
        by_time = {row["evaluated_at"]: row for row in feature_rows}
        counts = lambda rows: {name: sum(row["split"] == name for row in rows) for name in features.SPLITS}
        if (len(feature_rows) != summary["feature_rows"] or counts(feature_rows) != summary["rows_by_split"]
                or summary["feature_count"] != len(features.FEATURE_NAMES)):
            raise TrainingError("Bilan des variables incoherent.")
        forecasts = {}
        for minutes in features.HORIZONS_MINUTES:
            rows = read_table(f"forecast_{minutes}m.csv", minutes)
            actual_counts = counts(rows)
            if {"total": len(rows), **actual_counts} != summary["forecast_rows"][str(minutes)]:
                raise TrainingError("Bilan des cibles incoherent.")
            for row in rows:
                origin = by_time.get(row["evaluated_at"])
                target = by_time.get(row["target_evaluated_at"])
                if (origin is None or any(row[name] != origin[name] for name in features.FEATURE_FIELDS)
                        or target is None or target["split"] != row["split"]
                        or target["resource_segment_id"] != row["resource_segment_id"]
                        or any(row[f"target__{name}"] != target[f"{name}__current"] for name in METRICS)):
                    raise TrainingError("Entrees et cibles ne correspondent pas aux variables source.")
            for name, minimum in MIN_ROWS.items():
                if actual_counts[name] < minimum:
                    raise TrainingError(f"Horizon {minutes} min : au moins {minimum} lignes {name} necessaires ; {actual_counts[name]} disponibles.")
            forecasts[minutes] = rows
        return Inputs(directory.resolve(), metadata, hashlib.sha256(metadata_bytes).hexdigest(), forecasts)
    except (OSError, ValueError, TypeError, KeyError, AttributeError, csv.Error) as exc:
        raise TrainingError("Fichiers d'apprentissage illisibles ou invalides.") from exc


def matrices(rows: list[dict]) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    return (np.asarray([[row[name] for name in features.FEATURE_NAMES] for row in rows], dtype=float),
            np.asarray([[row[f"target__{name}"] for name in METRICS] for row in rows], dtype=float),
            np.asarray([[row[f"{name}__current"] for name in METRICS] for row in rows], dtype=float))


def error_scores(actual: np.ndarray, predicted: np.ndarray) -> dict[str, dict[str, float]]:
    if actual.shape != predicted.shape or actual.ndim != 2 or actual.shape[1] != len(METRICS) or not len(actual):
        raise TrainingError("Dimensions des predictions invalides.")
    if not np.isfinite(actual).all() or not np.isfinite(predicted).all():
        raise TrainingError("Predictions non finies.")
    errors = predicted - actual
    return {name: {"mae": float(np.mean(np.abs(errors[:, index]))),
                   "rmse": float(np.sqrt(np.mean(errors[:, index] ** 2))),
                   "bias": float(np.mean(errors[:, index]))} for index, name in enumerate(METRICS)}


def predict_record(record: dict, x: np.ndarray, *, clip: bool = True) -> np.ndarray:
    """Rejouer les coefficients JSON ; aucune deserialisation d'objet Python."""
    mean, scale = np.asarray(record["scaler_mean"], dtype=float), np.asarray(record["scaler_scale"], dtype=float)
    coefficients, intercept = np.asarray(record["coefficients"], dtype=float), np.asarray(record["intercept"], dtype=float)
    if (x.ndim != 2 or x.shape[1] != len(features.FEATURE_NAMES) or mean.shape != (x.shape[1],)
            or scale.shape != mean.shape or coefficients.shape != (len(METRICS), x.shape[1])
            or intercept.shape != (len(METRICS),) or np.any(scale <= 0)
            or not all(np.isfinite(item).all() for item in (x, mean, scale, coefficients, intercept))):
        raise TrainingError("Structure ou valeurs du modele JSON invalides.")
    raw = ((x - mean) / scale) @ coefficients.T + intercept
    if not np.isfinite(raw).all():
        raise TrainingError("Prediction JSON non finie.")
    return np.clip(raw, 0, 100) if clip else raw


def train_horizon(rows: list[dict], minutes: int, alphas=ALPHAS) -> tuple[dict, dict, list[dict]]:
    if not alphas or any(isinstance(alpha, bool) or not math.isfinite(alpha) or alpha <= 0 for alpha in alphas):
        raise TrainingError("Les coefficients de regularisation doivent etre positifs et finis.")
    partitions = {name: [row for row in rows if row["split"] == name] for name in features.SPLITS}
    arrays = {name: matrices(items) for name, items in partitions.items()}
    x_train, y_train, _ = arrays["train"]
    x_validation, y_validation, _ = arrays["validation"]
    candidates, best = [], None
    for alpha in alphas:
        pipeline = make_pipeline(StandardScaler(), Ridge(alpha=alpha, solver="svd"))
        pipeline.fit(x_train, y_train)
        raw = pipeline.predict(x_validation)
        if not np.isfinite(raw).all():
            raise TrainingError("Le modele produit une prediction non finie.")
        score = float(np.mean(np.abs(np.clip(raw, 0, 100) - y_validation)))
        candidates.append({"alpha": alpha, "validation_mean_mae": score})
        # Egalite a 12 decimales : privilegier la regularisation la plus forte.
        key = (round(score, 12), -alpha)
        if best is None or key < best[0]:
            best = (key, pipeline, alpha)
    _, pipeline, alpha = best
    scaler, ridge = pipeline.named_steps["standardscaler"], pipeline.named_steps["ridge"]
    record = {"horizon_minutes": minutes, "alpha": alpha,
              "scaler_mean": scaler.mean_.tolist(), "scaler_scale": scaler.scale_.tolist(),
              "coefficients": ridge.coef_.tolist(), "intercept": ridge.intercept_.tolist()}
    evaluations, predictions, preferred = {}, [], {}
    for split in ("validation", "test"):
        x, actual, baseline = arrays[split]
        raw = predict_record(record, x, clip=False)
        model = np.clip(raw, 0, 100)
        model_scores, baseline_scores = error_scores(actual, model), error_scores(actual, baseline)
        comparison = {}
        for index, name in enumerate(METRICS):
            model_mae, baseline_mae = model_scores[name]["mae"], baseline_scores[name]["mae"]
            if split == "validation":
                preferred[name] = "ridge" if model_mae + 1e-9 < baseline_mae else "persistence"
            comparison[name] = {
                "ridge": model_scores[name], "persistence": baseline_scores[name],
                "mae_gain_percent": (1 - model_mae / baseline_mae) * 100 if baseline_mae > 1e-12 else None,
                "preferred_on_validation": preferred[name],
                "clipped_predictions": int(np.sum((raw[:, index] < 0) | (raw[:, index] > 100))),
            }
        evaluations[split] = comparison
        for row_index, row in enumerate(partitions[split]):
            for column, name in enumerate(METRICS):
                predictions.append({"horizon_minutes": minutes, "split": split, "evaluated_at": row["evaluated_at"],
                                    "target_evaluated_at": row["target_evaluated_at"], "resource_segment_id": row["resource_segment_id"],
                                    "metric": name, "actual": float(actual[row_index, column]), "raw_ridge": float(raw[row_index, column]),
                                    "ridge": float(model[row_index, column]), "persistence": float(baseline[row_index, column]),
                                    "preferred_on_validation": preferred[name],
                                    "preferred_prediction": float(model[row_index, column] if preferred[name] == "ridge" else baseline[row_index, column])})
    record["preferred_on_validation"] = preferred
    report = {"rows_by_split": {name: len(items) for name, items in partitions.items()},
              "selected_alpha": alpha, "candidates": candidates, **evaluations}
    return record, report, predictions


def environment() -> dict:
    return {"python": platform.python_version(), "machine": platform.machine(),
            "packages": {name: package_version(name) for name in ("numpy", "scipy", "scikit-learn", "joblib", "threadpoolctl")}}


def perform_training(inputs: Inputs, output_dir: Path, progress=None) -> tuple[dict, Path, bool]:
    settings = {"tool_version": VERSION, "schema_version": SCHEMA_VERSION,
                "source_metadata_sha256": inputs.metadata_sha256, "source_input_sha256": inputs.metadata["input_sha256"],
                "environment": environment(), "alphas": list(ALPHAS), "minimum_rows": MIN_ROWS,
                "solver": "svd", "threads": 1, "clip_predictions_to_percent_domain": True,
                "selection": "validation_mean_mae_all_resources_then_larger_alpha_on_12_decimal_tie"}
    fingerprint = hashlib.sha256(json.dumps(settings, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    end = datetime.fromtimestamp(preparation.parse_utc(inputs.metadata["source_range"]["end"]), timezone.utc)
    directory = output_dir.resolve() / f"moodle_forecasts_{end:%Y%m%dT%H%M%SZ}_{fingerprint[:12]}"
    if directory.exists():
        try:
            existing = features.strict_json((directory / "report.json").read_bytes())
            if (existing["input_sha256"] != fingerprint or any(existing[key] != value for key, value in settings.items())
                    or set(existing["files"]) != {"models.json", "predictions.csv"}):
                raise TrainingError("Les resultats existants sont incoherents.")
            for filename in ("models.json", "predictions.csv"):
                if existing["files"][filename]["sha256"] != exporter.file_sha256(directory / filename):
                    raise TrainingError("Resultats existants corrompus ; ils ne seront pas ecrases.")
            return existing, directory, True
        except (OSError, ValueError, TypeError, KeyError) as exc:
            raise TrainingError("Dossier d'entrainement existant incomplet ou invalide.") from exc
    model_records, horizons, predictions = {}, {}, []
    with threadpool_limits(limits=1):
        for minutes, rows in inputs.forecasts.items():
            if progress:
                progress(f"Entrainement et evaluation : horizon {minutes} minutes")
            record, report, observations = train_horizon(rows, minutes)
            model_records[str(minutes)], horizons[str(minutes)] = record, report
            predictions.extend(observations)
    models = {"schema_version": SCHEMA_VERSION, "model_type": "standard_scaler_then_multioutput_ridge",
              "feature_names": list(features.FEATURE_NAMES), "target_metrics": list(METRICS),
              "clip_range": [0, 100], "input_sha256": fingerprint, "horizons": model_records}
    report = {
        **settings, "input_sha256": fingerprint, "created_at": exporter.utc_iso(time.time()),
        "status": "trained_experimental", "source_features_directory": inputs.directory.name,
        "source_range": inputs.metadata["source_range"], "split_periods": inputs.metadata["split_periods"], "horizons": horizons,
        "semantics": {
            "units": "MAE, RMSE et biais en points de pourcentage de la ressource.",
            "fit": "Normalisation et coefficients appris uniquement sur train ; aucun reajustement sur validation ou test.",
            "selection": "Reglage Ridge sur la MAE moyenne des trois ressources en validation ; test jamais utilise pour choisir.",
            "preferred_predictor": "Choix par ressource sur validation ; persistance en cas d'egalite a 1e-9 point pres.",
            "baseline": "Prediction egale a la derniere valeur mesuree, avec les memes lignes et horizons que Ridge.",
            "gain": "100*(1-MAE_Ridge/MAE_persistance) ; negatif si Ridge fait moins bien ; null si reference presque sans erreur.",
            "clipping": "Predictions bornees a [0,100] avant selection et evaluation ; valeurs brutes conservees dans predictions.csv.",
            "target": "Valeur a l'instant t+h ; CPU deja moyenne sur deux minutes ; disque = pourcentage disponible.",
            "models_json": "Coefficients, moyennes et echelles numeriques ; predictions rejouables sans charger un objet pickle.",
            "scope": "Premiere experience sur les ressources de cette VM ; ne demontre pas une capacite de prediction des pannes ou de generalisation.",
            "minimum_rows": "Seuils pour executer l'experience ; aucune garantie statistique. Les fenetres temporelles restent correlees.",
        },
    }
    directory.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".forecasts-", dir=directory.parent))
    try:
        (temporary / "models.json").write_text(json.dumps(models, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")
        with (temporary / "predictions.csv").open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=PREDICTION_FIELDS)
            writer.writeheader()
            writer.writerows(predictions)
        report["files"] = {name: {"sha256": exporter.file_sha256(temporary / name)} for name in ("models.json", "predictions.csv")}
        report["files"]["predictions.csv"]["rows"] = len(predictions)
        (temporary / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")
        os.rename(temporary, directory)
        return report, directory, False
    except FileExistsError as exc:
        raise TrainingError("Un autre processus a deja publie cette experience.") from exc
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


def compact_summary(report: dict, directory: Path, reused: bool) -> dict:
    return {"status": "already_trained_experimental" if reused else report["status"],
            "report_file": str(directory / "report.json"), "models_file": str(directory / "models.json"),
            "predictions_file": str(directory / "predictions.csv"),
            "horizons": {minutes: {"rows_by_split": horizon["rows_by_split"], "selected_alpha": horizon["selected_alpha"],
                                   "test": {name: {"mae_ridge": round(values["ridge"]["mae"], 6),
                                                   "mae_persistence": round(values["persistence"]["mae"], 6),
                                                   "gain_percent": round(values["mae_gain_percent"], 3) if values["mae_gain_percent"] is not None else None,
                                                   "preferred_on_validation": values["preferred_on_validation"]}
                                            for name, values in horizon["test"].items()}}
                         for minutes, horizon in report["horizons"].items()}}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--features-dir", required=True, type=Path)
    parser.add_argument("--output-dir", type=Path, default=Path(__file__).resolve().parent / "data/models")
    arguments = parser.parse_args(argv)
    try:
        inputs = read_inputs(arguments.features_dir)
        report, directory, reused = perform_training(
            inputs, arguments.output_dir, progress=lambda message: print(message, file=sys.stderr, flush=True)
        )
        print(json.dumps(compact_summary(report, directory, reused), indent=2, ensure_ascii=False, allow_nan=False))
        return 0
    except (TrainingError, OSError, ValueError) as exc:
        print(f"Entrainement interrompu : {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
