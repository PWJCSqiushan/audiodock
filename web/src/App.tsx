import { useEffect, useMemo, useState } from 'react'
import { ApiError, bitratesFor, buildJobOptions, cancelJob, checkHealth, createJob, downloadUrl, formatLabels, friendlyError, getJob, inspectInput, type AudioFormat, type Job, type SavedJob, type SourceInfo } from './api'

const stageText: Record<string, string> = { queued: '排队中', resolving: '读取来源', downloading: '获取音频', converting: '转换格式', verifying: '校验文件', completed: '已完成', failed: '处理失败', cancelled: '已取消', expired: '已过期' }
const stageOrder = ['queued', 'resolving', 'downloading', 'converting', 'verifying', 'completed']
const formatTime = (sec: number) => `${Math.floor(sec / 60)}:${String(Math.floor(sec % 60)).padStart(2, '0')}`
const formatBytes = (bytes: number) => bytes < 1024 * 1024 ? `${Math.max(1, Math.round(bytes / 1024))} KB` : `${(bytes / 1024 / 1024).toFixed(1)} MB`

function BrandMark() {
  return <svg className="brand-mark" viewBox="0 0 44 44" fill="none" aria-hidden="true"><rect x="1" y="1" width="42" height="42" rx="13" fill="#17232a" stroke="#2c4145"/><path d="M8 22h3.4l2.3-7 4.4 14 4.2-18 4.5 22 4.3-16 2.2 5H36" stroke="#76e2bf" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round"/></svg>
}
function Waveform({ muted = false }: { muted?: boolean }) {
  const heights = [10, 18, 12, 28, 16, 34, 20, 13, 30, 17, 24, 36, 14, 21, 31, 17, 10, 28, 16, 34, 20, 14, 25, 11, 30, 17, 23, 35, 14, 21, 31, 16, 11, 28, 17, 32, 21, 13, 25, 15, 30, 19, 11, 24, 16, 32, 18, 12, 27, 16, 21, 35, 12, 20, 29, 17, 11, 27, 18, 32, 20, 14, 25, 11, 30, 17, 23, 35, 14, 21, 31, 16, 11, 28, 17, 32, 21, 13, 25, 15, 30, 19, 11, 24]
  return <div className={`waveform ${muted ? 'muted' : ''}`} aria-hidden="true">{heights.map((height, i) => <i key={i} style={{ height }} />)}</div>
}

