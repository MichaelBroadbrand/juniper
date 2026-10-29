#!/usr/bin/env python3
"""Juniper: a voice front end for Claude Code.

Listens on the microphone for "hey Juniper" (or a push-to-talk key), transcribes what follows
on this Mac (Apple's speech recognition, or Whisper), runs it through Claude Code (your normal
login, settings, CLAUDE.md, MCP servers and skills) and reads the reply aloud with ElevenLabs
(or the Mac's own voice). ui.py adds the menu bar icon, the overlay and the global keys.

Your audio never leaves the laptop; only the text of your request goes to Claude, and only
Claude's reply goes to ElevenLabs.
"""
import argparse
import collections
import json
import os
import queue
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import tomllib
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent          # the code, models and helpers (read-only in the app)
# Your things: settings, sessions, voice profile, logs, in Application Support (one set per Mac
# user, whether you run the app or the source). JUNIPER_HOME puts them somewhere else.
BUNDLED = os.environ.get("JUNIPER_BUNDLED") == "1"
SUPPORT = Path.home() / "Library" / "Application Support"
DATA = Path(os.environ.get("JUNIPER_HOME") or SUPPORT / "Juniper")
OLD_NAME = "Hey Claude"          # Juniper's earlier name: its data folder and Keychain entry


def _migrate():
    """Bring settings, sessions and the voice profile across, once, from the folder Juniper used
    under its earlier name. (Its downloaded code updates stay behind; they're for the old app.)"""
    old = SUPPORT / OLD_NAME
    if DATA.exists() or os.environ.get("JUNIPER_HOME") or not old.is_dir():
        return
    shutil.copytree(old, DATA, ignore=shutil.ignore_patterns(
        "code", ".approve.sock", ".lock", "keywords.txt", "*.log", "*.err", ".preview.mp3"))
    config = DATA / "config.toml"
    if config.exists():
        text = config.read_text().replace(f"# {OLD_NAME} settings.", "# Juniper settings.")
        # Its settings spelled out the old wake word; that's now the wake_name setting.
        text = re.sub(r'(?m)^wake_keywords = \["HH EY1 K L AO1 D @hey_claude".*$', 'wake_name = "Juniper"', text)
        config.write_text(text)


_migrate()
DATA.mkdir(parents=True, exist_ok=True)
# Models and compiled helpers stay in the app; the code itself may be a newer update (boot.py).
RES = Path(os.environ.get("JUNIPER_RES") or HERE)
CODE_VERSION = (HERE / "version.txt").read_text().strip() if (HERE / "version.txt").exists() else "dev"


def _app_version():
    try:
        import plistlib
        return plistlib.loads((RES.parent.parent / "Info.plist").read_bytes())["CFBundleShortVersionString"]
    except (OSError, KeyError, ValueError):
        return CODE_VERSION


APP_VERSION = _app_version() if BUNDLED else CODE_VERSION
# A plain Python for helper scripts. In the app, sys.executable is the app itself (its launcher runs
# Python inside the app's process), so the launcher says where the bundled interpreter is.
PYTHON = os.environ.get("JUNIPER_PYTHON") or sys.executable
RATE = 16000
FRAME_MS = 30
FRAME_BYTES = RATE * FRAME_MS // 1000 * 2  # 16-bit mono

# Speech recognition writes some wake names a few ways: the spellings it's been seen to produce.
MISHEARD = {"juniper": ["junipur", "junipa", "junica", "junior", "jennifer", r"june\s?per", r"juni\s?per"],
            "claude": ["claud", "clode", "klaud"]}
GREETING = r"(?:hey|hi|high|hay|okay|ok|yo)"


def wake_patterns(name):
    """Regexes for a wake name ("Juniper"), each capturing what was said after it:
    - wake: "hey Juniper …" anywhere in a phrase (the whisper wake engine; the greeting is required
      so that just mentioning the name in conversation doesn't trigger it)
    - verify: after the keyword spotter fires, the greeting may be cut off but the name must be there
    - lead: the name at the start of a push-to-talk request, to drop it."""
    key = name.strip().lower()
    names = "|".join([re.escape(key).replace(r"\ ", r"\s?")] + MISHEARD.get(key, []))
    return (re.compile(rf"\b{GREETING}[\s,.!-]*(?:{names})\b[\s,.!?-]*(.*)", re.I | re.S),
            re.compile(rf"\b(?:{GREETING}[\s,.!-]*)?(?:{names})\b[\s,.!?-]*(.*)", re.I | re.S),
            re.compile(rf"\W*(?:{GREETING}\W+)?(?:{names})\b\W*(.*)", re.I | re.S))


def wake_keywords(name, lexicon):
    """What the keyword spotter listens for: "hey/hi/okay <name>" as pronunciations (CMU phonemes,
    "Juniper" is JH UW1 N AH0 P ER0), looked up in the wake-word model's own dictionary."""
    words = name.upper().split()
    found = {}
    with open(lexicon) as f:
        for line in f:
            word, _, phones = line.strip().partition(" ")
            if word in words and word not in found:
                found[word] = phones.strip()
    missing = [w for w in words if w not in found]
    if missing:
        raise ValueError(f"the wake-word model doesn't know how to say {', '.join(missing).title()}; "
                         "pick another wake_name or set wake_keywords")
    phones, tag = " ".join(found[w] for w in words), re.sub(r"\W+", "_", name.strip().lower())
    return [f"HH EY1 {phones} @hey_{tag}", f"HH AY1 {phones} @hi_{tag}", f"OW2 K EY1 {phones} @okay_{tag}"]

STOP_RE = re.compile(r"^\W*(stop|cancel|never ?mind|shut up|be quiet|quiet|enough|that's enough)\b\W*$", re.I)
RESET_RE = re.compile(r"\b(new conversation|start over|new chat|fresh start|reset)\b", re.I)
# What Whisper tends to "hear" in background noise.
HALLUCINATIONS = {"", "you", "thank you", "thanks", "thanks for watching", "bye", "okay", "ok", "hmm", "uh", "um"}

VOICE_PROMPT = """You are being driven by voice. The user spoke this request out loud on their Mac and it was \
transcribed by speech recognition, so expect misheard words and infer the intended meaning. The voice app \
they're using is called {name}: they start requests with "hey {name}", and that's you.

Everything you write as text is read aloud by text-to-speech, so:
- Keep replies short and conversational: one to three sentences unless the user asks for more.
- No markdown, bullet points, tables, headings, code blocks, file paths or URLs in what you say. Say numbers naturally.
- Before a task that will take more than a few seconds, say one short sentence about what you're about to do.
- When you finish work, say in a sentence or two what you did.
- If the answer is long (code, a document, a list, a table), save it to a file or open it on screen with `open`, \
and say where it is instead of reading it out.
- If the request is ambiguous or risky, ask one short question instead of guessing; the user can answer by voice.
- What you get is a speech transcript, so a word that doesn't fit is probably a mishearing of a similar-sounding name (e.g. "Italy" for "Nidalee"). Go with the closest likely term from the context and say which you took it to mean, rather than asking the user to spell it."""

DEFAULTS = {
    "working_dir": "~",
    "permission_mode": "auto",
    "model": "",
    "claude_path": "",
    "project_roots": [],
    "router_model": "haiku",
    "followup_seconds": 8,
    "listen_seconds": 6,
    "voice": "",
    "speech_rate": 200,
    "wake_engine": "sherpa",
    "sherpa_model": "sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20",
    "wake_name": "Juniper",
    "wake_keywords": [],              # empty = worked out from wake_name
    "wake_threshold": 0.15,
    "wake_boost": 1.5,
    "stt_engine": "apple",
    "stt_locale": "",                 # empty = this Mac's language and region
    "wake_model": "base.en",
    "command_model": "base.en",
    "vocabulary": "",
    "input_device": "",
    "vad_level": 2,
    "end_silence_ms": 1000,
    "max_utterance_seconds": 30,
    "chimes": True,
    "voice_check": True,
    "voice_threshold": 0,
    "speaker_model": "wespeaker_en_voxceleb_resnet34_LM.onnx",
    "tts": "elevenlabs",
    "eleven_voice_id": "EXAVITQu4vr4xnSDxMaL",
    "eleven_model": "eleven_v4_turbo",
    "open_mic": True,
    "ptt_key": "ctrl+option+space",
    "mic_key": "ctrl+option+m",
    "stop_key": "ctrl+option+escape",
    "overlay": True,
    "overlay_seconds": 8,
    "show_logo": True,
    "logo_size": 48,
    "speak_answers": True,
    "speak_typed": True,
    "logo_key": "ctrl+option+o",
    "type_key": "ctrl+option+t",
}

LOG_PATH = DATA / "juniper.log"
STATE_PATH = DATA / ".state.json"


def log(tag, msg, color=""):
    colors = {"dim": "\033[2m", "cyan": "\033[36m", "green": "\033[32m", "yellow": "\033[33m", "red": "\033[31m"}
    stamp = time.strftime('%H:%M:%S')
    print(f"{colors.get(color, '')}{stamp} {tag:<7}\033[0m {msg}", flush=True)
    try:
        with open(LOG_PATH, "a") as f:
            f.write(f"{time.strftime('%Y-%m-%d')} {stamp} {tag:<7} {msg}\n")
    except OSError:
        pass


KEYCHAIN_SERVICE = "Juniper"


def secret(name):
    """A secret (the ElevenLabs key): from .env in your data folder, else the macOS Keychain.
    Kept out of os.environ so Claude's tools never see it."""
    value = read_env().get(name, "")
    if value:
        return value
    for service in (KEYCHAIN_SERVICE, OLD_NAME):
        out = subprocess.run(["security", "find-generic-password", "-s", service, "-a", name, "-w"],
                             capture_output=True, text=True)
        if out.returncode == 0 and out.stdout.strip():
            if service != KEYCHAIN_SERVICE:
                save_secret(name, out.stdout.strip())       # keep it under the new name from now on
            return out.stdout.strip()
    return ""


def save_secret(name, value):
    """Store a secret in the login Keychain (replacing any old one); empty removes it."""
    subprocess.run(["security", "delete-generic-password", "-s", KEYCHAIN_SERVICE, "-a", name],
                   capture_output=True)
    if value:
        subprocess.run(["security", "add-generic-password", "-s", KEYCHAIN_SERVICE, "-a", name, "-w", value,
                        "-j", "Juniper's ElevenLabs key"], capture_output=True, check=True)


def retire_old_app(app):
    """Juniper replaces the app it used to be: if that's still installed or running, quit it and move
    it to the Bin (where Put Back restores it), so the two don't both listen, or both start at login.
    Its settings, sessions and voice profile have already come across (_migrate)."""
    running = subprocess.run(["pkill", "-x", "HeyClaude"], capture_output=True).returncode == 0
    old = [d / f"{OLD_NAME}.app" for d in (Path("/Applications"), Path.home() / "Applications")
           if (d / f"{OLD_NAME}.app").exists()]
    if not (running or old):
        return
    time.sleep(2)                       # let it finish quitting
    from Foundation import NSURL, NSFileManager
    kept = [str(a) for a in old if not NSFileManager.defaultManager().trashItemAtURL_resultingItemURL_error_(
        NSURL.fileURLWithPath_(str(a)), None, None)[0]]
    log("setup", f"retired {OLD_NAME}: {'quit it, ' if running else ''}"
                 f"{'moved it to the Bin' if len(kept) < len(old) else 'left it in place'}"
                 f"{f' (could not move {kept})' if kept else ''}", "dim")
    app.notify(f"{OLD_NAME} is now Juniper", "Your settings and sessions came across. "
               + (f"Delete {OLD_NAME} from Applications when you can." if kept
                  else f"{OLD_NAME} is in the Bin."))


def system_locale():
    """This Mac's language and region as a speech-recognition locale, e.g. "en-GB"."""
    out = subprocess.run(["defaults", "read", "-g", "AppleLocale"], capture_output=True, text=True).stdout.strip()
    locale = out.split("@")[0].replace("_", "-") or "en-US"
    return locale if "-" in locale else locale + "-US"


def macos_major():
    import platform
    try:
        return int(platform.mac_ver()[0].split(".")[0])
    except ValueError:
        return 0


def find_claude(configured=""):
    """The `claude` command: from settings, your PATH, or the places its installers put it. Apps
    started from Finder get a bare PATH, so the usual install locations are checked too."""
    if configured:
        return os.path.expanduser(configured)
    home = Path.home()
    extra = [home / ".local/bin", home / ".claude/local", Path("/opt/homebrew/bin"), Path("/usr/local/bin"),
             home / ".npm-global/bin", home / ".bun/bin", home / ".volta/bin"]
    extra += sorted((home / ".nvm/versions/node").glob("*/bin"), reverse=True)
    found = shutil.which("claude") or shutil.which("claude", path=os.pathsep.join(map(str, extra)))
    return found or ""


def read_env():
    """Secrets from .env in your data folder (developer setups; the app uses the Keychain)."""
    env = {}
    path = DATA / ".env"
    if path.exists():
        for line in path.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def key_hint(spec):
    """'ctrl+option+space' -> '⌃⌥Space' for menus."""
    sym = {"ctrl": "⌃", "control": "⌃", "option": "⌥", "alt": "⌥", "shift": "⇧", "cmd": "⌘", "command": "⌘"}
    parts = spec.lower().split("+")
    return "".join(sym.get(p, "") for p in parts[:-1]) + parts[-1].capitalize()


def get_state():
    try:
        return json.loads(STATE_PATH.read_text())
    except (OSError, ValueError):
        return {}


def set_state(**changes):
    """Merge into .state.json (prefs from the menu, and whether setup has been done)."""
    try:
        STATE_PATH.write_text(json.dumps({**get_state(), **changes}))
    except OSError:
        pass


