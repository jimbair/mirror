# AGENTS.md

Guidance for AI coding agents (and humans) working in this repository.

## What this repo is

Scripts that operate the Linux torrent mirror at [mirror.tsue.net](https://mirror.tsue.net/).
The production host runs AlmaLinux 10 with Transmission; these scripts live in
cron there. The repo itself has no build step and no runtime dependencies beyond
the Python 3 standard library.

## Commands

All commands run from the repo root (the directory containing this file):

```sh
python3 tests/test_new_torrents.py -v   # tests for new-torrents.py
python3 tests/test_status_update.py -v  # tests for status_update.py
python3 new-torrents.py --help          # checker CLI; run checkers with --only <name>
python3 status_update.py                # build status page (needs transmission-remote, vnstat)
```

- Tests are plain `unittest`, no external dependencies, no package install.
  The scripts under test are imported via `importlib` in each test file (the
  hyphen in `new-torrents.py` rules out a plain import) — preserve that pattern
  if you add or rename test files.
- CI (`.github/workflows/test.yml`) runs both test suites on Ubuntu (Python 3.11)
  and in an `almalinux:10` container, and only when the script/test/workflow
  paths change. If you touch a script but not its test file, update the
  workflow's `paths` list only if the relationship itself changes.
- `new-torrents.py` does real network fetches and reads
  `/var/lib/transmission/Downloads` when run without mocked inputs; don't run
  the full checkers casually — tests are the safe way to exercise behavior.

## Architecture invariants

These behaviors are load-bearing; the comments in the code explain the "why".
Keep them (and their comments) intact when refactoring.

### new-torrents.py (hourly cron checker)

- **Alert contract:** each checker prints one alert per line to stdout; any
  alert makes the process exit non-zero, which is how healthchecks.io fires.
  Alerts repeat every run until the condition clears — a silent no-op (exit 0)
  is a bug, not an optimization. Never "fix" an alert into silence.
- **status.txt is ground truth** for what Transmission already knows about.
  `status_update.py` writes it atomically precisely so concurrent readers
  (including this script) never see a half-written file. Preserve the
  temp-then-install pattern.
- **Threading invariant:** checkers run in a `ThreadPoolExecutor`, and each
  failure-tracker counter name is owned by exactly one checker thread — that
  is why `FailureTracker` is unsynchronized by design. If a new checker ever
  shares a fetch-failure name with another, add the lock the docstring says to
  add.
- **Failure thresholding:** a domain is only reported down after
  `FAIL_THRESHOLD` (3) consecutive failures; transient outages are swallowed
  by design. The Ubuntu checker additionally falls back to the cached EOL
  schedule (`~/.config/new-torrents/ubuntu-eol.json`) so a one-off schedule
  outage doesn't re-alert every past-EOL line as NEW.
- **Locking:** `fcntl.flock` on `~/.config/new-torrents/lock` prevents
  overlapping runs; a held lock must exit non-zero and loudly (a silent skip
  would mask a stuck run from healthchecks).

### status_update.py (status page builder)

- The `transmission-remote -l` table rendering is **verified byte-for-byte
  against real Transmission 3.x output** (fixed column widths, 2-space gutters,
  no auto-widening). Don't "improve" the formatting to be data-driven —
  matching transmission-remote exactly is the requirement.
- `TransmissionRow.from_line` parsing depends on the observation that Have/ETA
  are either a bare token or `NUMBER UNIT`; the unit frozensets are the
  boundary between those fields and the free-text Name column.

### Checker classes

Each `Checker` subclass documents in its docstring exactly what it watches
(that text feeds `--help`). When adding a distro, follow the existing shape:
fetch → parse → `check_iso`/`check_dir` comparisons against status.txt and
disk → emit `NEW:`/`ORPHAN:`/`STALE:`/`EOL:`/`DROPPED:`/`MISSING:` alerts.

## Adding a new distro checker

The feature-addition recipe for `new-torrents.py`:

1. Subclass `Checker` and implement `check()`. The class **must** have a
   docstring whose first line states what it watches: the `--help` checker
   list is generated from those first lines, and a missing docstring has
   crashed `--help` before.
2. Append the class to the `CHECKERS` list. That list is the single source
   of truth — CLI selection (short name and class name), the `--help`
   epilogue, and the default full-suite run all derive from it. Nothing
   else to wire up.
3. Do all network I/O through `self.fetch(url, name)` + `self.body_ok(...)`,
   and all disk/status comparisons through `self.check_iso()` /
   `self.check_dir()`. Those already implement the path-traversal guard
   (`_safe_path`) and whole-token status matching — don't reimplement
   either.
4. Give the checker its own fetch-failure counter name(s); no two checker
   threads may share a name (threading invariant above).
5. If the upstream page doesn't match the expected shape, alert
   `MALFORMED:<source>` — never silently skip. An unhandled exception is
   caught by `Checker.run()` and reported as `EXCEPTION:<Checker>`, which
   keeps the failure isolated, but a clean MALFORMED is the established
   response to structure drift (see Fedora's `torrents.json` handling for
   the careful pattern: skip bad entries, alert on unusable pages).
6. Test with the shared helpers at the top of `tests/test_new_torrents.py`:
   `make_checker(cls, iso_dir, status_content, ...)` to construct, and
   `fake_fetch_fn` / `fake_fetch_seq` to inject pages (both pad past
   `body_ok()`'s 250-char minimum), `patch('subprocess.run')` for
   rsync-based checkers, and an injected `now` clock for date logic.
   Call `main()` with an explicit `argv` list, never relying on `sys.argv`.
7. If you add a *new* test file (rather than extending an existing one),
   add its path to the `paths` filter in `.github/workflows/test.yml` and
   give it a job; the existing test files are already covered.

Exit-code contract to preserve in any change: 0 = clean, 1 = alerts present
or lock held, 2 = usage error. `--help` exits without creating the lockfile.

## Bug-scan primer

Recurring bug classes in this codebase (all from real regressions) and where
they live:

- **Fragile upstream parsing.** Every checker scrapes HTML, JSON, or an rsync
  listing with no schema guarantee. Check parse assumptions against the live
  page, not just the fixtures; unexpected shape must become a `MALFORMED:`
  alert.
- **Transfer/encoding quirks** in `Checker.fetch()`: raw-deflate mislabeled
  as `deflate`, garbage charsets, truncated gzip (`EOFError`), mid-response
  drops (`http.client.HTTPException`). Each has a `test_fetch_*` case.
- **Name matching** must stay whole-token (`_status_tokens` set
  membership); a substring check would hide a missing release (`-42` vs
  `-420`).
- **Scraped names are untrusted input**: `_safe_path()` blocks `..`/absolute
  traversal (alerting `UNSAFE:`); `stat()` must tolerate dangling symlinks;
  zero-byte ISOs count as missing.
- **Numeric vs lexical ordering**: versions and release dates are compared
  as numbers/tuples, never strings (a CachyOS regression was exactly this).
- **Date-dependent logic**: EOL verdicts re-derive from cached dates as the
  clock advances (`_eol_from()` is a pure function of data + `now`). New date
  logic must take an injectable clock or its tests will rot.
- **Alert fan-out**: one upstream release change spans many files — group
  alerts (Mint's version-bump logic) instead of firing one per file.
- **State durability**: every persisted file (`failures.json`,
  `ubuntu-eol.json`, `status.txt`) is written temp-then-`os.replace` in the
  same directory so a crash can't wipe or corrupt it.
- **`status_update.py`**'s high-risk area is the table: rendering is
  byte-for-byte against real `transmission-remote -l`; the `SAMPLE_OUTPUT`
  fixture in its test file is the reference.

## Conventions

- **Python:** standard library only, Python 3.11+ syntax (CI pins 3.11 on
  Ubuntu and distro python3 on AlmaLinux). No third-party imports.
- **Comments:** the codebase documents invariants, edge cases, and historical
  bugs in long inline comments (e.g. the flock-vs-PID-file rationale, the
  sort-key fallback behavior). Write new comments at the same depth: explain
  *why*, especially where behavior looks surprising or has a production
  incident behind it. Do not strip them during refactors.
- **Tests:** add or extend tests for behavior changes in the matching
  `tests/test_*.py`. Tests construct their own fixtures (sample tables, temp
  dirs) and mock the network — follow that style; clean up tempdirs.
- **Commits:** conventional-commit style with a scope, lowercase subject
  (`fix(ubuntu): ...`, `test(fetch): ...`, `chore: ...`, `docs: ...`).
- **Branches:** prefix new branches `bionic/` (e.g. `bionic/fix-...`), per
  existing PR history.

## Shell scripts (not covered by CI)

- `debian-torrents-fetch.sh` — rsyncs all current Debian `.torrent` files;
  run manually when a new Debian release drops.
- `new-speedtest.sh` — interactive; re-runs the speedtest and swaps
  `/home/jim/log/speedtest.log` if you accept it.
- `status-update.sh` — the pre-Python shell version of `status_update.py`.
  `status_update.py` is the maintained one; keep this only for reference and
  don't fix bugs here.
- `transmission-upgrade.sh` — builds Transmission from source on AlmaLinux
  (root, dnf) while upstream package bugs are open. Destructive by design
  (replaces the system daemon); verify version and checksums before running
  it on a host.

## Context that lives outside this repo

- **Deployment**: how these scripts reach the production host (sync method)
  and the cron entries that run them are not in this repo; neither is the
  healthchecks.io URL — the ping is done by the cron wrapper, not the
  scripts. Ask the maintainer before assuming.
- **Host state**: ISOs, `~/.config/new-torrents/`, and vnstat/speedtest data
  exist only on the production host, never in this checkout.

## Production paths referenced in code

- ISOs/downloads: `/var/lib/transmission/Downloads` (`ISO_DIR`)
- Status page: `.../Downloads/status.txt`
- Speedtest log: `/home/jim/log/speedtest.log`
- Runtime state: `~/.config/new-torrents/` (failures.json, lock, ubuntu-eol.json)

When editing constants, remember these are hardcoded for the production host
and the tests rely on the same values.
