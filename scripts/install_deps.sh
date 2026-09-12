#!/bin/bash
# Report what is present, what is missing, and offer to install it.
#
# Read the first line of output before assuming you need anything: recording needs nothing
# but python3 and a kernel with uvcvideo, both of which any Linux install already has.
# Everything below that line unlocks something optional -- device access from a service,
# building the C++ implementation, exporting to MP4 -- and is grouped by what it unlocks
# rather than by which package provides it, so the report doubles as documentation.
#
#   scripts/install_deps.sh            check, and offer to fix what is missing
#   scripts/install_deps.sh --check    report only, never install; exits 1 if anything is missing
#   scripts/install_deps.sh --yes      fix everything missing without asking
set -uo pipefail
cd "$(dirname "$0")/.."

CHECK_ONLY=0
ASSUME_YES=0

usage() {
    cat <<'USAGE'
usage: scripts/install_deps.sh [--check] [--yes]

  --check   report only; never install. Exits 1 if anything is missing.
  --yes     install everything missing without prompting.

With neither, it reports and asks before each change. Recording itself needs nothing
installed; everything offered here is optional.
USAGE
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --check) CHECK_ONLY=1 ;;
        --yes|-y) ASSUME_YES=1 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
    esac
    shift
done

MISSING=0
declare -a ACTIONS=()

# Colour only when writing to a terminal: piped into a file or a log, escape codes are
# noise, and this script's whole output is meant to be quotable in a bug report.
if [[ -t 1 ]]; then GREEN=$'\033[32m'; YELLOW=$'\033[33m'; RESET=$'\033[0m'
else GREEN=''; YELLOW=''; RESET=''; fi

ok()      { printf '  %sok%s       %-22s %s\n' "$GREEN" "$RESET" "$1" "${2:-}"; }
missing() { printf '  %smissing%s  %-22s %s\n' "$YELLOW" "$RESET" "$1" "${2:-}"
            MISSING=$((MISSING+1)); }
note()    { printf '  %-31s %s\n' "" "$1"; }
group()   { printf '\n%s\n' "$1"; }

have() { command -v "$1" >/dev/null 2>&1; }

# ---------------------------------------------------------------- capture: needs nothing
group "capture and storage -- nothing to install"
ok "python3" "$(python3 --version 2>&1 | cut -d' ' -f2)"
if [[ -d /sys/module/uvcvideo ]]; then
    ok "uvcvideo" "loaded"
else
    # It autoloads when a UVC device is plugged in, so absence is not a problem.
    ok "uvcvideo" "not loaded (autoloads when the card is plugged in)"
fi
if python3 tools/vcap-list >/dev/null 2>&1; then
    ok "capture device" "visible and openable"
else
    # Deliberately not counted as missing: no card attached is a normal state, and this
    # script cannot tell that apart from a permissions problem it is about to fix below.
    note "no capture device visible or openable right now"
fi

# ------------------------------------------------------------------------ device access
group "device access from a service, a cron job or SSH"
if [[ -f /etc/udev/rules.d/99-vcap.rules ]]; then
    ok "udev rule" "installed"
else
    missing "udev rule" "stable /dev/vcap0, and access without a desktop session"
    ACTIONS+=("udev")
fi
if id -nG | tr ' ' '\n' | grep -qx video; then
    ok "video group" "$(id -un) is a member"
else
    missing "video group" "$(id -un) is not a member"
    note "a desktop session gets an ACL on /dev/video* and works without this;"
    note "a systemd service or an SSH login does not."
    ACTIONS+=("group")
fi

# --------------------------------------------------------------------------- C++ client
group "building vcap_cpp (optional; the Python implementation needs no build)"
if have g++ || have c++; then
    ok "C++ compiler" "$( (g++ --version 2>/dev/null || c++ --version) | head -1)"
else
    missing "C++ compiler" "required to build vcap_cpp"
    ACTIONS+=("compiler")
fi
if have cmake; then
    ok "cmake" "$(cmake --version | head -1 | cut -d' ' -f3)"
