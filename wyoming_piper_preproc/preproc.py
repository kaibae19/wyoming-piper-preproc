"""Piper substitutions, shared by whole requests and incremental streams."""
from __future__ import annotations

import csv
import json
import logging
import re
from pathlib import Path
from typing import Iterable

_LOG = logging.getLogger(__name__)
_PUNCT = ",.;:!?"
_UNICODE = str.maketrans({
    "“": '"', "”": '"', "‘": "'", "’": "'",
    "—": "...", "–": "...", "…": "...",
})
_SPACE = re.compile(r"\s+")
_SPACE_LIMIT = 4096


def _load_pronunciations(
    paths: Iterable[Path],
) -> list[tuple[str, str]]:
    """Read real CSV; only keys are stripped, never replacement whitespace."""
    pairs: list[tuple[str, str]] = []
    for path in paths:
        try:
            with path.open(encoding="utf-8-sig", newline="") as source:
                for row in csv.reader(source, strict=True):
                    if not row or row[0].lstrip().startswith("#"):
                        continue
                    if len(row) != 2:
                        _LOG.warning("expected two CSV columns in %s", path)
                        continue
                    key = row[0].strip()
                    if key:
                        pairs.append((key, row[1]))
        except FileNotFoundError:
            continue
        except (OSError, UnicodeError, csv.Error) as error:
            _LOG.warning("could not read %s: %s", path, error)
    return pairs


def espeak_possessive(phonemes: str) -> str:
    """Household Piper convention: append the /s/, /z/, or /ɪz/ allomorph."""
    last = phonemes.rstrip("ˈˌː")[-1:]
    if (last and last in "szʃʒ") or phonemes.endswith(("tʃ", "dʒ")):
        return "ɪz"
    return "s" if last and last in "ptkfθ" else "z"


