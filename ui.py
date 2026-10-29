"""Menu bar icon, floating overlay and global keys for Juniper.

Everything here runs on the main thread (AppKit's rule). The listening and Claude threads never
touch the UI directly; they update app.state, and a 10-per-second timer here redraws from it.

The global keys use Carbon's RegisterEventHotKey, which needs no Accessibility or Input
Monitoring permission (unlike reading the keyboard directly).
"""
import ctypes
import math
import os
import threading
import re
import subprocess
import time
from ctypes import CFUNCTYPE, POINTER, Structure, byref, c_int32, c_uint32, c_ulong, c_void_p

import AppKit
import Foundation
import objc
import Quartz
import rumps

from juniper import (APP_VERSION, CODE_VERSION, DATA, restart, set_state, EFFORTS, HERE, LOCAL_COMMANDS, LOG_PATH, MODELS, MODES, key_hint, log, mode_label, model_label,
                        open_in_terminal, tool_part)

ACCENT = AppKit.NSColor.colorWithSRGBRed_green_blue_alpha_(0x6B / 255, 0x7F / 255, 0xD7 / 255, 1.0)   # juniper berry
THINK_FRAMES = 12          # the berries pulse one after another while it's thinking


def think_frame(now):
    return int(now * 10) % THINK_FRAMES


def draw_berries(cx, cy, size, color, frame=None, shine=True):
    """The logo: three juniper berries in a little triangle, centred on (cx, cy), `size` points
    across. With a frame number (see think_frame) they pulse in turn, for "thinking"."""
    r = size * 0.21
    for i, (dx, dy) in enumerate([(0, size * 0.19), (-size * 0.25, -size * 0.17), (size * 0.25, -size * 0.17)]):
        k, scale = 1.0, 1.0
        if frame is not None:
            wave = (1 + math.cos(2 * math.pi * (frame / THINK_FRAMES - i / 3))) / 2
            k, scale = 0.35 + 0.65 * wave ** 2, 0.82 + 0.18 * wave
        rr, x, y = r * scale, cx + dx, cy + dy
        color.colorWithAlphaComponent_(color.alphaComponent() * k).set()
        AppKit.NSBezierPath.bezierPathWithOvalInRect_(AppKit.NSMakeRect(x - rr, y - rr, 2 * rr, 2 * rr)).fill()
        if shine:          # a soft highlight, like light on a berry
            AppKit.NSColor.colorWithWhite_alpha_(1, 0.32 * k * color.alphaComponent()).set()
            h = rr * 0.34
            AppKit.NSBezierPath.bezierPathWithOvalInRect_(
                AppKit.NSMakeRect(x - rr * 0.38 - h, y + rr * 0.32 - h, 2 * h, 2 * h)).fill()


# -- global keys ------------------------------------------------------------------------------

_carbon = ctypes.CDLL("/System/Library/Frameworks/Carbon.framework/Carbon")


def _fourcc(s):
    return int.from_bytes(s.encode("mac_roman"), "big")


class _EventTypeSpec(Structure):
    _fields_ = [("eventClass", c_uint32), ("eventKind", c_uint32)]


class _HotKeyID(Structure):
    _fields_ = [("signature", c_uint32), ("id", c_uint32)]


_Handler = CFUNCTYPE(c_int32, c_void_p, c_void_p, c_void_p)
_carbon.GetApplicationEventTarget.restype = c_void_p
_carbon.InstallEventHandler.argtypes = [c_void_p, _Handler, c_ulong, POINTER(_EventTypeSpec), c_void_p, c_void_p]
_carbon.InstallEventHandler.restype = c_int32
_carbon.RegisterEventHotKey.argtypes = [c_uint32, c_uint32, _HotKeyID, c_void_p, c_uint32, POINTER(c_void_p)]
_carbon.RegisterEventHotKey.restype = c_int32
_carbon.GetEventKind.argtypes = [c_void_p]
_carbon.GetEventKind.restype = c_uint32
_carbon.GetEventParameter.argtypes = [c_void_p, c_uint32, c_uint32, c_void_p, c_ulong, c_void_p, c_void_p]
_carbon.GetEventParameter.restype = c_int32

_PRESSED, _RELEASED = 5, 6
_MODIFIERS = {"cmd": 0x100, "command": 0x100, "shift": 0x200, "option": 0x800, "alt": 0x800,
              "ctrl": 0x1000, "control": 0x1000}
_KEYS = {**{c: i for c, i in zip("asdfhgzxcv", [0, 1, 2, 3, 4, 5, 6, 7, 8, 9])},
         "b": 11, "q": 12, "w": 13, "e": 14, "r": 15, "y": 16, "t": 17, "1": 18, "2": 19, "3": 20, "4": 21,
         "6": 22, "5": 23, "9": 25, "7": 26, "8": 28, "0": 29, "o": 31, "u": 32, "i": 34, "p": 35, "l": 37,
         "j": 38, "k": 40, "n": 45, "m": 46, "space": 49, "return": 36, "tab": 48, "escape": 53,
         "f1": 122, "f2": 120, "f3": 99, "f4": 118, "f5": 96, "f6": 97, "f7": 98, "f8": 100, "f9": 101,
         "f10": 109, "f11": 103, "f12": 111}


class HotKeys:
    def __init__(self):
        self.actions = {}
        self.refs = []
        self.handler = _Handler(self._on_event)     # kept referenced so it isn't garbage collected
        specs = (_EventTypeSpec * 2)((_fourcc("keyb"), _PRESSED), (_fourcc("keyb"), _RELEASED))
        _carbon.InstallEventHandler(_carbon.GetApplicationEventTarget(), self.handler, 2, specs, None, None)

    def add(self, spec, on_down, on_up=None):
        *mods, key = spec.lower().split("+")
        if key not in _KEYS or any(m not in _MODIFIERS for m in mods):
            log("error", f"don't understand the key \"{spec}\" in config.toml", "red")
            return False
        hid = _HotKeyID(_fourcc("hyCl"), len(self.actions) + 1)
        ref = c_void_p()
        err = _carbon.RegisterEventHotKey(_KEYS[key], sum(_MODIFIERS[m] for m in mods), hid,
                                          _carbon.GetApplicationEventTarget(), 0, byref(ref))
        if err:
            log("error", f"couldn't claim {key_hint(spec)} (another app uses it?); pick another in config.toml", "red")
            return False
        self.actions[hid.id] = (on_down, on_up)
        self.refs.append(ref)
        return True

    def _on_event(self, _call, event, _data):
        hid = _HotKeyID()
        _carbon.GetEventParameter(event, _fourcc("----"), _fourcc("hkid"), None, ctypes.sizeof(hid), None, byref(hid))
        down, up = self.actions.get(hid.id, (None, None))
        fn = down if _carbon.GetEventKind(event) == _PRESSED else up
        try:
            if fn:
                fn()
        except Exception as e:
            log("error", f"key handler: {e!r}", "red")
        return 0


# -- overlay ----------------------------------------------------------------------------------

CIRCLE = 48          # the corner logo; the overlay's ✕ sits exactly where its centre was
CORNER = 22          # the overlay's corner radius
HEAD_C = CIRCLE / 2  # header row centre, from the top edge (and the ✕'s centre from its side edge)


def _label(size, weight=AppKit.NSFontWeightRegular, color=None):
    tf = AppKit.NSTextField.labelWithString_("")
    tf.setFont_(AppKit.NSFont.systemFontOfSize_weight_(size, weight))
    tf.setTextColor_(color or AppKit.NSColor.labelColor())
    tf.setLineBreakMode_(AppKit.NSLineBreakByTruncatingTail)
    return tf


def _clean(text):
    """Claude is asked not to use markdown, but strip the common bits if it does."""
    text = re.sub(r"\*\*(.+?)\*\*|__(.+?)__", lambda m: m.group(1) or m.group(2), text)
    text = re.sub(r"`([^`\n]+)`", r"\1", text)
    return re.sub(r"(?m)^\s*#{1,6}\s+", "", text).strip()


class _Bars(AppKit.NSView):
    """Five rounded bars: your voice level while listening, a gentle wave while Claude speaks."""

    def initWithFrame_(self, frame):
        self = objc.super(_Bars, self).initWithFrame_(frame)
        if self is None:
            return None
        self.levels = [0.0] * 5
        self.color = ACCENT
        return self

    @objc.python_method
    def show(self, levels, color):
        self.levels, self.color = levels, color
        self.setNeedsDisplay_(True)

    def drawRect_(self, rect):
        b = self.bounds()
        w, gap = 3.0, 3.0
        x0 = b.size.width - (len(self.levels) * (w + gap) - gap)
        self.color.set()
        for i, lv in enumerate(self.levels):
            h = max(3.0, min(1.0, lv) * b.size.height)
            r = AppKit.NSMakeRect(x0 + i * (w + gap), (b.size.height - h) / 2, w, h)
            AppKit.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(r, w / 2, w / 2).fill()


class _Flipped(AppKit.NSView):
    """Lays out from the top down, so messages stack like a chat."""

    def isFlipped(self):
        return True


class _Glyph(AppKit.NSView):
    """The berry logo centred in its view: show("logo"), show("think:3") for a frame of the
    thinking pulse, or show("+") (files dragged over it), drawn centred on the character's shape."""

    def initWithFrame_(self, frame):
        self = objc.super(_Glyph, self).initWithFrame_(frame)
        if self is None:
            return None
        self.text, self.color, self.size = "logo", ACCENT, 27.0
        return self

    @objc.python_method
    def show(self, text, color=None):
        if text != self.text or (color is not None and color != self.color):
            self.text, self.color = text, color or self.color
            self.setNeedsDisplay_(True)

    def drawRect_(self, rect):
        b = self.bounds()
        if self.text == "logo" or self.text.startswith("think:"):
            frame = int(self.text[6:]) if self.text.startswith("think:") else None
            draw_berries(b.size.width / 2, b.size.height / 2, self.size, self.color, frame)
            return
        attr = AppKit.NSAttributedString.alloc().initWithString_attributes_(self.text, {
            AppKit.NSFontAttributeName: AppKit.NSFont.systemFontOfSize_weight_(self.size, AppKit.NSFontWeightBold),
            AppKit.NSForegroundColorAttributeName: self.color})
        ink = attr.boundingRectWithSize_options_((1000, 1000), AppKit.NSStringDrawingUsesDeviceMetrics)
        x = b.size.width / 2 - (ink.origin.x + ink.size.width / 2)
        y = b.size.height / 2 - (ink.origin.y + ink.size.height / 2)
        attr.drawWithRect_options_(AppKit.NSMakeRect(x, y, 0, 0), AppKit.NSStringDrawingUsesDeviceMetrics)


class _KeyPanel(AppKit.NSPanel):
    """A borderless panel that can still take typing (borderless windows normally can't), without
    switching you away from the app you're in, like Spotlight."""

    def canBecomeKeyWindow(self):
        return True


def _floating_panel(cls, w, h):
    style = AppKit.NSWindowStyleMaskBorderless | AppKit.NSWindowStyleMaskNonactivatingPanel
    p = cls.alloc().initWithContentRect_styleMask_backing_defer_(
        AppKit.NSMakeRect(0, 0, w, h), style, AppKit.NSBackingStoreBuffered, False)
    p.setFloatingPanel_(True)
    # Above normal, floating and full-screen windows (setFloatingPanel_ resets the level, so set it after).
    p.setLevel_(AppKit.NSPopUpMenuWindowLevel)
    p.setOpaque_(False)
    p.setBackgroundColor_(AppKit.NSColor.clearColor())
    p.setHasShadow_(True)
    p.setHidesOnDeactivate_(False)
    p.setCanHide_(False)
    p.setBecomesKeyOnlyIfNeeded_(True)
    p.setCollectionBehavior_(AppKit.NSWindowCollectionBehaviorCanJoinAllSpaces
                             | AppKit.NSWindowCollectionBehaviorFullScreenAuxiliary
                             | AppKit.NSWindowCollectionBehaviorStationary
                             | AppKit.NSWindowCollectionBehaviorIgnoresCycle)
    p.setAppearance_(AppKit.NSAppearance.appearanceNamed_(AppKit.NSAppearanceNameVibrantDark))
    return p


def _blur(rect, radius):
    v = AppKit.NSVisualEffectView.alloc().initWithFrame_(rect)
    v.setMaterial_(AppKit.NSVisualEffectMaterialHUDWindow)
    v.setBlendingMode_(AppKit.NSVisualEffectBlendingModeBehindWindow)
    v.setState_(AppKit.NSVisualEffectStateActive)
    v.setWantsLayer_(True)
    v.layer().setCornerRadius_(radius)
    v.layer().setMasksToBounds_(True)
    v.layer().setBorderWidth_(1)
    v.layer().setBorderColor_(AppKit.NSColor.colorWithWhite_alpha_(1, 0.10).CGColor())
    return v


def _file_paths(pasteboard):
    urls = pasteboard.readObjectsForClasses_options_(
        [AppKit.NSURL], {AppKit.NSPasteboardURLReadingFileURLsOnlyKey: True}) or []
    return [u.path() for u in urls]


# -- hover feedback ---------------------------------------------------------------------------

_TRACK = (AppKit.NSTrackingMouseEnteredAndExited | AppKit.NSTrackingActiveAlways | AppKit.NSTrackingInVisibleRect)


class _Hover(AppKit.NSObject):
    """Animates a clickable view while the pointer is over it: icons grow a little ("lift"),
    chips, rows and buttons brighten ("glow"), and the pointer becomes a hand."""

    def mouseEntered_(self, event):
        _hover_apply(self, True)

    def mouseExited_(self, event):
        _hover_apply(self, False)


def _scale_about_centre(layer, size, s):
    ax, ay = layer.anchorPoint().x * size.width, layer.anchorPoint().y * size.height
    px, py = size.width / 2, size.height / 2
    return Quartz.CATransform3DConcat(Quartz.CATransform3DMakeScale(s, s, 1),
                                      Quartz.CATransform3DMakeTranslation((px - ax) * (1 - s), (py - ay) * (1 - s), 0))


def _hover_apply(h, on):
    view = h.view
    layer = view.layer()
    if layer is None:
        return
    (AppKit.NSCursor.pointingHandCursor() if on else AppKit.NSCursor.arrowCursor()).set()
    if h.kind == "lift":
        presented = layer.presentationLayer()
        anim = Quartz.CABasicAnimation.animationWithKeyPath_("transform")
        anim.setFromValue_(Foundation.NSValue.valueWithCATransform3D_(
            presented.transform() if presented else Quartz.CATransform3DIdentity))
        anim.setToValue_(Foundation.NSValue.valueWithCATransform3D_(
            _scale_about_centre(layer, view.bounds().size, h.scale if on else 1.0)))
        anim.setDuration_(0.14)
        anim.setTimingFunction_(Quartz.CAMediaTimingFunction.functionWithName_(Quartz.kCAMediaTimingFunctionEaseOut))
        anim.setFillMode_(Quartz.kCAFillModeForwards)
        anim.setRemovedOnCompletion_(False)
        layer.addAnimation_forKey_(anim, "hover")
    else:
        base = h.base
        target = base
        if on:
            target = (AppKit.NSColor.colorWithCGColor_(base).blendedColorWithFraction_ofColor_(0.22, AppKit.NSColor.whiteColor())
                      .colorWithAlphaComponent_(max(0.14, AppKit.NSColor.colorWithCGColor_(base).alphaComponent() + 0.08)).CGColor()
                      if base else AppKit.NSColor.colorWithWhite_alpha_(1, 0.09).CGColor())
        anim = Quartz.CABasicAnimation.animationWithKeyPath_("backgroundColor")
        anim.setFromValue_(layer.backgroundColor())
        anim.setToValue_(target)
        anim.setDuration_(0.14)
        layer.addAnimation_forKey_(anim, "hover")
        layer.setBackgroundColor_(target)


def hover(view, kind, keep, scale=1.14):
    """Give a clickable view hover feedback. `keep` holds the tracker (tracking areas don't)."""
    view.setWantsLayer_(True)
    h = _Hover.alloc().init()
    h.view, h.kind, h.scale = view, kind, scale
    h.base = view.layer().backgroundColor()
    view.addTrackingArea_(AppKit.NSTrackingArea.alloc().initWithRect_options_owner_userInfo_(view.bounds(), _TRACK, h, None))
    keep.append(h)
    return view


class _DropZone(AppKit.NSView):
    """Shown while you drag files over the overlay: a dashed outline saying where they'll go."""

    def initWithFrame_(self, frame):
        self = objc.super(_DropZone, self).initWithFrame_(frame)
        if self is None:
            return None
        self.setWantsLayer_(True)
        # Dims what's behind (so the message reads cleanly) with a cool berry tint.
        self.layer().setBackgroundColor_(AppKit.NSColor.colorWithSRGBRed_green_blue_alpha_(0.10, 0.11, 0.17, 0.93).CGColor())
        self.layer().setCornerRadius_(CORNER - 4)
        self.dash = Quartz.CAShapeLayer.layer()
        self.dash.setFillColor_(None)
        self.dash.setStrokeColor_(ACCENT.CGColor())
        self.dash.setLineWidth_(2)
        self.dash.setLineDashPattern_([7, 5])
        self.layer().addSublayer_(self.dash)
        self.icon = AppKit.NSImageView.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, 34, 34))
        self.icon.setImage_(_symbol_image("paperclip.circle.fill", 30))
        self.icon.setContentTintColor_(ACCENT)
        self.text = _label(15, AppKit.NSFontWeightSemibold)
        self.text.setAlignment_(AppKit.NSTextAlignmentCenter)
        self.addSubview_(self.icon)
        self.addSubview_(self.text)
        return self

    @objc.python_method
    def show(self, text):
        b = self.bounds()
        self.dash.setFrame_(b)
        self.dash.setPath_(Quartz.CGPathCreateWithRoundedRect(AppKit.NSInsetRect(b, 1, 1), CORNER - 5, CORNER - 5, None))
        self.text.setStringValue_(text)
        self.text.setFrame_(AppKit.NSMakeRect(10, b.size.height / 2 - 26, b.size.width - 20, 20))
        self.icon.setFrame_(AppKit.NSMakeRect(b.size.width / 2 - 17, b.size.height / 2 + 2, 34, 34))


class _Mover:
    """Drags a window with the mouse; tells a click from a drag (moved more than 3 points)."""

    def start(self, window):
        self.window, self.mouse0, self.origin0 = window, AppKit.NSEvent.mouseLocation(), window.frame().origin
        self.moved = False

    def drag(self):
        p = AppKit.NSEvent.mouseLocation()
        dx, dy = p.x - self.mouse0.x, p.y - self.mouse0.y
        if not self.moved and math.hypot(dx, dy) < 3:
            return False
        self.moved = True
        self.window.setFrameOrigin_((self.origin0.x + dx, self.origin0.y + dy))
        return True


class _DropView(AppKit.NSView):
    """Accepts files dragged onto it (the overlay and the logo)."""

    def initWithFrame_(self, frame):
        self = objc.super(_DropView, self).initWithFrame_(frame)
        if self is None:
            return None
        self.registerForDraggedTypes_([AppKit.NSPasteboardTypeFileURL])
        return self

    def draggingEntered_(self, sender):
        n = len(_file_paths(sender.draggingPasteboard()))
        if _ui and n:
            _ui.drag_hover(True, isinstance(self, _BadgeView), n)
        return AppKit.NSDragOperationCopy if n else AppKit.NSDragOperationNone

    def draggingExited_(self, sender):
        if _ui:
            _ui.drag_hover(False, isinstance(self, _BadgeView))

    def performDragOperation_(self, sender):
        paths = _file_paths(sender.draggingPasteboard())
        if _ui:
            _ui.drag_hover(False, isinstance(self, _BadgeView))
        if paths and _ui:
            _ui.files_dropped(paths, on_logo=isinstance(self, _BadgeView))
        return bool(paths)


class _ContentView(AppKit.NSView):
    """The overlay's contents. Dragging its header row moves the overlay (and the logo with it)."""

    def acceptsFirstMouse_(self, event):
        return True

    def mouseDown_(self, event):
        self.window().makeKeyWindow()
        y = self.convertPoint_fromView_(event.locationInWindow(), None).y
        self.mover = _Mover() if y > self.bounds().size.height - CIRCLE else None
        if self.mover:
            self.mover.start(self.window())

    def mouseDragged_(self, event):
        if getattr(self, "mover", None) and self.mover.drag() and _ui:
            _ui.overlay_dragging()

    def mouseUp_(self, event):
        if getattr(self, "mover", None) and self.mover.moved and _ui:
            _ui.overlay_dropped()
        self.mover = None


