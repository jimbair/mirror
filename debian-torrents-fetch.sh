#!/bin/bash
# A script to fetch all of the current debian torrents
# Leverages rsync for speed; this is only run when a new release
# is dropped and I need to update mirror.tsue.net's torrents

# Create two directories, one for the rsync target and one to easily grab all torrents
mkdir -p torrents_tree torrents || exit 1

# Sync the tree (only downloads .torrent files)
rsync -am \
  --include='*/' \
  --include='*.torrent' \
  --exclude='*' \
  rsync://cdimage.debian.org/debian-cd/ torrents_tree/ || exit 1

# Move torrent files into a single directory. With -exec ... \;, a failed
# move is only a false predicate and find can still exit successfully.
# The batched + form propagates command failures, retaining the source
# tree for recovery instead of deleting downloads that never moved.
find torrents_tree -type f -name '*.torrent' \
  -exec sh -c 'mv -- "$@" torrents/' debian-torrents-move {} + || exit 2

# Remove tree (which should now be empty)
rm -r torrents_tree || exit 3

# All done
exit 0
