#!/usr/bin/env python3
"""One-time migration: backfill backlog_title into bridge_llm_execution result files.

Reads source_artifact → next_bounded_candidate.title from each result file and
writes it back as backlog_title so _get_previous_attempts() can match by title.

Usage:
    python3 scripts/migrate_backlog_title.py [--results-dir PATH] [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path


_DEFAULT_STATE_DIR = Path(
    os.environ.get('STATE_DIR', '/var/lib/eeepc-agent/self-evolving-agent/state')
)


def migrate(results_dir: Path, dry_run: bool = False) -> int:
    """Backfill backlog_title into result files. Returns count updated."""
    if not results_dir.exists():
        print(f'results_dir does not exist: {results_dir}')
        return 0

    updated = 0
    skipped_already = 0
    skipped_no_title = 0

    for f in sorted(results_dir.glob('*.json')):
        if not f.is_file():
            continue
        try:
            data = json.loads(f.read_text(encoding='utf-8'))
        except Exception as e:
            print(f'  SKIP {f.name}: read error: {e}')
            continue

        if data.get('materialized_from') != 'bridge_llm_execution':
            continue

        if 'backlog_title' in data:
            skipped_already += 1
            continue

        src = data.get('source_artifact', '')
        if not src or not Path(src).exists():
            skipped_no_title += 1
            continue

        try:
            art = json.loads(Path(src).read_text(encoding='utf-8'))
            title = (art.get('next_bounded_candidate') or {}).get('title', '')
        except Exception:
            skipped_no_title += 1
            continue

        if not title:
            skipped_no_title += 1
            continue

        data['backlog_title'] = title
        if dry_run:
            print(f'  [dry-run] would update {f.name}: backlog_title={title[:60]}')
        else:
            try:
                f.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding='utf-8')
                print(f'  updated {f.name}: backlog_title={title[:60]}')
                updated += 1
            except Exception as e:
                print(f'  FAIL {f.name}: write error: {e}')

    if dry_run:
        print(f'\ndry-run: would update {updated} file(s), '
              f'{skipped_already} already migrated, {skipped_no_title} no title')
    else:
        print(f'\nmigration done: {updated} updated, '
              f'{skipped_already} already had backlog_title, {skipped_no_title} skipped (no title)')
    return updated


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        '--results-dir',
        type=Path,
        default=_DEFAULT_STATE_DIR / 'subagents' / 'results',
        help='Path to state/subagents/results/ directory',
    )
    parser.add_argument('--dry-run', action='store_true', help='Print what would change, no writes')
    args = parser.parse_args()
    migrate(args.results_dir, dry_run=args.dry_run)


if __name__ == '__main__':
    main()
