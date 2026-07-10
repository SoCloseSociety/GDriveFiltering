"""Generate the GDriveFiltering app icon: a filtering funnel on an indigo->cyan
rounded card. Pure-stdlib PNG writer, 2x supersampled, resolution-independent
(all shapes are defined in a 1000-unit design space).
Usage: python icon.py [out.png] [size]"""
import math
import struct
import sys
import zlib

OUT = sys.argv[1] if len(sys.argv) > 1 else "/tmp/gdf_icon.png"
S = int(sys.argv[2]) if len(sys.argv) > 2 else 512
SS = 2
W = H = S * SS
D = 1000.0
k = W / D                      # design-units -> pixels

INDIGO = (124, 92, 255)
CYAN = (34, 211, 238)
WHITE = (248, 250, 255)


def lerp(a, b, t):
    return tuple(a[i] + (b[i] - a[i]) * t for i in range(3))


MARGIN = 78 * k
RADIUS = 232 * k


def rr_sdf(x, y):
    hw = (W - 2 * MARGIN) / 2
    hh = (H - 2 * MARGIN) / 2
    px = abs(x - W / 2) - (hw - RADIUS)
    py = abs(y - H / 2) - (hh - RADIUS)
    dx, dy = max(px, 0), max(py, 0)
    return math.hypot(dx, dy) + min(max(px, py), 0) - RADIUS


# Funnel (filtering) + three items dropping into it, in design units.
FUNNEL = [(250, 322), (750, 322), (590, 560), (590, 770),
          (410, 770), (410, 560)]
FUNNEL = [(x * k, y * k) for x, y in FUNNEL]
DOTS = [((362, 250), 46), ((500, 226), 50), ((638, 250), 46)]
DOTS = [((cx * k, cy * k), r * k) for (cx, cy), r in DOTS]


def in_poly(x, y, poly):
    inside = False
    j = len(poly) - 1
    for i in range(len(poly)):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


img = bytearray(W * H * 4)
for y in range(H):
    for x in range(W):
        d = rr_sdf(x, y)
        a = max(0.0, min(1.0, 0.5 - d))
        if a <= 0:
            continue
        t = (x + y) / (W + H)
        r, g, b = lerp(INDIGO, CYAN, t)
        hl = max(0.0, 1 - y / (H * 0.85)) * 0.12
        r += (255 - r) * hl
        g += (255 - g) * hl
        b += (255 - b) * hl
        white = in_poly(x, y, FUNNEL)
        if not white:
            for (cx, cy), rad in DOTS:
                if (x - cx) ** 2 + (y - cy) ** 2 <= rad * rad:
                    white = True
                    break
        if white:
            r, g, b = WHITE
        i = (y * W + x) * 4
        img[i], img[i + 1], img[i + 2], img[i + 3] = int(r), int(g), int(b), int(a * 255)

# Downsample SSxSS -> 1 (premultiplied average).
out = bytearray(S * S * 4)
for oy in range(S):
    for ox in range(S):
        r = g = b = al = 0
        for dy in range(SS):
            base = ((oy * SS + dy) * W + ox * SS) * 4
            for dx in range(SS):
                i = base + dx * 4
                a = img[i + 3]
                r += img[i] * a
                g += img[i + 1] * a
                b += img[i + 2] * a
                al += a
        oi = (oy * S + ox) * 4
        if al:
            out[oi], out[oi + 1], out[oi + 2] = r // al, g // al, b // al
        out[oi + 3] = al // (SS * SS)


def chunk(t, d):
    c = t + d
    return struct.pack(">I", len(d)) + c + struct.pack(">I", zlib.crc32(c) & 0xffffffff)


raw = bytearray()
for y in range(S):
    raw.append(0)
    raw += out[y * S * 4:(y + 1) * S * 4]
png = (b"\x89PNG\r\n\x1a\n"
       + chunk(b"IHDR", struct.pack(">IIBBBBB", S, S, 8, 6, 0, 0, 0))
       + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
       + chunk(b"IEND", b""))
open(OUT, "wb").write(png)
print("png", S, "written ->", OUT)
