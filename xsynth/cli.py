"""Xsynth command line interface.

Usage examples::

    uv run xsynth sim   --phase 2
    uv run xsynth build --phase 2
    uv run xsynth host  status
    uv run xsynth host  note-on --hz 440 --wave saw
    uv run xsynth play  song.txt
    uv run xsynth listen 5
"""

from __future__ import annotations

import argparse
import sys
import time
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

    note_on = actions.add_parser(
        "note-on", help="start a note; with no --voice the firmware picks one")
    note_on.add_argument("--hz", type=float, default=440.0)
    note_on.add_argument("--note", type=int, default=None,
                         help="MIDI note number, overriding --hz")
    note_on.add_argument("--wave", choices=WAVES, default=None,
                         help="select a waveform first; with no --voice this "
                              "reaches every voice, not just the new note")
    note_on.add_argument("--voice", type=int, default=None,
                         help="pin one of the eight voices instead of asking")
    note_on.add_argument("--delay", type=int, default=0)
    _add_at(note_on)

    note_off = actions.add_parser(
        "note-off", help="stop a note by pitch, or a voice by number")
    note_off.add_argument("--hz", type=float, default=None,
                          help="the pitch the note was started at; required "
                               "unless --voice names one")
    note_off.add_argument("--note", type=int, default=None,
                          help="MIDI note number, overriding --hz")
    note_off.add_argument("--voice", type=int, default=None)
    note_off.add_argument("--delay", type=int, default=0)
    _add_at(note_off)

    freq = actions.add_parser("freq", help="change the frequency")
    freq.add_argument("hz", type=float)
    _add_target(freq)
    freq.add_argument("--delay", type=int, default=0)
    _add_at(freq)

    wave = actions.add_parser("wave", help="select a waveform")
    wave.add_argument("name", choices=WAVES)
    _add_target(wave)
    wave.add_argument("--delay", type=int, default=0)
    _add_at(wave)

    amp = actions.add_parser("amp", help="set the note level, 0.0 to 1.0")
    amp.add_argument("fraction", type=float)
    _add_target(amp)
    amp.add_argument("--delay", type=int, default=0)
    _add_at(amp)

    envelope = actions.add_parser(
        "envelope", help="set the shared ADSR; stages left out are untouched")
    envelope.add_argument("--attack", type=float, default=None,
                          help="seconds for the attack to cross the envelope")
    envelope.add_argument("--decay", type=float, default=None)
    envelope.add_argument("--sustain", type=float, default=None,
                          help="fraction of the note's own peak, 0.0 to 1.0")
    envelope.add_argument("--release", type=float, default=None)
    envelope.add_argument("--delay", type=int, default=0)
    _add_at(envelope)

    master = actions.add_parser(
        "master", help="scale the whole mix, 0.0 to 1.0")
    master.add_argument("fraction", type=float)
    master.add_argument("--delay", type=int, default=0)
    _add_at(master)

    actions.add_parser(
        "clear-schedule",
        help="drop the events waiting to play and go back to immediate")

    actions.add_parser(
        "anchor",
        help="take the sample count and make every later command relative "
             "to it")


def _add_at(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--at", type=int, default=None, metavar="SAMPLE",
        help="when to apply this, as an absolute 48 kHz sample count; "
             "`status` prints the current one")


def _add_target(parser: argparse.ArgumentParser) -> None:
    """One voice by number, or every voice. There is no default: naming no
    voice on these would have to mean one or the other, and guessing wrong is
    a silent change to a note the user did not mean to touch."""
    parser.add_argument("--voice", type=int, default=None)
    parser.add_argument("--all", action="store_true",
                        help="every voice, not just one")


def _checked_voice(voice: int) -> int:
    from xsynth.hdl.voice import VOICES
    from xsynth.protocol import VOICE_ANY

    if voice != VOICE_ANY and not 0 <= voice < VOICES:
        raise SystemExit(f"there is no voice {voice}; there are {VOICES}")
    return voice


def _voice(args) -> int:
    """A note names no voice unless it is told to."""
    from xsynth.protocol import VOICE_ANY

    return VOICE_ANY if args.voice is None else _checked_voice(args.voice)


def _target(args) -> int:
    from xsynth.protocol import VOICE_ANY

    if args.all and args.voice is not None:
        raise SystemExit("--voice and --all are opposites; pick one")
    if not args.all and args.voice is None:
        raise SystemExit(f"{args.action}: name a voice with --voice N, or "
                         f"--all for every one")
    return VOICE_ANY if args.all else _checked_voice(args.voice)


def _describe_voice(voice: int) -> str:
    from xsynth.protocol import VOICE_ANY

    return "every voice" if voice == VOICE_ANY else f"voice {voice}"


def _schedule(client, args) -> None:
    """An anchor has to go out before the command it applies to."""
    if getattr(args, "at", None) is not None:
        client.anchor(args.at)


def _note_to_hz(note: int) -> float:
    return 440.0 * 2 ** ((note - 69) / 12)


def _describe_cpu(status) -> str:
    if status.trapped:
        return "trapped"
    if status.halted:
        return "halted"
    return "running" if status.running else "stopped"