class _Input(AppKit.NSObject):
    """The input bar's buttons and keys."""

    def send_(self, sender):
        if _ui:
            _ui.send_or_stop()

    def attach_(self, sender):
        if _ui:
            _ui.pick_attachments()

    def record_(self, sender):
        if _ui:
            threading.Thread(target=_ui.app.record_toggle, daemon=True).start()

    def clearAttachments_(self, sender):
        if _ui:
            _ui.app.clear_attachments()

    def close_(self, sender):
        if _ui:
            _ui.close_overlay()

    def textView_doCommandBySelector_(self, textview, selector):
        sel = str(selector)
        o = _ui.overlay if _ui else None
        if o and o.slash_items:                             # the "/" list is open
            if sel in ("moveUp:", "moveDown:"):
                o.move_slash(-1 if sel == "moveUp:" else 1)
                return True
            if sel == "insertTab:" or (sel == "insertNewline:" and o.text().strip() != o.slash_items[o.slash_sel][0]):
                o.complete_slash()
                return True
            if sel == "cancelOperation:":
                o.slash_dismissed = o.text()
                return True
        if sel == "insertBacktab:" and _ui:                 # Shift-Tab: next mode, like Claude Code
            _ui.app.cycle_mode()
            return True
        if sel == "insertNewline:":
            if AppKit.NSApp.currentEvent().modifierFlags() & AppKit.NSEventModifierFlagShift:
                return False                                # Shift-Return: a new line
            if _ui:
                _ui.send_or_stop()                          # Return: send
            return True
        if sel == "cancelOperation:" and _ui:               # Esc: fold the overlay away
            _ui.close_overlay()
            return True
        return False


class _Picker(AppKit.NSObject):
    """The "which session?" menu for files dropped on the logo."""

    def pick_(self, sender):
        if _ui:
            _ui.attach_to_session(sender.representedObject())


_ui = None          # the MenuBar, for the view classes above


def _symbol_image(name, size):
    if name == "berries":          # the logo, as a template image that tints like the SF Symbols
        def draw(rect):
            draw_berries(size * 0.6, size * 0.6, size * 1.1, AppKit.NSColor.blackColor(), shine=False)
            return True
        img = AppKit.NSImage.imageWithSize_flipped_drawingHandler_((size * 1.2, size * 1.2), False, draw)
        img.setTemplate_(True)
        return img
    cfg = AppKit.NSImageSymbolConfiguration.configurationWithPointSize_weight_(size, AppKit.NSFontWeightMedium)
    return AppKit.NSImage.imageWithSystemSymbolName_accessibilityDescription_(name, name).imageWithSymbolConfiguration_(cfg)


def _symbol_button(name, target, action, size=15, tint=None):
    b = AppKit.NSButton.buttonWithImage_target_action_(_symbol_image(name, size), target, action)
    b.setBordered_(False)
    b.setContentTintColor_(tint or AppKit.NSColor.secondaryLabelColor())
    return b


SIDEBAR = 220        # width of the sessions sidebar
MODE_STYLE = {"auto": ("sparkles", ACCENT), "manual": ("hand.raised", None), "acceptEdits": ("pencil", None),
              "plan": ("list.bullet.clipboard", None), "bypassPermissions": ("exclamationmark.shield", None)}


def _pill(title, target, action, tag, primary=False, danger=False):
    """A rounded button for approval cards and options."""
    b = AppKit.NSButton.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, 10, 26))
    b.setBordered_(False)
    b.setWantsLayer_(True)
    colour = ACCENT if primary else (AppKit.NSColor.systemRedColor() if danger else AppKit.NSColor.whiteColor())
    b.layer().setBackgroundColor_(colour.colorWithAlphaComponent_(0.9 if primary else (0.22 if danger else 0.1)).CGColor())
    b.layer().setCornerRadius_(8)
    font = AppKit.NSFont.systemFontOfSize_weight_(12.5, AppKit.NSFontWeightSemibold)
    b.setAttributedTitle_(AppKit.NSAttributedString.alloc().initWithString_attributes_(title, {
        AppKit.NSFontAttributeName: font, AppKit.NSForegroundColorAttributeName: AppKit.NSColor.whiteColor()}))
    w = AppKit.NSAttributedString.alloc().initWithString_attributes_(title, {AppKit.NSFontAttributeName: font}).size().width
    b.setFrameSize_((math.ceil(w) + 22, 26))
    b.setTarget_(target)
    b.setAction_(action)
    b.setTag_(tag)
    return b


class _Actions(AppKit.NSObject):
    """Targets for the overlay's buttons and menus (they hand over to the MenuBar)."""

    def card_(self, sender):
        if _ui:
            _ui.card_choice(sender.tag())

    def copy_(self, sender):
        if _ui:
            _ui.copy_reply(sender)

    def sidebar_(self, sender):
        if _ui:
            _ui.toggle_sidebar()

    def newSession_(self, sender):
        if _ui:
            _ui.pick_folder(None)

    def modeMenu_(self, sender):
        if _ui:
            _ui.chip_menu(sender, "mode")

    def modelMenu_(self, sender):
        if _ui:
            _ui.chip_menu(sender, "model")

    def pickMode_(self, sender):
        if _ui:
            _ui.app.set_mode(sender.representedObject())

    def pickModel_(self, sender):
        if _ui:
            _ui.app.set_model(sender.representedObject())

    def pickEffort_(self, sender):
        if _ui:
            _ui.app.set_effort(sender.representedObject())

    def pickAI_(self, sender):
        if _ui:
            cid = sender.representedObject()
            threading.Thread(target=lambda: _ui.app.answer(f"Use {cid}", _ui.app.set_ai(cid)), daemon=True).start()

    def addAI_(self, sender):
        if _ui:
            _ui.add_ai()

    def slash_(self, sender):
        if _ui:
            _ui.overlay.complete_slash(sender.tag())


class _Sizer(AppKit.NSObject):
    def size_(self, slider):
        if _ui:
            _ui.set_logo_size(slider.doubleValue())


class _ChipView(AppKit.NSView):
    """The mode and model chips (a Python subclass, so it can hold its icon and label)."""


class _Row(AppKit.NSView):
    """A session in the sidebar: click to switch to it."""

    def isFlipped(self):
        return True

    def acceptsFirstMouse_(self, event):
        return True

    def mouseDown_(self, event):
        if _ui and getattr(self, "session_name", None):
            _ui.sidebar_pick(self.session_name)


class _Wheel(AppKit.NSView):
    """How much of the conversation's context is used, as a ring. Hover for the numbers."""

    def initWithFrame_(self, frame):
        self = objc.super(_Wheel, self).initWithFrame_(frame)
        if self is None:
            return None
        self.fraction = 0.0
        self.setWantsLayer_(True)
        self.lift = _Hover.alloc().init()
        self.lift.view, self.lift.kind, self.lift.scale, self.lift.base = self, "lift", 1.25, None
        self.addTrackingArea_(AppKit.NSTrackingArea.alloc().initWithRect_options_owner_userInfo_(
            self.bounds(), AppKit.NSTrackingMouseEnteredAndExited | AppKit.NSTrackingActiveAlways
            | AppKit.NSTrackingInVisibleRect, self, None))
        return self

    @objc.python_method
    def show(self, fraction):
        if abs(fraction - self.fraction) > 0.001:
            self.fraction = fraction
            self.setNeedsDisplay_(True)

    def drawRect_(self, rect):
        b = self.bounds()
        c = AppKit.NSMakePoint(b.size.width / 2, b.size.height / 2)
        r = min(b.size.width, b.size.height) / 2 - 2
        track = AppKit.NSBezierPath.bezierPath()
        track.appendBezierPathWithArcWithCenter_radius_startAngle_endAngle_(c, r, 0, 360)
        track.setLineWidth_(2.5)
        AppKit.NSColor.colorWithWhite_alpha_(1, 0.15).set()
        track.stroke()
        if self.fraction > 0:
            f = min(1.0, self.fraction)
            colour = (AppKit.NSColor.systemGreenColor() if f < 0.5 else ACCENT if f < 0.8
                      else AppKit.NSColor.systemRedColor())
            arc = AppKit.NSBezierPath.bezierPath()
            arc.appendBezierPathWithArcWithCenter_radius_startAngle_endAngle_clockwise_(c, r, 90, 90 - 360 * f, True)
            arc.setLineWidth_(2.5)
            arc.setLineCapStyle_(AppKit.NSLineCapStyleRound)
            colour.set()
            arc.stroke()

    def acceptsFirstMouse_(self, event):
        return True

    def mouseDown_(self, event):
        if _ui:
            _ui.compact_from_wheel()

    def mouseEntered_(self, event):
        if _ui:
            _ui.overlay.hover_context(True)
        _hover_apply(self.lift, True)

    def mouseExited_(self, event):
        if _ui:
            _ui.overlay.hover_context(False)
        _hover_apply(self.lift, False)


class _ResizeHandle(AppKit.NSView):
    """An invisible strip along one of the overlay's free edges (or the corner between them): drag it
    to resize, double-click it to go back to the automatic size."""

    def acceptsFirstMouse_(self, event):
        return True

    def updateTrackingAreas(self):
        for a in list(self.trackingAreas()):
            self.removeTrackingArea_(a)
        self.addTrackingArea_(AppKit.NSTrackingArea.alloc().initWithRect_options_owner_userInfo_(
            self.bounds(), AppKit.NSTrackingMouseEnteredAndExited | AppKit.NSTrackingActiveAlways, self, None))
        objc.super(_ResizeHandle, self).updateTrackingAreas()

    @objc.python_method
    def cursor(self):
        if self.kind == "x":
            return AppKit.NSCursor.resizeLeftRightCursor()
        if self.kind == "y":
            return AppKit.NSCursor.resizeUpDownCursor()
        try:                                                # a diagonal cursor (macOS 15+)
            return AppKit.NSCursor.frameResizeCursorFromPosition_inDirections_(self.position, 3)
        except Exception:
            return AppKit.NSCursor.crosshairCursor()

    def mouseEntered_(self, event):
        self.cursor().set()

    def mouseExited_(self, event):
        AppKit.NSCursor.arrowCursor().set()

    def mouseDown_(self, event):
        self.window().makeKeyWindow()
        if event.clickCount() == 2 and _ui:
            _ui.overlay.reset_size()
            return
        if _ui:
            _ui.overlay.begin_resize()
        self.start = AppKit.NSEvent.mouseLocation()

    def mouseDragged_(self, event):
        if _ui and getattr(self, "start", None):
            p = AppKit.NSEvent.mouseLocation()
            _ui.overlay.resize_by(self.kind, p.x - self.start.x, p.y - self.start.y)

    def mouseUp_(self, event):
        if _ui and getattr(self, "start", None):
            _ui.overlay.end_resize()
        self.start = None


