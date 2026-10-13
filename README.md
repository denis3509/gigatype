# GigaType — vibe-coded fork

[![Upstream CI](https://github.com/kpoxo6op/gigatype/actions/workflows/ci.yml/badge.svg)](https://github.com/kpoxo6op/gigatype/actions/workflows/ci.yml)

This is a **vibe-coded fork** of [kpoxo6op/gigatype](https://github.com/kpoxo6op/gigatype), developed iteratively with Codex. It adds live, revisable Russian dictation through IBus on GNOME/X11, confidence-filtered previews, context-aware spacing and casing, character-by-character additions, immediate corrections, and a status indicator beside the caret. The original project and its Git history are preserved.

GigaType is local Russian voice typing for Unix desktops. Press `F9`, speak, then press `F9` again to insert the transcription into the focused app.

It runs [GigaAM v3](https://huggingface.co/ai-sage/GigaAM-v3) on your computer. Speech recognition runs locally in Rust without a cloud API, account, or token. The GNOME live-dictation interface uses a small Python IBus/GTK bridge.

## Install

```bash
git clone https://github.com/denis3509/gigatype.git
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
- A small green **Диктовка** badge follows the text caret without taking focus or
  receiving mouse clicks. When F9 stops recording, it turns amber and shows
  **Обработка** until the final phrase is processed. It disappears on completion,
  cancellation, or a focus change. If a client omits caret coordinates, it appears
  near the mouse pointer instead. `GIGATYPE_LIVE_INDICATOR=0` hides the badge.
- After a pause (1800 ms by default), the phrase is committed and dictation continues.
- Press **F9** again to finish the final phrase, or **Shift+F9** to discard the
  current uncommitted phrase. Phrases already committed remain in the document.
- Changing the input context, typing manually, or moving the cursor stops dictation
  and clears the uncommitted draft. The previous keyboard engine is restored.
- Apps that do not expose an IBus input context cannot use this mode. Set
  `GIGATYPE_LIVE=0` in a systemd user override to use the original clipboard mode.

This uses repeated recognition of the accumulated phrase with the existing local
GigaAM v3 ONNX model, rather than a native streaming encoder. Updates are requested
every 500 ms after at least 1200 ms of phrase audio; actual latency also includes
inference time and confirmation. Only the common prefix of two consecutive
confidence-filtered hypotheses is previewed. The first token below the configured
posterior threshold holds back the remaining draft until it becomes more reliable
or final recognition completes. Final recognition retains the entire result.
These token posteriors are not calibrated probabilities of correctness: a stable,
high-scoring hypothesis may still be wrong.

The confirmed draft is revealed one Unicode character at a time with a default
12 ms interval, speeding up for long additions. Corrections preserve the common
prefix. A correction or shortened hypothesis replaces the displayed draft in one
update; only a newly added suffix is animated. This is presentation smoothing of available hypotheses, not a new streaming
speech model. `GIGATYPE_LIVE_CHARACTER_MS=0` disables the animation.

Where the app supplies IBus surrounding text, the preceding 512 characters are
used locally for spacing and sentence case. This text is not logged or sent to
the acoustic model. The current model has no document prompt input; semantic
conditioning on previously typed text is not implemented. Pauses no longer force
a period; punctuation is taken from the model or spoken punctuation commands.
Long continuous speech
is split after 18 seconds to bound the amount reprocessed. Final phrases are saved
in the existing transcript history; intermediate drafts are not saved.

Optional service environment settings: `GIGATYPE_LIVE_INTERVAL_MS` (250–5000 ms),
`GIGATYPE_LIVE_PAUSE_MS` (300–5000 ms), and `GIGATYPE_LIVE_SILENCE_RMS` (positive
normalized microphone RMS, default `80/32768`). Microphone selection still uses
`GIGATYPE_MICROPHONE`. Explicitly set `GIGATYPE_LIVE=1` to request live mode;
`auto` selects it only on GNOME/X11.
The existing 120-second session limit is retained; `GIGATYPE_MAX_RECORDING_MS`
can change it. At the limit, the last phrase is finalized automatically.

Preview tuning: `GIGATYPE_LIVE_INITIAL_MS` (300–5000, default 1200),
`GIGATYPE_LIVE_CONFIRMATIONS` (1–4, default 2),
`GIGATYPE_LIVE_MIN_CONFIDENCE` (0–1, default 0.55), and
`GIGATYPE_LIVE_CHARACTER_MS` (0–80, default 12). More confirmations or a higher
threshold delay more uncertain drafts; lowering them makes previews faster and
more prone to revisions. Settings can be added to a systemd user service override.

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
