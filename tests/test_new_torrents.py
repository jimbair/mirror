#!/usr/bin/env python3
"""Tests for new-torrents.py

Run from the repo root:
    python3 test_new_torrents.py

Or with verbose output:
    python3 test_new_torrents.py -v

No external dependencies required. The script under test is imported directly;
adjust SCRIPT_PATH below if you rename or move files.
"""

import importlib.util
import json
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Import the script as a module. Since new-torrents.py has a hyphen in its
# name, importlib is required rather than a plain import statement.
# ---------------------------------------------------------------------------
SCRIPT_PATH = Path(__file__).parent.parent / 'new-torrents.py'

spec = importlib.util.spec_from_file_location('new_torrents', SCRIPT_PATH)
nt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(nt)


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# FailureTracker
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Checker base: fetch() and check_iso() / check_dir()
# ---------------------------------------------------------------------------

class TestCheckerBase(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.ftrack = nt.FailureTracker(self.tmp / 'f.json', 3)

    def _checker(self, status=''):
        # Use AlmaChecker as a concrete stand-in for the abstract base
        return make_checker(nt.AlmaChecker, self.tmp, status_content=status,
                            failures=self.ftrack)

    # check_iso -----------------------------------------------------------

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

    # check_dir -----------------------------------------------------------

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

    # fetch() failure tracking -------------------------------------------

    def test_fetch_failure_increments_counter(self):
        c = self._checker()
        with patch('urllib.request.urlopen',
                   side_effect=urllib.error.URLError('connection refused')):
            c.fetch('https://example.com/x', 'example')
        self.assertEqual(self.ftrack._counts.get('example', 0), 1)

    def test_fetch_success_clears_counter(self):
        self.ftrack._counts['example'] = 2
        self.ftrack._dirty = True
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


# ---------------------------------------------------------------------------
# MintChecker
# ---------------------------------------------------------------------------

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

    def test_missing_isos_raise_new(self):
        updates = self._run()
        self.assertIn('NEW:linuxmint-22.0-cinnamon-64bit.iso', updates)
        self.assertIn('NEW:linuxmint-22.0-mate-64bit.iso', updates)

    def test_no_alert_when_isos_in_status(self):
        updates = self._run(status=' '.join(MINT_ISOS))
        self.assertNotIn('NEW:linuxmint-22.0-cinnamon-64bit.iso', updates)
        self.assertNotIn('NEW:linuxmint-22.0-mate-64bit.iso', updates)

    def test_stale_iso_alerted(self):
        old = self.tmp / 'linuxmint-21.3-cinnamon-64bit.iso'
        old.write_bytes(b'x' * 100)
        updates = self._run(status=' '.join(MINT_ISOS))
        self.assertIn('STALE:linuxmint-21.3-cinnamon-64bit.iso', updates)

    def test_missing_all_local_isos_alerts(self):
        # ISOs are in status (so no NEW:) but none exist on disk
        updates = self._run(status=' '.join(MINT_ISOS))
        self.assertIn('MISSING:linuxmint-*.iso', updates)

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


# ---------------------------------------------------------------------------
# ArchChecker
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# CachyChecker
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# UbuntuChecker
# ---------------------------------------------------------------------------

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

    def test_new_isos_alert(self):
        updates = self._run()
        self.assertIn('NEW:ubuntu-24.04-desktop-amd64.iso', updates)

    def test_beta_and_snapshot_filtered(self):
        """Beta and snapshot ISOs must not produce NEW: alerts."""
        updates = self._run()
        self.assertFalse(
            any('beta' in u.lower() or 'snapshot' in u.lower() for u in updates),
            f'Beta/snapshot leaked into alerts: {updates}',
        )

    def test_no_alert_when_in_status(self):
        updates = self._run(status=' '.join(UBUNTU_ISOS))
        self.assertNotIn('NEW:ubuntu-24.04-desktop-amd64.iso', updates)

    def test_stale_iso_alerted(self):
        old = self.tmp / 'ubuntu-20.04-desktop-amd64.iso'
        old.write_bytes(b'x' * 100)
        updates = self._run(status=' '.join(UBUNTU_ISOS))
        self.assertIn('STALE:ubuntu-20.04-desktop-amd64.iso', updates)

    def test_no_local_isos_alerts_missing(self):
        # ISOs in status but none on disk
        updates = self._run(status=' '.join(UBUNTU_ISOS))
        self.assertIn('MISSING:*buntu*.iso', updates)

    def test_kubuntu_matches_glob(self):
        """*buntu* glob catches Kubuntu; its presence should suppress MISSING."""
        p = self.tmp / 'kubuntu-24.04-desktop-amd64.iso'
        p.write_bytes(b'x' * 100)
        updates = self._run(status=' '.join(UBUNTU_ISOS))
        self.assertNotIn('MISSING:*buntu*.iso', updates)

    def test_malformed_page_alerts(self):
        updates = self._run(page='<html>nothing here</html>')
        self.assertIn('MALFORMED:Ubuntu-Tracker', updates)


# ---------------------------------------------------------------------------
# ProxmoxChecker
# ---------------------------------------------------------------------------

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
        # Both versions appear on page; only the latest (8.2-2) triggers NEW:
        # because check_iso fires for every version found
        updates = self._run()
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


# ---------------------------------------------------------------------------
# FedoraChecker
# ---------------------------------------------------------------------------

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
        updates = self._run(
            status='Fedora-Workstation-Live-x86_64-42 Fedora-Server-dvd-x86_64-42',
        )
        self.assertNotIn('NEW:Fedora-42', updates)

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


# ---------------------------------------------------------------------------
# AlmaChecker
# ---------------------------------------------------------------------------

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

    def test_stale_point_release_alerted(self):
        # Both 9.3 and 9.4 dirs exist; 9.3 should be STALE
        (self.tmp / 'AlmaLinux-9.3-x86_64').mkdir()
        (self.tmp / 'AlmaLinux-9.4-x86_64').mkdir()
        updates = self._run(status='AlmaLinux-9.4-x86_64')
        self.assertIn('STALE:AlmaLinux-9.3-x86_64', updates)

    def test_dropped_major_alerts(self):
        # Major 8 dirs exist locally but absent from the page
        (self.tmp / 'AlmaLinux-8.10-x86_64').mkdir()
        updates = self._run()
        self.assertIn('DROPPED:AlmaLinux-8', updates)

    def test_malformed_page_alerts(self):
        updates = self._run(page='<html>no isos links</html>')
        self.assertIn('MALFORMED:AlmaLinux-isos.html', updates)


# ---------------------------------------------------------------------------
# DebianChecker (mocks subprocess.run)
# ---------------------------------------------------------------------------

DEBIAN_RSYNC_OUTPUT = (
    'drwxr-xr-x          4,096 2025/01/01 00:00:00 .\n'
    'drwxr-xr-x          4,096 2025/01/01 00:00:00 12.9.0-amd64-DVD-1\n'
    '-rw-r--r-- 982024192 2025/01/01 00:00:00 '
    '12.9.0-amd64-DVD-1/debian-12.9.0-amd64-DVD-1.torrent\n'
    'drwxr-xr-x          4,096 2025/01/01 00:00:00 12.9.0-arm64-DVD-1\n'
    '-rw-r--r-- 982024192 2025/01/01 00:00:00 '
    '12.9.0-arm64-DVD-1/debian-12.9.0-arm64-DVD-1.torrent\n'
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

    def test_new_iso_alerts(self):
        updates = self._run()
        self.assertIn('NEW:debian-12.9.0-amd64-DVD-1', updates)
        self.assertIn('NEW:debian-12.9.0-arm64-DVD-1', updates)

    def test_no_alert_when_in_status(self):
        updates = self._run(status='debian-12.9.0-amd64-DVD-1')
        self.assertNotIn('NEW:debian-12.9.0-amd64-DVD-1', updates)

    def test_stale_iso_alerted(self):
        old = self.tmp / 'debian-12.8.0-amd64-DVD-1.iso'
        old.write_bytes(b'x' * 100)
        status = 'debian-12.9.0-amd64-DVD-1 debian-12.9.0-arm64-DVD-1'
        updates = self._run(status=status)
        self.assertIn('STALE:debian-12.8.0-amd64-DVD-1.iso', updates)

    def test_no_local_isos_alerts_missing(self):
        status = 'debian-12.9.0-amd64-DVD-1 debian-12.9.0-arm64-DVD-1'
        updates = self._run(status=status)
        self.assertIn('MISSING:debian-*.iso', updates)

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

    def test_malformed_rsync_output_alerts(self):
        # rsync succeeds but returns no .torrent lines
        updates = self._run(rsync_result=self._rsync(stdout='drwxr-xr-x 4,096 2025/01/01 .\n'))
        self.assertIn('MALFORMED:Debian-Tracker', updates)


# ---------------------------------------------------------------------------
# main() guard tests
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# ver_key
# ---------------------------------------------------------------------------

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
