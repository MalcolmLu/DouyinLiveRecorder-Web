"""Library import, isolation, fail-closed priority and authenticated API checks."""
from contextlib import closing
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest

import library_worker as library
from library_repository import LibraryRepository, reconcile_progress_phases
import queue_control
import test_console


class LibraryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='library-unit-')
        self.root = Path(self.temp.name).resolve()
        self.config, self.videos = self.root / 'config', self.root / 'videos'
        self.config.mkdir(); self.videos.mkdir()
        self.p = library.processor(self.config, self.videos)

    def tearDown(self):
        self.temp.cleanup()

    def test_progress_phase_reconciles_encoding_and_verifying_workers(self):
        encoding, verifying = 'a' * 64, 'b' * 64
        control = {
            'mode': 'running', 'phase': 'waiting_priority', 'state': 'waiting_priority',
            'current_job_id': verifying, 'suspended_job_id': encoding,
            'workers': [
                {'role': 'primary', 'job_id': encoding, 'phase': 'priority_wait'},
                {'role': 'priority', 'job_id': verifying, 'phase': 'waiting_priority'},
            ],
        }
        progress = {
            encoding: {'updated': 100, 'speed': 0.6, 'percent': 92.9},
            verifying: {'updated': 20, 'speed': 1.2, 'percent': 99.9},
        }
        reconciled = reconcile_progress_phases(control, progress, now=104)
        self.assertEqual(reconciled['phase'], 'encoding')
        self.assertEqual(reconciled['current_job_id'], encoding)
        self.assertIsNone(reconciled['suspended_job_id'])
        self.assertEqual([worker['phase'] for worker in reconciled['workers']], ['encoding', 'verifying'])

    def test_import_is_one_shot_and_excludes_sidecars_symlinks_and_temporary_files(self):
        (self.videos/'one.mp4').write_bytes(b'one')
        (self.videos/'danmaku.xml').write_bytes(b'keep')
        (self.videos/'.partial.mp4').write_bytes(b'keep')
        outside = self.root/'outside.mp4'; outside.write_bytes(b'outside')
        if os.name != 'nt':
            (self.videos/'escape.mp4').symlink_to(outside)
        folder = self.videos/'nested';folder.mkdir();(folder/'two.flv').write_bytes(b'two')
        result = library.seed(self.p)
        self.assertEqual(result, {'videos':2,'bytes':6,'sidecars_retained':1})
        with self.p.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM jobs').fetchone()[0], 2)
        (self.videos/'later.mp4').write_bytes(b'later')
        with self.assertRaisesRegex(ValueError, 'already imported'):
            library.seed(self.p)
        self.assertEqual(outside.read_bytes(), b'outside')

    @unittest.skipIf(os.name == 'nt', 'Creating test symlinks requires Windows developer privileges')
    def test_containment_rejects_parent_symlinks_and_paths_outside_requested_folder(self):
        outside = self.root/'external';outside.mkdir();(outside/'x.mp4').write_bytes(b'keep')
        (self.videos/'link').symlink_to(outside, target_is_directory=True)
        for path in [outside/'x.mp4', self.videos/'link/x.mp4']:
            with self.assertRaises(ValueError):library.safe_path(self.videos,path)

    def test_output_selection_preserves_tracks_and_does_not_overwrite_existing_video(self):
        source=self.videos/'recording.flv';source.write_bytes(b'input')
        other=source.with_suffix('.mp4');other.write_bytes(b'unrelated')
        simple={'streams':[{'codec_type':'video'},{'codec_type':'audio','codec_name':'aac'}]}
        destination=library.destination_for(source,simple,'a'*64)
        self.assertEqual(destination.name,'recording.H265-aaaaaaaa.mp4')
        extra={'streams':simple['streams']+[{'codec_type':'subtitle','codec_name':'ass'}]}
        self.assertEqual(library.destination_for(source,extra,'a'*64).suffix,'.mkv')
        destination.write_bytes(b'also keep')
        with self.assertRaises(FileExistsError):library.destination_for(source,simple,'a'*64)
        self.assertEqual(other.read_bytes(),b'unrelated')

    def test_reconcile_removes_only_missing_inactive_sources_and_keeps_fingerprints(self):
        from library_reconcile import reconcile
        for name in ('missing.flv','present.mp4','output.flv','active.mp4','retry.mp4'):
            (self.videos/name).write_bytes(b'keep')
        library.seed(self.p)
        for name in ('missing.flv','output.flv','active.mp4','retry.mp4'):(self.videos/name).unlink()
        (self.videos/'output.mp4').write_bytes(b'output')
        with self.p.connect() as db:
            db.execute("UPDATE jobs SET status='running' WHERE source LIKE '%active.mp4'")
            db.execute("UPDATE library_files SET retry_requested=1 WHERE id IN (SELECT id FROM jobs WHERE source LIKE '%retry.mp4')")
        result=reconcile(self.p.DB,self.videos)
        self.assertEqual(result['missing_count'],1)
        with self.p.connect() as db:self.assertEqual(db.execute("SELECT count(*) FROM jobs WHERE status='queued'").fetchone()[0],4)
        result=reconcile(self.p.DB,self.videos,True)
        self.assertEqual(result['missing_count'],1)
        self.assertEqual(reconcile(self.p.DB,self.videos,True)['missing_count'],0)
        with self.p.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM jobs').fetchone()[0],5)
            self.assertEqual(db.execute('SELECT count(*) FROM deleted_jobs').fetchone()[0],1)
            self.assertEqual(db.execute("SELECT count(*) FROM jobs WHERE status='running'").fetchone()[0],1)
        self.assertEqual((self.videos/'output.mp4').read_bytes(),b'output')

    def primary(self):
        self.primary_p=library.processor(self.config,self.videos,self.config/'h265')
        with self.primary_p.connect() as db:queue_control.runtime(db,'test-primary','idle')
        return library.PriorityGate(self.primary_p.DB)

    def test_queue_maintenance_returns_with_its_database_handles_closed(self):
        from unittest.mock import patch
        from library_reconcile import reconcile
        from library_prune_hevc import prune
        (self.videos/'present.flv').write_bytes(b'keep')
        library.seed(self.p)
        real_connect = sqlite3.connect
        for operation in (reconcile, prune):
            with self.subTest(operation=operation.__name__):
                connections = []

                def tracked_connect(*args, **kwargs):
                    connection = real_connect(*args, **kwargs)
                    connections.append(connection)
                    return connection

                def h264_source(root, row):
                    return dict(row, codec='h264')

                try:
                    with patch('sqlite3.connect', side_effect=tracked_connect), \
                         patch('library_prune_hevc.inspect', side_effect=h264_source):
                        result = operation(self.p.DB, self.videos, True)
                    self.assertTrue(connections)
                    for connection in connections:
                        with self.assertRaises(sqlite3.ProgrammingError):
                            connection.execute('SELECT 1')
                    backup = Path(result['backup'])
                    with closing(real_connect(backup)) as db:
                        self.assertEqual(db.execute('SELECT count(*) FROM jobs').fetchone()[0], 1)
                    backup.unlink()
                    self.assertFalse(backup.exists())
                    self.assertEqual((self.videos/'present.flv').read_bytes(), b'keep')
                finally:
                    for connection in connections:
                        connection.close()

    def test_priority_blocks_every_pending_primary_operation_and_stale_or_paused_workers(self):
        gate=self.primary()
        self.assertIsNone(gate.reason())
        for status in ['queued','running','published']:
            with self.primary_p.connect() as db:
                db.execute('DELETE FROM jobs')
                db.execute("INSERT INTO jobs(id,source,size,mtime,delete_source,h264,status,created,updated) VALUES('x','merge.mp4',1,1,1,0,?,1,1)",(status,))
            self.assertEqual(gate.reason(),'primary_busy')
        with self.primary_p.connect() as db:
            db.execute('DELETE FROM jobs');db.execute("UPDATE queue_control SET mode='paused'")
        self.assertEqual(gate.reason(),'primary_paused')
        with self.primary_p.connect() as db:
            db.execute("UPDATE queue_control SET mode='running'");db.execute('UPDATE queue_worker SET heartbeat=0')
        self.assertEqual(gate.reason(),'primary_unavailable')
        self.assertEqual(library.PriorityGate(self.root/'absent.db').reason(),'primary_unavailable')

    def test_manually_changed_source_is_retained_and_can_retry_with_a_new_snapshot(self):
        source=self.videos/'one.mp4';source.write_bytes(b'original')
        library.seed(self.p)
        with self.p.connect() as db:job=dict(db.execute('SELECT * FROM jobs').fetchone())
        source.write_bytes(b'newer media');os.utime(source,(time.time()-600,time.time()-600))
        with self.assertRaisesRegex(ValueError,'Source changed'):
            library.encode(self.p,job)
        with self.p.connect() as db:
            db.execute("UPDATE jobs SET status='failed'")
            db.execute('UPDATE library_files SET retry_requested=1')
        library.retry_requested(self.p)
        with self.p.connect() as db:
            row=db.execute('SELECT * FROM jobs').fetchone()
            self.assertEqual((row['status'],row['size']),('queued',11))
        self.assertEqual(source.read_bytes(),b'newer media')

    def test_previous_requested_merges_must_finish_before_the_batch_can_start(self):
        self.primary()
        identifier='a'*64
        gate=library.PriorityGate(self.primary_p.DB,[identifier])
        with self.primary_p.connect() as db:
            db.execute('CREATE TABLE merge_sessions(id TEXT PRIMARY KEY,job_id TEXT)')
        self.assertEqual(gate.reason(),'primary_prerequisites')
        with self.primary_p.connect() as db:
            db.execute("INSERT INTO jobs(id,source,size,mtime,delete_source,h264,status,created,updated) VALUES('merged','merge.mp4',1,1,1,0,'done',1,1)")
            db.execute('INSERT INTO merge_sessions VALUES(?,?)',(identifier,'merged'))
        self.assertIsNone(gate.reason())


