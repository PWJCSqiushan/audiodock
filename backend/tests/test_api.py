import hashlib
import os
import socket
import secrets
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta
from starlette.requests import Request

from app import main


def clear_state():
    main.jobs.clear()
    main.rate_hits.clear()
    while not main.work.empty():
        try: main.work.get_nowait(); main.work.task_done()
        except Exception: break


def request(ip="198.51.100.50"):
    return Request({"type":"http", "method":"POST", "path":"/api/jobs", "headers":[], "client":(ip,12345), "server":("test",80), "scheme":"http", "query_string":b""})


def test_health_contract():
    body = main.health()
    assert body["version"] == "0.1.0"
    assert body["limits"] == {"max_duration_seconds": 3600, "max_source_bytes": 268435456, "max_output_bytes": 786432000, "max_queue": 5, "jobs_per_hour": 3}
    assert body["formats"] == ["mp3", "m4a", "opus", "flac", "wav", "original"]


def test_request_body_limit_with_and_without_content_length(monkeypatch):
    clear_state()
    async def invoke(headers, chunks):
        events=[{"type":"http.request","body":part,"more_body":i<len(chunks)-1} for i,part in enumerate(chunks)]
        async def receive(): return events.pop(0) if events else {"type":"http.disconnect"}
        sent=[]
        async def send(message): sent.append(message)
        async def endpoint(scope,recv,send):
            body=bytearray()
            while True:
                msg=await recv()
                if msg["type"]!="http.request": break
                body.extend(msg.get("body",b""))
                if not msg.get("more_body",False): break
            await send({"type":"http.response.start","status":200,"headers":[]})
            await send({"type":"http.response.body","body":bytes(body)})
        scope={"type":"http","headers":headers}
        await main.RequestBodyLimit(endpoint,main.MAX_REQUEST_BODY)(scope,receive,send)
        return sent
    async def exercise():
        normal=await invoke([], [b'{"input":"https://youtu.be/abc"}'])
        assert normal[0]["status"]==200
        too_large=await invoke([(b"content-length",str(main.MAX_REQUEST_BODY+1).encode())],[b"x"])
        assert too_large[0]["status"]==413 and b'"code":"size_limit"' in too_large[1]["body"]
        chunked=await invoke([], [b"x"*8000,b"y"*9000])
        assert chunked[0]["status"]==413 and b'"code":"size_limit"' in chunked[1]["body"]
    def drive(coro):
        try:
            while True: coro.send(None)
        except StopIteration as result: return result.value
    drive(exercise())
    clear_state()


