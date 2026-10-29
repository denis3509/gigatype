//! Incremental GigaAM recognition with reversible IBus composition on GNOME.

use super::{audio, dirs_home, model, notify, runtime_dir, save_history,
    socket_path, MediaGuard, SoundCue, SuspendDetector, play_sound};
use std::collections::VecDeque;
use std::env;
use std::io::{BufRead, BufReader, Read, Write};
use std::os::unix::fs::PermissionsExt;
use std::os::unix::net::{UnixListener, UnixStream};
use std::process::{Child, Command};
use std::sync::{atomic::{AtomicBool, Ordering}, mpsc};
use std::thread;
use std::time::{Duration, Instant};

const RATE: usize = 16_000;
static STOP: AtomicBool = AtomicBool::new(false);

extern "C" fn stop_signal(_: libc::c_int) {
    STOP.store(true, Ordering::Relaxed);
}

pub fn enabled() -> bool {
    match env::var("GIGATYPE_LIVE").as_deref() {
        Ok("1") => true,
        Ok("auto") => env::var("XDG_CURRENT_DESKTOP")
            .is_ok_and(|value| value.to_ascii_lowercase().contains("gnome"))
            && env::var_os("DISPLAY").is_some()
            && env::var_os("WAYLAND_DISPLAY").is_none(),
        _ => false,
    }
}

struct Bridge {
    process: Child,
}

impl Bridge {
    fn start() -> Result<Self, String> {
        let script = env::var_os("GIGATYPE_IBUS_BRIDGE")
            .map(std::path::PathBuf::from)
            .unwrap_or_else(|| dirs_home().join(".local/share/gigatype/live-ibus.py"));
        let process = Command::new("/usr/bin/python3").arg(script).spawn()
            .map_err(|error| format!("could not start IBus bridge: {error}"))?;
        let mut bridge = Self { process };
        let deadline = Instant::now() + Duration::from_secs(5);
        while Instant::now() < deadline {
            if bridge.process.try_wait().map_err(|error| error.to_string())?.is_some() {
                return Err("IBus bridge exited; install python3-gi and gir1.2-ibus-1.0".into());
            }
            if bridge.request("ping", "").is_ok() {
                return Ok(bridge);
            }
            thread::sleep(Duration::from_millis(100));
        }
        Err("IBus bridge did not become ready".into())
    }

    fn request(&self, op: &str, text: &str) -> Result<serde_json::Value, String> {
        self.request_for_session(op, text, None)
    }

    fn request_for_session(&self, op: &str, text: &str, session: Option<u64>) -> Result<serde_json::Value, String> {
        let mut stream = UnixStream::connect(runtime_dir().join("gigatype-live-ibus.sock"))
            .map_err(|error| format!("IBus bridge: {error}"))?;
        stream.set_read_timeout(Some(Duration::from_secs(3))).map_err(|error| error.to_string())?;
        stream.set_write_timeout(Some(Duration::from_secs(1))).map_err(|error| error.to_string())?;
        writeln!(stream, "{}", serde_json::json!({"op": op, "text": text, "session": session}))
            .map_err(|error| error.to_string())?;
        let mut response = String::new();
        BufReader::new(stream).take(1024 * 1024).read_line(&mut response)
            .map_err(|error| error.to_string())?;
        let value: serde_json::Value = serde_json::from_str(&response).map_err(|error| error.to_string())?;
        if value["ok"].as_bool() != Some(true) {
            return Err(value["error"].as_str().unwrap_or("IBus composition failed").into());
        }
        Ok(value)
    }
}

impl Drop for Bridge {
    fn drop(&mut self) {
        let _ = self.request("shutdown", "");
        let deadline = Instant::now() + Duration::from_millis(700);
        while Instant::now() < deadline {
            if self.process.try_wait().ok().flatten().is_some() {
                return;
            }
            thread::sleep(Duration::from_millis(20));
        }
        let _ = self.process.kill();
        let _ = self.process.wait();
    }
}

struct Request {
    session: u64,
    segment: u64,
    final_text: bool,
    samples: Vec<f32>,
}

struct ResultText {
    session: u64,
    segment: u64,
    final_text: bool,
    text: Result<model::Hypothesis, String>,
}

