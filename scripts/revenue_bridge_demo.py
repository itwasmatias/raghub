import sys
from pathlib import Path

# Add repo root to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from revenue_bridge.demo import run_creator_test_drive

if __name__ == "__main__":
    success = run_creator_test_drive(verbose=True)
    sys.exit(0 if success else 1)
