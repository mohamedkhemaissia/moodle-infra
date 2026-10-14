"""Assembler le contexte API a partir des sources partagees, comme le role Ansible."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile


SHARED = {
    'backup_status/status.py': 'app/backup_status.py',
    'backup_status/pilotage.py': 'app/pilotage_backup_status.py',
    'backup_status/catalog.py': 'app/backup_catalog_status.py',
    'ml/isolation_model.py': 'app/isolation_model.py',
    'diagnostics/store.py': 'app/diagnostics_store.py',
    'remediation/store.py': 'app/remediation_store.py',
    'pilotage/__init__.py': 'pilotage/__init__.py',
    'pilotage/db.py': 'pilotage/db.py',
    'pilotage/schemas.py': 'pilotage/schemas.py',
    'pilotage/migrate.py': 'pilotage/migrate.py',
    'pilotage/auth.py': 'pilotage/auth.py',
    'pilotage/auth_cli.py': 'pilotage/auth_cli.py',
    'pilotage/passwords.py': 'pilotage/passwords.py',
    'pilotage/worker_auth.py': 'pilotage/worker_auth.py',
    'pilotage/recovery.py': 'pilotage/recovery.py',
    'pilotage/recovery_cli.py': 'pilotage/recovery_cli.py',
    'pilotage/recovery_execution.py': 'pilotage/recovery_execution.py',
    'pilotage/operating_policy.py': 'pilotage/operating_policy.py',
    'pilotage/decision_journal.py': 'pilotage/decision_journal.py',
}
BASE_FILES = ('Dockerfile', '.dockerignore', 'requirements-lock.txt')


def prepare(root: Path, destination: Path) -> dict:
    root = root.resolve()
    destination = destination.resolve()
    if destination.is_relative_to(root) or destination.exists():
        raise ValueError('Indiquer un nouveau repertoire temporaire hors du depot.')
    api = root / 'control-plane'
    sources = {name: api / name for name in BASE_FILES}
    for folder, pattern in (('app', '*.py'), ('tests', 'test_*.py')):
        # Keep archive-style paths on every controller.  ``str(Path)`` uses
        # backslashes on Windows, which made the required-file check fail
        # before the otherwise platform-independent fixture suite could run.
        sources.update({path.relative_to(api).as_posix(): path for path in (api / folder).glob(pattern)})
    # Les sources canoniques remplacent seulement les copies dans le nouveau contexte.
    sources.update({target: root / source for source, target in SHARED.items()})
    for required in ('app/__init__.py', 'app/main.py', 'tests/test_api.py'):
        if required not in sources:
            raise ValueError('Source API absente : ' + required)
    for relative, source in sources.items():
        if not source.is_file() or source.is_symlink():
            raise ValueError('Source absente ou lien symbolique : ' + str(source))
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.moodle-api-', dir=destination.parent) as temporary:
        staging = Path(temporary) / 'context'
        staging.mkdir()
        for relative, source in sources.items():
            target = staging / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
        if destination.exists():
            raise ValueError('Le repertoire cible est apparu pendant la preparation.')
        os.rename(staging, destination)
    return {
        'status': 'prepared', 'context_directory': str(destination),
        'shared_sources': {source: {'target': target, 'sha256': hashlib.sha256(
            (destination / target).read_bytes()).hexdigest()} for source, target in SHARED.items()},
    }


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path, required=True)
    args = parser.parse_args()
    try:
        result = prepare(Path(__file__).resolve().parents[1], args.output_dir)
    except (OSError, ValueError) as exc:
        parser.exit(1, 'Preparation API arretee : ' + str(exc) + '\n')
    print(json.dumps(result, indent=2, ensure_ascii=False))
