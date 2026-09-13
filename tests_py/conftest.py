import sys
from pathlib import Path

SRC = str(Path(__file__).resolve().parents[1] / "src")
while SRC in sys.path:
    sys.path.remove(SRC)
sys.path.insert(0, SRC)

# Purge any cached dev_orchestrator modules that might have been imported from another path
for mod in list(sys.modules):
    if mod == "dev_orchestrator" or mod.startswith("dev_orchestrator."):
        del sys.modules[mod]

