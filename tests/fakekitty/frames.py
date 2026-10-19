"""Decode the inline kitty graphics transmissions (a=t / a=T) recorded by fakekitty.py.

usage: python3 -I frames.py OUT.bin

Prints one line per decoded image and exits 0 when at least one image decoded to exactly width*height pixels.
"""
import base64
import re
import sys
import zlib

APC = re.compile(rb"\x1b_G([^;\x1b]*)(?:;([^\x1b]*))?\x1b\\")


def transmissions(data):
    """Yield (keys, payload) for every whole-image transmission, with chunked payloads joined."""
    current = None
    for m in APC.finditer(data):
        keys = dict(kv.split("=", 1) for kv in m.group(1).decode().split(",") if "=" in kv)
        chunk = m.group(2) or b""
        if current is not None:
            current[1].append(chunk)
            if keys.get("m") == "0":
                yield current[0], b"".join(current[1])
                current = None
            continue
        if keys.get("a", "t") not in ("t", "T") or keys.get("t", "d") != "d" or "s" not in keys:
            continue
        if keys.get("m") == "1":
            current = (keys, [chunk])
        else:
            yield keys, chunk


def decode(keys, payload):
    """(width, height, distinct pixel values in the first rows) or raise ValueError."""
    raw = base64.b64decode(payload)
    if keys.get("o") == "z":
        raw = zlib.decompress(raw)
    w, h, bpp = int(keys["s"]), int(keys["v"]), int(keys.get("f", "32")) // 8
    if len(raw) != w * h * bpp:
        raise ValueError(f"{len(raw)} bytes for {w}x{h}x{bpp}")
    sample = raw[: w * bpp * min(h, 64)]
    return w, h, len({sample[i:i + bpp] for i in range(0, len(sample), bpp)})


def main(path):
    with open(path, "rb") as f:
        data = f.read()
    good = 0
    for keys, payload in transmissions(data):
        try:
            w, h, colours = decode(keys, payload)
        except (ValueError, zlib.error) as e:
            print(f"image i={keys.get('i')}: undecodable: {e}")
            continue
        good += 1
        print(f"image i={keys.get('i')}: {w}x{h}, {colours} distinct colours in the first rows")
    print(f"{good} image(s) decoded")
    return 0 if good else 1


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    sys.exit(main(sys.argv[1]))
