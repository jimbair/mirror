#!/usr/bin/env python3
# Check for updates to torrents for our mirror
# https://mirror.tsue.net/
#
# This script runs once an hour via cron and raises alerts via healthchecks.io
# We send the output as a POST to /fail in the event of a non-zero exit.

import argparse
import fcntl
import gzip
import http.client
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
import urllib.error
import zlib
from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
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
# Prevents two overlapping invocations (e.g. a manual run colliding with
# cron, or a prior run stuck far longer than expected) from racing on
# FAIL_FILE and doubling up on every upstream fetch. Uses flock() rather
# than a PID file: the lock lives in the kernel, tied to this process's
# open file descriptor, so it can never go stale across a crash, kill -9,
# or reboot -- there's no on-disk "locked" state to clean up.
LOCK_FILE = Path.home() / '.config' / 'new-torrents' / 'lock'


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
        """Write counts to disk if they changed. Called once after all
        checkers finish, via a same-directory temp file + os.replace()
        (atomic rename on POSIX): a crash mid-write would otherwise leave
        a truncated JSON file that _load() silently discards, wiping the
        very outage counters this file exists to preserve.
        """
        if not self._dirty:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_name(self._path.name + '.tmp')
        tmp.write_text(json.dumps(self._counts, indent=2))
        os.replace(tmp, self._path)

    # Internal

    def _load(self) -> dict[str, int]:
        if not self._path.exists():
            return {}
        try:
            data = json.loads(self._path.read_text())
        except (json.JSONDecodeError, OSError):
            return {}
        # Valid JSON that isn't an object (e.g. a list) would raise at
        # the per-checker counter accesses; treat it like a corrupt file
        return data if isinstance(data, dict) else {}


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
      red    - finished via an unhandled exception in check()
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
        self._names                    = names                          # noqa: E221
        self._lock                     = threading.Lock()               # noqa: E221
        self._status: dict[str, str]   = {n: 'waiting' for n in names}  # noqa: E221
        self._start: dict[str, float]  = {}                             # noqa: E221
        self._alerts: dict[str, int]   = {}                             # noqa: E221
        self._done: dict[str, bool]    = {n: False for n in names}      # noqa: E221
        self._errored: dict[str, bool] = {}                             # noqa: E221

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

    def error(self, name: str) -> None:
        """Mark a checker as having raised an unhandled exception in check().

        Distinct from finish(): this is for genuine bugs (a checker's own
        parsing logic blowing up), not the anticipated fetch()/rsync failure
        modes, which still route through finish() via a normal alert.
        """
        with self._lock:
            self._done[name] = True
            self._errored[name] = True
            elapsed = time.monotonic() - self._start.get(name, time.monotonic())
            self._status[name] = f'error after {elapsed:.1f}s'
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
        # Re-measure on every redraw rather than trusting the width from
        # __init__: if the user resizes the terminal mid-run, a stale
        # width here would make _physical_rows() estimate a row count
        # that no longer matches how the real terminal actually wraps the
        # line it's about to print, corrupting the cursor-up math on
        # every subsequent redraw from that point on.
        try:
            self._term_width = os.get_terminal_size(sys.stderr.fileno()).columns
        except OSError:
            self._term_width = 80

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
            if self._errored.get(name):
                color = self._RED
            else:
                color = self._CYAN if self._alerts.get(name, 0) else self._GREEN
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
        # Precomputed once here rather than re-deriving per _known_to_transmission()
        # call: status_content's Name column entries are whitespace-delimited,
        # so splitting on whitespace once up front gives the exact same set
        # of "whole token" candidates a per-call regex search would find,
        # but as an O(1) membership test instead of re-compiling and
        # re-scanning the whole blob for every single ISO/directory checked
        # (hundreds of times per run for checkers like Debian).
        self._status_tokens = set(status_content.split())
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
                    # RFC 1950 (zlib-wrapped) is the correct interpretation
                    # of "deflate", but some servers actually send raw,
                    # headerless RFC 1951 deflate despite the label.
                    # zlib.decompress()'s default wbits expects the zlib
                    # wrapper and raises on raw deflate; retry with
                    # negative wbits (raw deflate, no header/trailer) if
                    # the first attempt fails, rather than treating a
                    # merely-mislabeled body as a fetch failure.
                    try:
                        raw = zlib.decompress(raw)
                    except zlib.error:
                        raw = zlib.decompress(raw, -zlib.MAX_WBITS)
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
        """Return False and alert if self._page is empty or below min_len
        characters. self._page is already decoded to str by this point, so
        this counts characters, not raw bytes -- for non-ASCII content the
        two diverge, making this marginally stricter than a byte count
        would be, but the purpose is just to catch empty/error responses,
        so that's harmless. A short response usually means a transient
        error page or a CDN block rather than real content; 250 is safely
        below any valid index page.
        """
        if not self._page or len(self._page) < min_len:
            self.alert(alert_name)
            return False
        return True

    def _known_to_transmission(self, name: str) -> bool:
        """True if name appears in status_content as a whole token, not
        merely as a prefix of some longer, unrelated name. A plain
        substring test would treat e.g. 'Fedora-Workstation-Live-x86_64-42'
        as already known if only '...-420' were actually present --
        silently hiding a genuinely missing release. status_content's
        Name column entries are whitespace-delimited, so an exact match
        against the whitespace-split token set (precomputed once in
        __init__) is equivalent to a whole-token regex search, without
        recompiling and rescanning per call.
        """
        return name in self._status_tokens

    def _safe_path(self, name: str) -> Path | None:
        """Resolve name against iso_dir, or None if the result would
        escape iso_dir entirely. name is never opened or read -- only
        checked for existence/size/is_dir -- but a compromised or simply
        misbehaving upstream page could still use a crafted name to probe
        for the existence of arbitrary files on the host: a relative
        '../../etc/passwd'-style name climbs out via '..' segments, and
        an absolute name like '/etc/passwd' exploits Path's own '/'
        operator semantics, where an absolute right-hand side replaces
        the left side entirely rather than being appended to it. Neither
        is a currently expected shape for any of these checkers' scraped
        names, so treat either as suspicious rather than silently
        resolving somewhere unintended.
        """
        path = (self.iso_dir / name).resolve()
        iso_dir_resolved = self.iso_dir.resolve()
        if not path.is_relative_to(iso_dir_resolved):
            return None
        return path

    def check_iso(self, iso: str, new_alert: str = '') -> None:
        """Check a flat ISO file against transmission status and local disk."""
        if not new_alert:
            new_alert = f'NEW:{iso}'
        # Transmission knows about this ISO; nothing to do
        if self._known_to_transmission(iso):
            return
        path = self._safe_path(iso)
        if path is None:
            self.alert(f'UNSAFE:{iso}')
            return
        # ISO is on disk but transmission has no record of it
        if path.exists() and path.stat().st_size > 0:
            self.alert(f'ORPHAN:{iso}')
        # ISO is not on disk and not known to transmission
        else:
            self.alert(new_alert)

    def check_dir(self, directory: str) -> None:
        """Check a torrent directory against transmission status and local disk."""
        # Transmission knows about this directory; nothing to do
        if self._known_to_transmission(directory):
            return
        path = self._safe_path(directory)
        if path is None:
            self.alert(f'UNSAFE:{directory}')
            return
        # Directory is on disk but transmission has no record of it
        if path.is_dir():
            self.alert(f'ORPHAN:{directory}')
        # Directory is not on disk and not known to transmission
        else:
            self.alert(f'NEW:{directory}')

    def run(self) -> set[str]:
        """Run the check and return accumulated alerts.

        An unexpected exception inside check() — a bug in a checker's own
        parsing logic, distinct from the anticipated fetch()/rsync failure
        modes that already alert and return normally — is caught here
        rather than left to propagate. Uncaught, it would surface via
        future.result() in main(), aborting the whole threaded run: every
        other checker's results get dropped, failures.save() never runs,
        and display.close() never runs, leaving a half-drawn status board.
        Converting it into an alert keeps this checker's failure isolated
        and visible in the normal report instead of a bare traceback.
        """
        if self._display:
            self._display.start(self._name)
        try:
            self.check()
        except Exception as e:
            self.alert(f'EXCEPTION:{self._name}: {e}')
            if self._display:
                self._display.error(self._name)
            return self.updates
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
    """Linux Mint — every supported release from download_all.php; local copies of past-EOL versions flagged EOL.

    pub.linuxmint.io/stable/ is a never-pruned archive (every release
    from 19.3 on is still listed), so it says nothing about which versions
    are supported; download_all.php is the one-stop anchor, listing only
    currently supported releases. Each supported version is therefore
    tracked independently, the same shape as Ubuntu's per-line tracking:
    a listed version with no local ISOs yet alerts NEW:Linux-Mint-VER,
    and once any local file exists for it, per-file checks run against
    its own directory listing (fetched lazily, only when needed). Local
    ISOs of a version the support page doesn't list are past EOL and are
    surfaced as EOL:Linux-Mint-VER, repeating every run until the local
    files are removed. When the support page is unfetchable or
    unparseable the checker fails open: only the newest version is
    tracked and nothing counts as past EOL.

    Version-level alerts:
      NEW:Linux-Mint-VER   - a tracked version has no matching ISOs on disk yet
      EOL:Linux-Mint-VER   - local ISOs exist for a version not listed on
                             download_all.php; repeats every run until the
                             local files are removed

    Per-file alerts (only once at least one local ISO matches that version):
      NEW:ISO    - version ISO absent from disk and unknown to transmission
      ORPHAN:ISO - version ISO present on disk but unknown to transmission
      STALE:ISO  - local ISO dropped from its version's listing, or
                   unparseable

      MISSING:linuxmint-*.iso  - no Linux Mint ISOs found on our disk at all
      MALFORMED:Linux-Mint     - stable index returned no version directories
      MALFORMED:Linux-Mint-VER - version directory returned no ISOs
      MALFORMED:Linux-Mint-Supported - support page returned no version cells
    """

    # pub.linuxmint.io ships the very first release of a major with a bare,
    # dot-less filename too (e.g. linuxmint-22-cinnamon-64bit.iso, confirmed
    # against a live mirror) -- point releases within that major then use a
    # dotted version (linuxmint-22.1-cinnamon-64bit.iso). Anchored right
    # after "linuxmint-" rather than searched anywhere in the string, so it
    # can't mistake a stray digit run elsewhere in the filename (e.g.
    # "64bit") for the version if the real version segment were ever
    # missing -- the plain [0-9]+\.[0-9]+ search here originally couldn't
    # see the bare-major case at all, which meant a fully-mirrored new
    # major release would loop forever reporting NEW:Linux-Mint-VER since
    # local_current could never match it.
    _VERSION_RE = re.compile(r'^linuxmint-(\d+(?:\.\d+)*)-')

    # download_all.php lists only currently supported releases; each
    # version sits in its own table cell, e.g. <td rowspan="3">22.3</td>
    # (a brand-new major uses the bare form, e.g. "21", matching the pub
    # index and the filenames). The page also carries an LMDE row
    # (version "7"), which is not a linuxmint release -- its artifacts
    # are lmde-*.iso under a different archive path -- so check()
    # intersects the parsed versions with the pub index to drop it.
    _SUPPORTED_RE = re.compile(r'<td rowspan="\d+">(\d+(?:\.\d+)*)</td>')

    def _version_of(self, filename: str) -> str | None:
        m = self._VERSION_RE.search(filename)
        return m.group(1) if m else None

    def check(self) -> None:
        if not self.fetch('https://pub.linuxmint.io/stable/', 'Linux-Mint'):
            return
        if not self.body_ok('pub.linuxmint.io'):
            return

        # pub.linuxmint.io/stable/ lists a brand-new major as a bare,
        # dot-less directory (e.g. "23/") before its first point release
        # ("23.1/") exists -- [0-9]+\.[0-9]+ alone can't see that entry at
        # all, so the newest major would be silently invisible to this
        # checker until its first point release ships, potentially months
        # later. The trailing (?:\.[0-9]+)* makes the dotted part optional.
        versions = re.findall(r'href="([0-9]+(?:\.[0-9]+)*)/"', self._page)
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

        # download_all.php is the one-stop support anchor: it lists only
        # currently supported releases. Each listed version is tracked:
        # one with no local ISOs yet alerts NEW:Linux-Mint-VER, and once
        # any local file exists for it, per-file checks run against its
        # own directory (fetched lazily, so an unmirrored version costs
        # no extra request). Local ISOs of a version the page doesn't
        # list are past EOL and surface as EOL:Linux-Mint-VER, repeating
        # every run until the local files are removed.
        supported: set[str] | None = None
        if self.fetch('https://linuxmint.com/download_all.php',
                      'Linux-Mint-Supported'):
            found = set(re.findall(self._SUPPORTED_RE, self._page))
            if found:
                # Intersect with the pub index: the page also carries an
                # LMDE row (version "7"), which is not a linuxmint
                # release and would otherwise loop reporting
                # NEW:Linux-Mint-7 on every run.
                supported = found & set(versions)
            else:
                self.alert('MALFORMED:Linux-Mint-Supported')
        # supported stays None when the page is unusable: fail open --
        # only current is tracked and nothing counts as past EOL.

        if supported is not None:
            for ver in sorted(supported - {current}, key=ver_key):
                local_ver = [p for p in local_isos
                             if self._version_of(p.name) == ver]
                if not local_ver:
                    # Supported, but nothing mirrored for it yet.
                    self.alert(f'NEW:Linux-Mint-{ver}')
                    continue
                if not self.fetch(f'https://pub.linuxmint.io/stable/{ver}/',
                                  'Linux-Mint-VER'):
                    continue
                if not self.body_ok('pub.linuxmint.io'):
                    continue
                isos = sorted(re.findall(r'href="(linuxmint-[^"]+\.iso)"',
                                         self._page))
                if not isos:
                    # Directory exists but lists no ISOs; structure drift.
                    self.alert(f'MALFORMED:Linux-Mint-{ver}')
                    continue
                for iso in isos:
                    self.check_iso(iso)
                listed = set(isos)
                for path in local_ver:
                    if path.name not in listed:
                        # Dropped from this version's listing; keep visible
                        # individually.
                        self.alert(f'STALE:{path.name}')

        eol_versions: set[str] = set()
        for path in local_isos:
            if path.name in upstream_set:
                continue
            ver = self._version_of(path.name)
            if ver is None:
                # Unparseable local file; keep visible individually.
                self.alert(f'STALE:{path.name}')
            elif ver == current:
                # Current-version file dropped from the listing; unusual
                # enough to keep visible individually.
                self.alert(f'STALE:{path.name}')
            elif supported is not None and ver not in supported:
                # Past EOL per download_all.php; group per version.
                eol_versions.add(ver)

        for ver in sorted(eol_versions, key=ver_key):
            self.alert(f'EOL:Linux-Mint-{ver}')


