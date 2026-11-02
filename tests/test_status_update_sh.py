#!/usr/bin/env python3
"""Exercise the active shell builder's publication with stub collectors."""

import subprocess
import tempfile
import time
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parent.parent / 'status-update.sh'


class TestStatusPublication(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.status = self.root / 'status.txt'
        self.status.write_text('old complete snapshot\n')

    def _command(self, body):
        return ['bash', '-c', 'source "$1"\nSTATUS_FILE=$2\nREADY=$3\nCONTINUE=$4\n' + body,
                'status-update-test', str(SCRIPT), str(self.status),
                str(self.root / 'ready'), str(self.root / 'continue')]

    def _run(self, body):
        return subprocess.run(self._command(body), capture_output=True,
                              text=True, timeout=5)

    def test_replacement_preserves_an_open_readers_complete_snapshot(self):
        with self.status.open() as reader:
            result = self._run('build_status() { printf "new complete snapshot\\n"; }\nmain')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(reader.read(), 'old complete snapshot\n')
        self.assertEqual(self.status.read_text(), 'new complete snapshot\n')
        self.assertEqual(self.status.stat().st_mode & 0o777, 0o644)
        self.assertEqual(list(self.root.glob('status.txt.tmp.*')), [])

    def test_failed_collection_leaves_previous_snapshot(self):
        result = self._run('build_status() { printf "partial"; return 1; }\nmain')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.status.read_text(), 'old complete snapshot\n')
        self.assertEqual(list(self.root.glob('status.txt.tmp.*')), [])

    def test_failed_rename_leaves_previous_snapshot_and_cleans_temp(self):
        result = self._run('build_status() { printf "new"; }\n'
                           'mv() { return 1; }\nmain')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.status.read_text(), 'old complete snapshot\n')
        self.assertEqual(list(self.root.glob('status.txt.tmp.*')), [])

    def test_readers_cannot_see_a_snapshot_while_it_is_being_built(self):
        body = '''build_status() {
    printf 'first half'
    touch "$READY"
    while [[ ! -e "$CONTINUE" ]]; do sleep 0.02; done
    printf ' second half\\n'
}
main
'''
        # Markers coordinate the real reader/writer overlap without trying
        # to win a race against a short-lived copy operation.
        process = subprocess.Popen(self._command(body), stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.monotonic() + 3
            while not (self.root / 'ready').exists():
                if process.poll() is not None or time.monotonic() >= deadline:
                    self.fail('collector did not reach the partial-write marker')
                time.sleep(0.01)
            self.assertEqual(self.status.read_text(), 'old complete snapshot\n')
            (self.root / 'continue').touch()
            _, stderr = process.communicate(timeout=3)
            self.assertEqual(process.returncode, 0, stderr)
            self.assertEqual(self.status.read_text(), 'first half second half\n')
        finally:
            if process.poll() is None:
                process.terminate()
            process.communicate(timeout=3)


if __name__ == '__main__':
    unittest.main()
