import { useEffect, useState } from 'react'
import { api } from '../api/client'
import type { Track } from '../types'
import { TrackInspector } from './TrackInspector'

export function LibraryView({ tracks, onChanged }: { tracks: Track[]; onChanged: () => void }) {
  const [folders, setFolders] = useState<string[]>([])
  const [newFolder, setNewFolder] = useState('')
  const [status, setStatus] = useState<string | null>(null)
  const [selectedId, setSelectedId] = useState<number | null>(null)

  useEffect(() => {
    api.listFolders().then(setFolders).catch(() => {})
  }, [])

  async function addAndScan() {
    try {
      setStatus('scanning…')
      if (newFolder.trim()) {
        setFolders(await api.addFolder(newFolder.trim()))
        setNewFolder('')
      }
      const result = await api.scan()
      setStatus(`${result.new_tracks} new track(s), ${result.queued} queued for analysis`)
      onChanged()
    } catch (e) {
      setStatus(String(e))
    }
  }

  const selected = tracks.find((t) => t.id === selectedId && t.analysis_status === 'done')

  return (
    <section>
      <div className="toolbar">
        <input
          value={newFolder}
          onChange={(e) => setNewFolder(e.target.value)}
          placeholder="D:\Music\Hardstyle"
          size={50}
        />
        <button onClick={() => void addAndScan()}>Add folder & scan</button>
        {status && <span className="status">{status}</span>}
      </div>
      {folders.length > 0 && <p className="muted">Folders: {folders.join(' · ')}</p>}
      <table>
        <thead>
          <tr>
            <th>Track</th>
            <th>Length</th>
            <th>BPM</th>
            <th>Key</th>
            <th>Energy</th>
            <th>Status</th>
          </tr>
        </thead>
        <tbody>
          {tracks.map((t) => (
            <tr
              key={t.id}
              className={`track-row ${t.id === selectedId ? 'selected' : ''}`}
              onClick={() =>
                t.analysis_status === 'done' && setSelectedId(t.id === selectedId ? null : t.id)
              }
            >
              <td>{t.filename}</td>
              <td>{fmtDuration(t.duration_sec)}</td>
              <td>{t.bpm ?? '–'}</td>
              <td>{t.camelot ? `${t.camelot} (${t.key_name})` : '–'}</td>
              <td>{t.energy != null ? t.energy.toFixed(2) : '–'}</td>
              <td className={`st-${t.analysis_status}`} title={t.analysis_error ?? undefined}>
                {t.analysis_status}
              </td>
            </tr>
          ))}
          {tracks.length === 0 && (
            <tr>
              <td colSpan={6} className="muted">
                No tracks yet — add a music folder above.
              </td>
            </tr>
          )}
        </tbody>
      </table>
      {tracks.length > 0 && !selected && (
        <p className="muted hint">Click an analyzed track to inspect &amp; correct its beat grid and sections.</p>
      )}
      {selected && <TrackInspector track={selected} onSaved={onChanged} />}
    </section>
  )
}

function fmtDuration(sec: number | null): string {
  if (sec == null) return '–'
  const m = Math.floor(sec / 60)
  const s = Math.floor(sec % 60)
  return `${m}:${s.toString().padStart(2, '0')}`
}
