# SPDX-License-Identifier: AGPL-3.0-only
"""Installed entry points must show help without creating runtime state."""
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class CliSmokeTests(unittest.TestCase):
    def test_help_never_creates_state_or_starts_login(self):
        for module, required_flag in (
            ('youpin_rent_repricer', '--apply'),
            ('youpin_rent_repricer.setup_server', '--state-dir'),
        ):
            with self.subTest(module=module), tempfile.TemporaryDirectory() as directory:
                result = subprocess.run(
                    [sys.executable, '-m', module, '--help'],
                    cwd=directory, capture_output=True, text=True, timeout=10,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(required_flag, result.stdout)
                self.assertEqual(list(Path(directory).iterdir()), [])


if __name__ == '__main__':
    unittest.main()
