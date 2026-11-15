# Guitar FX Box

A programmable effects box for my acoustic guitar. It sits between the guitar and the amp, runs effects I design (often AI-written), and is controlled from a web app on my phone. Built on a Raspberry Pi 5 with a Pisound audio HAT. No soldering.

## Hardware

- Raspberry Pi 5 (8 GB) with the official Active Cooler and 27 W USB-C power supply
- Blokas Pisound v1.2 (audio HAT: stereo ¼" in/out, gain and volume knobs, one programmable button), connected over I2S
- Pisound Acrylic Case
- microSD card (A2), OS: 64-bit Raspberry Pi OS Lite, running headless over Wi-Fi/SSH
- Guitar: 1990s Ovation Celebrity with an onboard active preamp (9V battery, EQ knobs, volume slider). Low-impedance output, so no external preamp is needed.

Signal chain: guitar → Pisound in → Pi (audio server → effect) → Pisound out → acoustic amp.
Control: phone/laptop browser → web app on the Pi → effect parameters/presets.

## Design decisions and why

**Two kinds of latency, treated differently.**
- *Audio latency* (pluck → speaker) is paid on every sample. Model: ≈ 2 × buffer ÷ 48 kHz + ~1 ms of converters. Target under ~10 ms; start at 128 samples (≈ 6.3 ms). With an acoustic, the guitar body is heard directly too, so a late amp signal causes comb filtering; keep latency low.
- *Control latency* (tap preset → new sound) does not matter. Presets change between songs, not while playing. A second to switch is fine.

**No real-time web UI for now.** The web app selects presets and edits settings *between* playing. It does not need low-latency parameter streaming. Don't over-engineer the web ↔ audio interface; a simple WebSocket or HTTP API that loads a preset is enough. Live knob-turning can come later.

**Clicks are missed deadlines, not slow math.** Each buffer is a deadline. A block is late when wake-up delay + compute time > buffer duration. Fix order:
1. Bigger buffer (more slack per deadline)
2. Keep the web server and builds off the audio core (low priority / CPU pinning)
3. Real-time kernel (PREEMPT_RT) only if clicks remain
Keep the Pi cool: throttling causes missed deadlines.

**Precompute the expensive parts.** Impulse responses, filter designs and new effects are prepared offline. Only the cheap per-sample loop runs live.

**The Pi 5 has plenty of compute.** ~50,000 cycles per sample per core at 48 kHz. A full acoustic chain (EQ, compressor, body-IR convolution, delay, reverb, pitch shift) uses a small fraction of one core. Only large neural amp models would strain it, and they aren't a goal here.

**Buffer size per preset.** Each effect/preset is profiled and stores the smallest buffer size it can run at safely. Switching presets may switch buffer size (one small glitch at switch time is acceptable).

## Planned architecture (to be decided in early milestones)

- Audio server: JACK2 or PipeWire (decide in M1; JACK is simpler to reason about for fixed buffers)
- Effects: C++ and/or Faust, compiled on the Pi. Candidate formats: standalone JACK client vs LV2 plugins (LV2 would also work inside MODEP, the browser-based pedalboard, as a quick-start option)
- Web app: small server on the Pi (Python or Node) serving a phone-friendly UI; talks to the audio engine to list/load presets and edit settings
- Presets: files in the repo (effect id + parameters + recommended buffer size)

## Milestones

- **M0 – Repo scaffold (laptop, before hardware):** project layout, effect interface, preset format, profiling harness that runs on the laptop for plumbing
- **M1 – First sound:** flash OS, SSH in, pass-through guitar → amp, set gain staging (guitar EQ flat, volume slider high, level set with the Pisound gain knob)
- **M2 – Measure latency:** loopback cable Pisound out → in, run `jack_iodelay`; record the real round-trip at 64/128/256
- **M3 – Profiling harness on the Pi:** render test recordings (strumming, hard picking, sustain, silence) through each effect at 32/64/128/256; record *worst-case* block time; combine with `cyclictest` worst wake-up under realistic load (`stress-ng` + web server traffic); output the smallest safe buffer per effect
- **M4 – First effects:** body-IR correction, EQ, compressor, delay, reverb, shimmer
- **M5 – Web app:** list presets, load a preset, edit and save settings, show health (buffer, latency, DSP load, xrun count)

## Coding rules for the audio path

- Never allocate memory, lock, log or do I/O inside the audio callback
- Profile on the Pi itself, with the cooler on and the CPU warmed up; laptop timings don't transfer
- Measure worst-case block time, not the average
- Enable flush-to-zero / denormals-are-zero in audio code (reverb tails otherwise cause CPU spikes); the harness's silence test should catch regressions
- Count xruns (JACK, or the ERR column in `pw-top`) as the real click metric

## Research notes

`research_notes/` (gitignored, local-only) holds notes from agents and me: work done and why,
decisions, hardware/latency measurements, profiling results, open questions. Skim titles before
related work; write one with `/research-note <what to record>`.

- **Filename:** `research_notes/YYYY-MM-DD_HHMM_<kebab-slug>.md`, local time. Flat directory.
- **Frontmatter:** `title` and `summary` required; optional `date` (ISO with offset), `author`,
  `tags`, `related`, `status` (active | done | superseded | abandoned).
- Never put secrets, keys or tokens in a note. Don't rewrite old notes; add a new one and mark
  the old `status: superseded`.

## Reference

- Pisound: https://blokas.io/pisound/
- Earlier explainer pages from planning (latency concepts, shopping list) live as Claude artifacts on my account
