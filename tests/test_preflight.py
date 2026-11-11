import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import preflight  # noqa: E402


def test_parse_vram():
    assert preflight.parse_vram("NVIDIA GeForce GTX 1650, 4096, 521") == ("NVIDIA GeForce GTX 1650", 4.0, 521 / 1024)


def test_free_ram_sane():
    free, total = preflight.free_ram_gb()
    assert 0 < free <= total


def test_config_has_boot_brain_and_no_real_key():
    cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    assert cfg["boot_brain"] in cfg["brains"]
    assert all(b["api_key"] == "ollama" for b in cfg["brains"].values())


def test_sample_notes():
    # the 30 shipped sample notes; ignore user captures and the trash folder
    notes = [p for p in (ROOT / "notes").rglob("*.md") if not {"captures", ".trash"} & set(p.relative_to(ROOT / "notes").parts)]
    assert len(notes) == 30
    titles = {p.stem for p in notes}
    for p in notes:
        assert p.read_text(encoding="utf-8").startswith("# ")
        for link in re.findall(r"\[\[([^\]]+)\]\]", p.read_text(encoding="utf-8")):
            assert link in titles, f"{p.name} has dangling link [[{link}]]"
