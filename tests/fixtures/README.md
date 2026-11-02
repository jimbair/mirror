# Upstream parser fixtures

These are small extracts from public responses fetched on 2026-11-01.
They retain representative upstream markup and filenames rather than
reconstructing the parser's expected shape. Unrelated rows and JSON fields
are omitted; these are test inputs, not complete release inventories.

| Fixture | Source |
| --- | --- |
| `mint-supported.html` | https://linuxmint.com/download_all.php |
| `ubuntu-schedule.html` | https://ubuntu.com/project/docs/release-team/list-of-releases/ |
| `fedora-torrents.json` | https://torrent.fedoraproject.org/torrents.json |
| `proxmox-downloads.html` | https://www.proxmox.com/en/downloads/proxmox-virtual-environment |
| `arch-download.html` | https://archlinux.org/download/ |
| `cachyos-torrent-props.txt` | https://cachyos.org/download/ |

Mint includes bare-major versions, multiple editions, and the LMDE row.
Ubuntu retains all four support tables and a historical point-release row
whose blank dates rely on another row in the same release line. Fedora
includes stable releases alongside a beta. Proxmox includes ARM64 and
x86_64 downloads. CachyOS preserves HTML-entity-encoded Astro properties.

Tests pad short extracts past the generic response-length check. Keep CI's
`tests/fixtures/**` path filter when adding or refreshing fixture files.