def needs_setup():
    """The first-run setup: on a new install of the app, or after "Set up again…" in the menu."""
    onboarded = get_state().get("onboarded")
    return onboarded is False or (BUNDLED and onboarded is None)


def set_config(path, **values):
    """Change settings in config.toml, keeping its comments: replaces `key = …` lines or appends."""
    path = Path(path)
    lines = path.read_text().splitlines() if path.exists() else []
    for key, value in values.items():
        text = (("true" if value else "false") if isinstance(value, bool) else
                json.dumps(value) if isinstance(value, (str, list)) else str(value))
        line = f"{key} = {text}"
        for i, old in enumerate(lines):
            if re.match(rf"\s*{re.escape(key)}\s*=", old):
                lines[i] = line
                break
        else:
            lines.append(line)
    path.write_text("\n".join(lines) + "\n")


def ensure_config(path):
    """A new user's config.toml: the commented template from the app, so Settings… has something to show."""
    path = Path(path)
    if not path.exists() and (HERE / "config.template.toml").exists():
        shutil.copy(HERE / "config.template.toml", path)


def load_config(path):
    cfg = dict(DEFAULTS)
    if path.exists():
        with open(path, "rb") as f:
            cfg.update(tomllib.load(f))
    return cfg


def chime(cfg, name):
    if cfg["chimes"]:
        subprocess.Popen(["afplay", f"/System/Library/Sounds/{name}.aiff"], stderr=subprocess.DEVNULL)


def to_speech(text):
    """Strip markdown so `say` doesn't read out asterisks and backticks."""
    text = re.sub(r"```.*?```", " There's a code block on screen. ", text, flags=re.S)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"https?://\S+", "the link on screen", text)
    lines = []
    for line in text.splitlines():
        if line.strip().startswith("|"):
            continue
        line = re.sub(r"^\s*#{1,6}\s*", "", line)
        line = re.sub(r"^\s*[-*•]\s+", "", line)
        lines.append(line)
    text = " ".join(lines)
    text = re.sub(r"[*_~]{1,3}", "", text)
    return re.sub(r"\s+", " ", text).strip()


class Speaker:
    """Reads text aloud one chunk at a time, and can be cut off mid-sentence.

    Uses ElevenLabs (streamed, so speech starts within about half a second) when there's a key,
    and the Mac voice (`say`) whenever ElevenLabs doesn't work: a bad key or used-up quota switches
    to the Mac voice until restart, a network problem for a minute, and you get one notification.
    """

    def __init__(self, cfg, mute=False):
        self.cfg, self.mute = cfg, mute
        self.q = queue.Queue()
        self.proc = None
        self.lock = threading.Lock()
        self.quiet_since = time.time()
        self.active = False
        self.interrupted = threading.Event()
        self.eleven_key = secret("ELEVENLABS_API_KEY")
        self.current = None       # () -> the open session's name, set by App
        self.use_eleven = cfg["tts"] == "elevenlabs" and bool(self.eleven_key)
        self.eleven_paused_until = 0.0   # after a network problem: the Mac voice until then
        self.on_problem = None           # (message) -> None: App shows it as a notification
        self.problem = ""                # the last problem reported (each is reported once)
        self.played = False              # did the current ElevenLabs chunk get as far as the speakers?
        threading.Thread(target=self._loop, daemon=True).start()

    def say(self, text, tag=None):
        """Queue text to speak. With a session tag, it's only spoken while that session is open."""
        text = to_speech(text)
        if text:
            self.active = True
            self.q.put((text, tag))

    def stop(self):
        while not self.q.empty():
            try:
                self.q.get_nowait()
            except queue.Empty:
                break
        self.interrupted.set()
        with self.lock:
            if self.proc and self.proc.poll() is None:
                self.proc.terminate()

    def speaking(self):
        return self.active

    def wait(self):
        while self.active:
            time.sleep(0.1)

    def _loop(self):
        while True:
            text, tag = self.q.get()
            self.interrupted.clear()
            if tag is not None and self.current and self.current() != tag:
                pass                                   # from a session you've switched away from
            elif self.mute:
                log("say", text, "dim")
            elif self.use_eleven and time.time() >= self.eleven_paused_until:
                try:
                    self._eleven(text)
                except Exception as e:
                    self._eleven_failed(e, text)
            else:
                self._say(text)
            if self.q.empty():
                self.active = False
                self.quiet_since = time.time()

    def _eleven_failed(self, e, text):
        """Speak this chunk with the Mac voice instead, and keep using it while ElevenLabs is broken."""
        reason, lasting = eleven_problem(e)
        if lasting:
            self.use_eleven = False
            self.eleven_paused_until = 0.0
        else:
            self.eleven_paused_until = time.time() + 60
        log("error", f"ElevenLabs: {reason} ({e!r}); using the Mac voice"
                     f"{'' if lasting else ' for a minute'}", "red")
        if self.on_problem and reason != self.problem:
            self.on_problem(reason[0].upper() + reason[1:] + (", so replies use the Mac voice. Fix it in "
                            "Set up again… (menu bar)." if lasting else ", so replies use the Mac voice for now."))
        self.problem = reason
        if not self.played and not self.interrupted.is_set():
            self._say(text)                  # nothing reached the speakers, so nothing is repeated

    def _say(self, text):
        args = ["say", "-r", str(self.cfg["speech_rate"])]
        if self.cfg["voice"]:
            args += ["-v", self.cfg["voice"]]
        with self.lock:
            self.proc = subprocess.Popen(args + ["--", text])
        self.proc.wait()

    def _eleven(self, text):
        import urllib.request
        import sounddevice as sd
        url = f"https://api.elevenlabs.io/v1/text-to-speech/{self.cfg['eleven_voice_id']}/stream?output_format=pcm_24000"
        req = urllib.request.Request(url, data=json.dumps({"text": text, "model_id": self.cfg["eleven_model"]}).encode(),
                                     headers={"xi-api-key": self.eleven_key, "Content-Type": "application/json"})
        self.played = False
        # A short timeout: offline, the Mac voice should take over in seconds, not after a long silence.
        with urllib.request.urlopen(req, timeout=6) as r, \
                sd.RawOutputStream(samplerate=24000, channels=1, dtype="int16") as out:
            self.problem = ""                # working again (after a network blip)
            left = b""
            while not self.interrupted.is_set():
                chunk = r.read(4800)
                if not chunk:
                    break
                chunk, left = left + chunk, b""
                if len(chunk) % 2:
                    chunk, left = chunk[:-1], chunk[-1:]
                out.write(chunk)
                self.played = True
            if self.interrupted.is_set():
                out.abort()


def eleven_problem(e):
    """What went wrong with ElevenLabs, in plain words, and whether it lasts (key, quota, voice)
    or may pass (network, busy, server trouble)."""
    import urllib.error
    if isinstance(e, urllib.error.HTTPError):
        try:
            detail = json.loads(e.read().decode() or "{}").get("detail") or {}
        except (ValueError, OSError):
            detail = {}
        detail = detail if isinstance(detail, dict) else {}
        status = str(detail.get("status", ""))
        kind = " ".join(str(detail.get(k, "")) for k in ("status", "code", "type"))
        if "quota" in kind or "credits" in kind or e.code == 402:
            return "your ElevenLabs quota is used up", True
        # A bad key comes back as 401, or as 400 when it's the wrong length; both say authentication_error.
        if e.code == 401 or "invalid_api_key" in kind or "authentication" in kind:
            return "the ElevenLabs key isn't valid", True
        if e.code == 404:
            return "the chosen ElevenLabs voice isn't on your account", True
        if e.code in (403, 422):
            return f"ElevenLabs refused the request ({status or e.code})", True
        if e.code == 429:
            return "ElevenLabs is busy (too many requests)", False
        return f"ElevenLabs had a problem ({e.code})", False
    if type(e).__name__ == "PortAudioError":
        return "the speakers couldn't play ElevenLabs audio", False
    return "ElevenLabs can't be reached", False


class State:
    """What the menu bar icon and overlay show. Worker threads write it; the UI polls it."""

    def __init__(self):
        self.lock = threading.Lock()
        self.recording = False
        self.heard = ""          # what you said
        self.level = 0.0         # mic loudness, 0..1, for the overlay's listening dot
        self.active_at = 0.0     # last time anything happened worth showing
        self.working = False     # sorting out a session command
        self.followup = False    # listening for a follow-up question (no "hey Juniper" needed)
        self.dismissed = False   # overlay closed (clicked, or the request was ignored); reopens next turn
        self.dismissed_at = 0.0  # when it was closed: a reply after that earns the logo a dot

    def set(self, **changes):
        with self.lock:
            for k, v in changes.items():
                setattr(self, k, v)
            self.active_at = time.time()



def tool_part(block, folder):
    """How a tool call shows in the overlay, Claude Code style: "⏺ Edit  src/app.ts  +3 −1" and,
    for file changes, the first few lines of the diff."""
    name, inp = block.get("name", ""), block.get("input") or {}
    root = folder.rstrip("/") + "/"

    def short(path):
        path = str(path or "")
        return path[len(root):] if path.startswith(root) else path.replace(str(Path.home()), "~", 1)

    label, arg, diff = name, "", []
    if name == "Bash":
        arg = (inp.get("command") or "").strip().split("\n")[0]
    elif name in ("Read", "Write", "Edit", "MultiEdit", "NotebookEdit"):
        arg = short(inp.get("file_path") or inp.get("notebook_path"))
    elif name in ("Grep", "Glob"):
        arg = inp.get("pattern", "")
    elif name == "WebFetch":
        arg = inp.get("url", "")
    elif name == "WebSearch":
        arg = inp.get("query", "")
    elif name in ("Task", "Agent"):
        arg = inp.get("description", "")
    elif name == "TodoWrite":
        label = "Update to-dos"
    elif name.startswith("mcp__"):
        bits = name.split("__")
        label = f"{bits[1].replace('_', ' ')} · {bits[-1].replace('_', ' ')}"
    else:
        arg = str(next((v for v in inp.values() if isinstance(v, str)), ""))
    if name == "Edit":
        diff = [("-", l) for l in str(inp.get("old_string", "")).split("\n")] + \
               [("+", l) for l in str(inp.get("new_string", "")).split("\n")]
    elif name == "MultiEdit":
        for e in inp.get("edits") or []:
            diff += [("-", l) for l in str(e.get("old_string", "")).split("\n")] + \
                    [("+", l) for l in str(e.get("new_string", "")).split("\n")]
    elif name == "Write":
        diff = [("+", l) for l in str(inp.get("content", "")).split("\n")]
    return {"t": "tool", "id": block.get("id"), "name": label, "arg": arg[:120],
            "add": sum(1 for k, _ in diff if k == "+"), "del": sum(1 for k, _ in diff if k == "-"),
            "diff": [[k, l[:160]] for k, l in diff[:8]], "more": max(0, len(diff) - 8), "ok": None}


def describe_tool(block):
    """A short human description of a tool call for the overlay."""
    inp = block.get("input") or {}
    if inp.get("description"):
        return inp["description"]
    name = block.get("name", "")
    for key in ("command", "file_path", "pattern", "query", "url", "prompt"):
        if isinstance(inp.get(key), str):
            return f"{name}: {inp[key][:60]}"
    return name


class Sessions:
    """Named conversations, each tied to a folder, kept until you clear them (sessions.json).

    Each one is an ordinary Claude Code session in that folder, so the folder's CLAUDE.md,
    memory and settings apply, and `claude --resume` there shows it in the terminal too.
    """

    PATH = DATA / "sessions.json"

    def __init__(self, cfg):
        self.lock = threading.Lock()
        home = os.path.expanduser(cfg["working_dir"])
        data = {}
        if self.PATH.exists():
            try:
                data = json.loads(self.PATH.read_text())
            except ValueError:
                pass
        self.items = data.get("sessions") or {}
        self.items.setdefault("general", {"folder": home, "session_id": None, "turns": [], "last_used": 0})
        self.current = data.get("current") if data.get("current") in self.items else "general"

    def get(self, name=None):
        return self.items[name or self.current]

    def names(self):
        """General first, then the most recently used."""
        rest = sorted((n for n in self.items if n != "general"), key=lambda n: -self.items[n]["last_used"])
        return ["general"] + rest

    def find(self, name):
        if not name:
            return None
        for n in self.items:
            if n.lower() == str(name).lower():
                return n
        return None

    def switch(self, name):
        with self.lock:
            self.current = name
            self.items[name]["last_used"] = time.time()
        self.save()

    def create(self, name, folder, session_id=None):
        with self.lock:
            base, n = name or os.path.basename(folder) or "session", 2
            name = base
            while self.find(name):
                name, n = f"{base} {n}", n + 1
            self.items[name] = {"folder": folder, "session_id": session_id, "turns": [], "last_used": time.time()}
            self.current = name
        self.save()
        return name

    def clear(self, name=None):
        with self.lock:
            s = self.items[name or self.current]
            s["session_id"], s["turns"], s["unread"] = None, [], False
        self.save()

    def new_turn(self, name, you):
        with self.lock:
            s = self.items[name]
            s["turns"] = (s["turns"] + [{"you": you, "claude": "", "at": time.time()}])[-8:]

    def add_reply(self, name, text):
        with self.lock:
            turns = self.items[name]["turns"]
            if not turns:
                turns.append({"you": "", "claude": "", "at": time.time()})
            turns[-1]["claude"] = (turns[-1]["claude"] + "\n\n" + text.strip()).strip()
            turns[-1].setdefault("parts", []).append({"t": "text", "v": text.strip()})

    def add_tool(self, name, part):
        """A command Claude ran or a file it changed, shown (not spoken) in the conversation."""
        with self.lock:
            turns = self.items[name]["turns"]
            if turns:
                parts = turns[-1].setdefault("parts", [])
                parts.append(part)
                del parts[:-60]

    def finish_tool(self, name, tool_id, ok):
        with self.lock:
            for t in reversed(self.items[name]["turns"][-4:]):     # an answer to Claude can come in between
                for part in t.get("parts", []):
                    if part.get("id") == tool_id:
                        part["ok"] = ok

    def turns(self, name=None):
        with self.lock:
            return [dict(t) for t in self.items[name or self.current]["turns"]]

    def set_unread(self, name, unread):
        with self.lock:
            self.items[name]["unread"] = unread

    def unread(self):
        return [n for n in self.names() if self.items[n].get("unread")]

    def save(self):
        with self.lock:
            for s in self.items.values():
                s["turns"] = s["turns"][-8:]
            data = json.dumps({"current": self.current, "sessions": self.items}, indent=1)
        try:
            self.PATH.write_text(data)
        except OSError:
            pass

    @staticmethod
    def transcript(folder, session_id):
        """Where Claude Code keeps a conversation (it only exists once something has been saved)."""
        return Path.home() / ".claude" / "projects" / re.sub(r"[^A-Za-z0-9]", "-", folder) / f"{session_id}.jsonl"

    @staticmethod
    def latest_terminal_session(folder):
        """The most recent Claude Code session started in a folder (e.g. one from the terminal)."""
        d = Path.home() / ".claude" / "projects" / re.sub(r"[^A-Za-z0-9]", "-", folder)
        files = sorted(d.glob("*.jsonl"), key=lambda f: f.stat().st_mtime) if d.is_dir() else []
        return files[-1].stem if files else None


