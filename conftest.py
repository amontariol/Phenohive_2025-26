"""Pytest root conftest: add the project root to sys.path.

Without this, `import src` and `import main` both fail when pytest is invoked
from outside the project root directory.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
