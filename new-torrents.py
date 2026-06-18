#!/usr/bin/env python3
# Check for updates to torrents for our mirror
# https://mirror.tsue.net/
#
# This script runs once an hour via cron and raises alerts via healthchecks.io
# We send the output as a POST to /fail in the event of a non-zero exit.

import gzip
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.request
import urllib.error
import zlib
from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# Where transmission stores downloaded torrents
ISO_DIR = Path('/var/lib/transmission/Downloads')

# Mirror status page; the bottom of this file is transmission-remote -l output
STATUS_FILE = ISO_DIR / 'status.txt'

# Number of consecutive fetch failures before a domain is reported as down.
# Transient outages are silently ignored until this threshold is reached.
FAIL_THRESHOLD = 3
# XDG-compliant config dir; created on first run if absent
FAIL_FILE = Path.home() / '.config' / 'new-torrents' / 'failures.json'


###########
# HELPERS #
###########

class FailureTracker:
    """Persists consecutive fetch failure counts across runs.

    Counts are loaded from JSON at construction, held in memory while checkers
    run, then written back once via save() only if they changed. This avoids
    concurrent read/write races from the threaded checkers and skips unnecessary
    disk writes on clean runs. Surviving reboots prevents a reboot from silently
    resetting an ongoing outage counter.
    """

    def __init__(self, path: Path, threshold: int) -> None:
        self._path      = path         # noqa: E221
        self._threshold = threshold    # noqa: E221
        self._counts    = self._load() # noqa: E221
        self._dirty     = False        # noqa: E221

    def increment(self, name: str) -> None:
        """Increment the failure counter for name."""
        self._counts[name] = self._counts.get(name, 0) + 1
        self._dirty = True

    def clear(self, name: str) -> None:
        """Remove the failure counter for name if one exists."""
        if name in self._counts:
            del self._counts[name]
            self._dirty = True

    def at_threshold(self, name: str) -> bool:
        """Return True if name has reached the alert threshold."""
        return self._counts.get(name, 0) >= self._threshold

    def save(self) -> None:
        """Write counts to disk if they changed. Called once after all checkers finish."""
        if not self._dirty:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(self._counts, indent=2))

    # Internal

    def _load(self) -> dict[str, int]:
        if not self._path.exists():
            return {}
        try:
            return json.loads(self._path.read_text())
        except (json.JSONDecodeError, OSError):
            return {}


def ver_key(v: str) -> tuple[int, ...]:
    """Version sort key: splits on any non-numeric delimiter."""
    return tuple(int(x) for x in re.findall(r'\d+', v))


##################
# STATUS DISPLAY #
##################