def claude_env():
    """Use the subscription login, not an API key, and don't look like a nested session."""
    return {k: v for k, v in os.environ.items()
            if k not in ("ANTHROPIC_API_KEY", "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SSE_PORT")}


# Requests with any of these words might be about sessions; a quick Claude call decides whether they are.
SESSION_WORDS = re.compile(r"\b(sessions?|switch|swap|go back to|compact|clear|start (?:over|fresh)|new (?:conversation|chat)"
                           r"|pick up|fresh conversation)\b", re.I)

ROUTER_PROMPT = """You route voice commands for a voice assistant that keeps several Claude Code sessions, \
each tied to a project folder. Speech recognition often mangles names (e.g. "store front" or "stored front" for storefront), \
so match names by sound and meaning.

Sessions (name: folder): {sessions}
Current session: {current}
Project folders: {folders}
Home folder: {home}

Command: "{command}"

Reply with only a JSON object: {{"action": ..., "session": ..., "folder": ..., "then": ...}}
action is one of:
- "switch": go to an existing session ("session" = its exact name).
- "new": start a new session ("session" = a short name, "folder" = one of the project folders or the home folder).
- "continue": pick up the user's most recent Claude Code session in a folder, e.g. one from the terminal \
("session" = a short name, "folder" = the folder).
- "clear": wipe a session's conversation so it starts fresh ("session" = its exact name, or the current one).
- "compact": compact (summarise) the current conversation to free up context.
- "list": the user wants to know which sessions exist.
- "current": the user wants to know which session they're in.
- "none": not about sessions; an ordinary request for the assistant.
"then": the rest of the command, if it asks for anything beyond the session action, copied word for word \
(e.g. "switch to storefront and check the sync" -> "check the sync"); "" if nothing."""


class Router:
    def __init__(self, cfg, claude):
        self.cfg, self.claude = cfg, claude

    def folders(self, sessions):
        found = {s["folder"] for s in sessions.items.values()}
        for root in self.cfg["project_roots"]:
            root = Path(os.path.expanduser(root))
            if root.is_dir():
                found |= {str(p) for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")}
        return sorted(found)

    def route(self, text, sessions):
        prompt = ROUTER_PROMPT.format(
            sessions=json.dumps({n: s["folder"] for n, s in sessions.items.items()}), current=sessions.current,
            folders=json.dumps(self.folders(sessions)), home=os.path.expanduser("~"), command=text)
        out = subprocess.run(
            [self.claude, "-p", prompt, "--model", self.cfg["router_model"], "--output-format", "json",
             "--system-prompt", "You route voice commands. Reply with JSON only.", "--setting-sources", "",
             "--strict-mcp-config", "--tools", "", "--disable-slash-commands", "--no-session-persistence"],
            capture_output=True, text=True, timeout=60, env=claude_env(), cwd=str(HERE))
        result = json.loads(out.stdout)["result"]
        result = json.loads(result[result.index("{"):result.rindex("}") + 1])
        log("route", json.dumps(result), "dim")
        return result


MODES = [("auto", "Auto"), ("manual", "Ask before acting"), ("acceptEdits", "Accept edits"), ("plan", "Plan"),
         ("bypassPermissions", "Bypass permissions")]
MODELS = [("claude-opus-5-5", "Opus 5.5"), ("claude-sonnet-5-5", "Sonnet 5.5"), ("claude-haiku-5-5", "Haiku 5.5"),
          ("claude-fable-5-1", "Fable 5.1"), ("claude-opus-4-8", "Opus 4.8")]
EDIT_TOOLS = {"Edit", "MultiEdit", "Write", "NotebookEdit"}
# Juniper's own slash commands (Claude Code's, and your skills, come from Claude Code itself).
LOCAL_COMMANDS = [("/clear", "Start this session fresh"), ("/compact", "Summarise the conversation to free up context"),
                  ("/mode", "Switch mode: auto, ask, edits, plan, bypass"), ("/model", "Switch model: opus, sonnet, haiku, fable"),
                  ("/sessions", "Show the sessions sidebar"), ("/effort", "How hard Claude thinks: low … max"),
                  ("/usage", "Your plan's usage limits and account"), ("/new", "New session for a project folder, e.g. /new storefront"),
                  ("/stop", "Stop the current task"), ("/help", "What you can type here")]
YES_RE = re.compile(r"^\W*(yes|yeah|yep|yup|sure|ok|okay|allow( it)?|approve( it)?|go ahead|do it|go for it|fine|"
                    r"that's fine|sounds good|please do)\b", re.I)
NO_RE = re.compile(r"^\W*(no|nope|nah|deny|don't|do not|reject|cancel|not now|stop)\b", re.I)


def mode_label(mode):
    return dict(MODES).get(mode, mode)


def model_label(model):
    return dict(MODELS).get(model, model or "Default")


def find_choice(text, choices):
    """Match what you said or typed to one of a few labels (by name, or "the second one")."""
    t = text.lower()
    for i, word in enumerate(["first", "second", "third", "fourth", "fifth"]):
        if re.search(rf"\b({word}|option {i + 1}|number {i + 1})\b", t) and i < len(choices):
            return choices[i]
    for c in sorted(choices, key=len, reverse=True):
        if c.lower() in t:
            return c
    return None


class Approvals:
    """Answers Claude Code's permission requests (see bin/approve_mcp.py) by asking you in the
    overlay, or by voice. Each request waits until you answer; tasks in other sessions keep
    going, and their requests wait in their own sessions."""

    # Unix socket paths are limited to ~104 bytes; very long home folders fall back to /tmp (still 0600).
    PATH = DATA / ".approve.sock" if len(str(DATA / ".approve.sock")) < 100 else Path(f"/tmp/juniper-{os.getuid()}.sock")

    def __init__(self, app):
        self.app = app
        self.lock = threading.Lock()
        self.pending = []                     # [{"id", "session", "tool_name", "input", "event", "decision"}]
        self.count = 0

    def listen(self):
        """Start answering the bridge (only the running app does this, not tests or --file runs)."""
        self.PATH.unlink(missing_ok=True)
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(str(self.PATH))
        os.chmod(self.PATH, 0o600)            # only you can ask (or answer) through it
        self.server.listen(8)
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            conn, _ = self.server.accept()
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn):
        try:
            data = b""
            while not data.endswith(b"\n"):
                chunk = conn.recv(65536)
                if not chunk:
                    return
                data += chunk
            req = json.loads(data)
            decision = self.decide(req)
            conn.sendall((json.dumps(decision) + "\n").encode())
        except (OSError, ValueError) as e:
            log("error", f"approval: {e!r}", "red")
        finally:
            conn.close()

    def decide(self, req):
        app, name, tool, inp = self.app, req.get("session") or "general", req.get("tool_name", ""), req.get("input") or {}
        session = app.sessions.items.get(name) or {}
        # Settled without asking: accept-edits mode, or something you allowed for this session.
        if session.get("mode") == "acceptEdits" and tool in EDIT_TOOLS:
            return {"behavior": "allow", "updatedInput": inp}
        if self.allow_key(tool, inp) in (session.get("allowed") or []):
            return {"behavior": "allow", "updatedInput": inp}
        with self.lock:
            self.count += 1
            item = {"id": self.count, "session": name, "tool_name": tool, "input": inp,
                    "event": threading.Event(), "decision": None, "answers": {}}
            self.pending.append(item)
        log("ask", f"[{name}] {tool} {json.dumps(inp)[:120]}", "yellow")
        self.announce(item)
        item["event"].wait()
        with self.lock:
            self.pending.remove(item)
        log("ask", f"[{name}] {tool} -> {item['decision'].get('behavior')}", "yellow")
        return item["decision"]

    @staticmethod
    def allow_key(tool, inp):
        """What "Allow for this session" remembers: the tool, and for commands the program name."""
        if tool == "Bash":
            return "Bash:" + ((inp.get("command") or "").strip().split() or [""])[0]
        return tool

    def announce(self, item):
        app, name, tool = self.app, item["session"], item["tool_name"]
        if name != app.sessions.current:
            app.notify(f"{name} needs your answer", self.summary(item))
            return
        app.state.set(dismissed=False)
        chime(app.cfg, "Ping")
        if app.should_speak(app.runner_for(name).typed):
            app.speaker.say(self.spoken(item), tag=name)

    @staticmethod
    def summary(item):
        tool, inp = item["tool_name"], item["input"]
        if tool == "Bash":
            return "Run: " + (inp.get("command") or "")[:120]
        if tool in EDIT_TOOLS:
            return f"{tool}: {os.path.basename(str(inp.get('file_path') or inp.get('notebook_path') or ''))}"
        if tool == "ExitPlanMode":
            return "The plan is ready. Go ahead?"
        if tool == "AskUserQuestion":
            qs = inp.get("questions") or []
            return qs[0].get("question", "A question for you") if qs else "A question for you"
        return f"Use {tool}"

    @staticmethod
    def spoken(item):
        tool, inp = item["tool_name"], item["input"]
        if tool == "Bash":
            return "Can I run a command? It's on screen."
        if tool in EDIT_TOOLS:
            return f"Can I change {os.path.basename(str(inp.get('file_path') or 'a file'))}?"
        if tool == "ExitPlanMode":
            return "That's my plan. Shall I go ahead?"
        if tool == "AskUserQuestion":
            q = (inp.get("questions") or [{}])[0]
            options = [o.get("label", "") for o in q.get("options") or []]
            return q.get("question", "") + (" " + ", or ".join(options) + "?" if options else "")
        return f"Can I use {tool}?"

    def current(self):
        """The oldest request from the session you're in, if any."""
        with self.lock:
            return next((p for p in self.pending if p["session"] == self.app.sessions.current), None)

    def waiting(self, name):
        with self.lock:
            return any(p["session"] == name for p in self.pending)

    def answer(self, item, choice, text=""):
        """Settle a request. choice: allow, allow_session, deny, plan_edits, plan_ask, plan_keep, or an
        option label for a question. text: your own words (sent to Claude with a "no")."""
        app, inp, tool = self.app, item["input"], item["tool_name"]
        session = app.sessions.items.get(item["session"]) or {}
        if tool == "AskUserQuestion":
            qs = inp.get("questions") or []
            if qs:
                item["answers"][qs[0].get("question", "")] = choice
            item["decision"] = {"behavior": "allow", "updatedInput": {**inp, "answers": item["answers"]}}
        elif choice in ("allow", "allow_session"):
            if choice == "allow_session":
                session.setdefault("allowed", []).append(self.allow_key(tool, inp))
                app.sessions.save()
            item["decision"] = {"behavior": "allow", "updatedInput": inp}
        elif choice in ("plan_edits", "plan_ask"):
            # Like Claude Code: leave plan mode, then either accept edits or keep asking first.
            session["mode"] = "acceptEdits" if choice == "plan_edits" else "manual"
            app.sessions.save()
            item["decision"] = {"behavior": "allow", "updatedInput": inp}
        else:
            msg = "The user said no." if not text else f"The user said no, and added: {text}"
            if choice == "plan_keep":
                msg = "The user wants to keep planning." + (f" They said: {text}" if text else "")
            item["decision"] = {"behavior": "deny", "message": msg}
        item["event"].set()

    def answer_by_words(self, item, text):
        """You answered out loud (or typed) instead of pressing a button."""
        tool = item["tool_name"]
        if tool == "AskUserQuestion":
            q = ((item["input"].get("questions") or [{}])[0])
            labels = [o.get("label", "") for o in q.get("options") or []]
            self.answer(item, find_choice(text, labels) or text)
        elif tool == "ExitPlanMode":
            self.answer(item, "plan_edits" if YES_RE.match(text) else "plan_keep", "" if NO_RE.match(text) else text)
        elif YES_RE.match(text):
            self.answer(item, "allow")
        else:
            self.answer(item, "deny", "" if NO_RE.match(text) else text)

    def cancel_session(self, name):
        """A task was stopped: anything it was waiting on is answered "no"."""
        with self.lock:
            items = [p for p in self.pending if p["session"] == name]
        for p in items:
            p["decision"] = {"behavior": "deny", "message": "Stopped by the user."}
            p["event"].set()


