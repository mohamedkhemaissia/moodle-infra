"""Executer les suites du projet et produire un bilan CI sans utiliser les donnees du pilote."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

from prepare_api import prepare

COMPONENTS = ('ci', 'ml', 'control-plane', 'diagnostics', 'remediation', 'scripts')


def run_suites(root: Path, context: Path, output: Path | None = None, preparation: dict | None = None) -> int:
    root = root.resolve()
    sys.path[:0] = [str(context), str(root), str(root / 'ci')]
    # Les tests de commandes lancent aussi Python dans des sous-processus.
    os.environ['PYTHONPATH'] = os.pathsep.join(
        [str(context), str(root), os.environ.get('PYTHONPATH', '')]
    ).rstrip(os.pathsep)
    os.chdir(root)
    report = {'evaluated_at': datetime.now(timezone.utc).isoformat(),
              'scope': 'tests_with_fixtures', 'api_preparation': preparation,
              'components': {}, 'status': 'passed'}
    failed = False
    for component in COMPONENTS:
        directory = (context if component == 'control-plane' else root / component) / 'tests'
        print(f'\nTests : {component}', flush=True)
        if not directory.is_dir() or not list(directory.glob('test_*.py')):
            report['components'][component] = {'status': 'failed', 'reason': 'test_suite_missing'}
            failed = True
            print('Suite absente : ' + str(directory), file=sys.stderr)
            continue
        try:
            suite = unittest.TestLoader().discover(str(directory), pattern='test_*.py')
            if suite.countTestCases() == 0:
                raise ValueError('Aucun test decouvert.')
            result = unittest.TextTestRunner(verbosity=2).run(suite)
            passed = result.wasSuccessful() and result.testsRun > 0
            failed = failed or not passed
            report['components'][component] = {
                'status': 'passed' if passed else 'failed', 'tests_run': result.testsRun,
                'failures': len(result.failures), 'errors': len(result.errors),
                'skipped': len(result.skipped),
            }
        except (ImportError, ValueError) as exc:
            failed = True
            report['components'][component] = {'status': 'failed', 'reason': str(exc)}
            print(str(exc), file=sys.stderr)
    report['status'] = 'failed' if failed else 'passed'
    report['total_tests_run'] = sum(item.get('tests_run', 0) for item in report['components'].values())
    encoded = json.dumps(report, indent=2, ensure_ascii=False) + '\n'
    print(encoded, flush=True)
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(encoded, encoding='utf-8')
    return 1 if failed else 0


def run(root: Path, output: Path | None = None) -> int:
    with tempfile.TemporaryDirectory(prefix='moodle-ci-api-') as temporary:
        context = Path(temporary) / 'context'
        preparation = prepare(root, context)
        return run_suites(root, context, output, preparation)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    sys.exit(run(Path(__file__).resolve().parents[1], args.output))
