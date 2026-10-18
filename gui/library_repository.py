"""Console access to the isolated, low-priority historical-video queue."""
from pathlib import Path
import re
import time

from repository import QueueRepository
from services import DashboardService
from shared import AppError, Paths


class LibraryRepository(QueueRepository):
    def __init__(self, paths):
        config = paths.config / 'library'
        super().__init__(Paths(config, Path('/app/library'), config / 'state'))

    def snapshot(self, status='all', search='', page=1):
        if not self.paths.queue.exists():
            return {'enabled': False}
        with self.connect() as db:
            batch = db.execute('SELECT * FROM library_batch WHERE id=1').fetchone()
            if not batch:
                return {'enabled': False}
            label, sidecars = batch['label'], batch['sidecars']
            hevc = db.execute('SELECT count(*) FROM library_files WHERE already_hevc=1').fetchone()[0]
        data = self.jobs(status, search, page)
        identifiers = [item['id'] for item in data['items']]
        with self.connect() as db:
            details = {r['id']: dict(r) for r in db.execute('SELECT * FROM library_files WHERE id IN (' +
                       ','.join('?' for _ in identifiers) + ')', identifiers)} if identifiers else {}
        for item in data['items']:
            item['already_hevc'] = bool(details.get(item['id'], {}).get('already_hevc'))
            item['retry_requested'] = bool(details.get(item['id'], {}).get('retry_requested'))
        data.update(enabled=True, label=label, sidecars_retained=sidecars, already_hevc=hevc,
                    summary=self.summary(), progress=DashboardService(self.paths, None, None).progress(),
                    log='library/h265/postprocess.log')
        return data

    def retry(self, identifier):
        if not isinstance(identifier, str) or not re.fullmatch(r'[0-9a-f]{64}', identifier):
            raise AppError('任务编号不正确。')
        with self.connect(write=True) as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT status FROM jobs WHERE id=? AND ' + self.visible_jobs(db), (identifier,)).fetchone()
            if not row:
                raise AppError('找不到这个任务。', 'not_found', 404)
            if row['status'] not in ('failed', 'stopped'):
                raise AppError('只能重试失败或已停止的任务。', 'conflict', 409)
            db.execute('UPDATE library_files SET retry_requested=1 WHERE id=?', (identifier,))

    def delete_record(self, identifier):
        if not isinstance(identifier, str) or not re.fullmatch(r'[0-9a-f]{64}', identifier):
            raise AppError('任务编号不正确。')
        with self.connect(write=True) as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT status,retry_requested FROM jobs JOIN library_files USING(id) WHERE id=?',
                             (identifier,)).fetchone()
            if not row:
                raise AppError('找不到这个任务。', 'not_found', 404)
            if row['retry_requested'] or row['status'] not in ('done','failed','stopped'):
                raise AppError('任务正在重试、排队或处理，请等待结束后再删除记录。', 'conflict', 409)
            self.migrate_deleted_jobs(db)
            db.execute('INSERT OR IGNORE INTO deleted_jobs(id,deleted_at) VALUES(?,?)', (identifier, time.time()))

