# Guitar FX Box

Programmable effects for an acoustic guitar on a Raspberry Pi 5 + Pisound.
See [CLAUDE.md](CLAUDE.md) for the hardware, design decisions and milestones.

## Layout

```
engine/
  include/fx/effect.h     Effect interface + Chain (the contract every effect and host follows)
  include/fx/preset.h     Preset format, strict validation, chain building, effect catalog
  include/fx/denormals.h  Flush-to-zero for audio threads (aarch64 + x86)
  include/fx/wav.h        WAV I/O for offline tools
  effects/                Effects (gain.cpp is the template for new ones)
  src/                    Core library sources; registry.cpp lists all effects
  tools/fxrender.cpp      Offline renderer + per-block timer
  tests/tests.cpp         Unit tests
presets/                  Preset JSON files
harness/fxharness/        Python: test signal generator, profiling harness
webapp/fxweb/             Python web app: API, engine interface (FakeEngine for now), static UI
webapp/tests/             Web app tests, incl. Python-vs-C++ preset validator agreement
webapp/guitar-fx-web@.service   systemd unit for the Pi (niced, off the audio core)
test_audio/               Generated signals (gitignored); put real recordings in recordings/
profiles/                 Profiling output (gitignored)
```

## Setup

Mac: `brew install cmake nlohmann-json` and [uv](https://docs.astral.sh/uv/).
Pi (Raspberry Pi OS): `sudo apt install cmake g++ nlohmann-json3-dev` and uv.
If nlohmann-json isn't installed, CMake fetches a pinned copy.

## Commands

```
make test                 # build + unit tests
make check                # validate every preset
make profile              # generate test signals, profile all presets at 32/64/128/256

./build/fxrender --preset presets/slapback.json --in test_audio/strum.wav --out out.wav --block 64
./build/fxrender --list-effects        # effect + parameter catalog as JSON (for the web app)
uv run fx-profile --help
```

## Web app

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

On the Pi, once while it has internet: `make build && uv sync --extra webapp`. Then run it as a
service at low priority (`Nice=10`, idle I/O; uncomment `CPUAffinity=` to keep it off the audio
core once that's pinned):

```
sudo cp webapp/guitar-fx-web@.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now guitar-fx-web@$USER    # assumes the repo is at ~/vibe-guitar
```

## Adding an effect

1. Copy `engine/effects/gain.cpp`: define `makeInfo()` (id, params with ranges and defaults),
   allocate in `prepare()`, clear in `reset()`, process in place in `process()`.
2. Declare `xxxRegistration()` in `engine/effects/effects.h` and add it to the list in
   `engine/src/registry.cpp`; add the .cpp to `fxcore` in `CMakeLists.txt`.
3. `make test`. The contract and block-size-invariance tests run against every registered
   effect and preset automatically.

Rules for `process()`: no allocation, locks, logging, exceptions or I/O; read each parameter
once per block; output must not depend on block size.

## Profiling numbers

Laptop timings check the plumbing only: macOS preempts the process, so the worst cases are
scheduler spikes, not DSP cost. Real buffer sizes come from running `make profile` on the Pi
(cooler on, CPU warm) with `--wakeup-us` set to the worst case from `cyclictest` (M3).
