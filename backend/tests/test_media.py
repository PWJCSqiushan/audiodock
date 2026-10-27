import hashlib
import shutil
import subprocess

import pytest

from app import main


@pytest.mark.parametrize("fmt,bitrate,ext", [
    ("mp3", 128, "mp3"),
    ("m4a", 128, "m4a"),
    ("opus", 96, "opus"),
    ("flac", 0, "flac"),
    ("wav", 0, "wav"),
    ("original", 0, "wav"),
])
def test_job_pipeline_converts_and_fully_decodes_synthetic_audio(tmp_path, monkeypatch, fmt, bitrate, ext):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("FFmpeg/ffprobe unavailable")
    media_dir = tmp_path / "media"
    media_dir.mkdir()
    source = media_dir / "source.wav"
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=3", "-c:a", "pcm_s16le", str(source)], check=True)
    monkeypatch.setattr(main, "DATA_DIR", tmp_path / "runtime")
    monkeypatch.setattr(main, "RECYCLE_DIR", tmp_path / "runtime" / "recycle")
    monkeypatch.setattr(main, "metadata", lambda url, site, internal=False: {"url":url,"site":site,"id":"synthetic","title":"Synthetic","uploader":"test","duration":3,"thumbnail":None,"_cid":7})
    monkeypatch.setattr(main, "public_host", lambda host: True)
    monkeypatch.setattr(main, "_download_source", lambda j, target, preflight: (dict(preflight, _ext="wav"), source))
    jid = f"media-{fmt}"
    main.jobs[jid] = {"id":jid,"input":"https://youtu.be/synthetic","format":fmt,"bitrate":bitrate,"sample_rate":0,"channels":0,"keep_original":True,"status":"queued","progress":0,"message":"waiting"}
    try:
        main._run_job(jid)
        job=main.jobs[jid]
        assert job["status"] == "completed", (job.get("error_code"),job.get("message"),job.get("_diagnostic"))
        assert job["result"]["filename"].endswith("."+ext)
        assert job["result"]["duration"] == pytest.approx(3, abs=0.1)
        assert job["result"]["bytes"] > 0
        assert len(job["result"]["sha256"]) == 64
        result=tmp_path/"runtime"/"jobs"/jid/f"audio.{ext}"
        assert result.is_file()
    finally:
        main.jobs.pop(jid, None)


def test_bilibili_range_download_resumes_without_corrupting_partial_chunk(tmp_path, monkeypatch):
    total=2*1024*1024+13
    payload=bytes((i%251 for i in range(total)))
    calls=[]

    class Response:
        status_code=206
        def __init__(self, start, end, fail_once=False):
            self.headers={"Content-Range":f"bytes {start}-{end}/{total}"}
            self.start=start; self.end=end; self.fail_once=fail_once
        def iter_content(self, chunk_size):
            piece=payload[self.start:self.end+1]
            if self.fail_once:
                yield piece[:65536]
                raise OSError("simulated interrupted CDN connection")
            for p in range(0,len(piece),chunk_size): yield piece[p:p+chunk_size]
        def close(self): pass

    class Session:
        def __init__(self): self.headers={}
        def get(self, url, params=None, headers=None, stream=False, timeout=None):
            if url.endswith("/playurl"):
                return type("APIResponse",(),{"status_code":200,"json":lambda self:{"data":{"dash":{"audio":[{"bandwidth":10,"baseUrl":"https://low.bilivideo.com/a"},{"bandwidth":20,"baseUrl":"https://high.bilivideo.com/a"}]}}}})()
            start=int(headers["Range"].split("=")[1].split("-")[0]); end=min(total-1,start+2*1024*1024-1)
            calls.append((url,start))
            fails=(start==0 and sum(1 for _,n in calls if n==0)==1)
            return Response(start,end,fails)

    monkeypatch.setattr(main,"metadata",lambda url,site,internal=False:{"url":url,"site":site,"id":"BV1234567890","title":"unit","uploader":"test","duration":3,"thumbnail":None,"_cid":8})
    monkeypatch.setattr(main,"GuardedSession",Session)
    monkeypatch.setattr(main,"public_host",lambda host:True)
    dest=tmp_path/"source.m4a"
    job={"id":"range-test","input":"https://www.bilibili.com/video/BV1234567890","deadline":main.time.time()+60,"progress":None}
    meta,_=main._bili_download("https://www.bilibili.com/video/BV1234567890",dest,job,{"id":"BV1234567890","title":"unit","_cid":8})
    assert meta["title"]=="unit"
    assert dest.read_bytes()==payload
    assert [start for _,start in calls]==[0,0,2*1024*1024]
    assert all("high.bilivideo.com" in url for url,_ in calls)


