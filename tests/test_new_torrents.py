#!/usr/bin/env python3
"""Tests for new-torrents.py

Run from the repo root:
    python3 tests/test_new_torrents.py

Or with verbose output:
    python3 tests/test_new_torrents.py -v

No external dependencies required. The script under test is imported directly;
adjust SCRIPT_PATH below if you rename or move files.
"""

import fcntl
import functools
import gzip
import http.client
import importlib.util
import io
import json
import re
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import zlib
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

    def test_save_leaves_no_temp_file(self):
        """A successful save cleans up after itself: no temp file
        remains next to failures.json."""
        t = self._tracker()
        t.increment('svc')
        t.save()
        self.assertFalse((self.path.parent / (self.path.name + '.tmp')).exists())

    def test_save_replaces_atomically_in_same_directory(self):
        """save() must commit via os.replace() from a temp file in the
        target's own directory: rename(2) is atomic on POSIX, and a
        temp file on another filesystem (e.g. /tmp) would not be."""
        t = self._tracker()
        t.increment('svc')
        with patch('os.replace') as replace:
            t.save()
        replace.assert_called_once()
        tmp, target = replace.call_args[0]
        self.assertEqual(target, self.path)
        self.assertEqual(tmp.parent, self.path.parent)

    def test_crash_before_replace_preserves_previous_file(self):
        """Simulated crash between the temp write and the rename: the
        previous good file must survive intact. The old code wrote the
        main file directly, so a mid-write crash truncated it and
        _load() silently started fresh -- wiping the very outage
        counters this file exists to preserve."""
        t = self._tracker()
        t.increment('svc')
        t.save()
        t.increment('svc')
        with patch('os.replace', side_effect=OSError('simulated crash')):
            with self.assertRaises(OSError):
                t.save()
        self.assertEqual(json.loads(self.path.read_text()), {'svc': 1})
        # The stale temp from the crashed save is harmless: the next
        # successful save overwrites it and leaves no trace.
        t.save()
        self.assertEqual(json.loads(self.path.read_text()), {'svc': 2})
        self.assertFalse((self.path.parent / (self.path.name + '.tmp')).exists())


class _FakeTTY(io.StringIO):
    """A StringIO that also has a working fileno(). Plain StringIO's
    fileno() raises io.UnsupportedOperation, which is itself an OSError
    subclass -- _redraw()'s `except OSError` swallows it and silently
    falls back to width 80 before os.get_terminal_size() is ever reached,
    which would mask any os.get_terminal_size mock in these tests
    regardless of what it's set to return."""

    def fileno(self) -> int:
        return 2  # stderr's real fd; StatusDisplay always writes to
        # sys.stderr, never sys.stdout. os.get_terminal_size is itself
        # mocked in every test that uses this, so the actual value
        # returned here is never used for anything -- but it should
        # still say what it claims to stand in for.


class TestStatusDisplay(unittest.TestCase):
    """StatusDisplay writes ANSI cursor-control codes to stderr; these tests
    capture that output rather than parsing it visually."""

    def _display(self, names=('CheckerA',), term_width=80):
        # _redraw() now re-measures the terminal on every call (to catch a
        # mid-run resize), so simulating a width means patching
        # os.get_terminal_size for the test's whole lifetime, not just
        # poking _term_width once after construction.
        fake_size = type('R', (), {'columns': term_width})()
        size_patcher = patch('os.get_terminal_size', return_value=fake_size)
        size_patcher.start()
        self.addCleanup(size_patcher.stop)

        with patch('sys.stderr', _FakeTTY()):
            d = nt.StatusDisplay(list(names))

        def _cleanup():
            with patch('sys.stderr', _FakeTTY()):
                d.close()
        self.addCleanup(_cleanup)
        return d

    def test_initial_row_counts_are_one_line_each(self):
        d = self._display(names=('CheckerA', 'CheckerB'))
        self.assertEqual(d._last_rows, [1, 1])

    def test_last_rows_updates_after_redraw(self):
        d = self._display(names=('CheckerA',))
        with patch('sys.stderr', _FakeTTY()):
            d.start('CheckerA')
        self.assertEqual(d._last_rows, [1])

    def test_cursor_moves_by_previous_not_new_row_count(self):
        """The core of the fix: when a line's wrapped height changes between
        redraws, the next redraw must move the cursor by how many rows are
        actually on screen from the PREVIOUS render, not by the row count
        of the content about to be printed — otherwise the live display
        drifts out of alignment with what's really there."""
        d = self._display(names=('CheckerA',), term_width=50)
        with patch('sys.stderr', _FakeTTY()):
            d.update('CheckerA', 'x' * 40)  # wraps to 2 rows at width 50
        self.assertEqual(d._last_rows, [2])

        buf = _FakeTTY()
        with patch('sys.stderr', buf):
            d.update('CheckerA', 'short')  # fits in 1 row
        cursor_up_amounts = [int(n) for n in re.findall(r'\x1b\[(\d+)A', buf.getvalue())]
        self.assertIn(
            2, cursor_up_amounts,
            f'Expected a cursor move by the old row count (2), got: {cursor_up_amounts}',
        )
        self.assertEqual(d._last_rows, [1])

    def test_term_width_refreshed_on_resize_mid_run(self):
        """A terminal resize between two redraws must be picked up by the
        next one, not left using the width measured at __init__. The
        rendered line (name prefix + status, 63 visible chars for this
        status text) wraps differently depending on which width is
        actually in effect: 1 row at 80 columns, 2 rows at 40."""
        d = self._display(names=('CheckerA',), term_width=80)
        with patch('sys.stderr', _FakeTTY()):
            d.update('CheckerA', 'x' * 40)
        self.assertEqual(d._term_width, 80)
        self.assertEqual(d._last_rows, [1])

        # Simulate the user narrowing their terminal mid-run.
        narrower = type('R', (), {'columns': 40})()
        with patch('os.get_terminal_size', return_value=narrower):
            with patch('sys.stderr', _FakeTTY()):
                d.update('CheckerA', 'x' * 40)
        self.assertEqual(
            d._term_width, 40,
            'Expected _redraw() to re-measure and pick up the new width',
        )
        self.assertEqual(
            d._last_rows, [2],
            'The same rendered line should now be computed as wrapping '
            'to 2 rows at the new, narrower width',
        )

    def test_close_leaves_final_state_visible(self):
        d = self._display(names=('CheckerA',))
        with patch('sys.stderr', _FakeTTY()):
            d.finish('CheckerA', 0)
        buf = _FakeTTY()
        with patch('sys.stderr', buf):
            d.close()
        self.assertIn('CheckerA', buf.getvalue())

    def test_error_marks_done_and_errored(self):
        """error() is distinct from finish(): it's for a checker crashing,
        not for a normal completion (with or without alerts)."""
        d = self._display(names=('CheckerA',))
        with patch('sys.stderr', _FakeTTY()):
            d.error('CheckerA')
        self.assertTrue(d._done['CheckerA'])
        self.assertTrue(d._errored.get('CheckerA'))

    def test_error_uses_red_not_green_or_cyan(self):
        """The RED color was previously reserved but unused; error() should
        actually render with it, not fall through to the finish() colors."""
        d = self._display(names=('CheckerA',))
        with patch('sys.stderr', _FakeTTY()):
            d.error('CheckerA')
        rendered = d._render_line('CheckerA')
        self.assertIn(nt.StatusDisplay._RED, rendered)
        self.assertNotIn(nt.StatusDisplay._GREEN, rendered)
        self.assertNotIn(nt.StatusDisplay._CYAN, rendered)


