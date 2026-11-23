import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from qmig.config import ConfigError, instance_names, load_dotenv, load_instance


class DotenvTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = Path(self.tmp.name) / ".env"
        patcher = patch.dict(os.environ, {}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        for key in [k for k in os.environ if k.startswith("QMIG_")]:
            del os.environ[key]

    def test_parses_comments_quotes_export_and_inline_comment(self):
        self.env.write_text('# note\n\nexport QMIG_A_URL="https://a.example.com"\nQMIG_A_TOKEN=abc123  # inline\n'
                            "QMIG_B_TOKEN='x y'\n")
        self.assertEqual(load_dotenv(self.env), ["QMIG_A_URL", "QMIG_A_TOKEN", "QMIG_B_TOKEN"])
        self.assertEqual(os.environ["QMIG_A_URL"], "https://a.example.com")
        self.assertEqual(os.environ["QMIG_A_TOKEN"], "abc123")
        self.assertEqual(os.environ["QMIG_B_TOKEN"], "x y")

    def test_real_environment_wins(self):
        os.environ["QMIG_A_TOKEN"] = "from-shell"
        self.env.write_text("QMIG_A_TOKEN=from-file\n")
        self.assertEqual(load_dotenv(self.env), [])
        self.assertEqual(os.environ["QMIG_A_TOKEN"], "from-shell")

    def test_missing_file_and_bad_line(self):
        self.assertEqual(load_dotenv(self.env), [])
        self.env.write_text("not a pair\n")
        with self.assertRaises(ConfigError):
            load_dotenv(self.env)

    def test_instances_come_from_the_environment(self):
        self.env.write_text("QMIG_HS_URL=https://hs.example.com\nQMIG_HS_TOKEN=tok123\n")
        load_dotenv(self.env)
        self.assertEqual(instance_names(), ["hs"])
        inst = load_instance("hs")
        self.assertEqual((inst.base_url, inst.token), ("https://hs.example.com/api/v3", "tok123"))

    def test_missing_settings_are_reported_by_name(self):
        self.assertEqual(instance_names(), [])
        with self.assertRaises(ConfigError) as caught:
            load_instance("hs")
        self.assertIn("QMIG_HS_URL", str(caught.exception))
        os.environ["QMIG_HS_URL"] = "https://hs.example.com"
        with self.assertRaises(ConfigError) as caught:
            load_instance("hs")
        self.assertIn("QMIG_HS_TOKEN", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
