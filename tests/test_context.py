import unittest

from agent.context import Ticket, build_user_message, detect_tokens, load_lab_data, select_articles

DATA = load_lab_data()


def article_ids(text, tokens_mentioned=False):
    return [a.id for a in select_articles(text, DATA.kb, tokens_mentioned)]


class SelectArticlesTest(unittest.TestCase):
    def test_kb_114_is_always_included(self):
        self.assertEqual(article_ids("hello, quick question"), ["KB-114"])

    def test_trigger_map(self):
        self.assertEqual(article_ids("My deposit isn't showing"), ["KB-101", "KB-114"])
        self.assertEqual(article_ids("My transfer to ACC-70011 never arrived"), ["KB-104", "KB-114"])
        self.assertEqual(article_ids("I lost my 2FA phone"), ["KB-108", "KB-114"])
        self.assertEqual(article_ids("Withdrawal is stuck"), ["KB-102", "KB-114"])

    def test_keywords_match_at_word_start_only(self):
        self.assertEqual(article_ids("it was uncredited"), ["KB-114"])

    def test_token_mention_pulls_in_kb_121(self):
        self.assertIn("KB-121", article_ids("what is this", tokens_mentioned=True))


class DetectTokensTest(unittest.TestCase):
    def symbols(self, text):
        return [t["symbol"] for t in detect_tokens(text, DATA.tokens)]

    def test_by_symbol_name_or_contract(self):
        self.assertEqual(self.symbols("I got some vlqr"), ["VLQR"])
        self.assertEqual(self.symbols("What is Thistlemint?"), ["THSM"])
        self.assertEqual(self.symbols("contract 0x9E8D7C6B5A4938271605F4E3D2C1B0A9F8E7D6C5"), ["CVUSD"])

    def test_symbol_must_be_a_whole_word(self):
        self.assertEqual(self.symbols("PLMTX and xTHSM"), [])


class UserMessageTest(unittest.TestCase):
    def test_token_metadata_is_included(self):
        message = build_user_message(Ticket("ACC-10021", "Someone airdropped me PLMT"), DATA)
        self.assertIn("[Token metadata: PLMT]", message)
        self.assertIn("pellumite.example", message)
        self.assertIn("[KB article KB-121]", message)

    def test_unknown_submitter_is_rejected(self):
        with self.assertRaises(ValueError):
            build_user_message(Ticket("ACC-00000", "hi"), DATA)


if __name__ == "__main__":
    unittest.main()
