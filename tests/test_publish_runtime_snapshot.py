from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'publish_runtime_snapshot.py'
GIT = shutil.which('git')


def run_git(*args: str, cwd: Path | None = None, check: bool = True, env=None):
    return subprocess.run([GIT, *args], cwd=cwd, check=check, text=True, capture_output=True, env=env)


def write_snapshot(folder: Path, as_of: str, marker: str | None = None):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / 'dashboard.json').write_text(json.dumps({'asOf': as_of, 'marker': marker or as_of}), encoding='utf-8')
    (folder / 'shadow-account.json').write_text(json.dumps({'asOf': as_of}), encoding='utf-8')


class PublishSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='t1-publish-test-')
        self.root = Path(self.temp.name)
        self.bare = self.root / 'runtime.git'
        self.publisher = self.root / 'publisher'
        self.competitor = self.root / 'competitor'
        run_git('init', '--bare', '--initial-branch=main', str(self.bare))
        run_git('clone', str(self.bare), str(self.publisher), check=False)
        run_git('clone', str(self.bare), str(self.competitor), check=False)
        self._commit(self.publisher, '2026-10-19T02:00:00+00:00', push=True)
        run_git('fetch', 'origin', 'main', cwd=self.competitor)
        run_git('checkout', '-B', 'main', 'origin/main', cwd=self.competitor)

    def tearDown(self):
        self.temp.cleanup()

    def _commit(self, repo: Path, as_of: str, marker: str | None = None, push=False):
        write_snapshot(repo / 'data', as_of, marker)
        run_git('add', 'data', cwd=repo)
        run_git('-c', 'user.name=fixture', '-c', 'user.email=fixture@example.com', 'commit', '-m', as_of, cwd=repo)
        if push:
            run_git('push', 'origin', 'HEAD:refs/heads/main', cwd=repo)

    def _publish(self, export: Path, attempts=4, env=None):
        return subprocess.run(['python3', str(SCRIPT), '--snapshot-dir', str(export), '--attempts', str(attempts)], cwd=self.publisher, text=True, capture_output=True, env=env)

    def _remote_dashboard(self):
        run_git('fetch', 'origin', 'main', cwd=self.publisher)
        return json.loads(run_git('show', 'origin/main:data/dashboard.json', cwd=self.publisher).stdout)

    def test_newer_snapshot_publishes(self):
        export = self.root / 'export'
        write_snapshot(export, '2026-10-19T02:01:00+00:00')
        result = self._publish(export)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self._remote_dashboard()['asOf'], '2026-10-19T02:01:00+00:00')

    def test_equal_and_late_stale_snapshots_are_idempotent(self):
        equal = self.root / 'equal'
        write_snapshot(equal, '2026-10-19T02:00:00+00:00')
        equal_result = self._publish(equal)
        self.assertEqual(equal_result.returncode, 0, equal_result.stderr)
        self.assertIn('already_current_or_newer', equal_result.stdout)
        stale = self.root / 'stale'
        write_snapshot(stale, '2026-10-19T01:59:00+00:00')
        stale_result = self._publish(stale)
        self.assertEqual(stale_result.returncode, 0, stale_result.stderr)
        self.assertEqual(self._remote_dashboard()['asOf'], '2026-10-19T02:00:00+00:00')

    def test_push_race_keeps_competing_newer_snapshot(self):
        export = self.root / 'export'
        write_snapshot(export, '2026-10-19T02:01:00+00:00')
        bindir = self.root / 'bin'
        bindir.mkdir()
        marker = self.root / 'raced'
        wrapper = bindir / 'git'
        wrapper.write_text(f'''#!/bin/sh
if [ "$1" = push ] && [ ! -e "{marker}" ]; then
  touch "{marker}"
  {GIT} -C "{self.competitor}" fetch origin main
  {GIT} -C "{self.competitor}" checkout -B main origin/main
  python3 -c 'import json,pathlib; p=pathlib.Path("{self.competitor}/data/dashboard.json"); p.write_text(json.dumps({{"asOf":"2026-10-19T02:02:00+00:00","marker":"racer"}}))'
  python3 -c 'import json,pathlib; p=pathlib.Path("{self.competitor}/data/shadow-account.json"); p.write_text(json.dumps({{"asOf":"2026-10-19T02:02:00+00:00"}}))'
  {GIT} -C "{self.competitor}" add data
  {GIT} -C "{self.competitor}" -c user.name=fixture -c user.email=fixture@example.com commit -m racer
  {GIT} -C "{self.competitor}" push origin HEAD:refs/heads/main
fi
exec {GIT} "$@"
''', encoding='utf-8')
        wrapper.chmod(0o755)
        env = os.environ.copy()
        env['PATH'] = str(bindir) + os.pathsep + env['PATH']
        result = self._publish(export, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('already_current_or_newer', result.stdout)
        self.assertEqual(self._remote_dashboard()['asOf'], '2026-10-19T02:02:00+00:00')
        self.assertEqual(self._remote_dashboard()['marker'], 'racer')


if __name__ == '__main__':
    unittest.main()
