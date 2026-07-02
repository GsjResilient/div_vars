#!/usr/bin/env python3
"""Generate a standalone AIG visualization from raw AIG variable IDs."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from partition_aig_cutpoints import (
    AigParseError,
    build_key_visualization_data,
    parse_aig,
    write_visualization,
)


class KeyVariableFileError(ValueError):
    pass


def read_key_variables(path: Path, maxvar: int) -> list[int]:
    variables: set[int] = set()
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except UnicodeDecodeError as exc:
        raise KeyVariableFileError(f"{path}: key-variable file must be UTF-8 text") from exc

    for line_number, original_line in enumerate(lines, start=1):
        content = original_line.split("#", 1)[0]
        for token in content.split():
            try:
                variable = int(token, 10)
            except ValueError as exc:
                raise KeyVariableFileError(
                    f"{path}:{line_number}: invalid raw AIG variable ID: {token!r}"
                ) from exc
            if variable < 1 or variable > maxvar:
                raise KeyVariableFileError(
                    f"{path}:{line_number}: raw AIG variable ID {variable} is outside 1..{maxvar}"
                )
            variables.add(variable)

    return sorted(variables)


def default_output_path(aig_path: Path, key_path: Path, root: Path) -> Path:
    filename = f"{aig_path.stem}.{key_path.stem}.visualization.html"
    return root / "aig_visualization_out" / aig_path.stem / filename


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("aig", help="input binary AIGER .aig file")
    parser.add_argument("key_variables", help="UTF-8 file containing raw AIG variable IDs")
    parser.add_argument("output_html", nargs="?", help="optional output HTML path")
    args = parser.parse_args(argv)

    aig_path = Path(args.aig).expanduser().resolve()
    key_path = Path(args.key_variables).expanduser().resolve()
    if not aig_path.is_file():
        print(f"error: AIG input does not exist: {aig_path}", file=sys.stderr)
        return 2
    if not key_path.is_file():
        print(f"error: key-variable file does not exist: {key_path}", file=sys.stderr)
        return 2

    try:
        aig = parse_aig(aig_path)
        key_variables = read_key_variables(key_path, aig["maxvar"])
    except (AigParseError, KeyVariableFileError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.output_html:
        output_path = Path(args.output_html).expanduser().resolve()
    else:
        output_path = default_output_path(aig_path, key_path, Path.cwd().resolve())

    data = build_key_visualization_data(aig, key_variables, key_path)
    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        write_visualization(output_path, data)
    except OSError as exc:
        print(f"error: failed to write visualization: {exc}", file=sys.stderr)
        return 2

    print(f"aig: {aig_path}")
    print(f"key_variable_file: {key_path}")
    print(f"key_variable_count: {len(key_variables)}")
    print("key_variables: " + " ".join(str(variable) for variable in key_variables))
    print(f"visualization_file: {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
