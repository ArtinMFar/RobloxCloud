# RobloxCloud: turns a fresh Colab GPU VM into a Roblox machine and starts streaming it.
#
# Runs inside the Colab kernel through `colab exec`. scripts/session.py writes this
# directory to /content/rc and defines RC = {...} (the session's settings) first.
#
# The GPU part is the notebook in ArtinMFar/7uiii88 (commit 55d13bc): Colab ships
# NVIDIA's graphics libraries but registers no Vulkan driver and has no
# /dev/nvidia-modeset, so both are added here and Roblox renders on the GPU rather
# than on the CPU (lavapipe).
#
# Progress goes to stdout as "@@STATUS <phase> <message>" and ends with
# "@@READY <json>" or "@@STATUS error <message>".
import concurrent.futures
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.request

D = "/content/rc"
TMP = "/tmp/rc"
DISPLAY = ":99"
CORDIAL_DEB = "https://github.com/luohoa97/cordial/releases/download/v0.27.0/cordial_0.27.0-1_amd64.deb"
CLOUDFLARED = "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64"
cfg = dict(RC)  # noqa: F821 -- defined by scripts/session.py
W, H = int(cfg["width"]), int(cfg["height"])


def status(phase, msg):
    print(f"@@STATUS {phase} {msg}", flush=True)


def fail(msg):
    status("error", msg)
    raise SystemExit(1)


def sh(cmd, timeout=900):
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout,
                       stdin=subprocess.DEVNULL)
    return r.returncode, (r.stdout + r.stderr).strip()


def bg(cmd, log):
    return subprocess.Popen(cmd, shell=True, stdout=open(log, "w"), stderr=subprocess.STDOUT,
                            stdin=subprocess.DEVNULL, start_new_session=True)


def fetch(url, dest):
    req = urllib.request.Request(url, headers={"User-Agent": "RobloxCloud"})
    with urllib.request.urlopen(req, timeout=120) as r, open(dest, "wb") as f:
        while chunk := r.read(1 << 20):
            f.write(chunk)
    return dest


os.makedirs(TMP, exist_ok=True)
os.chmod(TMP, 0o1777)
os.environ["DISPLAY"] = DISPLAY

# ------------------------------------------------------------------ the GPU
status("vm", "Checking the GPU")
rc, out = sh("nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv,noheader")
print(out, flush=True)
if rc != 0:
    fail("This Colab VM has no GPU")
gpu_name = out.splitlines()[0].split(",")[0].strip()
cpus = os.cpu_count()
print(f"{cpus} CPUs", flush=True)

# ---------------------------------------------------------------- installing
status("install", "Installing Cordial, a virtual display and the streaming tools")
with concurrent.futures.ThreadPoolExecutor(4) as pool:
    deb = pool.submit(fetch, CORDIAL_DEB, f"{D}/cordial_0.27.0-1_amd64.deb")
    tunnel = pool.submit(fetch, CLOUDFLARED, "/usr/local/bin/cloudflared")
    patched = None
    if not os.path.exists(f"{D}/cordial-run-multitouch") and cfg.get("cordial_run_url"):
        patched = pool.submit(fetch, cfg["cordial_run_url"], f"{D}/cordial-run-multitouch")
    pip = pool.submit(sh, f"{sys.executable} -m pip install -q aiohttp python-xlib", 600)
    for fut, what in ((deb, "Cordial"), (tunnel, "cloudflared")):
        try:
            fut.result()
        except Exception as e:
            fail(f"Could not download {what}: {e}")
    os.chmod("/usr/local/bin/cloudflared", 0o755)
    rc, out = sh("export DEBIAN_FRONTEND=noninteractive; apt-get update -qq >/dev/null 2>&1; "
                 f"apt-get install -y -qq {D}/cordial_0.27.0-1_amd64.deb mesa-vulkan-drivers vulkan-tools "
                 "xvfb x11-utils x11-xserver-utils xdotool imagemagick dbus-x11 gnome-keyring "
                 "pulseaudio pulseaudio-utils ffmpeg > /tmp/rc/apt.log 2>&1", 1200)
    if rc != 0:
        fail("Installing packages failed: " + open("/tmp/rc/apt.log").read()[-300:])
    rc, out = pip.result()
    if rc != 0:
        fail("pip install failed: " + out[-300:])
    if patched:
        try:
            patched.result()
        except Exception as e:
            print(f"no multi-touch build of cordial-run ({e}); phones get one finger", flush=True)

