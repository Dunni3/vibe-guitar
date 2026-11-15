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
