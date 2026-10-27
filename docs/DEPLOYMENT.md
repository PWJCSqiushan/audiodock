# Deployment

## Architecture

GitHub Pages serves the static `web/dist` build. A separate, single-worker FastAPI
service performs extraction and FFmpeg processing. The browser never receives
upstream signed media URLs or server credentials. Each job has an unguessable
access token; do not share its download link with anyone who should not get the file.

## Render Free

1. Create your own fork or use the public repository.
2. Sign into Render and create a Blueprint from the repository's `render.yaml`.
3. Confirm **Free**, the Docker runtime, and `/api/health` readiness path.
4. Set `AUDIODOCK_ALLOWED_ORIGINS` to the exact frontend origin, for example
   `https://YOURNAME.github.io` (no repository path).
5. Wait for the service build and check the deployed `/api/health` response.
6. In GitHub repository **Settings → Secrets and variables → Actions → Variables**,
   create `API_BASE_URL` with the actual `https://...onrender.com` origin.
7. Enable **Pages → GitHub Actions**, run **Deploy Pages**, then test a real job
   from the deployed page. Health alone does not verify a site's availability.

The included Blueprint requests Free only. Do not add a payment method or upgrade
compute just to make the demo work without first deciding your own spending limit.
Render may require you to complete account verification yourself.

You can also create a **Web Service → Public Git Repository** without connecting
your entire GitHub account. Choose Docker, branch `main`, Singapore and **Free**;
leave the root directory empty, use `/api/health`, and copy the backend environment
variables from `render.yaml`. This route requires manual deployments when the
dashboard does not offer automatic deploys for a public URL.

## Local development

Start the backend from repository root with:

```bash
python -m uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8787 --no-access-log --no-proxy-headers
```

Start `npm run dev` inside `web/`. Use `web/.env.local` to override
`VITE_API_BASE_URL` or `VITE_BASE_PATH`. Production builds default to an
unconfigured backend, rather than making requests to a visitor's localhost.

## Configuration

| Variable | Purpose |
| --- | --- |
| `PORT` | Container listen port; default 10000 |
| `AUDIODOCK_RUNTIME_DIR` | Writable, private runtime directory; container default `/tmp/audiodock` |
| `AUDIODOCK_ALLOWED_ORIGINS` | Comma-separated exact browser origins |
| `AUDIODOCK_TRUST_PROXY` | Enable only behind the trusted Render ingress; false locally |
| `VITE_API_BASE_URL` | Frontend build-time API origin, no trailing slash |
| `VITE_BASE_PATH` | Frontend path; `/audiodock/` for this repository's Pages site |

Run **one** Uvicorn worker; the queue and rate limiter belong to one process.
Do not expose a local development instance to the Internet without equivalent
configuration. Trusting arbitrary forwarding headers would make client limits
ineffective. Access logs are disabled to avoid logging download tokens in queries.

Media requests use a fixed Python HTTP stack, HTTPS/CDN host restrictions, and
checks on resolved public IP addresses. YouTube/Vimeo downloads currently require
a direct HTTPS audio stream; an external network downloader is not allowed.
FFmpeg accepts local files and pipes only. These checks are application safeguards,
not a system sandbox; do not install alternative native HTTP/download backends
without reviewing their network behavior.

## Files and retention

Input and output media are runtime data, never source assets. Download links
expire after 30 minutes. Expired, failed and cancelled job files move into the
application recycle area; storage limits stop admission before filling the disk.
The recycle area also uses ephemeral storage. Render can discard all runtime data
on restart, redeploy or idle shutdown, so download completed files promptly.

This is not archival storage. Do not commit media, runtime folders, Cookie files,
`.env` files, API keys or signed playback URLs. Development cleanup moves files to
the OS recycle bin instead of permanently removing them.

## Runtime licenses

The Docker build installs FFmpeg through Debian and saves its version/configuration
and copyright text in `/app/licenses/`. Refer to `THIRD_PARTY_NOTICES.md` before
redistributing an image or replacing that build. Python dependencies are installed
from the versioned requirements file, not a downloaded all-in-one executable.

## Verification and troubleshooting

- A sleeping Render service may return a platform loading page before JSON is
  available; show a wake-up state and retry with a bounded delay.
- A successful `/api/health` only means the application is running. Test metadata,
  actual audio download, conversion and browser delivery separately.
- A 412, 403, login request or region error from a platform must remain a clear
  upstream error. Do not insert third-party credentials or publicly shared proxies.
- Restarting a free instance can expire an in-flight job. The interface should
  explain the lost job and allow a new one, without pretending the old one succeeded.
