"""The README's code, output and example statement are the real ones."""

from __future__ import annotations

import contextlib
import io
import re
import runpy
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def block(name: str) -> str:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    match = re.search(
        rf"<!-- {name}:start -->\n```text\n(.*?)```\n<!-- {name}:end -->", readme, re.DOTALL
    )
    assert match is not None
    return match.group(1)


def test_quickstart_output_matches_the_readme() -> None:
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        runpy.run_path(str(ROOT / "examples" / "quickstart.py"), run_name="__main__")
    assert buffer.getvalue() == block("quickstart-output")


def test_readme_code_block_is_the_example() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    example = (ROOT / "examples" / "quickstart.py").read_text(encoding="utf-8")
    body = example.split('"""', 2)[2].lstrip("\n")
    assert f"```python\n{body}```" in readme


def test_readme_statement_is_the_committed_statement() -> None:
    # test_example.py checks that the committed statement is the generated one.
    committed = (ROOT / "examples" / "march" / "statement.md").read_text(encoding="utf-8")
    assert block("statement-output") == committed
