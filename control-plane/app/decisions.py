"""Recommandations deterministes en simulation a partir d'un instantane coherent."""
import copy
import hashlib
import json
import math
import re
from datetime import datetime, timezone

from .incident_signals import RESOURCE_RULES
from .incidents import DEFINITIONS, POLICY, IncidentStorageError, iso

POLICY_VERSION = "1.1.0"
RULES = {
    "php_pool_queue": {"action":"diagnose_php_pool", "priority":4,
        "dependencies":["prometheus_unavailable", "linux_collection", "moodle_collection", "moodle_https", "cpu_high", "memory_high", "disk_low"],
        "title":"Examiner la file PHP-FPM et le profil autorise",
        "steps":["Verifier la file et les processus du pool avec des observations recentes.",
                 "Verifier les ressources et les dependances avant un changement de profil.",
                 "Controler Moodle et revenir au profil precedent si le changement n'est pas confirme."]},
    "prometheus_unavailable": {"action":"inspect_prometheus", "priority":1, "dependencies":[],
        "title":"Verifier la lecture de Prometheus", "steps":["Consulter les cibles et les journaux de Prometheus.", "Verifier la connectivite locale et les delais de reponse."]},
    "linux_collection": {"action":"inspect_linux_collection", "priority":1, "dependencies":["prometheus_unavailable"],
        "title":"Diagnostiquer la collecte Linux", "steps":["Verifier la cible Linux et Node Exporter.", "Comparer les heures de collecte et rechercher les interruptions d'execution."]},
    "moodle_collection": {"action":"inspect_moodle_collection", "priority":1, "dependencies":["prometheus_unavailable"],
        "title":"Diagnostiquer la collecte de la sonde Moodle", "steps":["Verifier la cible Blackbox et ses journaux.", "Verifier la resolution du nom, le certificat et la configuration de la sonde."]},
    "anomaly_model": {"action":"inspect_anomaly_model", "priority":2, "dependencies":[],
        "title":"Verifier les artefacts du modele", "steps":["Consulter le statut du modele et ses journaux.", "Verifier les empreintes, la provenance et la compatibilite des artefacts."]},
    "analysis_history": {"action":"wait_contiguous_history", "priority":2, "dependencies":["prometheus_unavailable", "linux_collection", "anomaly_model"],
        "title":"Stabiliser la collecte pour obtenir un historique continu", "steps":["Rechercher les interruptions de collecte et d'execution.", "Accumuler la duree d'historique continu demandee par le modele.", "Reevaluer la disponibilite de l'analyse sans combler les donnees manquantes."]},
    "moodle_https": {"action":"diagnose_moodle_https", "priority":3, "dependencies":["prometheus_unavailable", "moodle_collection"],
        "title":"Identifier la cause de l'echec de la sonde HTTPS", "steps":["Verifier la sonde HTTPS avec validation du certificat.", "Consulter l'etat et les journaux d'Apache, PHP-FPM et PostgreSQL.", "Identifier le service concerne avant de choisir une correction."]},
    "disk_low": {"action":"diagnose_disk_usage", "priority":4, "dependencies":["prometheus_unavailable", "linux_collection"],
        "title":"Identifier l'origine de l'occupation du disque", "steps":["Mesurer l'occupation par repertoire et l'espace disponible.", "Verifier la croissance des journaux, volumes et sauvegardes.", "Valider une politique de retention avant toute suppression."]},
    "memory_high": {"action":"diagnose_memory_usage", "priority":5, "dependencies":["prometheus_unavailable", "linux_collection"],
        "title":"Identifier les consommateurs de memoire", "steps":["Mesurer la memoire des processus et conteneurs.", "Rechercher pression memoire, swap et evenements OOM.", "Comparer les mesures aux limites et a la charge applicative."]},
    "cpu_high": {"action":"diagnose_cpu_usage", "priority":5, "dependencies":["prometheus_unavailable", "linux_collection"],
        "title":"Identifier les consommateurs de CPU", "steps":["Mesurer le CPU des processus et conteneurs.", "Verifier les quotas CPU, les taches planifiees et la charge PHP-FPM.", "Verifier la file PHP-FPM avant de proposer un changement de son pool."]},
    "statistical_anomaly": {"action":"investigate_statistical_anomaly", "priority":6,
        "dependencies":["prometheus_unavailable", "linux_collection", "anomaly_model", "analysis_history"],
        "title":"Examiner l'observation atypique", "steps":["Comparer les ressources aux mesures recentes et aux incidents operationnels.", "Verifier le contexte du modele et la representativite des donnees.", "Etablir une cause operationnelle avant de choisir une correction."]},
}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def policy_document():
    document = {"version":POLICY_VERSION,"mode":"simulation","execution_enabled":False,
        "max_observation_age_seconds":POLICY["max_gap_seconds"],"confirmation_checks":POLICY["open_checks"],
        "resource_thresholds":copy.deepcopy(RESOURCE_RULES),"rules":copy.deepcopy(RULES),
        "constraints":["Lire un instantane coherent.", "Utiliser une observation recente et confirmee.",
                       "Verifier les dependances de collecte.", "Une anomalie statistique ne determine aucune cause.",
                       "Aucune commande ni aucun playbook n'est execute par ce module."]}
    document["sha256"] = digest(document)
    return document


