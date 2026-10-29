"""Other coding agents: Codex, Gemini CLI, Qwen Code and OpenCode, driven through the Agent Client
Protocol (ACP, agentclientprotocol.com): JSON-RPC over the agent's stdin and stdout.

Each session with one of these agents has its own agent process (AgentRunner), kept running
between requests like Claude Code is. Juniper installs an agent it needs into its own folder
(npm, into <data>/agents) unless it's already on your Mac.

Signing in is the agent's own business: with a subscription, the agent's own sign-in (in the
browser, or its login command in Terminal) and its own saved credentials; with an API key, the
key comes from your Keychain and is handed to the agent the way it expects, in its environment.
Juniper never sees a subscription's credentials, and no key is ever written to disk by Juniper.
"""
import collections
import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

from juniper import (CODE_VERSION, DATA, VOICE_PROMPT, ClaudeRunner, describe_tool, log, secret, tool_part)

# How each agent is started and signed in. Sign-in method ids differ between agents and change
# between versions, so the right one is picked from what the agent offers (pick_method).
AGENTS = {
    "codex": {"package": "@agentclientprotocol/codex-acp", "bin": "codex-acp", "args": [],
              "key_env": "OPENAI_API_KEY", "sub_methods": ["chat-gpt", "chatgpt"],
              "key_methods": ["api-key", "openai-api-key", "codex-api-key"]},
    "gemini": {"package": "@google/gemini-cli", "bin": "gemini", "args": ["--acp"],
               "key_env": "GEMINI_API_KEY", "sub_methods": ["oauth-personal"], "key_methods": ["gemini-api-key"]},
    "qwen-code": {"package": "@qwen-code/qwen-code", "bin": "qwen", "args": ["--acp"],
                  "key_env": "OPENAI_API_KEY", "sub_methods": [], "key_methods": ["openai"]},
    "opencode": {"package": "opencode-ai", "bin": "opencode", "args": ["acp"],
                 "login": ["auth", "login"], "sub_methods": ["opencode-login"], "key_methods": []},
}
FOLDER = DATA / "agents"
# Keys an agent could pick up from wherever Juniper was started; it only gets the one it should.
KEY_VARS = ("OPENAI_API_KEY", "CODEX_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY", "OPENAI_BASE_URL",
            "OPENAI_MODEL", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
QWEN_PAYG = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
QWEN_CODING_PLAN = "https://coding-intl.dashscope.aliyuncs.com/v1"     # keys that start sk-sp-


class AgentError(Exception):
    pass


# -- finding and installing agents --------------------------------------------------------------

def _search_path():
    """Where programs might be: your PATH plus the places Node, Homebrew and installers use (an
    app opened from Finder doesn't get your shell's PATH)."""
    home = Path.home()
    extra = [home / ".local/bin", Path("/opt/homebrew/bin"), Path("/usr/local/bin"), home / ".npm-global/bin",
             home / ".bun/bin", home / ".volta/bin", home / ".opencode/bin"]
    extra += sorted((home / ".nvm/versions/node").glob("*/bin"), reverse=True)
    return os.pathsep.join([os.environ.get("PATH", "")] + [str(p) for p in extra])


def find_node():
    return shutil.which("node", path=_search_path())


def find_agent(provider):
    spec = AGENTS[provider]
    mine = FOLDER / "node_modules" / ".bin" / spec["bin"]
    if mine.exists():
        return str(mine)
    return shutil.which(spec["bin"], path=_search_path())


def install(provider, progress=lambda line: None):
    """Installs an agent into Juniper's own folder with npm (nothing system-wide)."""
    npm = shutil.which("npm", path=_search_path())
    if not npm:
        raise AgentError("This agent needs Node.js. Install it from nodejs.org, then try again.")
    FOLDER.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, PATH=os.path.dirname(npm) + os.pathsep + _search_path())
    progress(f"Installing {AGENTS[provider]['package']}…")
    p = subprocess.Popen([npm, "install", "--prefix", str(FOLDER), "--no-audit", "--no-fund", "--loglevel=error",
                          AGENTS[provider]["package"]], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env)
    for line in p.stdout:
        if line.strip():
            progress(line.rstrip()[:160])
    if p.wait() or not find_agent(provider):
        raise AgentError("Installing it didn't work. Check your internet connection and try again.")


