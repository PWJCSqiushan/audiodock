from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import queue
import re
import secrets
import shutil
import socket
import subprocess
import sys
import threading
from contextlib import contextmanager
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urljoin

import requests
import yt_dlp
from fastapi import FastAPI, Header, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, Field

VERSION = "0.1.0"
MAX_DURATION = 3600
MAX_SOURCE = 268435456
MAX_OUTPUT = 786432000
MAX_REQUEST_BODY = 16 * 1024
QUEUE_SIZE = 5
TTL = 1800
JOB_TIMEOUT = 90 * 60
SITE_NAMES = {"bilibili": "哔哩哔哩", "youtube": "YouTube", "vimeo": "Vimeo"}
FORMATS = ["mp3", "m4a", "opus", "flac", "wav", "original"]
ALLOWED_HOSTS = {"bilibili.com", "www.bilibili.com", "m.bilibili.com", "b23.tv", "youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be", "vimeo.com", "www.vimeo.com", "player.vimeo.com"}
DATA_DIR = Path(os.environ.get("AUDIODOCK_RUNTIME_DIR", os.environ.get("AUDIODOCK_DATA_DIR", Path.cwd() / "runtime"))).resolve()
RECYCLE_DIR = DATA_DIR / "recycle"
STORAGE_CAP = int(os.environ.get("AUDIODOCK_STORAGE_CAP_BYTES", str(2 * 1024**3)))
app = FastAPI(title="AudioDock API", version=VERSION)

class RequestBodyLimit:
    def __init__(self, app, limit: int): self.app=app; self.limit=limit
    async def __call__(self, scope, receive, send):
        if scope["type"]!="http": return await self.app(scope,receive,send)
        headers={k.lower():v for k,v in scope.get("headers",[])}
        content_length=headers.get(b"content-length")
        if content_length:
            try: too_large=int(content_length)>self.limit
            except ValueError: too_large=True
            if too_large:
                body=b'{"detail":{"code":"size_limit","message":"\xe8\xaf\xb7\xe6\xb1\x82\xe5\x86\x85\xe5\xae\xb9\xe8\xbf\x87\xe5\xa4\xa7"}}'
                await send({"type":"http.response.start","status":413,"headers":[(b"content-type",b"application/json"),(b"content-length",str(len(body)).encode())]})
                await send({"type":"http.response.body","body":body})
                return
        messages=[]; size=0
        while True:
            message=await receive()
            if message["type"]!="http.request": break
            size+=len(message.get("body",b""))
            if size>self.limit:
                body=b'{"detail":{"code":"size_limit","message":"\xe8\xaf\xb7\xe6\xb1\x82\xe5\x86\x85\xe5\xae\xb9\xe8\xbf\x87\xe5\xa4\xa7"}}'
                await send({"type":"http.response.start","status":413,"headers":[(b"content-type",b"application/json"),(b"content-length",str(len(body)).encode())]})
                await send({"type":"http.response.body","body":body})
                return
            messages.append(message)
            if not message.get("more_body",False): break
        index=0
        async def replay():
            nonlocal index
            if index<len(messages):
                current=messages[index]; index+=1; return current
            return {"type":"http.disconnect"}
        await self.app(scope,replay,send)

app.add_middleware(RequestBodyLimit,limit=MAX_REQUEST_BODY)
origins = {x.strip().rstrip("/") for x in os.environ.get("AUDIODOCK_ALLOWED_ORIGINS", "").split(",") if x.strip()}
origins.update({"http://localhost:5173", "http://127.0.0.1:5173"})
app.add_middleware(CORSMiddleware, allow_origins=sorted(origins), allow_methods=["GET", "POST"], allow_headers=["Authorization", "Content-Type"])

@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError):
    return fail("invalid_options", "请求参数无效")

class InputBody(BaseModel):
    input: str = Field(min_length=1, max_length=4096)

class JobBody(InputBody):
    format: str = "mp3"
    bitrate: int = 320
    sample_rate: int = 0
    channels: int = 0
    keep_original: bool = False

jobs: dict[str, dict[str, Any]] = {}
rate_hits: dict[str, list[float]] = {}
lock = threading.RLock()
work: queue.Queue[str] = queue.Queue(maxsize=QUEUE_SIZE)
worker_started = False
PROCESS: subprocess.Popen | None = None
PROCESS_JOB: str | None = None
inspect_semaphore = threading.BoundedSemaphore(4)
inspect_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="audiodock-inspect")
inspect_hits: dict[str,list[float]] = {}

