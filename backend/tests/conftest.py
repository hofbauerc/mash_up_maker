"""Isolate test data before any test module imports app.* (config reads the
environment at import time; conftest is loaded before all test modules)."""

import os
import tempfile

os.environ.setdefault("MASHUP_DATA_DIR", tempfile.mkdtemp(prefix="mashup_test_"))
