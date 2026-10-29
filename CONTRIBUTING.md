# Contributing to Juniper

Thanks for helping. A few things keep the project easy to work on.

## Getting set up

Follow "Build from source" in the [README](README.md). Run your copy with `./juniper` to see its
log as you talk to it, and use `JUNIPER_HOME=/tmp/juniper-test ./juniper` for a clean set of
settings (it runs the first-run setup).

## Making a change

1. Open an issue first for anything bigger than a small fix, so we can agree on the approach.
2. Work on a branch and open a pull request against `main`. Keep each pull request to one change.
3. Say in the pull request what you changed, why, and how you checked it. For anything you can
   see or hear, a screenshot or a short recording helps a lot.
4. Match the code around you: plain Python, short comments that say why rather than what,
   user-facing text in plain words.

## Things to keep in mind

- **Privacy:** audio never leaves the Mac. Only the text of a request goes to Claude Code, and
  only the reply goes to the voice service. Changes must keep it that way.
- **Secrets:** never commit keys, `.env` files or anything from your data folder. The ElevenLabs
  key lives in the Keychain.
- **Updates:** `boot.py` is never updated over the air, so keep it small and careful. Anything in
  `packaging/` or `requirements.txt` needs an app release, not a code update.
- **Claude Code:** Juniper drives the official `claude` CLI and never calls Anthropic's API
  directly or handles your Claude credentials.

## Reporting a security problem

Please don't open a public issue. Use GitHub's private vulnerability reporting on the repository
(Security › Report a vulnerability).
