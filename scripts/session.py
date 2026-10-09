#!/usr/bin/env python3
"""RobloxCloud's GitHub Actions side, run by .github/workflows/session.yml.

  session.py login    finish the Google sign-in the page started, with the colab
                      CLI's own OAuth client, and hand the result back sealed
  session.py play     rent a Colab GPU VM (L4, or T4 when L4 is not available),
                      start Roblox on it, report the stream's address, keep the VM
                      while someone is playing, then release it
  session.py cleanup  release the VM (the workflow runs this even when cancelled)

How the page and this script talk, given that the page is static and the repo
is public:

- Progress: a commit status on the workflow's commit, context
  "roblox-cloud/<session id>", whose description is a small JSON object
  {"p": phase, "m": message, "g": gpu, "f": 1 if L4 fell back to T4}. When the
  stream is up the status's target_url is the stream's address, which is
  useless without the session key only the page has (this side gets its
  SHA-256 in KEY_HASH).
- Sign-in: the page builds the Google URL (the same one `colab` prints, with
  its own PKCE verifier) and stores the code the user pastes, with the
  verifier, as the COLAB_AUTH_CODE secret. `login` exchanges it here with the
  colab CLI's client config, seals the resulting token.json for the repo's
  Actions public key (REPO_PUBLIC_KEY, which the page passes in) and returns it
  in a check run, along with the account's email and GPU eligibility sealed for
  the page's one-time key (PAGE_PUBLIC_KEY). The page stores the first as the
  COLAB_TOKEN secret, which `play` reads. Nothing readable is left in public.
"""
import base64
import json
import os
import pathlib
import subprocess
import sys
import time
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
E = os.environ
SID = E.get("SESSION_ID", "local")
NAME = f"rc-{SID}"[:40]
STATE = pathlib.Path.home() / ".robloxcloud-session.json"
TOKEN_PATH = pathlib.Path.home() / ".config/colab-cli/token.json"
FINAL = {"stopped", "error", "signed-in"}


# --------------------------------------------------------------- GitHub
def github(method, path, body=None):
    if not E.get("GITHUB_TOKEN") or not E.get("GITHUB_REPOSITORY"):
        return None
    req = urllib.request.Request(
        f"https://api.github.com/repos/{E['GITHUB_REPOSITORY']}{path}", method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {E['GITHUB_TOKEN']}", "Accept": "application/vnd.github+json",
                 "Content-Type": "application/json"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read() or b"null")
        except Exception as e:
            print(f"(GitHub {method} {path} failed: {e})", flush=True)
            time.sleep(2 * (attempt + 1))
    return None


class Reporter:
    """Progress for the page, as a commit status (see the module docstring)."""

    def __init__(self):
        self.gpu = None
        self.fallback = 0
        self.url = None
        self.last = None

    def describe(self, phase, message):
        d = {"p": phase, "m": message, "g": self.gpu, "f": self.fallback}
        while True:
            text = json.dumps(d, separators=(",", ":"), ensure_ascii=False)
            if len(text) <= 140 or not d["m"]:
                return text
            d["m"] = d["m"][: max(0, len(d["m"]) - (len(text) - 140) - 1)] + "…"

    def __call__(self, phase, message, url=None):
        print(f"[{phase}] {message}", flush=True)
        if url:
            self.url = url
        text = self.describe(phase, message)
        if (text, self.url) == self.last:
            return
        self.last = (text, self.url)
        state = {"error": "error", "stopped": "success", "signed-in": "success"}.get(phase, "pending")
        run_url = (f"{E.get('GITHUB_SERVER_URL', 'https://github.com')}/{E.get('GITHUB_REPOSITORY')}"
                   f"/actions/runs/{E.get('GITHUB_RUN_ID')}")
        target = self.url if (self.url and phase not in FINAL) else run_url
        github("POST", f"/statuses/{E.get('GITHUB_SHA')}", {
            "state": state, "context": f"roblox-cloud/{SID}", "description": text, "target_url": target})


report = Reporter()


def seal(text, public_key_b64):
    from nacl.public import PublicKey, SealedBox
    box = SealedBox(PublicKey(base64.b64decode(public_key_b64)))
    return base64.b64encode(box.encrypt(text.encode())).decode()


