import type {
  Analysis,
  AnalysisPatch,
  ExportResult,
  OrderSuggestion,
  Project,
  SeamParams,
  SeamPreviewOut,
  SeamSuggestion,
  StemsStatus,
  Track,
  Waveform,
} from '../types'

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...init,
  })
  if (!res.ok) {
    const body = await res.text()
    throw new Error(`${res.status} ${res.statusText}: ${body}`)
  }
  return res.json() as Promise<T>
}

export const api = {
  addFolder: (path: string) =>
    request<string[]>('/api/library/folders', { method: 'POST', body: JSON.stringify({ path }) }),
  listFolders: () => request<string[]>('/api/library/folders'),
  scan: () => request<{ new_tracks: number; queued: number }>('/api/library/scan', { method: 'POST' }),
  listTracks: () => request<Track[]>('/api/library/tracks'),
  getAnalysis: (trackId: number) => request<Analysis>(`/api/library/tracks/${trackId}/analysis`),
  patchAnalysis: (trackId: number, patch: AnalysisPatch) =>
    request<Analysis>(`/api/library/tracks/${trackId}/analysis`, {
      method: 'PATCH',
      body: JSON.stringify(patch),
    }),
  getPeaks: (trackId: number, pps = 50) =>
    request<Waveform>(`/api/library/tracks/${trackId}/peaks?pps=${pps}`),
  trackAudioUrl: (trackId: number) => `/api/library/tracks/${trackId}/audio`,
  getStems: (trackId: number) => request<StemsStatus>(`/api/library/tracks/${trackId}/stems`),
  requestStems: (trackId: number) =>
    request<StemsStatus>(`/api/library/tracks/${trackId}/stems`, { method: 'POST' }),

  listProjects: () => request<string[]>('/api/projects'),
  loadProject: (name: string) => request<Project>(`/api/projects/${encodeURIComponent(name)}`),
  saveProject: (project: Project) =>
    request<Project>(`/api/projects/${encodeURIComponent(project.name)}`, {
      method: 'PUT',
      body: JSON.stringify(project),
    }),
  suggestOrder: (name: string) =>
    request<OrderSuggestion>(`/api/projects/${encodeURIComponent(name)}/suggest-order`, { method: 'POST' }),

  suggestSeam: (outTrackId: number, inTrackId: number) =>
    request<SeamSuggestion>('/api/seams/suggest', {
      method: 'POST',
      body: JSON.stringify({ out_track_id: outTrackId, in_track_id: inTrackId }),
    }),
  renderSeamPreview: (outTrackId: number, inTrackId: number, params: SeamParams) =>
    request<SeamPreviewOut>('/api/seams/preview', {
      method: 'POST',
      body: JSON.stringify({ out_track_id: outTrackId, in_track_id: inTrackId, params }),
    }),

  exportProject: (name: string) =>
    request<ExportResult>(`/api/export/${encodeURIComponent(name)}`, { method: 'POST' }),
}
