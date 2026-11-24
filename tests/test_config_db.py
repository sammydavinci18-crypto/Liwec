"""Run with:  python -m unittest discover -s tests"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# config.py imports SQLAlchemy and python-dotenv; the helper under test needs neither,
# so stand in for them when they are not installed (e.g. a bare CI box).
import types  # noqa: E402
for _name, _attrs in (("sqlalchemy", {}), ("sqlalchemy.pool", {"NullPool": object}),
                      ("dotenv", {"load_dotenv": lambda *a, **k: None})):
    try:
        __import__(_name)
    except ImportError:
        sys.modules[_name] = types.SimpleNamespace(**_attrs) if _name != "sqlalchemy" else types.ModuleType(_name)
        if _name == "sqlalchemy.pool":
            sys.modules[_name] = types.ModuleType(_name)
            sys.modules[_name].NullPool = object

from config import _postgres_driver  # noqa: E402

URL = "postgresql://u:p@aws-0-eu.pooler.supabase.com:6543/postgres?sslmode=require"


class PostgresDriverTests(unittest.TestCase):
    def test_psycopg3_only_gets_prepared_statements_off(self):
        uri, args = _postgres_driver(URL, have=lambda n: n == "psycopg")
        self.assertTrue(uri.startswith("postgresql+psycopg://"))
        self.assertEqual(args, {"prepare_threshold": None})

    def test_explicit_psycopg3_url(self):
        uri, args = _postgres_driver(URL.replace("postgresql", "postgresql+psycopg", 1), have=lambda n: True)
        self.assertEqual(args, {"prepare_threshold": None})

    def test_psycopg2_is_left_alone(self):
        uri, args = _postgres_driver(URL, have=lambda n: n in ("psycopg2", "psycopg"))
        self.assertEqual((uri, args), (URL, {}))

    def test_other_databases_and_empty_url(self):
        self.assertEqual(_postgres_driver("", have=lambda n: True), ("", {}))
        self.assertEqual(_postgres_driver("sqlite:///x.db", have=lambda n: True), ("sqlite:///x.db", {}))


if __name__ == "__main__":
    unittest.main()