def _resolve(args) -> None:
    """Settle the voice arguments before the port is opened, so a mistake on
    the command line is reported as one instead of as a serial problem."""
    from xsynth.protocol import VOICE_ANY

    if args.action in ("freq", "wave", "amp"):
        args.voice = _target(args)
    elif args.action in ("note-on", "note-off"):
        args.voice = _voice(args)

    if args.action == "note-off":
        named = args.hz is not None or args.note is not None
        if not named and args.voice == VOICE_ANY:
            raise SystemExit(
                "note-off: give the pitch with --hz or --note, or name a "
                "voice with --voice"
            )


def _run_play(args) -> int:
    from xsynth.host import XsynthClient
    from xsynth.host.player import play
    from xsynth.host.score import load_score

    score = load_score(args.score)
    if not score.notes:
        raise SystemExit(f"{args.score} has no notes in it")

    with XsynthClient(args.port, baud=args.baud, timeout=args.timeout) as client:
        print(f"port {client.port} at {client.baud} baud")
        print(f"{len(score.notes)} notes over {score.duration:.2f} s")
        started = time.monotonic()
        play(client, score)
        print(f"played in {time.monotonic() - started:.2f} s")
    return 0


def _run_listen(args) -> int:
    from xsynth.host.listen import listen, listing

    if args.list_sources:
        for handle, name in listing():
            print(f"{handle}  {name}")
        return 0
    if args.seconds is None:
        raise SystemExit("how long should it record? (--list shows the sources)")
    if args.source is None:
        raise SystemExit(
            "which source? `xsynth listen --list` shows them, then pass "
            "--source"
        )
    extra = {} if args.warmup is None else {"warmup": args.warmup}
    listen(args.seconds, source=args.source, output=args.output,
           play=not args.no_play, report=print, **extra)
    return 0


def _run_host(args) -> int:
    from xsynth.host import XsynthClient

    _resolve(args)

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
            _schedule(client, args)
            client.note_on(hz, voice=args.voice, wave=args.wave,
                           delay=args.delay)
            print(f"note on at {hz:.2f} Hz, {_describe_voice(args.voice)}")
        elif args.action == "note-off":
            hz = _note_to_hz(args.note) if args.note is not None else args.hz
            _schedule(client, args)
            client.note_off(hz, voice=args.voice, delay=args.delay)
            print(f"note off, {_describe_voice(args.voice)}")
        elif args.action == "freq":
            _schedule(client, args)
            client.set_freq(args.hz, voice=args.voice, delay=args.delay)
            print(f"frequency {args.hz:.2f} Hz, {_describe_voice(args.voice)}")
        elif args.action == "wave":
            _schedule(client, args)
            client.set_wave(args.name, voice=args.voice, delay=args.delay)
            print(f"waveform {args.name}, {_describe_voice(args.voice)}")
        elif args.action == "amp":
            _schedule(client, args)
            client.set_amp(args.fraction, voice=args.voice, delay=args.delay)
            print(f"level {args.fraction}, {_describe_voice(args.voice)}")
        elif args.action == "envelope":
            _schedule(client, args)
            client.set_envelope(
                attack=args.attack, decay=args.decay, sustain=args.sustain,
                release=args.release, delay=args.delay,
            )
            stages = {name: value for name, value in (
                ("attack", args.attack), ("decay", args.decay),
                ("sustain", args.sustain), ("release", args.release),
            ) if value is not None}
            if not stages:
                raise SystemExit("envelope: name at least one stage")
            print("envelope " + " ".join(
                f"{name}={value}" for name, value in stages.items()))
        elif args.action == "master":
            _schedule(client, args)
            client.set_master(args.fraction, delay=args.delay)
            print(f"master {args.fraction}")
        elif args.action == "clear-schedule":
            client.clear_schedule()
            print("schedule cleared; later commands apply at once again")
        elif args.action == "anchor":
            sample = client.now()
            client.anchor(sample)
            print(f"anchored at {sample}; later commands are relative to it")
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

    play = sub.add_parser(
        "play", help="play a score file through the board"
    )
    play.add_argument("score", help="a score: 'time pitch duration', one note "
                                    "a line, with optional settings above")
    play.add_argument("--port", default=None,
                      help="serial port (default: the only USB serial port)")
    play.add_argument("--baud", type=int, default=115_200)
    play.add_argument("--timeout", type=float, default=1.0)

    listen = sub.add_parser(
        "listen", help="record what the board is playing and play it back"
    )
    listen.add_argument("seconds", type=float, nargs="?",
                        help="how long to record")
    listen.add_argument("--list", dest="list_sources", action="store_true",
                        help="print the capture sources and their handles")
    listen.add_argument("--source", default=None,
                        help="which source, by handle from --list")
    listen.add_argument("--output", default=None,
                        help="where to keep the recording")
    listen.add_argument("--no-play", action="store_true",
                        help="record without playing it back")
    listen.add_argument("--warmup", type=float, default=None,
                        help="seconds of capture to throw away first "
                             "(default: 6)")

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
    elif args.command == "play":
        return _run_play(args)
    elif args.command == "listen":
        return _run_listen(args)
    elif args.command == "program":
        raise SystemExit("`xsynth program` is not implemented yet")
    else:  # pragma: no cover - argparse guarantees a known command
        parser.error(f"unknown command {args.command!r}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
