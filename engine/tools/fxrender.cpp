// fxrender: run a preset over a WAV file offline, block by block, exactly as
// the live engine would, and time every block.
//
//   fxrender --preset presets/slapback.json --in test_audio/strum.wav \
//            --out out.wav --block 64 --stats-json stats.json
//   fxrender --check presets/*.json
//   fxrender --list-effects
//
// The timing is the profiling harness's raw measurement: worst-case
// process() time per block against the block's deadline. Laptop numbers only
// prove the plumbing; real numbers come from the Pi (M3).
#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <fstream>
#include <iostream>
#include <string>
#include <vector>

#include <nlohmann/json.hpp>

#include "fx/denormals.h"
#include "fx/preset.h"
#include "fx/wav.h"

using nlohmann::json;

namespace {

void usage() {
  std::cerr <<
      R"(usage:
  fxrender --preset FILE --in IN.wav [options]
      --out OUT.wav       write the processed audio (32-bit float)
      --block N           block size in frames (default: the preset's buffer_size)
      --channels N        engine channels (default 2; a mono input is copied to all)
      --warmup N          untimed passes before measuring (default 1)
      --repeat N          timed passes (default 3); --out is written from the first
      --stats-json FILE   write timing statistics as JSON
      --quiet             no summary on stdout
  fxrender --check FILE...    validate presets
  fxrender --list-effects     print the effect catalog as JSON
  fxrender --host             print the OS and CPU architecture this binary was built for
)";
}

std::string hostDescription() {
  std::string s;
#if defined(__APPLE__)
  s = "macos";
#elif defined(__linux__)
  s = "linux";
#else
  s = "unknown-os";
#endif
#if defined(__aarch64__)
  s += "-arm64";
#elif defined(__x86_64__)
  s += "-x86_64";
#endif
  return s;
}

double percentile(const std::vector<double>& sorted, double q) {
  if (sorted.empty()) return 0.0;
  const auto idx = static_cast<size_t>(std::ceil(q * static_cast<double>(sorted.size()))) - 1;
  return sorted[std::min(idx, sorted.size() - 1)];
}

int checkPresets(int argc, char** argv, int first) {
  int failures = 0;
  for (int i = first; i < argc; ++i) {
    try {
      const fx::Preset p = fx::loadPresetFile(argv[i]);
      fx::buildChain(p);
      std::cout << "ok    " << argv[i] << "  (" << p.name << ", " << p.chain.size()
                << " effects, buffer " << p.bufferSize << ")\n";
    } catch (const std::exception& e) {
      std::cout << "FAIL  " << e.what() << "\n";
      ++failures;
    }
  }
  return failures == 0 ? 0 : 1;
}

}  // namespace

