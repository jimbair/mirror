#!/usr/bin/env python3
"""Block selected Transmission peers using their client name or peer ID.

Transmission 4.1+ (JSON-RPC 2.0); Python 3.9+; standard library only.
Applies bans by default; use --dry-run to preview without changing anything.
A normal run temporarily serves a PeerGuardian blocklist and tells Transmission
to download/reload it without restarting the daemon.

By default, match aria2/ client names and -GT0003- / -PC0200- peer IDs.
Use --no-block-gt0003 or --no-block-pc0200 to disable either ID match.
Additional client-name and peer-ID prefixes can be added in
~/.config/transmission-client-ban/signatures.txt, one per line, e.g.:
    client:AnotherDownloader/
    peer-id:-XX1234-
The file is optional and reloaded on every invocation. Peer-ID matches also
accept the same signature at the start of Transmission's displayed client name
when a decoded peer ID is unavailable. Identifiers are self-reported.

Every matching peer IP is retained in blocked-ips.txt as an observation.
After observing 5 distinct IPv6 addresses in one /64 (configurable), the entire
/64 is blocked instead of its individual addresses. blocked-rules.txt records
the last successfully applied effective rules using readable CIDR notation.
The PeerGuardian data sent to Transmission uses start-end ranges, not IPv6 CIDR.
Previously saved bans remain in place even if a matching option is disabled.

Run on the same host/network namespace as transmission-daemon by default.
Client names and peer IDs are self-reported and may be spoofed.
"""

import argparse
import base64
import binascii
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import os
from pathlib import Path
import sys
import threading
from urllib import error, request

DEFAULT_BLOCKLIST_URLS = {"", "https://www.example.com/blocklist", "http://www.example.com/blocklist"}
DEFAULT_SIGNATURES_FILE = Path("~/.config/transmission-client-ban/signatures.txt").expanduser()
DEFAULT_PEER_ID_PREFIXES = ("-GT0003-", "-PC0200-")


class RPC:
    def __init__(self, url, username, password):
        self.url = url
        self.session_id = None
        self.auth = None
        if username:
            token = base64.b64encode(f"{username}:{password}".encode()).decode()
            self.auth = "Basic " + token

    def call(self, method, params=None):
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method,
                           "params": params if params is not None else {}}).encode()
        for _ in range(3):
            headers = {"Content-Type": "application/json"}
            if self.auth:
                headers["Authorization"] = self.auth
            if self.session_id:
                headers["X-Transmission-Session-Id"] = self.session_id
            req = request.Request(self.url, data=body, headers=headers, method="POST")
            try:
                with request.urlopen(req, timeout=30) as response:
                    result = json.load(response)
                if "error" in result:
                    raise RuntimeError(f"Transmission RPC {method}: {result['error']}")
                if "result" not in result:
                    raise RuntimeError(f"Transmission RPC {method} returned no result")
                return result["result"]
            except error.HTTPError as exc:
                if exc.code == 409:
                    self.session_id = exc.headers.get("X-Transmission-Session-Id")
                    if self.session_id:
                        continue
                raise RuntimeError(f"Transmission RPC HTTP {exc.code}: {exc.reason}") from exc
        raise RuntimeError("Could not negotiate Transmission RPC session ID")


def address_sort_key(value):
    addr = ipaddress.ip_address(value)
    return addr.version, int(addr)


def rule_sort_key(rule):
    network = ipaddress.ip_network(rule, strict=False)
    return network.version, int(network.network_address), network.prefixlen


def read_addresses(path):
    if not path.exists():
        return set()
    addresses = set()
    for num, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line:
            continue
        try:
            addresses.add(str(ipaddress.ip_address(line)))
        except ValueError as exc:
            raise ValueError(f"Invalid address in {path}, line {num}: {line}") from exc
    return addresses