else
    # build.sh falls back to calling g++ directly, so this is genuinely optional.
    ok "cmake" "absent; build.sh falls back to direct g++"
fi

# ------------------------------------------------------------------- latency measurement
group "measuring capture latency with vcap-glass-to-glass (optional)"
if python3 -c "import tkinter" 2>/dev/null; then
    ok "tkinter" "present"
else
    # In the standard library, but Debian and Ubuntu package it separately -- so this is
    # missing on a fresh install even though nothing was left out of Python.
    missing "tkinter" "draws the timing pattern; nothing else here needs it"
    ACTIONS+=("tkinter")
fi

# -------------------------------------------------------------------------------- export
group "exporting to MP4 or MKV (optional; not needed to record)"
if have ffmpeg; then
    ok "ffmpeg" "$(ffmpeg -version 2>/dev/null | head -1 | cut -d' ' -f3)"
else
    missing "ffmpeg" "needed only by vcap_py/vcap/export/"
    ACTIONS+=("ffmpeg")
fi

# ---------------------------------------------------------------- decode (informational)
group "decoding frames to arrays, for training (optional)"
if python3 -c 'import sys; sys.path.insert(0, "vcap_py"); from vcap import decode; sys.exit(0 if decode.available() else 1)' 2>/dev/null; then
    ok "decode backend" "present"
else
    # Not offered for installation: this belongs in whatever environment trains, which is
    # usually a venv this script has no business touching.
    note "no numpy/Pillow/OpenCV. Not offered here -- install it in the environment"
    note "that trains, not system-wide. Capture and storage do not need it."
fi

echo
if [[ $MISSING -eq 0 ]]; then
    echo "nothing missing."
    exit 0
fi

if [[ $CHECK_ONLY -eq 1 ]]; then
    echo "$MISSING item(s) missing. Re-run without --check to install."
    exit 1
fi

# Prompting from a non-interactive shell would hang a service or a CI job.
if [[ ! -t 0 ]] && [[ $ASSUME_YES -eq 0 ]]; then
    echo "$MISSING item(s) missing, and stdin is not a terminal. Re-run with --yes." >&2
    exit 1
fi

confirm() {
    [[ $ASSUME_YES -eq 1 ]] && return 0
    read -r -p "  $1 [y/N] " reply
    [[ "$reply" == "y" || "$reply" == "Y" ]]
}

apt_install() {
    if ! have apt-get; then
        echo "  no apt-get; install these with your package manager: $*" >&2
        return 1
    fi
    sudo apt-get update -qq && sudo apt-get install -y "$@"
}

echo "$MISSING item(s) missing."
for action in "${ACTIONS[@]}"; do
    case "$action" in
        udev)
            if confirm "install the udev rule to /etc/udev/rules.d/?"; then
                sudo install -m 0644 scripts/udev/99-vcap.rules /etc/udev/rules.d/99-vcap.rules
                sudo udevadm control --reload-rules
                sudo udevadm trigger --subsystem-match=video4linux
                echo "  done. Replug the card if /dev/vcap0 does not appear."
            fi ;;
        group)
            if confirm "add $(id -un) to the video group?"; then
                sudo usermod -aG video "$(id -un)"
                # Group membership is read at login, so the running shell does not have it.
                echo "  done. Log out and back in for it to take effect."
            fi ;;
        compiler)
            confirm "install build-essential and cmake?" && apt_install build-essential cmake ;;
        tkinter)
            confirm "install python3-tk?" && apt_install python3-tk ;;
        ffmpeg)
            confirm "install ffmpeg?" && apt_install ffmpeg ;;
    esac
done

echo
echo "next:"
echo "  ./tools/vcap-list                    confirm the card is seen"
echo "  ./tools/vcap-probe -d MACROSILICON   measure what it delivers"
echo "  ./tools/vcap-view  -d MACROSILICON   LOOK AT THE PICTURE before recording;"
echo "                                       this card emits frames with no input"