function App() {
  const [input, setInput] = useState('')
  const [source, setSource] = useState<SourceInfo | null>(null)
  const [format, setFormat] = useState<AudioFormat>('mp3')
  const [bitrate, setBitrate] = useState(320)
  const [sampleRate, setSampleRate] = useState(0)
  const [channels, setChannels] = useState(0)
  const [keepOriginal, setKeepOriginal] = useState(false)
  const [jobRef, setJobRef] = useState<SavedJob | null>(() => { try { return JSON.parse(sessionStorage.getItem('audiodock-job') || 'null') } catch { return null } })
  const [job, setJob] = useState<Job | null>(null)
  const [busy, setBusy] = useState<'inspect' | 'create' | 'cancel' | ''>('')
  const [warming, setWarming] = useState(false)
  const [error, setError] = useState('')
  const [healthState, setHealthState] = useState<'unknown' | 'checking' | 'ready' | 'offline'>('unknown')
  const choices = useMemo(() => bitratesFor(format), [format])
  const activeJob = Boolean(jobRef && (!job || job.status === 'queued' || job.status === 'running'))
  const outputFormatLabel = (() => {
    const extension = job?.result?.filename.split('.').pop()?.toLowerCase()
    const detected = (['mp3', 'm4a', 'opus', 'flac', 'wav'] as AudioFormat[]).find(item => item === extension)
    return detected ? formatLabels[detected] : job?.result?.codec || formatLabels[format]
  })()

  useEffect(() => {
    if (!jobRef) { setJob(null); return }
    let active = true
    let keepPolling = true
    const refresh = async () => {
      try {
        const next = await getJob(jobRef)
        if (active) {
          setJob(next); setError('')
          if (['completed', 'failed', 'cancelled', 'expired'].includes(next.status)) keepPolling = false
        }
      } catch (e) {
        if (active) {
          setError(friendlyError(e))
          if (e instanceof ApiError && (e.code === 'expired' || e.status === 404 || e.status === 410)) {
            keepPolling = false; setJob(null); setJobRef(null); sessionStorage.removeItem('audiodock-job')
          }
        }
      }
    }
    void refresh()
    const timer = window.setInterval(() => { if (active && keepPolling) void refresh() }, 3000)
    return () => { active = false; window.clearInterval(timer) }
  }, [jobRef])

  const showError = (e: unknown) => setError(friendlyError(e))
  const onInspect = async () => {
    if (!input.trim() || busy) return
    setBusy('inspect'); setError(''); setSource(null); setJob(null); setJobRef(null); sessionStorage.removeItem('audiodock-job')
    setWarming(true); setHealthState('checking')
    let serviceReady = false
    try {
      await checkHealth(); serviceReady = true; setHealthState('ready'); setWarming(false)
      const result = await inspectInput(input)
      setSource(result)
    } catch (e) { if (!serviceReady) setHealthState('offline'); showError(e) }
    finally { setWarming(false); setBusy('') }
  }
  const onCreate = async () => {
    if (!source || busy) return
    setBusy('create'); setError('')
    try {
      const created = await createJob(buildJobOptions(source.url, format, bitrate, sampleRate, channels, keepOriginal))
      const saved = { id: created.id, token: created.token }
      setJobRef(saved); sessionStorage.setItem('audiodock-job', JSON.stringify(saved))
    } catch (e) { showError(e) }
    finally { setBusy('') }
  }
  const onCancel = async () => {
    if (!jobRef || busy) return
    setBusy('cancel'); setError('')
    try { await cancelJob(jobRef); const updated = await getJob(jobRef); setJob(updated) }
    catch (e) { showError(e) }
    finally { setBusy('') }
  }
  const reset = () => { setSource(null); setJob(null); setJobRef(null); setError(''); setInput(''); sessionStorage.removeItem('audiodock-job') }
  const submitEnabled = Boolean(input.trim()) && !busy && !activeJob
  const progress = typeof job?.progress === 'number' ? Math.max(0, Math.min(100, job.progress)) : null
  const currentStep = job ? stageOrder.indexOf(job.stage) : -1
  const isTerminal = job && ['failed', 'cancelled', 'expired'].includes(job.status)

  return <div className="app-shell">
    <div className="ambient ambient-a"/><div className="ambient ambient-b"/>
    <header className="topbar"><a className="brand" href="#top" aria-label="AudioDock 音频坞首页"><BrandMark/><span><strong>AudioDock</strong><small>音频坞</small></span></a><nav><a href="#how">使用说明</a><a className="github-link" href="https://github.com/PWJCSqiushan/audiodock" target="_blank" rel="noreferrer">GitHub <span>↗</span></a></nav></header>
    <main id="top">
      <section className="intro"><div className="eyebrow"><span className="pulse-dot"/> 轻一点，听得刚刚好</div><h1>声音，<em>归你掌控。</em></h1><p>粘贴公开分享链接，选择合适的音频格式。<br className="desktop-break"/>处理完成后，你可以直接下载文件。</p></section>
      <section className="workspace" aria-label="音频转换工作台">
        <div className="work-head"><div><span className="section-index">01 / SOURCE</span><h2>放入一个分享链接</h2></div><div className={`service-pill ${healthState}`}><span className="service-dot"/>{healthState === 'ready' ? '服务已就绪' : healthState === 'checking' ? '正在唤醒服务' : healthState === 'offline' ? '暂时无法连接' : '等待连接'}</div></div>
        <div className="url-row"><div className="url-icon"><svg viewBox="0 0 24 24" fill="none"><path d="M10 13.5a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1.7 1.7M14 10.5a4 4 0 0 0-5.7 0l-3 3A4 4 0 0 0 12 19.2l1.7-1.7" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round"/></svg></div><input aria-label="分享链接或文字" placeholder="粘贴哔哩哔哩、YouTube 或 Vimeo 分享链接" value={input} disabled={busy === 'create' || activeJob} onChange={e => { setInput(e.target.value); if (source) setSource(null) }} onKeyDown={e => { if (e.key === 'Enter') void onInspect() }} /><button className="inspect-button" disabled={!submitEnabled} onClick={() => void onInspect()}>{busy === 'inspect' ? <><span className="spinner"/> {warming ? '连接服务…' : '正在解析…'}</> : <>解析链接 <span>↗</span></>}</button></div>
        {warming && <div className="warm-note"><span className="spinner"/> 音频坞正在启动，首次连接可能需要一点时间。请保持此页面打开。</div>}
        {error && <div className="alert" role="alert"><span className="alert-symbol">!</span><span>{error}</span><button onClick={() => { setError(''); if (healthState === 'offline') void onInspect() }}>重试</button></div>}
        {source && !jobRef && <div className="source-card"><div className="source-art">{source.thumbnail ? <img src={source.thumbnail} alt=""/> : <div className="cover-placeholder"><Waveform/></div>}<span className="duration">{typeof source.duration === 'number' ? formatTime(source.duration) : '时长未知'}</span></div><div className="source-meta"><div className="source-kicker"><span className="source-dot"/>{source.site}</div><h3>{source.title}</h3><p>{source.uploader || '公开分享内容'}</p><button className="text-button" onClick={() => setSource(null)}>更换链接</button></div><div className="ready-tag">✓ 已识别</div></div>}
        {source && !jobRef && <div className="options"><div className="work-head option-heading"><div><span className="section-index">02 / FORMAT</span><h2>选择输出方式</h2></div><span className="option-note">适合你就好，不必追求更高数字</span></div>
          <div className="format-grid" role="group" aria-label="输出格式">{(['mp3','m4a','opus','flac','wav','original'] as AudioFormat[]).map((f, i) => <button key={f} className={`format-card ${format === f ? 'selected' : ''}`} onClick={() => { setFormat(f); if (f === 'mp3') setBitrate(320); else if (f === 'm4a') setBitrate(192); else if (f === 'opus') setBitrate(128) }} aria-pressed={format === f}><span className="format-symbol">{['M','A','O','F','W','↗'][i]}</span><span className="format-name">{formatLabels[f]}</span><span className="format-check">✓</span></button>)}</div>
          {format === 'original' ? <div className="original-note"><span>↗</span><div><strong>保留原始音频</strong><p>直接保存来源文件，不重新编码，也不调整采样率或声道。</p></div></div> : <div className="parameter-row"><label className="field"><span>比特率</span><select value={choices.length ? bitrate : 0} disabled={!choices.length} onChange={e => setBitrate(Number(e.target.value))}>{choices.length ? choices.map(v => <option key={v} value={v}>{v} kbps</option>) : <option value={0}>此格式无需比特率</option>}</select></label><label className="field"><span>采样率</span><select value={sampleRate} onChange={e => setSampleRate(Number(e.target.value))}><option value={0}>来源采样率</option><option value={44100}>44.1 kHz</option><option value={48000}>48 kHz</option></select></label><label className="field"><span>声道</span><select value={channels} onChange={e => setChannels(Number(e.target.value))}><option value={0}>来源声道</option><option value={2}>立体声</option><option value={1}>单声道</option></select></label></div>}
          <label className="keep-row"><input type="checkbox" checked={keepOriginal} onChange={e => setKeepOriginal(e.target.checked)}/><span className="check-box"/><span className="keep-copy"><strong>同时保留来源文件</strong><small>转换完成后，也可下载原始音频</small></span><span className="keep-hint">额外占用存储空间</span></label>
          {(format === 'flac' || format === 'wav') && <p className="lossless-note">FLAC / WAV 不会提升来源音质；只有原始文件本身清晰时，才能保留它的品质。</p>}
          <div className="create-row"><span><span className="shield">✳</span> 仅处理公开可访问的内容</span><button className="create-button" onClick={() => void onCreate()} disabled={busy === 'create'}>{busy === 'create' ? <><span className="spinner"/> 正在创建…</> : <>创建音频任务 <span>→</span></>}</button></div>
        </div>}
        {jobRef && <div className="job-panel"><div className="work-head option-heading"><div><span className="section-index">03 / YOUR AUDIO</span><h2>{job?.status === 'completed' ? '你的音频已准备好' : job?.status === 'failed' ? '这次没有处理成功' : job?.status === 'cancelled' ? '任务已取消' : job?.status === 'expired' ? '任务已过期' : '正在为你准备音频'}</h2></div>{job?.status === 'completed' ? <span className="completed-badge">✓ 已完成</span> : <span className={`job-status ${isTerminal ? 'terminal' : ''}`}>{job ? stageText[job.stage] : '正在恢复任务'}</span>}</div>
          <div className={`job-visual ${job?.status === 'completed' ? 'done' : ''}`}><div className="play-disc">{job?.status === 'completed' ? '✓' : <span className="mini-spinner"/>}</div><div className="job-wave-area"><div className="job-title-line"><strong>{job?.title || source?.title || '你的音频文件'}</strong><span>{job?.status === 'completed' ? (typeof job.result?.duration === 'number' ? formatTime(job.result.duration) : '') : job?.queue_position ? `前面还有 ${job.queue_position} 个任务` : ''}</span></div><Waveform muted={!job?.status || job.status === 'completed'}/><div className={`progress-line ${progress === null && job?.status !== 'completed' ? 'indeterminate' : ''}`}><span style={{ width: `${progress ?? (job?.status === 'completed' ? 100 : 0)}%` }}/></div></div></div>
          <div className="step-track">{['排队中','读取来源','获取音频','转换格式','文件校验'].map((name, i) => { const stepIndex = i; const done = job?.status === 'completed' || (currentStep > stepIndex); const active = (job?.status === 'queued' || job?.status === 'running') && currentStep === stepIndex; return <div key={name} className={`step ${done ? 'done' : ''} ${active ? 'active' : ''}`}><span>{done ? '✓' : `0${i + 1}`}</span><small>{name}</small></div> })}</div>
          {job?.status === 'queued' || job?.status === 'running' ? <div className="live-row"><span className="live-pulse"/>{job.message || (job.status === 'queued' ? (job.queue_position ? `前面还有 ${job.queue_position} 个任务` : '任务已进入队列，等待开始') : job.progress === null ? '正在处理，进度暂不可用' : `${stageText[job.stage]}中`)}{progress !== null && <strong>{progress}%</strong>}<button className="cancel-button" onClick={() => void onCancel()} disabled={busy === 'cancel'}>{busy === 'cancel' ? '取消中…' : '取消任务'}</button></div> : null}
          {job?.status === 'completed' && job.result && jobRef && <><div className="result-details"><span>{outputFormatLabel}<b>格式</b></span><span>{formatBytes(job.result.bytes)}<b>文件大小</b></span><span>{job.result.sample_rate ? `${(job.result.sample_rate / 1000).toFixed(1)} kHz` : '来源'}<b>采样率</b></span></div><a className="download-button" href={downloadUrl(jobRef)}><svg viewBox="0 0 24 24" fill="none"><path d="M12 3v12m0 0 4.5-4.5M12 15l-4.5-4.5M5 17v3h14v-3" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"/></svg>下载音频文件 <span>↓</span></a>{job.result.source_available && <a className="source-download" href={downloadUrl(jobRef,'source')}>也下载保留的来源文件 ↗</a>}</>}
          {job?.status === 'failed' && <p className="job-message">{job.message || '处理时遇到问题，请检查链接后重新创建任务。'}</p>}
          {job?.status === 'expired' && <p className="job-message">为保护存储空间，已完成的文件会在一段时间后失效。</p>}
          {(isTerminal || job?.status === 'completed') && <button className="text-button new-task" onClick={reset}>创建另一个任务 ↗</button>}
          {job?.status === 'completed' && !job.result && <p className="job-message">任务状态已完成，但下载信息暂不可用，请重新创建任务。</p>}
        </div>}
        <div className="workspace-foot"><span><span className="tiny-lock">⌑</span> 链接只用于本次处理</span><span>支持公开分享链接</span></div>
      </section>
      <section id="how" className="how"><div className="how-title"><span className="section-index">A SIMPLE FLOW</span><h2>三步，听到你想要的</h2></div><div className="how-grid"><article><span className="how-num">01</span><h3>粘贴链接</h3><p>放入公开分享文字或链接，先确认音频来源。</p></article><article><span className="how-num">02</span><h3>选择参数</h3><p>挑选格式、比特率和声道，也可以保留来源文件。</p></article><article><span className="how-num">03</span><h3>创建并下载</h3><p>查看真实处理状态，完成后直接保存音频。</p></article></div></section>
    </main>
    <footer><a className="footer-brand" href="#top"><BrandMark/><span>AudioDock <small>音频坞</small></span></a><span>让声音，刚刚好。</span><a href="https://github.com/PWJCSqiushan/audiodock" target="_blank" rel="noreferrer">开源项目 <span>↗</span></a></footer>
  </div>
}

export default App
