#!/usr/bin/env python3
"""Exercise HTML fragments and publication without production collectors."""

import html
import subprocess
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parent.parent / 'mirror-index.sh'
HEADER = 'ID   Done   Have      ETA      Up   Down  Ratio  Status       Name'
ROWS = [
    '1    100%   2.26 GB   Done     0.0  0.0   0.11   Idle         arch.iso',
    '2    44%    1.54 GB   10 mins  0.0  0.0   3.14   Downloading  fedora.iso',
    '3    100%   938.9 MB  Unknown  0.0  0.0   2,328  Idle         alma.iso',
    '4    100%   4.12 GB   2 hrs    0.0  0.0   49.20  Seeding      ubuntu.iso',
    '5    0%     None      Unknown  0.0  0.0   Inf    Stopped      <script> & test.iso',
]
SUMMARY = 'Sum:        9.0 GB              0.0  0.0'


class TestMirrorIndex(unittest.TestCase):
    def setUp(self):
        # Spaces also exercise quoting on every temporary/input/output path.
        self.tmp = tempfile.TemporaryDirectory(prefix='mirror index test ')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.theme = self.root / '.theme'
        self.theme.mkdir()
        self.header = self.theme / 'header.html'
        self.footer = self.theme / 'footer.html'
        self.header.write_text('old complete header\n')
        self.footer.write_text('old complete footer\n')
        self.status = self.root / 'status.txt'
        self.status.write_text('checker ground truth\n')
        self.speedtest = self.root / 'speedtest.log'
        self.speedtest.write_text('Speed <fast> & stable\n')
        self.remote = self.root / 'transmission.txt'
        self.remote.write_text('\n'.join([HEADER, *ROWS, SUMMARY]) + '\n')

    def _run(self, body='main'):
        # Source the actual script, then substitute collectors with functions.
        # All filesystem publication still uses the real main() and utilities.
        command = '''source "$1"
WEBROOT=$2
SPEEDTEST_LOG=$3
REMOTE_FILE=$4
TRANSMISSION_REMOTE=remote
VNSTAT=vnstat
IPV6_STATS=ipv6_stats
remote() { cat "$REMOTE_FILE"; }
vnstat() { printf 'Transit <total> & daily\\n'; }
ipv6_stats() { printf 'IPv6 <share> & interface\\n'; }
''' + body
        result = subprocess.run(
            ['bash', '-c', command, 'mirror-index-test', str(SCRIPT),
             str(self.root), str(self.speedtest), str(self.remote)],
            capture_output=True, text=True, timeout=5)
        self.assertEqual(self.status.read_text(), 'checker ground truth\n')
        self.assertEqual(list(self.theme.glob('*.tmp.*')), [])
        return result

    def _assert_unchanged(self, result):
        self.assertNotEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.header.read_text(), 'old complete header\n')
        self.assertEqual(self.footer.read_text(), 'old complete footer\n')

    def test_builds_escaped_fragments_with_the_existing_ratio_order(self):
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        header = self.header.read_text()
        self.assertIn('<h1>mirror.tsue.net</h1>', header)
        self.assertRegex(header, r'Currently seeding\s+5 torrents, 9\.0 GB total')
        footer = self.footer.read_text()
        self.assertIn('Speed &lt;fast&gt; &amp; stable', footer)
        self.assertIn('Transit &lt;total&gt; &amp; daily', footer)
        self.assertIn('IPv6 &lt;share&gt; &amp; interface', footer)
        self.assertNotIn('<script>', footer)
        expected_table = '\n'.join(html.escape(line, quote=False) for line in [
            HEADER, ROWS[4], ROWS[2], ROWS[3], ROWS[1], ROWS[0], SUMMARY,
        ])
        self.assertIn(expected_table, footer)
        for path in (self.header, self.footer):
            self.assertEqual(path.stat().st_mode & 0o777, 0o644)

    def test_zero_torrents_is_a_successful_snapshot(self):
        self.remote.write_text(HEADER + '\nSum: 0 B 0.0 0.0\n')
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertRegex(self.header.read_text(), r'Currently seeding\s+0 torrents, 0 B total')

    def test_failed_transmission_with_nonempty_stdout_preserves_both_fragments(self):
        self._assert_unchanged(self._run(
            'remote() { printf "Transmission unavailable\\n"; return 17; }\nmain'))

    def test_empty_transmission_preserves_both_fragments(self):
        self.remote.write_text('')
        self._assert_unchanged(self._run())

    def test_missing_speedtest_preserves_both_fragments(self):
        self.speedtest.unlink()
        self._assert_unchanged(self._run())

    def test_failed_transit_collectors_preserve_both_fragments(self):
        for collector in ('vnstat', 'ipv6_stats'):
            with self.subTest(collector=collector):
                self._assert_unchanged(self._run(
                    collector + '() { printf partial; return 1; }\nmain'))

    def test_failed_header_build_preserves_both_fragments(self):
        self._assert_unchanged(self._run(
            'build_header() { printf partial; return 1; }\nmain'))

    def test_failed_permission_setup_preserves_both_fragments(self):
        self._assert_unchanged(self._run('chmod() { return 1; }\nmain'))

    def test_failed_rename_preserves_both_fragments(self):
        self._assert_unchanged(self._run('mv() { return 1; }\nmain'))

    def test_failed_footer_rename_leaves_a_complete_old_footer(self):
        # Two paths cannot be replaced in one rename. If the second rename
        # fails, report failure while keeping each individual fragment whole.
        result = self._run('''mv() {
    if [[ "$3" == "$TMPF" ]]; then return 1; fi
    command mv "$@"
}
main''')
        self.assertNotEqual(result.returncode, 0, result.stderr)
        self.assertIn('<h1>mirror.tsue.net</h1>', self.header.read_text())
        self.assertEqual(self.footer.read_text(), 'old complete footer\n')

    def test_replacement_preserves_open_readers_complete_snapshots(self):
        with self.header.open() as header_reader, self.footer.open() as footer_reader:
            result = self._run()
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(header_reader.read(), 'old complete header\n')
            self.assertEqual(footer_reader.read(), 'old complete footer\n')
        self.assertIn('<h1>', self.header.read_text())
        self.assertIn('<h2>', self.footer.read_text())


if __name__ == '__main__':
    unittest.main()