def read_rules(path):
    """Read the record of the last successfully applied effective blocklist."""
    if not path.exists():
        return None  # Previous version didn't keep this record: force one reload.
    rules = set()
    for num, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            network = ipaddress.ip_network(line, strict=False)
        except ValueError as exc:
            raise ValueError(f"Invalid rule in {path}, line {num}: {line}") from exc
        rule = (str(network.network_address) if network.prefixlen == network.max_prefixlen
                else str(network))
        rules.add(rule)
    return rules


def write_lines(path, lines):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    try:
        with temp.open("w", encoding="utf-8") as out:
            os.chmod(temp, 0o600)
            for line in lines:
                out.write(line + "\n")
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def build_rules(addresses, ipv6_threshold):
    """Return effective rule strings, promoted /64 -> observed-address count."""
    ipv6_groups = defaultdict(set)
    for raw in addresses:
        addr = ipaddress.ip_address(raw)
        if addr.version == 6:
            ipv6_groups[ipaddress.ip_network(f"{addr}/64", strict=False)].add(str(addr))

    promoted = {
        network: len(ips)
        for network, ips in ipv6_groups.items()
        if ipv6_threshold > 0 and len(ips) >= ipv6_threshold
    }
    rules = {str(network) for network in promoted}
    for raw in addresses:
        addr = ipaddress.ip_address(raw)
        if addr.version == 4 or ipaddress.ip_network(f"{addr}/64", strict=False) not in promoted:
            rules.add(str(addr))
    return rules, promoted


def read_signatures(path):
    """Read optional additional client and peer-ID prefixes; fail on typos."""
    if not path.exists():
        return set(), set()
    client_prefixes, peer_id_prefixes = set(), set()
    for num, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        kind, separator, value = line.partition(":")
        value = value.strip()
        if not separator or not value:
            raise ValueError(f"Invalid signature in {path}, line {num}: {line!r}")
        if kind == "client":
            client_prefixes.add(value)
        elif kind == "peer-id":
            peer_id_prefixes.add(value)
        else:
            raise ValueError(f"Unknown signature type in {path}, line {num}: {kind!r}")
    return client_prefixes, peer_id_prefixes


def validate_peer_id_prefix(value):
    """Peer IDs are 20 bytes; do not allow dangerously broad match strings."""
    try:
        encoded = value.encode("ascii")
    except UnicodeEncodeError as exc:
        raise ValueError(f"Peer-ID prefix must be ASCII: {value!r}") from exc
    if not 4 <= len(encoded) <= 20:
        raise ValueError(f"Peer-ID prefix must be 4-20 bytes: {value!r}")
    return encoded


def matching_reasons(peer, client_prefixes, peer_id_prefixes):
    """Match configurable prefixes; tolerate absent/malformed Base64 peer IDs."""
    reasons = set()
    name = peer.get("client_name") or ""
    if isinstance(name, str):
        for prefix in client_prefixes:
            if name.lower().startswith(prefix.lower()):
                reasons.add(f"client: {name}")
        # Some clients expose their peer-ID signature as their client name.
        for prefix in peer_id_prefixes:
            if name.startswith(prefix.decode("ascii")):
                reasons.add(f"client ID: {prefix.decode('ascii')}")

    encoded = peer.get("peer_id")
    if isinstance(encoded, str):
        try:
            peer_id = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error):
            pass
        else:
            if len(peer_id) == 20:
                for prefix in peer_id_prefixes:
                    if peer_id.startswith(prefix):
                        reasons.add(f"peer ID: {prefix.decode('ascii')}")
    return reasons


def pg_payload(rules):
    # PeerGuardian labels do not need to be unique. The persistent IP log does
    # not distinguish reasons, so a generic label avoids claiming every rule
    # came from aria2. Transmission accepts IPv6 start-end ranges, not CIDR.
    lines = []
    for rule in sorted(rules, key=rule_sort_key):
        network = ipaddress.ip_network(rule, strict=False)
        lines.append(f"peer-ban:{network.network_address}-{network.broadcast_address}\n")
    return "".join(lines).encode("ascii")


