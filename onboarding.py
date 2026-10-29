"""First-run setup: a window that walks a new user through permissions, connecting the AIs they
use, choosing a voice, teaching Juniper their voice, and where their projects live. The menu bar's
"Add or change AIs" opens the same window with just the AI step (open_window(only="ai")).

The page is onboarding/index.html in a WKWebView; it talks to this file through
`window.webkit.messageHandlers.hc.postMessage({cmd, ...})`, and this file answers by calling
`hc.on(event, data)` in the page. Anything slow runs on a thread and answers on the main thread.
"""
import json
import os
import queue
import subprocess
import threading
import time
import urllib.request
from pathlib import Path

import AppKit
import AVFoundation
import objc
import WebKit
from Foundation import NSObject, NSURL

from juniper import (DATA, FRAME_BYTES, HERE, RATE, Recorder, VoiceID, enrol_lines, find_claude, get_state, log,
                        save_secret, secret, set_config, set_state)

PAGE = HERE / "onboarding" / "index.src.html"
LOGO = HERE / "onboarding" / "logo.svg.txt"          # the berries, as the app draws them

# ElevenLabs' built-in voices worth offering (the same set as config.toml's comments).
VOICES = [
    ("EXAVITQu4vr4xnSDxMaL", "Sarah", "American · warm"), ("cjVigY5qzO86Huf0OWal", "Eric", "American · smooth"),
    ("nPczCjzI2devNBz1zQrb", "Brian", "American · deep"), ("cgSgspJ2msm6clMCkdW9", "Jessica", "American · bright"),
    ("XrExE9yKIg1WjnnlVkGX", "Matilda", "American · friendly"), ("iP95p4xoKVk53GoZ742B", "Chris", "American · casual"),
    ("JBFqnCBsd6RMkjVDRZzb", "George", "British · warm"), ("onwK4e9ZLuTAKqWW03F9", "Daniel", "British · steady"),
    ("Xb7hH8MSUJpSbSDYk0k2", "Alice", "British · clear"), ("pFZP5JQG7iQjIQuC4Bku", "Lily", "British · soft"),
]


def on_main(fn):
    AppKit.NSOperationQueue.mainQueue().addOperationWithBlock_(fn)


def eleven(path, key, timeout=15):
    req = urllib.request.Request("https://api.elevenlabs.io/v1" + path, headers={"xi-api-key": key})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


