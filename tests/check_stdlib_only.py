#!/usr/bin/env python3
"""Fail if the core of `vcap` imports anything outside the standard library.

The point is not asceticism. Capturing, timestamping, indexing, writing and reading a
recording genuinely needs nothing installed, because V4L2 is ioctls and mmap and Python
already has both -- so a repo that submodules this one can record data on a freshly
installed machine with no venv, no pip and no build step. That property is easy to lose
one convenient import at a time, which is why it is checked rather than asserted.

It is not a rule that this repo has no dependencies anywhere. Decoding a JPEG into an
array is real work that numpy, Pillow and OpenCV do better than pure Python could, and a
machine that trains a policy has them already. So the boundary is explicit and narrow:
the modules in ALLOWED below may import whatever they need, nothing in the core imports
them, and they resolve their imports at call time so that importing them on a
capture-only machine still works.

If this check fails on a core module, the question is not "how do we install that". It is
whether the module belongs on the other side of that line.
"""
from __future__ import annotations

import ast
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
SKIP_DIRS = {".git", "__pycache__", "build", "data"}

# The declared dependency boundary. These may import third-party packages; see
# vcap/decode.py for the reasoning. Paths are relative to the repo root.
ALLOWED = {
    "vcap_py/vcap/decode.py",
    "vcap_py/vcap/export/to_mp4.py",
    "vcap_py/vcap/export/to_frames.py",
}


def imported_modules(path: pathlib.Path) -> set[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            # A relative import is within this package, so there is nothing to check.
            if node.level == 0 and node.module:
                found.add(node.module.split(".")[0])
    return found


def python_files() -> list[pathlib.Path]:
    files = []
    for path in ROOT.rglob("*"):
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.is_dir():
            continue
        if path.suffix == ".py":
            files.append(path)
        elif path.parent.name == "tools" and path.name.startswith("vcap-"):
            # The tools have no extension so that they read as commands, but they are
            # Python and the rule applies to them too.
            files.append(path)
    return sorted(files)


def main() -> int:
    stdlib = set(sys.stdlib_module_names)
    # First-party: the package itself, the tools' sys.path shim, and the test helper
    # that stands in for a capture card.
    local = {"vcap", "_bootstrap", "fake"}
    failures = []

    for path in python_files():
        relative = path.relative_to(ROOT).as_posix()
        if relative in ALLOWED:
            continue
        for module in sorted(imported_modules(path)):
            if module in stdlib or module in local:
                continue
            failures.append(f"{relative}: imports {module!r}")

    if failures:
        print("Non-stdlib imports outside the declared boundary:")
        for line in failures:
            print(f"  {line}")
        print("\nEither use the standard library, or move the code behind "
              "vcap/decode.py or vcap/export/ and add it to ALLOWED here -- "
              "deliberately, with a reason.")
        return 1

    checked = len(python_files()) - len(ALLOWED)
    print(f"  {checked} core files import only the standard library")
    print(f"  {len(ALLOWED)} files on the declared dependency boundary")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
