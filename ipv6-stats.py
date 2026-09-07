#!/usr/bin/env python3
"""
ipv6_share.py -- report the percentage of interface traffic that has been
IPv6 since the machine's last boot.

Data sources (both are LIVE KERNEL COUNTERS):
  - /proc/net/dev             total rx+tx bytes per interface, since boot
  - /proc/net/dev_snmp6/<if>  IPv6-only rx+tx byte counters, since boot

Both reset to zero on reboot, so the reported window is always
"since last boot". If you need a longer window, these counters have to be
polled and persisted over time yourself.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

PROC_NET_DEV = Path("/proc/net/dev")
PROC_NET_ROUTE = Path("/proc/net/route")
PROC_NET_DEV_SNMP6 = Path("/proc/net/dev_snmp6")
PROC_STAT = Path("/proc/stat")


@dataclass
class TrafficShare:
    interface: str
    total_bytes: int
    ipv6_bytes: int
    since: datetime

    @property
    def ipv4_bytes(self) -> int:
        return max(self.total_bytes - self.ipv6_bytes, 0)

    @property
    def ipv6_pct(self) -> float:
        if self.total_bytes == 0:
            return 0.0
        return (self.ipv6_bytes / self.total_bytes) * 100


def default_interface() -> str:
    """Return the interface used for the IPv4 default route."""
    for line in PROC_NET_ROUTE.read_text().splitlines()[1:]:
        fields = line.split()
        if len(fields) >= 2 and fields[1] == "00000000":
            return fields[0]
    raise RuntimeError("no default route found in /proc/net/route")


def total_bytes_for(interface: str) -> int:
    """rx_bytes + tx_bytes for `interface` from /proc/net/dev."""
    for line in PROC_NET_DEV.read_text().splitlines():
        if ":" not in line:
            continue
        name, rest = line.split(":", 1)
        if name.strip() != interface:
            continue
        fields = rest.split()
        rx_bytes = int(fields[0])
        tx_bytes = int(fields[8])
        return rx_bytes + tx_bytes
    raise RuntimeError(f"interface {interface!r} not found in /proc/net/dev")


def ipv6_bytes_for(interface: str) -> int:
    """Ip6InOctets + Ip6OutOctets for `interface` from dev_snmp6."""
    path = PROC_NET_DEV_SNMP6 / interface
    if not path.exists():
        raise RuntimeError(
            f"{path} does not exist -- kernel/interface may lack "
            "per-interface IPv6 SNMP stats (needs CONFIG_IPV6 and an "
            "IPv6 address on the interface)"
        )
    counters: dict[str, int] = {}
    for line in path.read_text().splitlines():
        key, value = line.split()
        if key and value:
            counters[key] = int(value)
    try:
        return counters["Ip6InOctets"] + counters["Ip6OutOctets"]
    except KeyError as exc:
        raise RuntimeError(f"missing counter {exc} in {path}") from exc


def boot_time() -> datetime:
    for line in PROC_STAT.read_text().splitlines():
        if line.startswith("btime "):
            return datetime.fromtimestamp(int(line.split()[1]))
    raise RuntimeError("btime not found in /proc/stat")


def gather(interface: str) -> TrafficShare:
    return TrafficShare(
        interface=interface,
        total_bytes=total_bytes_for(interface),
        ipv6_bytes=ipv6_bytes_for(interface),
        since=boot_time(),
    )


def format_bytes(n: int) -> str:
    value = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024:
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} PiB"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "-i", "--interface",
        help="interface to report on (default: default-route interface)",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true",
        help="also print raw byte counts",
    )
    args = parser.parse_args()

    try:
        interface = args.interface or default_interface()
        share = gather(interface)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    since_str = share.since.strftime("%Y-%m-%d %H:%M")
    print(
        f"       {share.ipv6_pct:.1f}% of traffic on "
        f"{interface} has been IPv6 since {since_str}"
    )

    if args.verbose:
        print(f"  ipv4:  {format_bytes(share.ipv4_bytes)}")
        print(f"  ipv6:  {format_bytes(share.ipv6_bytes)}")
        print(f"  total: {format_bytes(share.total_bytes)}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
