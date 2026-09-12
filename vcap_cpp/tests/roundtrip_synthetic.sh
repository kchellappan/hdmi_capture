#!/bin/bash
# The C++-writes / Python-reads contract, without a capture card.
#
# Runs anywhere, which is the point: the file format is where a bug costs a dataset, and
# the format is pure computation. Synthetic JPEGs stand in for the card, so CI can check
# that a recording written by the C++ client is read correctly by the Python one.
#
# What this cannot cover: V4L2 negotiation, buffer mmap and kernel timestamps. Those need
# hardware and live in tests/roundtrip.sh.
set -uo pipefail
cd "$(dirname "$0")/.."
export REPO="$(cd .. && pwd)"

PASS=0; FAIL=0
ok()  { echo "  PASS  $1"; PASS=$((PASS+1)); }
bad() { echo "  FAIL  $1"; echo "        $2"; FAIL=$((FAIL+1)); }

[[ -x build/write_synthetic ]] || { echo "run ./build.sh first" >&2; exit 1; }

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

if build_out="$(./build/write_synthetic "$TMP/rec" 400 2>&1)"; then
    ok "the C++ writer produces a recording ($build_out)"
else
    bad "write_synthetic failed" "$build_out"; echo "$FAIL failed"; exit 1
fi

segs="$(ls "$TMP"/rec/*.mjpg | wc -l)"
if [[ "$segs" -gt 2 ]]; then ok "it rolled over into $segs segments"
else bad "expected several segments" "got $segs"; fi

if python3 "$REPO/tools/vcap-verify" "$TMP/rec" --deep >"$TMP/v.txt" 2>&1; then
    ok "the Python verifier reads it and calls it usable"
else
    bad "vcap-verify rejected the C++ recording" "$(cat "$TMP/v.txt")"
fi

out="$(python3 - "$TMP/rec" <<'PY' 2>&1
import os, sys
sys.path.insert(0, os.path.join(os.environ["REPO"], "vcap_py"))
from vcap.reader import Recording
from vcap.frame import FLAG_CORRUPT, FLAG_GAP_BEFORE, looks_like_jpeg

with Recording(sys.argv[1]) as r:
    p = []
    if len(r) != 400: p.append(f"expected 400 frames, got {len(r)}")
    if not r.timestamps_are_monotonic: p.append("timestamps not monotonic")
    if r.manifest.get("written_by") != "vcap_cpp": p.append("no written_by")
    if (r.manifest.get("notes") or {}).get("writer") != "cpp-synthetic": p.append("notes lost")
    if [i for i in range(len(r)) if r.entry(i).index != i]:
        p.append("indices are per-segment, not global")
    # The first frame is the headless fragment, flagged and undecodable.
    if not (r.entry(0).flags & FLAG_CORRUPT): p.append("first frame not flagged corrupt")
    if looks_like_jpeg(r[0].data): p.append("first frame should be a fragment")
    # The injected gap must survive into the index.
    gaps = [e for e in r.flagged() if e.flags & FLAG_GAP_BEFORE]
    if len(gaps) != 1: p.append(f"expected 1 gap, got {len(gaps)}")
    elif gaps[0].dropped_before != 3: p.append(f"gap says {gaps[0].dropped_before}, want 3")
    # Every frame must be exactly the bytes the index claims, across segment boundaries.
    for i in range(len(r)):
        if len(r[i].data) != r.entry(i).length:
            p.append(f"frame {i} length mismatch"); break
    # Sequence numbers step by one except across the injected gap.
    seqs = [r.entry(i).seq for i in range(len(r))]
    steps = {b - a for a, b in zip(seqs, seqs[1:])}
    if steps != {1, 4}: p.append(f"unexpected sequence steps: {sorted(steps)}")
    ts = [r.entry(i).ts_mono_ns for i in range(len(r))]
    if ts != sorted(ts): p.append("timestamps not increasing")
    mid = ts[len(ts)//2]
    if r.at(mid, tolerance_ns=1) is None: p.append("exact timestamp lookup failed")
    print("; ".join(p) if p else "OK")
PY
)"
if [[ "$out" == "OK" ]]; then
    ok "frames, indices, flags, gaps and manifest all read correctly in Python"
else
    bad "the Python reader disagreed with the C++ writer" "$out"
fi

echo
echo "$PASS passed, $FAIL failed"
[[ $FAIL -eq 0 ]]
