"""Preparer les variables temporelles utilisees par Isolation Forest.

Python 3.10+, bibliotheque standard uniquement ; aucun modele n'est entraine.
Execution : python3 -B -m ml.prepare_features --dataset-dir CHEMIN_DU_DATASET
"""

from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time
from typing import Any

from ml import export_metrics as exporter
from ml import prepare_dataset as preparation


VERSION = "0.2.0"
SCHEMA_VERSION = 2
MAX_INPUT_ROWS = 50_000
SOURCE_CONTEXT_SECONDS = 120  # La requete CPU contient deja rate(...[2m]).
STATISTICS = ("current", "mean", "std", "min", "max", "delta", "slope_per_minute")
FEATURE_NAMES = tuple(f"{name}__{stat}" for name in exporter.RESOURCE_METRICS for stat in STATISTICS)
CONTEXT_FIELDS = ("evaluated_at", "window_start", "input_context_start", "resource_segment_id", "split")
FEATURE_FIELDS = (*CONTEXT_FIELDS, *FEATURE_NAMES)
SPLITS = ("train", "validation", "test")
FeatureError = exporter.ExportError


@dataclass
class Dataset:
    directory: Path
    metadata: dict[str, Any]
    metadata_sha256: str
    rows: list[dict[str, Any]]
    window: exporter.Window


def strict_json(value: str | bytes) -> dict[str, Any]:
    def reject_constant(constant):
        raise FeatureError(f"Constante JSON non finie : {constant}.")

    def unique_keys(pairs):
        result = {}
        for key, item in pairs:
            if key in result:
                raise FeatureError("Cle JSON dupliquee dans les metadonnees.")
            result[key] = item
        return result

    parsed = json.loads(value, parse_constant=reject_constant, object_pairs_hook=unique_keys)
    if not isinstance(parsed, dict):
        raise FeatureError("Les metadonnees doivent etre un objet JSON.")
    return parsed


