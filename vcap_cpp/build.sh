#!/bin/bash
# Build the C++ client. cmake if present, else a direct g++ call -- the fallback is
# load-bearing rather than decorative: it is what allows a build on a freshly imaged
# machine that has a compiler but no cmake.
set -euo pipefail
cd "$(dirname "$0")"
BUILD="${BUILD_DIR:-build}"

if [[ "${USE_CMAKE:-1}" == "1" ]] && command -v cmake >/dev/null; then
    cmake -S . -B "$BUILD" -DCMAKE_BUILD_TYPE=Release >/dev/null
    cmake --build "$BUILD" -j"$(nproc)"
else
    echo "cmake unavailable or disabled -- falling back to direct g++"
    mkdir -p "$BUILD"
    g++ -std=c++17 -O2 -Wall -Wextra -Iinclude \
        src/*.cpp tools/vcap_record.cpp -o "$BUILD/vcap-record-cpp" -pthread
    g++ -std=c++17 -O2 -Wall -Wextra -Iinclude \
        src/*.cpp tests/write_synthetic.cpp -o "$BUILD/write_synthetic" -pthread
    for t in tests/test_*.cpp; do
        g++ -std=c++17 -O2 -Wall -Wextra -Iinclude \
            src/*.cpp "$t" -o "$BUILD/$(basename "${t%.cpp}")" -pthread
    done
fi
echo "built into $BUILD/"
