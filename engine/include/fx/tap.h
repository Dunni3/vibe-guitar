// Signal tap: the audio engine publishes what it hears and plays into a
// shared-memory ring, and the web app reads it for live analysis and
// recording. The audio thread never waits for the reader: it overwrites the
// oldest frames, and a reader that falls behind counts the frames it lost.
//
// Layout of the file (little-endian; the Python reader in
// webapp/fxweb/live/tap.py mirrors it, change both together):
//
//   offset  size  field
//        0     8  magic "FXTAP001"
//        8     4  u32 version (1)
//       12     4  u32 channels (1..kTapMaxChannels)
//       16     4  u32 sample_rate
//       20     4  u32 capacity, in frames (power of two)
//       24     8  u64 session: changes every time a writer opens the tap
//       32     8  u64 write_pos: total frames written. Stored with release
//                 ordering after the frames themselves.
//       40    24  reserved (zero)
//       64    64  channel names, 4 x 16 bytes, NUL-padded ("in", "out", ...)
//      128     -  float32 frames, interleaved: data[(pos % capacity) * channels + c]
//
// Threading: open()/close() on the control thread (they allocate, map and
// touch every page so the audio thread never page-faults). write() on the
// audio thread: wait-free, no allocation, no syscalls. One writer per file.
#pragma once

#include <cstddef>
#include <cstdint>
#include <string>
#include <vector>

namespace fx {

constexpr int kTapMaxChannels = 4;
constexpr std::size_t kTapHeaderBytes = 128;
constexpr std::uint32_t kTapVersion = 1;

// /dev/shm on Linux (RAM, never touches the SD card). Elsewhere a file in
// $TMPDIR, which is page-cache backed and fine for development.
std::string defaultTapPath();

class TapWriter {
 public:
  TapWriter() = default;
  ~TapWriter() { close(); }
  TapWriter(const TapWriter&) = delete;
  TapWriter& operator=(const TapWriter&) = delete;

  // Control thread. Creates (or truncates) the file. capacityFrames is rounded
  // up to a power of two; 4 s at 48 kHz gives a slow reader plenty of slack.
  // Throws std::runtime_error.
  void open(const std::string& path, int channels, int sampleRate,
            const std::vector<std::string>& names, int capacityFrames = 1 << 18);
  void close();
  bool isOpen() const { return base_ != nullptr; }

  // Audio thread. channels[c][i], c < numChannels (extra channels are
  // ignored, missing ones written as silence), i < numFrames.
  void write(const float* const* channels, int numChannels, int numFrames) noexcept;

  std::uint64_t framesWritten() const noexcept { return pos_; }
  int channels() const noexcept { return channels_; }
  std::uint32_t capacity() const noexcept { return capacity_; }

 private:
  unsigned char* base_ = nullptr;
  std::size_t bytes_ = 0;
  float* data_ = nullptr;
  std::uint64_t* writePos_ = nullptr;
  std::uint64_t pos_ = 0;
  std::uint32_t capacity_ = 0;
  int channels_ = 0;
};

}  // namespace fx
