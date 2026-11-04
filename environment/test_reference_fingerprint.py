"""Environment content fingerprint regressions; all mutations stay in temporary files."""
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from environment.data import Dataset, ROOT as ENVIRONMENT_ROOT
from environment.service import Environment
from environment.store import MemoryStore

PROJECT_ROOT = ENVIRONMENT_ROOT.parent


class ReferenceFingerprintTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='esa-fingerprint-tests-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.environment_root = self.root / 'environment'
        self.environment_root.mkdir()
        for source in ENVIRONMENT_ROOT.glob('*.py'):
            if not source.name.startswith('test_'):
                shutil.copyfile(source, self.environment_root / source.name)
        for filename in ('catalog.json', 'catalog-schema.json', 'seed-schema.json'):
            shutil.copyfile(ENVIRONMENT_ROOT / filename, self.environment_root / filename)
        self.config = self.root / 'configs/tools.json'
        self.config.parent.mkdir()
        shutil.copyfile(PROJECT_ROOT / 'configs/tools.json', self.config)
        self.dataset = Dataset()
        self.addCleanup(self.dataset.temp.cleanup)

    def build(self, dataset=None):
        with patch('environment.service.ROOT', self.environment_root):
            return Environment(MemoryStore(test_only=True), dataset or self.dataset,
                               config_path=self.config, workspace_root=self.root / 'workspaces')

    def test_equal_contents_have_equal_fingerprint(self):
        first, second = self.build(), self.build()
        self.assertEqual(first.implementation_sha256, second.implementation_sha256)
        self.assertEqual(first.source_sha256, second.source_sha256)
        self.assertEqual(first.manifest(), second.manifest())
        self.assertRegex(first.implementation_sha256, r'^[0-9a-f]{64}$')

    def test_handler_source_bytes_change_implementation_fingerprint(self):
        original = self.build()
        path = self.environment_root / 'handlers.py'
        path.write_bytes(path.read_bytes() + b'\n# Fingerprint source change.\n')
        changed = self.build()
        self.assertNotEqual(original.implementation_sha256, changed.implementation_sha256)
        self.assertEqual(original.source_sha256, changed.source_sha256)
        self.assertEqual(original.dataset.seed_sha256, changed.dataset.seed_sha256)

    def test_server_and_session_source_are_covered(self):
        for filename in ('server.py', 'service.py', 'store.py', 'data.py', 'compat.py'):
            with self.subTest(source=filename):
                original = self.build()
                path = self.environment_root / filename
                path.write_bytes(path.read_bytes() + b'\n# Covered source content.\n')
                self.assertNotEqual(original.implementation_sha256, self.build().implementation_sha256)

    def test_catalog_change_updates_catalog_and_implementation_hash(self):
        original = self.build()
        path = self.environment_root / 'catalog.json'
        catalog = json.loads(path.read_text())
        catalog['tools'][0]['description'] += ' Deterministic local contract.'
        path.write_text(json.dumps(catalog, ensure_ascii=False))
        changed = self.build()
        self.assertNotEqual(original.source_sha256, changed.source_sha256)
        self.assertNotEqual(original.implementation_sha256, changed.implementation_sha256)
        self.assertEqual(changed.source_sha256, hashlib.sha256(path.read_bytes()).hexdigest())

    def test_tool_enable_configuration_is_covered(self):
        original = self.build()
        config = json.loads(self.config.read_text())
        entry = next(item for item in config['tools'] if item['name'] == 'get_joke')
        entry['enabled'] = not entry['enabled']
        self.config.write_text(json.dumps(config, ensure_ascii=False))
        changed = self.build()
        self.assertNotEqual(original.implementation_sha256, changed.implementation_sha256)
        self.assertEqual(original.source_sha256, changed.source_sha256)
        self.assertNotEqual(original.enabled, changed.enabled)

    def test_schema_source_is_covered(self):
        original = self.build()
        path = self.environment_root / 'seed-schema.json'
        schema = json.loads(path.read_text())
        schema['description'] = 'Temporary fingerprint variation.'
        path.write_text(json.dumps(schema))
        self.assertNotEqual(original.implementation_sha256, self.build().implementation_sha256)

    def test_test_files_do_not_change_runtime_implementation_hash(self):
        original = self.build()
        (self.environment_root / 'test_new_reference.py').write_text('def test_example():\n    pass\n')
        self.assertEqual(original.implementation_sha256, self.build().implementation_sha256)

    def test_seed_changes_have_separate_hash_with_same_version(self):
        original = self.build()
        seed = json.loads((ENVIRONMENT_ROOT / 'seed-v1.json').read_text())
        seed['weather'][0]['condition'] += '，测试资料变化'
        path = self.root / 'changed-seed.json'
        path.write_text(json.dumps(seed, ensure_ascii=False))
        dataset = Dataset(seed_path=path)
        self.addCleanup(dataset.temp.cleanup)
        changed = self.build(dataset)
        self.assertEqual(original.dataset.version, changed.dataset.version)
        self.assertNotEqual(original.dataset.seed_sha256, changed.dataset.seed_sha256)
        self.assertEqual(original.implementation_sha256, changed.implementation_sha256)
        self.assertNotEqual(original.manifest()['seed_sha256'], changed.manifest()['seed_sha256'])

    def test_snapshot_and_manifest_report_runtime_fingerprint(self):
        environment = self.build()
        created = environment.create({'tool_profile': ['get_vehicle_status']})
        try:
            snapshot = environment.snapshot(created['session_id'])
            self.assertEqual(snapshot['implementation_sha256'], environment.implementation_sha256)
            self.assertEqual(snapshot['manifest']['implementation_sha256'], environment.implementation_sha256)
            self.assertEqual(snapshot['seed_sha256'], self.dataset.seed_sha256)
            self.assertEqual(snapshot['source_sha256'], environment.source_sha256)
        finally:
            environment.store.delete(created['session_id'])
        self.assertEqual(environment.store.records, {})


if __name__ == '__main__':
    unittest.main()