def agent_env(conn):
    """The agent's environment: your PATH with Node on it, and for an API key, the key."""
    provider = conn["provider"]
    spec = AGENTS[provider]
    env = {k: v for k, v in os.environ.items() if k not in KEY_VARS}
    node = find_node()
    env["PATH"] = (os.path.dirname(node) + os.pathsep if node else "") + _search_path()
    if conn.get("way") == "key":
        import connections
        key = secret(connections.key_name(provider))
        if not key:
            raise AgentError("Its API key isn't in your Keychain any more. Connect it again from the menu bar.")
        env[spec["key_env"]] = key
        if provider == "qwen-code":
            env["OPENAI_BASE_URL"] = QWEN_CODING_PLAN if key.startswith("sk-sp-") else QWEN_PAYG
            env["OPENAI_MODEL"] = conn.get("model") or "qwen3-coder-plus"
    return env


def pick_method(methods, wanted):
    ids = [m.get("id") for m in methods or []]
    return next((w for w in wanted if w in ids), None)


# -- the protocol --------------------------------------------------------------------------------

class Acp:
    """One agent process and the JSON-RPC conversation with it. Updates and the agent's own
    requests (permission) go to the callbacks; call() waits for the answer to a request."""

    def __init__(self, argv, cwd, env, on_update=None, on_request=None, on_exit=None, name=""):
        self.name, self.on_update, self.on_request, self.on_exit = name, on_update, on_request, on_exit
        self.lock = threading.Lock()
        self.pending, self.next_id = {}, 0
        self.stderr = collections.deque(maxlen=40)
        self.proc = subprocess.Popen(argv, cwd=cwd, env=env, text=True, bufsize=1, stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=lambda: [self.stderr.append(l.rstrip()) for l in self.proc.stderr], daemon=True).start()

    def alive(self):
        return self.proc.poll() is None

    def _write(self, msg):
        try:
            with self.lock:
                self.proc.stdin.write(json.dumps(msg) + "\n")
                self.proc.stdin.flush()
            return True
        except (BrokenPipeError, OSError, ValueError):
            return False

    def call(self, method, params, timeout=60):
        with self.lock:
            self.next_id += 1
            rid = self.next_id
            slot = self.pending[rid] = [threading.Event(), None]
        if not self._write({"jsonrpc": "2.0", "id": rid, "method": method, "params": params}):
            raise AgentError(self._why("it stopped"))
        if not slot[0].wait(timeout):
            self.pending.pop(rid, None)
            raise AgentError(f"it didn't answer ({method})")
        msg = slot[1]
        if "error" in msg:
            err = msg["error"] or {}
            raise AgentError(err.get("message") or str(err))
        return msg.get("result") or {}

    def notify(self, method, params):
        self._write({"jsonrpc": "2.0", "method": method, "params": params})

    def respond(self, rid, result=None, error=None):
        msg = {"jsonrpc": "2.0", "id": rid}
        msg.update({"error": error} if error else {"result": result or {}})
        self._write(msg)

    def stop(self):
        if self.alive():
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()

    def _why(self, what):
        last = next((l for l in reversed(self.stderr) if l.strip()), "")
        return f"{what}: {last[:200]}" if last else what

    def _read(self):
        for line in self.proc.stdout:
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            try:
                if "method" in msg and "id" in msg:              # the agent asks us something
                    if self.on_request:
                        threading.Thread(target=self.on_request, args=(msg,), daemon=True).start()
                    else:
                        self.respond(msg["id"], error={"code": -32601, "message": "not supported"})
                elif "method" in msg:
                    if self.on_update:
                        self.on_update(msg)
                else:
                    slot = self.pending.pop(msg.get("id"), None)
                    if slot:
                        slot[1] = msg
                        slot[0].set()
            except Exception as e:      # one odd message mustn't stop the conversation
                log("error", f"[{self.name}] {e!r} handling {msg.get('method') or 'a reply'}", "red")
        self.proc.wait()
        for slot in list(self.pending.values()):
            slot[1] = {"error": {"message": self._why("it stopped")}}
            slot[0].set()
        self.pending.clear()
        if self.on_exit:
            self.on_exit(self)


def start(conn, cwd, on_update=None, on_request=None, on_exit=None, name=""):
    """Starts a connected agent and signs it in. Returns (Acp, initialize result)."""
    provider = conn["provider"]
    exe = find_agent(provider)
    if not exe:
        raise AgentError("It isn't installed. Connect it again from the menu bar.")
    acp = Acp([exe] + AGENTS[provider]["args"], cwd, agent_env(conn), on_update, on_request, on_exit, name)
    try:
        init = acp.call("initialize", {"protocolVersion": 1,
                                       "clientInfo": {"name": "juniper", "title": "Juniper", "version": CODE_VERSION},
                                       "clientCapabilities": {"fs": {"readTextFile": False, "writeTextFile": False},
                                                              "terminal": False}}, timeout=60)
        spec = AGENTS[provider]
        method = pick_method(init.get("authMethods"), spec["key_methods"] if conn.get("way") == "key" else spec["sub_methods"])
        if method and provider != "opencode":          # OpenCode signs in through its own login command
            acp.call("authenticate", {"methodId": method}, timeout=300)
        return acp, init
    except AgentError:
        acp.stop()
        raise


