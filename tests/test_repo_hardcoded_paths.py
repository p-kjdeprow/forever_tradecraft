"""Repository hardcode guard and scanner unit tests."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tests.support.hardcode_scan import (
    Finding,
    load_baseline,
    scan_bytes,
    scan_repository,
    scan_symlink,
    scan_text,
)

ROOT = Path(__file__).parents[1]
BASELINE = ROOT / "tests/fixtures/hardcode_baseline.json"


def _fragmented(*parts: str) -> str:
    return "".join(parts)


def _finding_keys(findings: tuple[Finding, ...]) -> dict[tuple[str, str, str], int]:
    return {(finding.path, finding.category, finding.match): finding.count for finding in findings}


def _describe(entries: dict[tuple[str, str, str], int]) -> str:
    return "\n".join(
        f"{path} [{category}] {match!r} (count={count})"
        for (path, category, match), count in sorted(entries.items())
    )


def test_repository_matches_hardcode_baseline() -> None:
    actual = _finding_keys(scan_repository(ROOT))
    expected = _finding_keys(load_baseline(BASELINE))
    added = {
        key: actual[key]
        for key in actual.keys() | expected.keys()
        if actual.get(key) != expected.get(key) and actual.get(key, 0) > expected.get(key, 0)
    }
    stale = {
        key: expected[key]
        for key in actual.keys() | expected.keys()
        if actual.get(key) != expected.get(key) and expected.get(key, 0) > actual.get(key, 0)
    }
    details: list[str] = []
    if added:
        details.append("new findings:\n" + _describe(added))
    if stale:
        details.append("stale baseline findings:\n" + _describe(stale))
    assert not added and not stale, (
        "hardcode baseline mismatch:\n"
        + "\n".join(details)
        + "\nRegenerate with: uv run python -m tests.support.hardcode_scan --write-baseline"
    )


def test_scan_repository_skips_tracked_file_deleted_from_worktree(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "--quiet", str(tmp_path)], check=True)
    deleted = tmp_path / "deleted.txt"
    deleted.write_text("/ho" + "me/example\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "deleted.txt"], check=True)
    deleted.unlink()

    assert scan_repository(tmp_path) == ()


@pytest.mark.parametrize(
    ("category", "text", "match"),
    [
        ("posix-home", _fragmented("path=/ho", "me/alice/project"), _fragmented("/ho", "me/alice")),
        ("posix-root", _fragmented("path=/ro", "ot/.config"), _fragmented("/ro", "ot/")),
        (
            "windows-user",
            _fragmented(r"path=C:\Use", r"rs\Alice\project"),
            _fragmented(r"C:\Use", r"rs\Alice"),
        ),
        ("runtime-dir", _fragmented("path=/run/us", "er/1000/crowley"), _fragmented("/run/us", "er/1000")),
        ("checkout-path", _fragmented("path=/work/repo", "s/crowley"), _fragmented("repo", "s/crowley")),
        (
            "mutter-auth",
            _fragmented("path=/tm", "p/.mutter-Xway", "landauth"),
            _fragmented(".mutter-Xway", "landauth"),
        ),
        (
            "temporary-path",
            _fragmented("path=/tm", "p/crowley-runtime"),
            _fragmented("/tm", "p/crowley-runtime"),
        ),
        (
            "docker-path",
            _fragmented("path=/var/lib/", "docker/containers"),
            _fragmented("/var/lib/", "docker"),
        ),
        (
            "systemd-absolute-path",
            _fragmented("ExecStart=/op", "t/crowley/bin/server"),
            _fragmented("/op", "t/crowley/bin/server"),
        ),
        ("shebang", _fragmented("#!", "/usr/bin/python3\n"), _fragmented("#!", "/usr/bin/python3")),
    ],
)
def test_scan_text_detects_each_category(category: str, text: str, match: str) -> None:
    findings = scan_text("synthetic.txt", text)
    assert Finding(path="synthetic.txt", category=category, match=match, count=1) in findings


def test_scan_text_aggregates_findings_by_exact_key() -> None:
    home = _fragmented("/ho", "me/alice")
    findings = scan_text("synthetic.txt", f"{home} {home}")
    assert findings == (Finding("synthetic.txt", "posix-home", home, 2),)


@pytest.mark.parametrize(
    ("text", "match"),
    [
        (_fragmented("path=/home/", "_build"), _fragmented("/home/", "_build")),
        (_fragmented("path=/t", "mp/", ".crowley.sock"), _fragmented("/t", "mp/", ".crowley.sock")),
        (_fragmented("path=/Users/", "Jane Doe"), _fragmented("/Users/", "Jane Doe")),
        (
            _fragmented("path=C:\\Use", "rs\\", "Jane Doe"),
            _fragmented("C:\\Use", "rs\\", "Jane Doe"),
        ),
    ],
)
def test_scan_text_supports_path_segment_edge_cases(text: str, match: str) -> None:
    findings = scan_text("synthetic.txt", text)
    assert any(finding.match == match for finding in findings)


def test_scan_text_detects_bare_temporary_directories_with_token_boundaries() -> None:
    tmp = _fragmented("/", "tmp")
    var_tmp = _fragmented("/var/", "tmp")
    nested_tmp = _fragmented("/", "tmp/one/two")
    nested_var_tmp = _fragmented("/var/", "tmp/one/two")
    tmpfs = _fragmented("/", "tmpfs")
    cases = (
        (f'path="{tmp}"', tmp),
        (f"path={tmp} value", tmp),
        (f"path={var_tmp}", var_tmp),
        (f"path={tmp}:socket", tmp),
        (f'path="{nested_tmp}"', nested_tmp),
        (f"path={nested_var_tmp} value", nested_var_tmp),
        (f"Use `{nested_tmp}` here.", nested_tmp),
        (f"See <{nested_var_tmp}>.", nested_var_tmp),
        (f"(`{tmp}`)", tmp),
        (f"[{nested_tmp}](link)", nested_tmp),
        (f"a {nested_tmp}, b", nested_tmp),
        (f"a {tmp}; b", tmp),
    )
    for text, match in cases:
        assert Finding("synthetic.txt", "temporary-path", match, 1) in scan_text("synthetic.txt", text)
    assert scan_text("synthetic.txt", f"path={tmpfs}") == ()


@pytest.mark.parametrize(
    "text",
    [
        "mount -t proc proc /proc/",
        "mount -t sysfs sysfs /sys/",
        "ExecStart=/dev/null",
        "ExecStart=/dev/shm",
        "ExecStart=/usr/bin/env",
        "ExecStart=/bin/sh",
        "#!/bin/sh\n",
        "#!/bin/bash\n",
        "#!/usr/bin/env python3\n",
    ],
)
def test_scan_text_allows_platform_contracts(text: str) -> None:
    assert scan_text("synthetic.txt", text) == ()


@pytest.mark.parametrize("specifier", ["%h", "%E", "%S", "%t", "%C"])
def test_scan_text_allows_systemd_specifiers(specifier: str) -> None:
    assert scan_text("synthetic.service", f"WorkingDirectory={specifier}/crowley") == ()


@pytest.mark.parametrize(
    ("directive", "prefix", "path"),
    [
        ("EnvironmentFile", "-", _fragmented("/op", "t/crowley.env")),
        ("WorkingDirectory", "-", _fragmented("/op", "t/crowley")),
        ("ExecStart", "@", _fragmented("/op", "t/crowley/bin/server")),
        ("ExecStart", "-", _fragmented("/op", "t/crowley/bin/server")),
        ("ExecStart", ":", _fragmented("/op", "t/crowley/bin/server")),
        ("ExecStart", "+", _fragmented("/op", "t/crowley/bin/server")),
        ("ExecStart", "!", _fragmented("/op", "t/crowley/bin/server")),
        ("ExecStart", "!!", _fragmented("/op", "t/crowley/bin/server")),
    ],
)
def test_scan_text_detects_prefixed_systemd_absolute_paths(directive: str, prefix: str, path: str) -> None:
    findings = scan_text("synthetic.service", f"{directive}={prefix}{path}")
    assert Finding("synthetic.service", "systemd-absolute-path", path, 1) in findings


def test_scan_text_detects_combined_systemd_execstart_prefixes() -> None:
    path = _fragmented("/op", "t/crowley/bin/server")
    findings = scan_text("synthetic.service", f"ExecStart=@-:+!{path}")
    assert Finding("synthetic.service", "systemd-absolute-path", path, 1) in findings


@pytest.mark.parametrize(
    ("text", "match"),
    [
        (
            _fragmented("http://127.0.", "0.1:8080/api"),
            _fragmented("http://127.0.", "0.1:8080/api"),
        ),
        (_fragmented("10.", "24.1.9"), _fragmented("10.", "24.1.9")),
        (_fragmented("100.64.", "0.1"), _fragmented("100.64.", "0.1")),
        (
            _fragmented("http://[", "::1", "]:8080"),
            _fragmented("http://[", "::1", "]:8080"),
        ),
        (_fragmented("service.", "local:9000"), _fragmented("service.", "local:9000")),
        (_fragmented("crowley.", "local (service)"), _fragmented("crowley.", "local")),
        (
            _fragmented("https://crowley.", "ts.net"),
            _fragmented("https://crowley.", "ts.net"),
        ),
        (_fragmented("crowley.", "ts.net (service)"), _fragmented("crowley.", "ts.net")),
        (_fragmented("127.0.", "0.1."), _fragmented("127.0.", "0.1")),
    ],
)
def test_scan_text_detects_machine_specific_urls(text: str, match: str) -> None:
    findings = scan_text("synthetic.txt", text)
    assert Finding("synthetic.txt", "machine-url", match, 1) in findings


def test_scan_text_ignores_python_local_attribute_but_flags_real_local_hosts() -> None:
    python_attribute = _fragmented("threading.", "local()")
    assert scan_text("synthetic.py", python_attribute) == ()
    string_literals = (
        (_fragmented('"printer.', 'local"'), _fragmented("printer.", "local")),
        (_fragmented('"http://box.', 'local"'), _fragmented("http://box.", "local")),
        (_fragmented('"foo.', 'local:8080"'), _fragmented("foo.", "local:8080")),
        (_fragmented('x = "a.', 'local"'), _fragmented("a.", "local")),
    )
    for source, match in string_literals:
        assert Finding("synthetic.py", "machine-url", match, 1) in scan_text("synthetic.py", source)


def test_scan_text_exempts_only_recorded_external_fixture_directory() -> None:
    recorded = (
        _fragmented("/var/lib/do", "cker/volumes/crowley_db/_data") + " " + _fragmented("172.20.", "0.2")
    )
    assert scan_text("tests/fixtures/external/docker-inspect.json", recorded) == ()
    assert scan_text("tests/fixtures/external/nested/docker-inspect.json", recorded) == ()
    docker_path = _fragmented("/var/lib/", "docker")
    finding = Finding("tests/fixtures/external-data/docker-inspect.json", "docker-path", docker_path, 1)
    assert finding in scan_text("tests/fixtures/external-data/docker-inspect.json", recorded)


def test_scan_text_counts_each_machine_url_occurrence() -> None:
    url = _fragmented("127.0.", "0.1:8080")
    findings = scan_text("synthetic.txt", f"{url} {url}")
    assert findings == (Finding("synthetic.txt", "machine-url", url, 2),)


def test_scan_text_allows_loopback_only_in_docker_deploy_files() -> None:
    url = _fragmented("http://127.0.", "0.1:8080/healthz")
    assert scan_text("deploy/docker/Dockerfile", url) == ()
    assert scan_text("src/crowley/ops/resources/docker-compose.yml", url) == ()
    assert Finding("README.md", "machine-url", url, 1) in scan_text("README.md", url)


def test_scan_text_ignores_public_urls() -> None:
    text = " ".join(
        [
            "https://github.com/p-kjdeprow/forever_tradecraft",
            "https://raw.githubusercontent.com/example/project/main/README.md",
            "https://blizzard.com/docs",
            "8.8.8.8:53",
        ]
    )
    assert scan_text("synthetic.txt", text) == ()


def test_scan_text_only_checks_the_first_line_for_shebangs() -> None:
    text = "documentation example\n" + _fragmented("#!", "/usr/bin/python3")
    assert scan_text("synthetic.txt", text) == ()


def test_scan_symlink_detects_absolute_target() -> None:
    assert scan_symlink("link", "/opt/crowley") == (
        Finding("link", "absolute-symlink-target", "/opt/crowley", 1),
    )
    assert scan_symlink("link", "../crowley") == ()
    assert scan_symlink("link", r"C:\Crowley") == (
        Finding("link", "absolute-symlink-target", r"C:\Crowley", 1),
    )


def test_scan_bytes_binary_content_is_not_scanned() -> None:
    assert scan_bytes("binary.dat", (_fragmented("/ho", "me/alice") + "\x00").encode()) == ()


def test_scan_repository_uses_tracked_and_intent_to_add_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GIT_INDEX_FILE", raising=False)
    subprocess.run(["git", "init", "--quiet"], cwd=tmp_path, check=True)
    home = _fragmented("/ho", "me/alice")
    (tmp_path / "untracked.txt").write_text(home, encoding="utf-8")
    intent_to_add = tmp_path / "intent-to-add.txt"
    intent_to_add.write_text(home, encoding="utf-8")
    subprocess.run(["git", "add", "-N", intent_to_add.name], cwd=tmp_path, check=True)

    findings = scan_repository(tmp_path)

    assert findings == (Finding("intent-to-add.txt", "posix-home", home, 1),)