_ALLOWED_NETWORK_SUFFIXES=("bilibili.com","b23.tv","bilivideo.com","bilivideo.cn","hdslb.com","youtube.com","youtu.be","googlevideo.com","youtubei.googleapis.com","googleapis.com","ytimg.com","ggpht.com","vimeo.com","vimeocdn.com","akamaized.net","cloudfront.net")
_network_context = threading.local()

@contextmanager
def _guarded_network():
    depth=getattr(_network_context,"depth",0)
    _network_context.depth=depth+1
    try: yield
    finally: _network_context.depth=depth

def _guard_network(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        with _guarded_network(): return fn(*args, **kwargs)
    return wrapped

def _is_public_ip(value: str) -> bool:
    try: ip=ipaddress.ip_address(value)
    except ValueError: return False
    if getattr(ip,"scope_id",None): return False
    if isinstance(ip,ipaddress.IPv6Address):
        if ip.ipv4_mapped: return _is_public_ip(str(ip.ipv4_mapped))
        if ip in ipaddress.ip_network("64:ff9b::/96") or ip in ipaddress.ip_network("64:ff9b:1::/48"): return False
    return ip.is_global and not ip.is_reserved and not ip.is_private and not ip.is_loopback and not ip.is_link_local and not ip.is_multicast and not ip.is_unspecified

def _socket_guard(event: str, args: tuple[Any,...]) -> None:
    if not getattr(_network_context,"depth",0): return
    if event == "socket.getaddrinfo":
        host,port=args[0],args[1]
        try: ip=ipaddress.ip_address(str(host))
        except ValueError:
            name=str(host).lower().rstrip(".")
            if not any(name==suffix or name.endswith("."+suffix) for suffix in _ALLOWED_NETWORK_SUFFIXES):
                raise PermissionError("outbound host is not allowlisted")
            if port not in (None,443,"https"): raise PermissionError("outbound port is not allowed")
        else:
            if not _is_public_ip(str(ip)) or port not in (None,443,"https"): raise PermissionError("outbound address is not allowed")
    elif event in {"socket.connect","socket.connect_ex"}:
        address=args[1]
        if isinstance(address,tuple) and len(address)>=2:
            if not _is_public_ip(str(address[0])) or address[1]!=443: raise PermissionError("connection target is not allowed")

sys.addaudithook(_socket_guard)

def fail(code: str, message: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"detail": {"code": code, "message": message}}, status_code=status)

def safe_input(value: str) -> tuple[str, str, str]:
    raw = value.strip()
    # Normalize common share text while never accepting arbitrary redirect hosts.
    match = re.search(r"https://[^\s<>\"']+", raw, flags=re.I)
    insecure = re.search(r"http://[^\s<>\"']+", raw, flags=re.I)
    if not match:
        m = re.search(r"(?:BV[0-9A-Za-z]{10}|av\d+|https?://[^\s]+)", raw, flags=re.I)
        if m and m.group(0).upper().startswith(("BV", "AV")):
            raw = "https://www.bilibili.com/video/" + m.group(0)
        elif insecure:
            raw = insecure.group(0).rstrip(".,，。)】")
        else:
            raise ValueError("invalid_url")
    else:
        raw = match.group(0).rstrip(".,，。)】")
    p = urlparse(raw)
    try: port=p.port
    except ValueError: raise ValueError("restricted")
    if p.scheme.lower() != "https" or not p.hostname or p.username or p.password or port not in (None, 443):
        raise ValueError("restricted")
    host = p.hostname.lower().rstrip(".")
    try:
        ip = ipaddress.ip_address(host)
        if not _is_public_ip(str(ip)): raise ValueError("restricted")
    except ValueError as ex:
        if str(ex) == "restricted": raise
    if host not in ALLOWED_HOSTS:
        raise ValueError("unsupported_site")
    if p.fragment: raw = raw.split("#", 1)[0]
    site = "bilibili" if host.endswith("bilibili.com") or host == "b23.tv" else ("youtube" if host.endswith("youtube.com") or host == "youtu.be" else "vimeo")
    # Reject common internal host spellings; all actual network resolution also checks public IPs.
    return raw, site, host

def public_host(host: str) -> bool:
    try:
        infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        return bool(infos) and all(_is_public_ip(row[4][0]) for row in infos)
    except (OSError, ValueError):
        return False

def client_ip(request: Request) -> str:
    ip=request.client.host if request.client else "unknown"
    if os.environ.get("AUDIODOCK_TRUST_PROXY", "false").lower()=="true":
        forwarded=request.headers.get("x-forwarded-for", "").split(",")
        candidate=forwarded[-1].strip() if forwarded else ""
        try:
            parsed=ipaddress.ip_address(candidate)
            if parsed.is_global: ip=str(parsed)
        except ValueError: pass
    return ip

