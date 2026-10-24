"""Read only the fixed Harness task's public stop/status fields; never its prompt."""
from __future__ import annotations

from contextlib import closing
import os
from pathlib import Path
import sqlite3
import stat


class GuardError(RuntimeError):
    pass


class HarnessTaskGuard:
    def __init__(self, state_dir, task_id=None):
        self.database = Path(os.path.abspath(os.fspath(Path(state_dir).expanduser()))) / 'harness.sqlite3'
        self.task_id = task_id

    def _read(self, sql, args=()):
        try:
            current = Path(self.database.anchor)
            for part in self.database.parts[1:]:
                current /= part
                info = current.lstat()
                if stat.S_ISLNK(info.st_mode):
                    raise GuardError('harness_guard_unsafe_path')
            for path in (self.database.parent, self.database, Path(str(self.database) + '-wal'), Path(str(self.database) + '-shm')):
                try:
                    info = path.lstat()
                except FileNotFoundError:
                    if path not in (self.database.parent, self.database):
                        continue
                    raise
                if stat.S_ISLNK(info.st_mode) or (path != self.database.parent and not stat.S_ISREG(info.st_mode)):
                    raise GuardError('harness_guard_unsafe_path')
                if hasattr(os, 'getuid') and info.st_uid != os.getuid():
                    raise GuardError('harness_guard_wrong_owner')
            if not stat.S_ISREG(self.database.lstat().st_mode):
                raise GuardError('harness_guard_unsafe_path')
            with closing(sqlite3.connect(self.database.as_uri() + '?mode=ro', uri=True, timeout=.1)) as db:
                db.execute('PRAGMA query_only=ON')
                return db.execute(sql, args).fetchall()
        except (OSError, sqlite3.Error, ValueError):
            raise GuardError('harness_guard_unavailable') from None

    @classmethod
    def bind(cls, state_dir):
        guard = cls(state_dir)
        rows = guard._read("SELECT id FROM tasks WHERE status IN ('queued','running') LIMIT 2")
        if len(rows) != 1 or not isinstance(rows[0][0], str) or not rows[0][0]:
            raise GuardError('harness_task_not_unique')
        guard.task_id = rows[0][0]
        return guard

    def __call__(self):
        try:
            rows = self._read('SELECT status,stop_requested FROM tasks WHERE id=?', (self.task_id,))
        except GuardError as exc:
            return str(exc)
        if len(rows) != 1:
            return 'harness_task_missing'
        if rows[0][1] != 0:
            return 'harness_task_stopped'
        return '' if rows[0][0] == 'running' else 'harness_task_inactive'
