#!/usr/bin/env python3
"""Tests for status_update.py

Run from the repo root:
    python3 tests/test_status_update.py

Or with verbose output:
    python3 tests/test_status_update.py -v

No external dependencies required. The script under test is imported via
importlib, matching test_new_torrents.py's pattern, so this keeps working
even if status_update.py is ever renamed to something hyphenated.
Adjust SCRIPT_PATH below if you rename or move files.
"""

import importlib.util
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPT_PATH = Path(__file__).parent.parent / 'status_update.py'

spec = importlib.util.spec_from_file_location('status_update', SCRIPT_PATH)
su = importlib.util.module_from_spec(spec)
spec.loader.exec_module(su)

# A representative transmission-remote -l output, combining real-world
# column values seen in the wild: percentages, GB/MB/kB sizes, mins/hrs/days
# ETAs, None/Unknown placeholders, and a multi-word torrent name.
SAMPLE_OUTPUT = '\n'.join([
    'ID   Done   Have      ETA      Up   Down  Ratio  Status      Name',
    '1    100%   2.26 GB   Done     0.0  0.0   0.11   Idle        Arch Linux x86_64 ISO',
    '2    44%    1.54 GB   10 mins  0.0  0.0   0.00   Downloading Fedora Workstation 41',
    '3    0%     None      Unknown  0.0  0.0   None   Idle        '
    'Warp Records - Artificial Intelligence (The Series)',
    '4    100%   938.9 MB  Unknown  0.0  0.0   9.40   Idle        AlmaLinux-10.0-x86_64',
    '5    100%   4.12 GB   2 hrs    0.0  0.0   49.20  Seeding     ubuntu-24.04.1-desktop-amd64.iso',
    '6    n/a    None      Unknown  0.0  0.0   None   Idle        unresolved-magnet-link',
    'Sum:        9.0 GB              0.0  0.0',
])


class TestTransmissionRow(unittest.TestCase):

    def test_from_line_parses_all_fields(self) -> None:
        row = su.TransmissionRow.from_line(
            '1    100%   2.26 GB   Done     0.0  0.0   0.11   Idle        Arch Linux x86_64 ISO'
        )
        self.assertEqual(row.id, '1')
        self.assertEqual(row.done, '100%')
        self.assertEqual(row.have, '2.26 GB')
        self.assertEqual(row.eta, 'Done')
        self.assertEqual(row.up, '0.0')
        self.assertEqual(row.down, '0.0')
        self.assertEqual(row.ratio, '0.11')
        self.assertEqual(row.status, 'Idle')
        self.assertEqual(row.name, 'Arch Linux x86_64 ISO')

    def test_name_with_internal_spaces_stays_whole(self) -> None:
        row = su.TransmissionRow.from_line(
            '3    0%     None      Unknown  0.0  0.0   None   Idle        '
            'Warp Records - Artificial Intelligence (The Series)'
        )
        self.assertEqual(row.name, 'Warp Records - Artificial Intelligence (The Series)')

    def test_from_line_rejects_short_lines(self) -> None:
        with self.assertRaises(ValueError):
            su.TransmissionRow.from_line('1 100% 2.26 GB')

    def test_values_returns_fields_in_display_order(self) -> None:
        line = '4    100%   938.9 MB  Unknown  0.0  0.0   9.40   Idle        AlmaLinux-10.0-x86_64'
        row = su.TransmissionRow.from_line(line)
        self.assertEqual(
            row.values(),
            ('4', '100%', '938.9 MB', 'Unknown', '0.0', '0.0', '9.40', 'Idle', 'AlmaLinux-10.0-x86_64'),
        )


