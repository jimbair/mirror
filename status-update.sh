#!/bin/bash
# Update status.txt for our mirror
set -o pipefail

STATUS_FILE=/var/lib/transmission/Downloads/status.txt

build_status() {

    # Bandwidth stats via vnstat
    /home/jim/bin/vnstat || return 1
    /home/jim/bin/vnstat --hours || return 1

    # IPv6 Traffic Stats
    /home/jim/bin/ipv6-stats.py || return 1

    # Daily ookla speedtest
    cat /home/jim/log/speedtest.log || return 1
    echo || return 1 # Formatting

    # Let's sort by ratio; thanks Claude!
    #
    # head/tail preserve the header & footer; the middle section gets sorted by Ratio (desc).
    #
    # sed #1: merges ' <unit>' into '@<unit>' for Have (B/kB/MB/GB/TB) and ETA
    # (sec/min/hr/day/monthy/year) so word-splitting doesn't miscount fields. Singular unit
    # roots also match plural forms as substrings, covering transmission-remote's inconsistent
    # pluralization.
    #
    # awk: $7 is Ratio. High ratios print with a thousands comma (e.g. '2,328'), which breaks
    # both sort -n and column alignment if stripped in place. So, awk prepends a comma-free copy
    # as a hidden tab-separated sort key instead of touching the real field.
    #
    # sort/cut: sort numerically on the hidden key, then drop it.
    #
    # sed #2: reverses the @ merge.
    #
    # NOTE: depends on sed #1 correctly collapsing ETA/Have so $7 is really Ratio. An unseen
    # ETA unit would make awk silently key on the wrong field.
    local REMOTE
    REMOTE=$(/usr/local/bin/transmission-remote -l) || return 1
    [[ -n "${REMOTE}" ]] || return 1
    head -n 1 <<< "${REMOTE}" || return 1
    sed '1d;$d' <<< "${REMOTE}" \
      | sed -E 's/ (B|kB|MB|GB|TB|sec|min|hr|day|month|year)/@\1/g' \
      | awk '{key=$7; if (key=="Inf") key="999999999"; else gsub(",","",key); print key "\t" $0}' \
      | sort -t$'\t' -k1,1rn \
      | cut -f2- \
      | sed -E 's/@(B|kB|MB|GB|TB|sec|min|hr|day|month|year)/ \1/g' || return 1
    tail -n 1 <<< "${REMOTE}" || return 1
}

main() {
    # Readers must see either complete snapshot. A temp in /tmp followed by
    # cat > status.txt truncates the public file while it is being copied;
    # even mv can copy across filesystems. Stage beside the destination so
    # the final mv is a same-filesystem atomic rename.
    TMP=$(/usr/bin/mktemp "${STATUS_FILE}.tmp.XXXXXX") || return 1
    trap 'rm -f -- "${TMP}"' EXIT
    build_status > "${TMP}" || return 1
    # mktemp creates mode 0600; the web server must be able to read the
    # replacement just as it could read the old, publicly served snapshot.
    chmod 0644 "${TMP}" || return 1
    mv -f -- "${TMP}" "${STATUS_FILE}" || return 1
}

# Sourcing exposes the real publication path to tests without running the
# host-specific data collectors.
if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi
