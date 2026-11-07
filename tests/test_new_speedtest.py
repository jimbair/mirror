#!/usr/bin/env python3
"""Exercise speedtest approval and atomic log publication with a stub CLI."""

import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parent.parent / 'new-speedtest.sh'
CANDIDATE = 'Server: test.example\r\nDownload: 900 Mbps\r\nUpload: 800 Mbps\r\n'
NORMALIZED = CANDIDATE.replace('\r', '')
PREVIOUS = 'previous complete speedtest\n'


class TestSpeedtestPublication(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='speedtest publication test ')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.log = self.root / 'speedtest.log'
        self.log.write_text(PREVIOUS)
        self.speedtest = self.root / 'speedtest'
        self.speedtest.write_text('''#!/bin/bash
printf '%s' "$SPEED_TEST_OUTPUT"
exit "$SPEED_TEST_STATUS"
''')
        self.speedtest.chmod(0o755)
        self.serial = 0

    def _command(self, body='main', log=None):
        self.serial += 1
        ready = self.root / f'ready-{self.serial}'
        command = '''source "$1"
SPEEDTEST_COMMAND=$2
SPEEDTEST_LOG=$3
READY=$4
read() { : > "$READY"; builtin read "$@"; }
''' + body
        return (['bash', '-c', command, 'speedtest-publication-test', str(SCRIPT),
                 str(self.speedtest), str(self.log if log is None else log), str(ready)], ready)

    def _env(self, output=CANDIDATE, status=0):
        return dict(os.environ, SPEED_TEST_OUTPUT=output, SPEED_TEST_STATUS=str(status))

    def _run(self, body='main', answer='y\n', output=CANDIDATE, status=0, log=None):
        command, ready = self._command(body, log)
        result = subprocess.run(command, env=self._env(output, status), input=answer,
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(list(self.root.glob('speedtest.log.tmp.*')), [])
        return result, ready

    def _assert_previous(self, result):
        self.assertNotEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.log.read_text(), PREVIOUS)

    def _start_at_prompt(self, output):
        command, ready = self._command()
        process = subprocess.Popen(command, env=self._env(output), stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(self._stop, process)
        deadline = time.monotonic() + 3
        while not ready.exists():
            if process.poll() is not None or time.monotonic() >= deadline:
                self.fail('speedtest did not reach its approval prompt')
            time.sleep(0.01)
        return process

    @staticmethod
    def _stop(process):
        if process.poll() is None:
            process.terminate()
        process.communicate(timeout=3)

    def test_accepted_result_is_normalized_and_replaced_atomically(self):
        with self.log.open() as reader:
            result, _ = self._run()
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(reader.read(), PREVIOUS)
        self.assertEqual(self.log.read_text(), NORMALIZED)
        self.assertEqual(self.log.stat().st_mode & 0o777, 0o644)
        self.assertIn('done!', result.stdout)
        self.assertIn('New:', result.stdout)

    def test_declining_or_empty_answer_keeps_the_previous_result(self):
        for answer in ('n\n', '\n'):
            with self.subTest(answer=answer):
                result, _ = self._run(answer=answer)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.log.read_text(), PREVIOUS)

    def test_uppercase_yes_is_accepted(self):
        result, _ = self._run(answer='YES\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.log.read_text(), NORMALIZED)

    def test_first_result_can_be_published_without_an_existing_log(self):
        self.log.unlink()
        result, _ = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('(no previous result)', result.stdout)
        self.assertEqual(self.log.read_text(), NORMALIZED)

    def test_failed_speedtest_preserves_the_log_and_skips_approval(self):
        result, ready = self._run(status=7)
        self._assert_previous(result)
        self.assertIn('Speedtest failed to run', result.stderr)
        self.assertFalse(ready.exists())

    def test_empty_speedtest_preserves_the_log_and_skips_approval(self):
        result, ready = self._run(output='')
        self._assert_previous(result)
        self.assertIn('Speedtest returned no output', result.stderr)
        self.assertFalse(ready.exists())

    def test_failed_temporary_setup_skips_approval(self):
        result, ready = self._run(log=self.root / 'missing' / 'speedtest.log')
        self._assert_previous(result)
        self.assertFalse(ready.exists())

    def test_failed_formatting_discards_partial_candidate(self):
        result, ready = self._run('sed() { printf partial; return 7; }\nmain')
        self._assert_previous(result)
        self.assertNotIn('done!', result.stdout)
        self.assertFalse(ready.exists())

    def test_failed_preview_preserves_the_previous_result(self):
        result, ready = self._run('cat() { return 7; }\nmain')
        self._assert_previous(result)
        self.assertFalse(ready.exists())

    def test_failed_permission_setup_does_not_attempt_publication(self):
        result, _ = self._run('''chmod() { return 7; }
mv() { printf 'RENAME_REACHED\\n'; }
main''')
        self._assert_previous(result)
        self.assertNotIn('RENAME_REACHED', result.stdout)

    def test_failed_publication_preserves_the_previous_result(self):
        result, _ = self._run('mv() { return 7; }\nmain')
        self._assert_previous(result)

    def test_eof_preserves_the_previous_result_and_cleans_the_candidate(self):
        result, _ = self._run(answer='')
        self._assert_previous(result)

    def test_interruption_at_prompt_preserves_the_log_and_cleans_the_candidate(self):
        process = self._start_at_prompt(CANDIDATE)
        self.assertEqual(len(list(self.root.glob('speedtest.log.tmp.*'))), 1)
        process.terminate()
        _, stderr = process.communicate(timeout=3)
        self.assertEqual(process.returncode, 143, stderr)
        self.assertEqual(self.log.read_text(), PREVIOUS)
        self.assertEqual(list(self.root.glob('speedtest.log.tmp.*')), [])

    def test_overlapping_previews_publish_only_the_accepted_sessions_result(self):
        first = self._start_at_prompt('result-A\n')
        second = self._start_at_prompt('result-B\n')
        self.assertEqual(len(list(self.root.glob('speedtest.log.tmp.*'))), 2)
        self.assertEqual(self.log.read_text(), PREVIOUS)
        _, first_error = first.communicate('y\n', timeout=3)
        self.assertEqual(first.returncode, 0, first_error)
        self.assertEqual(self.log.read_text(), 'result-A\n')
        self.assertEqual(len(list(self.root.glob('speedtest.log.tmp.*'))), 1)
        _, second_error = second.communicate('n\n', timeout=3)
        self.assertEqual(second.returncode, 0, second_error)
        self.assertEqual(self.log.read_text(), 'result-A\n')
        self.assertEqual(list(self.root.glob('speedtest.log.tmp.*')), [])

    def test_declining_one_session_does_not_remove_another_sessions_candidate(self):
        first = self._start_at_prompt('result-A\n')
        second = self._start_at_prompt('result-B\n')
        _, first_error = first.communicate('n\n', timeout=3)
        self.assertEqual(first.returncode, 0, first_error)
        self.assertEqual(self.log.read_text(), PREVIOUS)
        self.assertEqual(len(list(self.root.glob('speedtest.log.tmp.*'))), 1)
        _, second_error = second.communicate('y\n', timeout=3)
        self.assertEqual(second.returncode, 0, second_error)
        self.assertEqual(self.log.read_text(), 'result-B\n')
        self.assertEqual(list(self.root.glob('speedtest.log.tmp.*')), [])


if __name__ == '__main__':
    unittest.main()
