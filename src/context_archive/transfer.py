"""Atomic portable exports and consistent SQLite backups, without source writes."""
from __future__ import annotations
import base64
from contextlib import closing, contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import tempfile
from . import core

# FTS shadow tables are implementation details. Everything needed to rebuild the
# index, including historical projections and exact imported lines, is exported.
TABLES=('archive_info','sources','source_paths','captures','records','changes','current_rows','blobs','events','attempts','imports','import_lines')


def encode_cell(value):
    if isinstance(value,bytes):return {'$bytes_base64':base64.b64encode(value).decode('ascii')}
    if isinstance(value,int) and abs(value)>2**53-1:return {'$integer':str(value)}
    if isinstance(value,float) and not math.isfinite(value):return {'$float':value.hex()}
    return value


def protected_paths(archive):
    with closing(core.readonly(archive)) as c:
        return [archive]+[r[0] for r in c.execute('SELECT path FROM source_paths')]+[json.loads(r[0])['path'] for r in c.execute('SELECT details_json FROM imports')]


@contextmanager
def atomic_file(destination, binary=False):
    destination=Path(destination).expanduser().resolve()
    destination.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    fd,name=tempfile.mkstemp(prefix='.'+destination.name+'.',dir=destination.parent)
    try:
        with os.fdopen(fd,'wb' if binary else 'w',**({} if binary else {'encoding':'utf-8','newline':'\n'})) as f:
            yield f
            f.flush();os.fsync(f.fileno())
        os.replace(name,destination)
        directory=os.open(destination.parent,os.O_RDONLY)
        try:os.fsync(directory)
        finally:os.close(directory)
    finally:
        if os.path.exists(name):os.unlink(name)


def export_archive(archive,destination):
    core.protect(destination,protected_paths(archive))
    with closing(core.readonly(archive)) as c,atomic_file(destination) as f:
        c.execute('BEGIN')
        info=dict(c.execute('SELECT * FROM archive_info').fetchone())
        maximum=c.execute('SELECT coalesce(max(id),0) FROM captures').fetchone()[0]
        count=0;checksum=hashlib.sha256()
        def write(value, hashed=True):
            nonlocal count
            line=core.canonical(value)+'\n';f.write(line)
            if hashed:checksum.update(line.encode('utf-8'))
            count+=1
        write({'type':'manifest','format':'context-archive','format_version':1,'archive_id':info['archive_id'],
               'through_capture':maximum,'schema':[dict(r) for r in c.execute("SELECT name,sql FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'event_search%' ORDER BY name")],
               'note':'Original rows use payload_json with lossless cell tags; exported SQLite BLOB cells use $bytes_base64. FTS is rebuildable.'})
        for table in (*TABLES, *(['projects'] if c.execute("SELECT 1 FROM sqlite_schema WHERE name='projects' AND type='table'").fetchone() else [])):
            for row in c.execute('SELECT * FROM '+core.quote(table)+' ORDER BY rowid'):
                write({'type':table,'record':{k:encode_cell(v) for k,v in dict(row).items()}})
        write({'type':'end','preceding_lines':count,'sha256':checksum.hexdigest(),'through_capture':maximum},False)
    return {'destination':str(Path(destination).resolve()),'lines':count,'through_capture':maximum}


def backup(archive,destination):
    core.protect(destination,protected_paths(archive))
    destination=Path(destination).expanduser().resolve()
    destination.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    # Never overwrite an open SQLite database: its old sidecars could corrupt the
    # new backup. Require a fresh destination and let users rotate backups explicitly.
    if destination.exists() or any(Path(str(destination)+s).exists() for s in ('-wal','-shm','-journal')):
        raise ValueError('Backup destination must be new and have no SQLite sidecars')
    with core.snapshot(archive) as snap:
        with atomic_file(destination,binary=True) as f:
            # SQLite backup's validated temporary database is consistent even if
            # another capture commits while we copy its file bytes.
            path=Path(snap.execute('PRAGMA database_list').fetchone()[2])
            with path.open('rb') as src:
                while chunk:=src.read(1024*1024):f.write(chunk)
    return {'destination':str(destination)}
