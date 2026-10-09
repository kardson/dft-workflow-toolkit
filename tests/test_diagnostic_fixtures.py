"""Public synthetic fixtures verify branches, never real detector acceptance."""
from importlib import metadata
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
from diagnostic_taxonomy import diagnose_brmix
FIXTURES = ROOT / 'tests/fixtures/diagnostics'


class SyntheticDiagnostics(unittest.TestCase):
    def test_xml_parser_examples_only(self):
        ET.fromstring((FIXTURES / 'synthetic-complete-xml.txt').read_text())
        for name, codes in [('truncated', {3, 5, 6}), ('syntax', {7})]:
            with self.assertRaises(ET.ParseError) as failure:
                ET.fromstring((FIXTURES / f'synthetic-{name}-xml.txt').read_text())
            self.assertIn(failure.exception.code, codes)

    def test_missing_input_is_evidence_gap(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copyfile(FIXTURES / 'synthetic-missing-input-stdout.txt', root / 'vasp.stdout')
            self.assertEqual(diagnose_brmix(root)['state'], 'INSUFFICIENT_EVIDENCE')

    def test_recorded_custodian_positive_and_suppression(self):
        try:
            version = metadata.version('custodian')
        except metadata.PackageNotFoundError:
            self.skipTest('Optional recorded Custodian environment is unavailable')
        if version != '2025.12.14':
            self.skipTest('This synthetic check is scoped to Custodian 2025.12.14')
        from custodian.vasp.handlers import VaspErrorHandler
        from custodian.custodian import Custodian
        for fixture, state in [('positive', 'DETECTED'), ('nelect_suppression', 'SUPPRESSED')]:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                for label, name in [('input', 'INCAR'), ('stdout', 'vasp.stdout')]:
                    shutil.copyfile(FIXTURES / f'synthetic-{fixture}-{label}.txt', root / name)
                before = {p.name: p.read_bytes() for p in root.iterdir()}
                with patch.object(VaspErrorHandler, 'correct', side_effect=AssertionError), \
                     patch.object(Custodian, 'run', side_effect=AssertionError), \
                     patch('subprocess.Popen', side_effect=AssertionError), \
                     patch('socket.socket', side_effect=AssertionError):
                    finding = diagnose_brmix(root)
                self.assertEqual(finding['state'], state)
                self.assertEqual(before, {p.name: p.read_bytes() for p in root.iterdir()})


if __name__ == '__main__':
    unittest.main()
