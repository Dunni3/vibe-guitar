"""Profile presets: worst-case block time at each buffer size, and the
smallest buffer each preset can run at safely.

For every preset x test signal x block size, runs fxrender and records the
worst process() time. A block size is "safe" when

    wakeup_us + worst_block_us <= headroom * budget_us

where budget_us = block / 48 kHz and wakeup_us is the worst scheduling
wake-up delay measured with cyclictest under realistic load (M3). On the
laptop, leave --wakeup-us at 0: the numbers check the plumbing, they are not
the Pi's numbers.

    uv run fx-profile                         # all presets, all signals
    uv run fx-profile --presets presets/slapback.json --blocks 32,64
    uv run fx-profile --wakeup-us 80          # on the Pi, with cyclictest's max
"""

import argparse
import json
import platform
import subprocess
import sys
from datetime import datetime
from pathlib import Path

RENDER_BLOCK = 128  # block size used for the audio renders written for listening


def run_fxrender(fxrender: Path, preset: Path, signal: Path, block: int, repeat: int,
                 stats_path: Path, out_wav: Path | None) -> dict:
    cmd = [str(fxrender), "--preset", str(preset), "--in", str(signal), "--block", str(block),
           "--repeat", str(repeat), "--stats-json", str(stats_path), "--quiet"]
    if out_wav is not None:
        cmd += ["--out", str(out_wav)]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0 and not stats_path.exists():
        raise RuntimeError(f"fxrender failed: {' '.join(cmd)}\n{proc.stderr}")
    stats = json.loads(stats_path.read_text())
    stats["exit_code"] = proc.returncode
    return stats


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--presets", nargs="+", type=Path, default=sorted(Path("presets").glob("*.json")))
    ap.add_argument("--signals", type=Path, default=Path("test_audio"),
                    help="directory of WAVs, searched recursively (default: test_audio)")
    ap.add_argument("--blocks", default="32,64,128,256")
    ap.add_argument("--repeat", type=int, default=3, help="timed passes per render")
    ap.add_argument("--wakeup-us", type=float, default=0.0,
                    help="worst-case wake-up delay from cyclictest (M3); 0 on the laptop")
    ap.add_argument("--headroom", type=float, default=0.7,
                    help="fraction of the block deadline wake-up + compute may use")
    ap.add_argument("--fxrender", type=Path, default=Path("build/fxrender"))
    ap.add_argument("--out-dir", type=Path, default=None,
                    help="default: profiles/<host>_<timestamp>/")
    ap.add_argument("--no-renders", action="store_true", help="don't write processed audio")
    args = ap.parse_args()

    if not args.fxrender.exists():
        sys.exit(f"{args.fxrender} not found; build first (make build)")
    signals = sorted(args.signals.rglob("*.wav"))
    if not signals:
        sys.exit(f"no WAVs under {args.signals}; run `uv run fx-signals` first")
    blocks = [int(b) for b in args.blocks.split(",")]

    # Architecture from fxrender itself: this Python may run under Rosetta.
    arch = subprocess.run([str(args.fxrender), "--host"], capture_output=True, text=True,
                          check=True).stdout.strip()
    host = f"{platform.node().split('.')[0]}_{arch}"
    out_dir = args.out_dir or Path("profiles") / f"{host}_{datetime.now():%Y-%m-%d_%H%M%S}"
    (out_dir / "stats").mkdir(parents=True, exist_ok=True)
    if not args.no_renders:
        (out_dir / "renders").mkdir(exist_ok=True)

    results = []
    summary = []
    for preset in args.presets:
        print(f"\n{preset}")
        print(f"  {'block':>5}  {'budget':>9}  {'worst':>9}  {'load':>6}  worst signal")
        safe_blocks = []
        for block in blocks:
            budget_us = block / 48000 * 1e6
            worst = None
            for signal in signals:
                tag = f"{preset.stem}__{signal.stem}__b{block}"
                out_wav = None
                if not args.no_renders and block == RENDER_BLOCK:
                    out_wav = out_dir / "renders" / f"{preset.stem}__{signal.stem}.wav"
                s = run_fxrender(args.fxrender, preset, signal, block, args.repeat,
                                 out_dir / "stats" / f"{tag}.json", out_wav)
                results.append(s)
                if s.get("output_nonfinite_samples"):
                    print(f"  WARNING: {preset.stem} on {signal.name}: non-finite output")
                if worst is None or s["max_us"] > worst["max_us"]:
                    worst = s
            safe = args.wakeup_us + worst["max_us"] <= args.headroom * budget_us
            if safe:
                safe_blocks.append(block)
            print(f"  {block:>5}  {budget_us:>7.0f}us  {worst['max_us']:>7.1f}us  "
                  f"{100 * worst['max_load']:>5.1f}%  {Path(worst['input']).name} "
                  f"@ {worst['worst_block_time_s']:.2f}s{'' if safe else '  (unsafe)'}")
        recommended = min(safe_blocks) if safe_blocks else None
        print(f"  smallest safe buffer: {recommended or 'none of ' + args.blocks}")
        summary.append({"preset": str(preset), "recommended_buffer_size": recommended,
                        "safe_blocks": safe_blocks})

    report = {
        "host": host,
        "platform": platform.platform(),
        "created": datetime.now().astimezone().isoformat(timespec="seconds"),
        "wakeup_us": args.wakeup_us,
        "headroom": args.headroom,
        "blocks": blocks,
        "signals": [str(s) for s in signals],
        "summary": summary,
        "results": results,
    }
    (out_dir / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(f"\nreport: {out_dir / 'report.json'}")
    if not args.no_renders:
        print(f"renders (block {RENDER_BLOCK}): {out_dir / 'renders'}/")
    if args.wakeup_us == 0:
        print("note: --wakeup-us 0, so these are compute-only numbers (fine for the laptop, "
              "not for picking real buffer sizes)")


if __name__ == "__main__":
    main()
