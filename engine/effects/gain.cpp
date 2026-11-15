// Gain: the simplest possible effect, and the template for new ones.
// Ramps linearly across a block when the gain changes, to avoid zipper noise.
#include <cmath>

#include "effects.h"

namespace fx {
namespace {

float dbToLinear(float db) { return std::pow(10.0f, db / 20.0f); }

class Gain final : public Effect {
 public:
  enum Param { kGainDb };

  static EffectInfo makeInfo() {
    return {"gain", "Gain", "Volume trim in decibels.",
            {{"gain_db", "Gain", -60.0f, 24.0f, 0.0f, "dB"}}};
  }

  Gain() : Effect(makeInfo()) {}

  void prepare(double, int, int) override { reset(); }
  void reset() override { current_ = dbToLinear(param(kGainDb)); }

  void process(float* const* ch, int numChannels, int numFrames) noexcept override {
    if (numFrames <= 0) return;
    const float target = dbToLinear(param(kGainDb));
    const float step = (target - current_) / static_cast<float>(numFrames);
    for (int c = 0; c < numChannels; ++c) {
      float g = current_;
      float* x = ch[c];
      for (int i = 0; i < numFrames; ++i) {
        g += step;
        x[i] *= g;
      }
    }
    current_ = target;
  }

 private:
  float current_ = 1.0f;
};

}  // namespace

Registration gainRegistration() {
  return {Gain::makeInfo(), [] { return std::make_unique<Gain>(); }};
}

}  // namespace fx
