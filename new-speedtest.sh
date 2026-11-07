#!/bin/bash
# In case our speedtest result seems a bit off and it was a hiccup

SPEEDTEST_COMMAND=/usr/bin/speedtest
SPEEDTEST_LOG=/home/jim/log/speedtest.log

main() {
  local result answer
  printf 'Running speedtest...' || return 1
  if ! result=$("${SPEEDTEST_COMMAND}"); then
    printf 'failed.\nSpeedtest failed to run. Exiting.\n' >&2
    return 1
  fi
  if [[ -z "${result}" ]]; then
    printf 'failed.\nSpeedtest returned no output. Exiting.\n' >&2
    return 1
  fi

  # Each interactive session owns its candidate until approval. A fixed
  # /tmp filename lets a second run replace the result the first displayed.
  # Stage beside the destination so publishing is a same-filesystem rename,
  # preserving complete snapshots for the index builder's concurrent reads.
  SPEEDTEST_TMP=
  trap 'rm -f -- "${SPEEDTEST_TMP}"' EXIT
  trap 'exit 129' HUP
  trap 'exit 130' INT
  trap 'exit 143' TERM
  SPEEDTEST_TMP=$(/usr/bin/mktemp "${SPEEDTEST_LOG}.tmp.XXXXXX") || return 1

  # Check formatting/writes before presenting a candidate for acceptance.
  sed 's/\r//g' <<< "${result}" > "${SPEEDTEST_TMP}" || return 1
  printf 'done!\n\nExisting:\n' || return 1
  if [[ -e "${SPEEDTEST_LOG}" ]]; then
    cat -- "${SPEEDTEST_LOG}" || return 1
  else
    printf '(no previous result)\n' || return 1
  fi
  printf '\n%s\n\nNew:\n' \
    '------------------------------------------------------------------------------------------------------' || return 1
  cat -- "${SPEEDTEST_TMP}" || return 1
  printf '\n' || return 1

  read -r -p 'Would you like to update? ' answer || return 1
  case "${answer}" in
    [Yy]*)
      # mktemp starts private; publish a readable snapshot for collectors.
      chmod 0644 "${SPEEDTEST_TMP}" || return 1
      mv -f -- "${SPEEDTEST_TMP}" "${SPEEDTEST_LOG}" || return 1
      ;;
  esac
  return 0
}

# Tests source the real workflow with disposable paths and a stub speedtest.
if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi
