#!/usr/bin/env python3
"""Exercise route selection and IPv6 accounting with synthetic /proc files."""

import contextlib
import importlib.util
import io
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parent.parent / 'ipv6-stats.py'
spec = importlib.util.spec_from_file_location('ipv6_stats', SCRIPT)
ipv6 = importlib.util.module_from_spec(spec)
# dataclasses resolves postponed annotations through the module registry.
sys.modules[spec.name] = ipv6
spec.loader.exec_module(ipv6)

ROUTE_HEADER = 'Iface Destination Gateway Flags RefCnt Use Metric Mask MTU Window IRTT\n'
# Addresses are documentation ranges; no production host data is needed.
DEFAULT_ROUTE = 'eth0 00000000 010200C0 0003 0 0 100 00000000 0 0 0\n'
DEV_HEADER = ('Inter-| Receive | Transmit\n'
              ' face |bytes packets errs drop fifo frame compressed multicast '
              '|bytes packets errs drop fifo colls carrier compressed\n')
BOOT_TIME = 1_700_000_000


class TestIPv6Stats(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='ipv6 stats test ')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.route = self.root / 'route'
        self.dev = self.root / 'dev'
        self.snmp = self.root / 'dev_snmp6'
        self.snmp.mkdir()
        self.stat = self.root / 'stat'
        self.stat.write_text(f'cpu 10 0 10 200 0 0 0 0 0 0\nbtime {BOOT_TIME}\n')
        self.route.write_text(ROUTE_HEADER + DEFAULT_ROUTE)
        self._write_dev(600, 400)
        self._write_ipv6(200, 100)
        self.enterContext(patch.multiple(
            ipv6, PROC_NET_ROUTE=self.route, PROC_NET_DEV=self.dev,
            PROC_NET_DEV_SNMP6=self.snmp, PROC_STAT=self.stat))

    def _write_dev(self, rx, tx):
        def row(interface, incoming, outgoing):
            fields = [incoming, 0, 0, 0, 0, 0, 0, 0,
                      outgoing, 0, 0, 0, 0, 0, 0, 0]
            return interface + ': ' + ' '.join(map(str, fields)) + '\n'
        self.dev.write_text(DEV_HEADER + row('eth1', 9000, 8000) + row('eth0', rx, tx))

    def _write_ipv6(self, rx, tx):
        (self.snmp / 'eth0').write_text(
            f'ifIndex 2\nIp6InReceives 10\nIp6InOctets {rx}\n'
            f'Ip6OutOctets {tx}\nIcmp6InMsgs 3\n')

    def _main(self, argv=None):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = ipv6.main([] if argv is None else argv)
        return result, stdout.getvalue(), stderr.getvalue()

    def test_selects_default_among_multiple_interfaces_and_connected_routes(self):
        self.route.write_text(
            ROUTE_HEADER
            + 'br0 006433C6 00000000 0001 0 0 0 00FFFFFF 0 0 0\n'
            + 'eth1 007100CB 00000000 0001 0 0 101 00FFFFFF 0 0 0\n'
            + DEFAULT_ROUTE
            + 'eth0 000200C0 00000000 0001 0 0 100 00FFFFFF 0 0 0\n')
        self.assertEqual(ipv6.default_interface(), 'eth0')

    def test_zero_destination_with_nonzero_mask_is_not_a_default(self):
        self.route.write_text(ROUTE_HEADER
            + 'tun0 00000000 00000000 0001 0 0 0 00000080 0 0 0\n'
            + DEFAULT_ROUTE)
        self.assertEqual(ipv6.default_interface(), 'eth0')

    def test_ignores_down_reject_and_invalid_route_flags(self):
        for flags in ('0000', '0002', '0201', '0203', 'invalid'):
            with self.subTest(flags=flags):
                self.route.write_text(ROUTE_HEADER
                    + f'eth1 00000000 010200C0 {flags} 0 0 0 00000000 0 0 0\n'
                    + DEFAULT_ROUTE)
                self.assertEqual(ipv6.default_interface(), 'eth0')

    def test_on_link_default_does_not_require_the_gateway_flag(self):
        self.route.write_text(ROUTE_HEADER
            + 'eth0 00000000 00000000 0001 0 0 0 00000000 0 0 0\n')
        self.assertEqual(ipv6.default_interface(), 'eth0')

    def test_short_route_rows_are_ignored(self):
        self.route.write_text(ROUTE_HEADER + 'broken 00000000\n' + DEFAULT_ROUTE)
        self.assertEqual(ipv6.default_interface(), 'eth0')

    def test_missing_usable_default_fails_without_publishing_stats(self):
        self.route.write_text(ROUTE_HEADER
            + 'tun0 00000000 00000000 0001 0 0 0 00000080 0 0 0\n')
        result, stdout, stderr = self._main()
        self.assertEqual(result, 1)
        self.assertEqual(stdout, '')
        self.assertIn('no usable IPv4 default route', stderr)

    def test_gather_sums_both_directions_for_the_selected_interface(self):
        share = ipv6.gather('eth0')
        self.assertEqual(share.interface, 'eth0')
        self.assertEqual(share.total_bytes, 1000)
        self.assertEqual(share.ipv6_bytes, 300)
        self.assertEqual(share.non_ipv6_bytes, 700)
        self.assertEqual(share.ipv6_pct, 30.0)
        self.assertEqual(share.since, datetime.fromtimestamp(BOOT_TIME))

    def test_traffic_between_reads_does_not_use_an_older_total_for_newer_ipv6(self):
        self._write_dev(1000, 0)
        self._write_ipv6(800, 0)
        real_total = ipv6.total_bytes_for

        def advance_after_total(interface):
            total = real_total(interface)
            self._write_dev(1500, 0)
            self._write_ipv6(1200, 0)
            return total

        # The true share is 80% before and after the traffic. Reading total
        # first used to combine 1000 total with 1200 IPv6 and report 120%.
        with patch.object(ipv6, 'total_bytes_for', side_effect=advance_after_total):
            share = ipv6.gather('eth0')
        self.assertEqual(share.ipv6_pct, 80.0)
        self.assertEqual(share.total_bytes, 1000)
        self.assertEqual(share.ipv6_bytes, 800)

    def test_inconsistent_or_negative_samples_fail_cleanly(self):
        for total, subtotal in ((100, 120), (0, 1), (-1, 0), (100, -1)):
            with self.subTest(total=total, ipv6=subtotal):
                self._write_dev(total, 0)
                self._write_ipv6(subtotal, 0)
                result, stdout, stderr = self._main()
                self.assertEqual(result, 1)
                self.assertEqual(stdout, '')
                self.assertIn('inconsistent traffic counters', stderr)
                self.assertIn("'eth0'", stderr)
                self.assertNotIn('Traceback', stderr)

    def test_zero_traffic_is_valid(self):
        self._write_dev(0, 0)
        self._write_ipv6(0, 0)
        result, stdout, stderr = self._main()
        self.assertEqual(result, 0, stderr)
        self.assertIn('0.0% of traffic on eth0', stdout)
        self.assertEqual(stderr, '')

    def test_zero_and_full_ipv6_shares_are_valid(self):
        self._write_dev(1000, 0)
        for subtotal, percentage in ((0, '0.0%'), (1000, '100.0%')):
            with self.subTest(ipv6=subtotal):
                self._write_ipv6(subtotal, 0)
                result, stdout, stderr = self._main()
                self.assertEqual(result, 0, stderr)
                self.assertIn(percentage + ' of traffic on eth0', stdout)

    def test_verbose_labels_the_remainder_as_non_ipv6(self):
        result, stdout, stderr = self._main(['--verbose'])
        self.assertEqual(result, 0, stderr)
        self.assertIn('non-IPv6: 700.0 B', stdout)
        self.assertIn('ipv6:     300.0 B', stdout)
        self.assertIn('total:    1000.0 B', stdout)
        self.assertNotIn('ipv4:', stdout)

    def test_normal_output_keeps_the_existing_summary_format(self):
        result, stdout, stderr = self._main()
        self.assertEqual(result, 0, stderr)
        since = datetime.fromtimestamp(BOOT_TIME).strftime('%Y-%m-%d %H:%M')
        self.assertEqual(stdout,
            f'       30.0% of traffic on eth0 has been IPv6 since {since}\n')
        self.assertEqual(stderr, '')

    def test_explicit_interface_works_without_an_ipv4_route_table(self):
        self.route.unlink()
        result, stdout, stderr = self._main(['--interface', 'eth0'])
        self.assertEqual(result, 0, stderr)
        self.assertIn('30.0% of traffic on eth0', stdout)

    def test_missing_counter_files_fail_cleanly(self):
        for path in (self.dev, self.snmp / 'eth0', self.stat):
            with self.subTest(path=path.name):
                original = path.read_text()
                path.unlink()
                try:
                    result, stdout, stderr = self._main()
                    self.assertEqual(result, 1)
                    self.assertEqual(stdout, '')
                    self.assertTrue(stderr.startswith('error: '))
                    self.assertNotIn('Traceback', stderr)
                finally:
                    path.write_text(original)

    def test_missing_ipv6_octet_counter_fails_cleanly(self):
        (self.snmp / 'eth0').write_text('Ip6InOctets 200\n')
        result, stdout, stderr = self._main()
        self.assertEqual(result, 1)
        self.assertEqual(stdout, '')
        self.assertIn('Ip6OutOctets', stderr)

    def test_help_skips_counter_and_route_reads(self):
        with patch.object(ipv6, 'gather') as gather, \
             patch.object(ipv6, 'default_interface') as default, \
             contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as cm:
            ipv6.main(['--help'])
        self.assertEqual(cm.exception.code, 0)
        gather.assert_not_called()
        default.assert_not_called()


if __name__ == '__main__':
    unittest.main()
