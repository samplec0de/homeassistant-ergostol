"""Make the pure protocol layer importable without Home Assistant.

Importing custom_components.ergostol.protocol would execute the integration's
__init__.py, which imports homeassistant — unavailable in this venv. Point the
path at the integration directory instead and import the module directly.
"""

from pathlib import Path
import sys

sys.path.insert(
    0, str(Path(__file__).resolve().parents[1] / "custom_components" / "ergostol")
)
