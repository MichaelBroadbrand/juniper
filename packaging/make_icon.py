"""Draws Juniper's app icon (the three berries the app uses, on dark glass) and builds AppIcon.icns.
   python packaging/make_icon.py <out_dir>"""
import subprocess
import sys
from pathlib import Path

import AppKit

out = Path(sys.argv[1])
out.mkdir(parents=True, exist_ok=True)
S = 1024
img = AppKit.NSImage.alloc().initWithSize_((S, S))
img.lockFocus()
ctx = AppKit.NSGraphicsContext.currentContext()
ctx.setImageInterpolation_(AppKit.NSImageInterpolationHigh)
# macOS icon grid: an 824 pt rounded square centred in 1024, with a soft drop shadow.
body = AppKit.NSMakeRect(100, 100, 824, 824)
path = AppKit.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(body, 186, 186)
shadow = AppKit.NSShadow.alloc().init()
shadow.setShadowOffset_((0, -12))
shadow.setShadowBlurRadius_(28)
shadow.setShadowColor_(AppKit.NSColor.colorWithWhite_alpha_(0, 0.45))
AppKit.NSGraphicsContext.saveGraphicsState()
shadow.set()
AppKit.NSColor.blackColor().set()
path.fill()
AppKit.NSGraphicsContext.restoreGraphicsState()
grad = AppKit.NSGradient.alloc().initWithStartingColor_endingColor_(
    AppKit.NSColor.colorWithSRGBRed_green_blue_alpha_(0.16, 0.18, 0.27, 1),
    AppKit.NSColor.colorWithSRGBRed_green_blue_alpha_(0.05, 0.055, 0.08, 1))
grad.drawInBezierPath_angle_(path, -90)
# berry-blue glow behind the logo
AppKit.NSGraphicsContext.saveGraphicsState()
path.addClip()
glow = AppKit.NSGradient.alloc().initWithStartingColor_endingColor_(
    AppKit.NSColor.colorWithSRGBRed_green_blue_alpha_(0.42, 0.50, 0.84, 0.40),
    AppKit.NSColor.colorWithSRGBRed_green_blue_alpha_(0.42, 0.50, 0.84, 0.0))
glow.drawFromCenter_radius_toCenter_radius_options_((512, 500), 0, (512, 500), 420, 0)
AppKit.NSGraphicsContext.restoreGraphicsState()
# hairline edge
AppKit.NSColor.colorWithWhite_alpha_(1, 0.10).set()
path.setLineWidth_(3)
path.stroke()
# the berries, as the app draws them (ui.draw_berries), each shaded like a real juniper berry
C = lambda r, g, b, a=1: AppKit.NSColor.colorWithSRGBRed_green_blue_alpha_(r, g, b, a)
size, cx, cy = 600, 512, 500
r = size * 0.21
berry_shadow = AppKit.NSShadow.alloc().init()
berry_shadow.setShadowOffset_((0, -10))
berry_shadow.setShadowBlurRadius_(36)
berry_shadow.setShadowColor_(C(0.42, 0.5, 0.84, 0.55))
for dx, dy in [(-size * 0.25, -size * 0.17), (size * 0.25, -size * 0.17), (0, size * 0.19)]:   # back to front
    x, y = cx + dx, cy + dy
    oval = AppKit.NSBezierPath.bezierPathWithOvalInRect_(AppKit.NSMakeRect(x - r, y - r, 2 * r, 2 * r))
    AppKit.NSGraphicsContext.saveGraphicsState()
    berry_shadow.set()
    C(0.36, 0.43, 0.78).set()
    oval.fill()
    AppKit.NSGraphicsContext.restoreGraphicsState()
    body_grad = AppKit.NSGradient.alloc().initWithColors_([C(0.70, 0.76, 0.98), C(0.42, 0.50, 0.84), C(0.20, 0.25, 0.55)])
    body_grad.drawInBezierPath_relativeCenterPosition_(oval, (-0.35, 0.4))
    # the dusty "bloom" on juniper berries, and a soft highlight
    AppKit.NSColor.colorWithWhite_alpha_(1, 0.08).set()
    oval.fill()
    AppKit.NSGraphicsContext.saveGraphicsState()
    oval.addClip()
    sheen = AppKit.NSGradient.alloc().initWithStartingColor_endingColor_(
        AppKit.NSColor.colorWithWhite_alpha_(1, 0.5), AppKit.NSColor.colorWithWhite_alpha_(1, 0))
    hx, hy = x - r * 0.38, y + r * 0.36
    sheen.drawFromCenter_radius_toCenter_radius_options_((hx, hy), 0, (hx, hy), r * 0.42, 0)
    AppKit.NSGraphicsContext.restoreGraphicsState()
img.unlockFocus()

rep = AppKit.NSBitmapImageRep.imageRepWithData_(img.TIFFRepresentation())
png = out / "icon-1024.png"
rep.representationUsingType_properties_(AppKit.NSBitmapImageFileTypePNG, None).writeToFile_atomically_(str(png), True)
iconset = out / "AppIcon.iconset"
iconset.mkdir(exist_ok=True)
for size in (16, 32, 128, 256, 512):
    for scale in (1, 2):
        px = size * scale
        name = f"icon_{size}x{size}{'@2x' if scale == 2 else ''}.png"
        subprocess.run(["sips", "-z", str(px), str(px), str(png), "--out", str(iconset / name)], capture_output=True, check=True)
subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(out / "AppIcon.icns")], check=True)
print(out / "AppIcon.icns")