def options(result):
    """Modes and models an agent offers, from session/new or session/load: ([(id, name)], current,
    [(id, name)], current). Agents report them as modes/models or as config options."""
    modes, mode, models, model = [], None, [], None
    m = result.get("modes") or {}
    if m.get("availableModes"):
        modes = [(x.get("id"), x.get("name") or x.get("id")) for x in m["availableModes"]]
        mode = m.get("currentModeId")
    m = result.get("models") or {}
    if m.get("availableModels"):
        models = [(x.get("modelId"), x.get("name") or x.get("modelId")) for x in m["availableModels"]]
        model = m.get("currentModelId")
    for c in result.get("configOptions") or []:
        pairs = [(o.get("value"), o.get("name") or o.get("value")) for o in c.get("options") or []]
        if c.get("id") == "mode" and not modes:
            modes, mode = pairs, c.get("currentValue")
        elif c.get("id") == "model" and not models:
            models, model = pairs, c.get("currentValue")
    nice = lambda pairs: [(i, n[:1].upper() + n[1:] if n == i else n) for i, n in pairs]   # "build" -> "Build"
    return nice(modes), mode, models, model


def verify(conn, progress=lambda line: None):
    """Install (if needed), start and sign in an agent, and open a session to prove it works.
    Returns what it offers: {"modes", "mode", "models", "model"}."""
    provider = conn["provider"]
    if not find_agent(provider):
        install(provider, progress)
    progress("Starting it…")
    acp, _ = start(conn, str(Path.home()), name="setup")
    try:
        result = acp.call("session/new", {"cwd": str(Path.home()), "mcpServers": []}, timeout=120)
    finally:
        acp.stop()
    modes, mode, models, model = options(result)
    return {"modes": modes, "mode": mode, "models": models, "model": model}


def open_login(provider):
    """Agents whose sign-in is a command in Terminal (OpenCode)."""
    from juniper import open_in_terminal
    exe = find_agent(provider)
    if exe and AGENTS[provider].get("login"):
        open_in_terminal(" ".join([f"'{exe}'"] + AGENTS[provider]["login"]))


# -- a session's agent ---------------------------------------------------------------------------

def acp_tool(update):
    """An agent's tool call as the overlay shows Claude's: a block like Claude Code's tool_use."""
    kind, raw = update.get("kind") or "other", update.get("rawInput") or {}
    path = next((loc.get("path") for loc in update.get("locations") or [] if loc.get("path")), "") or \
        raw.get("filePath") or raw.get("file_path") or raw.get("path") or raw.get("absolute_path") or ""
    title = update.get("title") or kind
    if kind == "execute":
        return {"name": "Bash", "input": {"command": raw.get("command") if isinstance(raw.get("command"), str)
                                          else " ".join(raw.get("command") or []) or title}}
    if kind in ("edit", "delete", "move"):
        if "content" in raw:
            return {"name": "Write", "input": {"file_path": path, "content": raw.get("content") or ""}}
        return {"name": "Edit", "input": {"file_path": path, "old_string": raw.get("old_string") or raw.get("oldString") or "",
                                          "new_string": raw.get("new_string") or raw.get("newString") or ""}}
    if kind == "read":
        return {"name": "Read", "input": {"file_path": path or title}}
    if kind == "search":
        return {"name": "Grep", "input": {"pattern": raw.get("pattern") or raw.get("query") or title}}
    if kind == "fetch":
        return {"name": "WebFetch", "input": {"url": raw.get("url") or title}}
    return {"name": title, "input": raw if isinstance(raw, dict) else {}}


