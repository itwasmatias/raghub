import unittest

from services.generator import AnswerGenerator


class AnswerGeneratorTests(unittest.TestCase):
    def test_generate_returns_a_text_answer_from_prompt(self):
        generator = AnswerGenerator()
        result = generator.generate("User Query: Example prompt")

        self.assertIsInstance(result, str)
        self.assertTrue(result.strip())
        self.assertIn("local answer", result.lower())


if __name__ == "__main__":
    unittest.main()
