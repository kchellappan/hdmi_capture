"""Finding the display that feeds the capture card.

The pattern has to be drawn on whichever monitor is plugged into the card's HDMI input,
and a program has no direct way to know which that is. What it can do is enumerate the
monitors and their positions, so a window can be placed on one deliberately rather than
wherever the desktop happens to put it.

This reads the X11 layout. On a Wayland session that is XWayland's view, which is enough
for positioning an XWayland client such as a Tk window -- measured on GNOME/Wayland, where
`xrandr --listmonitors` reports the external output at its true offset even though native
Wayland clients cannot position themselves at all.

If it cannot work the layout out, the caller is expected to say so and let the operator
place the window by hand, not to guess.
"""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass


@dataclass(frozen=True)
class Output:
    name: str
    width: int
    height: int
    x: int
    y: int
    primary: bool

    @property
    def geometry(self) -> str:
        """Tk's geometry string for filling this output."""
        return f"{self.width}x{self.height}+{self.x}+{self.y}"

    def __str__(self) -> str:
        mark = " (primary)" if self.primary else ""
        return f"{self.name} {self.width}x{self.height} at +{self.x}+{self.y}{mark}"


# " 1: +HDMI-1 1920/600x1080/340+1920+0  HDMI-1"
_MONITOR = re.compile(
    r"^\s*\d+:\s+\+(?P<primary>\*)?(?P<name>\S+)\s+"
    r"(?P<w>\d+)/\d+x(?P<h>\d+)/\d+\+(?P<x>\d+)\+(?P<y>\d+)")


def list_outputs() -> list[Output]:
    """Every connected monitor. Empty if the layout cannot be read."""
    try:
        result = subprocess.run(["xrandr", "--listmonitors"], capture_output=True,
                                text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return []
    if result.returncode != 0:
        return []

    outputs = []
    for line in result.stdout.splitlines():
        match = _MONITOR.match(line)
        if match:
            outputs.append(Output(
                name=match.group("name"),
                width=int(match.group("w")), height=int(match.group("h")),
                x=int(match.group("x")), y=int(match.group("y")),
                primary=bool(match.group("primary"))))
    return outputs


def pick_external(outputs: list[Output], hint: str | None = None) -> Output | None:
    """The output most likely to be feeding the capture card.

    With a hint, matched against the name. Without one, the sole non-primary output --
    and only if there is exactly one, because picking between two would be a guess and
    the operator can see which display is which.
    """
    if hint:
        for output in outputs:
            if hint.lower() in output.name.lower():
                return output
        return None
    external = [o for o in outputs if not o.primary]
    return external[0] if len(external) == 1 else None
