# Tests import the bridge's pure modules directly (no radio, no FastAPI app).
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
