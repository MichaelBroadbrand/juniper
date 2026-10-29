"""The AIs Juniper can use, and the ones you've connected.

Every AI here runs inside Claude Code, Anthropic's coding agent on your Mac: Claude itself, or
another provider's models through that provider's Anthropic-compatible API (DeepSeek, Qwen, Kimi,
GLM, OpenRouter, Ollama on this Mac, or any compatible endpoint). Claude Code still does the
work (reading and editing files, running commands, your CLAUDE.md, MCP servers and skills); the
provider supplies the model.

What you've connected is in connections.json in your data folder: which AIs, how (your Claude
subscription, an API key, or a local model), the model each uses, and your default. API keys are
never in that file: they're in your login Keychain, and Claude Code reads one only when it needs
it (its apiKeyHelper setting runs `security`), so the key isn't in the environment of the
commands Claude runs, and it's sent only to the provider it belongs to. Juniper has no servers.
"""
import json
import re
import urllib.error
import urllib.request

from juniper import DATA, KEYCHAIN_SERVICE, log, save_secret, secret

PATH = DATA / "connections.json"

# kind "claude": runs in Claude Code. (Other agents, with their own harness, come in a later change.)
PROVIDERS = {
    "claude": {
        "name": "Claude", "maker": "Anthropic", "blurb": "Subscription or API key",
        "desc": "Claude Code with Claude, as you know it.", "ways": ["subscription", "key"],
        "key_url": "https://console.anthropic.com/settings/keys", "key_hint": "sk-ant-…",
        "models_url": "https://api.anthropic.com/v1/models",
    },
    "deepseek": {
        "name": "DeepSeek", "maker": "DeepSeek", "blurb": "API key", "ways": ["key"],
        "base_url": "https://api.deepseek.com/anthropic", "models_url": "https://api.deepseek.com/models",
        "key_url": "https://platform.deepseek.com/api_keys", "key_hint": "sk-…",
        "prefer": [r"v4-pro", r"pro", r"chat"], "fast": [r"flash", r"chat"],
        "models": ["deepseek-v4-pro", "deepseek-flash"],
    },
    "qwen": {
        "name": "Qwen", "maker": "Alibaba Cloud Model Studio", "blurb": "Alibaba · API key", "ways": ["key"],
        "base_url": "https://dashscope-intl.aliyuncs.com/apps/anthropic",
        "models_url": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1/models",
        "key_url": "https://modelstudio.console.alibabacloud.com/?tab=playground#/api-key", "key_hint": "sk-…",
        "prefer": [r"qwen3.*coder.*plus", r"coder.*plus", r"qwen3.*max", r"plus"], "fast": [r"coder.*flash", r"flash", r"turbo"],
        "models": ["qwen3-coder-plus", "qwen3-coder-flash"],
        "note": "International (Singapore) endpoint. For mainland China, use Other with dashscope.aliyuncs.com.",
    },
    "kimi": {
        "name": "Kimi", "maker": "Moonshot AI", "blurb": "Moonshot · API key", "ways": ["key"],
        "base_url": "https://api.moonshot.ai/anthropic", "models_url": "https://api.moonshot.ai/v1/models",
        "key_url": "https://platform.moonshot.ai/console/api-keys", "key_hint": "sk-…",
        "prefer": [r"kimi-k2\.\d", r"kimi-k2"], "fast": [r"turbo", r"kimi-k2"], "models": ["kimi-k2.5"],
    },
    "glm": {
        "name": "GLM", "maker": "Z.ai", "blurb": "Z.ai · API key", "ways": ["key"],
        "base_url": "https://api.z.ai/api/anthropic",
        "key_url": "https://z.ai/manage-apikey/apikey-list", "key_hint": "your Z.ai key",
        "prefer": [r"^glm-5\.\d$", r"^glm-5", r"^glm-4\.6"], "fast": [r"flash", r"air"],
        "models": ["glm-5.3", "glm-5.3-flash"],
    },
    "openrouter": {
        "name": "OpenRouter", "maker": "OpenRouter", "blurb": "Many models, one key", "ways": ["key"],
        "base_url": "https://openrouter.ai/api", "models_url": "https://openrouter.ai/api/v1/models",
        "key_check_url": "https://openrouter.ai/api/v1/key",
        "key_url": "https://openrouter.ai/settings/keys", "key_hint": "sk-or-…",
        "prefer": [r"qwen.*coder", r"deepseek", r"kimi", r"glm"], "fast": [r"flash", r"mini", r"haiku"],
        "models": [],
    },
    "ollama": {
        "name": "Ollama", "maker": "Models on this Mac", "blurb": "Free, on this Mac",
        "desc": "Runs in Claude Code with a model on this Mac. Nothing leaves your Mac.", "ways": ["local"],
        "base_url": "http://localhost:11434", "models_url": "http://localhost:11434/api/tags",
        "key_url": "https://ollama.com/download", "prefer": [r"coder", r"qwen", r"gpt-oss"], "fast": [], "models": [],
    },
    "custom": {
        "name": "Other", "maker": "Any Anthropic-compatible API", "blurb": "Any compatible API",
        "desc": "Runs in Claude Code with any API that speaks Anthropic's format (a gateway, LiteLLM, …).", "ways": ["key"],
        "key_hint": "its API key (if it needs one)", "prefer": [], "fast": [], "models": [],
    },
}
# Models that can't drive a coding agent (embeddings, speech, images, moderation…).
NOT_CHAT = re.compile(r"embed|tts|whisper|audio|speech|image|dall|rerank|moderation|realtime|ocr|asr|vision-only|search", re.I)