class GuardedSession(requests.Session):
    def __init__(self):
        super().__init__()
        self.trust_env=False
    def request(self, method: str, url: str, **kwargs: Any):
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower().rstrip(".")
        if parsed.scheme != "https" or not host or not public_host(host):
            raise requests.RequestException("restricted target")
        kwargs["allow_redirects"] = False
        return super().request(method, url, **kwargs)

@_guard_network
def metadata(url: str, site: str, *, internal: bool = False) -> dict[str, Any]:
    if site == "bilibili":
        video_id = _bvid(url)
        if not video_id:
            raise RuntimeError("upstream_unavailable")
        s = GuardedSession(); s.headers.update({"User-Agent": "Mozilla/5.0 AudioDock/0.1", "Referer": url})
        endpoint = "https://api.bilibili.com/x/web-interface/wbi/view/detail"
        resp = s.get(endpoint, params={"bvid": video_id, "platform": "web"}, timeout=15)
        if resp.status_code != 200:
            raise RuntimeError("upstream_unavailable")
        view = resp.json().get("data", {}).get("View")
        if not view:
            raise RuntimeError("upstream_unavailable")
        result = {"url": url, "site": site, "id": video_id, "title": str(view.get("title", ""))[:300], "uploader": str(view.get("owner", {}).get("name", ""))[:100], "duration": int(view.get("duration", 0)), "thumbnail": view.get("pic")}
        if internal: result["_cid"] = view.get("cid")
        return result
    if not public_host(urlparse(url).hostname or ""): raise RuntimeError("upstream_unavailable")
    opts = {"quiet": True, "no_warnings": True, "skip_download": True, "noplaylist": True, "ignoreconfig":True, "socket_timeout":15, "retries":1, "fragment_retries":1, "proxy":"", "extractor_args": {"youtube": {"player_client": ["web"]}}}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    return {"url": url, "site": site, "id": str(info.get("id", "")), "title": str(info.get("title", ""))[:300], "uploader": str(info.get("uploader") or info.get("channel") or "")[:100], "duration": int(info["duration"]) if info.get("duration") is not None else None, "thumbnail": info.get("thumbnail")}

def _bvid(url: str, hops: int = 0) -> str | None:
    p = urlparse(url)
    m = re.search(r"/video/(BV[0-9A-Za-z]{10}|av\d+)", p.path, re.I)
    if m: return m.group(1)
    if p.hostname == "b23.tv" and hops < 3:
        # Short URL resolution is handled without following arbitrary redirects.
        s = GuardedSession(); r = s.get(url, timeout=10)
        loc = r.headers.get("Location", "")
        target = urljoin(url, loc)
        if r.is_redirect and urlparse(target).hostname in ALLOWED_HOSTS and urlparse(target).scheme == "https":
            return _bvid(target,hops+1)
    return None

def options_ok(body: JobBody) -> str | None:
    allowed = {"mp3": {128,192,256,320}, "m4a": {128,192,256}, "opus": {64,96,128,160}, "flac": {0}, "wav": {0}, "original": {0}}
    if body.format not in allowed or body.bitrate not in allowed.get(body.format, set()): return "invalid_options"
    if body.sample_rate not in (0, 44100, 48000) or body.channels not in (0,1,2): return "invalid_options"
    if body.format == "opus" and body.sample_rate == 44100: return "invalid_options"
    if body.format == "original" and (body.sample_rate or body.channels): return "invalid_options"
    return None

def _timestamp() -> str: return datetime.now(timezone.utc).isoformat()
def _auth(job_id: str, authorization: str | None) -> dict[str, Any] | None:
    j = jobs.get(job_id)
    if not j or not authorization or not authorization.startswith("Bearer "): return None
    if not secrets.compare_digest(hashlib.sha256(authorization[7:].encode()).hexdigest(), j["token_hash"]): return None
    return j

def _queue_position(job_id: str) -> int | None:
    with work.mutex:
        try: return list(work.queue).index(job_id) + 1
        except ValueError: return None

def _remove_queued(job_id: str) -> bool:
    with work.mutex:
        try: work.queue.remove(job_id)
        except ValueError: return False
        work.unfinished_tasks-=1
        if work.unfinished_tasks==0: work.all_tasks_done.notify_all()
        work.not_full.notify()
        return True

def _public_job(j: dict[str, Any]) -> dict[str, Any]:
    pos = _queue_position(j["id"]) if j["status"] == "queued" else None
    return {k: j.get(k) for k in ("id","status","stage","progress","title","message","created_at","expires_at","result")} | {"queue_position": pos}

