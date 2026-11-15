// Presets: JSON files in presets/ that name a chain of effects, their
// parameters, and the buffer size to run at.
//
//   {
//     "format": 1,
//     "name": "Slapback",
//     "description": "Short single echo.",
//     "buffer_size": 128,
//     "chain": [
//       {"effect": "gain",  "params": {"gain_db": -2}},
//       {"effect": "delay", "params": {"time_ms": 110, "feedback": 10, "mix": 30}}
//     ]
//   }
//
// Validation is strict (unknown effects/params and out-of-range values are
// errors, not silently clamped) because presets are often machine-written.
// Params left out take the effect's default.
//
// All of this runs on the control thread: parsing and building allocate.
#pragma once

#include <map>
#include <stdexcept>
#include <string>
#include <vector>

#include <nlohmann/json_fwd.hpp>

#include "fx/effect.h"

namespace fx {

constexpr int kPresetFormat = 1;
constexpr int kMinBufferSize = 16;
constexpr int kMaxBufferSize = 2048;

struct EffectSlot {
  std::string effect;
  std::map<std::string, float> params;  // only the ones given in the file
};

struct Preset {
  std::string name;
  std::string description;
  int bufferSize = 128;  // smallest safe buffer; set by profiling (M3)
  std::vector<EffectSlot> chain;
};

class PresetError : public std::runtime_error {
 public:
  using std::runtime_error::runtime_error;
};

Preset parsePreset(const nlohmann::json& j);  // throws PresetError
Preset parsePresetString(const std::string& text);
Preset loadPresetFile(const std::string& path);

nlohmann::json presetToJson(const Preset& p);
void savePresetFile(const Preset& p, const std::string& path);

// Instantiate effects and apply parameters. The caller must still call
// chain.prepare() before processing.
Chain buildChain(const Preset& p);

// Every registered effect and its parameter specs, for the web UI.
nlohmann::json effectCatalogJson();

}  // namespace fx
