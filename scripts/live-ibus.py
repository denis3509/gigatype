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
        super().__init__(**kwargs)
        self.controller = controller
        self.focused = False
        self.text = ""

    def preview(self, value):
        self.text = value
        text = IBus.Text.new_from_string(value)
        text.append_attribute(IBus.AttrType.UNDERLINE, IBus.AttrUnderline.SINGLE, 0, len(value))
        self.update_preedit_text_with_mode(
            text, len(value), bool(value), IBus.PreeditFocusMode.CLEAR
        )

    def do_focus_in(self):
        self.focused = True
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
        return bool(self.active and self.engine and self.engine.focused and self.same_context())

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
            self.engine.preview("")
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
                    self.engine.preview(value)
                else:
                    self.engine.preview("")
                    if value:
                        self.engine.commit_text(IBus.Text.new_from_string(value))
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