class LibraryApiTests(unittest.TestCase):
    setUp=test_console.ConsoleTests.setUp
    tearDown=test_console.ConsoleTests.tearDown

    def install_batch(self):
        root=self.paths.downloads/'library';root.mkdir()
        (root/'historical.mp4').write_bytes(b'keep original')
        self.p=library.processor(self.paths.config,root)
        library.seed(self.p)
        with self.p.connect() as db:
            identifier=db.execute('SELECT id FROM jobs').fetchone()[0]
            db.execute("UPDATE jobs SET status='failed',error='test decode failed'")
            queue_control.runtime(db,'library-test','waiting_priority')
        return identifier

    def test_absent_batch_is_hidden_and_all_routes_require_auth_and_csrf(self):
        self.assertEqual(self.client.get('/api/library/jobs').json,{'enabled':False})
        self.assertEqual(self.app.test_client().get('/api/library/jobs').status_code,401)
        identifier=self.install_batch()
        self.assertEqual(self.client.post('/api/library/jobs/'+identifier+'/retry',json={}).status_code,403)
        self.assertEqual(self.app.test_client().post('/api/library/order',json={'revision':0,'sort':'largest'}).status_code,401)
        self.assertEqual(self.client.post('/api/library/order',json={'revision':0,'sort':'largest'}).status_code,403)
        self.assertEqual(self.client.get('/api/library/jobs?status=invalid').status_code,422)
        self.assertEqual(self.client.get('/api/library/jobs?page=0').status_code,422)

    def test_historical_concurrency_is_independent_and_revision_guarded(self):
        self.install_batch()
        before = self.client.get('/api/library/jobs').json['control']
        primary_before = self.parts['queue'].control()
        result = self.client.post('/api/library/concurrency', headers=self.headers,
                                  json={'max_workers': 2, 'revision': before['control_revision']})
        self.assertEqual(result.status_code, 200, result.json)
        self.assertEqual(result.json['control']['max_workers'], 2)
        self.assertEqual(self.parts['queue'].control(), primary_before)
        stale = self.client.post('/api/library/concurrency', headers=self.headers,
                                 json={'max_workers': 1, 'revision': before['control_revision']})
        self.assertEqual(stale.status_code, 409)
        three = self.client.post('/api/library/concurrency', headers=self.headers,
                                 json={'max_workers': 3, 'revision': result.json['control']['control_revision']})
        self.assertEqual(three.status_code, 200, three.json)
        self.assertEqual(three.json['control']['max_workers'], 3)

    def test_bulk_deletion_is_atomic_and_protects_retries_active_jobs_and_media(self):
        identifier=self.install_batch()
        other='f'*64
        with self.p.connect() as db:
            db.execute("INSERT INTO jobs(id,source,size,mtime,delete_source,h264,status,created,updated) VALUES(?, 'active.mp4',1,1,1,0,'queued',1,1)",(other,))
            db.execute('INSERT INTO library_files(id) VALUES(?)',(other,))
        route='/api/library/jobs/delete-records'
        self.assertEqual(self.client.post(route,json={'ids':[identifier]}).status_code,403)
        for ids,code in [([],422),([identifier,other],409),([identifier,'e'*64],404)]:
            self.assertEqual(self.client.post(route,headers=self.headers,json={'ids':ids}).status_code,code)
            self.assertEqual(self.client.get('/api/library/jobs?status=failed').json['total'],1)
        with self.p.connect() as db:db.execute('UPDATE library_files SET retry_requested=1 WHERE id=?',(identifier,))
        self.assertEqual(self.client.post(route,headers=self.headers,json={'ids':[identifier]}).status_code,409)
        with self.p.connect() as db:
            db.execute('UPDATE library_files SET retry_requested=0')
            db.execute("UPDATE jobs SET status='done' WHERE id=?",(other,))
        response=self.client.post(route,headers=self.headers,json={'ids':[identifier,other,identifier]})
        self.assertEqual((response.status_code,response.json['deleted']),(200,2))
        self.assertEqual(self.client.get('/api/library/jobs').json['total'],0)
        with self.p.connect() as db:self.assertEqual(db.execute('SELECT count(*) FROM jobs').fetchone()[0],2)
        self.assertEqual((self.p.ROOT/'historical.mp4').read_bytes(),b'keep original')

    def test_history_sorting_orders_the_list_and_waiting_jobs_without_touching_media(self):
        root = self.paths.downloads / 'sortable'
        root.mkdir()
        now = time.time()
        originals = {}
        for name, size, age in [('beta.mp4', 300, 100), ('Alpha.mp4', 100, 300), ('gamma.flv', 200, 200)]:
            path = root / name
            path.write_bytes(bytes([len(name)]) * size)
            os.utime(path, (now - age, now - age))
            originals[name] = path.read_bytes()
        self.p = library.processor(self.paths.config, root)
        library.seed(self.p)
        with self.p.connect() as db:
            queue_control.runtime(db, 'library-sort-test', 'idle')

        for query, expected in [
                ('sort=size&direction=asc', ['Alpha.mp4', 'gamma.flv', 'beta.mp4']),
                ('sort=size&direction=desc', ['beta.mp4', 'gamma.flv', 'Alpha.mp4']),
                ('sort=name&direction=asc', ['Alpha.mp4', 'beta.mp4', 'gamma.flv']),
                ('sort=name&direction=desc', ['gamma.flv', 'beta.mp4', 'Alpha.mp4']),
                ('sort=mtime&direction=asc', ['Alpha.mp4', 'gamma.flv', 'beta.mp4']),
                ('sort=mtime&direction=desc', ['beta.mp4', 'gamma.flv', 'Alpha.mp4'])]:
            data = self.client.get('/api/library/jobs?' + query).json
            self.assertEqual([item['name'] for item in data['items']], expected)
            self.assertTrue(all(item['modified'] for item in data['items']))
        self.assertEqual(self.client.get('/api/library/jobs?sort=unknown').status_code, 422)
        self.assertEqual(self.client.get('/api/library/jobs?sort=size&direction=sideways').status_code, 422)

        revision = self.client.get('/api/library/jobs?status=queued').json['control']['order_revision']
        self.assertEqual(self.client.post('/api/library/order', headers=self.headers,
                                          json={'revision': revision, 'sort': 'unknown'}).status_code, 422)
        for mode, expected in [('largest', ['beta.mp4', 'gamma.flv', 'Alpha.mp4']),
                               ('mtime_asc', ['Alpha.mp4', 'gamma.flv', 'beta.mp4']),
                               ('name_desc', ['gamma.flv', 'beta.mp4', 'Alpha.mp4'])]:
            result = self.client.post('/api/library/order', headers=self.headers,
                                      json={'revision': revision, 'sort': mode})
            self.assertEqual(result.status_code, 200, result.json)
            revision = result.json['control']['order_revision']
            names = [item['name'] for item in self.client.get('/api/library/jobs?status=queued').json['items']]
            self.assertEqual(names, expected)
        self.assertEqual(self.client.post('/api/library/order', headers=self.headers,
                                          json={'revision': revision - 1, 'sort': 'largest'}).status_code, 409)
        for name, content in originals.items():
            self.assertEqual((root / name).read_bytes(), content)

    def test_history_sorting_keeps_every_row_exactly_once_across_pages(self):
        root = self.paths.downloads / 'paged'
        root.mkdir()
        now = time.time()
        names = ['ep%02d.mp4' % index for index in range(30)]
        for index, name in enumerate(names):
            path = root / name
            path.write_bytes(b'x' * (index + 1))
            os.utime(path, (now - index, now - index))
        self.p = library.processor(self.paths.config, root)
        library.seed(self.p)
        with self.p.connect() as db:
            queue_control.runtime(db, 'library-page-test', 'idle')

        seen = []
        for page in (1, 2):
            data = self.client.get('/api/library/jobs?sort=size&direction=desc&page=' + str(page)).json
            self.assertEqual((data['total'], data['page'], data['page_size']), (30, page, 25))
            seen += [item['name'] for item in data['items']]
        self.assertEqual(seen, list(reversed(names)))
        self.assertEqual(len(set(seen)), 30)
        self.assertEqual(self.client.get('/api/library/jobs?sort=size&direction=desc&page=3').json['items'], [])

        paged = []
        for page in (1, 2):
            paged += [item['name'] for item in self.client.get('/api/library/jobs?page=' + str(page)).json['items']]
        self.assertEqual(sorted(paged), sorted(names))

    def test_library_start_now_promotes_queued_file_and_requires_auth_and_revision(self):
        root = self.paths.downloads / 'start-now'
        root.mkdir()
        for index, name in enumerate(('later.mp4', 'first.mp4')):
            path = root / name
            path.write_bytes(b'x' * (index + 1))
        self.p = library.processor(self.paths.config, root)
        library.seed(self.p)
        with self.p.connect() as db:
            queue_control.runtime(db, 'library-start-test', 'idle')
            identifiers = [row[0] for row in db.execute('SELECT id FROM jobs ORDER BY source')]
        route = '/api/library/jobs/' + identifiers[1] + '/start-now'
        data = self.client.get('/api/library/jobs?status=queued').json
        self.assertEqual(self.app.test_client().post(route, json={'revision': data['order_revision']}).status_code, 401)
        self.assertEqual(self.client.post(route, json={'revision': data['order_revision']}).status_code, 403)
        self.assertEqual(self.client.post(route, headers=self.headers,
                                          json={'revision': data['order_revision'] - 1}).status_code, 409)
        result = self.client.post(route, headers=self.headers,
                                  json={'revision': data['order_revision']})
        self.assertEqual(result.status_code, 200, result.json)
        self.assertEqual(self.client.get('/api/library/jobs?status=queued').json['items'][0]['id'],
                         identifiers[1])
        self.assertEqual(result.json['control']['priority_job_id'], identifiers[1])

    def test_all_sorts_paginate_ties_by_basename_and_keep_search_status_and_hidden_records(self):
        root = self.paths.downloads / 'sort-ties'
        root.mkdir()
        for index in range(56):
            folder = root / str(index)
            folder.mkdir()
            file = folder / ('Clip%d.mp4' % (index % 4))
            file.write_bytes(b'x' * (index % 3 + 1))
            os.utime(file, ns=(1_700_000_000_000_000_000 + index % 5,) * 2)
        self.p = library.processor(self.paths.config, root)
        library.seed(self.p)
        with self.p.connect() as db:
            queue_control.runtime(db, 'sort-ties', 'idle')
            rows = [dict(row) for row in db.execute('SELECT * FROM jobs ORDER BY id')]
            for row in rows[:3]:
                db.execute("UPDATE jobs SET status='done' WHERE id=?", (row['id'],))
            db.execute('CREATE TABLE deleted_jobs(id TEXT PRIMARY KEY,deleted_at REAL)')
            db.execute('INSERT INTO deleted_jobs VALUES(?,0)', (rows[0]['id'],))
        visible = [row for row in rows if row['id'] != rows[0]['id']]
        for column in ('size', 'name', 'mtime'):
            key = (lambda row: Path(row['source']).name.casefold()) if column == 'name' else lambda row: row[column]
            for direction in ('asc', 'desc'):
                stable = sorted(visible, key=lambda row: (row['created'], row['id']))
                expected = [row['id'] for row in sorted(stable, key=key, reverse=direction == 'desc')]
                actual = []
                for page in (1, 2, 3):
                    data = self.client.get('/api/library/jobs', query_string={
                        'sort': column, 'direction': direction, 'page': page, 'search': 'Clip'}).json
                    self.assertEqual(data['total'], 55)
                    actual += [item['id'] for item in data['items']]
                self.assertEqual(actual, expected, (column, direction))
                self.assertEqual(len(set(actual)), 55)
        filtered = self.client.get('/api/library/jobs?sort=name&direction=asc&status=done&search=Clip').json
        self.assertEqual(filtered['total'], 2)
        self.assertTrue(all(item['status'] == 'done' for item in filtered['items']))
        default_name = self.client.get('/api/library/jobs?sort=name').json
        explicit_name = self.client.get('/api/library/jobs?sort=name&direction=asc').json
        self.assertEqual([item['id'] for item in default_name['items']], [item['id'] for item in explicit_name['items']])

    def test_error_display_retry_control_and_record_deletion_keep_primary_queue_and_media(self):
        identifier=self.install_batch()
        with closing(sqlite3.connect(self.paths.queue)) as db:before=db.execute('SELECT * FROM jobs').fetchall()
        data=self.client.get('/api/library/jobs?status=failed').json
        self.assertEqual((data['total'],data['items'][0]['error']),(1,'test decode failed'))
        self.assertEqual(data['control']['phase'],'waiting_priority')
        result=self.client.post('/api/library/control',headers=self.headers,json={'action':'pause','revision':data['control']['control_revision']})
        self.assertEqual(result.status_code,200,result.json)
        result=self.client.post('/api/library/jobs/'+identifier+'/retry',headers=self.headers,json={})
        self.assertEqual(result.status_code,200,result.json)
        with self.p.connect() as db:self.assertEqual(db.execute('SELECT retry_requested FROM library_files').fetchone()[0],1)
        self.assertEqual(self.client.delete('/api/library/jobs/'+identifier,headers=self.headers).status_code,409)
        with self.p.connect() as db:db.execute('UPDATE library_files SET retry_requested=0')
        self.assertEqual(self.client.delete('/api/library/jobs/'+identifier,headers=self.headers).status_code,200)
        self.assertEqual(self.client.get('/api/library/jobs').json['total'],0)
        self.assertEqual((self.p.ROOT/'historical.mp4').read_bytes(),b'keep original')
        with closing(sqlite3.connect(self.paths.queue)) as db:self.assertEqual(db.execute('SELECT * FROM jobs').fetchall(),before)


if __name__=='__main__':unittest.main()
