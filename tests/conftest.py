"""Shared test fixtures for Hive Mind test suite."""

import sys
import os


# Ensure the project root is on sys.path so imports work
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# Point every gateway variable at a closed port before anything imports the
# code under test.
#
# Set, never delete. `config.py` calls `load_dotenv()` at import, and that
# import happens lazily inside the first attach — so a `monkeypatch.delenv`
# in a fixture holds only until the first test that touches the config, at
# which point the operator's real `.env` refills `COMMS_URL` and the suite
# starts issuing live requests against the running hive-comms container.
# `load_dotenv` does not overwrite a variable that already has a value, so
# claiming these names here is the one thing it cannot undo.
os.environ["COMMS_URL"] = "http://127.0.0.1:9"
os.environ["COMMS_ADMIN_BEARER_TOKEN"] = "test-token-never-valid"
os.environ["COMMS_BEARER_TOKEN"] = "test-token-never-valid"