EFFORTS = ["low", "medium", "high", "xhigh", "max"]


class Info:
    """What Claude Code can tell you about your setup: the account you're signed in with, your plan's
    usage limits, your MCP servers and the version. Fetched from the claude CLI in the background
    (none of it uses the model) and cached for a little while."""

    MAX_AGE = {"auth": 600, "usage": 90, "mcp": 300, "version": 3600}

    def __init__(self, claude):
        self.claude = claude
        self.lock = threading.Lock()
        self.data = {}
        self.fetched = {}
        self.busy = set()
        self.version_note = ""

    def get(self, what):
        with self.lock:
            return self.data.get(what)

    def age(self, what):
        return time.time() - self.fetched.get(what, 0)

    def refresh(self, what, force=False):
        with self.lock:
            if what in self.busy or (not force and self.age(what) < self.MAX_AGE[what]):
                return
            self.busy.add(what)
        threading.Thread(target=self._fetch, args=(what,), daemon=True).start()

    def _run(self, args, timeout=60):
        return subprocess.run([self.claude] + args, capture_output=True, text=True, timeout=timeout,
                              env=claude_env(), cwd=str(Path.home())).stdout

    def _fetch(self, what):
        try:
            if what == "auth":
                value = json.loads(self._run(["auth", "status"]))
            elif what == "usage":
                out = json.loads(self._run(["-p", "/usage", "--output-format", "json", "--no-session-persistence"]))
                value = self.parse_usage(out.get("result") or "")
            elif what == "mcp":
                value = self.parse_mcp(self._run(["mcp", "list"], timeout=120))
            else:
                value = self._run(["--version"]).split()[0]
        except Exception as e:
            log("error", f"couldn't fetch {what}: {e!r}", "red")
            value = None
        with self.lock:
            if value is not None:
                self.data[what] = value
                self.fetched[what] = time.time()
            self.busy.discard(what)

    @staticmethod
    def parse_usage(text):
        """Claude Code's /usage report -> limits (name, % used, when it resets) and what's using them."""
        limits, tips, section = [], [], None
        for line in text.splitlines():
            m = re.match(r"^\s*(.+?):\s*(\d+)% used(?:\s*·\s*resets\s+(.+?))?(?:\s*\(.+\))?\s*$", line)
            if m:
                limits.append({"name": m.group(1), "pct": int(m.group(2)), "resets": (m.group(3) or "").strip()})
                continue
            if line.startswith("Last 24h"):
                section = line.strip()
                continue
            if line.startswith("Last 7d"):
                section = None
            t = re.match(r"^\s+(\d+)%\s+(.+)$", line)
            if section and t:
                tips.append({"pct": int(t.group(1)), "text": t.group(2).strip()})
        return {"limits": limits, "tips": tips[:4], "period": section or ""}

    @staticmethod
    def parse_mcp(text):
        servers = []
        for line in text.splitlines():
            m = re.match(r"^(.+?):\s.*\s-\s(✔|!|✘)\s*(.*)$", line)
            if m:
                state = {"✔": "connected", "!": "needs sign-in", "✘": "failed"}[m.group(2)]
                servers.append({"name": m.group(1).replace("claude.ai ", ""), "state": state, "detail": m.group(3)})
        return servers

    def update(self):
        """`claude update`, in the background; the result shows in the panel."""
        self.version_note = "Checking for updates…"

        def go():
            out = self._run(["update"], timeout=300)
            last = [l for l in out.splitlines() if l.strip()]
            self.version_note = last[-1][:80] if last else "Done."
            self.refresh("version", force=True)
        threading.Thread(target=go, daemon=True).start()


def open_in_terminal(command):
    """Some things need an interactive Claude Code (signing in, /mcp): open them in Terminal."""
    subprocess.Popen(["osascript", "-e", f'tell application "Terminal" to do script "{command}"',
                      "-e", 'tell application "Terminal" to activate'])


class Turn:
    """One request to a session's Claude Code; join() waits until it's answered (or stopped)."""

    def __init__(self, prompt):
        self.prompt = prompt
        self.done = threading.Event()
        self.retried = False

    def join(self, timeout=None):
        self.done.wait(timeout)

    def is_alive(self):
        return not self.done.is_set()


