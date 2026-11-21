cmake_minimum_required(VERSION 3.20)

# On Apple silicon, build arm64 even if CMake itself runs under Rosetta
# (Intel Homebrew), so the laptop runs the same aarch64 code paths as the Pi.
if(CMAKE_HOST_SYSTEM_NAME STREQUAL "Darwin" AND NOT CMAKE_OSX_ARCHITECTURES)
  execute_process(COMMAND sysctl -n hw.optional.arm64
                  OUTPUT_VARIABLE FX_HOST_ARM64 OUTPUT_STRIP_TRAILING_WHITESPACE ERROR_QUIET)
  if(FX_HOST_ARM64 STREQUAL "1")
    set(CMAKE_OSX_ARCHITECTURES arm64 CACHE STRING "Target architecture")
  endif()
endif()

project(guitar_fx LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_EXPORT_COMPILE_COMMANDS ON)

# Timing numbers only mean something in an optimized build.
if(NOT CMAKE_BUILD_TYPE AND NOT CMAKE_CONFIGURATION_TYPES)
  set(CMAKE_BUILD_TYPE Release CACHE STRING "Build type" FORCE)
endif()

option(FX_BUILD_TESTS "Build unit tests" ON)

# JSON for presets. Homebrew: nlohmann-json. Raspberry Pi OS: nlohmann-json3-dev.
find_package(nlohmann_json 3.10 QUIET)
if(NOT nlohmann_json_FOUND)
  message(STATUS "nlohmann_json not found; fetching v3.11.3")
  include(FetchContent)
  FetchContent_Declare(json
    URL https://github.com/nlohmann/json/releases/download/v3.11.3/json.tar.xz
    URL_HASH SHA256=d6c65aca6b1ed68e7a182f4757257b107ae403032760ed6ef121c9d55e81757d)
  FetchContent_MakeAvailable(json)
endif()

set(FX_WARNINGS -Wall -Wextra -Wpedantic -Wshadow -Wconversion -Wno-sign-conversion)

# --- Core library: effect interface, registry, chain, presets, WAV I/O, effects ---
add_library(fxcore STATIC
  engine/src/effect.cpp
  engine/src/registry.cpp
  engine/src/preset.cpp
  engine/src/wav.cpp
  engine/src/tap.cpp
  engine/effects/gain.cpp
  engine/effects/delay.cpp
)
target_include_directories(fxcore PUBLIC engine/include PRIVATE engine/effects)
target_link_libraries(fxcore PUBLIC nlohmann_json::nlohmann_json)
target_compile_options(fxcore PRIVATE ${FX_WARNINGS})

# --- Offline renderer + block timer (runs on laptop and Pi) ---
add_executable(fxrender engine/tools/fxrender.cpp)
target_link_libraries(fxrender PRIVATE fxcore)
target_compile_options(fxrender PRIVATE ${FX_WARNINGS})

# --- Tap feeder: stand-in for the live engine's signal tap (see fx/tap.h) ---
add_executable(fxtapfeed engine/tools/fxtapfeed.cpp)
target_link_libraries(fxtapfeed PRIVATE fxcore)
target_compile_options(fxtapfeed PRIVATE ${FX_WARNINGS})

if(FX_BUILD_TESTS)
  enable_testing()
  add_executable(fxtests engine/tests/tests.cpp)
  target_link_libraries(fxtests PRIVATE fxcore)
  target_compile_definitions(fxtests PRIVATE FX_PRESETS_DIR="${CMAKE_SOURCE_DIR}/presets")
  target_compile_options(fxtests PRIVATE ${FX_WARNINGS})
  add_test(NAME fxtests COMMAND fxtests)
endif()
