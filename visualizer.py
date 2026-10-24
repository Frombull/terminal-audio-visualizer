#!/usr/bin/env python3
"""Terminal audio visualizer (Windows and Linux).

Captures what is playing on your PC (WASAPI loopback on Windows, PulseAudio/PipeWire
monitor on Linux) or a microphone and draws color-faded bars. When a strong bass hit
lands, the terminal window shakes for real (or, if the window can't be moved, the
content shakes instead).

    python visualizer.py            # default output (what you are listening to)
    python visualizer.py --demo     # synthetic signal, nothing needs to play
    python visualizer.py --list-devices

Keys:  M = menu   Up/Down = sensitivity   Left/Right = bar count   P = palette
       S = shake   F = effects   D = switch input   Q = quit
"""
from __future__ import annotations

import argparse
import sys

from viz.app import App, AudioInput, Settings, setup_terminal
from viz.audio import DemoSource
from viz.draw import ESC


def main() -> None:
    ap = argparse.ArgumentParser(description="Terminal audio visualizer")
    ap.add_argument("--demo", action="store_true", help="use a synthetic signal instead of capturing audio")
    ap.add_argument("--list-devices", action="store_true", help="list the available inputs")
    ap.add_argument("--device", help="part of the input name (e.g. 'Speakers' or 'Mic')")
    ap.add_argument("--reset", action="store_true", help="ignore the saved settings")
    args = ap.parse_args()

    settings = Settings() if args.reset else Settings.load()
    audio = DemoSource() if args.demo else AudioInput()

    if args.list_devices:
        for dv in audio.devices:
            print(f"{dv.label}  ({dv.rate} Hz, {dv.channels} channels)")
        audio.shutdown()
        return
    if args.device and audio.devices:
        match = next((d for d in audio.devices if args.device.lower() in d.label.lower()), None)
        if match:
            settings.device = match.label

    restore = setup_terminal() if sys.stdout.isatty() else None
    if restore is None:
        audio.shutdown()
        sys.exit("Run this inside a terminal (on Windows, preferably Windows Terminal).")
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    app = None
    sys.stdout.write(f"{ESC}[?1049h{ESC}[?25l{ESC}[2J")
    try:
        app = App(settings, audio)
        app.run()
    except KeyboardInterrupt:
        pass
    finally:
        if app is not None:
            app.shaker.reset()
        settings.save()
        audio.shutdown()
        sys.stdout.write(f"{ESC}[0m{ESC}[?25h{ESC}[?1049l")
        sys.stdout.flush()
        restore()


if __name__ == "__main__":
    main()
