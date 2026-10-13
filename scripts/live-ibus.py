#!/usr/bin/env python3
"""IBus composition bridge. Only the dictation's original input context is edited."""

import json
import math
import os
from pathlib import Path
import signal
import socket
import threading
import time

import gi

gi.require_version("IBus", "1.0")
from gi.repository import GLib, IBus

ENGINE_NAME = "gigatype-live"
IBus.init()


class CaretIndicator:
    """An unfocusable, click-through status badge anchored to the IBus caret."""

    WIDTH = 146
    HEIGHT = 30

    def __init__(self):
        self.window = None
        self.rect = None
        self.state = None
        self.timer = 0
        if os.environ.get("GIGATYPE_LIVE_INDICATOR") == "0":
            return
        try:
            gi.require_version("Gtk", "3.0")
            gi.require_version("Gdk", "3.0")
            gi.require_foreign("cairo")
            from gi.repository import Gtk, Gdk
            import cairo
            available, _args = Gtk.init_check([])
            if not available:
                return
        except (ImportError, ValueError):
            return
        self.gdk = Gdk
        self.cairo = cairo
        self.window = Gtk.Window(type=Gtk.WindowType.POPUP)
        self.window.set_title("GigaType dictation status")
        self.window.set_decorated(False)
        self.window.set_accept_focus(False)
        self.window.set_focus_on_map(False)
        self.window.set_keep_above(True)
        self.window.set_skip_taskbar_hint(True)
        self.window.set_skip_pager_hint(True)
        self.window.set_resizable(False)
        self.window.set_app_paintable(True)
        self.window.set_type_hint(Gdk.WindowTypeHint.TOOLTIP)
        visual = self.window.get_screen().get_rgba_visual()
        if visual:
            self.window.set_visual(visual)
        self.area = Gtk.DrawingArea()
        self.area.set_can_focus(False)
        self.area.set_size_request(self.WIDTH, self.HEIGHT)
        self.area.connect("draw", self.draw)
        self.window.add(self.area)
        self.window.connect("realize", self.click_through)

    def click_through(self, window):
        native = window.get_window()
        if native:
            native.set_accept_focus(False)
            native.set_pass_through(True)
            native.input_shape_combine_region(self.cairo.Region(), 0, 0)

    def place(self, rect, physical=True):
        self.rect = rect
        if not self.window:
            return
        x, y, width, height = rect
        if physical:
            # IBus uses X11 root pixels; GTK window positions use logical pixels.
            scale = max(1, self.window.get_scale_factor())
            x, y, width, height = (round(value / scale) for value in rect)
        display = self.window.get_display()
        monitor = display.get_monitor_at_point(x, y)
        if not monitor:
            monitor = display.get_primary_monitor()
        if not monitor:
            return
        bounds = monitor.get_workarea()
        left = min(max(bounds.x + 4, x + max(0, width) + 8),
                   bounds.x + bounds.width - self.WIDTH - 4)
        top = y - self.HEIGHT - 7
        if top < bounds.y + 4:
            top = y + max(16, height) + 7
        top = min(max(bounds.y + 4, top), bounds.y + bounds.height - self.HEIGHT - 4)
        self.window.move(left, top)

    def show(self, state, rect=None):
        if not self.window:
            return
        self.state = state
        if rect is not None:
            self.place(rect)
        elif self.rect is None:
            # A few clients omit caret geometry; still show recording by the pointer.
            pointer = self.window.get_display().get_default_seat().get_pointer()
            _screen, x, y = pointer.get_position()
            self.place((x, y, 0, 16), physical=False)
        self.window.show_all()
        native = self.window.get_window()
        if native:
            native.raise_()
        self.area.queue_draw()
        if not self.timer:
            self.timer = GLib.timeout_add(50, self.tick)

    def hide(self):
        self.state = None
        self.rect = None
        if self.timer:
            GLib.source_remove(self.timer)
            self.timer = 0
        if self.window:
            self.window.hide()

    def tick(self):
        if not self.state or not self.window:
            self.timer = 0
            return False
        self.area.queue_draw()
        return True

    def draw(self, _area, cr):
        cr.set_operator(self.cairo.OPERATOR_SOURCE)
        cr.set_source_rgba(0, 0, 0, 0)
        cr.paint()
        cr.set_operator(self.cairo.OPERATOR_OVER)
        radius = 12
        for x, y, start in [(self.WIDTH - 2 - radius, 2 + radius, -math.pi / 2),
                            (self.WIDTH - 2 - radius, self.HEIGHT - 2 - radius, 0),
                            (2 + radius, self.HEIGHT - 2 - radius, math.pi / 2),
                            (2 + radius, 2 + radius, math.pi)]:
            cr.arc(x, y, radius, start, start + math.pi / 2)
        cr.close_path()
        cr.set_source_rgba(0.08, 0.10, 0.14, 0.96)
        cr.fill_preserve()
        cr.set_line_width(1)
        cr.set_source_rgba(0.38, 0.47, 0.59, 0.55)
        cr.stroke()
        processing = self.state == "finishing"
        color = (1.0, 0.75, 0.33) if processing else (0.30, 0.88, 0.68)
        phase = time.monotonic() * 4
        if processing:
            cr.set_line_width(2)
            cr.arc(16, 15, 7, phase, phase + math.pi * 1.4)
            cr.set_source_rgba(*color, 0.9)
            cr.stroke()
        else:
            cr.arc(16, 15, 7 + math.sin(phase), 0, math.tau)
            cr.set_source_rgba(*color, 0.16 + (math.sin(phase) + 1) * 0.06)
            cr.fill()
        cr.arc(16, 15, 4, 0, math.tau)
        cr.set_source_rgba(*color, 1)
        cr.fill()
        cr.select_font_face("Sans", self.cairo.FONT_SLANT_NORMAL, self.cairo.FONT_WEIGHT_NORMAL)
        cr.set_font_size(11)
        cr.set_source_rgba(0.95, 0.97, 1, 1)
        cr.move_to(29, 19)
        cr.show_text("Индикатор" if self.state == "preview" else "Обработка" if processing else "Диктовка")
        cr.set_font_size(9)
        cr.set_source_rgba(0.62, 0.70, 0.80, 1)
        cr.move_to(123, 18)
        cr.show_text("F9")
        return False