# Cordial's devctl socket takes one finger at a time; the patched loader
# (patches/cordial-devctl-multitouch.patch) adds per-finger contacts.
mt = f"{D}/cordial-run-multitouch"
if os.path.exists(mt) and os.path.getsize(mt) > 1_000_000:
    want = cfg.get("cordial_run_sha256")
    got = hashlib.sha256(open(mt, "rb").read()).hexdigest()
    if want and want != got:
        print(f"cordial-run-multitouch checksum mismatch ({got}); not using it", flush=True)
    else:
        sh(f"install -m755 {mt} /usr/bin/cordial-run")
        print("installed the multi-touch cordial-run", flush=True)

# ---------------------------------------------------- GPU rendering (Vulkan)
status("render", f"Turning on GPU rendering on the {gpu_name}")
os.makedirs("/etc/vulkan/icd.d", exist_ok=True)
json.dump({"file_format_version": "1.0.1",
           "ICD": {"library_path": "/usr/lib64-nvidia/libGLX_nvidia.so.0", "api_version": "1.4.312"}},
          open("/etc/vulkan/icd.d/nvidia_icd.json", "w"), indent=2)
sh("echo /usr/lib64-nvidia > /etc/ld.so.conf.d/zz-colab-nvidia.conf && ldconfig 2>/dev/null; true")
# Without /dev/nvidia-modeset a non-root client's present-mode query fails with
# VK_ERROR_UNKNOWN and nothing is drawn; the driver only creates it for root.
sh("[ -e /dev/nvidia-modeset ] || mknod -m 666 /dev/nvidia-modeset c 195 254")

# ------------------------------------------------- display, user and sound
status("display", "Starting a virtual display")
if sh("pgrep -x Xvfb")[0] != 0:
    # No MIT-SHM: a non-root client cannot attach to root's X shared memory here,
    # and its windows stay black.
    bg(f"Xvfb {DISPLAY} -screen 0 {max(1600, W)}x{max(900, H)}x24 +extension GLX +render "
       "-extension MIT-SHM -noreset", "/tmp/rc/xvfb.log")
    for _ in range(30):
        if sh("xdpyinfo >/dev/null 2>&1")[0] == 0:
            break
        time.sleep(0.5)
sh("xhost +local:")
# Key repeats come from the browser; X's own would add release/press pairs to held keys.
sh("xset r off")

sh("id player >/dev/null 2>&1 || useradd -m -s /bin/bash player; "
   "u=$(id -u player); mkdir -p /run/user/$u; chown player:player /run/user/$u; chmod 700 /run/user/$u")
uid = int(sh("id -u player")[1])
open(f"{D}/touch", "w").write("1" if cfg["mode"] == "mobile" else "0")
open(f"{D}/as_player.sh", "w").write(r'''#!/bin/bash
# Runs a command as if from the player's desktop session.
export DISPLAY=:99 XDG_RUNTIME_DIR=/run/user/$(id -u) HOME=/home/player
export DBUS_SESSION_BUS_ADDRESS=unix:path=$XDG_RUNTIME_DIR/bus
# Only NVIDIA's Vulkan driver, so Roblox renders on the GPU.
[ -f /etc/vulkan/icd.d/nvidia_icd.json ] && export VK_ICD_FILENAMES=/etc/vulkan/icd.d/nvidia_icd.json
# The streamer drives touches, text and joins through Cordial's devctl socket.
export CORDIAL_DEV_CONTROL=1 CORDIAL_DEV_CONTROL_SOCKET=$XDG_RUNTIME_DIR/devctl.sock
# Mobile mode tells Roblox this is a touchscreen, so it draws its thumbstick and
# jump button; PC mode tells it there is none.
export CORDIAL_INPUT_TOUCH=$(cat /content/rc/touch 2>/dev/null || echo 0)
# A bus killed with the game leaves its socket behind, so ask it rather than test the file.
if ! dbus-send --bus=$DBUS_SESSION_BUS_ADDRESS --print-reply --dest=org.freedesktop.DBus /org/freedesktop/DBus org.freedesktop.DBus.GetId >/dev/null 2>&1; then
  rm -f $XDG_RUNTIME_DIR/bus
  (setsid dbus-daemon --session --nofork --address=$DBUS_SESSION_BUS_ADDRESS >/tmp/rc/player-dbus.log 2>&1 &)
  for i in 1 2 3 4 5 6 7 8 9 10; do [ -S $XDG_RUNTIME_DIR/bus ] && break; sleep 0.2; done
fi
# Roblox stops when an experience starts if it cannot open an audio device.
pactl info >/dev/null 2>&1 || pulseaudio --start --exit-idle-time=-1 --log-target=file:/tmp/rc/player-pulse.log
exec "$@"
''')
os.chmod(f"{D}/as_player.sh", 0o755)
rc, out = sh(f"runuser -u player -- {D}/as_player.sh pactl get-default-sink")
audio_source = (out.strip().splitlines() or [""])[-1] + ".monitor" if rc == 0 and out.strip() else "default"
print(f"audio: {audio_source}", flush=True)

