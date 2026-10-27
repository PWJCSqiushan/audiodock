# AudioDock backend

Python 3.12+ and FFmpeg are required. From the repository root:

```powershell
py -3.12 -m venv backend\.venv
backend\.venv\Scripts\python -m pip install -r backend\requirements-dev.txt
$env:AUDIODOCK_RUNTIME_DIR = "$PWD\runtime"
backend\.venv\Scripts\python -m uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8787 --no-access-log
```

Run backend checks with `backend\.venv\Scripts\python -m pytest backend\tests -q`.

Configuration: `AUDIODOCK_RUNTIME_DIR` (runtime files, default `./runtime`), `AUDIODOCK_STORAGE_CAP_BYTES` (default 2 GiB), `AUDIODOCK_ALLOWED_ORIGINS` (comma-separated exact Pages origins; local `http://localhost:5173` and `http://127.0.0.1:5173` are always allowed), `AUDIODOCK_TRUST_PROXY` (default `false`; enable only behind the trusted Render proxy), and `PORT` (Render port; default 8787). Run exactly one Uvicorn process/worker with access logs disabled because download links contain bearer tokens in their query. The Python outbound socket guard restricts connections to public IPs over port 443 and allowlisted platform/CDN domains. The in-memory queue and rate limits are process-local; Render ephemeral storage can be lost on redeploy.

`/api/health` reports local readiness and FFmpeg discovery only. Automated tests use generated short media and mocked network responses. They do not prove that upstream sites, Render, Pages, or a particular video will work. The Bilibili public API path supports the documented WBI view-detail and DASH audio fallback; YouTube and Vimeo use yt-dlp. No cookies, upload, payment, or DRM bypass are supported.
