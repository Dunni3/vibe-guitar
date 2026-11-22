# Guitar FX Box

A Raspberry Pi with a [Pisound](https://blokas.io/pisound/) audio board on it, sitting between an
acoustic guitar and its amp. Every sample of the guitar signal passes through code running on the
Pi, so the effects are whatever we write: plain arithmetic on numbers, from scratch, in C++. A web
app on the Pi, opened on a phone, picks the sound, edits its settings, shows what the box is
hearing and records it.

It's also a learning project. I'm new to audio, so alongside the code there's a growing set of
visual explainers for the signal processing, the maths behind it and how the hardware works. The
long-term goal is a framework for *learning* filters from target data instead of hand-designing
them.

```
acoustic guitar ──► Pisound in ──► Raspberry Pi 5: your effect code ──► Pisound out ──► amp
                                            ▲
                     phone browser ──Wi-Fi──┘  web app: presets, settings, live analysis, recording
```

## What it does

- **Runs effects you write.** An effect is a small C++ class that gets a block of samples and
  changes them in place: gain, delay, filters, anything you can express as arithmetic. Each one is
  tested and profiled automatically, and saved as a preset with its settings.
- **Is controlled from your phone.** Pick a preset between songs, tweak its sliders, save it. The
  web app is served by the Pi itself, so it works with no internet; the Pi falls back to its own
  Wi-Fi hotspot when it can't find yours.
- **Shows the signal live.** Level meters, a tuner, waveform, spectrum and spectrogram, a chord
  guess, overtones, attacks and tempo, noise floor and mains hum, and the frequency response
  between two signals (what an effect does, or how a mic differs from the pickup).
- **Records it.** Takes are saved as 24-bit WAV files with notes, markers and the preset that was
  playing, and a "save the last 30 s" button keeps what you already played.
- **Keeps latency low.** The guitar's own body is heard alongside the amp, so a late amp signal
  smears the sound. The target is under about 10 ms from pluck to speaker, and each preset records
  the smallest buffer it can run at without clicks.

## Where it's going: filters fitted to a target sound

A plugged-in acoustic sounds thin because its under-saddle pickup hears the strings but not the
wooden body. The first experiment fixes that by measurement instead of by ear:

1. Put a condenser microphone in front of the guitar and record it **at the same time** as the
   pickup, through a two-input USB audio interface (so both share one clock).
2. On a laptop, fit a filter that turns the pickup recording into the mic recording, as closely as
   possible.
3. Run that filter live on the Pi. The pickup now sounds like the mic'd guitar.

The Live page already previews this: with the mic and pickup as two channels it shows mic ÷ pickup
per frequency, which is the filter the fit will estimate. Past that, the same idea generalizes:
give the system recordings of a sound you want and let it learn the filter that gets there.

## Learning the theory: explainers

[`explainers/`](explainers/) holds self-contained interactive pages, each built to make one idea
concrete, with diagrams, sliders and sound. Download the folder and open any page in a browser;
the web app also serves them at `/explainers/`.

| Page | What it covers |
|------|----------------|
| [what-is-a-sample](explainers/what-is-a-sample.html) | What one sample is, and how five numbers become a different gain at every frequency |
| [filters-and-frequencies](explainers/filters-and-frequencies.html) | How filter arithmetic picks which frequencies get louder: the biquad, poles and zeros, a playground with audio |
| [feedback-unrolled](explainers/feedback-unrolled.html) | Recursion unrolled into an impulse response, and its link to the frequency response through Fourier |
| [latency](explainers/latency.html) | Where the milliseconds go, and why clicks are missed deadlines, not slow maths |
| [effects-compute-cost](explainers/effects-compute-cost.html) | How much arithmetic each effect costs, against what the Pi 5 can do |
| [live-analysis](explainers/live-analysis.html) | How each panel of the Live page is computed: pitch, chords, tempo, response and coherence |
| [shopping-list](explainers/shopping-list.html) | The parts list, assembly steps and what not to buy |

## Build your own

Everything plugs, clips or screws together: no soldering, and the only tool is a small Phillips
screwdriver.

| Part | Rough price (US, Nov 2026) |
|------|-----------------|
| Raspberry Pi 5 (4 GB is plenty; mine is 8 GB) | $110 |
| Blokas Pisound v1.2 (older versions don't fit the Pi 5's cooler) | $125 |
| Pisound acrylic case, Pi Active Cooler, official 27 W USB-C supply | $36 |
| microSD card, 32–64 GB, A2 rated | $18 |
| Two ¼" instrument cables | $20 |

That's about $310 for the box. For the filter-fitting experiment, add a small-diaphragm condenser
mic, a two-input USB audio interface with 48 V phantom power, an XLR cable and a mic stand: about
$290 more, used only on the laptop for measurements. A guitar with a **passive** pickup (no
battery, no knobs on the side) also needs a preamp/DI pedal in front of the box; mine, an Ovation
with an onboard preamp, doesn't. Details and a tick-off checklist:
[explainers/shopping-list.html](explainers/shopping-list.html).

Then: flash 64-bit Raspberry Pi OS Lite with Wi-Fi and SSH set in Raspberry Pi Imager, assemble,
clone this repo on the Pi, and follow [Setup](#setup) and [Web app](#web-app) below. The Pi's
system setup (hotspot fallback) is in [pi/README.md](pi/README.md).

## Status

Working today: the effect engine and its tests, presets, the profiling harness, the web app with
live analysis and recording, and the offline hotspot, all running on the Pi against a simulated
guitar signal. Next: the real-time audio path through the Pisound (a JACK engine), measuring its
latency, then the first effects (EQ, compressor, delay, reverb) and the body-filter fit. Design
decisions and milestones are in [CLAUDE.md](CLAUDE.md).

## For developers

### Layout

```
engine/
  include/fx/effect.h     Effect interface + Chain (the contract every effect and host follows)
  include/fx/preset.h     Preset format, strict validation, chain building, effect catalog
  include/fx/denormals.h  Flush-to-zero for audio threads (aarch64 + x86)
  include/fx/wav.h        WAV I/O for offline tools
  include/fx/tap.h        Signal tap: wait-free shared-memory ring the engine publishes in/out to
  effects/                Effects (gain.cpp is the template for new ones)
  src/                    Core library sources; registry.cpp lists all effects
  tools/fxrender.cpp      Offline renderer + per-block timer
  tools/fxtapfeed.cpp     Stand-in engine: plays a WAV through a preset into the tap, in real time
  tests/tests.cpp         Unit tests
presets/                  Preset JSON files
harness/fxharness/        Python: test signal generator, profiling harness
webapp/fxweb/             Python web app: API, engine interface (FakeEngine for now), static UI
webapp/fxweb/live/        Live signal: sources (sim/tap/alsa), analysis, recording, SSE stream
webapp/tests/             Web app tests, incl. Python-vs-C++ preset validator agreement
webapp/guitar-fx-web@.service   systemd unit for the Pi (niced, off the audio core)
pi/                       Pi system setup: Wi-Fi hotspot fallback + fxbox-net switch
test_audio/               Generated signals (gitignored); put real recordings in recordings/
recordings/               Takes from the web app's Live page (gitignored): <id>.wav + <id>.json
profiles/                 Profiling output (gitignored)
```

### Setup

Mac: `brew install cmake nlohmann-json` and [uv](https://docs.astral.sh/uv/).
Pi (Raspberry Pi OS): `sudo apt install cmake g++ nlohmann-json3-dev` and uv.
If nlohmann-json isn't installed, CMake fetches a pinned copy.

### Commands

```
make test                 # build + unit tests
make check                # validate every preset
make profile              # generate test signals, profile all presets at 32/64/128/256

./build/fxrender --preset presets/slapback.json --in test_audio/strum.wav --out out.wav --block 64
./build/fxrender --list-effects        # effect + parameter catalog as JSON (for the web app)
uv run fx-profile --help
```

### Web app

A phone-friendly page to pick, load, edit and save presets, with a health panel (buffer,
latency, DSP load, xruns, CPU temp). It runs against a **fake engine** until the Pisound is in:
loading a preset is tracked and the health numbers are simulated (CPU temp is real on the Pi).

```
make webapp               # build fxrender, then serve on 0.0.0.0:8080 (PORT=9000 to change)
make webapp-test          # pytest for the API, engine and validator
```

Open `http://<pi-address>:8080` on the phone. Everything (HTML, JS, CSS) is served from the Pi,
with no CDN, so it works with the Pi as its own Wi-Fi hotspot and no internet.

- **Load** a preset by tapping it. **Edit** opens sliders built from the effect catalog
  (`fxrender --list-effects`: ranges, defaults, units). **Try** plays your edits without
  saving; **Save** writes `presets/<id>.json` and reloads it if it's the one playing;
  **Save as…** makes a new file.
- Presets are validated in Python with the same rules as `engine/src/preset.cpp` (the tests check
  both agree on every preset and on a corpus of broken ones), then checked with
  `fxrender --check` before anything is written.
- The UI only talks to `fxweb.engine.Engine`. A real JACK-backed engine implements the same three
  methods (`load`, `loaded`, `health`) and registers in `ENGINES`; run with `--engine <name>`.
- API: `GET /api/presets`, `GET|PUT|DELETE /api/presets/{id}`, `POST /api/presets` (save-as),
  `POST /api/engine/load`, `GET /api/engine`, `GET /api/health`, `GET /api/catalog`.
  Interactive docs at `/api/docs`.

#### Live page and recording

**Live** shows what the box hears, about 30 times a second: level meters with clip lights and
headroom, a tuner, a note-locked waveform, spectrum and 20 s spectrogram, a chord guess with
pitch-class bars, overtone levels, attacks and tempo, brightness, noise floor and mains hum, and
(with two channels) the **response** between them: gain per frequency, coherence and delay,
out ÷ in for an effect, mic ÷ pickup for the body-IR work. What each panel means and how it's
computed: [explainers/live-analysis.html](explainers/live-analysis.html) (also served at
`/explainers/live-analysis.html`).

- **Record / Stop** writes a take to `recordings/<id>.wav` (24-bit, every channel) with a JSON
  sidecar: name, notes, the preset that was loaded, peaks, clipped samples, markers (**Mark**),
  and any audio lost because the box fell behind. The header is patched every second, so a power
  cut leaves a playable file; the next start marks it "interrupted".
- **Save last 30 s** keeps what you already played: the last 90 s are always held in memory
  (`--retro-seconds`).
- **Takes** lists them with waveforms and markers, plays one channel at a time in the browser,
  renames, annotates, downloads and deletes.

Analysis and recording run in the web server, never in the audio callback. Sources
(`--live-source`, or the picker on the page):

| source | what it reads |
|--------|---------------|
| `tap` | the engine's signal tap (`fx/tap.h`, `/dev/shm/fxbox-tap`): Pisound in and out. Until the JACK engine exists, `./build/fxtapfeed --preset presets/slapback.json --in test_audio/strum.wav --loop` stands in for it |
| `alsa:DEVICE[:CH]` | a capture device through `arecord` (alsa-utils), e.g. the USB interface for pickup + mic. Not the Pisound once JACK owns it |
| `sim` | a synthetic 32 s guitar loop with known notes, chords at 96 BPM, a clipped strum and 60 Hz hum; "out" is rendered through the loaded preset by `fxrender` |

`auto` (the default) uses the tap if something is writing to it, otherwise `sim`. Analysis costs
about 1.4 ms per frame on the Mac (≈4% of one core at 30 frames/s), only while a page is watching.
API: `/api/live`, `/api/live/sources`, `/api/live/source`, `/api/live/stream` (Server-Sent Events),
`/api/live/record/{start,stop,mark,retro}`, `/api/recordings[/{id}[/audio?ch=N]]`.

On the Pi, once while it has internet: `make build && uv sync --extra webapp`. Then run it as a
service at low priority (`Nice=10`, idle I/O; uncomment `CPUAffinity=` to keep it off the audio
core once that's pinned):

```
sudo cp webapp/guitar-fx-web@.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now guitar-fx-web@$USER    # assumes the repo is at ~/vibe-guitar
```

### Offline (no internet): the Pi's own hotspot

If the Pi can't join home Wi-Fi within ~40 s of booting, it starts its own Wi-Fi network `fxbox`
(WPA2; password set on the Pi, not in the repo). Join it on the phone and open
`http://fxbox.local:8080` (or `http://10.42.0.1:8080`). Switch by hand with
`sudo fxbox-net home|hotspot`; `fxbox-net status` shows where it is. Setup, design and testing:
[pi/README.md](pi/README.md).

### Adding an effect

1. Copy `engine/effects/gain.cpp`: define `makeInfo()` (id, params with ranges and defaults),
   allocate in `prepare()`, clear in `reset()`, process in place in `process()`.
2. Declare `xxxRegistration()` in `engine/effects/effects.h` and add it to the list in
   `engine/src/registry.cpp`; add the .cpp to `fxcore` in `CMakeLists.txt`.
3. `make test`. The contract and block-size-invariance tests run against every registered
   effect and preset automatically.

Rules for `process()`: no allocation, locks, logging, exceptions or I/O; read each parameter
once per block; output must not depend on block size.

### Profiling numbers

Laptop timings check the plumbing only: macOS preempts the process, so the worst cases are
scheduler spikes, not DSP cost. Real buffer sizes come from running `make profile` on the Pi
(cooler on, CPU warm) with `--wakeup-us` set to the worst case from `cyclictest` (M3).
