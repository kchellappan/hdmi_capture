"""Make `vcap` importable when a tool is run from a checkout.

The tools are meant to work in a fresh clone with no install step -- that is the whole
point of a package with no dependencies -- so they put the package directory on sys.path
themselves rather than requiring `pip install -e .` or PYTHONPATH to be set.

The package lives in vcap_py/ rather than at the repo root, so that the directory names
say which language each implementation is while the importable name stays `vcap` -- the
same shape as vcap_cpp/include/vcap/. Anything adding this repo to sys.path by hand wants
vcap_py, not the root.
"""
from __future__ import annotations

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PACKAGE_DIR = os.path.join(_ROOT, "vcap_py")
if _PACKAGE_DIR not in sys.path:
    sys.path.insert(0, _PACKAGE_DIR)