# ---------------------------------------------------------------- login
def login():
    from importlib import resources

    from colab_cli import auth
    from google_auth_oauthlib.flow import InstalledAppFlow

    report("login", "Finishing the Google sign-in")
    try:
        pending = json.loads(E.get("COLAB_AUTH_CODE") or "{}")
        code, verifier = pending["code"].strip(), pending["verifier"]
    except (ValueError, KeyError):
        report("error", "No sign-in code reached the workflow: try signing in again")
        sys.exit(1)

    # The same client, scopes and landing page as `colab`'s own remote sign-in
    # (colab_cli/auth.py), with the verifier the page used to build its URL.
    client_config = json.loads(resources.files("colab_cli").joinpath("oauth_config.json").read_text())
    flow = InstalledAppFlow.from_client_config(client_config, auth.PUBLIC_SCOPES, code_verifier=verifier,
                                               autogenerate_code_verifier=False)
    flow.redirect_uri = auth.REMOTE_REDIRECT_URI
    try:
        flow.fetch_token(code=code)
    except Exception as e:
        msg = str(e)
        hint = ("The code was already used or has expired: sign in again" if "invalid_grant" in msg
                else f"Google refused the code: {msg[:90]}")
        report("error", hint)
        sys.exit(1)
    creds = flow.credentials
    if not creds.refresh_token:
        report("error", "Google returned no refresh token: sign in again and allow every permission")
        sys.exit(1)
    granted = set(flow.oauth2session.token.get("scope") or [])
    if granted and "https://www.googleapis.com/auth/colaboratory" not in granted:
        report("error", "The Colab permission was not granted: sign in again and allow it")
        sys.exit(1)
    TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_PATH.write_text(creds.to_json())
    TOKEN_PATH.chmod(0o600)

    email = None
    id_token = flow.oauth2session.token.get("id_token")
    if id_token:
        try:
            payload = id_token.split(".")[1]
            email = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))).get("email")
        except (IndexError, ValueError):
            pass

    report("login", "Checking which GPUs this Colab account can use")
    gpus = gpu_eligibility(creds)

    result = {
        "token": seal(creds.to_json(), E["REPO_PUBLIC_KEY"]),
        "key_id": E["REPO_PUBLIC_KEY_ID"],
        "info": seal(json.dumps({"email": email, "gpus": gpus}), E["PAGE_PUBLIC_KEY"]),
    }
    made = github("POST", "/check-runs", {
        "name": f"roblox-cloud-login/{SID}", "head_sha": E["GITHUB_SHA"], "status": "completed",
        "conclusion": "success",
        "output": {"title": "Google sign-in (sealed)", "summary": json.dumps(result)}})
    if not made:
        report("error", "Could not hand the sign-in back to the page")
        sys.exit(1)
    report("signed-in", "Signed in")


def gpu_eligibility(creds):
    """Colab's own answer to "which accelerators can this account get", from the
    session backend's ccu-info (the endpoint `colab usage` reads). None if it
    does not say, in which case `play` finds out by asking for L4."""
    try:
        from colab_cli import client as cc
        from google.auth.transport.requests import AuthorizedSession
        s = AuthorizedSession(creds)
        r = s.get(cc.Prod().domain + "/tun/m/ccu-info", params={"authuser": "0"}, timeout=30, headers={
            cc.ACCEPT_JSON_HEADER["key"]: cc.ACCEPT_JSON_HEADER["value"],
            cc.COLAB_CLIENT_AGENT_HEADER["key"]: cc.COLAB_CLIENT_AGENT_HEADER["value"]})
        body = r.text[len(cc.XSSI_PREFIX):] if r.text.startswith(cc.XSSI_PREFIX) else r.text
        data = json.loads(body)
    except Exception as e:
        print(f"(ccu-info failed: {e})", flush=True)
        return None
    print("ccu-info fields:", sorted(data), flush=True)  # names only; values stay private

    def names(key):
        v = data.get(key)
        return [str(x).upper() for x in v] if isinstance(v, list) else None

    eligible = names("eligibleGpus") or names("eligibleAccelerators")
    ineligible = names("ineligibleGpus") or names("ineligibleAccelerators")
    if eligible is None and ineligible is None:
        return None
    return {"eligible": eligible or [], "ineligible": ineligible or [],
            "l4": any("L4" in g for g in (eligible or [])) if eligible is not None else None}


# ------------------------------------------------------------------ colab
def colab(*args, timeout=None, stdin=None):
    """Run the colab CLI; returns (returncode, combined output)."""
    try:
        r = subprocess.run(["colab", *args], capture_output=True, text=True, timeout=timeout, input=stdin)
    except subprocess.TimeoutExpired:
        return 124, f"colab {args[0]} timed out"
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def write_token():
    token = E.get("COLAB_TOKEN", "").strip()
    if not token and not E.get("GITHUB_ACTIONS") and TOKEN_PATH.exists():
        return  # run by hand: use the colab CLI's own sign-in on this machine
    if not token:
        report("error", "Not signed in to Google yet: sign in on the page first")
        sys.exit(1)
    try:
        json.loads(token)
    except ValueError:
        report("error", "The stored Google sign-in is damaged: sign in again on the page")
        sys.exit(1)
    TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_PATH.write_text(token)
    TOKEN_PATH.chmod(0o600)


def explain(out):
    lines = [line.strip() for line in out.strip().splitlines() if line.strip()]
    for line in reversed(lines):
        low = line.lower()
        if any(w in low for w in ("error", "denied", "quota", "unavailable", "failed", "not available")):
            return line[:200]
    return (lines[-1] if lines else "no output")[:200]


