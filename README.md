# RobloxCloud

Play Roblox in a browser, on a PC or a phone, running on a Google Colab GPU.

The site (GitHub Pages, in `site/`) signs you in to Google with the `colab`
command-line tool's own sign-in, lets you pick an **L4** or **T4** GPU, and on
**Play** starts a GitHub Action that rents a Colab VM with that GPU, installs
[Cordial](https://github.com/luohoa97/cordial) (an open-source Linux runtime
for Roblox's Android client), renders Roblox on the GPU and streams it back to
the page. **Stop** ends the game and releases the VM.

The GPU setup comes from the notebook in
[ArtinMFar/7uiii88](https://github.com/ArtinMFar/7uiii88) (commit `55d13bc`,
NVIDIA Vulkan, not the earlier CPU/lavapipe version).

## Setting it up

1. **Turn on GitHub Pages**: Settings → Pages → Build and deployment →
   Source: **GitHub Actions**. Then run the *Publish the site* workflow (or
   push to `site/`). The site is at `https://<owner>.github.io/RobloxCloud/`.
2. Open the site and follow its steps:
   - **Connect GitHub** (once): a
     [fine-grained token](https://github.com/settings/personal-access-tokens/new)
     for this repository with *Actions: read and write*, *Secrets: read and
     write* and *Commit statuses: read* (or a classic token with `repo`). It is
     kept in that browser only.
   - **Sign in with Google**: opens the same Google page as `colab`'s remote
     sign-in. Paste the code it shows. Takes about 40 seconds, because a
     GitHub Action finishes the sign-in with the colab CLI.
   - **Pick a GPU**: L4 needs Colab Pro or pay-as-you-go compute units. If the
     account cannot get an L4, T4 is picked automatically and the page says
     so, at sign-in (from Colab's own eligibility list) or when Play asks
     Colab for one.
   - **Play**. The first start takes a few minutes (VM, Cordial, a 400 MB
     Roblox download).

## Controls

Settings → Controls, or the ⋯ menu while playing:

- **PC**: keyboard and mouse, with Cordial's keyboard mapping on (WASD, Space,
  chat, mouse look). When a game locks the mouse (first person, shift lock),
  click once and the page captures the mouse; Esc lets go.
- **Mobile**: Roblox shows its own touch thumbstick and jump button
  (Cordial is told the device is a touchscreen) and every finger is passed to
  the game as its own touch, so you can move and turn at once. Keyboard keys
  are not mapped; the ⌨ button opens the phone's keyboard for chat and text
  boxes.

The ⋯ menu also has *Join* (a place ID or a `roblox.com/games/…` link; you
need to be signed in to Roblox), *Restart Roblox* and sound.

## How it fits together

```
site/ (GitHub Pages)  --GitHub API-->  .github/workflows/session.yml  (scripts/session.py)
   |  ^                                    |  colab new --gpu L4 (else T4)
   |  | commit status: progress, address   |  colab exec vm/setup.py
   |  +------------------------------------+
   |
   +--wss /video, /ctl (Cloudflare quick tunnel)-->  vm/streamer.py on the Colab VM
                                                      | ffmpeg: MPEG-1 + MP2 -> JSMpeg in the page
                                                      | PC input: XTest; mobile input: Cordial's devctl socket
                                                      +- Cordial + Roblox on Xvfb, rendered by NVIDIA Vulkan
```

- **Sign-in.** The page builds the colab CLI's Google URL with its own PKCE
  verifier and stores the code you paste as the `COLAB_AUTH_CODE` secret.
  `session.py login` exchanges it with the colab CLI's client config, seals the
  resulting `token.json` for the repository's Actions key and hands it back;
  the page stores it as the `COLAB_TOKEN` secret, which `session.py play`
  reads. The account's email and GPU eligibility come back sealed for a
  one-time key only the page has, because this repository is public.
- **Progress.** The workflow posts commit statuses (`roblox-cloud/<id>`) that
  the page polls; the last one carries the stream's address. The stream only
  accepts the session key the page made (the workflow only sees its SHA-256).
- **Ending.** Stop cancels the run; its cleanup step runs `colab stop`. The VM
  is also released when nobody has been connected for 10 minutes, at the time
  limit in Settings, or after 6 hours (the longest a GitHub Action runs).
- **Multi-touch.** Cordial's devctl socket takes one finger at a time.
  `patches/cordial-devctl-multitouch.patch` adds `touch <down|move|up> <id> <x> <y>`;
  *Build multi-touch Cordial* (`build-cordial.yml`) builds Cordial 0.27.0's
  loader with it and publishes it as the `cordial-0.27.0-multitouch` release,
  which the VM downloads. Without that release mobile mode still works, with
  one finger.

## Things to know

- Roblox bans accounts for using third-party clients, in waves (Cordial's own
  README warns about it). Use an account you can afford to lose.
- Colab's free tier does not allow remote desktops or using the VM mainly
  through another web UI; a paid plan does not have that rule. GPU time uses
  your Colab compute units.
- A session's VM is new every time, so Roblox is downloaded again and you sign
  in to Roblox again.

## Credits and licences

- [Cordial](https://github.com/luohoa97/cordial), GPL-3.0-or-later. The
  multi-touch build is upstream's v0.27.0 plus the patch in `patches/`.
- [JSMpeg](https://github.com/phoboslab/jsmpeg) (MIT),
  [TweetNaCl.js](https://github.com/dchest/tweetnacl-js) (public domain) and
  [blakejs](https://github.com/dcposch/blakejs) (CC0), in `site/vendor/`.
