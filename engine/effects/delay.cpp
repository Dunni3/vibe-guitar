// Feedback delay. Shows the prepare()-time allocation pattern: the delay line
// is sized for the maximum delay time up front, so process() never allocates.
// Delay time is read once per block and jumps (no smoothing); fine because
// presets change between songs, not while playing.
#include <cmath>
#include <vector>

#include "effects.h"

namespace fx {
namespace {

constexpr float kMaxDelayMs = 2000.0f;

class Delay final : public Effect {
 public:
  enum Param { kTimeMs, kFeedback, kMix };

  static EffectInfo makeInfo() {
    return {"delay",
            "Delay",
            "Feedback echo. Mix is the wet level; the dry signal always passes at full level.",
            {{"time_ms", "Time", 1.0f, kMaxDelayMs, 120.0f, "ms"},
             {"feedback", "Feedback", 0.0f, 95.0f, 20.0f, "%"},
             {"mix", "Mix", 0.0f, 100.0f, 25.0f, "%"}}};
  }

  Delay() : Effect(makeInfo()) {}

  void prepare(double sampleRate, int, int numChannels) override {
    sampleRate_ = sampleRate;
    const auto len = static_cast<size_t>(std::ceil(kMaxDelayMs * 1e-3 * sampleRate)) + 1;
    lines_.assign(static_cast<size_t>(numChannels), std::vector<float>(len, 0.0f));
    reset();
  }

  void reset() override {
    for (auto& line : lines_) std::fill(line.begin(), line.end(), 0.0f);
    writePos_ = 0;
  }

  void process(float* const* ch, int numChannels, int numFrames) noexcept override {
    if (lines_.empty() || numFrames <= 0) return;
    const size_t len = lines_[0].size();
    auto delay = static_cast<size_t>(std::lround(param(kTimeMs) * 1e-3 * sampleRate_));
    if (delay < 1) delay = 1;
    if (delay > len - 1) delay = len - 1;
    const float fb = param(kFeedback) * 0.01f;
    const float wet = param(kMix) * 0.01f;

    for (int c = 0; c < numChannels; ++c) {
      float* line = lines_[static_cast<size_t>(c)].data();
      float* x = ch[c];
      size_t w = writePos_;
      for (int i = 0; i < numFrames; ++i) {
        const size_t r = (w + len - delay) % len;
        const float delayed = line[r];
        line[w] = x[i] + fb * delayed;
        x[i] += wet * delayed;
        if (++w == len) w = 0;
      }
    }
    writePos_ = (writePos_ + static_cast<size_t>(numFrames)) % len;
  }

 private:
  double sampleRate_ = 48000.0;
  std::vector<std::vector<float>> lines_;
  size_t writePos_ = 0;
};

}  // namespace

Registration delayRegistration() {
  return {Delay::makeInfo(), [] { return std::make_unique<Delay>(); }};
}

}  // namespace fx
