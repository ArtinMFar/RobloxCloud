#!/usr/bin/env python3
"""Writes a region of an X display to stdout as raw BGRx frames at a steady rate.

For displays without MIT-SHM, where ffmpeg's own x11grab gets no frames (its
non-shared-memory path ends at the first read in Ubuntu 24.04's ffmpeg 6.1).
Uses Xlib's XGetSubImage into one reused image, through ctypes, so a frame is
one round trip and one write.

Usage: xcapture.py DISPLAY X Y WIDTH HEIGHT FPS
"""
import ctypes
import ctypes.util
import os
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


def main():
    name, x, y, w, h, fps = sys.argv[1], *map(int, sys.argv[2:7])
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

    dpy = xlib.XOpenDisplay(name.encode())
    if not dpy:
        sys.exit(f"xcapture: cannot open display {name}")
    root = xlib.XDefaultRootWindow(dpy)
    all_planes, zpixmap = ctypes.c_ulong(-1).value, 2
    img = xlib.XGetImage(dpy, root, x, y, w, h, all_planes, zpixmap)
    if not img:
        sys.exit("xcapture: XGetImage failed")
    im = img.contents
    if im.bits_per_pixel != 32 or im.bytes_per_line != w * 4:
        sys.exit(f"xcapture: unexpected image layout ({im.bits_per_pixel} bpp, {im.bytes_per_line} bytes a line)")
    frame = (ctypes.c_char * (w * h * 4)).from_address(im.data)
    out = sys.stdout.fileno()
    period = 1.0 / fps
    due = time.monotonic()
    while True:
        xlib.XGetSubImage(dpy, root, x, y, w, h, all_planes, zpixmap, img, 0, 0)
        view = memoryview(frame).cast("B")
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