def _storage_used() -> int:
    if not DATA_DIR.exists(): return 0
    total=0
    for path in DATA_DIR.rglob("*"):
        try:
            info=path.stat()
            if path.is_file(): total+=info.st_size
        except (OSError, RuntimeError):
            continue
    return total

def _jobdir(jid: str) -> Path:
    p = (DATA_DIR / "jobs" / jid).resolve()
    if not p.is_relative_to((DATA_DIR / "jobs").resolve()): raise RuntimeError("processing_failed")
    p.mkdir(parents=True, exist_ok=True)
    return p

def _move_recycle(j: dict[str, Any]) -> None:
    src = DATA_DIR / "jobs" / j["id"]
    if src.exists():
        RECYCLE_DIR.mkdir(parents=True, exist_ok=True)
        dst = RECYCLE_DIR / f"{j['id']}-{int(time.time())}-{secrets.token_hex(3)}"
        try: src.rename(dst)
        except OSError: pass

def _recycle_file(path: Path, label: str) -> None:
    if not path.exists(): return
    RECYCLE_DIR.mkdir(parents=True, exist_ok=True)
    try: path.replace(RECYCLE_DIR / f"{label}-{int(time.time())}-{secrets.token_hex(3)}-{path.name}")
    except OSError: pass

def _ffprobe(path: Path) -> dict[str, Any]:
    p = subprocess.run(["ffprobe", "-v", "error", "-protocol_whitelist", "file,pipe", "-select_streams", "a:0", "-show_entries", "format=duration:format_tags=encoder:stream=codec_name,bit_rate,sample_rate,channels", "-of", "json", str(path)], capture_output=True, text=True, timeout=30)
    if p.returncode: raise RuntimeError("processing_failed")
    data = json.loads(p.stdout); st = next((x for x in data.get("streams", []) if "codec_name" in x), {})
    return {"duration": float(data.get("format", {}).get("duration", 0)), "codec": st.get("codec_name"), "bit_rate": int(st["bit_rate"]) if st.get("bit_rate", "").isdigit() else None, "sample_rate": int(st["sample_rate"]) if st.get("sample_rate", "").isdigit() else None, "channels": st.get("channels")}

def _stop_process(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        try: proc.terminate()
        except OSError: pass
        try: proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try: proc.kill()
            except OSError: pass
            try: proc.wait(timeout=5)
            except subprocess.TimeoutExpired: pass

def _bili_download(url: str, dest: Path, j: dict[str, Any], meta: dict[str, Any]) -> tuple[dict[str, Any], str]:
    bv = meta["id"]
    s = GuardedSession(); s.headers.update({"User-Agent": "Mozilla/5.0", "Referer": url})
    cid = meta.get("_cid")
    if not cid: raise RuntimeError("upstream_unavailable")
    r = s.get("https://api.bilibili.com/x/player/playurl", params={"bvid":bv,"cid":cid,"fnval":4048,"qn":127,"fourk":1}, timeout=15)
    if r.status_code != 200: raise RuntimeError("upstream_unavailable")
    dash = r.json().get("data",{}).get("dash",{}); audios=dash.get("audio",[])
    if not audios: raise RuntimeError("upstream_unavailable")
    audio=max(audios,key=lambda x:int(x.get("bandwidth",0)))
    codecs=str(audio.get("codecs", "")).lower(); mime=str(audio.get("mimeType",audio.get("mime_type", ""))).lower()
    if "flac" in codecs or "flac" in mime: meta["_ext"]="flac"
    elif "webm" in mime: meta["_ext"]="webm"
    elif "mpeg" in mime or "mp3" in codecs: meta["_ext"]="mp3"
    else: meta["_ext"]="m4a"
    urls=[audio.get("baseUrl") or audio.get("base_url")]+audio.get("backupUrl",audio.get("backup_url",[])); urls=[x for x in urls if x]
    last=None
    source_budget=min(MAX_SOURCE,STORAGE_CAP-_storage_used())
    if source_budget<=0: raise RuntimeError("storage_full")
    for audio_url in urls:
        cdn_host=(urlparse(audio_url).hostname or "").lower()
        if not cdn_host.endswith((".bilivideo.com", ".bilivideo.cn", ".hdslb.com")) or not public_host(cdn_host): continue
        offset=0; failures=0
        while failures < 4:
            if j.get("cancel_requested"): raise RuntimeError("cancelled")
            if time.time() > j["deadline"]: raise RuntimeError("processing_failed")
            with dest.open("ab" if offset else "wb") as f:
                end=offset+2*1024*1024-1
                try:
                    resp=s.get(audio_url,headers={"Range":f"bytes={offset}-{end}"},stream=True,timeout=(15,30))
                    cr=resp.headers.get("Content-Range", ""); cm=re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)",cr)
                    if resp.status_code!=206 or not cm or int(cm.group(1))!=offset:
                        resp.close(); raise RuntimeError("upstream_unavailable")
                    range_end,total=int(cm.group(2)),int(cm.group(3))
                    if total>MAX_SOURCE: resp.close(); raise RuntimeError("size_limit")
                    if total>source_budget: resp.close(); raise RuntimeError("storage_full")
                    if range_end<offset or range_end-offset>=2*1024*1024:
                        resp.close(); raise RuntimeError("upstream_unavailable")
                    got=0
                    for chunk in resp.iter_content(65536):
                        if j.get("cancel_requested"): resp.close(); raise RuntimeError("cancelled")
                        if time.time()>j["deadline"]: resp.close(); raise RuntimeError("processing_failed")
                        if not chunk: continue
                        got+=len(chunk)
                        if offset+got>MAX_SOURCE: resp.close(); raise RuntimeError("size_limit")
                        if _storage_used()+len(chunk)>STORAGE_CAP: resp.close(); raise RuntimeError("storage_full")
                        f.write(chunk)
                    resp.close()
                    if got!=range_end-offset+1: raise requests.RequestException("truncated range")
                    offset+=got; failures=0
                    j["progress"]=int(offset*100/total)
                    if offset>=total: return meta,"audio"
                except RuntimeError as e:
                    f.flush(); f.seek(offset); f.truncate()
                    if str(e) in {"cancelled", "size_limit"}: raise
                    last=e; failures+=1
                except Exception as e:
                    f.flush(); f.seek(offset); f.truncate(); failures+=1; last=e
        _recycle_file(dest,f"{j['id']}-partial")
    raise RuntimeError("upstream_unavailable") from last