class ClaudeRunner:
    """One long-lived Claude Code process per session, fed one request at a time, like the terminal.

    It's `claude -p` in streaming-input mode, started (resuming the session's conversation) on the
    first request and kept running: anything Claude starts in the background (a dev server, an Expo
    or Vite server, a watcher) keeps going between requests instead of stopping with each one.
    Every session has its own, so a task keeps going in its session while you talk to another;
    only the session you're in speaks, and the others finish quietly and notify you.
    """

    def __init__(self, app, name):
        self.app, self.name = app, name
        self.cfg, self.speaker, self.state, self.sessions = app.cfg, app.speaker, app.state, app.sessions
        self.claude = app.claude_bin
        self.finished_at = 0.0
        self.proc = None
        self.turn = None          # the request being answered
        self.thread = None        # the same Turn (callers join it to wait for the answer)
        self.cancelled = False
        self.tool = ""            # what Claude is doing right now in this session
        self.typed = False        # was the current request typed (for speak_typed)?
        self.bg_tasks = []        # background tasks running in this session's Claude Code (servers…)
        self.stderr = collections.deque(maxlen=40)
        self.lock = threading.Lock()
        self.spoke, self.new_id, self.requests = False, None, 0
        self.queued = []          # [(prompt, typed, shown)]: typed or push-to-talk requests waiting their turn

    def busy(self):
        return self.turn is not None and not self.turn.done.is_set()

    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    def current(self):
        return self.sessions.current == self.name

    def cancel(self, clear_queue=True):
        """Stop the request in progress (and, unless it's "hey Juniper" cutting in, what's queued).
        Claude Code is asked to stop, so its background tasks keep running; if it doesn't, the
        process is ended (taking them with it)."""
        if clear_queue and self.queued:
            log("queue", f"[{self.name}] cleared {len(self.queued)} queued", "yellow")
            self.queued = []
        if not self.busy():
            return
        self.cancelled = True
        self.app.approvals.cancel_session(self.name)
        self._write({"type": "control_request", "request_id": f"stop-{time.time()}", "request": {"subtype": "interrupt"}})
        turn = self.turn

        def force():
            if not turn.done.wait(8):
                log("claude", f"[{self.name}] didn't stop; ending its process", "yellow")
                self.stop()
        threading.Thread(target=force, daemon=True).start()

    def enqueue(self, prompt, typed, shown):
        """A request that waits for the current one instead of interrupting it."""
        if not self.busy():
            self.sessions.new_turn(self.name, shown)
            return self.start(prompt, typed)
        self.queued.append((prompt, typed, shown))
        log("queue", f"[{self.name}] queued ({len(self.queued)} waiting): {shown}", "cyan")
        self.state.set()

    def _next(self):
        """The current request is done: start the next queued one, if any."""
        if self.queued and not self.busy():
            prompt, typed, shown = self.queued.pop(0)
            self.sessions.new_turn(self.name, shown)
            self.start(prompt, typed)

    def start(self, prompt, typed=False):
        self.typed = typed
        self.turn = self.thread = Turn(prompt)
        threading.Thread(target=self._send, args=(self.turn,), daemon=True).start()
        return self.turn

    def apply_settings(self):
        """A new mode, model or effort for this session reaches its running Claude Code at once."""
        if not self.alive():
            return
        s = self.sessions.get(self.name)
        self._control("set_permission_mode", mode=s.get("mode") or self.cfg["permission_mode"])
        if s.get("model") or self.cfg["model"]:
            self._control("set_model", model=s.get("model") or self.cfg["model"])
        if s.get("effort"):
            self._control("apply_flag_settings", settings={"effortLevel": s["effort"]})

    def stop(self):
        """End this session's Claude Code (and anything it runs in the background)."""
        proc, self.proc = self.proc, None
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        self.bg_tasks = []
        if self.turn and not self.turn.done.is_set():
            self._set_tool("")
            self.turn.done.set()

    def _reply(self, text):
        """Claude said something: record it in the session; speak and show it only if it's the open one."""
        self.sessions.add_reply(self.name, text)
        if self.current():
            if self.app.should_speak(self.typed):
                self.speaker.say(text, tag=self.name)
            self.state.set()                                  # keeps the overlay up

    def _set_tool(self, text):
        self.tool = text
        if self.current():
            self.state.set()

    # -- talking to Claude Code ------------------------------------------------------------------
    def _write(self, obj):
        proc = self.proc
        if proc and proc.poll() is None:
            try:
                proc.stdin.write(json.dumps(obj) + "\n")
                proc.stdin.flush()
                return True
            except (BrokenPipeError, OSError, ValueError):
                pass
        return False

    def _control(self, subtype, **fields):
        self.requests += 1
        self._write({"type": "control_request", "request_id": f"hc-{self.requests}", "request": {"subtype": subtype, **fields}})

    def _spawn(self, session):
        prompt_extra = VOICE_PROMPT.format(name=self.cfg["wake_name"])
        if self.cfg["vocabulary"]:
            prompt_extra += ("\n\nNames and terms this user often says, which speech recognition tends to mangle "
                             f"(e.g. a product name heard as ordinary words): {self.cfg['vocabulary']}.")
        hook, flag = str(HERE / "bin" / "capture-hook"), str(DATA / ".hide-overlay")
        hooks = {"hooks": {
            "PreToolUse": [{"matcher": "", "hooks": [{"type": "command", "command": f"'{hook}' pre '{flag}'"}]}],
            "PostToolUse": [{"matcher": "", "hooks": [{"type": "command", "command": f"'{hook}' post '{flag}'"}]}],
        }}
        # Permission requests come to the overlay (or your voice) through bin/approve_mcp.py.
        bridge = {"mcpServers": {"juniper": {"command": PYTHON, "args": [str(HERE / "bin" / "approve_mcp.py")],
                                             "env": {"JUNIPER_SOCK": str(Approvals.PATH), "JUNIPER_SESSION": self.name}}}}
        args = [self.claude, "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
                "--permission-mode", session.get("mode") or self.cfg["permission_mode"],
                "--append-system-prompt", prompt_extra, "--settings", json.dumps(hooks), "--mcp-config", json.dumps(bridge),
                "--permission-prompt-tool", "mcp__juniper__approve"]
        model = session.get("model") or self.cfg["model"]
        if model:
            args += ["--model", model]
        if session.get("effort"):
            args += ["--effort", session["effort"]]
        if session["session_id"] and not Sessions.transcript(session["folder"], session["session_id"]).exists():
            # e.g. a first request stopped before Claude Code saved anything: there's nothing to continue.
            log("session", f"[{self.name}] previous conversation wasn't saved; starting a fresh one", "yellow")
            session["session_id"] = None
        if session["session_id"]:
            args += ["--resume", session["session_id"]]
        self.stderr.clear()
        self.proc = proc = subprocess.Popen(args, cwd=session["folder"], env=claude_env(), text=True, bufsize=1,
                                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        threading.Thread(target=self._read, args=(proc,), daemon=True).start()
        threading.Thread(target=lambda: [self.stderr.append(l.rstrip()) for l in proc.stderr], daemon=True).start()
        log("claude", f"[{self.name}] started Claude Code{' (continuing)' if session['session_id'] else ''}", "dim")

    def _send(self, turn):
        session = self.sessions.get(self.name)
        with self.lock:
            if not self.alive():
                self._spawn(session)
            self.cancelled, self.spoke, self.new_id = False, False, None
            log("claude", f"[{self.name}{' cont.' if session['session_id'] else ''}] {turn.prompt}", "cyan")
            if not self._write({"type": "user", "message": {"role": "user", "content": turn.prompt}}):
                self._failed(turn, "Claude Code stopped before it could take the request")

    def _read(self, proc):
        for line in proc.stdout:
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            try:
                self._event(ev)
            except Exception as e:      # one odd event mustn't stop the session listening
                log("error", f"[{self.name}] {e!r} handling {ev.get('type')}", "red")
        # The process ended (crashed, stopped, or the conversation couldn't be resumed).
        proc.wait()
        if proc is self.proc:
            self.proc = None
            self.bg_tasks = []
            turn = self.turn
            if turn and not turn.done.is_set():
                err = "\n".join(self.stderr)
                if "No conversation found" in err and not turn.retried:
                    self._fresh_and_retry(turn)
                else:
                    self._failed(turn, err.strip().splitlines()[-1] if err.strip() else f"exit code {proc.returncode}")

    def _event(self, ev):
        session = self.sessions.get(self.name)
        if ev.get("session_id"):
            self.new_id = ev["session_id"]       # adopted below, once Claude Code has saved the conversation
        kind, sub = ev.get("type"), ev.get("subtype")
        if kind == "system" and sub == "init" and ev.get("slash_commands"):
            self.app.slash_commands, self.app.skills = ev["slash_commands"], ev.get("skills") or []
        if kind == "assistant":
            usage = ev["message"].get("usage") or {}
            if usage:      # how full the conversation is: everything sent this call, plus the reply
                session["context_used"] = sum(usage.get(k) or 0 for k in (
                    "input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens"))
            for block in ev["message"].get("content", []):
                if block.get("type") == "text" and block["text"].strip():
                    log("reply", f"[{self.name}] {block['text'].strip()}", "green")
                    self._set_tool("")
                    self._reply(block["text"])
                    self.spoke = True
                elif block.get("type") == "tool_use":
                    detail = json.dumps(block.get("input", {}))[:140]
                    log("tool", f"[{self.name}] {block['name']} {detail}", "dim")
                    if block.get("name") == "ExitPlanMode" and (block.get("input") or {}).get("plan"):
                        self.sessions.add_reply(self.name, block["input"]["plan"])      # shown, not read out
                    else:
                        self.sessions.add_tool(self.name, tool_part(block, session["folder"]))
                    self._set_tool(describe_tool(block))
        elif kind == "user":
            for block in (ev.get("message") or {}).get("content") or []:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    self.sessions.finish_tool(self.name, block.get("tool_use_id"), not block.get("is_error"))
        elif kind == "system" and sub == "background_tasks_changed":
            self.bg_tasks = ev.get("tasks") or []
            log("tasks", f"[{self.name}] running in the background: "
                         f"{', '.join(t.get('description') or t.get('task_id', '?') for t in self.bg_tasks) or 'nothing'}", "dim")
        elif kind == "system" and sub == "status":
            # Claude Code summarises the conversation when it gets long (or when asked to).
            self._set_tool("Compacting the conversation…" if ev.get("status") == "compacting" else "")
        elif kind == "system" and sub == "compact_boundary":
            meta = ev.get("compact_metadata") or {}
            log("compact", f"[{self.name}] {meta.get('trigger', '')}: {meta.get('pre_tokens', '?')} -> "
                           f"{meta.get('post_tokens', '?')} tokens", "dim")
        elif kind == "control_response" and (ev.get("response") or {}).get("subtype") == "error":
            log("claude", f"[{self.name}] Claude Code refused: {(ev.get('response') or {}).get('error')}", "yellow")
        elif kind == "result":
            self._finished(ev)

    def _fresh_and_retry(self, turn):
        """The conversation to continue has gone: start afresh with the same request."""
        log("session", f"[{self.name}] conversation not found; starting a fresh one", "yellow")
        turn.retried = True
        self.stop_process_only()
        self.sessions.get(self.name)["session_id"] = None
        self._send(turn)

    def stop_process_only(self):
        proc, self.proc = self.proc, None
        if proc and proc.poll() is None:
            proc.terminate()

    def _failed(self, turn, detail):
        log("error", f"[{self.name}] {detail}", "red")
        self._set_tool("")
        if not self.cancelled:
            self._reply("Sorry, that didn't work. " + str(detail)[:200])
            if not self.current():
                self.sessions.set_unread(self.name, True)
                self.app.notify_finished(self.name, True)
        self.sessions.save()
        self.finished_at = time.time()
        turn.done.set()
        self._next()

    def _finished(self, result):
        """Claude Code answered (or stopped) the current request."""
        turn = self.turn
        session = self.sessions.get(self.name)
        (DATA / ".hide-overlay").unlink(missing_ok=True)     # in case a screenshot failed mid-way
        model = session.get("model") or self.cfg["model"]
        windows = [u.get("contextWindow") for m, u in (result.get("modelUsage") or {}).items()
                   if u.get("contextWindow") and (not model or m.startswith(model))]
        if windows:
            session["context_window"] = max(windows)
        if self.new_id and Sessions.transcript(session["folder"], self.new_id).exists():
            session["session_id"] = self.new_id
        if turn is None or turn.done.is_set():
            return                                            # (e.g. something Claude finished on its own)
        if "No conversation found" in str(result.get("result") or "") and not turn.retried:
            self._fresh_and_retry(turn)
            return
        session["last_used"] = time.time()
        self._set_tool("")
        if self.cancelled:
            log("claude", f"[{self.name}] interrupted", "yellow")
            self.sessions.save()
            turn.done.set()            # (what's queued waits for whatever cut in, or was cleared by Stop)
            return
        failed = bool(result.get("is_error"))
        if failed:
            self._reply("Sorry, that didn't work. " + str(result.get("result") or result.get("subtype"))[:200])
            log("error", f"[{self.name}] {result.get('result') or result.get('subtype')}", "red")
        elif not self.spoke and result.get("result"):
            self._reply(result["result"])
        elif not self.spoke and turn.prompt.strip() == "/compact":
            self._reply("Done, I've compacted the conversation.")
        log("done", f"[{self.name}] {result.get('duration_ms', 0) / 1000:.1f}s, {result.get('num_turns', 0)} turns"
                    + (f", {len(self.bg_tasks)} still running in the background" if self.bg_tasks else ""), "dim")
        if not self.current():
            self.sessions.set_unread(self.name, True)
            self.app.notify_finished(self.name, failed)
        self.sessions.save()
        # Set last, after the reply is queued for speech, so a follow-up window can't open early.
        self.finished_at = time.time()
        turn.done.set()
        self._next()


class Segmenter:
    """Cuts the mic stream into utterances using WebRTC voice activity detection."""

    def __init__(self, cfg):
        import webrtcvad
        self.vad = webrtcvad.Vad(cfg["vad_level"])
        self.pre = collections.deque(maxlen=10)    # 300 ms kept from before speech starts
        self.ring = collections.deque(maxlen=8)
        self.end_frames = cfg["end_silence_ms"] // FRAME_MS
        self.max_frames = cfg["max_utterance_seconds"] * 1000 // FRAME_MS
        self.active = False
        self.frames = []
        self.silence = 0
        self.started = 0.0

    def push(self, frame, now):
        voiced = self.vad.is_speech(frame, RATE)
        if not self.active:
            self.pre.append(frame)
            self.ring.append(voiced)
            if sum(self.ring) >= 6:
                self.active, self.frames, self.silence = True, list(self.pre), 0
                self.started = now - len(self.pre) * FRAME_MS / 1000
                self.pre.clear()
                self.ring.clear()
            return None
        self.frames.append(frame)
        self.silence = 0 if voiced else self.silence + 1
        if self.silence >= self.end_frames or len(self.frames) >= self.max_frames:
            pcm, self.frames, self.active = b"".join(self.frames), [], False
            if len(pcm) >= RATE * 2 * 0.5:
                return pcm, self.started
        return None


class SherpaWake:
    """Listens for the sound of "hey Juniper" with a tiny keyword-spotting model (sherpa-onnx).

    Uses almost no CPU and turns nothing into text; speech recognition only runs once this fires.
    Keywords are pronunciations (CMU phonemes, see wake_keywords()).
    """

    def __init__(self, cfg):
        import sherpa_onnx
        d = RES / "models" / cfg["sherpa_model"]
        keywords = DATA / "keywords.txt"          # written each start (the app's own folder is read-only)
        keywords.write_text("\n".join(cfg["wake_keywords"] or wake_keywords(cfg["wake_name"], d / "en.phone")) + "\n")
        self.kws = sherpa_onnx.KeywordSpotter(
            tokens=str(d / "tokens.txt"),
            encoder=str(d / "encoder-epoch-13-avg-2-chunk-16-left-64.int8.onnx"),
            decoder=str(d / "decoder-epoch-13-avg-2-chunk-16-left-64.onnx"),
            joiner=str(d / "joiner-epoch-13-avg-2-chunk-16-left-64.int8.onnx"),
            keywords_file=str(keywords), num_threads=1,
            keywords_threshold=cfg["wake_threshold"], keywords_score=cfg["wake_boost"])
        self.stream = self.kws.create_stream()

    def push(self, frame):
        self.stream.accept_waveform(RATE, np.frombuffer(frame, np.int16).astype(np.float32) / 32768.0)
        while self.kws.is_ready(self.stream):
            self.kws.decode_stream(self.stream)
        hit = self.kws.get_result(self.stream)
        if hit:
            self.kws.reset_stream(self.stream)
        return hit


class Recorder:
    """Records one request: waits up to `wait` seconds for speech, then until a pause."""

    def __init__(self, cfg, vad, kind, prefix, now, wait):
        self.vad, self.kind = vad, kind
        self.prefix = list(prefix)                 # audio from just before (the "hey Juniper" itself)
        self.pre = collections.deque(maxlen=10)
        self.ring = collections.deque(maxlen=5)
        self.frames = []
        self.speaking = False
        self.silence = 0
        self.deadline = now + wait
        self.started = now - len(self.prefix) * FRAME_MS / 1000
        self.end_frames = cfg["end_silence_ms"] // FRAME_MS
        self.max_frames = cfg["max_utterance_seconds"] * 1000 // FRAME_MS

    def push(self, frame, now):
        """Returns None while recording, then the audio (b"" if nobody spoke)."""
        voiced = self.vad.is_speech(frame, RATE)
        if not self.speaking:
            self.pre.append(frame)
            self.ring.append(voiced)
            if sum(self.ring) >= 3:
                self.speaking, self.frames = True, self.prefix + list(self.pre)
            elif now > self.deadline:
                return b""
            return None
        self.frames.append(frame)
        self.silence = 0 if voiced else self.silence + 1
        if self.silence >= self.end_frames or len(self.frames) >= self.max_frames:
            return b"".join(self.frames)
        return None


class VoiceID:
    """Checks that a request was spoken by you, the way "Hey Siri" does.

    Enrolment turns a few recordings of you into voice embeddings (a few hundred numbers that
    describe what your voice sounds like). Each request's audio is turned into the same kind of
    numbers and compared with your profile; too far from it and the request is ignored.
    Confident matches are added to the profile so it keeps up with how you sound day to day.
    """

    PATH = DATA / "voiceprint.npz"
    KEEP_RECENT = 10

    def __init__(self, cfg):
        import sherpa_onnx
        model = RES / "models" / "speaker" / cfg["speaker_model"]
        self.ex = sherpa_onnx.SpeakerEmbeddingExtractor(
            sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(model), num_threads=1))
        self.cfg = cfg
        self.enrolled, self.recent, self.auto = None, [], 0.0
        if self.PATH.exists():
            d = np.load(self.PATH)
            self.enrolled, self.recent, self.auto = d["enrolled"], list(d["recent"]), float(d["threshold"])

    def ready(self):
        return self.enrolled is not None

    def threshold(self):
        return self.cfg["voice_threshold"] or self.auto

    def embed(self, pcm):
        s = self.ex.create_stream()
        s.accept_waveform(RATE, np.frombuffer(pcm, np.int16).astype(np.float32) / 32768.0)
        s.input_finished()
        e = np.array(self.ex.compute(s))
        return e / np.linalg.norm(e)

    def _profile(self):
        v = np.mean(np.vstack([self.enrolled] + [np.array(self.recent)] * bool(self.recent)), axis=0)
        return v / np.linalg.norm(v)

    def check(self, pcm):
        e = self.embed(pcm)
        score = float(e @ self._profile())
        ok = score >= self.threshold()
        if ok and score >= self.threshold() + 0.05:
            self.recent = (self.recent + [e])[-self.KEEP_RECENT:]
            self._save()
        return ok, score

    def enroll(self, clips):
        """Build the profile from your recordings and pick the match threshold."""
        self.enrolled, self.recent = np.array([self.embed(c) for c in clips]), []
        return self.calibrate()

    def calibrate(self):
        """Put the threshold between how much your own recordings vary and how close strangers get.

        "Strangers" are the Mac's built-in English voices reading the enrolment lines, a stand-in
        for other people. Returns (your scores, the closest stranger's score, the threshold).
        """
        E = self.enrolled
        total = E.sum(axis=0)
        yours = [float(e @ ((total - e) / np.linalg.norm(total - e))) for e in E]
        profile = total / np.linalg.norm(total)
        strangers = []
        tmp = DATA / ".cohort.wav"
        import wave
        for voice in ["Daniel", "Samantha", "Karen", "Moira", "Rishi", "Tara", "Tessa", "Aman", "Fred", "Ralph"]:
            for line in enrol_lines(self.cfg)[:2]:
                if subprocess.run(["say", "-v", voice, "--file-format=WAVE", f"--data-format=LEI16@{RATE}",
                                   "-o", str(tmp), line], capture_output=True).returncode:
                    break   # voice not installed
                with wave.open(str(tmp), "rb") as w:
                    pcm = w.readframes(w.getnframes())
                strangers.append(float(self.embed(pcm) @ profile))
        tmp.unlink(missing_ok=True)
        closest = max(strangers) if strangers else 0.4
        if min(yours) > closest:
            self.auto = (min(yours) + closest) / 2
        else:
            self.auto = closest + 0.02   # your recordings were inconsistent; err on the side of strangers
        self.auto = float(np.clip(self.auto, 0.45, 0.85))
        self._save()
        return yours, closest, self.auto

    def _save(self):
        np.savez(self.PATH, enrolled=self.enrolled, threshold=self.auto,
                 recent=np.array(self.recent) if self.recent else np.zeros((0, self.enrolled.shape[1])))


ENROL_LINES = [
    "Hey {name}, what's the weather like today?",
    "Hey {name}, open my email.",
    "Hey {name}, how many orders came in this morning?",
    "Hey {name}, remind me to call the bank at three.",
    "Hey {name}, what time is my next meeting?",
]


def enrol_lines(cfg):
    """The lines read out to teach it your voice, with your wake word in them."""
    name = cfg.get("wake_name") or DEFAULTS["wake_name"]
    return [line.format(name=name) for line in ENROL_LINES]


