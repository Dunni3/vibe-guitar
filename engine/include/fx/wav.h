// Minimal WAV file I/O for the offline tools (not used on the audio thread).
// Reads 16/24/32-bit PCM and 32-bit float, including WAVE_FORMAT_EXTENSIBLE.
// Writes 32-bit float.
#pragma once

#include <stdexcept>
#include <string>
#include <vector>

namespace fx {

struct AudioBuffer {
  int sampleRate = 48000;
  std::vector<std::vector<float>> channels;  // planar: channels[c][frame]

  int numChannels() const { return static_cast<int>(channels.size()); }
  int numFrames() const { return channels.empty() ? 0 : static_cast<int>(channels[0].size()); }
};

class WavError : public std::runtime_error {
 public:
  using std::runtime_error::runtime_error;
};

AudioBuffer readWav(const std::string& path);
void writeWavFloat(const AudioBuffer& audio, const std::string& path);

}  // namespace fx
