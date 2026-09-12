"""Make `vcap` importable when a tool is run from a checkout.

The tools are meant to work in a fresh clone with no install step -- that is the whole
point of a package with no dependencies -- so they put the repo root on sys.path
themselves rather than requiring `pip install -e .` or PYTHONPATH to be set.
"""
from __future__ import annotations

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