class Transcriber:
    def __init__(self, cfg, need_quick=True):
        self.cfg, self.need_quick, self.models = cfg, need_quick, {}
        self.vocab = cfg["vocabulary"]
        self.engine, self.locale = cfg["stt_engine"], cfg["stt_locale"] or system_locale()
        if self.engine == "apple" and not (RES / "bin" / "apple-stt").exists():
            log("warn", "bin/apple-stt is missing (run scripts/dev-app.sh); using Whisper", "red")
            self.engine = "whisper"
        if self.engine == "apple" and macos_major() < 26:
            log("setup", "Apple's speech recognizer needs macOS 26; using Whisper", "dim")
            self.engine = "whisper"
        if self.engine == "whisper" or need_quick:
            # Whisper is needed for every request: load it now (the first time downloads it).
            threading.Thread(target=lambda: (self.accurate, self.fast), daemon=True).start()

    def _model(self, name):
        """Whisper models load (and the first time, download) only when they're needed."""
        if name not in self.models:
            from faster_whisper import WhisperModel
            log("setup", f"loading Whisper {name}…", "dim")
            self.models[name] = WhisperModel(name, device="cpu", compute_type="int8")
        return self.models[name]

    @property
    def accurate(self):
        return self._model(self.cfg["command_model"])

    @property
    def fast(self):
        return self._model(self.cfg["wake_model"]) if self.need_quick else self.accurate

    def _run(self, model, pcm, prompt, beam):
        audio = np.frombuffer(pcm, np.int16).astype(np.float32) / 32768.0
        segments, _ = model.transcribe(audio, language="en", beam_size=beam, initial_prompt=prompt,
                                       condition_on_previous_text=False, without_timestamps=True)
        return " ".join(s.text.strip() for s in segments if s.no_speech_prob < 0.6).strip()

    # The prompt describes the conversation rather than starting it: a prompt of "Hey Juniper,"
    # makes Whisper treat those words as already said and drop them from the transcript.
    def quick(self, pcm):
        return self._run(self.fast, pcm, f"A conversation with an assistant named {self.cfg['wake_name']}.", 1)

    def full(self, pcm):
        """Your request as text: Apple's on-device recognizer if chosen, otherwise (or if it fails) Whisper."""
        if self.engine == "apple":
            try:
                return self._apple(pcm)
            except Exception as e:
                log("error", f"Apple speech recognition failed ({e!r}), using Whisper", "red")
        vocab = f" Words that may come up: {self.vocab}." if self.vocab else ""
        return self._run(self.accurate, pcm, f"A conversation with an assistant named {self.cfg['wake_name']}." + vocab, 5)

    def _apple(self, pcm):
        import tempfile
        import wave
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            path = f.name
        try:
            with wave.open(path, "wb") as w:
                w.setnchannels(1), w.setsampwidth(2), w.setframerate(RATE), w.writeframes(pcm)
            words = ", ".join(dict.fromkeys([self.cfg["wake_name"], "Claude"] +
                                            [v.strip() for v in self.vocab.split(",") if v.strip()]))
            out = subprocess.run([str(RES / "bin" / "apple-stt"), self.locale, words, path],
                                 capture_output=True, text=True, timeout=30)
            if out.returncode:
                raise RuntimeError(out.stderr.strip()[:200] or f"exit {out.returncode}")
            return out.stdout.strip()
        finally:
            os.unlink(path)


class Mic:
    """The microphone stream, which can be opened and closed (macOS shows the orange dot while open)."""

    def __init__(self, cfg):
        self.device = cfg["input_device"] or None
        self.frames = queue.Queue()
        self.stream = None
        self.lock = threading.Lock()
        self.opened_at = 0.0

    def is_open(self):
        return self.stream is not None

    def open(self):
        import sounddevice as sd
        with self.lock:
            if self.stream:
                return
            self.stream = sd.RawInputStream(samplerate=RATE, blocksize=FRAME_BYTES // 2, channels=1, dtype="int16",
                                            callback=lambda d, *_: self.frames.put(bytes(d)), device=self.device)
            self.stream.start()
            self.opened_at = time.time()

    def close(self):
        with self.lock:
            if self.stream:
                self.stream.stop()
                self.stream.close()
                self.stream = None

    def name(self):
        import sounddevice as sd
        return sd.query_devices(self.device, kind="input")["name"]


