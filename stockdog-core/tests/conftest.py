"""Pytest configuration for stockdog tests."""
import sys
from pathlib import Path

# Add the stockdog-core directory to sys.path so imports work
stockdog_core_root = Path(__file__).parent.parent
sys.path.insert(0, str(stockdog_core_root))
