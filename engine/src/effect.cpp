#include "fx/effect.h"

#include <algorithm>
#include <stdexcept>

namespace fx {

Effect::Effect(EffectInfo info)
    : info_(std::move(info)),
      values_(std::make_unique<std::atomic<float>[]>(info_.params.size())) {
  for (size_t i = 0; i < info_.params.size(); ++i) {
    values_[i].store(info_.params[i].def, std::memory_order_relaxed);
  }
}

int Effect::paramIndex(std::string_view id) const {
  for (size_t i = 0; i < info_.params.size(); ++i) {
    if (info_.params[i].id == id) return static_cast<int>(i);
  }
  return -1;
}

void Effect::setParam(int index, float value) {
  if (index < 0 || index >= numParams()) throw std::out_of_range("param index");
  const ParamSpec& spec = info_.params[index];
  values_[index].store(std::clamp(value, spec.min, spec.max), std::memory_order_relaxed);
}

float Effect::getParam(int index) const {
  if (index < 0 || index >= numParams()) throw std::out_of_range("param index");
  return values_[index].load(std::memory_order_relaxed);
}

}  // namespace fx