def read_dataset(directory: Path) -> Dataset:
    """Verifier l'integrite, la grille, les domaines et les segments declares."""
    try:
        metadata_bytes = (directory / "metadata.json").read_bytes()
        metadata = strict_json(metadata_bytes)
        if metadata["schema_version"] != preparation.SCHEMA_VERSION:
            raise FeatureError("Version du dataset non prise en charge.")
        bounds = metadata["range"]
        start, end = preparation.parse_utc(bounds["start"]), preparation.parse_utc(bounds["end"])
        step = bounds["step_seconds"]
        if (type(step) is not int or step < 15 or not start.is_integer() or not end.is_integer()
                or start > end or start % step or end % step):
            raise FeatureError("Grille du dataset invalide.")
        window = exporter.Window(int(start), int(end), step)
        if window.count > MAX_INPUT_ROWS:
            raise FeatureError(f"Dataset trop grand ; limiter l'entree a {MAX_INPUT_ROWS} lignes.")
        csv_path = directory / "dataset.csv"
        if exporter.file_sha256(csv_path) != metadata["dataset_sha256"]:
            raise FeatureError("L'empreinte du dataset.csv ne correspond pas aux metadonnees.")

        sources = metadata["sources"]
        if not isinstance(sources, list) or not sources:
            raise FeatureError("Manifest des sources absent.")
        source_by_id = {source["source_export_id"]: source for source in sources}
        if len(source_by_id) != len(sources):
            raise FeatureError("Identifiants de sources dupliques.")
        description = {key: metadata[key] for key in (
            "tool_version", "schema_version", "configuration_sha256", "step_seconds",
            "source_metadata_sha256", "identical_sources_skipped", "selection_policy",
        )}
        fingerprint = hashlib.sha256(json.dumps(description, sort_keys=True).encode()).hexdigest()
        if (fingerprint != metadata["input_sha256"] or description["step_seconds"] != step
                or sorted(source["metadata_sha256"] for source in sources) != description["source_metadata_sha256"]):
            raise FeatureError("Manifest ou empreinte d'entree incoherente.")
        for source in sources:
            if source["source_export_id"] != source["metadata_sha256"][:16]:
                raise FeatureError("Identifiant de provenance incoherent.")
            preparation.parse_utc(source["created_at"])

        rows, declared_segments = [], []
        with csv_path.open(encoding="utf-8", newline="") as stream:
            reader = csv.DictReader(stream)
            if reader.fieldnames != list(preparation.FIELDS):
                raise FeatureError("Colonnes du dataset non prises en charge.")
            for index, raw in enumerate(reader):
                if index >= window.count or any(not isinstance(item, str) for item in raw.values()):
                    raise FeatureError("Nombre de lignes ou structure du CSV invalide.")
                timestamp = window.start + index * step
                if preparation.parse_utc(raw["evaluated_at"]) != timestamp:
                    raise FeatureError("Horodatages dupliques, desordonnes ou hors grille.")
                row: dict[str, Any] = {"evaluated_at": exporter.utc_iso(timestamp)}
                for name in exporter.METRIC_QUERIES:
                    parsed = preparation.number(raw[name])
                    validated = exporter.metric_value(name, parsed)
                    if parsed is not None and validated != parsed:
                        raise FeatureError(f"Valeur de mesure invalide : {name}.")
                    row[name] = validated
                for job in exporter.JOBS:
                    status = raw[f"{job}_collection_status"]
                    age = preparation.number(raw[f"{job}_scrape_age_seconds"])
                    if status not in ("up", "down", "stale", "missing") or (status != "missing" and age is None):
                        raise FeatureError("Qualite de collecte invalide.")
                    row[f"{job}_collection_status"], row[f"{job}_scrape_age_seconds"] = status, age
                    if status != "up" and any(row[name] is not None for name in exporter.METRIC_QUERIES
                                              if name.startswith("moodle_") == (job == "moodle_https")):
                        raise FeatureError("Une mesure depend d'une collecte non exploitable.")
                for flag, names in (("resource_metrics_complete", exporter.RESOURCE_METRICS),
                                    ("all_metrics_complete", exporter.METRIC_QUERIES)):
                    complete = int(all(row[name] is not None for name in names))
                    if raw[flag] != str(complete):
                        raise FeatureError("Indicateur de completude incoherent.")
                    row[flag] = complete
                declared_segments.append((raw["resource_segment_id"], raw["all_metrics_segment_id"]))
                identifier = raw["source_export_id"]
                if identifier:
                    source = source_by_id.get(identifier)
                    if source is None or raw["source_created_at"] != source["created_at"]:
                        raise FeatureError("Provenance d'une ligne inconnue ou incoherente.")
                    if not preparation.parse_utc(source["range"]["start"]) <= timestamp <= preparation.parse_utc(source["range"]["end"]):
                        raise FeatureError("Ligne hors de la periode de sa source.")
                elif (raw["source_created_at"] or any(row[name] is not None for name in exporter.METRIC_QUERIES)
                      or any(row[f"{job}_collection_status"] != "missing" or row[f"{job}_scrape_age_seconds"] is not None
                             for job in exporter.JOBS)):
                    raise FeatureError("Une ligne observee est sans provenance.")
                rows.append(row)
        if len(rows) != window.count:
            raise FeatureError("Lignes du dataset manquantes.")
        segments = preparation.assign_segments(rows)
        for row, declared in zip(rows, declared_segments):
            expected = tuple(str(row[name]) if row[name] is not None else ""
                             for name in ("resource_segment_id", "all_metrics_segment_id"))
            if declared != expected:
                raise FeatureError("Segments incoherents avec les interruptions de mesures.")
        summary = metadata["summary"]
        for name, expected in {**exporter.summarize(rows), **segments, "sources_verified": len(sources)}.items():
            if summary[name] != expected:
                raise FeatureError("Bilan du dataset incoherent.")
        return Dataset(directory.resolve(), metadata, hashlib.sha256(metadata_bytes).hexdigest(), rows, window)
    except (OSError, ValueError, TypeError, KeyError, AttributeError, csv.Error) as exc:
        raise FeatureError("Dataset illisible ou invalide.") from exc


def validate_settings(step: int, lookback_minutes: int) -> int:
    if type(lookback_minutes) is not int or not 1 <= lookback_minutes <= 60:
        raise FeatureError("La fenetre doit durer de 1 a 60 minutes entieres.")
    lookback_seconds = lookback_minutes * 60
    if lookback_seconds % step or lookback_seconds < step:
        raise FeatureError("La fenetre doit etre un multiple du pas temporel.")
    return lookback_seconds // step + 1


