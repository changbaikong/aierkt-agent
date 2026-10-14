"""Extract reserved expression markers before assistant text becomes visible."""

EXPRESSIONS = frozenset(
    {'neutral', 'serious', 'angry', 'sad', 'tired', 'thinking', 'concern', 'joy', 'shy', 'surprise'}
)


class ExpressionStream:
    def __init__(self) -> None:
        self.pending = ''
        self.discarding = False
        self.offset = 0
        self.started = False
        self.cues: list[tuple[int, str]] = []

    def feed(self, chunk: str) -> str:
        output = []
        for char in chunk:
            if self.discarding:
                if char == ']' or char == '\n':
                    self.discarding = False
                continue
            if self.pending or char == '[':
                self.pending += char
                if '[EMO'.startswith(self.pending):
                    continue
                if self.pending.startswith('[EMO'):
                    if char == ']':
                        expression = self.pending[4:-1].strip()
                        if expression in EXPRESSIONS and (not self.cues or self.cues[-1][1] != expression):
                            self.cues.append((self.offset, expression))
                        self.pending = ''
                    elif len(self.pending) > 96 or char == '\n':
                        self.pending = ''
                        self.discarding = char != '\n'
                    continue
                text, self.pending = self.pending, ''
            else:
                text = char
            if not self.started:
                text = text.lstrip()
                self.started = bool(text)
            output.append(text)
            # JavaScript string length and the typewriter use UTF-16 code units.
            self.offset += len(text.encode('utf-16-le', errors='surrogatepass')) // 2
        return ''.join(output)

    def finish(self) -> str:
        reserved = self.pending.startswith('[EMO') or (len(self.pending) > 1 and '[EMO'.startswith(self.pending))
        text = '' if reserved else self.pending
        self.pending = ''
        self.discarding = False
        return text


def clean_expression_text(text: str) -> str:
    parser = ExpressionStream()
    return parser.feed(text) + parser.finish()