class Overlay:
    """The conversation panel. It opens out of the corner logo towards the middle of the screen:
    down from a logo in the top half, up from one in the bottom half; right from a logo on the
    left, left from one on the right. The header (with ✕ on the logo's side) is always on top."""

    W, PAD = 460, 18

    def __init__(self):
        self.panel = p = _floating_panel(_KeyPanel, self.W, 120)
        p.setBecomesKeyOnlyIfNeeded_(False)     # any click on it focuses it
        p.setHasShadow_(False)                  # our own shadow follows the panel exactly as it animates
        blur = _blur(AppKit.NSMakeRect(0, 0, self.W, 120), CORNER)
        root = _DropView.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, self.W, 120))
        root.setWantsLayer_(True)
        shadow = AppKit.NSView.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, self.W, 120))
        shadow.setWantsLayer_(True)
        shadow.layer().setShadowColor_(AppKit.NSColor.blackColor().CGColor())
        shadow.layer().setShadowOpacity_(0.45)
        shadow.layer().setShadowRadius_(18)
        shadow.layer().setShadowOffset_((0, -8))
        root.addSubview_(shadow)
        root.addSubview_(blur)
        p.setContentView_(root)
        self.root, self.view, self.shadow = root, blur, shadow
        self.canvas, self.geo, self.target, self.tween, self.timer = None, None, None, None, None
        self.handler = _Input.alloc().init()

        self.logo = _Glyph.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, 22, 24))
        self.logo.size = 19
        self.title = _label(14, AppKit.NSFontWeightSemibold)
        self.title.setStringValue_("Juniper")
        self.status = _label(12, AppKit.NSFontWeightMedium, AppKit.NSColor.secondaryLabelColor())
        self.session = _label(12, AppKit.NSFontWeightMedium, AppKit.NSColor.tertiaryLabelColor())
        # The spinner has its own fixed-width, centred slot so the status text never shifts as it animates.
        self.spinner = _Glyph.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, 16, 18))
        self.spinner.size = 13
        self.bars = _Bars.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, 30, 18))
        self.close = _symbol_button("xmark.circle.fill", self.handler, "close:", 15, AppKit.NSColor.tertiaryLabelColor())
        self.close.setToolTip_("Fold away (doesn't stop anything)")
        self.step = _label(11.5, color=AppKit.NSColor.tertiaryLabelColor())
        self.note = _label(11.5, AppKit.NSFontWeightMedium, ACCENT)

        inner = self.W - 2 * self.PAD
        doc = _Flipped.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, inner, 10))
        sv = AppKit.NSScrollView.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, inner, 10))
        sv.setDocumentView_(doc)
        sv.setHasVerticalScroller_(True)
        sv.setAutohidesScrollers_(True)
        sv.setScrollerStyle_(AppKit.NSScrollerStyleOverlay)
        sv.setDrawsBackground_(False)
        sv.setBorderType_(AppKit.NSNoBorder)
        self.doc, self.scroll = doc, sv
        self.text_h = 0

        # Input bar: paperclip, a text box that grows downwards as you type, send (or stop).
        bar = AppKit.NSView.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, inner, 34))
        bar.setWantsLayer_(True)
        bar.layer().setBackgroundColor_(AppKit.NSColor.colorWithWhite_alpha_(1, 0.07).CGColor())
        bar.layer().setCornerRadius_(12)
        bar.layer().setBorderWidth_(1)
        bar.layer().setBorderColor_(AppKit.NSColor.colorWithWhite_alpha_(1, 0.06).CGColor())
        clip = _symbol_button("paperclip", self.handler, "attach:", 14)
        clip.setFrame_(AppKit.NSMakeRect(8, 7, 20, 20))
        clip.setToolTip_("Attach files (or drag them here, or paste with ⌘V)")
        self.W = self.LW = self.W     # the width you chose, and the chat's actual width (see _layout_width)
        self.field_w = inner - 32 - 38 - 28
        tv = AppKit.NSTextView.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, self.field_w, 19))
        tv.setFont_(AppKit.NSFont.systemFontOfSize_(13.5))
        tv.setTextColor_(AppKit.NSColor.labelColor())
        tv.setInsertionPointColor_(ACCENT)
        tv.setDrawsBackground_(False)
        tv.setRichText_(False)
        tv.setAllowsUndo_(True)
        tv.setTextContainerInset_((0, 1))
        tv.textContainer().setLineFragmentPadding_(0)
        tv.textContainer().setWidthTracksTextView_(True)
        tv.setVerticallyResizable_(True)
        tv.setHorizontallyResizable_(False)
        tv.setDelegate_(self.handler)
        box = AppKit.NSScrollView.alloc().initWithFrame_(AppKit.NSMakeRect(32, 8, self.field_w, 19))
        box.setDocumentView_(tv)
        box.setHasVerticalScroller_(True)
        box.setAutohidesScrollers_(True)
        box.setScrollerStyle_(AppKit.NSScrollerStyleOverlay)
        box.setDrawsBackground_(False)
        box.setBorderType_(AppKit.NSNoBorder)
        self.placeholder = _label(13.5, color=AppKit.NSColor.tertiaryLabelColor())
        self.placeholder.setStringValue_("Ask Claude…  (Shift-Return for a new line)")
        self.placeholder.setFrame_(AppKit.NSMakeRect(32, 8, self.field_w, 19))
        send = _symbol_button("arrow.up.circle.fill", self.handler, "send:", 20, ACCENT)
        send.setFrame_(AppKit.NSMakeRect(inner - 30, 5, 24, 24))
        mic = _symbol_button("mic.fill", self.handler, "record:", 15, AppKit.NSColor.secondaryLabelColor())
        mic.setFrame_(AppKit.NSMakeRect(inner - 58, 5, 24, 24))
        mic.setToolTip_("Speak instead (stops when you pause; click again to send now)")
        mic.setWantsLayer_(True)
        mic.layer().setCornerRadius_(12)
        self.mic, self.mic_rec = mic, False
        for v in (clip, self.placeholder, box, mic, send):
            bar.addSubview_(v)
        self.input, self.box, self.field, self.send, self.send_mode = bar, box, tv, send, "send"
        self.field_h = 19

        self.attach_label = _label(11.5, AppKit.NSFontWeightMedium, AppKit.NSColor.secondaryLabelColor())
        self.attach_clear = _symbol_button("xmark.circle.fill", self.handler, "clearAttachments:", 11,
                                           AppKit.NSColor.tertiaryLabelColor())
        self.attach_row = AppKit.NSView.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, inner, 16))
        self.attach_label.setFrame_(AppKit.NSMakeRect(2, 0, inner - 24, 16))
        self.attach_clear.setFrame_(AppKit.NSMakeRect(inner - 18, 0, 16, 16))
        self.attach_row.addSubview_(self.attach_label)
        self.attach_row.addSubview_(self.attach_clear)

        self.actions = _Actions.alloc().init()
        # Toolbar under the input: mode, model, and the context wheel (hover for numbers).
        self.toolbar = AppKit.NSView.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, inner, 22))
        self.mode_chip = self._chip("modeMenu:")
        self.model_chip = self._chip("modelMenu:")
        self.wheel = _Wheel.alloc().initWithFrame_(AppKit.NSMakeRect(inner - 20, 2, 18, 18))
        self.ctx_pct = _label(11, AppKit.NSFontWeightMedium, AppKit.NSColor.tertiaryLabelColor())
        self.ctx_pct.setAlignment_(AppKit.NSTextAlignmentRight)
        self.ctx_pct.setFrame_(AppKit.NSMakeRect(inner - 100, 3, 76, 16))
        for v in (self.mode_chip, self.model_chip, self.ctx_pct, self.wheel):
            self.toolbar.addSubview_(v)
        self.ctx_tip = AppKit.NSView.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, 10, 10))
        self.ctx_tip.setWantsLayer_(True)
        self.ctx_tip.layer().setBackgroundColor_(AppKit.NSColor.colorWithWhite_alpha_(0.1, 0.96).CGColor())
        self.ctx_tip.layer().setCornerRadius_(9)
        self.ctx_tip.layer().setBorderWidth_(1)
        self.ctx_tip.layer().setBorderColor_(AppKit.NSColor.colorWithWhite_alpha_(1, 0.1).CGColor())
        self.ctx_tip_label = AppKit.NSTextField.wrappingLabelWithString_("")
        self.ctx_tip_label.setAlignment_(AppKit.NSTextAlignmentCenter)
        self.ctx_tip.addSubview_(self.ctx_tip_label)
        self.ctx_tip.setHidden_(True)
        self.ctx_text = ""
        # Sidebar toggle (header), the "/" command list and the approval card (above the input).
        self.side_btn = _symbol_button("sidebar.left", self.actions, "sidebar:", 15, AppKit.NSColor.secondaryLabelColor())
        self.side_btn.setToolTip_("Sessions")
        self.slash_view = _Flipped.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, inner, 10))
        self.slash_items, self.slash_sel, self.slash_key, self.slash_dismissed = [], 0, None, None
        self.card = _Flipped.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, inner, 10))
        self.card.setWantsLayer_(True)
        self.card.layer().setBackgroundColor_(ACCENT.colorWithAlphaComponent_(0.10).CGColor())
        self.card.layer().setBorderColor_(ACCENT.colorWithAlphaComponent_(0.45).CGColor())
        self.card.layer().setBorderWidth_(1)
        self.card.layer().setCornerRadius_(12)
        self.card_key, self.card_h, self.card_item, self.card_choices = None, 0, None, []
        self.copy_texts = []
        # The sessions sidebar: slides out of the side facing the middle of the screen.
        self.sidebar = _Flipped.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, SIDEBAR, 120))
        self.sidebar.setWantsLayer_(True)
        self.sidebar.layer().setBackgroundColor_(AppKit.NSColor.colorWithWhite_alpha_(1, 0.035).CGColor())
        self.sidebar.layer().setMasksToBounds_(True)
        self.sidebar_open, self.sidebar_key = False, None
        head = _label(12, AppKit.NSFontWeightSemibold, AppKit.NSColor.secondaryLabelColor())
        head.setStringValue_("Sessions")
        head.setFrame_(AppKit.NSMakeRect(16, HEAD_C - 8, 120, 16))
        self.plus = plus = _symbol_button("plus.circle.fill", self.actions, "newSession:", 19, ACCENT)
        plus.setToolTip_("New session for a folder…")
        plus.setFrame_(AppKit.NSMakeRect(SIDEBAR - 40, HEAD_C - 12, 24, 24))
        self.side_doc = _Flipped.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, SIDEBAR, 10))
        self.side_scroll = AppKit.NSScrollView.alloc().initWithFrame_(AppKit.NSMakeRect(0, CIRCLE, SIDEBAR, 10))
        self.side_scroll.setDocumentView_(self.side_doc)
        self.side_scroll.setHasVerticalScroller_(True)
        self.side_scroll.setAutohidesScrollers_(True)
        self.side_scroll.setScrollerStyle_(AppKit.NSScrollerStyleOverlay)
        self.side_scroll.setDrawsBackground_(False)
        self.side_scroll.setBorderType_(AppKit.NSNoBorder)
        for v in (head, plus, self.side_scroll):
            self.sidebar.addSubview_(v)
        blur.addSubview_(self.sidebar)

        content = _ContentView.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, self.W, 120))
        content.setWantsLayer_(True)
        blur.addSubview_(content)
        self.content = content
        for v in (self.logo, self.title, self.session, self.spinner, self.status, self.bars, self.close, sv,
                  self.step, self.note, self.attach_row, self.input, self.toolbar, self.slash_view, self.card,
                  self.side_btn, self.ctx_tip):
            content.addSubview_(v)
        for v in (self.status, self.session, self.step, self.note):
            v.setWantsLayer_(True)

        self.shown = False
        self.anchor = None            # the logo's circle (x, y, w, h); the panel opens out of it
        self.sides = ("right", "top")  # which corner of the panel is pinned to the logo
        self.badge = None
        self.morphing = False
        self.dragging = False
        self.content_key = None
        self.frame_key = None
        self.last_front = 0.0
        self.drafts = {}              # half-typed messages, per session
        self.draft_session = None
        self.user_h = None            # a height you've chosen by dragging (None: grows with the chat)
        self.resizing = False
        self.handles = {}
        for kind in ("x", "y", "xy"):
            h = _ResizeHandle.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, 8, 8))
            h.kind, h.position = kind, 0
            root.addSubview_(h)
            self.handles[kind] = h
        # Hover feedback (the trackers are kept here; tracking areas don't keep them alive).
        self.hovers = {"fixed": [], "doc": [], "card": [], "slash": [], "side": []}
        for v in (self.send, clip, self.close, self.side_btn, self.attach_clear, plus, self.mic):
            hover(v, "lift", self.hovers["fixed"], 1.18)
        for chip in (self.mode_chip, self.model_chip):
            hover(chip, "glow", self.hovers["fixed"])
        # Drop zone: covers the panel while files are dragged over it.
        self.drop_zone = _DropZone.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, self.W, 120))
        self.drop_zone.setHidden_(True)
        blur.addSubview_(self.drop_zone)

    # -- text input ------------------------------------------------------------------------

    def text(self):
        # A copy: NSTextView hands back its live, mutable string, which would change under a saved draft.
        return "" + self.field.string()

    def set_text(self, text):
        self.field.setString_(text)

    def _measure_input(self):
        """Height for the text box: one line, growing a line at a time as you type (up to 8)."""
        lm, tc = self.field.layoutManager(), self.field.textContainer()
        tc.setContainerSize_((self.field_w, 1e7))
        lm.ensureLayoutForTextContainer_(tc)
        h = lm.usedRectForTextContainer_(tc).size.height + 2
        return int(min(max(19, math.ceil(h)), 8 * 17 + 2))

    # -- toolbar, approval card, "/" list, sidebar ---------------------------------------------

    def _chip(self, action):
        """A chip: icon, label and a small chevron, all centred on one line; the whole chip is the button."""
        box = _ChipView.alloc().initWithFrame_(AppKit.NSMakeRect(0, 1, 90, 20))
        box.setWantsLayer_(True)
        box.layer().setCornerRadius_(7)
        box.layer().setBackgroundColor_(AppKit.NSColor.colorWithWhite_alpha_(1, 0.07).CGColor())
        box.icon = AppKit.NSImageView.alloc().initWithFrame_(AppKit.NSMakeRect(7, 4, 12, 12))
        box.text = _label(11.5, AppKit.NSFontWeightMedium)
        box.chevron = AppKit.NSImageView.alloc().initWithFrame_(AppKit.NSMakeRect(0, 6, 8, 8))
        box.chevron.setImage_(_symbol_image("chevron.down", 7))
        box.button = AppKit.NSButton.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, 90, 20))
        box.button.setBordered_(False)
        box.button.setTitle_("")
        box.button.setTransparent_(True)
        box.button.setTarget_(self.actions)
        box.button.setAction_(action)
        for v in (box.icon, box.text, box.chevron, box.button):
            box.addSubview_(v)
        return box

    def _set_chip(self, chip, symbol, title, colour, x):
        chip.icon.setImage_(_symbol_image(symbol, 10))
        chip.icon.setContentTintColor_(colour)
        chip.chevron.setContentTintColor_(colour)
        chip.text.setStringValue_(title)
        chip.text.setTextColor_(colour)
        chip.text.sizeToFit()
        tw, th = chip.text.frame().size.width, chip.text.frame().size.height
        chip.text.setFrame_(AppKit.NSMakeRect(23, (20 - th) / 2, tw, th))
        chip.chevron.setFrame_(AppKit.NSMakeRect(23 + tw + 5, 6, 8, 8))
        w = 23 + tw + 5 + 8 + 8
        chip.setFrame_(AppKit.NSMakeRect(x, 1, w, 20))
        chip.button.setFrame_(AppKit.NSMakeRect(0, 0, w, 20))
        return x + w + 6

    def hover_context(self, on):
        """The context wheel's hover box: tokens used and left, and that clicking compacts. Centred both ways."""
        self.ctx_tip.setHidden_(not on or not self.ctx_text)
        if on and self.ctx_text:
            label = self.ctx_tip_label
            size = label.cell().cellSizeForBounds_(AppKit.NSMakeRect(0, 0, 300, 100))
            w, h = math.ceil(size.width) + 2, math.ceil(size.height)
            bw, bh = w + 24, h + 14
            f = self.toolbar.frame()
            self.ctx_tip.setFrame_(AppKit.NSMakeRect(f.origin.x + f.size.width - bw, f.origin.y + f.size.height + 6, bw, bh))
            label.setFrame_(AppKit.NSMakeRect((bw - w) / 2, (bh - h) / 2, w, h))

    def _toolbar(self, mode, model, ctx):
        icon, colour = MODE_STYLE.get(mode, ("hand.raised", None))
        colour = colour or (AppKit.NSColor.systemRedColor() if mode == "bypassPermissions"
                            else AppKit.NSColor.secondaryLabelColor())
        x = self._set_chip(self.mode_chip, icon, mode_label(mode), colour, 0)
        self._set_chip(self.model_chip, "cpu", model_label(model), AppKit.NSColor.secondaryLabelColor(), x)
        used, window = ctx
        frac = used / window if used and window else 0.0
        self.wheel.show(frac)
        self.ctx_pct.setStringValue_(f"{round(100 * (1 - frac))}% left" if used and window else "")
        busy = bool(_ui and _ui.app.runner.busy())
        def tokens(n):
            return f"{n / 1e6:.1f}".rstrip("0").rstrip(".") + "M" if n >= 1e6 else f"{n / 1000:.0f}k"
        first = (f"{tokens(used)} of {tokens(window)} tokens used · {100 * (1 - frac):.0f}% left"
                 if used and window else "Nothing in the context yet")
        second = "Click to compact once Claude has finished" if busy else "Click to compact the conversation"
        self.ctx_text = first + "\n" + second
        if self.ctx_tip_label.stringValue() != self.ctx_text:
            para = AppKit.NSMutableParagraphStyle.alloc().init()
            para.setAlignment_(AppKit.NSTextAlignmentCenter)
            para.setLineSpacing_(2)
            text = AppKit.NSMutableAttributedString.alloc().initWithString_attributes_(first + "\n", {
                AppKit.NSFontAttributeName: AppKit.NSFont.systemFontOfSize_weight_(11.5, AppKit.NSFontWeightSemibold),
                AppKit.NSForegroundColorAttributeName: AppKit.NSColor.labelColor(), AppKit.NSParagraphStyleAttributeName: para})
            text.appendAttributedString_(AppKit.NSAttributedString.alloc().initWithString_attributes_(second, {
                AppKit.NSFontAttributeName: AppKit.NSFont.systemFontOfSize_(11),
                AppKit.NSForegroundColorAttributeName: ACCENT, AppKit.NSParagraphStyleAttributeName: para}))
            self.ctx_tip_label.setAttributedStringValue_(text)

    def _card(self, item, width):
        """Claude is waiting on you: allow / deny, approve a plan, or answer its question."""
        key = (item["id"], len(item.get("answers") or {})) if item else None
        if key == self.card_key:
            return self.card_h
        self.card_key, self.card_item = key, item
        self.hovers["card"] = []
        for v in list(self.card.subviews()):
            v.removeFromSuperview()
        if not item:
            self.card_h, self.card_choices = 0, []
            return 0
        pad, w = 12, width - 24
        tool, inp = item["tool_name"], item["input"]
        detail = ""
        if tool == "AskUserQuestion":
            q = (inp.get("questions") or [{}])[0]
            title = q.get("question", "Claude has a question")
            choices = [(o.get("label", ""), o.get("label", ""), "primary" if i == 0 else "") for i, o in enumerate(q.get("options") or [])]
            detail = "\n".join(f"{o.get('label', '')}: {o.get('description', '')}" for o in q.get("options") or [] if o.get("description"))
        elif tool == "ExitPlanMode":
            title = "Go ahead with this plan? (It's in the conversation above.)"
            choices = [("Yes, auto-accept edits", "plan_edits", "primary"), ("Yes, ask before edits", "plan_ask", ""),
                       ("Keep planning", "plan_keep", "")]
        else:
            if tool == "Bash":
                title, detail = "Run this command?", (inp.get("command") or "")
            elif tool in ("Edit", "MultiEdit", "Write", "NotebookEdit"):
                part = tool_part({"name": tool, "input": inp}, "")
                title = f"Make this change to {os.path.basename(str(inp.get('file_path') or inp.get('notebook_path') or 'a file'))}?"
                detail = "\n".join(f"{k} {l}" for k, l in part["diff"]) + (f"\n… {part['more']} more lines" if part["more"] else "")
            else:
                title, detail = f"Allow {tool}?", json.dumps(inp, ensure_ascii=False)[:400]
            choices = [("Allow", "allow", "primary"), ("Allow for this session", "allow_session", ""), ("Deny", "deny", "danger")]
        self.card_choices = [c[1] for c in choices]
        y = pad
        head = self._text(title, 13, AppKit.NSFontWeightSemibold, AppKit.NSColor.labelColor())
        _, h = self._measure(head, w)
        head.setFrame_(AppKit.NSMakeRect(pad, y, w, h))
        self.card.addSubview_(head)
        y += h + 8
        if detail:
            mono = AppKit.NSFont.monospacedSystemFontOfSize_weight_(11.5, AppKit.NSFontWeightRegular)
            body = AppKit.NSTextField.wrappingLabelWithString_("\n".join(detail.split("\n")[:10]))
            body.setFont_(mono if tool != "AskUserQuestion" else AppKit.NSFont.systemFontOfSize_(12))
            body.setTextColor_(AppKit.NSColor.secondaryLabelColor())
            body.setSelectable_(True)
            _, bh = self._measure(body, w - 16)
            box = AppKit.NSView.alloc().initWithFrame_(AppKit.NSMakeRect(pad, y, w, bh + 12))
            box.setWantsLayer_(True)
            box.layer().setBackgroundColor_(AppKit.NSColor.colorWithWhite_alpha_(0, 0.25).CGColor())
            box.layer().setCornerRadius_(7)
            body.setFrame_(AppKit.NSMakeRect(8, 6, w - 16, bh))
            box.addSubview_(body)
            self.card.addSubview_(box)
            y += bh + 12 + 10
        x = pad
        for i, (label, _, style) in enumerate(choices):
            b = hover(_pill(label, self.actions, "card:", i, primary=style == "primary", danger=style == "danger"),
                      "glow", self.hovers["card"])
            bw = b.frame().size.width
            if x + bw > pad + w:
                x, y = pad, y + 32
            b.setFrameOrigin_((x, y))
            self.card.addSubview_(b)
            x += bw + 8
        y += 26 + 8
        hint = _label(11, color=AppKit.NSColor.tertiaryLabelColor())
        hint.setStringValue_("Or just say it, or type a reply below.")
        hint.setFrame_(AppKit.NSMakeRect(pad, y, w, 15))
        self.card.addSubview_(hint)
        self.card_h = y + 15 + pad
        return self.card_h

    def slash_candidates(self, commands, skills):
        """What to offer while you type a "/" command."""
        t = self.text()
        if not t.startswith("/") or " " in t or "\n" in t or t == self.slash_dismissed:
            return []
        q = t[1:].lower()
        local = list(LOCAL_COMMANDS)
        names = {c for c, _ in local}
        allc = local + [("/" + c, "skill" if c in skills else "Claude Code") for c in commands if "/" + c not in names]
        first = [c for c in allc if c[0][1:].lower().startswith(q)]
        rest = [c for c in allc if q and q in c[0].lower() and c not in first]
        return (first + rest)[:6]

    def _slash(self, items, width):
        if [c[0] for c in items] != [c[0] for c in self.slash_items]:
            self.slash_sel = 0
        self.slash_items = items
        self.slash_sel = min(self.slash_sel, max(0, len(items) - 1))
        key = (tuple(c[0] for c in items), self.slash_sel)
        if key != self.slash_key:
            self.slash_key = key
            self.hovers["slash"] = []
            for v in list(self.slash_view.subviews()):
                v.removeFromSuperview()
            mono = AppKit.NSFont.monospacedSystemFontOfSize_weight_(12, AppKit.NSFontWeightMedium)
            for i, (name, desc) in enumerate(items):
                b = AppKit.NSButton.alloc().initWithFrame_(AppKit.NSMakeRect(0, i * 24, width, 22))
                b.setBordered_(False)
                b.setAlignment_(AppKit.NSTextAlignmentLeft)
                title = AppKit.NSMutableAttributedString.alloc().initWithString_attributes_(f"  {name}", {
                    AppKit.NSFontAttributeName: mono, AppKit.NSForegroundColorAttributeName: AppKit.NSColor.labelColor()})
                title.appendAttributedString_(AppKit.NSAttributedString.alloc().initWithString_attributes_(f"   {desc}", {
                    AppKit.NSFontAttributeName: AppKit.NSFont.systemFontOfSize_(11.5),
                    AppKit.NSForegroundColorAttributeName: AppKit.NSColor.tertiaryLabelColor()}))
                b.setAttributedTitle_(title)
                b.setTarget_(self.actions)
                b.setAction_("slash:")
                b.setTag_(i)
                b.setWantsLayer_(True)
                b.layer().setCornerRadius_(6)
                if i == self.slash_sel:
                    b.layer().setBackgroundColor_(AppKit.NSColor.colorWithWhite_alpha_(1, 0.1).CGColor())
                hover(b, "glow", self.hovers["slash"])
                self.slash_view.addSubview_(b)
        return len(items) * 24

    def move_slash(self, step):
        if self.slash_items:
            self.slash_sel = (self.slash_sel + step) % len(self.slash_items)

    def complete_slash(self, i=None):
        if self.slash_items:
            name = self.slash_items[self.slash_sel if i is None else i][0]
            self.set_text(name + " ")
            self.field.setSelectedRange_((len(name) + 1, 0))

    @staticmethod
    def sidebar_height(n):
        """How tall the overlay should be (at least) to show n sessions in the sidebar."""
        return CIRCLE + n * 50 + 10

    def _sidebar(self, rows, height):
        """One row per session: name, folder, and whether it's working, waiting on you, or has a reply.
        The list scrolls when there are more than fit."""
        self.side_scroll.setFrame_(AppKit.NSMakeRect(0, CIRCLE, SIDEBAR, max(0, height - CIRCLE)))
        key = tuple(rows)
        if key == self.sidebar_key:
            return
        self.sidebar_key = key
        self.hovers["side"] = []
        for v in list(self.side_doc.subviews()):
            v.removeFromSuperview()
        colours = {"working": ACCENT, "needs you": AppKit.NSColor.systemYellowColor(),
                   "new reply": AppKit.NSColor.systemRedColor()}
        y = 0
        for name, folder, status, current in rows:
            row = _Row.alloc().initWithFrame_(AppKit.NSMakeRect(8, y, SIDEBAR - 16, 46))
            row.session_name = name
            row.setWantsLayer_(True)
            row.layer().setCornerRadius_(10)
            if current:
                row.layer().setBackgroundColor_(ACCENT.colorWithAlphaComponent_(0.18).CGColor())
            n = _label(13, AppKit.NSFontWeightSemibold)
            n.setStringValue_(name)
            n.setFrame_(AppKit.NSMakeRect(10, 6, SIDEBAR - 36, 18))
            f = _label(11, color=AppKit.NSColor.tertiaryLabelColor())
            f.setStringValue_(folder)
            if status:
                text = AppKit.NSMutableAttributedString.alloc().initWithString_attributes_(f"{folder}  ·  ", {
                    AppKit.NSFontAttributeName: AppKit.NSFont.systemFontOfSize_(11),
                    AppKit.NSForegroundColorAttributeName: AppKit.NSColor.tertiaryLabelColor()})
                text.appendAttributedString_(AppKit.NSAttributedString.alloc().initWithString_attributes_(status, {
                    AppKit.NSFontAttributeName: AppKit.NSFont.systemFontOfSize_weight_(11, AppKit.NSFontWeightMedium),
                    AppKit.NSForegroundColorAttributeName: colours.get(status, AppKit.NSColor.secondaryLabelColor())}))
                f.setAttributedStringValue_(text)
            f.setFrame_(AppKit.NSMakeRect(10, 25, SIDEBAR - 36, 15))
            row.addSubview_(n)
            row.addSubview_(f)
            hover(row, "glow", self.hovers["side"])
            self.side_doc.addSubview_(row)
            y += 50
        self.side_doc.setFrame_(AppKit.NSMakeRect(0, 0, SIDEBAR, y + 4))

    # -- messages ---------------------------------------------------------------------------

    @staticmethod
    def _text(text, size, weight, color, spacing=2.0):
        tf = AppKit.NSTextField.wrappingLabelWithString_("")
        para = AppKit.NSMutableParagraphStyle.alloc().init()
        para.setLineSpacing_(spacing)
        tf.setAttributedStringValue_(AppKit.NSAttributedString.alloc().initWithString_attributes_(text, {
            AppKit.NSFontAttributeName: AppKit.NSFont.systemFontOfSize_weight_(size, weight),
            AppKit.NSForegroundColorAttributeName: color, AppKit.NSParagraphStyleAttributeName: para}))
        tf.setDrawsBackground_(False)
        tf.setSelectable_(True)          # so you can copy from it
        # Without this, clicking the text swaps in a plain editor that drops the styling (it shrinks).
        tf.setAllowsEditingTextAttributes_(True)
        return tf

    @staticmethod
    def _measure(tf, max_w):
        size = tf.cell().cellSizeForBounds_(AppKit.NSMakeRect(0, 0, max_w, 1e5))
        return math.ceil(size.width), math.ceil(size.height)

    def _messages(self, turns, width, queued=()):
        """Your words in berry-blue bubbles on the right; Claude's replies on the left, marked with the berries.
        Earlier exchanges are faded; requests waiting their turn come last, pale and marked
        "Queued". Returns the total height."""
        for v in list(self.doc.subviews()):
            v.removeFromSuperview()
        self.copy_texts = []
        self.hovers["doc"] = []
        y = 0.0
        for i, t in enumerate(turns):
            current = i == len(turns) - 1
            fade = 1.0 if current else 0.5
            if i:
                y += 16
            if t["you"]:
                tf = self._text(t["you"], 13.5, AppKit.NSFontWeightMedium,
                                AppKit.NSColor.whiteColor().colorWithAlphaComponent_(0.97 if current else 0.75))
                w, h = self._measure(tf, width * 0.8 - 24)
                bubble = AppKit.NSView.alloc().initWithFrame_(AppKit.NSMakeRect(width - w - 24, y, w + 24, h + 14))
                bubble.setWantsLayer_(True)
                bubble.layer().setBackgroundColor_(ACCENT.colorWithAlphaComponent_(0.92 if current else 0.4).CGColor())
                bubble.layer().setCornerRadius_(min(15, (h + 14) / 2))
                tf.setFrame_(AppKit.NSMakeRect(12, 7, w, h))
                bubble.addSubview_(tf)
                self.doc.addSubview_(bubble)
                y += h + 14
            parts = t.get("parts") or ([{"t": "text", "v": t["claude"]}] if t["claude"] else [])
            for j, part in enumerate(parts):
                y += 10 if (j or t["you"]) else 0
                if part["t"] == "tool":
                    y += self._tool_row(part, y, width, fade)
                    continue
                mark = _Glyph.alloc().initWithFrame_(AppKit.NSMakeRect(0, y + 1, 16, 18))
                mark.size, mark.color = 12, ACCENT.colorWithAlphaComponent_(fade)
                tf = self._text(_clean(part["v"]), 15 if current else 13.5, AppKit.NSFontWeightRegular,
                                AppKit.NSColor.labelColor().colorWithAlphaComponent_(fade), 3.0)
                _, h = self._measure(tf, width - 22)
                tf.setFrame_(AppKit.NSMakeRect(22, y, width - 22, h))
                self.doc.addSubview_(mark)
                self.doc.addSubview_(tf)
                y += h
            texts = [p["v"] for p in parts if p["t"] == "text"]
            if texts:
                # Copy the whole reply (select any part of it and ⌘C, or right-click, for just that bit).
                self.copy_texts.append("\n\n".join(_clean(x) for x in texts))
                b = _symbol_button("doc.on.doc", self.actions, "copy:", 11, AppKit.NSColor.tertiaryLabelColor())
                b.setTag_(len(self.copy_texts) - 1)
                b.setToolTip_("Copy reply")
                b.setFrame_(AppKit.NSMakeRect(22, y + 4, 18, 18))
                hover(b, "lift", self.hovers["doc"], 1.2)
                self.doc.addSubview_(b)
                y += 24
        y += 6                                   # breathing room under the last reply's copy button
        for q in queued:
            y += 12
            tf = self._text(q, 13.5, AppKit.NSFontWeightMedium, AppKit.NSColor.whiteColor().colorWithAlphaComponent_(0.85))
            w, h = self._measure(tf, width * 0.8 - 24)
            bubble = AppKit.NSView.alloc().initWithFrame_(AppKit.NSMakeRect(width - w - 24, y, w + 24, h + 14))
            bubble.setWantsLayer_(True)
            bubble.layer().setBackgroundColor_(ACCENT.colorWithAlphaComponent_(0.28).CGColor())
            bubble.layer().setBorderColor_(ACCENT.colorWithAlphaComponent_(0.55).CGColor())
            bubble.layer().setBorderWidth_(1)
            bubble.layer().setCornerRadius_(min(15, (h + 14) / 2))
            tf.setFrame_(AppKit.NSMakeRect(12, 7, w, h))
            bubble.addSubview_(tf)
            self.doc.addSubview_(bubble)
            y += h + 14
            tag = _label(10.5, AppKit.NSFontWeightSemibold, AppKit.NSColor.tertiaryLabelColor())
            tag.setStringValue_("Queued")
            tag.setAlignment_(AppKit.NSTextAlignmentRight)
            tag.setFrame_(AppKit.NSMakeRect(width - 120, y + 2, 116, 14))
            self.doc.addSubview_(tag)
            y += 16
        self.doc.setFrame_(AppKit.NSMakeRect(0, 0, width, y))
        return y

    def _tool_row(self, part, y, width, fade):
        """A step Claude took, like Claude Code shows it: "⏺ Bash  npm test" or "⏺ Edit  app.ts  +3 −1",
        then for file changes a few lines of the diff. Shown, never spoken. Returns its height."""
        mono = AppKit.NSFont.monospacedSystemFontOfSize_weight_(11.5, AppKit.NSFontWeightRegular)
        dot_colour = {None: ACCENT, True: AppKit.NSColor.systemGreenColor(),
                      False: AppKit.NSColor.systemRedColor()}[part.get("ok")]
        green, red = AppKit.NSColor.systemGreenColor(), AppKit.NSColor.systemRedColor()
        line = AppKit.NSMutableAttributedString.alloc().init()

        def add(text, colour, font):
            line.appendAttributedString_(AppKit.NSAttributedString.alloc().initWithString_attributes_(text, {
                AppKit.NSFontAttributeName: font,
                AppKit.NSForegroundColorAttributeName: colour.colorWithAlphaComponent_(fade)}))
        add("⏺ ", dot_colour, AppKit.NSFont.systemFontOfSize_(10))
        add(part["name"], AppKit.NSColor.labelColor(), AppKit.NSFont.systemFontOfSize_weight_(12.5, AppKit.NSFontWeightSemibold))
        if part.get("arg"):
            add("  " + part["arg"], AppKit.NSColor.secondaryLabelColor(), mono)
        if part.get("add"):
            add(f"  +{part['add']}", green, mono)
        if part.get("del"):
            add(f" −{part['del']}", red, mono)
        head = AppKit.NSTextField.labelWithAttributedString_(line)
        head.setLineBreakMode_(AppKit.NSLineBreakByTruncatingTail)
        head.setSelectable_(True)
        head.setAllowsEditingTextAttributes_(True)
        head.setFrame_(AppKit.NSMakeRect(2, y, width - 2, 18))
        self.doc.addSubview_(head)
        h = 18
        if part.get("diff"):
            diff = AppKit.NSMutableAttributedString.alloc().init()
            for i, (kind, text) in enumerate(part["diff"]):
                diff.appendAttributedString_(AppKit.NSAttributedString.alloc().initWithString_attributes_(
                    ("\n" if i else "") + f"{kind} {text}", {
                        AppKit.NSFontAttributeName: mono,
                        AppKit.NSForegroundColorAttributeName: (green if kind == "+" else red).colorWithAlphaComponent_(0.85 * fade)}))
            if part.get("more"):
                diff.appendAttributedString_(AppKit.NSAttributedString.alloc().initWithString_attributes_(
                    f"\n… {part['more']} more line{'s' * (part['more'] > 1)}", {
                        AppKit.NSFontAttributeName: mono,
                        AppKit.NSForegroundColorAttributeName: AppKit.NSColor.tertiaryLabelColor()}))
            body = AppKit.NSTextField.labelWithAttributedString_(diff)
            body.setLineBreakMode_(AppKit.NSLineBreakByTruncatingTail)
            body.setSelectable_(True)
            body.setAllowsEditingTextAttributes_(True)
            body.setWantsLayer_(True)
            body.layer().setBackgroundColor_(AppKit.NSColor.colorWithWhite_alpha_(1, 0.04).CGColor())
            body.layer().setCornerRadius_(6)
            bh = math.ceil(body.cell().cellSizeForBounds_(AppKit.NSMakeRect(0, 0, width - 22, 1e5)).height)
            body.setFrame_(AppKit.NSMakeRect(18, y + h + 3, width - 22, bh))
            self.doc.addSubview_(body)
            h += 3 + bh
        return h

    # -- layout -----------------------------------------------------------------------------

    def _frames(self, total, view_h, bottom, busy, session):
        """Where everything sits in a panel `total` points tall (origin bottom-left)."""
        pad, inner, W = self.PAD, self.LW - 2 * self.PAD, self.LW
        c = total - HEAD_C                                   # the header row's centre line
        left = self.sides[0] == "left"
        # ✕ on the logo's side, its centre exactly where the logo's centre is when the panel opens
        # downward out of it.
        close_x = HEAD_C - 9 if left else W - HEAD_C - 9
        lx = HEAD_C + 9 + 12 if left else pad              # the logo and "Juniper" start after the ✕ on the left
        bars_x = W - pad - 30 if left else W - HEAD_C - 9 - 10 - 30
        sw = 0
        if session:
            self.session.sizeToFit()
            sw = min(self.session.frame().size.width, 130)
        sx = lx + 82 + (sw + 8 if session else 0)
        status_x = sx + (20 if busy else 0)
        f = {"logo": (lx - 1, c - 12, 22, 24), "title": (lx + 24, c - 9, 60, 18),
             "session": (lx + 76, c - 8, sw, 16), "spinner": (sx - 2, c - 9, 16, 18),
             "status": (status_x, c - 8, max(40, min(170, bars_x - 32 - status_x - 6)), 16),
             "bars": (bars_x, c - 9, 30, 18), "close": (close_x, c - 9, 18, 18),
             "side_btn": (bars_x - 32, c - 12, 24, 24),
             "scroll": (pad, total - CIRCLE - view_h, inner, view_h)}
        y = pad
        for name, h in bottom:              # stacked upwards from the bottom edge
            f[name] = (pad, y, inner, h)
            y += h + 6 if h else 0
        return f

    def _crossfade(self, label, text):
        """Change a label's text with a quick cross-fade (instant while hidden)."""
        if label.stringValue() == text:
            return
        if self.shown:
            t = Quartz.CATransition.animation()
            t.setType_(Quartz.kCATransitionFade)
            t.setDuration_(0.22)
            label.layer().addAnimation_forKey_(t, "fade")
        label.setStringValue_(text)

    def _sides_for(self, anchor):
        """Open towards the middle of the screen from wherever the logo is."""
        vf = AppKit.NSScreen.mainScreen().visibleFrame()
        cx, cy = anchor[0] + anchor[2] / 2, anchor[1] + anchor[3] / 2
        return ("left" if cx < vf.origin.x + vf.size.width / 2 else "right",
                "top" if cy > vf.origin.y + vf.size.height / 2 else "bottom")

    def render(self, status, status_color, turns, step, busy=False, session="", note="", files=(), can_stop=False,
               mode="auto", model="", ctx=(0, 0), approval=None, slash=(), rows=(), queued=()):
        # Half-typed messages stay with their session.
        if session != self.draft_session:
            if self.draft_session is not None:
                self.drafts[self.draft_session] = self.text()
            self.set_text(self.drafts.get(session, ""))
            self.draft_session = session
        self._crossfade(self.session, f"· {session}" if session else "")
        self._crossfade(self.status, status)
        self.status.setTextColor_(status_color)
        self._crossfade(self.step, step)
        self._crossfade(self.note, note)
        self.placeholder.setHidden_(bool(self.text()))
        who = _ui.app.ai_name() if _ui else "Claude"
        self.placeholder.setStringValue_(f"Reply to {who}…" if approval else f"Ask {who}…  (/ for commands, Shift-Return for a new line)")
        self._toolbar(mode, model, ctx)
        names = ", ".join(os.path.basename(f) for f in files)
        self.attach_label.setStringValue_(f"📎 {names}" if files else "")
        # Send turns into stop while Claude is working or talking (and nothing new is typed).
        mode = "stop" if can_stop and not self.text().strip() else "send"
        if mode != self.send_mode:
            self.send_mode = mode
            name, tint = (("stop.circle.fill", AppKit.NSColor.systemRedColor()) if mode == "stop"
                          else ("arrow.up.circle.fill", ACCENT))
            self.send.setImage_(_symbol_image(name, 20))
            self.send.setContentTintColor_(tint)
            self.send.setToolTip_("Stop" if mode == "stop" else "Send")
        field_h = self._measure_input()
        if field_h != self.field_h:
            self.field_h = field_h
            self.box.setFrame_(AppKit.NSMakeRect(32, 8, self.field_w, field_h))
            self.field.scrollRangeToVisible_((len(self.text()), 0))

        lw = max(320, min(self.W, self._room_width() - (SIDEBAR if self.sidebar_open else 0)))
        self._layout_width(lw)                # the chat gives way to the sidebar rather than leave the screen
        key = repr(([(t["you"], t["claude"], t.get("parts")) for t in turns], queued))
        changed = key != self.content_key
        inner = self.LW - 2 * self.PAD
        if changed:
            self.content_key = key
            self.text_h = self._messages(turns, inner, queued)
        if not (self.shown or self.morphing) and self.anchor:
            self.sides = self._sides_for(self.anchor)      # chosen when it opens; kept while it's open
        vf = AppKit.NSScreen.mainScreen().visibleFrame()
        card_h = self._card(approval, inner)
        slash_h = self._slash(list(slash), inner)
        bottom = [("toolbar", 22), ("input", field_h + 15), ("slash_view", slash_h), ("card", card_h),
                  ("attach_row", 16 if files else 0), ("note", 16 if note else 0), ("step", 16 if step else 0)]
        stack = sum(h + 6 for _, h in bottom if h) - 6
        fixed = CIRCLE + 12 + stack + self.PAD               # everything but the conversation
        biggest = max(self._max_height(), fixed)
        want = fixed + min(self.text_h, biggest - fixed)
        if self.sidebar_open:                                # tall enough for the session list (it scrolls beyond)
            want = max(want, self.sidebar_height(len(rows)))
        if self.user_h:                                      # you've set a height by dragging an edge
            want = max(self.user_h, fixed + 60)
        total = min(want, biggest)
        view_h = max(0, total - fixed)                       # the conversation takes any spare height
        fkey = (total, view_h, tuple(h for _, h in bottom), bool(step), busy, session, bool(note), bool(files),
                self.anchor, self.sides, self.sidebar_open)
        if fkey != self.frame_key and not (self.morphing or self.dragging):
            self.frame_key = fkey
            frames = self._frames(total, view_h, bottom, busy, session)
            self.session.setHidden_(not session)
            self.spinner.setHidden_(not busy)
            self.scroll.setHidden_(not view_h)
            self.step.setHidden_(not step)
            self.note.setHidden_(not note)
            self.attach_row.setHidden_(not files)
            self.slash_view.setHidden_(not slash_h)
            self.card.setHidden_(not card_h)
            right = self.sides[0] == "right"
            # The sidebar sits beside the chat on the side facing the middle of the screen. Closed, it
            # waits just outside the panel's edge, so opening reveals it like a drawer.
            content_rect = (SIDEBAR if self.sidebar_open and right else 0, 0, self.LW, total)
            side_rect = ((0 if self.sidebar_open else -SIDEBAR) if right else self.LW, 0, SIDEBAR, total)
            self.side_btn.setImage_(_symbol_image("sidebar.left" if right else "sidebar.right", 15))
            self.side_btn.setContentTintColor_(ACCENT if self.sidebar_open else AppKit.NSColor.secondaryLabelColor())
            self._sidebar(list(rows), total)
            if not self.shown:
                self._ensure_canvas()           # the window only moves while the panel is hidden
            x, y, w, h = self._panel_rect(total, self.LW + (SIDEBAR if self.sidebar_open else 0))
            self.target = {"rect": (x - self.canvas[0], y - self.canvas[1], w, h),
                           "content": tuple(content_rect), "side": tuple(side_rect),
                           "frames": {k: tuple(v) for k, v in frames.items()}, "radius": CORNER, "alpha": 1.0}
            if self.shown and self.panel.isVisible() and self.geo and not self.resizing:
                self._tween(self.target)
            else:
                self.tween = None
                self._apply(self.target)
        if changed or view_h != getattr(self, "last_view_h", None):
            # Keep the latest in view, also when the chat area shrinks (e.g. the text box grows).
            self.last_view_h = view_h
            self.doc.scrollPoint_((0, max(0, self.text_h - view_h)))

    def _limits(self):
        """The band the panel lives in: between the menu bar and the Dock (the usable screen)."""
        vf = AppKit.NSScreen.mainScreen().visibleFrame()
        return vf, vf.origin.y + 6, vf.origin.y + vf.size.height - 6

    def _max_height(self):
        vf, lower, upper = self._limits()
        if self.anchor and self.sides[1] == "top":
            # Opening downwards: room from the logo down to the floor, or, if the logo is too low
            # for that to be useful, the whole band (the panel then rises above the logo).
            room = self.anchor[1] + self.anchor[3] - lower
            return room if room >= 260 else upper - lower
        return upper - lower

    # -- geometry and animation ---------------------------------------------------------------
    #
    # The window is a fixed, transparent canvas around where the panel can be; it never resizes
    # while anything animates. Every animation is driven here, frame by frame: each tick works out
    # where every piece goes (background, shadow, chat, sidebar, header items) and sets them all at
    # once with the system's own animations off. One clock, so nothing can drift apart.
    #
    # A "geo" says where everything is: the panel's rect in canvas coordinates, the chat and the
    # sidebar relative to the panel, every subview's frame, the corner radius, and the opacity.

    M = 40                          # canvas margin around the panel (room for the shadow)

    def _panel_rect(self, total, width=None):
        width = width or self.LW
        vf, lower, upper = self._limits()
        if self.anchor:
            ax, ay, aw, ah = self.anchor
            x = ax if self.sides[0] == "left" else ax + aw - width
            y = ay + ah - total if self.sides[1] == "top" else ay
        else:
            x, y = vf.origin.x + vf.size.width - width - 12, upper - 4 - total
        # Growing up into the menu bar? Grow downwards from there instead. Never below the floor.
        y = max(min(y, upper - total), lower)
        x = min(max(x, vf.origin.x + 6), vf.origin.x + vf.size.width - width - 6)
        return (x, y, width, total)

    def _canvas_rect(self):
        """The whole usable screen (clicks go straight through its transparent parts)."""
        vf = AppKit.NSScreen.mainScreen().visibleFrame()
        return (vf.origin.x, vf.origin.y, vf.size.width, vf.size.height)

    def _ensure_canvas(self):
        c = self._canvas_rect()
        if c == self.canvas:
            return
        old = self.canvas
        self.canvas = c
        self.panel.setFrame_display_(AppKit.NSMakeRect(*c), False)
        self.root.setFrame_(AppKit.NSMakeRect(0, 0, c[2], c[3]))
        if old and self.geo:                     # keep the panel where it is on screen
            g = dict(self.geo)
            x, y, w, h = g["rect"]
            g["rect"] = (x + old[0] - c[0], y + old[1] - c[1], w, h)
            self._apply(g)

    def _place_handles(self, x, y, w, h):
        """The resize strips sit just inside the edges away from the logo's corner."""
        t = 10
        left, top = self.sides[0] == "right", self.sides[1] == "top"   # which edges are free
        hx = x if left else x + w - t
        vy = y if top else y + h - t
        self.handles["x"].setFrame_(AppKit.NSMakeRect(hx, y + CORNER, t, h - 2 * CORNER))
        self.handles["y"].setFrame_(AppKit.NSMakeRect(x + CORNER, vy, w - 2 * CORNER, t))
        self.handles["xy"].setFrame_(AppKit.NSMakeRect(x if left else x + w - 16, y if top else y + h - 16, 16, 16))
        # Which corner the diagonal cursor points from (NSCursorFrameResizePosition).
        self.handles["xy"].position = {(True, True): 6, (False, True): 4, (True, False): 8, (False, False): 2}[(left, top)]

    def begin_resize(self):
        self.resizing = True
        self.resize_from = (self.LW, self.geo["rect"][3] if self.geo else 400)

    def resize_by(self, kind, dx, dy):
        """Dragging an edge: wider/narrower and taller/shorter, within the usable screen."""
        w0, h0 = self.resize_from
        vf, lower, upper = self._limits()
        if kind in ("x", "xy"):
            w = w0 - dx if self.sides[0] == "right" else w0 + dx
            self.set_width(min(max(380, w), self._room_width() - (SIDEBAR if self.sidebar_open else 0)))
        if kind in ("y", "xy"):
            h = h0 - dy if self.sides[1] == "top" else h0 + dy
            self.user_h = min(max(200, h), upper - lower)
        self.frame_key = None
        if _ui:
            _ui.tick(None)                     # redraw now (no animation while you drag)

    def end_resize(self):
        self.resizing = False
        if _ui:
            _ui.app.set_overlay_size(self.W, self.user_h)

    def reset_size(self):
        self.set_width(Overlay.W)
        self.user_h = None
        self.frame_key = None
        if _ui:
            _ui.app.set_overlay_size(None, None)

    def set_width(self, w):
        """The width you've chosen for the chat (it may be narrower when there isn't room)."""
        self.W = int(w)
        self.frame_key = None

    def _room_width(self):
        """How wide the overlay can be: from the logo's corner to the far edge of the usable screen."""
        vf = AppKit.NSScreen.mainScreen().visibleFrame()
        if not self.anchor:
            return vf.size.width - 18
        ax, ay, aw, ah = self.anchor
        if self.sides[0] == "right":
            return ax + aw - vf.origin.x - 6
        return vf.origin.x + vf.size.width - ax - 6

    def _layout_width(self, w):
        """Lay out everything that depends on the chat's width."""
        w = int(w)
        if w == self.LW:
            return
        self.LW = w
        inner = w - 2 * self.PAD
        self.field_w = inner - 32 - 38 - 28
        self.box.setFrame_(AppKit.NSMakeRect(32, 8, self.field_w, self.field_h))
        self.placeholder.setFrame_(AppKit.NSMakeRect(32, 8, self.field_w, 19))
        self.mic.setFrame_(AppKit.NSMakeRect(inner - 58, 5, 24, 24))
        self.send.setFrame_(AppKit.NSMakeRect(inner - 30, 5, 24, 24))
        self.attach_label.setFrame_(AppKit.NSMakeRect(2, 0, inner - 24, 16))
        self.attach_clear.setFrame_(AppKit.NSMakeRect(inner - 18, 0, 16, 16))
        self.wheel.setFrame_(AppKit.NSMakeRect(inner - 20, 2, 18, 18))
        self.ctx_pct.setFrame_(AppKit.NSMakeRect(inner - 100, 3, 76, 16))
        self.content_key = self.card_key = self.slash_key = None      # re-flow the conversation and cards

    def _apply(self, g):
        """Put everything exactly where a geo says, in one go, with implicit animations off."""
        Quartz.CATransaction.begin()
        Quartz.CATransaction.setDisableActions_(True)
        x, y, w, h = g["rect"]
        r = AppKit.NSMakeRect(x, y, w, h)
        self.view.setFrame_(r)
        self.shadow.setFrame_(r)
        self._place_handles(x, y, w, h)
        radius = g["radius"]
        self.view.layer().setCornerRadius_(radius)
        self.shadow.layer().setShadowPath_(Quartz.CGPathCreateWithRoundedRect(AppKit.NSMakeRect(0, 0, w, h), radius, radius, None))
        self.content.setFrame_(AppKit.NSMakeRect(*g["content"]))
        self.sidebar.setFrame_(AppKit.NSMakeRect(*g["side"]))
        for name, f in g["frames"].items():
            getattr(self, name).setFrame_(AppKit.NSMakeRect(*f))
        self.content.setAlphaValue_(g["alpha"])
        self.sidebar.setAlphaValue_(g["alpha"])
        Quartz.CATransaction.commit()
        self.geo = g

    @staticmethod
    def _lerp(a, b, e):
        return tuple(p + (q - p) * e for p, q in zip(a, b))

    def _blend(self, a, b, e, alpha):
        frames = {k: (self._lerp(a["frames"][k], v, e) if k in a["frames"] else v) for k, v in b["frames"].items()}
        return {"rect": self._lerp(a["rect"], b["rect"], e), "content": self._lerp(a["content"], b["content"], e),
                "side": self._lerp(a["side"], b["side"], e), "frames": frames,
                "radius": a["radius"] + (b["radius"] - a["radius"]) * e, "alpha": alpha}

    EASE = {
        "out": lambda t: 1 - (1 - t) ** 3,
        "smooth": lambda t: 1 - (1 - t) ** 4,      # quick start, long gentle settle, never past the end
        "inout": lambda t: 4 * t ** 3 if t < 0.5 else 1 - (-2 * t + 2) ** 3 / 2,
    }

    def _tween(self, target, duration=0.32, ease="out", alpha=None, done=None):
        """Animate from wherever things are now to `target`, driving every frame ourselves."""
        self.tween = {"from": self.geo or target, "to": target, "t0": time.time(), "dur": duration,
                      "ease": self.EASE[ease], "alpha": alpha, "done": done}
        if self.timer is None:
            self.timer = AppKit.NSTimer.timerWithTimeInterval_repeats_block_(1 / 120, True, self._step)
            AppKit.NSRunLoop.currentRunLoop().addTimer_forMode_(self.timer, AppKit.NSRunLoopCommonModes)

    def _step(self, timer):
        tw = self.tween
        if tw is None:
            timer.invalidate()
            self.timer = None
            return
        t = min(1.0, (time.time() - tw["t0"]) / tw["dur"])
        a = tw["alpha"](t) if tw["alpha"] else tw["from"]["alpha"] + (tw["to"]["alpha"] - tw["from"]["alpha"]) * t
        self._apply(self._blend(tw["from"], tw["to"], tw["ease"](t), a))
        if t >= 1.0:
            self.tween = None
            timer.invalidate()
            self.timer = None
            if tw["done"]:
                tw["done"]()

    def _shifted(self, g, rect, radius, alpha):
        """The same layout with the panel's background moved/resized to `rect`, the chat and sidebar
        staying put on screen (so the background reveals or covers them)."""
        dx, dy = g["rect"][0] - rect[0], g["rect"][1] - rect[1]
        c, s = g["content"], g["side"]
        return {**g, "rect": rect, "radius": radius, "alpha": alpha,
                "content": (c[0] + dx, c[1] + dy, c[2], c[3]), "side": (s[0] + dx, s[1] + dy, s[2], s[3])}

    def screen_rect(self):
        """The panel on screen right now."""
        x, y, w, h = self.geo["rect"]
        return (self.canvas[0] + x, self.canvas[1] + y, w, h)

    # -- showing and hiding -----------------------------------------------------------------

    def _circle(self):
        ax, ay, aw, ah = self.anchor
        return (ax - self.canvas[0], ay - self.canvas[1], aw, ah)

    def grow_from_logo(self):
        """The logo's circle expands into the panel towards the middle of the screen: the corners relax
        from a circle to the panel's, the chat stays still while the background opens over it, and the
        contents fade in as it grows."""
        self.shown, self.morphing = True, True
        target = self.target
        start = self._shifted(target, self._circle(), self.anchor[2] / 2, 0.0)
        self.view.layer().removeAllAnimations()
        self.shadow.layer().removeAllAnimations()
        self._apply(start)
        self.panel.setAlphaValue_(1.0)
        self.panel.orderFrontRegardless()
        self.last_front = time.time()
        if self.badge:
            self.badge.panel.setAlphaValue_(0.0)          # the logo becomes the panel

        def done():
            self.morphing = False
            self.frame_key = None             # catch up on anything that changed while growing
        self._tween(target, 0.4, "smooth", alpha=lambda t: min(1.0, t ** 1.5 * 1.3), done=done)

    def shrink_to_logo(self):
        """The reverse: the contents fade, the panel folds back into the circle, then the logo is back."""
        self.shown, self.morphing = False, True
        badge = self.badge
        end = self._shifted(self.geo, self._circle(), self.anchor[2] / 2, 0.0)

        def done():
            if self.shown:
                return
            if badge:
                badge.panel.setAlphaValue_(1.0)
            self.panel.orderOut_(None)
            self.morphing = False
            self.frame_key = None
            if self.target:
                self._apply(self.target)
        self._tween(end, 0.3, "inout", alpha=lambda t: max(0.0, 1 - t * 3), done=done)

    def _scaled(self, layer, scale, lift):
        """A transform that shrinks a layer towards its pinned edge (top or bottom centre)."""
        b = self.view.bounds()
        ax, ay = layer.anchorPoint().x * b.size.width, layer.anchorPoint().y * b.size.height
        top = self.sides[1] == "top"
        px, py = b.size.width / 2, (b.size.height if top else 0)
        return Quartz.CATransform3DConcat(
            Quartz.CATransform3DMakeScale(scale, scale, 1),
            Quartz.CATransform3DMakeTranslation((px - ax) * (1 - scale), (py - ay) * (1 - scale) + (lift if top else -lift), 0))

    def appear(self):
        """Grow out of the logo, or (with the logo hidden) drop in with a springy scale-up."""
        if self.anchor and self.badge and self.badge.shown and self.target:
            return self.grow_from_logo()
        self.shown = True
        if self.target:
            self._apply(self.target)
        self.panel.setAlphaValue_(0.0)
        self.panel.orderFrontRegardless()
        self.last_front = time.time()
        for layer in (self.view.layer(), self.shadow.layer()):
            layer.removeAllAnimations()
            pop = Quartz.CASpringAnimation.animationWithKeyPath_("transform")
            pop.setFromValue_(Foundation.NSValue.valueWithCATransform3D_(self._scaled(layer, 0.88, 14)))
            pop.setToValue_(Foundation.NSValue.valueWithCATransform3D_(Quartz.CATransform3DIdentity))
            pop.setDamping_(16)
            pop.setStiffness_(210)
            pop.setMass_(1)
            pop.setDuration_(pop.settlingDuration())
            layer.addAnimation_forKey_(pop, "pop")

        def group(ctx):
            ctx.setDuration_(0.16)
            self.panel.animator().setAlphaValue_(1.0)
        AppKit.NSAnimationContext.runAnimationGroup_completionHandler_(group, None)

    def disappear(self):
        """Fold back into the logo, or (with the logo hidden) shrink and fade away."""
        if self.anchor and self.badge and self.badge.shown and self.geo:
            return self.shrink_to_logo()
        self.shown = False
        for layer in (self.view.layer(), self.shadow.layer()):
            out = Quartz.CABasicAnimation.animationWithKeyPath_("transform")
            out.setFromValue_(Foundation.NSValue.valueWithCATransform3D_(Quartz.CATransform3DIdentity))
            out.setToValue_(Foundation.NSValue.valueWithCATransform3D_(self._scaled(layer, 0.95, 8)))
            out.setDuration_(0.2)
            out.setTimingFunction_(Quartz.CAMediaTimingFunction.functionWithName_(Quartz.kCAMediaTimingFunctionEaseIn))
            out.setFillMode_(Quartz.kCAFillModeForwards)
            out.setRemovedOnCompletion_(False)
            layer.addAnimation_forKey_(out, "pop")

        def group(ctx):
            ctx.setDuration_(0.2)
            self.panel.animator().setAlphaValue_(0.0)

        def done():
            if not self.shown:
                self.panel.orderOut_(None)
                self.view.layer().removeAnimationForKey_("pop")
                self.shadow.layer().removeAnimationForKey_("pop")
        AppKit.NSAnimationContext.runAnimationGroup_completionHandler_(group, done)

    def fade_to(self, visible):
        """Called every tick: animates in or out on a change, and keeps the panel in front."""
        now = time.time()
        if visible and not self.shown:
            self.appear()
        elif not visible and self.shown:
            self.disappear()
        elif visible and (not self.panel.isVisible() or now - self.last_front > 1):
            self.panel.orderFrontRegardless()      # back after a screenshot, or over an app that raised itself
            self.last_front = now

    def show_drop(self, on, text=""):
        z = self.drop_zone
        if on:
            b = self.view.bounds()
            z.setFrame_(AppKit.NSInsetRect(b, 6, 6))
            z.show(text)
            z.setAlphaValue_(0.0)
            z.setHidden_(False)

            def group(ctx):
                ctx.setDuration_(0.15)
                z.animator().setAlphaValue_(1.0)
            AppKit.NSAnimationContext.runAnimationGroup_completionHandler_(group, None)
        else:
            z.setHidden_(True)

    def focus(self):
        """Take the keyboard for typing (without switching you away from your app)."""
        self.panel.makeKeyWindow()
        self.panel.makeFirstResponder_(self.field)

    def show_recording(self, on, level=0.0):
        """The mic button: red with a pulsing halo while it's recording you."""
        if on != self.mic_rec:
            self.mic_rec = on
            self.mic.setImage_(_symbol_image("stop.fill" if on else "mic.fill", 13 if on else 15))
            self.mic.setContentTintColor_(AppKit.NSColor.whiteColor() if on else AppKit.NSColor.secondaryLabelColor())
            self.mic.setToolTip_("Stop and send" if on else "Speak instead (stops when you pause; click again to send now)")
        red = AppKit.NSColor.systemRedColor()
        self.mic.layer().setBackgroundColor_(red.colorWithAlphaComponent_(0.85).CGColor() if on else None)
        self.mic.layer().setShadowColor_(red.CGColor())
        self.mic.layer().setShadowOffset_((0, 0))
        self.mic.layer().setShadowRadius_(4 + 8 * level if on else 0)
        self.mic.layer().setShadowOpacity_(0.9 if on else 0)

    def set_border(self, color, width=1.0):
        layer = self.view.layer()
        layer.setBorderColor_(color.CGColor())
        layer.setBorderWidth_(width)


