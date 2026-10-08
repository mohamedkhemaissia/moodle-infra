import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from prepare_api import BASE_FILES, SHARED, prepare


class ContextTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.repo = self.base / 'repo'
        self.output = self.base / 'context'
        self.api = self.repo / 'control-plane'
        for name in BASE_FILES:
            self.write(self.api / name, 'base fixture\n')
        self.write(self.api / 'app/__init__.py', '')
        self.write(self.api / 'app/main.py', 'from .isolation_model import VALUE\n')
        self.write(self.api / 'tests/test_api.py', 'import unittest\n')
        for name in SHARED:
            self.write(self.repo / name, 'VALUE = 42\n')

    def write(self, path, text):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def test_clean_checkout_is_assembled_with_importable_shared_modules(self):
        report = prepare(self.repo, self.output)
        self.assertEqual(report['status'], 'prepared')
        for source, target in SHARED.items():
            self.assertEqual((self.output / target).read_bytes(), (self.repo / source).read_bytes())
            self.assertFalse((self.api / target).exists())
        spec = importlib.util.spec_from_file_location('api_context_fixture', self.output / 'app/__init__.py',
                                                     submodule_search_locations=[str(self.output / 'app')])
        import sys
        package = importlib.util.module_from_spec(spec)
        sys.modules['api_context_fixture'] = package
        self.addCleanup(sys.modules.pop, 'api_context_fixture', None)
        spec.loader.exec_module(package)
        spec = importlib.util.spec_from_file_location('api_context_fixture.main', self.output / 'app/main.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertEqual(module.VALUE, 42)
        self.addCleanup(sys.modules.pop, 'api_context_fixture.isolation_model', None)

    def test_local_generated_copies_cannot_override_canonical_sources(self):
        for target in SHARED.values():
            self.write(self.api / target, 'STALE = True\n')
        prepare(self.repo, self.output)
        for source, target in SHARED.items():
            self.assertEqual((self.output / target).read_bytes(), (self.repo / source).read_bytes())
            self.assertEqual((self.api / target).read_text(), 'STALE = True\n')

    def test_missing_source_fails_before_publishing_a_context(self):
        (self.repo / 'diagnostics/store.py').unlink()
        with self.assertRaises(ValueError):
            prepare(self.repo, self.output)
        self.assertFalse(self.output.exists())

    def test_existing_destination_and_repository_paths_are_preserved(self):
        self.output.mkdir()
        self.write(self.output / 'old-work.txt', 'preserve\n')
        for destination in (self.output, self.api, self.repo / 'another-folder'):
            with self.assertRaises(ValueError):
                prepare(self.repo, destination)
        self.assertEqual((self.output / 'old-work.txt').read_text(), 'preserve\n')

    def test_interrupted_copy_does_not_publish_a_partial_context(self):
        with patch('prepare_api.shutil.copyfile', side_effect=OSError('simulated failure')):
            with self.assertRaises(OSError):
                prepare(self.repo, self.output)
        self.assertFalse(self.output.exists())
        self.assertFalse(list(self.base.glob('.moodle-api-*')))


if __name__ == '__main__':
    unittest.main()
