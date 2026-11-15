"""Real FFmpeg batch verification and preemption in disposable directories."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent/'gui'))
import library_worker as library
import queue_control
from shared import write_json


def wait(check, description, timeout=15):
    until=time.monotonic()+timeout
    while time.monotonic()<until:
        if check():return
        time.sleep(.1)
    raise AssertionError('Timed out: '+description)


def main():
    with tempfile.TemporaryDirectory(prefix='dlr-library-ffmpeg-test-') as directory:
        root=Path(directory).resolve()
        seed=root/'seed.flv'
        subprocess.run(['ffmpeg','-nostdin','-hide_banner','-v','error','-f','lavfi','-i','testsrc2=size=160x90:rate=10',
                        '-f','lavfi','-i','sine=frequency=440:sample_rate=48000','-t','4','-c:v','libx264',
                        '-preset','ultrafast','-threads','1','-c:a','aac',str(seed)],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,timeout=30)

        def case(name, suffix='.flv'):
            config, videos=root/name/'config',root/name/'videos'
            videos.mkdir(parents=True)
            source=videos/('中文录播'+suffix)
            if suffix=='.flv':shutil.copyfile(seed,source)
            else:
                subtitle=root/'captions.srt';subtitle.write_text('1\n00:00:00,200 --> 00:00:02,000\nRetain subtitles\n')
                attachment=root/'notes.txt';attachment.write_text('Retain attachment')
                subprocess.run(['ffmpeg','-nostdin','-v','error','-i',str(seed),'-i',str(subtitle),'-map','0','-map','1',
                                '-c','copy','-attach',str(attachment),'-metadata:s:t','mimetype=text/plain',str(source)],
                               check=True,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,timeout=30)
            os.utime(source,(time.time()-600,time.time()-600))
            p=library.processor(config,videos)
            write_json(p.HOME/'settings.json',{'enabled':True,'encoder':'libx265','preset':'ultrafast','cpu_threads':1})
            library.seed(p)
            with p.connect() as db:
                job=dict(db.execute('SELECT * FROM jobs').fetchone())
                db.execute("UPDATE jobs SET status='running'")
            return p,source,job

        def state(p, job):
            with p.connect() as db:return dict(db.execute('SELECT * FROM jobs WHERE id=?',(job['id'],)).fetchone())

        p,source,job=case('verified')
        library.encode(p,job)
        done=state(p,job)
        assert done['status']=='done' and not source.exists()
        assert p.probe(done['output'])[1]['codec_name']=='hevc'
        with p.connect() as db:
            proof=json.loads(db.execute('SELECT verification FROM library_files').fetchone()[0])
            assert proof['full_output_decode'] and proof['packet_counts'][0]=='40'
        print('PASS real H.264 + audio converts to HEVC, every packet and full decode verify, then source is removed')
        hevc_fixture=Path(done['output'])
        config,videos=root/'not-smaller/config',root/'not-smaller/videos';videos.mkdir(parents=True)
        source=videos/'keep-original.mp4'
        subprocess.run(['ffmpeg','-nostdin','-hide_banner','-v','error','-i',str(seed),
                        '-c:v','libx264','-preset','veryslow','-crf','45','-threads','1',
                        '-c:a','copy',str(source)],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,timeout=30)
        os.utime(source,(time.time()-600,)*2)
        original_bytes,original_mtime=source.read_bytes(),source.stat().st_mtime_ns
        p=library.processor(config,videos)
        write_json(p.HOME/'settings.json',{'enabled':True,'encoder':'libx265','preset':'ultrafast','crf':0,'cpu_threads':1})
        library.seed(p)
        with p.connect() as db:
            job=dict(db.execute('SELECT * FROM jobs').fetchone());db.execute("UPDATE jobs SET status='running'")
        library.encode(p,job)
        retained=state(p,job)
        with p.connect() as db:proof=json.loads(db.execute('SELECT verification FROM library_files').fetchone()[0])
        assert source.read_bytes()==original_bytes and source.stat().st_mtime_ns==original_mtime
        assert retained['status']=='done' and retained['output']==str(source) and retained['output_size']==retained['size']
        assert proof['decision']=='retained_original' and proof['candidate_size']>=retained['size']
        assert proof['candidate_verification']['full_output_decode']
        assert list(videos.iterdir())==[source]
        print('PASS real larger HEVC candidate fully verifies, is discarded, and keeps the in-place H.264 original byte-for-byte')
        config,videos=root/'already-hevc/config',root/'already-hevc/videos';videos.mkdir(parents=True)
        existing=videos/'existing.mp4';shutil.copyfile(hevc_fixture,existing);os.utime(existing,(time.time()-600,)*2)
        p=library.processor(config,videos);library.seed(p)
        before=existing.stat()
        with p.connect() as db:job=dict(db.execute('SELECT * FROM jobs').fetchone())
        library.encode(p,job)
        with p.connect() as db:assert db.execute('SELECT already_hevc FROM library_files').fetchone()[0]==1
        assert existing.stat().st_mtime_ns==before.st_mtime_ns and existing.stat().st_size==before.st_size
        print('PASS existing HEVC keeps its sole file unchanged and is recorded as already complete')

        # VFR frames with distinct fine timestamps can collide after the
        # null muxer's default 1/fps rescaling. Keep every decoded frame.
        config,videos=root/'vfr/config',root/'vfr/videos';videos.mkdir(parents=True)
        vfr=videos/'vfr.mp4'
        subprocess.run(['ffmpeg','-nostdin','-hide_banner','-v','error','-f','lavfi',
                        '-i','testsrc2=size=160x90:rate=22','-f','lavfi','-i','sine=sample_rate=48000',
                        '-t','8','-vf',r'settb=1/1000,setpts=N*1000/22+if(eq(mod(N\,22)\,10)\,-35\,0)',
                        '-fps_mode','passthrough','-enc_time_base:v','1:1000','-c:v','libx265',
                        '-preset','ultrafast','-x265-params','pools=1:frame-threads=1:log-level=error',
                        '-c:a','aac',str(vfr)],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,timeout=30)
        p=library.processor(config,videos);library.seed(p)
        with p.connect() as db:job=dict(db.execute('SELECT * FROM jobs').fetchone())
        legacy=subprocess.run(['ffmpeg','-nostdin','-hide_banner','-v','error','-xerror','-err_detect','explode',
                               '-threads','1','-i',str(vfr),'-map','0:v:0','-map','0:a?','-f','null','-'],
                              stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,timeout=30)
        assert b'non monotonically increasing dts' in legacy.stderr,legacy.stderr
        library.verify_streams(p,job,vfr,vfr)
        with p.connect() as db:proof=json.loads(db.execute('SELECT verification FROM library_files').fetchone()[0])
        decoded_frames=int(subprocess.check_output(['ffprobe','-v','error','-select_streams','v:0','-count_frames',
                                                   '-show_entries','stream=nb_read_frames','-of','csv=p=0',str(vfr)]))
        assert proof['full_output_decode'] and proof['decoded_video_frames']==decoded_frames
        assert proof['video_timebase']=='demux'
        print('PASS genuine VFR null-muxer timestamp rounding is reproduced; corrected verifier decodes every frame without drops')

        for name,maps in [('silent',['-map','0:v:0']),('audio-first',['-map','0:a:0','-map','0:v:0'])]:
            variant=root/(name+'.mp4')
            subprocess.run(['ffmpeg','-nostdin','-v','error','-i',str(vfr),*maps,'-c','copy',str(variant)],
                           check=True,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,timeout=30)
            library.verify_streams(p,job,variant,variant)
        print('PASS VFR verification also supports silent files and audio-first stream ordering')

        from library_prune_hevc import prune
        config,videos=root/'hevc-prune/config',root/'hevc-prune/videos';videos.mkdir(parents=True)
        for name,fixture in [('already-hevc.mp4',vfr),('active-hevc.mp4',vfr),('needs-encoding.flv',seed)]:
            shutil.copyfile(fixture,videos/name)
        batch=library.processor(config,videos);library.seed(batch)
        with batch.connect() as db:db.execute("UPDATE jobs SET status='running' WHERE source LIKE '%active-hevc.mp4'")
        snapshot={file.name:file.read_bytes() for file in videos.iterdir()}
        removed=prune(batch.DB,videos,True)
        assert removed['hevc']==1 and removed['excluded']==1,removed
        assert prune(batch.DB,videos,True)['excluded']==0
        with batch.connect() as db:
            assert db.execute('SELECT count(*) FROM jobs').fetchone()[0]==3
            assert db.execute("SELECT count(*) FROM jobs WHERE status='queued'").fetchone()[0]==1
            assert db.execute("SELECT count(*) FROM jobs WHERE status='running'").fetchone()[0]==1
        assert snapshot=={file.name:file.read_bytes() for file in videos.iterdir()}
        print('PASS real codec scan excludes queued HEVC, keeps H.264 and active tasks, retains fingerprints and every source byte')

        # Corrupt one AAC payload without changing container layout or packet
        # counts: the VFR fix must still reject genuine audio corruption.
        damaged=root/'corrupt-aac.mp4';shutil.copyfile(vfr,damaged)
        raw=subprocess.check_output(['ffprobe','-v','error','-select_streams','a:0','-show_packets',
                                     '-show_entries','packet=pts_time,pos,size','-of','json',str(damaged)])
        packet=next(packet for packet in json.loads(raw)['packets'] if float(packet['pts_time'])>=1)
        with damaged.open('r+b') as media:
            media.seek(int(packet['pos']));media.write(b'\xff'*int(packet['size']))
        try:
            library.verify_streams(p,job,vfr,damaged)
            raise AssertionError('Corrupt AAC was accepted')
        except ValueError as error:
            assert 'output decode failed' in str(error),str(error)
        assert vfr.is_file()
        print('PASS genuine corrupt AAC remains a strict verification failure with the original retained')

        # Exercise the actual H.264 -> HEVC path, not just verification of a
        # prebuilt HEVC fixture. A coarse encoder clock aliases close VFR PTS.
        config,videos=root/'vfr-end-to-end/config',root/'vfr-end-to-end/videos';videos.mkdir(parents=True)
        reference=root/'vfr-h264-reference.mkv'
        source=videos/'vfr-source.mp4'
        subprocess.run(['ffmpeg','-nostdin','-hide_banner','-v','error','-f','lavfi',
                        '-i','testsrc2=size=160x96:rate=22','-frames:v','176','-vf',
                        r'settb=1/1000,setpts=N*1000/22+if(eq(mod(N\,22)\,10)\,-35\,0)',
                        '-fps_mode','passthrough','-enc_time_base:v','1:1000','-c:v','libx264',
                        '-preset','ultrafast','-threads','1',str(reference)],check=True,
                       stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,timeout=30)
        subprocess.run(['ffmpeg','-nostdin','-v','error','-i',str(reference),'-c','copy',str(source)],
                       check=True,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,timeout=30)
        def video_pts(file):
            raw=subprocess.check_output(['ffprobe','-v','error','-select_streams','v:0','-show_packets',
                                         '-show_entries','packet=pts_time','-of','json',str(file)])
            return sorted(float(packet['pts_time']) for packet in json.loads(raw)['packets'])
        original_pts=video_pts(source)
        os.utime(source,(time.time()-600,)*2)
        encoded=library.processor(config,videos)
        write_json(encoded.HOME/'settings.json',{'enabled':True,'encoder':'libx265','preset':'ultrafast','cpu_threads':1})
        library.seed(encoded)
        with encoded.connect() as db:encoding_job=dict(db.execute('SELECT * FROM jobs').fetchone())
        library.encode(encoded,encoding_job)
        result=state(encoded,encoding_job)
        output_pts=video_pts(result['output'])
        assert result['status']=='done' and len(original_pts)==len(output_pts)==176
        assert len(set(output_pts))==176,'The encoder rounded distinct VFR frame timestamps'
        offset=output_pts[0]-original_pts[0]
        assert max(abs(actual-offset-expected) for expected,actual in zip(original_pts,output_pts))<.0001
        assert encoded.probe(result['output'])[1]['codec_name']=='hevc'
        print('PASS actual H.264 VFR -> HEVC retains all 176 distinct presentation timestamps, verifies, and safely publishes')

        p,source,job=case('collision')
        unrelated=source.with_suffix('.mp4');unrelated.write_bytes(b'Existing video')
        library.encode(p,job)
        assert unrelated.read_bytes()==b'Existing video' and '.H265-' in state(p,job)['output']
        print('PASS unrelated existing MP4 is preserved and output uses a distinct name')

        p,source,job=case('subtitle-attachment','.mkv')
        library.encode(p,job)
        done=state(p,job);assert done['status']=='done'
        kinds=[s['codec_type'] for s in library.stream_info(p,done['output'])['streams']]
        assert kinds==['video','audio','subtitle','attachment'],kinds
        print('PASS Matroska retains subtitle and attachment tracks while encoding the video')

        p,source,job=case('failed-verification')
        original=source.read_bytes()
        with patch.object(library,'verify_streams',side_effect=ValueError('simulated missing output frames')):
            try:library.encode(p,job);raise AssertionError('Unverified output accepted')
            except ValueError as error:assert 'missing output frames' in str(error)
        assert source.read_bytes()==original and not source.with_suffix('.mp4').exists()
        assert not list(source.parent.glob('.*.tmp.*'))
        print('PASS verification failure preserves source byte-for-byte and removes incomplete output')

        p,source,job=case('published-recovery')
        unlink=Path.unlink
        def fail_delete(path,*args,**kwargs):
            if path==source:raise OSError('simulated interruption after publication')
            return unlink(path,*args,**kwargs)
        with patch.object(Path,'unlink',fail_delete):
            try:library.encode(p,job)
            except OSError:pass
        published=state(p,job);assert published['status']=='published' and source.exists()
        p.finish_published(published)
        assert state(p,job)['status']=='done' and not source.exists()
        print('PASS interrupted post-verification deletion safely finishes from the published output')

        # The primary database is a fixture. No production jobs or controls are touched.
        primary=library.processor(root/'primary',root,root/'primary/h265')
        with primary.connect() as db:queue_control.runtime(db,'primary-test','idle')
        p,source,job=case('preempt')
        control=library.PriorityControl(p.DB,'library-test',job['id'],gate=library.PriorityGate(primary.DB))
        progress=p.HOME/'preemption-progress.txt'
        command=['ffmpeg','-nostdin','-hide_banner','-v','error','-progress',str(progress),'-stats_period','0.1',
                 '-re','-i',str(source),'-map','0:v:0','-map','0:a?','-c','copy','-f','null','-']
        def runtime():
            with p.connect() as db:return queue_control.snapshot(db)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future=pool.submit(control.run,command,check=True,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,timeout=6)
            wait(lambda:progress.exists() and 'out_time_us=' in progress.read_text(),'real FFmpeg progress')
            with primary.connect() as db:
                db.execute("INSERT INTO jobs(id,source,size,mtime,delete_source,h264,status,created,updated) VALUES('merge','merge.mp4',1,1,1,0,'queued',1,1)")
            wait(lambda:control.suspended and runtime()['phase']=='waiting_priority','background suspension for primary merge')
            first=progress.read_bytes();time.sleep(1.3)
            assert progress.read_bytes()==first and not future.done()
            with primary.connect() as db:
                db.execute('DELETE FROM jobs');queue_control.runtime(db,'primary-test','idle')
            future.result(timeout=10)
        assert source.exists() and 'progress=end' in progress.read_text()
        print('PASS incoming primary merge suspends real FFmpeg; idle resumes same process and paused time is excluded from timeout')

        # A corrupt file must fail visibly while the worker continues to later files.
        config,videos=root/'worker/config',root/'worker/videos';videos.mkdir(parents=True)
        bad=videos/'bad.mp4';bad.write_bytes(b'not a video');os.utime(bad,(time.time()-600,)*2)
        p=library.processor(config,videos);write_json(p.HOME/'settings.json',{'enabled':True,'encoder':'libx265'})
        library.seed(p)
        with primary.connect() as db:queue_control.runtime(db,'primary-test','idle')
        script="import sys;from pathlib import Path;sys.path.insert(0,sys.argv[1]);import library_worker as l;l.worker(l.processor(Path(sys.argv[2]),Path(sys.argv[3])),Path(sys.argv[4]))"
        worker=subprocess.Popen([sys.executable,'-c',script,str(Path(library.__file__).parent),str(config),str(videos),str(primary.DB)],
                                stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
        try:
            def failed():
                with p.connect() as db:row=db.execute("SELECT 1 FROM jobs WHERE status='failed'").fetchone()
                log=p.HOME/'postprocess.log'
                return row and log.exists() and 'moov atom not found' in log.read_text()
            wait(failed,'persisted corrupt-file failure')
            with p.connect() as db:reason=db.execute('SELECT error FROM jobs').fetchone()[0]
            assert 'FFprobe failed' in reason and 'moov atom not found' in reason,reason
            assert bad.read_bytes()==b'not a video'
            log=(p.HOME/'postprocess.log').read_text();assert 'bad.mp4' in log and 'moov atom not found' in log
        finally:
            worker.terminate();worker.communicate(timeout=10)
        print('PASS corrupt-file reason persists in the console database and rotating log; source retained')


if __name__=='__main__':main()
