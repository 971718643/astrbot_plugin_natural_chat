"""Build an installable plugin using a fixed source allowlist."""

import ast
import hashlib
import json
from pathlib import Path
import zipfile


ROOT = Path(__file__).resolve().parent
FILES = (
    "_conf_schema.json", "delivery_status.py", "initiative.py", "initiative_runtime.py",
    "main.py", "metadata.yaml", "open_thread_runtime.py", "open_threads.py", "outcomes.py",
    "proactive.py", "README.md", "reply_marker_runtime.py", "reply_markers.py", "voice_markers.py",
)


def build():
    for name in FILES:
        path = ROOT / name
        if path.suffix == ".py":
            ast.parse(path.read_text(encoding="utf-8"), filename=name)
    json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
    version = next(line.split(":", 1)[1].strip() for line in
                   (ROOT / "metadata.yaml").read_text(encoding="utf-8").splitlines()
                   if line.startswith("version:"))
    destination = ROOT / "dist"
    destination.mkdir(exist_ok=True)
    target = destination / f"astrbot_plugin_turn_taking-{version}.zip"
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in sorted(FILES):
            archive.write(ROOT / name, "astrbot_plugin_turn_taking/" + name)
    record = {"version": version, "package": target.name, "bytes": target.stat().st_size,
              "sha256": hashlib.sha256(target.read_bytes()).hexdigest()}
    (destination / "package.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
    print(json.dumps(record, indent=2))


if __name__ == "__main__":
    build()
