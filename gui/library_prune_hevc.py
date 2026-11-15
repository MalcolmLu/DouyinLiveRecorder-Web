"""Exclude already-HEVC sources from an existing batch; never modify media."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import argparse
import json
import math
import os
from pathlib import Path
import sqlite3
import subprocess
import time


def inspect(root, row):
    result = {'id': row['id'], 'source': row['source'], 'status': row['status']}
    try:
        path = Path(row['source'])
        path.relative_to(root)
        if any(part.is_symlink() for part in [path, *path.parents] if part != root):
            raise ValueError('Source symlink; skipped')
        before = path.stat()
        if not path.is_file() or (before.st_size, before.st_mtime_ns) != (row['size'], row['mtime']):
            raise ValueError('Source changed; skipped')
        probe = subprocess.run(['ffprobe', '-v', 'error', '-show_entries',
                                'format=duration:stream=codec_type,codec_name,width,height:stream_disposition=attached_pic',
                                '-of', 'json', str(path)], stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
        if probe.returncode:
            raise ValueError('FFprobe failed: ' + probe.stderr.decode(errors='replace')[-600:])
        info = json.loads(probe.stdout)
        videos = [stream for stream in info.get('streams', []) if stream.get('codec_type') == 'video']
        if len(videos) != 1 or videos[0].get('disposition', {}).get('attached_pic'):
            raise ValueError('Expected one video stream; skipped')
        duration = float(info['format']['duration'])
        if not math.isfinite(duration) or duration <= 0 or not videos[0].get('width') or not videos[0].get('height'):
            raise ValueError('Invalid media dimensions or duration; skipped')
        after = path.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise ValueError('Source changed during probe; skipped')
        result.update(codec=videos[0]['codec_name'], size=before.st_size, mtime=before.st_mtime_ns)
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        result['error'] = str(exc)
    return result


def prune(database, root, apply=False, progress=None):
    root = Path(root).resolve(strict=True)
    next(root.iterdir(), None)
    db = sqlite3.connect(Path(database).resolve(strict=True), timeout=30)
    db.row_factory = sqlite3.Row
    try:
        batch = db.execute('SELECT root FROM library_batch WHERE id=1').fetchone()
        if not batch or Path(batch['root']).resolve() != root:
            raise ValueError('Library root differs from imported batch')
        hidden = bool(db.execute("SELECT 1 FROM sqlite_master WHERE name='deleted_jobs'").fetchone())
        visible = ' AND NOT EXISTS (SELECT 1 FROM deleted_jobs WHERE deleted_jobs.id=jobs.id)' if hidden else ''
        rows = [dict(row) for row in db.execute("SELECT jobs.*,already_hevc,retry_requested FROM jobs JOIN library_files USING(id) "
                      "WHERE (status='queued' OR (status='done' AND already_hevc=1)) AND retry_requested=0" + visible)]
        inspected = []
        with ThreadPoolExecutor(max_workers=2) as pool:
            for item in pool.map(lambda row: inspect(root, row), rows):
                inspected.append(item)
                if progress and (len(inspected) % 25 == 0 or len(inspected) == len(rows)):
                    progress({'checked': len(inspected), 'total': len(rows),
                              'hevc': sum(item.get('codec') == 'hevc' for item in inspected),
                              'errors': sum('error' in item for item in inspected)})
        excluded, changed, backup = [], [], None
        if apply:
            backup = str(Path(database).with_name('queue.before-hevc-prune-' + str(time.time_ns()) + '.sqlite3'))
            with closing(sqlite3.connect(backup)) as target:
                db.backup(target)
            db.execute('BEGIN IMMEDIATE')
            db.execute('CREATE TABLE IF NOT EXISTS deleted_jobs(id TEXT PRIMARY KEY,deleted_at REAL NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS library_exclusions(id TEXT PRIMARY KEY,reason TEXT,previous_status TEXT,excluded_at REAL NOT NULL)')
            for item in inspected:
                if item.get('codec') != 'hevc':
                    continue
                row = db.execute('SELECT jobs.*,retry_requested FROM jobs JOIN library_files USING(id) WHERE id=?', (item['id'],)).fetchone()
                if (not row or row['status'] not in ('queued', 'done') or row['retry_requested'] or
                        db.execute('SELECT 1 FROM deleted_jobs WHERE id=?', (item['id'],)).fetchone()):
                    changed.append(item['id']); continue
                try:
                    stat = Path(item['source']).stat()
                    if (stat.st_size, stat.st_mtime_ns) != (item['size'], item['mtime']):
                        changed.append(item['id']); continue
                except OSError:
                    changed.append(item['id']); continue
                now = time.time()
                db.execute('INSERT OR REPLACE INTO library_exclusions VALUES(?,?,?,?)', (item['id'], 'already_hevc', row['status'], now))
                db.execute("UPDATE jobs SET status='done',output=source,output_size=size,output_mtime=mtime,error=NULL,updated=? WHERE id=?", (now, item['id']))
                db.execute('UPDATE library_files SET already_hevc=1 WHERE id=?', (item['id'],))
                db.execute('INSERT INTO deleted_jobs VALUES(?,?)', (item['id'], now))
                db.execute('DELETE FROM queue_order WHERE job_id=?', (item['id'],))
                excluded.append(item['id'])
            if excluded:
                db.execute('UPDATE queue_control SET order_revision=order_revision+1 WHERE id=1')
            db.commit()
        return {'applied': apply, 'checked': len(inspected), 'hevc': sum(item.get('codec') == 'hevc' for item in inspected),
                'excluded': len(excluded), 'changed_or_active': changed, 'backup': backup, 'items': inspected}
    finally:
        db.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', type=Path, required=True)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    if hasattr(os, 'nice'):
        os.nice(15)
    result = prune(args.database, args.root, args.apply, lambda value: print(json.dumps(value), flush=True))
    args.report.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({key: value for key, value in result.items() if key != 'items'}), flush=True)
