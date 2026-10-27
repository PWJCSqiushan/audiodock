# AudioDock v0.1 contract

The web frontend lives in `web/`. The Python FastAPI app lives in `backend/`.
Backend starts with `uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8787`.

## JSON API (snake_case)

- `GET /api/health` => `{status:"ok",version:"0.1.0",ffmpeg_available:true,limits:{max_duration_seconds:3600,max_source_bytes:268435456,max_output_bytes:786432000,max_queue:5,jobs_per_hour:3},sites:[{id:"bilibili",name:"哔哩哔哩",status:"available"}],formats:["mp3","m4a","opus","flac","wav","original"]}`. Health is readiness, not proof upstream sites work.
- `POST /api/inspect` body `{input:"share text or https URL"}` => `{url,site,id,title,uploader,duration,thumbnail}`. Public metadata only; no signed source URLs returned.
- `POST /api/jobs` body `{input,format:"mp3",bitrate:320,sample_rate:0,channels:0,keep_original:false}` => `{id,token,status:"queued"}`. bitrate uses kbps, 0 means irrelevant/default; sample_rate 0/source or 44100/48000; channels 0/source or 1/2.
- `GET /api/jobs/{id}` with `Authorization: Bearer <token>` => `{id,status,stage,progress,title,message,queue_position,created_at,expires_at,result:null|{filename,bytes,duration,codec,bit_rate,sample_rate,channels,sha256,source_available}}`. status: queued/running/completed/failed/cancelled/expired. stage: queued/resolving/downloading/converting/verifying/completed/failed/cancelled/expired. progress: real 0..100, null if unknown; frontend must not manufacture percentages.
- `POST /api/jobs/{id}/cancel` Bearer token => `{id,status}`.
- `GET /api/jobs/{id}/download?token=...&kind=output|source` => attachment; `kind=source` only when keep_original true. Token is random per job; logs must redact query strings.
- Errors: `{detail:{code,message}}` with user-safe Chinese message; no stack, secret, CDN signed URLs or private paths. Typical codes: invalid_url, unsupported_site, restricted, upstream_unavailable, duration_limit, size_limit, queue_full, rate_limited, cancelled, expired, invalid_options, processing_failed.

UI: Chinese by default, AudioDock / 音频坞, deep slate navy + teal, original waveform logo, polished responsive workspace. URL-only first release (no WASM/upload). Default MP3 320 kbps, original sample rate/channels, keep_original false. MP3 128/192/256/320; M4A 128/192/256; Opus 64/96/128/160; FLAC/WAV/original disable bitrate, original disables resampling and channels. Explicit empty/loading/queued/progress/failure/result/cold-start states. No fake live conversions.

Web config: `VITE_API_BASE_URL` backend origin without trailing slash, `VITE_BASE_PATH=/audiodock/` for Pages; dev API default http://127.0.0.1:8787. All URLs resolve correctly under project Pages path. Frontend uses fetch with job bearer token; downloads anchor uses token query; sessionStorage may retain active job only. No cookies/analytics.

Backend: one uvicorn worker + single processing worker, five waiting queue slots; three accepted tasks per client/IP per hour; streaming bounded downloads; supported input domains only Bilibili/b23, YouTube/youtu.be, Vimeo. Public media only. yt-dlp then Bilibili public-API fallback; FFmpeg array args, full decode verification; no shell interpolation. Reject inner/private targets and redirect hops; guard output paths; random runtime job dirs. Finished downloads expire after 30min. Move expired/cancelled/failed task files to application recycle area; storage cap backpressure, do not directly delete user files. Cloud redeploy may discard ephemeral runtime. Runtime paths never in source tree, no credentials/media committed.