def test_uvicorn_process_starts_and_serves_health(tmp_path):
    import pytest
    if os.environ.get("AUDIODOCK_TEST_LOOPBACK")!="1": pytest.skip("loopback integration requires AUDIODOCK_TEST_LOOPBACK=1")
    import requests
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port=listener.getsockname()[1]
    env=os.environ.copy()
    env["AUDIODOCK_RUNTIME_DIR"]=str(tmp_path/"runtime")
    env["PYTHONPATH"]=str(__import__("pathlib").Path(__file__).resolve().parents[1])
    proc=subprocess.Popen([sys.executable,"-m","uvicorn","app.main:app","--app-dir",str(__import__("pathlib").Path(__file__).resolve().parents[1]),"--host","127.0.0.1","--port",str(port),"--no-access-log","--no-proxy-headers"],env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    try:
        deadline=time.monotonic()+10
        while time.monotonic()<deadline and proc.poll() is None:
            try:
                response=requests.get(f"http://127.0.0.1:{port}/api/health",timeout=0.5)
                if response.status_code==200:
                    assert response.json()["status"]=="ok"
                    created=requests.post(f"http://127.0.0.1:{port}/api/jobs",json={"input":"https://youtu.be/test"},timeout=2)
                    assert created.status_code==200 and created.json()["status"]=="queued"
                    oversized=requests.post(f"http://127.0.0.1:{port}/api/jobs",data=b"x"*(main.MAX_REQUEST_BODY+1),headers={"content-type":"application/json"},timeout=2)
                    assert oversized.status_code==413 and oversized.json()["detail"]["code"]=="size_limit"
                    def pieces():
                        yield b"x"*9000
                        yield b"y"*9000
                    chunked=requests.post(f"http://127.0.0.1:{port}/api/jobs",data=pieces(),headers={"content-type":"application/json"},timeout=2)
                    assert chunked.status_code==413 and chunked.json()["detail"]["code"]=="size_limit"
                    return
            except requests.RequestException:
                time.sleep(0.1)
        raise AssertionError("uvicorn did not serve /api/health")
    finally:
        proc.terminate()
        try: proc.wait(timeout=5)
        except subprocess.TimeoutExpired: proc.kill(); proc.wait(timeout=5)


def test_restricts_non_https_private_and_unknown_domains():
    for url, code in [("http://youtube.com/watch?v=x", "restricted"), ("https://127.0.0.1/video", "restricted"), ("https://example.org/video", "unsupported_site"), ("https://user:pw@youtube.com/watch?v=x", "restricted")]:
        try: main.safe_input(url)
        except ValueError as ex: assert str(ex) == code
        else: assert False, f"accepted unsupported URL {url}"


def test_inspect_logs_exception_class_without_input_or_error_text(monkeypatch, caplog):
    import logging
    import requests
    secret_url="https://cdn.example/audio?token=SECRET_TOKEN&sig=SECRET_SIGNATURE"
    def broken_metadata(*args,**kwargs): raise requests.RequestException(secret_url)
    monkeypatch.setattr(main,"metadata",broken_metadata)
    with caplog.at_level(logging.WARNING,logger="audiodock.upstream"):
        response=main.inspect(main.InputBody(input="https://youtu.be/abc?token=PRIVATE_INPUT"),request("198.51.100.240"))
    assert response.status_code==502
    logs="\n".join(record.getMessage() for record in caplog.records)
    assert "stage=inspect_upstream" in logs and "exception=RequestException" in logs
    assert "SECRET_TOKEN" not in logs and "SECRET_SIGNATURE" not in logs and "PRIVATE_INPUT" not in logs
    assert secret_url not in logs and "https://" not in logs


def test_parameter_rules_and_queue_capacity():
    clear_state()
    bad = main.create_job(main.JobBody(input="https://youtu.be/abc", format="mp3", bitrate=96), request())
    assert bad.status_code == 400
    assert __import__("json").loads(bad.body)["detail"]["code"] == "invalid_options"
    bad_opus=main.create_job(main.JobBody(input="https://youtu.be/abc",format="opus",bitrate=96,sample_rate=44100),request("198.51.100.52"))
    assert bad_opus.status_code==400 and __import__("json").loads(bad_opus.body)["detail"]["code"]=="invalid_options"
    for _ in range(5):
        main.work.put_nowait("occupied")
    full = main.create_job(main.JobBody(input="https://youtu.be/abc"), request())
    assert full.status_code == 429
    assert __import__("json").loads(full.body)["detail"]["code"] == "queue_full"
    clear_state()


def test_per_ip_limit_token_auth_cancel_and_expiry():
    clear_state(); req=request("198.51.100.51")
    made = [main.create_job(main.JobBody(input="https://youtu.be/abc"), req) for _ in range(3)]
    made = [x for x in made]
    made = [{"id":x["id"], "token":x["token"]} for x in made]
    limited = main.create_job(main.JobBody(input="https://youtu.be/abc"), req)
    assert limited.status_code == 429 and __import__("json").loads(limited.body)["detail"]["code"] == "rate_limited"
    item = made[0]
    assert main.get_job(item["id"], None).status_code == 404
    auth = f"Bearer {item['token']}"
    assert main.get_job(item["id"], auth)["status"] == "queued"
    assert main.cancel(item["id"], auth)["status"] == "cancelled"
    j = main.jobs[item["id"]]
    j.update(status="completed", stage="completed", result={"filename":"a.mp3"}, expires_at=(datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat())
    expired = main.get_job(item["id"], auth)
    assert expired["status"] == "expired" and expired["result"] is None
    clear_state()


def test_cancelled_queued_job_releases_queue_slot():
    clear_state()
    created=[main.create_job(main.JobBody(input="https://youtu.be/abc"),request(f"198.51.100.{60+i}")) for i in range(5)]
    first=created[0]
    assert main.cancel(first["id"],f"Bearer {first['token']}")["status"]=="cancelled"
    next_job=main.create_job(main.JobBody(input="https://youtu.be/abc"),request("198.51.100.70"))
    assert next_job["status"]=="queued"
    assert main.work.qsize()==5
    clear_state()


def test_download_http_response_preserves_full_file_bytes(tmp_path, monkeypatch):
    import asyncio
    from urllib.parse import urlencode
    clear_state()
    monkeypatch.setattr(main,"DATA_DIR",tmp_path)
    jid="download-bytes"
    token="download-token"
    payload=bytes(range(256))*4096
    job_dir=tmp_path/"jobs"/jid
    job_dir.mkdir(parents=True)
    (job_dir/"audio.mp3").write_bytes(payload)
    main.jobs[jid]={"id":jid,"token_hash":hashlib.sha256(token.encode()).hexdigest(),"status":"completed","stage":"completed","keep_original":False,"result":{"filename":"audio.mp3"},"expires_at":None}
    async def exercise():
        sent=[]
        request_sent=False
        disconnected=False
        receive_waiting=asyncio.Event()
        disconnect=asyncio.Event()
        async def send(message): sent.append(message)
        async def receive():
            nonlocal request_sent,disconnected
            if not request_sent:
                request_sent=True
                return {"type":"http.request","body":b"","more_body":False}
            receive_waiting.set()
            await disconnect.wait()
            disconnected=True
            return {"type":"http.disconnect"}
        path=f"/api/jobs/{jid}/download"
        scope={"type":"http","asgi":{"version":"3.0","spec_version":"2.3"},"http_version":"1.1","method":"GET","scheme":"http","path":path,"raw_path":path.encode(),"root_path":"","query_string":urlencode({"token":token}).encode(),"headers":[(b"host",b"test")],"client":("127.0.0.1",12345),"server":("127.0.0.1",80)}
        app_task=asyncio.create_task(main.app(scope,receive,send))
        try:
            await asyncio.wait_for(receive_waiting.wait(),2)
            await asyncio.wait_for(app_task,5)
        finally:
            disconnect.set()
            if not app_task.done(): await app_task
        assert sent[0]["status"]==200
        streamed=b"".join(event.get("body",b"") for event in sent[1:])
        assert len(streamed)==1024*1024
        assert hashlib.sha256(streamed).digest()==hashlib.sha256(payload).digest()
        assert not disconnected
    asyncio.run(exercise())
    clear_state()


def test_bearer_hash_is_not_stored_as_plaintext():
    clear_state(); token = secrets.token_urlsafe(32)
    jid = "test"
    main.jobs[jid] = {"id":jid,"token_hash":hashlib.sha256(token.encode()).hexdigest(),"status":"queued","stage":"queued"}
    assert main.get_job(jid, f"Bearer {token}")["status"] == "queued"
    assert token not in repr(main.jobs[jid])
    main.jobs.pop(jid)
