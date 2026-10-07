"""Best-effort recovery of locked Chromium history; source files are never written.

Adapted from browser-history-receiver's copy_locked_db approach. A stable file
set and a successful integrity check reduce risk, but do not establish an atomic
snapshot. Consumers must retain this distinction and never infer deletions.
"""
from contextlib import ExitStack, contextmanager
from pathlib import Path
import os
import sqlite3
import tempfile
import time


class SourceChangedError(OSError):
    pass


def manifest(path):
    result = {}
    # SHM is a disposable WAL index. SQLite rebuilds it in the private copy.
    for suffix in ('', '-wal', '-journal'):
        p = Path(str(path) + suffix)
        try:
            s = p.stat()
            result[suffix] = (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
        except FileNotFoundError:
            if not suffix: raise
    return result


def copy_file(source, destination, deadline):
    with source.open('rb') as src, destination.open('xb') as dst:
        os.chmod(destination, 0o600)
        while True:
            if time.monotonic() >= deadline: raise TimeoutError('Copy deadline exceeded')
            chunk = src.read(1024 * 1024)
            if not chunk: break
            dst.write(chunk)


@contextmanager
def copied_snapshot(path, timeout=30, retries=3):
    path = Path(path)
    deadline = time.monotonic() + timeout
    with ExitStack() as stack:
        last_error = None
        for attempt in range(retries):
            directory = stack.enter_context(tempfile.TemporaryDirectory(prefix='context-copy-'))
            dest = Path(directory) / 'snapshot.sqlite3'
            try:
                before = manifest(path)
                for suffix in before:
                    copy_file(Path(str(path) + suffix), Path(str(dest) + suffix), deadline)
                if manifest(path) != before:
                    raise SourceChangedError('Source files changed while copying')
                # Writable ONLY in the private temporary copy, so SQLite can
                # recover a hot rollback journal or rebuild the WAL index.
                snap = sqlite3.connect(dest, timeout=1)
                try:
                    snap.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
                    if snap.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
                        raise ValueError('Copied database failed integrity check')
                    snap.execute('PRAGMA query_only=ON')
                except BaseException:
                    snap.close()
                    raise
                stack.callback(snap.close)
                break
            except (SourceChangedError, FileNotFoundError) as exc:
                last_error = exc
                if time.monotonic() >= deadline: raise TimeoutError('Copy deadline exceeded') from exc
        else:
            raise SourceChangedError('Source did not stabilize during bounded copy retries') from last_error
        from .core import text_factory
        snap.row_factory = sqlite3.Row
        snap.text_factory = text_factory
        yield snap, {'method': 'file_copy', 'consistency': 'best_effort',
                     'integrity_check': 'ok', 'file_set_unchanged': True,
                     'copied_suffixes': sorted(before), 'attempts': attempt + 1,
                     'deletions_inferred': False}


def is_locked(exc):
    if isinstance(exc, sqlite3.Error):
        return getattr(exc, 'sqlite_errorcode', None) in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)
    return isinstance(exc, TimeoutError) and 'database locked' in str(exc)


@contextmanager
def acquire(source):
    from . import core
    with ExitStack() as stack:
        try:
            # Only acquisition failures trigger fallback, never exceptions from
            # archive writing or normalization after this context has yielded.
            snap = stack.enter_context(core.snapshot(source.path, timeout=5) if source.adapter == 'chromium'
                                       else core.snapshot(source.path))
        except (sqlite3.Error, TimeoutError) as exc:
            if source.adapter != 'chromium' or not is_locked(exc): raise
            snap, metadata = stack.enter_context(copied_snapshot(source.path))
        else:
            metadata = {'method': 'sqlite_backup', 'consistency': 'consistent'}
        yield snap, metadata