class StatusDisplay:
    """Live per-checker status display for --verbose mode.

    Maintains one status line per checker, redrawn in place using ANSI cursor
    control. Each line shows elapsed time and the checker's current activity.
    A background thread refreshes the display every second so long-running
    checkers (e.g. Debian rsync) show elapsed time ticking even when silent.

    Output is left in place on close() so the final state is visible after
    the run completes. Alerts print below the display block.

    Thread-safe: all mutations go through a single lock.

    Color coding:
      dim    - waiting to start
      yellow - running
      green  - finished, no alerts
      cyan   - finished with alerts
      red    - reserved for future error/exception states
    """

    _ERASE_LINE = '\x1b[2K'   # noqa: E221
    _CURSOR_UP  = '\x1b[{}A'  # noqa: E221
    _RESET      = '\x1b[0m'   # noqa: E221
    _DIM        = '\x1b[2m'   # noqa: E221
    _YELLOW     = '\x1b[33m'  # noqa: E221
    _GREEN      = '\x1b[32m'  # noqa: E221
    _CYAN       = '\x1b[36m'  # noqa: E221
    _RED        = '\x1b[31m'  # noqa: E221

    def __init__(self, names: list[str]) -> None:
        self._names                   = names                          # noqa: E221
        self._lock                    = threading.Lock()               # noqa: E221
        self._status: dict[str, str]  = {n: 'waiting' for n in names}  # noqa: E221
        self._start: dict[str, float] = {}                             # noqa: E221
        self._alerts: dict[str, int]  = {}                             # noqa: E221
        self._done: dict[str, bool]   = {n: False for n in names}      # noqa: E221

        # Measure terminal width once; used to compute physical row count when
        # a rendered line wraps. Falls back to 80 if stderr is not a tty.
        try:
            self._term_width = os.get_terminal_size(sys.stderr.fileno()).columns
        except OSError:
            self._term_width = 80

        # Reserve space by printing the initial waiting lines
        for name in names:
            print(f'  {self._DIM}{name:<20}{self._RESET} waiting', file=sys.stderr)

        # Background thread redraws every second so elapsed timers tick
        # for silent checkers (e.g. Debian rsync)
        self._stop_refresh = threading.Event()
        self._refresh_thread = threading.Thread(target=self._refresh_loop, daemon=True)
        self._refresh_thread.start()

    # Public interface called by Checker

    def update(self, name: str, msg: str) -> None:
        with self._lock:
            self._status[name] = msg
            self._redraw()

    def start(self, name: str) -> None:
        with self._lock:
            self._start[name] = time.monotonic()
            self._status[name] = 'running'
            self._redraw()

    def finish(self, name: str, alert_count: int) -> None:
        with self._lock:
            self._done[name] = True
            self._alerts[name] = alert_count
            elapsed = time.monotonic() - self._start.get(name, time.monotonic())
            noun = 'alert' if alert_count == 1 else 'alerts'
            self._status[name] = f'done in {elapsed:.1f}s — {alert_count} {noun}'
            self._redraw()

    def close(self) -> None:
        """Stop the refresh thread and leave final state visible."""
        self._stop_refresh.set()
        self._refresh_thread.join()
        with self._lock:
            self._redraw()
        print(file=sys.stderr)  # blank line before alerts

    # Internal

    def _refresh_loop(self) -> None:
        """Redraw every second so elapsed timers tick for silent checkers."""
        while not self._stop_refresh.wait(timeout=1.0):
            with self._lock:
                self._redraw()

    def _physical_rows(self, rendered: str) -> int:
        """Number of terminal rows a rendered line occupies after wrapping."""
        visible = re.sub(r'\x1b\[[^m]*m', '', rendered)
        if self._term_width <= 0:
            return 1
        # Ceiling division: how many full terminal rows does this line consume?
        return max(1, (len(visible) + self._term_width - 1) // self._term_width)

    def _move_to_top(self, rendered_lines: list[str]) -> None:
        total_rows = sum(self._physical_rows(line) for line in rendered_lines)
        if total_rows:
            print(self._CURSOR_UP.format(total_rows), file=sys.stderr, end='')

    def _redraw(self) -> None:
        rendered = [self._render_line(name) for name in self._names]
        self._move_to_top(rendered)
        for line in rendered:
            rows = self._physical_rows(line)
            # Erase every physical row the previous render of this line occupied,
            # then move the cursor back to the first row before printing the new content.
            # This handles lines that wrapped differently between redraws.
            for _ in range(rows):
                print(self._ERASE_LINE, file=sys.stderr, end='\n')
            print(self._CURSOR_UP.format(rows), file=sys.stderr, end='')
            print(line, file=sys.stderr)

    def _render_line(self, name: str) -> str:
        status = self._status[name]
        if self._done[name]:
            color  = self._CYAN if self._alerts.get(name, 0) else self._GREEN  # noqa: E221
            timing = ''
        elif name in self._start:
            color  = self._YELLOW                                              # noqa: E221
            secs   = time.monotonic() - self._start[name]                      # noqa: E221
            timing = f' {self._DIM}({secs:.1f}s){self._RESET}'
        else:
            color  = self._DIM                                                 # noqa: E221
            timing = ''
        return f'  {color}{name:<20}{self._RESET} {status}{timing}'


################
# BASE CHECKER #
################

class Checker(ABC):
    """Base class for all distro checkers.

    Each subclass implements check() and calls self.alert(), self.check_iso(),
    self.check_dir(), and self.fetch() rather than touching any module globals.
    After run() returns, self.updates contains all alerts raised.
    """

    def __init__(self, iso_dir: Path, status_content: str,
                 failures: FailureTracker,
                 display: 'StatusDisplay | None' = None) -> None:
        self.iso_dir = iso_dir
        self.status_content = status_content
        self.updates: set[str] = set()
        self._page: str = ''
        self._failures = failures
        self._display = display
        self._name = self.__class__.__name__

    def _debug(self, msg: str) -> None:
        if self._display:
            self._display.update(self._name, msg)

    # Public interface called by subclasses

    def alert(self, name: str) -> None:
        self._debug(f'alert {name}')
        self.updates.add(name)

    def fetch(self, url: str, name: str) -> bool:
        """Fetch url into self._page. Tracks consecutive failures per name.
        Returns True on success, False on failure.
        """
        domain = url.split('/')[2]
        self._debug(f'fetch {url}')
        try:
            req = urllib.request.Request(
                url,
                # Mimic curl's UA; some servers (e.g. Proxmox) return stripped
                # pages to non-browser agents
                headers={'Accept-Encoding': 'gzip, deflate', 'User-Agent': 'curl/8.5.0'},
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                raw = resp.read()
                encoding = resp.headers.get_content_charset('utf-8')
                ce = resp.headers.get('Content-Encoding', '')
                if ce == 'gzip':
                    raw = gzip.decompress(raw)
                elif ce == 'deflate':
                    raw = zlib.decompress(raw)
                self._page = raw.decode(encoding, errors='replace')

            self._debug(f'fetch ok ({len(self._page)} bytes)')
            self._failures.clear(name)
            return True

        except (urllib.error.URLError, OSError) as e:
            self._debug(f'fetch failed: {e}')
            self._page = ''
            self._failures.increment(name)
            # Check threshold against in-memory state; no disk read needed
            if self._failures.at_threshold(name):
                self.alert(domain)
            return False

    def body_ok(self, alert_name: str, min_len: int = 250) -> bool:
        """Return False and alert if self._page is empty or below min_len bytes.
        A short response usually means a transient error page or a CDN block
        rather than real content; 250 bytes is safely below any valid index page.
        """
        if not self._page or len(self._page) < min_len:
            self.alert(alert_name)
            return False
        return True

    def check_iso(self, iso: str, new_alert: str = '') -> None:
        """Check a flat ISO file against transmission status and local disk."""
        if not new_alert:
            new_alert = f'NEW:{iso}'
        # Transmission knows about this ISO; nothing to do
        if iso in self.status_content:
            return
        path = self.iso_dir / iso
        # ISO is on disk but transmission has no record of it
        if path.exists() and path.stat().st_size > 0:
            self.alert(f'ORPHAN:{iso}')
        # ISO is not on disk and not known to transmission
        else:
            self.alert(new_alert)

    def check_dir(self, directory: str) -> None:
        """Check a torrent directory against transmission status and local disk."""
        # Transmission knows about this directory; nothing to do
        if directory in self.status_content:
            return
        # Directory is on disk but transmission has no record of it
        if (self.iso_dir / directory).is_dir():
            self.alert(f'ORPHAN:{directory}')
        # Directory is not on disk and not known to transmission
        else:
            self.alert(f'NEW:{directory}')

    def run(self) -> set[str]:
        """Run the check and return accumulated alerts."""
        if self._display:
            self._display.start(self._name)
        self.check()
        if self._display:
            self._display.finish(self._name, len(self.updates))
        return self.updates

    @abstractmethod
    def check(self) -> None:
        """Perform all checks for this distro. Implemented by each subclass."""


####################
# DISTRO SUBCLASSES #
####################

class MintChecker(Checker):
    """Linux Mint — scrapes pub.linuxmint.io/stable/ for the current version directory.

    Alerts:
      NEW:ISO                  - index ISO absent from disk and unknown to transmission
      ORPHAN:ISO               - index ISO present on disk but unknown to transmission
      STALE:ISO                - local linuxmint-*.iso not present in current version directory
      MISSING:linuxmint-*.iso  - no Linux Mint ISOs found on our disk at all
      MALFORMED:Linux-Mint     - stable index returned no version directories
      MALFORMED:Linux-Mint-VER - version directory returned no ISOs
    """

    def check(self) -> None:
        if not self.fetch('https://pub.linuxmint.io/stable/', 'Linux-Mint'):
            return
        if not self.body_ok('pub.linuxmint.io'):
            return

        versions = re.findall(r'href="([0-9]+\.[0-9]+)/"', self._page)
        # Stable index structure could change; alert and bail if it does
        if not versions:
            self.alert('MALFORMED:Linux-Mint')
            return

        current = sorted(versions, key=ver_key)[-1]

        if not self.fetch(f'https://pub.linuxmint.io/stable/{current}/', 'Linux-Mint-VER'):
            return
        if not self.body_ok('pub.linuxmint.io'):
            return

        upstream_isos = sorted(re.findall(r'href="(linuxmint-[^"]+\.iso)"', self._page))
        # Version directory structure could change; alert and bail if it does
        if not upstream_isos:
            self.alert(f'MALFORMED:Linux-Mint-{current}')
            return

        for iso in upstream_isos:
            self.check_iso(iso)

        local_isos = sorted(self.iso_dir.glob('linuxmint-*.iso'))
        if not local_isos:
            self.alert('MISSING:linuxmint-*.iso')
            return

        for path in local_isos:
            if path.name not in set(upstream_isos):
                self.alert(f'STALE:{path.name}')


class CachyChecker(Checker):
    """CachyOS — scrapes cachyos.org/download/ for HTML-entity-encoded torrent URLs.

    Alerts:
      NEW:CachyOS-YYMMDD          - current release not present on local disk
      ORPHAN:cachyos-EDITION.iso  - current ISO on disk but unknown to transmission
      STALE:cachyos-OLD.iso       - local ISO superseded by the current release
      MALFORMED:cachyos.org       - page returned no torrent URLs
    """

    def check(self) -> None:
        if not self.fetch('https://cachyos.org/download/', 'CachyOS'):
            return
        if not self.body_ok('cachyos.org'):
            return

        # The download page embeds torrent metadata in HTML-entity-encoded JSON props
        # on Astro island components; torrent_url values look like:
        #   torrent_url&quot;:[0,&quot;https://host/path/cachyos-NAME.torrent&quot;
        upstream_isos = sorted(
            iso + '.iso'
            for iso in re.findall(
                r'torrent_url&quot;:\[0,&quot;[^&]+/(cachyos-[^&]+)\.torrent&quot;',
                self._page,
            )
        )
        # Page structure could change; alert and bail if it does
        if not upstream_isos:
            self.alert('MALFORMED:cachyos.org')
            return

        release_dates = sorted({
            m
            for iso in upstream_isos
            for m in re.findall(r'cachyos-[^-]+-linux-(\d+)\.iso', iso)
        })
        current_release = release_dates[-1] if release_dates else ''

        for iso in upstream_isos:
            self.check_iso(iso, f'NEW:CachyOS-{current_release}')

        for path in self.iso_dir.glob('cachyos-*.iso'):
            if not path.stat().st_size:
                continue
            if path.name not in set(upstream_isos):
                self.alert(f'STALE:{path.name}')


class ArchChecker(Checker):
    """Arch Linux — scrapes archlinux.org/download/ for the current release date.

    Alerts:
      NEW:Arch-YYYY.MM.DD              - current release not present on local disk
      ORPHAN:archlinux-YYYY-x86_64.iso - current ISO on disk but unknown to transmission
      STALE:archlinux-OLD-x86_64.iso   - local ISO superseded by a newer release
    """

    def check(self) -> None:
        if not self.fetch('https://archlinux.org/download/', 'Arch'):
            return
        if not self.body_ok('archlinux.org'):
            return

        m = re.search(r'Current Release:</strong> (\d{4}\.\d{2}\.\d{2})', self._page)
        # Page structure could change; alert and bail if it does
        if not m:
            self.alert('MALFORMED:archlinux.org')
            return

        current_release = m.group(1)
        current_iso = f'archlinux-{current_release}-x86_64.iso'

        self.check_iso(current_iso, f'NEW:Arch-{current_release}')

        for path in self.iso_dir.glob('archlinux-*.iso'):
            if path.stat().st_size and path.name != current_iso:
                self.alert(f'STALE:{path.name}')


class FedoraChecker(Checker):
    """Fedora — fetches torrent.fedoraproject.org/torrents.json.

    Version-level alerts:
      NEW:Fedora-VER     - version appeared in JSON but no local directories exist yet
      DROPPED:Fedora-VER - local directories exist for a version absent from the JSON

    Per-torrent alerts (only once at least one local directory exists for a version):
      NEW:DIR    - torrent directory absent from disk and unknown to transmission
      ORPHAN:DIR - torrent directory present on disk but unknown to transmission
      STALE:DIR  - torrent directory present on disk but removed from the tracker
    """

    def check(self) -> None:
        if not self.fetch('https://torrent.fedoraproject.org/torrents.json', 'Fedora'):
            return
        if not self.body_ok('torrent.fedoraproject.org'):
            return

        try:
            data = json.loads(self._page)
        # JSON structure could change; alert and bail if it does
        except json.JSONDecodeError:
            self.alert('MALFORMED:Fedora-Tracker')
            return

        tracker_versions = sorted(
            (entry['name'] for entry in data),
            key=ver_key,
        )
        # Empty version list means the JSON structure changed
        if not tracker_versions:
            self.alert('MALFORMED:Fedora-Tracker')
            return

        # Collect versions present in local directories. The trailing slash in
        # the glob pattern ensures we only match directories, not ISO files.
        local_versions = {
            dirpath.name.rsplit('-', 1)[-1]
            for dirpath in self.iso_dir.glob('Fedora-*-*/')
            if dirpath.is_dir()
        }

        for ver in tracker_versions:
            local_ver_dirs = [d for d in self.iso_dir.glob(f'Fedora-*-{ver}/') if d.is_dir()]
            if not local_ver_dirs:
                self.alert(f'NEW:Fedora-{ver}')
                continue

            ver_torrents = sorted(
                torrent['torrent']
                for entry in data
                if entry['name'] == ver
                for torrent in entry.get('torrents', [])
            )
            self._check_version(ver, ver_torrents)

        for ver in local_versions:
            if ver not in tracker_versions:
                self.alert(f'DROPPED:Fedora-{ver}')

    def _check_version(self, ver: str, ver_torrents: list[str]) -> None:
        """Check individual torrents for a single Fedora version."""
        for torrent in ver_torrents:
            self.check_dir(torrent.removesuffix('.torrent'))

        for dirpath in self.iso_dir.glob(f'Fedora-*-{ver}/'):
            directory = dirpath.name
            if f'{directory}.torrent' not in ver_torrents:
                self.alert(f'STALE:{directory}')


class AlmaChecker(Checker):
    """AlmaLinux — scrapes mirrors.almalinux.org/isos.html.

    Version-level alerts:
      NEW:AlmaLinux-MAJOR   - new major on isos.html with no local directories
      NEW:AlmaLinux-VER     - new point release on isos.html but not locally
      DROPPED:AlmaLinux-MAJ - local directories exist for a major absent from isos.html

    Per version+arch alerts:
      NEW:AlmaLinux-VER-ARCH    - expected directory absent from disk and transmission
      ORPHAN:AlmaLinux-VER-ARCH - directory present on disk but unknown to transmission
      STALE:AlmaLinux-VER-ARCH  - local directory superseded by a newer point release
    """

    def check(self) -> None:
        if not self.fetch('https://mirrors.almalinux.org/isos.html', 'AlmaLinux'):
            return
        if not self.body_ok('mirrors.almalinux.org'):
            return

        # Extract (version, arch) pairs from /isos/ARCH/VERSION.html links;
        # the regex captures (arch, version) so we swap on unpack
        raw = re.findall(r'/isos/([^/]+)/([0-9]+\.[0-9]+)\.html', self._page)
        pairs = sorted(
            {(ver, arch) for arch, ver in raw},
            key=lambda p: ver_key(p[0]),
        )
        # Page structure could change; alert and bail if it does
        if not pairs:
            self.alert('MALFORMED:AlmaLinux-isos.html')
            return

        tracker_majors = sorted({ver.split('.')[0] for ver, _ in pairs}, key=int)

        local_majors = {
            dirpath.name.removeprefix('AlmaLinux-').split('.')[0]
            for dirpath in self.iso_dir.glob('AlmaLinux-*.*-*/')
            if dirpath.is_dir()
        }

        for major in tracker_majors:
            local_major_dirs = [
                d for d in self.iso_dir.glob(f'AlmaLinux-{major}.*-*/')
                if d.is_dir()
            ]
            if not local_major_dirs:
                self.alert(f'NEW:AlmaLinux-{major}')
                continue

            current_version = sorted(
                {ver for ver, _ in pairs if ver.split('.')[0] == major},
                key=ver_key,
            )[-1]
            current_arches = sorted(arch for ver, arch in pairs if ver == current_version)
            self._check_version(major, current_version, current_arches)

        for maj in local_majors:
            if maj not in tracker_majors:
                self.alert(f'DROPPED:AlmaLinux-{maj}')

    def _check_version(self, major: str, current_version: str, arches: list[str]) -> None:
        """Check a single AlmaLinux major version against local disk."""
        local_current = [
            d for d in self.iso_dir.glob(f'AlmaLinux-{current_version}-*/')
            if d.is_dir()
        ]
        if not local_current:
            self.alert(f'NEW:AlmaLinux-{current_version}')
        else:
            for arch in arches:
                self.check_dir(f'AlmaLinux-{current_version}-{arch}')

        for dirpath in self.iso_dir.glob(f'AlmaLinux-{major}.*-*/'):
            if not dirpath.is_dir():
                continue
            ver = dirpath.name.removeprefix('AlmaLinux-').rsplit('-', 1)[0]
            if ver != current_version:
                self.alert(f'STALE:{dirpath.name}')


class UbuntuChecker(Checker):
    """Ubuntu — scrapes torrent.ubuntu.com/tracker_index.

    Alerts:
      NEW:ISO              - tracker ISO absent from disk and unknown to transmission
      ORPHAN:ISO           - tracker ISO present on disk but unknown to transmission
      STALE:ISO            - local *buntu*.iso no longer listed on the tracker
      MISSING:*buntu*.iso  - no Ubuntu ISOs found on our disk at all
    """

    def check(self) -> None:
        if not self.fetch('https://torrent.ubuntu.com/tracker_index', 'Ubuntu'):
            return
        if not self.body_ok('torrent.ubuntu.com'):
            return

        lines = [
            ln for ln in self._page.splitlines()
            if not re.search(r'beta|snapshot', ln, re.IGNORECASE)
        ]
        upstream_isos = re.findall(r'>([^<]+\.iso)<', '\n'.join(lines))
        # Page structure could change; alert and bail if it does
        if not upstream_isos:
            self.alert('MALFORMED:Ubuntu-Tracker')
            return

        for iso in upstream_isos:
            self.check_iso(iso)

        local_isos = sorted(self.iso_dir.glob('*buntu*.iso'))
        if not local_isos:
            self.alert('MISSING:*buntu*.iso')
            return

        upstream_set = set(upstream_isos)
        for path in local_isos:
            if path.name not in upstream_set:
                self.alert(f'STALE:{path.name}')


class ProxmoxChecker(Checker):
    """Proxmox VE — scrapes the downloads page for ISO version strings.

    Alerts:
      NEW:Proxmox-X.Y-Z           - version on page but no local ISO exists
      ORPHAN:proxmox-ve_X.Y-Z.iso - ISO on disk but unknown to transmission
      STALE:proxmox-ve_X.Y-Z.iso  - local ISO superseded by a newer point release
      DROPPED:Proxmox-MAJOR       - local ISOs exist for a major absent from the page
    """

    def check(self) -> None:
        url = 'https://www.proxmox.com/en/downloads/proxmox-virtual-environment'
        if not self.fetch(url, 'Proxmox'):
            return
        if not self.body_ok('www.proxmox.com'):
            return

        versions = sorted(
            set(re.findall(r'\d+\.\d+-\d+', self._page)),
            key=ver_key,
        )
        # Page structure could change; alert and bail if it does
        if not versions:
            self.alert('MALFORMED:Proxmox-Downloads')
            return

        page_majors = {v.split('.')[0] for v in versions}

        for ver in versions:
            self.check_iso(f'proxmox-ve_{ver}.iso', f'NEW:Proxmox-{ver}')

        for path in self.iso_dir.glob('proxmox-ve_*.iso'):
            if not path.stat().st_size:
                continue
            ver = path.name.removeprefix('proxmox-ve_').removesuffix('.iso')
            if ver in versions:
                continue
            major = ver.split('.')[0]
            if major in page_majors:
                self.alert(f'STALE:{path.name}')
            else:
                self.alert(f'DROPPED:Proxmox-{major}')


class DebianChecker(Checker):
    """Debian — uses rsync --list-only against cdimage.debian.org.

    Alerts:
      NEW:ISO                  - rsync ISO absent from disk and unknown to transmission
      ORPHAN:ISO               - rsync ISO present on disk but unknown to transmission
      STALE:ISO                - local debian-*.iso no longer listed in rsync output
      MISSING:debian-*.iso     - no Debian ISOs found on our disk at all
      MALFORMED:Debian-Tracker - rsync ran but returned no .torrent filenames
    """

    def check(self) -> None:
        # Filter rules are order-dependent: include directories so rsync recurses,
        # include .torrent files, then exclude everything else
        try:
            result = subprocess.run(
                [
                    'rsync', '--list-only', '--no-motd', '-r',
                    '--include=*/',
                    '--include=*.torrent',
                    '--exclude=*',
                    'rsync://cdimage.debian.org/debian-cd/',
                ],
                capture_output=True,
                text=True,
                timeout=60,
            )
        except subprocess.TimeoutExpired:
            self._debug('rsync timed out')
            self._failures.increment('Debian')
            if self._failures.at_threshold('Debian'):
                self.alert('cdimage.debian.org')
            return

        if result.returncode != 0:
            self._failures.increment('Debian')
            # Check threshold against in-memory state; no disk read needed
            if self._failures.at_threshold('Debian'):
                self.alert('cdimage.debian.org')
            return

        self._failures.clear('Debian')

        upstream_isos = sorted(
            line.split()[-1].rsplit('/', 1)[-1].removesuffix('.torrent')
            for line in result.stdout.splitlines()
            if line.endswith('.torrent')
        )
        # rsync succeeded but returned no .torrent files; structure may have changed
        if not upstream_isos:
            self.alert('MALFORMED:Debian-Tracker')
            return

        for iso in upstream_isos:
            self.check_iso(iso)

        local_isos = sorted(self.iso_dir.glob('debian-*.iso'))
        if not local_isos:
            self.alert('MISSING:debian-*.iso')
            return

        upstream_set = set(upstream_isos)
        for path in local_isos:
            if path.name not in upstream_set:
                self.alert(f'STALE:{path.name}')


########
# MAIN #
########

CHECKERS: list[type[Checker]] = [
    AlmaChecker,
    ArchChecker,
    CachyChecker,
    DebianChecker,
    FedoraChecker,
    MintChecker,
    ProxmoxChecker,
    UbuntuChecker,
]


def main() -> int:
    # Bail early if the download directory is missing
    if not ISO_DIR.is_dir():
        print(f'ERROR: transmission download directory {ISO_DIR} is missing. Exiting.')
        return 1

    # Bail early if rsync is missing
    if subprocess.run(['rsync', '--version'], capture_output=True).returncode != 0:
        print('ERROR: Please install rsync to proceed. Exiting.')
        return 1

    # Require a valid status.txt to proceed
    if not STATUS_FILE.exists() or STATUS_FILE.stat().st_size == 0:
        print(f'ERROR: status.txt is missing or empty at {STATUS_FILE}. Exiting.')
        return 1

    status_content = STATUS_FILE.read_text()
    # 'Sum:' appears in the totals line of transmission-remote -l output;
    # its absence means the file wasn't written by transmission or was truncated
    if 'Sum:' not in status_content:
        print(f'ERROR: status.txt appears malformed at {STATUS_FILE}. Exiting.')
        return 1

    # Run all checkers concurrently. Show the live status display when running
    # interactively or when --verbose is passed; cron gets quiet output.
    interactive = sys.stderr.isatty() or '--verbose' in sys.argv
    names = [cls.__name__ for cls in CHECKERS]
    display = StatusDisplay(names) if interactive else None
    failures = FailureTracker(FAIL_FILE, FAIL_THRESHOLD)
    instances = [cls(ISO_DIR, status_content, failures, display) for cls in CHECKERS]
    all_updates: set[str] = set()

    with ThreadPoolExecutor(max_workers=len(instances)) as pool:
        for future in as_completed(pool.submit(checker.run) for checker in instances):
            all_updates |= future.result()

    # Persist failure counts now that all checkers have finished
    failures.save()

    if display:
        display.close()

    # Report all accumulated alerts and exit non-zero so healthchecks.io fires
    if all_updates:
        print('\n'.join(sorted(all_updates)))
        return 1

    # All checks passed
    return 0


if __name__ == '__main__':
    sys.exit(main())