class CachyChecker(Checker):
    """CachyOS — the current release from cachyos.org/download/; superseded local ISOs go stale.

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
        # upstream_isos parsed fine, but if none of those names also yielded
        # a release date (e.g. a compound edition name like "desktop-gnome"
        # defeats the single-segment [^-]+ here), don't silently fall back
        # to an empty version string in the alert. Unlike the upstream_isos
        # guard above, this doesn't return early: the per-file ORPHAN/STALE
        # checks below only need real filenames, which we do have, so
        # bailing out entirely would throw away detection they can still
        # correctly do. UNKNOWN keeps any resulting NEW: alert honest about
        # why, rather than blank.
        if not release_dates:
            self.alert('MALFORMED:cachyos.org')
            current_release = 'UNKNOWN'
        else:
            current_release = release_dates[-1]

        for iso in upstream_isos:
            self.check_iso(iso, f'NEW:CachyOS-{current_release}')

        upstream_set = set(upstream_isos)
        for path in self.iso_dir.glob('cachyos-*.iso'):
            if not (path.exists() and path.stat().st_size > 0):
                continue
            if path.name not in upstream_set:
                self.alert(f'STALE:{path.name}')


class ArchChecker(Checker):
    """Arch Linux — the current rolling release from archlinux.org/download/; superseded local ISOs go stale.

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
            if not (path.exists() and path.stat().st_size > 0):
                continue
            if path.name != current_iso:
                self.alert(f'STALE:{path.name}')