class TestCheckerBase(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.ftrack = nt.FailureTracker(self.tmp / 'f.json', 3)

    def _checker(self, status=''):
        # Use AlmaChecker as a concrete stand-in for the abstract base
        return make_checker(nt.AlmaChecker, self.tmp, status_content=status,
                            failures=self.ftrack)

    @staticmethod
    def _content_encoding(value):
        """A headers.get() side_effect scoped to the one real call fetch()
        makes (headers.get('Content-Encoding', '')), falling back to the
        caller's own default for any other header. A blanket
        `.return_value = value` would return that same value for ANY
        header lookup, silently masking a future refactor that reads a
        different header through the same mock."""
        return lambda key, default='': value if key == 'Content-Encoding' else default

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

    def test_check_dir_prefix_of_unrelated_entry_not_treated_as_known(self):
        """A raw substring test on status_content would treat
        'Fedora-Workstation-Live-x86_64-42' as already known if only the
        unrelated, longer 'Fedora-Workstation-Live-x86_64-420' (a
        different Fedora major) were actually present -- directory names
        have no trailing extension to act as a natural boundary the way
        *.iso filenames do, so a version number that's an exact numeric
        prefix of another silently masks a genuinely missing release.
        Requires a whole-token match instead of a plain substring test."""
        c = self._checker(status='Fedora-Workstation-Live-x86_64-420')
        c.check_dir('Fedora-Workstation-Live-x86_64-42')
        self.assertIn('NEW:Fedora-Workstation-Live-x86_64-42', c.updates)

    def test_check_dir_still_matches_known_entry_with_trailing_whitespace(self):
        """The whole-token fix must not regress the common case: a real
        status_content entry followed by whitespace/newline (as in real
        transmission-remote -l output) still counts as known."""
        c = self._checker(status='Fedora-Workstation-Live-x86_64-42\n')
        c.check_dir('Fedora-Workstation-Live-x86_64-42')
        self.assertEqual(c.updates, set())

    def test_check_iso_blocks_relative_path_traversal(self):
        """A scraped name is never opened or read, only checked for
        existence/size -- but a crafted '../'-laden name could otherwise
        be used to probe for the existence of arbitrary files on the host,
        entirely outside iso_dir. Must alert UNSAFE, not ORPHAN/NEW."""
        c = self._checker()
        c.check_iso('../../../../../../etc/passwd')
        self.assertEqual(c.updates, {'UNSAFE:../../../../../../etc/passwd'})

    def test_check_iso_blocks_absolute_path_injection(self):
        """Path's own '/' operator replaces the left side entirely when
        the right side is absolute, so a name like '/etc/passwd' escapes
        iso_dir just as effectively as a relative traversal -- and more
        directly, since it needs no '../' at all."""
        c = self._checker()
        c.check_iso('/etc/passwd')
        self.assertEqual(c.updates, {'UNSAFE:/etc/passwd'})

    def test_check_dir_blocks_path_traversal(self):
        """Same protection applies to check_dir(), used by the Fedora and
        AlmaLinux checkers."""
        c = self._checker()
        c.check_dir('../../../../etc')
        self.assertEqual(c.updates, {'UNSAFE:../../../../etc'})

    def test_check_iso_normal_names_unaffected_by_safety_check(self):
        """The safety check must not false-positive on any legitimate,
        ordinary filename."""
        c = self._checker()
        c.check_iso('archlinux-2025.06.01-x86_64.iso')
        self.assertEqual(c.updates, {'NEW:archlinux-2025.06.01-x86_64.iso'})

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
        mock_resp.headers.get.side_effect = self._content_encoding('')
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
        mock_resp.headers.get.side_effect = self._content_encoding('deflate')
        with patch('urllib.request.urlopen', return_value=mock_resp):
            result = c.fetch('https://example.com/x', 'svc')
        self.assertFalse(result)
        self.assertEqual(self.ftrack._counts.get('svc', 0), 1)

    def test_fetch_handles_truncated_gzip(self):
        """EOFError from a truncated Content-Encoding: gzip body must not
        escape fetch() — it's not an OSError subclass, so without explicit
        handling it would crash the whole threaded run instead of being
        treated as an ordinary fetch failure."""
        c = self._checker()
        full = gzip.compress(b'hello world' * 50)
        mock_resp = MagicMock()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.read.return_value = full[:len(full)//2]
        mock_resp.headers.get_content_charset.return_value = 'utf-8'
        mock_resp.headers.get.side_effect = self._content_encoding('gzip')
        with patch('urllib.request.urlopen', return_value=mock_resp):
            result = c.fetch('https://example.com/x', 'svc')
        self.assertFalse(result)
        self.assertEqual(self.ftrack._counts.get('svc', 0), 1)

    def test_fetch_handles_raw_headerless_deflate(self):
        """RFC 1950 (zlib-wrapped) is the correct interpretation of a
        Content-Encoding: deflate response, but some servers actually send
        raw, headerless RFC 1951 deflate despite the label. zlib.decompress()
        with default wbits expects the zlib wrapper and raises on this --
        must fall back to raw deflate (negative wbits) rather than treating
        a merely-mislabeled-but-valid body as a fetch failure."""
        c = self._checker()
        co = zlib.compressobj(wbits=-15)
        raw_deflate_body = co.compress(b'hello world' * 50) + co.flush()
        mock_resp = MagicMock()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.read.return_value = raw_deflate_body
        mock_resp.headers.get_content_charset.return_value = 'utf-8'
        mock_resp.headers.get.side_effect = self._content_encoding('deflate')
        with patch('urllib.request.urlopen', return_value=mock_resp):
            result = c.fetch('https://example.com/x', 'svc')
        self.assertTrue(result)
        self.assertIn('hello world', c._page)
        self.assertEqual(self.ftrack._counts.get('svc', 0), 0)

    def test_fetch_handles_unknown_charset(self):
        """LookupError from a garbage charset name in Content-Type must not
        escape fetch() either."""
        c = self._checker()
        mock_resp = MagicMock()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.read.return_value = b'hello world' * 50
        mock_resp.headers.get_content_charset.return_value = 'totally-bogus-charset'
        mock_resp.headers.get.side_effect = self._content_encoding('')
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


class TestCheckerRunExceptionSafety(unittest.TestCase):
    """Checker.run() must not let an unexpected exception in check() escape.

    Uncaught, it would surface via future.result() in main(), aborting the
    whole threaded run: every other checker's results get dropped,
    failures.save() never runs, and display.close() never runs. run()
    catches it, turns it into its own alert, and marks the display with
    the (previously unused) red error state instead."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.ftrack = nt.FailureTracker(self.tmp / 'f.json', 3)

    def test_exception_becomes_alert_not_crash(self):
        c = make_checker(nt.AlmaChecker, self.tmp, failures=self.ftrack)
        c.check = MagicMock(side_effect=KeyError('name'))
        updates = c.run()  # must not raise
        self.assertTrue(
            any(u.startswith('EXCEPTION:AlmaChecker') for u in updates),
            f'Expected an EXCEPTION: alert, got: {updates}',
        )

    def test_exception_marks_display_errored_not_finished(self):
        display = MagicMock()
        c = make_checker(nt.AlmaChecker, self.tmp, failures=self.ftrack, display=display)
        c.check = MagicMock(side_effect=RuntimeError('boom'))
        c.run()
        display.error.assert_called_once_with('AlmaChecker')
        display.finish.assert_not_called()

    def test_clean_run_still_uses_finish_not_error(self):
        """A normal (non-crashing) run must still go through finish(), not
        error() -- the try/except shouldn't change behavior on the happy path."""
        display = MagicMock()
        c = make_checker(nt.AlmaChecker, self.tmp, failures=self.ftrack, display=display)
        c.fetch = fake_fetch_fn(c, '<html>no isos links</html>')
        c.run()
        display.finish.assert_called_once()
        display.error.assert_not_called()


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

# download_all.php support-page fixtures. Each supported release takes
# one table row; the version cell is <td rowspan="3">X.Y</td> (bare
# form for a brand-new major). The codename cells are not parsed.
MINT_SUPPORTED = '<td rowspan="3">22.0</td><td rowspan="3">Wilma</td>'
MINT_SUPPORTED_BOTH = (
    '<td rowspan="3">22.0</td><td rowspan="3">Wilma</td>'
    '<td rowspan="3">21.3</td><td rowspan="3">Uma</td>'
)
# The same page also carries an LMDE row (version "7"); "7" never
# appears in the pub index, so check() must filter it out.
MINT_SUPPORTED_LMDE = (
    '<td rowspan="3">22.0</td><td rowspan="3">Wilma</td>'
    '<td rowspan="3">7</td><td rowspan="3">Gigi</td>'
)
MINT_VER_21_3 = (
    '<a href="linuxmint-21.3-cinnamon-64bit.iso">linuxmint-21.3-cinnamon-64bit.iso</a>'
    '<a href="linuxmint-21.3-mate-64bit.iso">linuxmint-21.3-mate-64bit.iso</a>'
)


class TestMintChecker(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def _run(self, status='', pages=None):
        if pages is None:
            # Default: index, current version's directory, then the
            # support page. Tests exercising per-version tracking pass a
            # longer sequence; each listed version with local files
            # appends its own directory page in ascending version order.
            pages = [MINT_INDEX, MINT_VER, MINT_SUPPORTED]
        c = make_checker(nt.MintChecker, self.tmp, status_content=status)
        c.fetch = fake_fetch_seq(c, pages)
        c.check()
        return c.updates

    def test_version_bump_grouped_not_per_file(self):
        """A version bump collapses to one grouped NEW for the new
        release plus one grouped EOL for the old release's local files
        (unlisted on the support page), instead of one alert per
        edition."""
        for ed in ('cinnamon', 'mate'):
            (self.tmp / f'linuxmint-21.3-{ed}-64bit.iso').write_bytes(b'x' * 100)
        updates = self._run()
        self.assertEqual(updates, {'NEW:Linux-Mint-22.0', 'EOL:Linux-Mint-21.3'})

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

    def test_bare_major_directory_detected_as_current(self):
        """pub.linuxmint.io/stable/ lists a brand-new major as a bare,
        dot-less directory (e.g. '23/') before its first point release
        exists -- confirmed against the real, live index, which still
        shows this pattern historically (20/, 21/, 22/ each preceded
        their first dotted point release). The old [0-9]+\\.[0-9]+-only
        regex couldn't see that entry at all, so a fully-mirrored old
        major would report a clean run with zero alerts even though an
        entire new major release existed and nothing had been mirrored
        for it -- the exact silent-failure case this script exists to
        catch. Verifies the new major is picked as current once it has
        its own ISOs listed."""
        bare_major_index = (
            '<a href="22.1/">22.1/</a>'
            '<a href="22.2/">22.2/</a>'
            '<a href="22.3/">22.3/</a>'
            '<a href="23/">23/</a>'
        )
        v23_page = '<a href="linuxmint-23-cinnamon-64bit.iso">linuxmint-23-cinnamon-64bit.iso</a>'
        # The support page still lists 22.3 alongside the new bare
        # major; 22.3's own directory page lists the two local files, so
        # the per-version scan finds nothing new or dropped.
        support_22_3_and_23 = (
            '<td rowspan="3">22.3</td><td rowspan="3">Tessa</td>'
            '<td rowspan="3">23</td><td rowspan="3">Vera</td>'
        )
        v22_3_page = (
            '<a href="linuxmint-22.3-cinnamon-64bit.iso">linuxmint-22.3-cinnamon-64bit.iso</a>'
            '<a href="linuxmint-22.3-mate-64bit.iso">linuxmint-22.3-mate-64bit.iso</a>'
        )
        for iso in ('linuxmint-22.3-cinnamon-64bit.iso', 'linuxmint-22.3-mate-64bit.iso'):
            (self.tmp / iso).write_bytes(b'x' * 100)
        updates = self._run(
            status='linuxmint-22.3-cinnamon-64bit.iso linuxmint-22.3-mate-64bit.iso',
            pages=[bare_major_index, v23_page, support_22_3_and_23, v22_3_page],
        )
        self.assertEqual(updates, {'NEW:Linux-Mint-23'})

    def test_bare_major_directory_with_no_isos_yet_alerts_rather_than_silence(self):
        """Even in the narrower window where the new major's own directory
        genuinely has no ISOs published yet, the checker should say
        something (MALFORMED, matching the same convention used
        everywhere else in this codebase for 'found the structure but not
        the expected content') rather than silently staying locked onto
        the previous major forever."""
        bare_major_index = (
            '<a href="22.1/">22.1/</a>'
            '<a href="22.2/">22.2/</a>'
            '<a href="22.3/">22.3/</a>'
            '<a href="23/">23/</a>'
        )
        for iso in ('linuxmint-22.3-cinnamon-64bit.iso', 'linuxmint-22.3-mate-64bit.iso'):
            (self.tmp / iso).write_bytes(b'x' * 100)
        updates = self._run(
            status='linuxmint-22.3-cinnamon-64bit.iso linuxmint-22.3-mate-64bit.iso',
            pages=[bare_major_index, '<html>nothing published yet</html>'],
        )
        self.assertEqual(updates, {'MALFORMED:Linux-Mint-23'})

    def test_bare_major_filenames_also_recognized_not_just_the_directory(self):
        """Confirmed against a live Mint mirror: the very first release of
        a major uses BARE filenames too (linuxmint-22-cinnamon-64bit.iso,
        no dot), not just a bare directory. _version_of() originally still
        required a dot, so even after the directory-listing fix correctly
        identified a new bare major as current, local_current could never
        match its own (also bare) filenames -- a fully-mirrored, fully-
        known release would report NEW:Linux-Mint-VER on every single run
        forever, the opposite failure from the original silence bug."""
        bare_major_index = '<a href="22.3/">22.3/</a><a href="23/">23/</a>'
        v23_page = (
            '<a href="linuxmint-23-cinnamon-64bit.iso">linuxmint-23-cinnamon-64bit.iso</a>'
            '<a href="linuxmint-23-mate-64bit.iso">linuxmint-23-mate-64bit.iso</a>'
        )
        for iso in ('linuxmint-23-cinnamon-64bit.iso', 'linuxmint-23-mate-64bit.iso'):
            (self.tmp / iso).write_bytes(b'x' * 100)
        # The support page lists only the new major; a 22.3 row would
        # add a NEW:Linux-Mint-22.3 alert (no local 22.3 files here).
        support_23_only = '<td rowspan="3">23</td><td rowspan="3">Vera</td>'
        updates = self._run(
            status='linuxmint-23-cinnamon-64bit.iso linuxmint-23-mate-64bit.iso',
            pages=[bare_major_index, v23_page, support_23_only],
        )
        self.assertEqual(updates, set())

    def test_unlisted_version_locals_alert_grouped_eol(self):
        """Local ISOs for a version the support page no longer lists are
        past EOL: one grouped alert per version, not one per file."""
        for iso in MINT_ISOS:
            (self.tmp / iso).write_bytes(b'x' * 100)
        for ed in ('cinnamon', 'mate'):
            (self.tmp / f'linuxmint-20.3-{ed}-64bit.iso').write_bytes(b'x' * 100)
        updates = self._run(status=' '.join(MINT_ISOS))
        self.assertEqual(updates, {'EOL:Linux-Mint-20.3'})

    def test_still_supported_version_fully_mirrored_is_silent(self):
        """Regression for the old grouped STALE: a version still listed
        on the support page used to alert STALE:Linux-Mint-VER the
        moment a newer current shipped, even with every local file
        present and tracked; it is now tracked per-version and reports
        nothing."""
        local = MINT_ISOS + [
            'linuxmint-21.3-cinnamon-64bit.iso',
            'linuxmint-21.3-mate-64bit.iso',
        ]
        for iso in local:
            (self.tmp / iso).write_bytes(b'x' * 100)
        updates = self._run(
            status=' '.join(local),
            pages=[MINT_INDEX, MINT_VER, MINT_SUPPORTED_BOTH, MINT_VER_21_3],
        )
        self.assertEqual(updates, set())

    def test_supported_version_not_yet_mirrored_alerts_new_without_fetch(self):
        """A listed version with no local ISOs alerts NEW:Linux-Mint-VER
        and must not fetch its directory; the page sequence deliberately
        ends at the support page, so any over-fetch raises StopIteration."""
        for iso in MINT_ISOS:
            (self.tmp / iso).write_bytes(b'x' * 100)
        updates = self._run(
            status=' '.join(MINT_ISOS),
            pages=[MINT_INDEX, MINT_VER, MINT_SUPPORTED_BOTH],
        )
        self.assertEqual(updates, {'NEW:Linux-Mint-21.3'})

    def test_supported_version_partially_mirrored_checks_per_file(self):
        """Once any local file exists for a listed version, its directory
        is fetched lazily and per-file checks run against it: the
        missing edition surfaces by name instead of waiting on the
        group."""
        local = MINT_ISOS + ['linuxmint-21.3-cinnamon-64bit.iso']
        for iso in local:
            (self.tmp / iso).write_bytes(b'x' * 100)
        updates = self._run(
            status=' '.join(local),
            pages=[MINT_INDEX, MINT_VER, MINT_SUPPORTED_BOTH, MINT_VER_21_3],
        )
        self.assertEqual(updates, {'NEW:linuxmint-21.3-mate-64bit.iso'})

    def test_supported_version_dir_fetch_failure_stays_silent(self):
        """A failed directory fetch for one supported version skips only
        that version (fail-open per version); the other versions' alerts
        still fire and the run does not crash. The interception is by URL
        because every version directory shares the 'Linux-Mint-VER'
        failure-tracker name."""
        index = (
            '<a href="19.3/">19.3/</a>'
            '<a href="20.3/">20.3/</a>'
            '<a href="21.3/">21.3/</a>'
            '<a href="22.0/">22.0/</a>'
        )
        support = (
            '<td rowspan="3">22.0</td><td rowspan="3">Wilma</td>'
            '<td rowspan="3">21.3</td><td rowspan="3">Uma</td>'
            '<td rowspan="3">20.3</td><td rowspan="3">Diana</td>'
        )
        local = MINT_ISOS + [
            'linuxmint-21.3-cinnamon-64bit.iso',
            'linuxmint-19.3-cinnamon-64bit.iso',
        ]
        for iso in local:
            (self.tmp / iso).write_bytes(b'x' * 100)
        c = make_checker(nt.MintChecker, self.tmp,
                         status_content=' '.join(local))
        seq = fake_fetch_seq(c, [index, MINT_VER, support])

        def _fetch(url, name):
            if '21.3' in url:
                return False
            return seq(url, name)

        c.fetch = _fetch
        c.check()
        self.assertEqual(
            c.updates, {'NEW:Linux-Mint-20.3', 'EOL:Linux-Mint-19.3'})

    def test_support_page_unparseable_alerts_and_fails_open(self):
        """A support page with no version cells alerts MALFORMED and
        fails open: only the current version is tracked and no local
        file is flagged past EOL, since support is now unknown rather
        than disproven."""
        for iso in MINT_ISOS:
            (self.tmp / iso).write_bytes(b'x' * 100)
        (self.tmp / 'linuxmint-20.3-cinnamon-64bit.iso').write_bytes(b'x' * 100)
        updates = self._run(
            status=' '.join(MINT_ISOS),
            pages=[MINT_INDEX, MINT_VER, '<html>no version cells here</html>'],
        )
        self.assertEqual(updates, {'MALFORMED:Linux-Mint-Supported'})

    def test_support_page_fetch_failure_fails_open_silently(self):
        """An unfetchable support page fails open without alerting:
        nothing is flagged past EOL and the run does not crash. Keyed by
        fetch name rather than URL because the support page has its own
        'Linux-Mint-Supported' tracker name, unlike the version
        directories which all share 'Linux-Mint-VER'."""
        for iso in MINT_ISOS:
            (self.tmp / iso).write_bytes(b'x' * 100)
        (self.tmp / 'linuxmint-20.3-cinnamon-64bit.iso').write_bytes(b'x' * 100)
        c = make_checker(nt.MintChecker, self.tmp,
                         status_content=' '.join(MINT_ISOS))
        seq = fake_fetch_seq(c, [MINT_INDEX, MINT_VER, MINT_SUPPORTED])

        def _fetch(url, name):
            if name == 'Linux-Mint-Supported':
                return False
            return seq(url, name)

        c.fetch = _fetch
        c.check()
        self.assertEqual(c.updates, set())

    def test_unparseable_local_file_alerts_stale_by_name(self):
        """A local linuxmint-*.iso whose name carries no recognizable
        version can't be attributed to any tracked version, so it
        surfaces individually as STALE rather than being swallowed by a
        version-level alert."""
        for iso in MINT_ISOS:
            (self.tmp / iso).write_bytes(b'x' * 100)
        (self.tmp / 'linuxmint-broken.iso').write_bytes(b'x' * 100)
        updates = self._run(status=' '.join(MINT_ISOS))
        self.assertEqual(updates, {'STALE:linuxmint-broken.iso'})

    def test_lmde_row_on_support_page_is_filtered_out(self):
        """The support page also carries an LMDE row (version "7"); "7"
        never appears in the pub index, so check() must drop it via the
        intersection -- otherwise a new LMDE release would loop
        reporting NEW:Linux-Mint-7 on every run."""
        for iso in MINT_ISOS:
            (self.tmp / iso).write_bytes(b'x' * 100)
        updates = self._run(
            status=' '.join(MINT_ISOS),
            pages=[MINT_INDEX, MINT_VER, MINT_SUPPORTED_LMDE],
        )
        self.assertEqual(updates, set())


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

    def test_broken_symlink_stale_not_alerted(self):
        """A dangling symlink is skipped like a zero-byte file: glob()
        returns it, but stat() would follow the missing target and raise
        FileNotFoundError, killing the checker mid-scan."""
        old = self.tmp / 'archlinux-2024.01.01-x86_64.iso'
        old.symlink_to('no-such-target.iso')
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

    def test_unparseable_edition_name_alerts_malformed_not_empty_version(self):
        """A compound edition name with an internal hyphen (e.g. a
        hypothetical 'desktop-gnome' spin) is captured fine by the
        torrent_url regex ([^&]+, any non-& char), but defeats the
        release_dates regex's single-segment [^-]+. This used to silently
        produce 'NEW:CachyOS-' with an empty version suffix instead of
        flagging the page as unparseable."""
        page = (
            'torrent_url&quot;:[0,&quot;https://cdn.cachyos.org/ISO/241201/'
            'cachyos-desktop-gnome-linux-241201.torrent&quot;\n'
        )
        updates = self._run(page=page)
        self.assertIn('MALFORMED:cachyos.org', updates)
        self.assertFalse(
            any(u == 'NEW:CachyOS-' for u in updates),
            f'Unexpected empty-version alert: {updates}',
        )

    def test_unparseable_edition_name_still_catches_orphan(self):
        """The MALFORMED guard above must not throw away detection it
        doesn't need to sacrifice: upstream_isos itself parsed fine (real
        filenames), only the release-date extraction failed, so a
        genuinely orphaned local file for that same edition should still
        be caught -- alongside the MALFORMED signal, not instead of it."""
        page = (
            'torrent_url&quot;:[0,&quot;https://cdn.cachyos.org/ISO/241201/'
            'cachyos-desktop-gnome-linux-241201.torrent&quot;\n'
        )
        local = self.tmp / 'cachyos-desktop-gnome-linux-241201.iso'
        local.write_bytes(b'x' * 100)
        updates = self._run(page=page)  # empty status -> nothing known to transmission
        self.assertEqual(
            updates,
            {'MALFORMED:cachyos.org', 'ORPHAN:cachyos-desktop-gnome-linux-241201.iso'},
        )

    def test_zero_byte_stale_not_alerted(self):
        old = self.tmp / 'cachyos-kde-linux-231101.iso'
        old.write_bytes(b'')
        updates = self._run(status=' '.join(CACHY_ISOS))
        self.assertNotIn('STALE:cachyos-kde-linux-231101.iso', updates)

    def test_broken_symlink_stale_not_alerted(self):
        """A dangling symlink is skipped like a zero-byte file: glob()
        returns it, but stat() would follow the missing target and raise
        FileNotFoundError, killing the checker mid-scan."""
        old = self.tmp / 'cachyos-kde-linux-231101.iso'
        old.symlink_to('no-such-target.iso')
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

# Minimal version of Canonical's support-schedule page (ubuntu.com/project/
# docs/release-team/list-of-releases/), carrying the four table headers
# _eol_lines() keys on and only the rows the tests need. Dates are the real
# ones, interpreted relative to UBUNTU_FROZEN_NOW below, so the assertions
# hold regardless of when the suite runs:
#   26.04 - active in every mode (standard support runs to May 2031)
#   20.04 - standard ended May 2025, ESM runs to Apr 2030
#   16.04 - standard and ESM over, legacy add-on runs to Apr 2031
#   12.04 - past every tier (ESM ended Apr 2019)
#   24.10 - interim; 9-month standard support ended Jul 2025
UBUNTU_SCHEDULE_PAGE = (
    '<table>'
    '<tr><th>Version</th><th>Code name</th><th>Docs</th><th>Release</th>'
    '<th>End of Standard Support</th><th>End of Life</th></tr>'
    '<tr><td>Ubuntu 26.04 LTS</td><td>Resolute Raccoon</td><td>Release notes</td>'
    '<td>Apr 23, 2026</td><td>May 2031</td><td>Apr 2041</td></tr>'
    '<tr><td>Ubuntu 20.04 LTS</td><td>Focal Fossa</td><td>Release notes</td>'
    '<td>Apr 23, 2020</td><td>May 2025</td><td>Apr 2035</td></tr>'
    '<tr><td>Ubuntu 16.04 LTS</td><td>Xenial Xerus</td><td>Release notes</td>'
    '<td>Apr 21, 2016</td><td>Apr 2021</td><td>Apr 2031</td></tr>'
    '</table>'
    '<table>'
    '<tr><th>Version</th><th>Detailed ESM coverage</th><th>Start of ESM</th>'
    '<th># of years</th><th>End of Life</th></tr>'
    '<tr><td>Ubuntu 20.04 ESM</td><td>SecurityTeam/ESM/20.04</td><td>Jun 2025</td>'
    '<td>5 years</td><td>Apr 2030</td></tr>'
    '<tr><td>Ubuntu 16.04 ESM</td><td>SecurityTeam/ESM/16.04</td><td>Apr 2021</td>'
    '<td>5 years</td><td>Apr 2026</td></tr>'
    '<tr><td>Ubuntu 12.04 ESM</td><td>SecurityTeam/ESM/12.04</td><td>Apr 28, 2017</td>'
    '<td>2 years</td><td>Apr 2019</td></tr>'
    '</table>'
    '<table>'
    '<tr><th>Version</th><th>Detailed Legacy coverage</th><th>Start of Legacy</th>'
    '<th># of years</th><th>End of Life</th></tr>'
    '<tr><td>Ubuntu 16.04 ESM</td><td>SecurityTeam/ESM/16.04</td><td>Apr 2026</td>'
    '<td>5 years</td><td>Apr 2031</td></tr>'
    '</table>'
    '<table>'
    '<tr><th>Version</th><th>Code name</th><th>Docs</th><th>Release</th>'
    '<th>End of Life</th></tr>'
    '<tr><td>Ubuntu 24.10</td><td>Orchid Ocelot</td><td>Release notes</td>'
    '<td>Oct 10, 2024</td><td>Jul 2025</td></tr>'
    '<tr><td>Ubuntu 12.04 LTS</td><td>Precise Pangolin</td><td>Release Notes</td>'
    '<td>Apr 26, 2012</td><td>Apr 28, 2017</td></tr>'
    '</table>'
)


# Frozen (year, month) "today" for every date-sensitive Ubuntu test: all
# fixture dates above were chosen relative to this instant, so the suite
# no longer silently rots as real-world expirations pass.
#
# COUPLED TO THE FIXTURE DATES ABOVE. Do not bump this constant without
# re-checking every future-dated fixture it must outrun:
#   20.04 ESM end          Apr 2030  (governs test_esm_mode_drops_line_past_esm_end)
#   16.04 legacy end       Apr 2031  (governs test_esm_active_line_not_dropped_in_hard_mode)
#   26.04 standard end     May 2031  (governs test_standard_mode_drops_line_past_standard_end)
#   20.04 LTS End of Life  Apr 2035  (governs the two grouped-STALE tests, hard mode)
#   26.04 End of Life      Apr 2041  (governs test_eol_line_not_alerted)
UBUNTU_FROZEN_NOW = (2026, 8)


class TestUbuntuChecker(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def _run(self, status='', page=UBUNTU_PAGE, schedule=UBUNTU_SCHEDULE_PAGE,
             now=None):
        c = make_checker(nt.UbuntuChecker, self.tmp, status_content=status)
        # check() fetches twice: tracker page, then the support schedule.
        c.fetch = fake_fetch_seq(c, [page, schedule])
        if now is not None:
            # Pin the clock for this run: the instance attribute shadows
            # the bound method, so check()'s self._eol_lines() sees it.
            c._eol_lines = functools.partial(
                nt.UbuntuChecker._eol_lines, c, now=now)
        c.check()
        return c.updates

    def test_eol_line_not_alerted(self):
        """A line past the end of every support tier (12.04 in the
        fixture) is dropped before alerting: its unmirrored point release
        must not raise NEW:Ubuntu-VER, while a still-active line (26.04)
        must."""
        page = ('<td>ubuntu-12.04.5-desktop-i386.iso</td>\n'
                '<td>ubuntu-26.04-desktop-amd64.iso</td>\n')
        updates = self._run(page=page, now=UBUNTU_FROZEN_NOW)
        self.assertIn('NEW:Ubuntu-26.04', updates)
        self.assertNotIn('NEW:Ubuntu-12.04.5', updates)
        self.assertNotIn('MALFORMED:Ubuntu-EOL', updates)

    def test_esm_active_line_not_dropped_in_hard_mode(self):
        """'hard' mode (the default) only drops a line once every tier,
        including the paid ESM/legacy add-ons, has ended. In the fixture
        16.04's standard support and ESM are over but its legacy add-on
        runs to Apr 2031, so it must stay tracked."""
        page = ('<td>ubuntu-16.04.7-desktop-amd64.iso</td>\n'
                '<td>ubuntu-26.04-desktop-amd64.iso</td>\n')
        updates = self._run(page=page, now=UBUNTU_FROZEN_NOW)
        self.assertIn('NEW:Ubuntu-16.04.7', updates)
        self.assertIn('NEW:Ubuntu-26.04', updates)

    def test_standard_mode_drops_line_past_standard_end(self):
        """'standard' mode drops 20.04 (standard support ended May 2025 in
        the fixture) even though its ESM runs to Apr 2030."""
        nt.UbuntuChecker._EOL_MODE = 'standard'
        self.addCleanup(setattr, nt.UbuntuChecker, '_EOL_MODE', 'hard')
        page = ('<td>ubuntu-20.04.6-desktop-amd64.iso</td>\n'
                '<td>ubuntu-26.04-desktop-amd64.iso</td>\n')
        updates = self._run(page=page, now=UBUNTU_FROZEN_NOW)
        self.assertIn('NEW:Ubuntu-26.04', updates)
        self.assertNotIn('NEW:Ubuntu-20.04.6', updates)

    def test_esm_mode_drops_line_past_esm_end(self):
        """'esm' mode drops 16.04 (ESM ended Apr 2026 in the fixture) but
        keeps 20.04 (ESM to Apr 2030); the interim 24.10 never gets ESM
        and is dropped at its 9-month standard end (Jul 2025)."""
        nt.UbuntuChecker._EOL_MODE = 'esm'
        self.addCleanup(setattr, nt.UbuntuChecker, '_EOL_MODE', 'hard')
        page = ('<td>ubuntu-16.04.7-desktop-amd64.iso</td>\n'
                '<td>ubuntu-20.04.6-desktop-amd64.iso</td>\n'
                '<td>ubuntu-24.10-desktop-amd64.iso</td>\n'
                '<td>ubuntu-26.04-desktop-amd64.iso</td>\n')
        updates = self._run(page=page, now=UBUNTU_FROZEN_NOW)
        self.assertIn('NEW:Ubuntu-20.04.6', updates)
        self.assertIn('NEW:Ubuntu-26.04', updates)
        self.assertNotIn('NEW:Ubuntu-16.04.7', updates)
        self.assertNotIn('NEW:Ubuntu-24.10', updates)

    def test_schedule_fetch_failure_tracks_everything(self):
        """When the schedule page can't be fetched the checker fails open:
        EOL lines are tracked exactly as if the filter didn't exist."""
        page = ('<td>ubuntu-12.04.5-desktop-i386.iso</td>\n'
                '<td>ubuntu-26.04-desktop-amd64.iso</td>\n')
        c = make_checker(nt.UbuntuChecker, self.tmp)

        def _fetch(url, name):
            if name == 'Ubuntu-EOL':
                return False
            c._page = page + _PAD
            return True

        c.fetch = _fetch
        c.check()
        self.assertIn('NEW:Ubuntu-12.04.5', c.updates)
        self.assertIn('NEW:Ubuntu-26.04', c.updates)

    def test_malformed_schedule_alerts_and_fails_open(self):
        """A schedule page with no parseable tables alerts
        MALFORMED:Ubuntu-EOL and fails open rather than silently disabling
        the filter."""
        page = ('<td>ubuntu-12.04.5-desktop-i386.iso</td>\n'
                '<td>ubuntu-26.04-desktop-amd64.iso</td>\n')
        updates = self._run(page=page,
                            schedule='<html><body>no tables here</body></html>')
        self.assertIn('MALFORMED:Ubuntu-EOL', updates)
        self.assertIn('NEW:Ubuntu-12.04.5', updates)
        self.assertIn('NEW:Ubuntu-26.04', updates)

    def test_all_lines_eol_fails_open(self):
        """If the schedule data ever marked every tracked line EOL -- only
        possible with corrupt data, since the current release is never
        EOL -- the checker must track everything rather than go silent."""
        page = '<td>ubuntu-12.04.5-desktop-i386.iso</td>\n'
        updates = self._run(page=page, now=UBUNTU_FROZEN_NOW)
        self.assertIn('NEW:Ubuntu-12.04.5', updates)

    def test_off_mode_never_fetches_schedule(self):
        """'off' mode must not even fetch the schedule page."""
        nt.UbuntuChecker._EOL_MODE = 'off'
        self.addCleanup(setattr, nt.UbuntuChecker, '_EOL_MODE', 'hard')
        page = ('<td>ubuntu-12.04.5-desktop-i386.iso</td>\n'
                '<td>ubuntu-26.04-desktop-amd64.iso</td>\n')
        c = make_checker(nt.UbuntuChecker, self.tmp)
        fetched = []

        def _fetch(url, name):
            fetched.append(name)
            c._page = page + _PAD
            return True

        c.fetch = _fetch
        c.check()
        self.assertEqual(fetched, ['Ubuntu'])
        self.assertIn('NEW:Ubuntu-12.04.5', c.updates)

    def test_local_eol_line_files_alert_grouped(self):
        """Canonical keeps past-EOL ISOs on the tracker, so local files
        for a past-EOL line (12.04 in the fixture) are in upstream_set and
        the stale scan's skip hid them entirely. They must surface as one
        grouped EOL:Ubuntu-X.Y per line instead -- here both local 12.04.5
        files collapse into a single alert while the active, fully-mirrored
        24.04 line stays silent."""
        page = ('<td>ubuntu-12.04.5-dvd-amd64.iso</td>\n'
                '<td>ubuntu-12.04.5-dvd-i386.iso</td>\n'
                '<td>ubuntu-24.04-desktop-amd64.iso</td>\n')
        for name in ('ubuntu-12.04.5-dvd-amd64.iso',
                     'ubuntu-12.04.5-dvd-i386.iso',
                     'ubuntu-24.04-desktop-amd64.iso'):
            (self.tmp / name).write_bytes(b'x' * 100)
        updates = self._run(page=page,
                            status='ubuntu-12.04.5-dvd-amd64.iso '
                                   'ubuntu-12.04.5-dvd-i386.iso '
                                   'ubuntu-24.04-desktop-amd64.iso',
                            now=UBUNTU_FROZEN_NOW)
        self.assertEqual(updates, {'EOL:Ubuntu-12.04'})

    def test_local_eol_line_files_not_flagged_when_schedule_unavailable(self):
        """Fail-open: when the schedule page can't be fetched, _eol_lines()
        is None, nothing is EOL, and past-EOL local files that are still on
        the tracker raise nothing -- exactly the pre-filter behavior."""
        page = ('<td>ubuntu-12.04.5-dvd-amd64.iso</td>\n'
                '<td>ubuntu-24.04-desktop-amd64.iso</td>\n')
        for name in ('ubuntu-12.04.5-dvd-amd64.iso',
                     'ubuntu-24.04-desktop-amd64.iso'):
            (self.tmp / name).write_bytes(b'x' * 100)
        c = make_checker(nt.UbuntuChecker, self.tmp,
                         status_content='ubuntu-12.04.5-dvd-amd64.iso '
                                        'ubuntu-24.04-desktop-amd64.iso')

        def _fetch(url, name):
            if name == 'Ubuntu-EOL':
                return False
            c._page = page + _PAD
            return True

        c.fetch = _fetch
        c.check()
        self.assertEqual(c.updates, set())

    def test_local_eol_line_files_not_flagged_in_off_mode(self):
        """'off' mode disables the filter entirely: past-EOL local files
        that are still on the tracker raise nothing."""
        nt.UbuntuChecker._EOL_MODE = 'off'
        self.addCleanup(setattr, nt.UbuntuChecker, '_EOL_MODE', 'hard')
        page = ('<td>ubuntu-12.04.5-dvd-amd64.iso</td>\n'
                '<td>ubuntu-24.04-desktop-amd64.iso</td>\n')
        for name in ('ubuntu-12.04.5-dvd-amd64.iso',
                     'ubuntu-24.04-desktop-amd64.iso'):
            (self.tmp / name).write_bytes(b'x' * 100)
        updates = self._run(page=page,
                            status='ubuntu-12.04.5-dvd-amd64.iso '
                                   'ubuntu-24.04-desktop-amd64.iso')
        self.assertEqual(updates, set())

    def test_local_eol_line_file_off_tracker_alerts_eol_not_stale(self):
        """If Canonical has also removed a past-EOL line from the tracker,
        the local file is subsumed into EOL:Ubuntu-X.Y rather than reported
        as STALE -- same action (delete the files), one fewer alert."""
        page = '<td>ubuntu-24.04-desktop-amd64.iso</td>\n'
        for name in ('ubuntu-12.04.5-dvd-amd64.iso',
                     'ubuntu-24.04-desktop-amd64.iso'):
            (self.tmp / name).write_bytes(b'x' * 100)
        updates = self._run(page=page,
                            status='ubuntu-12.04.5-dvd-amd64.iso '
                                   'ubuntu-24.04-desktop-amd64.iso',
                            now=UBUNTU_FROZEN_NOW)
        self.assertEqual(updates, {'EOL:Ubuntu-12.04'})

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
        updates = self._run(status=' '.join(UBUNTU_ISOS),
                            now=UBUNTU_FROZEN_NOW)
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

    def test_current_version_kubuntu_tracked_like_plain_ubuntu(self):
        """torrent.ubuntu.com tracks all official flavors, not just plain
        ubuntu-*.iso -- confirmed directly against the live tracker_index,
        which lists kubuntu, xubuntu, lubuntu, edubuntu, ubuntu-mate, and
        others alongside plain ubuntu. A prior fix here assumed the tracker
        was Ubuntu-only and special-cased plain ubuntu-*.iso in the per-file
        STALE branch; that assumption was wrong and silently suppressed
        legitimate STALE alerts for any other flavor genuinely dropped from
        the tracker (see test_dropped_flavor_file_still_alerts_stale below).
        This test locks in the corrected behavior: a current-version Kubuntu
        file that IS present upstream is recognized as known, same as any
        plain ubuntu file would be."""
        page = (
            '<td>ubuntu-24.04-desktop-amd64.iso</td>\n'
            '<td>kubuntu-24.04-desktop-amd64.iso</td>\n'
        )
        status = 'ubuntu-24.04-desktop-amd64.iso kubuntu-24.04-desktop-amd64.iso'
        for name in ('ubuntu-24.04-desktop-amd64.iso', 'kubuntu-24.04-desktop-amd64.iso'):
            (self.tmp / name).write_bytes(b'x' * 100)
        updates = self._run(status=status, page=page)
        self.assertEqual(updates, set())

    def test_dropped_flavor_file_still_alerts_stale(self):
        """A current-version flavor file (Kubuntu here) that has genuinely
        been dropped from the tracker -- absent from upstream_isos on this
        scrape, unlike its still-tracked plain-ubuntu sibling -- must still
        alert STALE individually, exactly like a plain ubuntu-*.iso file
        would in the same situation. This is the real-world scenario the
        prior fix's 'skip anything not prefixed ubuntu-' logic got wrong."""
        page = '<td>ubuntu-24.04-desktop-amd64.iso</td>\n'
        status = 'ubuntu-24.04-desktop-amd64.iso kubuntu-24.04-desktop-amd64.iso'
        for name in ('ubuntu-24.04-desktop-amd64.iso', 'kubuntu-24.04-desktop-amd64.iso'):
            (self.tmp / name).write_bytes(b'x' * 100)
        updates = self._run(status=status, page=page)
        self.assertIn('STALE:kubuntu-24.04-desktop-amd64.iso', updates)

    def test_old_version_kubuntu_still_grouped_stale(self):
        """A non-current-version Kubuntu file should still be swept into the
        grouped STALE:Ubuntu-VER alert alongside its plain-ubuntu sibling."""
        for name in ('ubuntu-20.04-desktop-amd64.iso', 'kubuntu-20.04-desktop-amd64.iso'):
            (self.tmp / name).write_bytes(b'x' * 100)
        for name in UBUNTU_ISOS:
            (self.tmp / name).write_bytes(b'x' * 100)
        updates = self._run(status=' '.join(UBUNTU_ISOS),
                            now=UBUNTU_FROZEN_NOW)
        self.assertEqual(updates, {'STALE:Ubuntu-20.04'})

    def test_malformed_page_alerts(self):
        updates = self._run(page='<html>nothing here</html>')
        self.assertIn('MALFORMED:Ubuntu-Tracker', updates)

    def test_malformed_when_no_filename_has_parseable_version(self):
        updates = self._run(page='<td>ubuntu-README.iso</td>\n')
        self.assertIn('MALFORMED:Ubuntu-Tracker', updates)

    def test_eol_clock_is_injectable_and_month_granular(self):
        """_eol_lines() honors the injected now: the same schedule page
        flips a line from active to EOL strictly after its tier-end month,
        independent of the real date. Locks in the parameter and the strict
        '<' comparison (a line is still active during its end month)."""
        page = (
            '<table>'
            '<tr><th>Version</th><th>Code name</th><th>Docs</th><th>Release</th>'
            '<th>End of Standard Support</th><th>End of Life</th></tr>'
            '<tr><td>Ubuntu 20.04 LTS</td><td>Focal Fossa</td><td>Release notes</td>'
            '<td>Apr 23, 2020</td><td>May 2025</td><td>Apr 2035</td></tr>'
            '</table>'
        )
        c = make_checker(nt.UbuntuChecker, self.tmp)

        def _fetch(url, name):
            c._page = page + _PAD
            return True

        c.fetch = _fetch

        # 'hard' mode keys on the last tier of any kind: End of Life Apr 2035.
        self.assertEqual(c._eol_lines(now=(2035, 4)), set())
        self.assertEqual(c._eol_lines(now=(2035, 5)), {'20.04'})


# ProxmoxChecker

# The checker scrapes the exact ISO filenames from the download hrefs on the
# Proxmox downloads page, so the fixture mirrors that real structure
# (enterprise.proxmox.com/iso/*.iso links) rather than prose.
PROXMOX_PAGE = (
    '<a href="https://enterprise.proxmox.com/iso/proxmox-ve_8.2-1.iso">proxmox-ve_8.2-1.iso</a>\n'
    '<a href="https://enterprise.proxmox.com/iso/proxmox-ve_8.2-2.iso">proxmox-ve_8.2-2.iso</a>\n'
)


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
        self.assertIn('NEW:proxmox-ve_8.2-1', updates)
        self.assertIn('NEW:proxmox-ve_8.2-2', updates)

    def test_no_alert_when_in_status(self):
        updates = self._run(status='proxmox-ve_8.2-2.iso')
        self.assertNotIn('NEW:proxmox-ve_8.2-2', updates)

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

    def test_broken_symlink_not_alerted(self):
        """A dangling symlink is skipped like a zero-byte file: glob()
        returns it, but stat() would follow the missing target and raise
        FileNotFoundError, killing the checker mid-scan."""
        old = self.tmp / 'proxmox-ve_8.1-1.iso'
        old.symlink_to('no-such-target.iso')
        updates = self._run(status='proxmox-ve_8.2-2.iso')
        self.assertNotIn('STALE:proxmox-ve_8.1-1.iso', updates)

    def test_malformed_page_alerts(self):
        updates = self._run(page='<html>no versions</html>')
        self.assertIn('MALFORMED:Proxmox-Downloads', updates)

    def test_non_numeric_page_version_does_not_crash(self):
        """The extraction regex also matches a letter after 'proxmox-ve_',
        so a malformed upstream filename must be skipped when building
        page_majors instead of crashing the checker on None.group(1); its
        own NEW: alert must still fire, and so must the numeric version's."""
        page = (
            '<a href="https://enterprise.proxmox.com/iso/proxmox-ve_9.1-1.iso">proxmox-ve_9.1-1.iso</a>\n'
            '<a href="https://enterprise.proxmox.com/iso/proxmox-ve_beta.iso">proxmox-ve_beta.iso</a>\n'
        )
        updates = self._run(page=page)
        self.assertIn('NEW:proxmox-ve_9.1-1', updates)
        self.assertIn('NEW:proxmox-ve_beta', updates)


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

    def test_stray_directory_without_version_suffix_not_treated_as_dropped(self):
        """Fedora-*-*/ matches a directory even if its last hyphen-segment
        isn't actually a version number (e.g. a partial rename leaving
        behind 'Fedora-Workstation-Live-x86_64', with no trailing -42).
        rsplit('-', 1)[-1] would extract 'x86_64' and, since that never
        matches any real tracker version, produce a spurious
        DROPPED:Fedora-x86_64. Fedora versions are always bare integers
        (confirmed against the live tracker), so anything non-digit
        should be silently ignored instead."""
        (self.tmp / 'Fedora-Workstation-Live-x86_64').mkdir()
        updates = self._run()
        self.assertFalse(
            any(u.startswith('DROPPED:Fedora-x86_64') for u in updates),
            f'Unexpected spurious DROPPED alert: {updates}',
        )

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

    def test_dict_top_level_alerts_malformed(self):
        # A wrapped payload (top-level object instead of the bare list)
        # is a structure change, not a usable tracker page
        updates = self._run(page='{"name": "42"}')
        self.assertIn('MALFORMED:Fedora-Tracker', updates)

    def test_all_malformed_entries_alert_malformed(self):
        page = json.dumps([
            42,
            {'name': 41},
            {'name': '40', 'torrents': 'not a list'},
        ])
        updates = self._run(page=page)
        self.assertIn('MALFORMED:Fedora-Tracker', updates)

    def test_malformed_entries_do_not_break_valid_versions(self):
        """Malformed entries are skipped individually: the valid version
        42 is still tracked (no NEW alert since its dir exists) and no
        MALFORMED or EXCEPTION fires for the garbage around it."""
        (self.tmp / 'Fedora-Workstation-Live-x86_64-42').mkdir()
        page = json.dumps([
            'garbage',
            {'name': 41, 'torrents': []},
            {'name': '41', 'torrents': 'not a list'},
            {
                'name': '42',
                'torrents': [
                    {'torrent': 'Fedora-Workstation-Live-x86_64-42.torrent'},
                ],
            },
        ])
        updates = self._run(
            status='Fedora-Workstation-Live-x86_64-42',
            page=page,
        )
        self.assertNotIn('MALFORMED:Fedora-Tracker', updates)
        self.assertNotIn('NEW:Fedora-42', updates)
        self.assertFalse(any(u.startswith('EXCEPTION') for u in updates))

    def test_null_torrents_key_entry_is_skipped(self):
        """A 'torrents' key present but null is unusable; the entry is
        skipped (the local 41 dir then reads as DROPPED) instead of
        crashing the checker."""
        (self.tmp / 'Fedora-Workstation-Live-x86_64-41').mkdir()
        (self.tmp / 'Fedora-Workstation-Live-x86_64-42').mkdir()
        page = json.dumps([
            {'name': '41', 'torrents': None},
            {
                'name': '42',
                'torrents': [
                    {'torrent': 'Fedora-Workstation-Live-x86_64-42.torrent'},
                ],
            },
        ])
        updates = self._run(
            status=(
                'Fedora-Workstation-Live-x86_64-41 '
                'Fedora-Workstation-Live-x86_64-42'
            ),
            page=page,
        )
        self.assertIn('DROPPED:Fedora-41', updates)
        self.assertNotIn('MALFORMED:Fedora-Tracker', updates)
        self.assertNotIn('NEW:Fedora-42', updates)
        self.assertFalse(any(u.startswith('EXCEPTION') for u in updates))

    def test_non_dict_torrent_items_are_skipped(self):
        """Malformed items inside a valid entry's torrent list are
        skipped individually; the one usable torrent keeps the version
        tracked and its directory recognized as current."""
        (self.tmp / 'Fedora-Workstation-Live-x86_64-42').mkdir()
        page = json.dumps([
            {
                'name': '42',
                'torrents': [
                    'not a dict',
                    {'no_torrent_key': True},
                    {'torrent': 7},
                    {'torrent': 'Fedora-Workstation-Live-x86_64-42.torrent'},
                ],
            },
        ])
        updates = self._run(
            status='Fedora-Workstation-Live-x86_64-42',
            page=page,
        )
        self.assertNotIn('MALFORMED:Fedora-Tracker', updates)
        self.assertNotIn('NEW:Fedora-42', updates)
        self.assertNotIn('STALE:Fedora-Workstation-Live-x86_64-42', updates)


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

    def test_rsync_failure_updates_display(self):
        """A plain nonzero-exit rsync failure should update the live status
        board too, matching the timeout branch's self._debug() call --
        previously only the timeout path did this, leaving --verbose mode
        silent about an ordinary rsync failure."""
        ftrack_path = self.tmp / 'f.json'
        ftrack_path.write_text('{}')
        ftrack = nt.FailureTracker(ftrack_path, 3)
        display = MagicMock()
        c = make_checker(nt.DebianChecker, self.tmp, failures=ftrack, display=display)
        with patch('subprocess.run', return_value=self._rsync(returncode=11)):
            c.check()
        display.update.assert_any_call('DebianChecker', 'rsync failed (exit 11)')

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

    def _run_main(self, rsync_present=True, argv=None, extra_patches=None):
        patches = {
            'ISO_DIR': self.iso_dir,
            'STATUS_FILE': self.iso_dir / 'status.txt',
            'FAIL_FILE': self.tmp / 'failures.json',
            'LOCK_FILE': self.tmp / 'lock',
        }
        if extra_patches:
            patches.update(extra_patches)
        ctx_managers = [patch.object(nt, k, v) for k, v in patches.items()]
        # _run() bails out early when rsync is absent, so pin shutil.which to a
        # deterministic value: the suite must behave identically whether or not
        # the CI image happens to ship rsync (the almalinux:10 container does
        # not). Default to present so these tests reach the checks under test;
        # test_missing_rsync_returns_1 opts out with rsync_present=False.
        ctx_managers.append(patch.object(
            nt.shutil, 'which',
            return_value=('/usr/bin/rsync' if rsync_present else None)))
        for ctx in ctx_managers:
            ctx.start()
        try:
            # Explicit argv (default []) so the test runner's own
            # command line never leaks into argument parsing.
            ret = nt.main(argv=argv if argv is not None else [])
        finally:
            for ctx in ctx_managers:
                ctx.stop()
        return ret

    def test_lock_held_by_another_open_prevents_run(self):
        """A distinct open() of the lock file already holding flock() must
        make main() return 1 without touching ISO_DIR/STATUS_FILE at all --
        two separate open() calls on the same file conflict via flock()
        even within one process (verified directly: this isn't relying on
        an assumption about cross-process semantics)."""
        lock_path = self.tmp / 'lock'
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        holder_fd = open(lock_path, 'w')
        fcntl.flock(holder_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            # Deliberately do NOT create iso_dir/status.txt -- if main()
            # incorrectly proceeded past the lock check, it would fail for
            # a DIFFERENT reason (missing ISO_DIR) and still return 1,
            # which would make this test pass for the wrong reason. Check
            # stdout instead to confirm it's actually the lock message.
            buf = io.StringIO()
            with patch('sys.stdout', buf):
                ret = self._run_main()
            self.assertEqual(ret, 1)
            self.assertIn('another instance is already running', buf.getvalue())
        finally:
            holder_fd.close()

    def test_lock_released_after_clean_run_allows_next_invocation(self):
        """The lock must not leak past a single run -- a second, later
        invocation needs to succeed normally once the first has finished."""
        self.iso_dir.mkdir()
        (self.iso_dir / 'status.txt').write_text('Sum: 1')

        def noop_run(self_inner):
            return set()

        checker_patches = [patch.object(cls, 'run', noop_run) for cls in nt.CHECKERS]
        for p in checker_patches:
            p.start()
        try:
            first = self._run_main()
            second = self._run_main()
        finally:
            for p in checker_patches:
                p.stop()

        self.assertEqual(first, 0)
        self.assertEqual(second, 0)

    def test_lock_prevents_concurrent_run_across_real_processes(self):
        """End-to-end confirmation with an actual separate OS process
        holding the lock, not just a second file descriptor in this same
        test process -- the scenario this exists for (a real overlapping
        invocation) rather than just the mechanism in isolation."""
        lock_path = self.tmp / 'lock'
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        holder = subprocess.Popen([
            sys.executable, '-c',
            'import fcntl, time, sys\n'
            'f = open(sys.argv[1], "w")\n'
            'fcntl.flock(f, fcntl.LOCK_EX)\n'
            'time.sleep(5)\n',
            str(lock_path),
        ])
        try:
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                probe = open(lock_path, 'w')
                try:
                    fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    fcntl.flock(probe, fcntl.LOCK_UN)
                except BlockingIOError:
                    break  # the child process now holds the lock
                finally:
                    probe.close()
                time.sleep(0.05)
            else:
                self.fail('child process never acquired the lock in time')

            ret = self._run_main()
            self.assertEqual(ret, 1)
        finally:
            holder.terminate()
            holder.wait(timeout=5)

    def test_missing_iso_dir_returns_1(self):
        ret = self._run_main()
        self.assertEqual(ret, 1)

    def test_missing_status_file_returns_1(self):
        self.iso_dir.mkdir()
        ret = self._run_main()
        self.assertEqual(ret, 1)

    def test_empty_status_file_returns_1(self):
        self.iso_dir.mkdir()
        (self.iso_dir / 'status.txt').write_text('')
        ret = self._run_main()
        self.assertEqual(ret, 1)

    def test_malformed_status_file_returns_1(self):
        """status.txt without 'Sum:' is rejected."""
        self.iso_dir.mkdir()
        (self.iso_dir / 'status.txt').write_text('some content but no sum line')
        ret = self._run_main()
        self.assertEqual(ret, 1)

    def test_missing_rsync_returns_1(self):
        """When rsync is not installed, main() bails out and returns 1."""
        self.iso_dir.mkdir()
        (self.iso_dir / 'status.txt').write_text('Sum: 1')
        ret = self._run_main(rsync_present=False)
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
            ret = self._run_main()
        finally:
            for p in checker_patches:
                p.stop()

        self.assertEqual(ret, 1)

    def test_one_checker_crashing_does_not_abort_the_run(self):
        """An unexpected exception in one checker's check() (patched here,
        not run() -- the real, unpatched run() is what contains the fix)
        must not crash main() or prevent it from finishing cleanly."""
        self.iso_dir.mkdir()
        (self.iso_dir / 'status.txt').write_text('Sum: 1')

        def ok_check(self_inner):
            pass  # no alerts

        def raise_check(self_inner):
            raise RuntimeError('boom')

        checker_patches = [patch.object(cls, 'check', ok_check) for cls in nt.CHECKERS[1:]]
        checker_patches.append(patch.object(nt.CHECKERS[0], 'check', raise_check))
        for p in checker_patches:
            p.start()
        try:
            ret = self._run_main()  # must not raise
        finally:
            for p in checker_patches:
                p.stop()

        # Non-zero because of the crashing checker's own EXCEPTION: alert,
        # not because main() itself blew up.
        self.assertEqual(ret, 1)

    def test_clean_run_with_display_has_no_trailing_blank_line(self):
        """With the live status display active (--verbose forces it even
        without a real tty) and zero alerts, there's nothing for a
        separator blank line to separate the status board from — it
        shouldn't be there."""
        self.iso_dir.mkdir()
        (self.iso_dir / 'status.txt').write_text('Sum: 1')

        def noop_run(self_inner):
            return set()

        checker_patches = [patch.object(cls, 'run', noop_run) for cls in nt.CHECKERS]
        for p in checker_patches:
            p.start()
        try:
            buf = io.StringIO()
            with patch('sys.stderr', buf):
                ret = self._run_main(argv=['--verbose'])
        finally:
            for p in checker_patches:
                p.stop()

        self.assertEqual(ret, 0)
        self.assertFalse(
            buf.getvalue().endswith('\n\n'),
            f'Unexpected trailing blank line: {buf.getvalue()!r}',
        )

    def test_alerts_run_with_display_still_separates_alerts(self):
        """With the display active and real alerts, the status board
        should still get a blank line separating it from the alert list
        that follows on stdout."""
        self.iso_dir.mkdir()
        (self.iso_dir / 'status.txt').write_text('Sum: 1')

        def alert_run(self_inner):
            return {'NEW:something'}

        checker_patches = [patch.object(cls, 'run', alert_run) for cls in nt.CHECKERS]
        for p in checker_patches:
            p.start()
        try:
            buf = io.StringIO()
            with patch('sys.stderr', buf):
                ret = self._run_main(argv=['--verbose'])
        finally:
            for p in checker_patches:
                p.stop()

        self.assertEqual(ret, 1)
        self.assertTrue(
            buf.getvalue().endswith('\n\n'),
            f'Expected a separating blank line before the alerts: {buf.getvalue()!r}',
        )

    def test_help_exits_zero_and_lists_all_checkers(self):
        """--help prints usage plus the generated available-checkers
        list (one entry per CHECKERS element) and exits 0, argparse's
        convention for a successful help request."""
        buf = io.StringIO()
        with patch('sys.stdout', buf), \
             self.assertRaises(SystemExit) as cm:
            nt._parse_args(['--help'])
        self.assertEqual(cm.exception.code, 0)
        out = buf.getvalue()
        self.assertIn('usage:', out)
        for cls in nt.CHECKERS:
            self.assertIn(cls.__name__.removesuffix('Checker').lower(), out)

    def test_unknown_checker_name_is_usage_error_before_lock(self):
        """An unknown checker name is a usage error (exit 2, valid
        selectors listed) and must be rejected before the lock is
        acquired -- a failed invocation must not create the lockfile."""
        buf = io.StringIO()
        with patch('sys.stderr', buf):
            ret = nt.main(argv=['definitely-not-a-checker'])
        self.assertEqual(ret, 2)
        out = buf.getvalue()
        self.assertIn("unknown checker 'definitely-not-a-checker'", out)
        self.assertIn('choose from:', out)
        self.assertFalse((self.tmp / 'lock').exists())

    def test_resolve_checker_accepts_short_and_class_names(self):
        """The selector is case-insensitive and accepts the short name
        as well as the full class name; None selects the full suite."""
        for name in ('mint', 'Mint', 'MintChecker', 'MINTCHECKER'):
            self.assertIs(nt._resolve_checker(name), nt.MintChecker)
        self.assertIs(nt._resolve_checker(None), None)

    def test_resolve_checker_unknown_name_lists_valid_selectors(self):
        """The ValueError message must name every valid selector so the
        error alone tells the user what to type instead."""
        with self.assertRaises(ValueError) as cm:
            nt._resolve_checker('debian-edu')
        for cls in nt.CHECKERS:
            self.assertIn(
                cls.__name__.removesuffix('Checker').lower(), str(cm.exception))

    def test_single_checker_selector_runs_only_that_checker(self):
        """'mint' selects only MintChecker: no other checker's run() is
        invoked, and the selected checker's alerts still drive the exit
        code and the alert output."""
        self.iso_dir.mkdir()
        (self.iso_dir / 'status.txt').write_text('Sum: 1')
        called: list[str] = []

        def run_for(cls):
            def run(self_inner):
                called.append(cls.__name__)
                if cls is nt.MintChecker:
                    return {'NEW:linuxmint-test.iso'}
                return set()
            return run

        checker_patches = [
            patch.object(cls, 'run', run_for(cls)) for cls in nt.CHECKERS]
        for p in checker_patches:
            p.start()
        try:
            out = io.StringIO()
            with patch('sys.stdout', out):
                ret = self._run_main(argv=['mint'])
        finally:
            for p in checker_patches:
                p.stop()

        self.assertEqual(ret, 1)
        self.assertEqual(called, ['MintChecker'])
        self.assertIn('NEW:linuxmint-test.iso', out.getvalue())


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
