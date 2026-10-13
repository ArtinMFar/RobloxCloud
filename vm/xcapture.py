#!/usr/bin/env python3
"""Writes a region of an X display to stdout as raw BGRx frames at a steady rate.

For displays without MIT-SHM, where ffmpeg's own x11grab gets no frames (its
non-shared-memory path ends at the first read in Ubuntu 24.04's ffmpeg 6.1).

Two ways to read a frame:

- From Xvfb's own framebuffer, when Xvfb was started with -fbdir and FBFILE is
  given: the screen is a memory-mapped XWD file, so a frame is one memcpy. The
  copy happens while this client holds the server grab, so a frame the game is
  drawing in the same moment never shows half old, half new. Holds the X
  server for well under a millisecond, where an XGetImage of 1280x720 keeps it
  busy for about 14 ms (at most ~72 frames a second, measured on Colab).
- Otherwise Xlib's XGetSubImage into one reused image, through ctypes, so a
  frame is one round trip and one write.

Usage: xcapture.py DISPLAY X Y WIDTH HEIGHT FPS [FBFILE]
"""
import ctypes
import ctypes.util
import mmap
import os
import struct
import sys
import time


class XImage(ctypes.Structure):
    _fields_ = [
        ("width", ctypes.c_int), ("height", ctypes.c_int), ("xoffset", ctypes.c_int),
        ("format", ctypes.c_int), ("data", ctypes.c_void_p), ("byte_order", ctypes.c_int),
        ("bitmap_unit", ctypes.c_int), ("bitmap_bit_order", ctypes.c_int), ("bitmap_pad", ctypes.c_int),
        ("depth", ctypes.c_int), ("bytes_per_line", ctypes.c_int), ("bits_per_pixel", ctypes.c_int),
        ("red_mask", ctypes.c_ulong), ("green_mask", ctypes.c_ulong), ("blue_mask", ctypes.c_ulong),
    ]


def open_xlib(name):
    xlib = ctypes.CDLL(ctypes.util.find_library("X11") or "libX11.so.6")
    xlib.XOpenDisplay.restype = ctypes.c_void_p
    xlib.XOpenDisplay.argtypes = [ctypes.c_char_p]
    xlib.XDefaultRootWindow.restype = ctypes.c_ulong
    xlib.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
    xlib.XGetImage.restype = ctypes.POINTER(XImage)
    xlib.XGetImage.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int, ctypes.c_int,
                               ctypes.c_uint, ctypes.c_uint, ctypes.c_ulong, ctypes.c_int]
    xlib.XGetSubImage.restype = ctypes.POINTER(XImage)
    xlib.XGetSubImage.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int, ctypes.c_int,
                                  ctypes.c_uint, ctypes.c_uint, ctypes.c_ulong, ctypes.c_int,
                                  ctypes.POINTER(XImage), ctypes.c_int, ctypes.c_int]
    for fn in (xlib.XGrabServer, xlib.XUngrabServer, xlib.XFlush):
        fn.argtypes = [ctypes.c_void_p]
    xlib.XSync.argtypes = [ctypes.c_void_p, ctypes.c_int]
    dpy = xlib.XOpenDisplay(name.encode())
    if not dpy:
        sys.exit(f"xcapture: cannot open display {name}")
    return xlib, dpy


def xgetimage_frames(xlib, dpy, x, y, w, h):
    """Yields the same buffer, refilled with the next frame, each time round."""
    root = xlib.XDefaultRootWindow(dpy)
    all_planes, zpixmap = ctypes.c_ulong(-1).value, 2
    img = xlib.XGetImage(dpy, root, x, y, w, h, all_planes, zpixmap)
    if not img:
        sys.exit("xcapture: XGetImage failed")
    im = img.contents
    if im.bits_per_pixel != 32 or im.bytes_per_line != w * 4:
        sys.exit(f"xcapture: unexpected image layout ({im.bits_per_pixel} bpp, {im.bytes_per_line} bytes a line)")
    frame = memoryview((ctypes.c_char * (w * h * 4)).from_address(im.data)).cast("B")
    while True:
        xlib.XGetSubImage(dpy, root, x, y, w, h, all_planes, zpixmap, img, 0, 0)
        yield frame


def fbdir_frames(xlib, dpy, path, x, y, w, h):
    """The same from Xvfb's -fbdir screen file (an XWD dump that Xvfb draws into)."""
    f = open(path, "rb")
    fb = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
    header = struct.unpack(">25I", fb[:100])
    header_size, depth, bpp, bpl, ncolors = header[0], header[3], header[11], header[12], header[19]
    width, height = header[4], header[5]
    if bpp != 32 or depth != 24 or x + w > width or y + h > height:
        sys.exit(f"xcapture: unexpected framebuffer ({width}x{height}, depth {depth}, {bpp} bpp)")
    start = header_size + ncolors * 12 + y * bpl + x * 4
    screen = memoryview(fb)
    frame = bytearray(w * h * 4)
    out = memoryview(frame)
    row = w * 4
    rows = [(r * row, start + r * bpl) for r in range(h)]
    while True:
        xlib.XGrabServer(dpy)
        xlib.XSync(dpy, 0)  # every request before the grab, a frame being drawn included, is done
        if bpl == row:
            out[:] = screen[start:start + row * h]
        else:
            for dst, src in rows:
                out[dst:dst + row] = screen[src:src + row]
        xlib.XUngrabServer(dpy)
        xlib.XFlush(dpy)
        yield out


def main():
    name, x, y, w, h, fps = sys.argv[1], *map(int, sys.argv[2:7])
    fbfile = sys.argv[7] if len(sys.argv) > 7 else ""
    xlib, dpy = open_xlib(name)
    if fbfile and os.access(fbfile, os.R_OK):
        frames = fbdir_frames(xlib, dpy, fbfile, x, y, w, h)
    else:
        frames = xgetimage_frames(xlib, dpy, x, y, w, h)
    out = sys.stdout.fileno()
    period = 1.0 / fps
    due = time.monotonic()
    for view in frames:
        while view:
            view = view[os.write(out, view):]
        due += period
        now = time.monotonic()
        if due > now:
            time.sleep(due - now)
        elif now - due > 4 * period:
            due = now  # fell behind (a slow consumer): skip ahead rather than burst


if __name__ == "__main__":
    try:
        main()
    except (BrokenPipeError, KeyboardInterrupt):
        pass
