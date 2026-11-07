#!/bin/bash
# Build and upgrade Transmission from source on alma10 
# until BZ#2401885 / BZ#2401887 are resolved.
#
# Must be run as root

# Configuration
SRC_DIR="/usr/src/transmission"
GITHUB_API="https://api.github.com/repos/transmission/transmission/releases/latest"
SERVICE="transmission-daemon"
TRANSMISSION_REMOTE="/usr/local/bin/transmission-remote"

# Helpers
log()  { echo "[$(date '+%H:%M:%S')] $*"; }
die()  { echo "[ERROR] $*" >&2; exit 1; }

installed_version() {
    local output
    output=$("$TRANSMISSION_REMOTE" --version 2>&1) || return 1
    grep -oE '[0-9]+\.[0-9]+(\.[0-9]+)?' <<< "$output" | head -1
}

prepare_build() {
    if [[ -d "$BUILD_DIR" ]]; then
        log "Source directory already exists; removing stale build: $BUILD_DIR"
        rm -rf -- "$BUILD_DIR" || die "Could not remove stale build: $BUILD_DIR"
    fi
    log "Extracting $TARBALL..."
    # tar can create the expected directory before failing partway through.
    # Its exit status, not directory existence alone, establishes that the
    # source is complete. A failed cd must also stop before cmake can build
    # an unrelated project in the caller's working directory.
    tar xf "$TARBALL_PATH" -C "$SRC_DIR" || die "Tarball extraction failed"
    [[ -d "$BUILD_DIR" ]] || die "Expected source dir not found after extraction: $BUILD_DIR"
    cd -- "$BUILD_DIR" || die "Could not enter source directory: $BUILD_DIR"
}

verify_install() {
    local installed_now
    installed_now=$(installed_version) || die "Failed to query installed version after install"
    [[ -n "$installed_now" ]] || die "Failed to capture installed version after install"
    # A successful install command can still leave the old executable in
    # place. Check the requested release before reporting upgrade success.
    [[ "$installed_now" == "$LATEST_VERSION" ]] \
        || die "Installed version $installed_now does not match expected $LATEST_VERSION"
    log "Upgrade complete. Installed version: $installed_now"
}

restart_service() {
    systemctl start "$SERVICE" || return 1
    sleep 2
    systemctl is-active --quiet "$SERVICE"
}

restore_service_on_exit() {
    local result=$?
    trap - EXIT HUP INT TERM
    if [[ "$RESTART_SERVICE" == true ]]; then
        log "Restoring $SERVICE after upgrade failure..."
        # An unsuccessful install can still leave usable binaries behind.
        # Try to restore the original running state, but retain the install
        # error and make a failed recovery explicit instead of claiming
        # success. This is recovery, not a rollback of installed files.
        if ! restart_service; then
            echo "[ERROR] Could not restore $SERVICE — check: journalctl -u $SERVICE" >&2
            result=1
        fi
    fi
    exit "$result"
}

install_build() {
    RESTART_SERVICE=false
    trap restore_service_on_exit EXIT
    trap 'exit 129' HUP
    trap 'exit 130' INT
    trap 'exit 143' TERM

    if systemctl is-active --quiet "$SERVICE" 2>/dev/null; then
        # Arm recovery before stopping: even a failed stop or an interrupt
        # immediately after it must not strand a previously running daemon.
        RESTART_SERVICE=true
        log "Stopping $SERVICE..."
        systemctl stop "$SERVICE" || die "Could not stop $SERVICE; install aborted"
    fi

    log "Installing..."
    cmake --install build || die "Install stage failed"

    if [[ "$RESTART_SERVICE" == true ]]; then
        log "Restarting $SERVICE..."
        restart_service || die "$SERVICE failed to start — check: journalctl -u $SERVICE"
        RESTART_SERVICE=false
        log "$SERVICE restarted successfully."
    fi
    trap - EXIT HUP INT TERM
}

# Expose preparation, verification, and the install lifecycle to tests without
# running root-only setup, network requests, builds, or installation on source.
if [[ "${BASH_SOURCE[0]}" != "$0" ]]; then
    return
