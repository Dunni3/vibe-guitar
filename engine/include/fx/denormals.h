// Flush-to-zero for the current thread. Decaying reverb/delay tails produce
// denormal floats, which are very slow on many CPUs and cause CPU spikes
// (missed deadlines) exactly when the guitar goes quiet. Every audio thread
// should hold a ScopedFlushDenormals for its whole lifetime.
#pragma once

#include <cstdint>
#include <cstring>

#if defined(__x86_64__) || defined(_M_X64)
#include <xmmintrin.h>
#endif

namespace fx {

class ScopedFlushDenormals {
 public:
  ScopedFlushDenormals() {
#if defined(__aarch64__)
    // FPCR.FZ (bit 24): flush denormal inputs and outputs to zero. The
    // "memory" clobber stops the compiler moving float code across the switch.
    asm volatile("mrs %0, fpcr" : "=r"(saved_)::"memory");
    asm volatile("msr fpcr, %0" ::"r"(saved_ | (uint64_t{1} << 24)) : "memory");
#elif defined(__x86_64__) || defined(_M_X64)
    saved_ = _mm_getcsr();
    _mm_setcsr(static_cast<unsigned>(saved_) | 0x8040u);  // FTZ | DAZ
#endif
  }
  ~ScopedFlushDenormals() {
#if defined(__aarch64__)
    asm volatile("msr fpcr, %0" ::"r"(saved_) : "memory");
#elif defined(__x86_64__) || defined(_M_X64)
    _mm_setcsr(static_cast<unsigned>(saved_));
#endif
  }
  ScopedFlushDenormals(const ScopedFlushDenormals&) = delete;
  ScopedFlushDenormals& operator=(const ScopedFlushDenormals&) = delete;

 private:
  uint64_t saved_ = 0;
};

// True if denormals are currently flushed on this thread.
//
// noinline on purpose: with FZ on, AArch64 also flushes the *inputs* of float
// compares, so if this were inlined the compiler could compute the product
// before a mode switch but compare it after, and get the wrong answer. A call
// can't be reordered across the switch's "memory" clobber.
[[gnu::noinline]] inline bool denormalsAreFlushed() {
  volatile float tiny = 1e-30f;
  const float result = tiny * 1e-10f;  // 1e-40 is denormal for float
  uint32_t bits;
  std::memcpy(&bits, &result, sizeof bits);
  return (bits & 0x7fffffffu) == 0;
}

}  // namespace fx
