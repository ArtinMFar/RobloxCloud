#!/usr/bin/env python3
"""Opens Cordial's launcher, presses "Roblox" (and "Download Roblox" on the first
run), and waits for the Roblox client to reach its first screen.

The button positions come from the notebook in ArtinMFar/7uiii88, measured on
Cordial 0.27.0's launcher (760x800) and its first-run setup window (560x520),
and are applied relative to wherever those windows actually are.

Usage: launch_roblox.py [--restart]
Prints "@@STATUS <phase> <message>" lines that scripts/session.py relays to the page.
"""
import os
import subprocess
import sys
import time

DISPLAY = os.environ.get("DISPLAY", ":99")
RC = "/content/rc"
LOG = "/tmp/rc/cordial.log"
AS_PLAYER = f"{RC}/as_player.sh"


def status(phase, msg):
    print(f"@@STATUS {phase} {msg}", flush=True)


def x(cmd):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True,
                          env={**os.environ, "DISPLAY": DISPLAY})


def find_window(name):
    """Geometry (id, x, y, w, h) of the first visible window with exactly this name."""
    r = x(f"xdotool search --onlyvisible --name '^{name}$'")
    for wid in r.stdout.split():
        g = x(f"xdotool getwindowgeometry --shell {wid}").stdout
        v = dict(line.split("=", 1) for line in g.split() if "=" in line)
        try:
            return int(wid), int(v["X"]), int(v["Y"]), int(v["WIDTH"]), int(v["HEIGHT"])
        except (KeyError, ValueError):
            continue
    return None


def wait_window(name, timeout):
    end = time.time() + timeout
    while time.time() < end:
        w = find_window(name)
        if w:
            return w
        time.sleep(1)
    return None


def click_rel(win, fx, fy):
    _, wx, wy, ww, wh = win
    x(f"xdotool mousemove {int(wx + fx * ww)} {int(wy + fy * wh)} click 1")


def log_text():
    try:
        return open(LOG, errors="ignore").read()
    except OSError:
        return ""


def wait_log(pattern, timeout):
    start = time.time()
    while time.time() < start + timeout:
        if pattern in log_text():
            return True
        if time.time() > start + 20 and subprocess.run("pgrep -u player cordial >/dev/null", shell=True).returncode:
            return False  # nothing of Cordial's is running any more
        time.sleep(2)
    return False


def screenshot(name):
    x(f"import -window root /tmp/rc/{name}.png")


def main():
    restart = "--restart" in sys.argv
    if restart:
        status("launch", "Restarting Roblox")
        subprocess.run("pkill -u player -f cordial-run; pkill -u player -f cordial-shell; sleep 2; "
                       "pkill -KILL -u player -f cordial-run; pkill -KILL -u player -f cordial-shell", shell=True)
        time.sleep(1)

    os.makedirs("/tmp/rc", exist_ok=True)
    subprocess.Popen(f"runuser -u player -- {AS_PLAYER} cordial", shell=True,
                     stdout=open(LOG, "w"), stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                     start_new_session=True)
    launcher = wait_window("Cordial", 90)
    if not launcher:
        screenshot("fail-launcher")
        status("error", "Cordial's launcher did not open")
        sys.exit(1)
    time.sleep(4)
    status("launch", "Starting Roblox")
    click_rel(launcher, 380 / 760, 432 / 800)  # "Roblox"

    setup = wait_window("Set up Roblox", 25)
    if setup:  # first run on this VM: no Roblox build yet
        time.sleep(3)
        status("download", "Downloading Roblox (about 400 MB, checked against Roblox's signature)")
        click_rel(setup, 279 / 560, 397 / 520)  # "Download Roblox"

    if not wait_log("app ready:", 900 if setup else 300):
        screenshot("fail-roblox")
        tail = log_text()[-1500:].replace("\n", " | ")
        status("error", f"Roblox did not start. Cordial log: {tail[-300:]}")
        sys.exit(1)
    gpu_line = [line for line in log_text().splitlines() if "vulkan: physical device" in line]
    if gpu_line:
        print(gpu_line[-1], flush=True)
    status("launch", "Roblox is running")


if __name__ == "__main__":
    main()
