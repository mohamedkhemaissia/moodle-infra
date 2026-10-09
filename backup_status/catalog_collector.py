"""Lister les references Restic ; seules des metadonnees publiques sont publiees."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import tempfile
import time

from . import catalog, pilotage, status
from .collector import publish

MAX_OUTPUT = 8 * 1024 * 1024
MAX_SNAPSHOTS = 10000
TIMEOUT = 45


class ListingError(Exception):
    pass


def stop(process):
    # Restic peut lancer rclone ; les deux appartiennent au groupe prive de cette lecture.
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait(timeout=2)


def command(argv, timeout=TIMEOUT, max_bytes=MAX_OUTPUT):
    start = time.monotonic()
    with tempfile.TemporaryFile() as output:
        process = None
        try:
            process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.DEVNULL,
                start_new_session=True, env={"PATH": "/usr/local/bin:/usr/bin:/bin", "LC_ALL": "C"})
            while process.poll() is None:
                if os.fstat(output.fileno()).st_size > max_bytes:
                    raise ListingError("repository_output_too_large")
                if time.monotonic() - start > timeout:
                    raise ListingError("repository_timeout")
                time.sleep(.025)
            if process.returncode != 0:
                raise ListingError("repository_unavailable")
            if time.monotonic() - start > timeout:
                raise ListingError("repository_timeout")
            output.seek(0)
            raw = output.read(max_bytes + 1)
            if len(raw) > max_bytes:
                raise ListingError("repository_output_too_large")
            return raw
        except OSError:
            raise ListingError("repository_unavailable") from None
        finally:
            if process is not None:
                stop(process)


def parse_time(value):
    match = re.fullmatch(
        r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.\d{1,9})?"
        r"(Z|[+-](?:[01]\d|2[0-3]):[0-5]\d)", value) if isinstance(value, str) else None
    if match is None:
        raise ValueError("Date Restic invalide.")
    # Restic emet des nanosecondes. Python 3.10 ne lit pas les fractions de neuf
    # chiffres ; la projection publique conserve deja des secondes entieres.
    # Valider la fraction ci-dessus puis tronquer, sans arrondir ni perdre le fuseau.
    offset = "+00:00" if match[2] == "Z" else match[2]
    try:
        date = datetime.fromisoformat(match[1] + offset)
        return status.encoded_time(date)
    except OverflowError:
        raise ValueError("Date Restic hors limites UTC.") from None


def source_path(path):
    if not isinstance(path, str) or not re.fullmatch(r"/[a-zA-Z0-9_./-]+", path) or ".." in path:
        raise ValueError("Repertoire invalide.")
    return path.rstrip("/")


def project_name(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,80}", value) or ".." in value:
        raise ValueError("Projet invalide.")
    return value


def project_scope(rows, tag, staging, record, now):
    items = []
    try:
        for row in rows:
            tags = row.get("tags", [])
            if not isinstance(tags, list) or any(not isinstance(t, str) for t in tags):
                raise ValueError("Tags invalides.")
            if tag not in tags:
                continue
            paths = row.get("paths")
            if (not isinstance(paths, list) or len(paths) != 1 or not isinstance(paths[0], str)
                    or not paths[0].startswith(staging + "/")):
                raise ValueError("Perimetre du point incompatible.")
            suffix = paths[0][len(staging)+1:]
            if not re.fullmatch(r"run-[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}", suffix) or ".." in suffix:
                raise ValueError("Chemin du point incompatible.")
            if (not isinstance(row.get("tree"), str) or not status.SNAPSHOT.fullmatch(row["tree"])
                    or not isinstance(row.get("hostname"), str) or not catalog.HOST.fullmatch(row["hostname"])):
                raise ValueError("Metadonnees du point invalides.")
            archived = parse_time(row.get("time"))
            if status.utc(archived) > now:
                raise ValueError("Date future.")
            items.append({"id": row["id"], "archived_at": archived, "recovery_at": None,
                          "hostname": row["hostname"], "success_recorded": False})
        latest = record["latest"] if record["status"] == "recorded" else None
        latest_id = latest["snapshot"] if latest else None
        presence = "unknown" if latest is None else "absent"
        for item in items:
            if item["id"] == latest_id:
                # Le marqueur confirme un gel/capture ; le timestamp Restic indique le transfert.
                if status.utc(latest["frozen_at_utc"]) > status.utc(item["archived_at"]) or (
                        status.utc(item["archived_at"]) > status.utc(latest["completed_at_utc"])):
                    raise ValueError("Dates du marqueur incompatibles.")
                presence = "present"
                item.update(recovery_at=latest["frozen_at_utc"], success_recorded=True)
        items.sort(key=lambda item: (item["archived_at"], item["id"]), reverse=True)
        value = {"status": "available", "reason": None, "total": len(items),
                 "items": items[:catalog.MAX_ITEMS], "truncated": len(items) > catalog.MAX_ITEMS,
                 "latest_success_presence": presence, "latest_success_id": latest_id}
        return catalog.validate_scope(value, now)
    except (ValueError, TypeError, KeyError):
        return catalog.unavailable("scope_metadata_invalid")


def collect(project, moodle_staging, marker_path, pilotage_root=None, run=command, clock=None):
    project_name(project)
    moodle_staging = source_path(moodle_staging)
    source_path(str(marker_path))
    if pilotage_root is not None:
        pilotage_root = source_path(str(pilotage_root))
    clock = clock or (lambda: datetime.now(timezone.utc))
    # Age conservateur : le debut de lecture, jamais sa publication tardive.
    now = clock()
    try:
        # Sans verrou/caches Restic : cette commande n'ecrit rien dans le depot.
        raw = run(["/usr/local/sbin/" + project + "-restic", "snapshots", "--json", "--no-lock", "--no-cache"])
        if not isinstance(raw, bytes) or len(raw) > MAX_OUTPUT:
            raise ListingError("repository_output_too_large")
        try:
            rows = json.loads(raw, object_pairs_hook=status._pairs, parse_constant=status._constant)
            if not isinstance(rows, list) or len(rows) > MAX_SNAPSHOTS:
                raise ValueError("Liste invalide.")
            ids = set()
            for row in rows:
                identifier = row.get("id") if isinstance(row, dict) else None
                if not isinstance(identifier, str) or not status.SNAPSHOT.fullmatch(identifier) or identifier in ids:
                    raise ValueError("Reference invalide ou dupliquee.")
                ids.add(identifier)
        except (ValueError, TypeError, UnicodeError, RecursionError):
            raise ListingError("repository_output_invalid") from None
        scopes = {"moodle": project_scope(rows, project+"-full", moodle_staging,
                    status.marker(marker_path, now), now), "pilotage": catalog.unavailable("not_configured")}
        if pilotage_root is not None:
            scopes["pilotage"] = project_scope(rows, "moodle-pilotage-database", pilotage_root+"/staging",
                                              pilotage.read_marker(Path(pilotage_root), now), now)
    except ListingError as exc:
        reason = str(exc) if str(exc) in catalog.REASONS else "repository_unavailable"
        scopes = {scope: catalog.unavailable(reason) for scope in catalog.SCOPES}
        if pilotage_root is None:
            scopes["pilotage"] = catalog.unavailable("not_configured")
    return catalog.validate_document({"schema_version": 1, "observed_at": status.encoded_time(now), "scopes": scopes})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--moodle-staging", required=True)
    parser.add_argument("--marker", required=True)
    parser.add_argument("--pilotage-root")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    try:
        value = collect(args.project, args.moodle_staging, args.marker, args.pilotage_root)
        publish(args.output, value, catalog.validate_document)
    except (OSError, ValueError):
        parser.exit(1, "Publication du catalogue impossible.\n")
    print(json.dumps({"status": "published", "scopes": {scope: {
        "status": v["status"], "reason": v["reason"], "total": v["total"]} for scope, v in value["scopes"].items()}}))


if __name__ == "__main__":
    main()
