from __future__ import annotations

from pathlib import Path

from tools import client_data_export


def test_client_data_docker_build_args_match_python_pins() -> None:
    dockerfile = Path("tools/client_data/Dockerfile").read_text(encoding="utf-8")
    captured: list[list[str]] = []

    def runner(command: list[str], *, check: bool) -> None:
        assert check
        captured.append(command)

    client_data_export.build_image(runner=runner)
    assert len(captured) == 1
    command = captured[0]
    for argument, revision in client_data_export._docker_source_revisions().items():
        assert f"{argument}={revision}" in command
        assert f"ARG {argument}" in dockerfile
    assert "sed -i" not in dockerfile
