"""Les registres de CI changent sans changer les images ni les preuves fonctionnelles."""
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import yaml

CI = Path(__file__).resolve().parents[1]
ROOT = CI.parent
sys.path.insert(0, str(CI))
from registry_sources import docker_arguments, image_reference, locked_images
from prepare_registry_context import prepare_context


class SourceTests(unittest.TestCase):
    def test_original_digest_and_version_are_preserved_for_mirrored_images(self):
        images = locked_images()
        self.assertEqual(len(images), 7)
        for original, alternate in images.items():
            with self.subTest(original=original):
                mapped = image_reference(original, source='ecr-public')
                self.assertEqual(mapped, alternate)
                self.assertRegex(mapped, r'@sha256:[0-9a-f]{64}$')
                if '@' in original:
                    self.assertEqual(original.split('@')[1], mapped.split('@')[1])
                self.assertEqual(original.split('@')[0].rsplit(':', 1)[1],
                                 mapped.split('@')[0].rsplit(':', 1)[1])

    def test_default_source_and_other_registries_are_unchanged(self):
        original = next(iter(locked_images()))
        self.assertEqual(image_reference(original, source='dockerhub'), original)
        for value in ('ghcr.io/example/exporter:1', 'quay.io/example/exporter:1',
                      'moodle-pfe/php:fixture'):
            self.assertEqual(image_reference(value, source='ecr-public'), value)

    def test_unknown_image_or_source_is_rejected_instead_of_using_latest(self):
        with self.assertRaises(ValueError):
            image_reference('postgres:latest', source='ecr-public')
        with self.assertRaises(ValueError):
            image_reference('postgres:16.15-bookworm', source='unknown')

    def test_docker_translation_changes_only_known_image_arguments(self):
        original = next(iter(locked_images()))
        arguments = ['/usr/bin/docker', 'image', 'inspect', '--format', '{{json .Id}}', original]
        with patch.dict(os.environ, {'CI_IMAGE_SOURCE': 'ecr-public'}):
            translated = docker_arguments(arguments)
            self.assertEqual(translated[:-1], arguments[:-1])
            self.assertEqual(translated[-1], locked_images()[original])
            self.assertEqual(arguments[-1], original)
            self.assertEqual(docker_arguments(['/usr/bin/restic', original]), ['/usr/bin/restic', original])
            self.assertEqual(docker_arguments(['docker', 'rm', '--force', 'fixture-container']),
                             ['docker', 'rm', '--force', 'fixture-container'])


class ContextTests(unittest.TestCase):
    def test_canonical_dockerfiles_and_context_payload_are_preserved(self):
        originals = {name: (ROOT / name / 'Dockerfile').read_bytes()
                     for name in ('control-plane', 'dashboard')}
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            root = temporary / 'repo'
            (root / 'dashboard').mkdir(parents=True)
            (root / 'dashboard/Dockerfile').write_bytes(originals['dashboard'])
            (root / 'dashboard/payload.txt').write_text('same source')
            destination = temporary / 'dashboard-context'
            result = prepare_context('dashboard', destination, root=root, source='ecr-public')
            self.assertEqual((destination / 'payload.txt').read_text(), 'same source')
            altered = (destination / 'Dockerfile').read_text()
            for pair in result['base_images']:
                self.assertIn(pair['alternate'], altered)
                altered = altered.replace(pair['alternate'], pair['original'])
            self.assertEqual(altered.encode(), originals['dashboard'])
            self.assertEqual((root / 'dashboard/Dockerfile').read_bytes(), originals['dashboard'])
            api = temporary / 'api-context'
            api.mkdir()
            (api / 'Dockerfile').write_bytes(originals['control-plane'])
            (api / 'shared.py').write_bytes(b'shared source')
            result = prepare_context('api', api, root=root, source='ecr-public')
            self.assertEqual((api / 'shared.py').read_bytes(), b'shared source')
            self.assertEqual(len(result['base_images']), 1)
            with self.assertRaises(ValueError):
                prepare_context('api', root / 'unsafe-context', root=root, source='ecr-public')
        for name, content in originals.items():
            self.assertEqual((ROOT / name / 'Dockerfile').read_bytes(), content)

    def test_workflow_uses_public_sources_without_a_docker_hub_login(self):
        text = (ROOT / '.github/workflows/ci.yml').read_text()
        workflow = yaml.safe_load(text)
        self.assertNotIn('docker login', text)
        self.assertNotIn('DOCKERHUB_TOKEN', text)
        services = workflow['jobs']['postgresql']['services']['postgres']
        self.assertTrue(services['image'].startswith('public.ecr.aws/docker/library/postgres:'))
        self.assertNotIn('credentials', services)
        self.assertEqual(services['ports'], ['15432:5432'])
        for job in ('recovery-tests', 'application-metrics', 'images'):
            self.assertEqual(workflow['jobs'][job]['env']['CI_IMAGE_SOURCE'], 'ecr-public')
        builder = next(step for step in workflow['jobs']['images']['steps']
                       if step.get('uses', '').startswith('docker/setup-buildx-action@'))
        self.assertEqual(builder['with']['driver'], 'docker')
        checks = [step.get('run', '') for step in workflow['jobs']['recovery-tests']['steps']]
        self.assertTrue(any('sudo env "CI_IMAGE_SOURCE=$CI_IMAGE_SOURCE"' in value and
                            'ci/run_recovery_moodle_test.py' in value for value in checks))
        self.assertFalse(any(step.get('continue-on-error') is True
                             for job in workflow['jobs'].values() for step in job.get('steps', [])))


if __name__ == '__main__':
    unittest.main()