class App:
    def __init__(self, cfg, mute=False, dry_run=False):
        self.cfg, self.dry_run = cfg, dry_run
        self.state = State()
        self.speaker = Speaker(cfg, mute)
        self.voice = VoiceID(cfg) if cfg["voice_check"] else None
        if self.voice and not self.voice.ready():
            log("setup", "voice check is on but you haven't taught it your voice yet (menu > Set up again…)", "yellow")
        self.sessions = Sessions(cfg)
        self.speaker.current = lambda: self.sessions.current
        self.speaker.on_problem = lambda msg: self.notify("Using the Mac voice", msg)
        self.claude_bin = find_claude(cfg["claude_path"]) or str(Path.home() / ".local/bin/claude")
        self.runners = {}                    # session name -> its ClaudeRunner
        self.pending = {}                    # session name -> files to send with its next request
        self.router = Router(cfg, self.claude_bin)
        self.approvals = Approvals(self)
        self.info = Info(self.claude_bin)
        self.slash_commands = []             # Claude Code's commands and your skills (from its last start-up)
        self.skills = []
        self.ui_requests = queue.Queue()     # things only the menu bar can do (e.g. show the sidebar)
        self.ui_busy = lambda: False         # the menu bar says whether you're mid-way through typing
        self.updater = None
        self.sherpa = cfg["wake_engine"] == "sherpa"
        self.wake_re, self.verify_re, self.lead_re = wake_patterns(cfg["wake_name"])
        self.transcriber = Transcriber(cfg, need_quick=not self.sherpa)
        self.segmenter = Segmenter(cfg)
        if self.sherpa:
            log("setup", f"wake word: \"hey {cfg['wake_name']}\" (sherpa-onnx, {cfg['sherpa_model']})", "dim")
            self.wake = SherpaWake(cfg)
        self.history = collections.deque(maxlen=2000 // FRAME_MS)   # last 2 s, holds "hey Juniper"
        self.recorder = None
        self.since_recorder = 0              # frames heard since the last recording ended
        self.listen_request = False          # worker asks: record the request after a bare "hey Juniper"
        self.pending_wake = b""
        self.followup_from = 0.0
        self.awaiting_until = 0.0
        self.followup_used_at = 0.0
        self.utterances = queue.Queue()
        self.last_thread = None
        self.mic = Mic(cfg)
        self.ptt = None                      # push-to-talk recording in progress
        self.ptt_lock = threading.Lock()
        saved = {}
        if STATE_PATH.exists():
            try:
                saved = json.loads(STATE_PATH.read_text())
            except ValueError:
                pass
        self.open_mic = saved.get("open_mic", cfg["open_mic"])
        self.show_logo = saved.get("show_logo", cfg["show_logo"])
        self.speak_answers = saved.get("speak_answers", cfg["speak_answers"])
        self.speak_typed = saved.get("speak_typed", cfg["speak_typed"])
        self.logo_pos = saved.get("logo_pos")         # where you've dragged the corner logo (screen points)
        self.logo_size = saved.get("logo_size", cfg["logo_size"])
        self.overlay_w, self.overlay_h = saved.get("overlay_w"), saved.get("overlay_h")   # your size (None: automatic)
        self.last_typed = False

    # -- deciding what an utterance means -------------------------------------------------

    def heard_own_voice(self, started):
        # Ignore anything that overlapped with our own text-to-speech.
        return self.speaker.speaking() or started < self.speaker.quiet_since + 0.3

    def followup_open(self, now):
        s = self.cfg["followup_seconds"]
        if not s or not self.runner.finished_at or self.runner.busy() or self.speaker.speaking() or self.runner.typed:
            return False
        opened = max(self.runner.finished_at, self.speaker.quiet_since)
        return now - opened < s and self.followup_used_at < self.runner.finished_at

    def handle(self, pcm, started):
        """whisper mode: a phrase someone said; act on it only if it contains "hey Juniper"."""
        now = time.time()
        quick = self.transcriber.quick(pcm)
        if not quick:
            return
        wake = self.wake_re.search(quick)
        if not wake:
            plain = re.sub(r"[^\w\s']", "", quick).strip().lower()
            listening = now < self.awaiting_until or self.followup_open(now)
            if not listening or plain in HALLUCINATIONS or self.heard_own_voice(started):
                log("heard", quick, "dim")
                return
            if now >= self.awaiting_until:
                self.followup_used_at = now
            self.awaiting_until = 0
            self.command(self.transcriber.full(pcm) or quick)
            return

        if self.voice and self.voice.ready():
            ok, score = self.voice.check(pcm)
            if not ok:
                log("ignored", f"not your voice (match {score:.2f}): {quick}", "dim")
                return
        log("wake", quick, "yellow")
        self.speaker.stop()
        rest = wake.group(1).strip(" .,!?")
        if not rest:
            self.awaiting_until = time.time() + self.cfg["listen_seconds"]
            self.state.set(recording=True, followup=False, heard="", reply="", tool="", dismissed=False)
            chime(self.cfg, "Tink")
            return
        if STOP_RE.match(rest):
            self.command(rest)
            return
        full = self.transcriber.full(pcm)
        m = self.wake_re.search(full)
        self.command(m.group(1).strip() if m and m.group(1).strip() else rest)

    def voice_ok(self, pcm):
        """Is this you? Also keeps the clip in debug/ (last 20) so thresholds can be tuned on real audio."""
        if not (self.voice and self.voice.ready()):
            return True
        ok, score = self.voice.check(pcm)
        try:
            d = DATA / "debug"
            d.mkdir(exist_ok=True)
            import wave
            with wave.open(str(d / f"{time.strftime('%H%M%S')}-{'ok' if ok else 'no'}-{score:.2f}.wav"), "wb") as w:
                w.setnchannels(1), w.setsampwidth(2), w.setframerate(RATE), w.writeframes(pcm)
            for old in sorted(d.glob("*.wav"))[:-20]:
                old.unlink()
        except OSError:
            pass
        if ok:
            log("voice", f"match {score:.2f}", "dim")
        else:
            log("ignored", f"not your voice (match {score:.2f}, needs {self.voice.threshold():.2f})", "dim")
            self.state.set(dismissed=True)
        return ok

    def handle_recorded(self, kind, pcm):
        """Audio recorded after the wake word fired, after a bare "hey Juniper", in a follow-up
        window, or with push-to-talk."""
        self.state.set(recording=False)
        if not pcm:
            if kind == "followup":
                return     # no follow-up; the overlay fades out on its own
            log("wake", "didn't hear a request", "dim")
            self.pending_wake = b""
            self.state.set(dismissed=True)
            return
        text = self.transcriber.full(pcm)
        if kind == "wake":
            # The keyword spotter only knows sounds; speech recognition confirms it was really the
            # name (not "hey, junior doctor…") before anything is sent.
            m = self.verify_re.search(text)
            if not m:
                log("ignored", f"not a wake word: {text}", "dim")
                self.state.set(dismissed=True)
                return
            log("heard", text, "yellow")
            rest = m.group(1).strip()
            if not rest.strip(" .,!?"):
                # Just "hey Juniper": keep listening for the request. The voice check waits for it,
                # because "hey Juniper" plus a request is a far more reliable sample than one second.
                self.pending_wake = pcm
                self.state.set(recording=True)
                self.listen_request = True
                return
            if STOP_RE.match(rest) or self.voice_ok(pcm):
                self.command(rest)
            return
        if kind == "command":
            wake, self.pending_wake = self.pending_wake, b""
            if not STOP_RE.match(text) and not self.voice_ok(wake + pcm):
                return
        elif kind == "followup" and len(pcm) >= RATE * 2 * 2 and not self.voice_ok(pcm):
            # Short answers ("yes", "the second one") are too brief to judge the voice reliably,
            # and come straight after your own question, so only longer follow-ups are checked.
            return
        # Push-to-talk needs no voice check: pressing the key on your laptop is proof enough.
        if kind in ("ptt", "followup"):
            m = self.lead_re.match(text)
            text = m.group(1) if m else text
        if re.sub(r"[^\w\s']", "", text).strip().lower() in HALLUCINATIONS:
            log("heard", "nothing", "dim")
            self.state.set(dismissed=True)
            return
        self.command(text, queue=kind == "ptt")      # push-to-talk / the record button wait their turn

    def command(self, text, typed=False, queue=False):
        """A request. Typed ones and push-to-talk ones (queue) wait for a task that's running;
        "hey Juniper" cuts in."""
        text = text.strip()
        queue = queue or typed
        self.last_typed = typed
        log("you", text, "cyan")
        self.state.set(heard=text, recording=False, dismissed=False)
        if STOP_RE.match(text):
            self.runner.cancel()
            self.speaker.stop()
            chime(self.cfg, "Pop")
            self.state.set(dismissed=True)
            return
        waiting = self.approvals.current()
        if waiting:
            # Claude is waiting on you ("Can I run this?"): what you said or typed is the answer.
            self.sessions.new_turn(self.sessions.current, text)
            self.approvals.answer_by_words(waiting, text)
            return
        if text.startswith("/"):
            text = self.slash(text)
            if not text:
                return
        if RESET_RE.search(text) and len(text.split()) <= 5:
            self.clear_session()
            return
        if SESSION_WORDS.search(text):
            # Might be about sessions ("switch to Mypus"): a quick Claude call works out what was meant.
            chime(self.cfg, "Pop")
            self.state.set(working=True)
            try:
                route = self.router.route(text, self.sessions)
            except Exception as e:
                log("error", f"couldn't work out the session command ({e!r}); treating it as a request", "red")
                route = {"action": "none"}
            finally:
                self.state.set(working=False)
            if route.get("action", "none") != "none":
                text = self.session_action(text, route)
                if not text:
                    return
        waits = queue and self.runner.busy()
        interrupted = False if waits else self.interrupt()
        if interrupted:
            # Like pressing Esc in Claude Code and typing something else: same conversation, new direction.
            text = "(I interrupted you before you finished.) " + text
        chime(self.cfg, "Pop")
        shown = self.state.heard if text != "/compact" else "Compact the conversation"
        files, self.attachments = self.attachments, []
        if files and text != "/compact":
            # Claude reads attachments itself (images and PDFs included) from their paths.
            shown += "\n📎 " + ", ".join(os.path.basename(f) for f in files)
            text += "\n\nAttached files (read them as needed):\n" + "\n".join(f"- {f}" for f in files)
            log("attach", ", ".join(files), "dim")
        if self.dry_run:
            log("dry-run", "would send to Claude", "dim")
            return
        if waits:
            self.runner.enqueue(text, typed, shown)      # runs when the current task finishes
            return
        self.sessions.new_turn(self.sessions.current, shown)
        self.last_thread = self.runner.start(text, typed)

    # -- typing and attachments ----------------------------------------------------------------

    @property
    def attachments(self):
        return self.pending.setdefault(self.sessions.current, [])

    @attachments.setter
    def attachments(self, files):
        self.pending[self.sessions.current] = files

    def typed(self, text):
        """A request typed into the overlay: handled exactly like a spoken one (but see speak_typed)."""
        if text.strip() or self.attachments:
            self.command(text.strip() or "Have a look at the attached files.", typed=True)

    def attach(self, paths):
        new = [p for p in paths if os.path.exists(p) and p not in self.attachments]
        self.attachments += new
        if new:
            log("attach", f"+ {', '.join(os.path.basename(p) for p in new)}", "dim")
            self.state.set(dismissed=False)

    def clear_attachments(self):
        self.attachments = []

    def attach_image_data(self, png):
        """A pasted image: save it so Claude can open it, then attach it."""
        folder = DATA / "attachments"
        folder.mkdir(exist_ok=True)
        for old in folder.glob("*.png"):                     # tidy up pastes older than a week
            if time.time() - old.stat().st_mtime > 7 * 86400:
                old.unlink(missing_ok=True)
        path = folder / f"pasted-{time.strftime('%Y%m%d-%H%M%S')}.png"
        path.write_bytes(png)
        self.attach([str(path)])

    # -- sessions ----------------------------------------------------------------------------

    @property
    def runner(self):
        """The open session's runner."""
        return self.runner_for(self.sessions.current)

    def runner_for(self, name):
        if name not in self.runners:
            self.runners[name] = ClaudeRunner(self, name)
        return self.runners[name]

    def answer(self, you, text):
        """A reply Juniper gives itself (about sessions), shown and spoken like Claude's."""
        name = self.sessions.current
        self.sessions.new_turn(name, you)
        self.sessions.add_reply(name, text)
        self.sessions.save()
        self.state.set(dismissed=False)
        if self.should_speak(self.last_typed):
            self.speaker.say(text, tag=name)

    def enter_session(self, name):
        """Open a session. Whatever was being said stops at once; a task running in the session you
        leave keeps going. An answer that finished while you were away is read out."""
        self.speaker.stop()
        self.sessions.switch(name)
        self.state.set(dismissed=False)
        log("session", f"now in {name}", "green")
        if self.sessions.get(name).get("unread"):
            self.sessions.set_unread(name, False)
            self.sessions.save()
            turns = self.sessions.turns(name)
            if turns and turns[-1]["claude"] and self.speak_answers:
                self.speaker.say(turns[-1]["claude"], tag=name)
                return True
        return False

    def notify_finished(self, name, failed=False):
        """A session you're not in finished: macOS notification and a chime."""
        turns = self.sessions.turns(name)
        last = to_speech(turns[-1]["claude"])[:160] if turns and turns[-1]["claude"] else ""
        subtitle = f"{name}: something went wrong" if failed else f"{name} is done"
        log("session", f"{subtitle} (in the background)", "green")

        def esc(t):
            return t.replace("\\", "\\\\").replace('"', '\\"')
        subprocess.Popen(["osascript", "-e", f'display notification "{esc(last or subtitle)}" with title "Juniper" '
                                             f'subtitle "{esc(subtitle)}" sound name "Glass"'],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def session_action(self, text, route):
        """Carry out a session command. Returns what to send to Claude next ("" for nothing)."""
        action, then = route.get("action"), (route.get("then") or "").strip()
        name = self.sessions.find(route.get("session"))
        folder = os.path.expanduser(route.get("folder") or "")
        if action == "list":
            names = self.sessions.names()
            described = [n + (" (still working)" if self.runner_for(n).busy() and n != self.sessions.current else "")
                         + (" (new reply)" if self.sessions.get(n).get("unread") else "") for n in names]
            listed = ", ".join(described[:-1]) + " and " + described[-1] if len(names) > 1 else described[0]
            self.answer(text, f"You have {len(names)} session{'s' * (len(names) > 1)}: {listed}. "
                              f"You're in {self.sessions.current}.")
            return ""
        if action == "current":
            s = self.sessions.get()
            self.answer(text, f"You're in the {self.sessions.current} session, working in "
                              f"{os.path.basename(s['folder']) or 'your home folder'}.")
            return ""
        if action == "compact":
            self.interrupt()
            return "/compact"
        if action == "clear":
            self.clear_session(name or self.sessions.current, you=text)
            return then
        if action == "switch" and name:
            read_out = self.enter_session(name)
            if not then and not read_out:
                busy = self.runner.busy()
                self.answer(text, f"Switched to {name}." + (" It's still working on your last request." if busy else ""))
            return then
        if action in ("new", "continue", "switch"):
            if not os.path.isdir(folder):
                self.answer(text, f"I couldn't find a folder for {route.get('session') or 'that'}.")
                return ""
            sid = Sessions.latest_terminal_session(folder) if action == "continue" else None
            if action == "continue" and not sid:
                self.answer(text, f"There's no earlier Claude Code session in {os.path.basename(folder)}.")
                return ""
            if action == "continue" and name and self.sessions.get(name)["folder"] == folder:
                if self.runner_for(name).busy():
                    self.answer(text, f"{name} is busy right now; try again when it's done.")
                    return ""
                self.sessions.get(name)["session_id"] = sid
            else:
                name = self.sessions.create(route.get("session") or os.path.basename(folder), folder, sid)
            self.enter_session(name)
            log("session", f"{action} {name} in {folder}", "green")
            if not then:
                self.answer(text, f"Picked up your last {name} session." if action == "continue"
                                  else f"New session for {name}.")
            return then
        self.answer(text, "Sorry, I couldn't work out which session you meant.")
        return ""

    def slash(self, text):
        """Juniper's own slash commands. Anything else (Claude Code's commands, your skills) goes
        to Claude as typed. Returns what to send to Claude ("" for nothing)."""
        cmd, _, arg = text.partition(" ")
        cmd, arg = cmd.lower(), arg.strip()
        if cmd == "/clear":
            self.clear_session(you=text)
        elif cmd == "/compact":
            return "/compact"
        elif cmd == "/stop":
            self.stop_everything()
        elif cmd == "/sessions":
            self.ui_requests.put("sidebar")
        elif cmd == "/mode":
            self.answer(text, self.set_mode(arg) if arg else
                        f"You're in {mode_label(self.sessions.get().get('mode') or self.cfg['permission_mode'])} mode. "
                        "Try /mode auto, ask, edits, plan or bypass.")
        elif cmd == "/model":
            self.answer(text, self.set_model(arg) if arg else
                        f"This session uses {model_label(self.sessions.get().get('model') or self.cfg['model'])}. "
                        "Try /model opus, sonnet, haiku or fable.")
        elif cmd == "/effort":
            self.answer(text, self.set_effort(arg) if arg else
                        f"Effort here is {self.sessions.get().get('effort') or 'your default'}. "
                        f"Try /effort {' / '.join(EFFORTS)}.")
        elif cmd == "/usage":
            self.ui_requests.put("usage")
        elif cmd == "/new":
            folders = self.router.folders(self.sessions)
            match = next((f for f in folders if os.path.basename(f).lower() == arg.lower()), None) or \
                next((f for f in folders if arg.lower() in os.path.basename(f).lower()), None)
            if not arg or not match:
                self.answer(text, f"I couldn't find a project folder called {arg or 'that'}.")
            else:
                self.enter_session(self.sessions.create(os.path.basename(match), match))
                self.answer(text, f"New session for {os.path.basename(match)}.")
        elif cmd == "/help":
            self.answer(text, "You can type: " + "; ".join(f"{c} ({d.lower()})" for c, d in LOCAL_COMMANDS)
                        + ". Claude Code's own commands and your skills work too.")
        else:
            return text
        return ""

    def set_mode(self, wanted, name=None):
        """Change a session's permission mode (from the chip, Shift-Tab, /mode or your voice)."""
        aliases = {"ask": "manual", "default": "manual", "manual": "manual", "normal": "manual",
                   "edits": "acceptEdits", "accept": "acceptEdits", "accept edits": "acceptEdits",
                   "plan": "plan", "planning": "plan", "auto": "auto", "bypass": "bypassPermissions",
                   "yolo": "bypassPermissions"}
        w = wanted.strip().lower()
        mode = next((m for m, _ in MODES if m.lower() == w), None) or aliases.get(w) or \
            next((v for k, v in aliases.items() if k in w), None)
        if not mode:
            return f"I don't know a mode called {wanted}."
        self.sessions.get(name)["mode"] = mode
        self.sessions.save()
        self.runner_for(name or self.sessions.current).apply_settings()
        log("mode", f"[{name or self.sessions.current}] {mode}", "green")
        return f"{mode_label(mode)} mode."

    def set_model(self, wanted, name=None):
        w = wanted.strip().lower()
        model = next((m for m, label in MODELS if w in (m, label.lower())), None) or \
            next((m for m, label in MODELS if w and w.split()[0] in label.lower()), None)
        if not model:
            return f"I don't know a model called {wanted}."
        self.sessions.get(name)["model"] = model
        self.sessions.save()
        self.runner_for(name or self.sessions.current).apply_settings()
        log("model", f"[{name or self.sessions.current}] {model}", "green")
        return f"Using {model_label(model)} from now on in this session."

    def set_effort(self, wanted, name=None):
        w = wanted.strip().lower()
        effort = next((e for e in EFFORTS if e == w or (w and e.startswith(w))), None)
        if not effort:
            return f"Effort can be {', '.join(EFFORTS)}."
        self.sessions.get(name)["effort"] = effort
        self.sessions.save()
        self.runner_for(name or self.sessions.current).apply_settings()
        log("effort", f"[{name or self.sessions.current}] {effort}", "green")
        return f"Effort set to {effort} for this session."

    def cycle_mode(self):
        """Shift-Tab in the text box, like Claude Code."""
        modes = [m for m, _ in MODES if m != "bypassPermissions"]
        current = self.sessions.get().get("mode") or self.cfg["permission_mode"]
        nxt = modes[(modes.index(current) + 1) % len(modes)] if current in modes else modes[0]
        self.set_mode(nxt)

    def notify(self, subtitle, text):
        def esc(t):
            return t.replace("\\", "\\\\").replace('"', '\\"')
        subprocess.Popen(["osascript", "-e", f'display notification "{esc(text)}" with title "Juniper" '
                                             f'subtitle "{esc(subtitle)}" sound name "Glass"'],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def compact(self):
        """From the menu bar: compact the current session's conversation."""
        self.interrupt()
        self.state.set(heard="Compact the conversation", dismissed=False)
        self.sessions.new_turn(self.sessions.current, "Compact the conversation")
        self.last_thread = self.runner.start("/compact")

    def switch_session(self, name):
        """From the menu bar."""
        self.enter_session(name)

    def new_session_in(self, folder):
        """From the menu bar's folder picker."""
        name = self.sessions.create(os.path.basename(folder), folder)
        self.enter_session(name)

    def clear_session(self, name=None, you=None):
        name = name or self.sessions.current
        r = self.runner_for(name)
        if r.busy():
            r.cancel()
            if r.thread:
                r.thread.join(timeout=10)
        stopped = len(r.bg_tasks)
        r.stop()                       # its Claude Code holds the old conversation: start afresh next time
        self.sessions.clear(name)
        log("session", f"cleared {name}", "green")
        if you is not None or name == self.sessions.current:
            extra = f" That stopped {stopped} thing{'s' * (stopped != 1)} it was running in the background." if stopped else ""
            self.answer(you or "Clear this session", f"Okay, the {name} session is cleared.{extra}")

    # -- controls (called from the menu bar and the global keys) ------------------------------

    def interrupt(self):
        """Stop the open session's running task (if any) and wait for it to end. Returns whether one was."""
        r = self.runner
        if not r.busy():
            return False
        r.cancel(clear_queue=False)     # "hey Juniper" cuts in; what's queued still runs afterwards
        self.speaker.stop()
        if r.thread:
            r.thread.join(timeout=10)
        return True

    def stop_everything(self):
        """Stop talking and cancel the open session's task (other sessions keep going)."""
        if self.runner.busy() or self.speaker.speaking():
            log("you", "stop", "cyan")
        self.runner.cancel()
        self.speaker.stop()
        self.state.set(dismissed=True)


    def save_prefs(self):
        """Remember the switches you flip from the menu or keys (open mic, the corner logo)."""
        set_state(open_mic=self.open_mic, show_logo=self.show_logo, speak_answers=self.speak_answers,
                  speak_typed=self.speak_typed, logo_pos=self.logo_pos, logo_size=self.logo_size,
                  overlay_w=self.overlay_w, overlay_h=self.overlay_h)

    def should_speak(self, typed=False):
        """Spoken replies are on, and (for a typed request) speaking typed answers is on too."""
        return self.speak_answers and (not typed or self.speak_typed)

    def set_speak(self, answers=None, typed=None):
        if answers is not None:
            self.speak_answers = answers
            if not answers:
                self.speaker.stop()
        if typed is not None:
            self.speak_typed = typed
        self.save_prefs()

    def set_overlay_size(self, w, h):
        self.overlay_w, self.overlay_h = w, h
        self.save_prefs()

    def set_logo_size(self, size):
        self.logo_size = size
        self.save_prefs()

    def set_logo_pos(self, pos):
        self.logo_pos = pos
        self.save_prefs()

    def set_show_logo(self, on):
        self.show_logo = on
        self.save_prefs()
        log("logo", "corner logo on" if on else "corner logo off", "dim")

    def set_open_mic(self, on):
        self.open_mic = on
        self.save_prefs()
        if on:
            self.mic.open()
            log("mic", f"open mic on: listening for \"hey {self.cfg['wake_name']}\" on {self.mic.name()}", "green")
            chime(self.cfg, "Purr")
        else:
            self.recorder = None
            with self.ptt_lock:
                if not self.ptt:
                    self.mic.close()
            log("mic", f"open mic off: hold {key_hint(self.cfg['ptt_key'])} to talk", "yellow")
            chime(self.cfg, "Bottle")

    def ptt_down(self):
        """Push-to-talk key pressed: hold it while you talk, or tap it and talk until you pause."""
        with self.ptt_lock:
            if self.ptt:
                return            # key repeat
            self.speaker.stop()
            # With the mic already open, keep the last moment of audio so the first word isn't clipped.
            prefix = list(self.history)[-10:] if self.mic.is_open() else []
            self.ptt = {"frames": prefix, "start": time.time(), "recorder": None}
        self.recorder = None
        chime(self.cfg, "Tink")
        self.state.set(recording=True, followup=False, heard="", reply="", tool="", dismissed=False)
        self.mic.open()

    def ptt_up(self):
        with self.ptt_lock:
            p = self.ptt
            if not p or p["recorder"]:
                return
            if time.time() - p["start"] < 0.35:   # a tap: keep listening until you pause
                p["recorder"] = Recorder(self.cfg, self.segmenter.vad, "ptt", p["frames"], time.time(),
                                         self.cfg["listen_seconds"])
                return
            self.ptt = None
        self.finish_ptt(b"".join(p["frames"]))

    def record_toggle(self):
        """The overlay's mic button. Click: record until you pause (like tapping push-to-talk). Click
        again while recording: stop and send what you've said so far."""
        with self.ptt_lock:
            p = self.ptt
        if not p:
            self.ptt_down()
            self.ptt_up()                     # straight away = a tap: listen until you pause
            return
        with self.ptt_lock:
            self.ptt = None
        rec = p["recorder"]
        pcm = b"".join((rec.frames if rec.speaking else rec.prefix + list(rec.pre)) if rec else p["frames"])
        self.finish_ptt(pcm)

    def recording_now(self):
        return self.ptt is not None

    def ptt_frame(self, p, frame, now):
        if p["recorder"]:
            pcm = p["recorder"].push(frame, now)
        else:
            p["frames"].append(frame)
            too_long = len(p["frames"]) * FRAME_MS >= self.cfg["max_utterance_seconds"] * 1000
            pcm = b"".join(p["frames"]) if too_long else None
        if pcm is not None:
            with self.ptt_lock:
                self.ptt = None
            self.finish_ptt(pcm)

    def finish_ptt(self, pcm):
        if not self.open_mic:
            self.mic.close()
        if len(pcm) < RATE * 2 * 0.3:
            log("ptt", "nothing recorded", "dim")
            self.state.set(recording=False, dismissed=True)
            return
        self.utterances.put(("ptt", pcm))

    # -- audio plumbing ---------------------------------------------------------------------

    def process(self, item):
        try:
            if item[0] == "segment":
                self.handle(item[1], item[2])
            else:
                self.handle_recorded(item[0], item[1])
        except Exception as e:  # keep listening whatever happens
            log("error", repr(e), "red")

    def worker(self):
        while True:
            self.process(self.utterances.get())

    def feed(self, frame, now):
        """Open-mic listening: one 30 ms frame at a time."""
        self.history.append(frame)
        if not self.sherpa:
            out = self.segmenter.push(frame, now)
            if out:
                self.utterances.put(("segment", *out))
            return
        self.since_recorder += 1
        hit = self.wake.push(frame)
        if self.recorder:
            pcm = self.recorder.push(frame, now)
            if self.recorder.kind == "followup" and self.recorder.speaking and self.state.followup:
                self.state.set(recording=True, followup=False)     # you started talking
            if pcm is not None:
                if self.recorder.kind == "followup":
                    self.state.set(followup=False)
                self.utterances.put((self.recorder.kind, pcm))
                self.recorder, self.since_recorder = None, 0
            return
        if self.listen_request:
            # Pick up from where "hey Juniper" ended: you may already have started (or even finished)
            # your request while it was being checked, so replay that audio into the new recording.
            self.listen_request = False
            n = min(self.since_recorder, len(self.history))
            rec = Recorder(self.cfg, self.segmenter.vad, "command", [], now, self.cfg["listen_seconds"])
            for earlier in list(self.history)[-n:]:
                pcm = rec.push(earlier, now)
                if pcm is not None:
                    self.utterances.put(("command", pcm))
                    self.since_recorder = 0
                    return
            self.recorder = rec
            return
        if hit:
            log("wake", hit, "yellow")
            self.speaker.stop()
            chime(self.cfg, "Tink")
            self.state.set(recording=True, followup=False, heard="", reply="", tool="", dismissed=False)
            self.recorder = Recorder(self.cfg, self.segmenter.vad, "wake", self.history, now,
                                     self.cfg["listen_seconds"])
            return
        # Follow-up window: once Claude has finished answering, listen briefly without the wake word.
        r = self.runner
        # (Not after a typed request: if you typed, you're not about to talk.)
        if (self.cfg["followup_seconds"] and r.finished_at > self.followup_from and not r.typed
                and not r.busy() and not self.speaker.speaking() and now - self.speaker.quiet_since > 0.3):
            self.followup_from = r.finished_at
            self.state.set(followup=True)
            self.recorder = Recorder(self.cfg, self.segmenter.vad, "followup", [], now,
                                     self.cfg["followup_seconds"])

    def audio_loop(self):
        buf, loudest, checked_open = b"", 0, 0.0
        while True:
            try:
                buf += self.mic.frames.get(timeout=0.2)
            except queue.Empty:
                continue
            while len(buf) >= FRAME_BYTES:
                frame, buf = buf[:FRAME_BYTES], buf[FRAME_BYTES:]
                now = time.time()
                samples = np.frombuffer(frame, np.int16)
                peak = int(np.abs(samples).max())
                self.state.level = min(1.0, float(np.sqrt(np.mean(samples.astype(np.float32) ** 2))) / 3000)
                # Warn once if the mic gives pure silence (no microphone permission).
                if checked_open != self.mic.opened_at:
                    loudest = max(loudest, peak)
                    if now - self.mic.opened_at > 5:
                        checked_open = self.mic.opened_at
                        if loudest == 0:
                            log("warn", "the microphone is sending pure silence. Allow Juniper (or your terminal) in "
                                        "System Settings > Privacy & Security > Microphone, then restart.", "red")
                        loudest = 0
                with self.ptt_lock:
                    p = self.ptt
                if p:
                    self.history.append(frame)
                    self.ptt_frame(p, frame, now)
                elif self.open_mic:
                    self.feed(frame, now)

    def start(self):
        """Start listening in background threads (the main thread is left for the menu bar)."""
        self.approvals.listen()
        threading.Thread(target=self.worker, daemon=True).start()
        threading.Thread(target=self.audio_loop, daemon=True).start()
        if self.open_mic:
            self.mic.open()
            log("ready", f"listening for \"hey {self.cfg['wake_name']}\" on {self.mic.name()}. Session: {self.sessions.current}", "green")
        else:
            log("ready", f"open mic is off: hold {key_hint(self.cfg['ptt_key'])} to talk. "
                         f"Session: {self.sessions.current}", "green")
        if BUNDLED:
            log("ready", f"Juniper {CODE_VERSION} (app {APP_VERSION}) from {HERE}", "dim")
        chime(self.cfg, "Purr")
        import updater
        self.updater = updater.start(self)   # over-the-air updates (the packaged app only)
        threading.Thread(target=retire_old_app, args=(self,), daemon=True).start()

    def shutdown(self):
        for r in self.runners.values():
            r.stop()                   # ends each session's Claude Code (and its background tasks)
        self.speaker.stop()
        self.mic.close()

    def run_file(self, path):
        """Feed a recording through the same pipeline (for testing without talking)."""
        raw = subprocess.run(["ffmpeg", "-loglevel", "error", "-i", path, "-f", "s16le", "-ac", "1",
                              "-ar", str(RATE), "-"], capture_output=True, check=True).stdout
        raw += b"\0" * (RATE * 2 * 2)  # trailing silence so the last utterance ends
        for i in range(0, len(raw) - FRAME_BYTES + 1, FRAME_BYTES):
            self.feed(raw[i:i + FRAME_BYTES], time.time())
            while not self.utterances.empty():
                # A recording plays back instantly, so let each request finish, as a person would.
                self.process(self.utterances.get())
                if self.last_thread:
                    self.last_thread.join()
                self.speaker.wait()
                time.sleep(0.5)
        if self.last_thread:
            self.last_thread.join()
        self.speaker.wait()


def enroll(cfg, files=None):
    """Record you saying a few requests and build your voice profile."""
    import webrtcvad
    voice = VoiceID(cfg)
    if files:
        clips = [subprocess.run(["ffmpeg", "-loglevel", "error", "-i", f, "-f", "s16le", "-ac", "1", "-ar", str(RATE),
                                 "-"], capture_output=True, check=True).stdout for f in files]
    else:
        import sounddevice as sd
        vad, audio, clips = webrtcvad.Vad(cfg["vad_level"]), queue.Queue(), []
        print("\nVoice enrolment. Read each line out loud in your normal voice, from where you usually sit.\n"
              "Wait for the chime before each one.\n")
        with sd.RawInputStream(samplerate=RATE, blocksize=FRAME_BYTES // 2, channels=1, dtype="int16",
                               callback=lambda d, *_: audio.put(bytes(d)), device=cfg["input_device"] or None):
            i, lines = 0, enrol_lines(cfg)
            while i < len(lines):
                print(f"  {i + 1}/{len(lines)}  \033[1m{lines[i]}\033[0m")
                subprocess.run(["afplay", "/System/Library/Sounds/Tink.aiff"])
                while not audio.empty():   # drop the chime and anything before it
                    audio.get()
                rec, buf, pcm = Recorder(cfg, vad, "enrol", [], time.time(), 10), b"", None
                while pcm is None:
                    buf += audio.get()
                    while len(buf) >= FRAME_BYTES and pcm is None:
                        frame, buf = buf[:FRAME_BYTES], buf[FRAME_BYTES:]
                        pcm = rec.push(frame, time.time())
                if len(pcm) < RATE * 2 * 1.5:
                    print("        didn't catch that, once more")
                    continue
                clips.append(pcm)
                i += 1
    print("\nCalibrating against other voices…")
    yours, closest, threshold = voice.enroll(clips)
    print(f"Saved your voice profile to {VoiceID.PATH}.")
    print(f"Your recordings matched your profile at {' '.join(f'{x:.2f}' for x in yours)}; "
          f"the closest other voice reached {closest:.2f}  ->  match needed: {threshold:.2f}")
    if min(yours) <= closest + 0.05:
        print("Some of your recordings were close to other voices (background noise, or a different distance "
              "from the mic?). Run --enroll again somewhere quiet for a clearer gap.")


def main():
    p = argparse.ArgumentParser(description="Voice front end for Claude Code. Say \"hey Juniper\".")
    p.add_argument("--config", default=str(DATA / "config.toml"))
    p.add_argument("--file", help="process an audio file instead of the microphone")
    p.add_argument("--mute", action="store_true", help="print replies instead of speaking them")
    p.add_argument("--dry-run", action="store_true", help="transcribe only, don't call Claude")
    p.add_argument("--list-devices", action="store_true")
    p.add_argument("--enroll", nargs="*", metavar="FILE",
                   help="record your voice so only you can wake it (or enrol from audio files)")
    p.add_argument("--no-ui", action="store_true", help="no menu bar icon, overlay or keys; just the terminal")
    a = p.parse_args()
    if a.enroll is not None:
        enroll(load_config(Path(a.config)), a.enroll)
        return
    if a.list_devices:
        import sounddevice as sd
        print(sd.query_devices())
        return
    ensure_config(a.config)
    cfg = load_config(Path(a.config))
    if a.file:
        app = App(cfg, mute=a.mute, dry_run=a.dry_run)
        app.run_file(a.file)
        return

    # Only one copy may listen at a time (e.g. the login app plus one started from a terminal).
    import fcntl
    lock = open(DATA / ".lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit("Juniper is already running (look for it in the menu bar).")

    if needs_setup() and not a.no_ui:
        import onboarding
        if onboarding.run(cfg, Path(a.config)):
            restart()            # start properly with the settings the setup just chose
        return

    app = App(cfg, mute=a.mute, dry_run=a.dry_run)
    app.start()
    try:
        if a.no_ui:
            while True:
                time.sleep(3600)
        else:
            import ui
            ui.run(app)
    except KeyboardInterrupt:
        pass
    finally:
        app.shutdown()


def restart():
    """Replace this process with a fresh copy of itself (the lock is released on exec)."""
    exe = os.environ.get("JUNIPER_EXE")
    if exe:
        os.execv(exe, [exe])                      # the app: its launcher starts Python again
    os.execv(sys.executable, [sys.executable] + sys.argv)


if __name__ == "__main__":
    main()
