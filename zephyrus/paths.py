import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.environ.get("ZEPHYRUS_DATA", os.path.join(ROOT, "data"))
OUTPUT_DIR = os.environ.get("ZEPHYRUS_OUTPUT", os.path.join(ROOT, "output"))
