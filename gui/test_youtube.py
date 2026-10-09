"""Offline regression checks for YouTube track selection and FFmpeg adaptation."""
import unittest
from youtube_source import live_data, with_audio


class YouTubeTests(unittest.TestCase):
    def test_separate_audio_is_required_and_quality_order_is_preserved(self):
        info={"is_live":True,"channel":"test","formats":[
            {"protocol":"m3u8_native","url":"low","vcodec":"avc1","acodec":"none","height":360},
            {"protocol":"m3u8_native","url":"high","vcodec":"avc1","acodec":"none","height":1080},
            {"protocol":"m3u8_native","url":"audio","vcodec":"none","tbr":128}]}
        result=live_data(info)
        self.assertEqual(result["play_url_list"],["high","low"])
        self.assertEqual(result["audio_url"],"audio")
        # v4.0.7 get_stream_url(spec=True) reads both compatibility fields.
        self.assertEqual(result["m3u8_url"],"high")
        self.assertEqual(result["flv_url"],"high")
        info["formats"].pop()
        with self.assertRaisesRegex(ValueError,"缺少音频"):live_data(info)

    def test_offline_and_muxed_live(self):
        self.assertFalse(live_data({"is_live":False,"channel":"test"})["is_live"])
        result=live_data({"is_live":True,"formats":[{"protocol":"m3u8_native","url":"muxed",
            "vcodec":"avc1","acodec":"mp4a","height":720}]})
        self.assertIsNone(result["audio_url"])

    def test_network_options_and_video_audio_maps_for_segment_recording(self):
        cmd=["ffmpeg","-http_proxy","http://fixture.invalid:1","-y","-rw_timeout","50000000",
             "-protocol_whitelist","http,https,tls,httpproxy","-re","-i","video",
             "-map","0","-c:v","copy","-c:a","copy","-f","segment","out_%03d.ts"]
        out=with_audio(cmd,"audio")
        self.assertEqual(out.count("-http_proxy"),2)
        self.assertEqual(out.count("-protocol_whitelist"),2)
        self.assertIn(["-map","0:v:0","-map","1:a:0"], [out[i:i+4] for i in range(len(out))])
        self.assertEqual([out[i+1] for i,v in enumerate(out) if v=="-i"],["video","audio"])
        self.assertEqual(out[-1],"out_%03d.ts")

    def test_audio_only_recording_maps_audio_input(self):
        out=with_audio(["ffmpeg","-i","video","-map","0:a","-c:a","aac","out.m4a"],"audio")
        self.assertEqual(out[out.index("-map")+1],"1:a:0")
        self.assertNotIn("0:v:0",out)


if __name__ == "__main__":unittest.main(verbosity=2)
