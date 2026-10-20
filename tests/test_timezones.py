"""Contact-hour scheduling must work without an OS-provided IANA database."""
import os
import subprocess
import sys


def test_berlin_contact_hours_have_dst_with_no_system_timezone_database():
    env = {**os.environ, "PYTHONTZPATH": ""}
    code = """
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
berlin = ZoneInfo('Europe/Berlin')
assert datetime(2026, 7, 1, 9, tzinfo=berlin).utcoffset() == timedelta(hours=2)
assert datetime(2026, 11, 15, 9, tzinfo=berlin).utcoffset() == timedelta(hours=1)
"""
    result = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