class FedoraChecker(Checker):
    """Fedora — release versions and their torrent directories from torrent.fedoraproject.org/torrents.json.

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

        # Normalize to version -> torrent names in one pass so the checks
        # below never touch raw JSON. The tracker JSON could be a dict
        # (a wrapped payload), carry non-dict entries, or have entries
        # without a usable string 'name' or a list 'torrents' -- any of
        # those would raise inside a bare comprehension and surface as an
        # EXCEPTION alert instead of the clean MALFORMED alert the rest
        # of the structure-change paths use. Malformed entries are skipped
        # individually so one bad entry can't take down the versions the
        # rest still describe; if nothing usable survives, the page has
        # changed shape.
        torrents_by_version: dict[str, list[str]] = {}
        for entry in data if isinstance(data, list) else []:
            if not isinstance(entry, dict):
                continue
            name = entry.get('name')
            torrents = entry.get('torrents', [])
            if not isinstance(name, str) or not isinstance(torrents, list):
                continue
            torrents_by_version.setdefault(name, []).extend(
                t['torrent'] for t in torrents
                if isinstance(t, dict) and isinstance(t.get('torrent'), str)
            )

        # Empty version map means the JSON structure changed
        if not torrents_by_version:
            self.alert('MALFORMED:Fedora-Tracker')
            return

        tracker_versions = sorted(torrents_by_version, key=ver_key)

        # Collect versions present in local directories. The trailing slash in
        # the glob pattern ensures we only match directories, not ISO files.
        # Fedora versions are always bare integers (confirmed against the
        # real tracker: '42', '43', '44', never dotted or lettered) --
        # filter to digit-only here so a stray directory missing its
        # trailing version number (e.g. a partial rename leaving behind
        # "Fedora-Workstation-Live-x86_64") can't have its last
        # hyphen-segment ("x86_64") mistaken for a version and produce a
        # spurious DROPPED:Fedora-x86_64 alert.
        local_versions = {
            dirpath.name.rsplit('-', 1)[-1]
            for dirpath in self.iso_dir.glob('Fedora-*-*/')
            if dirpath.is_dir() and dirpath.name.rsplit('-', 1)[-1].isdigit()
        }

        for ver in tracker_versions:
            local_ver_dirs = [d for d in self.iso_dir.glob(f'Fedora-*-{ver}/') if d.is_dir()]
            if not local_ver_dirs:
                self.alert(f'NEW:Fedora-{ver}')
                continue

            self._check_version(ver, sorted(torrents_by_version[ver]))

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
    """AlmaLinux — new point releases, superseded local directories, and dropped majors from mirrors.almalinux.org/isos.html.

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
    """Ubuntu — active release lines from torrent.ubuntu.com/tracker_index; past-EOL lines excluded.

    Unlike Debian/Mint, Ubuntu runs multiple release lines (X.Y) at once —
    typically an LTS plus the current interim release — so there's no single
    global "current version" to group around; grouping by the overall max
    would misclassify a perfectly current, unrelated line as stale the
    moment any other line advances. Each X.Y line is tracked and grouped
    independently instead.

    Canonical keeps past-EOL releases on the tracker (12.04.5 has sat
    there since 2017), so a past-EOL line's unmirrored point release
    would alert NEW:Ubuntu-VER on every run. Each line is therefore
    cross-checked against Canonical's support schedule (ubuntu.com/
    project/docs/release-team/list-of-releases/) and lines whose support
    has ended per _EOL_MODE are dropped before alerting. Local ISOs for
    a past-EOL line that is still listed on the tracker would otherwise
    be skipped by the stale scan as if they were current, so they are
    surfaced as EOL:Ubuntu-X.Y instead. When the schedule page is
    unfetchable or unparseable the checker fails open and tracks every
    line the tracker lists.

    Version-level alerts:
      NEW:Ubuntu-VER   - a line's current point release has no local ISOs yet
      STALE:Ubuntu-VER - local ISOs exist for a point release no longer
                          current within its line (superseded, or the whole
                          line dropped from the tracker)
      EOL:Ubuntu-X.Y   - local ISOs exist for a release line past EOL per
                          _EOL_MODE; repeats every run until the local
                          files are removed

    Per-file alerts (only once at least one local ISO matches that line's current version):
      NEW:ISO    - tracker ISO absent from disk and unknown to transmission
      ORPHAN:ISO - tracker ISO present on disk but unknown to transmission
      STALE:ISO  - current-version (or unparseable) local ISO dropped from the tracker

      MISSING:*buntu*.iso      - no Ubuntu-family ISOs found on our disk at all
      MALFORMED:Ubuntu-Tracker - tracker page returned no ISOs, or none had a
                                  parseable version
      MALFORMED:Ubuntu-EOL     - support-schedule page returned no usable dates
    """

    _VERSION_RE = re.compile(r'(\d+\.\d+(?:\.\d+)*)')
    # Month-year dates as written on the support-schedule page: "Apr 2019",
    # "Jul 2026", and the day-bearing variants older rows use ("Apr 28,
    # 2017", "Apr 13th, 2009"). Only month and year are kept; the day is
    # noise for comparing support windows.
    _SCHED_DATE_RE = re.compile(
        r'\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*'
        r'(?:\s+\d{1,2}(?:st|nd|rd|th)?\s*,?\s+)?\s*(\d{4})\b',
        re.IGNORECASE)
    _SCHED_TABLE_RE = re.compile(r'<table[^>]*>(.*?)</table>',
                                 re.DOTALL | re.IGNORECASE)
    _SCHED_ROW_RE   = re.compile(r'<tr[^>]*>(.*?)</tr>',
                                 re.DOTALL | re.IGNORECASE)
    _SCHED_CELL_RE  = re.compile(r'<t[dh][^>]*>(.*?)</t[dh]>',
                                 re.DOTALL | re.IGNORECASE)
    _SCHED_MONTHS = {
        'jan': 1, 'feb': 2, 'mar': 3, 'apr': 4, 'may': 5, 'jun': 6,
        'jul': 7, 'aug': 8, 'sep': 9, 'oct': 10, 'nov': 11, 'dec': 12,
    }
    # Which tier of Canonical support expiring marks a release line as EOL:
    #   'standard' - end of standard support (9 months for interim releases)
    #   'esm'      - end of Expanded Security Maintenance (Ubuntu Pro);
    #                interims never get ESM and use their standard end
    #   'hard'     - the last of all tiers, including the paid legacy
    #                add-on; a line is dropped only when Canonical no
    #                longer supports it in any form
    #   'off'      - no filtering; track every line the tracker lists
    _EOL_MODE = 'hard'

    def _version_of(self, filename: str) -> str | None:
        m = self._VERSION_RE.search(filename)
        return m.group(1) if m else None

    def _line_of(self, version: str) -> str:
        """Reduce a full X.Y(.Z) version to its X.Y release line."""
        return '.'.join(version.split('.')[:2])

    # Support-schedule parsing. The page is server-rendered static HTML; the
    # four tables we care about are identified by their header cells rather
    # than their position, so a re-ordered layout still parses:
    #   LTS     - Version|...|End of Standard Support|End of Life
    #             (one row per point release; the End of Life column is the
    #             last tier of any kind, i.e. the legacy add-on end)
    #   ESM     - Version|Detailed ESM coverage|...|End of Life
    #   LEGACY  - Version|Detailed Legacy coverage|...|End of Life
    #   PAST    - Version|Code name|...|End of Life
    #             (interims, whose End of Life is their 9-month standard
    #             end, plus 12.04's rows, which the LTS table doesn't carry)

    def _sched_cells(self, row_html: str) -> list[str]:
        """Cell texts of one <tr> row: inner tags stripped, whitespace collapsed."""
        return [' '.join(re.sub(r'<[^>]+>', ' ', cell).split())
                for cell in self._SCHED_CELL_RE.findall(row_html)]

    def _sched_ym(self, cell: str) -> tuple[int, int] | None:
        """First (year, month) found in a schedule cell, else None."""
        m = self._SCHED_DATE_RE.search(cell)
        if not m:
            return None
        return (int(m.group(2)), self._SCHED_MONTHS[m.group(1).lower()[:3]])

    def _sched_update(self, ends: dict[str, tuple[int, int]], line: str,
                      ym: tuple[int, int] | None) -> None:
        """Keep the latest (year, month) per line in ends."""
        if ym is not None and (line not in ends or ym > ends[line]):
            ends[line] = ym

    def _eol_lines(self) -> set[str] | None:
        """Release lines (X.Y) whose Canonical support has ended per
        _EOL_MODE, parsed off the support-schedule page.

        Returns None when the schedule is unusable (fetch failure or no
        parseable dates) so the caller falls back to tracking every line
        the tracker lists. Lines the schedule doesn't mention (upcoming
        releases, or a restructured page) count as still active — we never
        drop a line we can't place.
        """
        if self._EOL_MODE == 'off':
            return set()
        if not self.fetch('https://ubuntu.com/project/docs/release-team/'
                          'list-of-releases/', 'Ubuntu-EOL'):
            return None

        std_end: dict[str, tuple[int, int]] = {}  # standard support
        esm_end: dict[str, tuple[int, int]] = {}  # ESM / Ubuntu Pro
        max_end: dict[str, tuple[int, int]] = {}  # last tier of any kind
        lts_lines: set[str] = set()

        for table in self._SCHED_TABLE_RE.findall(self._page):
            rows = self._SCHED_ROW_RE.findall(table)
            if not rows:
                continue
            header = ' '.join(self._sched_cells(rows[0]))
            hcols = [' '.join(c.split()).lower()
                     for c in self._sched_cells(rows[0])]
            std_col = next((i for i, c in enumerate(hcols)
                            if 'end of standard support' in c), None)
            eol_col = next((i for i, c in enumerate(hcols)
                            if c == 'end of life'), None)
            if 'End of Standard Support' in header and 'End of Life' in header:
                kind = 'LTS'
            elif 'Detailed ESM coverage' in header:
                kind = 'ESM'
            elif 'Detailed Legacy coverage' in header:
                kind = 'LEGACY'
            elif 'Code name' in header and 'End of Life' in header:
                kind = 'PAST'
            else:
                continue
            for row in rows[1:]:
                cells = self._sched_cells(row)
                if eol_col is None or eol_col >= len(cells):
                    continue
                m = re.search(r'(\d{1,2}\.\d{2})', cells[0])
                if not m:
                    continue
                line = m.group(1)
                eol_ym = self._sched_ym(cells[eol_col])
                if kind == 'LTS':
                    lts_lines.add(line)
                    if std_col is not None and std_col < len(cells):
                        self._sched_update(std_end, line,
                                           self._sched_ym(cells[std_col]))
                    self._sched_update(max_end, line, eol_ym)
                elif kind == 'ESM':
                    lts_lines.add(line)
                    self._sched_update(esm_end, line, eol_ym)
                    self._sched_update(max_end, line, eol_ym)
                elif kind == 'LEGACY':
                    lts_lines.add(line)
                    self._sched_update(max_end, line, eol_ym)
                else:  # PAST
                    self._sched_update(std_end, line, eol_ym)
                    self._sched_update(max_end, line, eol_ym)

        if not (std_end or esm_end or max_end):
            # The page yielded no dates at all: structure change (or a
            # short error shell that still passed fetch()). Alert rather
            # than silently disabling the filter.
            self.alert('MALFORMED:Ubuntu-EOL')
            return None

        now_ym = (date.today().year, date.today().month)
        eol: set[str] = set()
        for line in set(std_end) | set(esm_end) | set(max_end):
            if self._EOL_MODE == 'standard':
                end = std_end.get(line)
            elif self._EOL_MODE == 'esm':
                end = esm_end.get(line)
                if end is None and line not in lts_lines:
                    # Interims get no ESM; their standard end is their
                    # only support window.
                    end = std_end.get(line)
            else:  # 'hard'
                end = max_end.get(line)
            if end is not None and end < now_ym:
                eol.add(line)
        return eol

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

        # Drop lines whose support has ended per _EOL_MODE; _eol_lines()
        # returns None when the schedule page is unusable, in which case
        # fail open and track everything. `or set()` normalizes that None
        # to an empty set so the stale scan below can test membership
        # unconditionally (no EOL flagging when the schedule is unusable
        # or the mode is 'off').
        eol_lines = self._eol_lines() or set()
        if eol_lines:
            upstream_versions = {
                v for v in upstream_versions
                if self._line_of(v) not in eol_lines
            }
            if not upstream_versions:
                # A healthy schedule always leaves the current release
                # active. If filtering removed every line, the schedule
                # data is suspect (e.g. a restructured page shifted our
                # columns); track everything rather than go silent.
                upstream_versions = {
                    v for v in (self._version_of(iso) for iso in upstream_isos)
                    if v is not None
                }

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
        eol_lines_present: set[str] = set()
        for path in local_isos:
            ver = self._version_of(path.name)
            line = self._line_of(ver) if ver is not None else None
            if line is not None and line in eol_lines:
                # Canonical keeps past-EOL ISOs on the tracker, so the
                # upstream_set skip below would hide them; flag the line
                # (grouped) until the local files are removed.
                eol_lines_present.add(line)
                continue
            if path.name in upstream_set:
                continue
            if ver is not None and ver not in current_versions:
                # Superseded within its line, or the whole line is gone from
                # the tracker; either way, group instead of one alert per file.
                stale_versions.add(ver)
            else:
                # Current-version (or unparseable) file dropped from the tracker;
                # unusual enough to keep visible individually. torrent.ubuntu.com
                # tracks all official flavors (kubuntu, xubuntu, lubuntu,
                # edubuntu, ubuntu-mate/budgie/gnome/unity/cinnamon/kylin/studio,
                # ubuntu-mini-iso, mythbuntu, ...), confirmed directly against
                # the live tracker_index -- not just plain ubuntu-*.iso -- so
                # there's no reason to special-case the prefix here.
                self.alert(f'STALE:{path.name}')

        for ver in sorted(stale_versions, key=ver_key):
            self.alert(f'STALE:Ubuntu-{ver}')
        for line in sorted(eol_lines_present, key=ver_key):
            self.alert(f'EOL:Ubuntu-{line}')


