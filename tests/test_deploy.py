import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location('configure', ROOT / 'scripts/configure.py')
configure = importlib.util.module_from_spec(spec)
spec.loader.exec_module(configure)


class DeploymentTests(unittest.TestCase):
    def values(self):
        return dict(DOMAIN='bot.test.org', ACME_EMAIL='owner@test.org', BOT_TOKEN='123:fake',
                    ADMIN_IDS='42, 43', ROLLYPAY_API_KEY='fake-key',
                    ROLLYPAY_SIGNING_SECRET='fake-secret', ROLLYPAY_TEST='true')

    def test_validate_settings_without_printing_values(self):
        self.assertEqual(configure.validate(self.values()), [])
        for key, value in [('DOMAIN', 'https://bot.test.org'), ('ADMIN_IDS', '-1'),
                           ('ROLLYPAY_TEST', 'yes'), ('ROLLYPAY_API_KEY', "bad'quote")]:
            values = self.values()
            values[key] = value
            self.assertIn(key, configure.validate(values))

    def test_literal_secret_roundtrip(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'settings'
            path.write_text("ROLLYPAY_SIGNING_SECRET='fake$literal#secret'\n")
            self.assertEqual(configure.read_config(path)['ROLLYPAY_SIGNING_SECRET'], 'fake$literal#secret')

    def run_deploy(self, fail_build=False, existing=False):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'scripts').mkdir()
            (root / 'bin').mkdir()
            shutil.copy(ROOT / 'scripts/deploy.sh', root / 'scripts/deploy.sh')
            shutil.copy(ROOT / 'scripts/configure.py', root / 'scripts/configure.py')
            (root / '.env.production').write_text(''.join(f"{key}='{value}'\n" for key, value in self.values().items()))
            scripts = {
                'bin/uname': '#!/bin/sh\necho Linux\n',
                'bin/flock': '#!/bin/sh\nexit 0\n',
                'bin/docker': '#!/bin/sh\nif [ "$1" = inspect ]; then echo true; fi\n',
                'scripts/backup.sh': '#!/bin/sh\necho backup >> trace\n',
                'scripts/production.sh': '#!/bin/sh\necho "$*" >> trace\n'
                    + ('if [ "$1" = ps ] && [ "$2" = -a ]; then echo fake-container; fi\n' if existing else '')
                    + ('if [ "$1" = build ]; then exit 1; fi\n' if fail_build else '') + 'exit 0\n',
            }
            for name, content in scripts.items():
                path = root / name
                path.write_text(content)
                path.chmod(0o755)
            result = subprocess.run(['sh', 'scripts/deploy.sh'], cwd=root,
                env={**os.environ, 'PATH': str(root / 'bin') + os.pathsep + os.environ['PATH']},
                capture_output=True, text=True)
            trace = (root / 'trace').read_text()
            self.assertNotIn('fake-secret', result.stdout + result.stderr)
            return result, trace

    def test_first_deployment(self):
        result, trace = self.run_deploy()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('up -d --wait', trace)
        self.assertNotIn('backup\n', trace)

    def test_backup_precedes_build_and_start(self):
        result, trace = self.run_deploy(existing=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertLess(trace.index('backup\n'), trace.index('build --pull'))
        self.assertLess(trace.index('build --pull'), trace.index('up -d'))

    def test_build_failure_does_not_restart_service(self):
        result, trace = self.run_deploy(fail_build=True, existing=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('backup\n', trace)
        self.assertNotIn('up -d', trace)
