#include "fx/tap.h"

#include <fcntl.h>
#include <sys/mman.h>
#include <unistd.h>

#include <chrono>
#include <cstdlib>
#include <cstring>
#include <string>
#include <stdexcept>

namespace fx {

namespace {

template <typename T>
void put(unsigned char* base, std::size_t offset, T value) {
  std::memcpy(base + offset, &value, sizeof(T));  // host order; both targets are little-endian
}

std::uint32_t nextPow2(std::uint32_t v) {
  std::uint32_t p = 1;
  while (p < v) p <<= 1;
  return p;
}

}  // namespace

std::string defaultTapPath() {
#if defined(__linux__)
  return "/dev/shm/fxbox-tap";
#else
  const char* tmp = std::getenv("TMPDIR");
  std::string dir = tmp && *tmp ? tmp : "/tmp";
  if (dir.back() != '/') dir += '/';
  return dir + "fxbox-tap";
#endif
}

void TapWriter::open(const std::string& path, int channels, int sampleRate,
                     const std::vector<std::string>& names, int capacityFrames) {
  close();
  if (channels < 1 || channels > kTapMaxChannels) throw std::runtime_error("tap: 1-4 channels");
  if (capacityFrames < 256) throw std::runtime_error("tap: capacity too small");
  const std::uint32_t cap = nextPow2(static_cast<std::uint32_t>(capacityFrames));
  const std::size_t bytes = kTapHeaderBytes + std::size_t{cap} * channels * sizeof(float);

  // Build the new file beside the old one and rename it into place: a reader
  // that still maps the old file keeps a valid (if stale) mapping instead of
  // faulting on a truncated one, and reopens when it sees the new inode.
  const std::string tmp = path + ".new." + std::to_string(::getpid());
  const int fd = ::open(tmp.c_str(), O_RDWR | O_CREAT | O_TRUNC, 0644);
  if (fd < 0) throw std::runtime_error("tap: cannot create " + tmp);
  if (::ftruncate(fd, static_cast<off_t>(bytes)) != 0) {
    ::close(fd);
    ::unlink(tmp.c_str());
    throw std::runtime_error("tap: cannot size " + tmp);
  }
  void* m = ::mmap(nullptr, bytes, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
  ::close(fd);
  if (m == MAP_FAILED) {
    ::unlink(tmp.c_str());
    throw std::runtime_error("tap: cannot map " + tmp);
  }

  base_ = static_cast<unsigned char*>(m);
  bytes_ = bytes;
  // Touch every page now, and try to pin them, so write() never page-faults.
  std::memset(base_, 0, bytes_);
  (void)::mlock(base_, bytes_);

  std::memcpy(base_, "FXTAP001", 8);
  put<std::uint32_t>(base_, 8, kTapVersion);
  put<std::uint32_t>(base_, 12, static_cast<std::uint32_t>(channels));
  put<std::uint32_t>(base_, 16, static_cast<std::uint32_t>(sampleRate));
  put<std::uint32_t>(base_, 20, cap);
  const auto session = static_cast<std::uint64_t>(
      std::chrono::system_clock::now().time_since_epoch().count());
  put<std::uint64_t>(base_, 24, session);
  for (int c = 0; c < kTapMaxChannels && c < static_cast<int>(names.size()); ++c) {
    std::strncpy(reinterpret_cast<char*>(base_ + 64 + 16 * c), names[c].c_str(), 15);
  }

  writePos_ = reinterpret_cast<std::uint64_t*>(base_ + 32);
  data_ = reinterpret_cast<float*>(base_ + kTapHeaderBytes);
  capacity_ = cap;
  channels_ = channels;
  pos_ = 0;
  __atomic_store_n(writePos_, 0, __ATOMIC_RELEASE);
  if (::rename(tmp.c_str(), path.c_str()) != 0) {
    ::unlink(tmp.c_str());
    close();
    throw std::runtime_error("tap: cannot rename into " + path);
  }
}

void TapWriter::close() {
  if (base_) {
    ::munlock(base_, bytes_);
    ::munmap(base_, bytes_);
  }
  base_ = nullptr;
  data_ = nullptr;
  writePos_ = nullptr;
  bytes_ = 0;
  capacity_ = 0;
  channels_ = 0;
  pos_ = 0;
}

void TapWriter::write(const float* const* channels, int numChannels, int numFrames) noexcept {
  if (!base_ || numFrames <= 0) return;
  const std::uint32_t mask = capacity_ - 1;
  for (int i = 0; i < numFrames; ++i) {
    float* frame = data_ + std::size_t{(static_cast<std::uint32_t>(pos_ + i) & mask)} * channels_;
    for (int c = 0; c < channels_; ++c) frame[c] = c < numChannels ? channels[c][i] : 0.0f;
  }
  pos_ += static_cast<std::uint64_t>(numFrames);
  __atomic_store_n(writePos_, pos_, __ATOMIC_RELEASE);
}

}  // namespace fx
