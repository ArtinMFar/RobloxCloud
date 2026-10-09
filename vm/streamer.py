#!/usr/bin/env python3
"""Streams the Roblox window to a browser and plays the browser's input back.

Runs on the Colab VM as root, next to Xvfb and Cordial (which runs as the
`player` user). Started by vm/setup.py and reached through a Cloudflare quick
tunnel.

Video and audio: ffmpeg grabs the Roblox window from the X display and the
player's PulseAudio sink, encodes MPEG-1 video and MP2 audio in an MPEG-TS
stream, and every connected /video websocket gets the bytes. The page decodes
them with JSMpeg, which works the same in desktop and mobile browsers.

Input arrives on the /ctl websocket as small JSON messages:

- PC mode goes through XTest, exactly like a real keyboard and mouse, so
  Cordial's own keyboard and mouse handling applies (WASD, mouse look, chat).
- Mobile mode sends each finger into Cordial's devctl socket as a touch
  contact, so Roblox draws its own thumbstick and jump button and takes the
  touches directly. Keyboard keys are not forwarded in this mode; text from the
  phone's on-screen keyboard is typed into the focused text box instead.

Both websockets require ?k=<session key>; only its SHA-256 is known here.
"""

import argparse
import asyncio
import collections
import hashlib
import hmac
import json
import os
import shlex
import signal
import subprocess
import sys
import time
import urllib.parse

from aiohttp import WSMsgType, web
from Xlib import X, XK
from Xlib import display as xdisplay
from Xlib import error as xerror
from Xlib.ext import xtest