def split_periods(dataset: Dataset) -> list[dict[str, Any]]:
    positions = [index for index, row in enumerate(dataset.rows) if row["resource_metrics_complete"]]
    if not positions:
        return []
    first, last = positions[0], positions[-1]
    count = last - first + 1
    # Les ratios portent sur la duree observee, avant exclusion des fenetres.
    cuts = (first, first + count * 70 // 100, first + count * 85 // 100, last + 1)
    return [{"split": name, "start": dataset.window.start + cuts[index] * dataset.window.step,
             "end_exclusive": dataset.window.start + cuts[index + 1] * dataset.window.step}
            for index, name in enumerate(SPLITS)]


def window_statistics(values: list[float], step: int) -> dict[str, float]:
    count = len(values)
    average = math.fsum(values) / count
    variance = math.fsum((value - average) ** 2 for value in values) / count
    center = (count - 1) / 2
    denominator = math.fsum(((index - center) * step / 60) ** 2 for index in range(count))
    slope = math.fsum((index - center) * step / 60 * (value - average)
                      for index, value in enumerate(values)) / denominator
    return {name: round(value, 6) for name, value in zip(STATISTICS, (
        values[-1], average, math.sqrt(variance), min(values), max(values),
        values[-1] - values[0], slope,
    ))}


def build_tables(dataset: Dataset, lookback_minutes: int = 5) -> tuple[dict[str, list[dict]], dict[str, Any]]:
    step = dataset.window.step
    points = validate_settings(step, lookback_minutes)
    lookback_seconds = lookback_minutes * 60
    periods = split_periods(dataset)
    tables: dict[str, list[dict]] = {"features.csv": []}
    skipped = {"incomplete_resources": 0, "insufficient_history": 0, "split_boundary": 0}
    segment_start = 0
    previous_segment = None
    for index, row in enumerate(dataset.rows):
        segment = row["resource_segment_id"]
        if segment is None:
            skipped["incomplete_resources"] += 1
            previous_segment = None
            continue
        if segment != previous_segment:
            segment_start = index
        previous_segment = segment
        timestamp = dataset.window.start + index * step
        start_index = index - points + 1
        context_start = timestamp - lookback_seconds - SOURCE_CONTEXT_SECONDS
        if context_start < dataset.window.start + segment_start * step:
            skipped["insufficient_history"] += 1
            continue
        period = next(period for period in periods if period["start"] <= timestamp < period["end_exclusive"])
        if context_start < period["start"]:
            skipped["split_boundary"] += 1
            continue
        feature = {
            "evaluated_at": row["evaluated_at"],
            "window_start": exporter.utc_iso(timestamp - lookback_seconds),
            "input_context_start": exporter.utc_iso(context_start),
            "resource_segment_id": segment, "split": period["split"],
        }
        window_rows = dataset.rows[start_index:index + 1]
        for name in exporter.RESOURCE_METRICS:
            statistics = window_statistics([item[name] for item in window_rows], step)
            feature.update({f"{name}__{stat}": value for stat, value in statistics.items()})
        tables["features.csv"].append(feature)
    counts = {filename: {name: sum(row["split"] == name for row in rows) for name in SPLITS}
              for filename, rows in tables.items()}
    summary = {
        "input_grid_rows": len(dataset.rows),
        "input_resource_complete_rows": dataset.metadata["summary"]["resource_complete_rows"],
        "feature_count": len(FEATURE_NAMES), "feature_rows": len(tables["features.csv"]),
        "rows_by_split": counts["features.csv"], "skipped_feature_rows": skipped,
    }
    settings = {
        "tool_version": VERSION, "schema_version": SCHEMA_VERSION,
        "source_dataset_sha256": dataset.metadata["dataset_sha256"],
        "source_metadata_sha256": dataset.metadata_sha256,
        "step_seconds": step, "lookback_seconds": lookback_seconds,
        "window_points_including_endpoints": points, "source_context_seconds": SOURCE_CONTEXT_SECONDS,
        "feature_names": list(FEATURE_NAMES),
        "split_policy": "chronological_duration_70_15_15_with_disjoint_contexts",
    }
    fingerprint = hashlib.sha256(json.dumps(settings, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    metadata = {
        **settings, "input_sha256": fingerprint, "created_at": exporter.utc_iso(time.time()),
        "source_dataset_directory": dataset.directory.name, "source_range": dataset.metadata["range"],
        "split_periods": [{"split": period["split"], "start": exporter.utc_iso(period["start"]),
                           "end_exclusive": exporter.utc_iso(period["end_exclusive"])} for period in periods],
        "summary": summary,
        "semantics": {
            "features": "Ressources CPU, RAM et disque ; fenetre passee inclusive, aucune valeur future.",
            "std": "Ecart-type de population ; pas une estimation d'ecart-type d'echantillon.",
            "delta": "Derniere valeur moins premiere valeur ; points de pourcentage.",
            "slope_per_minute": "Pente par moindres carres ; points de pourcentage par minute.",
            "source_context": "Deux minutes reservees avant la fenetre car CPU est deja une moyenne rate[2m].",
            "gaps": "Historique dans le meme segment complet ; aucune interpolation.",
            "splits": "70/15/15 de la duree entre premiere et derniere ligne complete, avant exclusions ; nombres finaux de lignes variables.",
            "boundaries": "Contexte des entrees contenu dans une meme partition ; les fenetres chevauchantes sont exclues.",
            "training": "Aucun modele, normalisation apprise ou selection de variables ; apprendre ces traitements uniquement sur train.",
            "labels": "Aucune etiquette d'anomalie ; completude des ressources ne signifie pas fonctionnement normal.",
            "scope": "Cette version prepare uniquement les ressources serveur ; la sonde Moodle reste dans le dataset source.",
            "evaluation": "Des partitions non vides ne prouvent pas la representativite des donnees ni la qualite d'un modele.",
        },
    }
    return tables, metadata


def publish_features(tables: dict[str, list[dict]], metadata: dict[str, Any], output_dir: Path) -> tuple[dict, Path, bool]:
    end = datetime.fromtimestamp(preparation.parse_utc(metadata["source_range"]["end"]), timezone.utc)
    directory = output_dir.resolve() / f"moodle_features_{end:%Y%m%dT%H%M%SZ}_{metadata['input_sha256'][:12]}"
    if directory.exists():
        try:
            existing = strict_json((directory / "metadata.json").read_bytes())
            if (existing["input_sha256"] != metadata["input_sha256"] or existing["summary"] != metadata["summary"]
                    or set(existing["files"]) != set(tables)):
                raise FeatureError("Le dossier de variables existant est incoherent.")
            for filename, rows in tables.items():
                if (existing["files"][filename]["sha256"] != exporter.file_sha256(directory / filename)
                        or existing["files"][filename]["rows"] != len(rows)):
                    raise FeatureError("Un fichier existant est corrompu ; il ne sera pas ecrase.")
            return existing, directory, True
        except (OSError, ValueError, TypeError, KeyError) as exc:
            raise FeatureError("Le dossier de variables existant est incomplet ou invalide.") from exc
    directory.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".features-", dir=directory.parent))
    try:
        files = {}
        for filename, rows in tables.items():
            path = temporary / filename
            with path.open("w", encoding="utf-8", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=FEATURE_FIELDS)
                writer.writeheader()
                writer.writerows(rows)
            files[filename] = {"rows": len(rows), "sha256": exporter.file_sha256(path)}
        published = {**metadata, "files": files}
        (temporary / "metadata.json").write_text(json.dumps(published, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")
        os.rename(temporary, directory)
        return published, directory, False
    except FileExistsError as exc:
        raise FeatureError("Un autre processus a deja publie ces variables.") from exc
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset-dir", required=True, type=Path, help="Dossier contenant dataset.csv et metadata.json.")
    parser.add_argument("--lookback-minutes", type=int, default=5)
    parser.add_argument("--output-dir", type=Path, default=Path(__file__).resolve().parent / "data/features")
    arguments = parser.parse_args(argv)
    try:
        dataset = read_dataset(arguments.dataset_dir)
        tables, metadata = build_tables(dataset, arguments.lookback_minutes)
        metadata, directory, reused = publish_features(tables, metadata, arguments.output_dir)
        summary = {"status": "already_prepared" if reused else "prepared",
                   "output_directory": str(directory), "metadata_file": str(directory / "metadata.json"),
                   **metadata["summary"]}
        if summary["feature_rows"] == 0:
            summary["status"] = "no_usable_feature_windows"
        print(json.dumps(summary, indent=2, ensure_ascii=False, allow_nan=False))
        return 0 if summary["feature_rows"] > 0 else 3
    except (FeatureError, OSError) as exc:
        print(f"Preparation des variables interrompue : {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
