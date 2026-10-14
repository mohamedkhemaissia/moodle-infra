"""Entrainer et valider les modeles IA a partir du Prometheus du pilote.

Le pipeline ne complete aucune valeur manquante et n'utilise que la plus longue
periode continue de mesures CPU, memoire et disque. Les artefacts restent locaux
dans ``ml/data`` et sont verifies avec les chargeurs utilises par l'API.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import sys
import tempfile
from typing import Any

from ml import export_metrics as exporter
from ml import prepare_dataset
from ml import prepare_features


STEP_SECONDS = 30
MIN_CONTINUOUS_HOURS = 4.0


def longest_complete_window(rows: list[dict[str, Any]], window: exporter.Window) -> exporter.Window | None:
    """Retourner le plus long segment continu de mesures de ressources completes."""
    best_start = best_end = current_start = None
    for index, row in enumerate(rows):
        timestamp = window.start + index * window.step
        if row.get("resource_metrics_complete") == 1:
            if current_start is None:
                current_start = timestamp
            if best_start is None or timestamp - current_start > best_end - best_start:
                best_start, best_end = current_start, timestamp
        else:
            current_start = None
    return None if best_start is None else exporter.Window(best_start, best_end, window.step)


def validate_runtime_bundles(
    repository: Path, features_dir: Path, anomaly_dir: Path, forecast_dir: Path,
) -> None:
    """Charger les artefacts avec les validateurs reels de l'API."""
    # Le service ne charge pas directement ``control-plane/app`` : son image est
    # assemblee avec les sources partagees, dont le moteur numerique IA canonique
    # ``ml/isolation_model.py``. Valider le meme contexte evite un faux echec ici
    # alors que l'image de production contient bien ce module.
    from ci.prepare_api import prepare as prepare_api

    with tempfile.TemporaryDirectory(prefix="moodle-ai-validation-") as temporary:
        root = Path(temporary)
        api_context = root / "api-context"
        prepare_api(repository, api_context)
        previous_app_modules = {
            name: module for name, module in sys.modules.items()
            if name == "app" or name.startswith("app.")
        }
        for name in previous_app_modules:
            sys.modules.pop(name, None)
        sys.path.insert(0, str(api_context))
        try:
            from app.anomalies import AnomalyBundle
            from app.forecasts import ForecastBundle

            anomaly_bundle = root / "anomalies"
            forecast_bundle = root / "forecasts"
            anomaly_bundle.mkdir()
            forecast_bundle.mkdir()
            shutil.copy2(anomaly_dir / "model.json", anomaly_bundle / "model.json")
            shutil.copy2(anomaly_dir / "report.json", anomaly_bundle / "report.json")
            shutil.copy2(features_dir / "metadata.json", anomaly_bundle / "features_metadata.json")
            shutil.copy2(forecast_dir / "models.json", forecast_bundle / "models.json")
            shutil.copy2(forecast_dir / "report.json", forecast_bundle / "report.json")
            shutil.copy2(features_dir / "metadata.json", forecast_bundle / "features_metadata.json")
            AnomalyBundle(anomaly_bundle)
            ForecastBundle(forecast_bundle)
        finally:
            sys.path.remove(str(api_context))
            for name in list(sys.modules):
                if name == "app" or name.startswith("app."):
                    sys.modules.pop(name, None)
            sys.modules.update(previous_app_modules)


