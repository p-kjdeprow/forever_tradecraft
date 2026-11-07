"""Scan tracked text files for machine-specific paths and launch contracts."""

from __future__ import annotations

import argparse
import ipaddress
import json
import ntpath
import re
import subprocess
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

BASELINE_RELATIVE_PATH: Final = Path("tests/fixtures/hardcode_baseline.json")
_ARCHIVE_PREFIX = ".archive/"
_BINARY_NUL = b"\x00"
_SYSTEMD_SPECIFIERS = frozenset({"%h", "%E", "%S", "%t", "%C"})
_SYSTEMD_ASSIGNMENT_RE = re.compile(
    r"^[ \t]*(?P<directive>WorkingDirectory|ExecStart|EnvironmentFile)=(?P<value>[^\r\n]*)$",
    re.MULTILINE,
)
_SHEBANG_RE = re.compile(r"^#!(?=[/ \t])[^\r\n]*$")
_PATH_SEGMENT = r"[^/\\\r\n\"'<>`]+"
_PATH_TOKEN = r"[^/\\\s\r\n\"'<>`]+"
_POSIX_HOME_RE = re.compile(r"(?<![A-Za-z0-9_.-])/(?:home|Users)/" + _PATH_SEGMENT)
_ROOT_HOME_RE = re.compile(r"(?<![A-Za-z0-9_.-])/" + r"root(?:/|$)")
_WINDOWS_USER_RE = re.compile(r"(?i)(?<![A-Za-z0-9_.-])C:[\\/]+Users[\\/]+" + _PATH_SEGMENT)
_RUNTIME_DIR_RE = re.compile(r"(?<![A-Za-z0-9_.-])/run/" + r"user/[0-9]+")
_CHECKOUT_RE = re.compile(r"(?<![A-Za-z0-9_.-])repos[/\\]" + _PATH_TOKEN)
_MUTTER_AUTH_RE = re.compile(r"\.mutter-Xway" + r"landauth")
_TMP_PATH_RE = re.compile(r"(?<![A-Za-z0-9_.-])/(?:tmp|var/tmp)/" + _PATH_SEGMENT)
_DOCKER_PATH_RE = re.compile(r"(?<![A-Za-z0-9_.-])/var/lib/" + r"docker(?=$|[/\\])")
_SYSTEMD_PREFIXES = {
    "WorkingDirectory": frozenset("-"),
    "ExecStart": frozenset("@-:+!"),
    "EnvironmentFile": frozenset("-"),
}
_MACHINE_HOST_RE = (
    r"(?:\[[0-9a-f:]+\]|"
    + "local"
    + "host"
    + r"|(?:[0-9]{1,3}\.){3}[0-9]{1,3}"
    + r"|[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?(?:\.[A-Za-z0-9-]+)*\.(?:local|ts\.net))"
)
_MACHINE_URL_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9_.:/-])"
    r"(?:(?:[A-Za-z][A-Za-z0-9+.-]*)://)?(?P<host>"
    + _MACHINE_HOST_RE
    + r")(?::[0-9]+)?(?:[/?#][^\s\"'<>]*)?(?![A-Za-z0-9_-]|\.(?=[A-Za-z0-9_-]))"
)
_PRIVATE_IPV4_NETWORKS = (
    ipaddress.ip_network("10" + ".0.0.0/8"),
    ipaddress.ip_network("172" + ".16.0.0/12"),
    ipaddress.ip_network("192" + ".168.0.0/16"),
    ipaddress.ip_network("100" + ".64.0.0/10"),
)
_DOCKER_DEPLOY_PATHS: Final = frozenset(
    {
        "deploy/docker/Dockerfile",
        "src/crowley/ops/resources/docker-compose.yml",
    }
)


@dataclass(frozen=True, slots=True)
class Finding:
    """One aggregated scanner finding."""

    path: str
    category: str
    match: str
    count: int


def _is_excluded(path: str) -> bool:
    return path == ".archive" or path.startswith(_ARCHIVE_PREFIX) or path == str(BASELINE_RELATIVE_PATH)


