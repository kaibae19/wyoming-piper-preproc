"""Regression tests for the public whole-text and incremental APIs."""
import csv
import json
import tempfile
import unittest
from pathlib import Path

from wyoming_piper_preproc.preproc import TextPreprocessor, preprocess


def write_csv(path, rows):
    with path.open("w", encoding="utf-8", newline="") as output:
        csv.writer(output).writerows(rows)
    return path


class PreprocessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def csv(self, name, rows):
        return write_csv(self.root / name, rows)

    def test_longest_substrings_nonrecursive_and_optional_case(self):
        path = self.csv("shared.csv", [
            ("Umer", "Name"), ("Umerfarooq", "Full"),
            ("Name", "WRONG"), ("Full", "WRONG"),
            ("cycle-one", "cycle-two"), ("cycle-two", "cycle-one"),
        ])
        text = "Umer Umerfarooq Umerfarooqs umer cycle-one cycle-two"
        self.assertEqual(preprocess(text, [path]), "Name Full Fulls umer cycle-two cycle-one")
        self.assertEqual(preprocess("UMERfarooq UMerabc", [path], True), "Full Nameabc")

    def test_real_csv_unicode_empty_keys_deletion_and_whitespace(self):
        path = self.root / "shared.csv"
        path.write_text(
            '\ufeff# comment\r\n\r\n,never\r\n"a,b"," said ""hello"", μg "\r\n'
            '°F, degrees Fahrenheit \r\nremove,\r\n"multi\nline","two\nlines"\r\n',
            encoding="utf-8",
        )
        self.assertEqual(
            preprocess("a,b 5°F remove multi\nline", [path]),
            ' said "hello", μg  5 degrees Fahrenheit   two\nlines',
        )

    def test_later_files_and_rows_win_within_each_layer(self):
        first = self.csv("first.csv", [("Name", "old"), ("Name", "new")])
        last = self.csv("last.csv", [("NAME", "last")])
        self.assertEqual(preprocess("Name", [first]), "new")
        self.assertEqual(preprocess("name", [first, last], True), "last")
        self.assertEqual(preprocess("name", [], True, [first, last]), "last")

    def test_invalid_files_rows_and_missing_files_do_not_break_synthesis(self):
        bad = self.root / "bad.csv"
        bad.write_bytes(b"\xff")
        malformed = self.root / "malformed.csv"
        malformed.write_text('word,"unclosed', encoding="utf-8")
        rows = self.csv("rows.csv", [("bad", "row", "extra"), ("good", "ok")])
        with self.assertLogs("wyoming_piper_preproc.preproc", level="WARNING") as logs:
            self.assertEqual(preprocess("good bad word", [bad, malformed, rows, self.root / "missing.csv"]), "ok bad word")
        self.assertEqual(len(logs.output), 3)
        self.assertNotIn("unclosed", "\n".join(logs.output))

    def test_shared_then_voice_cascade_not_merge_or_recursion(self):
        shared = self.csv("shared.csv", [
            ("°C", " degrees Celsius"), ("Umie", "Umer"),
            ("global", "consumed"), ("cycle-one", "cycle-two"),
            ("cycle-two", "cycle-one"), ("already", "[[ ˈʊmɚ ]]"),
        ])
        voice = self.csv("voice.csv", [
            ("Celsius", "[[ ˈsɛlsiəs ]]"), ("Umer", "[[ ˈʊmɚ ]]"),
            ("global", "WRONG"), ("consumed", "normalized"),
            ("normalized", "WRONG"), ("cycle-two", "cycle-one"),
            ("cycle-one", "cycle-two"), ("ʊmɚ", "WRONG"),
        ])
        self.assertEqual(
            preprocess("5°C! Umie’s, global cycle-one already.", [shared], True, [voice]),
            "5 degrees [[ ˈsɛlsiəs! ]] [[ ˈʊmɚz, ]] normalized cycle-one [[ ˈʊmɚ. ]]",
        )
        self.assertEqual(preprocess("Umie global", [shared], True, [voice]), "[[ ˈʊmɚ ]] normalized")
        self.assertEqual(preprocess("Umie global", [shared]), "Umer consumed")

    def test_voice_cannot_override_consumed_shared_key(self):
        shared = self.csv("shared.csv", [("Umer", "shared-name")])
        voice = self.csv("voice.csv", [("Umer", "voice-name")])
        self.assertEqual(preprocess("Umer", [shared], voice_csv_paths=[voice]), "shared-name")

    def test_possessive_allomorphs_and_adjacent_punctuation(self):
        path = self.csv("voice.csv", [
            ("Umer", "[[ ˈʊmɚ ]]"), ("Pat", "[[ pæt ]]"),
            ("Ash", "[[ æʃ ]]"), ("Judge", "[[ dʒʌdʒ ]]"),
        ])
        self.assertEqual(
            preprocess("Umer's! Pat’s, Ash'S; Judge's?! Umerfarooq.", [], True, [path]),
            "[[ ˈʊmɚz! ]] [[ pæts, ]] [[ æʃɪz; ]] [[ dʒʌdʒɪz?! ]] [[ ˈʊmɚ ]]farooq.",
        )

    def test_existing_blocks_are_opaque_in_both_layers_even_unclosed(self):
        path = self.csv("shared.csv", [("Umer", "WRONG"), ("ʊmɚ", "WRONG")])
        text = "[[ Umer’s—ʊmɚ! ]] [[ Umer..."
        self.assertEqual(preprocess(text, [path], True, [path]), text)

    def test_keys_cannot_cross_existing_blocks(self):
        path = self.csv("shared.csv", [("a [[ Umer ]]", "WRONG"), ("a", "ok"), ("Umer", "WRONG")])
        self.assertEqual(preprocess("a [[ Umer ]]", [path]), "ok [[ Umer ]]")

    def test_voice_normalization_can_match_unicode_shared_output(self):
        shared = self.csv("shared.csv", [("alias", "Umer’s"), ("quote", "“hello”")])
        voice = self.csv("voice.csv", [("Umer", " [[ ˈʊmɚ ]]  "), ("“hello”", "normalized")])
        self.assertEqual(
            preprocess("alias! quote", [shared], voice_csv_paths=[voice]),
            " [[ ˈʊmɚz! ]]   normalized",
        )

    def test_unicode_and_double_dash_normalization_without_mappings(self):
        self.assertEqual(preprocess('“Hi” — ‘yes’ – ok… a -- b', []), '"Hi" ... \'yes\' ... ok... a... b')

    def test_every_split_and_character_chunks_match_whole_cascades(self):
        shared = self.csv("shared.csv", [("°C", " degrees Celsius"), ("Umie", "Umer")])
        voice = self.csv("voice.csv", [("Celsius", "[[ sɛlsiəs ]]"), ("Umer", "[[ ˈʊmɚ ]]")])
        samples = [
            "Umie’s?! 5°C; Umerfarooq. [[ Umer’s— ]] Umie'S.",
            "Umie -- 5°C... [[ Umer ]", "[[ Umer", "[", "---", "   --  ",
        ]
        for text in samples:
            expected = preprocess(text, [shared], True, [voice])
            splits = [[text[:i], text[i:]] for i in range(len(text) + 1)] + [list(text)]
            for chunks in splits:
                with self.subTest(text=text, chunks=chunks):
                    processor = TextPreprocessor([shared], True, [voice])
                    actual = "".join(processor.feed(chunk) for chunk in chunks)
                    self.assertEqual(actual + processor.feed("", final=True), expected)

    def test_utterance_snapshot_and_hot_reload_both_layers(self):
        shared = self.csv("shared.csv", [("Umie", "Umer")])
        voice = self.csv("voice.csv", [("Umer", "first")])
        processor = TextPreprocessor([shared], voice_csv_paths=[voice])
        output = processor.feed("Um")
        write_csv(shared, [("Umie", "Other")])
        write_csv(voice, [("Other", "second")])
        self.assertEqual(output + processor.feed("ie", final=True), "first")
        self.assertEqual(preprocess("Umie", [shared], voice_csv_paths=[voice]), "second")

    def test_long_stream_makes_progress_without_waiting_for_stop(self):
        path = self.csv("voice.csv", [("Umer", "[[ ˈʊmɚ ]]")])
        processor = TextPreprocessor([], voice_csv_paths=[path])
        chunks = ["Umer's! " * 64] * 128
        output = [processor.feed(chunk) for chunk in chunks]
        self.assertTrue(all(output))
        self.assertEqual("".join(output) + processor.feed("", True), "[[ ˈʊmɚz! ]] " * (64 * 128))

    def test_long_whitespace_and_block_and_punctuation_split_parity(self):
        path = self.csv("voice.csv", [("Umer", "[[ ˈʊmɚ ]]")])
        for text in [" " * 9000 + "-- hi", "[[ " + "x" * 20000 + "]] Umer", "Umer" + "!" * 20000]:
            expected = preprocess(text, [], voice_csv_paths=[path])
            processor = TextPreprocessor([], voice_csv_paths=[path])
            out = [processor.feed(text[i:i + 37]) for i in range(0, len(text), 37)]
            self.assertTrue(any(out[:-1]))
            self.assertEqual("".join(out) + processor.feed("", True), expected)

    def test_json_preserves_strings_and_snapshots_both_stages(self):
        path = self.root / "pronunciations.json"
        path.write_text(json.dumps({
            "shared": {'a,b': ' say "μ"\n', "alias": "Umer", "remove": ""},
            "voices": {"hfc": {"Umer": "[[ ˈʊmɚ ]]", "μ": "micro"}},
        }), encoding="utf-8")
        processor = TextPreprocessor.from_json(path, "hfc", True)
        path.write_text('{}', encoding="utf-8")
        result = "".join(processor.feed(char) for char in 'a,b ALIAS! remove')
        self.assertEqual(result + processor.feed("", True), ' say "micro"\n [[ ˈʊmɚ! ]] ')
        self.assertEqual(TextPreprocessor.from_json(path, "hfc").feed("alias", True), "alias")

    def test_json_rejects_non_string_or_non_mapping_rules(self):
        path = self.root / "bad.json"
        for value in [None, [], {"shared": []}, {"shared": {"word": 7}},
                      {"voices": []}, {"voices": {"hfc": {"word": None}}}]:
            with self.subTest(value=value):
                path.write_text(json.dumps(value), encoding="utf-8")
                with self.assertRaises(ValueError):
                    TextPreprocessor.from_json(path)


if __name__ == "__main__":
    unittest.main()