def evidence_document(
    source: dict[str, Any], features: dict[str, Any], anomaly: dict[str, Any],
    forecasts: dict[str, Any], paths: dict[str, str],
) -> tuple[dict[str, Any], str]:
    """Construire une preuve JSON et un resume Markdown sans sur-vendre les modeles."""
    forecast_rows = []
    for horizon, report in forecasts["horizons"].items():
        for metric, evaluation in report["test"].items():
            forecast_rows.append({
                "horizon_minutes": int(horizon),
                "metric": metric,
                "selected_method": evaluation["preferred_on_validation"],
                "test_mae_ridge": round(evaluation["ridge"]["mae"], 6),
                "test_mae_persistence": round(evaluation["persistence"]["mae"], 6),
                "test_gain_percent": (
                    round(evaluation["mae_gain_percent"], 3)
                    if evaluation["mae_gain_percent"] is not None else None
                ),
            })
    evidence = {
        "schema_version": 1,
        "status": "validated_for_observation",
        "created_at": exporter.utc_iso(datetime.now(timezone.utc).timestamp()),
        "data": {
            "source": "Prometheus du pilote Moodle",
            "range": source["range"],
            "resource_complete_rows": source["summary"]["resource_complete_rows"],
            "feature_rows_by_split": features["summary"]["rows_by_split"],
            "interpolation": False,
        },
        "anomaly_detection": {
            "algorithm": "Isolation Forest",
            "threshold": anomaly["anomaly_threshold"],
            "test_rows": anomaly["by_split"]["test"]["rows"],
            "test_statistical_anomaly_rate_percent": anomaly["by_split"]["test"]["statistical_anomaly_rate_percent"],
            "quality": anomaly["detection_quality"],
        },
        "forecasts": forecast_rows,
        "validation": {
            "api_bundle_loaders": "passed",
            "train_validation_test_separation": "chronological_70_15_15",
        },
        "paths": paths,
        "limits": [
            "Le score d'anomalie n'est pas une probabilite de panne.",
            "Precision et rappel exigent des incidents independamment annotes.",
            "Les previsions portent sur ce pilote et sont comparees a la persistance.",
            "Aucune remediation n'est declenchee par ces modeles.",
        ],
    }
    metric_labels = {
        "cpu_usage_percent": "CPU utilise",
        "memory_usage_percent": "Memoire utilisee",
        "disk_available_percent": "Disque disponible",
    }
    lines = [
        "# Preuve d'entrainement IA du pilote Moodle", "",
        f"- Statut : **{evidence['status']}**",
        f"- Source : {evidence['data']['source']}",
        f"- Periode UTC : {source['range']['start']} -> {source['range']['end']}",
        f"- Mesures completes : {evidence['data']['resource_complete_rows']}",
        "- Valeurs inventees ou interpolees : non", "",
        "## Detection d'anomalies", "",
        "Modele Isolation Forest, appris sur train uniquement.",
        f"Seuil : {anomaly['anomaly_threshold']:.6f}. "
        f"Taux de signalement sur test : {anomaly['by_split']['test']['statistical_anomaly_rate_percent']:.2f} %.",
        "Ce score mesure un comportement atypique, pas une probabilite de panne.", "",
        "## Previsions", "",
        "| Horizon | Ressource | Methode retenue | MAE Ridge | MAE persistance | Gain Ridge |",
        "| ---: | --- | --- | ---: | ---: | ---: |",
    ]
    for row in forecast_rows:
        gain = "n/a" if row["test_gain_percent"] is None else f"{row['test_gain_percent']:.2f} %"
        lines.append(
            f"| {row['horizon_minutes']} min | {metric_labels[row['metric']]} | "
            f"{row['selected_method']} | {row['test_mae_ridge']:.4f} | "
            f"{row['test_mae_persistence']:.4f} | {gain} |"
        )
    lines.extend([
        "", "Le choix Ridge/persistance est fait sur validation ; le test reste reserve a l'evaluation.",
        "", "## Limites", "",
        *[f"- {item}" for item in evidence["limits"]], "",
    ])
    return evidence, "\n".join(lines)