def provision(want):
    """Create the VM. L4 falls back to T4; T4 tries the high-RAM shape (more CPUs) first."""
    attempts = [("L4", [])] if want == "L4" else []
    attempts += [("T4", ["--high-mem"]), ("T4", [])]
    why_l4, out = None, ""
    for gpu, extra in attempts:
        report.gpu = gpu
        report("gpu", f"Requesting a{'n' if gpu == 'L4' else ''} {gpu} GPU from Colab")
        rc, out = colab("new", "-s", NAME, "--gpu", gpu, *extra, timeout=600)
        print(out, flush=True)
        if rc == 0:
            return gpu
        if any(s in out for s in ("invalid_grant", "Token has been expired", "401")):
            report("error", "The Google sign-in expired or was revoked: sign in again on the page")
            sys.exit(1)
        colab("stop", "-s", NAME, timeout=120)
        if gpu == "L4":
            why_l4 = explain(out)
            report.fallback = 1
            report.gpu = "T4"
            report("gpu-fallback", "L4 is not available on this Colab account right now, so T4 was picked")
    report("error", f"Colab would not give a GPU: {explain(out)}" + (f" (L4: {why_l4})" if why_l4 else ""))
    sys.exit(1)


def bootstrap_script(cfg):
    """vm/setup.py with the vm/ directory and the settings inlined, for one `colab exec`."""
    files = {p.name: base64.b64encode(p.read_bytes()).decode() for p in sorted((ROOT / "vm").glob("*.py"))}
    return (
        "import base64, os\n"
        "os.makedirs('/content/rc', exist_ok=True)\n"
        f"for _n, _b in {files!r}.items():\n"
        "    open('/content/rc/' + _n, 'wb').write(base64.b64decode(_b))\n"
        f"RC = {cfg!r}\n"
        "exec(compile(open('/content/rc/setup.py').read(), '/content/rc/setup.py', 'exec'))\n"
    )


def run_setup(cfg):
    script = pathlib.Path(f"/tmp/{NAME}-bootstrap.py")
    script.write_text(bootstrap_script(cfg))
    proc = subprocess.Popen(["colab", "exec", "-s", NAME, "--timeout", "2400", "-f", str(script)],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    ready, failed = None, False
    for line in proc.stdout:
        line = line.rstrip()
        print(line, flush=True)
        if line.startswith("@@STATUS "):
            _, phase, *msg = line.split(" ", 2)
            report(phase, msg[0] if msg else phase)
            failed = failed or phase == "error"
        elif line.startswith("@@READY "):
            ready = json.loads(line[len("@@READY "):])
    proc.wait()
    if not ready and not failed:
        report("error", "Setting up the VM failed (the workflow log has the details)")
    return ready


def vm_state():
    rc, out = colab("exec", "-s", NAME, "--timeout", "60",
                    stdin="print('@@STATE ' + open('/tmp/rc/state.json').read())", timeout=150)
    for line in out.splitlines():
        if line.startswith("@@STATE "):
            return json.loads(line[len("@@STATE "):])
    print(out[-500:], flush=True)
    return None


def release(message):
    if STATE.exists():
        rc, out = colab("stop", "-s", NAME, timeout=300)
        print(out, flush=True)
        STATE.unlink(missing_ok=True)
    report("stopped", message)


def play():
    write_token()
    STATE.write_text(json.dumps({"name": NAME}))  # from here on, cleanup releases the VM
    gpu = provision(E.get("GPU", "L4").upper())
    w, h = int(E.get("WIDTH", 1280)), int(E.get("HEIGHT", 720))
    cfg = {
        "key_hash": E["KEY_HASH"], "mode": E.get("MODE", "pc"), "width": w - w % 2, "height": h - h % 2,
        "fps": int(E.get("FPS") or 30), "bitrate": int(E.get("BITRATE") or 3500),
        "cordial_run_url": E.get("CORDIAL_RUN_URL", ""), "cordial_run_sha256": E.get("CORDIAL_RUN_SHA256", ""),
    }
    ready = run_setup(cfg)
    if not ready:
        if STATE.exists():
            colab("stop", "-s", NAME, timeout=300)
            STATE.unlink(missing_ok=True)
        sys.exit(1)
    report.gpu = gpu
    report("ready", f"Roblox is running on the {ready['gpu']}", url=ready["url"])

    started = time.time()
    max_s = float(E.get("MAX_MINUTES") or 120) * 60
    idle_s = float(E.get("IDLE_MINUTES") or 10) * 60
    misses = 0
    while True:
        time.sleep(30)
        st = vm_state()
        if st is None:
            misses += 1
            if misses >= 4:
                release("The Colab VM stopped answering (Colab may have reclaimed it)")
                return
            continue
        misses = 0
        if st.get("stopped"):
            release("Stopped")
            return
        if st.get("clients", 0) == 0 and time.time() - st.get("last_client", time.time()) > idle_s:
            release(f"Nobody was connected for {int(idle_s // 60)} minutes, so the VM was released")
            return
        if time.time() - started > max_s:
            release(f"The {int(max_s // 60)}-minute session limit was reached")
            return


def cleanup():
    if STATE.exists():
        if E.get("COLAB_TOKEN") and not TOKEN_PATH.exists():
            write_token()
        release("Stopped")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "play"
    try:
        {"login": login, "play": play, "cleanup": cleanup}[cmd]()
    except KeyboardInterrupt:  # the run was cancelled; the cleanup step releases the VM
        report("stopping", "Stopping")
        sys.exit(130)
