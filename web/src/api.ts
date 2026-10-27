export type AudioFormat = 'mp3' | 'm4a' | 'opus' | 'flac' | 'wav' | 'original'
export type JobStatus = 'queued' | 'running' | 'completed' | 'failed' | 'cancelled' | 'expired'
export type JobStage = 'queued' | 'resolving' | 'downloading' | 'converting' | 'verifying' | 'completed' | 'failed' | 'cancelled' | 'expired'
export type SourceInfo = { url: string; site: string; id: string; title: string; uploader: string; duration: number | null; thumbnail?: string | null }
export type JobResult = { filename: string; bytes: number; duration: number; codec: string; bit_rate: number; sample_rate: number; channels: number; sha256: string; source_available: boolean }
export type Job = { id: string; status: JobStatus; stage: JobStage; progress: number | null; title: string; message: string; queue_position: number | null; created_at: string; expires_at: string; result: JobResult | null }
export type JobOptions = { input: string; format: AudioFormat; bitrate: number; sample_rate: number; channels: number; keep_original: boolean }
export type SavedJob = { id: string; token: string }

const bitrateChoices: Record<AudioFormat, number[]> = {
  mp3: [128, 192, 256, 320], m4a: [128, 192, 256], opus: [64, 96, 128, 160], flac: [], wav: [], original: [],
}
export const bitratesFor = (format: AudioFormat) => bitrateChoices[format]
export const formatLabels: Record<AudioFormat, string> = { mp3: 'MP3', m4a: 'M4A · AAC', opus: 'OPUS', flac: 'FLAC', wav: 'WAV', original: '原始文件' }
export const canAdjustSource = (format: AudioFormat) => format !== 'original'

export function secureKnownBiliThumbnail(thumbnail: string | null | undefined): string | undefined {
  if (!thumbnail) return undefined
  try {
    const url = new URL(thumbnail)
    const host = url.hostname.toLowerCase()
    if (url.protocol === 'http:' && (host === 'hdslb.com' || host.endsWith('.hdslb.com'))) url.protocol = 'https:'
    return url.toString()
  } catch { return thumbnail }
}

export function buildJobOptions(input: string, format: AudioFormat, bitrate: number, sampleRate: number, channels: number, keepOriginal: boolean): JobOptions {
  const reencodes = format !== 'original'
  return {
    input: input.trim(), format,
    bitrate: reencodes && bitratesFor(format).includes(bitrate) ? bitrate : 0,
    sample_rate: reencodes ? sampleRate : 0,
    channels: reencodes ? channels : 0,
    keep_original: keepOriginal,
  }
}

const configuredOrigin = import.meta.env.VITE_API_BASE_URL
const apiOrigin = (configuredOrigin || (import.meta.env.DEV ? 'http://127.0.0.1:8787' : '')).replace(/\/$/, '')
export const apiUrl = (path: string) => `${apiOrigin}${path}`

export class ApiError extends Error {
  constructor(public code: string, message: string, public status?: number) { super(message); this.name = 'ApiError' }
}

export const friendlyError = (error: unknown): string => {
  if (error instanceof ApiError) {
    if (error.code === 'api_unconfigured') return '音频坞服务暂未连接，请稍后再试。'
    if (error.code === 'queue_full') return '任务队列暂时已满，请稍后再试。'
    if (error.code === 'rate_limited') return '当前时段的任务次数已用完，请稍后再来。'
    if (error.code === 'expired') return '这个任务已过期，请重新创建任务。'
    if (error.code === 'upstream_unavailable') return '来源暂时无法访问，请过一会儿重试。'
    if (error.code === 'unsupported_site') return '暂不支持这个分享来源。'
    if (error.code === 'invalid_url') return '没有识别到有效的分享链接，请检查后重试。'
    if (error.code === 'restricted') return '该内容暂时无法处理，请确认内容可公开访问。'
    if (error.code === 'duration_limit') return '音频时长超过当前可处理范围。'
    if (error.code === 'size_limit') return '文件大小超过当前可处理范围。'
    if (error.code === 'cancelled') return '任务已取消。'
    return error.message || '请求没有完成，请稍后重试。'
  }
  return '暂时连接不上音频坞，请检查网络后重试。'
}

async function request<T>(path: string, init: RequestInit = {}, timeout = 20000): Promise<T> {
  if (!apiOrigin) throw new ApiError('api_unconfigured', '音频坞服务暂未连接。')
  const controller = new AbortController()
  const timer = window.setTimeout(() => controller.abort(), timeout)
  try {
    const response = await fetch(apiUrl(path), { ...init, credentials: 'omit', signal: controller.signal, headers: { 'Content-Type': 'application/json', ...init.headers } })
    const body = await response.json().catch(() => null)
    if (!response.ok) {
      const detail = body?.detail
      throw new ApiError(detail?.code || `http_${response.status}`, detail?.message || '请求没有完成，请稍后重试。', response.status)
    }
    return body as T
  } catch (e) {
    if (e instanceof ApiError) throw e
    throw e
  } finally { window.clearTimeout(timer) }
}

export async function checkHealth(): Promise<void> {
  if (!apiOrigin) throw new ApiError('api_unconfigured', '音频坞服务暂未连接。')
  // A sleeping service may take a minute to wake; bound each attempt and the total retries.
  let lastError: unknown
  for (let attempt = 0; attempt < 3; attempt++) {
    try {
      const result = await request<{ status: string }>('/api/health', {}, 65000)
      if (result.status !== 'ok') throw new Error('not_ready')
      return
    } catch (e) {
      lastError = e
      if (attempt < 2) await new Promise(resolve => window.setTimeout(resolve, 1200 * (attempt + 1)))
    }
  }
  throw lastError
}

export const inspectInput = (input: string) => request<SourceInfo>('/api/inspect', { method: 'POST', body: JSON.stringify({ input: input.trim() }) }, 60000)
export const createJob = (options: JobOptions) => request<SavedJob & { status: JobStatus }>('/api/jobs', { method: 'POST', body: JSON.stringify(options) }, 60000)
export const getJob = (job: SavedJob) => request<Job>(`/api/jobs/${encodeURIComponent(job.id)}`, { headers: { Authorization: `Bearer ${job.token}` } })
export const cancelJob = (job: SavedJob) => request<{ id: string; status: JobStatus }>(`/api/jobs/${encodeURIComponent(job.id)}/cancel`, { method: 'POST', headers: { Authorization: `Bearer ${job.token}` } })
export const downloadUrl = (job: SavedJob, kind: 'output' | 'source' = 'output') => `${apiUrl(`/api/jobs/${encodeURIComponent(job.id)}/download`)}?token=${encodeURIComponent(job.token)}&kind=${kind}`
