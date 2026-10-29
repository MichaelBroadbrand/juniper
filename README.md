# Juniper

**Talk to your AI coding agent from your Mac's menu bar.** Say "hey Juniper, …" (or hold a key)
and your request goes to the real Claude Code on your Mac, with Claude or the AI you prefer
(DeepSeek, Qwen, Kimi, GLM, OpenRouter, a model on your Mac with Ollama, or any compatible API).
Your `CLAUDE.md`, MCP servers, skills and hooks all apply. The reply is spoken aloud and shown in
a small floating overlay, with every command it runs and every file it changes.

Your audio stays on your Mac. A tiny keyword model listens for "hey Juniper" using under 1% of
one CPU core, a voice check makes sure it's you, and on-device speech recognition turns your
request into text. Only that text goes to your AI, and only its reply goes to the voice
service (ElevenLabs, or the Mac's own voice, which never leaves your Mac).

> Juniper is an independent open-source project. It isn't made, endorsed or supported by
> Anthropic. Claude and Claude Code are trademarks of Anthropic, PBC.

## Install

Download `Juniper-<version>.dmg` from the [latest release](https://github.com/MichaelBroadbrand/juniper/releases/latest),
open it and drag Juniper into Applications. The app is signed and notarised by Apple, so it
opens like any other download.

On first open, a short setup walks you through:

1. **The microphone.**
2. **Your AI:** pick the ones you use and connect each: sign in with your Claude subscription, or
   paste an API key (it goes in your Keychain). It installs Claude Code if you don't have it.
3. **A voice:** your own ElevenLabs key (stored in your Keychain), or the free Mac voice.
4. **Teaching it your voice:** read five lines, so that "hey Juniper" only works for you.
5. **Your projects folder**, so you can say "new session for my-app".

**Requirements:** an Apple Silicon Mac on macOS 14 or later, and one of: a Claude subscription,
an API key for Anthropic, DeepSeek, Qwen (Alibaba Cloud Model Studio), Kimi, GLM (Z.ai) or
OpenRouter, Ollama with a model, or any Anthropic-compatible endpoint. macOS 26 uses Apple's on-device speech recognition; older versions download a
Whisper model (about 150 MB) the first time it's needed.

## Using it

| | |
|---|---|
| "Hey Juniper, …" | Ask anything, in one breath or with a pause after "hey Juniper" |
| Hold **⌃⌥Space** | Push to talk: speak while holding, let go to send |
| Tap **⌃⌥Space** | Talk until you pause |
| 🎙 in the input bar | Speak without open mic: records until you pause |
| **⌃⌥M** | Open mic on/off. Off closes the mic completely; push-to-talk still works |
| Click the logo, or **⌃⌥T** | Type a request instead (Return sends, Shift-Return adds a line, Esc folds it away) |
| Drag files onto the overlay or logo, 📎, or ⌘V | Attach files or a pasted screenshot to your next request |
| ■ in the input bar, or **⌃⌥Esc** | Stop the current task and the voice |
| "Hey Juniper, stop" | Cut off speech or cancel the running task |
| "Hey Juniper, clear this session" | Start the current session fresh (sessions otherwise never expire) |

While Claude is answering, typed and push-to-talk requests queue up behind it; "hey Juniper"
cuts in. Anything Claude starts in the background, like a dev server, keeps running between
requests.

**Menu bar icon:** waveform = listening for "hey Juniper", crossed-out mic = open mic off,
mic = recording you, pulsing berries = Claude is working, speaker = talking, ear = waiting for a
follow-up. Click it for quick toggles, your sessions and usage; right-click for a classic menu.

**The overlay** shows the conversation like Claude Code does. Under the text box are the
**mode** (Auto, Ask before acting, Accept edits, Plan, Bypass permissions; Shift-Tab cycles
them), the **model** and a **context** gauge. When Claude needs permission, wants a plan
approved, or asks you something, a card appears: press a button, say "yes"/"no", or type.
Type `/` for commands: Juniper's own (`/clear`, `/compact`, `/mode`, `/model`, `/sessions`,
`/new`, `/stop`, `/help`) plus Claude Code's commands and your skills.

## Your AI

Juniper drives Claude Code, Anthropic's coding agent, which does the actual work on your Mac.
The model behind it is your choice:

| AI | How you connect |
|---|---|
| Claude | Your Claude subscription (Pro or Max), or an Anthropic API key |
| DeepSeek, Qwen, Kimi, GLM, OpenRouter | An API key from that provider; Claude Code talks to its Anthropic-compatible API |
| Ollama | A model on your Mac; nothing leaves it |
| Other | Any Anthropic-compatible address (a company gateway, LiteLLM, …) |

Connect them in the setup, or later from the menu bar (Add or change AIs…). Each session keeps
its AI; the model chip under the text box switches model or AI (a new AI starts the
conversation fresh), as do `/ai deepseek` and `/model …`, or saying "new session for my-app on
DeepSeek". **We never see your API keys:** Juniper has no servers. Keys live in your login
Keychain, Claude Code reads one only when it needs it (they're never in the environment of the
commands it runs), and it's sent only to its own provider. With another provider, nothing goes
to Anthropic.

## Sessions

Every conversation belongs to a session, and each session works in a folder, so that project's
`CLAUDE.md`, memory and settings apply. "general" works in your home folder. Sessions last until
you clear them and survive restarts. They're ordinary Claude Code sessions, so `claude --resume`
in the folder shows them too.

- "Hey Juniper, new session for my-app" (any folder inside your projects folders)
- "Switch to my-app" / "go back to general" / "switch to my-app and run the tests"
- "Which session am I in?" / "list my sessions" / "clear the my-app session"
- "Pick up my last my-app session": continue what you were doing in the terminal there

Sessions run side by side: switching never stops a task. When one finishes in another session
you get a notification, and switching to it reads the answer out.

## Settings

Menu › Settings… opens `config.toml` in `~/Library/Application Support/Juniper` (restart after
changing it). Among other things: the voice, keys, permission mode, the wake word (`wake_name`,
any word the wake-word model knows), its sensitivity, the voice-check threshold, and
`vocabulary`, a list of names speech recognition should expect.

## Build from source

You need Xcode's command-line tools (for `swiftc`) and Python 3.11.

```sh
git clone https://github.com/MichaelBroadbrand/juniper.git && cd juniper
python3.11 -m venv .venv && .venv/bin/pip install -r requirements.txt
scripts/fetch-models.sh        # the wake-word and voice-check models (~60 MB), hash-checked
scripts/dev-app.sh             # builds "Juniper Dev.app" here, plus the speech helper
open "Juniper Dev.app"         # or ./juniper to run with its log in the terminal
```

Your settings, sessions and logs go to `~/Library/Application Support/Juniper`
(`JUNIPER_HOME=/some/folder` keeps a separate set while testing).
Testing without talking: `./juniper --file clip.wav --mute --dry-run`.

### How it fits together

| File | What it does |
|---|---|
| `juniper.py` | Microphone, wake word, voice check, speech recognition, one long-lived Claude Code process per session, speaking |
| `connections.py` | The AIs Juniper can use, the ones you've connected, checking keys and listing models |
| `ui.py` | Menu bar icon and panel, overlay, logo, global keys |
| `onboarding.py`, `onboarding/` | The first-run setup (a web page in a native window) |
| `bin/approve_mcp.py` | A tiny MCP server Claude Code calls for permission, which asks you in the overlay |
| `bin/capture-hook` | Hides the overlay while Claude takes a screenshot |
| `stt/transcribe.swift` | Apple's on-device speech recognition, as a command-line helper |
| `updater.py`, `boot.py`, `ed25519.py` | Signed over-the-air updates |
| `packaging/` | Builds, signs and notarises the app and DMG, and publishes releases |

## Packaging and releases

```sh
packaging/package.sh                                        # → packaging/dist/Juniper.dmg
packaging/publish-update.sh code 2.0.1 "What changed"      # code only: installs by itself
packaging/publish-update.sh app  2.1.0 "What changed"      # new DMG: people are offered it
```

- The DMG holds a self-contained `Juniper.app`: its own Python and packages, the models and
  the speech helper. Nothing of the person building it goes in.
- With a "Developer ID Application" certificate in the Keychain and notary credentials stored
  (`xcrun notarytool store-credentials juniper --apple-id … --team-id …`), the app is signed and
  notarised. Without them it's signed ad hoc and needs Privacy & Security › Open Anyway, and
  `publish-update.sh app` refuses to publish it.
- **Code updates** (the Python files and the setup page, about 100 KB) download in the background
  and install the next time Juniper is idle. **App updates** are for new packages, models or
  anything in `packaging/`; the panel offers the new DMG.
- Releases are signed: the feed with an Ed25519 key (`~/.config/juniper/update-key`; the app
  holds only the public half), and the code bundle by its hash in the signed feed. A forged
  feed or a swapped bundle is refused. `boot.py`, which is never updated over the air, sets aside
  an update that fails to start.
- **Forks:** make your own key (32 random bytes, hex, in that file), put its public half
  (`ed25519.public_key`) in `updater.py`, and point `JUNIPER_REPO` and `FEED` at your repo.

## Contributing

Issues and pull requests are welcome. See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

[MIT](LICENSE)
