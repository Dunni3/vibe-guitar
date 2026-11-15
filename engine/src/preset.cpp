#include "fx/preset.h"

#include <fstream>
#include <sstream>

#include <nlohmann/json.hpp>

#include "fx/registry.h"

namespace fx {

using nlohmann::json;

namespace {

bool isPowerOfTwo(int n) { return n > 0 && (n & (n - 1)) == 0; }

[[noreturn]] void fail(const std::string& msg) { throw PresetError(msg); }

}  // namespace

Preset parsePreset(const json& j) {
  if (!j.is_object()) fail("preset must be a JSON object");

  static const char* const kKnownKeys[] = {"format", "name", "description", "buffer_size",
                                           "chain"};
  for (const auto& [key, _] : j.items()) {
    bool known = false;
    for (const char* k : kKnownKeys) known = known || key == k;
    if (!known) fail("unknown preset key '" + key + "'");
  }

  if (!j.contains("format") || !j["format"].is_number_integer() ||
      j["format"].get<int>() != kPresetFormat) {
    fail("'format' must be " + std::to_string(kPresetFormat));
  }

  Preset p;
  if (!j.contains("name") || !j["name"].is_string() || j["name"].get<std::string>().empty()) {
    fail("'name' must be a non-empty string");
  }
  p.name = j["name"].get<std::string>();

  if (j.contains("description")) {
    if (!j["description"].is_string()) fail("'description' must be a string");
    p.description = j["description"].get<std::string>();
  }

  if (j.contains("buffer_size")) {
    if (!j["buffer_size"].is_number_integer()) fail("'buffer_size' must be an integer");
    p.bufferSize = j["buffer_size"].get<int>();
  }
  if (!isPowerOfTwo(p.bufferSize) || p.bufferSize < kMinBufferSize ||
      p.bufferSize > kMaxBufferSize) {
    fail("'buffer_size' must be a power of two in [" + std::to_string(kMinBufferSize) + ", " +
         std::to_string(kMaxBufferSize) + "], got " + std::to_string(p.bufferSize));
  }

  if (!j.contains("chain") || !j["chain"].is_array()) fail("'chain' must be an array");
  int slotIndex = 0;
  for (const auto& s : j["chain"]) {
    const std::string where = "chain[" + std::to_string(slotIndex++) + "]";
    if (!s.is_object()) fail(where + " must be an object");
    for (const auto& [key, _] : s.items()) {
      if (key != "effect" && key != "params") fail(where + ": unknown key '" + key + "'");
    }
    if (!s.contains("effect") || !s["effect"].is_string()) {
      fail(where + ": 'effect' must be a string");
    }
    EffectSlot slot;
    slot.effect = s["effect"].get<std::string>();
    const Registration* reg = findEffect(slot.effect);
    if (!reg) fail(where + ": unknown effect '" + slot.effect + "'");

    if (s.contains("params")) {
      if (!s["params"].is_object()) fail(where + ": 'params' must be an object");
      for (const auto& [id, value] : s["params"].items()) {
        const ParamSpec* spec = nullptr;
        for (const auto& ps : reg->info.params) {
          if (ps.id == id) spec = &ps;
        }
        if (!spec) fail(where + ": effect '" + slot.effect + "' has no param '" + id + "'");
        if (!value.is_number()) fail(where + "." + id + " must be a number");
        const float v = value.get<float>();
        if (v < spec->min || v > spec->max) {
          std::ostringstream msg;
          msg << where << "." << id << " = " << v << " is outside [" << spec->min << ", "
              << spec->max << "]";
          fail(msg.str());
        }
        slot.params[id] = v;
      }
    }
    p.chain.push_back(std::move(slot));
  }
  return p;
}

Preset parsePresetString(const std::string& text) {
  json j;
  try {
    j = json::parse(text);
  } catch (const json::parse_error& e) {
    fail(std::string("invalid JSON: ") + e.what());
  }
  return parsePreset(j);
}

Preset loadPresetFile(const std::string& path) {
  std::ifstream in(path);
  if (!in) fail("cannot open preset '" + path + "'");
  std::stringstream buf;
  buf << in.rdbuf();
  try {
    return parsePresetString(buf.str());
  } catch (const PresetError& e) {
    fail(path + ": " + e.what());
  }
}

json presetToJson(const Preset& p) {
  json chain = json::array();
  for (const auto& slot : p.chain) {
    json params = json::object();
    for (const auto& [id, v] : slot.params) params[id] = v;
    chain.push_back({{"effect", slot.effect}, {"params", params}});
  }
  return {{"format", kPresetFormat},
          {"name", p.name},
          {"description", p.description},
          {"buffer_size", p.bufferSize},
          {"chain", chain}};
}

void savePresetFile(const Preset& p, const std::string& path) {
  std::ofstream out(path);
  if (!out) fail("cannot write preset '" + path + "'");
  out << presetToJson(p).dump(2) << "\n";
}

Chain buildChain(const Preset& p) {
  Chain chain;
  for (const auto& slot : p.chain) {
    const Registration* reg = findEffect(slot.effect);
    if (!reg) fail("unknown effect '" + slot.effect + "'");
    auto effect = reg->make();
    for (const auto& [id, v] : slot.params) {
      const int idx = effect->paramIndex(id);
      if (idx < 0) fail("effect '" + slot.effect + "' has no param '" + id + "'");
      effect->setParam(idx, v);
    }
    chain.add(std::move(effect));
  }
  return chain;
}

json effectCatalogJson() {
  json effects = json::array();
  for (const auto& r : registry()) {
    json params = json::array();
    for (const auto& ps : r.info.params) {
      params.push_back({{"id", ps.id},
                        {"name", ps.name},
                        {"min", ps.min},
                        {"max", ps.max},
                        {"default", ps.def},
                        {"unit", ps.unit}});
    }
    effects.push_back({{"id", r.info.id},
                       {"name", r.info.name},
                       {"description", r.info.description},
                       {"params", params}});
  }
  return {{"preset_format", kPresetFormat}, {"effects", effects}};
}

}  // namespace fx