class TestSizeAndEtaKeys(unittest.TestCase):

    def test_size_key_parses_value_ignoring_unit(self) -> None:
        self.assertEqual(su._size_key('0.11'), 0.11)

    def test_size_key_converts_units_to_common_base(self) -> None:
        # 4.12 GB must outrank 938.9 MB once converted to a common base;
        # transmission-remote -l mixes units freely across rows, so a raw
        # numeric comparison of '4.12' vs '938.9' would get this backwards.
        self.assertGreater(su._size_key('4.12 GB'), su._size_key('938.9 MB'))
        self.assertGreater(su._size_key('938.9 MB'), su._size_key('500 kB'))
        self.assertGreater(su._size_key('1 TB'), su._size_key('999 GB'))

    def test_size_key_handles_none_as_lowest(self) -> None:
        self.assertEqual(su._size_key('None'), -1.0)

    def test_percent_key_parses_value(self) -> None:
        self.assertEqual(su._percent_key('44%'), 44.0)
        self.assertEqual(su._percent_key('100%'), 100.0)
        self.assertEqual(su._percent_key('0%'), 0.0)

    def test_percent_key_handles_na_as_lowest(self) -> None:
        # transmission-remote -l emits 'n/a' here for torrents with no
        # metadata yet (e.g. an unresolved magnet link)
        self.assertEqual(su._percent_key('n/a'), -1.0)

    def test_eta_key_converts_units_to_seconds(self) -> None:
        self.assertEqual(su._eta_key('10 mins'), 600.0)
        self.assertEqual(su._eta_key('2 hrs'), 7200.0)
        self.assertEqual(su._eta_key('1 days'), 86400.0)

    def test_eta_key_handles_unknown_and_done_as_lowest(self) -> None:
        self.assertEqual(su._eta_key('Unknown'), -1.0)
        self.assertEqual(su._eta_key('Done'), -1.0)


