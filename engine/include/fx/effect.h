// The effect interface. Every effect (hand-written C++ or Faust-generated)
// implements this, and every host (offline renderer, JACK client) drives it.
//
// Threading contract
//   Control thread (non-real-time): construct, prepare(), reset(), setParam().
//   Audio thread (real-time):       process() only.
//   setParam() may be called while process() runs on another thread; values
//   are stored in atomics and an effect reads each one once per block.
//
// process() must never allocate, lock, log, throw or do I/O. Allocate
// everything (delay lines, scratch buffers) in prepare().
#pragma once

#include <atomic>
#include <memory>
#include <string>
#include <string_view>
#include <vector>

namespace fx {

struct ParamSpec {
  std::string id;    // stable key used in preset files, e.g. "gain_db"
  std::string name;  // display name for the web UI
  float min = 0.0f;
  float max = 1.0f;
  float def = 0.0f;
  std::string unit;  // "dB", "ms", "%", "Hz", or ""
};

struct EffectInfo {
  std::string id;  // stable key used in preset files, e.g. "delay"
  std::string name;
  std::string description;
  std::vector<ParamSpec> params;
};

class Effect {
 public:
  virtual ~Effect() = default;
  Effect(const Effect&) = delete;
  Effect& operator=(const Effect&) = delete;

  // Control thread. Allocate state for up to maxBlockSize frames and
  // numChannels channels. Called before the first process(), and again if
  // the host changes sample rate, block size or channel count. Implies reset().
  virtual void prepare(double sampleRate, int maxBlockSize, int numChannels) = 0;

  // Control thread. Clear internal state (delay lines, envelopes, filters).
  virtual void reset() = 0;

  // Audio thread. Process in place. channels[c][i] for c < numChannels,
  // i < numFrames. numFrames may be anything in [0, maxBlockSize];
  // numChannels equals the value passed to prepare().
  virtual void process(float* const* channels, int numChannels, int numFrames) noexcept = 0;

  const EffectInfo& info() const { return info_; }
  int numParams() const { return static_cast<int>(info_.params.size()); }
  int paramIndex(std::string_view id) const;  // -1 if unknown

  // Any thread. Clamped to the spec's [min, max].
  void setParam(int index, float value);
  float getParam(int index) const;

 protected:
  explicit Effect(EffectInfo info);

  // For process(): relaxed atomic load of a parameter's current value.
  float param(int index) const noexcept {
    return values_[index].load(std::memory_order_relaxed);
  }

 private:
  EffectInfo info_;
  std::unique_ptr<std::atomic<float>[]> values_;
};

// An ordered list of effects processed in place, one after another.
class Chain {
 public:
  void add(std::unique_ptr<Effect> effect) { effects_.push_back(std::move(effect)); }
  void prepare(double sampleRate, int maxBlockSize, int numChannels) {
    for (auto& e : effects_) e->prepare(sampleRate, maxBlockSize, numChannels);
  }
  void reset() {
    for (auto& e : effects_) e->reset();
  }
  void process(float* const* channels, int numChannels, int numFrames) noexcept {
    for (auto& e : effects_) e->process(channels, numChannels, numFrames);
  }
  int size() const { return static_cast<int>(effects_.size()); }
  Effect& at(int i) { return *effects_[i]; }

 private:
  std::vector<std::unique_ptr<Effect>> effects_;
};

}  // namespace fx
