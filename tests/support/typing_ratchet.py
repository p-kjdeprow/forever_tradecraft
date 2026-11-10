"""Run Pyright and maintain the repository's type-error ratchet.

The typing test requires both the ``pyright`` command and its Node.js runtime.
Neither dependency is optional: if Pyright cannot produce JSON diagnostics, the
test and this command fail loudly rather than skipping or using a fallback.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

BASELINE_RELATIVE_PATH: Final = Path("tests/fixtures/typing_baseline.json")
SOURCE_RELATIVE_PATH: Final = Path("src/crowley")
STRICT_DIRECTORY_RELATIVE_PATHS: Final = (Path("advisor"), Path("config"), Path("domain"), Path("platform"))
STRICT_FILE_RELATIVE_PATHS: Final = (Path("core/auction_rules.py"),)


class PyrightExecutionError(RuntimeError):
    """Raised when Pyright or its required Node.js runtime cannot run."""


@dataclass(frozen=True, slots=True, order=True)
class PackageErrorCount:
    """The number of Pyright errors assigned to one source package."""

    package: str
    errors: int


@dataclass(frozen=True, slots=True)
class TypingCounts:
    """Pyright error counts for basic and strict source areas."""

    basic: tuple[PackageErrorCount, ...]
    strict: tuple[PackageErrorCount, ...]


def _strict_package(source_relative_path: Path) -> str | None:
    for directory in STRICT_DIRECTORY_RELATIVE_PATHS:
        if source_relative_path == directory or directory in source_relative_path.parents:
            return directory.as_posix()
    for file_path in STRICT_FILE_RELATIVE_PATHS:
        if source_relative_path == file_path:
            return file_path.as_posix()
    return None


def _package_names(root: Path) -> tuple[str, ...]:
    source_root = root / SOURCE_RELATIVE_PATH
    package_names = [
        entry.name for entry in source_root.iterdir() if entry.is_dir() and (entry / "__init__.py").is_file()
    ]
    strict_directories = set(STRICT_DIRECTORY_RELATIVE_PATHS)
    return tuple(
        sorted(
            package_name
            for package_name in package_names
            if Path(package_name) not in strict_directories
            or any(
                path.is_file()
                for path in (source_root / package_name).rglob("*.py")
                if _strict_package(path.relative_to(source_root)) is None
            )
        )
    )


def _relative_source_path(root: Path, filename: str) -> Path:
    path = Path(filename)
    if path.is_absolute():
        try:
            path = path.relative_to(root)
        except ValueError as exc:
            raise PyrightExecutionError(
                f"Pyright reported a file outside the repository: {filename}"
            ) from exc
    try:
        return path.relative_to(SOURCE_RELATIVE_PATH)
    except ValueError as exc:
        raise PyrightExecutionError(f"Pyright reported a file outside src/crowley: {filename}") from exc


def _json_object(value: object, description: str) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise PyrightExecutionError(f"Pyright JSON field {description} must be an object")
    return value


def _diagnostic_filename(diagnostic: Mapping[str, object]) -> str:
    filename = diagnostic.get("file")
    if not isinstance(filename, str) or not filename:
        raise PyrightExecutionError("Pyright JSON diagnostic is missing a file name")
    return filename


def _diagnostic_severity(diagnostic: Mapping[str, object]) -> str:
    severity = diagnostic.get("severity")
    if not isinstance(severity, str) or not severity:
        raise PyrightExecutionError("Pyright JSON diagnostic is missing a severity")
    return severity


def _counts_from_report(root: Path, report: Mapping[str, object]) -> TypingCounts:
    diagnostics_value = report.get("generalDiagnostics")
    if not isinstance(diagnostics_value, list):
        raise PyrightExecutionError("Pyright JSON report is missing generalDiagnostics")

    basic_counts = {package: 0 for package in _package_names(root)}
    strict_counts = {
        path.as_posix(): 0 for path in (*STRICT_DIRECTORY_RELATIVE_PATHS, *STRICT_FILE_RELATIVE_PATHS)
    }
    for raw_diagnostic in diagnostics_value:
        diagnostic = _json_object(raw_diagnostic, "generalDiagnostics entry")
        if _diagnostic_severity(diagnostic) != "error":
            continue
        source_relative_path = _relative_source_path(root, _diagnostic_filename(diagnostic))
        strict_package = _strict_package(source_relative_path)
        if strict_package is not None:
            strict_counts[strict_package] += 1
            continue
        if not source_relative_path.parts:
            raise PyrightExecutionError("Pyright reported an error without a source package")
        package = source_relative_path.parts[0]
        if package not in basic_counts:
            raise PyrightExecutionError(f"Pyright reported an error for an unknown package: {package}")
        basic_counts[package] += 1

    return TypingCounts(
        basic=tuple(PackageErrorCount(package, basic_counts[package]) for package in sorted(basic_counts)),
        strict=tuple(PackageErrorCount(package, strict_counts[package]) for package in sorted(strict_counts)),
    )


def run_pyright(root: Path) -> TypingCounts:
    """Run Pyright in JSON mode and return deterministic package error counts."""

    try:
        result = subprocess.run(
            ["pyright", "--outputjson", "--project", "pyproject.toml"],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        raise PyrightExecutionError(
            "Pyright and Node.js are required for typing checks; unable to execute pyright"
        ) from exc
    if result.returncode not in (0, 1):
        detail = result.stderr.strip()
        if detail:
            raise PyrightExecutionError(f"Pyright exited with status {result.returncode}: {detail}")
        raise PyrightExecutionError(f"Pyright exited with status {result.returncode} without diagnostics")
    if not result.stdout.strip():
        detail = result.stderr.strip()
        if detail:
            raise PyrightExecutionError(f"Pyright could not run: {detail}")
        raise PyrightExecutionError("Pyright could not run: it produced no JSON output")
    try:
        report_value: object = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise PyrightExecutionError("Pyright produced invalid JSON output") from exc
    report = _json_object(report_value, "root")
    return _counts_from_report(root, report)


def _section_counts(value: object, section: str, path: Path) -> tuple[PackageErrorCount, ...]:
    section_value = value
    if not isinstance(section_value, dict):
        raise ValueError(f"typing baseline section {section!r} must be an object: {path}")
    counts: list[PackageErrorCount] = []
    for package, errors in section_value.items():
        if not isinstance(package, str) or not package:
            raise ValueError(f"typing baseline {section!r} package names must be non-empty strings: {path}")
        if isinstance(errors, bool) or not isinstance(errors, int) or errors < 0:
            raise ValueError(f"typing baseline {section!r} count for {package!r} is invalid: {path}")
        counts.append(PackageErrorCount(package, errors))
    return tuple(sorted(counts))


def load_baseline(path: Path) -> TypingCounts:
    """Load and validate a JSON typing baseline."""

    raw: object = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"typing baseline must be a JSON object: {path}")
    if set(raw) != {"basic", "strict"}:
        raise ValueError(f"typing baseline must contain only basic and strict sections: {path}")
    return TypingCounts(
        basic=_section_counts(raw["basic"], "basic", path),
        strict=_section_counts(raw["strict"], "strict", path),
    )


def _as_mapping(counts: Sequence[PackageErrorCount]) -> dict[str, int]:
    return {entry.package: entry.errors for entry in counts}


def compare_baseline(expected: TypingCounts, actual: TypingCounts) -> tuple[str, ...]:
    """Return ratchet violations; all counts must remain exactly reproducible."""

    details: list[str] = []
    for section, expected_entries, actual_entries in (
        ("basic", expected.basic, actual.basic),
        ("strict", expected.strict, actual.strict),
    ):
        expected_counts = _as_mapping(expected_entries)
        actual_counts = _as_mapping(actual_entries)
        for package in sorted(expected_counts.keys() | actual_counts.keys()):
            expected_count = expected_counts.get(package)
            actual_count = actual_counts.get(package)
            if expected_count is None:
                details.append(f"new {section} package {package!r} has no baseline entry")
            elif actual_count is None:
                details.append(f"baseline {section} package {package!r} is no longer present")
            elif actual_count > expected_count:
                details.append(
                    f"{section} package {package!r} increased from {expected_count} to {actual_count} errors"
                )
            elif actual_count < expected_count:
                details.append(
                    f"{section} package {package!r} shrank from {expected_count} to {actual_count} errors"
                )
    for entry in actual.strict:
        if entry.errors:
            details.append(f"strict package {entry.package!r} has {entry.errors} errors")
    return tuple(details)


def render_baseline(counts: TypingCounts) -> str:
    """Render deterministic JSON for a generated typing baseline."""

    payload = {
        "basic": _as_mapping(counts.basic),
        "strict": _as_mapping(counts.strict),
    }
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
    counts = run_pyright(root)
    baseline_path = root / BASELINE_RELATIVE_PATH
    if args.write_baseline:
        baseline_path.write_text(render_baseline(counts), encoding="utf-8")
        print(f"wrote typing baseline to {baseline_path}")
    else:
        print(render_baseline(counts), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
