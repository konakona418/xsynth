"""Xsynth command line interface.

Usage examples::

    uv run xsynth sim   --phase 2
    uv run xsynth build --phase 2
    uv run xsynth host  status
    uv run xsynth host  note-on --hz 440 --wave saw
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--board", default="tang-nano-9k", help="target board name")
    parser.add_argument("--phase", type=int, default=0, help="design phase to build")


def _add_pattern(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--pattern",
        default="bars",
        help="video test pattern: 'bars', 'cycle', or a hex colour like 'ff0000'",
    )
    parser.add_argument(
        "--video-mode",
        default="640x480",
        help="video timing mode: '640x480' (default) or '1280x720'",
    )
    parser.add_argument(
        "--hdmi",
        action="store_true",
        help="emit a full HDMI signal (with data islands) instead of DVI",
    )


def _add_audio(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--tone",
        type=float,
        default=440.0,
        help="audio test tone in Hz (phase 1)",
    )
    parser.add_argument(
        "--baud",
        type=int,
        default=None,
        help="control UART baud rate (phase 2 and later)",
    )


def _add_host(parser: argparse.ArgumentParser) -> None:
    from xsynth.hdl.voice import WAVES

    parser.add_argument("--port", default=None,
                        help="serial port (default: the only USB serial port)")
    parser.add_argument("--baud", type=int, default=115_200)
    parser.add_argument("--timeout", type=float, default=1.0)

    actions = parser.add_subparsers(dest="action", required=True)
    actions.add_parser("ping", help="check that the board answers")
    actions.add_parser("status", help="read the version, flags and FIFO level")
    actions.add_parser("reset", help="reset the voice and clear error flags")

    run = actions.add_parser("run", help="let the CPU out of reset")
    run.add_argument("--stop", action="store_true",
                     help="put the CPU back into reset instead")

    load = actions.add_parser("load", help="upload a program and run it")
    load.add_argument("image", nargs="?", default=None,
                      help="a flat binary image (default: build the bundled "
                           "firmware)")
    load.add_argument("--no-run", action="store_true",
                      help="load without letting the CPU out of reset")

    note_on = actions.add_parser("note-on", help="start a note")
    note_on.add_argument("--hz", type=float, default=440.0)
    note_on.add_argument("--note", type=int, default=None,
                         help="MIDI note number, overriding --hz")
    note_on.add_argument("--wave", choices=WAVES, default=None)
    note_on.add_argument("--delay", type=int, default=0)

    note_off = actions.add_parser("note-off", help="stop the note")
    note_off.add_argument("--delay", type=int, default=0)

    freq = actions.add_parser("freq", help="change the frequency")
    freq.add_argument("hz", type=float)
    freq.add_argument("--delay", type=int, default=0)

    wave = actions.add_parser("wave", help="select a waveform")
    wave.add_argument("name", choices=WAVES)
    wave.add_argument("--delay", type=int, default=0)

    amp = actions.add_parser("amp", help="set the amplitude, 0.0 to 1.0")
    amp.add_argument("fraction", type=float)
    amp.add_argument("--delay", type=int, default=0)


def _note_to_hz(note: int) -> float:
    return 440.0 * 2 ** ((note - 69) / 12)


def _describe_cpu(status) -> str:
    if status.trapped:
        return "trapped"
    if status.halted:
        return "halted"
    return "running" if status.running else "stopped"


def _run_host(args) -> int:
    from xsynth.host import XsynthClient

    with XsynthClient(args.port, baud=args.baud, timeout=args.timeout) as client:
        print(f"port {client.port} at {client.baud} baud")
        if args.action == "ping":
            print("pong" if client.ping() else "no pong")
        elif args.action == "status":
            status = client.status()
            print(f"version  {status.version}")
            print(f"locked   {status.locked}")
            print(f"fifo     {status.fifo_level}")
            print(f"samples  {status.samples}")
            print(f"cpu      {_describe_cpu(status)}")
            print(f"cpu_stat {status.cpu_status:#010x}")
            print(f"cpu_ctr  {status.cpu_counter}")
            print(f"errors   {', '.join(status.errors) or 'none'}")
        elif args.action == "run":
            client.run(not args.stop)
            print("stopped" if args.stop else "running")
        elif args.action == "load":
            if args.image is None:
                from xsynth.firmware import build_firmware

                image = build_firmware()
                print(f"built {len(image)} bytes of firmware")
            else:
                image = Path(args.image).read_bytes()
            client.run(False)
            client.load(image)
            print(f"loaded {len(image)} bytes")
            if not args.no_run:
                client.run(True)
                print("running")
        elif args.action == "reset":
            client.reset()
            print("reset sent")
        elif args.action == "note-on":
            hz = _note_to_hz(args.note) if args.note is not None else args.hz
            client.note_on(hz, wave=args.wave, delay=args.delay)
            print(f"note on at {hz:.2f} Hz")
        elif args.action == "note-off":
            client.note_off(delay=args.delay)
            print("note off")
        elif args.action == "freq":
            client.set_freq(args.hz, delay=args.delay)
            print(f"frequency {args.hz:.2f} Hz")
        elif args.action == "wave":
            client.set_wave(args.name, delay=args.delay)
            print(f"waveform {args.name}")
        elif args.action == "amp":
            client.set_amp(args.fraction, delay=args.delay)
            print(f"amplitude {args.fraction}")
        else:  # pragma: no cover - argparse guarantees a known action
            raise SystemExit(f"unknown host action {args.action!r}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="xsynth", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    build = sub.add_parser("build", help="synthesize and place & route a bitstream")
    _add_common(build)
    _add_pattern(build)
    _add_audio(build)
    build.add_argument(
        "--no-flash",
        action="store_true",
        help="program to SRAM instead of flash (non-persistent)",
    )
    build.add_argument(
        "--no-program",
        action="store_true",
        help="only produce the bitstream, do not program the device",
    )

    sim = sub.add_parser("sim", help="run the Amaranth simulation")
    _add_common(sim)
    _add_pattern(sim)
    _add_audio(sim)
    sim.add_argument("--vcd", default=None, help="write a VCD trace to this path")

    program = sub.add_parser("program", help="flash an already built bitstream")
    _add_common(program)
    program.add_argument("--no-flash", action="store_true")

    host = sub.add_parser("host", help="control a programmed board over UART")
    _add_host(host)

    analyse = sub.add_parser(
        "analyse", help="measure a capture-card recording (offline, no board)"
    )
    analyse.add_argument("wav", help="a WAV file, e.g. from parecord")
    analyse.add_argument("--channel", type=int, default=0)
    analyse.add_argument("--harmonics", type=int, default=8)

    args = parser.parse_args(argv)

    if args.command == "sim":
        from xsynth.design import simulate

        simulate(args.phase, args.board, vcd=args.vcd)
    elif args.command == "build":
        from xsynth.design import build

        build(
            args.phase,
            args.board,
            program=not args.no_program,
            program_to_flash=not args.no_flash,
            pattern=args.pattern,
            mode_name=args.video_mode,
            dvi_output=False if args.hdmi else None,
            tone_hz=args.tone,
            baud=args.baud,
        )
    elif args.command == "host":
        return _run_host(args)
    elif args.command == "analyse":
        from xsynth.host.analysis import analyse

        print(analyse(args.wav, channel=args.channel,
                      harmonics=args.harmonics).describe())
    elif args.command == "program":
        raise SystemExit("`xsynth program` is not implemented yet")
    else:  # pragma: no cover - argparse guarantees a known command
        parser.error(f"unknown command {args.command!r}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