class TestTransmissionTable(unittest.TestCase):

    def test_from_output_separates_header_rows_footer(self) -> None:
        table = su.TransmissionTable.from_output(SAMPLE_OUTPUT)
        self.assertTrue(table.header.startswith('ID'))
        self.assertTrue(table.footer.startswith('Sum:'))
        self.assertEqual(len(table.rows), 6)

    def test_sorted_by_done_handles_percent_and_na(self) -> None:
        table = su.TransmissionTable.from_output(SAMPLE_OUTPUT)
        sorted_table = table.sorted_by('done')
        done_values = [row.done for row in sorted_table.rows]
        # 100%, 100%, 44%, 0%, then n/a last (unparsable, sorts to the bottom)
        self.assertEqual(done_values[-1], 'n/a')
        self.assertEqual(done_values[:2], ['100%', '100%'])

    def test_from_output_rejects_too_few_lines(self) -> None:
        with self.assertRaises(ValueError):
            su.TransmissionTable.from_output('only one line')

    def test_sorted_by_ratio_descending_matches_expected_order(self) -> None:
        table = su.TransmissionTable.from_output(SAMPLE_OUTPUT)
        sorted_table = table.sorted_by('ratio')
        ratios = [row.ratio for row in sorted_table.rows]
        # 49.20, 9.40, 0.11, 0.00, then the two unparsable 'None' rows last
        self.assertEqual(ratios[:4], ['49.20', '9.40', '0.11', '0.00'])
        self.assertEqual(set(ratios[4:]), {'None'})

    def test_sorted_by_name_is_case_insensitive_ascending(self) -> None:
        table = su.TransmissionTable.from_output(SAMPLE_OUTPUT)
        sorted_table = table.sorted_by('name', reverse=False)
        names = [row.name for row in sorted_table.rows]
        self.assertEqual(names, sorted(names, key=str.lower))

    def test_sorted_by_have_treats_embedded_unit_correctly(self) -> None:
        # 2.26 GB and 4.12 GB should both outrank 938.9 MB despite the
        # latter's larger raw number, since transmission-remote -l output
        # is unit-consistent per magnitude and we only need relative order
        # to match the original sed-based @ swap trick.
        table = su.TransmissionTable.from_output(SAMPLE_OUTPUT)
        sorted_table = table.sorted_by('have')
        haves = [row.have for row in sorted_table.rows]
        self.assertEqual(haves[0], '4.12 GB')

    def test_sorted_by_unknown_column_raises(self) -> None:
        table = su.TransmissionTable.from_output(SAMPLE_OUTPUT)
        with self.assertRaises(ValueError):
            table.sorted_by('bogus')

    def test_sorted_by_does_not_mutate_original(self) -> None:
        table = su.TransmissionTable.from_output(SAMPLE_OUTPUT)
        original_order = [row.id for row in table.rows]
        table.sorted_by('ratio')
        self.assertEqual([row.id for row in table.rows], original_order)

    def test_render_round_trips_header_and_footer(self) -> None:
        table = su.TransmissionTable.from_output(SAMPLE_OUTPUT)
        rendered = table.render()
        self.assertEqual(rendered.splitlines()[0], table.header)
        self.assertEqual(rendered.splitlines()[-1], table.footer)

    def test_render_right_aligns_numeric_columns(self) -> None:
        # Ratios '0.11' and '49.20' should right-align to the same width as
        # the widest ratio in the table, matching transmission-remote -l's
        # own column alignment. Restrict to rows whose ratio is a unique,
        # unambiguous substring of their line (some fixture rows reuse
        # 'None' in both Have and Ratio, which would match the wrong field).
        table = su.TransmissionTable.from_output(SAMPLE_OUTPUT)
        rendered = table.render()
        data_lines = rendered.splitlines()[1:-1]

        ratio_field_ends = set()
        for line, row in zip(data_lines, table.rows):
            if row.ratio == 'None':
                continue
            idx = line.index(row.ratio) + len(row.ratio)
            ratio_field_ends.add(idx)
        # All ratio values should end at the same column position
        self.assertEqual(len(ratio_field_ends), 1)

    def test_render_left_aligns_status_and_name(self) -> None:
        # Status values of different lengths ('Idle' vs 'Seeding') should
        # start at the same column position (left-aligned), unlike the
        # right-aligned numeric columns.
        table = su.TransmissionTable.from_output(SAMPLE_OUTPUT)
        rendered = table.render()
        data_lines = rendered.splitlines()[1:-1]

        status_field_starts = set()
        for line, row in zip(data_lines, table.rows):
            status_field_starts.add(line.index(row.status))
        self.assertEqual(len(status_field_starts), 1)

    def test_render_widens_column_to_fit_widest_value(self) -> None:
        # A long ratio like '1,024' should widen the Ratio column for every
        # row, not just its own — this is the exact case that caused the
        # highest-ratio torrent to visibly misalign in the original sed-based
        # script if ever pointed at a column wider than expected.
        wide_output = '\n'.join([
            'ID   Done   Have      ETA      Up   Down  Ratio  Status      Name',
            '1    100%   2.26 GB   Done     0.0  0.0   1,024  Idle        Arch Linux x86_64 ISO',
            '2    44%    1.54 GB   10 mins  0.0  0.0   0.00   Downloading Fedora Workstation 41',
        ])
        table = su.TransmissionTable.from_output(wide_output)
        rendered = table.render()
        data_lines = rendered.splitlines()[1:]

        # Both rows' ratio fields should end at the same position, even
        # though '1,024' (5 chars) is much wider than '0.00' (4 chars)
        end_positions = {
            line.index(row.ratio) + len(row.ratio)
            for line, row in zip(data_lines, table.rows)
        }
        self.assertEqual(len(end_positions), 1)

    def test_render_with_no_rows_omits_blank_table_body(self) -> None:
        table = su.TransmissionTable('ID   Done   Have', [], 'Sum:')
        rendered = table.render()
        self.assertEqual(rendered, 'ID   Done   Have\nSum:')


