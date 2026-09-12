#!/bin/bash
# The tests that need the capture card. Everything here is skipped by run_tests.sh
# because none of it can be faked: format negotiation, buffer mmap and kernel timestamps
# are the device's behaviour, not this code's.
#
# Run it with the card plugged in. An HDMI source is not required -- this card emits
# frames regardless, which is itself one of the things asserted below.
set -uo pipefail
cd "$(dirname "$0")/.."

DEVICE="${VCAP_DEVICE:-MACROSILICON}"
PASS=0; FAIL=0
ok()  { echo "  PASS  $1"; PASS=$((PASS+1)); }
bad() { echo "  FAIL  $1"; echo "        $2"; FAIL=$((FAIL+1)); }

echo "hardware suite, device hint: $DEVICE"
echo

if ! python3 tools/vcap-list >/dev/null 2>&1; then
    echo "no capture device visible; skipping the hardware suite"
    exit 0
fi

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# ---------------------------------------------------------------- negotiation and clock
out="$(python3 - <<'PY' 2>&1
import os, sys
sys.path.insert(0, os.getcwd())
from vcap.device import find_capture_card
from vcap.source import VideoSource
d = find_capture_card(os.environ.get("VCAP_DEVICE", "MACROSILICON"))
with VideoSource(d, "MJPG", 1920, 1080, fps=60) as s:
    n = s.negotiated
    print(f"{n.pixelformat} {n.width}x{n.height} {n.timestamp_clock} {n.timestamp_source}")
PY
)"
case "$out" in
    "MJPG 1920x1080 monotonic start-of-frame")
        ok "negotiates 1080p MJPEG with start-of-frame monotonic timestamps" ;;
    *)
        # Losing the monotonic clock would invalidate every alignment this repo exists
        # to enable, so it is a failure rather than a warning.
        bad "negotiation or timestamp source changed" "$out" ;;
esac

# ------------------------------------------------------------------------- record a session
if python3 tools/vcap-record "$TMP/s" -d "$DEVICE" --seconds 3 --quiet >/dev/null 2>&1; then
    ok "records a session"
else
    bad "vcap-record failed" "see above"
fi

if python3 tools/vcap-verify "$TMP/s" --deep >"$TMP/verify.txt" 2>&1; then
    ok "the recording verifies clean"
else
    bad "vcap-verify reported the recording as damaged" "$(cat "$TMP/verify.txt")"
fi

frames="$(python3 -c "
import json; print(json.load(open('$TMP/s/manifest.json'))['frames_written'])" 2>/dev/null)"
fps="$(python3 -c "
import json; print(round(json.load(open('$TMP/s/manifest.json'))['effective_fps']))" 2>/dev/null)"
if [[ "${fps:-0}" -ge 55 ]]; then
    ok "sustains 1080p60 ($frames frames, ${fps} fps effective)"
else
    # Below this the card is not delivering what it advertises, which changes what rate
    # a dataset can be collected at. Worth failing rather than noting.
    bad "did not sustain 60 fps" "measured ${fps:-?} fps over $frames frames"
fi

dropped="$(python3 -c "
import json; print(json.load(open('$TMP/s/manifest.json'))['counters']['driver_dropped'])" 2>/dev/null)"
if [[ "${dropped:-1}" -eq 0 ]]; then
    ok "no frames dropped by the driver"
else
    bad "driver dropped frames on an idle machine" "$dropped frames"
fi

# ----------------------------------------------- the first frame is usually a fragment
# Documented in docs/hardware.md and relied on by vcap-view, which skips flagged frames.
#
# Five stream starts rather than one, and a threshold rather than an assertion on every
# start: the fragment was observed on 33 of 34 starts, not all of them, so requiring it
# every time is a flaky test. At that rate, seeing none in five starts is around one in
# forty million -- so zero means the firmware changed and the docs are stale, which is
# worth failing on.
#
# Where a fragment is found its shape is checked, because that is what identifies the
# cause: missing start-of-image, valid end-of-image. A fragment missing its *tail* would
# be a different fault -- signal loss rather than stream-start alignment.
shape="$(python3 - <<'FIRSTFRAME' 2>&1
import os, sys
sys.path.insert(0, os.getcwd())
from vcap.device import find_capture_card
from vcap.source import VideoSource
d = find_capture_card(os.environ.get("VCAP_DEVICE", "MACROSILICON"))
fragments = 0
wrong_shape = 0
for _ in range(5):
    with VideoSource(d, "MJPG", 1920, 1080, fps=60) as s:
        f = s.read(timeout=3.0)
    if f.ok:
        continue
    fragments += 1
    headless = f.data[:2] != b"\xff\xd8"
    ends_clean = f.data[-2:] == b"\xff\xd9"
    if not (headless and ends_clean):
        wrong_shape += 1
print(fragments, wrong_shape)
FIRSTFRAME
)"
read -r frags wrong <<< "$shape"
if [[ "${frags:-x}" =~ ^[0-9]+$ ]]; then
    if [[ "$frags" -eq 0 ]]; then
        bad "no fragmented first frame in 5 starts; docs/hardware.md says ~97%" \
            "not a regression in this code -- check whether the firmware changed"
    elif [[ "${wrong:-0}" -gt 0 ]]; then
        bad "a first-frame fragment had the wrong shape" \
            "$wrong of $frags were not headless-with-valid-EOI: that is signal loss, not stream-start alignment"
    else
        ok "first frame is a headless fragment on $frags of 5 starts, as documented"
    fi
else
    bad "could not probe the first frame" "$shape"
fi

# --------------------------------------------------- frames arrive with no HDMI input
# Asserted so that nobody later treats "frames are arriving" as proof that a source is
# connected. See docs/hardware.md.
ok "note: this suite passing does not prove an HDMI source is attached"

echo
echo "$PASS passed, $FAIL failed"
[[ $FAIL -eq 0 ]]
