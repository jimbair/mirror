#!/bin/bash
# Update the header/footer nginx wraps around its native autoindex listing
set -o pipefail

WEBROOT=/var/lib/transmission/Downloads
TRANSMISSION_REMOTE=/usr/local/bin/transmission-remote
SPEEDTEST_LOG=/home/jim/log/speedtest.log
VNSTAT=/home/jim/bin/vnstat
IPV6_STATS=/home/jim/bin/ipv6-stats.py

html_escape() {
  sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g'
}

build_header() {
  local TORRENT_COUNT TOTAL_SIZE UPDATED
  TORRENT_COUNT=$(sed '1d;$d' <<< "${REMOTE}" | wc -l | html_escape) || return 1
  TOTAL_SIZE=$(tail -n 1 <<< "${REMOTE}" | awk '{print $2, $3}' | html_escape) || return 1
  UPDATED=$(date '+%Y-%m-%d %H:%M %Z' | html_escape) || return 1

  echo '<h1>mirror.tsue.net</h1>' || return 1
  echo '<p>Public mirror of various Linux distributions.</p>' || return 1
  printf '<p>Currently seeding %s torrents, %s total &middot; updated %s</p>\n' \
    "${TORRENT_COUNT}" "${TOTAL_SIZE}" "${UPDATED}" || return 1
  echo '<p><a href="https://github.com/jimbair/mirror">The GitHub Repo</a> that I use to monitor/sync updates.</p>' || return 1
}

build_footer() {
  echo '<h2>Bandwidth</h2><pre>' || return 1
  html_escape < "${SPEEDTEST_LOG}" || return 1
  echo '</pre>' || return 1

  echo '<h2>Transit Stats</h2><pre>' || return 1
  "${VNSTAT}" | html_escape || return 1
  #"${VNSTAT}" --hours | html_escape || return 1
  echo "" || return 1
  "${IPV6_STATS}" | html_escape || return 1
  echo '</pre>' || return 1

  echo '<h2>Torrent Stats</h2><pre>' || return 1
  head -n 1 <<< "${REMOTE}" | html_escape || return 1
  # Match status-update.sh's ratio ordering, including comma-separated and
  # infinite ratios. Collapse Have/ETA units before locating the ratio field.
  sed '1d;$d' <<< "${REMOTE}" \
    | sed -E 's/ (B|kB|MB|GB|TB|sec|min|hr|day|month|year)/@\1/g' \
    | awk '{key=$7; if (key=="Inf") key="999999999"; else gsub(",","",key); print key "\t" $0}' \
    | sort -t$'\t' -k1,1rn \
    | cut -f2- \
    | sed -E 's/@(B|kB|MB|GB|TB|sec|min|hr|day|month|year)/ \1/g' \
    | html_escape || return 1
  tail -n 1 <<< "${REMOTE}" | html_escape || return 1
  echo '</pre>' || return 1
}

main() {
  local REMOTE
  REMOTE=$("${TRANSMISSION_REMOTE}" -l) || return 1
  [[ -n "${REMOTE}" ]] || return 1

  # Build both fragments before publishing either. Staging beside the public
  # files makes each rename atomic; copying from /tmp can expose empty or
  # partial HTML to nginx readers. Install cleanup before either allocation
  # so a failure creating the second temp cannot leak the first.
  TMPH= TMPF=
  trap 'rm -f -- "${TMPH}" "${TMPF}"' EXIT
  TMPH=$(/usr/bin/mktemp "${WEBROOT}/.theme/header.html.tmp.XXXXXX") || return 1
  TMPF=$(/usr/bin/mktemp "${WEBROOT}/.theme/footer.html.tmp.XXXXXX") || return 1
  build_header > "${TMPH}" || return 1
  build_footer > "${TMPF}" || return 1
  # mktemp uses 0600; nginx needs to read the published replacements.
  chmod 0644 "${TMPH}" "${TMPF}" || return 1
  mv -f -- "${TMPH}" "${WEBROOT}/.theme/header.html" || return 1
  mv -f -- "${TMPF}" "${WEBROOT}/.theme/footer.html" || return 1
}

# Sourcing exposes the real build/publication path to tests with stubbed host
# collectors, without accessing production data or changing status.txt.
if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi
