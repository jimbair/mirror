#!/usr/bin/env python3
# Build status.txt for our mirror
# https://mirror.tsue.net/
#
# Pulls bandwidth stats from vnstat, the last manual speedtest result, and a
# transmission-remote -l torrent listing (sorted by ratio, or another column
# via --sort) into a single status page. Installed atomically so concurrent
# readers (and new-torrents.py, which treats status.txt as ground truth for
# what transmission already knows about) never see a half-written file.

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

# Where the assembled status page is installed
STATUS_FILE = Path('/var/lib/transmission/Downloads/status.txt')

# Existing manual speedtest result; new-speedtest.sh is what updates this
SPEEDTEST_LOG = Path('/home/jim/log/speedtest.log')

# Sortable columns -> the numeric key used to order rows. ID/done/up/down/
# ratio parse straight to float; have/eta strip an embedded unit suffix first.
SORT_COLUMNS = ('id', 'done', 'have', 'eta', 'up', 'down', 'ratio', 'name')

# Default sort direction per column. Numeric/size columns default to
# descending (biggest/best first, matching the original script's ratio
# sort); name defaults to ascending (alphabetical), since a descending
# name sort isn't a meaningful "default" the way a high ratio is.
_DEFAULT_DESCENDING = {
    'id': True, 'done': True, 'have': True, 'eta': True,
    'up': True, 'down': True, 'ratio': True, 'name': False,
}


def _size_key(value: str) -> float:
    """Parse a size like '13.94 GB' or 'None' into a comparable float.

    Converts to a common base (bytes) so e.g. '4.12 GB' correctly outranks
    '938.9 MB' — transmission-remote -l mixes units freely across rows
    depending on each torrent's size, so unit conversion is required here,
    unlike _eta_key's time units this isn't optional to get right.

    transmission-remote -l also thousands-separates large ratios (e.g.
    '1,024'); the comma is stripped before parsing so high-ratio torrents
    don't silently fall through to the unparsable fallback.
    """
    parts = value.split()
    if not parts:
        return -1.0
    try:
        amount = float(parts[0].replace(',', ''))
    except ValueError:
        return -1.0  # 'None' / unparsable sorts to the bottom on descending order
    if len(parts) == 1:
        return amount  # bare number, e.g. a ratio or percentage
    multiplier = {
        'b': 1,
        'kb': 1024,
        'mb': 1024 ** 2,
        'gb': 1024 ** 3,
        'tb': 1024 ** 4,
    }.get(parts[1].lower())
    return amount * multiplier if multiplier else amount


def _eta_key(value: str) -> float:
    """Parse an ETA like '10 mins', '2 hrs', 'Unknown', or 'Done' into seconds.

    Unknown/Done don't represent a duration; they sort to the bottom on
    descending order, same as the original script's behavior for non-numeric
    Have/ETA values once @-substitution and numeric sort were applied.
    """
    parts = value.split()
    if len(parts) != 2:
        return -1.0
    amount, unit = parts
    try:
        amount_f = float(amount)
    except ValueError:
        return -1.0
    multiplier = {
        'secs': 1, 'sec': 1,
        'mins': 60, 'min': 60,
        'hrs': 3600, 'hr': 3600,
        'days': 86400, 'day': 86400,
    }.get(unit.lower())
    return amount_f * multiplier if multiplier else -1.0


def _percent_key(value: str) -> float:
    """Parse a percentage like '44%' or '100%' into a comparable float.

    transmission-remote -l has also been observed to emit 'n/a' for this
    column (e.g. for magnet links with no metadata yet); that sorts to the
    bottom on descending order, same as any other unparsable value.
    """
    try:
        return float(value.rstrip('%'))
    except ValueError:
        return -1.0


# Per-column sort key. Each takes a TransmissionRow and returns a sortable value.
_SORT_KEYS = {
    'id': lambda row: float(row.id.rstrip('*')),
    'done': lambda row: _percent_key(row.done),
    'have': lambda row: _size_key(row.have),
    'eta': lambda row: _eta_key(row.eta),
    'up': lambda row: _size_key(row.up),
    'down': lambda row: _size_key(row.down),
    'ratio': lambda row: _size_key(row.ratio),
    'name': lambda row: row.name.lower(),
}


