# AGENTS.md

Guidance for AI coding agents (and humans) working in this repository.

## What this repo is

Scripts that operate the Linux torrent mirror at [mirror.tsue.net](https://mirror.tsue.net/).
The production host runs AlmaLinux 10 with Transmission; these scripts live in
cron there. The repo has no build step or third-party Python packages. The
Debian checker needs rsync; the status builders also need the host tools and
data described in README.md. The checker uses Unix-only `fcntl.flock`.

## Commands

All commands run from the repo root (the directory containing this file):

```sh
python3 tests/test_new_torrents.py -v   # tests for new-torrents.py
python3 tests/test_status_update.py -v  # tests for status_update.py
python3 tests/test_status_update_sh.py -v  # active shell publication tests
python3 new-torrents.py --help          # checker CLI; select with a positional name
python3 new-torrents.py mint            # just Mint; names are case-insensitive
bash -n status-update.sh               # shell syntax check
python3 status_update.py                # build status page (needs transmission-remote, vnstat)
```

- **macOS interpreters:** the system `python3` (Xcode Command Line
  Tools, 3.9.x) is too old for this repo — it fails at import time on
  the `Path | None` annotations (the scripts use 3.11+ syntax; CI pins
  3.11). The suggested solution is a locally installed modern Python from
  Homebrew: `brew install python@3.11`, then run the commands above with
  the versioned interpreter (`python3.11 tests/test_new_torrents.py -v`).
  Don't rely on a bare `python3` on macOS — the maintainer's shell does
  not have `/opt/homebrew/bin` in PATH, so it still resolves to the
  system 3.9; use the full path `/opt/homebrew/bin/python3.11` if the
  versioned name isn't in PATH. Any Python 3.11+ works.

- Tests are plain `unittest`, no external dependencies, no package install.
  The scripts under test are imported via `importlib` in each test file (the
  hyphen in `new-torrents.py` rules out a plain import) — preserve that pattern
  if you add or rename test files.
- CI (`.github/workflows/test.yml`) runs all three suites on Ubuntu (Python 3.11)
  and in an `almalinux:10` container, and only when the script/test/workflow
  paths or `tests/fixtures/**` change. If you touch a script but not its test
  file, update the workflow's `paths` list only if the relationship changes.
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
  `status-update.sh` writes it atomically precisely so concurrent readers
  (including this script) never see a half-written file. Preserve the
  temp-then-install pattern.
- **Threading invariant:** checkers run in a `ThreadPoolExecutor`, and each
  failure-tracker counter name is owned by exactly one checker thread — that
  is why `FailureTracker` is unsynchronized by design. If a new checker ever
  shares a fetch-failure name with another, add the lock the docstring says to
  add.
- **Failure thresholding:** a domain is only reported down after
  `FAIL_THRESHOLD` (3) consecutive failures; transient outages are swallowed
  by design. Mint directory counters are per release, so healthy siblings
  cannot reset a persistent outage. Ubuntu and Mint fall back to validated
  support caches (`ubuntu-eol.json` and `mint-supported.json`), preventing
  transient outages or partial parses from replacing reliable metadata.
- **Malformed is not removed:** valid Fedora entries are still checked when
  siblings are malformed, but incomplete lists cannot justify STALE/DROPPED
  cleanup alerts for their affected version. Both support parsers require
  complete table structure before replacing their caches. Cached Mint
  absence can classify only archive versions known at cache time as EOL.
- **State-write errors:** report them without discarding completed alerts or
  the freshly parsed support verdict. Close the display even if persistence
  fails. State loaders treat invalid JSON and non-UTF-8 files as corrupt.
- **HTTP bounds:** encoded responses are limited to 8 MiB and decompressed
  bodies to 16 MiB. A 30-second read budget is checked between bounded reads,
  alongside a 10-second socket timeout; it is not an OS-level DNS watchdog.
  Keep gzip and both wrapped/raw deflate support, and use failure thresholding
  for response-limit errors just as for other failed fetches.
- **Locking:** `fcntl.flock` on `~/.config/new-torrents/lock` prevents
  overlapping runs; a held lock must exit non-zero and loudly (a silent skip
  would mask a stuck run from healthchecks).

### status_update.py (Python status page builder)

- The `transmission-remote -l` table rendering is **verified byte-for-byte
  against real Transmission 4.1.3 captures** (fixed column widths, 2-space gutters,
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
   the careful pattern: check valid entries, alert on unusable metadata, and
   avoid removal conclusions for malformed versions).
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
  `ubuntu-eol.json`, `mint-supported.json`, `status.txt`) is published through
  a temporary file and same-directory replacement (`os.replace` in Python,
  `mv` in the shell builder). Never copy over the public destination: that
  exposes a truncated snapshot to concurrent readers.
- **`status_update.py`**'s high-risk area is the table: rendering is
  byte-for-byte against real `transmission-remote -l`; the `SAMPLE_OUTPUT`
  fixture covers alignment, and the captured Transmission 4.1.3 fixtures
  pin exact byte output, including error-marked IDs and overflow ratios.

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
- **Sign-off and attribution:** sign off every commit with `git commit -s`.
  Follow the client's default attribution convention, using a single
  assistance or co-author trailer. Do not add both forms of attribution.
- **Branches:** prefix new branches `bionic/` (e.g. `bionic/fix-...`), per
  existing PR history.

## Shell scripts

- `debian-torrents-fetch.sh` — rsyncs all current Debian `.torrent` files;
  run manually when a new Debian release drops.
- `new-speedtest.sh` — interactive; re-runs the speedtest and swaps
  `/home/jim/log/speedtest.log` if you accept it.
- `status-update.sh` — the original version of `status_update.py`.
  `status-update.sh` is the version actively in use; migrating to the python
  version is currently a TODO to be completed by the author. Publication is
  tested in `tests/test_status_update_sh.py` on Ubuntu and AlmaLinux in CI;
  the host-specific collectors are stubbed. Other shell scripts have no CI.
- `transmission-upgrade.sh` — builds Transmission from source on AlmaLinux
  (root, dnf) while upstream package bugs are open. Destructive by design
  (replaces the system daemon); verify version and checksums before running
  it on a host.

## Context that lives outside this repo

- **Deployment is manual**: changes in this repo do not reach the production
  host automatically — the maintainer copies them over by hand. A merged
  change is not live until it has been deployed.
- **Cadence (informational only)**: `new-torrents.py` runs hourly and
  `status-update.sh` runs every 5 minutes on the production host. The actual
  cron entries live on the host and are intentionally not in this repo.
- **The healthchecks.io URL is secret — never write it into this repo.**
  The ping is done by the cron wrapper, not the scripts; publishing the URL
  (or its /fail endpoint) would let anyone poison the alerts or mask a real
  outage. Do not add the URL, a ping endpoint, or full cron lines to code,
  comments, tests, or documentation, and treat any pasted in by a user the
  same way: flag it rather than commit it.
- **Host state**: ISOs, `~/.config/new-torrents/`, and vnstat/speedtest data
  exist only on the production host, never in this checkout.

## Production paths referenced in code

- ISOs/downloads: `/var/lib/transmission/Downloads` (`ISO_DIR`)
- Status page: `.../Downloads/status.txt`
- Speedtest log: `/home/jim/log/speedtest.log`
- Runtime state: `~/.config/new-torrents/` (failures.json, lock,
  ubuntu-eol.json, mint-supported.json)

When editing constants, remember these are hardcoded for the production host
and the tests rely on the same values.
