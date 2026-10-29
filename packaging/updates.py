"""Builds signed over-the-air updates (see updater.py for what the app does with them).

    python packaging/updates.py build code 1.0.1 [notes]   → packaging/release/1.0.1/
    python packaging/updates.py build app 1.1.0 [notes]    → also expects the DMG built for 1.1.0

Writes the code bundle (zip), feed.json and feed.sig (Ed25519, with the key in
~/.config/juniper/update-key), and remembers in packaging/releases.json which app version
future code updates need. publish-update.sh runs this, then uploads with `gh release create`.
"""
import base64
import hashlib
import json
import os
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import ed25519  # noqa: E402

REPO = os.environ.get("JUNIPER_REPO") or "MichaelBroadbrand/juniper"     # where releases are published
KEY = Path(os.environ.get("JUNIPER_UPDATE_KEY") or Path.home() / ".config" / "juniper" / "update-key")
STATE = ROOT / "packaging" / "releases.json"
# Everything the app runs from an update: the Python, the setup page and the helper scripts.
# (boot.py, the models and the compiled speech helper stay in the app bundle.)
CODE_FILES = ["juniper.py", "ui.py", "onboarding.py", "connections.py", "updater.py", "ed25519.py", "config.template.toml",
              "onboarding/index.src.html", "onboarding/logo.svg.txt", "bin/approve_mcp.py", "bin/capture-hook"]


def asset_url(version, name):
    return f"https://github.com/{REPO}/releases/download/v{version}/{name}"


def build(kind, version, notes=""):
    state = json.loads(STATE.read_text()) if STATE.exists() else {"min_app": "1.0.0", "app": None}
    out = ROOT / "packaging" / "release" / version
    out.mkdir(parents=True, exist_ok=True)
    zname = f"juniper-code-{version}.zip"
    with zipfile.ZipFile(out / zname, "w", zipfile.ZIP_DEFLATED) as z:
        for f in CODE_FILES:
            z.write(ROOT / f, f)
        z.writestr("version.txt", version + "\n")
    blob = (out / zname).read_bytes()
    assets = [zname, "feed.json", "feed.sig"]
    if kind == "app":
        dmg = ROOT / "packaging" / "dist" / "Juniper.dmg"
        dname = f"Juniper-{version}.dmg"
        (out / dname).write_bytes(dmg.read_bytes())
        state["app"] = {"version": version, "url": asset_url(version, dname), "notes": notes}
        state["min_app"] = version           # code from now on may rely on what this app ships
        assets.append(dname)
    feed = {"code": {"version": version, "min_app": state["min_app"], "url": asset_url(version, zname),
                     "sha256": hashlib.sha256(blob).hexdigest(), "notes": notes},
            "app": state["app"]}
    body = json.dumps(feed, indent=1).encode()
    secret = bytes.fromhex(KEY.read_text().strip())
    sig = ed25519.sign(secret, body)
    (out / "feed.json").write_bytes(body)
    (out / "feed.sig").write_text(base64.b64encode(sig).decode() + "\n")
    STATE.write_text(json.dumps(state, indent=1) + "\n")
    print(f"{kind} {version}: {len(blob) // 1024} KB code, needs app {state['min_app']}, "
          f"signature checks out: {ed25519.verify(ed25519.public_key(secret), body, sig)}")
    print("\n".join(str(out / a) for a in assets))


if __name__ == "__main__":
    if len(sys.argv) < 4 or sys.argv[1] != "build" or sys.argv[2] not in ("code", "app"):
        sys.exit(__doc__)
    build(sys.argv[2], sys.argv[3], " ".join(sys.argv[4:]))