def _add_matches(occurrences: Counter[tuple[str, str]], category: str, matches: Sequence[str]) -> None:
    for match in matches:
        occurrences[(category, match)] += 1


def _path_matches(pattern: re.Pattern[str], text: str) -> list[str]:
    return [match.rstrip() for match in pattern.findall(text)]


def _systemd_path(directive: str, value: str) -> str | None:
    stripped = value.strip()
    prefixes = _SYSTEMD_PREFIXES[directive]
    while stripped and stripped[0] in prefixes:
        stripped = stripped[1:]
    if not stripped or any(stripped.startswith(specifier) for specifier in _SYSTEMD_SPECIFIERS):
        return None
    first_value = stripped.split(maxsplit=1)[0].strip("\"'")
    if not first_value.startswith("/"):
        return None
    return first_value


def _is_allowed_systemd_path(path: str) -> bool:
    if path in {"/dev/null", "/dev/shm", "/proc/", "/sys/", "/usr/bin/env", "/bin/sh"}:
        return True
    return False


def _is_machine_host(host: str) -> bool:
    normalized = host.strip("[]").lower()
    if normalized == "local" + "host" or normalized.endswith(".local") or normalized.endswith(".ts.net"):
        return True
    try:
        address = ipaddress.ip_address(normalized)
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv4Address):
        return address.is_loopback or any(address in network for network in _PRIVATE_IPV4_NETWORKS)
    return address == ipaddress.IPv6Address(":" + ":1")


def _is_allowed_docker_loopback(path: str, host: str) -> bool:
    """Allow container-local loopback only in the Docker deployment files.

    IPv4 loopback and hostname forms in ``deploy/docker/`` refer to the
    container itself or enforce a loopback-only host bind; the same values
    elsewhere remain machine-specific.
    """

    if path not in _DOCKER_DEPLOY_PATHS:
        return False
    normalized = host.strip("[]").lower()
    if normalized == "local" + "host":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def _sorted_findings(path: str, occurrences: Counter[tuple[str, str]]) -> tuple[Finding, ...]:
    return tuple(
        Finding(path=path, category=category, match=match, count=occurrences[(category, match)])
        for category, match in sorted(occurrences)
    )


def scan_text(path: str, text: str) -> tuple[Finding, ...]:
    """Scan one decoded text file and return deterministic aggregated findings."""

    occurrences: Counter[tuple[str, str]] = Counter()
    _add_matches(occurrences, "posix-home", _path_matches(_POSIX_HOME_RE, text))
    _add_matches(occurrences, "posix-root", _ROOT_HOME_RE.findall(text))
    _add_matches(occurrences, "windows-user", _path_matches(_WINDOWS_USER_RE, text))
    _add_matches(occurrences, "runtime-dir", _RUNTIME_DIR_RE.findall(text))
    _add_matches(occurrences, "checkout-path", _path_matches(_CHECKOUT_RE, text))
    _add_matches(occurrences, "mutter-auth", _MUTTER_AUTH_RE.findall(text))
    _add_matches(occurrences, "temporary-path", _path_matches(_TMP_PATH_RE, text))
    _add_matches(occurrences, "docker-path", _DOCKER_PATH_RE.findall(text))

    systemd_matches: list[str] = []
    for assignment in _SYSTEMD_ASSIGNMENT_RE.finditer(text):
        systemd_path = _systemd_path(assignment.group("directive"), assignment.group("value"))
        if systemd_path is not None and not _is_allowed_systemd_path(systemd_path):
            systemd_matches.append(systemd_path)
    _add_matches(occurrences, "systemd-absolute-path", systemd_matches)

    first_line = text.partition("\n")[0].rstrip("\r")
    shebang = _SHEBANG_RE.fullmatch(first_line)
    invalid_shebangs = []
    if (
        shebang is not None
        and shebang.group(0) not in {"#!/bin/sh", "#!/bin/bash"}
        and re.fullmatch(r"#!/usr/bin/env [^\s]+", shebang.group(0)) is None
    ):
        invalid_shebangs.append(shebang.group(0))
    _add_matches(occurrences, "shebang", invalid_shebangs)

    url_matches = [
        match.group(0)
        for match in _MACHINE_URL_RE.finditer(text)
        if _is_machine_host(match.group("host"))
        and not _is_allowed_docker_loopback(path, match.group("host"))
    ]
    _add_matches(occurrences, "machine-url", url_matches)
    return _sorted_findings(path, occurrences)


