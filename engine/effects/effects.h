// One registration function per effect, listed in engine/src/registry.cpp.
#pragma once

#include "fx/registry.h"

namespace fx {

Registration gainRegistration();
Registration delayRegistration();

}  // namespace fx
