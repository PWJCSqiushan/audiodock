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

  it('shows the backend validation error without presenting a source as recognized', async () => {
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url.endsWith('/api/health')) return Response.json({ status: 'ok' })
      if (url.endsWith('/api/inspect')) return Response.json({ detail: { code: 'unsupported_site', message: '暂不支持这个站点。' } }, { status: 400 })
      throw new Error('Unexpected API request')
    }))
    render(<App />)
    fireEvent.change(screen.getByLabelText('分享链接或文字'), { target: { value: 'https://example.org/audio' } })
    fireEvent.click(screen.getByRole('button', { name: /解析链接/ }))
    expect(await screen.findByRole('alert')).toHaveTextContent('暂不支持这个分享来源。')
    expect(screen.getByText('服务已就绪')).toBeInTheDocument()
    expect(screen.queryByText('✓ 已识别')).not.toBeInTheDocument()
  })
})
