"""Reconcile an existing one-shot batch without importing or touching media."""
import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import time


def absent(path):
    try:
        path.stat()
    except FileNotFoundError:
        return True
    return False


def reconcile(database, root, apply=False):
    root = Path(root).resolve(strict=True)
    if not root.is_dir():
        raise ValueError('Library root is unavailable')
    # Force a directory read: permission/I/O failures must abort, not mean deleted.
    next(root.iterdir(), None)
    db = sqlite3.connect(Path(database).resolve(strict=True), timeout=10)
    db.row_factory = sqlite3.Row
    try:
        backup = None
        if apply:
            backup = str(Path(database).with_name('queue.before-reconcile-' + str(time.time_ns()) + '.sqlite3'))
            with closing(sqlite3.connect(backup)) as target:
                db.backup(target)
        db.execute('BEGIN IMMEDIATE' if apply else 'BEGIN')
        batch = db.execute('SELECT root FROM library_batch WHERE id=1').fetchone()
        if not batch or Path(batch['root']).resolve() != root:
            raise ValueError('Mounted library root differs from the imported batch')
        rows = db.execute("SELECT jobs.*,destination,retry_requested FROM jobs JOIN library_files USING(id) "
                          "WHERE status IN ('queued','failed','stopped')").fetchall()
        missing, retained = [], 0
        archived = bool(db.execute("SELECT 1 FROM sqlite_master WHERE name='deleted_jobs'").fetchone())
        for row in rows:
            if row['retry_requested'] or (archived and db.execute('SELECT 1 FROM deleted_jobs WHERE id=?', (row['id'],)).fetchone()):
                continue
            source = Path(row['source'])
            source.relative_to(root)
            if any(part.is_symlink() for part in [source, *source.parents] if part != root):
                raise ValueError('Symlink found in batch source')
            if not absent(source):
                retained += 1
                continue
            candidates = {source.with_suffix(suffix) for suffix in ('.mp4', '.mkv')}
            candidates.update(source.with_name(source.stem + '.H265-' + row['id'][:8] + suffix) for suffix in ('.mp4', '.mkv'))
            candidates.update(Path(row[key]) for key in ('output', 'destination') if row[key])
            for path in candidates:
                path.relative_to(root)
            if any(not absent(path) for path in candidates):
                retained += 1
                continue
            missing.append({'id': row['id'], 'source': str(source), 'status': row['status'], 'size': row['size']})
        if apply and missing:
            db.execute('CREATE TABLE IF NOT EXISTS deleted_jobs(id TEXT PRIMARY KEY,deleted_at REAL NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS library_missing(id TEXT PRIMARY KEY,previous_status TEXT,removed_at REAL NOT NULL)')
            now = time.time()
            for row in missing:
                db.execute('INSERT INTO library_missing VALUES(?,?,?)', (row['id'],row['status'],now))
                db.execute("UPDATE jobs SET status='stopped',error='Source and output removed by user; excluded during queue reconciliation',updated=? WHERE id=?", (now,row['id']))
                db.execute('INSERT INTO deleted_jobs VALUES(?,?)', (row['id'],now))
                db.execute('DELETE FROM queue_order WHERE job_id=?', (row['id'],))
            db.execute('UPDATE queue_control SET order_revision=order_revision+1 WHERE id=1')
        db.commit()
        return {'applied':apply, 'backup':backup, 'missing_count':len(missing),'missing_bytes':sum(row['size'] for row in missing),
                'retained_candidates':retained,'missing':missing}
    finally:
        db.close()


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', type=Path, required=True)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--report', type=Path)
    args=parser.parse_args()
    result=reconcile(args.database,args.root,args.apply)
    if args.report:
        args.report.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({key:value for key,value in result.items() if key!='missing'},ensure_ascii=False))