# -- corner logo ------------------------------------------------------------------------------

class _BadgeView(_DropView):
    """The logo: click to open or fold away the conversation, drag to move it, drop files on it."""

    def acceptsFirstMouse_(self, event):
        return True

    def mouseDown_(self, event):
        self.mover = _Mover()
        self.mover.start(self.window())

    def mouseDragged_(self, event):
        if getattr(self, "mover", None) and self.mover.drag() and _ui:
            b = _ui.badge
            cx, cy = b.clamp(*b.centre_now())
            self.window().setFrameOrigin_((cx - b.SIZE / 2 - 4, cy - b.SIZE / 2 - 4))

    def mouseUp_(self, event):
        mover, self.mover = getattr(self, "mover", None), None
        if not _ui or not mover:
            return
        if mover.moved:
            _ui.logo_dropped()
        else:
            _ui.toggle_pinned()


class Badge:
    """The always-on Juniper logo (top-right by default; drag it anywhere on the usable screen).

    Shows what's happening at a glance (ring for listening/speaking, spinner while thinking, a dot
    when there's a reply you haven't seen). Click it to open or fold away the conversation.
    """

    def __init__(self, size=CIRCLE):
        self.SIZE = size
        self.panel = p = _floating_panel(AppKit.NSPanel, size + 8, size + 8)
        root = _BadgeView.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, size + 8, size + 8))
        root.setWantsLayer_(True)
        circle = _blur(AppKit.NSMakeRect(4, 4, size, size), size / 2)
        glyph = _Glyph.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, size, size))
        circle.addSubview_(glyph)
        dot = AppKit.NSView.alloc().initWithFrame_(AppKit.NSMakeRect(size - 7, size - 7, 13, 13))
        self.root = root
        dot.setWantsLayer_(True)
        dot.layer().setBackgroundColor_(AppKit.NSColor.systemRedColor().CGColor())
        dot.layer().setCornerRadius_(6.5)
        dot.layer().setBorderWidth_(2)
        dot.layer().setBorderColor_(AppKit.NSColor.colorWithWhite_alpha_(0.12, 1).CGColor())
        dot.setHidden_(True)
        root.addSubview_(circle)
        root.addSubview_(dot)
        p.setContentView_(root)
        root.setToolTip_("Juniper: click to open, drag to move, drop files to attach")
        self.hovers = []
        hover(circle, "lift", self.hovers, 1.08)
        self.circle, self.glyph, self.dot = circle, glyph, dot
        self.shown = False
        self.last_front = 0.0
        self.pos = None               # centre on screen, if you've moved it
        self.resize(size)

    def resize(self, size):
        """The logo-size slider: everything scales, the centre stays where it was."""
        centre = self.centre_now() if self.panel.isVisible() else None
        self.SIZE = size = int(size)
        self.panel.setContentSize_((size + 8, size + 8))
        self.root.setFrame_(AppKit.NSMakeRect(0, 0, size + 8, size + 8))
        self.circle.setFrame_(AppKit.NSMakeRect(4, 4, size, size))
        self.circle.layer().setCornerRadius_(size / 2)
        self.glyph.setFrame_(AppKit.NSMakeRect(0, 0, size, size))
        self.glyph.size = size * 0.56
        self.glyph.setNeedsDisplay_(True)
        d = max(9, size * 0.26)
        self.dot.setFrame_(AppKit.NSMakeRect(size + 4 - d * 0.8, size + 4 - d * 0.8, d, d))
        self.dot.layer().setCornerRadius_(d / 2)
        if centre and not self.pos:
            self.pos = centre
        if self.panel.isVisible():
            self.place()

    def clamp(self, cx, cy):
        """Keep the circle on the usable screen: never over the menu bar or the Dock."""
        vf, s = AppKit.NSScreen.mainScreen().visibleFrame(), self.SIZE
        cx = min(max(cx, vf.origin.x + s / 2 + 4), vf.origin.x + vf.size.width - s / 2 - 4)
        cy = min(max(cy, vf.origin.y + s / 2 + 4), vf.origin.y + vf.size.height - s / 2 - 4)
        return cx, cy

    def circle_rect(self):
        """Where the circle is on screen (x, y, width, height), kept on screen."""
        vf, s = AppKit.NSScreen.mainScreen().visibleFrame(), self.SIZE
        if self.pos:
            cx, cy = self.pos
        else:
            cx, cy = vf.origin.x + vf.size.width - 14 - s / 2, vf.origin.y + vf.size.height - 10 - s / 2
        cx, cy = self.clamp(cx, cy)
        return (cx - s / 2, cy - s / 2, s, s)

    def centre_now(self):
        f = self.panel.frame()
        return (f.origin.x + 4 + self.SIZE / 2, f.origin.y + 4 + self.SIZE / 2)

    def place(self):
        x, y, _, _ = self.circle_rect()
        self.panel.setFrameOrigin_((x - 4, y - 4))

    def show(self, visible):
        now = time.time()
        if visible and not self.shown:
            self.shown = True
            self.place()
            self.panel.setAlphaValue_(0.0)
            self.panel.orderFrontRegardless()
            self.last_front = now

            def group(ctx):
                ctx.setDuration_(0.25)
                self.panel.animator().setAlphaValue_(1.0)
            AppKit.NSAnimationContext.runAnimationGroup_completionHandler_(group, None)
        elif not visible and self.shown:
            self.shown = False

            def group(ctx):
                ctx.setDuration_(0.18)
                self.panel.animator().setAlphaValue_(0.0)

            def done():
                if not self.shown:
                    self.panel.orderOut_(None)
            AppKit.NSAnimationContext.runAnimationGroup_completionHandler_(group, done)
        elif visible and (not self.panel.isVisible() or now - self.last_front > 2):
            self.panel.orderFrontRegardless()
            self.last_front = now

    def drop_hover(self, on):
        """Files are being dragged over the logo: it swells, glows and shows a +."""
        self.dropping = on
        _hover_apply(self.hovers[0], on)
        self.circle.layer().setBackgroundColor_(ACCENT.colorWithAlphaComponent_(0.35).CGColor() if on else None)

    def draw(self, glyph, ring, ring_width, dot):
        if getattr(self, "dropping", False):
            glyph, ring, ring_width = "+", ACCENT, 3.0
        self.glyph.show(glyph)
        layer = self.circle.layer()
        layer.setBorderColor_(ring.CGColor())
        layer.setBorderWidth_(ring_width)
        self.dot.setHidden_(not dot)