class ProxmoxChecker(Checker):
    """Proxmox VE — every ISO the proxmox.com downloads page offers, in all architectures.

    Filenames are taken verbatim from the enterprise.proxmox.com/iso/*.iso
    download hrefs rather than reconstructed from version strings, so every
    architecture the page offers (x86_64, arm64) is tracked as its own ISO.

    Alerts:
      NEW:proxmox-ve_X.Y-Z[-ARCH] - ISO on page but no local copy exists
      ORPHAN:proxmox-ve_X.Y-Z.iso - ISO on disk but unknown to transmission
      STALE:proxmox-ve_X.Y-Z.iso  - local ISO superseded within its major series
      DROPPED:Proxmox-MAJOR       - local ISOs exist for a major absent from the page
    """

    def check(self) -> None:
        url = 'https://www.proxmox.com/en/downloads/proxmox-virtual-environment'
        if not self.fetch(url, 'Proxmox'):
            return
        if not self.body_ok('www.proxmox.com'):
            return

        # The page's download buttons link the exact ISO filenames, e.g.
        # https://enterprise.proxmox.com/iso/proxmox-ve_9.2-1-arm64.iso
        # (plus a sibling .iso.torrent per entry, which the .iso$ anchor
        # excludes). Parsing hrefs instead of reconstructing names from
        # version strings keeps every offered architecture in scope.
        versions = sorted(set(re.findall(
            r'enterprise\.proxmox\.com/iso/(proxmox-ve_[\w.\-]+\.iso)"',
            self._page)),
            key=ver_key,
        )
        # Page structure could change; alert and bail if it does
        if not versions:
            self.alert('MALFORMED:Proxmox-Downloads')
            return

        # The extraction regex above only guarantees the "proxmox-ve_"
        # prefix, not a digit right after it (its character class also
        # matches letters), so a malformed upstream filename (e.g.
        # "proxmox-ve_beta.iso") must be skipped for major purposes rather
        # than crash the whole checker on None.group(1) -- the same
        # tolerance the local-file branch below applies to disk files.
        page_majors = set()
        for v in versions:
            match = re.match(r'proxmox-ve_(\d+)', v)
            if match:
                page_majors.add(match.group(1))

        for iso in versions:
            self.check_iso(iso, f'NEW:{iso.removesuffix(".iso")}')

        for path in self.iso_dir.glob('proxmox-ve_*.iso'):
            if not (path.exists() and path.stat().st_size > 0):
                continue
            if path.name in versions:
                continue
            m = re.match(r'proxmox-ve_(\d+)', path.name)
            major = m.group(1) if m else None
            if major and major in page_majors:
                self.alert(f'STALE:{path.name}')
            else:
                self.alert(f'DROPPED:Proxmox-{major or "unknown"}')


