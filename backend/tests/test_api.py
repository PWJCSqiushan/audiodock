import hashlib
import secrets
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


def test_restricts_non_https_private_and_unknown_domains():
    for url, code in [("http://youtube.com/watch?v=x", "restricted"), ("https://127.0.0.1/video", "restricted"), ("https://example.org/video", "unsupported_site"), ("https://user:pw@youtube.com/watch?v=x", "restricted")]:
        try: main.safe_input(url)
        except ValueError as ex: assert str(ex) == code
        else: assert False, f"accepted unsupported URL {url}"


def test_parameter_rules_and_queue_capacity():
    clear_state()
    bad = main.create_job(main.JobBody(input="https://youtu.be/abc", format="mp3", bitrate=96), request())
    assert bad.status_code == 400
    assert __import__("json").loads(bad.body)["detail"]["code"] == "invalid_options"
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


def test_bearer_hash_is_not_stored_as_plaintext():
    clear_state(); token = secrets.token_urlsafe(32)
    jid = "test"
    main.jobs[jid] = {"id":jid,"token_hash":hashlib.sha256(token.encode()).hexdigest(),"status":"queued","stage":"queued"}
    assert main.get_job(jid, f"Bearer {token}")["status"] == "queued"
    assert token not in repr(main.jobs[jid])
    main.jobs.pop(jid)
