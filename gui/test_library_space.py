"""Exercise the real publication lifecycle without touching production media."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

import library_worker as library
from library_policy import retention_details
import test_library as library_tests
from shared import write_json


class LibrarySpaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='library-space-unit-')
        root = Path(self.temp.name).resolve()
        self.config, self.videos = root/'config', root/'videos'
        self.config.mkdir(); self.videos.mkdir()
        self.source = self.videos/'original.flv'
        self.source.write_bytes(b'O'*128)
        os.utime(self.source, (time.time()-600,)*2)
        self.p = library.processor(self.config, self.videos)
        write_json(self.p.HOME/'settings.json', {'enabled':True, 'encoder':'libx265', 'cpu_threads':1})
        library.seed(self.p)
        with self.p.connect() as db:
            self.job = dict(db.execute('SELECT * FROM jobs').fetchone())
            db.execute("UPDATE jobs SET status='running'")

    def tearDown(self):
        self.temp.cleanup()

    def encode(self, size, *, decode_error=False, changed=False):
        video = {'codec_type':'video', 'codec_name':'h264', 'width':16, 'height':16,
                 'pix_fmt':'yuv420p', 'nb_read_packets':'40'}
        audio = {'codec_type':'audio', 'codec_name':'aac', 'sample_rate':'48000',
                 'channels':2, 'channel_layout':'stereo', 'nb_read_packets':'80'}

        def info(p, file, packets=False):
            return {'streams':[{**video, 'codec_name':'h264' if Path(file)==self.source else 'hevc'}, audio]}

        def probe(file):
            return 4.0, {**video, 'codec_name':'h264' if Path(file)==self.source else 'hevc'}, 1

        def process(command, **kwargs):
            if command[-1] == '-':
                if changed:
                    self.source.write_bytes(b'user changed the original')
                return subprocess.CompletedProcess(command, 1 if decode_error else 0,
                    b'frame=40\ndup_frames=0\ndrop_frames=0\nprogress=end\n',
                    b'damaged audio' if decode_error else b'')
            Path(command[-1]).write_bytes(b'E'*size)
            return subprocess.CompletedProcess(command, 0, b'', b'')

        with patch.object(library, 'stream_info', side_effect=info), \
             patch.object(self.p, 'probe', side_effect=probe), \
             patch.object(self.p, 'opened_by_ffmpeg', return_value=False), \
             patch.object(self.p, 'run_process', side_effect=process):
            library.encode(self.p, self.job)

    def result(self):
        with self.p.connect() as db:
            job = dict(db.execute('SELECT * FROM jobs').fetchone())
            raw = db.execute('SELECT verification FROM library_files').fetchone()[0]
        return job, json.loads(raw) if raw else None

    def test_larger_and_equal_candidates_keep_original_bytes_timestamps_and_zero_savings(self):
        for size in (256, 128):
            with self.subTest(candidate_size=size):
                before = self.source.stat()
                self.encode(size)
                job, proof = self.result()
                self.assertEqual(self.source.read_bytes(), b'O'*128)
                self.assertEqual(self.source.stat().st_mtime_ns, before.st_mtime_ns)
                self.assertEqual(list(self.videos.iterdir()), [self.source])
                self.assertEqual((job['status'], job['output'], job['output_size']), ('done', str(self.source), 128))
                self.assertEqual(job['size']-job['output_size'], 0)
                self.assertEqual(retention_details(proof), {'retained_original':True, 'candidate_size':size})
                self.assertTrue(proof['candidate_verification']['full_output_decode'])

    def test_smaller_verified_result_replaces_source_and_records_actual_savings(self):
        self.encode(64)
        job, proof = self.result()
        self.assertFalse(self.source.exists())
        self.assertEqual(Path(job['output']).read_bytes(), b'E'*64)
        self.assertEqual(job['size']-job['output_size'], 64)
        self.assertFalse(retention_details(proof)['retained_original'])

    def test_larger_corrupt_candidate_still_fails_instead_of_claiming_success(self):
        with self.assertRaisesRegex(ValueError, 'damaged audio'):
            self.encode(256, decode_error=True)
        self.assertEqual(self.source.read_bytes(), b'O'*128)
        self.assertEqual(list(self.videos.iterdir()), [self.source])
        self.assertFalse(retention_details(self.result()[1])['retained_original'])

    def test_source_change_during_verification_is_not_recorded_as_a_retained_success(self):
        with self.assertRaisesRegex(ValueError, 'Source changed'):
            self.encode(256, changed=True)
        self.assertEqual(self.source.read_bytes(), b'user changed the original')
        self.assertFalse(retention_details(self.result()[1])['retained_original'])

    def test_retained_original_can_retry_with_fresh_proof_and_same_fingerprint_row(self):
        self.encode(256)
        with self.p.connect() as db:
            db.execute('UPDATE library_files SET retry_requested=1')
        library.retry_requested(self.p)
        job, proof = self.result()
        self.assertEqual((job['status'], job['id']), ('queued', self.job['id']))
        self.assertIsNone(proof)
        self.assertEqual(self.source.read_bytes(), b'O'*128)

    def test_in_place_mp4_is_preserved_when_larger_and_replaced_only_on_a_smaller_retry(self):
        mp4 = self.source.with_suffix('.mp4')
        self.source.rename(mp4)
        self.source = mp4
        self.job['source'] = str(mp4)
        with self.p.connect() as db:
            db.execute('UPDATE jobs SET source=?', (str(mp4),))
        self.encode(256)
        self.assertEqual(mp4.read_bytes(), b'O'*128)
        with self.p.connect() as db:
            db.execute('UPDATE library_files SET retry_requested=1')
        library.retry_requested(self.p)
        with self.p.connect() as db:
            self.job = dict(db.execute('SELECT * FROM jobs').fetchone())
            db.execute("UPDATE jobs SET status='running'")
        self.encode(64)
        self.assertEqual(mp4.read_bytes(), b'E'*64)
        self.assertEqual(list(self.videos.iterdir()), [mp4])
        self.assertFalse(retention_details(self.result()[1])['retained_original'])

    def test_policy_metadata_handles_legacy_or_malformed_proofs(self):
        for value in (None, '', '{bad', '[]', 'null', '42', '{"codec":"hevc"}'):
            self.assertFalse(retention_details(value)['retained_original'])
        self.assertEqual(retention_details({'decision':'retained_original','candidate_size':True}),
                         {'retained_original':True, 'candidate_size':None})


class LibrarySpaceApiTests(unittest.TestCase):
    setUp = library_tests.LibraryApiTests.setUp
    tearDown = library_tests.LibraryApiTests.tearDown
    install_batch = library_tests.LibraryApiTests.install_batch

    def retained(self):
        identifier = self.install_batch()
        with self.p.connect() as db:
            db.execute("UPDATE jobs SET status='done',error=NULL,output=source,output_size=size")
            db.execute('UPDATE library_files SET verification=?',
                       (json.dumps({'decision':'retained_original','candidate_size':900}),))
        return identifier

    def test_snapshot_explains_retention_and_does_not_count_discarded_candidate_as_savings(self):
        self.retained()
        data = self.client.get('/api/library/jobs?status=done').json
        self.assertEqual(data['retained_original'], 1)
        self.assertTrue(data['items'][0]['retained_original'])
        self.assertEqual(data['items'][0]['candidate_size'], 900)
        self.assertEqual(data['summary']['saved_bytes'], 0)

    def test_retry_requires_auth_csrf_and_keeps_hidden_retained_records_hidden(self):
        identifier = self.retained()
        route = '/api/library/jobs/'+identifier+'/retry'
        self.assertEqual(self.client.post(route,json={}).status_code, 403)
        self.assertEqual(self.client.post(route,headers=self.headers,json={}).status_code, 200)
        self.assertEqual(self.client.delete('/api/library/jobs/'+identifier,headers=self.headers).status_code, 409)
        library.retry_requested(self.p)
        with self.p.connect() as db:
            self.assertEqual(db.execute('SELECT status FROM jobs').fetchone()[0], 'queued')
            db.execute("UPDATE jobs SET status='done'")
            db.execute('UPDATE library_files SET verification=?',
                       (json.dumps({'decision':'retained_original','candidate_size':900}),))
        self.assertEqual(self.client.delete('/api/library/jobs/'+identifier,headers=self.headers).status_code, 200)
        self.assertEqual(self.client.post(route,headers=self.headers,json={}).status_code, 404)


if __name__ == '__main__':
    unittest.main()
