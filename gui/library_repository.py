"""Console access to the isolated, low-priority historical-video queue."""
from pathlib import Path
import re
import time

from repository import QueueRepository
from library_policy import retention_details
from services import DashboardService
from shared import AppError, Paths


def reconcile_progress_phases(control, progress_by_job, *, now=None):
    """Correct a stale control phase from the worker's live progress file."""
    if not control or control.get('mode') != 'running':
        return control
    now = time.time() if now is None else now
    encoding = []
    verifying = []
    for worker in control.get('workers', []):
        progress = progress_by_job.get(worker.get('job_id'))
        if not progress or progress.get('kind') == 'verification' or worker.get('phase') not in (
                'preparing', 'priority_wait', 'waiting_priority', 'waiting_encoder'):
            continue
        updated = progress.get('updated')
        if (isinstance(updated, (int, float)) and now - updated <= 6 and
                (progress.get('speed') or 0) > 0):
            worker['phase'] = 'encoding'
            encoding.append(worker['job_id'])
        elif progress.get('percent') is not None and progress['percent'] >= 99 and worker.get('role') == 'priority':
            worker['phase'] = 'verifying'
            verifying.append(worker['job_id'])
    if encoding:
        control.update(phase='encoding', state='encoding', current_job_id=encoding[0], suspended_job_id=None)
    elif verifying:
        control.update(phase='verifying', state='verifying', current_job_id=verifying[0], suspended_job_id=None)
    return control


class LibraryRepository(QueueRepository):
    def __init__(self, paths):
        config = paths.config / 'library'
        super().__init__(Paths(config, Path('/app/library'), config / 'state'))

    def snapshot(self, status='all', search='', page=1, sort=None, direction=None):
        if not self.paths.queue.exists():
            return {'enabled': False}
        with self.connect() as db:
            batch = db.execute('SELECT * FROM library_batch WHERE id=1').fetchone()
            if not batch:
                return {'enabled': False}
            label, sidecars = batch['label'], batch['sidecars']
            hevc = db.execute('SELECT count(*) FROM library_files WHERE already_hevc=1').fetchone()[0]
            retained = sum(retention_details(row[0])['retained_original'] for row in
                           db.execute("SELECT verification FROM library_files JOIN jobs USING(id) WHERE status='done'"))
        data = self.jobs(status, search, page, sort=sort, direction=direction)
        identifiers = [item['id'] for item in data['items']]
        with self.connect() as db:
            details = {r['id']: dict(r) for r in db.execute('SELECT * FROM library_files WHERE id IN (' +
                       ','.join('?' for _ in identifiers) + ')', identifiers)} if identifiers else {}
        for item in data['items']:
            item['already_hevc'] = bool(details.get(item['id'], {}).get('already_hevc'))
            item['retry_requested'] = bool(details.get(item['id'], {}).get('retry_requested'))
            item.update(retention_details(details.get(item['id'], {}).get('verification')))
        dashboard = DashboardService(self.paths, None, None)
        progress_by_job = dashboard.progress_by_job(data['control'])
        reconcile_progress_phases(data['control'], progress_by_job)
        data.update(enabled=True, label=label, sidecars_retained=sidecars, already_hevc=hevc,
                    retained_original=retained,
                    summary=self.summary(), progress=dashboard.progress(data['control']),
                    suspended_progress=dashboard.progress(data['control'], suspended=True),
                    progress_by_job=progress_by_job,
                    log='library/h265/postprocess.log')
        return data

    def retry(self, identifier):
        if not isinstance(identifier, str) or not re.fullmatch(r'[0-9a-f]{64}', identifier):
            raise AppError('任务编号不正确。')
        with self.connect(write=True) as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT status,verification FROM jobs JOIN library_files USING(id) WHERE id=? AND ' +
                             self.visible_jobs(db), (identifier,)).fetchone()
            if not row:
                raise AppError('找不到这个任务。', 'not_found', 404)
            if row['status'] not in ('failed', 'stopped') and not (
                    row['status'] == 'done' and retention_details(row['verification'])['retained_original']):
                raise AppError('只能重试失败、已停止或未节省空间的任务。', 'conflict', 409)
            db.execute('UPDATE library_files SET retry_requested=1 WHERE id=?', (identifier,))

    def delete_record(self, identifier):
        return self.delete_records([identifier])

    def delete_records(self, identifiers):
        if not isinstance(identifiers, list) or not 1 <= len(identifiers) <= 200 or any(
                not isinstance(value, str) or not re.fullmatch(r'[0-9a-f]{64}', value) for value in identifiers):
            raise AppError('请选择 1 至 200 条有效的任务记录。')
        identifiers = list(dict.fromkeys(identifiers))
        with self.connect(write=True) as db:
            db.execute('BEGIN IMMEDIATE')
            placeholders = ','.join('?' for _ in identifiers)
            rows = db.execute('SELECT status,retry_requested FROM jobs JOIN library_files USING(id) WHERE id IN (' +
                              placeholders + ')', identifiers).fetchall()
            if len(rows) != len(identifiers):
                raise AppError('找不到这个任务。', 'not_found', 404)
            if any(row['retry_requested'] or row['status'] not in ('done','failed','stopped') for row in rows):
                raise AppError('任务正在重试、排队或处理，请等待结束后再删除记录。', 'conflict', 409)
            self.migrate_deleted_jobs(db)
            deleted_at = time.time()
            db.executemany('INSERT OR IGNORE INTO deleted_jobs(id,deleted_at) VALUES(?,?)',
                           [(identifier, deleted_at) for identifier in identifiers])
        return len(identifiers)