class TestStatusReport(unittest.TestCase):

    def setUp(self) -> None:
        self.speedtest_log = Path('/fake/speedtest.log')

    @patch.object(su, 'run_command')
    @patch('pathlib.Path.read_text')
    def test_build_assembles_sections_in_order(self, mock_read_text, mock_run) -> None:
        mock_read_text.return_value = 'Existing speedtest log\n'
        mock_run.side_effect = [
            'vnstat totals\n',
            'vnstat hourly\n',
            SAMPLE_OUTPUT,
        ]
        report = su.StatusReport(self.speedtest_log)
        content = report.build()

        self.assertIn('vnstat totals', content)
        self.assertIn('vnstat hourly', content)
        self.assertIn('Existing speedtest log', content)
        self.assertIn('Sum:', content)
        # vnstat totals should appear before vnstat hourly, which should
        # appear before the speedtest log, matching the shell script's order
        self.assertLess(content.index('vnstat totals'), content.index('vnstat hourly'))
        self.assertLess(content.index('vnstat hourly'), content.index('Existing speedtest log'))

    @patch.object(su, 'run_command')
    @patch('pathlib.Path.read_text')
    def test_build_sorts_table_by_requested_column(self, mock_read_text, mock_run) -> None:
        mock_read_text.return_value = 'log\n'
        mock_run.side_effect = ['', '', SAMPLE_OUTPUT]
        report = su.StatusReport(self.speedtest_log, sort_column='name')
        content = report.build()

        # First row's name in the rendered table should be alphabetically first
        table_lines = content.splitlines()
        sum_idx = next(i for i, line in enumerate(table_lines) if line.startswith('Sum:'))
        header_idx = next(i for i, line in enumerate(table_lines) if line.startswith('ID'))
        first_row = table_lines[header_idx + 1]
        self.assertIn('AlmaLinux-10.0-x86_64', first_row)
        self.assertLess(header_idx, sum_idx)


class TestStatusReportWrite(unittest.TestCase):
    """Separate case so we can use a real temp directory for atomic-write checks."""

    def setUp(self) -> None:
        import tempfile
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.tmp_path = Path(self._tmpdir.name)

        self.speedtest_log = self.tmp_path / 'speedtest.log'
        self.speedtest_log.write_text('Existing speedtest log\n')

    @patch.object(su, 'run_command')
    def test_write_creates_destination_file_with_full_content(self, mock_run) -> None:
        mock_run.side_effect = ['vnstat totals\n', 'vnstat hourly\n', SAMPLE_OUTPUT]
        destination = self.tmp_path / 'status.txt'

        report = su.StatusReport(self.speedtest_log)
        report.write(destination)

        self.assertTrue(destination.exists())
        content = destination.read_text()
        self.assertIn('vnstat totals', content)
        self.assertIn('Sum:', content)

    @patch.object(su, 'run_command')
    def test_write_does_not_require_parent_directory_write_access(self, mock_run) -> None:
        # Simulates the real failure this was built for: status.txt's
        # parent directory (owned by the transmission daemon) isn't
        # writable by whatever user runs this script, but the file itself
        # is. write() should only need permission on destination, not on
        # destination.parent, since it no longer creates a sibling temp
        # file there.
        mock_run.side_effect = ['', '', SAMPLE_OUTPUT]
        destination = self.tmp_path / 'status.txt'
        destination.write_text('placeholder')
        destination.chmod(0o644)
        self.tmp_path.chmod(0o555)  # read+execute only, no write
        self.addCleanup(self.tmp_path.chmod, 0o755)

        report = su.StatusReport(self.speedtest_log)
        report.write(destination)

        self.assertIn('Sum:', destination.read_text())

    @patch.object(su, 'run_command')
    def test_write_overwrites_existing_destination(self, mock_run) -> None:
        destination = self.tmp_path / 'status.txt'
        destination.write_text('stale content')
        mock_run.side_effect = ['', '', SAMPLE_OUTPUT]

        report = su.StatusReport(self.speedtest_log)
        report.write(destination)

        self.assertNotIn('stale content', destination.read_text())


if __name__ == '__main__':
    unittest.main()
