# GigaType

[![CI](https://github.com/kpoxo6op/gigatype/actions/workflows/ci.yml/badge.svg)](https://github.com/kpoxo6op/gigatype/actions/workflows/ci.yml)

GigaType is local Russian voice typing for Unix desktops. Press `F9`, speak, then press `F9` again to insert the transcription into the focused app.

It runs [GigaAM v3](https://huggingface.co/ai-sage/GigaAM-v3) on your computer. Dictation does not use a cloud API, account, token, or Python runtime.

## Install

```bash
git clone https://github.com/kpoxo6op/gigatype.git
cd gigatype
./scripts/install.sh
```

The installer downloads the checked model files, builds GigaType, configures autostart, and requests any desktop permissions it needs.

GigaType supports Linux on Wayland or X11, macOS, and BSD desktops running X11. Run this after installation to check your setup:

```bash
gigatype doctor
```

## Use

- `F9`: start listening; press again to stop, transcribe, and insert
- `Shift+F9`: cancel the current recording

Useful commands:

```bash
gigatype status
gigatype microphones
gigatype transcribe-file recording.wav
gigatype authorize
```

On macOS, allow GigaType access to the microphone and Accessibility controls when prompted. Wayland permission is handled by `gigatype authorize` and the installer.

## Live dictation on GNOME/X11

The `codex/live-dictation` branch adds inline, revisable dictation through IBus.
The installed systemd service enables it automatically on GNOME/X11. Other
desktops keep the original record-then-insert mode. Requires `/usr/bin/python3`,
`python3-gi`, `gir1.2-ibus-1.0`, a running IBus session, and an IBus-aware text field.

- Focus a text field and press **F9**. An underlined draft appears while speaking
  and is revised as more audio arrives.
- After a pause (800 ms by default), the phrase is committed and dictation continues.
- Press **F9** again to finish the final phrase, or **Shift+F9** to discard the
  current uncommitted phrase. Phrases already committed remain in the document.
- Changing the input context, typing manually, or moving the cursor stops dictation
  and clears the uncommitted draft. The previous keyboard engine is restored.
- Apps that do not expose an IBus input context cannot use this mode. Set
  `GIGATYPE_LIVE=0` in a systemd user override to use the original clipboard mode.

This uses repeated recognition of the accumulated phrase with the existing local
GigaAM v3 ONNX model, rather than a native streaming encoder. Updates are requested
every 900 ms; actual latency also includes inference time. Long continuous speech
is split after 18 seconds to bound the amount reprocessed. Final phrases are saved
in the existing transcript history; intermediate drafts are not saved.

Optional service environment settings: `GIGATYPE_LIVE_INTERVAL_MS` (250–5000 ms),
`GIGATYPE_LIVE_PAUSE_MS` (300–3000 ms), and `GIGATYPE_LIVE_SILENCE_RMS` (positive
normalized microphone RMS, default `80/32768`). Microphone selection still uses
`GIGATYPE_MICROPHONE`. Explicitly set `GIGATYPE_LIVE=1` to request live mode;
`auto` selects it only on GNOME/X11.
The existing 120-second session limit is retained; `GIGATYPE_MAX_RECORDING_MS`
can change it. At the limit, the last phrase is finalized automatically.

## Local data

- Model: `~/.local/share/gigatype/models/`
- Transcript history: `~/.local/share/gigatype/history.jsonl`
- Temporary audio: deleted after transcription or cancellation

The model is downloaded once. Normal dictation works offline.

## Testing

GitHub-hosted E2E tests the real GigaAM model through a virtual microphone, checks that the beginning and end of the recording are preserved, inserts the result into a focused X11 editor, and verifies clipboard restoration.

CI also holds a fake clipboard transfer open indefinitely and verifies that GigaType times out, inserts without preserving that clipboard, returns to idle, and accepts the next `F9`.

It does not test a physical microphone or KDE Wayland. Those require an interactive machine.

## Remove

```bash
./scripts/uninstall.sh
```

The uninstaller removes GigaType and its local services. It keeps the downloaded model so it does not need to be downloaded again if you reinstall.