int main(int argc, char** argv) {
  std::string presetPath, inPath, outPath, statsPath;
  int block = 0, channels = 2, warmup = 1, repeat = 3;
  bool quiet = false;

  try {
    for (int i = 1; i < argc; ++i) {
      const std::string a = argv[i];
      auto next = [&]() -> std::string {
        if (i + 1 >= argc) throw std::runtime_error("missing value for " + a);
        return argv[++i];
      };
      if (a == "--host") {
        std::cout << hostDescription() << "\n";
        return 0;
      } else if (a == "--list-effects") {
        std::cout << fx::effectCatalogJson().dump(2) << "\n";
        return 0;
      } else if (a == "--check") {
        return checkPresets(argc, argv, i + 1);
      } else if (a == "--preset") {
        presetPath = next();
      } else if (a == "--in") {
        inPath = next();
      } else if (a == "--out") {
        outPath = next();
      } else if (a == "--block") {
        block = std::stoi(next());
      } else if (a == "--channels") {
        channels = std::stoi(next());
      } else if (a == "--warmup") {
        warmup = std::stoi(next());
      } else if (a == "--repeat") {
        repeat = std::stoi(next());
      } else if (a == "--stats-json") {
        statsPath = next();
      } else if (a == "--quiet") {
        quiet = true;
      } else if (a == "-h" || a == "--help") {
        usage();
        return 0;
      } else {
        throw std::runtime_error("unknown argument " + a);
      }
    }
    if (presetPath.empty() || inPath.empty()) {
      usage();
      return 2;
    }
    if (channels < 1 || warmup < 0 || repeat < 1) throw std::runtime_error("bad numeric option");

    const fx::Preset preset = fx::loadPresetFile(presetPath);
    if (block == 0) block = preset.bufferSize;
    if (block < 1) throw std::runtime_error("block size must be positive");

    const fx::AudioBuffer input = fx::readWav(inPath);
    if (input.sampleRate != 48000) {
      std::cerr << "warning: " << inPath << " is " << input.sampleRate
                << " Hz; the box runs at 48000 Hz\n";
    }
    const int frames = input.numFrames();
    const double sr = input.sampleRate;

    fx::Chain chain = fx::buildChain(preset);
    chain.prepare(sr, block, channels);

    // All buffers allocated up front, as in the live engine.
    std::vector<std::vector<float>> buf(channels, std::vector<float>(block));
    std::vector<float*> ptrs(channels);
    for (int c = 0; c < channels; ++c) ptrs[c] = buf[c].data();

    fx::AudioBuffer output;
    output.sampleRate = input.sampleRate;
    if (!outPath.empty()) output.channels.assign(channels, std::vector<float>(frames));

    const int blocksPerPass = (frames + block - 1) / block;
    std::vector<double> times;  // microseconds, measured passes only
    times.reserve(static_cast<size_t>(blocksPerPass) * repeat);
    std::vector<int> blockIndex;
    blockIndex.reserve(times.capacity());

    const fx::ScopedFlushDenormals ftz;
    using Clock = std::chrono::steady_clock;

    for (int pass = 0; pass < warmup + repeat; ++pass) {
      chain.reset();
      const bool measured = pass >= warmup;
      const bool capture = pass == warmup && !outPath.empty();
      for (int b = 0; b < blocksPerPass; ++b) {
        const int start = b * block;
        const int n = std::min(block, frames - start);
        for (int c = 0; c < channels; ++c) {
          const auto& src = input.channels[c % input.numChannels()];
          std::copy_n(src.begin() + start, n, buf[c].begin());
        }
        const auto t0 = Clock::now();
        chain.process(ptrs.data(), channels, n);
        const auto t1 = Clock::now();
        if (measured) {
          times.push_back(std::chrono::duration<double, std::micro>(t1 - t0).count());
          blockIndex.push_back(b);
        }
        if (capture) {
          for (int c = 0; c < channels; ++c) {
            std::copy_n(buf[c].begin(), n, output.channels[c].begin() + start);
          }
        }
      }
    }

    // --- Statistics ---
    const size_t worstAt =
        static_cast<size_t>(std::max_element(times.begin(), times.end()) - times.begin());
    const int worstBlock = times.empty() ? 0 : blockIndex[worstAt];
    std::vector<double> sorted = times;
    std::sort(sorted.begin(), sorted.end());
    double sum = 0.0;
    for (double t : times) sum += t;
    const double budgetUs = block / sr * 1e6;
    const double maxUs = sorted.empty() ? 0.0 : sorted.back();

    float peak = 0.0f;
    long nonFinite = 0;
    for (const auto& ch : output.channels) {
      for (float v : ch) {
        if (!std::isfinite(v)) {
          ++nonFinite;
        } else {
          peak = std::max(peak, std::fabs(v));
        }
      }
    }

    json stats = {
        {"preset", preset.name},
        {"preset_file", presetPath},
        {"input", inPath},
        {"host", hostDescription()},
        {"sample_rate", input.sampleRate},
        {"block_size", block},
        {"channels", channels},
        {"frames", frames},
        {"warmup_passes", warmup},
        {"measured_passes", repeat},
        {"blocks_measured", times.size()},
        {"budget_us", budgetUs},
        {"mean_us", times.empty() ? 0.0 : sum / static_cast<double>(times.size())},
        {"p50_us", percentile(sorted, 0.50)},
        {"p99_us", percentile(sorted, 0.99)},
        {"p999_us", percentile(sorted, 0.999)},
        {"max_us", maxUs},
        {"max_load", maxUs / budgetUs},
        {"worst_block_time_s", worstBlock * block / sr},
    };
    if (!outPath.empty()) {
      stats["output_peak_dbfs"] = peak > 0.0f ? 20.0 * std::log10(peak) : -999.0;
      stats["output_nonfinite_samples"] = nonFinite;
      fx::writeWavFloat(output, outPath);
    }
    if (!statsPath.empty()) {
      std::ofstream f(statsPath);
      if (!f) throw std::runtime_error("cannot write " + statsPath);
      f << stats.dump(2) << "\n";
    }

    if (!quiet) {
      std::printf("%s | %s | block %d (%.0f us budget)\n", preset.name.c_str(), inPath.c_str(),
                  block, budgetUs);
      std::printf("  mean %.2f us   p99 %.2f us   max %.2f us   worst load %.1f%% at t=%.2fs\n",
                  stats["mean_us"].get<double>(), stats["p99_us"].get<double>(), maxUs,
                  100.0 * maxUs / budgetUs, stats["worst_block_time_s"].get<double>());
      if (!outPath.empty()) {
        std::printf("  wrote %s (peak %.1f dBFS)\n", outPath.c_str(),
                    stats["output_peak_dbfs"].get<double>());
        if (nonFinite) std::printf("  WARNING: %ld non-finite output samples\n", nonFinite);
      }
    }
    return nonFinite ? 1 : 0;
  } catch (const std::exception& e) {
    std::cerr << "fxrender: " << e.what() << "\n";
    return 1;
  }
}
