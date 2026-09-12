"""The dependency boundary of this package.

Everything else in `vcap` imports only the standard library, and that is enforced by
tests/check_stdlib_only.py. That property was not a goal pursued for its own sake -- it
fell out of the fact that V4L2 is ioctls and mmap, both of which Python already has, so
capturing, indexing, writing and reading a recording genuinely needs nothing installed.
Submoduling this repo therefore adds no dependencies to yours.

Decoding is where that stops being true, and pretending otherwise would be worse than
admitting it. Turning a JPEG into an array is real work that numpy, Pillow or OpenCV
already do faster and more correctly than a pure-Python implementation could, and a
training pipeline that wants frames as tensors has those installed already.

So the boundary is drawn here, in one module, explicitly:

  * `vcap` core          stdlib only, enforced. Capture, storage, timestamps.
  * `vcap.decode`        optional imports, resolved at call time with a clear error.
  * `vcap.export.*`      optional imports and external binaries (ffmpeg).

The rule this keeps is not "no dependencies". It is that a consumer only pays for what it
uses: recording an episode on a fresh machine needs nothing, and needing tensors is an
explicit step that says so.
"""
from __future__ import annotations

from .frame import Frame

_BACKENDS = ("cv2", "PIL", "numpy+imageio")


class DecodeUnavailable(ImportError):
    """No decoding backend is installed.

    Raised at call time rather than import time so that importing `vcap.decode` from a
    module that only sometimes decodes does not break a capture-only installation.
    """


def _load_backend():
    try:
        import cv2  # noqa: PLC0415
        import numpy as np  # noqa: PLC0415

        def decode_cv2(data: bytes):
            buf = np.frombuffer(data, dtype=np.uint8)
            img = cv2.imdecode(buf, cv2.IMREAD_COLOR)
            if img is None:
                raise ValueError("not a decodable image")
            # BGR is OpenCV's convention and a persistent source of silently wrong
            # colour in training data. Returning RGB here makes this module's contract
            # unambiguous regardless of which backend served the call.
            return cv2.cvtColor(img, cv2.COLOR_BGR2RGB)

        return "cv2", decode_cv2
    except ImportError:
        pass

    try:
        import io  # noqa: PLC0415

        import numpy as np  # noqa: PLC0415
        from PIL import Image  # noqa: PLC0415

        def decode_pil(data: bytes):
            with Image.open(io.BytesIO(data)) as img:
                return np.asarray(img.convert("RGB"))

        return "PIL", decode_pil
    except ImportError:
        pass

    raise DecodeUnavailable(
        "decoding needs one of: " + ", ".join(_BACKENDS) + ". The capture and storage "
        "path does not -- install a backend only in the environment that trains.")


_backend: tuple[str, object] | None = None


def backend_name() -> str:
    global _backend
    if _backend is None:
        _backend = _load_backend()
    return _backend[0]


def available() -> bool:
    try:
        backend_name()
    except DecodeUnavailable:
        return False
    return True


def to_rgb(frame: Frame | bytes):
    """A frame as an HxWx3 uint8 RGB array.

    RGB, not BGR, whichever backend is in use. See the note in the cv2 path.
    """
    global _backend
    if _backend is None:
        _backend = _load_backend()
    data = frame.data if isinstance(frame, Frame) else frame
    return _backend[1](data)