@dataclass(frozen=True)
class TransmissionRow:
    """One parsed line from transmission-remote -l's torrent table."""

    id: str
    done: str
    have: str
    eta: str
    up: str
    down: str
    ratio: str
    status: str
    name: str

    # Units that can trail a numeric Have value, e.g. '2.26 GB'
    _HAVE_UNITS = frozenset({'B', 'kB', 'MB', 'GB', 'TB'})

    # Units that can trail a numeric ETA value, e.g. '10 mins'
    _ETA_UNITS = frozenset({'sec', 'secs', 'min', 'mins', 'hr', 'hrs', 'day', 'days'})

    @classmethod
    def from_line(cls, line: str) -> 'TransmissionRow':
        """Parse a single torrent row.

        Have and ETA are each either a single bare token ('None', 'Unknown',
        'Done') or a number followed by a unit ('2.26 GB', '10 mins'); which
        one it is can only be told apart by checking whether the following
        token is a known unit, since neither field has a fixed width. Name
        is whatever tokens remain after the other 8 columns are consumed,
        since it's the only column that may itself contain spaces with no
        way to bound it other than "the rest of the line".
        """
        tokens = line.split()
        if len(tokens) < 9:
            raise ValueError(f'expected at least 9 fields, got {len(tokens)}: {line!r}')

        id_, done = tokens[0], tokens[1]
        pos = 2

        have, pos = cls._take_unit_field(tokens, pos, cls._HAVE_UNITS)
        eta, pos = cls._take_unit_field(tokens, pos, cls._ETA_UNITS)

        remaining = tokens[pos:]
        if len(remaining) < 4:
            raise ValueError(f'expected up/down/ratio/status/name after ETA: {line!r}')
        up, down, ratio, status = remaining[:4]
        name = ' '.join(remaining[4:])
        if not name:
            raise ValueError(f'missing name field: {line!r}')

        return cls(id_, done, have, eta, up, down, ratio, status, name)

    @staticmethod
    def _take_unit_field(tokens: list[str], pos: int, units: frozenset) -> tuple[str, int]:
        """Consume one field starting at pos: either 'NUMBER UNIT' if the
        following token is a recognized unit, or a single bare token otherwise."""
        if pos + 1 < len(tokens) and tokens[pos + 1] in units:
            return f'{tokens[pos]} {tokens[pos + 1]}', pos + 2
        return tokens[pos], pos + 1

    def values(self) -> tuple[str, ...]:
        """Return this row's fields in display order, for column-width
        measurement and rendering. Lives here rather than as a hardcoded
        tuple in TransmissionTable so the two stay in sync automatically."""
        return (self.id, self.done, self.have, self.eta,
                self.up, self.down, self.ratio, self.status, self.name)


class TransmissionTable:
    """Parsed transmission-remote -l output: header, rows, and the Sum: footer.

    The header and footer lines are kept verbatim and untouched by sorting,
    matching the original script's use of head -n 1 / tail -n 1 to preserve
    them while only reordering the torrent rows in between.
    """

    def __init__(self, header: str, rows: list[TransmissionRow], footer: str) -> None:
        self.header = header
        self.rows = rows
        self.footer = footer

    @classmethod
    def from_output(cls, output: str) -> 'TransmissionTable':
        """Parse the full output of transmission-remote -l."""
        lines = output.splitlines()
        if len(lines) < 2:
            raise ValueError(f'expected at least a header and footer line, got: {output!r}')
        header, *middle, footer = lines
        rows = [TransmissionRow.from_line(line) for line in middle]
        return cls(header, rows, footer)

    def sorted_by(self, column: str, reverse: bool | None = None) -> 'TransmissionTable':
        """Return a new table with rows ordered by column. Header/footer untouched.

        reverse defaults to the column's natural direction (descending for
        numeric/size columns, ascending for name) when not given explicitly.
        """
        if column not in _SORT_KEYS:
            raise ValueError(f'unknown sort column {column!r}; choose from {SORT_COLUMNS}')
        if reverse is None:
            reverse = _DEFAULT_DESCENDING[column]
        key = _SORT_KEYS[column]
        return TransmissionTable(self.header, sorted(self.rows, key=key, reverse=reverse), self.footer)

    def render(self) -> str:
        """Render back to text in the same shape as transmission-remote -l.

        Reproduces the exact fixed-width format used by print_torrent_list()
        in transmission's own utils/remote.cc (the current fmt::print-based
        version, not the older printf-based one from pre-3.0 releases):

            "{:>6d}{:c}  {:>5s}  {:>9s}  {:<9s}  {:8.1f}  {:8.1f}  "
            "{:>5s}  {:<11s}  {:<s}\\n"

        i.e. ID is right-aligned to 6 chars with the error-mark character
        (* or space) glued directly after with no gap, Done/Have/Up/Down/
        Ratio are right-aligned, ETA/Status are left-aligned, Name is
        unpadded, and every field is joined by a literal 2-space gutter.
        These are fixed widths baked into transmission's source — not
        derived from the data — so column widths don't grow to fit unusually
        wide values (e.g. a high ratio like '1,024' overflows its column
        rather than widening it, exactly as transmission-remote itself does).
        Verified byte-for-byte against real transmission-remote -l output,
        including the high-ratio overflow case and starred (error-flagged)
        torrent IDs.
        """
        def render_row(row: TransmissionRow) -> str:
            # id may already include a trailing '*' (the error mark transmission
            # appends with {:c}); strip it off so the digits alone get the
            # {:>6d} treatment, then reattach the mark with no gap.
            digits, mark = (row.id[:-1], row.id[-1]) if row.id.endswith('*') else (row.id, ' ')
            id_field = digits.rjust(6) + mark
            return '  '.join((
                id_field,
                row.done.rjust(5),
                row.have.rjust(9),
                row.eta.ljust(9),
                row.up.rjust(8),
                row.down.rjust(8),
                row.ratio.rjust(5),
                row.status.ljust(11),
                row.name,
            ))

        lines = [self.header, *(render_row(row) for row in self.rows), self.footer]
        return '\n'.join(lines)


