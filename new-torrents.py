#!/usr/bin/env python3
# Check for updates to torrents for our mirror
# https://mirror.tsue.net/
#
# This script runs once an hour via cron and raises alerts via healthchecks.io
# We send the output as a POST to /fail in the event of a non-zero exit.

import gzip
import http.client
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

        # Reserve space by printing the initial waiting lines, and remember
        # how many physical rows each occupied. _redraw() needs this on the
        # NEXT call to know how far to move the cursor up — using the row
        # counts of the content it's about to print instead would drift
        # whenever a line's wrapped height changes between redraws (e.g. a
        # long "fetch failed: <url error>" message, then a short one next
        # tick), corrupting the live display.
        initial_lines = [self._render_line(name) for name in names]
        for line in initial_lines:
            print(line, file=sys.stderr)
        self._last_rows = [self._physical_rows(line) for line in initial_lines]

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

    def _redraw(self) -> None:
        rendered = [self._render_line(name) for name in self._names]

        # Move up to the top of the block as it currently exists on screen
        # (based on what was actually last printed, tracked in _last_rows),
        # erase every row it occupies, then print the new block fresh.
        # Erasing and reprinting line-by-line in place only works if each
        # line's wrapped height is unchanged since the last redraw; grouping
        # it into one whole-block erase avoids relying on that.
        old_total = sum(self._last_rows)
        if old_total:
            print(self._CURSOR_UP.format(old_total), file=sys.stderr, end='')
        for _ in range(old_total):
            print(self._ERASE_LINE, file=sys.stderr)
        if old_total:
            print(self._CURSOR_UP.format(old_total), file=sys.stderr, end='')

        for line in rendered:
            print(line, file=sys.stderr)

        self._last_rows = [self._physical_rows(line) for line in rendered]

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

        # URLError/OSError cover connection- and DNS-level failures. The rest
        # are malformed-response failure modes that are just as much "this
        # fetch didn't work" but aren't OSError subclasses, so without them
        # a single bad response anywhere crashes the whole threaded run and
        # silently drops every other checker's results: zlib.error from a
        # corrupt "Content-Encoding: deflate" body, LookupError from a
        # garbage charset name in the Content-Type header, and
        # http.client.HTTPException (e.g. IncompleteRead) from a connection
        # that drops mid-response.
        except (urllib.error.URLError, OSError, zlib.error,
                LookupError, http.client.HTTPException) as e:
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

    Only one version is ever current (the previous one is retired once a new
    one ships), so unlike Ubuntu this can group around a single version —
    same shape as DebianChecker.

    Version-level alerts:
      NEW:Linux-Mint-VER   - current version has no matching ISOs on disk yet
      STALE:Linux-Mint-VER - local ISOs exist for a version no longer current

    Per-file alerts (only once at least one local ISO matches the current version):
      NEW:ISO    - current-version ISO absent from disk and unknown to transmission
      ORPHAN:ISO - current-version ISO present on disk but unknown to transmission
      STALE:ISO  - current-version (or unparseable) local ISO no longer listed

      MISSING:linuxmint-*.iso  - no Linux Mint ISOs found on our disk at all
      MALFORMED:Linux-Mint     - stable index returned no version directories
      MALFORMED:Linux-Mint-VER - version directory returned no ISOs
    """

    _VERSION_RE = re.compile(r'(\d+\.\d+(?:\.\d+)*)')

    def _version_of(self, filename: str) -> str | None:
        m = self._VERSION_RE.search(filename)
        return m.group(1) if m else None

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

        # This listing is already scoped to the current version's directory,
        # so (unlike Debian/Ubuntu) every upstream_iso here is current by
        # construction — no per-filename version filtering needed on this side.
        upstream_isos = sorted(re.findall(r'href="(linuxmint-[^"]+\.iso)"', self._page))
        # Version directory structure could change; alert and bail if it does
        if not upstream_isos:
            self.alert(f'MALFORMED:Linux-Mint-{current}')
            return

        local_isos = sorted(self.iso_dir.glob('linuxmint-*.iso'))
        if not local_isos:
            self.alert('MISSING:linuxmint-*.iso')
            self.alert(f'NEW:Linux-Mint-{current}')
            return

        local_current = [p for p in local_isos if self._version_of(p.name) == current]

        if not local_current:
            # Nothing for the new release yet: one alert beats one per file.
            self.alert(f'NEW:Linux-Mint-{current}')
        else:
            # Already partway through mirroring; fall back to per-file checks
            # so stragglers and orphans still surface individually.
            for iso in upstream_isos:
                self.check_iso(iso)

        upstream_set = set(upstream_isos)
        stale_versions: set[str] = set()
        for path in local_isos:
            if path.name in upstream_set:
                continue
            ver = self._version_of(path.name)
            if ver is not None and ver != current:
                # Whole prior release superseded; group instead of one alert per file.
                stale_versions.add(ver)
            else:
                # Current-version (or unparseable) file dropped from the listing;
                # unusual enough to keep visible individually.
                self.alert(f'STALE:{path.name}')

        for ver in sorted(stale_versions, key=ver_key):
            self.alert(f'STALE:Linux-Mint-{ver}')


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

        # Group superseded point releases by version instead of alerting once
        # per arch directory (e.g. a 10.0 → 10.1 bump used to fire one STALE
        # per arch; this collapses it to a single STALE:AlmaLinux-10.0).
        stale_versions: set[str] = set()
        for dirpath in self.iso_dir.glob(f'AlmaLinux-{major}.*-*/'):
            if not dirpath.is_dir():
                continue
            ver = dirpath.name.removeprefix('AlmaLinux-').rsplit('-', 1)[0]
            if ver != current_version:
                stale_versions.add(ver)

        for ver in sorted(stale_versions, key=ver_key):
            self.alert(f'STALE:AlmaLinux-{ver}')


class UbuntuChecker(Checker):
    """Ubuntu — scrapes torrent.ubuntu.com/tracker_index.

    Unlike Debian/Mint, Ubuntu runs multiple release lines (X.Y) at once —
    typically an LTS plus the current interim release — so there's no single
    global "current version" to group around; grouping by the overall max
    would misclassify a perfectly current, unrelated line as stale the
    moment any other line advances. Each X.Y line is tracked and grouped
    independently instead. Which lines currently exist is read entirely off
    the tracker page — nothing about LTS/interim status is hardcoded.

    Version-level alerts:
      NEW:Ubuntu-VER   - a line's current point release has no local ISOs yet
      STALE:Ubuntu-VER - local ISOs exist for a point release no longer
                          current within its line (superseded, or the whole
                          line dropped from the tracker)

    Per-file alerts (only once at least one local ISO matches that line's current version):
      NEW:ISO    - tracker ISO absent from disk and unknown to transmission
      ORPHAN:ISO - tracker ISO present on disk but unknown to transmission
      STALE:ISO  - current-version (or unparseable) local ISO dropped from the tracker

      MISSING:*buntu*.iso      - no Ubuntu-family ISOs found on our disk at all
      MALFORMED:Ubuntu-Tracker - tracker page returned no ISOs, or none had a
                                  parseable version
    """

    _VERSION_RE = re.compile(r'(\d+\.\d+(?:\.\d+)*)')

    def _version_of(self, filename: str) -> str | None:
        m = self._VERSION_RE.search(filename)
        return m.group(1) if m else None

    def _line_of(self, version: str) -> str:
        """Reduce a full X.Y(.Z) version to its X.Y release line."""
        return '.'.join(version.split('.')[:2])

    def check(self) -> None:
        if not self.fetch('https://torrent.ubuntu.com/tracker_index', 'Ubuntu'):
            return
        if not self.body_ok('torrent.ubuntu.com'):
            return

        page_lines = [
            ln for ln in self._page.splitlines()
            if not re.search(r'beta|snapshot', ln, re.IGNORECASE)
        ]
        upstream_isos = re.findall(r'>([^<]+\.iso)<', '\n'.join(page_lines))
        # Page structure could change; alert and bail if it does
        if not upstream_isos:
            self.alert('MALFORMED:Ubuntu-Tracker')
            return

        upstream_versions = {self._version_of(iso) for iso in upstream_isos}
        upstream_versions.discard(None)
        # Every filename failed to parse a version; structure may have changed
        if not upstream_versions:
            self.alert('MALFORMED:Ubuntu-Tracker')
            return

        # Independently find the current (max) point release within each
        # release line — e.g. 24.04.3 and 25.10 can both be current at once.
        current_by_line: dict[str, str] = {}
        for v in upstream_versions:
            line = self._line_of(v)
            if line not in current_by_line or ver_key(v) > ver_key(current_by_line[line]):
                current_by_line[line] = v
        current_versions = set(current_by_line.values())

        local_isos = sorted(self.iso_dir.glob('*buntu*.iso'))
        if not local_isos:
            self.alert('MISSING:*buntu*.iso')
            for ver in current_versions:
                self.alert(f'NEW:Ubuntu-{ver}')
            return

        for current_version in current_versions:
            local_current = [
                p for p in local_isos if self._version_of(p.name) == current_version
            ]
            if not local_current:
                # Nothing for this line's new release yet: one alert beats one per file.
                self.alert(f'NEW:Ubuntu-{current_version}')
            else:
                # Already partway through mirroring this line; fall back to
                # per-file checks so stragglers and orphans still surface
                # individually, scoped to just this line's current version.
                for iso in upstream_isos:
                    if self._version_of(iso) == current_version:
                        self.check_iso(iso)

        upstream_set = set(upstream_isos)
        stale_versions: set[str] = set()
        for path in local_isos:
            if path.name in upstream_set:
                continue
            ver = self._version_of(path.name)
            if ver is not None and ver not in current_versions:
                # Superseded within its line, or the whole line is gone from
                # the tracker; either way, group instead of one alert per file.
                stale_versions.add(ver)
            else:
                # Current-version (or unparseable) file dropped from the tracker;
                # unusual enough to keep visible individually.
                self.alert(f'STALE:{path.name}')

        for ver in sorted(stale_versions, key=ver_key):
            self.alert(f'STALE:Ubuntu-{ver}')


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

    Every debian/debian-edu/debian-live/debian-mac filename (flat installer
    ISOs, live spins, and the 22-disc source set) carries the same point
    release, e.g. debian-13.6.0-amd64-DVD-1.iso or
    debian-live-13.6.0-amd64-kde.iso. Unlike Fedora/AlmaLinux there's no
    upstream JSON/HTML giving us that version directly, so it's pulled out
    of each filename with a regex instead.

    Version-level alerts:
      NEW:Debian-VER   - current release has no matching ISOs on disk yet
      STALE:Debian-VER - local ISOs exist for a version no longer on the tracker

    Per-file alerts (only once at least one local ISO matches the current version):
      NEW:ISO    - tracker ISO absent from disk and unknown to transmission
      ORPHAN:ISO - tracker ISO present on disk but unknown to transmission
      STALE:ISO  - current-version (or unparseable) local ISO dropped from the tracker

      MISSING:debian-*.iso     - no Debian ISOs found on our disk at all
      MALFORMED:Debian-Tracker - rsync ran but returned no .torrent filenames,
                                  or no filename had a parseable version
    """

    # The point release is the only dotted numeric run in these filenames; a
    # fixed prefix offset doesn't work since "-edu"/"-live"/"-mac" and the
    # "source" DVDs shift where it falls, so search for it instead.
    _VERSION_RE = re.compile(r'(\d+\.\d+(?:\.\d+)*)')

    def _version_of(self, filename: str) -> str | None:
        m = self._VERSION_RE.search(filename)
        return m.group(1) if m else None

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

        upstream_versions = {self._version_of(iso) for iso in upstream_isos}
        upstream_versions.discard(None)
        # Every filename failed to parse a version; structure may have changed
        if not upstream_versions:
            self.alert('MALFORMED:Debian-Tracker')
            return
        current_version = sorted(upstream_versions, key=ver_key)[-1]

        local_isos = sorted(self.iso_dir.glob('debian-*.iso'))
        if not local_isos:
            self.alert('MISSING:debian-*.iso')
            self.alert(f'NEW:Debian-{current_version}')
            return

        local_current = [p for p in local_isos if self._version_of(p.name) == current_version]

        if not local_current:
            # Nothing for the new release yet: one alert beats one per file.
            self.alert(f'NEW:Debian-{current_version}')
        else:
            # Already partway through mirroring the current release (or it's
            # been fully mirrored); fall back to per-file checks so stragglers
            # and orphans still surface individually.
            for iso in upstream_isos:
                if self._version_of(iso) == current_version:
                    self.check_iso(iso)

        upstream_set = set(upstream_isos)
        stale_versions: set[str] = set()
        for path in local_isos:
            if path.name in upstream_set:
                continue
            ver = self._version_of(path.name)
            if ver is not None and ver != current_version:
                # Whole prior release superseded; group instead of one alert per file.
                stale_versions.add(ver)
            else:
                # Current-version (or unparseable) file dropped from the tracker;
                # unusual enough to keep visible individually.
                self.alert(f'STALE:{path.name}')

        for ver in sorted(stale_versions, key=ver_key):
            self.alert(f'STALE:Debian-{ver}')


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
        if display:
            print(file=sys.stderr)  # blank line separating the status board from the alerts
        print('\n'.join(sorted(all_updates)))
        return 1

    # All checks passed
    return 0


if __name__ == '__main__':
    sys.exit(main())