struct Session {
    id: u64,
    recorder: Option<audio::Recorder>,
    media: Option<MediaGuard>,
    started: Instant,
    last_poll: Instant,
    last_preview: Instant,
    scanned: usize,
    segment_start: usize,
    segment: u64,
    voice_samples: usize,
    voice_end: usize,
    pending_finals: usize,
    hypotheses: VecDeque<String>,
    preview: String,
}

impl Drop for Session {
    fn drop(&mut self) {
        if let Some(media) = self.media.take() { media.resume(); }
    }
}

impl Session {
    fn start(id: u64, bridge: &Bridge) -> Result<Self, String> {
        eprintln!("GigaType live: event=session_start session={id}");
        bridge.request_for_session("begin", "", Some(id))?;
        let media = MediaGuard::pause_playing();
        play_sound(SoundCue::Listening);
        let preferred = env::var("GIGATYPE_MICROPHONE").ok();
        let recorder = match audio::Recorder::start(preferred.as_deref()) {
            Ok(recorder) => recorder,
            Err(error) => {
                media.resume();
                let _ = bridge.request("cancel", "");
                return Err(error);
            }
        };
        let now = Instant::now();
        notify("Диктовка", "Текст появляется во время речи. F9 — закончить; Shift+F9 — остановить, сохранив черновик.");
        Ok(Self { id, recorder: Some(recorder), media: Some(media), started: now,
            last_poll: now, last_preview: now, scanned: 0, segment_start: 0,
            segment: 0, voice_samples: 0, voice_end: 0, pending_finals: 0,
            hypotheses: VecDeque::new(), preview: String::new() })
    }

    fn scan(&mut self, audio: &[f32], threshold: f32) {
        // Scan every captured frame, including frames arriving during inference.
        let from = self.scanned.min(audio.len());
        for (index, frame) in audio[from..].chunks(RATE / 50).enumerate() {
            let rms = (frame.iter().map(|sample| sample * sample).sum::<f32>()
                / frame.len().max(1) as f32).sqrt();
            let peak = frame.iter().map(|sample| sample.abs()).fold(0.0_f32, f32::max);
            if rms >= threshold && peak >= threshold * 2.0 {
                let start = from + index * (RATE / 50);
                if self.voice_samples == 0 {
                    self.segment_start = self.segment_start.max(start.saturating_sub(RATE / 5));
                }
                self.voice_samples += frame.len();
                self.voice_end = start + frame.len();
            }
        }
        self.scanned = audio.len();
    }

    fn final_segment(&mut self, audio: &[f32], queue: &mut VecDeque<Request>) {
        if self.voice_samples >= RATE / 12 {
            let end = audio.len().min(self.voice_end + RATE / 4);
            if end > self.segment_start {
                queue.push_back(Request { session: self.id, segment: self.segment,
                    final_text: true, samples: audio[self.segment_start..end].to_vec() });
                self.pending_finals += 1;
            }
        }
        eprintln!("GigaType live: event=segment_closed session={} segment={} voice_samples={} pending_finals={}", self.id, self.segment, self.voice_samples, self.pending_finals);
        self.segment += 1;
        self.hypotheses.clear();
        self.preview.clear();
        self.segment_start = audio.len();
        self.voice_samples = 0;
        self.voice_end = audio.len();
    }

    fn finish(&mut self, queue: &mut VecDeque<Request>, threshold: f32, bridge: &Bridge) -> Result<(), String> {
        let Some(recorder) = self.recorder.take() else { return Ok(()); };
        eprintln!("GigaType live: event=recording_finished session={} elapsed_ms={}", self.id, self.started.elapsed().as_millis());
        let _ = bridge.request("indicator", "finishing");
        let audio = recorder.finish()?;
        self.scan(&audio, threshold);
        self.final_segment(&audio, queue);
        if let Some(media) = self.media.take() { media.resume(); }
        Ok(())
    }
}

fn close_session(active: &mut Option<Session>, queue: &mut VecDeque<Request>, bridge: &Bridge, reason: &str) {
    if let Some(session) = active.take() {
        eprintln!("GigaType live: event=session_closed session={} reason={} elapsed_ms={} pending_finals={} queued={}",
            session.id, reason, session.started.elapsed().as_millis(), session.pending_finals, queue.len());
    }
    queue.clear();
    let _ = bridge.request("end", "");
}

