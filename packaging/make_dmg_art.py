"""The DMG window's background: drag-to-Applications arrow and the first-open instructions.
   python packaging/make_dmg_art.py <out.png> [notarized]   (660×420 points, drawn at 2x)"""
import sys

import AppKit

W, H, K = 660, 420, 2
img = AppKit.NSImage.alloc().initWithSize_((W * K, H * K))
img.lockFocus()
AppKit.NSGraphicsContext.currentContext().setImageInterpolation_(AppKit.NSImageInterpolationHigh)
xf = AppKit.NSAffineTransform.transform()
xf.scaleBy_(K)
xf.concat()
# Light and cool: Finder draws icon labels in black on a custom background, so it can't be dark.
bg = AppKit.NSGradient.alloc().initWithStartingColor_endingColor_(
    AppKit.NSColor.colorWithSRGBRed_green_blue_alpha_(0.965, 0.97, 0.995, 1),
    AppKit.NSColor.colorWithSRGBRed_green_blue_alpha_(0.905, 0.92, 0.975, 1))
bg.drawInRect_angle_(AppKit.NSMakeRect(0, 0, W, H), -90)
glow = AppKit.NSGradient.alloc().initWithStartingColor_endingColor_(
    AppKit.NSColor.colorWithSRGBRed_green_blue_alpha_(0.42, 0.50, 0.84, 0.16),
    AppKit.NSColor.colorWithSRGBRed_green_blue_alpha_(0.42, 0.50, 0.84, 0))
glow.drawFromCenter_radius_toCenter_radius_options_((170, 230), 0, (170, 230), 260, 0)
accent = AppKit.NSColor.colorWithSRGBRed_green_blue_alpha_(0.42, 0.50, 0.84, 1)


def text(s, x, y, size, weight, color, w=W, align=AppKit.NSTextAlignmentCenter):
    para = AppKit.NSMutableParagraphStyle.alloc().init()
    para.setAlignment_(align)
    para.setLineSpacing_(3)
    attr = AppKit.NSAttributedString.alloc().initWithString_attributes_(s, {
        AppKit.NSFontAttributeName: AppKit.NSFont.systemFontOfSize_weight_(size, weight),
        AppKit.NSForegroundColorAttributeName: color, AppKit.NSParagraphStyleAttributeName: para})
    attr.drawInRect_(AppKit.NSMakeRect(x, y, w, 200))


# Finder's y runs down from the top; AppKit's runs up. text() draws from the top of a 200-pt box,
# so a line meant to start `top` points down sits at y = H - top - 200.
text("Drag Juniper into Applications", 0, H - 44 - 200, 19, AppKit.NSFontWeightSemibold,
     AppKit.NSColor.colorWithSRGBRed_green_blue_alpha_(0.11, 0.12, 0.19, 1))
# arrow between the icons (icons sit at y=190 from the top, x=170 and x=490)
arrow = AppKit.NSBezierPath.bezierPath()
arrow.moveToPoint_((262, H - 190))
arrow.lineToPoint_((388, H - 190))
arrow.setLineWidth_(3)
arrow.setLineCapStyle_(AppKit.NSLineCapStyleRound)
accent.set()
arrow.stroke()
head = AppKit.NSBezierPath.bezierPath()
head.moveToPoint_((376, H - 180))
head.lineToPoint_((390, H - 190))
head.lineToPoint_((376, H - 200))
head.setLineWidth_(3)
head.setLineCapStyle_(AppKit.NSLineCapStyleRound)
head.setLineJoinStyle_(AppKit.NSLineJoinStyleRound)
head.stroke()
dim = AppKit.NSColor.colorWithSRGBRed_green_blue_alpha_(0.11, 0.12, 0.19, 0.6)
if "notarized" in sys.argv[2:]:      # Apple has checked it: it opens like any download
    note = "Then open Juniper from Applications. It sets itself up from there."
else:
    note = ("The first time you open it, macOS says it can't check the app.\n"
            "Open System Settings › Privacy & Security and click Open Anyway.")
text(note, 40, H - 334 - 200, 12, AppKit.NSFontWeightRegular, dim, w=W - 80)
img.unlockFocus()
rep = AppKit.NSBitmapImageRep.imageRepWithData_(img.TIFFRepresentation())
rep.setSize_((W, H))
rep.representationUsingType_properties_(AppKit.NSBitmapImageFileTypePNG, None).writeToFile_atomically_(sys.argv[1], True)
