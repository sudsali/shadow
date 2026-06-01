"""Pytest fixtures shared across unit / integration / contract suites.

Adds the bot's source tree to sys.path so suites can `from shadow import …`
without a packaged install.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "scripts"))