# -- the menu bar panel ---------------------------------------------------------------------------

class _PopActions(AppKit.NSObject):
    """Buttons in the menu bar panel; each tag is an index into the panel's list of actions."""

    def act_(self, sender):
        if _ui and sender.tag() < len(_ui.popover.actions):
            _ui.popover.actions[sender.tag()]()

    def statusClick_(self, sender):
        if _ui:
            _ui.status_clicked()


class _Tile(AppKit.NSView):
    """A quick-toggle tile, like Control Center's: coloured when on."""

    def isFlipped(self):
        return True


class StatusPopover:
    """What you get by clicking the menu bar icon: status, quick toggles, the logo size, your
    sessions (styled like a calendar's day list) and actions. Right-click gives the classic menu."""

    W, PAD = 372, 16

    def __init__(self, ui):
        self.ui = ui
        self.pop = AppKit.NSPopover.alloc().init()
        self.pop.setBehavior_(AppKit.NSPopoverBehaviorTransient)
        self.pop.setAnimates_(True)
        self.pop.setAppearance_(AppKit.NSAppearance.appearanceNamed_(AppKit.NSAppearanceNameVibrantDark))
        self.root = _Flipped.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, self.W, 400))
        vc = AppKit.NSViewController.alloc().init()
        vc.setView_(self.root)
        self.pop.setContentViewController_(vc)
        self.target = _PopActions.alloc().init()
        self.actions, self.hovers, self.key = [], [], None
        self.tab = "home"

    def shown(self):
        return self.pop.isShown()

    def toggle(self, button):
        if self.shown():
            self.pop.performClose_(None)
            return
        self.build()
        AppKit.NSApp.activateIgnoringOtherApps_(True)
        self.pop.showRelativeToRect_ofView_preferredEdge_(button.bounds(), button, AppKit.NSRectEdgeMinY)
        self.anchor_x = button.window().frame().origin.x

    def follow(self, button):
        """Keep the arrow on the icon when the menu bar shifts under it (macOS's orange mic-in-use
        indicator appears or goes when the mic opens or closes): move with it at once, instead of
        jumping the next time the panel redraws."""
        if not self.shown() or button.window() is None:
            return
        x = button.window().frame().origin.x
        if x != getattr(self, "anchor_x", x):
            self.pop.setPositioningRect_(button.bounds())
        self.anchor_x = x

    def refresh(self):
        """Called by the menu bar's timer: rebuild when something shown has changed."""
        if self.shown() and self._key() != self.key:
            self.build()

    def _key(self):
        app, ss = self.ui.app, self.ui.app.sessions
        sessions = tuple((n, ss.current == n, app.runner_for(n).busy(), app.approvals.waiting(n),
                          bool(ss.get(n).get("unread")), ss.get(n).get("mode"), ss.get(n).get("model"), ss.get(n).get("ai"))
                         for n in ss.names())
        info = app.info
        up = app.updater
        return ((up.status, up.checking, bool(up.app_update)) if up else None,
                self.ui.status_line.title, app.open_mic, app.show_logo, app.speak_answers, app.speak_typed,
                app.speaker.use_eleven, sessions, self.tab, tuple(sorted(info.fetched.items())),
                tuple(sorted(info.busy)), info.version_note, ss.get().get("effort"), ss.get().get("context_used"),
                tuple(app.connections.items), app.connections.default)

    # -- building blocks ----------------------------------------------------------------------

    def _do(self, fn):
        self.actions.append(fn)
        return len(self.actions) - 1

    def _click(self, view, fn):
        """Make a whole view clickable with an invisible button on top of it."""
        b = AppKit.NSButton.alloc().initWithFrame_(view.bounds())
        b.setBordered_(False)
        b.setTitle_("")
        b.setTransparent_(True)
        b.setTarget_(self.target)
        b.setAction_("act:")
        b.setTag_(self._do(fn))
        view.addSubview_(b)
        return b

    def _text(self, text, size, weight=AppKit.NSFontWeightRegular, color=None, x=0, y=0, w=200, align=None):
        tf = _label(size, weight, color)
        tf.setStringValue_(text)
        tf.setFrame_(AppKit.NSMakeRect(x, y, w, math.ceil(size * 1.35)))
        if align:
            tf.setAlignment_(align)
        self.root.addSubview_(tf)
        return tf

    def _tile(self, x, y, w, h, symbol, title, hint, on, fn):
        t = _Tile.alloc().initWithFrame_(AppKit.NSMakeRect(x, y, w, h))
        t.setWantsLayer_(True)
        t.layer().setCornerRadius_(14)
        t.layer().setBackgroundColor_((ACCENT.colorWithAlphaComponent_(0.92) if on
                                       else AppKit.NSColor.colorWithWhite_alpha_(1, 0.07)).CGColor())
        circle = AppKit.NSView.alloc().initWithFrame_(AppKit.NSMakeRect(10, 10, 28, 28))
        circle.setWantsLayer_(True)
        circle.layer().setCornerRadius_(14)
        circle.layer().setBackgroundColor_((AppKit.NSColor.colorWithWhite_alpha_(1, 0.25) if on
                                            else AppKit.NSColor.colorWithWhite_alpha_(1, 0.08)).CGColor())
        icon = AppKit.NSImageView.alloc().initWithFrame_(AppKit.NSMakeRect(6, 6, 16, 16))
        icon.setImage_(_symbol_image(symbol, 13))
        icon.setContentTintColor_(AppKit.NSColor.whiteColor() if on else ACCENT)
        circle.addSubview_(icon)
        t.addSubview_(circle)
        name = _label(12, AppKit.NSFontWeightSemibold, AppKit.NSColor.whiteColor() if on else AppKit.NSColor.labelColor())
        name.setStringValue_(title)
        name.setFrame_(AppKit.NSMakeRect(10, h - 34, w - 16, 16))
        t.addSubview_(name)
        if hint:
            sub = _label(10.5, AppKit.NSFontWeightMedium,
                         AppKit.NSColor.whiteColor().colorWithAlphaComponent_(0.75) if on else AppKit.NSColor.tertiaryLabelColor())
            sub.setStringValue_(hint)
            sub.setFrame_(AppKit.NSMakeRect(10, h - 18, w - 16, 14))
            t.addSubview_(sub)
        self._click(t, fn)
        hover(t, "glow", self.hovers)
        self.root.addSubview_(t)

    def _pill(self, x, y, symbol, title, fn, primary=False, danger=False):
        font = AppKit.NSFont.systemFontOfSize_weight_(12, AppKit.NSFontWeightSemibold)
        w = math.ceil(AppKit.NSAttributedString.alloc().initWithString_attributes_(
            title, {AppKit.NSFontAttributeName: font}).size().width) + (48 if symbol else 30)
        p = _Tile.alloc().initWithFrame_(AppKit.NSMakeRect(x, y, w, 28))
        p.setWantsLayer_(True)
        p.layer().setCornerRadius_(9)
        bg = ACCENT.colorWithAlphaComponent_(0.9) if primary else (
            AppKit.NSColor.systemRedColor().colorWithAlphaComponent_(0.25) if danger else AppKit.NSColor.colorWithWhite_alpha_(1, 0.08))
        p.layer().setBackgroundColor_(bg.CGColor())
        tx = 12
        if symbol:
            icon = AppKit.NSImageView.alloc().initWithFrame_(AppKit.NSMakeRect(11, 7, 14, 14))
            icon.setImage_(_symbol_image(symbol, 11))
            icon.setContentTintColor_(AppKit.NSColor.whiteColor() if primary else ACCENT)
            p.addSubview_(icon)
            tx = 29
        label = _label(12, AppKit.NSFontWeightSemibold, AppKit.NSColor.whiteColor() if primary else AppKit.NSColor.labelColor())
        label.setStringValue_(title)
        label.setFrame_(AppKit.NSMakeRect(tx, 6, w - tx - 4, 16))
        p.addSubview_(label)
        self._click(p, fn)
        hover(p, "glow", self.hovers)
        self.root.addSubview_(p)
        return w

    def _divider(self, y):
        d = AppKit.NSView.alloc().initWithFrame_(AppKit.NSMakeRect(self.PAD, y, self.W - 2 * self.PAD, 1))
        d.setWantsLayer_(True)
        d.layer().setBackgroundColor_(AppKit.NSColor.colorWithWhite_alpha_(1, 0.08).CGColor())
        self.root.addSubview_(d)

    # -- Usage and System tabs ------------------------------------------------------------------

    def _bar(self, x, y, w, pct):
        track = AppKit.NSView.alloc().initWithFrame_(AppKit.NSMakeRect(x, y, w, 8))
        track.setWantsLayer_(True)
        track.layer().setCornerRadius_(4)
        track.layer().setBackgroundColor_(AppKit.NSColor.colorWithWhite_alpha_(1, 0.1).CGColor())
        fill = AppKit.NSView.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, max(8, w * min(100, pct) / 100), 8))
        fill.setWantsLayer_(True)
        fill.layer().setCornerRadius_(4)
        colour = AppKit.NSColor.systemRedColor() if pct >= 85 else ACCENT if pct >= 50 else AppKit.NSColor.systemGreenColor()
        fill.layer().setBackgroundColor_(colour.CGColor())
        track.addSubview_(fill)
        self.root.addSubview_(track)

    def _section(self, y, title, refresh=None, note=""):
        P, W = self.PAD, self.W
        self._text(title, 13, AppKit.NSFontWeightBold, ACCENT, x=P, y=y, w=200)
        if note:
            self._text(note, 11, AppKit.NSFontWeightMedium, AppKit.NSColor.tertiaryLabelColor(),
                       x=W - P - 190, y=y + 2, w=160, align=AppKit.NSTextAlignmentRight)
        if refresh:
            b = _symbol_button("arrow.clockwise", self.target, "act:", 12, AppKit.NSColor.secondaryLabelColor())
            b.setTag_(self._do(refresh))
            b.setToolTip_("Refresh")
            b.setFrame_(AppKit.NSMakeRect(W - P - 20, y - 1, 20, 20))
            hover(b, "lift", self.hovers, 1.2)
            self.root.addSubview_(b)
        return y + 26

    def _usage(self, y):
        ui, app, info = self.ui, self.ui.app, self.ui.app.info
        P, W = self.PAD, self.W
        info.refresh("auth")
        info.refresh("usage")
        # Who you're signed in as.
        auth = info.get("auth") or {}
        email = auth.get("email") or "Checking your account…"
        card = _Tile.alloc().initWithFrame_(AppKit.NSMakeRect(P - 4, y, W - 2 * P + 8, 64))
        card.setWantsLayer_(True)
        card.layer().setCornerRadius_(14)
        card.layer().setBackgroundColor_(AppKit.NSColor.colorWithWhite_alpha_(1, 0.06).CGColor())
        self.root.addSubview_(card)
        avatar = AppKit.NSView.alloc().initWithFrame_(AppKit.NSMakeRect(P + 8, y + 12, 40, 40))
        avatar.setWantsLayer_(True)
        avatar.layer().setCornerRadius_(20)
        avatar.layer().setBackgroundColor_(ACCENT.CGColor())
        initial = _label(18, AppKit.NSFontWeightBold, AppKit.NSColor.whiteColor())
        initial.setStringValue_((email[:1] if auth else "?").upper())
        initial.setAlignment_(AppKit.NSTextAlignmentCenter)
        initial.setFrame_(AppKit.NSMakeRect(0, 9, 40, 22))
        avatar.addSubview_(initial)
        self.root.addSubview_(avatar)
        self._text(email, 13.5, AppKit.NSFontWeightSemibold, x=P + 58, y=y + 13, w=W - 2 * P - 150)
        plan = " · ".join(x for x in [auth.get("orgName"), (auth.get("subscriptionType") or "").title() + " plan"
                                      if auth.get("subscriptionType") else ""] if x)
        via = {"claude.ai": "Claude account", "apiKey": "API key"}.get(auth.get("authMethod"), auth.get("authMethod") or "")
        self._text(plan or via, 11.5, AppKit.NSFontWeightMedium,
                   AppKit.NSColor.secondaryLabelColor(), x=P + 58, y=y + 33, w=W - 2 * P - 150)
        self._pill(W - P - 84, y + 18, None, "Switch", lambda: open_in_terminal("claude auth login"))
        y += 80
        # How much of your plan you've used.
        usage = info.get("usage")
        age = info.age("usage")
        note = "updating…" if "usage" in info.busy else (f"updated {int(age // 60)}m ago" if age > 60 else "just now") if usage else ""
        y = self._section(y, "Plan usage", lambda: info.refresh("usage", force=True), note)
        if not usage:
            self._text("Asking Claude Code…", 12, color=AppKit.NSColor.secondaryLabelColor(), x=P, y=y, w=W - 2 * P)
            y += 26
        else:
            for lim in usage["limits"]:
                name = lim["name"].replace("Current session", "This session").replace("Current week", "This week")
                self._text(name, 12.5, AppKit.NSFontWeightSemibold, x=P, y=y, w=W - 2 * P - 60)
                self._text(f"{lim['pct']}%", 12.5, AppKit.NSFontWeightBold, x=W - P - 60, y=y, w=60,
                           align=AppKit.NSTextAlignmentRight)
                self._bar(P, y + 22, W - 2 * P, lim["pct"])
                if lim["resets"]:
                    self._text(f"Resets {lim['resets']}", 10.5, color=AppKit.NSColor.tertiaryLabelColor(), x=P, y=y + 34, w=W - 2 * P)
                y += 56
            if usage["tips"]:
                y += 4
                self._text("What's using it (last 24 hours)", 11.5, AppKit.NSFontWeightSemibold,
                           AppKit.NSColor.secondaryLabelColor(), x=P, y=y, w=W - 2 * P)
                y += 20
                for tip in usage["tips"]:
                    self._text(f"{tip['pct']}%", 11, AppKit.NSFontWeightBold, ACCENT, x=P, y=y, w=36)
                    self._text(tip["text"], 11, color=AppKit.NSColor.secondaryLabelColor(), x=P + 38, y=y, w=W - 2 * P - 38)
                    y += 18
        y += 8
        # This session's context.
        s = app.sessions.get()
        used, window = s.get("context_used") or 0, s.get("context_window") or 0
        y = self._section(y, f"Context · {app.sessions.current}")
        pct = round(100 * used / window) if used and window else 0
        self._text(f"{used / 1000:.0f}k of {window / 1000:.0f}k tokens" if window else "Nothing yet in this session",
                   12, color=AppKit.NSColor.secondaryLabelColor(), x=P, y=y, w=W - 2 * P - 110)
        self._pill(W - P - 100, y - 6, "arrow.down.right.and.arrow.up.left", "Compact",
                   lambda: threading.Thread(target=app.compact, daemon=True).start())
        self._bar(P, y + 26, W - 2 * P, pct)
        return y + 46

    def _system(self, y):
        ui, app, info = self.ui, self.ui.app, self.ui.app.info
        P, W = self.PAD, self.W
        info.refresh("version")
        info.refresh("mcp")
        # Juniper itself: version and updates (the packaged app updates itself).
        up = app.updater
        y = self._section(y, "Juniper")
        version = CODE_VERSION if CODE_VERSION == APP_VERSION else f"{CODE_VERSION} (app {APP_VERSION})"
        self._text(f"Version {version}", 12.5, AppKit.NSFontWeightSemibold, x=P, y=y + 4, w=180)
        note = ("checking…" if up.checking else up.status) if up else "updates come from git in this folder"
        self._text(note, 10.5, color=AppKit.NSColor.tertiaryLabelColor(), x=P, y=y + 24, w=W - 2 * P - 150)
        if up and up.app_update:
            self._pill(W - P - 150, y, "arrow.down.circle", f"Download {up.app_update['version']}", up.download_app,
                       primary=True)
        elif up:
            self._pill(W - P - 120, y, "arrow.clockwise", "Check now",
                       lambda: threading.Thread(target=up.check, kwargs={"manual": True}, daemon=True).start())
        y += 48
        # Your AIs: which are connected, the default for new sessions, and a way to add or change them.
        import connections
        y = self._section(y, "Your AI")
        for cid in app.connections.items:
            conn = app.connections.get(cid)
            self._text(connections.label(conn), 12.5, AppKit.NSFontWeightSemibold, x=P, y=y + 2, w=W - 2 * P - 90)
            if cid == app.connections.default:
                self._text("default", 10.5, AppKit.NSFontWeightMedium, ACCENT, x=W - P - 80, y=y + 4, w=80,
                           align=AppKit.NSTextAlignmentRight)
            y += 22
        self._pill(P, y + 4, "plus.circle", "Add or change AIs…", ui.add_ai)
        y += 40
        self._text("Keys stay in your Keychain and only go to their own AI. Juniper has no servers.", 10.5,
                   color=AppKit.NSColor.tertiaryLabelColor(), x=P, y=y, w=W - 2 * P)
        y += 26
        # Claude Code itself.
        y = self._section(y, "Claude Code")
        self._text(f"Version {info.get('version') or '…'}", 12.5, AppKit.NSFontWeightSemibold, x=P, y=y + 4, w=150)
        if info.version_note:
            self._text(info.version_note, 10.5, color=AppKit.NSColor.tertiaryLabelColor(), x=P, y=y + 24, w=W - 2 * P - 150)
        self._pill(W - P - 170, y, "arrow.down.circle", "Check for updates", info.update)
        y += 48
        # Effort for this session.
        y = self._section(y, f"Effort · {app.sessions.current}")
        current = app.sessions.get().get("effort")
        x = P
        for e in EFFORTS:
            x += self._pill(x, y, None, e.title(), lambda e=e: app.set_effort(e), primary=e == current) + 6
        y += 34
        self._text("How hard Claude thinks. Higher is slower, more thorough.", 10.5,
                   color=AppKit.NSColor.tertiaryLabelColor(), x=P, y=y, w=W - 2 * P)
        y += 26
        # MCP servers.
        servers = info.get("mcp")
        counts = {}
        for srv in servers or []:
            counts[srv["state"]] = counts.get(srv["state"], 0) + 1
        short = {"connected": "on", "needs sign-in": "need sign-in", "failed": "failed"}
        note = "checking…" if "mcp" in info.busy else " · ".join(f"{n} {short[k]}" for k, n in counts.items())
        y = self._section(y, "MCP servers", lambda: info.refresh("mcp", force=True), note)
        colours = {"connected": AppKit.NSColor.systemGreenColor(), "needs sign-in": AppKit.NSColor.systemYellowColor(),
                   "failed": AppKit.NSColor.systemRedColor()}
        order = {"failed": 0, "needs sign-in": 1, "connected": 2}
        shown = sorted(servers or [], key=lambda srv: (order[srv["state"]], srv["name"].lower()))
        for srv in shown[:12]:
            dot = AppKit.NSView.alloc().initWithFrame_(AppKit.NSMakeRect(P + 2, y + 5, 8, 8))
            dot.setWantsLayer_(True)
            dot.layer().setCornerRadius_(4)
            dot.layer().setBackgroundColor_(colours[srv["state"]].CGColor())
            self.root.addSubview_(dot)
            self._text(srv["name"], 12, AppKit.NSFontWeightMedium, x=P + 18, y=y, w=W - 2 * P - 120)
            self._text(srv["state"], 11, color=colours[srv["state"]], x=W - P - 110, y=y + 1, w=110,
                       align=AppKit.NSTextAlignmentRight)
            y += 20
        if servers and len(shown) > 12:
            self._text(f"and {len(shown) - 12} more", 11, color=AppKit.NSColor.tertiaryLabelColor(), x=P + 18, y=y, w=200)
            y += 20
        if not servers:
            self._text("Checking your servers (this takes a few seconds)…", 11.5, color=AppKit.NSColor.secondaryLabelColor(),
                       x=P, y=y, w=W - 2 * P)
            y += 22
        y += 8
        x = P
        if counts.get("needs sign-in"):
            x += self._pill(x, y, "person.badge.key", "Sign in to servers…",
                            lambda: open_in_terminal("cd ~ && claude /mcp")) + 6
        self._pill(x, y, "stethoscope", "Health check", lambda: open_in_terminal("claude doctor"))
        y += 36
        n_skills, n_cmds = len(app.skills), len(app.slash_commands)
        if n_cmds:
            self._text(f"{n_skills} skills and {n_cmds} commands available (type / in the chat)", 10.5,
                       color=AppKit.NSColor.tertiaryLabelColor(), x=P, y=y, w=W - 2 * P)
            y += 20
        return y

    def switch_tab(self, tab):
        self.tab = tab
        self.build()

    def _home(self, y):
        ui, app, cfg = self.ui, self.ui.app, self.ui.cfg
        P, W = self.PAD, self.W
        bg = lambda fn: (lambda: threading.Thread(target=fn, daemon=True).start())
        # Quick toggles.
        tw, th, gap = (W - 2 * P - 16) / 3, 70, 8
        tiles = [("mic.fill", "Open mic", key_hint(cfg["mic_key"]), app.open_mic, ui.toggle_mic),
                 ("berries", "Logo", key_hint(cfg["logo_key"]), app.show_logo, ui.toggle_logo),
                 ("speaker.wave.2.fill", "Voice replies", "Speak answers", app.speak_answers,
                  lambda: app.set_speak(answers=not app.speak_answers)),
                 ("keyboard", "Typed replies", "Read aloud too", app.speak_answers and app.speak_typed,
                  lambda: app.set_speak(typed=not app.speak_typed)),
                 ("waveform", "ElevenLabs", "Natural voice", app.speaker.use_eleven, lambda: ui.toggle_eleven(None)),
                 ("mic.badge.plus", "Speak now", f"or {key_hint(cfg['ptt_key'])}", app.recording_now(),
                  bg(app.record_toggle))]
        for i, (sym, title, hint, on, fn) in enumerate(tiles):
            self._tile(P + (i % 3) * (tw + gap), y + (i // 3) * (th + gap), tw, th, sym, title, hint, on, fn)
        y += 2 * (th + gap) + 4

        # Logo size.
        small = AppKit.NSImageView.alloc().initWithFrame_(AppKit.NSMakeRect(P + 2, y + 5, 12, 12))
        small.setImage_(_symbol_image("berries", 8))
        small.setContentTintColor_(AppKit.NSColor.tertiaryLabelColor())
        large = AppKit.NSImageView.alloc().initWithFrame_(AppKit.NSMakeRect(W - P - 18, y + 2, 18, 18))
        large.setImage_(_symbol_image("berries", 14))
        large.setContentTintColor_(AppKit.NSColor.tertiaryLabelColor())
        slider = AppKit.NSSlider.sliderWithValue_minValue_maxValue_target_action_(app.logo_size, 28, 110, ui.sizer, "size:")
        slider.setContinuous_(True)
        slider.setTrackFillColor_(ACCENT)
        slider.setFrame_(AppKit.NSMakeRect(P + 22, y, W - 2 * P - 46, 22))
        for v in (small, large, slider):
            self.root.addSubview_(v)
        self._text("Logo size", 10.5, AppKit.NSFontWeightMedium, AppKit.NSColor.tertiaryLabelColor(),
                   x=P, y=y + 24, w=W - 2 * P, align=AppKit.NSTextAlignmentCenter)
        y += 50
        self._divider(y)
        y += 14

        # Sessions, like a calendar's day list.
        self._text("Sessions", 13, AppKit.NSFontWeightBold, ACCENT, x=P, y=y, w=120)
        n = len(app.sessions.names())
        self._text(f"·  {n} open", 12, AppKit.NSFontWeightMedium, ACCENT.colorWithAlphaComponent_(0.7), x=P + 66, y=y + 1, w=100)
        plus = _symbol_button("plus", self.target, "act:", 14, AppKit.NSColor.secondaryLabelColor())
        plus.setTag_(self._do(lambda: ui.pick_folder(None)))
        plus.setToolTip_("New session for a folder…")
        plus.setFrame_(AppKit.NSMakeRect(W - P - 22, y - 2, 22, 22))
        hover(plus, "lift", self.hovers, 1.2)
        self.root.addSubview_(plus)
        y += 28
        ss = app.sessions
        for name in ss.names()[:7]:
            s = ss.get(name)
            current = name == ss.current
            waiting, busy_s = app.approvals.waiting(name), app.runner_for(name).busy()
            unread = bool(s.get("unread")) and not current
            bar_colour = (AppKit.NSColor.systemYellowColor() if waiting else AppKit.NSColor.systemRedColor() if unread
                          else ACCENT if current else AppKit.NSColor.colorWithWhite_alpha_(1, 0.25))
            row = _Tile.alloc().initWithFrame_(AppKit.NSMakeRect(P - 6, y, W - 2 * P + 12, 50))
            row.setWantsLayer_(True)
            row.layer().setCornerRadius_(10)
            row.layer().setBackgroundColor_((ACCENT.colorWithAlphaComponent_(0.13) if current
                                             else AppKit.NSColor.colorWithWhite_alpha_(1, 0.03)).CGColor())
            bar = AppKit.NSView.alloc().initWithFrame_(AppKit.NSMakeRect(8, 8, 4, 34))
            bar.setWantsLayer_(True)
            bar.layer().setCornerRadius_(2)
            bar.layer().setBackgroundColor_(bar_colour.CGColor())
            row.addSubview_(bar)
            title = _label(13.5, AppKit.NSFontWeightSemibold)
            title.setStringValue_(name)
            title.setFrame_(AppKit.NSMakeRect(22, 7, W - 2 * P - 110, 18))
            row.addSubview_(title)
            folder = os.path.basename(s["folder"]) or "~"
            detail = _label(11, AppKit.NSFontWeightRegular, AppKit.NSColor.secondaryLabelColor())
            detail.setStringValue_(f"{folder}  ·  {app.ai_label(name)}  ·  {mode_label(app.mode_text(name))}")
            detail.setFrame_(AppKit.NSMakeRect(22, 27, W - 2 * P - 40, 15))
            row.addSubview_(detail)
            state, colour = (("needs you", AppKit.NSColor.systemYellowColor()) if waiting else
                             ("working…", ACCENT) if busy_s else ("new reply", AppKit.NSColor.systemRedColor()) if unread
                             else ("current", ACCENT) if current else ("", None))
            if state:
                tag = _label(10.5, AppKit.NSFontWeightSemibold, colour)
                tag.setStringValue_(state)
                tag.setAlignment_(AppKit.NSTextAlignmentRight)
                tag.setFrame_(AppKit.NSMakeRect(W - 2 * P - 84, 9, 84, 14))
                row.addSubview_(tag)
            self._click(row, bg(lambda n=name: app.switch_session(n)))
            hover(row, "glow", self.hovers)
            self.root.addSubview_(row)
            y += 54
            if current:
                x = P + 16
                x += self._pill(x, y, "trash", "Clear", bg(app.clear_session)) + 8
                self._pill(x, y, "arrow.down.right.and.arrow.up.left", "Compact", bg(app.compact))
                y += 36
        y += 4
        self._divider(y)
        y += 14

        # Actions.
        x = P
        x += self._pill(x, y, "keyboard", f"Type  {key_hint(cfg['type_key'])}", ui.open_typing, primary=True) + 8
        x += self._pill(x, y, "text.bubble", "Last answer", lambda: ui.show_last(None)) + 8
        self._pill(x, y, "stop.fill", "Stop", app.stop_everything, danger=True)
        y += 40
        return y


    # -- the panel ----------------------------------------------------------------------------

    def build(self):
        ui, app, cfg = self.ui, self.ui.app, self.ui.cfg
        self.key = self._key()
        for v in list(self.root.subviews()):
            v.removeFromSuperview()
        self.actions, self.hovers = [], []
        P, W = self.PAD, self.W
        bg = lambda fn: (lambda: threading.Thread(target=fn, daemon=True).start())

        # Header: the logo, the name, and what it's doing right now.
        glyph = _Glyph.alloc().initWithFrame_(AppKit.NSMakeRect(P - 4, 12, 40, 40))
        glyph.size = 28
        self.root.addSubview_(glyph)
        self._text("Juniper", 17, AppKit.NSFontWeightBold, x=P + 42, y=13, w=180)
        status = ui.status_line.title
        busy = status in ("Thinking…", "Listening…", "Speaking", "Ask a follow-up…")
        dot_colour = (AppKit.NSColor.systemRedColor() if status == "Listening…" else ACCENT if busy
                      else AppKit.NSColor.systemGreenColor() if app.open_mic else AppKit.NSColor.tertiaryLabelColor())
        dot = AppKit.NSView.alloc().initWithFrame_(AppKit.NSMakeRect(P + 43, 40, 7, 7))
        dot.setWantsLayer_(True)
        dot.layer().setCornerRadius_(3.5)
        dot.layer().setBackgroundColor_(dot_colour.CGColor())
        self.root.addSubview_(dot)
        self._text(status.replace("Mic off: ", "Mic off · "), 11.5, AppKit.NSFontWeightMedium,
                   AppKit.NSColor.secondaryLabelColor(), x=P + 55, y=35, w=W - P - 55 - 70)
        gear = _symbol_button("gearshape.fill", self.target, "act:", 14, AppKit.NSColor.secondaryLabelColor())
        gear.setTag_(self._do(lambda: subprocess.Popen(["open", "-t", str(DATA / "config.toml")])))
        gear.setToolTip_("Settings")
        gear.setFrame_(AppKit.NSMakeRect(W - P - 52, 20, 22, 22))
        power = _symbol_button("power", self.target, "act:", 14, AppKit.NSColor.secondaryLabelColor())
        power.setTag_(self._do(lambda: ui.quit(None)))
        power.setToolTip_("Quit Juniper")
        power.setFrame_(AppKit.NSMakeRect(W - P - 22, 20, 22, 22))
        for b in (gear, power):
            hover(b, "lift", self.hovers, 1.18)
            self.root.addSubview_(b)
        # Tabs.
        tabs = AppKit.NSSegmentedControl.segmentedControlWithLabels_trackingMode_target_action_(
            ["Home", "Usage", "System"], AppKit.NSSegmentSwitchTrackingSelectOne, self.target, "act:")
        tabs.setTag_(self._do(lambda: self.switch_tab(["home", "usage", "system"][self.tabs.selectedSegment()])))
        tabs.setSelectedSegment_(["home", "usage", "system"].index(self.tab))
        tabs.setFrame_(AppKit.NSMakeRect(P, 62, W - 2 * P, 26))
        tabs.setSelectedSegmentBezelColor_(ACCENT)          # ours, not the system's blue
        for i in range(3):
            tabs.setWidth_forSegment_((W - 2 * P - 6) / 3, i)
        self.tabs = tabs
        self.root.addSubview_(tabs)
        y = 102
        y = {"home": self._home, "usage": self._usage, "system": self._system}[self.tab](y)
        y += 4

        # Footer.
        self._divider(y)
        y += 10
        for i, (label, fn) in enumerate([("Settings…", lambda: subprocess.Popen(["open", "-t", str(DATA / "config.toml")])),
                                         ("Log…", lambda: subprocess.Popen(["open", "-a", "Console", str(LOG_PATH)])),
                                         ("Set up again…", self.ui.setup_again),
                                         ("Right-click: classic menu", None)]):
            t = self._text(label, 11, AppKit.NSFontWeightMedium,
                           AppKit.NSColor.secondaryLabelColor() if fn else AppKit.NSColor.tertiaryLabelColor(),
                           x=P + i * 66 if fn else W - P - 130, y=y, w=90 if fn else 130,
                           align=None if fn else AppKit.NSTextAlignmentRight)
            if fn:
                self._click(t, fn)
        y += 26
        # Only tell the popover the new size: it positions its own view (setting the view's frame
        # ourselves, even while it's closed, shifts everything sideways).
        if self.root.window() is None:              # before the popover has ever shown it
            self.root.setFrame_(AppKit.NSMakeRect(0, 0, W, y))
        self.pop.setContentSize_((W, y))


# -- menu bar app -----------------------------------------------------------------------------

_SYMBOLS = {}
SYMBOL_PT, SYMBOL_WEIGHT, SYMBOL_BOX = 16, AppKit.NSFontWeightSemibold, (26.0, 22.0)
BADGE_PT = 9             # the small symbol on the berries in the menu bar


def status_item_shown(win):
    """macOS 26 says a menu bar item's window is visible even when it has moved it off the screen,
    or left of the notch under the frontmost app's menus; whether any of it can be seen (occlusion)
    is what tells. Its position is checked too."""
    if win is None or not win.isVisible() or not (win.occlusionState() & AppKit.NSWindowOcclusionStateVisible):
        return False
    return icon_visible(win.frame(), [(s.frame(), s.auxiliaryTopLeftArea(), s.auxiliaryTopRightArea(),
                                       s.safeAreaInsets().top) for s in AppKit.NSScreen.screens()])


def icon_visible(frame, screens):
    """Can you see a menu bar item at this frame? screens: (frame, the areas left and right of a
    notch, the notch's height) for each display. It must be on a display, and on one with a notch,
    clear of it (macOS parks items it has no room for behind the notch)."""
    cx, cy = frame.origin.x + frame.size.width / 2, frame.origin.y + frame.size.height / 2
    for sf, left, right, notch in screens:
        if not (sf.origin.x <= cx <= sf.origin.x + sf.size.width and sf.origin.y <= cy <= sf.origin.y + sf.size.height):
            continue
        if notch <= 0:
            return True
        return any(a.size.width > 0 and a.origin.x <= cx <= a.origin.x + a.size.width for a in (left, right))
    return False


def _symbol(name):
    """A menu bar icon centred on a fixed 26×22 canvas: the berries with a small SF Symbol badge
    for what Juniper is doing ("juniper:waveform"), "berries:3" for a frame of the thinking pulse,
    or a plain SF Symbol. The symbols' own sizes differ (13 to 24 pt wide, 14 to 21 tall), which
    made the status item, and the popover hanging from it, move a point or two whenever the icon
    changed."""
    if name in _SYMBOLS:
        return _SYMBOLS[name]
    W, H = SYMBOL_BOX
    if name.startswith("juniper:"):
        badge = AppKit.NSImage.imageWithSystemSymbolName_accessibilityDescription_(name[8:], "Juniper")
        badge = badge.imageWithSymbolConfiguration_(AppKit.NSImageSymbolConfiguration.configurationWithPointSize_weight_scale_(
            BADGE_PT, AppKit.NSFontWeightBold, AppKit.NSImageSymbolScaleSmall))
        bw, bh = badge.size().width, badge.size().height
        bx, by = W - 5.5, 5.0                      # the badge's centre: bottom right

        def draw(rect):
            draw_berries(W / 2 - 2, H / 2 + 1.5, 19, AppKit.NSColor.blackColor(), shine=False)
            # Cut a gap around the badge so it reads on its own at menu bar size.
            ctx = AppKit.NSGraphicsContext.currentContext()
            ctx.setCompositingOperation_(AppKit.NSCompositingOperationClear)
            r = max(bw, bh) / 2 + 1.4
            AppKit.NSBezierPath.bezierPathWithOvalInRect_(AppKit.NSMakeRect(bx - r, by - r, 2 * r, 2 * r)).fill()
            ctx.setCompositingOperation_(AppKit.NSCompositingOperationSourceOver)
            badge.drawInRect_(AppKit.NSMakeRect(bx - bw / 2, by - bh / 2, bw, bh))
            return True
    elif name.startswith("berries"):
        frame = int(name[8:]) if name.startswith("berries:") else None

        def draw(rect):
            draw_berries(W / 2, H / 2, 19, AppKit.NSColor.blackColor(), frame, shine=False)
            return True
    else:
        sym = AppKit.NSImage.imageWithSystemSymbolName_accessibilityDescription_(name, "Juniper")
        if not sym:
            return None
        # Size and weight to sit with the other menu bar icons (the defaults look thin and small there).
        sym = sym.imageWithSymbolConfiguration_(AppKit.NSImageSymbolConfiguration.configurationWithPointSize_weight_scale_(
            SYMBOL_PT, SYMBOL_WEIGHT, AppKit.NSImageSymbolScaleMedium))
        w, h = sym.size().width, sym.size().height

        def draw(rect):
            sym.drawInRect_(AppKit.NSMakeRect(round((W - w) / 2), round((H - h) / 2), w, h))
            return True
    img = AppKit.NSImage.imageWithSize_flipped_drawingHandler_((W, H), False, draw)
    img.setTemplate_(True)
    img.setAccessibilityDescription_("Juniper")
    _SYMBOLS[name] = img
    return img


class MenuBar(rumps.App):
    ICON_W = 30
    def __init__(self, app):
        super().__init__("Juniper", title="Juniper", quit_button=None)
        self.app, self.cfg = app, app.cfg
        ptt, mic = key_hint(self.cfg["ptt_key"]), key_hint(self.cfg["mic_key"])
        self.status_line = rumps.MenuItem("Starting…")
        self.mic_item = rumps.MenuItem(f"Open mic  ({mic})", callback=self.toggle_mic)
        self.eleven_item = rumps.MenuItem("ElevenLabs voice", callback=self.toggle_eleven)
        self.logo_item = rumps.MenuItem(f"Show the logo  ({key_hint(self.cfg['logo_key'])})", callback=self.toggle_logo)
        self.speak_item = rumps.MenuItem("Speak answers", callback=lambda _: self.app.set_speak(
            answers=not self.app.speak_answers))
        self.speak_typed_item = rumps.MenuItem("Speak answers to typed requests", callback=lambda _: self.app.set_speak(
            typed=not self.app.speak_typed))
        self.sessions_menu = rumps.MenuItem("Session")
        self.sessions_key = None
        self.size_item = rumps.MenuItem("Logo size")
        self.menu = [
            self.status_line,
            None,
            self.mic_item,
            self.logo_item,
            self.size_item,
            rumps.MenuItem(f"Push to talk: hold {ptt}, or tap and talk"),
            rumps.MenuItem(f"Type a request  ({key_hint(self.cfg['type_key'])})", callback=lambda _: self.open_typing()),
            None,
            rumps.MenuItem("Show last answer", callback=self.show_last),
            rumps.MenuItem(f"Stop  ({key_hint(self.cfg['stop_key'])})", callback=lambda _: self.app.stop_everything()),
            None,
            self.sessions_menu,
            rumps.MenuItem("Clear this session", callback=lambda _: threading.Thread(
                target=self.app.clear_session, daemon=True).start()),
            rumps.MenuItem("Compact this session", callback=lambda _: threading.Thread(
                target=self.app.compact, daemon=True).start()),
            None,
            self.speak_item,
            self.speak_typed_item,
            self.eleven_item,
            rumps.MenuItem("Settings…", callback=lambda _: subprocess.Popen(["open", "-t", str(DATA / "config.toml")])),
            rumps.MenuItem("Log…", callback=lambda _: subprocess.Popen(["open", "-a", "Console", str(LOG_PATH)])),
            rumps.MenuItem("Add or change AIs…", callback=lambda _: self.add_ai()),
            rumps.MenuItem("Set up again…", callback=lambda _: self.setup_again()),
            None,
            rumps.MenuItem("Quit Juniper", callback=self.quit),
        ]
        self.overlay = None
        self.hotkeys = None
        self.started = False
        self.icon_key = None
        self.phase = None
        self.level = 0.0
        self.badge = None
        self.pinned = False           # conversation opened by you (logo, ⌃⌥T); stays until you fold it away
        self.want_focus = False
        self.dropped = []             # files dropped on the logo, waiting for "which session?"
        self.picker = None
        self.timer = rumps.Timer(self.tick, 0.1)
        self.timer.start()
        self.anim_timer = rumps.Timer(self.animate, 1 / 30)
        self.anim_timer.start()

    # -- menu actions -----------------------------------------------------------------------

    def setup_again(self, _=None):
        """Run the first-run setup again (Claude Code, voice, your voice profile, projects)."""
        set_state(onboarded=False)
        self.app.shutdown()
        restart()

    def toggle_mic(self, _=None):
        self.app.set_open_mic(not self.app.open_mic)

    def toggle_logo(self, _=None):
        self.app.set_show_logo(not self.app.show_logo)

    def toggle_eleven(self, _):
        if self.app.speaker.eleven_key:
            self.app.speaker.use_eleven = not self.app.speaker.use_eleven
        else:
            rumps.alert("No ElevenLabs key", f"Add ELEVENLABS_API_KEY=… to {DATA / '.env'} and restart.")

    def show_last(self, _):
        self.app.state.set(dismissed=False)

    def quit(self, _):
        self.app.shutdown()
        rumps.quit_application()

    def _size_slider(self):
        """"Logo size" in the menu is a slider you drag; the size is remembered."""
        box = AppKit.NSView.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, 240, 30))
        label = AppKit.NSTextField.labelWithString_("Logo size")
        label.setFont_(AppKit.NSFont.menuFontOfSize_(13))
        label.setFrame_(AppKit.NSMakeRect(20, 6, 70, 18))
        slider = AppKit.NSSlider.sliderWithValue_minValue_maxValue_target_action_(
            self.app.logo_size, 28, 110, self.sizer, "size:")
        slider.setContinuous_(True)
        slider.setTrackFillColor_(ACCENT)
        slider.setFrame_(AppKit.NSMakeRect(92, 5, 132, 20))
        box.addSubview_(label)
        box.addSubview_(slider)
        self.size_item._menuitem.setView_(box)

    def set_logo_size(self, size):
        self.badge.resize(size)
        self.app.set_logo_size(int(size))

    def drag_hover(self, on, on_logo, count=0):
        if on_logo:
            self.badge.drop_hover(on)
        elif on:
            name = self.app.sessions.current
            self.overlay.show_drop(True, f"Drop to attach {count} file{'s' * (count != 1)} to {name}")
        else:
            self.overlay.show_drop(False)

    def compact_from_wheel(self):
        """Clicking the context wheel compacts the conversation (unless Claude is busy)."""
        if self.app.runner.busy():
            return
        self.overlay.hover_context(False)
        threading.Thread(target=self.app.compact, daemon=True).start()

    def toggle_sidebar(self):
        self.overlay.sidebar_open = not self.overlay.sidebar_open
        if self.overlay.sidebar_open and not self.overlay.shown:
            self.open_typing()

    def sidebar_pick(self, name):
        threading.Thread(target=self.app.switch_session, args=(name,), daemon=True).start()

    def chip_menu(self, chip, kind):
        """The mode and model chips: a little menu, ticked at the current choice."""
        if kind == "model":
            return self.model_menu(chip)
        s = self.app.sessions.get()
        agent = self.app.agent_modes()
        current = s.get(kind) or (self.cfg["permission_mode"] if agent is None else self.app.connection_for().get("current_mode"))
        menu = AppKit.NSMenu.alloc().initWithTitle_(kind)
        for value, label in (MODES if agent is None else agent or [("", "This AI has one mode")]):
            item = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(label, "pickMode:", "")
            item.setTarget_(self.overlay.actions)
            item.setRepresentedObject_(value)
            item.setState_(int(value == current))
            menu.addItem_(item)
        menu.addItem_(AppKit.NSMenuItem.separatorItem())
        tip = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("Shift-Tab in the text box cycles modes", None, "")
        tip.setEnabled_(False)
        menu.addItem_(tip)
        menu.popUpMenuPositioningItem_atLocation_inView_(None, (0, -4), chip.superview() or chip)

    def model_menu(self, chip):
        """The model chip: this session's model, the AI it uses (switching starts afresh), and for
        Claude how hard it thinks."""
        import connections
        app = self.app
        s, conn = app.sessions.get(), app.connection_for()
        claude = conn["provider"] == "claude"
        menu = AppKit.NSMenu.alloc().initWithTitle_("model")

        def add(title, action=None, value=None, on=False):
            item = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, action, "")
            if action:
                item.setTarget_(self.overlay.actions)
                item.setRepresentedObject_(value)
            else:
                item.setEnabled_(False)
            item.setState_(int(on))
            menu.addItem_(item)
        add("Model")
        current = s.get("model") or (self.cfg["model"] if claude else conn.get("model") or conn.get("current_model"))
        choices = MODELS if claude else conn.get("agent_models") or [(m, m) for m in conn.get("models") or [conn.get("model")] if m]
        for value, label in list(choices)[:30]:
            add(label, "pickModel:", value, value == current)
        menu.addItem_(AppKit.NSMenuItem.separatorItem())
        add("AI for this session (switching starts afresh)")
        for cid in app.connections.items:
            title = connections.label(app.connections.get(cid))
            add(title + ("   · default" if cid == app.connections.default else ""), "pickAI:", cid, cid == conn["id"])
        add("Add or change AIs…", "addAI:")
        if claude:
            menu.addItem_(AppKit.NSMenuItem.separatorItem())
            add("Effort")
            for e in EFFORTS:
                add(e.title(), "pickEffort:", e, e == s.get("effort"))
        menu.popUpMenuPositioningItem_atLocation_inView_(None, (0, -4), chip.superview() or chip)

    def add_ai(self, _=None):
        """The setup window's AI step, while Juniper keeps running; your AIs reload when it closes."""
        import onboarding
        if getattr(self, "ai_setup", None):
            self.ai_setup[0].makeKeyAndOrderFront_(None)
            return

        def done():
            self.app.connections.load()
            self.ai_setup = None
            log("ai", f"your AIs: {', '.join(self.app.connections.items)} (default {self.app.connections.default})", "green")
        self.ai_setup = onboarding.open_window(self.cfg, DATA / "config.toml", only="ai", on_done=done)

    def card_choice(self, i):
        o = self.overlay
        item = o.card_item
        if item and i < len(o.card_choices):
            choice = o.card_choices[i]
            self.app.sessions.new_turn(item["session"], {"allow": "Allow", "allow_session": "Allow for this session",
                                                         "deny": "Deny", "plan_edits": "Go ahead (auto-accept edits)",
                                                         "plan_ask": "Go ahead (ask before edits)",
                                                         "plan_keep": "Keep planning"}.get(choice, choice))
            self.app.approvals.answer(item, choice)

    def copy_reply(self, button):
        texts = self.overlay.copy_texts
        if button.tag() < len(texts):
            pb = AppKit.NSPasteboard.generalPasteboard()
            pb.clearContents()
            pb.setString_forType_(texts[button.tag()], AppKit.NSPasteboardTypeString)
            button.setImage_(_symbol_image("checkmark", 11))         # "copied"

    # -- the overlay: open, fold away, type, stop -------------------------------------------

    def toggle_pinned(self):
        """Clicking the logo opens the conversation (ready to type), or folds it away."""
        if self.overlay.shown:
            self.close_overlay()
        else:
            self.open_typing()

    def open_typing(self):
        self.pinned = True
        self.want_focus = True
        self.app.state.set(dismissed=False)

    def close_overlay(self):
        """✕, Esc or clicking the logo: fold the overlay back into the logo. Nothing is cancelled;
        a task keeps going and the logo shows it (spinner, then a dot when the reply is in)."""
        self.pinned = False
        self.app.state.set(dismissed=True, dismissed_at=time.time())

    def send_or_stop(self):
        o = self.overlay
        text = o.text()
        if o.send_mode == "stop":
            log("you", "stop (button)", "cyan")
            self.app.runner.cancel()
            self.app.speaker.stop()
            return
        if not text.strip() and not self.app.attachments:
            return
        o.set_text("")
        self.pinned = True
        threading.Thread(target=self.app.typed, args=(text,), daemon=True).start()

    def pick_attachments(self):
        AppKit.NSApp.activateIgnoringOtherApps_(True)
        panel = AppKit.NSOpenPanel.openPanel()
        panel.setCanChooseFiles_(True)
        panel.setCanChooseDirectories_(True)
        panel.setAllowsMultipleSelection_(True)
        panel.setPrompt_("Attach")
        if panel.runModal() == AppKit.NSModalResponseOK:
            self.app.attach([u.path() for u in panel.URLs()])
        self.want_focus = True

    def files_dropped(self, paths, on_logo=False):
        """Files dropped on the open overlay go to the session it shows; dropped on the logo, with
        more than one session, you pick the session first."""
        names = self.app.sessions.names()
        if not on_logo or len(names) < 2:
            self.app.attach(paths)
            self.open_typing()
            return
        self.dropped = paths
        AppKit.NSOperationQueue.mainQueue().addOperationWithBlock_(self._ask_session)   # after the drop finishes

    def _ask_session(self):
        ss = self.app.sessions
        self.picker = self.picker or _Picker.alloc().init()
        menu = AppKit.NSMenu.alloc().initWithTitle_("Attach to")
        n = len(self.dropped)
        head = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            f"Attach {n} file{'s' * (n > 1)} to which session?", None, "")
        head.setEnabled_(False)
        menu.addItem_(head)
        for name in ss.names():
            folder = os.path.basename(ss.get(name)["folder"]) or "~"
            item = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                f"{name}   ({folder})" if folder != name else name, "pick:", "")
            item.setTarget_(self.picker)
            item.setRepresentedObject_(name)
            item.setState_(int(name == ss.current))
            menu.addItem_(item)
        AppKit.NSApp.activateIgnoringOtherApps_(True)
        menu.popUpMenuPositioningItem_atLocation_inView_(None, AppKit.NSEvent.mouseLocation(), None)

    def attach_to_session(self, name):
        if name != self.app.sessions.current:
            self.app.enter_session(name)
        self.app.attach(self.dropped)
        self.dropped = []
        self.open_typing()

    # -- moving the logo and the overlay ------------------------------------------------------

    def logo_dropped(self):
        self.badge.pos = self.badge.clamp(*self.badge.centre_now())
        self.app.set_logo_pos(list(self.badge.pos))

    def overlay_dragging(self):
        """While you drag the open overlay, the (hidden) logo follows the corner it opened from."""
        o = self.overlay
        o.dragging = True
        f = o.panel.frame()
        o.canvas = (f.origin.x, f.origin.y, f.size.width, f.size.height)
        x, y, w, h = o.screen_rect()
        f = AppKit.NSMakeRect(x, y, w, h)
        half = CIRCLE / 2
        cx = f.origin.x + half if o.sides[0] == "left" else f.origin.x + f.size.width - half
        cy = f.origin.y + f.size.height - half if o.sides[1] == "top" else f.origin.y + half
        self.badge.pos = (cx, cy)
        self.badge.place()
        o.anchor = self.badge.circle_rect()

    def overlay_dropped(self):
        self.overlay.dragging = False
        self.overlay._ensure_canvas()          # back to covering the whole usable screen
        self.overlay.frame_key = None
        self.app.set_logo_pos(list(self.badge.pos))

    def on_key(self, event):
        o = self.overlay
        if not (o and o.panel.isKeyWindow()) or not (event.modifierFlags() & AppKit.NSEventModifierFlagCommand):
            return event
        key = (event.charactersIgnoringModifiers() or "").lower()
        if key == "v":
            pb = AppKit.NSPasteboard.generalPasteboard()
            paths = _file_paths(pb)
            if paths:                                        # files copied in Finder
                self.app.attach(paths)
                return None
            types = pb.types() or []
            if AppKit.NSPasteboardTypeString not in types and (
                    AppKit.NSPasteboardTypePNG in types or AppKit.NSPasteboardTypeTIFF in types):
                data = pb.dataForType_(AppKit.NSPasteboardTypePNG)
                if data is None:                             # screenshots usually come as TIFF
                    rep = AppKit.NSBitmapImageRep.imageRepWithData_(pb.dataForType_(AppKit.NSPasteboardTypeTIFF))
                    data = rep.representationUsingType_properties_(AppKit.NSBitmapImageFileTypePNG, {})
                self.app.attach_image_data(bytes(data))
                return None
        action = {"v": "paste:", "c": "copy:", "x": "cut:", "a": "selectAll:", "z": "undo:"}.get(key)
        if action and AppKit.NSApp.sendAction_to_from_(action, None, None):
            return None
        return event

    # -- sessions menu ------------------------------------------------------------------------

    def _rebuild_sessions(self):
        """The Session submenu: one item per session (ticked = current), plus "New session…"."""
        ss = self.app.sessions
        key = (tuple((n, self.app.runner_for(n).busy(), bool(ss.get(n).get("unread"))) for n in ss.names()),
               ss.current)
        if key == self.sessions_key:
            return
        self.sessions_key = key
        self.sessions_menu.title = f"Session: {ss.current}"
        if len(self.sessions_menu):
            self.sessions_menu.clear()
        for name in ss.names():
            folder = os.path.basename(ss.get(name)["folder"]) or "~"
            label = f"{name}   ({folder})" if folder != name else name
            if name != ss.current and self.app.runner_for(name).busy():
                label += "   · working…"
            elif ss.get(name).get("unread"):
                label += "   · ● new reply"
            item = rumps.MenuItem(label,
                                  callback=lambda _, n=name: threading.Thread(
                                      target=self.app.switch_session, args=(n,), daemon=True).start())
            item.state = int(name == ss.current)
            self.sessions_menu.add(item)
        self.sessions_menu.add(None)
        self.sessions_menu.add(rumps.MenuItem("New session for a folder…", callback=self.pick_folder))

    def pick_folder(self, _):
        AppKit.NSApp.activateIgnoringOtherApps_(True)
        panel = AppKit.NSOpenPanel.openPanel()
        panel.setCanChooseDirectories_(True)
        panel.setCanChooseFiles_(False)
        panel.setPrompt_("Start session")
        panel.setMessage_("Choose the project folder for the new session")
        if panel.runModal() == AppKit.NSModalResponseOK:
            folder = panel.URL().path()
            threading.Thread(target=self.app.new_session_in, args=(folder,), daemon=True).start()

    # -- startup and the two timers ------------------------------------------------------------

    def _first_tick(self):
        AppKit.NSApp.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)  # no Dock icon
        global _ui
        _ui = self
        self.overlay = Overlay()
        if self.app.overlay_w:
            self.overlay.set_width(self.app.overlay_w)
        self.overlay.user_h = self.app.overlay_h
        self.badge = Badge(self.app.logo_size)
        self.badge.pos = tuple(self.app.logo_pos) if self.app.logo_pos else None
        self.sizer = _Sizer.alloc().init()
        self._size_slider()
        self.popover = StatusPopover(self)
        item = self._nsapp.nsstatusitem
        # New menu bar icons go to the far left, the first ones hidden behind a MacBook's notch when
        # space runs out (as it does when macOS's orange mic indicator appears). Start next to the
        # system icons instead; ⌘-dragging it elsewhere is remembered under the same name.
        defaults, key = AppKit.NSUserDefaults.standardUserDefaults(), "NSStatusItem Preferred Position Juniper"
        if defaults.objectForKey_(key) is None:
            defaults.setDouble_forKey_(440.0, key)
        item.setAutosaveName_("Juniper")
        self.classic_menu = item.menu()
        item.setMenu_(None)
        item.button().setTarget_(self.popover.target)
        item.button().setAction_("statusClick:")
        item.button().sendActionOn_(AppKit.NSEventMaskLeftMouseUp | AppKit.NSEventMaskRightMouseUp)
        self.overlay.badge = self.badge
        self.typing = False
        self.app.ui_busy = lambda: self.typing
        # ⌘V/⌘C/⌘X/⌘A/⌘Z in the overlay (an app with no Edit menu doesn't get these for free), and
        # pasting an image or copied files attaches them.
        AppKit.NSEvent.addLocalMonitorForEventsMatchingMask_handler_(AppKit.NSEventMaskKeyDown, self.on_key)
        self.hotkeys = HotKeys()
        self.hotkeys.add(self.cfg["ptt_key"], self.app.ptt_down, self.app.ptt_up)
        self.hotkeys.add(self.cfg["mic_key"], self.toggle_mic)
        self.hotkeys.add(self.cfg["stop_key"], self.app.stop_everything)
        self.hotkeys.add(self.cfg["logo_key"], self.toggle_logo)
        self.hotkeys.add(self.cfg["type_key"], self.open_typing)
        self.started = True
        # macOS can hide the icon without a word: check once things have settled.
        Foundation.NSTimer.scheduledTimerWithTimeInterval_repeats_block_(6, False, lambda t: self.check_icon_shown())

    def icon_shown(self):
        """Is macOS showing the menu bar icon? It hides one when the bar is full (sooner beside a
        MacBook's notch), or when it isn't allowed under System Settings › Menu Bar (macOS 26)."""
        button = self._nsapp.nsstatusitem.button()
        return status_item_shown(button.window() if button else None)

    def check_icon_shown(self, again=True):
        """If the icon isn't showing, say why and how to fix it, and make sure the corner logo is
        there in the meantime (it opens everything the icon does)."""
        from juniper import BUNDLED, get_state, macos_major
        if not AppKit.NSMenu.menuBarVisible():          # hidden menu bar, full-screen app: look later
            Foundation.NSTimer.scheduledTimerWithTimeInterval_repeats_block_(30, False, lambda t: self.check_icon_shown(again))
            return
        shown = self.icon_shown()
        win = self._nsapp.nsstatusitem.button().window()
        if win is not None:      # what macOS says about it, for anyone working out why it's missing
            f = win.frame()
            log("menubar", f"icon {'shown' if shown else 'hidden'}: x={f.origin.x:.0f} width={f.size.width:.0f} "
                           f"visible={bool(win.isVisible())} "
                           f"uncovered={bool(win.occlusionState() & AppKit.NSWindowOcclusionStateVisible)}", "dim")
        else:
            log("menubar", "icon hidden: macOS gave it no window", "dim")
        if shown or get_state().get("menubar_hint_off"):
            return
        if again:                                        # a moment's reshuffle (an icon appearing) isn't worth a dialog
            Foundation.NSTimer.scheduledTimerWithTimeInterval_repeats_block_(4, False, lambda t: self.check_icon_shown(False))
            return
        log("menubar", "macOS isn't showing the menu bar icon", "yellow")
        if not self.app.show_logo:
            self.app.set_show_logo(True)
        owner = "Juniper" if BUNDLED else "Python"      # run from source, the icon belongs to Python
        allow = (f"• It isn't allowed: in System Settings › Menu Bar, under Allow in the Menu Bar, turn on {owner}"
                 + ("" if BUNDLED else " (that's Juniper, running from its source folder)") + ".\n") if macos_major() >= 26 else ""
        alert = AppKit.NSAlert.alloc().init()
        icon = AppKit.NSImage.imageWithSize_flipped_drawingHandler_(
            (64, 64), False, lambda rect: draw_berries(32, 32, 52, ACCENT) or True)
        alert.setIcon_(icon)
        alert.setMessageText_("Juniper's menu bar icon is hidden")
        alert.setInformativeText_(
            "macOS isn't showing it, usually for one of these reasons:\n\n" + allow +
            "• Your menu bar is full: hold ⌘ and drag some other icons out of it, or quit an app you don't need there.\n\n"
            "Until then, click the berries in the corner of your screen to open Juniper, or press "
            f"{key_hint(self.cfg['type_key'])} to type to it.")
        if macos_major() >= 26:
            alert.addButtonWithTitle_("Open Menu Bar Settings")
        alert.addButtonWithTitle_("OK")
        alert.setShowsSuppressionButton_(True)
        alert.suppressionButton().setTitle_("Don't remind me")
        AppKit.NSApp.activateIgnoringOtherApps_(True)
        choice = alert.runModal()
        if alert.suppressionButton().state():
            set_state(menubar_hint_off=True)
        if macos_major() >= 26 and choice == AppKit.NSAlertFirstButtonReturn:
            subprocess.Popen(["open", "x-apple.systempreferences:com.apple.ControlCenter-Settings.extension"])

    def status_clicked(self):
        event = AppKit.NSApp.currentEvent()
        right = event is not None and (event.type() == AppKit.NSEventTypeRightMouseUp
                                       or event.modifierFlags() & AppKit.NSEventModifierFlagControl)
        item = self._nsapp.nsstatusitem
        if right and self.classic_menu is not None:
            self.popover.pop.performClose_(None)
            item.popUpStatusItemMenu_(self.classic_menu)
        else:
            self.popover.toggle(item.button())

    def _set_icon(self, symbol=None, text=None):
        key = (symbol, text)
        if key == self.icon_key:
            return
        self.icon_key = key
        item = self._nsapp.nsstatusitem
        button = item.button()
        button.setImage_(_symbol(symbol) if symbol else None)
        button.setTitle_(text or "")
        # Same width for every icon, so the item doesn't change size and an open popover doesn't
        # jump sideways; it only grows for an unread count.
        extra = math.ceil(button.attributedTitle().size().width) + 4 if text else 0
        item.setLength_(max(self.ICON_W, SYMBOL_BOX[0] + extra))

    def _animate_badge(self):
        s, now, phase = self.app.state, time.time(), self.phase
        lv = min(1.0, self.level * 1.5)
        if phase in ("recording", "followup"):
            colour = AppKit.NSColor.systemRedColor() if phase == "recording" else ACCENT
            glyph, ring, width = "logo", colour.colorWithAlphaComponent_(0.45 + 0.55 * lv), 1.5 + 2 * lv
        elif phase == "busy":
            glyph = f"think:{think_frame(now)}"
            ring, width = ACCENT.colorWithAlphaComponent_(0.15 + 0.2 * (1 + math.sin(now * 3.2)) / 2), 1.4
        elif phase == "speaking":
            glyph, ring, width = "logo", ACCENT.colorWithAlphaComponent_(0.35 + 0.35 * abs(math.sin(now * 4))), 2.0
        else:
            glyph, ring, width = "logo", AppKit.NSColor.colorWithWhite_alpha_(1, 0.12), 1.0
        others = [n for n in self.app.sessions.unread() if n != self.app.sessions.current]
        unseen = s.dismissed and self.app.runner.finished_at > s.dismissed_at
        self.badge.draw(glyph, ring, width, bool(others) or unseen)
        if not (self.overlay and self.overlay.shown):
            # The overlay normally smooths the mic level; do it here when only the logo is up.
            target = s.level if phase in ("recording", "followup") else 0.0
            self.level += (target - self.level) * 0.35

    def tick(self, _):
        if not self.started:
            self._first_tick()
        app, s, now = self.app, self.app.state, time.time()
        # Half-typed messages (open or saved as drafts) hold off an automatic update's restart.
        o = self.overlay
        self.typing = bool(o and (o.text().strip() or any(v.strip() for v in o.drafts.values())))
        while not app.ui_requests.empty():
            req = app.ui_requests.get()
            if req == "sidebar":
                self.overlay.sidebar_open = True
                self.open_typing()
            elif req == "usage":
                self.popover.tab = "usage"
                if not self.popover.shown():
                    self.popover.toggle(self._nsapp.nsstatusitem.button())
        busy, speaking = app.runner.busy() or s.working, app.speaker.speaking()
        approval = app.approvals.current()
        self._rebuild_sessions()
        recording = s.recording and now - s.active_at < self.cfg["max_utterance_seconds"] + self.cfg["listen_seconds"]
        followup = s.followup and not (recording or busy or speaking)

        # Other sessions: finished with an answer you haven't seen, or still working.
        unread = [n for n in app.sessions.unread() if n != app.sessions.current]
        working = [n for n in app.sessions.names() if n != app.sessions.current and n in app.runners
                   and app.runners[n].busy()]
        badge = f" {len(unread)}" if unread else ""
        if recording:
            self._set_icon("juniper:mic.fill", badge)
        elif busy:
            self._set_icon(f"berries:{think_frame(now)}", badge)
        elif speaking:
            self._set_icon("juniper:speaker.wave.2.fill", badge)
        elif followup:
            self._set_icon("juniper:ear", badge)
        else:
            self._set_icon("juniper:waveform" if app.open_mic else "juniper:mic.slash.fill", badge)

        ptt = key_hint(self.cfg["ptt_key"])
        labels = {"recording": "Listening…", "busy": "Thinking…", "speaking": "Speaking", "followup": "Ask a follow-up…"}
        phase = next((k for k, on in (("recording", recording), ("busy", busy), ("speaking", speaking),
                                      ("followup", followup)) if on), None)
        self.status_line.title = (labels[phase] if phase
                                  else f"Listening for “hey {app.cfg['wake_name']}”" if app.open_mic
                                  else f"Mic off: hold {ptt} to talk")
        self.mic_item.state = int(app.open_mic)
        self.logo_item.state = int(app.show_logo)
        self.speak_item.state = int(app.speak_answers)
        self.speak_typed_item.state = int(app.speak_answers and app.speak_typed)
        self.eleven_item.state = int(app.speaker.use_eleven)
        self.popover.refresh()

        # Claude is taking a screenshot (see bin/capture-hook): get out of the picture at once.
        flag = DATA / ".hide-overlay"
        try:
            capturing = time.time() - flag.stat().st_mtime < 30
        except OSError:
            capturing = False
        if capturing:
            for panel in (self.overlay.panel, self.badge.panel):
                if panel.isVisible():
                    panel.orderOut_(None)
            return
        logo = app.show_logo
        self.badge.show(logo)
        if not (self.overlay.morphing or self.overlay.dragging):
            # The overlay always opens from the logo's spot, even while the logo itself is hidden.
            self.overlay.anchor = self.badge.circle_rect()
        last = now if speaking else max(s.active_at, app.speaker.quiet_since)
        turns = app.sessions.turns()
        show = self.cfg["overlay"] and (self.pinned or approval is not None or not s.dismissed and (
            phase is not None or (now - last < self.cfg["overlay_seconds"] and turns)))
        self.phase = phase
        if show:
            status_color = {"recording": AppKit.NSColor.systemRedColor(), "followup": ACCENT,
                            "busy": ACCENT}.get(phase, AppKit.NSColor.secondaryLabelColor())
            status = labels.get(phase, "")
            if not self.pinned:
                turns = [t for i, t in enumerate(turns) if i == len(turns) - 1 or now - t.get("at", 0) < 900]
            if phase == "recording" and turns and now - turns[-1].get("at", 0) > 900:
                turns = []          # picking up an old conversation: don't show its last exchange while listening
            tool = app.runner.tool
            step = f"↳ {tool}" if busy and tool else ""
            if phase == "busy" and tool.startswith("Compacting"):
                status, step = "Compacting…", ""
            if approval:
                status, status_color, step = "Needs your answer", AppKit.NSColor.systemYellowColor(), ""
            waiting = [n for n in app.sessions.names() if n != app.sessions.current and app.approvals.waiting(n)]
            note = "   ".join([f"? {n} needs you" for n in waiting] + [f"✓ {n} finished" for n in unread]
                               + [f"◌ {n} working…" for n in working if n not in waiting])
            sess = app.sessions.get()
            rows = []
            if self.overlay.sidebar_open:
                for n in app.sessions.names():
                    r = app.runners.get(n)
                    st = ("needs you" if app.approvals.waiting(n) else "working" if r and r.busy()
                          else "new reply" if app.sessions.get(n).get("unread") else "")
                    if r and r.queued:
                        st = f"{st} · {len(r.queued)} queued".strip(" ·")
                    if r and r.bg_tasks and not r.busy():
                        st = f"{st} · {len(r.bg_tasks)} running in background".strip(" ·")
                    rows.append((n, os.path.basename(app.sessions.get(n)["folder"]) or "~", st, n == app.sessions.current))
            self.overlay.render(status, status_color, turns, step, phase == "busy" and not approval, app.sessions.current,
                                note, tuple(app.attachments), app.runner.busy() or speaking,
                                app.mode_text(), app.ai_label(),
                                (sess.get("context_used") or 0, sess.get("context_window") or 0), approval,
                                self.overlay.slash_candidates(app.slash_commands, app.skills), rows,
                                tuple(s for _, _, s in app.runner.queued))
        self.overlay.fade_to(show)
        if self.want_focus and self.overlay.shown:
            self.want_focus = False
            self.overlay.focus()

    def animate(self, _):
        """30 times a second: the corner logo, and (while it's up) the overlay's bars, spinner and border."""
        if self.badge and self.badge.shown:
            self._animate_badge()
        if self.started:
            self.popover.follow(self._nsapp.nsstatusitem.button())
        if not (self.overlay and self.overlay.shown):
            return
        s, now, phase, o = self.app.state, time.time(), self.phase, self.overlay
        o.show_recording(self.app.recording_now(), self.level)
        target = s.level if phase in ("recording", "followup") else 0.0
        self.level += (target - self.level) * 0.35          # smooth the mic level so the bars glide
        lv = self.level
        red = AppKit.NSColor.systemRedColor()
        if phase in ("recording", "followup"):
            colour = red if phase == "recording" else ACCENT
            levels = [min(1.0, 0.14 + lv * 1.7 * (0.5 + 0.5 * math.sin(now * 10 + i * 1.4) ** 2)) for i in range(5)]
            o.set_border(colour.colorWithAlphaComponent_(0.18 + 0.55 * min(1.0, lv * 1.5)), 1.0 + 0.8 * min(1.0, lv * 1.5))
        elif phase == "speaking":
            colour = ACCENT
            levels = [0.25 + 0.55 * abs(math.sin(now * 5.5 + i * 0.9)) for i in range(5)]
            o.set_border(ACCENT.colorWithAlphaComponent_(0.22))
        elif phase == "busy":
            colour = AppKit.NSColor.tertiaryLabelColor()
            levels = [0.15 + 0.12 * (1 + math.sin(now * 4 - i * 0.8)) / 2 for i in range(5)]
            o.set_border(ACCENT.colorWithAlphaComponent_(0.12 + 0.16 * (1 + math.sin(now * 3.2)) / 2))
            o.spinner.show(f"think:{think_frame(now)}")
        else:
            colour, levels = AppKit.NSColor.tertiaryLabelColor(), [0.15] * 5
            o.set_border(AppKit.NSColor.colorWithWhite_alpha_(1, 0.10))
        o.bars.show(levels, colour)


def run(app):
    AppKit.NSApplication.sharedApplication().setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)
    MenuBar(app).run()
