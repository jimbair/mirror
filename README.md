# Mirror scripts

Scripts used to maintain the Linux torrent mirror at
[mirror.tsue.net](https://mirror.tsue.net/). The production host runs AlmaLinux
10 and Transmission. Deployment is manual: pushing this repository does not
update the production scripts.

## Requirements

- Python 3.11 or newer on a Unix system. Python code uses only the standard
  library; there is no package installation or build step.
- `rsync` for the Debian checker and the default full checker run. Other
  individual checkers do not require it.
- The status builders need the configured host's Transmission, vnStat,
  IPv6 statistics, and speedtest data. The active shell builder also uses
  Bash and standard Unix utilities.

On macOS, the system `python3` may be too old. Install a modern Python with
`brew install python@3.11` and use `python3.11`, or
`/opt/homebrew/bin/python3.11` if Homebrew is outside your PATH. On Linux,
`python3` is suitable when its version is at least 3.11.

## Checking torrents

Run these commands from the repository root on the configured mirror host:

```sh
python3.11 new-torrents.py --help
python3.11 new-torrents.py                 # all checkers
python3.11 new-torrents.py arch            # one checker
python3.11 new-torrents.py mint --verbose  # one checker with progress
```

Available selectors are `alma`, `arch`, `cachy`, `debian`, `fedora`, `mint`,
`proxmox`, and `ubuntu`. Selectors are case-insensitive; full class names such
as `MintChecker` also work. Progress goes to stderr and is enabled automatically
when stderr is a terminal. Alerts go to stdout.

The checker compares upstream listings with
`/var/lib/transmission/Downloads/status.txt` and local files. `status.txt` is
the source of truth for what Transmission already knows about. The checker
reports work to do; downloading, adding, and removing torrents remain manual.

| Exit code | Meaning |
| --- | --- |
| `0` | Clean run (including fetch failures below the alert threshold), or successful self-update. |
| `1` | Alerts, operational/update errors, or an overlapping run holding the lock. |
| `2` | Invalid command-line arguments. |

Alerts repeat each run until their condition clears. Version-level alerts
group related files so a release change does not produce one alert per ISO.

| Alert | Meaning |
| --- | --- |
| `NEW:<target>` | A release or artifact needs mirroring. |
| `ORPHAN:<target>` | Present on disk but unknown to Transmission. |
| `STALE:<target>` | A local artifact has been superseded or removed upstream. |
| `EOL:<line>` | Local files belong to a release past its support policy. |
| `DROPPED:<line>` | A locally mirrored release has disappeared upstream. |
| `MISSING:<glob>` | No local artifacts matched the distro's expected glob. |
| `MALFORMED:<source>` | Upstream metadata is unusable or incomplete. |
| `DOWN:<domain>` | Fetch failures reached three consecutive attempts for that source; details follow. |
| `UNSAFE:<name>` | An upstream name resolves outside the download directory. |
| `EXCEPTION:<checker>` | A checker encountered an unexpected exception. |
| `ERROR:...` | A local prerequisite or state operation failed. |
| A bare hostname | A fetched response was empty or unexpectedly short. |

Failure histories are independent per source, including each Mint release
directory. Malformed metadata alerts immediately; usable portions can still
be checked. Incomplete release metadata is not evidence of removal.

Ubuntu uses the newest listed revision of each artifact within each release
line. Its default EOL policy retains a line through the final Canonical support
tier, including paid legacy support. Mint tracks all releases on its supported
versions page. Both checkers retain validated support metadata for upstream
outages; Ubuntu stores dates and recalculates EOL as the clock advances.
When Ubuntu prunes expired schedule rows, cached dates preserve their EOL
history while fresh dates continue to update. Losing or shortening an
unexpired tier, or receiving an incomplete table, alerts and retains the
previous cache. Genuine upstream support reductions also require review
instead of silently making an active release eligible for cleanup.

## Updating the checker

Update the installed `new-torrents.py` from this repository's latest `main`
branch:

```sh
python3.11 new-torrents.py --update
# Equivalent: python3.11 new-torrents.py -u
```

Use the update option on its own. It requires network access and write access
to the script's directory. The download is limited to 2 MiB and checked for
valid Python and expected checker classes/functions without executing it.
It replaces the script atomically, preserving permissions and any symlink
used to launch it. An identical download reports that it is already up to date.

Updates overwrite local edits to `new-torrents.py`. Only this script is updated;
the command exits and the next invocation uses the new version. Updating runs
independently of torrent checks and needs no rsync, downloads, status file, or
runtime-state directory. Update errors leave the installed script intact and
exit 1; combining the option with a checker name or `--verbose` is a usage
error (exit 2).

## State and scheduling

The production checker runs hourly; the active `status-update.sh` builder runs
every five minutes. The host's cron wrapper handles monitoring using the exit
code. Monitoring credentials and complete cron entries do not belong in this
repository.

Runtime state lives under `~/.config/new-torrents/`:

- `lock`: a kernel `flock` prevents overlapping runs.
- `failures.json`: consecutive source failure counts.
- `ubuntu-eol.json`: validated Ubuntu support-end dates.
- `mint-supported.json`: validated Mint support membership and the archive
  versions known when it was captured. Cached absence cannot make a later
  release EOL.

The checker's state snapshots and the active shell builder's `status.txt` are
published through temporary files followed by same-directory atomic replacement.
State-write failures are reported while completed checker alerts remain available.

Checker HTTP fetches allow 8 MiB of encoded data and 16 MiB after decompression.
A 30-second elapsed budget is checked between reads alongside a 10-second
socket timeout. One blocking operation can outlast the remaining budget; this
is not a process-level deadline for operating-system DNS or connection setup.
Debian's rsync listing has a separate 60-second timeout.

`status_update.py` is the Python status-builder alternative; migration from the
active shell version is still pending. Both builders rely on production paths
and data that are absent from a normal development checkout. Both publish
`status.txt` atomically and require write access to its parent directory;
file-only write permission is insufficient. Set up directory access for the
cron user before switching builders.

`mirror-index.sh` separately generates `.theme/header.html` and
`.theme/footer.html` under the download directory for the host's nginx
autoindex wrapper. The `.theme` directory and nginx integration must already
exist, and the cron user needs write access to that directory. Both fragments
are built before publication; each is replaced atomically with mode `0644`.
A collection or build failure leaves both previous fragments in place. A
failure of the second rename can leave a new header with the previous footer;
the command still exits non-zero, and each fragment remains complete.

The footer publishes the same host data as `status-update.sh`: the complete
speedtest log, vnStat output, IPv6 statistics, and the torrent listing sorted
by ratio. Dynamic text is HTML-escaped. This script does not update
`status.txt`; keep the existing status-builder job running because the torrent
checker still relies on that file.

## Tests and development

Tests create temporary files and mock network access. They do not require
Transmission, rsync, or production data:

```sh
python3.11 -B tests/test_new_torrents.py -v
python3.11 -B tests/test_status_update.py -v
python3.11 -B tests/test_status_update_sh.py -v
python3.11 -B tests/test_mirror_index.py -v
python3.11 -B tests/test_transmission_upgrade.py -v
python3.11 -B tests/test_debian_torrents_fetch.py -v
bash -n status-update.sh
bash -n mirror-index.sh
bash -n transmission-upgrade.sh
bash -n debian-torrents-fetch.sh
```

CI runs the suites on Ubuntu and AlmaLinux 10. Upgrade tests stub host commands
and exercise service recovery without root or a real installation.
Torrent-fetch tests stub rsync and keep all downloads in temporary directories.
Representative upstream snippets and their source URLs live in
[tests/fixtures](tests/fixtures/README.md).
See [AGENTS.md](AGENTS.md) for architecture invariants and contribution guidance.

## Commit conventions

Sign off every commit with `git commit -s`.

Follow the active client's native AI-attribution defaults unchanged. Do not
override its trailer format or identity, add a second credit trailer, or
add model names or reasoning-effort labels unless the client includes them
by default.
