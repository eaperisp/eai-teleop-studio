#!/usr/bin/env python3
"""Create a tar.gz package for one local LeRobot dataset."""

from __future__ import annotations

import argparse
import tarfile
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lerobot-home", help="LeRobot root directory, e.g. /data03/data/datasets/lerobot")
    parser.add_argument("--repo-id", help="LeRobot repo id, e.g. local/task_name")
    parser.add_argument("--source-dir", help="Generic source directory to package, e.g. an OpenPI assets norm_stats dir")
    parser.add_argument("--arcname", help="Archive root name for --source-dir. Defaults to source directory name")
    parser.add_argument("--output", required=True, help="Output .tar.gz path")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.source_dir:
        lerobot_home = None
        repo_id = str(args.arcname or Path(args.source_dir).name).strip().strip("/")
        source = Path(args.source_dir).expanduser().resolve()
        root_arcname = Path(repo_id)
    else:
        if not args.lerobot_home or not args.repo_id:
            raise SystemExit("--lerobot-home and --repo-id are required unless --source-dir is used")
        lerobot_home = Path(args.lerobot_home).expanduser().resolve()
        repo_id = str(args.repo_id).strip().strip("/")
        source = (lerobot_home / repo_id).resolve()
        try:
            source.relative_to(lerobot_home)
        except ValueError as exc:
            raise SystemExit(f"Dataset path escapes lerobot home: {source}") from exc
        root_arcname = source.relative_to(lerobot_home)
    output = Path(args.output).expanduser().resolve()

    if not source.is_dir():
        raise SystemExit(f"Source directory not found: {source}")

    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        raise SystemExit(f"Output already exists: {output}")

    files = [path for path in source.rglob("*") if path.is_file()]
    total = len(files)
    print(f"Packaging directory: {repo_id}")
    print(f"Source: {source}")
    print(f"Output: {output}")
    print(f"Files: {total}")

    with tarfile.open(output, "w:gz") as archive:
        archive.add(source, arcname=str(root_arcname), recursive=False)
        for index, path in enumerate(files, start=1):
            archive.add(path, arcname=str(root_arcname / path.relative_to(source)))
            if index == 1 or index == total or index % 500 == 0:
                percent = round(index / total * 100) if total else 100
                print(f"Packaging files: {percent}% | {index}/{total}", flush=True)

    print(f"Package complete: {output}")
    print(f"Size bytes: {output.stat().st_size}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
