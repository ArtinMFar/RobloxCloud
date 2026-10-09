/* RobloxCloud's page. Static: everything it does goes through the GitHub API (to
 * start and stop .github/workflows/session.yml and to keep secrets) and, once a
 * session is up, through two websockets to the streamer on the Colab VM
 * (vm/streamer.py): /video for the picture and sound, /ctl for input. */
"use strict";
(() => {
  // The colab CLI's own sign-in (colab_cli/auth.py): its OAuth client, scopes and
  // the landing page that shows the code. The workflow finishes it with the
  // CLI's client config, so nothing secret lives here.
  const GOOGLE = {
    clientId: "764086051850-6qr4p6gpi6hn506pt8ejuq83di341hur.apps.googleusercontent.com",
    redirectUri: "https://sdk.cloud.google.com/applicationdefaultauthcode.html",
    scopes: [
      "openid",
      "https://www.googleapis.com/auth/userinfo.profile",
      "https://www.googleapis.com/auth/userinfo.email",
      "https://www.googleapis.com/auth/cloud-platform",
      "https://www.googleapis.com/auth/colaboratory",
      "https://www.googleapis.com/auth/drive.file",
    ],
  };
  const WORKFLOW = "session.yml";
  const DEFAULT_REPO = "ArtinMFar/RobloxCloud";
  const STEPS = [
    ["action", "Starting a GitHub Action", ["queued"]],
    ["gpu", "Getting a GPU from Colab", ["gpu", "gpu-fallback"]],
    ["setup", "Setting up the VM", ["vm", "install", "render", "display"]],
    ["roblox", "Starting Roblox", ["launch", "download"]],
    ["stream", "Connecting the stream", ["stream", "ready"]],
  ];

  const $ = (s) => document.querySelector(s);
  const $$ = (s) => Array.from(document.querySelectorAll(s));
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const isTouchFirst = matchMedia("(pointer: coarse)").matches && !matchMedia("(pointer: fine)").matches;
  const hasTouch = navigator.maxTouchPoints > 0;

  // -------------------------------------------------------------- storage
  const store = {
    get(k, d = null) {
      try {
        const v = localStorage.getItem(k);
        return v === null ? d : JSON.parse(v);
      } catch {
        return d;
      }
    },
    set(k, v) {
      try {
        localStorage.setItem(k, JSON.stringify(v));
      } catch {}
    },
    del(k) {
      try {
        localStorage.removeItem(k);
      } catch {}
    },
  };

  function guessRepo() {
    const host = location.hostname.toLowerCase();
    if (host.endsWith(".github.io")) {
      const owner = host.slice(0, -".github.io".length);
      const first = location.pathname.split("/").filter(Boolean)[0];
      return `${owner}/${first || host}`;
    }
    return DEFAULT_REPO;
  }

  const settings = Object.assign(
    { mode: isTouchFirst ? "mobile" : "pc", quality: "720", limit: "120", repo: guessRepo(), gpu: "L4" },
    store.get("rc.settings", {})
  );
  const saveSettings = () => store.set("rc.settings", settings);

  // ---------------------------------------------------------------- bytes
  const b64 = (bytes) => {
    let s = "";
    for (let i = 0; i < bytes.length; i++) s += String.fromCharCode(bytes[i]);
    return btoa(s);
  };
  const unb64 = (s) => Uint8Array.from(atob(s), (c) => c.charCodeAt(0));
  const b64url = (bytes) => b64(bytes).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  const random = (n) => crypto.getRandomValues(new Uint8Array(n));
  const randomId = () => Array.from(random(6), (b) => (b % 36).toString(36)).join("") + Date.now().toString(36).slice(-4);
  async function sha256(text) {
    return new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text)));
  }
  const hex = (bytes) => Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");

  // -------------------------------------------------------------------- UI bits
  let toastTimer;
  function toast(text, kind = "", ms = 6000) {
    const t = $("#toast");
    t.textContent = text;
    t.className = "toast " + kind;
    t.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => (t.hidden = true), ms);
  }
  function setMsg(el, text, kind = "") {
    el.textContent = text || "";
    el.className = "msg" + (kind ? " " + kind : "");
  }
  function banner(el, text, kind = "") {
    el.hidden = !text;
    el.textContent = text || "";
    el.className = "banner" + (kind ? " " + kind : "");
  }
  function show(view) {
    $("#home").hidden = view !== "home";
    $("#launch").hidden = view !== "launch";
    $("#stage").hidden = view !== "stage";
    document.querySelector(".topbar").hidden = view === "stage";
  }

  // --------------------------------------------------------------- GitHub API
  class GhError extends Error {
    constructor(message, status) {
      super(message);
      this.status = status;
    }
  }
  const ghToken = () => store.get("rc.ghToken", "");

  function explainGh(status, data, path, method) {
    const m = (data && data.message) || "";
    if (status === 401) return "GitHub did not accept the token. Check it in Settings.";
    if (status === 403 || (status === 404 && ghToken() && /secrets|dispatches|cancel/.test(path))) {
      if (/rate limit/i.test(m)) return "GitHub's API rate limit was hit. Wait a minute and try again.";
      const need = /secrets/.test(path)
        ? "Secrets: Read and write"
        : /statuses/.test(path)
          ? "Commit statuses: Read"
          : /actions/.test(path)
            ? "Actions: Read and write"
            : "access to the repository";
      return `The GitHub token is missing a permission on ${settings.repo}: ${need}.`;
    }
    if (status === 404 && /dispatches/.test(path))
      return `${WORKFLOW} is not on ${settings.repo}'s default branch, or the repository name in Settings is wrong.`;
    if (status === 404) return `GitHub could not find ${settings.repo}${path.includes("/actions/runs/") ? "'s workflow run" : ""}. Check the repository in Settings.`;
    return `GitHub said ${status}${m ? ": " + m : ""} (${method} ${path.split("?")[0]})`;
  }

  async function gh(path, { method = "GET", body, token } = {}) {
    token = token || ghToken();
    if (!token) throw new GhError("Connect GitHub first (step 0, or Settings).", 0);
    let r;
    try {
      r = await fetch("https://api.github.com" + path, {
        method,
        cache: "no-store",
        headers: {
          Authorization: `Bearer ${token}`,
          Accept: "application/vnd.github+json",
          ...(body !== undefined ? { "Content-Type": "application/json" } : {}),
        },
        body: body !== undefined ? JSON.stringify(body) : undefined,
      });
    } catch {
      throw new GhError("Could not reach GitHub. Check the connection.", 0);
    }
    if (r.status === 204) return null;
    const text = await r.text();
    let data = null;
    try {
      data = text ? JSON.parse(text) : null;
    } catch {}
    if (!r.ok) throw new GhError(explainGh(r.status, data, path, method), r.status);
    return data;
  }
  const repoPath = () => `/repos/${settings.repo}`;

  async function putSecret(name, sealedValue, keyId) {
    await gh(`${repoPath()}/actions/secrets/${name}`, { method: "PUT", body: { encrypted_value: sealedValue, key_id: keyId } });
  }

  /** Start session.yml; returns the run's id. */
  async function dispatch(inputs) {
    const repo = await gh(repoPath());
    const since = new Date(Date.now() - 60000).toISOString().replace(/\.\d+Z$/, "Z");
    const path = `${repoPath()}/actions/workflows/${WORKFLOW}/dispatches`;
    let res;
    try {
      res = await gh(path, { method: "POST", body: { ref: repo.default_branch, inputs, return_run_details: true } });
    } catch (e) {
      if (e.status !== 422 || !/return_run_details/.test(e.message)) throw e;
      res = await gh(path, { method: "POST", body: { ref: repo.default_branch, inputs } });
    }
    if (res && res.workflow_run_id) return res.workflow_run_id;
    // Older API: no run id in the reply, so find the run by its name (run-name has the session id).
    for (let i = 0; i < 40; i++) {
      const list = await gh(
        `${repoPath()}/actions/workflows/${WORKFLOW}/runs?event=workflow_dispatch&per_page=20&created=${encodeURIComponent(">=" + since)}`
      );
      const run = (list.workflow_runs || []).find((r) => (r.display_title || r.name || "").includes(inputs.session_id));
      if (run) return run.id;
      await sleep(1500);
    }
    throw new Error("The GitHub Action was started but did not show up. Check the Actions tab on GitHub.");
  }

  /** The newest roblox-cloud/<id> status on a commit, parsed. */
  async function latestStatus(sha, sid) {
    const list = await gh(`${repoPath()}/commits/${sha}/statuses?per_page=100`);
    const st = (list || []).find((s) => s.context === `roblox-cloud/${sid}`);
    if (!st) return null;
    let d = {};
    try {
      d = JSON.parse(st.description || "{}");
    } catch {
      d = { m: st.description };
    }
    return { phase: d.p, msg: d.m || "", gpu: d.g, fallback: !!d.f, url: st.target_url, state: st.state };
  }

  // ----------------------------------------------------------- home: steps
  function renderHome() {
    const token = ghToken();
    const google = store.get("rc.google");
    const pending = store.get("rc.pkce");
    const login = store.get("rc.login");

    $("#repoName").textContent = settings.repo;
    $("#sourceLink").href = `https://github.com/${settings.repo}`;
    const gh1 = $("#stepGithub");
    gh1.classList.toggle("done", !!token);
    $("#githubSub").textContent = token
      ? `Connected to ${settings.repo}.`
      : "One time. GitHub Actions starts and stops the Colab VM.";

    const g = $("#stepGoogle");
    g.classList.toggle("locked", !token);
    g.classList.toggle("done", !!google);
    $("#googleSub").textContent = google
      ? `Signed in${google.email ? " as " + google.email : ""}.`
      : "The Colab CLI's sign-in. Colab bills GPU time to this account.";
    $("#codeForm").hidden = !(pending || login);
    $("#accountChip").hidden = !google;
    $("#accountChip").textContent = google ? google.email || "Signed in" : "";

    const gpuStep = $("#stepGpu");
    gpuStep.classList.toggle("locked", !google);
    renderGpus();

    const ready = token && google;
    $("#playBtn").disabled = !ready;
    $("#playMeta").textContent = `${settings.gpu} GPU · ${settings.mode === "mobile" ? "Mobile" : "PC"} controls · ${settings.quality}p`;
  }

  // ------------------------------------------------------------ step 0: GitHub
  $("#githubForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const token = $("#githubToken").value.trim();
    const msg = $("#githubMsg");
    setMsg(msg, "Checking…");
    try {
      await checkGithub(token);
      store.set("rc.ghToken", token);
      $("#githubToken").value = "";
      setMsg(msg, "");
      renderHome();
      toast(`Connected to ${settings.repo}.`);
    } catch (err) {
      setMsg(msg, err.message, "error");
    }
  });

  async function checkGithub(token) {
    const repo = await gh(repoPath(), { token });
    if (!repo.permissions || !repo.permissions.push) {
      throw new Error(`This token cannot run workflows on ${settings.repo}.`);
    }
    await gh(`${repoPath()}/actions/secrets/public-key`, { token });
    await gh(`${repoPath()}/actions/workflows/${WORKFLOW}`, { token });
    return repo;
  }

  // ------------------------------------------------------------ step 1: Google
  async function openGoogle() {
    const verifier = b64url(random(48));
    const challenge = b64url(await sha256(verifier));
    const state = b64url(random(16));
    store.set("rc.pkce", { verifier, state, at: Date.now() });
    const url =
      "https://accounts.google.com/o/oauth2/auth?" +
      new URLSearchParams({
        response_type: "code",
        client_id: GOOGLE.clientId,
        redirect_uri: GOOGLE.redirectUri,
        scope: GOOGLE.scopes.join(" "),
        state,
        code_challenge: challenge,
        code_challenge_method: "S256",
        prompt: "consent",
        token_usage: "remote",
        access_type: "offline",
      });
    window.open(url, "_blank", "noopener");
    store.del("rc.login");
    renderHome();
    setMsg($("#googleMsg"), "Google's page opened in a new tab. Copy the code it shows, then paste it above.");
    $("#codeInput").focus();
  }
  $("#googleBtn").addEventListener("click", openGoogle);
  $("#googleAgain").addEventListener("click", openGoogle);

  function cleanCode(raw) {
    raw = raw.trim();
    const m = raw.match(/[?&]code=([^&\s]+)/);
    return m ? decodeURIComponent(m[1]) : raw.replace(/\s+/g, "");
  }

  $("#codeForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const code = cleanCode($("#codeInput").value);
    const msg = $("#googleMsg");
    const pk = store.get("rc.pkce");
    if (!code) return;
    if (!pk || Date.now() - pk.at > 30 * 60000) {
      setMsg(msg, "That sign-in has expired. Press Sign in with Google again.", "error");
      return;
    }
    $("#codeBtn").disabled = true;
    try {
      setMsg(msg, "Handing the code to the colab CLI on GitHub Actions…");
      const key = await gh(`${repoPath()}/actions/secrets/public-key`);
      // The code and PKCE verifier go to the workflow as a secret, never in the open.
      await putSecret("COLAB_AUTH_CODE", sealForGitHub(JSON.stringify({ code, verifier: pk.verifier }), key.key), key.key_id);
      const pageKey = nacl.box.keyPair();
      const sid = "login-" + randomId();
      const runId = await dispatch({
        action: "login",
        session_id: sid,
        repo_public_key: `${key.key_id}:${key.key}`,
        page_public_key: b64(pageKey.publicKey),
      });
      store.set("rc.login", { sid, runId, pk: b64(pageKey.publicKey), sk: b64(pageKey.secretKey), at: Date.now() });
      store.del("rc.pkce");
      $("#codeInput").value = "";
      await waitForLogin();
    } catch (err) {
      setMsg(msg, err.message, "error");
    } finally {
      $("#codeBtn").disabled = false;
    }
  });

  let loginWaiting = false;
  async function waitForLogin() {
    if (loginWaiting) return;
    loginWaiting = true;
    const msg = $("#googleMsg");
    try {
      const login = store.get("rc.login");
      if (!login) return;
      const t0 = Date.now();
      while (Date.now() - t0 < 6 * 60000) {
        const run = await gh(`${repoPath()}/actions/runs/${login.runId}`);
        const st = await latestStatus(run.head_sha, login.sid).catch(() => null);
        const secs = Math.round((Date.now() - login.at) / 1000);
        if (run.status !== "completed") {
          setMsg(msg, `${st && st.msg ? st.msg : run.status === "queued" ? "Waiting for a GitHub runner" : "Starting"}… (${secs}s, usually about 40s)`);
          await sleep(2500);
          continue;
        }
        if (run.conclusion !== "success" || (st && st.phase === "error")) {
          store.del("rc.login");
          throw new Error((st && st.msg) || `The sign-in workflow ${run.conclusion}. Open the Actions tab on GitHub for its log.`);
        }
        const checks = await gh(
          `${repoPath()}/commits/${run.head_sha}/check-runs?check_name=${encodeURIComponent("roblox-cloud-login/" + login.sid)}`
        );
        const cr = (checks.check_runs || [])[0];
        if (!cr) throw new Error("The sign-in finished but its result is missing. Sign in again.");
        const result = JSON.parse(cr.output.summary);
        await putSecret("COLAB_TOKEN", result.token, result.key_id);
        let info = {};
        try {
          info = JSON.parse(openSealed(result.info, { publicKey: unb64(login.pk), secretKey: unb64(login.sk) }));
        } catch {}
        store.set("rc.google", { email: info.email || "", gpus: info.gpus || null, at: Date.now() });
        store.del("rc.login");
        gh(`${repoPath()}/actions/secrets/COLAB_AUTH_CODE`, { method: "DELETE" }).catch(() => {});
        setMsg(msg, "");
        applyGpuEligibility(true);
        renderHome();
        toast(`Signed in${info.email ? " as " + info.email : ""}.`);
        return;
      }
      throw new Error("The sign-in workflow is taking too long. Check the Actions tab on GitHub.");
    } catch (err) {
      setMsg(msg, err.message, "error");
      renderHome();
    } finally {
      loginWaiting = false;
    }
  }

  async function signOut() {
    try {
      await gh(`${repoPath()}/actions/secrets/COLAB_TOKEN`, { method: "DELETE" });
    } catch (e) {
      if (e.status !== 404) toast(e.message, "error");
    }
    store.del("rc.google");
    renderHome();
    renderSettings();
    toast("Signed out. You can also remove the colab CLI's access at myaccount.google.com/permissions.", "", 9000);
  }

  // --------------------------------------------------------------- step 2: GPU
  function renderGpus() {
    $$(".gpu").forEach((b) => b.setAttribute("aria-checked", String(b.dataset.gpu === settings.gpu)));
  }
  function l4Known() {
    const g = store.get("rc.google");
    if (g && g.gpus && g.gpus.l4 === false) return "account";
    const recent = store.get("rc.l4Unavailable");
    if (recent && Date.now() - recent < 6 * 3600e3) return "recent";
    return null;
  }
  /** If L4 is picked but known to be unavailable, pick T4 and say so. */
  function applyGpuEligibility(announce) {
    const why = l4Known();
    if (settings.gpu === "L4" && why) {
      settings.gpu = "T4";
      saveSettings();
      const text =
        why === "account"
          ? "L4 isn't available on this Colab account, so T4 was picked automatically."
          : "L4 wasn't available last time, so T4 was picked automatically.";
      setMsg($("#gpuMsg"), text, "");
      if (announce) toast(text, "warn", 8000);
    }
    renderGpus();
  }
  $$(".gpu").forEach((b) =>
    b.addEventListener("click", () => {
      settings.gpu = b.dataset.gpu;
      saveSettings();
      setMsg($("#gpuMsg"), "");
      if (settings.gpu === "L4" && l4Known() === "recent") store.del("rc.l4Unavailable"); // let them try again
      applyGpuEligibility(true);
      renderHome();
    })
  );

  // ----------------------------------------------------------------- playing
  let session = store.get("rc.session"); // {sid, key, runId, at, gpu, mode, w, h, url, ...}
  let launchTick;

  function streamSize() {
    const h = settings.quality === "540" ? 540 : 720;
    let aspect = 16 / 9;
    if (settings.mode === "mobile") {
      const long = Math.max(screen.width, screen.height);
      const short = Math.min(screen.width, screen.height) || 1;
      aspect = Math.min(2.4, Math.max(1.33, long / short));
    }
    const w = Math.round((h * aspect) / 8) * 8;
    return { w, h };
  }

  async function play() {
    if (session) return;
    const btn = $("#playBtn");
    btn.disabled = true;
    banner($("#banner"), "");
    try {
      applyGpuEligibility(false);
      const key = b64url(random(32));
      const sid = randomId();
      const { w, h } = streamSize();
      const runId = await dispatch({
        action: "play",
        session_id: sid,
        key_hash: hex(await sha256(key)),
        gpu: settings.gpu,
        mode: settings.mode,
        size: `${w}x${h}`,
        max_minutes: String(settings.limit),
      });
      session = { sid, key, runId, at: Date.now(), gpu: settings.gpu, mode: settings.mode, w, h };
      store.set("rc.session", session);
      startLaunchView();
      followSession();
    } catch (err) {
      banner($("#banner"), err.message, "error");
    } finally {
      btn.disabled = false;
      renderHome();
    }
  }
  $("#playBtn").addEventListener("click", play);

  function startLaunchView() {
    show("launch");
    const ol = $("#progress");
    ol.innerHTML = "";
    for (const [id, label] of STEPS) {
      const li = document.createElement("li");
      li.dataset.step = id;
      li.innerHTML = `<span class="dot"></span><span></span>`;
      li.lastChild.textContent = label;
      ol.appendChild(li);
    }
    setProgress("queued", "Starting a GitHub Action…");
    banner($("#launchBanner"), session.fallback ? fallbackText() : "", "");
    clearInterval(launchTick);
    launchTick = setInterval(() => {
      const s = Math.max(0, Math.round((Date.now() - session.at) / 1000));
      $("#launchTimer").textContent = `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
    }, 1000);
  }

  function setProgress(phase, message, failed = false) {
    const idx = Math.max(0, STEPS.findIndex(([, , phases]) => phases.includes(phase)));
    $$("#progress li").forEach((li, i) => {
      li.className = i < idx ? "done" : i === idx ? (failed ? "failed" : phase === "ready" ? "done" : "active") : "";
    });
    $("#launchMsg").textContent = message;
  }
  const fallbackText = () => "L4 isn't available on your Colab account right now, so T4 was picked automatically.";

  let following = false;
  async function followSession() {
    if (following) return;
    following = true;
    let errors = 0;
    try {
      while (session && !session.ended) {
        try {
          const run = await gh(`${repoPath()}/actions/runs/${session.runId}`);
          $("#runLink").href = run.html_url;
          $("#runLink").hidden = false;
          const st = await latestStatus(run.head_sha, session.sid);
          errors = 0;
          if (st) onStatus(st);
          else if (!session.url) setProgress("queued", run.status === "queued" ? "Waiting for a GitHub runner…" : "Starting…");
          if (run.status === "completed" && session && !session.ended) {
            const why =
              st && ["error", "stopped"].includes(st.phase)
                ? st.msg
                : run.conclusion === "cancelled"
                  ? "Stopped."
                  : `The session's workflow ended (${run.conclusion}).`;
            endSession(why, st ? st.phase === "error" : run.conclusion !== "cancelled" && run.conclusion !== "success");
          }
        } catch (err) {
          errors++;
          if (errors > 3) $("#launchMsg").textContent = err.message;
          if (err.status === 401 || err.status === 404) {
            endSession(err.message, true);
            break;
          }
        }
        await sleep(session && session.url ? 15000 : 2500);
      }
    } finally {
      following = false;
    }
  }

  function onStatus(st) {
    if (!session || session.ended) return;
    if (st.fallback && !session.fallback) {
      session.fallback = true;
      session.gpu = "T4";
      store.set("rc.l4Unavailable", Date.now());
      if (settings.gpu === "L4") {
        settings.gpu = "T4";
        saveSettings();
      }
      banner($("#launchBanner"), fallbackText());
      toast(fallbackText(), "warn", 9000);
    }
    if (st.gpu) session.gpu = st.gpu;
    if (st.phase === "error") {
      setProgress(currentPhase || "queued", st.msg, true);
      return;
    }
    if (st.phase === "stopped" || st.phase === "stopping") return;
    currentPhase = st.phase;
    setProgress(st.phase, st.msg);
    if (st.phase === "ready" && isStreamUrl(st.url) && session.url !== st.url) {
      session.url = st.url;
      store.set("rc.session", session);
      openStage();
    }
  }
  let currentPhase = null;
  // The tunnel's HTTPS address; a loopback one is allowed for testing a streamer locally.
  const isStreamUrl = (u) => /^https:\/\/[^/]+$/.test(u || "") || /^http:\/\/(127\.0\.0\.1|localhost)(:\d+)?$/.test(u || "");

  async function stopSession() {
    if (!session) return;
    const s = session;
    s.stopping = true;
    send({ t: "stop" });
    $("#launchMsg").textContent = "Stopping…";
    try {
      await gh(`${repoPath()}/actions/runs/${s.runId}/cancel`, { method: "POST" });
    } catch (err) {
      if (err.status !== 409) toast(err.message, "error"); // 409: the run already finished
    }
    endSession("Stopped. The Colab VM is being released.", false);
  }
  $("#launchStop").addEventListener("click", stopSession);
  $("#stopBtn").addEventListener("click", stopSession);

  function endSession(message, isError) {
    if (session) session.ended = true;
    session = null;
    store.del("rc.session");
    clearInterval(launchTick);
    closeStage();
    show("home");
    banner($("#banner"), message || "The session ended.", isError ? "error" : "info");
    renderHome();
    applyGpuEligibility(false);
  }

  // ------------------------------------------------------------------- stage
  let player = null;
  let ctl = null;
  let ctlBackoff = 1;
  let serverState = { locked: false, multiTouch: true };
  let firstFrame = false;
  let wakeLock = null;
  let pingTimer;
  let lastRtt = null;
  const canvas = $("#video");

  function openStage() {
    show("stage");
    clearInterval(launchTick);
    firstFrame = false;
    overlay("Connecting to the stream…", true);
    renderModeButtons();
    $("#kbBtn").hidden = !(hasTouch || settings.mode === "mobile");
    fit();
    const base = session.url.replace(/^http/, "ws");
    const k = encodeURIComponent(session.key);
    player = new JSMpeg.Player(`${base}/video?k=${k}`, {
      canvas,
      audio: true,
      video: true,
      pauseWhenHidden: false,
      videoBufferSize: 4 * 1024 * 1024,
      audioBufferSize: 512 * 1024,
      reconnectInterval: 2,
      onVideoDecode: () => {
        if (!firstFrame) {
          firstFrame = true;
          fit();
          overlay(null);
          readyToPlay();
        }
      },
    });
    openCtl(base, k);
    clearInterval(pingTimer);
    pingTimer = setInterval(() => send({ t: "ping", ts: performance.now() }), 2000);
    fadeHudSoon();
  }

  function closeStage() {
    clearInterval(pingTimer);
    if (player) {
      try {
        player.destroy();
      } catch {}
      player = null;
    }
    if (ctl) {
      const c = ctl;
      ctl = null;
      c.onclose = null;
      try {
        c.close();
      } catch {}
    }
    if (document.pointerLockElement) document.exitPointerLock();
    if (document.fullscreenElement) document.exitFullscreen().catch(() => {});
    if (wakeLock) wakeLock.release().catch(() => {});
    wakeLock = null;
    pointers.clear();
    $("#moreMenu").hidden = true;
    $("#lockHint").hidden = true;
  }

  function overlay(text, spinner = false) {
    $("#stageOverlay").hidden = !text;
    $("#overlayText").textContent = text || "";
    $("#overlaySpinner").hidden = !spinner;
    $("#tapToPlay").hidden = true;
  }

  // Browsers only allow sound, fullscreen and the landscape lock after a tap or
  // click, so the first frame waits behind one.
  function readyToPlay() {
    const ctx = player && player.audioOut && player.audioOut.context;
    if (!isTouchFirst && audioUnlocked() && !(ctx && ctx.state === "suspended")) return;
    $("#stageOverlay").hidden = false;
    $("#overlaySpinner").hidden = true;
    $("#overlayText").textContent = `Roblox is running on the ${session ? session.gpu || "" : ""} GPU.`;
    $("#tapToPlayText").textContent = hasTouch ? "Tap to play" : "Click to play";
    $("#tapToPlay").hidden = false;
  }
  $("#tapToPlay").addEventListener("click", async () => {
    unlockAudio();
    overlay(null);
    if (settings.mode === "mobile" || isTouchFirst) await enterFullscreen();
    requestWakeLock();
    canvas.focus();
  });

  const audioUnlocked = () => !player || !player.audioOut || player.audioOut.unlocked !== false;
  function unlockAudio() {
    try {
      if (player && player.audioOut && player.audioOut.unlock) player.audioOut.unlock(() => {});
      if (player && player.audioOut && player.audioOut.context && player.audioOut.context.state === "suspended")
        player.audioOut.context.resume();
    } catch {}
  }
  async function requestWakeLock() {
    try {
      wakeLock = await navigator.wakeLock.request("screen");
    } catch {}
  }

  async function enterFullscreen() {
    const el = document.documentElement;
    try {
      if (!document.fullscreenElement) {
        if (el.requestFullscreen) await el.requestFullscreen({ navigationUI: "hide" });
        else if (el.webkitRequestFullscreen) el.webkitRequestFullscreen();
      }
    } catch {}
    try {
      if (screen.orientation && screen.orientation.lock) await screen.orientation.lock("landscape");
    } catch {}
    try {
      if (navigator.keyboard && navigator.keyboard.lock && settings.mode === "pc") await navigator.keyboard.lock();
    } catch {}
    setTimeout(fit, 300);
  }
  $("#fsBtn").addEventListener("click", () => {
    if (document.fullscreenElement) document.exitFullscreen().catch(() => {});
    else enterFullscreen();
  });
  document.addEventListener("fullscreenchange", () => setTimeout(fit, 100));

  function dims() {
    return { w: serverState.w || (session && session.w) || 1280, h: serverState.h || (session && session.h) || 720 };
  }
  function fit() {
    const { w, h } = dims();
    const stage = $("#stage");
    const sw = stage.clientWidth || innerWidth;
    const sh = stage.clientHeight || innerHeight;
    const s = Math.min(sw / w, sh / h);
    canvas.style.width = `${Math.floor(w * s)}px`;
    canvas.style.height = `${Math.floor(h * s)}px`;
  }
  addEventListener("resize", fit);

  // -------------------------------------------------------------- control link
  function openCtl(base, k) {
    const ws = new WebSocket(`${base}/ctl?k=${k}`);
    ctl = ws;
    ws.onopen = () => {
      ctlBackoff = 1;
      send({ t: "mode", mode: settings.mode });
    };
    ws.onmessage = (e) => {
      let m;
      try {
        m = JSON.parse(e.data);
      } catch {
        return;
      }
      if (m.t === "state") onServerState(m);
      else if (m.t === "pong" && typeof m.ts === "number") {
        lastRtt = Math.round(performance.now() - m.ts);
        updateStat();
      } else if (m.t === "stopped") endSession(m.why === "stopped from the page" ? "Stopped." : m.why || "Stopped.", false);
    };
    ws.onclose = () => {
      if (ctl !== ws || !session || session.ended) return;
      ctl = null;
      lastRtt = null;
      updateStat();
      if (firstFrame) overlay("Reconnecting…", true);
      setTimeout(() => session && !session.ended && openCtl(base, k), Math.min(10, ctlBackoff) * 1000);
      ctlBackoff *= 2;
    };
  }
  function send(m) {
    if (ctl && ctl.readyState === 1) ctl.send(JSON.stringify(m));
  }

  function onServerState(m) {
    const was = serverState;
    serverState = m;
    if (m.w !== was.w || m.h !== was.h) fit();
    if (firstFrame && $("#overlayText").textContent === "Reconnecting…") overlay(null);
    if (m.restarting) overlay("Restarting Roblox…", true);
    else if (was.restarting) overlay(null);
    // The game took or let go of the mouse (mouse lock, first person, shift lock).
    if (settings.mode === "pc" && !hasTouchOnlyPointer) {
      if (m.locked && document.pointerLockElement !== canvas) $("#lockHint").hidden = false;
      if (!m.locked) {
        $("#lockHint").hidden = true;
        if (document.pointerLockElement === canvas) document.exitPointerLock();
      }
    }
    updateStat();
  }
  let hasTouchOnlyPointer = isTouchFirst;

  function updateStat() {
    const parts = [];
    if (session && session.gpu) parts.push(session.gpu);
    if (lastRtt !== null) parts.push(`${lastRtt} ms`);
    if (settings.mode === "mobile" && serverState.multiTouch === false) parts.push("1 finger");
    $("#hudStat").textContent = parts.join(" · ");
    $("#menuInfo").textContent =
      `Stream ${dims().w}×${dims().h}` +
      (settings.mode === "mobile"
        ? serverState.multiTouch === false
          ? " · This VM's Cordial takes one finger at a time."
          : " · Several fingers at once."
        : " · Keyboard and mouse.");
  }

  // ------------------------------------------------------------------- input
  const pointers = new Map(); // pointerId -> {x, y, dirty}
  let pending = { mm: null, dx: 0, dy: 0 };
  let flushQueued = false;

  function norm(e) {
    const r = canvas.getBoundingClientRect();
    return {
      x: Math.min(1, Math.max(0, (e.clientX - r.left) / r.width)),
      y: Math.min(1, Math.max(0, (e.clientY - r.top) / r.height)),
    };
  }
  const r4 = (v) => Math.round(v * 10000) / 10000;

  function queueFlush() {
    if (flushQueued) return;
    flushQueued = true;
    requestAnimationFrame(flush);
  }
  function flush() {
    flushQueued = false;
    const moved = [];
    for (const [id, p] of pointers) {
      if (p.dirty) {
        moved.push([id, r4(p.x), r4(p.y)]);
        p.dirty = false;
      }
    }
    if (moved.length) send({ t: "tm", p: moved });
    if (pending.mm) {
      send({ t: "mm", x: r4(pending.mm.x), y: r4(pending.mm.y) });
      pending.mm = null;
    }
    if (pending.dx || pending.dy) {
      send({ t: "mr", dx: Math.round(pending.dx), dy: Math.round(pending.dy) });
      pending.dx = pending.dy = 0;
    }
  }

  function wakeHud() {
    $("#hud").classList.remove("faded");
    fadeHudSoon();
  }
  let hudTimer;
  function fadeHudSoon() {
    clearTimeout(hudTimer);
    hudTimer = setTimeout(() => {
      if ($("#moreMenu").hidden) $("#hud").classList.add("faded");
    }, 4000);
  }

  canvas.addEventListener("pointerdown", (e) => {
    e.preventDefault();
    unlockAudio();
    canvas.focus({ preventScroll: true });
    if (!$("#moreMenu").hidden) closeMenu();
    hasTouchOnlyPointer = e.pointerType === "touch" && isTouchFirst;
    if (settings.mode === "mobile") {
      const p = norm(e);
      try {
        canvas.setPointerCapture(e.pointerId);
      } catch {}
      pointers.set(e.pointerId, { ...p, dirty: false });
      flush();
      send({ t: "td", id: e.pointerId, x: r4(p.x), y: r4(p.y) });
      return;
    }
    // PC mode: a mouse (a touchscreen's primary finger acts as one).
    if (e.pointerType === "touch" && !e.isPrimary) return;
    const b = e.pointerType === "mouse" ? e.button : 0;
    if (document.pointerLockElement === canvas) {
      send({ t: "mb", b, d: 1 });
      return;
    }
    if (serverState.locked && e.pointerType === "mouse") {
      lockPointer();
      send({ t: "mb", b, d: 1 });
      return;
    }
    try {
      canvas.setPointerCapture(e.pointerId);
    } catch {}
    const p = norm(e);
    pending.mm = null;
    send({ t: "mb", b, d: 1, x: r4(p.x), y: r4(p.y) });
  });

  canvas.addEventListener("pointermove", (e) => {
    if (settings.mode === "mobile") {
      const p = pointers.get(e.pointerId);
      if (!p) return;
      Object.assign(p, norm(e), { dirty: true });
      queueFlush();
      return;
    }
    if (e.pointerType === "touch" && !e.isPrimary) return;
    if (document.pointerLockElement === canvas) {
      const r = canvas.getBoundingClientRect();
      const { w, h } = dims();
      pending.dx += e.movementX * (w / r.width);
      pending.dy += e.movementY * (h / r.height);
    } else {
      pending.mm = norm(e);
      if (e.pointerType === "mouse") wakeHudIfTop(e);
    }
    queueFlush();
  });

  function wakeHudIfTop(e) {
    if (e.clientY < 70) wakeHud();
  }

  function pointerEnd(e) {
    if (settings.mode === "mobile") {
      if (!pointers.has(e.pointerId)) return;
      flush();
      pointers.delete(e.pointerId);
      send({ t: "tu", id: e.pointerId });
      return;
    }
    if (e.pointerType === "touch" && !e.isPrimary) return;
    const b = e.pointerType === "mouse" ? e.button : 0;
    flush();
    if (document.pointerLockElement === canvas) send({ t: "mb", b, d: 0 });
    else {
      const p = norm(e);
      send({ t: "mb", b, d: 0, x: r4(p.x), y: r4(p.y) });
    }
  }
  canvas.addEventListener("pointerup", pointerEnd);
  canvas.addEventListener("pointercancel", pointerEnd);
  canvas.addEventListener("contextmenu", (e) => e.preventDefault());
  canvas.addEventListener(
    "wheel",
    (e) => {
      e.preventDefault();
      if (settings.mode === "pc") send({ t: "wh", dy: e.deltaMode === 1 ? e.deltaY * 33 : e.deltaY });
    },
    { passive: false }
  );

  function lockPointer() {
    try {
      const p = canvas.requestPointerLock({ unadjustedMovement: true });
      if (p && p.catch) p.catch(() => canvas.requestPointerLock());
    } catch {
      try {
        canvas.requestPointerLock();
      } catch {}
    }
  }
  document.addEventListener("pointerlockchange", () => {
    $("#lockHint").hidden = !(serverState.locked && document.pointerLockElement !== canvas && settings.mode === "pc");
  });

  // Keys: PC mode only. Mobile mode turns Cordial's keyboard mapping off, so the
  // game sees a phone; text still goes in through the on-screen keyboard.
  const stageActive = () => !$("#stage").hidden && !$("#settings").open;
  addEventListener("keydown", (e) => {
    if (!stageActive() || e.target === softKb || e.target === $("#joinInput")) return;
    if (settings.mode !== "pc") return;
    if (e.code === "F11" || (e.ctrlKey && e.shiftKey && e.code === "KeyI")) return;
    e.preventDefault();
    send({ t: "k", code: e.code, d: 1, r: e.repeat ? 1 : 0 });
  });
  addEventListener("keyup", (e) => {
    if (!stageActive() || e.target === softKb || e.target === $("#joinInput")) return;
    if (settings.mode !== "pc") return;
    e.preventDefault();
    send({ t: "k", code: e.code, d: 0 });
  });
  function letGo() {
    if (settings.mode === "mobile") {
      if (pointers.size) send({ t: "tc" });
      pointers.clear();
    } else send({ t: "rel" });
  }
  addEventListener("blur", letGo);
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) letGo();
    else if (wakeLock === null && !$("#stage").hidden) requestWakeLock();
  });

  // The phone's on-screen keyboard, through a hidden text box. What changed in
  // the box is sent as text and Backspaces, so autocorrect and swipe typing work.
  const softKb = $("#softKb");
  const PREFIX = "  ";
  let kbLast = PREFIX;
  let composing = false;
  softKb.value = PREFIX;
  function kbDiff() {
    const v = softKb.value;
    let c = 0;
    while (c < kbLast.length && c < v.length && kbLast[c] === v[c]) c++;
    for (let i = 0; i < kbLast.length - c; i++) send({ t: "key", k: "Backspace" });
    const added = v.slice(c);
    if (added) {
      added.split("\n").forEach((part, i, all) => {
        if (part) send({ t: "text", s: part });
        if (i < all.length - 1) send({ t: "key", k: "Enter" });
      });
    }
    kbLast = v;
    if (!composing && (v.length < PREFIX.length || v.length > 60 || !v.startsWith(PREFIX))) {
      softKb.value = kbLast = PREFIX;
    }
  }
  softKb.addEventListener("input", kbDiff);
  softKb.addEventListener("compositionstart", () => (composing = true));
  softKb.addEventListener("compositionend", () => {
    composing = false;
    kbDiff();
  });
  softKb.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      send({ t: "key", k: "Enter" });
    } else if (e.key === "Escape") {
      softKb.blur();
    }
  });
  $("#kbBtn").addEventListener("click", () => {
    if (document.activeElement === softKb) softKb.blur();
    else {
      softKb.value = kbLast = PREFIX;
      softKb.focus();
    }
  });

  // ------------------------------------------------------------- more menu
  function closeMenu() {
    $("#moreMenu").hidden = true;
    $("#moreBtn").setAttribute("aria-expanded", "false");
    fadeHudSoon();
  }
  $("#moreBtn").addEventListener("click", () => {
    const open = $("#moreMenu").hidden;
    $("#moreMenu").hidden = !open;
    $("#moreBtn").setAttribute("aria-expanded", String(open));
    wakeHud();
    updateStat();
    $("#soundBtn").textContent = player && player.volume === 0 ? "Sound on" : "Sound off";
  });
  $("#hud").addEventListener("pointerdown", wakeHud);
  $("#restartBtn").addEventListener("click", () => {
    send({ t: "restart" });
    closeMenu();
    overlay("Restarting Roblox…", true);
  });
  $("#soundBtn").addEventListener("click", () => {
    if (!player) return;
    unlockAudio();
    player.volume = player.volume === 0 ? 1 : 0;
    $("#soundBtn").textContent = player.volume === 0 ? "Sound on" : "Sound off";
  });
  $("#joinForm").addEventListener("submit", (e) => {
    e.preventDefault();
    const raw = $("#joinInput").value.trim();
    const id = (raw.match(/(\d{3,})/) || [])[1];
    if (!id) return toast("Paste a place ID or a roblox.com/games/… link.", "warn");
    send({ t: "join", place: id });
    toast(`Joining place ${id}… (you need to be signed in to Roblox)`);
    $("#joinInput").value = "";
    closeMenu();
  });

  function setMode(mode) {
    if (mode === settings.mode) return;
    letGo();
    settings.mode = mode;
    saveSettings();
    send({ t: "mode", mode });
    if (document.pointerLockElement) document.exitPointerLock();
    $("#lockHint").hidden = true;
    $("#kbBtn").hidden = !(hasTouch || settings.mode === "mobile");
    renderModeButtons();
    renderHome();
    updateStat();
    toast(
      mode === "mobile"
        ? "Mobile controls: Roblox's thumbstick and buttons, keyboard mapping off."
        : "PC controls: keyboard and mouse, keyboard mapping on."
    );
  }
  function renderModeButtons() {
    $$("[data-mode]").forEach((b) => b.setAttribute("aria-checked", String(b.dataset.mode === settings.mode)));
  }
  $$("[data-mode]").forEach((b) => b.addEventListener("click", () => setMode(b.dataset.mode)));

  // ---------------------------------------------------------------- settings
  function renderSettings() {
    renderModeButtons();
    $$("[data-quality]").forEach((b) => b.setAttribute("aria-checked", String(b.dataset.quality === settings.quality)));
    $("#limitSelect").value = String(settings.limit);
    $("#repoInput").value = settings.repo;
    $("#tokenInput").value = "";
    $("#tokenInput").placeholder = ghToken() ? "Saved (type to replace)" : "Not set";
    const g = store.get("rc.google");
    $("#settingsGoogle").textContent = g ? `Signed in${g.email ? " as " + g.email : ""}.` : "Not signed in.";
    $("#signOut").disabled = !g;
  }
  $("#settingsBtn").addEventListener("click", () => {
    renderSettings();
    setMsg($("#settingsGithubMsg"), "");
    $("#settings").showModal();
  });
  $("#modeLink").addEventListener("click", () => $("#settingsBtn").click());
  $$("[data-quality]").forEach((b) =>
    b.addEventListener("click", () => {
      settings.quality = b.dataset.quality;
      saveSettings();
      renderSettings();
      renderHome();
    })
  );
  $("#limitSelect").addEventListener("change", (e) => {
    settings.limit = e.target.value;
    saveSettings();
  });
  $("#saveGithub").addEventListener("click", async () => {
    const msg = $("#settingsGithubMsg");
    const repo = $("#repoInput").value.trim().replace(/^https:\/\/github\.com\//, "").replace(/\/$/, "");
    const token = $("#tokenInput").value.trim() || ghToken();
    if (!/^[\w.-]+\/[\w.-]+$/.test(repo)) return setMsg(msg, "Write the repository as owner/name.", "error");
    const old = settings.repo;
    settings.repo = repo;
    setMsg(msg, "Checking…");
    try {
      await checkGithub(token);
      saveSettings();
      if (token) store.set("rc.ghToken", token);
      setMsg(msg, "Saved. GitHub accepts the token.", "ok");
      renderSettings();
      renderHome();
    } catch (err) {
      settings.repo = old;
      setMsg(msg, err.message, "error");
    }
  });
  $("#signOut").addEventListener("click", signOut);
  $("#forget").addEventListener("click", () => {
    for (const k of ["rc.settings", "rc.ghToken", "rc.google", "rc.pkce", "rc.login", "rc.session", "rc.l4Unavailable"]) store.del(k);
    location.reload();
  });
  $("#settings").addEventListener("close", () => renderHome());

  // -------------------------------------------------------------------- start
  renderHome();
  applyGpuEligibility(false);
  if (store.get("rc.login")) waitForLogin();
  if (session && Date.now() - session.at < 6 * 3600e3) {
    // A session from before a reload: pick it up again.
    session.url = null;
    startLaunchView();
    followSession();
  } else if (session) {
    session = null;
    store.del("rc.session");
  }
  window.__rc = { settings, store, get session() { return session; } }; // for debugging from the console
})();
