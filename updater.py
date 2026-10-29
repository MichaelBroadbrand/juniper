"""Over-the-air updates for the packaged app.

Most updates are code only (Python and the setup page, a few hundred KB): they're downloaded,
checked against the publisher's signature, unpacked into your data folder, and used from the next
start (boot.py picks them up), which happens by itself once Juniper is idle. The app bundle is
never touched, so macOS doesn't ask about it or the microphone again.

When an update needs a newer app (new packages or models), the feed points to a new DMG instead,
and the menu bar panel offers to download it.

The feed is feed.json plus feed.sig (an Ed25519 signature of feed.json's exact bytes):
    {"code": {"version", "min_app", "url", "sha256", "notes"}, "app": {"version", "url", "notes"}}
"""
import base64
import hashlib
import json
import os
import shutil
import subprocess
import threading
import time
import urllib.request
import zipfile
from pathlib import Path

import ed25519
from juniper import APP_VERSION, BUNDLED, CODE_VERSION, DATA, log, restart, set_state

# JUNIPER_FEED points at a test server; whatever it serves must still carry the publisher's signature.
FEED = os.environ.get("JUNIPER_FEED") or \
    "https://github.com/MichaelBroadbrand/juniper/releases/latest/download/feed.json"
# The publisher's key (packaging/updates.py signs with its secret half, kept out of the app).
PUBLIC_KEY = bytes.fromhex("ba0efa3ddb33b815a86fe2b3c948d631e0e2f026d3c9fcdc0dd9a086bab03216")
CODE = DATA / "code"
EVERY = 4 * 3600


def vt(v):
    """'1.2.10' -> (1, 2, 10) for comparing versions."""
    return tuple(int(x) for x in str(v).split(".") if x.isdigit()) or (0,)


def fetch(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": f"Juniper/{CODE_VERSION}"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


class Updater(threading.Thread):
    """Checks the feed after start-up and every few hours; installs code updates when idle."""

    def __init__(self, app):
        super().__init__(daemon=True)
        self.app = app
        self.status = "up to date"        # what the menu bar panel shows
        self.app_update = None            # a newer app (DMG) to download, from the feed
        self.ready = None                 # a code version unpacked and waiting for an idle moment
        self.checking = False
        self.lock = threading.Lock()

    def run(self):
        time.sleep(20)                     # let start-up finish first
        while True:
            self.check()
            end = time.time() + EVERY
            while time.time() < end:
                if self.ready and self.idle():
                    self.apply()
                time.sleep(15)

    # ── checking ─────────────────────────────────────────────────────────────────────────
    def check(self, manual=False):
        if not self.lock.acquire(blocking=False):
            return
        self.checking = True
        try:
            feed = self._feed()
            code, app = feed.get("code") or {}, feed.get("app") or {}
            if code and code["version"] in set_aside():
                self.status = "up to date"            # that version failed to start here; wait for the next
            elif code and vt(code["version"]) > vt(CODE_VERSION) and vt(code.get("min_app", "0")) <= vt(APP_VERSION):
                if self.ready != code["version"]:
                    self.status = f"downloading {code['version']}…"
                    self._install(code)
                    self.ready = code["version"]
                self.status = f"{code['version']} installs when Juniper is idle"
            elif app and vt(app["version"]) > vt(APP_VERSION):
                if not self.app_update or self.app_update["version"] != app["version"]:
                    self.app.notify("A new version of Juniper is ready",
                                    f"Version {app['version']}: download it from the menu bar panel (System).")
                self.app_update = app
                self.status = f"version {app['version']} available"
            else:
                self.status = "up to date"
            log("update", f"checked: {self.status}", "dim")
        except Exception as e:
            self.status = "couldn't check for updates" if manual else self.status
            log("update", f"check failed: {e!r}", "dim")
        finally:
            self.checking = False
            self.lock.release()

    def _feed(self):
        body = fetch(FEED)
        sig = base64.b64decode(fetch(FEED.rsplit("/", 1)[0] + "/feed.sig").strip())
        if not ed25519.verify(PUBLIC_KEY, body, sig):
            raise ValueError("the update feed isn't signed by Juniper's publisher")
        return json.loads(body)

    def _install(self, code):
        """Download, check the fingerprint the signed feed gave, unpack beside the current code."""
        CODE.mkdir(parents=True, exist_ok=True)
        blob = fetch(code["url"], timeout=120)
        if hashlib.sha256(blob).hexdigest() != code["sha256"]:
            raise ValueError("the downloaded update doesn't match the signed feed")
        version = code["version"]
        part, final = CODE / f"{version}.part", CODE / version
        shutil.rmtree(part, ignore_errors=True)
        part.mkdir()
        zpath = CODE / f"{version}.zip"
        zpath.write_bytes(blob)
        with zipfile.ZipFile(zpath) as z:
            for name in z.namelist():
                target = (part / name).resolve()
                if not str(target).startswith(str(part.resolve()) + os.sep):
                    raise ValueError(f"unsafe path in update: {name}")
            z.extractall(part)
        zpath.unlink()
        for f in (part / "bin").glob("*"):
            f.chmod(0o755)
        if (part / "version.txt").read_text().strip() != version:
            raise ValueError("the update's version.txt doesn't match the feed")
        shutil.rmtree(final, ignore_errors=True)
        part.rename(final)
        (CODE / "current.json").write_text(json.dumps({"version": version, "min_app": code.get("min_app", "0"),
                                                       "tries": 0, "ok": False}))
        # keep this version and the one before it (to fall back to); drop the rest
        keep = {version, CODE_VERSION}
        for old in CODE.iterdir():
            if old.is_dir() and old.name not in keep:
                shutil.rmtree(old, ignore_errors=True)
        log("update", f"code {version} downloaded and verified", "green")

    # ── applying ─────────────────────────────────────────────────────────────────────────
    def idle(self):
        """Nothing going on: not working, speaking, listening, asking, or holding a half-typed message."""
        app = self.app
        # A dev server or watcher Claude started counts as busy: restarting would stop it. The
        # update then simply starts with the next launch.
        busy = (any(r.busy() or r.bg_tasks for r in app.runners.values()) or app.speaker.speaking()
                or app.state.recording)
        typing = getattr(app, "ui_busy", lambda: False)()
        return not busy and not typing and not app.approvals.pending and time.time() - app.state.active_at > 90

    def apply(self):
        log("update", f"restarting into {self.ready}", "green")
        set_state(updated_from=CODE_VERSION)
        self.app.shutdown()
        restart()

    def download_app(self):
        if self.app_update:
            subprocess.Popen(["open", self.app_update["url"]])


def set_aside():
    """Versions boot.py gave up on because they never finished starting."""
    try:
        info = json.loads((CODE / "current.json").read_text())
        return {info["version"]} if info.get("bad") else set()
    except (OSError, ValueError, KeyError):
        return set()


def started(app):
    """Called once the app is up: this code works (boot.py stops counting failed starts), and if it
    just arrived as an update, say so."""
    current = CODE / "current.json"
    try:
        info = json.loads(current.read_text())
        if info.get("version") == CODE_VERSION and not info.get("ok"):
            info.update(ok=True, tries=0)
            current.write_text(json.dumps(info))
    except (OSError, ValueError):
        pass
    from juniper import get_state
    before = get_state().get("updated_from")
    if before and before != CODE_VERSION:
        app.notify("Juniper updated", f"Now on version {CODE_VERSION}.")
        set_state(updated_from=None)


def start(app):
    """The updater runs only in the packaged app (a folder checkout updates with git)."""
    if not BUNDLED:
        return None
    started(app)
    u = Updater(app)
    u.start()
    return u