pub fn daemon() -> Result<(), String> {
    std::fs::create_dir_all(runtime_dir()).map_err(|error| error.to_string())?;
    let _ = std::fs::remove_file(super::transcribing_path());
    let bridge = Bridge::start()?;
    let path = socket_path();
    let _ = std::fs::remove_file(&path);
    let listener = UnixListener::bind(&path).map_err(|error| error.to_string())?;
    std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o600))
        .map_err(|error| error.to_string())?;
    listener.set_nonblocking(true).map_err(|error| error.to_string())?;
    let model = model::GigaAm::load_default()?;
    let confidence = env::var("GIGATYPE_LIVE_MIN_CONFIDENCE").ok()
        .and_then(|value| value.parse::<f32>().ok()).filter(|value| value.is_finite())
        .unwrap_or(0.55).clamp(0.0, 1.0);
    let (requests, incoming) = mpsc::channel::<Request>();
    let (outgoing, results) = mpsc::channel::<ResultText>();
    thread::spawn(move || {
        let mut model = model;
        while let Ok(mut job) = incoming.recv() {
            job.samples.resize(job.samples.len() + RATE / 4, 0.0);
            let started = Instant::now();
            let text = if job.final_text {
                model.transcribe_samples(&job.samples)
                    .map(|text| model::Hypothesis { text, preview: String::new() })
            } else {
                model.transcribe_partial(&job.samples, confidence)
            };
            match &text {
                Ok(hypothesis) => eprintln!("GigaType live: event=recognition session={} segment={} final={} audio_ms={} elapsed_ms={} chars={} reliable_chars={}",
                    job.session, job.segment, job.final_text, job.samples.len() * 1000 / RATE,
                    started.elapsed().as_millis(), hypothesis.text.chars().count(), hypothesis.preview.chars().count()),
                Err(error) => eprintln!("GigaType live: event=recognition_error session={} segment={} final={} error={error}",
                    job.session, job.segment, job.final_text),
            }
            if outgoing.send(ResultText { session: job.session, segment: job.segment,
                final_text: job.final_text, text }).is_err() { break; }
        }
    });
    let _shortcuts = super::shortcuts::ShortcutManager::start()
        .ok_or_else(|| "could not register F9/Shift+F9 in the X11 session".to_string())?;
    let mut active: Option<Session> = None;
    let mut queue = VecDeque::new();
    let mut busy = false;
    let mut next_id = 0;
    let mut suspend = SuspendDetector::new();
    let mut checked_focus = Instant::now();
    let interval = Duration::from_millis(env_u64("GIGATYPE_LIVE_INTERVAL_MS", 500).clamp(250, 5000));
    let pause = env_u64("GIGATYPE_LIVE_PAUSE_MS", 1800).clamp(300, 5000) as usize * RATE / 1000;
    let initial_audio = env_u64("GIGATYPE_LIVE_INITIAL_MS", 1200).clamp(300, 5000) as usize * RATE / 1000;
    let confirmations = env_u64("GIGATYPE_LIVE_CONFIRMATIONS", 2).clamp(1, 4) as usize;
    let maximum = Duration::from_millis(env_u64("GIGATYPE_MAX_RECORDING_MS", 120_000).max(1000));
    let threshold = env::var("GIGATYPE_LIVE_SILENCE_RMS").ok()
        .and_then(|value| value.parse::<f32>().ok()).filter(|value| value.is_finite() && *value > 0.0)
        .unwrap_or(80.0 / 32768.0);
    STOP.store(false, Ordering::Relaxed);
    unsafe {
        libc::signal(libc::SIGTERM, stop_signal as *const () as libc::sighandler_t);
        libc::signal(libc::SIGINT, stop_signal as *const () as libc::sighandler_t);
    }
    eprintln!("GigaType live: event=settings pid={} interval_ms={} pause_ms={} initial_ms={} confirmations={} min_confidence={} max_session_ms={}",
        std::process::id(), interval.as_millis(), pause * 1000 / RATE, initial_audio * 1000 / RATE,
        confirmations, confidence, maximum.as_millis());
    eprintln!("GigaType live: ready; IBus inline composition, F9 start/stop, Shift+F9 cancel");

    while !STOP.load(Ordering::Relaxed) {
        if suspend.resumed() && active.is_some() {
            close_session(&mut active, &mut queue, &bridge, "resume_from_suspend");
        }
        if active.is_some() && checked_focus.elapsed() >= Duration::from_millis(200) {
            checked_focus = Instant::now();
            let state = bridge.request("state", "");
            if let Err(error) = &state {
                eprintln!("GigaType live: event=bridge_state_error error={error}");
            }
            if !state.ok().is_some_and(|value| value["active"] == true) {
                close_session(&mut active, &mut queue, &bridge, "input_context_inactive");
                notify("Диктовка остановлена", "Поле ввода изменилось или начат ручной ввод.");
            }
        }
        while let Ok(result) = results.try_recv() {
            busy = false;
            let Some(session) = active.as_mut().filter(|session| session.id == result.session) else {
                eprintln!("GigaType live: event=result_discarded session={} segment={} reason=session_closed", result.session, result.segment);
                continue;
            };
            if !result.final_text && (session.segment != result.segment || session.recorder.is_none()) {
                eprintln!("GigaType live: event=result_discarded session={} segment={} reason=obsolete_preview", result.session, result.segment);
                continue;
            }
            let outcome = result.text.and_then(|hypothesis| {
                if result.final_text {
                    // Pauses are not automatic sentence boundaries.
                    let text = super::format_transcript(&hypothesis.text, false);
                    session.pending_finals = session.pending_finals.saturating_sub(1);
                    if !text.is_empty() {
                        eprintln!("GigaType live: event=commit_requested session={} segment={} chars={}", session.id, result.segment, text.chars().count());
                        bridge.request("commit", &text)?;
                        eprintln!("GigaType live: event=commit_bridge_accepted session={} segment={} application_ack=false", session.id, result.segment);
                        save_history(&text)?;
                        eprintln!("GigaType live: event=history_saved session={} segment={}", session.id, result.segment);
                    } else {
                        eprintln!("GigaType live: event=empty_final session={} segment={}", session.id, result.segment);
                        bridge.request("preview", "")?;
                    }
                } else {
                    let text = hypothesis.preview.split_whitespace().collect::<Vec<_>>().join(" ");
                    session.hypotheses.push_back(text);
                    while session.hypotheses.len() > confirmations { session.hypotheses.pop_front(); }
                    if session.hypotheses.len() == confirmations {
                        let mut common = session.hypotheses[0].clone();
                        for text in session.hypotheses.iter().skip(1) {
                            common = common.chars().zip(text.chars())
                                .take_while(|(left, right)| left == right).map(|(left, _)| left).collect();
                        }
                        // A truncated subword is shown only after repeated confirmation.
                        // Sentence-final punctuation waits for final recognition.
                        let common = common.trim_end_matches(|c: char| c.is_whitespace() || ".!?…,:;".contains(c));
                        // Confidence filtering can temporarily retract the stable prefix.
                        // Keep the displayed draft until there is new text or a correction;
                        // final recognition remains authoritative, including deletions.
                        if !common.is_empty() && !session.preview.starts_with(common) {
                            bridge.request("preview", common)?;
                            eprintln!("GigaType live: event=preview_sent session={} segment={} chars={}", session.id, result.segment, common.chars().count());
                            session.preview = common.to_owned();
                        } else {
                            eprintln!("GigaType live: event=preview_retained session={} segment={} candidate_chars={} retained_chars={}",
                                session.id, result.segment, common.chars().count(), session.preview.chars().count());
                        }
                    }
                }
                Ok(())
            });
            if let Err(error) = outcome {
                eprintln!("GigaType live: event=result_error error={error}");
                close_session(&mut active, &mut queue, &bridge, "result_error");
                notify("Ошибка диктовки", &error);
            }
        }
        if active.as_ref().is_some_and(|session| session.recorder.is_none() && session.pending_finals == 0) {
            close_session(&mut active, &mut queue, &bridge, "completed");
            play_sound(SoundCue::Transcribing);
        }
        if let Some(session) = active.as_mut() {
            if session.recorder.is_some() && session.started.elapsed() >= maximum {
                eprintln!("GigaType live: event=session_time_limit session={}", session.id);
                if let Err(error) = session.finish(&mut queue, threshold, &bridge) {
                    close_session(&mut active, &mut queue, &bridge, "finish_error");
                    notify("Ошибка диктовки", &error);
                }
            }
        }
        if let Some(session) = active.as_mut() {
            if let Some(recorder) = session.recorder.as_ref().filter(|_| session.last_poll.elapsed() >= Duration::from_millis(120)) {
                let snapshot = recorder.snapshot();
                session.last_poll = Instant::now();
                match snapshot {
                    Ok(audio) => {
                        session.scan(&audio, threshold);
                        if session.voice_samples > 0 && (audio.len().saturating_sub(session.voice_end) >= pause
                            || audio.len().saturating_sub(session.segment_start) >= RATE * 18) {
                            eprintln!("GigaType live: event=segment_boundary session={} segment={} silence_ms={} segment_ms={}",
                                session.id, session.segment, audio.len().saturating_sub(session.voice_end) * 1000 / RATE,
                                audio.len().saturating_sub(session.segment_start) * 1000 / RATE);
                            session.final_segment(&audio, &mut queue);
                        } else if !busy && queue.is_empty() && session.pending_finals == 0
                            && session.voice_samples >= RATE / 5 && session.last_preview.elapsed() >= interval
                            && audio.len().saturating_sub(session.segment_start) >= initial_audio {
                            session.last_preview = Instant::now();
                            requests.send(Request { session: session.id, segment: session.segment,
                                final_text: false, samples: audio[session.segment_start..].to_vec() })
                                .map_err(|error| error.to_string())?;
                            busy = true;
                        }
                    }
                    Err(error) => {
                        eprintln!("GigaType live: event=microphone_error error={error}");
                        close_session(&mut active, &mut queue, &bridge, "microphone_error");
                        notify("Ошибка микрофона", &error);
                    }
                }
            }
        }
        if !busy {
            if let Some(job) = queue.pop_front() {
                requests.send(job).map_err(|error| error.to_string())?;
                busy = true;
            }
        }
        match listener.accept() {
            Ok((mut stream, _)) => {
                stream.set_read_timeout(Some(Duration::from_millis(500))).ok();
                let mut command = String::new();
                if BufReader::new(&stream).take(128).read_line(&mut command).is_err() { continue; }
                if matches!(command.trim(), "toggle" | "cancel" | "quit") {
                    eprintln!("GigaType live: event=command command={} session={:?}", command.trim(), active.as_ref().map(|session| session.id));
                }
                let response: Result<String, String> = match command.trim() {
                    "status" => Ok(match &active {
                        None => "idle (live mode)",
                        Some(session) if session.recorder.is_some() => "recording (live mode)",
                        Some(_) => "transcribing (live mode)",
                    }.into()),
                    "toggle" if active.is_none() => {
                        next_id += 1;
                        Session::start(next_id, &bridge).map(|session| {
                            active = Some(session);
                            "recording (live mode)".into()
                        })
                    }
                    "toggle" => {
                        let session = active.as_mut().unwrap();
                        if session.started.elapsed() < Duration::from_millis(300) {
                            Ok("recording (live mode)".into())
                        } else {
                            session.finish(&mut queue, threshold, &bridge).map(|_| "transcribing (live mode)".into())
                        }
                    }
                    "cancel" => {
                        close_session(&mut active, &mut queue, &bridge, "user_cancel");
                        Ok("cancelled".into())
                    }
                    "quit" => {
                        writeln!(stream, "bye").ok();
                        break;
                    }
                    _ => Err("unknown command".into()),
                };
                match response {
                    Ok(message) => { writeln!(stream, "{message}").ok(); }
                    Err(error) => {
                        eprintln!("GigaType live: event=command_error error={error}");
                        close_session(&mut active, &mut queue, &bridge, "command_error");
                        notify("Ошибка диктовки", &error);
                        writeln!(stream, "error: {error}").ok();
                    }
                }
            }
            Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => thread::sleep(Duration::from_millis(20)),
            Err(error) => return Err(error.to_string()),
        }
    }
    close_session(&mut active, &mut queue, &bridge, "daemon_shutdown");
    let _ = std::fs::remove_file(path);
    Ok(())
}

fn env_u64(name: &str, default: u64) -> u64 {
    env::var(name).ok().and_then(|value| value.parse().ok()).unwrap_or(default)
}
