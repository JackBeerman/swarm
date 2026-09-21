"""
Shared test setup.

The modules live at the repo root rather than in a package, so the root
goes on sys.path here. A placeholder key is set before anything imports
swarm.py, which reads its configuration at import time. No test touches
the network: httpx is mocked at the transport layer with respx.
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("TYPESAFE_API_KEY", "test-key")
