"""Live signal: sources, analysis, recording.

The audio engine (a separate real-time process) publishes its input and output
to a shared-memory tap (engine/include/fx/tap.h). This package reads that, or
another source, in a background thread of the web server, never inside the
audio callback:

    source thread   reads ~10 ms blocks -> History ring (last N s, for "save last 30 s")
                                        -> active Recorder (WAV writer thread)
    analysis thread 30x/s while someone is watching: levels, spectrum, pitch,
                    chords, onsets, dry/wet response -> JSON frames -> SSE clients
"""
