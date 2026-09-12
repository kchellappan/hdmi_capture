#!/bin/bash
# The C++ client's suite. Builds, runs the unit tests, then the round trips.
set -uo pipefail
cd "$(dirname "$0")/.."

echo "building"
./build.sh >/dev/null || { echo "  FAIL  build"; exit 1; }
echo "  ok    build"
echo

FAIL=0
echo "unit"
./build/test_index_format || FAIL=1
./build/test_jpeg || FAIL=1
echo

echo "round trip: C++ writes, Python reads (no hardware)"
./tests/roundtrip_synthetic.sh || FAIL=1
echo

echo "round trip: against the capture card"
./tests/roundtrip.sh || FAIL=1

exit $FAIL
