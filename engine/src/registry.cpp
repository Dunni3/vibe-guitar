#include "fx/registry.h"

#include "effects.h"

namespace fx {

const std::vector<Registration>& registry() {
  static const std::vector<Registration> all = {
      gainRegistration(),
      delayRegistration(),
  };
  return all;
}

const Registration* findEffect(std::string_view id) {
  for (const auto& r : registry()) {
    if (r.info.id == id) return &r;
  }
  return nullptr;
}

}  // namespace fx
