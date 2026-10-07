"""Rejouer une foret d'isolation numerique JSON, sans dependance ML."""

from __future__ import annotations

import json
import math
from pathlib import Path
import struct
from typing import Any


SCHEMA_VERSION = 1
MODEL_TYPE = "isolation_forest_numeric_trees"
MAX_MODEL_BYTES = 8 * 1024 * 1024
EULER_GAMMA = 0.5772156649015329


class ModelError(ValueError):
    pass


def average_path_length(samples: int) -> float:
    if samples <= 1:
        return 0.0
    if samples == 2:
        return 1.0
    return 2 * (math.log(samples - 1) + EULER_GAMMA) - 2 * (samples - 1) / samples


def _number(value: Any) -> float:
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ModelError("Valeur numerique non finie ou invalide.")
    return float(value)


def validate_model(model: dict, expected_features: list[str] | None = None) -> dict:
    """Refuser les structures malformees, boucles et arbres incoherents."""
    try:
        if (type(model["schema_version"]) is not int
                or model["schema_version"] != SCHEMA_VERSION
                or model["model_type"] != MODEL_TYPE or model["input_dtype"] != "float32"):
            raise ModelError("Version ou type de modele non pris en charge.")
        names = model["feature_names"]
        if (not isinstance(names, list) or not 1 <= len(names) <= 128
                or any(not isinstance(name, str) or not name or len(name) > 100 for name in names)
                or len(set(names)) != len(names)
                or (expected_features is not None and names != expected_features)):
            raise ModelError("Ordre ou noms des variables invalides.")
        samples = model["max_samples"]
        if type(samples) is not int or not 2 <= samples <= 256:
            raise ModelError("Taille d'echantillon invalide.")
        if not 0 < _number(model["anomaly_threshold"]) < 1:
            raise ModelError("Seuil d'anomalie invalide.")
        trees = model["trees"]
        if not isinstance(trees, list) or not 1 <= len(trees) <= 100:
            raise ModelError("Nombre d'arbres invalide.")
        fields = ("left", "right", "feature", "threshold", "samples")
        for tree in trees:
            count = len(tree["left"])
            if not 1 <= count <= 2 * samples - 1:
                raise ModelError("Nombre de noeuds invalide.")
            if any(not isinstance(tree[key], list) or len(tree[key]) != count for key in fields):
                raise ModelError("Dimensions d'arbre incoherentes.")
            if tree["samples"][0] != samples:
                raise ModelError("Effectif racine incoherent.")
            visited, pending = set(), [(0, 0)]
            while pending:
                node, depth = pending.pop()
                if node in visited or depth > 16:
                    raise ModelError("Arbre cyclique, partage ou trop profond.")
                visited.add(node)
                left, right = tree["left"][node], tree["right"][node]
                feature, size = tree["feature"][node], tree["samples"][node]
                if any(type(value) is not int for value in (left, right, feature, size)):
                    raise ModelError("Indice ou effectif non entier.")
                if not 1 <= size <= samples:
                    raise ModelError("Effectif de noeud invalide.")
                _number(tree["threshold"][node])
                if left == right == -1:
                    if feature != -2:
                        raise ModelError("Variable d'une feuille invalide.")
                    continue
                if (not node < left < count or not node < right < count or left == right
                        or not 0 <= feature < len(names)):
                    raise ModelError("Enfants ou variable d'un noeud invalides.")
                if size != tree["samples"][left] + tree["samples"][right]:
                    raise ModelError("Effectifs des enfants incoherents.")
                pending.extend(((right, depth + 1), (left, depth + 1)))
            if len(visited) != count:
                raise ModelError("Noeuds d'arbre inaccessibles.")
        return model
    except (KeyError, TypeError, IndexError, OverflowError) as exc:
        raise ModelError("Structure du modele JSON invalide.") from exc


def export_forest(forest: Any, names: list[str], threshold: float) -> dict:
    """Exporter les seuls nombres utiles ; pas de pickle ni d'objet Python."""
    trees = []
    for estimator, mapping in zip(forest.estimators_, forest.estimators_features_):
        tree = estimator.tree_
        trees.append({
            "left": tree.children_left.tolist(),
            "right": tree.children_right.tolist(),
            "feature": [int(mapping[index]) if index >= 0 else -2 for index in tree.feature],
            "threshold": tree.threshold.tolist(),
            "samples": tree.n_node_samples.tolist(),
        })
    model = {
        "schema_version": SCHEMA_VERSION, "model_type": MODEL_TYPE,
        "feature_names": names, "max_samples": int(forest.max_samples_),
        "anomaly_threshold": float(threshold), "input_dtype": "float32",
        "trees": trees,
    }
    return validate_model(model, names)


def score_rows(model: dict, rows: list[list[float]]) -> list[float]:
    """Plus le score est eleve, plus l'observation est atypique."""
    validate_model(model)
    if not isinstance(rows, list) or len(rows) > 50_000:
        raise ModelError("Lot de variables invalide ou trop grand.")
    denominator = len(model["trees"]) * average_path_length(model["max_samples"])
    corrections = [[average_path_length(n) for n in tree["samples"]] for tree in model["trees"]]
    result = []
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) != len(model["feature_names"]):
            raise ModelError("Nombre de variables d'entree invalide.")
        try:
            # Scikit-learn convertit les entrees des arbres en float32.
            values = [struct.unpack("f", struct.pack("f", _number(value)))[0] for value in row]
        except (OverflowError, struct.error) as exc:
            raise ModelError("Une variable ne tient pas dans un float32.") from exc
        if not all(math.isfinite(value) for value in values):
            raise ModelError("Variable non finie apres conversion float32.")
        length = 0.0
        for tree, correction in zip(model["trees"], corrections):
            node, depth = 0, 0
            while tree["left"][node] != -1:
                node = (tree["left"][node] if values[tree["feature"][node]] <= tree["threshold"][node]
                        else tree["right"][node])
                depth += 1
            length += depth + correction[node]
        result.append(2 ** (-length / denominator))
    return result


def load_model(path: Path, expected_features: list[str] | None = None) -> dict:
    def unique_keys(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ModelError("Cle JSON dupliquee.")
            result[key] = value
        return result

    def reject_constant(value):
        raise ModelError(f"Constante JSON interdite : {value}.")

    try:
        if path.stat().st_size > MAX_MODEL_BYTES:
            raise ModelError("Modele JSON trop grand.")
        model = json.loads(path.read_bytes(), object_pairs_hook=unique_keys, parse_constant=reject_constant)
        if not isinstance(model, dict):
            raise ModelError("Le modele doit etre un objet JSON.")
        return validate_model(model, expected_features)
    except (OSError, ValueError) as exc:
        raise ModelError("Modele JSON illisible ou invalide.") from exc
