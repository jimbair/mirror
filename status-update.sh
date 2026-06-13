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
#
# 'head' and 'tail' print the header and footer lines untouched.
# 'sed 1d;$d' strips the header/footer from the middle section before sorting.
# The first 'sed -E' replaces spaces inside sizes (e.g. '13.94 GB') and ETAs
# (e.g. '10 mins') with '@' so they don't break column-based sorting.
# 'sort -k7rn' sorts numerically (-n) and in reverse/descending (-r) by the 7th column.
# The final 'sed -E' swaps the '@' characters back into normal spaces.
REMOTE=$(/usr/local/bin/transmission-remote -l)
[[ -n "${REMOTE}" ]] || exit 1
head -n 1 <<< ${REMOTE} >> ${TMP} || exit 1
sed '1d;$d' <<< ${REMOTE} | sed -E 's/ (GB|MB|kB|B|mins|hrs|days|secs)/@\1/g' | sort -k7rn | sed -E 's/@(GB|MB|kB|B|mins|hrs|days|secs)/ \1/g' >> ${TMP} || exit 1
tail -n 1 <<< ${REMOTE} >> ${TMP} || exit 1

# Install the updated status
cat ${TMP} > /var/lib/transmission/Downloads/status.txt || exit 1 
rm ${TMP} || exit 1 

# All done
exit 0