def reload_blocklist(rpc, payload, bind_address, advertise_host, port, allow_existing_url):
    session = rpc.call("session_get", {"fields": ["blocklist_url", "blocklist_enabled"]})
    original_url = session.get("blocklist_url", "")
    if original_url not in DEFAULT_BLOCKLIST_URLS and not allow_existing_url:
        raise RuntimeError(
            "Transmission has a custom blocklist URL. Updating would replace its downloaded "
            "list. Refusing; use --allow-existing-url only if you accept that tradeoff."
        )

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path != "/blocklist":
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=ascii")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer((bind_address, port), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host = f"[{advertise_host}]" if ":" in advertise_host else advertise_host
    url = f"http://{host}:{server.server_port}/blocklist"
    changed = False
    try:
        rpc.call("session_set", {"blocklist_url": url})
        changed = True
        result = rpc.call("blocklist_update")
        count = result.get("blocklist_size", 0)
        if not isinstance(count, int) or count <= 0:
            raise RuntimeError(f"Blocklist update returned no usable rules: {result}")
        rpc.call("session_set", {"blocklist_enabled": True})
        return count
    finally:
        if changed:
            try:
                rpc.call("session_set", {"blocklist_url": original_url})
            except Exception as exc:
                print(f"WARNING: Could not restore original blocklist URL: {exc}", file=sys.stderr)
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--rpc-url", default=os.getenv("TRANSMISSION_RPC_URL", "http://127.0.0.1:9091/transmission/rpc"))
    parser.add_argument("--username", default=os.getenv("TRANSMISSION_RPC_USER", ""))
    parser.add_argument("--password", default=os.getenv("TRANSMISSION_RPC_PASSWORD", ""),
                        help="Prefer the TRANSMISSION_RPC_PASSWORD environment variable")
    parser.add_argument("--client-prefix", default="aria2/", help="Case-insensitive prefix (default: aria2/)")
    parser.add_argument("--no-block-gt0003", action="store_false", dest="block_gt0003", default=True,
                        help="Do not match -GT0003- (enabled by default)")
    parser.add_argument("--no-block-pc0200", action="store_false", dest="block_pc0200", default=True,
                        help="Do not match -PC0200- (enabled by default)")
    parser.add_argument("--signatures-file", type=Path, default=DEFAULT_SIGNATURES_FILE,
                        help="Optional text file with client: and peer-id: prefix lines")
    parser.add_argument("--state-file", type=Path,
                        default=Path("~/.local/state/transmission-client-ban/blocked-ips.txt").expanduser())
    parser.add_argument("--ipv6-threshold", type=int, default=5,
                        help="Distinct observed IPv6 addresses needed to block their /64 (default: 5; 0 disables)")
    parser.add_argument("--dry-run", action="store_true", help="Preview matches and rules without changing anything")
    parser.add_argument("--apply", action="store_true", help=argparse.SUPPRESS)  # Legacy no-op for existing cron jobs.
    parser.add_argument("--refresh", action="store_true", help="Reapply saved bans even without rule changes")
    parser.add_argument("--bind-address", default="127.0.0.1", help="HTTP listener bind address")
    parser.add_argument("--advertise-host", default="127.0.0.1", help="Address Transmission can reach (same namespace by default)")
    parser.add_argument("--port", type=int, default=0, help="Temporary HTTP port (0 = random)")
    parser.add_argument("--allow-existing-url", action="store_true",
                        help="Allow replacing a downloaded custom Transmission blocklist")
    args = parser.parse_args()
    if args.ipv6_threshold < 0:
        parser.error("--ipv6-threshold cannot be negative")

    # Preserve the original per-IP observation log, and separately remember the
    # effective rules last applied. This permits future threshold changes and
    # avoids unnecessary reloads when a new IP is already inside a banned /64.
    rules_file = args.state_file.with_name("blocked-rules.txt")
    extra_clients, extra_peers = read_signatures(args.signatures_file.expanduser())
    if not args.client_prefix:
        parser.error("--client-prefix cannot be empty")
    client_prefixes = {args.client_prefix, *extra_clients}
    peer_id_strings = set(DEFAULT_PEER_ID_PREFIXES) | extra_peers
    if not args.block_gt0003:
        peer_id_strings.discard("-GT0003-")
    if not args.block_pc0200:
        peer_id_strings.discard("-PC0200-")
    peer_id_prefixes = {validate_peer_id_prefix(value) for value in peer_id_strings}
    rpc = RPC(args.rpc_url, args.username, args.password)
    info = rpc.call("session_get", {"fields": ["version", "rpc_version"]})
    if info.get("rpc_version", 0) < 18:
        raise RuntimeError("This script requires Transmission 4.1+ JSON-RPC 2.0")

    torrents = rpc.call("torrent_get", {"fields": ["id", "name", "peers"]}).get("torrents", [])
    observed = {}
    reasons_by_ip = defaultdict(set)
    for torrent in torrents:
        for peer in torrent.get("peers", []):
            reasons = matching_reasons(peer, client_prefixes, peer_id_prefixes)
            if not reasons:
                continue
            try:
                ip = str(ipaddress.ip_address(peer["address"]))
            except (KeyError, ValueError):
                continue
            observed.setdefault(ip, set()).add(torrent.get("name", "?"))
            reasons_by_ip[ip].update(reasons)

    previous = read_addresses(args.state_file)
    newly_found = set(observed) - previous
    total = previous | set(observed)
    rules, promoted = build_rules(total, args.ipv6_threshold)
    applied_rules = read_rules(rules_file)
    promoted_before = {str(ipaddress.ip_network(rule)) for rule in applied_rules or set() if "/64" in rule and ":" in rule}

    print(f"Transmission: {info.get('version', '?')}")
    print(f"Client filters: {', '.join(sorted(client_prefixes)) or 'none'}; "
          f"peer IDs: {', '.join(sorted(peer_id_strings)) or 'none'}")
    print(f"Scanned {len(torrents)} torrents; found {len(observed)} matching IPs "
          f"across {sum(map(len, observed.values()))} torrent/peer combinations")
    print(f"Previously saved: {len(previous)}; new: {len(newly_found)}; total: {len(total)}")
    for ip in sorted(newly_found, key=address_sort_key)[:25]:
        names = sorted(observed[ip])
        print(f"  NEW {ip}  ({len(names)} torrent(s); {', '.join(sorted(reasons_by_ip[ip]))})")
        for name in names:
            print(f"      - {name}")
    if len(newly_found) > 25:
        print(f"  ...and {len(newly_found) - 25} more")
    print(f"Effective blocklist: {len(rules)} rules, including {len(promoted)} IPv6 /64(s) "
          f"(threshold: {args.ipv6_threshold if args.ipv6_threshold else 'disabled'})")
    for network, count in sorted(promoted.items(), key=lambda item: int(item[0].network_address)):
        if str(network) not in promoted_before:
            print(f"  PROMOTE {network}  ({count} observed addresses)")

    if args.dry_run:
        print("DRY RUN: Nothing changed. Omit --dry-run to save and apply the blocklist.")
        return
    if not total:
        print("No peers to ban; leaving Transmission unchanged.")
        return
    if applied_rules == rules and not args.refresh:
        if newly_found:
            write_lines(args.state_file, sorted(total, key=address_sort_key))
            print(f"Saved {len(newly_found)} new observed addresses; no change to the effective "
                  "blocklist, so no reload was needed.")
        else:
            print("No rule changes; nothing to reload. Use --refresh to reapply saved bans.")
        return

    count = reload_blocklist(rpc, pg_payload(rules), args.bind_address,
                             args.advertise_host, args.port, args.allow_existing_url)
    write_lines(args.state_file, sorted(total, key=address_sort_key))
    write_lines(rules_file, sorted(rules, key=rule_sort_key))
    print(f"Applied Transmission blocklist ({count} parsed ranges); saved {len(total)} observed "
          f"addresses to {args.state_file} and {len(rules)} rules to {rules_file}")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, RuntimeError, error.URLError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
