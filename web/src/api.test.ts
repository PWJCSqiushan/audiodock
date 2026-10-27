import { describe, expect, it } from 'vitest'
import { ApiError, bitratesFor, buildJobOptions, downloadUrl, friendlyError } from './api'

describe('format-dependent options', () => {
  it.each([
    ['mp3', [128, 192, 256, 320]], ['m4a', [128, 192, 256]], ['opus', [64, 96, 128, 160]], ['flac', []], ['wav', []], ['original', []],
  ] as const)('%s exposes only the contract bitrates', (format, expected) => {
    expect(bitratesFor(format)).toEqual(expected)
  })

  it('keeps source sampling and channels at zero in the default MP3 request', () => {
    expect(buildJobOptions('  https://youtu.be/abc  ', 'mp3', 320, 0, 0, false)).toEqual({
      input: 'https://youtu.be/abc', format: 'mp3', bitrate: 320, sample_rate: 0, channels: 0, keep_original: false,
    })
  })

  it('uses zero for non-applicable settings and all adjustments in original mode', () => {
    expect(buildJobOptions('share', 'original', 320, 48000, 2, true)).toEqual({
      input: 'share', format: 'original', bitrate: 0, sample_rate: 0, channels: 0, keep_original: true,
    })
  })

  it('rejects a stale bitrate selection when constructing an API request', () => {
    expect(buildJobOptions('share', 'm4a', 320, 44100, 1, false).bitrate).toBe(0)
  })
})

describe('user-facing API states', () => {
  it('explains a full queue and an expired download in clear Chinese', () => {
    expect(friendlyError(new ApiError('queue_full', 'Queue is full'))).toContain('队列')
    expect(friendlyError(new ApiError('expired', 'expired')).toLowerCase()).toContain('过期')
  })

  it('builds an attachment URL with the job token and requested kind', () => {
    expect(downloadUrl({ id: 'job / 1', token: 'a+b' }, 'source')).toContain('/api/jobs/job%20%2F%201/download?token=a%2Bb&kind=source')
  })
})
