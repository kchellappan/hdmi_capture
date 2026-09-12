"""Converting a recording into something other than its on-disk form.

Everything here is allowed to use optional imports and external binaries; nothing in the
core imports this package. See vcap/decode.py for the reasoning behind that split.

Export is always a separate step from capture, never folded into it. A recorder that also
transcodes is a recorder that drops frames when the encoder stalls.
"""
