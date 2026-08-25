# BetterVoice — Linux port handoff

**Date:** 2026-08-24 (updated later the same day)
**Upstream:** https://github.com/TarunTomar122/better-voice (macOS, Swift/SwiftUI)
**Scope:** the Linux port lives alongside the untouched macOS sources; see [Layout](#layout).

## State in one line

The port runs as a native Linux app: tray, setup window, global hotkey, mic capture, local ASR, pointer trail overlay, screenshot-on-circle, and text injection all work. 311 unit tests pass, ruff is clean, and the integration suite now passes 12/12 — its intermittent failures were a real app bug, [now fixed](#fixed-the-microphone-routing-race). The integration suite still **must not be run in a loop** — see [Do not do this](#do-not-do-this).

```bash
.venv/bin/python -m pytest tests/test_core.py tests/test_linux_layer.py tests/test_desktops.py -q
```

## Do not do this

Repeatedly running `tests/test_integration.py` against the live session **locked up the desktop and blacked out the primary monitor** on 2026-08-24. KWin's journal showed:

```
Failed to allocate an egl gbm swapchain graphics buffer
Rendering a layer failed!
Failed to find a working output layer configuration!
```

Cause: every recording built a **fullscreen translucent window per monitor**, and the suite was run dozens of times over hours (plus soak runs of 25+25 sessions). Fullscreen surface churn exhausted GPU buffers until the compositor could not allocate a framebuffer for the output. The monitor was never "disabled" — KWin simply had nothing to draw it with.

Two mitigations are now in place:

1. **Overlay windows are cached and reused** across recordings (`RecordingOverlay._cache`, keyed by screen name). `stop()` hides; only `close()` — called from `AppController.shutdown()` — destroys them. This is the change that landed last; see [Last change](#last-change-overlay-window-reuse).
2. **Run the integration suite deliberately, once, and look at the result.** Never on a timer, never unattended.

## Layout

Swift sources (`Sources/`, `Tests/`, `Package.swift`, `Info.plist`) are the untouched macOS original. Everything Linux is new:

| Path | What it is |
|---|---|
| `bettervoice/app.py` | `AppController` — the state machine tying every backend together |
| `bettervoice/core/` | Pure logic, no Qt, no I/O: trail geometry, circle-gesture detection, session policy, shortcut state, sound cues, delivery reporting |
| `bettervoice/backends/` | One module per Linux facility (see [Platform mapping](#platform-mapping)) |
| `bettervoice/ui/` | Tray, setup window, overlay, recovery dialog, palette, icons, widgets |
| `bettervoice/audio/` | Device enumeration and the PortAudio recorder |
| `bettervoice/asr/` | Parakeet transcription + optional grammar cleanup |
| `bettervoice/doctor.py` | `bettervoice --doctor`, reports which backends resolved |
| `bettervoice/resources/` | `.desktop`, icon SVG, AppStream metainfo, systemd user service |
| `install.sh` / `uninstall.sh` | venv + desktop entry + icon install |
| `README.md` | User-facing docs; the macOS original's README lives upstream |

## Platform mapping

| macOS | Linux |
|---|---|
| AppKit / SwiftUI | PyQt6 |
| CoreAudio | PipeWire via `sounddevice`, per-stream routing with `PIPEWIRE_NODE` + `pactl move-source-output` |
| ScreenCaptureKit | `org.freedesktop.portal.Screenshot` |
| `NSEvent` global monitor | portal `GlobalShortcuts` → evdev → Xlib, in that preference order |
| `CGEvent` pointer polling | KWin script bridge (`workspace.cursorPos`), Xlib on X11 |
| `NSPasteboard` | `wl-copy` (`wlr-data-control`) / portal Clipboard, with content-checked restore |
| Accessibility API text insert | portal `RemoteDesktop` synthetic keystrokes, clipboard paste fallback |
| Screen-saver-level `NSPanel` | fullscreen input-transparent `QWidget` per screen |

## Things that cost real time — don't rediscover them

- **PyQt6 cannot marshal `a(sa{sv})`.** The `GlobalShortcuts` portal needs it, so that one backend is written on **jeepney**, not QtDBus. Everything else uses QtDBus.
- **`QTimer.singleShot` posts to the *calling* thread.** Cross-thread callbacks were being silently dropped. Use the queued-signal `_Dispatcher` in `workers.py`.
- **The `Clipboard` portal needs an explicit `QStringList`.** `SetSelection` declares `mime_types` as `as`; PyQt marshals a plain Python list inside a variant map as `av` and the portal rejects the whole call — `Expected type 'as' for option 'mime_types', got 'av'`. The screenshots then never reach the clipboard and the only trace is one `WARNING` in the log. Wrap with `QDBusArgument(values, QMetaType.Type.QStringList.value)`. Verify marshalling on the wire, not by the portal's reply: an invalid session is rejected *before* the type is checked, so a bad type looks fine in isolation. `dbus-monitor --session "interface='org.freedesktop.portal.Clipboard'"` shows `array [ variant string ... ]` for the broken form and `array [ string ... ]` for the correct one.
- **The `RemoteDesktop` portal needs explicit `uint32`.** Three separate arguments (`types`, `persist_mode`, keyboard `state`) fail silently as plain ints — wrap with `QDBusArgument(v, QMetaType.Type.UInt.value)`.
- **PortAudio callbacks must be held on the instance.** `self._callback = callback`, and always `close()` the stream. Letting the GC take it kills the process with SIGSEGV while PortAudio's thread is still inside the callback.
- **PyQt6 aborts the process on an unhandled exception in a slot.** A plain `AttributeError` inside a signal handler presents as `Fatal Python error: Aborted` with a C-level traceback and no Python cause. If you see an abort, suspect a slot before you suspect Qt.
- **Never emit `QGuiApplication.screenAdded` from a test** unless the handler is airtight — Qt has its own connections on it.
- **Wayland ignores requested window positions.** `showFullScreen()` is the only way to pin an overlay to a chosen output.
- **KWin scripts can call out (`callDBus`) but nothing can call in.** The bridge is one-directional by design.
- **evdev over-claims.** A gaming mouse's macro endpoint looks like a keyboard. Read `/proc/bus/input/devices` and require both `kbd` and `leds`.
- **Parakeet hallucinates "Yeah." from silence.** Gated at peak `< 0.015`.
- **The portals identify the app by its systemd scope, not its desktop file.** Running `.venv/bin/python -m bettervoice` from a terminal that belongs to another application — an IDE, an editor's built-in shell — puts it in *that* app's `app-*.scope`, and KDE registers BetterVoice's global shortcuts under the host's id with no keys assigned — both setup rows come up unchecked and nothing works. `install.sh` already generates a `~/.local/bin/bettervoice` wrapper that avoids this; to run the working tree the same way:

  ```bash
  systemd-run --user --scope --quiet --collect \
      --unit="app-io.github.taruntomar122.BetterVoice-$$" .venv/bin/python -m bettervoice
  ```

  Symptom to recognise: a stray `[<host-app-id>]` group in `~/.config/kglobalshortcutsrc` holding `push-to-talk=none,Alt+V,...`. On this Plasma version global shortcuts are served by **kwin_wayland itself**, so there is no daemon to restart — a stale group survives in memory until logout.
- **`~/.local/share/BetterVoice/venv` is a copy, not an editable install.** Launching from the application menu runs that copy, not the working tree; re-run `install.sh` before testing that way.

## Last change: overlay window reuse

The mitigation for the compositor lock-up above.

- `_OverlayWindow.adopt(screen)` — re-points a cached window at a new `QScreen`, returns `False` if the C++ side is gone.
- `RecordingOverlay._cache: dict[str, _OverlayWindow]` keyed by screen name; `_window_for()` reuses, `_evict_stale()` drops windows for detached monitors.
- `stop()` → `_hide_windows()` (hide, keep). `close()` → destroy everything; called from `AppController.shutdown()`.
- Two tests that asserted the old "rebuild makes new objects" contract were inverted to assert reuse; two new tests cover reuse across sessions and release on close.

**Verified against a live desktop** on a two-monitor KDE Wayland session (DP-3 + HDMI-A-5): across three start/stop cycles each screen reused the same window object, every window reported the screen it was assigned to with matching geometry, the HUD followed the requested screen, and `close()` emptied the cache. The compositor journal stayed clean. That closes the one behaviour the unit tests could not prove.

## Fixed: the microphone routing race

The integration suite's intermittent failures — `the capture stream never reached
bettervoice_test_sink.monitor` — were not test flakiness. They were a real bug that
put a recording on the wrong microphone for its whole length.

**How it was cornered.** The two failing tests passed in isolation, so it was
order-dependent. Rather than loop the suite (which is what blacked out the
monitor), the routing path was soaked on its own — no Qt, no overlay, no GPU —
in `AudioRecorder` alone. 30/30 clean at idle; under CPU load (the transcription
tests saturate the machine) it reproduced at 2/20, the same symptom and roughly
the same rate as the suite. Instrumenting `_check_routing` showed it returning
`True` on its *first* poll, 0.04s in, while the stream sat on the default
microphone for the next twelve seconds.

**Two causes, both in `_check_routing`:**

1. It found our stream by `application.name == "BetterVoice"`. Two recordings a
   moment apart both answer to that, so a stream that was still closing — already
   correctly on the target source — was accepted as proof that *this* recording
   was routed.
2. It returned `True` as soon as it had *issued* `pactl move-source-output`.
   PipeWire returns 0 for a move against a stream it is still setting up and then
   drops the request, so the move was believed and never checked.

**The fix.** Each recording mints a token (`uuid4().hex`) and publishes it on the
stream as `bettervoice.stream.id` via `PIPEWIRE_PROPS`; the check matches on that
token, so it can only ever act on its own stream. A move now returns `False` — the
routing stays *unknown* until the stream is actually observed on the requested
source. An unconfirmed move is re-issued, throttled to `MOVE_RETRY_SECONDS = 1.0`
so a 100ms poll does not mean a `pactl` process every 100ms. A routing thread that
outlived its recording (`stop()` only joins for a second) now notices the token
changed and leaves the next recording alone.

Custom properties do survive into `pactl -f json list source-outputs` — that was
checked before relying on it.

**Result:** 0/40 under the same load that failed 2/20 and 1/25 before, and 12/12
on the integration suite.

Worth knowing: `PIPEWIRE_NODE` never works for a null sink's monitor. `X.monitor`
is a PulseAudio name, not a PipeWire node name, so every test routes through the
`move-source-output` fallback — which is why the tests exercised this path so much
harder than real use does.

## Fixed: transcript insertion needed Set Up on every launch

macOS held its Accessibility grant from launch. The `RemoteDesktop` portal
equivalent is a session that dies with the process, and `prepare()` was only ever
reachable from the setup window's **Set Up** button — so every launch started with
transcript insertion unavailable and the row unchecked, even with the grant given
and its restore token already in `settings.json`.

`AppController.start()` now calls `_restore_text_insertion()`, which reconnects
only when `text_insertion.grant_remembered` is true — a saved restore token means
the user already said yes and the portal hands the session straight back with no
prompt. Startup must never raise a permission dialog on its own; that belongs to
onboarding. A restore that comes back refused clears the token, so a revoked grant
does not mean a refused prompt at every launch.

Verified live: with the app running in its own scope, the portal shows an active
`rdsession_*` session immediately after startup, with no user interaction.

## Security review before the first commit

Three findings, all fixed. Two shared a root cause: the KWin bridge exports
D-Bus slots that **any peer on the session bus can call**, and PyQt6 has no
`QDBusContext`, so an exported slot cannot see who called it.

**Script injection into the compositor (was the serious one).** `_RESTORE_SOURCE`
had `var betterVoiceTarget = "{target}";` and `target` came straight from the open
`Captured` slot. A value containing a quote stopped being a string and became
code — running inside KWin, unsandboxed, with `callDBus` under the compositor's
identity. Fixed twice over: `window_id` is validated against the only shape KWin
ever sends (a QUuid), and every interpolated value now goes in via `json.dumps`
rather than being pasted inside hand-written quotes. That second half covers
`service`, `path` and `interface` in both scripts and in `pointer.py`.

**Forged callbacks.** With no sender check, any peer could call `Captured` and
name its own window as the paste target — the dictated transcript typed into
someone else's window — or drive `Moved` to steer the circle gesture. Each
generated script now carries a fresh `secrets.token_hex(16)` nonce that the
callback must echo back, compared with `compare_digest`; the script files are
written 0600 in a 0700 directory so the nonce is not readable by other accounts.
Against an attacker already running as the same user this raises the bar rather
than closing the door — such an attacker can read the clipboard the transcript
lands on anyway — but it ends anonymous access from the rest of the bus.

**Recordings could land in shared `/tmp`.** `runtime_dir()` fell back to
`/tmp/BetterVoice` whenever `XDG_RUNTIME_DIR` was unset — normal on
non-systemd distributions, under `su`, in minimal containers, over plain SSH —
and `ensure()` would happily accept a symlink planted there, because
`mkdir(exist_ok=True)` falls through to `is_dir()`, which follows links. Raw
microphone audio then sat world-readable, or in someone else's directory. The
fallback is now the private cache directory, `ensure_private()` refuses a
symlink or a directory owned by anyone else, and the WAV is claimed at 0600 with
`O_CREAT | O_EXCL | O_NOFOLLOW` before any audio reaches it.

Checked and found sound, for whoever reviews this next: `_rich_html()` escapes
the transcript and the URL; `remove_abandoned_recordings()` only ever passes
signal 0; the portal signal handlers register match rules with an explicit
`sender=` and re-check the session path before writing to a pipe; the grammar
model download pins a commit and verifies SHA-256; no `subprocess` call uses
`shell=True` and no argv is attacker-influenced. `ExportAllSlots` was checked on
the live bus and exports only the three intended slots, not `deleteLater`.

One thing left as a note rather than a change: `transcriber.py` calls
`snapshot_download` without a `revision` pin, where `grammar.py` pins one.
Tightening it means choosing a commit, and guessing wrong breaks model downloads
for everyone, so it wants a deliberate decision rather than a drive-by fix.

**Verified rather than assumed.** The symlink refusal, the 0700/0600 modes, the
cache fallback and the injection payload were each exercised against the real
filesystem; the nonce protocol was confirmed live against the running compositor,
where KWin's own `internalId` passes the new UUID check and both callbacks still
arrive.

## Known-unresolved

- **Occasional core dumps.** The routing race is fixed and the suite now passes 12/12, but earlier runs sometimes ended in a core dump rather than an assertion failure. That was never root-caused and has not been seen since; if it comes back, suspect a slot before you suspect Qt.
- Nothing is committed. The port should get an initial commit before further work.
- No packaging beyond `install.sh` — no Flatpak, no distro package.