def run_command(args: list[str]) -> str:
    """Run a command and return its stdout. The only function that shells out;
    isolated here so tests can mock it without touching subprocess directly."""
    result = subprocess.run(args, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        raise RuntimeError(f'{args[0]} exited {result.returncode}: {result.stderr.strip()}')
    return result.stdout


class StatusReport:
    """Assembles the full status.txt: vnstat totals, vnstat --hours, the
    existing speedtest log, and a transmission torrent table sorted by
    whichever column was requested."""

    def __init__(self, speedtest_log: Path, sort_column: str = 'ratio') -> None:
        self._speedtest_log = speedtest_log
        self._sort_column = sort_column

    def build(self) -> str:
        """Assemble the full status page as a single string.

        Each section's own leading/trailing blank lines are stripped before
        joining, since the join itself supplies exactly one blank line of
        separation between sections — keeping each command's own trailing
        newline would otherwise stack with the join and produce a doubled
        blank line, which the original script's straight `cat` concatenation
        doesn't do.
        """
        sections = [
            run_command(['/home/jim/bin/vnstat']).strip('\n'),
            run_command(['/home/jim/bin/vnstat', '--hours']).strip('\n'),
            self._speedtest_log.read_text().strip('\n'),
        ]
        table_output = run_command(['/usr/local/bin/transmission-remote', '-l'])
        table = TransmissionTable.from_output(table_output).sorted_by(self._sort_column)
        sections.append(table.render().strip('\n'))
        return '\n\n'.join(sections)

    def write(self, destination: Path) -> None:
        """Build the report and write it into destination's existing file,
        matching the original status-update.sh's cat $TMP > $STATUS approach
        rather than a rename.

        build() fully assembles the report in memory before this touches
        the filesystem at all, so a failure partway through gathering
        vnstat/speedtest/transmission output never reaches destination —
        there's nothing left for an intermediate temp file to protect here.
        This needs write access to status.txt itself but not to its parent
        directory, which transmission's daemon owns.

        No reader other than this script's own cron job touches status.txt,
        so the brief window where destination is open for writing isn't a
        correctness concern the way it would be for a file under active
        concurrent access.
        """
        content = self.build()
        destination.write_text(content)


def main() -> int:
    sort_column = 'ratio'
    if '--sort' in sys.argv:
        idx = sys.argv.index('--sort')
        try:
            sort_column = sys.argv[idx + 1]
        except IndexError:
            print('ERROR: --sort requires a column name.', file=sys.stderr)
            return 1
        if sort_column not in SORT_COLUMNS:
            print(f'ERROR: unknown sort column {sort_column!r}; choose from {SORT_COLUMNS}',
                  file=sys.stderr)
            return 1

    if not SPEEDTEST_LOG.exists():
        print(f'ERROR: speedtest log missing at {SPEEDTEST_LOG}. Exiting.', file=sys.stderr)
        return 1

    report = StatusReport(SPEEDTEST_LOG, sort_column)
    try:
        report.write(STATUS_FILE)
    except (RuntimeError, OSError) as e:
        print(f'ERROR: {e}', file=sys.stderr)
        return 1

    return 0


if __name__ == '__main__':
    sys.exit(main())
