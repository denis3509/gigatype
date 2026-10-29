// SPDX-License-Identifier: MIT
import Clutter from 'gi://Clutter';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import St from 'gi://St';
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as PanelMenu from 'resource:///org/gnome/shell/ui/panelMenu.js';

const STATES = {
    idle: ['audio-input-microphone-symbolic', '', 'Диктовка готова — F9', 'idle'],
    recording: ['media-record-symbolic', 'Диктовка', 'Идёт диктовка — F9 завершить', 'recording'],
    transcribing: ['view-refresh-symbolic', 'Обработка', 'Распознавание речи', 'processing'],
    offline: ['microphone-disabled-symbolic', '', 'Служба GigaType недоступна', 'offline'],
};

export default class GigaTypeStatus extends Extension {
    enable() {
        const run = {active: true, polling: false, cancel: null, deadline: 0, state: null};
        this._run = run;
        this._indicator = new PanelMenu.Button(0.0, 'GigaType', true);
        // Opening a menu or taking focus could itself cancel live dictation.
        this._indicator.reactive = false;
        this._indicator.can_focus = false;
        this._box = new St.BoxLayout({style_class: 'gigatype-status'});
        this._icon = new St.Icon({style_class: 'system-status-icon'});
        this._label = new St.Label({y_align: Clutter.ActorAlign.CENTER});
        this._box.add_child(this._icon);
        this._box.add_child(this._label);
        this._indicator.add_child(this._box);
        Main.panel.addToStatusArea(this.uuid, this._indicator, 0, 'right');
        this._display(run, 'offline');
        this._poll(run);
        run.timer = GLib.timeout_add(GLib.PRIORITY_DEFAULT, 500, () => {
            this._poll(run);
            return GLib.SOURCE_CONTINUE;
        });
    }

    _display(run, state) {
        if (!run.active || run.state === state)
            return;
        run.state = state;
        const [icon, label, description, style] = STATES[state];
        this._icon.icon_name = icon;
        this._label.text = label;
        this._label.visible = Boolean(label);
        this._indicator.accessible_name = description;
        this._box.set_style_class_name(`gigatype-status gigatype-${style}`);
    }

    async _poll(run) {
        if (!run.active || run.polling)
            return;
        run.polling = true;
        const cancellable = new Gio.Cancellable();
        run.cancel = cancellable;
        run.deadline = GLib.timeout_add(GLib.PRIORITY_DEFAULT, 1500, () => {
            run.deadline = 0;
            cancellable.cancel();
            return GLib.SOURCE_REMOVE;
        });
        let connection = null;
        try {
            const client = new Gio.SocketClient();
            const address = new Gio.UnixSocketAddress({
                path: GLib.build_filenamev([GLib.get_user_runtime_dir(), 'gigatype.sock']),
            });
            connection = await new Promise((resolve, reject) => {
                client.connect_async(address, cancellable, (source, result) => {
                    try { resolve(source.connect_finish(result)); } catch (error) { reject(error); }
                });
            });
            const output = connection.get_output_stream();
            await new Promise((resolve, reject) => {
                output.write_all_async(new TextEncoder().encode('status\n'), GLib.PRIORITY_DEFAULT,
                    cancellable, (source, result) => {
                        try { resolve(source.write_all_finish(result)); } catch (error) { reject(error); }
                    });
            });
            // Bound the response even if a different process owns the socket.
            const input = connection.get_input_stream();
            let response = '';
            while (!response.includes('\n') && response.length < 128) {
                const bytes = await new Promise((resolve, reject) => {
                    input.read_bytes_async(128 - response.length, GLib.PRIORITY_DEFAULT,
                        cancellable, (source, result) => {
                            try { resolve(source.read_bytes_finish(result)); } catch (error) { reject(error); }
                        });
                });
                if (!bytes.get_size())
                    break;
                response += new TextDecoder().decode(bytes.get_data());
            }
            const state = response.trim().split(/\s+/)[0];
            this._display(run, Object.hasOwn(STATES, state) ? state : 'offline');
        } catch (_error) {
            // A stopped/restarting daemon is normal, not a notification-worthy error.
            this._display(run, 'offline');
        } finally {
            if (run.deadline) {
                GLib.Source.remove(run.deadline);
                run.deadline = 0;
            }
            if (connection) {
                connection.close_async(GLib.PRIORITY_DEFAULT, null, (source, result) => {
                    try { source.close_finish(result); } catch (_error) { /* Already closed. */ }
                });
            }
            run.cancel = null;
            run.polling = false;
        }
    }

    disable() {
        const run = this._run;
        if (run) {
            run.active = false;
            if (run.timer)
                GLib.Source.remove(run.timer);
            if (run.deadline) {
                GLib.Source.remove(run.deadline);
                run.deadline = 0;
            }
            run.cancel?.cancel();
        }
        this._indicator?.destroy();
        this._indicator = null;
        this._box = null;
        this._icon = null;
        this._label = null;
        this._run = null;
    }
}