def describe(provider):
    """One line for a provider's card: where it runs and where your requests go."""
    p = PROVIDERS[provider]
    return p.get("desc") or f"Runs in Claude Code with {p['name']}'s models. Your requests go to {p['maker']}."


def key_name(provider):
    """The Keychain entry (and .env name) for a provider's API key."""
    return "ANTHROPIC_API_KEY" if provider == "claude" else f"{provider.upper()}_API_KEY"


class Connections:
    """connections.json: {"default": id, "items": {id: {"provider", "way", "model", "fast_model",
    "base_url", "models"}}}. Until something is saved (and for everyone from before there was a
    choice), you're on Claude with your subscription."""

    ASSUMED = {"claude": {"provider": "claude", "way": "subscription"}}

    def __init__(self):
        self.load()

    def load(self):
        try:
            data = json.loads(PATH.read_text())
        except (OSError, ValueError):
            data = {}
        self.saved = data.get("items") or {}
        self.default = data.get("default") if data.get("default") in self.saved else next(iter(self.saved), "claude")

    @property
    def items(self):
        return self.saved or self.ASSUMED

    def save(self):
        PATH.write_text(json.dumps({"default": self.default, "items": self.saved}, indent=1))

    def get(self, cid=None):
        cid = cid if cid in self.items else self.default
        return {"id": cid, **self.items.get(cid, self.ASSUMED["claude"])}

    def set(self, cid, **fields):
        self.saved.setdefault(cid, {"provider": cid}).update(fields)
        if self.default not in self.saved:
            self.default = cid
        self.save()

    def set_default(self, cid):
        if cid in self.saved:
            self.default = cid
            self.save()

    def remove(self, cid):
        self.saved.pop(cid, None)
        if self.default not in self.saved:
            self.default = next(iter(self.saved), "claude")
        self.save()

    def find(self, words, asked=False):
        """A connected AI by name ("deepseek"), or with asked=True one asked for in a sentence
        ("… on DeepSeek", "with Qwen", "using Kimi"; not "new session for qwen-app")."""
        for cid, c in self.items.items():
            names = {cid, (c.get("name") or PROVIDERS[c["provider"]]["name"]).lower()}
            pattern = "|".join(re.escape(n) for n in names)
            if re.search(rf"\b(?:on|with|using|via|in)\s+(?:the\s+)?(?:{pattern})\b(?![-\w])" if asked
                         else rf"^\s*(?:{pattern})\s*$", words, re.I):
                return cid
        return None


def label(conn, model=None):
    """How a connection shows in menus and chips: "DeepSeek · deepseek-v4-pro"."""
    p = PROVIDERS[conn["provider"]]
    name = conn.get("name") or p["name"]
    if conn["provider"] == "claude":
        return name + (" (API key)" if conn.get("way") == "key" else "")
    return f"{name} · {model or conn.get('model') or 'default model'}"


def launch(conn, model=None):
    """What a session's Claude Code needs for this AI: (environment, settings, model).
    The settings carry apiKeyHelper, which reads the key from the Keychain when Claude Code needs
    it; the environment points Claude Code at the provider and names its models."""
    provider, settings = conn["provider"], {}
    if conn.get("way") == "key":
        settings["apiKeyHelper"] = (f"/usr/bin/security find-generic-password -s '{KEYCHAIN_SERVICE}' "
                                    f"-a '{key_name(provider)}' -w")
    if provider == "claude":
        return {}, settings, model
    # Always a helper for anyone else, even with no key: without one Claude Code would fall back to
    # your Claude login, and send that to the other provider.
    settings.setdefault("apiKeyHelper", "echo ollama" if provider == "ollama" else "echo none")
    model = model or conn.get("model") or (PROVIDERS[provider]["models"] or [""])[0]
    fast = conn.get("fast_model") or model
    env = {"ANTHROPIC_BASE_URL": conn.get("base_url") or PROVIDERS[provider]["base_url"],
           "ANTHROPIC_MODEL": model, "ANTHROPIC_DEFAULT_OPUS_MODEL": model, "ANTHROPIC_DEFAULT_SONNET_MODEL": model,
           "ANTHROPIC_DEFAULT_HAIKU_MODEL": fast, "ANTHROPIC_SMALL_FAST_MODEL": fast, "CLAUDE_CODE_SUBAGENT_MODEL": model,
           # Nothing goes to Anthropic: no usage stats or update checks while on another provider.
           "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"}
    return env, settings, model


