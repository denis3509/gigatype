#!/usr/bin/env python3
"""IBus composition bridge. Only the dictation's original input context is edited."""

import json
import os
from pathlib import Path
import signal
import socket
import threading

import gi

gi.require_version("IBus", "1.0")
from gi.repository import GLib, IBus

ENGINE_NAME = "gigatype-live"
IBus.init()


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
        self.target = target
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
        if self.engine:
            self.engine.clear()
        GLib.idle_add(self.restore)

    def handle(self, request, reply):
        try:
            op = request["op"]
            if op == "ping":
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