class Setup:
    """What the page can ask for. Each handler gets the message dict; results go back with emit()."""

    def __init__(self, cfg, config_path, web, on_done=None):
        import connections
        self.cfg, self.config_path, self.web, self.on_done = cfg, config_path, web, on_done
        self.conns = connections.Connections()
        self.choices = {}            # what the user picked, written to config.toml at the end
        self.enroller = None
        self.done = False

    def emit(self, event, data=None):
        js = f"hc.on({json.dumps(event)}, {json.dumps(data)})"
        on_main(lambda: self.web.evaluateJavaScript_completionHandler_(js, None))

    def handle(self, msg):
        fn = getattr(self, "cmd_" + str(msg.get("cmd", "")), None)
        if fn:
            threading.Thread(target=self._safe, args=(fn, msg), daemon=True).start()

    def _safe(self, fn, msg):
        try:
            fn(msg)
        except Exception as e:   # anything unexpected shows up in the page instead of vanishing
            log("setup", f"{fn.__name__}: {e!r}", "red")
            self.emit("error", {"cmd": msg.get("cmd"), "message": str(e)})

    # ── start ────────────────────────────────────────────────────────────────────────────
    def cmd_hello(self, _):
        mac = subprocess.run(["sw_vers", "-productVersion"], capture_output=True, text=True).stdout.strip()
        self.emit("hello", {"user": os.environ.get("USER", ""), "macos": mac,
                            "apple_stt": int(mac.split(".")[0] or 0) >= 26})

    def cmd_open_url(self, msg):
        if str(msg.get("url", "")).startswith("https://"):
            subprocess.run(["open", msg["url"]])

    # ── permissions ──────────────────────────────────────────────────────────────────────
    def _mic_status(self):
        s = AVFoundation.AVCaptureDevice.authorizationStatusForMediaType_(AVFoundation.AVMediaTypeAudio)
        return {0: "ask", 1: "restricted", 2: "denied", 3: "granted"}.get(s, "ask")

    def cmd_mic_status(self, _):
        self.emit("mic", {"status": self._mic_status()})

    def cmd_mic_request(self, _):
        if self._mic_status() == "denied":
            subprocess.run(["open", "x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone"])
            return
        done = threading.Event()
        AVFoundation.AVCaptureDevice.requestAccessForMediaType_completionHandler_(
            AVFoundation.AVMediaTypeAudio, lambda ok: done.set())
        done.wait(120)
        self.emit("mic", {"status": self._mic_status()})

    # ── Claude Code ──────────────────────────────────────────────────────────────────────
    def cmd_claude_status(self, _):
        exe = find_claude(self.cfg.get("claude_path", ""))
        if not exe:
            self.emit("claude", {"installed": False})
            return
        version = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=30).stdout.strip()
        try:
            auth = json.loads(subprocess.run([exe, "auth", "status"], capture_output=True, text=True, timeout=30).stdout)
        except ValueError:
            auth = {}
        self.emit("claude", {"installed": True, "path": exe, "version": version.split(" ")[0],
                             "loggedIn": bool(auth.get("loggedIn")), "email": auth.get("email", ""),
                             "org": auth.get("orgName", ""), "plan": auth.get("subscriptionType", "")})

    def cmd_claude_install(self, _):
        """Anthropic's own installer (claude.ai/install.sh), with its output shown in the page."""
        p = subprocess.Popen(["/bin/bash", "-c", "curl -fsSL https://claude.ai/install.sh | bash"],
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        for line in p.stdout:
            if line.strip():
                self.emit("install_line", line.rstrip()[:160])
        p.wait()
        self.emit("install_done", {"ok": p.returncode == 0})
        self.cmd_claude_status(None)

    def cmd_claude_login(self, _):
        """Sign-in happens in the browser; Terminal hosts the login command (it may ask to paste a code)."""
        exe = find_claude(self.cfg.get("claude_path", "")) or "claude"
        script = f'tell application "Terminal" to do script "{exe} auth login"\ntell application "Terminal" to activate'
        subprocess.run(["osascript", "-e", script])

    # ── your AI ──────────────────────────────────────────────────────────────────────────
    def cmd_ai_state(self, _):
        import connections
        providers = [{"id": k, "name": p["name"], "maker": p["maker"], "blurb": p["blurb"], "ways": p["ways"],
                      "desc": connections.describe(k), "key_url": p.get("key_url", ""), "key_hint": p.get("key_hint", ""),
                      "note": p.get("note", "")} for k, p in connections.PROVIDERS.items()]
        if not self.conns.saved and self.on_done:
            # Opened from the menu bar by someone who's been using Claude with their subscription
            # all along: that counts as connected, so adding another AI doesn't drop it.
            self.conns.set("claude", provider="claude", way="subscription")
        connected = {k: {"way": v.get("way"), "model": v.get("model", ""), "models": v.get("models") or []}
                     for k, v in self.conns.saved.items()}
        self.emit("ai_state", {"providers": providers, "connected": connected,
                               "default": self.conns.default if self.conns.saved else None})

    def cmd_ai_connect(self, msg):
        import connections
        provider, way = msg.get("provider"), msg.get("way") or "key"
        if provider not in connections.PROVIDERS:
            return
        if provider == "claude" and way == "subscription":
            self.conns.set("claude", provider="claude", way="subscription")       # signed in (claude_status)
            r = {"ok": True, "message": "", "models": [], "model": ""}
        else:
            r = connections.connect(self.conns, provider, way, key=msg.get("key", ""), base_url=msg.get("base_url", ""),
                                    model=msg.get("model", ""), name=msg.get("name", ""))
        self.emit("ai_result", {"provider": provider, "way": way, **r, "default": self.conns.default})

    def cmd_ai_model(self, msg):
        if msg.get("provider") in self.conns.saved:
            self.conns.set(msg["provider"], model=str(msg.get("model", "")))

    def cmd_ai_default(self, msg):
        self.conns.set_default(msg.get("provider"))

    # ── voice ────────────────────────────────────────────────────────────────────────────
    def cmd_eleven_key(self, msg):
        key = (msg.get("key") or "").strip() or secret("ELEVENLABS_API_KEY")
        if not key:
            self.emit("eleven", {"ok": False})
            return
        try:
            sub = eleven("/user/subscription", key)
        except Exception as e:
            import urllib.error
            # Any refusal (400 for a wrong-length key, 401 for a wrong key) means the key; 429/5xx don't.
            bad = isinstance(e, urllib.error.HTTPError) and e.code < 429
            log("setup", f"ElevenLabs key check failed: {e!r}", "red")
            self.emit("eleven", {"ok": False, "message": "That key didn't work." if bad else
                                 "Couldn't reach ElevenLabs.", "fallback": True})
            return
        if msg.get("key"):
            save_secret("ELEVENLABS_API_KEY", key)
        voices = [{"id": v, "name": n, "desc": d} for v, n, d in VOICES]
        try:     # the user's own voices (cloned or designed) go first, with previews for all
            mine = eleven("/voices", key).get("voices", [])
            previews = {v["voice_id"]: v.get("preview_url") for v in mine}
            own = [{"id": v["voice_id"], "name": v["name"], "desc": "Your voice", "own": True}
                   for v in mine if v.get("category") in ("cloned", "generated", "professional")]
            voices = own + voices
            for v in voices:
                v["preview"] = previews.get(v["id"])
        except Exception:
            pass
        used, limit = sub.get("character_count", 0), sub.get("character_limit", 0)
        self.emit("eleven", {"ok": True, "tier": sub.get("tier", ""), "used": used, "limit": limit, "voices": voices,
                             "current": self.cfg.get("eleven_voice_id"), "exhausted": bool(limit) and used >= limit})

    def cmd_preview(self, msg):
        """Play a voice's sample. ElevenLabs serves them as text/plain, which WebKit may refuse to
        play, so Python downloads it and macOS's player plays it (stopping any sample still playing)."""
        url = str(msg.get("url", ""))
        if not url.startswith("https://"):
            return
        old = getattr(self, "preview_proc", None)
        if old and old.poll() is None:
            old.terminate()
        path = DATA / ".preview.mp3"
        with urllib.request.urlopen(url, timeout=15) as r:
            path.write_bytes(r.read())
        self.preview_proc = subprocess.Popen(["afplay", str(path)])

    def cmd_voice_choose(self, msg):
        if msg.get("mac"):
            self.choices.update(tts="say")
        else:
            self.choices.update(tts="elevenlabs", eleven_voice_id=msg["id"])

    def cmd_mac_voice_test(self, _):
        subprocess.run(["say", "Hi, I'm Juniper. Just ask."])

    # ── your voice (enrolment) ───────────────────────────────────────────────────────────
    def cmd_enroll_start(self, _):
        if self.enroller and self.enroller.is_alive():
            return
        self.enroller = Enroller(self)
        self.enroller.start()

    def cmd_enroll_skip(self, _):
        if self.enroller:
            self.enroller.stop = True
        self.choices.update(voice_check=False)

    # ── projects ─────────────────────────────────────────────────────────────────────────
    def cmd_projects_suggest(self, _):
        home = Path.home()
        dirs = ["Developer", "Projects", "projects", "Code", "code", "src", "dev", "repos", "GitHub",
                "Documents/GitHub", "Documents/Projects", "Desktop"]
        found = []
        for d in dirs:
            p = home / d
            if p.is_dir() and str(p) not in found:
                kids = [c for c in p.iterdir() if c.is_dir() and not c.name.startswith(".")][:200]
                found.append({"path": str(p), "label": "~/" + d, "count": len(kids)})
        self.emit("projects", {"suggest": found, "chosen": self.cfg.get("project_roots") or []})

    def cmd_projects_pick(self, _):
        def pick():
            panel = AppKit.NSOpenPanel.openPanel()
            panel.setCanChooseDirectories_(True)
            panel.setCanChooseFiles_(False)
            panel.setAllowsMultipleSelection_(True)
            panel.setPrompt_("Use these folders")
            panel.setMessage_("Pick the folders your projects live in (each project is a folder inside).")
            AppKit.NSApp.activateIgnoringOtherApps_(True)
            if panel.runModal() == AppKit.NSModalResponseOK:
                self.emit("picked", [str(u.path()) for u in panel.URLs()])
        on_main(pick)

    def cmd_projects_set(self, msg):
        home = str(Path.home())
        self.choices.update(project_roots=[p.replace(home, "~", 1) for p in msg.get("paths", [])])

    # ── finish ───────────────────────────────────────────────────────────────────────────
    def cmd_login_item(self, msg):
        app = os.environ.get("JUNIPER_APP", "")
        if msg.get("on") and app:
            subprocess.run(["osascript", "-e", 'tell application "System Events" to make login item at end with '
                                               f'properties {{path:"{app}", hidden:true}}'], capture_output=True)

    def cmd_finish(self, _):
        if self.on_done:                     # just the AI step, from the menu bar: Juniper keeps running
            self.done = True
            on_main(self.on_done)
            return
        if self.enroller:
            self.enroller.stop = True
        if self.choices:
            set_config(self.config_path, **self.choices)
        set_state(onboarded=True)
        log("setup", f"finished: {json.dumps(self.choices)}", "green")
        self.done = True
        on_main(lambda: AppKit.NSApp.stop_(None))


class Enroller(threading.Thread):
    """Records the five enrolment lines with live levels for the page, then builds the voice profile."""

    def __init__(self, setup):
        super().__init__(daemon=True)
        self.setup, self.stop = setup, False

    def run(self):
        import sounddevice as sd
        import webrtcvad
        cfg, emit = self.setup.cfg, self.setup.emit
        vad, audio, clips = webrtcvad.Vad(cfg["vad_level"]), queue.Queue(), []
        with sd.RawInputStream(samplerate=RATE, blocksize=FRAME_BYTES // 2, channels=1, dtype="int16",
                               callback=lambda d, *_: audio.put(bytes(d)), device=cfg["input_device"] or None):
            i, lines = 0, enrol_lines(cfg)
            while i < len(lines) and not self.stop:
                emit("enroll_line", {"i": i, "text": lines[i], "of": len(lines)})
                time.sleep(0.5)
                while not audio.empty():
                    audio.get()
                rec, buf, pcm, last = Recorder(cfg, vad, "enrol", [], time.time(), 10), b"", None, 0.0
                while pcm is None and not self.stop:
                    try:
                        buf += audio.get(timeout=0.5)
                    except queue.Empty:
                        continue
                    while len(buf) >= FRAME_BYTES and pcm is None:
                        frame, buf = buf[:FRAME_BYTES], buf[FRAME_BYTES:]
                        pcm = rec.push(frame, time.time())
                        now = time.time()
                        if now - last > 0.05:
                            import numpy as np
                            level = float(np.sqrt(np.mean((np.frombuffer(frame, np.int16) / 32768.0) ** 2)))
                            emit("level", min(1.0, level * 12))
                            last = now
                if self.stop:
                    return
                if len(pcm) < RATE * 2 * 1.5:
                    emit("enroll_retry", {"i": i})
                    continue
                clips.append(pcm)
                emit("enroll_got", {"i": i})
                i += 1
        emit("enroll_calibrating")
        voice = VoiceID(cfg)
        yours, closest, threshold = voice.enroll(clips)
        self.setup.choices.update(voice_check=True)
        emit("enroll_done", {"yours": [round(x, 2) for x in yours], "closest": round(closest, 2),
                             "threshold": round(threshold, 2), "clear": min(yours) > closest + 0.05})


class _Handler(NSObject):
    def userContentController_didReceiveScriptMessage_(self, controller, message):
        self.setup.handle(dict(message.body()))


class _NavDelegate(NSObject):
    """Logs the page loading (and WebKit's content process failing), so a blank window can be explained."""

    def webView_didFinishNavigation_(self, web, nav):
        log("setup", "page loaded", "dim")

    def webView_didFailProvisionalNavigation_withError_(self, web, nav, err):
        log("setup", f"page failed to load: {err}", "red")

    def webView_didFailNavigation_withError_(self, web, nav, err):
        log("setup", f"page failed: {err}", "red")

    def webViewWebContentProcessDidTerminate_(self, web):
        log("setup", "WebKit's content process stopped; reloading the page", "red")
        web.reload()


class _WindowDelegate(NSObject):
    def windowWillClose_(self, note):
        if self.on_close:
            self.on_close()
        else:
            AppKit.NSApp.stop_(None)


def open_window(cfg, config_path, only="", on_done=None):
    """Builds and shows the setup window. Returns (window, setup); keep them while it's open.
    only="ai" shows just the AI step, with on_done called (on the main thread) when it's done
    or closed, without stopping the app it's running in."""
    app = AppKit.NSApplication.sharedApplication()
    W, H = 820, 600
    style = (AppKit.NSWindowStyleMaskTitled | AppKit.NSWindowStyleMaskClosable |
             AppKit.NSWindowStyleMaskFullSizeContentView | AppKit.NSWindowStyleMaskMiniaturizable)
    win = AppKit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
        AppKit.NSMakeRect(0, 0, W, H), style, AppKit.NSBackingStoreBuffered, False)
    win.setTitlebarAppearsTransparent_(True)
    win.setTitleVisibility_(AppKit.NSWindowTitleHidden)
    win.setMovableByWindowBackground_(True)
    win.setBackgroundColor_(AppKit.NSColor.colorWithSRGBRed_green_blue_alpha_(0.06, 0.07, 0.095, 1))
    win.setAppearance_(AppKit.NSAppearance.appearanceNamed_(AppKit.NSAppearanceNameDarkAqua))
    win.setTitle_("Your AI · Juniper" if only else "Set up Juniper")
    win.center()

    conf = WebKit.WKWebViewConfiguration.alloc().init()
    controller = WebKit.WKUserContentController.alloc().init()
    conf.setUserContentController_(controller)
    web = WebKit.WKWebView.alloc().initWithFrame_configuration_(AppKit.NSMakeRect(0, 0, W, H), conf)
    web.setValue_forKey_(False, "drawsBackground")
    web.setAutoresizingMask_(AppKit.NSViewWidthSizable | AppKit.NSViewHeightSizable)
    closed = []

    def finish():
        if not closed:
            closed.append(True)
            win.orderOut_(None)
            on_done()
    setup = Setup(cfg, config_path, web, finish if on_done else None)
    handler = _Handler.alloc().init()
    handler.setup = setup
    controller.addScriptMessageHandler_name_(handler, "hc")
    nav = _NavDelegate.alloc().init()
    web.setNavigationDelegate_(nav)
    html = PAGE.read_text().replace("{{LOGO}}", LOGO.read_text()).replace("{{ONLY}}", only)
    web.loadHTMLString_baseURL_(html, NSURL.fileURLWithPath_(str(PAGE.parent) + "/"))
    win.setContentView_(web)
    delegate = _WindowDelegate.alloc().init()
    delegate.on_close = finish if on_done else None
    win.setDelegate_(delegate)
    win.setReleasedWhenClosed_(False)
    win.makeKeyAndOrderFront_(None)

    def bring_forward():
        # Once the event loop runs (activating before that is ignored): to the front, above other apps.
        win.orderFrontRegardless()
        if hasattr(app, "activate"):
            app.activate()                      # macOS 14+
        else:
            app.activateIgnoringOtherApps_(True)
        win.makeKeyWindow()
    on_main(bring_forward)
    log("setup", "showing the AI step" if only else "showing first-run setup", "green")
    setup.keep = (win, handler, nav, delegate, controller)    # Cocoa holds some of these weakly: keep them alive
    return win, setup


def run(cfg, config_path):
    """Shows the first-run setup until it's finished (True) or closed (False)."""
    app = AppKit.NSApplication.sharedApplication()
    app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyRegular)
    win, setup = open_window(cfg, config_path)
    app.run()
    win.orderOut_(None)
    return setup.done