class DebianChecker(Checker):
    """Debian — the current point release from cdimage.debian.org's torrent listing (via rsync).

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
            self._debug(f'rsync failed (exit {result.returncode})')
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
                # Current-version (or unparseable) local ISO dropped from the tracker;
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


def _short_name(cls: type[Checker]) -> str:
    """'MintChecker' -> 'mint': the short command-line selector for a
    checker (the full class name is accepted as well)."""
    return cls.__name__.removesuffix('Checker').lower()


def _resolve_checker(name: str | None) -> type[Checker] | None:
    """Map a command-line selector to its checker class.

    None (no argument given) means the full suite. Otherwise name is
    matched case-insensitively against the CHECKERS entries, accepting
    either the full class name (MintChecker) or the short name (mint).
    CHECKERS is the single source of truth, so a checker added there
    becomes selectable without touching this. An unknown name raises
    ValueError listing the valid selectors.
    """
    if name is None:
        return None
    wanted = name.lower()
    for cls in CHECKERS:
        if wanted in (cls.__name__.lower(), _short_name(cls)):
            return cls
    valid = ', '.join(_short_name(cls) for cls in CHECKERS)
    raise ValueError(f"unknown checker '{name}' (choose from: {valid})")


def _checkers_epilogue() -> str:
    """The --help 'available checkers' list. Generated from CHECKERS and
    each class docstring's first line so it can't drift from reality."""
    lines = ['available checkers:']
    for cls in CHECKERS:
        doc_lines = (cls.__doc__ or '').strip().splitlines()
        first = doc_lines[0] if doc_lines else _short_name(cls)
        lines.append(f'  {_short_name(cls):<8} {first}')
    return '\n'.join(lines)


def _parse_args(argv: list[str]) -> argparse.Namespace:
    """Parse and validate the command line.

    Returns a namespace with .checker_cls (None for the full suite) and
    .verbose. Raises ValueError for an unknown checker name; argparse
    itself raises SystemExit(2) on other usage errors and SystemExit(0)
    after printing help for --help.
    """
    parser = argparse.ArgumentParser(
        prog='new-torrents.py',
        description=(
            'Check for new ISO torrents to mirror on mirror.tsue.net.\n'
            '\n'
            'Alerts print to stdout, one per line; any alert makes the run\n'
            'exit non-zero so healthchecks.io fires. Each alert repeats\n'
            'every run until the condition clears. Forms:\n'
            '  NEW:<target>    upstream offers it, but no local copy exists yet\n'
            '  ORPHAN:<target> on disk, but transmission has no record of it\n'
            '  STALE:<target>  no longer offered upstream (superseded or removed)\n'
            '  EOL:<line>      local ISOs exist for a release line past EOL\n'
            '  DROPPED:<line>  local ISOs exist for a version no longer offered\n'
            '  MISSING:<glob>  no ISOs of that distro found on disk at all\n'
            '  MALFORMED:<src> upstream page or listing returned no usable data\n'
            '  UNSAFE:<name>   upstream-supplied name would escape the download dir\n'
            '  EXCEPTION:<cls> a checker crashed unexpectedly (details follow)\n'
            '  <domain>        fetches from that upstream domain are failing:\n'
            '                  repeated errors, or empty/error responses'
        ),
        epilog=_checkers_epilogue(),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        'checker',
        nargs='?',
        metavar='CHECKER',
        help='run only this checker (e.g. mint); default: run all of them',
    )
    parser.add_argument(
        '--verbose',
        action='store_true',
        help='show the live per-checker status display (default: on when '
             'stderr is a terminal, off under cron)',
    )
    args = parser.parse_args(argv)
    args.checker_cls = _resolve_checker(args.checker)
    return args


def _run(args: argparse.Namespace) -> int:
    """The actual check logic. Split out from main() so main() can focus
    purely on argument parsing and the lockfile, and so this can be
    called only once the lock is safely held."""
    # Bail early if the download directory is missing
    if not ISO_DIR.is_dir():
        print(f'ERROR: transmission download directory {ISO_DIR} is missing. Exiting.')
        return 1

    # Bail early if rsync is missing
    if shutil.which('rsync') is None:
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

    # Run the selected checker(s) concurrently. Show the live status
    # display when running interactively or when --verbose is passed;
    # cron gets quiet output.
    checkers = [args.checker_cls] if args.checker_cls is not None else CHECKERS
    interactive = sys.stderr.isatty() or args.verbose
    names = [cls.__name__ for cls in checkers]
    display = StatusDisplay(names) if interactive else None
    failures = FailureTracker(FAIL_FILE, FAIL_THRESHOLD)
    instances = [
        cls(ISO_DIR, status_content, failures, display) for cls in checkers
    ]
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


def main(argv: list[str] | None = None) -> int:
    """Parse arguments, acquire the lockfile, then run the actual checks.

    argv defaults to sys.argv[1:] when omitted; tests pass it explicitly
    so they never inherit the test runner's own arguments. Arguments are
    parsed before the lock is touched: --help and usage errors (e.g. an
    unknown checker name) shouldn't need -- or create -- a lockfile.

    A held lock means either a legitimate overlap (a manual run colliding
    with cron) or a prior invocation stuck far longer than it should be --
    either way worth surfacing rather than silently no-op'ing, since a
    silent 0 here would mean healthchecks.io never fires and a genuinely
    stuck run could go unnoticed indefinitely. flock() ties the lock to
    this process's open file descriptor rather than the lock file's
    on-disk content, so it can't go stale across a crash, kill -9, or
    reboot the way a hand-rolled PID file could.
    """
    try:
        args = _parse_args(sys.argv[1:] if argv is None else argv)
    except ValueError as e:
        print(f'ERROR: {e}. Exiting.', file=sys.stderr)
        return 2

    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    lock_fd = open(LOCK_FILE, 'w')
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock_fd.close()
        print(f'ERROR: another instance is already running (lock held on {LOCK_FILE}). Exiting.')
        return 1

    lock_fd.write(str(os.getpid()))
    lock_fd.flush()
    try:
        return _run(args)
    finally:
        lock_fd.close()


if __name__ == '__main__':
    sys.exit(main())
