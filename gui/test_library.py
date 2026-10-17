"""Library import, isolation, fail-closed priority and authenticated API checks."""
from contextlib import closing
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest

import library_worker as library
from library_repository import LibraryRepository
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

    def primary(self):
        self.primary_p=library.processor(self.config,self.videos,self.config/'h265')
        with self.primary_p.connect() as db:queue_control.runtime(db,'test-primary','idle')
        return library.PriorityGate(self.primary_p.DB)

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
        self.assertEqual(self.client.get('/api/library/jobs?status=invalid').status_code,422)
        self.assertEqual(self.client.get('/api/library/jobs?page=0').status_code,422)

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
