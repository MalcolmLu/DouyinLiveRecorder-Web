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
             patch.object(processor, 'mapped_packet_counts', return_value=[175103, 136788]):
            with self.assertRaisesRegex(ValueError, 'normalized source=2918.219s differs'):
                processor.verify_duration(self.source, self.output, 2908.84, 2600)

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

    def test_nonfinite_duration_is_rejected(self):
        for value in ('NaN', 'inf', '-1'):
            response = ('{"format":{"duration":"' + value + '"},"streams":[{"codec_type":"video"}]}').encode()
            with patch.object(processor.subprocess, 'check_output', return_value=response):
                with self.assertRaisesRegex(ValueError, 'Invalid duration'):
                    processor.probe(self.source)


if __name__ == '__main__':
    unittest.main()
