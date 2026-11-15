#include "fx/wav.h"

#include <algorithm>
#include <cstdint>
#include <cstring>
#include <fstream>
#include <iterator>

namespace fx {
namespace {

constexpr uint16_t kFormatPcm = 1;
constexpr uint16_t kFormatFloat = 3;
constexpr uint16_t kFormatExtensible = 0xFFFE;

uint16_t u16(const uint8_t* p) { return static_cast<uint16_t>(p[0] | (p[1] << 8)); }
uint32_t u32(const uint8_t* p) {
  return uint32_t{p[0]} | (uint32_t{p[1]} << 8) | (uint32_t{p[2]} << 16) | (uint32_t{p[3]} << 24);
}

void put16(std::vector<uint8_t>& out, uint16_t v) {
  out.push_back(static_cast<uint8_t>(v));
  out.push_back(static_cast<uint8_t>(v >> 8));
}
void put32(std::vector<uint8_t>& out, uint32_t v) {
  for (int i = 0; i < 4; ++i) out.push_back(static_cast<uint8_t>(v >> (8 * i)));
}
void putTag(std::vector<uint8_t>& out, const char* tag) { out.insert(out.end(), tag, tag + 4); }

}  // namespace

AudioBuffer readWav(const std::string& path) {
  std::ifstream in(path, std::ios::binary);
  if (!in) throw WavError("cannot open '" + path + "'");
  const std::vector<uint8_t> data((std::istreambuf_iterator<char>(in)),
                                  std::istreambuf_iterator<char>());
  if (data.size() < 12 || std::memcmp(data.data(), "RIFF", 4) != 0 ||
      std::memcmp(data.data() + 8, "WAVE", 4) != 0) {
    throw WavError("'" + path + "' is not a RIFF/WAVE file");
  }

  uint16_t format = 0, numChannels = 0, bits = 0;
  uint32_t sampleRate = 0;
  const uint8_t* samples = nullptr;
  size_t sampleBytes = 0;

  size_t pos = 12;
  while (pos + 8 <= data.size()) {
    const uint8_t* chunk = data.data() + pos;
    const size_t size = u32(chunk + 4);
    const size_t body = pos + 8;
    const size_t avail = std::min(size, data.size() - body);  // tolerate a truncated last chunk
    if (std::memcmp(chunk, "fmt ", 4) == 0) {
      if (avail < 16) throw WavError("'" + path + "': fmt chunk too short");
      const uint8_t* f = data.data() + body;
      format = u16(f);
      numChannels = u16(f + 2);
      sampleRate = u32(f + 4);
      bits = u16(f + 14);
      if (format == kFormatExtensible) {
        if (avail < 26) throw WavError("'" + path + "': extensible fmt chunk too short");
        format = u16(f + 24);  // first two bytes of the SubFormat GUID
      }
    } else if (std::memcmp(chunk, "data", 4) == 0) {
      samples = data.data() + body;
      sampleBytes = avail;
    }
    pos = body + size + (size & 1);  // chunks are word-aligned
  }

  if (numChannels == 0 || sampleRate == 0) throw WavError("'" + path + "': missing fmt chunk");
  if (!samples) throw WavError("'" + path + "': missing data chunk");
  const bool supported = (format == kFormatPcm && (bits == 16 || bits == 24 || bits == 32)) ||
                         (format == kFormatFloat && bits == 32);
  if (!supported) {
    throw WavError("'" + path + "': unsupported format " + std::to_string(format) + " / " +
                   std::to_string(bits) + "-bit");
  }

  const size_t bytesPerSample = bits / 8u;
  const size_t numFrames = sampleBytes / (bytesPerSample * numChannels);
  AudioBuffer audio;
  audio.sampleRate = static_cast<int>(sampleRate);
  audio.channels.assign(numChannels, std::vector<float>(numFrames));

  for (size_t i = 0; i < numFrames; ++i) {
    for (size_t c = 0; c < numChannels; ++c) {
      const uint8_t* s = samples + (i * numChannels + c) * bytesPerSample;
      float v = 0.0f;
      if (format == kFormatFloat) {
        const uint32_t raw = u32(s);
        std::memcpy(&v, &raw, sizeof v);
      } else if (bits == 16) {
        v = static_cast<float>(static_cast<int16_t>(u16(s))) / 32768.0f;
      } else if (bits == 24) {
        // Place the 3 bytes in the top of a 32-bit word to sign-extend.
        const uint32_t raw = (uint32_t{s[0]} << 8) | (uint32_t{s[1]} << 16) | (uint32_t{s[2]} << 24);
        v = static_cast<float>(static_cast<int32_t>(raw)) / 2147483648.0f;
      } else {
        v = static_cast<float>(static_cast<double>(static_cast<int32_t>(u32(s))) / 2147483648.0);
      }
      audio.channels[c][i] = v;
    }
  }
  return audio;
}

void writeWavFloat(const AudioBuffer& audio, const std::string& path) {
  const auto numChannels = static_cast<uint32_t>(audio.numChannels());
  const auto numFrames = static_cast<uint32_t>(audio.numFrames());
  if (numChannels == 0) throw WavError("cannot write a WAV with no channels");
  const uint32_t dataBytes = numFrames * numChannels * 4u;

  std::vector<uint8_t> out;
  out.reserve(44 + dataBytes);
  putTag(out, "RIFF");
  put32(out, 36 + dataBytes);
  putTag(out, "WAVE");
  putTag(out, "fmt ");
  put32(out, 16);
  put16(out, kFormatFloat);
  put16(out, static_cast<uint16_t>(numChannels));
  put32(out, static_cast<uint32_t>(audio.sampleRate));
  put32(out, static_cast<uint32_t>(audio.sampleRate) * numChannels * 4u);  // byte rate
  put16(out, static_cast<uint16_t>(numChannels * 4u));                    // block align
  put16(out, 32);
  putTag(out, "data");
  put32(out, dataBytes);
  for (uint32_t i = 0; i < numFrames; ++i) {
    for (uint32_t c = 0; c < numChannels; ++c) {
      uint32_t raw;
      std::memcpy(&raw, &audio.channels[c][i], sizeof raw);
      put32(out, raw);
    }
  }

  std::ofstream f(path, std::ios::binary);
  if (!f) throw WavError("cannot write '" + path + "'");
  f.write(reinterpret_cast<const char*>(out.data()), static_cast<std::streamsize>(out.size()));
  if (!f) throw WavError("write failed for '" + path + "'");
}

}  // namespace fx
