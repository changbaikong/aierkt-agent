import unittest

from agent_core.expression_stream import ExpressionStream


class ExpressionStreamTests(unittest.TestCase):
    def test_leading_whitespace_does_not_shift_final_text_cues(self):
        parser = ExpressionStream()
        self.assertEqual(parser.feed(' \n[EMO joy]你好'), '你好')
        self.assertEqual(parser.cues, [(0, 'joy')])

    def test_every_possible_split_keeps_text_and_offsets(self):
        raw = '[EMO joy]你好😀。[EMO shy]才没有呢。'
        for split in range(len(raw) + 1):
            parser = ExpressionStream()
            clean = parser.feed(raw[:split]) + parser.feed(raw[split:]) + parser.finish()
            self.assertEqual(clean, '你好😀。才没有呢。')
            self.assertEqual(parser.cues, [(0, 'joy'), (5, 'shy')])

    def test_invalid_and_unfinished_markers_never_leak(self):
        parser = ExpressionStream()
        clean = parser.feed('文字[EMO unknown]继续[EMO ' + 'x' * 200 + ']结束[EMO sh') + parser.finish()
        self.assertEqual(clean, '文字继续结束')
        self.assertEqual(parser.cues, [])

    def test_ordinary_brackets_and_markdown_survive(self):
        parser = ExpressionStream()
        raw = '[链接](https://example.test) [1, 2] [Example]'
        self.assertEqual(''.join(parser.feed(char) for char in raw) + parser.finish(), raw)

    def test_no_duplicate_expression_cues(self):
        parser = ExpressionStream()
        self.assertEqual(parser.feed('[EMO joy]你好[EMO joy]呀'), '你好呀')
        self.assertEqual(parser.cues, [(0, 'joy')])

    def test_accepts_angry_and_sad_as_distinct_expression_cues(self):
        parser = ExpressionStream()
        clean = parser.feed('[EMO angry]别闹了。[EMO sad]我只是有点难过。')

        self.assertEqual(clean, '别闹了。我只是有点难过。')
        self.assertEqual(parser.cues, [(0, 'angry'), (4, 'sad')])
