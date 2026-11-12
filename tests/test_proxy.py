"""Black-box CLI tests: real TCP Wyoming client, proxy process, fake Piper."""
import asyncio
import csv
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

from wyoming.event import Event, async_read_event, async_write_event


class ProxyTests(unittest.IsolatedAsyncioTestCase):
    use_json = False
    program_name = None

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.shared = self.write_csv("shared.csv", [("°C", " degrees Celsius"), ("Umie", "Umer")])
        self.voice = self.write_csv("lessac.csv", [("Umer", "[[ ˈʊmɚ ]]"), ("Celsius", "[[ sɛlsiəs ]]")])
        other = self.write_csv("other.csv", [("Umer", "OTHER")])
        first = self.write_csv("first.csv", [("Umer", "WRONG")])
        self.events = asyncio.Queue()
        self.calls = []
        self.handlers = set()
        self.connections = set()
        self.process = self.server = self.writer = None
        self.logs = ""
        self.addAsyncCleanup(self.close_resources)
        self.audio = [
            Event("audio-start", {"rate": 22050, "width": 2, "channels": 1, "extra": {"a": "μ"}}),
            Event("audio-chunk", {"rate": 22050, "width": 2, "channels": 1, "timestamp": 42}, b"\x00\xff\x80\x01"),
            Event("audio-stop", {"timestamp": 43, "extra": [1, 2]}),
        ]
        self.info = Event("info", {
            "tts": [{"name": "Piper", "custom": True, "supports_synthesize_streaming": True,
                     "voices": [{"name": "en_US-lessac-medium", "description": "Lessac", "speakers": ["0"]}]}],
            "asr": [{"name": "unrelated"}], "future": {"kept": True},
        }, b"info-payload")
        self.server = await asyncio.start_server(self.fake_piper, "127.0.0.1", 0)
        port = self.server.sockets[0].getsockname()[1]
        mapping_args = [
            "--pronunciations", str(self.shared),
            "--voice-pronunciations", f"en_US-lessac-medium={first}",
            "--voice-pronunciations", f"en_US-lessac-medium={self.voice}",
            "--voice-pronunciations", f"en_US-hfc_male-medium={self.voice}",
            "--voice-pronunciations", f"other={other}",
        ]
        if self.use_json:
            self.config = self.root / "pronunciations.json"
            with self.shared.open() as source:
                shared_rules = dict(csv.reader(source))
            with self.voice.open() as source:
                voice_rules = dict(csv.reader(source))
            self.config.write_text(json.dumps({
                "shared": shared_rules,
                "voices": {"en_US-lessac-medium": voice_rules,
                           "en_US-hfc_male-medium": voice_rules, "other": {"Umer": "OTHER"}},
            }), encoding="utf-8")
            mapping_args = ["--config", str(self.config)]
        if self.program_name is not None:
            mapping_args += ["--name", self.program_name]
        self.process = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "wyoming_piper_preproc", "--host", "127.0.0.1", "--port", "0",
            "--upstream-host", "127.0.0.1", "--upstream-port", str(port),
            *mapping_args, "--ignore-case", "--default-voice", "en_US-lessac-medium",
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
        async with asyncio.timeout(5):
            while True:
                line = await self.process.stderr.readline()
                self.assertTrue(line, self.logs)
                self.logs += line.decode()
                match = re.search(r"listening on \('127.0.0.1', (\d+)\)", self.logs)
                if match:
                    break
        self.reader, self.writer = await asyncio.open_connection("127.0.0.1", int(match[1]))

    def write_csv(self, name, rows):
        path = self.root / name
        with path.open("w", encoding="utf-8", newline="") as output:
            csv.writer(output).writerows(rows)
        return path

    async def close_resources(self):
        if self.writer is not None:
            self.writer.close()
            await self.writer.wait_closed()
        if self.process is not None:
            if self.process.returncode is None:
                self.process.terminate()
            await asyncio.wait_for(self.process.wait(), 5)
            self.logs += (await self.process.stderr.read()).decode()
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()
        for writer in self.connections.copy():
            writer.close()
        if self.handlers:
            await asyncio.wait_for(asyncio.gather(*self.handlers), 5)
        self.assertNotIn("rewrote:", self.logs)
        self.assertNotIn("Umie", self.logs)
        self.assertNotIn("forwarder error", self.logs)

    async def fake_piper(self, reader, writer):
        task = asyncio.current_task()
        self.handlers.add(task)
        self.connections.add(writer)
        streaming = False
        text = ""
        try:
            while (event := await async_read_event(reader)) is not None:
                await self.events.put(event)
                if event.type == "describe":
                    await async_write_event(self.info, writer)
                elif event.type == "audio-now":
                    for audio in self.audio:
                        await async_write_event(audio, writer)
                elif event.type == "synthesize-start":
                    streaming, text = True, ""
                elif event.type == "synthesize-chunk" and streaming:
                    text += event.data["text"]
                elif event.type == "synthesize-stop" or (event.type == "synthesize" and not streaming):
                    self.calls.append(text if streaming else event.data["text"])
                    for audio in self.audio:
                        await async_write_event(audio, writer)
                    if streaming:
                        await async_write_event(Event("synthesize-stopped", {"custom": "done"}), writer)
                    streaming = False
        finally:
            writer.close()
            await writer.wait_closed()
            self.connections.discard(writer)
            self.handlers.discard(task)

    async def send(self, *events):
        for event in events:
            await async_write_event(event, self.writer)

    async def received(self):
        return await asyncio.wait_for(self.events.get(), 5)

    async def response(self, streaming=False):
        expected = self.audio + ([Event("synthesize-stopped", {"custom": "done"})] if streaming else [])
        for event in expected:
            self.assertEqual(await asyncio.wait_for(async_read_event(self.reader), 5), event)

    async def test_standalone_metadata_payload_default_voice_and_audio(self):
        original = Event("synthesize", {"text": "UMIE's, 5°C!", "context": {"id": 1}, "future": ["μ"]}, b"opaque")
        await self.send(original)
        forwarded = await self.received()
        self.assertEqual(forwarded, Event("synthesize", {
            **original.data, "text": "[[ ˈʊmɚz, ]] 5 degrees [[ sɛlsiəs! ]]",
            "voice": {"name": "en_US-lessac-medium"},
        }, b"opaque"))
        await self.response()
        self.assertEqual(len(self.calls), 1)

    async def test_explicit_voices_do_not_leak_and_language_is_not_defaulted(self):
        cases = [
            ({"name": "other", "speaker": "7", "custom": True}, "OTHER 5 degrees Celsius"),
            ({"name": "unmapped"}, "Umer 5 degrees Celsius"),
            ({"language": "en", "custom": 1}, "Umer 5 degrees Celsius"),
            ({"name": "en_US-lessac-medium", "speaker": "9"}, "[[ ˈʊmɚ ]] 5 degrees [[ sɛlsiəs ]]"),
            ({"name": "en_US-hfc_male-medium"}, "[[ ˈʊmɚ ]] 5 degrees [[ sɛlsiəs ]]"),
        ]
        for voice, text in cases:
            with self.subTest(voice=voice):
                original = Event("synthesize", {"text": "Umie 5°C", "voice": voice, "context": {"session": "kept"}})
                await self.send(original)
                self.assertEqual(await self.received(), Event("synthesize", {**original.data, "text": text}))
                await self.response()

    async def test_discovery_name_preserves_voices_capabilities_and_other_events(self):
        events = [Event("describe", {"custom": "Umie"}), Event("custom", {"text": "Umie", "voice": {"name": "x"}}, b"\xff")]
        await self.send(*events)
        for event in events:
            self.assertEqual(await self.received(), event)
        expected = Event("info", {
            "tts": [{"name": self.program_name or "Piper Preprocessed", "custom": True,
                     "supports_synthesize_streaming": True,
                     "voices": [{"name": "en_US-lessac-medium", "description": "Lessac", "speakers": ["0"]}]}],
            "asr": [{"name": "unrelated"}], "future": {"kept": True},
        }, b"info-payload")
        self.assertEqual(await asyncio.wait_for(async_read_event(self.reader), 5), expected)

    async def run_stream(self, text, chunks, voice=None):
        data = {"context": {"session": "stream"}, "future": {"x": 1}}
        if voice is not None:
            data["voice"] = voice
        start = Event("synthesize-start", data, b"start")
        await self.send(start)
        actual_start = await self.received()
        self.assertEqual(actual_start, Event("synthesize-start", {
            **data, **({"voice": {"name": "en_US-lessac-medium"}} if voice is None else {}),
        }, b"start"))
        actual = []
        for index, chunk in enumerate(chunks):
            event = Event("synthesize-chunk", {"text": chunk, "sequence": index}, b"chunk")
            await self.send(event)
            forwarded = await self.received()
            self.assertEqual({k: v for k, v in forwarded.data.items() if k != "text"}, {"sequence": index})
            self.assertEqual(forwarded.payload, b"chunk")
            actual.append(forwarded.data["text"])
        aggregate = Event("synthesize", {"text": text, "voice": {"name": "other"}, "context": {"aggregate": True}}, b"aggregate")
        marker = Event("custom", {"text": "Umie", "context": {"keep": True}}, b"marker")
        stop = Event("synthesize-stop", {"context": {"stop": True}}, b"stop")
        await self.send(aggregate, marker, stop)
        self.assertEqual(await self.received(), aggregate)
        self.assertEqual(await self.received(), marker)
        while (event := await self.received()).type != "synthesize-stop":
            self.assertEqual(event.type, "synthesize-chunk")
            actual.append(event.data["text"])
        self.assertEqual(event, stop)
        await self.response(streaming=True)
        return "".join(actual)

    async def test_stream_character_boundaries_cascade_blocks_possessives_aggregate(self):
        text = "UMIE’s, 5°C! [[ Umer— ]] Umerfarooq."
        result = await self.run_stream(text, list(text))
        self.assertEqual(result, "[[ ˈʊmɚz, ]] 5 degrees [[ sɛlsiəs! ]] [[ Umer— ]] [[ ˈʊmɚ ]]farooq.")
        self.assertEqual(self.calls, [result])
        # A following standalone request is not mistaken for an aggregate.
        await self.send(Event("synthesize", {"text": "Umie"}))
        self.assertEqual((await self.received()).data["text"], "[[ ˈʊmɚ ]]")
        await self.response()
        self.assertEqual(len(self.calls), 2)

    async def test_stream_explicit_voice_and_utterance_hot_reload(self):
        self.assertEqual(await self.run_stream("Umie 5°C", ["Um", "ie 5°", "C"], {"name": "other", "speaker": "2"}), "OTHER 5 degrees Celsius")
        self.assertEqual(await self.run_stream("Umie 5°C", list("Umie 5°C"), {"name": "en_US-hfc_male-medium"}), "[[ ˈʊmɚ ]] 5 degrees [[ sɛlsiəs ]]")
        self.write_csv("shared.csv", [("Umie", "Changed")])
        self.write_csv("lessac.csv", [("Changed", "RELOADED")])
        if self.use_json:
            self.config.write_text(json.dumps({
                "shared": {"Umie": "Changed"},
                "voices": {"en_US-lessac-medium": {"Changed": "RELOADED"}},
            }), encoding="utf-8")
        self.assertEqual(await self.run_stream("Umie", ["Um", "ie"]), "RELOADED")

    async def test_stream_cascade_parity_at_every_two_chunk_split(self):
        text = "Umie’s, 5°C!"
        expected = "[[ ˈʊmɚz, ]] 5 degrees [[ sɛlsiəs! ]]"
        for split in range(len(text) + 1):
            with self.subTest(split=split):
                self.assertEqual(await self.run_stream(text, [text[:split], text[split:]]), expected)
        self.assertEqual(self.calls, [expected] * (len(text) + 1))

    async def test_audio_is_forwarded_while_text_stream_remains_open(self):
        await self.send(Event("synthesize-start"), Event("synthesize-chunk", {"text": "Umie, " * 10}), Event("audio-now"))
        for _ in range(3):
            await self.received()
        await self.response()
        self.assertEqual(self.calls, [])
        await self.send(Event("synthesize-stop"))
        while (await self.received()).type != "synthesize-stop":
            pass
        await self.response(streaming=True)

    async def test_stream_makes_progress_before_stop_and_empty_stream(self):
        await self.send(Event("synthesize-start"))
        await self.received()
        await self.send(Event("synthesize-chunk", {"text": "Umie's, " * 1000}))
        forwarded = await self.received()
        self.assertIn("[[ ˈʊmɚz, ]]", forwarded.data["text"])
        await self.send(Event("synthesize-stop"))
        while (await self.received()).type != "synthesize-stop":
            pass
        await self.response(streaming=True)
        self.assertEqual(self.calls, ["[[ ˈʊmɚz, ]] " * 1000])
        self.assertEqual(await self.run_stream("", []), "")


class JsonProxyTests(ProxyTests):
    use_json = True
    program_name = "Piper Personalized"


if __name__ == "__main__":
    unittest.main()
