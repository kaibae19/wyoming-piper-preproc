# wyoming-piper-preproc

A tiny [Wyoming protocol](https://github.com/rhasspy/wyoming) proxy that sits in front of [Piper](https://github.com/rhasspy/wyoming-piper) and preprocesses standalone and streaming synthesis text using shared and per-voice mappings (JSON or legacy CSV). This is a Piper proxy, not a multi-backend TTS engine.

Use it when Piper mispronounces specific words (your kid's name, a brand, a place) or reads unicode glyphs as raw characters (`μg/m³` → "mug em three"). Without this, you'd be patching pronunciations into the LLM's system prompt and hoping it doesn't slip; with this, you get a deterministic substitution layer that lives outside the model.

## Architecture

```
HA  ──Synthesize(text)─▶  wyoming-piper-preproc  ──Synthesize(text')─▶  Piper
HA  ◀────audio chunks─────         │             ◀────audio chunks─────  Piper
                               (mappings)
```

Synthesis text is rewritten, plus an optional default voice on requests that omit it. Discovery advertises the TTS program as `Piper Preprocessed` by default (configurable with `--name`). Voice names/IDs, unknown fields, context, speaker selection, payloads, other events, and upstream audio/metadata are preserved. One inbound TCP connection ↔ one upstream connection; multiple utterances on a connection are supported. Speech text is not logged, including at INFO.

## Quick start

```bash
git clone https://github.com/kaibae19/wyoming-piper-preproc.git
cd wyoming-piper-preproc

# Edit data/pronunciations-piper.csv to taste
# Set UPSTREAM_HOST/PORT for your real Piper instance
docker compose up -d --build

# In Home Assistant, edit the Piper Wyoming integration
# and change Port from 10200 to 10210.
```

## Published container image

GitHub Actions runs the regression tests, then builds the Dockerfile for
`linux/amd64` and `linux/arm64`. Pull requests build without publishing or
logging in to the registry. Pushes to `main` publish to
`ghcr.io/<owner>/<repository>` with `latest` and
`sha-<full-commit-SHA>` tags. Pushing a version tag such as `v1.2.3` publishes
that image tag and its SHA tag without moving `latest`. The workflow can also
be run manually; only runs on `main` update `latest`.

The image namespace comes from `github.repository`, not a hard-coded account.
For this repository it is `ghcr.io/kaibae19/wyoming-piper-preproc`; forks publish
under their own owner and repository name.

Publishing uses the repository's `GITHUB_TOKEN` with `packages: write`; no
separate registry secret is required. GitHub Actions must be enabled for the
repository. To allow pulls without authentication, set the GHCR package's visibility
to public after its first publication.

After the repository's first publication, use its image instead of building locally:

```bash
docker run --rm --network host \
  -v "$PWD/pronunciations.json:/data/pronunciations.json:ro" \
  ghcr.io/kaibae19/wyoming-piper-preproc:latest \
  --host 127.0.0.1 --port 10210 \
  --upstream-host 127.0.0.1 --upstream-port 10200 \
  --config /data/pronunciations.json \
  --default-voice en_US-lessac-medium --ignore-case
```

This example uses Linux host networking and the JSON format below. Pin a
`sha-...` tag or image digest when you need a fixed build.

## Configuration

| env var | default | meaning |
|---|---|---|
| `LISTEN_HOST` | `0.0.0.0` | Bind host |
| `LISTEN_PORT` | `10210` | Bind port |
| `UPSTREAM_HOST` | `127.0.0.1` | Real Piper host |
| `UPSTREAM_PORT` | `10200` | Real Piper port |
| `PRONUNCIATIONS` | `/data/pronunciations.csv:/data/pronunciations-piper.csv` | Colon-separated list of CSVs; later wins on conflicts |
| `LOG_LEVEL` | `INFO` | Set to `DEBUG` for per-connection trace |

The corresponding CLI flags are `--host`, `--port`, `--upstream-host`,
`--upstream-port`, `--pronunciations PATH ...`, and `--log-level`.
Explicit CLI values take precedence over environment defaults.

```bash
python -m wyoming_piper_preproc \
  --upstream-host piper --upstream-port 10200 \
  --config /data/pronunciations.json \
  --default-voice en_US-lessac-medium --ignore-case
```

- `--config PATH`: load one JSON document containing shared and per-voice maps.
  This replaces both CSV options; CSV files are not loaded when it is supplied.
- `--name NAME`: advertise this TTS program name in discovery responses; defaults
  to `Piper Preprocessed`. For example, `--name "Piper Personalized"` sets a local
  display name without changing voice names/IDs or capabilities. Home Assistant
  may need its Wyoming integration reloaded to pick up the name; existing manual
  entity-name overrides are not changed by this option.
- `--ignore-case`: optional Unicode case-insensitive key matching (Python
  `re.IGNORECASE`); the default is case-sensitive. Replacement case is preserved.
- `--voice-pronunciations VOICE=PATH`: repeatable; only files for the selected
  voice's exact name are used. Repeat the same voice to layer several files.
  No voice mappings are loaded for another/unmapped voice.
- `--default-voice NAME`: when a standalone request or stream start has neither
  a voice name nor language, insert this name and select its mappings. Existing
  speaker/extra voice fields are kept. Explicit names take precedence;
  language-only requests are left to Piper and use shared mappings only.
  Without this option, an omitted voice also uses shared mappings only.
- These options are CLI-only (including as Docker entrypoint arguments).
  Use `--pronunciations` with no paths to disable shared CSVs.

## Pronunciation JSON format

```json
{
  "shared": {
    "°C": " degrees Celsius",
    "Umie": "Umer"
  },
  "voices": {
    "en_US-lessac-medium": {
      "Umer": "[[ ˈʊmɚ ]]"
    },
    "en_US-hfc_male-medium": {
      "Umer": "[[ ˈʊmɚ ]]"
    }
  }
}
```

The file is UTF-8 (an optional BOM is accepted). `shared` and `voices` are
optional objects; each replacement map must have string keys and string values.
Keys are stripped; replacement whitespace is preserved. An empty replacement
deletes the key; empty keys are ignored. A voice uses only its exact-name map;
unlisted voices receive shared rules only. Invalid/unreadable JSON fails startup;
an invalid update closes the affected connection rather than using partial rules.
Update the file atomically. The example deliberately reuses one pronunciation
for two voices; phonemes should be evaluated separately for each voice.

### Authoring phoneme overrides

With [eSpeak NG](https://github.com/espeak-ng/espeak-ng) installed, generate an
IPA starting point for an English pronunciation:

```bash
espeak-ng -v en-us --ipa=3 -q 'example'
```

Choose the language that matches your Piper voice, wrap the phonemes in
`[[ ... ]]`, and add the replacement to that voice's map. Listen to the result
with the target Piper voice: eSpeak NG output is a starting point, not a
guarantee of the model's pronunciation. Put a rule in `shared` only when it is
appropriate for every voice using the proxy. These are text-time overrides;
they do not modify the Piper model or add model-level pronunciation dictionaries.

## Legacy pronunciation CSV format

Without `--config`, CSV options remain supported:

```bash
python -m wyoming_piper_preproc \
  --pronunciations /data/shared.csv \
  --voice-pronunciations en_US-lessac-medium=/data/lessac.csv
```

```csv
# Two columns, no header. Lines starting with # are comments.
# Longest entries are applied first so multi-word phrases match before substrings.
μg/m³,micrograms per cubic meter
°C,degrees Celsius
Kailey,Kaylee
ADP,A-Dee-Pee
```

CSV files are UTF-8 (an optional BOM is accepted), with exactly two columns and
no header. Standard CSV quoting supports commas, doubled quotes, and multiline
fields. Keys are stripped; **replacement whitespace is preserved**, including
leading/trailing spaces. An empty replacement deletes the key; empty keys and
blank/comment records are ignored. Missing files are skipped; malformed rows
and unreadable files produce warnings without logging their contents.

For example, keep common units in `shared.csv`:

```csv
°C, degrees Celsius
Umie,Umer
```

and voice-dependent pronunciations in `lessac.csv`:

```csv
Umer,[[ ˈʊmɚ ]]
# Add a Celsius phoneme rule here if this voice needs it.
```

### Matching and two-pass contract

1. Normalize smart quotes to ASCII, em/en dashes and ellipses to `...`, and
   `--` plus surrounding whitespace to `... `, outside existing phoneme blocks.
   To bound streaming lookahead, complete runs of 4096 whitespace characters
   preceding `--` are preserved; only the remaining shorter run is removed.
2. Run the shared map (or all shared CSVs) as **one pass**.
3. Run the selected voice's map (or CSVs) as **one separate pass over shared output**.

Both layers accept arbitrary literal text-to-text replacements, including
normalization, respelling, deletion, and Piper `[[ phonemes ]]` syntax. Each
pass scans left-to-right, choosing the **longest key at each position**.
Substring matching is intentional: `Umer` in `Umerfarooq` is replaced. Regex
metacharacters in keys have no special meaning. Duplicate keys within a layer
use the last row/file (case variants also collide with `--ignore-case`).

Replacements are **not revisited within the same pass**, so cycles do not loop
and replacements cannot accidentally cascade within a layer. Cascades **between
the two layers are intentional**: shared `°C → degrees Celsius` can feed a
voice-specific `Celsius → [[ phonemes ]]`, and shared `Umie → Umer` can feed
the Lessac rule above. The voice stage is not merged with the shared dictionary:
if a shared rule already consumes `Umer`, a voice rule with the original `Umer`
key cannot override it. Keep voice-dependent rules in the voice layer.

Existing `[[...]]` blocks are opaque to normalization and both passes, including
blocks produced by the shared stage. An unclosed block protects the remaining
text. A replacement consisting of a single phoneme block absorbs an immediately
adjacent `'s`/`’s` (the /s/, /z/, or /ɪz/ suffix) and `,.;:!?` punctuation inside
the block: `Umer’s,` becomes `[[ ˈʊmɚz, ]]`. This avoids Piper reading the suffix
or punctuation as separate words. The proxy does not validate phonemes against
a voice's model.

Both layers reload at each standalone `synthesize` or `synthesize-start`.
A stream uses one mapping snapshot until stop; file edits affect the next
utterance, not the middle of the current one. Replace mapping files atomically
when updating them.

### Streaming contract

Use a **streaming-capable Wyoming Piper** for streamed requests. The proxy does
not negotiate a fallback or turn chunks into standalone synthesis calls.
Send `synthesize-start`, zero or more `synthesize-chunk` events, optionally the
protocol's compatibility aggregate `synthesize`, then `synthesize-stop`.
The start event determines the voice and context for the entire stream.
The aggregate is forwarded **unchanged**, not synthesized again or used to
replace stream metadata; streaming Piper ignores it. A standalone `synthesize`
outside a stream is preprocessed normally. Chunks outside a stream pass through.

Keys, possessives, punctuation, and phoneme delimiters may cross any chunk
boundary. Concatenating forwarded chunk text gives the same result as whole-text
preprocessing. Only bounded lexical lookahead is held: fewer than 4096 whitespace
characters for normalization, and roughly the longest key plus two characters
per mapping pass. Long phoneme blocks and punctuation runs make incremental progress;
the proxy does not collect the whole utterance. Generated or existing blocks may
span forwarded chunks, which Piper must concatenate as part of its text stream.
Original chunk events are retained (their text can be empty or shifted);
unknown chunk metadata and payloads stay on their original events. Any remaining
text is sent in one additional text-only chunk immediately before the original
stop event. Audio and other upstream events are forwarded independently while
text continues to arrive.

## Building / testing locally

```bash
# Direct (no docker), matching the container runtime dependency
pip install wyoming==1.9.0
python -m unittest discover -s tests -v
python -m wyoming_piper_preproc --upstream-host 192.168.x.x --upstream-port 10200

# Smoke test against an upstream Piper
python - <<'PY'
import asyncio
from wyoming.client import AsyncTcpClient
from wyoming.tts import Synthesize, SynthesizeVoice
from wyoming.audio import AudioChunk, AudioStop

async def main():
    async with AsyncTcpClient("127.0.0.1", 10210) as c:
        await c.write_event(Synthesize(text="The PM2.5 reading is 1 μg/m³.").event())
        chunks = []
        while True:
            e = await c.read_event()
            if e is None or AudioStop.is_type(e.type): break
            if AudioChunk.is_type(e.type): chunks.append(AudioChunk.from_event(e).audio)
        print(f"got {sum(map(len,chunks))} bytes of audio")
asyncio.run(main())
PY
```

Regression tests exercise the public preprocessing APIs and launch the real CLI
against a local TCP fake Piper, checking voice isolation, two-stage cascades,
chunk boundaries, hot reload, aggregate handling, and transparent audio/metadata.
They do not download a Piper voice or assess pronunciation acoustically.

## Lessons learned (don't repeat them)

- **"cubic" sounded like Kubrick** via Piper's default voice (`en_US-hfc_female-medium`). Tempting fix: phonetic respelling like `cubic,kyoo-bik`. **Don't.** It came out as "kuel bic" (the hyphen breaks Piper's phrasing) and was worse than the original. Leave Piper's pronunciation of common technical words alone unless you can verify the result with the round-trip diagnostic first.

## License

MIT — see [LICENSE](LICENSE).

## Acknowledgements

The text-preprocessing pipeline pattern (normalize → pronunciations → engine-punctuation, all engine-aware) is borrowed from the TTS preprocessor in the VE-Analysis-API project (private). Same idea — substitute then synthesize — applied to a different host.
