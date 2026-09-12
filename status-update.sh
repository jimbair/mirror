#!/bin/bash
# Update status.txt for our mirror
set -o pipefail

# Build in temp and install once done as we use status.txt for other cronjobs
TMP=$(/usr/bin/mktemp)
[[ -f "${TMP}" ]] || exit 1

# Clean-up our temp regardless of our exit condition
trap 'rm -f "${TMP}"' EXIT

# Bandwidth stats via vnstat
/home/jim/bin/vnstat >> ${TMP} || exit 1
/home/jim/bin/vnstat --hours >> ${TMP} || exit 1 

# IPv6 Traffic Stats
/home/jim/bin/ipv6-stats.py >> ${TMP} || exit 1

# Daily ookla speedtest
cat /home/jim/log/speedtest.log >> ${TMP} || exit 1 
echo >> ${TMP} || exit 1 # Formatting

# Let's sort by ratio; thanks Claude!
#
# head/tail preserve the header & footer; the middle section gets sorted by Ratio (desc).
#
# sed #1: merges ' <unit>' into '@<unit>' for Have (GB/MB/kB/B) and ETA (min/hr/day/sec)
# so word-splitting doesn't miscount fields. Singular unit roots also match plural forms
# as substrings, covering transmission-remote's inconsistent pluralization.
#
# awk: $7 is now reliably Ratio. High ratios print with a thousands comma (e.g. '2,328'),
# which breaks both sort -n and column alignment if stripped in place — so awk prepends a
# comma-free copy as a hidden tab-separated sort key instead of touching the real field.
#
# sort/cut: sort numerically on the hidden key, then drop it — original line untouched.
#
# sed #2: reverses the @ merge.
#
# NOTE: depends on sed #1 correctly collapsing ETA/Have so $7 is really Ratio. An unseen
# ETA unit would make awk silently key on the wrong field.
REMOTE=$(/usr/local/bin/transmission-remote -l)
[[ -n "${REMOTE}" ]] || exit 1
head -n 1 <<< ${REMOTE} >> ${TMP} || exit 1
sed '1d;$d' <<< ${REMOTE} \
  | sed -E 's/ (B|kB|MB|GB|TB|sec|min|hr|day|month|year)/@\1/g' \
  | awk '{key=$7; if (key=="Inf") key="999999999"; else gsub(",","",key); print key "\t" $0}' \
  | sort -t$'\t' -k1,1rn \
  | cut -f2- \
  | sed -E 's/@(B|kB|MB|GB|TB|sec|min|hr|day|month|year)/ \1/g' >> ${TMP} || exit 1
tail -n 1 <<< ${REMOTE} >> ${TMP} || exit 1

# Install the updated status
cat ${TMP} > /var/lib/transmission/Downloads/status.txt || exit 1 

# All done
exit 0
