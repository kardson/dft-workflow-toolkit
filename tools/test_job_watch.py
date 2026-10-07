"""Synthetic tests only: no VASP, SSH, notification service, or production edits."""
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from job_watch import supervise


class WatchTests(unittest.TestCase):
    def run_fixture(self, exit_code=0, normal=True, kind='static', notifier=None):
        temp = tempfile.TemporaryDirectory(
            dir=Path(__file__).resolve().parents[2], prefix='.vasp-test-')
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        case = root / 'case'
        case.mkdir()
        for name in ('OSZICAR', 'vasprun.xml', 'CONTCAR'):
            (case / name).write_text('synthetic')
        (case / 'OUTCAR').write_text('aborting loop because EDIFF is reached\n' +
            ('General timing and accounting informations\n' if normal else ''))
        (case / 'run_timing.txt').write_text('exit_code=0\n')
        config = root / 'watch.json'
        config.write_text(json.dumps({'command': [sys.executable, '-c', f'raise SystemExit({exit_code})'],
            'cases': [{'path': 'case', 'kind': kind}], 'notify_argv': notifier or []}))
        with contextlib.redirect_stdout(io.StringIO()):
            code = supervise(config, require_tmux=False)
        state = json.loads((root / '.job_watch/status.json').read_text())
        return config, code, state

    def test_success_and_duplicate_refusal(self):
        config, code, state = self.run_fixture()
        self.assertEqual(code, 0)
        self.assertEqual(state['notification']['status'], 'FILE_ONLY')
        with self.assertRaises(FileExistsError):
            supervise(config, require_tmux=False)

    def test_runner_failure(self):
        self.assertEqual(self.run_fixture(exit_code=7)[1], 1)

    def test_truncated_output(self):
        self.assertEqual(self.run_fixture(normal=False)[1], 1)

    def test_relaxation_not_converged(self):
        self.assertEqual(self.run_fixture(kind='relaxation')[1], 1)

    def test_notification_failure_preserves_result(self):
        _, code, state = self.run_fixture(notifier=[sys.executable, '-c', 'raise SystemExit(3)'])
        self.assertEqual(code, 0)
        self.assertEqual(state['notification']['status'], 'FAILED')
        self.assertIsNone(state['notification']['user_acknowledged'])

    def test_zero_exit_adapter_is_not_user_acknowledgement(self):
        _, code, state = self.run_fixture(notifier=[sys.executable, '-c', 'raise SystemExit(0)'])
        self.assertEqual(code, 0)
        self.assertEqual(state['notification']['status'], 'ADAPTER_RETURNED_ZERO')
        self.assertIsNone(state['notification']['user_acknowledged'])

    def test_execution_receipt_has_identity_timestamps_and_stage_durations(self):
        _, code, state = self.run_fixture()
        self.assertEqual(code, 0)
        self.assertTrue(state['task_id'])
        self.assertTrue(state['execution_id'])
        self.assertTrue(state['started_utc'].endswith('Z'))
        self.assertTrue(state['ended_utc'].endswith('Z'))
        self.assertTrue(state['updated_utc'].endswith('Z'))
        self.assertEqual(state['record_written_utc'], state['updated_utc'])
        self.assertLessEqual(state['state_changed_utc'], state['record_written_utc'])
        self.assertEqual(state['notification_event_id'],
                         f"{state['execution_id']}:{state['status']}")
        self.assertGreaterEqual(state['stage_seconds']['preflight'], 0)
        self.assertGreaterEqual(state['stage_seconds']['runner'], 0)
        self.assertGreaterEqual(state['stage_seconds']['postcheck'], 0)
        self.assertEqual(state['scientific_acceptance'], 'PENDING_USER_AND_SOL_REVIEW')


if __name__ == '__main__':
    unittest.main()
