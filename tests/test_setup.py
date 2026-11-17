"""Deployment checks which specifically need malicious/ambiguous fixtures."""
from pathlib import Path
import runpy

import pytest

setup = runpy.run_path(str(Path(__file__).resolve().parents[1] / "setup_page.py"))


@pytest.mark.parametrize("entry", [
    "Path = Aki/../escape", "Path = C:\\escape", "Path = Aki/NUL.txt",
    "Path = Aki/file:stream", "Path = Aki/redirect\nSymbolic Link = ../../other",
    "Path = Aki/redirect\nHard Link = outside", "Path = Aki/junction\nAttributes = DL",
])
def test_reject_unsafe_archive_before_extraction(entry):
    with pytest.raises(ValueError, match="不安全"):
        setup["members"](entry)


def test_require_original_bundle_layout():
    with pytest.raises(ValueError, match="缺少"):
        setup["members"]("Path = demo/main.py\nSize = 100")
