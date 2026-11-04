"""Regenerates openapi.json from the code, so the two can't drift apart.

Run from the repo root: python -m scripts.export_openapi
"""

import json
from pathlib import Path

from app.main import create_app

spec = create_app().openapi()
Path("openapi.json").write_text(json.dumps(spec, indent=2) + "\n")
print(f"wrote openapi.json ({len(spec['paths'])} paths)")
