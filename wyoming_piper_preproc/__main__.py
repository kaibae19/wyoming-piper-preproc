"""A text-only Wyoming proxy for standalone and streaming Piper requests."""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
from pathlib import Path

from wyoming.event import Event, async_read_event, async_write_event
from wyoming.info import Info
from wyoming.tts import Synthesize, SynthesizeChunk, SynthesizeStart, SynthesizeStop

from .preproc import TextPreprocessor

_LOG = logging.getLogger("wyoming_piper_preproc")


async def _forward(
    src_reader: asyncio.StreamReader,
    dst_writer: asyncio.StreamWriter,
    intercept: bool,
    csv_paths: list[Path],
    voice_paths: dict[str, list[Path]],
    default_voice: str | None,
    ignore_case: bool,
    config_path: Path | None,
    program_name: str,
) -> None:
    """Rewrite synthesis text/default voice downstream and TTS program names upstream."""
    stream: TextPreprocessor | None = None

    def begin(event: Event) -> TextPreprocessor:
        voice = event.data.get("voice") or {}
        name = voice.get("name")
        # Language-only requests select a voice upstream, not our default.
        if not name and not voice.get("language") and default_voice:
            name = default_voice
            event.data = {**event.data, "voice": {**voice, "name": name}}
        if config_path is not None:
            return TextPreprocessor.from_json(config_path, name, ignore_case)
        return TextPreprocessor(csv_paths, ignore_case, voice_paths.get(name, ()))

    try:
        while True:
            event = await async_read_event(src_reader)
            if event is None:
                return
            if intercept:
                if SynthesizeStart.is_type(event.type):
                    stream = begin(event)
                elif SynthesizeChunk.is_type(event.type) and stream is not None:
                    event.data = {**event.data, "text": stream.feed(event.data["text"])}
                elif SynthesizeStop.is_type(event.type) and stream is not None:
                    tail = stream.feed("", final=True)
                    if tail:
                        await async_write_event(SynthesizeChunk(tail).event(), dst_writer)
                    stream = None
                elif Synthesize.is_type(event.type) and stream is None:
                    processor = begin(event)
                    event.data = {
                        **event.data, "text": processor.feed(event.data["text"], final=True),
                    }
                # A Synthesize inside a stream is the protocol's compatibility
                # aggregate. Streaming Piper ignores it: leave it entirely alone.
            elif Info.is_type(event.type) and "tts" in event.data:
                # Avoid deserializing/reserializing Info: retain unknown fields,
                # voice identifiers, capabilities, and the original payload.
                event.data = {
                    **event.data,
                    "tts": [{**program, "name": program_name} for program in event.data["tts"]],
                }
            await async_write_event(event, dst_writer)
    except (ConnectionResetError, BrokenPipeError):
        return
    except Exception:
        _LOG.exception("forwarder error")


async def _handle_client(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    upstream_host: str,
    upstream_port: int,
    csv_paths: list[Path],
    voice_paths: dict[str, list[Path]],
    default_voice: str | None,
    ignore_case: bool,
    config_path: Path | None,
    program_name: str,
) -> None:
    peer = writer.get_extra_info("peername")
    _LOG.debug("client connected: %s", peer)
    try:
        up_reader, up_writer = await asyncio.open_connection(
            upstream_host, upstream_port
        )
    except (OSError, asyncio.TimeoutError) as e:
        _LOG.error("upstream %s:%d unreachable: %s", upstream_host, upstream_port, e)
        writer.close()
        await writer.wait_closed()
        return

    tasks = [asyncio.create_task(_forward(
        src, dst, intercept, csv_paths, voice_paths, default_voice, ignore_case, config_path, program_name,
    )) for src, dst, intercept in (
        (reader, up_writer, True), (up_reader, writer, False),
    )]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for connection in (writer, up_writer):
            connection.close()
        await asyncio.gather(writer.wait_closed(), up_writer.wait_closed(), return_exceptions=True)
    _LOG.debug("client closed: %s", peer)


async def _amain(args: argparse.Namespace) -> None:
    csv_paths = [Path(p) for p in args.pronunciations]
    voice_paths: dict[str, list[Path]] = {}
    for voice, path in args.voice_pronunciations:
        voice_paths.setdefault(voice, []).append(path)

    server = await asyncio.start_server(
        lambda r, w: _handle_client(
            r, w, args.upstream_host, args.upstream_port, csv_paths,
            voice_paths, args.default_voice, args.ignore_case, args.config, args.name,
        ),
        host=args.host,
        port=args.port,
    )
    sockets = ", ".join(str(s.getsockname()) for s in server.sockets)
    _LOG.info("listening on %s, forwarding to %s:%d", sockets, args.upstream_host, args.upstream_port)
    if args.config is not None:
        _LOG.info("pronunciation config: %s", args.config)
    else:
        _LOG.info("pronunciation files: %s", [str(p) for p in csv_paths])
    async with server:
        await server.serve_forever()


def _voice_pronunciation(value: str) -> tuple[str, Path]:
    voice, separator, path = value.partition("=")
    if not separator or not voice or not path:
        raise argparse.ArgumentTypeError("expected VOICE=PATH")
    return voice, Path(path)


def main() -> None:
    p = argparse.ArgumentParser(prog="wyoming-piper-preproc")
    p.add_argument("--name", default="Piper Preprocessed", help="TTS program name advertised to clients")
    p.add_argument("--host", default=os.environ.get("LISTEN_HOST", "0.0.0.0"))
    p.add_argument("--port", type=int, default=int(os.environ.get("LISTEN_PORT", "10210")))
    p.add_argument("--upstream-host", default=os.environ.get("UPSTREAM_HOST", "127.0.0.1"))
    p.add_argument("--upstream-port", type=int, default=int(os.environ.get("UPSTREAM_PORT", "10200")))
    p.add_argument(
        "--pronunciations",
        nargs="*",
        default=os.environ.get(
            "PRONUNCIATIONS",
            "/data/pronunciations.csv:/data/pronunciations-piper.csv",
        ).split(":"),
        help="Shared CSV files (later files win duplicate keys within the shared pass)",
    )
    p.add_argument(
        "--voice-pronunciations", action="append", type=_voice_pronunciation,
        default=[], metavar="VOICE=PATH",
        help="Repeatable voice CSVs, applied as a separate pass after shared CSVs",
    )
    p.add_argument("--default-voice", help="Voice name for requests without a name or language")
    p.add_argument("--ignore-case", action="store_true", help="Match mapping keys case-insensitively")
    p.add_argument("--config", type=Path, help="JSON shared/voices mapping file; replaces CSV options")
    p.add_argument("--log-level", default=os.environ.get("LOG_LEVEL", "INFO"))
    args = p.parse_args()
    if args.config is not None:
        try:
            TextPreprocessor.from_json(args.config)
        except (OSError, ValueError) as error:
            p.error(f"invalid pronunciation config: {error}")

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        asyncio.run(_amain(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
