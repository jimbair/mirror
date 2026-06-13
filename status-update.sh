#!/bin/bash
# Update status.txt for our mirror

# Build in temp and install once done as we use status.txt for other cronjobs
TMP=$(/usr/bin/mktemp)
[[ -f "${TMP}" ]] || exit 1

# Bandwidth stats via vnstat and ookla
/home/jim/bin/vnstat > ${TMP} || exit 1
/home/jim/bin/vnstat --hours >> ${TMP} || exit 1 
cat /home/jim/log/speedtest.log >> ${TMP} || exit 1 
echo >> ${TMP} || exit 1 # Formatting

# Let's sort by ratio; thanks Gemini!
REMOTE=$(/usr/local/bin/transmission-remote -l)
[[ -n "${REMOTE}" ]] || exit 1
head -n 1 <<< ${REMOTE} >> ${TMP} || exit 1
sed '1d;$d' <<< ${REMOTE} | sed -E 's/ (GB|MB|kB|B|mins|hrs|days|secs)/@\1/g' | sort -k7rn | sed -E 's/@(GB|MB|kB|B|mins|hrs|days|secs)/ \1/g' >> ${TMP} || exit 1
tail -n 1 <<< ${REMOTE} >> ${TMP} || exit 1

# Install the updated status
cat ${TMP} > /var/lib/transmission/Downloads/status.txt || exit 1 
rm ${TMP} || exit 1 

# All done
curl -fsS -m 10 --retry 5 -o /dev/null https://hc-ping.com/a754b727-50d2-4bf5-8195-7a4cf7d468a3
exit 0
