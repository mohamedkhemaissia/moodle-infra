"""Verifier le catalogue avec le Restic fige du projet et un depot temporaire."""
import argparse
import bz2
from datetime import datetime, timezone, timedelta
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backup_status import catalog, catalog_collector, status


def pinned_restic(directory):
    source = (ROOT / "ansible/roles/operations/defaults/main.yml").read_text()
    versions = re.findall(r"^moodle_backup_restic_version: '([0-9]+\.[0-9]+\.[0-9]+)'$", source, re.M)
    hashes = re.findall(r"^moodle_backup_restic_sha256: ([a-f0-9]{64})$", source, re.M)
    if len(versions) != 1 or len(hashes) != 1:
        raise ValueError("Version Restic non figee.")
    version = versions[0]
    url = f"https://github.com/restic/restic/releases/download/v{version}/restic_{version}_linux_amd64.bz2"
    with urlopen(url, timeout=60) as response:
        data = response.read(30*1024*1024+1)
    if len(data) > 30*1024*1024 or hashlib.sha256(data).hexdigest() != hashes[0]:
        raise ValueError("Archive Restic non verifiee.")
    decoder = bz2.BZ2Decompressor()
    binary = decoder.decompress(data, max_length=64*1024*1024+1)
    if not decoder.eof or len(binary) > 64*1024*1024 or not binary.startswith(b"\x7fELF"):
        raise ValueError("Binaire Restic non verifie.")
    path = directory / "restic"
    path.write_bytes(binary)
    path.chmod(0o700)
    return path


def fingerprint(directory):
    return {str(p.relative_to(directory)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(directory.rglob("*")) if p.is_file()}


def exercise(binary, directory):
    repository = directory / "repository"
    password = directory / "password"
    password.write_text("temporary-ci-catalog-password-only\n")
    password.chmod(0o600)
    base = [str(binary), "--repo", str(repository), "--password-file", str(password), "--no-cache"]

    def restic(*args):
        value = subprocess.run([*base, *args], stdin=subprocess.DEVNULL, capture_output=True, timeout=90)
        if value.returncode:
            raise RuntimeError("Commande Restic de test en echec.")
        return value.stdout

    restic("init")
    moodle_staging = directory / "moodle-staging"
    pgroot = directory / "pilotage"
    pgstaging = pgroot / "staging"
    for staging, name, tag in ((moodle_staging, "run-first", "moodle-pilote-full"),
                               (moodle_staging, "run-second", "moodle-pilote-full"),
                               (pgstaging, "run-first", "moodle-pilotage-database"),
                               (directory / "foreign", "run-first", "unrelated")):
        source = staging / name
        source.mkdir(parents=True)
        (source / "fixture.txt").write_text("Fictitious backup fixture; no pedagogical or production data.\n")
        restic("backup", "--json", "--host", "ci-catalog", "--tag", tag, str(source))
    rows = json.loads(restic("snapshots", "--json", "--no-lock"))
    selected = next(row for row in rows if row["paths"] == [str(moodle_staging / "run-second")])
    archived = status.utc(catalog_collector.parse_time(selected["time"]))
    frozen = (archived-timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    completed = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    marker = directory / "last-success.txt"
    marker.write_text(f"snapshot={selected['id']}\nfrozen_at_utc={frozen}\ncompleted_at_utc={completed}\n")
    before = fingerprint(repository)

    def listing(argv):
        if argv != ["/usr/local/sbin/moodle-pilote-restic", "snapshots", "--json", "--no-lock", "--no-cache"]:
            raise AssertionError("Une commande autre que la lecture de snapshots est interdite.")
        return catalog_collector.command([*base, *argv[1:]])

    value = catalog_collector.collect("moodle-pilote", str(moodle_staging), marker, pgroot, run=listing)
    assert fingerprint(repository) == before, "La lecture a modifie le depot."
    assert value["scopes"]["moodle"]["total"] == 2, value
    assert value["scopes"]["pilotage"]["total"] == 1, value
    assert value["scopes"]["moodle"]["latest_success_presence"] == "present", value
    point = next(p for p in value["scopes"]["moodle"]["items"] if p["id"] == selected["id"])
    assert point["recovery_at"] == frozen and point["success_recorded"] is True
    assert len(point["id"]) == 64
    public = directory / "catalog.json"
    from backup_status.collector import publish
    publish(public, value, catalog.validate_document)
    now = datetime.now(timezone.utc)
    result = catalog.summary(public, now, limit=1)
    assert result["total"] == 2 and result["has_more"] is True
    assert catalog.summary(public, now, limit=1, offset=1)["has_more"] is False
    for secret in (str(repository), str(password), "temporary-ci-catalog-password-only", "paths", "username"):
        assert secret not in json.dumps(result)
    # Simuler une retention, uniquement dans ce depot de test.
    restic("forget", selected["id"])
    changed = catalog_collector.collect("moodle-pilote", str(moodle_staging), marker, pgroot, run=listing)
    assert changed["scopes"]["moodle"]["total"] == 1
    assert changed["scopes"]["moodle"]["latest_success_presence"] == "absent"
    wrong = directory / "wrong-password"
    wrong.write_text("wrong-ci-only-password\n")
    wrong.chmod(0o600)
    def denied(argv):
        return catalog_collector.command([str(binary), "--repo", str(repository), "--password-file", str(wrong), *argv[1:]])
    failed = catalog_collector.collect("moodle-pilote", str(moodle_staging), marker, pgroot, run=denied)
    assert failed["scopes"]["moodle"]["reason"] == "repository_unavailable"
    assert failed["scopes"]["moodle"]["items"] == []
    print(json.dumps({"status": "passed", "scope": "temporary_local_repository",
        "checks": ["pinned_restic", "actual_json", "scope_separation", "snapshot_presence",
                   "archive_vs_capture", "pagination", "listing_does_not_write_repository",
                   "retained_snapshot_disappears", "repository_failure_is_unknown"]}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--restic", type=Path)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="ci-backup-catalog-") as temporary:
        directory = Path(temporary)
        binary = args.restic.resolve() if args.restic else pinned_restic(directory)
        exercise(binary, directory)


if __name__ == "__main__":
    main()
