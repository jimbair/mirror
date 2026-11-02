#!/usr/bin/env python3
"""Run torrent collection in disposable directories with rsync stubbed."""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parent.parent / 'debian-torrents-fetch.sh'


class TestDebianTorrentCollection(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.work = self.root / 'work'
        self.work.mkdir()
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        self.fixture = self.root / 'fixture'
        self.files = {'amd64/bt-cd/demo.iso.torrent': b'first torrent',
                      'arm64/bt-dvd/demo two.iso.torrent': b'second torrent'}
        for name, content in self.files.items():
            path = self.fixture / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        self.calls = self.root / 'calls'
        self._stub('rsync', '''printf 'rsync\\n' >> "$FETCH_TEST_CALLS"
cp -R "$FETCH_TEST_FIXTURE/." torrents_tree/ || exit 99
exit "$FETCH_TEST_RSYNC_RESULT"
''')

    def _stub(self, name, body):
        command = self.bin / name
        command.write_text('#!/bin/sh\n' + body)
        command.chmod(0o755)

    def _run(self, rsync_result=0):
        env = dict(os.environ, PATH=str(self.bin) + os.pathsep + os.defpath,
                   FETCH_TEST_CALLS=str(self.calls),
                   FETCH_TEST_FIXTURE=str(self.fixture),
                   FETCH_TEST_RSYNC_RESULT=str(rsync_result))
        return subprocess.run(['bash', str(SCRIPT)], cwd=self.work, env=env,
                              capture_output=True, text=True, timeout=5)

    def _assert_tree_retained(self):
        for name, content in self.files.items():
            self.assertEqual((self.work / 'torrents_tree' / name).read_bytes(), content)

    def test_success_collects_nested_torrents_and_removes_the_tree(self):
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        for name, content in self.files.items():
            self.assertEqual((self.work / 'torrents' / Path(name).name).read_bytes(), content)
        self.assertFalse((self.work / 'torrents_tree').exists())

    def test_failed_directory_setup_aborts_before_rsync(self):
        destination = self.work / 'torrents'
        destination.write_text('a file blocks directory creation')
        result = self._run()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.calls.exists())
        self.assertEqual(destination.read_text(), 'a file blocks directory creation')

    def test_failed_move_reports_failure_and_keeps_downloaded_files(self):
        self._stub('mv', "printf 'move failed\\n' >&2\nexit 7\n")
        result = self._run()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('move failed', result.stderr)
        self._assert_tree_retained()

    def test_failed_rsync_keeps_partial_downloads(self):
        result = self._run(rsync_result=9)
        self.assertNotEqual(result.returncode, 0)
        self._assert_tree_retained()
        self.assertEqual(list((self.work / 'torrents').iterdir()), [])


if __name__ == '__main__':
    unittest.main()
