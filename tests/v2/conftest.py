import pathlib
import sys
import os

SRC = pathlib.Path(__file__).resolve().parents[2] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
os.environ["PYTHONPATH"] = os.pathsep.join(
    part for part in (str(SRC), os.environ.get("PYTHONPATH", "")) if part
)
