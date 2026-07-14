#!/usr/bin/env python3
"""Tests for new-torrents.py

Run from the repo root:
    python3 tests/test_new_torrents.py

Or with verbose output:
    python3 tests/test_new_torrents.py -v

No external dependencies required. The script under test is imported directly;
adjust SCRIPT_PATH below if you rename or move files.
"""

import http.client
import importlib.util
import io
import json
import re
import subprocess
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

# Import the script as a module. Since new-torrents.py has a hyphen in its
# name, importlib is required rather than a plain import statement.
SCRIPT_PATH = Path(__file__).parent.parent / 'new-torrents.py'

spec = importlib.util.spec_from_file_location('new_torrents', SCRIPT_PATH)
nt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(nt)


###################
# Shared fixtures #
###################

# Padding used to satisfy body_ok()'s 250-byte minimum without polluting
# page content.  Appended to every fake page that carries real test data.
_PAD = ' ' * 300


def make_checker(cls, iso_dir, status_content='', failures=None, display=None):
    """Construct a checker with a real FailureTracker backed by a temp file."""
    if failures is None:
        fd = tempfile.NamedTemporaryFile(suffix='.json', delete=False)
        fd.close()
        Path(fd.name).write_text('{}')
        failures = nt.FailureTracker(Path(fd.name), 3)
    return cls(iso_dir, status_content, failures, display)


def fake_fetch_fn(checker, page):
    """Return a fetch() replacement that sets checker._page and returns True.

    The page is padded to exceed body_ok()'s 250-byte minimum so callers
    only need to include the content-relevant markup, not filler bytes.
    """
    padded = page + _PAD

    def _fetch(url, name):
        checker._page = padded
        return True

    return _fetch


def fake_fetch_seq(checker, pages):
    """Return a fetch() that cycles through a sequence of pages, each padded."""
    padded = [p + _PAD for p in pages]
    it = iter(padded)

    def _fetch(url, name):
        checker._page = next(it)
        return True

    return _fetch


