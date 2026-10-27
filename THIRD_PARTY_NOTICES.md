# Third-party notices

AudioDock's original application code is MIT licensed. Dependencies retain
their own licenses; this project's MIT license does not relicense them.

| Project | Role | License / reference |
| --- | --- | --- |
| [yt-dlp](https://github.com/yt-dlp/yt-dlp) and its contributors | Public media extraction | Python source/package: [Unlicense](https://github.com/yt-dlp/yt-dlp/blob/master/LICENSE). Bundled executables can have different licensing; AudioDock installs the Python package. |
| [FFmpeg](https://ffmpeg.org/) developers | Audio conversion and verification | [LGPL/GPL depending on build](https://ffmpeg.org/legal.html). The container installs Debian's FFmpeg package, with the actual build configuration and copyright notice saved under `/app/licenses/`. Corresponding source is available through the [Debian FFmpeg source package](https://sources.debian.org/src/ffmpeg/). No FFmpeg executable is committed to this repository. |
| [FastAPI](https://github.com/fastapi/fastapi), [Uvicorn](https://github.com/encode/uvicorn), [Requests](https://github.com/psf/requests) | HTTP application and networking | MIT, BSD-3-Clause, Apache-2.0 respectively; see each project's license. |
| [React](https://github.com/facebook/react), [Vite](https://github.com/vitejs/vite), [TypeScript](https://github.com/microsoft/TypeScript) | Web interface and build tooling | MIT, MIT, Apache-2.0 respectively; see each project's license. |

Thanks to **nilaoda**, the author of [BBDown](https://github.com/nilaoda/BBDown),
for open-source work that informed our initial investigation of Bilibili media.
BBDown is a research reference, not an AudioDock runtime dependency; its original
repository is archived. AudioDock does not claim affiliation with these projects
or with supported video platforms.

For exact installed dependency versions, use the lock/requirements files,
`python -m pip freeze`, `ffmpeg -version`, and `ffmpeg -L`. If redistributing a
container or replacing FFmpeg, retain the notices and source/build information
required by that particular distribution.
