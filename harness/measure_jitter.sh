#!/bin/bash
# Worst-case scheduling wake-up latency on the Pi (M3), measured with cyclictest.
#
#   sudo bash harness/measure_jitter.sh [seconds-per-scenario]   (default 60)
#
# Feed the worst "Max" (µs) from the scenario that matches how the box will run into
#   uv run fx-profile --wakeup-us <max>
#
# Scenarios:
#   idle      nothing else running (the web app, if started, sits idle)
#   load      stress on all 4 cores + web traffic; cyclictest on every core
#   pinned    stress + web traffic + web app confined to cores 0-2; cyclictest on core 3 only
#             (models reserving one core for audio)
#
# Needs root for SCHED_FIFO and mlockall. Installs rt-tests and stress-ng if missing.
set -u
[ "$(id -u)" = 0 ] || { echo "run with sudo"; exit 1; }

DUR=${1:-60}
INTERVAL=500   # µs between wake-ups (32 samples @ 48 kHz is 667 µs)
WEB=http://127.0.0.1:8080
OWNER=${SUDO_USER:-root}
OUT=$(eval echo "~$OWNER")/jitter_$(date +%Y%m%d_%H%M%S)
mkdir -p "$OUT"

if ! command -v cyclictest >/dev/null || ! command -v stress-ng >/dev/null; then
  echo "installing rt-tests stress-ng..."
  apt-get install -y rt-tests stress-ng >/dev/null || exit 1
fi

{
  echo "date: $(date -Is)"
  echo "kernel: $(uname -r)  $(uname -v)"
  echo "governor: $(cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor 2>/dev/null)"
  echo "cpu max kHz: $(cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_max_freq 2>/dev/null)"
  echo "temp start: $(vcgencmd measure_temp)"
  echo "throttled start: $(vcgencmd get_throttled)"
  echo "web app: $(curl -s -m 2 -o /dev/null -w '%{http_code}' $WEB/api/health || echo down)"
  echo "duration per scenario: ${DUR}s, interval ${INTERVAL}us"
} | tee "$OUT/system.txt"

web_traffic() {  # hit the web app as fast as it answers, until killed
  while :; do
    curl -s -m 2 $WEB/api/presets >/dev/null
    curl -s -m 2 $WEB/api/health >/dev/null
    curl -s -m 2 $WEB/api/catalog >/dev/null
  done
}

stop_bg() { for p in "$@"; do kill "$p" 2>/dev/null; done; wait "$@" 2>/dev/null; }

summarize() {  # print one line per cyclictest thread
  grep -E '^T:' "$OUT/$1.txt" | sed -E 's/.*T: *([0-9]+).*Min: *([0-9]+).*Avg: *([0-9]+).*Max: *([0-9]+).*/  thread \1: min \2  avg \3  max \4 us/'
}

CT="cyclictest --mlockall --priority=90 --policy=fifo --interval=$INTERVAL --distance=0 --duration=$DUR --quiet"

echo; echo "== idle (${DUR}s)"
$CT --smp > "$OUT/idle.txt" 2>&1
summarize idle

echo; echo "== load: stress on all cores + web traffic (${DUR}s)"
stress-ng --cpu 4 --io 1 --vm 2 --vm-bytes 256M --timeout $((DUR + 10))s >/dev/null 2>&1 & S=$!
web_traffic & W=$!
sleep 3
$CT --smp > "$OUT/load.txt" 2>&1
stop_bg $S $W
summarize load

echo; echo "== pinned: load on cores 0-2, cyclictest on core 3 (${DUR}s)"
for p in $(pgrep -f fx-web); do taskset -apc 0-2 "$p" >/dev/null; done
taskset -c 0-2 stress-ng --cpu 3 --io 1 --vm 2 --vm-bytes 256M --timeout $((DUR + 10))s >/dev/null 2>&1 & S=$!
taskset -c 0-2 bash -c "$(declare -f web_traffic); WEB=$WEB; web_traffic" & W=$!
sleep 3
$CT --affinity=3 --threads=1 > "$OUT/pinned.txt" 2>&1
stop_bg $S $W
for p in $(pgrep -f fx-web); do taskset -apc 0-3 "$p" >/dev/null; done
summarize pinned

{
  echo "temp end: $(vcgencmd measure_temp)"
  echo "throttled end: $(vcgencmd get_throttled)"
} | tee -a "$OUT/system.txt"

chown -R "$OWNER" "$OUT"
echo; echo "results in $OUT"
echo "JITTER-DONE"
