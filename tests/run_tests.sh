#!/bin/bash
# The whole suite. Needs python3 and nothing else -- no capture card, no USB, no pip.
#
# That is possible because the storage format is where a bug costs a dataset, and the
# storage format is pure computation: synthetic JPEGs in tests/fake.py stand in for the
# card, and everything from SegmentWriter through Recording is exercised against them.
#
# What is NOT covered here, and cannot be: format negotiation, buffer mmap, kernel
# timestamps, and every hardware behaviour in docs/hardware.md. Those need the card.
# Run tests/hardware.sh for those, on a machine with one plugged in.
set -uo pipefail

cd "$(dirname "$0")"

PASS=0; FAIL=0

run() {
    local name="$1"; shift
    local out
    if out="$("$@" 2>&1)"; then
        echo "$out"
        PASS=$((PASS+1))
    else
        echo "$out"
        echo "        ^ $name failed"
        FAIL=$((FAIL+1))
    fi
}

echo "hdmi_capture test suite"
echo

echo "dependency boundary"
run "stdlib check" python3 check_stdlib_only.py
echo

echo "kernel ABI"
run "ioctl ABI" python3 test_ioctl_abi.py
echo

echo "frame parsing"
run "jpeg" python3 test_jpeg.py
echo

echo "storage"
run "index" python3 test_index.py
run "storage" python3 test_storage.py
echo

echo "buffering"
run "ring" python3 test_ring.py
echo

echo "tools parse and respond to --help"
# A tool that cannot even print its usage is broken in a way no unit test would catch,
# and these are the entry points a user actually touches.
for tool in ../tools/vcap-*; do
    name="$(basename "$tool")"
    if out="$(python3 "$tool" --help 2>&1)"; then
        echo "  ok    $name --help"
        PASS=$((PASS+1))
    else
        echo "  FAIL  $name --help"
        echo "$out" | sed 's/^/        /'
        FAIL=$((FAIL+1))
    fi
done
echo

echo "shell scripts parse"
for f in ../scripts/*.sh ./*.sh; do
    [[ -e "$f" ]] || continue
    if bash -n "$f"; then
        echo "  ok    $(basename "$f")"
        PASS=$((PASS+1))
    else
        echo "  FAIL  $(basename "$f")"
        FAIL=$((FAIL+1))
    fi
done
echo

echo "$PASS passed, $FAIL failed"
[[ $FAIL -eq 0 ]]
