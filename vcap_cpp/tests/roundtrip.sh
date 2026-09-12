#!/bin/bash
# The contract between the two clients: C++ writes a recording, Python reads it.
#
# This is the test that matters. The unit tests check that the index record encodes
# identically, but a recording is three files that have to agree with each other, and a
# manifest this client emits has to satisfy a reader it never sees. Nothing but an actual
# round trip catches a mismatch in, say, whether byte offsets are per-segment.
#
# Needs the capture card. Skips cleanly without it, because CI has no hardware -- see
# tests/roundtrip_synthetic.sh for the part that runs anywhere.
set -uo pipefail
cd "$(dirname "$0")/.."
export REPO="$(cd .. && pwd)"
DEVICE="${VCAP_DEVICE:-MACROSILICON}"

PASS=0; FAIL=0
ok()  { echo "  PASS  $1"; PASS=$((PASS+1)); }
bad() { echo "  FAIL  $1"; echo "        $2"; FAIL=$((FAIL+1)); }

if ! python3 "$REPO/tools/vcap-list" >/dev/null 2>&1; then
    echo "no capture device visible; skipping the round-trip suite"
    exit 0
fi
[[ -x build/vcap-record-cpp ]] || { echo "run ./build.sh first" >&2; exit 1; }

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# Small segments, so rollover is exercised in a few seconds rather than a few minutes.
if ./build/vcap-record-cpp "$TMP/rec" -d "$DEVICE" --seconds 6 --segment-mb 8 \
       --note writer=cpp --quiet >/dev/null 2>&1; then
    ok "the C++ recorder writes a session"
else
    bad "vcap-record-cpp failed" "see above"; echo "$FAIL failed"; exit 1
fi

segs="$(ls "$TMP"/rec/*.mjpg 2>/dev/null | wc -l)"
if [[ "$segs" -gt 1 ]]; then ok "it rolled over into $segs segments"
else bad "expected rollover with --segment-mb 8" "got $segs segment(s)"; fi

if python3 "$REPO/tools/vcap-verify" "$TMP/rec" --deep >"$TMP/verify.txt" 2>&1; then
    ok "the Python verifier reads it and calls it usable"
else
    bad "vcap-verify rejected a C++ recording" "$(cat "$TMP/verify.txt")"
fi

# The properties a dataset builder depends on, checked through the Python reader.
out="$(python3 - "$TMP/rec" <<'PY' 2>&1
import os, sys
sys.path.insert(0, os.path.join(os.environ["REPO"], "vcap_py"))
from vcap.reader import Recording
from vcap.frame import jpeg_dimensions, looks_like_jpeg

with Recording(sys.argv[1]) as r:
    problems = []
    if len(r) == 0:
        problems.append("no frames")
    if not r.timestamps_are_monotonic:
        problems.append("timestamps not monotonic")
    if r.manifest.get("written_by") != "vcap_cpp":
        problems.append("manifest does not say who wrote it")
    if (r.manifest.get("notes") or {}).get("writer") != "cpp":
        problems.append("notes did not survive")
    # Entries must be numbered across the whole session, not per segment.
    if [i for i in range(len(r)) if r.entry(i).index != i]:
        problems.append("indices are per-segment, not global")
    # Every frame the index points at must be the bytes the index claims.
    for i in range(0, len(r), max(1, len(r) // 40)):
        f = r[i]
        if len(f.data) != r.entry(i).length:
            problems.append(f"frame {i} length mismatch")
            break
        if f.ok and not looks_like_jpeg(f.data):
            problems.append(f"frame {i} flagged ok but is not a whole JPEG")
            break
        if f.ok and jpeg_dimensions(f.data) != (r.manifest["granted"]["width"],
                                                r.manifest["granted"]["height"]):
            problems.append(f"frame {i} geometry disagrees with the manifest")
            break
    # Timestamps must rise; a regression would mean the writer reordered frames.
    ts = [r.entry(i).ts_mono_ns for i in range(len(r))]
    if ts != sorted(ts):
        problems.append("timestamps are not increasing")
    if r.at(ts[len(ts)//2], tolerance_ns=1) is None:
        problems.append("timestamp lookup failed on an exact value")
    print("; ".join(problems) if problems else "OK")
PY
)"
if [[ "$out" == "OK" ]]; then
    ok "frames, indices, timestamps and manifest all read correctly in Python"
else
    bad "the Python reader disagreed with the C++ writer" "$out"
fi

echo
echo "$PASS passed, $FAIL failed"
[[ $FAIL -eq 0 ]]