class TestFailureTracker(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.path = self.tmp / 'failures.json'

    def _tracker(self, threshold=3):
        return nt.FailureTracker(self.path, threshold)

    def test_starts_empty(self):
        t = self._tracker()
        self.assertFalse(t.at_threshold('x'))

    def test_increment_and_threshold(self):
        t = self._tracker(threshold=2)
        t.increment('svc')
        self.assertFalse(t.at_threshold('svc'))
        t.increment('svc')
        self.assertTrue(t.at_threshold('svc'))

    def test_clear_removes_counter(self):
        t = self._tracker()
        t.increment('svc')
        t.clear('svc')
        self.assertFalse(t.at_threshold('svc'))

    def test_clear_nonexistent_is_noop(self):
        t = self._tracker()
        t.clear('ghost')  # should not raise

    def test_save_writes_json(self):
        t = self._tracker()
        t.increment('svc')
        t.save()
        data = json.loads(self.path.read_text())
        self.assertEqual(data['svc'], 1)

    def test_save_skipped_when_clean(self):
        # A tracker that never mutates should not create the file
        path = self.tmp / 'never.json'
        t = nt.FailureTracker(path, 3)
        t.save()
        self.assertFalse(path.exists())

    def test_persists_across_instances(self):
        t1 = self._tracker(threshold=5)
        t1.increment('svc')
        t1.increment('svc')
        t1.save()

        t2 = self._tracker(threshold=5)
        self.assertFalse(t2.at_threshold('svc'))
        t2.increment('svc')
        t2.increment('svc')
        t2.increment('svc')
        self.assertTrue(t2.at_threshold('svc'))

    def test_corrupt_json_starts_fresh(self):
        self.path.write_text('not valid json')
        t = self._tracker()
        self.assertFalse(t.at_threshold('x'))  # no crash, empty state


class TestStatusDisplay(unittest.TestCase):
    """StatusDisplay writes ANSI cursor-control codes to stderr; these tests
    capture that output rather than parsing it visually."""

    def _display(self, names=('CheckerA',), term_width=80):
        with patch('sys.stderr', io.StringIO()):
            d = nt.StatusDisplay(list(names))
        d._term_width = term_width

        def _cleanup():
            with patch('sys.stderr', io.StringIO()):
                d.close()
        self.addCleanup(_cleanup)
        return d

    def test_initial_row_counts_are_one_line_each(self):
        d = self._display(names=('CheckerA', 'CheckerB'))
        self.assertEqual(d._last_rows, [1, 1])

    def test_last_rows_updates_after_redraw(self):
        d = self._display(names=('CheckerA',))
        with patch('sys.stderr', io.StringIO()):
            d.start('CheckerA')
        self.assertEqual(d._last_rows, [1])

    def test_cursor_moves_by_previous_not_new_row_count(self):
        """The core of the fix: when a line's wrapped height changes between
        redraws, the next redraw must move the cursor by how many rows are
        actually on screen from the PREVIOUS render, not by the row count
        of the content about to be printed — otherwise the live display
        drifts out of alignment with what's really there."""
        d = self._display(names=('CheckerA',), term_width=50)
        with patch('sys.stderr', io.StringIO()):
            d.update('CheckerA', 'x' * 40)  # wraps to 2 rows at width 50
        self.assertEqual(d._last_rows, [2])

        buf = io.StringIO()
        with patch('sys.stderr', buf):
            d.update('CheckerA', 'short')  # fits in 1 row
        cursor_up_amounts = [int(n) for n in re.findall(r'\x1b\[(\d+)A', buf.getvalue())]
        self.assertIn(
            2, cursor_up_amounts,
            f'Expected a cursor move by the old row count (2), got: {cursor_up_amounts}',
        )
        self.assertEqual(d._last_rows, [1])

    def test_close_leaves_final_state_visible(self):
        d = self._display(names=('CheckerA',))
        with patch('sys.stderr', io.StringIO()):
            d.finish('CheckerA', 0)
        buf = io.StringIO()
        with patch('sys.stderr', buf):
            d.close()
        self.assertIn('CheckerA', buf.getvalue())


class TestCheckerBase(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.ftrack = nt.FailureTracker(self.tmp / 'f.json', 3)

    def _checker(self, status=''):
        # Use AlmaChecker as a concrete stand-in for the abstract base
        return make_checker(nt.AlmaChecker, self.tmp, status_content=status,
                            failures=self.ftrack)

    def test_check_iso_in_status_no_alert(self):
        """ISO already known to transmission → no alert."""
        c = self._checker(status='archlinux-2025.01.01-x86_64.iso')
        c.check_iso('archlinux-2025.01.01-x86_64.iso')
        self.assertEqual(c.updates, set())

    def test_check_iso_missing_alerts_new(self):
        """ISO not on disk and not in transmission → NEW: alert."""
        c = self._checker()
        c.check_iso('archlinux-2025.01.01-x86_64.iso')
        self.assertIn('NEW:archlinux-2025.01.01-x86_64.iso', c.updates)

    def test_check_iso_missing_custom_alert(self):
        """check_iso respects a custom alert name."""
        c = self._checker()
        c.check_iso('archlinux-2025.01.01-x86_64.iso', 'NEW:Arch-2025.01.01')
        self.assertIn('NEW:Arch-2025.01.01', c.updates)
        self.assertNotIn('NEW:archlinux-2025.01.01-x86_64.iso', c.updates)

    def test_check_iso_orphan_on_disk_not_in_status(self):
        """ISO present on disk but not in transmission → ORPHAN: alert."""
        iso = self.tmp / 'archlinux-2025.01.01-x86_64.iso'
        iso.write_bytes(b'x' * 100)
        c = self._checker()
        c.check_iso('archlinux-2025.01.01-x86_64.iso')
        self.assertIn('ORPHAN:archlinux-2025.01.01-x86_64.iso', c.updates)

    def test_check_iso_zero_byte_file_treated_as_missing(self):
        """A zero-byte ISO on disk is treated as absent, not an orphan."""
        iso = self.tmp / 'archlinux-2025.01.01-x86_64.iso'
        iso.write_bytes(b'')
        c = self._checker()
        c.check_iso('archlinux-2025.01.01-x86_64.iso')
        self.assertIn('NEW:archlinux-2025.01.01-x86_64.iso', c.updates)
        self.assertNotIn('ORPHAN:archlinux-2025.01.01-x86_64.iso', c.updates)

    def test_check_dir_in_status_no_alert(self):
        d = self.tmp / 'Fedora-Workstation-42'
        d.mkdir()
        c = self._checker(status='Fedora-Workstation-42')
        c.check_dir('Fedora-Workstation-42')
        self.assertEqual(c.updates, set())

    def test_check_dir_missing_alerts_new(self):
        c = self._checker()
        c.check_dir('Fedora-Workstation-42')
        self.assertIn('NEW:Fedora-Workstation-42', c.updates)

    def test_check_dir_orphan_on_disk_not_in_status(self):
        d = self.tmp / 'Fedora-Workstation-42'
        d.mkdir()
        c = self._checker()
        c.check_dir('Fedora-Workstation-42')
        self.assertIn('ORPHAN:Fedora-Workstation-42', c.updates)

    def test_fetch_failure_increments_counter(self):
        c = self._checker()
        with patch('urllib.request.urlopen',
                   side_effect=urllib.error.URLError('connection refused')):
            c.fetch('https://example.com/x', 'example')
        self.assertEqual(self.ftrack._counts.get('example', 0), 1)

    def test_fetch_success_clears_counter(self):
        self.ftrack.increment('example')
        self.ftrack.increment('example')
        c = self._checker()
        mock_resp = MagicMock()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.read.return_value = b'hello world' * 50
        mock_resp.headers.get_content_charset.return_value = 'utf-8'
        mock_resp.headers.get.return_value = ''
        with patch('urllib.request.urlopen', return_value=mock_resp):
            c.fetch('https://example.com/x', 'example')
        self.assertEqual(self.ftrack._counts.get('example', 0), 0)

    def test_fetch_no_alert_below_threshold(self):
        c = self._checker()
        err = urllib.error.URLError('down')
        with patch('urllib.request.urlopen', side_effect=err):
            c.fetch('https://example.com/x', 'svc')
            c.fetch('https://example.com/x', 'svc')
        # Two failures, threshold is 3 — should not alert yet
        self.assertEqual(c.updates, set())

    def test_fetch_alerts_at_threshold(self):
        c = self._checker()
        err = urllib.error.URLError('down')
        with patch('urllib.request.urlopen', side_effect=err):
            c.fetch('https://example.com/x', 'svc')
            c.fetch('https://example.com/x', 'svc')
            c.fetch('https://example.com/x', 'svc')
        self.assertTrue(c.updates)

    def test_fetch_handles_corrupt_deflate_body(self):
        """zlib.error from a corrupt Content-Encoding: deflate body must not
        escape fetch() — it's not an OSError subclass, so without explicit
        handling it would crash the whole threaded run instead of being
        treated as an ordinary fetch failure."""
        c = self._checker()
        mock_resp = MagicMock()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.read.return_value = b'not valid zlib data at all'
        mock_resp.headers.get_content_charset.return_value = 'utf-8'
        mock_resp.headers.get.return_value = 'deflate'
        with patch('urllib.request.urlopen', return_value=mock_resp):
            result = c.fetch('https://example.com/x', 'svc')
        self.assertFalse(result)
        self.assertEqual(self.ftrack._counts.get('svc', 0), 1)

    def test_fetch_handles_unknown_charset(self):
        """LookupError from a garbage charset name in Content-Type must not
        escape fetch() either."""
        c = self._checker()
        mock_resp = MagicMock()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.read.return_value = b'hello world' * 50
        mock_resp.headers.get_content_charset.return_value = 'totally-bogus-charset'
        mock_resp.headers.get.return_value = ''
        with patch('urllib.request.urlopen', return_value=mock_resp):
            result = c.fetch('https://example.com/x', 'svc')
        self.assertFalse(result)
        self.assertEqual(self.ftrack._counts.get('svc', 0), 1)

    def test_fetch_handles_dropped_connection(self):
        """A connection that drops mid-response raises
        http.client.HTTPException (e.g. IncompleteRead), also not an
        OSError/URLError subclass."""
        c = self._checker()
        with patch('urllib.request.urlopen',
                   side_effect=http.client.IncompleteRead(b'partial')):
            result = c.fetch('https://example.com/x', 'svc')
        self.assertFalse(result)
        self.assertEqual(self.ftrack._counts.get('svc', 0), 1)

    def test_body_ok_alerts_on_empty_page(self):
        """body_ok fires when page is empty."""
        c = self._checker()
        c._page = ''
        self.assertFalse(c.body_ok('test-domain'))
        self.assertIn('test-domain', c.updates)

    def test_body_ok_alerts_on_short_page(self):
        """body_ok fires when page is below the 250-byte minimum."""
        c = self._checker()
        c._page = 'x' * 100
        self.assertFalse(c.body_ok('test-domain'))
        self.assertIn('test-domain', c.updates)

    def test_body_ok_passes_on_adequate_page(self):
        """body_ok returns True when page exceeds the minimum."""
        c = self._checker()
        c._page = 'x' * 300
        self.assertTrue(c.body_ok('test-domain'))
        self.assertEqual(c.updates, set())

    def test_body_ok_respects_custom_min_len(self):
        """body_ok respects a custom min_len argument."""
        c = self._checker()
        c._page = 'x' * 50
        self.assertFalse(c.body_ok('test-domain', min_len=100))
        self.assertIn('test-domain', c.updates)


# MintChecker

MINT_INDEX = (
    '<a href="21.3/">21.3/</a>'
    '<a href="22.0/">22.0/</a>'
)

MINT_VER = (
    '<a href="linuxmint-22.0-cinnamon-64bit.iso">linuxmint-22.0-cinnamon-64bit.iso</a>'
    '<a href="linuxmint-22.0-mate-64bit.iso">linuxmint-22.0-mate-64bit.iso</a>'
)

MINT_ISOS = ['linuxmint-22.0-cinnamon-64bit.iso', 'linuxmint-22.0-mate-64bit.iso']


class TestMintChecker(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def _run(self, status='', pages=None):
        if pages is None:
            pages = [MINT_INDEX, MINT_VER]
        c = make_checker(nt.MintChecker, self.tmp, status_content=status)
        c.fetch = fake_fetch_seq(c, pages)
        c.check()
        return c.updates

    def test_version_bump_grouped_not_per_file(self):
        """A version bump collapses to one grouped NEW/STALE instead of one
        per edition."""
        for ed in ('cinnamon', 'mate'):
            (self.tmp / f'linuxmint-21.3-{ed}-64bit.iso').write_bytes(b'x' * 100)
        updates = self._run()
        self.assertEqual(updates, {'NEW:Linux-Mint-22.0', 'STALE:Linux-Mint-21.3'})

    def test_no_alert_when_current_version_fully_present(self):
        for iso in MINT_ISOS:
            (self.tmp / iso).write_bytes(b'x' * 100)
        updates = self._run(status=' '.join(MINT_ISOS))
        self.assertEqual(updates, set())

    def test_missing_file_within_current_version_alerts_individually(self):
        """Once mirroring for the current release has started, a file
        that's still missing alerts by name instead of waiting on the
        group alert."""
        (self.tmp / 'linuxmint-22.0-cinnamon-64bit.iso').write_bytes(b'x' * 100)
        updates = self._run(status='linuxmint-22.0-cinnamon-64bit.iso')
        self.assertEqual(updates, {'NEW:linuxmint-22.0-mate-64bit.iso'})

    def test_orphan_within_current_version_alerts(self):
        for iso in MINT_ISOS:
            (self.tmp / iso).write_bytes(b'x' * 100)
        # mate exists on disk but isn't tracked by transmission
        updates = self._run(status='linuxmint-22.0-cinnamon-64bit.iso')
        self.assertEqual(updates, {'ORPHAN:linuxmint-22.0-mate-64bit.iso'})

    def test_stale_same_version_file_alerts_individually(self):
        """A file matching the CURRENT version but no longer listed isn't
        something a version-level alert can express, so it should still
        surface by name."""
        for iso in MINT_ISOS:
            (self.tmp / iso).write_bytes(b'x' * 100)
        (self.tmp / 'linuxmint-22.0-oldvariant-64bit.iso').write_bytes(b'x' * 100)
        updates = self._run(status=' '.join(MINT_ISOS))
        self.assertEqual(updates, {'STALE:linuxmint-22.0-oldvariant-64bit.iso'})

    def test_missing_all_local_isos_alerts(self):
        # ISOs are in status (so no NEW: per file) but none exist on disk
        updates = self._run(status=' '.join(MINT_ISOS))
        self.assertEqual(updates, {'MISSING:linuxmint-*.iso', 'NEW:Linux-Mint-22.0'})

    def test_no_missing_alert_when_local_iso_exists(self):
        p = self.tmp / 'linuxmint-22.0-cinnamon-64bit.iso'
        p.write_bytes(b'x' * 100)
        updates = self._run(status=' '.join(MINT_ISOS))
        self.assertNotIn('MISSING:linuxmint-*.iso', updates)

    def test_selects_highest_version(self):
        """Only ISOs from the highest version (22.0) are checked, not 21.3."""
        updates = self._run(status=' '.join(MINT_ISOS))
        self.assertFalse(
            any('21.3' in u for u in updates),
            f'Unexpected 21.3 alert in: {updates}',
        )

    def test_malformed_index_alerts(self):
        c = make_checker(nt.MintChecker, self.tmp)
        c.fetch = fake_fetch_fn(c, '<html>no versions here</html>')
        c.check()
        self.assertIn('MALFORMED:Linux-Mint', c.updates)

    def test_malformed_version_page_alerts(self):
        c = make_checker(nt.MintChecker, self.tmp)
        c.fetch = fake_fetch_seq(c, [MINT_INDEX, '<html>no isos here</html>'])
        c.check()
        self.assertIn('MALFORMED:Linux-Mint-22.0', c.updates)


# ArchChecker

ARCH_PAGE = '<strong>Current Release:</strong> 2025.06.01'


class TestArchChecker(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def _run(self, status='', page=ARCH_PAGE):
        c = make_checker(nt.ArchChecker, self.tmp, status_content=status)
        c.fetch = fake_fetch_fn(c, page)
        c.check()
        return c.updates

    def test_new_release_alerts(self):
        updates = self._run()
        self.assertIn('NEW:Arch-2025.06.01', updates)

    def test_no_alert_when_current_in_status(self):
        updates = self._run(status='archlinux-2025.06.01-x86_64.iso')
        self.assertNotIn('NEW:Arch-2025.06.01', updates)

    def test_stale_iso_alerted(self):
        old = self.tmp / 'archlinux-2024.01.01-x86_64.iso'
        old.write_bytes(b'x' * 100)
        updates = self._run(status='archlinux-2025.06.01-x86_64.iso')
        self.assertIn('STALE:archlinux-2024.01.01-x86_64.iso', updates)

    def test_malformed_page_alerts(self):
        updates = self._run(page='<html>no release here</html>')
        self.assertIn('MALFORMED:archlinux.org', updates)

    def test_zero_byte_stale_not_alerted(self):
        """Zero-byte files are skipped in the STALE loop."""
        old = self.tmp / 'archlinux-2024.01.01-x86_64.iso'
        old.write_bytes(b'')
        updates = self._run(status='archlinux-2025.06.01-x86_64.iso')
        self.assertNotIn('STALE:archlinux-2024.01.01-x86_64.iso', updates)


# CachyChecker

CACHY_PAGE = (
    'torrent_url&quot;:[0,&quot;https://cdn.cachyos.org/ISO/241201/'
    'cachyos-kde-linux-241201.torrent&quot;\n'
    'torrent_url&quot;:[0,&quot;https://cdn.cachyos.org/ISO/241201/'
    'cachyos-gnome-linux-241201.torrent&quot;\n'
)

CACHY_ISOS = ['cachyos-kde-linux-241201.iso', 'cachyos-gnome-linux-241201.iso']


class TestCachyChecker(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def _run(self, status='', page=CACHY_PAGE):
        c = make_checker(nt.CachyChecker, self.tmp, status_content=status)
        c.fetch = fake_fetch_fn(c, page)
        c.check()
        return c.updates

    def test_new_release_alerts(self):
        updates = self._run()
        self.assertIn('NEW:CachyOS-241201', updates)

    def test_no_alert_when_in_status(self):
        updates = self._run(status=' '.join(CACHY_ISOS))
        self.assertNotIn('NEW:CachyOS-241201', updates)

    def test_stale_iso_alerted(self):
        old = self.tmp / 'cachyos-kde-linux-231101.iso'
        old.write_bytes(b'x' * 100)
        updates = self._run(status=' '.join(CACHY_ISOS))
        self.assertIn('STALE:cachyos-kde-linux-231101.iso', updates)

    def test_malformed_page_alerts(self):
        updates = self._run(page='<html>no torrents here</html>')
        self.assertIn('MALFORMED:cachyos.org', updates)

    def test_zero_byte_stale_not_alerted(self):
        old = self.tmp / 'cachyos-kde-linux-231101.iso'
        old.write_bytes(b'')
        updates = self._run(status=' '.join(CACHY_ISOS))
        self.assertNotIn('STALE:cachyos-kde-linux-231101.iso', updates)


# UbuntuChecker

# The tracker_index format uses bare >NAME< spans, one per line.
# The beta/snapshot filter works at the line level, so each entry must be
# on its own line for the filter to be able to drop the unwanted ones.
UBUNTU_PAGE = (
    '<td>ubuntu-24.04-desktop-amd64.iso</td>\n'
    '<td>ubuntu-24.04-live-server-amd64.iso</td>\n'
    '<td>ubuntu-22.04.4-desktop-amd64.iso</td>\n'
    '<td>ubuntu-24.10-beta-amd64.iso</td>\n'
    '<td>ubuntu-24.10-snapshot-amd64.iso</td>\n'
)

UBUNTU_ISOS = [
    'ubuntu-24.04-desktop-amd64.iso',
    'ubuntu-24.04-live-server-amd64.iso',
    'ubuntu-22.04.4-desktop-amd64.iso',
]


class TestUbuntuChecker(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def _run(self, status='', page=UBUNTU_PAGE):
        c = make_checker(nt.UbuntuChecker, self.tmp, status_content=status)
        c.fetch = fake_fetch_fn(c, page)
        c.check()
        return c.updates

    def test_new_release_grouped_per_line_when_nothing_local(self):
        """Ubuntu runs multiple release lines at once (an LTS plus the
        current interim release) — each line's current point release gets
        its own grouped NEW:Ubuntu-VER, not a NEW: per file."""
        updates = self._run()
        self.assertIn('NEW:Ubuntu-24.04', updates)
        self.assertIn('NEW:Ubuntu-22.04.4', updates)
        self.assertFalse(
            any(u.startswith('NEW:') and u not in ('NEW:Ubuntu-24.04', 'NEW:Ubuntu-22.04.4')
                for u in updates),
            f'Unexpected per-file NEW: alert in: {updates}',
        )

    def test_beta_and_snapshot_filtered(self):
        """Beta and snapshot ISOs must not produce alerts."""
        updates = self._run()
        self.assertFalse(
            any('beta' in u.lower() or 'snapshot' in u.lower() for u in updates),
            f'Beta/snapshot leaked into alerts: {updates}',
        )

    def test_no_alert_when_current_versions_fully_present(self):
        for name in UBUNTU_ISOS:
            (self.tmp / name).write_bytes(b'x' * 100)
        updates = self._run(status=' '.join(UBUNTU_ISOS))
        self.assertEqual(updates, set())

    def test_independent_lines_dont_cross_contaminate(self):
        """A point-release bump within one line must not affect a
        different, still-current line — the reason Ubuntu can't group
        around one global 'current version' the way Debian/Mint can."""
        for name in ('ubuntu-24.04-desktop-amd64.iso', 'ubuntu-24.04-live-server-amd64.iso'):
            (self.tmp / name).write_bytes(b'x' * 100)
        (self.tmp / 'ubuntu-22.04.3-desktop-amd64.iso').write_bytes(b'x' * 100)
        status = 'ubuntu-24.04-desktop-amd64.iso ubuntu-24.04-live-server-amd64.iso'
        updates = self._run(status=status)
        # The 22.04 line's bump (.3 on disk -> .4 current on the tracker) surfaces, grouped
        self.assertEqual(updates, {'NEW:Ubuntu-22.04.4', 'STALE:Ubuntu-22.04.3'})
        # Critically: the fully-current, unrelated 24.04 line is untouched
        self.assertFalse(any('24.04' in u for u in updates), f'24.04 leaked in: {updates}')

    def test_missing_file_within_current_version_alerts_individually(self):
        """Once mirroring for a line's current release has started, a file
        that's still missing alerts by name instead of waiting on the group."""
        (self.tmp / 'ubuntu-24.04-desktop-amd64.iso').write_bytes(b'x' * 100)
        updates = self._run(status='ubuntu-24.04-desktop-amd64.iso')
        # live-server (24.04) is still missing; the untouched 22.04 line also
        # has nothing local yet, so it gets its own grouped alert
        self.assertEqual(updates, {'NEW:ubuntu-24.04-live-server-amd64.iso', 'NEW:Ubuntu-22.04.4'})

    def test_orphan_within_current_version_alerts(self):
        for name in UBUNTU_ISOS:
            (self.tmp / name).write_bytes(b'x' * 100)
        # live-server exists on disk but isn't tracked by transmission
        status = 'ubuntu-24.04-desktop-amd64.iso ubuntu-22.04.4-desktop-amd64.iso'
        updates = self._run(status=status)
        self.assertEqual(updates, {'ORPHAN:ubuntu-24.04-live-server-amd64.iso'})

    def test_stale_release_grouped_not_per_file(self):
        """An old line no longer on the tracker collapses to one
        STALE:Ubuntu-VER instead of one per leftover flavor."""
        for name in ('ubuntu-20.04-desktop-amd64.iso', 'kubuntu-20.04-desktop-amd64.iso'):
            (self.tmp / name).write_bytes(b'x' * 100)
        for name in UBUNTU_ISOS:
            (self.tmp / name).write_bytes(b'x' * 100)
        updates = self._run(status=' '.join(UBUNTU_ISOS))
        self.assertEqual(updates, {'STALE:Ubuntu-20.04'})

    def test_stale_same_version_file_alerts_individually(self):
        for name in UBUNTU_ISOS:
            (self.tmp / name).write_bytes(b'x' * 100)
        (self.tmp / 'ubuntu-24.04-oldvariant-amd64.iso').write_bytes(b'x' * 100)
        updates = self._run(status=' '.join(UBUNTU_ISOS))
        self.assertEqual(updates, {'STALE:ubuntu-24.04-oldvariant-amd64.iso'})

    def test_no_local_isos_alerts_missing(self):
        # ISOs in status but none on disk
        updates = self._run(status=' '.join(UBUNTU_ISOS))
        self.assertEqual(
            updates,
            {'MISSING:*buntu*.iso', 'NEW:Ubuntu-24.04', 'NEW:Ubuntu-22.04.4'},
        )

    def test_kubuntu_matches_glob(self):
        """*buntu* glob catches Kubuntu; its presence should suppress MISSING."""
        p = self.tmp / 'kubuntu-24.04-desktop-amd64.iso'
        p.write_bytes(b'x' * 100)
        updates = self._run(status=' '.join(UBUNTU_ISOS))
        self.assertNotIn('MISSING:*buntu*.iso', updates)

    def test_malformed_page_alerts(self):
        updates = self._run(page='<html>nothing here</html>')
        self.assertIn('MALFORMED:Ubuntu-Tracker', updates)

    def test_malformed_when_no_filename_has_parseable_version(self):
        updates = self._run(page='<td>ubuntu-README.iso</td>\n')
        self.assertIn('MALFORMED:Ubuntu-Tracker', updates)


# ProxmoxChecker

PROXMOX_PAGE = 'Download Proxmox VE 8.2-1 ISO\nDownload Proxmox VE 8.2-2 ISO\n'


class TestProxmoxChecker(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def _run(self, status='', page=PROXMOX_PAGE):
        c = make_checker(nt.ProxmoxChecker, self.tmp, status_content=status)
        c.fetch = fake_fetch_fn(c, page)
        c.check()
        return c.updates

    def test_new_version_alerts(self):
        updates = self._run()
        self.assertIn('NEW:Proxmox-8.2-1', updates)
        self.assertIn('NEW:Proxmox-8.2-2', updates)

    def test_no_alert_when_in_status(self):
        updates = self._run(status='proxmox-ve_8.2-2.iso')
        self.assertNotIn('NEW:Proxmox-8.2-2', updates)

    def test_stale_same_major_alerts(self):
        old = self.tmp / 'proxmox-ve_8.1-1.iso'
        old.write_bytes(b'x' * 100)
        # 8.1-1 is on disk but not on the page (page has 8.2-x); same major → STALE
        updates = self._run(status='proxmox-ve_8.2-2.iso')
        self.assertIn('STALE:proxmox-ve_8.1-1.iso', updates)

    def test_dropped_old_major_alerts(self):
        old = self.tmp / 'proxmox-ve_7.4-1.iso'
        old.write_bytes(b'x' * 100)
        updates = self._run(status='proxmox-ve_8.2-2.iso')
        self.assertIn('DROPPED:Proxmox-7', updates)

    def test_zero_byte_not_alerted(self):
        old = self.tmp / 'proxmox-ve_8.1-1.iso'
        old.write_bytes(b'')
        updates = self._run(status='proxmox-ve_8.2-2.iso')
        self.assertNotIn('STALE:proxmox-ve_8.1-1.iso', updates)

    def test_malformed_page_alerts(self):
        updates = self._run(page='<html>no versions</html>')
        self.assertIn('MALFORMED:Proxmox-Downloads', updates)


# FedoraChecker

FEDORA_JSON = json.dumps([
    {
        'name': '41',
        'torrents': [
            {'torrent': 'Fedora-Workstation-Live-x86_64-41.torrent'},
            {'torrent': 'Fedora-Server-dvd-x86_64-41.torrent'},
        ],
    },
    {
        'name': '42',
        'torrents': [
            {'torrent': 'Fedora-Workstation-Live-x86_64-42.torrent'},
            {'torrent': 'Fedora-Server-dvd-x86_64-42.torrent'},
        ],
    },
])


class TestFedoraChecker(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def _run(self, status='', page=FEDORA_JSON):
        c = make_checker(nt.FedoraChecker, self.tmp, status_content=status)
        c.fetch = fake_fetch_fn(c, page)
        c.check()
        return c.updates

    def test_new_version_alerts_when_no_local_dirs(self):
        updates = self._run()
        self.assertIn('NEW:Fedora-41', updates)
        self.assertIn('NEW:Fedora-42', updates)

    def test_no_new_alert_when_local_dirs_exist(self):
        (self.tmp / 'Fedora-Workstation-Live-x86_64-42').mkdir()
        updates = self._run(status='Fedora-Workstation-Live-x86_64-42')
        self.assertNotIn('NEW:Fedora-42', updates)

    def test_missing_torrent_within_known_version_alerts_new(self):
        """When one torrent dir exists for a version but another is absent
        from both disk and transmission, the missing one alerts NEW."""
        (self.tmp / 'Fedora-Workstation-Live-x86_64-42').mkdir()
        updates = self._run(status='Fedora-Workstation-Live-x86_64-42')
        self.assertIn('NEW:Fedora-Server-dvd-x86_64-42', updates)

    def test_orphan_torrent_within_known_version_alerts(self):
        """A torrent directory on disk but absent from transmission status
        should produce an ORPHAN alert."""
        (self.tmp / 'Fedora-Workstation-Live-x86_64-42').mkdir()
        (self.tmp / 'Fedora-Server-dvd-x86_64-42').mkdir()
        updates = self._run(status='Fedora-Workstation-Live-x86_64-42')
        self.assertIn('ORPHAN:Fedora-Server-dvd-x86_64-42', updates)

    def test_dropped_version_alerts(self):
        # Version 40 exists locally but is absent from the tracker JSON
        (self.tmp / 'Fedora-Workstation-Live-x86_64-40').mkdir()
        updates = self._run()
        self.assertIn('DROPPED:Fedora-40', updates)

    def test_stale_directory_alerted(self):
        # Version 41 Workstation exists locally but was removed from the tracker
        (self.tmp / 'Fedora-Workstation-Live-x86_64-41').mkdir()
        json_without_workstation = json.dumps([
            {
                'name': '41',
                'torrents': [{'torrent': 'Fedora-Server-dvd-x86_64-41.torrent'}],
            },
        ])
        updates = self._run(
            status='Fedora-Server-dvd-x86_64-41',
            page=json_without_workstation,
        )
        self.assertIn('STALE:Fedora-Workstation-Live-x86_64-41', updates)

    def test_malformed_json_alerts(self):
        updates = self._run(page='not json at all')
        self.assertIn('MALFORMED:Fedora-Tracker', updates)

    def test_empty_version_list_alerts(self):
        updates = self._run(page='[]')
        self.assertIn('MALFORMED:Fedora-Tracker', updates)


# AlmaChecker

ALMA_PAGE = (
    '<a href="/isos/x86_64/9.4.html">AlmaLinux 9.4 x86_64</a>'
    '<a href="/isos/aarch64/9.4.html">AlmaLinux 9.4 aarch64</a>'
    '<a href="/isos/x86_64/10.0.html">AlmaLinux 10.0 x86_64</a>'
)


class TestAlmaChecker(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def _run(self, status='', page=ALMA_PAGE):
        c = make_checker(nt.AlmaChecker, self.tmp, status_content=status)
        c.fetch = fake_fetch_fn(c, page)
        c.check()
        return c.updates

    def test_new_major_alerts_when_no_local_dirs(self):
        updates = self._run()
        self.assertIn('NEW:AlmaLinux-9', updates)
        self.assertIn('NEW:AlmaLinux-10', updates)

    def test_new_point_release_when_only_older_version_present(self):
        # Major 9 dir exists but at 9.3, not 9.4
        (self.tmp / 'AlmaLinux-9.3-x86_64').mkdir()
        updates = self._run()
        self.assertIn('NEW:AlmaLinux-9.4', updates)

    def test_no_alert_when_current_version_in_status(self):
        (self.tmp / 'AlmaLinux-9.4-x86_64').mkdir()
        (self.tmp / 'AlmaLinux-9.4-aarch64').mkdir()
        status = 'AlmaLinux-9.4-x86_64 AlmaLinux-9.4-aarch64'
        updates = self._run(status=status)
        self.assertNotIn('NEW:AlmaLinux-9', updates)
        self.assertNotIn('NEW:AlmaLinux-9.4', updates)

    def test_stale_point_release_grouped_not_per_arch(self):
        """Two arch directories left over from a superseded point release
        collapse into one STALE:AlmaLinux-VER instead of one per arch."""
        for arch in ('x86_64', 'aarch64'):
            (self.tmp / f'AlmaLinux-9.3-{arch}').mkdir()
            (self.tmp / f'AlmaLinux-9.4-{arch}').mkdir()
        status = 'AlmaLinux-9.4-x86_64 AlmaLinux-9.4-aarch64'
        updates = self._run(status=status)
        self.assertEqual(
            [u for u in updates if u.startswith('STALE:')],
            ['STALE:AlmaLinux-9.3'],
        )

    def test_dropped_major_alerts(self):
        # Major 8 dirs exist locally but absent from the page
        (self.tmp / 'AlmaLinux-8.10-x86_64').mkdir()
        updates = self._run()
        self.assertIn('DROPPED:AlmaLinux-8', updates)

    def test_missing_arch_alerts_new_dir(self):
        """When current version exists for one arch but not another, the
        missing arch should produce a NEW:AlmaLinux-VER-ARCH alert."""
        (self.tmp / 'AlmaLinux-9.4-x86_64').mkdir()
        updates = self._run(status='AlmaLinux-9.4-x86_64')
        self.assertIn('NEW:AlmaLinux-9.4-aarch64', updates)
        self.assertNotIn('NEW:AlmaLinux-9.4-x86_64', updates)

    def test_orphan_arch_dir_alerts(self):
        """A current-version arch directory on disk but absent from
        transmission status should produce an ORPHAN alert."""
        (self.tmp / 'AlmaLinux-9.4-x86_64').mkdir()
        (self.tmp / 'AlmaLinux-9.4-aarch64').mkdir()
        updates = self._run(status='AlmaLinux-9.4-x86_64')
        self.assertIn('ORPHAN:AlmaLinux-9.4-aarch64', updates)

    def test_malformed_page_alerts(self):
        updates = self._run(page='<html>no isos links</html>')
        self.assertIn('MALFORMED:AlmaLinux-isos.html', updates)


# DebianChecker (mocks subprocess.run)

# Every torrent filename on cdimage.debian.org is the ISO name with .torrent
# appended, e.g. debian-12.9.0-amd64-DVD-1.iso.torrent. The .iso matters: it's
# what ends up in local filenames and in transmission's status output, so the
# fixture needs it too or the version-vs-file matching below tests the wrong
# thing. (The previous fixture omitted it — harmless for the old per-file
# checks, but it would have hidden a false-positive STALE in the new
# version-grouping logic, since a local "foo.iso" can never string-match an
# upstream "foo" with no extension.)
DEBIAN_RSYNC_OUTPUT = (
    'drwxr-xr-x          4,096 2025/01/01 00:00:00 .\n'
    'drwxr-xr-x          4,096 2025/01/01 00:00:00 12.9.0-amd64-DVD-1\n'
    '-rw-r--r-- 982024192 2025/01/01 00:00:00 '
    '12.9.0-amd64-DVD-1/debian-12.9.0-amd64-DVD-1.iso.torrent\n'
    'drwxr-xr-x          4,096 2025/01/01 00:00:00 12.9.0-arm64-DVD-1\n'
    '-rw-r--r-- 982024192 2025/01/01 00:00:00 '
    '12.9.0-arm64-DVD-1/debian-12.9.0-arm64-DVD-1.iso.torrent\n'
)

# A wider release spanning the -edu/-live/-mac variants and a numbered source
# disc, to check the version regex against every filename shape it has to
# parse — these are exactly the families that produced the 46-alert spam.
DEBIAN_RSYNC_OUTPUT_WIDE = DEBIAN_RSYNC_OUTPUT + (
    '-rw-r--r--      12,345 2025/01/01 00:00:00 debian-edu-12.9.0-amd64-netinst.iso.torrent\n'
    '-rw-r--r--      12,345 2025/01/01 00:00:00 debian-live-12.9.0-amd64-kde.iso.torrent\n'
    '-rw-r--r--      12,345 2025/01/01 00:00:00 debian-mac-12.9.0-amd64-netinst.iso.torrent\n'
    '-rw-r--r--      12,345 2025/01/01 00:00:00 '
    '12.9.0-source-DVD-1/debian-12.9.0-source-DVD-1.iso.torrent\n'
)


class TestDebianChecker(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def _rsync(self, stdout=DEBIAN_RSYNC_OUTPUT, returncode=0):
        r = MagicMock()
        r.returncode = returncode
        r.stdout = stdout
        return r

    def _run(self, status='', rsync_result=None):
        if rsync_result is None:
            rsync_result = self._rsync()
        c = make_checker(nt.DebianChecker, self.tmp, status_content=status)
        with patch('subprocess.run', return_value=rsync_result):
            c.check()
        return c.updates

    def test_version_bump_grouped_not_per_file(self):
        """The original bug report, in miniature: a version bump used to
        alert once per file on both sides (46 NEW + 46 STALE for a real
        Debian point release). Old-version files on disk plus a new version
        upstream must collapse to one NEW:Debian-VER and one STALE:Debian-VER
        with no individual per-file alerts leaking through."""
        for name in ('debian-12.8.0-amd64-DVD-1.iso', 'debian-12.8.0-arm64-DVD-1.iso'):
            (self.tmp / name).write_bytes(b'x' * 100)
        updates = self._run()
        self.assertEqual(updates, {'NEW:Debian-12.9.0', 'STALE:Debian-12.8.0'})

    def test_no_local_isos_alerts_missing(self):
        """Completely empty disk pairs MISSING with a single grouped
        NEW:Debian-VER rather than one NEW: per upstream file."""
        status = 'debian-12.9.0-amd64-DVD-1.iso debian-12.9.0-arm64-DVD-1.iso'
        updates = self._run(status=status)
        self.assertEqual(updates, {'MISSING:debian-*.iso', 'NEW:Debian-12.9.0'})

    def test_no_alert_when_current_version_fully_present(self):
        for name in ('debian-12.9.0-amd64-DVD-1.iso', 'debian-12.9.0-arm64-DVD-1.iso'):
            (self.tmp / name).write_bytes(b'x' * 100)
        status = 'debian-12.9.0-amd64-DVD-1.iso debian-12.9.0-arm64-DVD-1.iso'
        updates = self._run(status=status)
        self.assertEqual(updates, set())

    def test_missing_file_within_current_version_alerts_individually(self):
        """Once mirroring for the current release has started, a file
        that's still missing alerts by name instead of waiting on the
        group alert — mirrors FedoraChecker's per-torrent fallback."""
        (self.tmp / 'debian-12.9.0-amd64-DVD-1.iso').write_bytes(b'x' * 100)
        status = 'debian-12.9.0-amd64-DVD-1.iso'
        updates = self._run(status=status)
        self.assertEqual(updates, {'NEW:debian-12.9.0-arm64-DVD-1.iso'})

    def test_orphan_within_current_version_alerts(self):
        """A current-version file on disk but unknown to transmission is
        still an individual ORPHAN once mirroring has started."""
        for name in ('debian-12.9.0-amd64-DVD-1.iso', 'debian-12.9.0-arm64-DVD-1.iso'):
            (self.tmp / name).write_bytes(b'x' * 100)
        status = 'debian-12.9.0-amd64-DVD-1.iso'  # arm64 exists but isn't tracked
        updates = self._run(status=status)
        self.assertEqual(updates, {'ORPHAN:debian-12.9.0-arm64-DVD-1.iso'})

    def test_stale_same_version_file_alerts_individually(self):
        """A file matching the CURRENT version but dropped from the tracker
        isn't something a version-level alert can express, so it should
        still surface by name rather than being swallowed by the group."""
        for name in ('debian-12.9.0-amd64-DVD-1.iso', 'debian-12.9.0-arm64-DVD-1.iso'):
            (self.tmp / name).write_bytes(b'x' * 100)
        (self.tmp / 'debian-12.9.0-oldvariant-1.iso').write_bytes(b'x' * 100)
        status = 'debian-12.9.0-amd64-DVD-1.iso debian-12.9.0-arm64-DVD-1.iso'
        updates = self._run(status=status)
        self.assertEqual(updates, {'STALE:debian-12.9.0-oldvariant-1.iso'})

    def test_version_parsed_across_edu_live_mac_and_source_variants(self):
        """The point release must parse correctly regardless of where the
        -edu/-live/-mac tag or the source-disc numbering shifts it in the
        filename — the grouping in every other test here depends on it."""
        for name in ('debian-12.8.0-amd64-DVD-1.iso', 'debian-12.8.0-arm64-DVD-1.iso'):
            (self.tmp / name).write_bytes(b'x' * 100)
        updates = self._run(rsync_result=self._rsync(stdout=DEBIAN_RSYNC_OUTPUT_WIDE))
        self.assertEqual(updates, {'NEW:Debian-12.9.0', 'STALE:Debian-12.8.0'})

    def test_rsync_failure_increments_counter(self):
        ftrack_path = self.tmp / 'f.json'
        ftrack_path.write_text('{}')
        ftrack = nt.FailureTracker(ftrack_path, 3)
        c = make_checker(nt.DebianChecker, self.tmp, failures=ftrack)
        with patch('subprocess.run', return_value=self._rsync(returncode=11)):
            c.check()
        self.assertEqual(ftrack._counts.get('Debian', 0), 1)

    def test_rsync_failure_at_threshold_alerts(self):
        ftrack_path = self.tmp / 'f.json'
        ftrack_path.write_text(json.dumps({'Debian': 2}))
        ftrack = nt.FailureTracker(ftrack_path, 3)
        c = make_checker(nt.DebianChecker, self.tmp, failures=ftrack)
        with patch('subprocess.run', return_value=self._rsync(returncode=11)):
            c.check()
        self.assertTrue(c.updates)

    def test_rsync_timeout_increments_counter(self):
        ftrack_path = self.tmp / 'f.json'
        ftrack_path.write_text('{}')
        ftrack = nt.FailureTracker(ftrack_path, 3)
        c = make_checker(nt.DebianChecker, self.tmp, failures=ftrack)
        with patch('subprocess.run', side_effect=subprocess.TimeoutExpired('rsync', 60)):
            c.check()
        self.assertEqual(ftrack._counts.get('Debian', 0), 1)

    def test_rsync_timeout_at_threshold_alerts(self):
        ftrack_path = self.tmp / 'f.json'
        ftrack_path.write_text(json.dumps({'Debian': 2}))
        ftrack = nt.FailureTracker(ftrack_path, 3)
        c = make_checker(nt.DebianChecker, self.tmp, failures=ftrack)
        with patch('subprocess.run', side_effect=subprocess.TimeoutExpired('rsync', 60)):
            c.check()
        self.assertIn('cdimage.debian.org', c.updates)

    def test_malformed_rsync_output_alerts(self):
        # rsync succeeds but returns no .torrent lines
        updates = self._run(rsync_result=self._rsync(stdout='drwxr-xr-x 4,096 2025/01/01 .\n'))
        self.assertIn('MALFORMED:Debian-Tracker', updates)

    def test_malformed_when_no_filename_has_parseable_version(self):
        """rsync succeeds and returns a .torrent file, but nothing in the
        name is a parseable version — MALFORMED rather than crashing on
        an empty ver_key() sort."""
        stdout = '-rw-r--r-- 123 2025/01/01 00:00:00 debian-README.torrent\n'
        updates = self._run(rsync_result=self._rsync(stdout=stdout))
        self.assertIn('MALFORMED:Debian-Tracker', updates)


# main() guard tests

class TestMain(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.iso_dir = self.tmp / 'Downloads'

    def _run_main(self, extra_patches=None):
        patches = {
            'ISO_DIR': self.iso_dir,
            'STATUS_FILE': self.iso_dir / 'status.txt',
            'FAIL_FILE': self.tmp / 'failures.json',
        }
        if extra_patches:
            patches.update(extra_patches)
        ctx_managers = [patch.object(nt, k, v) for k, v in patches.items()]
        for ctx in ctx_managers:
            ctx.start()
        try:
            ret = nt.main()
        finally:
            for ctx in ctx_managers:
                ctx.stop()
        return ret

    def _rsync_ok(self):
        m = MagicMock()
        m.returncode = 0
        return m

    def test_missing_iso_dir_returns_1(self):
        ret = self._run_main()
        self.assertEqual(ret, 1)

    def test_missing_status_file_returns_1(self):
        self.iso_dir.mkdir()
        with patch('subprocess.run', return_value=self._rsync_ok()):
            ret = self._run_main()
        self.assertEqual(ret, 1)

    def test_empty_status_file_returns_1(self):
        self.iso_dir.mkdir()
        (self.iso_dir / 'status.txt').write_text('')
        with patch('subprocess.run', return_value=self._rsync_ok()):
            ret = self._run_main()
        self.assertEqual(ret, 1)

    def test_malformed_status_file_returns_1(self):
        """status.txt without 'Sum:' is rejected."""
        self.iso_dir.mkdir()
        (self.iso_dir / 'status.txt').write_text('some content but no sum line')
        with patch('subprocess.run', return_value=self._rsync_ok()):
            ret = self._run_main()
        self.assertEqual(ret, 1)

    def test_missing_rsync_returns_1(self):
        self.iso_dir.mkdir()
        (self.iso_dir / 'status.txt').write_text('Sum: 1')
        failed = MagicMock()
        failed.returncode = 1
        with patch('subprocess.run', return_value=failed):
            ret = self._run_main()
        self.assertEqual(ret, 1)

    def test_clean_run_returns_0(self):
        """When all checkers produce no alerts, main() returns 0."""
        self.iso_dir.mkdir()
        (self.iso_dir / 'status.txt').write_text('Sum: 1')

        def noop_run(self_inner):
            return set()

        checker_patches = [patch.object(cls, 'run', noop_run) for cls in nt.CHECKERS]
        for p in checker_patches:
            p.start()
        try:
            with patch('subprocess.run', return_value=self._rsync_ok()):
                ret = self._run_main()
        finally:
            for p in checker_patches:
                p.stop()

        self.assertEqual(ret, 0)

    def test_alerts_produce_nonzero_exit(self):
        """When checkers return alerts, main() returns 1."""
        self.iso_dir.mkdir()
        (self.iso_dir / 'status.txt').write_text('Sum: 1')

        def alert_run(self_inner):
            return {'NEW:something'}

        checker_patches = [patch.object(cls, 'run', alert_run) for cls in nt.CHECKERS]
        for p in checker_patches:
            p.start()
        try:
            with patch('subprocess.run', return_value=self._rsync_ok()):
                ret = self._run_main()
        finally:
            for p in checker_patches:
                p.stop()

        self.assertEqual(ret, 1)


class TestVerKey(unittest.TestCase):

    def test_major_version_ordering(self):
        # Sanity check: different majors sort correctly across a realistic range
        versions = ['9.3', '10.0', '9.4', '8.10']
        self.assertEqual(
            sorted(versions, key=nt.ver_key),
            ['8.10', '9.3', '9.4', '10.0'],
        )

    def test_minor_version_sorts_numerically_not_lexicographically(self):
        # String sort would wrongly place '8.9' after '8.10' since '9' > '1'.
        # ver_key converts components to integers so 8.9 < 8.10 as expected.
        versions = ['8.10', '8.9', '9.0']
        self.assertEqual(
            sorted(versions, key=nt.ver_key),
            ['8.9', '8.10', '9.0'],
        )

    def test_date_versions(self):
        dates = ['2024.01.01', '2025.06.01', '2024.12.31']
        self.assertEqual(
            sorted(dates, key=nt.ver_key),
            ['2024.01.01', '2024.12.31', '2025.06.01'],
        )

    def test_single_component(self):
        self.assertEqual(nt.ver_key('42'), (42,))


if __name__ == '__main__':
    unittest.main()