# --------------------------------------------------------------- Roblox
status("launch", "Opening Cordial")
p = subprocess.Popen([sys.executable, f"{D}/launch_roblox.py"], stdout=subprocess.PIPE,
                     stderr=subprocess.STDOUT, text=True)
for line in p.stdout:
    print(line.rstrip(), flush=True)
if p.wait() != 0:
    raise SystemExit(1)  # launch_roblox.py already printed why

# ------------------------------------------------------------ streaming
status("stream", "Starting the stream")
port = 8765
json.dump({
    "port": port, "key_hash": cfg["key_hash"], "mode": cfg["mode"], "width": W, "height": H,
    "fps": int(cfg.get("fps", 30)), "bitrate": int(cfg.get("bitrate", 3500)), "display": DISPLAY,
    "devctl": f"/run/user/{uid}/devctl.sock", "state_file": "/tmp/rc/state.json", "user": "player",
    "as_player": f"{D}/as_player.sh", "launcher": f"{D}/launch_roblox.py",
    "audio_source": audio_source, "gpu": gpu_name,
}, open("/tmp/rc/streamer.json", "w"))
# (Bracketed patterns, so pkill does not match the shell running it.)
sh("pkill -f '[/]content/rc/streamer.py'; pkill -f '[/]content/rc/xcapture.py'; pkill -x cloudflared; true")
bg(f"{sys.executable} -u {D}/streamer.py /tmp/rc/streamer.json", "/tmp/rc/streamer.log")
for _ in range(40):
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2).read()
        break
    except OSError:
        time.sleep(0.5)
else:
    fail("The streamer did not start: " + open("/tmp/rc/streamer.log").read()[-400:])

# HTTP/2 over TCP, never QUIC: Colab throttles UDP so hard that a QUIC tunnel
# carried 20 KB/s where an HTTP/2 one carried 12 MB/s, measured from the same VM.
url = None
for attempt in range(2):
    bg(f"cloudflared tunnel --no-autoupdate --protocol http2 --url http://127.0.0.1:{port}",
       "/tmp/rc/cloudflared.log")
    end = time.time() + 45
    while time.time() < end and not url:
        m = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", open("/tmp/rc/cloudflared.log").read())
        url = m.group(0) if m else None
        time.sleep(1)
    if url:
        break
    sh("pkill -x cloudflared")
if not url:
    fail("Could not open a tunnel: " + open("/tmp/rc/cloudflared.log").read()[-300:])

# A new quick tunnel can take a few seconds before its name resolves.
end = time.time() + 90
while True:
    try:
        req = urllib.request.Request(url + "/health", headers={"User-Agent": "RobloxCloud"})
        json.loads(urllib.request.urlopen(req, timeout=10).read())
        break
    except Exception as e:
        if time.time() > end:
            fail(f"The tunnel at {url} did not answer: {e}")
        time.sleep(2)

print("@@READY " + json.dumps({"url": url, "gpu": gpu_name, "cpus": cpus}), flush=True)