@_guard_network
def _download_source(j: dict[str, Any], src: Path, preflight: dict[str, Any]) -> tuple[dict[str, Any], Path]:
    url,site,_=safe_input(j["input"])
    if site == "bilibili":
        meta = dict(preflight)
        source = src.parent / "source.m4a"
        j["stage"]="downloading"; j["progress"]=None
        meta, _ = _bili_download(url,source,j,meta)
        meta.setdefault("_ext", "m4a")
        return meta, source
    j["stage"]="downloading"; j["progress"]=None
    def progress_hook(d):
        if d.get("status") != "downloading": return
        if j.get("cancel_requested"): raise yt_dlp.utils.DownloadError("cancelled")
        if time.time() > j["deadline"]: raise yt_dlp.utils.DownloadError("processing deadline exceeded")
        downloaded=int(d.get("downloaded_bytes",0))
        if downloaded>MAX_SOURCE: raise yt_dlp.utils.DownloadError("source limit exceeded")
        if _storage_used()>STORAGE_CAP: raise yt_dlp.utils.DownloadError("storage capacity reached")
        total=d.get("total_bytes") or d.get("total_bytes_estimate") or MAX_SOURCE
        j["progress"]=max(0,min(99,int(downloaded*100/max(int(total),1))))
    source_budget=min(MAX_SOURCE,STORAGE_CAP-_storage_used())
    if source_budget<=0: raise RuntimeError("storage_full")
    opts={"format":"bestaudio[protocol=https]","outtmpl":str(src.parent/"source.%(ext)s"),"noplaylist":True,"ignoreconfig":True,"quiet":True,"no_warnings":True,"socket_timeout":15,"retries":1,"fragment_retries":1,"proxy":"","max_filesize":source_budget,"restrictfilenames":True,"hls_prefer_native":True,"progress_hooks":[progress_hook]}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info=ydl.extract_info(url,download=False)
            direct_audio=[f for f in info.get("formats",[]) if f.get("acodec") not in (None,"none") and f.get("vcodec")=="none" and f.get("protocol")=="https"]
            if not direct_audio: raise RuntimeError("upstream_unavailable")
            info=ydl.process_ie_result(info,download=True)
    except yt_dlp.utils.DownloadError as e:
        error=str(e).lower()
        if "cancelled" in error: raise RuntimeError("cancelled") from e
        if "source limit" in error: raise RuntimeError("size_limit") from e
        if "storage capacity" in error: raise RuntimeError("storage_full") from e
        raise RuntimeError("upstream_unavailable") from e
    candidates=list(src.parent.glob("source.*"));
    if not candidates: raise RuntimeError("upstream_unavailable")
    source=candidates[0]
    if source.stat().st_size>MAX_SOURCE: raise RuntimeError("size_limit")
    return {"url":url,"site":site,"id":str(info.get("id","")),"title":str(info.get("title",""))[:300],"uploader":str(info.get("uploader") or "")[:100],"duration":info.get("duration"),"thumbnail":info.get("thumbnail"),"_ext":str(info.get("ext") or source.suffix.lstrip("."))},source

