"""Fusionner les exports Moodle verifies, sans inventer de mesures.

Python 3.10+, bibliotheque standard uniquement.
Execution depuis la racine du depot : python3 -m ml.prepare_dataset --help
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


VERSION = "0.1.0"
SCHEMA_VERSION = 1
MAX_DATASET_ROWS = 300_000
FIELDS = (
    *exporter.CSV_FIELDS,
    "source_export_id",
    "source_created_at",
    "resource_segment_id",
    "all_metrics_segment_id",
)
ExportError = exporter.ExportError


@dataclass
class VerifiedExport:
    metadata: dict[str, Any]
    metadata_sha256: str
    csv_path: Path
    window: exporter.Window
    created_timestamp: float
    rows: list[dict[str, Any]]


def parse_utc(value: Any) -> float:
    if not isinstance(value, str):
        raise ExportError("Horodatage absent ou invalide dans un export.")
    try:
        return exporter.parse_timestamp(value)
    except argparse.ArgumentTypeError as exc:
        raise ExportError("Horodatage sans fuseau ou invalide dans un export.") from exc


def number(value: str) -> float | None:
    if value == "":
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ExportError("Valeur numerique invalide dans le CSV.") from exc
    if not math.isfinite(parsed) or parsed < 0:
        raise ExportError("Valeur numerique negative ou non finie dans le CSV.")
    return parsed


def validate_row(raw: dict[str, str], expected_timestamp: int, max_age: float) -> dict[str, Any]:
    if set(raw) != set(exporter.CSV_FIELDS) or any(not isinstance(value, str) for value in raw.values()):
        raise ExportError("Ligne CSV incomplete ou colonnes inattendues.")
    timestamp = parse_utc(raw["evaluated_at"])
    if timestamp != expected_timestamp:
        raise ExportError("La chronologie du CSV ne correspond pas a sa grille declaree.")
    row: dict[str, Any] = {"evaluated_at": exporter.utc_iso(timestamp)}
    for name in exporter.METRIC_QUERIES:
        parsed = number(raw[name])
        validated = exporter.metric_value(name, parsed)
        if parsed is not None and validated != parsed:
            raise ExportError(f"Valeur hors du domaine attendu : {name}.")
        row[name] = validated
    for job in exporter.JOBS:
        status = raw[f"{job}_collection_status"]
        age = number(raw[f"{job}_scrape_age_seconds"])
        if status not in ("up", "down", "stale", "missing"):
            raise ExportError("Etat de collecte CSV inconnu.")
        # Les ages CSV sont arrondis a trois decimales par l'exporteur.
        if status in ("up", "down") and (age is None or age > max_age + 0.0005):
            raise ExportError("L'age et l'etat de collecte sont incoherents.")
        if status == "stale" and (age is None or age < max_age - 0.0005):
            raise ExportError("L'age et l'etat de collecte sont incoherents.")
        row[f"{job}_collection_status"] = status
        row[f"{job}_scrape_age_seconds"] = age
        names = (name for name in exporter.METRIC_QUERIES if
                 name.startswith("moodle_") == (job == "moodle_https"))
        if status != "up" and any(row[name] is not None for name in names):
            raise ExportError("Une mesure provient d'une collecte non exploitable.")
    expected_flags = {
        "resource_metrics_complete": int(all(row[name] is not None for name in exporter.RESOURCE_METRICS)),
        "all_metrics_complete": int(all(row[name] is not None for name in exporter.METRIC_QUERIES)),
    }
    for name, expected in expected_flags.items():
        if raw[name] != str(expected):
            raise ExportError("Indicateur de completude CSV incoherent.")
        row[name] = expected
    return row


def read_export(metadata_path: Path) -> VerifiedExport:
    try:
        metadata_bytes = metadata_path.read_bytes()
        metadata = json.loads(metadata_bytes)
        if not isinstance(metadata, dict) or not isinstance(metadata.get("prometheus_url"), str):
            raise ExportError("Structure des metadonnees d'export invalide.")
        if metadata["schema_version"] != exporter.SCHEMA_VERSION:
            raise ExportError("Version de schema d'export non prise en charge.")
        max_age = float(metadata["max_scrape_age_seconds"])
        configuration, fingerprint = exporter.export_configuration(metadata["prometheus_url"], max_age)
        if metadata["queries"] != configuration["queries"] or metadata["configuration_sha256"] != fingerprint:
            raise ExportError("Configuration ou requetes d'export incoherentes.")
        bounds = metadata["range"]
        start, end = parse_utc(bounds["start"]), parse_utc(bounds["end"])
        step = bounds["step_seconds"]
        if type(step) is not int or step < 15 or not start.is_integer() or not end.is_integer():
            raise ExportError("Grille temporelle de l'export invalide.")
        if start > end or start % step != 0 or end % step != 0:
            raise ExportError("Grille temporelle de l'export invalide.")
        window = exporter.Window(int(start), int(end), step)
        if window.count > exporter.MAX_POINTS:
            raise ExportError("Nombre de lignes de l'export excessif.")
        created_timestamp = parse_utc(metadata["created_at"])
        csv_path = metadata_path.parent / "metrics.csv"
        if exporter.file_sha256(csv_path) != metadata["csv_sha256"]:
            raise ExportError("L'empreinte du CSV ne correspond pas aux metadonnees.")
        rows = []
        with csv_path.open(encoding="utf-8", newline="") as stream:
            reader = csv.DictReader(stream)
            if reader.fieldnames != list(exporter.CSV_FIELDS):
                raise ExportError("Colonnes CSV non prises en charge.")
            for index, raw in enumerate(reader):
                if index >= window.count:
                    raise ExportError("Le CSV contient trop de lignes.")
                rows.append(validate_row(raw, window.start + index * window.step, max_age))
        if len(rows) != window.count or exporter.summarize(rows) != metadata["summary"]:
            raise ExportError("Le nombre de lignes ou le bilan de qualite de l'export est incoherent.")
        return VerifiedExport(
            metadata, hashlib.sha256(metadata_bytes).hexdigest(), csv_path,
            window, created_timestamp, rows,
        )
    except (OSError, ValueError, TypeError, KeyError, csv.Error) as exc:
        raise ExportError(f"Export illisible ou invalide : {metadata_path.parent.name}.") from exc


def discover_exports(input_dirs: list[Path]) -> list[Path]:
    paths = set()
    for directory in input_dirs:
        if not directory.is_dir():
            raise ExportError(f"Repertoire d'entree absent ou inaccessible : {directory}.")
        paths.update(path.resolve() for path in directory.glob("moodle_metrics_*/metadata.json"))
    if not paths:
        raise ExportError("Aucun export publie trouve dans les repertoires d'entree.")
    return sorted(paths)


def missing_row(timestamp: int) -> dict[str, Any]:
    row = {name: None for name in exporter.CSV_FIELDS}
    row["evaluated_at"] = exporter.utc_iso(timestamp)
    for job in exporter.JOBS:
        row[f"{job}_collection_status"] = "missing"
    row["resource_metrics_complete"] = row["all_metrics_complete"] = 0
    row["source_export_id"] = row["source_created_at"] = None
    return row


def assign_segments(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts = {}
    for flag, column in (
        ("resource_metrics_complete", "resource_segment_id"),
        ("all_metrics_complete", "all_metrics_segment_id"),
    ):
        segment = 0
        previous_complete = False
        for row in rows:
            complete = bool(row[flag])
            if complete and not previous_complete:
                segment += 1
            row[column] = segment if complete else None
            previous_complete = complete
        counts[column.removesuffix("_id") + "s"] = segment
    return counts


def merge_exports(paths: list[Path], progress=None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    selected = {}
    seen_values = {}
    conflicts = set()
    identifiers = set()
    manifest = []
    skipped = 0
    candidates = 0
    configuration = None
    step = None
    first_start = None
    last_end = None
    for index, path in enumerate(paths, start=1):
        if progress:
            progress(f"Validation {index}/{len(paths)} : {path.parent.name}")
        source = read_export(path)
        settings = (source.metadata["configuration_sha256"], source.window.step)
        if configuration is not None and settings != (configuration, step):
            raise ExportError("Les exports doivent partager la meme configuration et le meme pas temporel.")
        configuration, step = settings
        if source.metadata_sha256 in identifiers:
            skipped += 1
            continue
        identifiers.add(source.metadata_sha256)
        first_start = source.window.start if first_start is None else min(first_start, source.window.start)
        last_end = source.window.end if last_end is None else max(last_end, source.window.end)
        if (last_end - first_start) // step + 1 > MAX_DATASET_ROWS:
            raise ExportError("Periode fusionnee trop longue ; limiter les exports d'entree.")
        source_id = source.metadata_sha256[:16]
        manifest.append({
            "source_export_id": source_id,
            "export_name": path.parent.name,
            "metadata_sha256": source.metadata_sha256,
            "csv_sha256": source.metadata["csv_sha256"],
            "created_at": source.metadata["created_at"],
            "range": source.metadata["range"],
        })
        candidates += len(source.rows)
        for row_index, row in enumerate(source.rows):
            timestamp = source.window.start + row_index * step
            observed = seen_values.setdefault(timestamp, {})
            known_count = 0
            for name in exporter.METRIC_QUERIES:
                value = row[name]
                if value is not None:
                    known_count += 1
                    if name in observed and observed[name] != value:
                        conflicts.add(timestamp)
                    observed.setdefault(name, value)
            score = (known_count, source.created_timestamp, source.metadata_sha256)
            existing = selected.get(timestamp)
            if existing is None or score > existing[0]:
                selected[timestamp] = (score, {
                    **row, "source_export_id": source_id,
                    "source_created_at": source.metadata["created_at"],
                })
    if not selected or step is None:
        raise ExportError("Aucune ligne source a fusionner.")
    start, end = min(selected), max(selected)
    rows = [selected[timestamp][1] if timestamp in selected else missing_row(timestamp)
            for timestamp in range(start, end + 1, step)]
    summary = {
        **exporter.summarize(rows),
        "sources_verified": len(manifest),
        "identical_sources_skipped": skipped,
        "candidate_rows": candidates,
        "unique_source_rows": len(selected),
        "duplicate_rows_removed": candidates - len(selected),
        "conflicting_timestamps": len(conflicts),
        "uncovered_grid_rows": len(rows) - len(selected),
        **assign_segments(rows),
    }
    input_description = {
        "tool_version": VERSION,
        "schema_version": SCHEMA_VERSION,
        "configuration_sha256": configuration,
        "step_seconds": step,
        "source_metadata_sha256": sorted(identifiers),
        "identical_sources_skipped": skipped,
        "selection_policy": "whole_row_by_known_metric_count_then_creation_time_then_sha256",
    }
    fingerprint = hashlib.sha256(json.dumps(input_description, sort_keys=True).encode()).hexdigest()
    metadata = {
        **input_description,
        "input_sha256": fingerprint,
        "created_at": exporter.utc_iso(time.time()),
        "range": {"start": exporter.utc_iso(start), "end": exporter.utc_iso(end), "step_seconds": step},
        "summary": summary,
        "sources": sorted(manifest, key=lambda source: source["source_export_id"]),
        "semantics": {
            "rows": "Grille UTC reguliere ; cellules vides conservees, sans interpolation ni lissage.",
            "deduplication": "Une ligne entiere par instant ; priorite au nombre de mesures connues, puis a la date de creation la plus recente.",
            "ties": "Si completude et date sont identiques, l'empreinte departage de facon deterministe.",
            "conflicting_timestamps": "Instants ou une meme mesure connue a plusieurs valeurs parmi les exports ; le conflit ne constitue pas une anomalie Moodle.",
            "uncovered_grid_rows": "Instants absents de toutes les fenetres exportees ; mesures inconnues et provenance vide.",
            "segments": "Groupes de lignes consecutives avec mesures completes ; une interruption ouvre un nouveau groupe.",
            "complete_flags": "Completude des mesures, sans jugement sur la sante de Moodle.",
        },
    }
    return rows, metadata


def publish_dataset(rows: list[dict[str, Any]], metadata: dict[str, Any], output_dir: Path) -> tuple[dict[str, Any], Path, bool]:
    bounds = metadata["range"]
    compact = lambda value: datetime.fromtimestamp(parse_utc(value), timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    directory = output_dir.resolve() / (
        f"moodle_dataset_{compact(bounds['start'])}_{compact(bounds['end'])}"
        f"_step{bounds['step_seconds']}s_{metadata['input_sha256'][:12]}"
    )
    if directory.exists():
        try:
            existing = json.loads((directory / "metadata.json").read_text(encoding="utf-8"))
            if (existing["input_sha256"] != metadata["input_sha256"] or
                    existing["range"] != bounds or existing["summary"] != metadata["summary"] or
                    existing["dataset_sha256"] != exporter.file_sha256(directory / "dataset.csv")):
                raise ExportError("Le jeu de donnees existant est incoherent ; il ne sera pas ecrase.")
            return existing, directory, True
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise ExportError("Le jeu de donnees existant est incomplet ou invalide.") from exc
    directory.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".dataset-", dir=directory.parent))
    try:
        csv_path = temporary / "dataset.csv"
        with csv_path.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=FIELDS)
            writer.writeheader()
            writer.writerows(rows)
        metadata = {**metadata, "dataset_sha256": exporter.file_sha256(csv_path)}
        (temporary / "metadata.json").write_text(
            json.dumps(metadata, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8"
        )
        os.rename(temporary, directory)
        return metadata, directory, False
    except FileExistsError as exc:
        raise ExportError("Un autre processus a deja publie ce jeu de donnees.") from exc
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", type=Path, action="append", help="Repertoire d'exports ; option repetable.")
    parser.add_argument("--output-dir", type=Path, default=Path(__file__).resolve().parent / "data/datasets")
    arguments = parser.parse_args(argv)
    try:
        paths = discover_exports(arguments.input_dir or [Path(__file__).resolve().parent / "data/exports"])
        rows, metadata = merge_exports(paths, progress=lambda message: print(message, file=sys.stderr, flush=True))
        metadata, directory, reused = publish_dataset(rows, metadata, arguments.output_dir)
        summary = {
            "status": "already_prepared" if reused else "prepared",
            "dataset_file": str(directory / "dataset.csv"),
            "metadata_file": str(directory / "metadata.json"),
            "range": metadata["range"],
            **metadata["summary"],
        }
        if summary["resource_complete_rows"] == 0:
            summary["status"] = "no_usable_resource_data"
        print(json.dumps(summary, indent=2, ensure_ascii=False))
        return 0 if summary["resource_complete_rows"] > 0 else 3
    except (ExportError, OSError) as exc:
        print(f"Preparation interrompue : {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