# Browser KeyboardEvent.code -> X keysym name. Physical keys, so WASD stays WASD
# on any keyboard layout; X's own US keymap turns Shift+key into the right
# character for chat.
KEYS = {
    "Space": "space", "Enter": "Return", "NumpadEnter": "KP_Enter", "Escape": "Escape",
    "Backspace": "BackSpace", "Tab": "Tab", "Delete": "Delete", "Insert": "Insert",
    "Home": "Home", "End": "End", "PageUp": "Prior", "PageDown": "Next",
    "ArrowUp": "Up", "ArrowDown": "Down", "ArrowLeft": "Left", "ArrowRight": "Right",
    "ShiftLeft": "Shift_L", "ShiftRight": "Shift_R", "ControlLeft": "Control_L",
    "ControlRight": "Control_R", "AltLeft": "Alt_L", "AltRight": "Alt_R",
    "CapsLock": "Caps_Lock", "Minus": "minus", "Equal": "equal",
    "BracketLeft": "bracketleft", "BracketRight": "bracketright", "Backslash": "backslash",
    "Semicolon": "semicolon", "Quote": "apostrophe", "Backquote": "grave", "Comma": "comma",
    "Period": "period", "Slash": "slash",
}
KEYS.update({f"Key{c}": c.lower() for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"})
KEYS.update({f"Digit{d}": str(d) for d in range(10)})
KEYS.update({f"Numpad{d}": f"KP_{d}" for d in range(10)})
KEYS.update({f"F{n}": f"F{n}" for n in range(1, 13)})

# Linux evdev codes for the few keys the phone's on-screen keyboard sends as keys.
EVDEV = {"Backspace": 14, "Enter": 28, "Space": 57, "Tab": 15, "Escape": 1}


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


class Devctl:
    """A line-oriented client for Cordial's development control socket.

    One reply line comes back per command, in order, so replies are matched to
    commands by a queue: `send` ignores its reply, `ask` waits for it.
    """

    def __init__(self, path):
        self.path = path
        self.writer = None
        self.multi_touch = None  # unknown until probed
        self.editor = None  # whether the `editor` verb exists; unknown until asked
        self.on_probe = None
        self.lock = asyncio.Lock()
        self.pending = collections.deque()

    async def _connect(self):
        if self.writer and not self.writer.is_closing():
            return True
        try:
            reader, self.writer = await asyncio.open_unix_connection(self.path)
        except OSError:
            self.writer = None
            return False
        self._fail_pending()
        asyncio.get_running_loop().create_task(self._drain(reader))
        self.multi_touch = None
        # The first reply says whether this is the multi-touch build: the
        # unpatched devctl answers "err unknown verb" to `touch`.
        self.pending.append("probe")
        self.writer.write(b"touch cancel\n")
        await self.writer.drain()
        return True

    def _fail_pending(self):
        while self.pending:
            fut = self.pending.popleft()
            if isinstance(fut, asyncio.Future) and not fut.done():
                fut.set_result(None)

    async def _drain(self, reader):
        while True:
            line = await reader.readline()
            if not line:
                self._fail_pending()
                return
            text = line.decode(errors="replace").strip()
            waiter = self.pending.popleft() if self.pending else None
            if waiter == "probe":
                self.multi_touch = text == "ok"
                log(f"devctl: multi-touch {'available' if self.multi_touch else 'NOT available (single finger)'}")
                if self.on_probe:
                    self.on_probe()
            elif isinstance(waiter, asyncio.Future):
                if not waiter.done():
                    waiter.set_result(text)
            elif text.startswith("err"):
                log("devctl:", text)

    async def _write(self, line, waiter):
        async with self.lock:
            for _ in range(2):
                if not await self._connect():
                    return False
                try:
                    self.pending.append(waiter)
                    self.writer.write((line + "\n").encode())
                    await self.writer.drain()
                    return True
                except (ConnectionError, OSError):
                    self.writer = None
            return False

    async def send(self, line):
        return await self._write(line, None)

    async def ask(self, line, timeout=2.0):
        fut = asyncio.get_running_loop().create_future()
        if not await self._write(line, fut):
            return None
        try:
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            return None

    def reset(self):
        if self.writer:
            self.writer.close()
        self.writer = None
        self._fail_pending()


def parse_editor(reply):
    """`editor`'s reply (patches/cordial-devctl-multitouch.patch) as a dict."""
    fields = dict(part.split("=", 1) for part in reply.split()[1:] if "=" in part)
    if fields.get("focus") != "1":
        return {"focus": False}
    out = {"focus": True, "text": urllib.parse.unquote(fields.get("text", "")),
           "caret": int(fields.get("caret", "0") or 0)}
    if fields.get("x", "none") != "none":
        for k in ("x", "y", "w", "h", "size"):
            out[k] = float(fields.get(k, "0"))
        out["password"] = fields.get("password") == "1"
        out["multiline"] = fields.get("multiline", "0") != "0"
        out["xalign"] = int(fields.get("xalign", "0"))
    return out


class Screen:
    """The X display: finds the Roblox window and injects keyboard and mouse."""

    def __init__(self, name, width, height):
        self.d = xdisplay.Display(name)
        self.root = self.d.screen().root
        self.w, self.h = width, height
        self.roblox = None  # the game's window id
        self.windows = []  # mapped top-level windows, bottom to top: (id, x, y, w, h)
        self.last_abs = None
        self.last_rel = 0.0
        self.locked = False
        self.unlock_votes = 0
        self.held_keys = set()
        self.held_buttons = set()

    def sync(self):
        self.d.sync()

    def scan(self):
        """Refresh the window list and keep the game window at 0,0 at the stream size."""
        found, roblox = [], None
        for win in self.root.query_tree().children:
            try:
                attrs = win.get_attributes()
                if attrs.map_state != X.IsViewable:
                    continue
                g = win.get_geometry()
                wm_class = win.get_wm_class() or ()
            except xerror.XError:
                continue
            found.append((win.id, g.x, g.y, g.width, g.height))
            # Cordial's game window carries WM_CLASS class "Cordial"
            # (cordial-runtime's window.rs); its launcher, also titled
            # "Cordial <version>", is GTK's "cordial".
            if len(wm_class) == 2 and wm_class[1] == "Cordial":
                roblox = win
        self.windows = found
        if roblox is None:
            self.roblox = None
            return
        if self.roblox != roblox.id:
            log(f"game window 0x{roblox.id:x}")
            self.roblox = roblox.id
            roblox.set_input_focus(X.RevertToParent, X.CurrentTime)
        g = roblox.get_geometry()
        if (g.x, g.y, g.width, g.height) != (0, 0, self.w, self.h):
            roblox.configure(x=0, y=0, width=self.w, height=self.h)
            roblox.raise_window()
        self.sync()

    def window_at(self, x, y):
        for wid, wx, wy, ww, wh in reversed(self.windows):
            if wx <= x < wx + ww and wy <= y < wy + wh:
                return wid
        return None

    def on_game(self, x, y):
        top = self.window_at(x, y)
        return top is None or top == self.roblox

    def focus_at(self, x, y):
        # Click-to-focus, which a window manager would otherwise do.
        wid = self.window_at(x, y)
        if wid:
            try:
                self.d.create_resource_object("window", wid).set_input_focus(X.RevertToParent, X.CurrentTime)
            except xerror.XError:
                pass

    def move(self, x, y):
        x, y = int(x), int(y)
        self.last_abs = (x, y)
        xtest.fake_input(self.d, X.MotionNotify, x=x, y=y)
        self.sync()

    def move_rel(self, dx, dy):
        self.last_rel = time.monotonic()
        # python-xlib: detail=True makes MotionNotify relative.
        xtest.fake_input(self.d, X.MotionNotify, detail=True, x=int(dx), y=int(dy))
        self.sync()

    def button(self, b, down):
        if down:
            self.held_buttons.add(b)
        else:
            self.held_buttons.discard(b)
        xtest.fake_input(self.d, X.ButtonPress if down else X.ButtonRelease, b)
        self.sync()

    def wheel(self, dy):
        b = 4 if dy < 0 else 5
        for _ in range(min(5, max(1, round(abs(dy) / 100)))):
            xtest.fake_input(self.d, X.ButtonPress, b)
            xtest.fake_input(self.d, X.ButtonRelease, b)
        self.sync()

    def key(self, code, down):
        name = KEYS.get(code)
        if not name:
            return
        kc = self.d.keysym_to_keycode(XK.string_to_keysym(name))
        if not kc:
            return
        if down:
            self.held_keys.add(kc)
        elif kc not in self.held_keys:
            return  # a release for a press that never reached X (pressed before focus)
        else:
            self.held_keys.discard(kc)
        xtest.fake_input(self.d, X.KeyPress if down else X.KeyRelease, kc)
        self.sync()

    def release_all(self):
        for kc in list(self.held_keys):
            xtest.fake_input(self.d, X.KeyRelease, kc)
        for b in list(self.held_buttons):
            xtest.fake_input(self.d, X.ButtonRelease, b)
        self.held_keys.clear()
        self.held_buttons.clear()
        self.sync()

    def type_text(self, text):
        # For a non-game window (a captcha page) in mobile mode.
        subprocess.run(["xdotool", "type", "--delay", "20", "--", text],
                       env={**os.environ, "DISPLAY": self.d.get_display_name()})

    def poll_lock(self):
        """Whether the game holds the mouse (mouse lock / first person / shift lock).

        Cordial's X11 pointer lock grabs the pointer and warps it back to the
        window centre after every motion; when it lets go it puts the pointer
        back where it was. So the pointer sitting on the centre while the last
        absolute move was elsewhere means locked, and the pointer staying off
        centre with no relative motion going on means unlocked.
        """
        p = self.root.query_pointer()
        cx, cy = self.w // 2, self.h // 2
        at_centre = abs(p.root_x - cx) <= 1 and abs(p.root_y - cy) <= 1
        if not self.locked:
            if at_centre and self.last_abs and (abs(self.last_abs[0] - cx) > 3 or abs(self.last_abs[1] - cy) > 3):
                self.locked = True
                self.unlock_votes = 0
        else:
            if not at_centre and time.monotonic() - self.last_rel > 0.3:
                self.unlock_votes += 1
                if self.unlock_votes >= 2:
                    self.locked = False
                    self.last_abs = (p.root_x, p.root_y)
            else:
                self.unlock_votes = 0
        return self.locked


class Video:
    """One ffmpeg, many viewers."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.viewers = set()
        self.proc = None
        self.audio = True
        self.running = True

    def command(self):
        c = self.cfg
        w, h, fps, br = c["width"], c["height"], c["fps"], c["bitrate"]
        if c.get("capture") == "x11grab":
            prefix = ""
            video_in = ["-thread_queue_size", "64", "-f", "x11grab", "-draw_mouse", "0", "-framerate", str(fps),
                        "-video_size", f"{w}x{h}", "-i", f"{c['display']}.0+0,0"]
        else:
            # No MIT-SHM on this display (see vm/setup.py), and ffmpeg's x11grab
            # gets no frames without it, so frames come from vm/xcapture.py.
            prefix = shlex.join([sys.executable, c["xcapture"], c["display"], "0", "0", str(w), str(h), str(fps)]) + " | "
            video_in = ["-thread_queue_size", "64", "-use_wallclock_as_timestamps", "1", "-f", "rawvideo",
                        "-pix_fmt", "bgr0", "-video_size", f"{w}x{h}", "-framerate", str(fps), "-i", "pipe:0"]
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin"] + video_in
        if self.audio:
            cmd += ["-thread_queue_size", "1024", "-f", "pulse", "-fragment_size", "4096",
                    "-i", c.get("audio_source") or "default"]
        cmd += ["-f", "mpegts", "-c:v", "mpeg1video", "-fps_mode", "cfr", "-r", str(fps),
                "-b:v", f"{br}k", "-maxrate", f"{br}k", "-bufsize", f"{max(br // 2, 500)}k", "-g", str(fps), "-bf", "0"]
        if self.audio:
            cmd += ["-c:a", "mp2", "-b:a", "128k", "-ar", "44100", "-ac", "2"]
        cmd += ["-flush_packets", "1", "-muxdelay", "0.001", "pipe:1"]
        # As the player, for its PulseAudio; Xvfb lets local users in.
        return ["runuser", "-u", c["user"], "--", c["as_player"], "sh", "-c", prefix + shlex.join(cmd)]

    async def run(self):
        failures = 0
        while self.running:
            started = time.monotonic()
            log("video:", self.command()[-1])
            self.proc = await asyncio.create_subprocess_exec(
                *self.command(), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                start_new_session=True)
            err_task = asyncio.create_task(self.proc.stderr.read())
            while True:
                chunk = await self.proc.stdout.read(32768)
                if not chunk:
                    break
                for q in list(self.viewers):
                    if q.qsize() < 64:
                        q.put_nowait(chunk)
                    # else: a viewer that cannot keep up skips data and
                    # resynchronises on the next keyframe.
            await self.proc.wait()
            try:
                os.killpg(self.proc.pid, signal.SIGKILL)  # whatever of the pipeline is left
            except ProcessLookupError:
                pass
            err = (await err_task).decode(errors="replace").strip()
            if not self.running:
                return
            log(f"ffmpeg exited {self.proc.returncode}: {err[-500:]}")
            if time.monotonic() - started < 5:
                failures += 1
                if self.audio and failures >= 2:
                    log("ffmpeg keeps failing with audio; streaming video only")
                    self.audio = False
                    failures = 0
            else:
                failures = 0
            await asyncio.sleep(min(5, 0.5 * (failures + 1)))

    def stop(self):
        self.running = False
        if self.proc and self.proc.returncode is None:
            try:
                os.killpg(self.proc.pid, signal.SIGKILL)  # the capture and ffmpeg with it
            except ProcessLookupError:
                pass


class Streamer:
    def __init__(self, cfg):
        self.cfg = cfg
        self.mode = cfg["mode"]
        self.key_hash = cfg["key_hash"]
        self.screen = Screen(cfg["display"], cfg["width"], cfg["height"])
        cfg.setdefault("capture", "x11grab" if "MIT-SHM" in self.screen.d.list_extensions() else "xgetimage")
        cfg.setdefault("xcapture", os.path.join(os.path.dirname(os.path.abspath(__file__)), "xcapture.py"))
        log("capture:", cfg["capture"])
        self.devctl = Devctl(cfg["devctl"])
        self.devctl.on_probe = lambda: asyncio.get_running_loop().create_task(self.broadcast(self.state()))
        self.video = Video(cfg)
        self.ctl_clients = set()
        self.last_client = time.time()
        self.stopped = False
        self.restarting = False
        self.last_editor = None
        self.fingers = {}  # browser pointer id -> ("game"|"x", contact id)
        self.x_finger = None  # the browser pointer driving the X mouse in mobile mode

    # ---------------------------------------------------------------- auth
    def authorised(self, request):
        k = request.query.get("k", "")
        digest = hashlib.sha256(k.encode()).hexdigest()
        return bool(k) and hmac.compare_digest(digest, self.key_hash)

    def state(self):
        return {
            "t": "state", "mode": self.mode, "w": self.cfg["width"], "h": self.cfg["height"],
            "game": self.screen.roblox is not None, "multiTouch": self.devctl.multi_touch,
            "locked": self.screen.locked, "stopped": self.stopped, "restarting": self.restarting,
            "gpu": self.cfg.get("gpu"),
        }

    async def broadcast(self, msg):
        data = json.dumps(msg)
        for ws in list(self.ctl_clients):
            try:
                await ws.send_str(data)
            except ConnectionError:
                pass

    # ------------------------------------------------------------- handlers
    async def health(self, request):
        return web.json_response({"ok": True, "game": self.screen.roblox is not None,
                                  "stopped": self.stopped},
                                 headers={"Access-Control-Allow-Origin": "*", "Cache-Control": "no-store"})

    async def video_ws(self, request):
        if not self.authorised(request):
            return web.Response(status=403, text="bad key")
        ws = web.WebSocketResponse(max_msg_size=1 << 16, heartbeat=20)
        await ws.prepare(request)
        q = asyncio.Queue()
        self.video.viewers.add(q)
        log("video viewer connected")

        async def pump():
            while True:
                chunk = await q.get()
                # Coalesce what queued up, so a slow link sends fewer frames.
                while not q.empty() and len(chunk) < 262144:
                    chunk += q.get_nowait()
                await ws.send_bytes(chunk)

        sender = asyncio.create_task(pump())
        try:
            async for _ in ws:
                pass
        finally:
            sender.cancel()
            self.video.viewers.discard(q)
            log("video viewer left")
        return ws

    async def ctl_ws(self, request):
        if not self.authorised(request):
            return web.Response(status=403, text="bad key")
        ws = web.WebSocketResponse(max_msg_size=1 << 16, heartbeat=20)
        await ws.prepare(request)
        self.ctl_clients.add(ws)
        self.last_client = time.time()
        log("controller connected")
        await ws.send_str(json.dumps(self.state()))
        self.last_editor = None  # resend it to the new page
        try:
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    continue
                self.last_client = time.time()
                try:
                    m = json.loads(msg.data)
                    reply = await self.handle(m)
                except Exception as e:  # one bad message must not drop the session
                    log("input error:", repr(e), msg.data[:200])
                    continue
                if reply:
                    await ws.send_str(json.dumps(reply))
        finally:
            self.ctl_clients.discard(ws)
            self.last_client = time.time()
            await self.lift_everything()
            log("controller left")
        return ws

    async def lift_everything(self):
        # A page that vanished mid-gesture must not leave a key or finger down.
        self.screen.release_all()
        if self.fingers:
            await self.devctl.send("touch cancel" if self.devctl.multi_touch else "up 0 0")
            self.fingers.clear()
        self.x_finger = None

    async def handle(self, m):
        t = m.get("t")
        s = self.screen
        W, H = self.cfg["width"], self.cfg["height"]

        def px(m):
            return (min(max(float(m.get("x", 0)), 0.0), 1.0) * (W - 1),
                    min(max(float(m.get("y", 0)), 0.0), 1.0) * (H - 1))

        if t == "ping":
            return {"t": "pong", "ts": m.get("ts")}
        if self.stopped:
            return None
        if t == "mode":
            if m.get("mode") in ("pc", "mobile") and m["mode"] != self.mode:
                await self.lift_everything()
                self.mode = m["mode"]
                log("mode:", self.mode)
                await self.broadcast(self.state())
            return None
        if t == "stop":
            await self.stop("stopped from the page")
            return None
        if t == "restart":
            asyncio.create_task(self.restart_game())
            return None
        if t == "join":
            place = str(m.get("place", "")).strip()
            if place.isdigit():
                await self.devctl.send(f"joinplace {place}")
            return None

        # ---- PC: a real keyboard and mouse, through X
        if t == "mm":
            x, y = px(m)
            s.move(x, y)
        elif t == "mr":
            s.move_rel(float(m.get("dx", 0)), float(m.get("dy", 0)))
        elif t == "mb":
            b = {0: 1, 1: 2, 2: 3}.get(int(m.get("b", 0)), 1)
            if "x" in m and not s.locked:
                x, y = px(m)
                s.move(x, y)
                if m.get("d"):
                    s.focus_at(x, y)
            s.button(b, bool(m.get("d")))
        elif t == "wh":
            s.wheel(float(m.get("dy", 0)))
        elif t == "rel":  # the page lost focus: let go of every key and button
            s.release_all()
        elif t == "k":
            # Keyboard keys only in PC mode: mobile mode turns the mapping off,
            # so the game sees a phone. Repeats come from the browser (X's own
            # auto-repeat is off), as presses without releases.
            if self.mode == "pc":
                s.key(m.get("code", ""), bool(m.get("d")))

        # ---- Mobile: fingers into the game, a finger on any other window is a mouse
        elif t == "td":
            await self.finger_down(int(m["id"]), *px(m))
        elif t == "tm":
            for pid, x, y in m.get("p", []):
                await self.finger_move(int(pid), *px({"x": x, "y": y}))
        elif t == "tu":
            await self.finger_up(int(m["id"]))
        elif t == "tc":
            await self.lift_everything()

        # ---- Text from the phone's on-screen keyboard
        elif t == "text":
            text = str(m.get("s", ""))[:500]
            if not text:
                return None
            if self.mode == "pc" or self.x_finger is not None or s.roblox is None:
                s.type_text(text)
            elif text.strip() == "":
                for _ in text:
                    await self.devctl.send(f"tap {EVDEV['Space']}")
            else:
                await self.devctl.send("text " + text.replace("\n", " "))
        elif t == "key":
            code = m.get("k")
            if self.mode == "mobile" and self.x_finger is None and code in EVDEV:
                await self.devctl.send(f"tap {EVDEV[code]}")
            elif code in KEYS:
                s.key(code, True)
                s.key(code, False)
        return None

    async def finger_down(self, pid, x, y):
        s = self.screen
        if not s.on_game(x, y):
            # A captcha page or dialog: Cordial's touch path only reaches the
            # game, so the first finger here works it like a mouse.
            if self.x_finger is None:
                self.x_finger = pid
                self.fingers[pid] = ("x", 0)
                s.move(x, y)
                s.focus_at(x, y)
                s.button(1, True)
            return
        if self.devctl.multi_touch is False:
            if any(kind == "game" for kind, _ in self.fingers.values()):
                return  # unpatched Cordial: one finger only
            self.fingers[pid] = ("game", 0)
            await self.devctl.send(f"down {x:.1f} {y:.1f}")
            return
        used = {cid for kind, cid in self.fingers.values() if kind == "game"}
        cid = next(i for i in range(64) if i not in used)
        self.fingers[pid] = ("game", cid)
        await self.devctl.send(f"touch down {cid} {x:.1f} {y:.1f}")

    async def finger_move(self, pid, x, y):
        f = self.fingers.get(pid)
        if not f:
            return
        kind, cid = f
        if kind == "x":
            self.screen.move(x, y)
        elif self.devctl.multi_touch is False:
            await self.devctl.send(f"move {x:.1f} {y:.1f}")
        else:
            await self.devctl.send(f"touch move {cid} {x:.1f} {y:.1f}")

    async def finger_up(self, pid):
        f = self.fingers.pop(pid, None)
        if not f:
            return
        kind, cid = f
        if kind == "x":
            self.screen.button(1, False)
            self.x_finger = None
        elif self.devctl.multi_touch is False:
            p = self.screen.root.query_pointer()
            await self.devctl.send(f"up {p.root_x} {p.root_y}")
        else:
            await self.devctl.send(f"touch up {cid} 0 0")

    # ------------------------------------------------------------ lifecycle
    async def restart_game(self):
        if self.restarting or self.stopped:
            return
        self.restarting = True
        await self.broadcast(self.state())
        self.devctl.reset()
        self.video.audio = True  # the restart takes PulseAudio down and up again
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, self.cfg["launcher"], "--restart",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
            out, _ = await proc.communicate()
            log("restart:", out.decode(errors="replace")[-800:])
        finally:
            self.restarting = False
            await self.broadcast(self.state())

    async def stop(self, why):
        if self.stopped:
            return
        log("stopping:", why)
        self.stopped = True
        self.video.stop()
        subprocess.run(["pkill", "-KILL", "-u", self.cfg["user"]])
        self.write_state()
        await self.broadcast({"t": "stopped", "why": why})

    def write_state(self):
        tmp = self.cfg["state_file"] + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"ts": time.time(), "clients": len(self.ctl_clients),
                       "last_client": self.last_client, "stopped": self.stopped,
                       "game": self.screen.roblox is not None, "mode": self.mode}, f)
        os.replace(tmp, self.cfg["state_file"])

    async def poll_editor(self):
        """Send the page the focused text box and its text whenever they change.

        Cordial's X11 backend never draws what is typed into a box (its editor
        overlay is Wayland-only), so the page draws it over the stream."""
        reply = await self.devctl.ask("editor", timeout=1.0)
        if reply is None:
            return
        if "unknown verb" in reply:
            self.devctl.editor = False
            log("devctl: no `editor` verb in this Cordial build; typed text will not show")
            return
        self.devctl.editor = True
        try:
            ed = parse_editor(reply)
        except ValueError:
            return
        if ed != self.last_editor:
            self.last_editor = ed
            await self.broadcast({"t": "editor", **ed})

    async def housekeeping(self):
        last_scan = 0.0
        last_state = 0.0
        last_editor = 0.0
        was_locked, had_game = False, None
        while True:
            now = time.monotonic()
            try:
                if now - last_scan > 1.0:
                    last_scan = now
                    self.screen.scan()
                locked = self.screen.poll_lock() if self.mode == "pc" else False
            except xerror.XError as e:
                log("X error:", e)
                locked = was_locked
            game = self.screen.roblox is not None
            if locked != was_locked or game != had_game:
                was_locked, had_game = locked, game
                await self.broadcast(self.state())
            if self.ctl_clients and game and self.devctl.editor is not False and now - last_editor > 0.15:
                last_editor = now
                await self.poll_editor()
            if now - last_state > 5:
                last_state = now
                self.write_state()
                if self.devctl.multi_touch is None and game:
                    await self.devctl.send("ping")
            await asyncio.sleep(0.15)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("config")
    cfg = json.load(open(ap.parse_args().config))
    st = Streamer(cfg)

    app = web.Application()
    app.router.add_get("/health", st.health)
    app.router.add_get("/video", st.video_ws)
    app.router.add_get("/ctl", st.ctl_ws)
    app.router.add_get("/", lambda r: web.Response(text="RobloxCloud streamer\n"))

    async def start(app):
        loop = asyncio.get_running_loop()
        app["tasks"] = [loop.create_task(st.video.run()), loop.create_task(st.housekeeping())]
        def leave():  # SystemExit from a loop callback ends run_app, as aiohttp's own handler does
            raise web.GracefulExit()

        async def on_sigterm():
            # Leave the game alone: a restarted streamer picks it up again.
            st.video.stop()
            loop.call_soon(leave)

        loop.add_signal_handler(signal.SIGTERM, lambda: loop.create_task(on_sigterm()))

    app.on_startup.append(start)
    log(f"listening on 127.0.0.1:{cfg['port']} mode={cfg['mode']} {cfg['width']}x{cfg['height']}")
    try:
        web.run_app(app, host="127.0.0.1", port=cfg["port"], print=None, access_log=None)
    finally:
        st.video.stop()


if __name__ == "__main__":
    main()
