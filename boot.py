"""Starts the packaged app: the newest code update that's been downloaded (updater.py), or else the
code that came with the app. This file is never updated over the air, so it stays the safe way in:
an update that fails to start twice in a row is set aside and the app's own code runs instead.
"""
import json
import os
import plistlib
import runpy
import sys
from pathlib import Path

RES = Path(__file__).resolve().parent          # Contents/Resources/app: code, models, helpers
DATA = Path(os.environ.get("JUNIPER_HOME") or Path.home() / "Library" / "Application Support" / "Juniper")
os.environ["JUNIPER_RES"] = str(RES)


def vt(v):
    return tuple(int(x) for x in str(v).split(".") if x.isdigit()) or (0,)


def pick():
    current = DATA / "code" / "current.json"
    try:
        info = json.loads(current.read_text())
    except (OSError, ValueError):
        return RES
    folder = DATA / "code" / str(info.get("version"))
    bundled = (RES / "version.txt").read_text().strip()
    try:
        app_version = plistlib.loads((RES.parent.parent / "Info.plist").read_bytes())["CFBundleShortVersionString"]
    except (OSError, KeyError, ValueError):
        app_version = "0"
    if (info.get("bad") or not (folder / "juniper.py").exists() or vt(info["version"]) <= vt(bundled)
            or vt(info.get("min_app", "0")) > vt(app_version)):
        return RES
    if not info.get("ok"):
        if info.get("tries", 0) >= 2:                  # it never got as far as "ready": give up on it
            info["bad"] = True
            current.write_text(json.dumps(info))
            return RES
        info["tries"] = info.get("tries", 0) + 1
        current.write_text(json.dumps(info))
    return folder


def run(folder):
    sys.path.insert(0, str(folder))
    sys.argv[0] = str(folder / "juniper.py")
    runpy.run_path(str(folder / "juniper.py"), run_name="__main__")


code = pick()
try:
    run(code)
except Exception as e:
    if code == RES:
        raise
    # The update broke on the way up (an import or start-up error): set it aside and carry on with
    # the app's own code in this same launch. (Hard crashes are caught by the tries count instead.)
    import traceback
    traceback.print_exc()
    print(f"Juniper: update {code.name} failed to start ({e!r}); using the app's own code", file=sys.stderr)
    current = DATA / "code" / "current.json"
    info = json.loads(current.read_text())
    info["bad"] = True
    current.write_text(json.dumps(info))
    for name in [m for m, mod in list(sys.modules.items()) if str(getattr(mod, "__file__", "")).startswith(str(code))]:
        del sys.modules[name]
    sys.path.remove(str(code))
    run(RES)
