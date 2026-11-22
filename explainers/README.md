# Explainers

Self-contained HTML pages that explain the ideas behind the design decisions. Open any of them
directly in a browser; each one only fetches Google Fonts.

| Page | What it covers |
|------|----------------|
| [latency.html](latency.html) | Where the milliseconds go: blocks, buffer size, comb filtering against the acoustic sound, why clicks are missed deadlines |
| [effects-compute-cost.html](effects-compute-cost.html) | Operations per sample for EQ, compressor, delay, reverb and body-IR convolution, against the Pi 5's budget |
| [what-is-a-sample.html](what-is-a-sample.html) | What one sample is, and how five scalar coefficients become a different gain for every frequency |
| [feedback-unrolled.html](feedback-unrolled.html) | The recursion unrolled into an impulse response, the Fourier link to the frequency response, and poles and zeros, each checked two or three ways |
| [live-analysis.html](live-analysis.html) | The web app's Live page: the wait-free tap ring, levels and noise floor, spectrum resolution, YIN pitch, chroma and the chord guess, onsets and tempo, H1 response and coherence (the body-IR measurement), and crash-safe recording |
| [shopping-list.html](shopping-list.html) | Parts list for the Pi 5 + Pisound box and the mic + interface measurement rig, assembly steps, and what not to buy |
