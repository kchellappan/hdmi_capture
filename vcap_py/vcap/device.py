"""Finding a capture device and asking it what it is.

Device node numbering is not stable. A UVC device registers two nodes -- video and UVC
payload-header metadata -- so this card alone consumes /dev/video4 and /dev/video5 on a
laptop whose built-in camera already took 0 through 3. Replug it, or boot with a
different set of cameras attached, and those numbers move. Nothing should ever record or
configure a bare /dev/videoN.

The stable identifier is the by-id symlink, which is derived from the USB descriptor
strings. This module resolves through it and keeps the identity it resolved, so a
recording says which device produced it in terms that survive a reboot.
"""
from __future__ import annotations

import ctypes
import glob
import os
import pathlib
import re
from dataclasses import dataclass, field

from . import v4l2

BY_ID_DIR = "/dev/v4l/by-id"


@dataclass(frozen=True)
class Mode:
    """A format the device advertises. Advertised, not verified -- see probe.measure()."""
    pixelformat: str
    width: int
    height: int
    rates_hz: tuple[float, ...]
    compressed: bool

    def __str__(self) -> str:
        rates = ", ".join(f"{r:g}" for r in self.rates_hz) or "?"
        return f"{self.pixelformat} {self.width}x{self.height} @ {rates}"


@dataclass
class Device:
    """A V4L2 video capture node, identified by something that outlives a replug."""
    path: str                    # the by-id symlink where one exists, else /dev/videoN
    node: str                    # what it resolves to right now
    driver: str
    card: str
    bus_info: str
    device_caps: int
    modes: list[Mode] = field(default_factory=list)

    @property
    def is_capture(self) -> bool:
        return bool(self.device_caps & v4l2.CAP_VIDEO_CAPTURE)

    @property
    def is_metadata(self) -> bool:
        return bool(self.device_caps & v4l2.CAP_META_CAPTURE)

    @property
    def usb(self) -> dict | None:
        """Vendor, product and serial from sysfs, or None when this is not a USB device.

        Resolved by walking up from the video node's own sysfs entry rather than by
        parsing bus_info: bus_info names the PCI host controller and port chain
        ("usb-0000:00:14.0-2"), which is not the bus-port form sysfs uses ("4-2"), so
        matching the two textually means reconstructing the kernel's bus numbering. The
        symlink already knows the answer.
        """
        node = os.path.basename(self.node)
        base = pathlib.Path("/sys/class/video4linux") / node / "device"
        try:
            cur = base.resolve(strict=True)
        except OSError:
            return None
        # base is the USB *interface*; idVendor lives on its parent, the USB device.
        for _ in range(4):
            if (cur / "idVendor").exists():
                return {
                    "vendor_id": _read(cur / "idVendor"),
                    "product_id": _read(cur / "idProduct"),
                    "vendor": _read(cur / "manufacturer"),
                    "product": _read(cur / "product"),
                    "serial": _read(cur / "serial"),
                    "speed_mbps": _read(cur / "speed"),
                }
            if cur.parent == cur:
                break
            cur = cur.parent
        return None

    def identity(self) -> dict:
        """What goes in a recording's manifest to say which device produced it."""
        return {
            "path": self.path,
            "node_at_capture": self.node,
            "driver": self.driver,
            "card": self.card,
            "bus_info": self.bus_info,
            "usb": self.usb,
        }

    def mode(self, pixelformat: str, width: int, height: int) -> Mode | None:
        for m in self.modes:
            if (m.pixelformat == pixelformat and m.width == width
                    and m.height == height):
                return m
        return None


def _read(path) -> str | None:
    try:
        with open(path) as fh:
            return fh.read().strip()
    except OSError:
        return None


def _query(fd: int) -> v4l2.Capability:
    cap = v4l2.Capability()
    v4l2.ioctl(fd, v4l2.VIDIOC_QUERYCAP, cap)
    return cap