@pytest.mark.parametrize("address", ["127.0.0.1", "10.0.0.1", "::1", "fe80::1", "64:ff9b::808:808", "64:ff9b:1::1", "::ffff:127.0.0.1"])
def test_network_guard_rejects_private_and_special_ip_ranges(address):
    assert not main._is_public_ip(address)


def test_network_guard_rejects_unknown_hosts_and_non_https_ports():
    with main._guarded_network():
        with pytest.raises(PermissionError): main._socket_guard("socket.getaddrinfo", ("evil.example", 443, 0, 1, 0, 0))
        with pytest.raises(PermissionError): main._socket_guard("socket.getaddrinfo", ("youtube.com", 80, 0, 1, 0, 0))
        with pytest.raises(PermissionError): main._socket_guard("socket.connect", (None, ("10.0.0.1", 443)))


def test_guarded_network_context_blocks_real_loopback_connection():
    import socket
    with main._guarded_network():
        with pytest.raises(PermissionError): socket.create_connection(("127.0.0.1", 443), timeout=0.1)


@pytest.mark.parametrize("protocol,accepted", [("https", True), ("m3u8_native", False)])
def test_ytdlp_audio_fetch_allows_only_direct_https(tmp_path, monkeypatch, protocol, accepted):
    source=tmp_path/"source.m4a"
    state={}

    class FakeYDL:
        def __init__(self, opts): state["opts"]=opts
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def extract_info(self, url, download=False):
            return {"id":"x","title":"x","duration":3,"ext":"m4a","formats":[{"acodec":"aac","vcodec":"none","protocol":protocol}]}
        def process_ie_result(self, info, download=True):
            source.write_bytes(b"synthetic-audio")
            return info

    monkeypatch.setattr(main.yt_dlp,"YoutubeDL",FakeYDL)
    monkeypatch.setattr(main,"_storage_used",lambda:0)
    job={"id":"direct-test","input":"https://youtu.be/synthetic","deadline":main.time.time()+60,"progress":None}
    if accepted:
        meta,path=main._download_source(job,tmp_path/"source.bin",{"duration":3})
        assert meta["_ext"]=="m4a" and path==source
        assert state["opts"]["format"]=="bestaudio[protocol=https]"
        assert state["opts"]["ignoreconfig"] is True and state["opts"]["proxy"]==""
    else:
        with pytest.raises(RuntimeError,match="upstream_unavailable"):
            main._download_source(job,tmp_path/"source.bin",{"duration":3})
        assert not source.exists()


def test_duration_limit_is_checked_before_source_download(tmp_path, monkeypatch):
    monkeypatch.setattr(main,"DATA_DIR",tmp_path/"runtime")
    monkeypatch.setattr(main,"metadata",lambda url,site,internal=False:{"duration":3601,"title":"long","id":"x"})
    monkeypatch.setattr(main,"public_host",lambda host:True)
    monkeypatch.setattr(main,"_download_source",lambda *args:pytest.fail("download should not begin"))
    jid="duration-test"
    main.jobs[jid]={"id":jid,"input":"https://youtu.be/x","format":"mp3","bitrate":320,"sample_rate":0,"channels":0,"keep_original":False,"status":"queued"}
    try:
        main._run_job(jid)
        assert main.jobs[jid]["status"]=="failed"
        assert main.jobs[jid]["error_code"]=="duration_limit"
    finally: main.jobs.pop(jid,None)