def scan_bytes(path: str, data: bytes) -> tuple[Finding, ...]:
    """Scan one tracked file, skipping binary or non-UTF-8 content."""

    if _BINARY_NUL in data:
        return ()
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return ()
    return scan_text(path, text)


def scan_symlink(path: str, target: str) -> tuple[Finding, ...]:
    """Return a finding when a tracked symlink points at an absolute target."""

    if Path(target).is_absolute() or ntpath.isabs(target):
        return (Finding(path=path, category="absolute-symlink-target", match=target, count=1),)
    return ()


def _tracked_paths(root: Path) -> tuple[str, ...]:
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    paths = tuple(path.decode("utf-8") for path in result.stdout.split(b"\x00") if path)
    return tuple(path for path in paths if not _is_excluded(path))


def scan_repository(root: Path) -> tuple[Finding, ...]:
    """Scan files listed by ``git ls-files``, including intent-to-add files."""

    findings: list[Finding] = []
    for relative_path in _tracked_paths(root):
        file_path = root / relative_path
        if not file_path.exists() and not file_path.is_symlink():
            continue
        if file_path.is_symlink():
            findings.extend(scan_symlink(relative_path, file_path.readlink().as_posix()))
            continue
        findings.extend(scan_bytes(relative_path, file_path.read_bytes()))
    return tuple(sorted(findings, key=lambda finding: (finding.path, finding.category, finding.match)))


def load_baseline(path: Path) -> tuple[Finding, ...]:
    """Load and validate a JSON baseline."""

    raw: object = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError(f"hardcode baseline must be a JSON list: {path}")

    findings: list[Finding] = []
    for index, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise ValueError(f"hardcode baseline entry {index} must be an object: {path}")
        required = ("path", "category", "match", "count")
        if any(key not in entry for key in required):
            raise ValueError(f"hardcode baseline entry {index} is missing a required key: {path}")
        entry_path = entry["path"]
        category = entry["category"]
        match = entry["match"]
        count = entry["count"]
        if not isinstance(entry_path, str) or not isinstance(category, str) or not isinstance(match, str):
            raise ValueError(f"hardcode baseline entry {index} has non-string fields: {path}")
        if isinstance(count, bool) or not isinstance(count, int) or count < 1:
            raise ValueError(f"hardcode baseline entry {index} has an invalid count: {path}")
        findings.append(Finding(path=entry_path, category=category, match=match, count=count))
    return tuple(sorted(findings, key=lambda finding: (finding.path, finding.category, finding.match)))


def render_baseline(findings: Sequence[Finding]) -> str:
    """Render findings as sorted, deterministic JSON."""

    payload = [
        {"path": finding.path, "category": finding.category, "match": finding.match, "count": finding.count}
        for finding in sorted(findings, key=lambda finding: (finding.path, finding.category, finding.match))
    ]
    return f"{json.dumps(payload, indent=2, sort_keys=True)}\n"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--write-baseline",
        action="store_true",
        help=f"write the generated baseline to {BASELINE_RELATIVE_PATH}",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    root = Path.cwd()
    findings = scan_repository(root)
    baseline_path = root / BASELINE_RELATIVE_PATH
    if args.write_baseline:
        baseline_path.write_text(render_baseline(findings), encoding="utf-8")
        print(f"wrote {len(findings)} findings to {baseline_path}")
    else:
        print(render_baseline(findings), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