def _run_job(jid: str) -> None:
    global PROCESS, PROCESS_JOB
    j=jobs[jid]; base=_jobdir(jid); src=base/"source.bin"; out=None
    try:
        if j.get("cancel_requested"): raise RuntimeError("cancelled")
        j.update(status="running",stage="resolving",progress=None,message="正在读取公开媒体信息",deadline=time.time()+JOB_TIMEOUT)
        url,site,_=safe_input(j["input"])
        if not public_host(urlparse(url).hostname or ""): raise RuntimeError("restricted")
        preflight=metadata(url,site,internal=(site=="bilibili"))
        duration=int(preflight.get("duration") or 0)
        if duration>MAX_DURATION: raise RuntimeError("duration_limit")
        if not duration: raise RuntimeError("upstream_unavailable")
        j["title"]=preflight.get("title", "")
        meta,src=_download_source(j,src,preflight); title=meta["title"]
        j["source_filename"]=src.name
        if src.stat().st_size>MAX_SOURCE: raise RuntimeError("size_limit")
        original_ext = meta.get("_ext", "m4a")
        if not re.fullmatch(r"[a-z0-9]{1,8}", original_ext): original_ext = "m4a"
        extension = original_ext if j["format"] == "original" else {"mp3":"mp3","m4a":"m4a","opus":"opus","flac":"flac","wav":"wav"}[j["format"]]
        out=base/("audio."+extension)
        j.update(title=title,stage="converting",progress=None,message="正在转换音频")
        if j["format"]=="original":
            copied=0
            with src.open("rb") as input_file, out.open("wb") as output_file:
                for chunk in iter(lambda:input_file.read(1024*1024),b""):
                    if j.get("cancel_requested"): raise RuntimeError("cancelled")
                    if time.time()>j["deadline"]: raise RuntimeError("processing_failed")
                    copied+=len(chunk)
                    if copied>MAX_OUTPUT: raise RuntimeError("size_limit")
                    if _storage_used()+len(chunk)>STORAGE_CAP: raise RuntimeError("storage_full")
                    output_file.write(chunk)
        else:
            codec={"mp3":"libmp3lame","m4a":"aac","opus":"libopus","flac":"flac","wav":"pcm_s16le"}[j["format"]]
            args=["ffmpeg","-nostdin","-hide_banner","-loglevel","error","-y","-protocol_whitelist","file,pipe","-i",str(src),"-map","0:a:0","-vn"]
            if j["channels"]: args += ["-ac",str(j["channels"])]
            if j["sample_rate"]: args += ["-ar",str(j["sample_rate"])]
            if j["format"] in ("mp3","m4a","opus"): args += ["-c:a",codec,"-b:a",f"{j['bitrate']}k"]
            else: args += ["-c:a",codec]
            output_budget=min(MAX_OUTPUT,STORAGE_CAP-_storage_used())
            if output_budget<=0: raise RuntimeError("storage_full")
            args += ["-fs",str(output_budget)]
            args += [str(out)]
            proc=subprocess.Popen(args,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            with lock: PROCESS=proc; PROCESS_JOB=jid
            try:
                while proc.poll() is None:
                    if j.get("cancel_requested"): raise RuntimeError("cancelled")
                    if time.time()>j["deadline"]: raise RuntimeError("processing_failed")
                    if out.exists() and out.stat().st_size>MAX_OUTPUT: raise RuntimeError("size_limit")
                    if _storage_used()>STORAGE_CAP: raise RuntimeError("storage_full")
                    time.sleep(.2)
            finally:
                _stop_process(proc)
                with lock: PROCESS=None; PROCESS_JOB=None
            if proc.returncode: raise RuntimeError("processing_failed")
        if out.stat().st_size>MAX_OUTPUT: raise RuntimeError("size_limit")
        j.update(stage="verifying",progress=None,message="正在校验完整音频")
        decode=subprocess.Popen(["ffmpeg","-nostdin","-v","error","-xerror","-protocol_whitelist","file,pipe","-i",str(out),"-map","0:a:0","-f","null","-"],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        verify_started=time.time()
        try:
            while decode.poll() is None:
                if j.get("cancel_requested"): raise RuntimeError("cancelled")
                if time.time()>j["deadline"] or time.time()-verify_started>120: raise RuntimeError("processing_failed")
                time.sleep(.2)
        finally: _stop_process(decode)
        probe=_ffprobe(out)
        if decode.returncode or probe["duration"]<=0 or probe["duration"]>MAX_DURATION or abs(probe["duration"]-duration)>max(3.0,duration*0.01): raise RuntimeError("processing_failed")
        digest=hashlib.sha256()
        with out.open("rb") as stream:
            for chunk in iter(lambda:stream.read(1024*1024),b""):
                if j.get("cancel_requested"): raise RuntimeError("cancelled")
                digest.update(chunk)
        result={"filename": re.sub(r"[\\/:*?\"<>|\r\n\x00-\x1f]+","_",(title or "audio"))[:100]+"."+out.suffix.lstrip("."),"bytes":out.stat().st_size,"duration":probe["duration"],"codec":probe["codec"],"bit_rate":probe["bit_rate"],"sample_rate":probe["sample_rate"],"channels":probe["channels"],"sha256":digest.hexdigest(),"source_available":j["keep_original"]}
        if not j["keep_original"]:
            RECYCLE_DIR.mkdir(parents=True, exist_ok=True)
            try: src.replace(RECYCLE_DIR / f"{jid}-source-{int(time.time())}-{src.name}")
            except OSError: pass
        else: j["source_filename"]=src.name
        j.update(status="completed",stage="completed",progress=100,message="转换完成",result=result,source_available=j["keep_original"],expires_at=datetime.fromtimestamp(time.time()+TTL,timezone.utc).isoformat())
    except Exception as e:
        msg=str(e)
        code="cancelled" if msg=="cancelled" or "cancelled" in msg else ("duration_limit" if msg=="duration_limit" else ("size_limit" if msg=="size_limit" or "source limit exceeded" in msg else ("queue_full" if msg=="storage_full" or "storage capacity" in msg else ("upstream_unavailable" if msg=="upstream_unavailable" else "processing_failed"))))
        if code=="cancelled": status="cancelled"; stage="cancelled"
        else: status="failed"; stage="failed"
        j.update(status=status,stage=stage,progress=None,message={"cancelled":"任务已取消","duration_limit":"视频超过 60 分钟限制","size_limit":"音频文件超过大小限制","queue_full":"服务存储空间繁忙，请稍后重试","upstream_unavailable":"暂时无法读取该视频，请稍后重试","processing_failed":"音频处理失败，请尝试其他格式"}.get(code,"音频处理失败，请稍后重试"),error_code=code)
        _move_recycle({"id":jid})

def _worker() -> None:
    while True:
        _maintenance()
        try: jid=work.get(timeout=15)
        except queue.Empty: continue
        try: _run_job(jid)
        finally: work.task_done()

def _maintenance() -> None:
    now=time.time()
    for store,age in ((rate_hits,3600),(inspect_hits,60)):
        for ip,hits in list(store.items()):
            fresh=[stamp for stamp in hits if now-stamp<age]
            if fresh: store[ip]=fresh
            else: store.pop(ip,None)
    for jid,j in list(jobs.items()):
        expiry=j.get("expires_at")
        if j.get("status")=="completed" and expiry and datetime.fromisoformat(expiry).timestamp()<=now:
            j.update(status="expired",stage="expired",message="下载已过期",result=None)
            _move_recycle(j)
        created=j.get("created_epoch",now)
        if j.get("status") in {"expired","failed","cancelled"} and now-created>86400:
            jobs.pop(jid,None)

@app.on_event("startup")
def startup():
    global worker_started
    DATA_DIR.mkdir(parents=True,exist_ok=True); RECYCLE_DIR.mkdir(parents=True,exist_ok=True)
    if not worker_started:
        threading.Thread(target=_worker,name="audiodock-worker",daemon=True).start(); worker_started=True

@app.get("/api/health")
def health():
    return {"status":"ok","version":VERSION,"ffmpeg_available":shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None,"limits":{"max_duration_seconds":MAX_DURATION,"max_source_bytes":MAX_SOURCE,"max_output_bytes":MAX_OUTPUT,"max_queue":QUEUE_SIZE,"jobs_per_hour":3},"sites":[{"id":"bilibili","name":"哔哩哔哩","status":"available"}],"formats":FORMATS}

@app.post("/api/inspect")
def inspect(body: InputBody, request: Request):
    try: url,site,_=safe_input(body.input)
    except ValueError as e: return fail(str(e),"请输入受支持的 HTTPS 视频链接")
    ip=client_ip(request); now=time.time()
    with lock:
        hits=[stamp for stamp in inspect_hits.get(ip,[]) if now-stamp<60]
        if len(hits)>=10: return fail("rate_limited","查询过于频繁，请稍后再试",429)
        hits.append(now); inspect_hits[ip]=hits
    if not inspect_semaphore.acquire(timeout=0.1): return fail("upstream_unavailable","查询繁忙，请稍后重试",503)
    released=True
    future=inspect_executor.submit(metadata,url,site)
    try: return future.result(timeout=25)
    except FutureTimeout:
        released=False; future.add_done_callback(lambda _: inspect_semaphore.release())
        return fail("upstream_unavailable","读取视频信息超时，请稍后重试",504)
    except Exception: return fail("upstream_unavailable","暂时无法读取视频信息，请稍后重试",502)
    finally:
        if released: inspect_semaphore.release()

@app.post("/api/jobs")
def create_job(body: JobBody, request: Request):
    bad=options_ok(body)
    if bad: return fail(bad,"音频格式参数无效")
    try: url,site,_=safe_input(body.input)
    except ValueError as e: return fail(str(e),"请输入受支持的 HTTPS 视频链接")
    ip=client_ip(request)
    now=time.time()
    with lock:
        hits=[t for t in rate_hits.get(ip,[]) if now-t<3600]
        if len(hits)>=3: return fail("rate_limited","每个 IP 每小时最多提交 3 个任务",429)
        if work.full(): return fail("queue_full","当前排队已满，请稍后再试",429)
        if _storage_used()>=STORAGE_CAP: return fail("queue_full","服务存储空间繁忙，请稍后重试",503)
        hits.append(now); rate_hits[ip]=hits
        jid=uuid.uuid4().hex; token=secrets.token_urlsafe(32)
        job={"id":jid,"token_hash":hashlib.sha256(token.encode()).hexdigest(),"input":url,"site":site,"format":body.format,"bitrate":body.bitrate,"sample_rate":body.sample_rate,"channels":body.channels,"keep_original":body.keep_original,"status":"queued","stage":"queued","progress":0,"title":"","message":"等待处理","created_at":_timestamp(),"created_epoch":now,"expires_at":None,"result":None}
        jobs[jid]=job
        try: work.put_nowait(jid)
        except queue.Full: jobs.pop(jid,None); return fail("queue_full","当前排队已满，请稍后再试",429)
    return {"id":jid,"token":token,"status":"queued"}

@app.get("/api/jobs/{job_id}")
def get_job(job_id: str, authorization: str | None = Header(default=None)):
    j=_auth(job_id,authorization)
    if not j: return fail("restricted","任务不存在或访问令牌无效",404)
    if j.get("expires_at") and datetime.fromisoformat(j["expires_at"]).timestamp()<=time.time() and j["status"]=="completed":
        j.update(status="expired",stage="expired",message="下载已过期",result=None); _move_recycle(j)
    _maintenance()
    return _public_job(j)

@app.post("/api/jobs/{job_id}/cancel")
def cancel(job_id: str, authorization: str | None = Header(default=None)):
    global PROCESS
    j=_auth(job_id,authorization)
    if not j: return fail("restricted","任务不存在或访问令牌无效",404)
    if j["status"] in ("completed","failed","cancelled","expired"): return {"id":job_id,"status":j["status"]}
    j["cancel_requested"]=True
    if j["status"] == "queued":
        _remove_queued(job_id)
        j.update(status="cancelled",stage="cancelled",progress=None,message="任务已取消")
        _move_recycle(j)
    with lock:
        if PROCESS_JOB==job_id and PROCESS:
            try: PROCESS.terminate()
            except OSError: pass
    return {"id":job_id,"status":"cancelled" if j["status"]=="cancelled" else "running"}

@app.get("/api/jobs/{job_id}/download")
def download(job_id: str, token: str=Query(...), kind: str="output"):
    j=_auth(job_id,"Bearer "+token)
    if not j: return fail("restricted","任务不存在或访问令牌无效",404)
    if j["status"]=="completed" and j.get("expires_at") and datetime.fromisoformat(j["expires_at"]).timestamp()<=time.time():
        j.update(status="expired",stage="expired",message="下载已过期",result=None); _move_recycle(j)
    if j["status"]!="completed":
        return fail("expired","下载已过期或尚未准备好",410)
    if kind not in ("output","source") or (kind=="source" and not j["keep_original"]): return fail("invalid_options","该文件不可下载")
    base=(DATA_DIR/"jobs"/job_id).resolve(); path=(base/(j.get("source_filename", "source.m4a") if kind=="source" else "audio."+j["result"]["filename"].rsplit(".",1)[-1])).resolve()
    if not path.is_relative_to(base) or not path.is_file(): return fail("expired","下载文件不可用",410)
    return FileResponse(path,filename=j["result"]["filename"] if kind=="output" else j.get("source_filename", "source.m4a"),media_type="application/octet-stream")