class DictationEngine(IBus.Engine):
    def __init__(self, controller, **kwargs):
        super().__init__(active_surrounding_text=True, **kwargs)
        self.controller = controller
        self.focused = False
        self.text = ""
        self.target = ""
        self.animation = 0
        self.before = ""
        self.surrounding_before = None
        self.composition_started = False
        self.cursor_rect = None

    def do_set_cursor_location(self, x, y, width, height):
        self.cursor_rect = (x, y, width, height)
        if self.controller.active and self.controller.engine is self:
            self.controller.indicator.place(self.cursor_rect)

    def stop_animation(self):
        if self.animation:
            GLib.source_remove(self.animation)
            self.animation = 0

    def prepare(self, value):
        if not value:
            return ""
        before = self.before.rstrip()
        boundary = before.rstrip('\"\'»”)]')
        sentence_start = not before or self.before.rstrip(" \t").endswith("\n") or boundary.endswith(tuple(".!?…"))
        first_word = value.split(None, 1)[0]
        # Preserve acronyms. GigaAM often capitalizes every isolated audio segment.
        if value[0].isalpha() and sum(char.isupper() for char in first_word) < 2:
            value = (value[0].upper() if sentence_start else value[0].lower()) + value[1:]
        separator = ""
        if self.before and not self.before[-1].isspace() and value[0] not in ".,!?…:;)]}»”\n":
            separator = " "
        return separator + value

    def animate(self, value):
        target = self.prepare(value)
        if target == self.target:
            return
        self.composition_started = True
        self.stop_animation()
        correction = not target.startswith(self.text) or (self.target and not target.startswith(self.target))
        self.target = target
        if correction:
            # Replace a revised hypothesis atomically; only new suffixes animate.
            self.preview(target)
            return
        common = 0
        for left, right in zip(self.text, target):
            if left != right:
                break
            common += 1
        if common < len(self.text):
            self.preview(target[:common])
        remaining = len(target) - common
        try:
            character_ms = max(0, min(80, int(os.environ.get("GIGATYPE_LIVE_CHARACTER_MS", "12"))))
        except ValueError:
            character_ms = 12
        if not remaining or not character_ms:
            self.preview(target)
            return
        # Reveal one Unicode character at a time, catching up within ~400 ms.
        delay = max(2, min(character_ms, 400 // remaining))

        def step():
            if not self.controller.valid():
                self.animation = 0
                return False
            self.preview(self.target[:len(self.text) + 1])
            if self.text == self.target:
                self.animation = 0
                return False
            return True

        self.animation = GLib.timeout_add(delay, step)

    def clear(self):
        self.stop_animation()
        self.target = ""
        self.preview("")

    def begin_context(self):
        self.clear()
        self.composition_started = False
        self.before = self.surrounding_before or ""
        self.get_surrounding_text()

    def do_set_surrounding_text(self, text, cursor_pos, anchor_pos):
        value = text.get_text()
        start = min(cursor_pos, anchor_pos, len(value))
        self.surrounding_before = value[:start][-512:]
        if self.controller.active and not self.composition_started:
            self.before = self.surrounding_before

    def preview(self, value):
        self.text = value
        if self.get_connection() is None:
            return
        text = IBus.Text.new_from_string(value)
        text.append_attribute(IBus.AttrType.UNDERLINE, IBus.AttrUnderline.SINGLE, 0, len(value))
        self.update_preedit_text_with_mode(
            text, len(value), bool(value), IBus.PreeditFocusMode.CLEAR
        )

    def do_focus_in(self):
        self.focused = True
        self.surrounding_before = None
        self.get_surrounding_text()
        self.controller.engine = self
        if self.controller.active and not self.controller.same_context():
            self.controller.cancel()

    def do_focus_out(self):
        self.focused = False
        if self.controller.active:
            self.controller.cancel()

    def do_reset(self):
        if self.controller.active:
            self.controller.cancel()

    def do_disable(self):
        self.focused = False
        if self.controller.active:
            self.controller.cancel()

    def do_process_key_event(self, keyval, keycode, state):
        # Let ordinary keyboard input through, but stop revising dictation first.
        modifiers = {IBus.KEY_Shift_L, IBus.KEY_Shift_R, IBus.KEY_Control_L,
                     IBus.KEY_Control_R, IBus.KEY_Alt_L, IBus.KEY_Alt_R,
                     IBus.KEY_Super_L, IBus.KEY_Super_R, IBus.KEY_F9}
        if not state & IBus.ModifierType.RELEASE_MASK and keyval not in modifiers:
            if self.controller.active:
                self.controller.cancel()
        return False


class Controller:
    def __init__(self):
        self.bus = IBus.Bus()
        if not self.bus.is_connected():
            raise RuntimeError("IBus is not connected to the desktop session")
        self.engine = None
        self.engine_count = 0
        self.active = False
        self.switching = False
        self.previous_engine = None
        self.context = None
        self.loop = GLib.MainLoop()
        self.indicator = CaretIndicator()
        self.factory = IBus.Factory.new(self.bus.get_connection())
        self.factory.connect("create-engine", self.create_engine)
        self.bus.connect("disconnected", lambda *_: self.loop.quit())
        component = IBus.Component.new(
            "org.freedesktop.IBus.GigaTypeLive", "GigaType live dictation", "0.3.0",
            "MIT", "GigaType", "https://github.com/kpoxo6op/gigatype", "", "gigatype"
        )
        component.add_engine(IBus.EngineDesc.new(
            ENGINE_NAME, "GigaType live dictation", "Local GigaAM dictation", "ru",
            "MIT", "GigaType", "audio-input-microphone", "default"
        ))
        if not self.bus.register_component(component):
            raise RuntimeError("Could not register the GigaType input method")
        self.component = component

    def create_engine(self, factory, name):
        if name != ENGINE_NAME:
            raise RuntimeError(f"Unknown engine {name}")
        self.engine_count += 1
        self.engine = DictationEngine(
            self, connection=self.bus.get_connection(),
            object_path=f"/org/freedesktop/IBus/GigaTypeLive/Engine{self.engine_count}"
        )
        return self.engine

    def same_context(self):
        return bool(self.context and self.bus.current_input_context() == self.context)

    def valid(self):
        return bool(self.active and self.engine and self.engine.get_connection()
                    and self.engine.focused and self.same_context())

    def restore(self):
        previous, self.previous_engine = self.previous_engine, None
        current = self.bus.get_global_engine()
        if previous and current and current.get_name() == ENGINE_NAME:
            self.switching = True

            def restored(bus, result, _data):
                try:
                    bus.set_global_engine_async_finish(result)
                finally:
                    self.switching = False

            self.bus.set_global_engine_async(previous, 1000, None, restored, None)
        return False

    def cancel(self):
        self.active = False
        self.indicator.hide()
        if self.engine:
            self.engine.clear()
        GLib.idle_add(self.restore)

    def handle(self, request, reply):
        try:
            op = request["op"]
            if op == "ping":
                reply({"ok": True,
                       "indicator_enabled": self.indicator.window is not None,
                       "indicator_visible": bool(self.indicator.window and self.indicator.window.get_visible()),
                       "indicator_position": self.indicator.window.get_position() if self.indicator.window else None,
                       "caret": self.engine.cursor_rect if self.engine else None})
            elif op == "preview-indicator":
                if self.active or self.switching:
                    raise RuntimeError("Cannot preview the indicator during dictation")
                self.indicator.show("preview")

                def hide_preview():
                    if self.indicator.state == "preview":
                        self.indicator.hide()
                    return False

                GLib.timeout_add(15000, hide_preview)
                reply({"ok": True, "visible": bool(self.indicator.window and self.indicator.window.get_visible())})
            elif op == "hide-preview":
                if self.indicator.state == "preview":
                    self.indicator.hide()
                reply({"ok": True})
            elif op == "begin":
                if self.active or self.switching:
                    raise RuntimeError("A dictation session is already active")
                self.context = self.bus.current_input_context()
                if not self.context:
                    raise RuntimeError("Focus a text field before starting dictation")
                current = self.bus.get_global_engine()
                self.previous_engine = current.get_name() if current else "xkb:us::eng"
                self.switching = True

                def ready(bus, result, _data):
                    try:
                        if not bus.set_global_engine_async_finish(result):
                            raise RuntimeError("Could not activate GigaType's input method")

                        def focused():
                            self.switching = False
                            self.active = bool(self.engine and self.engine.focused and self.same_context())
                            if not self.active:
                                self.cancel()
                                reply({"ok": False, "error": "This field does not expose an IBus input context"})
                            else:
                                self.engine.begin_context()
                                self.indicator.show("recording", self.engine.cursor_rect)
                                reply({"ok": True})
                            return False

                        GLib.timeout_add(100, focused)
                    except Exception as exc:
                        self.switching = False
                        self.cancel()
                        reply({"ok": False, "error": str(exc)})

                self.bus.set_global_engine_async(ENGINE_NAME, 1500, None, ready, None)
            elif op == "state":
                reply({"ok": True, "active": self.valid()})
            elif op == "indicator":
                if not self.valid():
                    raise RuntimeError("The original text field is no longer focused")
                state = request.get("text", "recording")
                if state not in {"recording", "finishing"}:
                    raise RuntimeError("Unknown dictation indicator state")
                self.indicator.show(state, self.engine.cursor_rect)
                reply({"ok": True})
            elif op in {"preview", "commit"}:
                if not self.valid():
                    raise RuntimeError("The original text field is no longer focused")
                value = request.get("text", "")
                if op == "preview":
                    self.engine.animate(value)
                else:
                    value = self.engine.prepare(value)
                    self.engine.composition_started = True
                    self.engine.clear()
                    if value:
                        self.engine.commit_text(IBus.Text.new_from_string(value))
                        self.engine.before = (self.engine.before + value)[-512:]
                reply({"ok": True})
            elif op in {"cancel", "end", "shutdown"}:
                self.cancel()
                reply({"ok": True})
                if op == "shutdown":
                    GLib.timeout_add(250, lambda: (self.loop.quit(), False)[1])
            else:
                raise RuntimeError(f"Unknown operation {op}")
        except Exception as exc:
            reply({"ok": False, "error": str(exc)})
        return False


def main():
    controller = Controller()
    path = Path(os.environ.get("XDG_RUNTIME_DIR", f"/tmp/gigatype-{os.getuid()}")) / "gigatype-live-ibus.sock"
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.unlink(missing_ok=True)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(path))
    os.chmod(path, 0o600)
    server.listen(8)

    def serve_connection(conn):
        try:
            conn.settimeout(4)
            data = bytearray()
            while b"\n" not in data:
                chunk = conn.recv(4096)
                if not chunk or len(data) > 1024 * 1024:
                    return
                data.extend(chunk)
            request = json.loads(data.split(b"\n", 1)[0])
            event = threading.Event()
            response = []

            def reply(value):
                response.append(value)
                event.set()

            GLib.idle_add(controller.handle, request, reply)
            if event.wait(4):
                conn.sendall((json.dumps(response[0], ensure_ascii=False) + "\n").encode())
        except (OSError, ValueError):
            pass
        finally:
            conn.close()

    def accept():
        while True:
            try:
                conn, _ = server.accept()
            except OSError:
                return
            threading.Thread(target=serve_connection, args=(conn,), daemon=True).start()

    def shutdown(*_args):
        GLib.idle_add(controller.handle, {"op": "shutdown"}, lambda _reply: None)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    threading.Thread(target=accept, daemon=True).start()
    try:
        controller.loop.run()
    finally:
        server.close()
        path.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
