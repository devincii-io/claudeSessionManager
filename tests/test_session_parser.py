from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from csm.session_parser import iter_file_records, read_new_lines


class IncrementalReaderTests(unittest.TestCase):
    def test_partial_trailing_line_is_not_consumed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            transcript = Path(tmp) / "session.jsonl"
            transcript.write_bytes(b'{"n":1}\n{"n":2}')
            lines, offset = read_new_lines(transcript, 0)
            self.assertEqual(lines, [b'{"n":1}'])
            self.assertEqual(offset, len(b'{"n":1}\n'))

            with transcript.open("ab") as fh:
                fh.write(b"\n")
            appended, new_offset = read_new_lines(transcript, offset)
            self.assertEqual(appended, [b'{"n":2}'])
            self.assertEqual(new_offset, transcript.stat().st_size)

    def test_full_record_iteration_skips_bad_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            transcript = Path(tmp) / "session.jsonl"
            transcript.write_bytes(b'{"ok":1}\nnot-json\n{"ok":2}\n')
            self.assertEqual(list(iter_file_records(transcript)), [{"ok": 1}, {"ok": 2}])


if __name__ == "__main__":
    unittest.main()