class _Normalizer:
    """Normalize outside existing markup, retaining only lexical lookahead."""

    def __init__(self) -> None:
        self.pending = ""
        self.in_block = False
        self.skip_space = False

    def feed(self, text: str, final: bool) -> str:
        text = self.pending + text
        self.pending = ""
        out: list[str] = []
        pos = 0
        while pos < len(text):
            if self.skip_space:
                space = _SPACE.match(text, pos)
                if space:
                    pos = space.end()
                    continue
                self.skip_space = False
            if self.in_block:
                end = text.find("]]", pos)
                if end < 0:
                    keep = int(not final and text.endswith("]"))
                    out.append(text[pos:len(text) - keep])
                    pos = len(text) - keep
                    break
                out.append(text[pos:end + 2])
                pos = end + 2
                self.in_block = False
                continue
            if text.startswith("[[", pos):
                out.append("[[")
                pos += 2
                self.in_block = True
                continue
            if len(text) - pos == 1 and text[pos] == "[" and not final:
                break
            space = _SPACE.match(text, pos)
            end = space.end() if space else pos
            # Flush complete 4 KiB whitespace runs deterministically, including
            # in whole requests, so memory bounds do not depend on chunking.
            if end - pos >= _SPACE_LIMIT:
                count = ((end - pos) // _SPACE_LIMIT) * _SPACE_LIMIT
                out.append(text[pos:pos + count])
                pos += count
                continue
            if not final and (end == len(text) or (len(text) - end == 1 and text[end] == "-")):
                break
            if text.startswith("--", end):
                out.append("... ")
                pos = end + 2
                self.skip_space = True
                continue
            if space:
                out.append(text[pos:end])
                pos = end
                continue
            out.append(text[pos].translate(_UNICODE))
            pos += 1
        self.pending = text[pos:]
        return "".join(out)


class _ReplacementPass:
    """One layer's mapping snapshot and bounded incremental lexer.

    Matching is leftmost, longest-first, and non-cascading. Existing phoneme
    blocks are opaque (even across chunks). A replacement consisting of one
    phoneme block absorbs an adjacent possessive and punctuation inside it.
    """

    def __init__(self, pairs: Iterable[tuple[str, str]], ignore_case: bool = False) -> None:
        unique = {}
        for key, value in pairs:
            key = key.strip()
            if key:
                unique[key.lower() if ignore_case else key] = (key, value)
        pairs = sorted(unique.values(), key=lambda pair: len(pair[0]), reverse=True)
        self.replacements = [value for _, value in pairs]
        flags = re.IGNORECASE if ignore_case else 0
        self.pattern = re.compile(
            "|".join(f"({re.escape(key)})" for key, _ in pairs), flags,
        ) if pairs else None
        self.lookahead = max((len(key) for key, _ in pairs), default=0) + 2
        self.pending = ""
        self.in_block = False
        self.phoneme_tail = False
        self.phoneme_suffix = ""

    def feed(self, text: str, final: bool = False) -> str:
        text = self.pending + text
        self.pending = ""
        out: list[str] = []
        pos = 0
        block_start = text.find("[[")
        while pos < len(text):
            if self.phoneme_tail:
                # The block's opening/phonemes were already emitted. Consume
                # arbitrarily long punctuation runs without retaining them.
                if text[pos] in _PUNCT:
                    out.append(text[pos])
                    pos += 1
                    continue
                out.append(" ]]" + self.phoneme_suffix)
                self.phoneme_tail = False
            if self.in_block:
                end = text.find("]]", pos)
                if end < 0:
                    keep = int(not final and text.endswith("]"))
                    out.append(text[pos:len(text) - keep])
                    pos = len(text) - keep
                    break
                out.append(text[pos:end + 2])
                pos = end + 2
                self.in_block = False
                block_start = text.find("[[", pos)
                continue
            if text.startswith("[[", pos):
                out.append("[[")
                pos += 2
                self.in_block = True
                continue
            if not final and len(text) - pos < self.lookahead:
                break
            endpos = block_start if block_start >= pos else len(text)
            match = self.pattern.match(text, pos, endpos) if self.pattern else None
            if match:
                replacement = self.replacements[match.lastindex - 1]
                pos = match.end()
                phonemes = re.fullmatch(r"(\s*)\[\[\s*(.*?)\s*\]\](\s*)", replacement, re.DOTALL)
                if phonemes and "[[" not in phonemes[2] and "]]" not in phonemes[2]:
                    value = phonemes[2]
                    if text[pos:pos + 2].lower() in ("'s", "’s", "‘s"):
                        value += espeak_possessive(value)
                        pos += 2
                    out.append(phonemes[1] + "[[ " + value)
                    self.phoneme_suffix = phonemes[3]
                    self.phoneme_tail = True
                else:
                    out.append(replacement)
                continue
            out.append(text[pos])
            pos += 1
        if final and self.phoneme_tail:
            out.append(" ]]" + self.phoneme_suffix)
            self.phoneme_tail = False
        self.pending = text[pos:]
        return "".join(out)


class TextPreprocessor:
    """Normalize once, then run shared and selected-voice passes in order.

    Output may cascade between the two passes, never within a pass. Both
    snapshots are loaded at construction, not halfway through an utterance.
    """

    def __init__(
        self, csv_paths: Iterable[Path], ignore_case: bool = False,
        voice_csv_paths: Iterable[Path] = (),
        *, shared_rules: dict[str, str] | None = None,
        voice_rules: dict[str, str] | None = None,
    ) -> None:
        self.normalizer = _Normalizer()
        self.shared = _ReplacementPass(
            shared_rules.items() if shared_rules is not None else _load_pronunciations(csv_paths),
            ignore_case,
        )
        self.voice = _ReplacementPass(
            voice_rules.items() if voice_rules is not None else _load_pronunciations(voice_csv_paths),
            ignore_case,
        )

    @classmethod
    def from_json(cls, path: Path, voice: str | None = None, ignore_case: bool = False) -> TextPreprocessor:
        """Snapshot a shared/voices JSON document for one utterance."""
        with path.open(encoding="utf-8-sig") as source:
            config = json.load(source)
        if not isinstance(config, dict):
            raise ValueError("pronunciation config must be a JSON object")
        shared, voices = config.get("shared", {}), config.get("voices", {})
        if not isinstance(voices, dict):
            raise ValueError("voices must be an object of voice names to replacement maps")
        for rules in (shared, *voices.values()):
            if not isinstance(rules, dict) or not all(
                isinstance(key, str) and isinstance(value, str) for key, value in rules.items()
            ):
                raise ValueError("each replacement map must contain string keys and values")
        return cls((), ignore_case, shared_rules=shared, voice_rules=voices.get(voice, {}))

    def feed(self, text: str, final: bool = False) -> str:
        text = self.normalizer.feed(text, final)
        text = self.shared.feed(text, final)
        return self.voice.feed(text, final)


def preprocess(
    text: str, csv_paths: list[Path], ignore_case: bool = False,
    voice_csv_paths: Iterable[Path] = (),
) -> str:
    """Rewrite a whole request, reloading the CSVs for this utterance."""
    return TextPreprocessor(csv_paths, ignore_case, voice_csv_paths).feed(text, final=True)