def epoch(value):
    if not isinstance(value,str) or not value.endswith("Z"):
        raise ValueError("Date UTC attendue.")
    result = datetime.fromisoformat(value.replace("Z","+00:00")).timestamp()
    if not math.isfinite(result) or result < 0:
        raise ValueError("Date invalide.")
    return result


def number(value, low=0, high=100):
    return type(value) in (int,float) and math.isfinite(value) and low <= value <= high


def freshness(snapshot, now):
    collector = snapshot.get("collector")
    signals = snapshot.get("signals",{})
    reasons = []
    point = None
    if collector is None:
        reasons.append("observer_missing")
    else:
        try:
            point = epoch(collector["input_evaluated_at"])
            completed = epoch(collector["last_completed_at"])
            if (collector.get("clock_inconsistent") or min(now-point,now-completed) < 0 or completed < point):
                reasons.append("clock_inconsistent")
            elif max(now-point,now-completed) > POLICY["max_gap_seconds"]:
                reasons.append("observer_stale")
            elif collector.get("status") != "running":
                reasons.append("observer_not_running")
            if collector.get("processed_signals") != len(DEFINITIONS):
                reasons.append("observation_incomplete")
            if collector.get("policy") != POLICY:
                reasons.append("observation_policy_incompatible")
        except (KeyError,TypeError,ValueError):
            reasons.append("invalid_collector_metadata")
    if set(signals) != set(DEFINITIONS):
        reasons.append("signal_snapshot_incomplete")
    elif point is not None:
        for signal in signals.values():
            try:
                if epoch(signal["last_observed_at"]) != point:
                    reasons.append("mixed_observation_times")
                    break
            except (KeyError,TypeError,ValueError):
                reasons.append("invalid_signal_metadata")
                break
    return list(dict.fromkeys(reasons)), point


def evidence_problem(incident, point):
    key, evidence = incident["signal_key"],incident["evidence"]
    if key in RESOURCE_RULES:
        metric,trigger,recovery,direction = RESOURCE_RULES[key]
        value = evidence.get("value")
        if (evidence.get("metric") != metric or not number(value)
                or evidence.get("trigger_threshold") != trigger or evidence.get("recovery_threshold") != recovery
                or evidence.get("direction") != direction
                or not (value >= trigger if direction == "upper" else value <= trigger)):
            return "trigger_evidence_inconsistent"
    elif key == "php_pool_queue":
        keys=("active_processes","idle_processes","total_processes","listen_queue")
        if (evidence.get("pool") != "www" or not all(number(evidence.get(k),0,100000) for k in keys)
                or evidence["listen_queue"] <= 0 or evidence["idle_processes"] != 0
                or evidence["active_processes"] <= 0 or evidence["active_processes"] != evidence["total_processes"]
                or not number(evidence.get("scrape_age_seconds"),0,30)):
            return "pool_evidence_inconsistent"
    elif key == "moodle_https" and evidence.get("probe_success") is not False:
        return "trigger_evidence_inconsistent"
    elif key == "analysis_history":
        seconds=evidence.get("required_history_seconds")
        if type(seconds) is not int or not 1 <= seconds <= 86400:
            return "history_requirement_missing"
    elif key == "statistical_anomaly":
        score,threshold,identifier=evidence.get("score"),evidence.get("threshold"),evidence.get("model_id")
        if (not number(score,0,1) or not number(threshold,0,1) or score <= threshold
                or not isinstance(identifier,str) or not re.fullmatch(r"[0-9a-f]{64}",identifier)
                or incident["context"] != "model:"+identifier or evidence.get("experimental") is not True
                or evidence.get("quality") != "not_evaluated_no_incident_labels"):
            return "statistical_evidence_inconsistent"
        try:
            if epoch(evidence.get("input_evaluated_at")) != point:
                return "statistical_evidence_outdated"
        except ValueError:
            return "statistical_evidence_outdated"
    return None


EXPLANATIONS = {
    "blocked":"La recommandation est suspendue ; les conditions d'observation ne sont pas reunies.",
    "monitoring":"Le signal est en retour a la normale ou dans la zone d'hysteresis ; poursuivre l'observation.",
    "closed":"Cet episode est resolu ; aucune action courante n'est proposee.",
    "recommendation":"Les conditions de la politique permettent de proposer ce diagnostic en simulation.",
}