# -- checking a key and listing models -----------------------------------------------------------

def _get(url, key=None, provider="", timeout=15):
    headers = {"User-Agent": "Juniper"}
    if key:
        if provider == "claude":
            headers.update({"x-api-key": key, "anthropic-version": "2023-06-01"})
        else:
            headers["Authorization"] = f"Bearer {key}"
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=timeout) as r:
        return json.loads(r.read())


def _ping(base_url, key, model):
    """A one-token request in Anthropic's format: the surest check for an endpoint with no model list."""
    body = json.dumps({"model": model, "max_tokens": 1, "messages": [{"role": "user", "content": "hi"}]}).encode()
    headers = {"content-type": "application/json", "anthropic-version": "2023-06-01", "User-Agent": "Juniper"}
    if key:
        headers.update({"x-api-key": key, "Authorization": f"Bearer {key}"})
    req = urllib.request.Request(base_url.rstrip("/") + "/v1/messages", data=body, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def _model_ids(data, provider):
    rows = data.get("models") if provider == "ollama" else data.get("data")
    ids = []
    for m in rows or []:
        mid = m.get("name") if provider == "ollama" else m.get("id")
        if not mid or NOT_CHAT.search(mid):
            continue
        if provider == "openrouter" and "tools" not in (m.get("supported_parameters") or []):
            continue          # a coding agent needs a model that can call tools
        ids.append(mid)
    return ids


def pick(models, patterns):
    for pat in patterns:
        hit = next((m for m in models if re.search(pat, m, re.I)), None)
        if hit:
            return hit
    return models[0] if models else ""


def check(provider, key="", base_url="", model=""):
    """Is this key (or local install, or endpoint) good? -> {"ok", "message", "models", "model",
    "fast_model"}. A key is only rejected when the provider says so; if it can't be reached, it's
    kept and you're told so."""
    p = PROVIDERS[provider]
    result = {"ok": False, "message": "", "models": [], "model": "", "fast_model": ""}
    try:
        if p.get("key_check_url"):
            _get(p["key_check_url"], key, provider)
        if provider == "custom":
            if not base_url.startswith(("https://", "http://localhost", "http://127.0.0.1")):
                return {**result, "message": "The address should start with https://"}
            if not model:
                return {**result, "message": "Which model should it use? Type its name."}
            _ping(base_url, key, model)
            models = [model]
        elif p.get("models_url"):
            models = _model_ids(_get(p["models_url"], key, provider, timeout=5 if provider == "ollama" else 15), provider)
        else:
            _ping(p["base_url"], key, p["models"][0])
            models = list(p["models"])
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return {**result, "message": "That key didn't work. Check it was copied in full."}
        if provider == "custom" and e.code in (400, 404):
            return {**result, "message": f"It answered, but not as an Anthropic-compatible API would ({e.code})."}
        log("connect", f"{provider}: check returned {e.code}; keeping the key", "yellow")
        models = list(p["models"])
        result["message"] = f"Saved, but {p['name']} answered with an error ({e.code}), so it isn't checked yet."
    except (urllib.error.URLError, OSError, ValueError) as e:
        if provider == "ollama":
            return {**result, "message": "Ollama isn't running on this Mac. Install it from ollama.com, "
                                         "pull a model (e.g. ollama pull qwen3-coder), then check again."}
        log("connect", f"{provider}: couldn't check ({e!r}); keeping the key", "yellow")
        models = list(p["models"])
        result["message"] = f"Couldn't reach {p['name']} to check the key. It's saved; it'll be tried when you use it."
    if provider == "ollama" and not models:
        return {**result, "message": "Ollama is running but has no models yet. Try: ollama pull qwen3-coder"}
    models = models or list(p["models"])
    if provider == "claude":
        return {**result, "ok": True, "models": [], "model": "", "fast_model": ""}
    chosen = model if model in models else pick(models, p["prefer"])
    return {**result, "ok": True, "models": models[:200], "model": chosen,
            "fast_model": pick(models, p["fast"]) if p["fast"] else chosen}


def connect(conns, provider, way, key="", base_url="", model="", name=""):
    """Check and save a connection (the key into the Keychain). Returns check()'s result."""
    if way == "key" and not key and provider != "custom":
        key = secret(key_name(provider))           # already saved: re-check it
    r = check(provider, key, base_url, model)
    if r["ok"]:
        if way == "key" and key:
            save_secret(key_name(provider), key)
        fields = {"provider": provider, "way": way, "model": r["model"], "fast_model": r["fast_model"],
                  "models": r["models"]}
        if provider == "custom":
            fields.update(base_url=base_url.rstrip("/"), name=name or "Other",
                          way="key" if key else "none")
        conns.set(provider, **fields)
        log("connect", f"connected {provider} ({way}){' · ' + r['model'] if r['model'] else ''}", "green")
    return r
