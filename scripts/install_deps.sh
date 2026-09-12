#!/bin/bash
# Optional setup. Nothing here is needed to record: the capture path imports only the
# standard library and uvcvideo is in the kernel, so a fresh machine can already run
# tools/vcap-record.
#
# What this does install is for the cases where that is not enough:
#   - device access from a service or over SSH, rather than a desktop session
#   - a stable /dev/vcap0 symlink
#   - ffmpeg, for vcap/export only
set -euo pipefail

cd "$(dirname "$0")/.."

WANT_UDEV=1
WANT_FFMPEG=0
WANT_GROUP=0

usage() {
    cat <<'USAGE'
usage: scripts/install_deps.sh [options]

  --no-udev     skip the udev rule
  --ffmpeg      also install ffmpeg (export only; not needed to record)
  --group       add the current user to the video group (needed for service/SSH use)
  -h, --help

With no options, installs only the udev rule.
USAGE
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --no-udev) WANT_UDEV=0 ;;
        --ffmpeg)  WANT_FFMPEG=1 ;;
        --group)   WANT_GROUP=1 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
    esac
    shift
done

echo "== what is already true =="
python3 --version
if [[ -d /sys/module/uvcvideo ]]; then
    echo "uvcvideo: loaded"
else
    # It autoloads when a UVC device is plugged in, so this is informational.
    echo "uvcvideo: not loaded (it autoloads when the card is plugged in)"
fi
if python3 tools/vcap-list >/dev/null 2>&1; then
    echo "capture device: visible and openable"
else
    echo "capture device: not visible, or not openable by $(id -un)"
fi
echo

if [[ $WANT_UDEV -eq 1 ]]; then
    echo "== udev rule =="
    echo "installing /etc/udev/rules.d/99-vcap.rules"
    sudo install -m 0644 scripts/udev/99-vcap.rules /etc/udev/rules.d/99-vcap.rules
    sudo udevadm control --reload-rules
    sudo udevadm trigger --subsystem-match=video4linux
    echo "done. Replug the card if /dev/vcap0 does not appear."
    echo
fi

if [[ $WANT_GROUP -eq 1 ]]; then
    echo "== video group =="
    if id -nG | tr ' ' '\n' | grep -qx video; then
        echo "$(id -un) is already in the video group"
    else
        sudo usermod -aG video "$(id -un)"
        # Group membership is read at login, so the running shell does not have it yet.
        echo "added $(id -un) to the video group -- log out and back in for it to apply"
    fi
    echo
fi

if [[ $WANT_FFMPEG -eq 1 ]]; then
    echo "== ffmpeg =="
    # Needed only by vcap/export. It is an external binary invoked as a subprocess, not
    # a Python dependency, so it does not affect what a submodule costs to install.
    if command -v ffmpeg >/dev/null; then
        echo "already installed: $(ffmpeg -version 2>/dev/null | head -1)"
    elif command -v apt-get >/dev/null; then
        sudo apt-get update && sudo apt-get install -y ffmpeg
    else
        echo "no apt-get; install ffmpeg with your package manager" >&2
    fi
    echo
fi

echo "== next =="
echo "  ./tools/vcap-list                  # confirm the card is seen"
echo "  ./tools/vcap-probe -d MACROSILICON  # measure what it delivers"
echo "  ./tools/vcap-view  -d MACROSILICON  # LOOK AT THE PICTURE before recording;"
echo "                                      # this card emits frames with no input"