def write_evidence(output_dir: Path, evidence: dict[str, Any], markdown: str) -> Path:
    end = evidence["data"]["range"]["end"].replace("-", "").replace(":", "")
    directory = output_dir / f"moodle_ai_evidence_{end}"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "evidence.json").write_text(
        json.dumps(evidence, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8"
    )
    (directory / "jury-summary.md").write_text(markdown, encoding="utf-8")
    return directory


def run_pipeline(repository: Path, prometheus_url: str, hours: float) -> dict[str, Any]:
    # Les dependances numeriques sont chargees seulement pour l'entrainement ;
    # l'aide, les controles de fenetre et la generation de preuve restent legers.
    from ml import train_anomalies
    from ml import train_forecasts

    data_dir = repository / "ml" / "data"
    reader = exporter.PrometheusReader(prometheus_url)
    requested = exporter.make_window(hours=hours, step=STEP_SECONDS)
    print(f"Lecture de {hours:g} h de metriques reelles dans Prometheus...", file=sys.stderr, flush=True)
    _, discovery_dir, _ = exporter.perform_export(
        reader, requested, data_dir / "exports", progress=lambda value: print(value, file=sys.stderr, flush=True)
    )
    discovery = prepare_dataset.read_export(discovery_dir / "metadata.json")
    continuous = longest_complete_window(discovery.rows, discovery.window)
    minimum_points = int(MIN_CONTINUOUS_HOURS * 3600 / STEP_SECONDS) + 1
    if continuous is None or continuous.count < minimum_points:
        available = 0 if continuous is None else (continuous.count - 1) * STEP_SECONDS / 3600
        raise exporter.ExportError(
            f"Historique continu insuffisant : {available:.2f} h disponibles, "
            f"{MIN_CONTINUOUS_HOURS:g} h necessaires. Laisser le Codespace actif puis relancer."
        )
    print(
        f"Periode continue retenue : {exporter.utc_iso(continuous.start)} -> "
        f"{exporter.utc_iso(continuous.end)} ({continuous.count} mesures).",
        file=sys.stderr, flush=True,
    )
    export_metadata, export_dir, _ = exporter.perform_export(
        reader, continuous, data_dir / "exports", progress=lambda value: print(value, file=sys.stderr, flush=True)
    )
    rows, dataset_metadata = prepare_dataset.merge_exports([export_dir / "metadata.json"])
    dataset_metadata, dataset_dir, _ = prepare_dataset.publish_dataset(
        rows, dataset_metadata, data_dir / "datasets"
    )
    dataset = prepare_features.read_dataset(dataset_dir)
    tables, feature_metadata = prepare_features.build_tables(dataset)
    feature_metadata, features_dir, _ = prepare_features.publish_features(
        tables, feature_metadata, data_dir / "features"
    )
    anomaly_inputs = train_anomalies.read_inputs(features_dir)
    anomaly_report, anomaly_dir, _ = train_anomalies.perform_training(
        anomaly_inputs, data_dir / "models"
    )
    forecast_inputs = train_forecasts.read_inputs(features_dir)
    forecast_report, forecast_dir, _ = train_forecasts.perform_training(
        forecast_inputs, data_dir / "models",
        progress=lambda value: print(value, file=sys.stderr, flush=True),
    )
    validate_runtime_bundles(repository, features_dir, anomaly_dir, forecast_dir)
    paths = {
        "features_directory": str(features_dir),
        "anomaly_directory": str(anomaly_dir),
        "forecast_directory": str(forecast_dir),
    }
    evidence, markdown = evidence_document(
        export_metadata, feature_metadata, anomaly_report, forecast_report, paths
    )
    evidence_dir = write_evidence(data_dir / "reports", evidence, markdown)
    return {
        "status": "trained_and_validated",
        **paths,
        "evidence_json": str(evidence_dir / "evidence.json"),
        "jury_summary": str(evidence_dir / "jury-summary.md"),
        "next_command": "bash scripts/codespaces/deploy.sh",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prometheus-url", default="http://127.0.0.1:9090")
    parser.add_argument("--hours", type=float, default=6.0, help="Historique examine ; 6 heures par defaut.")
    parser.add_argument("--repository", type=Path, default=Path(__file__).resolve().parents[1])
    arguments = parser.parse_args(argv)
    try:
        if arguments.hours < MIN_CONTINUOUS_HOURS or arguments.hours > 24:
            raise exporter.ExportError("Choisir une duree comprise entre 4 et 24 heures.")
        result = run_pipeline(arguments.repository.resolve(), arguments.prometheus_url, arguments.hours)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    except (exporter.ExportError, OSError, ValueError) as exc:
        print(f"Pipeline IA interrompu : {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