fi

# Must be run as root
[[ $EUID -eq 0 ]] || die "This script must be run as root."

# Must be run on a system with dnf (only tested on Alma10)
command -v dnf &>/dev/null || die "This script requires dnf"

# Check build / runtime dependencies
MISSING=()
for pkg in cmake gcc gcc-c++ make pkgconf-pkg-config curl jq; do
    rpm -q "$pkg" &>/dev/null || MISSING+=("$pkg")
done
if [[ ${#MISSING[@]} -gt 0 ]]; then
    die "Missing required packages: ${MISSING[*]}
       Install with: dnf install -y ${MISSING[*]}"
fi

# Cache the API response for both version and checksum data
log "Checking GitHub for latest Transmission release..."
API_RESPONSE=$(curl -fsSL "$GITHUB_API") || die "GitHub API request failed."

# Check upstream for a new release
LATEST_VERSION=$(echo "$API_RESPONSE" | jq -r '.tag_name')
[[ -n "$LATEST_VERSION" ]] || die "Could not determine latest version from GitHub API."
log "Latest available version: $LATEST_VERSION"

# Grab the checksum
TARBALL_DIGEST=$(echo "$API_RESPONSE" \
    | jq -r --arg name "transmission-${LATEST_VERSION}.tar.xz" \
        '.assets[] | select(.name == $name) | .digest | ltrimstr("sha256:")')
[[ -n "$TARBALL_DIGEST" ]] || die "Could not extract tarball digest from GitHub API."

# Check installed version
INSTALLED_VERSION=""
if [[ -x "$TRANSMISSION_REMOTE" ]]; then
    INSTALLED_VERSION=$(installed_version) || die "Could not query installed Transmission version"
fi

# Exit if we can't find the installed version
[[ -n "$INSTALLED_VERSION" ]] || die "Transmission does not appear to be installed yet."
log "Currently installed version: $INSTALLED_VERSION"

# If upstream matches our current version, nothing to do
if [[ "$INSTALLED_VERSION" == "$LATEST_VERSION" ]]; then
    log "Already up to date ($INSTALLED_VERSION). Nothing to do."
    exit 0
fi

# We've got one!
log "Upgrade available: $INSTALLED_VERSION -> $LATEST_VERSION"

# Check if we already have the tarball
TARBALL="transmission-${LATEST_VERSION}.tar.xz"
TARBALL_PATH="$SRC_DIR/$TARBALL"
RELEASE_BASE="https://github.com/transmission/transmission/releases/download/${LATEST_VERSION}"

mkdir -p -- "$SRC_DIR" || die "Could not create source directory: $SRC_DIR"

if [[ -f "$TARBALL_PATH" ]]; then
    log "Tarball already present: $TARBALL_PATH"
else
    log "Downloading $TARBALL..."
    curl -fL --progress-bar -o "$TARBALL_PATH" "$RELEASE_BASE/$TARBALL" \
        || die "Tarball download failed: $RELEASE_BASE/$TARBALL"
fi

# Check the checksum baby
log "Verifying checksum..."
ACTUAL_DIGEST=$(sha256sum "$TARBALL_PATH" | awk '{print $1}')
if [[ "$ACTUAL_DIGEST" != "$TARBALL_DIGEST" ]]; then
    rm -f "$TARBALL_PATH"
    die "Checksum mismatch — tarball deleted. Re-run to download fresh.
       Expected: $TARBALL_DIGEST
       Got:      $ACTUAL_DIGEST"
fi
log "Checksum OK."

# Extract it, fresh if needed
BUILD_DIR="$SRC_DIR/transmission-${LATEST_VERSION}"
prepare_build

# Build it
log "Configuring build..."
cmake -B build -DCMAKE_BUILD_TYPE=Release || die "Build stage 1 failed"
log "Compiling (using $(nproc) cores)..."
cmake --build build -- -j"$(nproc)" || die "Build stage 2 failed"

install_build

verify_install
exit 0
