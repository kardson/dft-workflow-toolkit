"""Exercise the assembled public entrypoints without private configuration."""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

TOOLS = Path(__file__).resolve().parents[1] / 'tools'


class GenericCLIIntegration(unittest.TestCase):
    def invoke(self, *args, tools=TOOLS):
        return subprocess.run([sys.executable, '-E', '-s', '-B', str(tools / 'vasp_core_cli.py'),
                               *args], capture_output=True, text=True, timeout=30)

    def test_catalog_and_help_match_installed_commands(self):
        result = self.invoke('capabilities')
        self.assertEqual(result.returncode, 0, result.stderr)
        record = json.loads(result.stdout)
        expected = {'validate-bundle', 'preflight', 'postcheck', 'evidence', 'capabilities'}
        self.assertEqual({c['name'] for c in record['commands']}, expected)
        self.assertEqual(record['unavailable_commands'], [])
        for entry in record['commands']:
            self.assertEqual(entry['scientific_acceptance'], 'NOT_AUTHORIZED')
            self.assertFalse(entry['effects']['remote_compute_control'])
        self.assertEqual(self.invoke('--help').returncode, 0)

    def test_private_operation_is_not_exposed(self):
        result = self.invoke('prepare-bundle')
        self.assertEqual(result.returncode, 2)
        self.assertIn('invalid choice', result.stderr)

    def test_workspace_escape_is_rejected_before_reading(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self.invoke('--workspace', directory, 'preflight',
                                 '--manifest', '../outside.json')
            self.assertEqual(result.returncode, 2)
            self.assertIn('inside the workspace', result.stderr)
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_missing_dependency_is_not_advertised_or_executed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for file in TOOLS.glob('*.py'):
                if file.name != 'vasp_contracts.py':
                    shutil.copyfile(file, root / file.name)
            result = self.invoke('capabilities', tools=root)
            self.assertEqual(result.returncode, 0, result.stderr)
            record = json.loads(result.stdout)
            unavailable = {c['name'] for c in record['unavailable_commands']}
            self.assertTrue({'preflight', 'postcheck'}.issubset(unavailable))
            self.assertEqual(self.invoke('preflight', tools=root).returncode, 2)


if __name__ == '__main__':
    unittest.main()
