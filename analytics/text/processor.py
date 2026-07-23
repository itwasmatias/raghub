import string


class TextProcessor:

    STOP_WORDS = {
        "the",
        "a",
        "an",
        "is",
        "of",
        "to",
        "in",
        "for",
        "and",
        "on",
    }

    def process(self, text: str) -> list[str]:
        tokens = []

        for word in text.lower().split():
            word = word.strip(string.punctuation)

            if word and word not in self.STOP_WORDS:
                tokens.append(word)

        return tokens