def _enumerate_modes(fd: int) -> list[Mode]:
    modes: list[Mode] = []
    index = 0
    while True:
        desc = v4l2.FmtDesc(index=index, type=v4l2.BUF_TYPE_VIDEO_CAPTURE)
        try:
            v4l2.ioctl(fd, v4l2.VIDIOC_ENUM_FMT, desc)
        except OSError:
            break                      # EINVAL: no more formats
        pixfmt = desc.pixelformat
        compressed = bool(desc.flags & v4l2.FMT_FLAG_COMPRESSED)
        size_index = 0
        while True:
            size = v4l2.FrmSizeEnum(index=size_index, pixel_format=pixfmt)
            try:
                v4l2.ioctl(fd, v4l2.VIDIOC_ENUM_FRAMESIZES, size)
            except OSError:
                break
            if size.type != v4l2.FRMSIZE_TYPE_DISCRETE:
                # Stepwise and continuous ranges exist in V4L2 but no UVC capture card
                # reports them; handling them would be untested code.
                size_index += 1
                continue
            w, h = size.u.discrete.width, size.u.discrete.height
            modes.append(Mode(v4l2.fourcc(pixfmt), w, h,
                              tuple(_enumerate_rates(fd, pixfmt, w, h)), compressed))
            size_index += 1
        index += 1
    return modes


def _enumerate_rates(fd: int, pixfmt: int, width: int, height: int) -> list[float]:
    rates: list[float] = []
    index = 0
    while True:
        ival = v4l2.FrmIvalEnum(index=index, pixel_format=pixfmt,
                                width=width, height=height)
        try:
            v4l2.ioctl(fd, v4l2.VIDIOC_ENUM_FRAMEINTERVALS, ival)
        except OSError:
            break
        if ival.type == v4l2.FRMIVAL_TYPE_DISCRETE:
            hz = ival.u.discrete.as_hz()
            if hz:
                rates.append(hz)
        index += 1
    return rates


def open_device(path: str, *, modes: bool = True) -> Device:
    """Query a node without starting a stream. Raises OSError if it cannot be opened."""
    node = os.path.realpath(path)
    fd = os.open(node, os.O_RDWR | os.O_NONBLOCK)
    try:
        cap = _query(fd)
        dev = Device(
            path=path,
            node=node,
            driver=cap.driver.decode(errors="replace"),
            card=cap.card.decode(errors="replace"),
            bus_info=cap.bus_info.decode(errors="replace"),
            device_caps=cap.device_caps,
        )
        if modes and dev.is_capture:
            dev.modes = _enumerate_modes(fd)
        return dev
    finally:
        os.close(fd)


def enumerate_devices(*, capture_only: bool = True) -> list[Device]:
    """Every V4L2 node on the machine, preferring by-id paths.

    Nodes reachable through by-id are reported by that path; anything else falls back to
    /dev/videoN. Metadata nodes are excluded by default: they share a card name with the
    video node they belong to and would otherwise show up as a second, unusable capture
    device.
    """
    by_node: dict[str, str] = {}
    for link in sorted(glob.glob(os.path.join(BY_ID_DIR, "*"))):
        by_node.setdefault(os.path.realpath(link), link)

    devices: list[Device] = []
    for node in sorted(glob.glob("/dev/video*"), key=_node_sort_key):
        try:
            dev = open_device(by_node.get(node, node))
        except OSError:
            continue                   # busy, or no permission; not ours to report on
        if capture_only and not dev.is_capture:
            continue
        devices.append(dev)
    return devices


def _node_sort_key(path: str):
    m = re.search(r"(\d+)$", path)
    return (int(m.group(1)) if m else 0, path)


def find_capture_card(hint: str | None = None) -> Device:
    """The capture device to use, or a clear error explaining what was found instead.

    `hint` is matched against the by-id path and the card name as a substring, which is
    what makes a config file portable: "MACROSILICON" identifies the card regardless of
    which /dev/videoN it landed on this boot.
    """
    if hint and os.path.exists(hint):
        return open_device(hint)

    candidates = enumerate_devices()
    if hint:
        lowered = hint.lower()
        matched = [d for d in candidates
                   if lowered in d.path.lower() or lowered in d.card.lower()]
        if not matched:
            found = ", ".join(f"{d.path} ({d.card})" for d in candidates) or "nothing"
            raise LookupError(f"no capture device matching {hint!r}; found {found}")
        if len(matched) > 1:
            paths = ", ".join(d.path for d in matched)
            raise LookupError(f"{hint!r} matches more than one device: {paths}")
        return matched[0]

    if not candidates:
        raise LookupError(
            "no V4L2 capture device found. If the card is plugged in, check that this "
            "user can open /dev/video* -- see scripts/install_deps.sh")
    if len(candidates) > 1:
        paths = ", ".join(f"{d.path} ({d.card})" for d in candidates)
        raise LookupError(
            f"more than one capture device; pass one explicitly. Found: {paths}")
    return candidates[0]