class AgentRunner(ClaudeRunner):
    """One session's agent, kept running between requests, fed one request at a time. Everything
    around it (the queue, stopping, speaking, the overlay, permission cards) is ClaudeRunner's."""

    def __init__(self, app, name):
        super().__init__(app, name)
        self.acp, self.sid = None, None
        self.loading = False            # session/load replays the conversation: don't show or say it again
        self.fresh = False              # a new conversation: the voice guidance goes with the first request
        self.chunks, self.chunk_id = [], None
        self.tools = {}                 # toolCallId -> block, as it fills in

    def alive(self):
        return self.acp is not None and self.acp.alive()

    def cancel(self, clear_queue=True):
        if clear_queue and self.queued:
            log("queue", f"[{self.name}] cleared {len(self.queued)} queued", "yellow")
            self.queued = []
        if not self.busy():
            return
        self.cancelled = True
        self.app.approvals.cancel_session(self.name)
        if self.acp and self.sid:
            self.acp.notify("session/cancel", {"sessionId": self.sid})
        turn = self.turn

        def force():
            if not turn.done.wait(8):
                log("agent", f"[{self.name}] didn't stop; ending it", "yellow")
                self.stop()
        threading.Thread(target=force, daemon=True).start()

    def stop(self):
        acp, self.acp, self.sid = self.acp, None, None
        if acp:
            acp.stop()
        if self.turn and not self.turn.done.is_set():
            self._set_tool("")
            self.turn.done.set()

    def stop_process_only(self):
        acp, self.acp, self.sid = self.acp, None, None
        if acp:
            acp.stop()

    def apply_settings(self):
        """A mode or model chosen in Juniper reaches the running agent."""
        if not (self.alive() and self.sid):
            return
        s = self.sessions.get(self.name)
        conn = self.app.connection_for(self.name)
        for what, value in (("mode", s.get("mode")), ("model", s.get("model"))):
            if not value or value == conn.get("current_" + what):
                continue
            try:
                if what == "mode" and conn.get("mode_api") == "modes":
                    self.acp.call("session/set_mode", {"sessionId": self.sid, "modeId": value}, timeout=30)
                elif what == "model" and conn.get("model_api") == "models":
                    self.acp.call("session/set_model", {"sessionId": self.sid, "modelId": value}, timeout=30)
                else:
                    self.acp.call("session/set_config_option", {"sessionId": self.sid, "configId": what, "value": value},
                                  timeout=30)
            except Exception as e:
                log("agent", f"[{self.name}] couldn't set {what} to {value}: {e}", "yellow")

    # -- talking to the agent ------------------------------------------------------------------
    def _spawn(self, session):
        import connections
        conn = self.app.connection_for(self.name)
        folder = session["folder"]
        self.acp, init = start(conn, folder, self._update, self._request, self._exited, self.name)
        caps = init.get("agentCapabilities") or {}
        result = None
        if session.get("session_id") and caps.get("loadSession"):
            self.loading = True
            try:
                result = self.acp.call("session/load", {"sessionId": session["session_id"], "cwd": folder,
                                                        "mcpServers": []}, timeout=120)
                self.sid = session["session_id"]
            except AgentError as e:
                log("session", f"[{self.name}] couldn't continue the last conversation ({e}); starting a fresh one", "yellow")
            finally:
                self.loading = False
        if not self.sid:
            result = self.acp.call("session/new", {"cwd": folder, "mcpServers": []}, timeout=120)
            self.sid, self.fresh = result["sessionId"], True
            session["session_id"] = self.sid
        modes, mode, models, model = options(result or {})
        if modes or models:      # keep the lists for the chips, even before this session's agent runs
            self.app.connections.set(conn["id"], **({"modes": modes, "mode_api": "modes" if (result or {}).get("modes") else "config"}
                                                     if modes else {}),
                                     **({"agent_models": models, "model_api": "models" if (result or {}).get("models") else "config"}
                                        if models else {}))
        self.app.connections.items[conn["id"]].update(current_mode=mode, current_model=model)
        log("agent", f"[{self.name}] started {connections.label(conn, session.get('model') or model)}"
                     f"{' (continuing)' if not self.fresh else ''}", "dim")
        self.apply_settings()

    def _send(self, turn):
        session = self.sessions.get(self.name)
        with self.lock:
            try:
                if not self.alive():
                    self._spawn(session)
            except Exception as e:
                self.stop_process_only()
                self._failed(turn, f"{self.app.ai_name(self.name)} couldn't start: {e}")
                return
            self.cancelled, self.spoke, self.chunks, self.chunk_id = False, False, [], None
            text = turn.prompt
            if self.fresh:          # how to talk when everything is read aloud (Claude Code gets this as a system prompt)
                guide = VOICE_PROMPT.format(name=self.cfg["wake_name"])
                if self.cfg["vocabulary"]:
                    guide += f"\n\nNames and terms this user often says: {self.cfg['vocabulary']}."
                text = f"<voice-guidance>\n{guide}\n</voice-guidance>\n\n{text}"
                self.fresh = False
            log("agent", f"[{self.name}] {turn.prompt}", "cyan")
        threading.Thread(target=self._prompt, args=(turn, text), daemon=True).start()

    def _prompt(self, turn, text):
        try:
            result = self.acp.call("session/prompt", {"sessionId": self.sid, "prompt": [{"type": "text", "text": text}]},
                                   timeout=None)
        except Exception as e:
            self._flush()
            if not turn.done.is_set():
                self._failed(turn, str(e))
            return
        self._flush()
        self._done(turn, result)

    def _exited(self, acp):
        if acp is self.acp:
            self.acp, self.sid = None, None

    def _flush(self):
        """Say what the agent has written since the last tool call (it arrives in small pieces)."""
        text, self.chunks = "".join(self.chunks).strip(), []
        if text:
            log("reply", f"[{self.name}] {text}", "green")
            self._set_tool("")
            self._reply(text)
            self.spoke = True

    def _update(self, msg):
        if msg.get("method") != "session/update":
            return
        u = (msg.get("params") or {}).get("update") or {}
        kind = u.get("sessionUpdate")
        session = self.sessions.get(self.name)
        if kind == "usage_update":
            session["context_used"], session["context_window"] = u.get("used") or 0, u.get("size") or 0
            return
        if kind == "current_mode_update":
            self.app.connections.items.get(session.get("ai"), {})["current_mode"] = u.get("currentModeId")
            return
        if self.loading or self.turn is None or self.turn.done.is_set():
            return                  # history being replayed, or something said between requests
        if kind == "agent_message_chunk":
            content = u.get("content") or {}
            if u.get("messageId") and u["messageId"] != self.chunk_id and self.chunks:
                self._flush()
            self.chunk_id = u.get("messageId")
            if content.get("type") == "text":
                self.chunks.append(content.get("text") or "")
        elif kind == "agent_thought_chunk":
            self._set_tool("Thinking…")
        elif kind in ("tool_call", "tool_call_update"):
            tid = u.get("toolCallId")
            if kind == "tool_call":
                self._flush()
            known = self.tools.get(tid) or {}
            merged = {**known, **{k: v for k, v in u.items() if v not in (None, [], {})}}
            self.tools[tid] = merged
            block = {**acp_tool(merged), "id": tid}
            if not known:
                log("tool", f"[{self.name}] {block['name']} {json.dumps(block['input'])[:140]}", "dim")
                self.sessions.add_tool(self.name, tool_part(block, session["folder"]))
            elif merged.get("rawInput") and not known.get("rawInput"):
                self.sessions.update_tool(self.name, tid, tool_part(block, session["folder"]))
            if merged.get("status") in ("completed", "failed"):
                self.sessions.finish_tool(self.name, tid, merged["status"] == "completed")
                self._set_tool("")
            else:
                self._set_tool(describe_tool(block))
        elif kind == "plan":
            self._set_tool("Planning…")

    def _request(self, msg):
        """The agent asks for permission: the same card, voice and session rules as Claude Code."""
        rid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
        if method != "session/request_permission":
            self.acp.respond(rid, error={"code": -32601, "message": f"{method} isn't supported"})
            return
        offered = params.get("options") or []
        tc = params.get("toolCall") or {}
        merged = {**self.tools.get(tc.get("toolCallId"), {}), **{k: v for k, v in tc.items() if v not in (None, [], {})}}
        block = acp_tool(merged)
        decision = self.app.approvals.decide({"session": self.name, "tool_name": block["name"], "input": block["input"]})
        allow = decision.get("behavior") == "allow"
        wanted = ["allow_once", "allow_always"] if allow else ["reject_once", "reject_always"]
        option = next((o for w in wanted for o in offered if o.get("kind") == w), None)
        if self.cancelled or not option:
            self.acp.respond(rid, {"outcome": {"outcome": "cancelled"}})
        else:
            self.acp.respond(rid, {"outcome": {"outcome": "selected", "optionId": option.get("optionId")}})

    def _done(self, turn, result):
        session = self.sessions.get(self.name)
        session["last_used"] = time.time()
        self._set_tool("")
        if turn.done.is_set():
            return
        if self.cancelled or result.get("stopReason") == "cancelled":
            log("agent", f"[{self.name}] interrupted", "yellow")
            self.sessions.save()
            turn.done.set()
            return
        stop = result.get("stopReason")
        if stop == "refusal":
            self._reply("It declined to do that.")
        elif stop in ("max_tokens", "max_turn_requests"):
            self._reply("It stopped before finishing: it hit its limit for one request. Say carry on to continue.")
        elif not self.spoke:
            self._reply("Done.")
        log("done", f"[{self.name}] {stop}", "dim")
        if not self.current():
            self.sessions.set_unread(self.name, True)
            self.app.notify_finished(self.name, False)
        self.sessions.save()
        self.finished_at = time.time()
        turn.done.set()
        self._next()
