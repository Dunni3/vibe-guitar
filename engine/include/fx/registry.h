// The list of available effects. To add an effect: write it in
// engine/effects/, declare its registration function in effects.h, and add it
// to the list in engine/src/registry.cpp. (Explicit list, not static
// self-registration, because the linker drops unreferenced objects from a
// static library.)
#pragma once

#include <functional>
#include <memory>
#include <string_view>
#include <vector>

#include "fx/effect.h"

namespace fx {

struct Registration {
  EffectInfo info;
  std::function<std::unique_ptr<Effect>()> make;
};

const std::vector<Registration>& registry();
const Registration* findEffect(std::string_view id);  // nullptr if unknown

}  // namespace fx
