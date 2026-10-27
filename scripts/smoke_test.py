"""Run a real API job and verify its downloaded audio (never prints job tokens)."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import time
from urllib.error import HTTPError
from urllib.request import Request, build_opener, ProxyHandler


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", help="A publicly accessible supported video URL")
    parser.add_argument("--api", default="http://127.0.0.1:8787")
    parser.add_argument("--output", type=Path, default=Path("artifacts/smoke.mp3"))
    parser.add_argument("--no-proxy", action="store_true", help="Ignore environment proxies for this verification process only")
    args = parser.parse_args()
    origin = args.api.rstrip("/")
    opener = build_opener(ProxyHandler({})) if args.no_proxy else build_opener()
    if args.output.exists():
        raise FileExistsError("Choose a fresh output path; existing files are never overwritten")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    state_path = args.output.with_suffix(args.output.suffix + ".job.json")

    def api(path: str, data: dict | None = None, token: str | None = None) -> dict:
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = "Bearer " + token
        request = Request(origin + path, data=json.dumps(data).encode() if data is not None else None, headers=headers)
        try:
            with opener.open(request, timeout=90) as response:
                return json.load(response)
        except HTTPError as error:
            raise RuntimeError(error.read().decode()) from None

    if state_path.exists():
        saved = json.loads(state_path.read_text(encoding="utf-8"))
        if saved["api"] != origin or saved["input"] != args.url:
            raise ValueError("Saved job belongs to a different API or input")
        source, job = saved["source"], saved["job"]
        print("Resuming saved job; no new job quota consumed", flush=True)
    else:
        source = api("/api/inspect", {"input": args.url})
        job = api("/api/jobs", {"input": args.url, "format": "mp3", "bitrate": 320,
                                "sample_rate": 0, "channels": 0, "keep_original": False})
        with state_path.open("x", encoding="utf-8") as state_file:
            json.dump({"api": origin, "input": args.url, "source": source, "job": job}, state_file)
        print("Private job checkpoint saved beside output; do not share it", flush=True)
    deadline = time.monotonic() + 1800
    previous = None
    while time.monotonic() < deadline:
        status = api("/api/jobs/" + job["id"], token=job["token"])
        state = (status["status"], status["stage"])
        if state != previous:
            print(" / ".join(state), flush=True)
            previous = state
        if status["status"] == "completed":
            break
        if status["status"] in {"failed", "cancelled", "expired"}:
            raise RuntimeError(status["message"])
        time.sleep(3)
    else:
        api("/api/jobs/" + job["id"] + "/cancel", {}, job["token"])
        raise TimeoutError("Job exceeded 30-minute verification deadline")

    request = Request(origin + "/api/jobs/" + job["id"] + "/download?token=" + job["token"])
    digest = hashlib.sha256()
    with opener.open(request, timeout=90) as response, args.output.open("xb") as target:
        while chunk := response.read(1024 * 1024):
            digest.update(chunk)
            target.write(chunk)
    result = status["result"]
    if args.output.stat().st_size != result["bytes"] or digest.hexdigest() != result["sha256"]:
        raise RuntimeError("Downloaded file does not match API result")
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-xerror", "-i", str(args.output), "-f", "null", "-"], check=True)
    if source.get("duration") and abs(result["duration"] - source["duration"]) > 3:
        raise RuntimeError("Output duration differs from source metadata")
    print(json.dumps({"source_id": source["id"], "output": str(args.output), **result}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
