import subprocess
import sys

import pytest


@pytest.mark.parametrize(
    "statement",
    [
        "import parameter_sweep",
    ],
    ids=repr,
)
@pytest.mark.parametrize(
    "sentinel",
    [
        "DEPRECATED",
    ],
    ids=repr,
)
def test_no_deprecation_warnings_emitted(sentinel: str, statement: str):
    res = subprocess.run(
        [sys.executable, "-c", statement],
        text=True,
        stderr=subprocess.PIPE,
        stdout=subprocess.PIPE,
    )
    assert res.returncode == 0
    assert not sentinel in res.stdout
    assert not sentinel in res.stderr
