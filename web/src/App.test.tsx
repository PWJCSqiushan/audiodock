import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import App from './App'

const completedJob = {
  id: 'job-1', status: 'completed', stage: 'completed', progress: 100, title: 'A sample episode', message: '', queue_position: null,
  created_at: '2026-10-27T00:00:00Z', expires_at: '2026-10-27T00:30:00Z',
  result: { filename: 'sample.mp3', bytes: 12000, duration: 42, codec: 'mp3', bit_rate: 320000, sample_rate: 44100, channels: 2, sha256: 'abc', source_available: true },
}

afterEach(() => { cleanup(); vi.restoreAllMocks(); sessionStorage.clear() })

describe('AudioDock task flow', () => {
  it('sends the selected original-mode payload, follows the real stage, and offers tokenized downloads', async () => {
    const calls: Array<{ url: string; init: RequestInit }> = []
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init: RequestInit = {}) => {
      const url = String(input); calls.push({ url, init })
      if (url.endsWith('/api/health')) return Response.json({ status: 'ok' })
      if (url.endsWith('/api/inspect')) return Response.json({ url: 'https://youtu.be/normalized', site: 'YouTube', id: 'abc', title: 'A sample episode', uploader: 'Example', duration: 42 })
      if (url.endsWith('/api/jobs') && init.method === 'POST') return Response.json({ id: 'job-1', token: 'secret-token', status: 'queued' })
      if (url.includes('/api/jobs/job-1')) return Response.json(completedJob)
      throw new Error(`Unexpected API request: ${url}`)
    }))
    render(<App />)
    fireEvent.change(screen.getByLabelText('分享链接或文字'), { target: { value: 'https://youtu.be/abc' } })
    fireEvent.click(screen.getByRole('button', { name: /解析链接/ }))
    expect(await screen.findByText('A sample episode')).toBeInTheDocument()
    fireEvent.change(screen.getByLabelText('采样率'), { target: { value: '44100' } })
    fireEvent.click(screen.getByRole('button', { name: /OPUS/ }))
    expect(screen.getByLabelText('采样率')).toHaveValue('0')
    expect(screen.queryByRole('option', { name: '44.1 kHz' })).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /FLAC/ }))
    expect(screen.getByLabelText('比特率')).toBeDisabled()
    expect(screen.getByLabelText('比特率')).toHaveDisplayValue('此格式无需比特率')
    fireEvent.click(screen.getByRole('button', { name: /原始文件/ }))
    fireEvent.click(screen.getByRole('checkbox', { name: /同时保留来源文件/ }))
    fireEvent.click(screen.getByRole('button', { name: /创建音频任务/ }))
    expect(await screen.findByText('你的音频已准备好')).toBeInTheDocument()
    await waitFor(() => expect(calls.some(c => c.url.endsWith('/api/jobs/job-1'))).toBe(true))
    const payloadCall = calls.find(c => c.url.endsWith('/api/jobs') && c.init.method === 'POST')!
    expect(JSON.parse(String(payloadCall.init.body))).toEqual({ input: 'https://youtu.be/normalized', format: 'original', bitrate: 0, sample_rate: 0, channels: 0, keep_original: true })
    expect(payloadCall.init.credentials).toBe('omit')
    expect(screen.getByRole('link', { name: /下载音频文件/ })).toHaveAttribute('href', expect.stringContaining('token=secret-token&kind=output'))
    expect(screen.getByRole('link', { name: /也下载保留的来源文件/ })).toHaveAttribute('href', expect.stringContaining('kind=source'))
    expect(sessionStorage.getItem('audiodock-job')).toContain('job-1')
  })

  it('does not manufacture a percentage when the backend reports unknown progress', async () => {
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init: RequestInit = {}) => {
      const url = String(input)
      if (url.endsWith('/api/health')) return Response.json({ status: 'ok' })
      if (url.endsWith('/api/inspect')) return Response.json({ url: 'https://youtu.be/abc', site: 'YouTube', id: 'abc', title: 'A sample episode', uploader: 'Example', duration: null })
      if (url.endsWith('/api/jobs') && init.method === 'POST') return Response.json({ id: 'job-2', token: 'token-2', status: 'queued' })
      if (url.includes('/api/jobs/job-2')) return Response.json({ ...completedJob, id: 'job-2', status: 'running', stage: 'converting', progress: null, result: null })
      throw new Error('Unexpected API request')
    }))
    render(<App />)
    fireEvent.change(screen.getByLabelText('分享链接或文字'), { target: { value: 'https://youtu.be/abc' } })
    fireEvent.click(screen.getByRole('button', { name: /解析链接/ }))
    await screen.findByText('A sample episode')
    expect(screen.getByText('时长未知')).toBeInTheDocument()
    expect(screen.queryByText('0:00')).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /创建音频任务/ }))
    await screen.findByText('正在为你准备音频')
    expect(await screen.findByText('正在处理，进度暂不可用')).toBeInTheDocument()
    expect(screen.queryByText(/\d+%/)).not.toBeInTheDocument()
  })

  it('retries a failed inspection and shows the recognized source after the real retry succeeds', async () => {
    let inspectCount = 0
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url.endsWith('/api/health')) return Response.json({ status: 'ok' })
      if (url.endsWith('/api/inspect')) {
        inspectCount++
        if (inspectCount === 1) return Response.json({ detail: { code: 'upstream_unavailable', message: '来源暂时无法访问。' } }, { status: 503 })
        return Response.json({ url: 'https://youtu.be/abc', site: 'YouTube', id: 'abc', title: 'Retry succeeded', uploader: 'Example', duration: 42 })
      }
      throw new Error('Unexpected API request')
    }))
    render(<App />)
    fireEvent.change(screen.getByLabelText('分享链接或文字'), { target: { value: 'https://example.org/audio' } })
    fireEvent.click(screen.getByRole('button', { name: /解析链接/ }))
    expect(await screen.findByRole('alert')).toHaveTextContent('来源暂时无法访问，请过一会儿重试。')
    expect(screen.getByText('服务已就绪')).toBeInTheDocument()
    expect(screen.queryByText('✓ 已识别')).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '重新解析' }))
    expect(await screen.findByText('Retry succeeded')).toBeInTheDocument()
    expect(inspectCount).toBe(2)
  })

  it('only closes a task status error instead of starting another inspection', async () => {
    let inspectCount = 0
    let jobCount = 0
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init: RequestInit = {}) => {
      const url = String(input)
      if (url.endsWith('/api/health')) return Response.json({ status: 'ok' })
      if (url.endsWith('/api/inspect')) {
        inspectCount++
        return Response.json({ url: 'https://youtu.be/abc', site: 'YouTube', id: 'abc', title: 'A sample episode', uploader: 'Example', duration: 42 })
      }
      if (url.endsWith('/api/jobs') && init.method === 'POST') return Response.json({ id: 'job-error', token: 'token', status: 'queued' })
      if (url.includes('/api/jobs/job-error')) {
        jobCount++
        return Response.json({ detail: { code: 'processing_failed', message: '服务暂时不可用。' } }, { status: 503 })
      }
      throw new Error('Unexpected API request')
    }))
    render(<App />)
    fireEvent.change(screen.getByLabelText('分享链接或文字'), { target: { value: 'https://youtu.be/abc' } })
    fireEvent.click(screen.getByRole('button', { name: /解析链接/ }))
    await screen.findByText('A sample episode')
    fireEvent.click(screen.getByRole('button', { name: /创建音频任务/ }))
    expect(await screen.findByRole('alert')).toHaveTextContent('服务暂时不可用。')
    expect(screen.getByRole('button', { name: '关闭提示' })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '关闭提示' }))
    expect(inspectCount).toBe(1)
    expect(jobCount).toBe(1)
  })
})
