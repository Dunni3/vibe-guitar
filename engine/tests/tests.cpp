// Unit tests. Tiny self-contained runner: no test framework dependency.
//   ctest --test-dir build --output-on-failure     (or ./build/fxtests)
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <filesystem>
#include <fstream>
#include <functional>
#include <random>
#include <string>
#include <vector>

#include <nlohmann/json.hpp>

#include "fx/denormals.h"
#include "fx/preset.h"
#include "fx/registry.h"
#include "fx/wav.h"

namespace fs = std::filesystem;
using Planar = std::vector<std::vector<float>>;

// --- Minimal test runner ---------------------------------------------------

namespace {
int g_failures = 0;

struct TestCase {
  const char* name;
  void (*fn)();
};
std::vector<TestCase>& allTests() {
  static std::vector<TestCase> t;
  return t;
}
struct Registrar {
  Registrar(const char* name, void (*fn)()) { allTests().push_back({name, fn}); }
};
}  // namespace

#define TEST(name)                               \
  static void name();                            \
  static const Registrar reg_##name(#name, name); \
  static void name()

#define CHECK(cond)                                                              \
  do {                                                                           \
    if (!(cond)) {                                                               \
      std::fprintf(stderr, "  %s:%d: CHECK(%s) failed\n", __FILE__, __LINE__, #cond); \
      ++g_failures;                                                              \
    }                                                                            \
  } while (0)

#define CHECK_NEAR(a, b, tol)                                                         \
  do {                                                                                \
    const double a_ = (a), b_ = (b);                                                  \
    if (!(std::fabs(a_ - b_) <= (tol))) {                                             \
      std::fprintf(stderr, "  %s:%d: %s = %.9g, expected %.9g (tol %g)\n", __FILE__, \
                   __LINE__, #a, a_, b_, static_cast<double>(tol));                   \
      ++g_failures;                                                                   \
    }                                                                                 \
  } while (0)

template <class E, class F>
static void checkThrows(F&& f, const std::string& mustContain, int line) {
  try {
    f();
  } catch (const E& e) {
    if (std::string(e.what()).find(mustContain) == std::string::npos) {
      std::fprintf(stderr, "  line %d: error '%s' does not mention '%s'\n", line, e.what(),
                   mustContain.c_str());
      ++g_failures;
    }
    return;
  }
  std::fprintf(stderr, "  line %d: expected an exception mentioning '%s'\n", line,
               mustContain.c_str());
  ++g_failures;
}

// --- Helpers ---------------------------------------------------------------

constexpr double kSr = 48000.0;

// Render planar input through a chain in blocks, the way a host does.
static Planar render(fx::Chain& chain, const Planar& in, int block) {
  const int nch = static_cast<int>(in.size());
  const int frames = static_cast<int>(in[0].size());
  chain.prepare(kSr, block, nch);
  Planar out = in;
  std::vector<float*> ptrs(nch);
  for (int start = 0; start < frames; start += block) {
    for (int c = 0; c < nch; ++c) ptrs[c] = out[c].data() + start;
    chain.process(ptrs.data(), nch, std::min(block, frames - start));
  }
  return out;
}

static fx::Chain chainOf(const std::string& effect,
                         const std::vector<std::pair<std::string, float>>& params) {
  fx::Preset p;
  p.name = "test";
  fx::EffectSlot slot{effect, {}};
  for (const auto& [k, v] : params) slot.params[k] = v;
  p.chain.push_back(slot);
  return fx::buildChain(p);
}

static Planar noise(int channels, int frames, unsigned seed, float amp = 0.5f) {
  std::mt19937 rng(seed);
  std::uniform_real_distribution<float> d(-amp, amp);
  Planar x(channels, std::vector<float>(frames));
  for (auto& ch : x)
    for (auto& v : ch) v = d(rng);
  return x;
}

// --- Effects ---------------------------------------------------------------

TEST(gain_scales_by_decibels) {
  auto chain = chainOf("gain", {{"gain_db", -6.0206f}});
  const Planar in = noise(2, 1000, 1);
  const Planar out = render(chain, in, 64);
  for (int c = 0; c < 2; ++c)
    for (int i = 0; i < 1000; ++i) CHECK_NEAR(out[c][i], 0.5 * in[c][i], 1e-6);
}

TEST(gain_ramps_on_change_without_jumping) {
  auto chain = chainOf("gain", {{"gain_db", 0.0f}});
  chain.prepare(kSr, 64, 1);
  std::vector<float> x(64, 1.0f);
  float* p = x.data();
  chain.process(&p, 1, 64);
  CHECK_NEAR(x[63], 1.0, 1e-6);

  chain.at(0).setParam(0, -6.0206f);  // halve
  std::fill(x.begin(), x.end(), 1.0f);
  chain.process(&p, 1, 64);
  for (int i = 1; i < 64; ++i) CHECK(x[i] < x[i - 1]);  // smooth ramp down
  CHECK(x[0] < 1.0f && x[0] > 0.98f);
  CHECK_NEAR(x[63], 0.5, 1e-5);
}

TEST(delay_impulse_response) {
  auto chain = chainOf("delay", {{"time_ms", 10.0f}, {"feedback", 50.0f}, {"mix", 100.0f}});
  Planar in(1, std::vector<float>(2000, 0.0f));
  in[0][0] = 1.0f;
  const Planar out = render(chain, in, 64);
  // Dry impulse at 0; echoes every 480 samples (10 ms), halving each time.
  for (int i = 0; i < 2000; ++i) {
    double expected = 0.0;
    if (i == 0) expected = 1.0;
    if (i > 0 && i % 480 == 0) expected = std::pow(0.5, i / 480 - 1);
    CHECK_NEAR(out[0][i], expected, 1e-6);
  }
}

TEST(delay_tail_never_goes_denormal) {
  const fx::ScopedFlushDenormals ftz;
  auto chain = chainOf("delay", {{"time_ms", 1.0f}, {"feedback", 95.0f}, {"mix", 100.0f}});
  Planar in(1, std::vector<float>(200000, 0.0f));
  in[0][0] = 1.0f;
  const Planar out = render(chain, in, 128);
  int subnormals = 0;
  for (float v : out[0]) subnormals += std::fpclassify(v) == FP_SUBNORMAL;
  CHECK(subnormals == 0);
  CHECK(out[0].back() == 0.0f);
}

// Every registered effect, at default/min/max params, must survive any block
// size from 0 up to the prepared maximum and produce finite output.
TEST(effect_contract_all_registered_effects) {
  for (const auto& reg : fx::registry()) {
    for (int setting = 0; setting < 3; ++setting) {
      auto e = reg.make();
      for (int i = 0; i < e->numParams(); ++i) {
        const auto& spec = reg.info.params[i];
        if (setting == 1) e->setParam(i, spec.min);
        if (setting == 2) e->setParam(i, spec.max);
      }
      e->prepare(kSr, 64, 1);  // re-prepare with different sizes must work
      e->prepare(kSr, 256, 2);
      Planar x = noise(2, 256, 7, 1.0f);
      std::vector<float*> ptrs{x[0].data(), x[1].data()};
      std::mt19937 rng(3);
      std::uniform_int_distribution<int> size(0, 256);
      bool finite = true;
      for (int b = 0; b < 2000; ++b) {
        const int n = b < 3 ? b : size(rng);  // include 0, 1, 2
        e->process(ptrs.data(), 2, n);
        for (auto& ch : x)
          for (int i = 0; i < n; ++i) {
            finite = finite && std::isfinite(ch[i]) && std::fabs(ch[i]) < 1e4f;
            ch[i] = 0.1f * (static_cast<float>(i % 17) - 8.0f);  // fresh input
          }
      }
      e->reset();
      if (!finite) std::fprintf(stderr, "  effect '%s' setting %d\n", reg.info.id.c_str(), setting);
      CHECK(finite);
    }
  }
}

// Output must not depend on the buffer size, so profiling can pick any safe
// size without changing the sound.
TEST(every_preset_is_block_size_invariant) {
  const Planar in = [] {
    Planar x = noise(2, 30000, 11);
    x[0][100] = 1.0f;
    return x;
  }();
  for (const auto& entry : fs::directory_iterator(FX_PRESETS_DIR)) {
    if (entry.path().extension() != ".json") continue;
    const fx::Preset p = fx::loadPresetFile(entry.path().string());
    auto ref = fx::buildChain(p);
    const Planar expected = render(ref, in, 64);
    for (int block : {1, 7, 32, 128, 256, 1000}) {
      auto chain = fx::buildChain(p);
      const Planar got = render(chain, in, block);
      double maxDiff = 0.0;
      for (int c = 0; c < 2; ++c)
        for (size_t i = 0; i < in[0].size(); ++i)
          maxDiff = std::max(maxDiff, static_cast<double>(std::fabs(got[c][i] - expected[c][i])));
      if (maxDiff > 1e-6) {
        std::fprintf(stderr, "  %s at block %d differs by %g\n", entry.path().filename().c_str(),
                     block, maxDiff);
      }
      CHECK(maxDiff <= 1e-6);
    }
  }
}

// --- Presets ---------------------------------------------------------------

TEST(all_preset_files_load) {
  int count = 0;
  for (const auto& entry : fs::directory_iterator(FX_PRESETS_DIR)) {
    if (entry.path().extension() != ".json") continue;
    const fx::Preset p = fx::loadPresetFile(entry.path().string());
    CHECK(!p.name.empty());
    CHECK(fx::buildChain(p).size() == static_cast<int>(p.chain.size()));
    ++count;
  }
  CHECK(count > 0);
}

TEST(preset_round_trips_through_json) {
  const fx::Preset a = fx::parsePresetString(R"({
    "format": 1, "name": "X", "description": "d", "buffer_size": 64,
    "chain": [{"effect": "delay", "params": {"time_ms": 300, "mix": 10}}, {"effect": "gain"}]
  })");
  const fx::Preset b = fx::parsePreset(fx::presetToJson(a));
  CHECK(b.name == "X" && b.description == "d" && b.bufferSize == 64);
  CHECK(b.chain.size() == 2);
  CHECK(b.chain[0].effect == "delay" && b.chain[0].params.at("time_ms") == 300.0f);
  CHECK(b.chain[0].params.size() == 2);
  CHECK(b.chain[1].effect == "gain" && b.chain[1].params.empty());
}

TEST(preset_defaults_fill_missing_params) {
  auto chain = fx::buildChain(fx::parsePresetString(
      R"({"format": 1, "name": "X", "chain": [{"effect": "delay", "params": {"mix": 10}}]})"));
  fx::Effect& d = chain.at(0);
  CHECK(d.getParam(d.paramIndex("mix")) == 10.0f);
  CHECK(d.getParam(d.paramIndex("time_ms")) == 120.0f);
}

TEST(preset_validation_errors) {
  auto bad = [](const std::string& text, const std::string& why, int line) {
    checkThrows<fx::PresetError>([&] { fx::parsePresetString(text); }, why, line);
  };
  const std::string head = R"("format": 1, "name": "X", )";
  bad("not json", "invalid JSON", __LINE__);
  bad("[]", "JSON object", __LINE__);
  bad(R"({"name": "X", "chain": []})", "format", __LINE__);
  bad(R"({"format": 2, "name": "X", "chain": []})", "format", __LINE__);
  bad(R"({"format": 1, "chain": []})", "name", __LINE__);
  bad("{" + head + R"("chain": {}})", "chain", __LINE__);
  bad("{" + head + R"("chain": [], "colour": 1})", "unknown preset key 'colour'", __LINE__);
  bad("{" + head + R"("buffer_size": 100, "chain": []})", "power of two", __LINE__);
  bad("{" + head + R"("buffer_size": 8, "chain": []})", "power of two", __LINE__);
  // Huge values must not be narrowed to int first (2^32 + 128 used to pass as 128).
  bad("{" + head + R"("buffer_size": 4294967424, "chain": []})", "got 4294967424", __LINE__);
  bad("{" + head + R"("buffer_size": -4294967168, "chain": []})", "power of two", __LINE__);
  bad("{" + head + R"("buffer_size": 18446744073709551615, "chain": []})", "power of two", __LINE__);
  bad("{" + head + R"("chain": [{"effect": "fuzz"}]})", "unknown effect 'fuzz'", __LINE__);
  bad("{" + head + R"("chain": [{"effect": "gain", "params": {"gian_db": 1}}]})", "no param 'gian_db'", __LINE__);
  bad("{" + head + R"("chain": [{"effect": "gain", "params": {"gain_db": 99}}]})", "outside", __LINE__);
  bad("{" + head + R"("chain": [{"effect": "gain", "params": {"gain_db": "loud"}}]})", "number", __LINE__);
  bad("{" + head + R"("chain": [{"effect": "gain", "bypass": true}]})", "unknown key 'bypass'", __LINE__);
}

TEST(effect_catalog_lists_every_effect) {
  const auto cat = fx::effectCatalogJson();
  CHECK(cat["effects"].size() == fx::registry().size());
  for (const auto& e : cat["effects"]) CHECK(fx::findEffect(e["id"].get<std::string>()) != nullptr);
}

// --- Denormals -------------------------------------------------------------

TEST(scoped_flush_denormals) {
  const bool before = fx::denormalsAreFlushed();
  {
    const fx::ScopedFlushDenormals ftz;
    CHECK(fx::denormalsAreFlushed());
  }
  CHECK(fx::denormalsAreFlushed() == before);
}

// --- WAV I/O ---------------------------------------------------------------

TEST(wav_float_round_trip) {
  fx::AudioBuffer a;
  a.sampleRate = 44100;
  a.channels = noise(2, 777, 5);
  const auto path = (fs::temp_directory_path() / "fxtests_roundtrip.wav").string();
  fx::writeWavFloat(a, path);
  const fx::AudioBuffer b = fx::readWav(path);
  fs::remove(path);
  CHECK(b.sampleRate == 44100 && b.numChannels() == 2 && b.numFrames() == 777);
  CHECK(a.channels == b.channels);
}

static void writeBytes(const std::string& path, const std::vector<uint8_t>& bytes) {
  std::ofstream f(path, std::ios::binary);
  f.write(reinterpret_cast<const char*>(bytes.data()), static_cast<std::streamsize>(bytes.size()));
}

static std::vector<uint8_t> pcmWav(int bits, const std::vector<uint8_t>& sampleBytes) {
  auto le32 = [](uint32_t v) {
    return std::vector<uint8_t>{uint8_t(v), uint8_t(v >> 8), uint8_t(v >> 16), uint8_t(v >> 24)};
  };
  std::vector<uint8_t> w = {'R', 'I', 'F', 'F'};
  auto app = [&](const std::vector<uint8_t>& v) { w.insert(w.end(), v.begin(), v.end()); };
  app(le32(static_cast<uint32_t>(36 + 4 + 12 + sampleBytes.size())));  // includes a LIST chunk
  app({'W', 'A', 'V', 'E', 'f', 'm', 't', ' '});
  app(le32(16));
  const auto bps = static_cast<uint32_t>(bits / 8);
  app({1, 0, 1, 0});  // PCM, mono
  app(le32(48000));
  app(le32(48000 * bps));
  app({static_cast<uint8_t>(bps), 0, static_cast<uint8_t>(bits), 0});
  app({'L', 'I', 'S', 'T'});  // an unknown chunk the reader must skip
  app(le32(4));
  app({'I', 'N', 'F', 'O'});
  app({'d', 'a', 't', 'a'});
  app(le32(static_cast<uint32_t>(sampleBytes.size())));
  app(sampleBytes);
  return w;
}

TEST(wav_reads_16_and_24_bit_pcm) {
  const auto path = (fs::temp_directory_path() / "fxtests_pcm.wav").string();

  writeBytes(path, pcmWav(16, {0x00, 0x40, 0x00, 0x80, 0xFF, 0x7F}));  // 0.5, -1, ~1
  fx::AudioBuffer a = fx::readWav(path);
  CHECK(a.numFrames() == 3 && a.sampleRate == 48000);
  CHECK_NEAR(a.channels[0][0], 0.5, 1e-9);
  CHECK_NEAR(a.channels[0][1], -1.0, 1e-9);
  CHECK_NEAR(a.channels[0][2], 32767.0 / 32768.0, 1e-9);

  writeBytes(path, pcmWav(24, {0x00, 0x00, 0x40, 0x00, 0x00, 0x80, 0x01, 0x00, 0x00}));
  a = fx::readWav(path);
  fs::remove(path);
  CHECK(a.numFrames() == 3);
  CHECK_NEAR(a.channels[0][0], 0.5, 1e-9);
  CHECK_NEAR(a.channels[0][1], -1.0, 1e-9);
  CHECK_NEAR(a.channels[0][2], 1.0 / 8388608.0, 1e-12);
}

TEST(wav_rejects_non_wav) {
  const auto path = (fs::temp_directory_path() / "fxtests_bad.wav").string();
  writeBytes(path, {'n', 'o', 'p', 'e'});
  checkThrows<fx::WavError>([&] { fx::readWav(path); }, "not a RIFF", __LINE__);
  fs::remove(path);
}

// --- Main ------------------------------------------------------------------

int main() {
  int failedTests = 0;
  for (const auto& t : allTests()) {
    const int before = g_failures;
    t.fn();
    const bool ok = g_failures == before;
    failedTests += !ok;
    std::printf("%s  %s\n", ok ? "pass" : "FAIL", t.name);
  }
  std::printf("\n%zu tests, %d failed\n", allTests().size(), failedTests);
  return failedTests == 0 ? 0 : 1;
}
