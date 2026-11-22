// fxtapfeed: stand-in for the live engine's signal tap. Plays a WAV through a
// preset in real time, block by block, and publishes the dry input and the
// processed output to the shared-memory tap (fx/tap.h) that the web app's
// Live page reads. The real JACK engine will call TapWriter::write() from its
// process callback in exactly the same way.
//
//   fxtapfeed --preset presets/slapback.json --in test_audio/strum.wav --loop
//   fx-web --live-source tap          # in another terminal
#include <algorithm>
#include <atomic>
#include <chrono>
#include <csignal>
#include <cstdio>
#include <iostream>
#include <string>
#include <thread>
#include <vector>

#include "fx/denormals.h"
#include "fx/preset.h"
#include "fx/tap.h"
#include "fx/wav.h"

namespace {

std::atomic<bool> g_stop{false};
void onSignal(int) { g_stop = true; }

void usage() {
  std::cerr << R"(usage: fxtapfeed --preset FILE --in IN.wav [options]
    --tap PATH     tap file (default: )" << fx::defaultTapPath() << R"()
    --block N      block size in frames (default: the preset's buffer_size)
    --loop         start again at the end of the file (default: play once)
    --quiet        no status line
Tap channels: "in" (dry input, channel 0 of the WAV) and "out" (processed, left).
)";
}

}  // namespace

int main(int argc, char** argv) {
  std::string presetPath, inPath, tapPath = fx::defaultTapPath();
  int block = 0;
  bool loop = false, quiet = false;
  try {
    for (int i = 1; i < argc; ++i) {
      const std::string a = argv[i];
      auto next = [&]() -> std::string {
        if (i + 1 >= argc) throw std::runtime_error("missing value for " + a);
        return argv[++i];
      };
      if (a == "--preset") presetPath = next();
      else if (a == "--in") inPath = next();
      else if (a == "--tap") tapPath = next();
      else if (a == "--block") block = std::stoi(next());
      else if (a == "--loop") loop = true;
      else if (a == "--quiet") quiet = true;
      else if (a == "-h" || a == "--help") { usage(); return 0; }
      else throw std::runtime_error("unknown argument " + a);
    }
    if (presetPath.empty() || inPath.empty()) {
      usage();
      return 2;
    }

    const fx::Preset preset = fx::loadPresetFile(presetPath);
    if (block == 0) block = preset.bufferSize;
    if (block < 1) throw std::runtime_error("block size must be positive");
    const fx::AudioBuffer input = fx::readWav(inPath);
    const int frames = input.numFrames();
    if (frames == 0) throw std::runtime_error(inPath + " is empty");
    const int sr = input.sampleRate;
    constexpr int kEngineChannels = 2;

    fx::Chain chain = fx::buildChain(preset);
    chain.prepare(sr, block, kEngineChannels);

    std::vector<std::vector<float>> buf(kEngineChannels, std::vector<float>(block));
    std::vector<float> dry(block);
    std::vector<float*> ptrs(kEngineChannels);
    for (int c = 0; c < kEngineChannels; ++c) ptrs[c] = buf[c].data();

    fx::TapWriter tap;
    tap.open(tapPath, 2, sr, {"in", "out"});
    std::signal(SIGINT, onSignal);
    std::signal(SIGTERM, onSignal);
    if (!quiet) {
      std::printf("fxtapfeed: %s through \"%s\" at block %d -> %s (Ctrl-C to stop)\n",
                  inPath.c_str(), preset.name.c_str(), block, tapPath.c_str());
      std::fflush(stdout);
    }

    const fx::ScopedFlushDenormals ftz;
    using Clock = std::chrono::steady_clock;
    const auto blockDur = std::chrono::duration<double>(static_cast<double>(block) / sr);
    auto deadline = Clock::now();
    int pos = 0;
    long late = 0;
    while (!g_stop) {
      const int n = std::min(block, frames - pos);
      const auto& src = input.channels[0];
      std::copy_n(src.begin() + pos, n, dry.begin());
      for (int c = 0; c < kEngineChannels; ++c) {
        const auto& s = input.channels[c % input.numChannels()];
        std::copy_n(s.begin() + pos, n, buf[c].begin());
      }
      chain.process(ptrs.data(), kEngineChannels, n);
      const float* tapCh[2] = {dry.data(), buf[0].data()};
      tap.write(tapCh, 2, n);

      pos += n;
      if (pos >= frames) {
        if (!loop) break;
        pos = 0;
      }
      deadline += std::chrono::duration_cast<Clock::duration>(blockDur * (static_cast<double>(n) / block));
      if (Clock::now() > deadline + std::chrono::milliseconds(50)) {
        ++late;  // we fell far behind (laptop asleep, debugger): resync, don't burst
        deadline = Clock::now();
      }
      std::this_thread::sleep_until(deadline);
    }
    if (!quiet) {
      std::printf("fxtapfeed: wrote %.1f s%s\n", static_cast<double>(tap.framesWritten()) / sr,
                  late ? " (resynced after falling behind)" : "");
    }
    return 0;
  } catch (const std::exception& e) {
    std::cerr << "fxtapfeed: " << e.what() << "\n";
    return 1;
  }
}