def decide(incident, snapshot, now, policy):
    key=incident["signal_key"]
    rule=RULES.get(key)
    reasons,point=freshness(snapshot,now)
    signal=snapshot.get("signals",{}).get(key)
    state=incident["latest_state"]
    if rule is None:
        reasons.append("no_policy_for_signal")
    if incident["status"] == "resolved":
        status,reasons="closed",["incident_resolved"]
    else:
        if signal is None:
            reasons.append("incident_signal_missing")
        elif (signal["state"] != state or signal["context"] != incident["context"]
              or signal["last_observed_at"] != incident["last_observed_at"]
              or signal["evidence"] != incident["evidence"]):
            reasons.append("incident_signal_inconsistent")
        if reasons:
            status="blocked"
        elif state in ("clear","hold"):
            status,reasons="monitoring",["recovery_confirmation_pending" if state == "clear" else "hysteresis_hold"]
        elif state != "triggered":
            status,reasons="blocked",["current_signal_unknown"]
        elif type(signal["bad_count"]) is not int or signal["bad_count"] < POLICY["open_checks"]:
            status,reasons="blocked",["trigger_reconfirmation_required"]
        else:
            for dependency in rule["dependencies"]:
                if snapshot["signals"][dependency]["state"] != "clear":
                    reasons.append("dependency_unhealthy:"+dependency)
            issue=evidence_problem(incident,point)
            if issue:
                reasons.append(issue)
            if key == "statistical_anomaly" and snapshot["signals"]["analysis_history"]["context"] != incident["context"]:
                reasons.append("analysis_model_context_inconsistent")
            status="blocked" if reasons else "recommendation"
            if not reasons:
                reasons=["confirmed_recent_signal"]
    action=None
    if status == "recommendation":
        action={"id":rule["action"],"title":rule["title"],"steps":copy.deepcopy(rule["steps"]),
                "kind":"diagnostic_recommendation","executable":False}
        if key == "analysis_history":
            action["required_history_seconds"]=incident["evidence"]["required_history_seconds"]
    related=[row for row in snapshot["open_incidents"] if row["id"] != incident["id"] and (
             row["signal_key"] in (rule["dependencies"] if rule else []) or row["signal_key"] in ("cpu_high","memory_high","disk_low","moodle_https"))]
    decision={"incident_id":incident["id"],"signal_key":key,"incident_status":incident["status"],
        "latest_state":state,"context":incident["context"],"status":status,"priority":rule["priority"] if rule else 99,
        "reason_codes":list(dict.fromkeys(reasons)),"explanation":EXPLANATIONS[status],
        "recommended_action":action,"related_incidents":related,"evidence":copy.deepcopy(incident["evidence"]),
        "input_evaluated_at":iso(point),"valid_until":iso(point+POLICY["max_gap_seconds"]) if point is not None else None,
        "policy_version":POLICY_VERSION,"policy_sha256":policy["sha256"],
        "mode":"simulation","execution_allowed":False,"persisted":False}
    decision["decision_id"]=digest(decision)
    return decision


def evaluate(snapshot, now):
    if type(now) not in (int,float) or not math.isfinite(now) or now < 0:
        raise ValueError("Date d'evaluation invalide.")
    policy=policy_document()
    try:
        # Un contenu SQLite incoherent ou non fini ne produit aucun plan.
        json.dumps(snapshot,allow_nan=False)
        decisions=[decide(incident,snapshot,now,policy) for incident in snapshot["incidents"]]
        decisions.sort(key=lambda item:(item["priority"],item["incident_id"]))
        counts={state:sum(item["status"] == state for item in decisions) for state in EXPLANATIONS}
        reasons,point=freshness(snapshot,now)
    except (KeyError,TypeError,ValueError,OverflowError) as exc:
        raise IncidentStorageError("Instantane des decisions incoherent.") from exc
    return {"status":"available","mode":"simulation","evaluated_at":iso(now),
        "policy_version":POLICY_VERSION,"policy_sha256":policy["sha256"],
        "collector":snapshot["collector"],"snapshot_usable":not reasons,"snapshot_reason_codes":reasons,
        "input_evaluated_at":iso(point),"counts":counts,"decisions":decisions,
        "execution":{"enabled":False,"runner_configured":False,"persisted":False},
        "semantics":{"recommendation":"Diagnostic propose ; aucune correction executee.",
            "cause":"Un signal ou une correlation ne prouve aucune cause de panne.",
            "validity":"La validite part de la date des donnees ; relire et revalider avant une action future.",
            "audit":"Les recommandations sont calculees a la lecture ; leur execution et leur journalisation restent a implementer."}}


def summarize(store, now, identifier=None):
    snapshot=store.decision_snapshot(now,identifier)
    if identifier is not None and not snapshot["incidents"]:
        return None
    result=evaluate(snapshot,now)
    if identifier is not None:
        result["decision"]=result.pop("decisions")[0]
    return result
