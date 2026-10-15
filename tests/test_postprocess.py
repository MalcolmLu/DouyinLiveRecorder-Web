"""Reject incomplete output while accepting a verified TS duration estimate error."""
import importlib.util
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('duration_verification', Path(__file__).resolve().parent.parent / 'h265/postprocess.py')
processor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(processor)


class DurationVerificationTests(unittest.TestCase):
    def setUp(self):
        self.source, self.output = Path('recording.ts'), Path('output.mp4')

    def test_matching_duration_keeps_fast_validation(self):
        with patch.object(processor, 'normalized_reference') as scan:
            processor.verify_duration(self.source, self.output, 3600, 3600.07)
            scan.assert_not_called()

    def test_bad_ts_estimate_requires_matching_full_timeline_and_every_stream(self):
        with patch.object(processor, 'normalized_reference', return_value=(2918.219, 175103)), \
             patch.object(processor, 'mapped_packet_counts', side_effect=[[175103, 136788], [175103, 136788]]):
            processor.verify_duration(self.source, self.output, 2908.84, 2918.27)

    def test_truncated_output_is_rejected_even_when_quick_estimate_is_wrong(self):
        with patch.object(processor, 'normalized_reference', return_value=(2918.219, 175103)), \
             patch.object(processor, 'mapped_packet_counts', side_effect=[[175103, 136788], [160000, 130000]]):
            with self.assertRaisesRegex(ValueError, 'packet counts differ.*original retained'):
                processor.verify_duration(self.source, self.output, 2908.84, 2700)

    def test_missing_audio_packets_are_rejected(self):
        with patch.object(processor, 'normalized_reference', return_value=(2918.219, 175103)), \
             patch.object(processor, 'mapped_packet_counts', side_effect=[[175103, 136788], [175103, 136000]]):
            with self.assertRaisesRegex(ValueError, 'packet counts differ'):
                processor.verify_duration(self.source, self.output, 2908.84, 2918.27)

    def test_equal_counts_with_wrong_timing_are_rejected(self):
        with patch.object(processor, 'normalized_reference', return_value=(2918.219, 175103)), \
             patch.object(processor, 'mapped_packet_counts', return_value=[175103, 136788]), \
             patch.object(processor, 'verify_mp4_timeline', side_effect=ValueError('MP4 reference=2918.219s differs from output')):
            with self.assertRaisesRegex(ValueError, 'MP4 reference=2918.219s differs'):
                processor.verify_duration(self.source, self.output, 2908.84, 2600)

    def test_longer_audio_requires_mp4_track_verification(self):
        with patch.object(processor, 'normalized_reference', return_value=(1926.618, 121772)), \
             patch.object(processor, 'mapped_packet_counts', return_value=[121772, 88130]), \
             patch.object(processor, 'verify_mp4_timeline', return_value=2046.373) as muxed:
            processor.verify_duration(self.source, self.output, 1914.843, 2046.373)
            muxed.assert_called_once_with(self.source, self.output, [121772, 88130], 2046.373)

    def test_mp4_reference_checks_every_track_not_only_container_duration(self):
        reference = (2046.373, [('video', .033, 1926.592), ('audio', 0, 2046.373)])
        for changed in (
                (2046.373, [('video', .033, 1800), ('audio', 0, 2046.373)]),
                (2046.373, [('video', 10, 1926.592), ('audio', 0, 2046.373)]),
                (2046.373, [('video', .033, 1926.592), ('audio', 0, 2000)]),
                (2046.373, [('video', .033, 1926.592)])):
            with patch.object(processor.subprocess, 'run'), \
                 patch.object(processor, 'mapped_packet_counts', return_value=[121772, 88130]), \
                 patch.object(processor, 'mapped_stream_timings', side_effect=[reference, changed]):
                with self.assertRaisesRegex(ValueError, 'track .*differs'):
                    processor.verify_mp4_timeline(self.source, self.output, [121772, 88130], 2046.373)

    def test_mp4_reference_accepts_preserved_longer_audio_and_cleans_up(self):
        reference = (2046.373, [('video', .033, 1926.592), ('audio', 0, 2046.373)])
        actual = (2046.373, [('video', .050, 1926.600), ('audio', 0, 2046.373)])
        with patch.object(processor.subprocess, 'run') as run, \
             patch.object(processor, 'mapped_packet_counts', return_value=[121772, 88130]), \
             patch.object(processor, 'mapped_stream_timings', side_effect=[reference, actual]):
            self.assertEqual(processor.verify_mp4_timeline(self.source, self.output, [121772, 88130], 2046.373), 2046.373)
            command = run.call_args.args[0]
            self.assertIn('copy', command)
            self.assertFalse(Path(command[-1]).parent.exists())

    def test_mp4_reference_incomplete_copy_and_timeout_fail_closed(self):
        with patch.object(processor.subprocess, 'run'), \
             patch.object(processor, 'mapped_packet_counts', return_value=[121772, 80000]):
            with self.assertRaisesRegex(ValueError, 'preserve every mapped packet'):
                processor.verify_mp4_timeline(self.source, self.output, [121772, 88130], 2046.373)
        with patch.object(processor.subprocess, 'run', side_effect=subprocess.TimeoutExpired('ffmpeg', 1800)) as run:
            with self.assertRaises(subprocess.TimeoutExpired):
                processor.verify_mp4_timeline(self.source, self.output, [121772, 88130], 2046.373)
            self.assertFalse(Path(run.call_args.args[0][-1]).parent.exists())

    def test_partial_reference_scan_and_timeouts_are_rejected(self):
        for error in (ValueError('scan incomplete'), subprocess.TimeoutExpired('ffmpeg', 1800)):
            with patch.object(processor, 'normalized_reference', side_effect=error):
                with self.assertRaisesRegex(ValueError, 'original retained'):
                    processor.verify_duration(self.source, self.output, 10, 20)

    def test_reference_copy_must_process_every_video_packet(self):
        with patch.object(processor, 'normalized_reference', return_value=(20, 299)), \
             patch.object(processor, 'mapped_packet_counts', return_value=[300, 960]):
            with self.assertRaisesRegex(ValueError, 'packet counts differ'):
                processor.verify_duration(self.source, self.output, 10, 20)

    def test_stream_copy_without_frame_progress_requires_complete_mp4_reference(self):
        result = subprocess.CompletedProcess([], 0, stdout=b'out_time_us=24064000\r\nprogress=end\r\n')
        with patch.object(processor.subprocess, 'run', return_value=result):
            self.assertEqual(processor.normalized_reference(self.source), (24.064, None))
        with patch.object(processor, 'normalized_reference', return_value=(24.064, None)), \
             patch.object(processor, 'mapped_packet_counts', return_value=[360, 1130]), \
             patch.object(processor, 'verify_mp4_timeline', return_value=24.064) as reference:
            processor.verify_duration(self.source, self.output, 12.021, 24.166)
            reference.assert_called_once_with(self.source, self.output, [360, 1130], 24.166)

    def test_missing_counter_cannot_accept_truncated_packets(self):
        with patch.object(processor, 'normalized_reference', return_value=(24.064, None)), \
             patch.object(processor, 'mapped_packet_counts', side_effect=[[360, 1130], [120, 400]]), \
             patch.object(processor, 'verify_mp4_timeline') as reference:
            with self.assertRaisesRegex(ValueError, 'packet counts differ.*original retained'):
                processor.verify_duration(self.source, self.output, 12.021, 8)
            reference.assert_not_called()

    def test_missing_counter_still_rejects_bad_mp4_track_timing(self):
        with patch.object(processor, 'normalized_reference', return_value=(24.064, None)), \
             patch.object(processor, 'mapped_packet_counts', return_value=[360, 1130]), \
             patch.object(processor, 'verify_mp4_timeline', side_effect=ValueError('MP4 reference audio track timing differs')):
            with self.assertRaisesRegex(ValueError, 'audio track timing differs.*original retained'):
                processor.verify_duration(self.source, self.output, 12.021, 24.166)

    def test_missing_counter_reference_failure_and_timeout_retain_original(self):
        for error in (ValueError('reference did not preserve every mapped packet'), subprocess.TimeoutExpired('ffmpeg', 1800)):
            with patch.object(processor, 'normalized_reference', return_value=(24.064, None)), \
                 patch.object(processor, 'mapped_packet_counts', return_value=[360, 1130]), \
                 patch.object(processor, 'verify_mp4_timeline', side_effect=error):
                with self.assertRaisesRegex(ValueError, 'original retained'):
                    processor.verify_duration(self.source, self.output, 12.021, 24.166)

    def test_explicit_zero_frames_or_incomplete_progress_remains_invalid(self):
        for raw in (b'out_time_us=24064000\nframe=0\nprogress=end\n',
                    b'out_time_us=24064000\nprogress=continue\n'):
            result = subprocess.CompletedProcess([], 0, stdout=raw)
            with patch.object(processor.subprocess, 'run', return_value=result):
                with self.assertRaisesRegex(ValueError, 'verification did not finish'):
                    processor.normalized_reference(self.source)

    def test_nonfinite_duration_is_rejected(self):
        for value in ('NaN', 'inf', '-1'):
            response = ('{"format":{"duration":"' + value + '"},"streams":[{"codec_type":"video"}]}').encode()
            with patch.object(processor.subprocess, 'check_output', return_value=response):
                with self.assertRaisesRegex(ValueError, 'Invalid duration'):
                    processor.probe(self.source)


if __name__ == '__main__':
    unittest.main()
