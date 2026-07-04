import { useEffect, useMemo, useState } from 'react'
import { api } from '../api/client'
import { seamBadges } from '../compat'
import type { ExportResult, Project, SeamParams, Track } from '../types'
import { SeamEditor } from './SeamEditor'

const DEFAULT_PROJECT = 'my-set'

// TODO(ui): drag-and-drop reordering, multiple projects, waveform lane view.
export function SetTimeline({ tracks }: { tracks: Track[] }) {
  const [project, setProject] = useState<Project | null>(null)
  const [selectedSeam, setSelectedSeam] = useState<number | null>(null) // index into adjacent pairs
  const [status, setStatus] = useState<string | null>(null)
  const [exportResult, setExportResult] = useState<ExportResult | null>(null)

  const byId = useMemo(() => new Map(tracks.map((t) => [t.id, t])), [tracks])
  const inSet = new Set(project?.track_ids ?? [])
  const analyzed = tracks.filter((t) => t.analysis_status === 'done' && !inSet.has(t.id))

  useEffect(() => {
    api
      .loadProject(DEFAULT_PROJECT)
      .then(setProject)
      .catch(() => setProject({ name: DEFAULT_PROJECT, track_ids: [], seams: [], track_gains: {} }))
  }, [])

  async function save(next: Project) {
    setProject(next)
    try {
      await api.saveProject(next)
      setStatus(null)
    } catch (e) {
      setStatus(String(e))
    }
  }

  function move(index: number, delta: number) {
    if (!project) return
    const ids = [...project.track_ids]
    const j = index + delta
    if (j < 0 || j >= ids.length) return
    ;[ids[index], ids[j]] = [ids[j], ids[index]]
    void save({ ...project, track_ids: ids })
  }

  // Seams are keyed by track pair so reordering preserves crafted transitions.
  function commitSeam(outId: number, inId: number, params: SeamParams) {
    if (!project) return
    const seams = [...project.seams]
    const i = seams.findIndex((s) => s.out_track_id === outId && s.in_track_id === inId)
    const entry = { out_track_id: outId, in_track_id: inId, params }
    if (i >= 0) seams[i] = entry
    else seams.push(entry)
    void save({ ...project, seams })
  }

  async function suggestOrder() {
    if (!project) return
    try {
      const suggestion = await api.suggestOrder(project.name)
      void save({ ...project, track_ids: suggestion.track_ids })
      setStatus('Order suggested (BPM/key/energy greedy) — reorder freely.')
    } catch (e) {
      setStatus(String(e))
    }
  }

  function setTrim(id: number, gainDb: number) {
    if (!project) return
    void save({ ...project, track_gains: { ...project.track_gains, [id]: gainDb } })
  }

  async function autoGain() {
    if (!project) return
    setStatus('Measuring loudness… (first run decodes each track once)')
    try {
      const suggestions = await api.autoGain(project.name)
      const track_gains = { ...project.track_gains }
      for (const s of suggestions) track_gains[s.track_id] = s.gain_db
      void save({ ...project, track_gains })
      setStatus('Trims matched to the set median — tweak any of them freely.')
    } catch (e) {
      setStatus(String(e))
    }
  }

  async function doExport() {
    if (!project) return
    setStatus('Rendering… (synchronous for now, hang tight)')
    setExportResult(null)
    try {
      setExportResult(await api.exportProject(project.name))
      setStatus(null)
    } catch (e) {
      setStatus(String(e))
    }
  }

  if (!project) return <p className="muted">Loading project…</p>

  return (
    <section>
      <div className="toolbar">
        <strong>{project.name}</strong>
        <button
          title="Sorts the set so neighboring tracks match in tempo, key and energy. Just a suggestion — reorder freely afterwards."
          onClick={() => void suggestOrder()}
          disabled={project.track_ids.length < 2}
        >
          Suggest order
        </button>
        <button
          title="Measure every track's loudness and suggest a dB trim toward the set's median, so quieter masters don't make blends feel like a dip. Fills the editable trim fields — adjust them freely afterwards."
          onClick={() => void autoGain()}
          disabled={project.track_ids.length < 2}
        >
          Auto gain
        </button>
        <button
          title="Render the finished mix into one continuous WAV + MP3 plus a timestamped tracklist (saved under data/exports)."
          onClick={() => void doExport()}
          disabled={project.track_ids.length === 0}
        >
          Export set
        </button>
        {status && <span className="status">{status}</span>}
      </div>

      {exportResult && (
        <p className="status">
          Rendered {Math.round(exportResult.duration_sec / 60)} min → {exportResult.wav_path}
          {exportResult.mp3_path ? ` + MP3` : ' (MP3 skipped: ffmpeg missing?)'} + tracklist
        </p>
      )}

      <ol className="set-lane">
        {project.track_ids.map((id, i) => {
          const t = byId.get(id)
          return (
            <li key={id}>
              <div className="set-track">
                <span>
                  {t?.filename ?? `#${id}`}{' '}
                  <span
                    className="muted"
                    title="Tempo (beats per minute) · key in Camelot notation. Keys with the same number, or ±1 with the same letter, blend harmonically."
                  >
                    {t?.bpm ?? '?'} BPM · {t?.camelot ?? '?'}
                  </span>
                </span>
                <span className="row-actions">
                  <label
                    className="trim-field"
                    title="Level trim for this track in the whole mix (dB). Seeded by Auto gain, yours to override."
                  >
                    trim{' '}
                    <input
                      type="number"
                      step={0.5}
                      min={-12}
                      max={12}
                      value={project.track_gains[id] ?? 0}
                      onChange={(e) => {
                        const v = Number(e.target.value)
                        if (Number.isFinite(v)) setTrim(id, Math.max(-12, Math.min(12, v)))
                      }}
                    />{' '}
                    dB
                  </label>
                  <button onClick={() => move(i, -1)}>↑</button>
                  <button onClick={() => move(i, 1)}>↓</button>
                  <button
                    onClick={() =>
                      void save({ ...project, track_ids: project.track_ids.filter((x) => x !== id) })
                    }
                  >
                    ✕
                  </button>
                </span>
              </div>
              {i < project.track_ids.length - 1 && (
                <div className="seam-row">
                  <button
                    className={`seam ${selectedSeam === i ? 'active' : ''}`}
                    title="Open the transition editor for this pair — where the mix between the two tracks is crafted."
                    onClick={() => setSelectedSeam(selectedSeam === i ? null : i)}
                  >
                    ⇅ transition
                  </button>
                  {(() => {
                    const next = byId.get(project.track_ids[i + 1])
                    return t && next
                      ? seamBadges(t, next).map((b) => (
                          <span key={b.text} className={`badge badge-${b.level}`} title={b.title}>
                            {b.text}
                          </span>
                        ))
                      : null
                  })()}
                </div>
              )}
            </li>
          )
        })}
      </ol>

      {selectedSeam !== null &&
        project.track_ids[selectedSeam + 1] !== undefined &&
        (() => {
          const outId = project.track_ids[selectedSeam]
          const inId = project.track_ids[selectedSeam + 1]
          const saved = project.seams.find((s) => s.out_track_id === outId && s.in_track_id === inId)
          return (
            <SeamEditor
              key={`${outId}-${inId}`}
              outTrack={byId.get(outId)!}
              inTrack={byId.get(inId)!}
              savedParams={saved?.params ?? null}
              outGainDb={project.track_gains[outId] ?? 0}
              inGainDb={project.track_gains[inId] ?? 0}
              onCommit={(params) => commitSeam(outId, inId, params)}
            />
          )
        })()}

      <h3>Add analyzed tracks</h3>
      <ul className="picker">
        {analyzed.map((t) => (
          <li key={t.id}>
            <button onClick={() => void save({ ...project, track_ids: [...project.track_ids, t.id] })}>
              +
            </button>{' '}
            {t.filename} <span className="muted">{t.bpm} BPM · {t.camelot}</span>
          </li>
        ))}
        {analyzed.length === 0 && <li className="muted">No (more) analyzed tracks in the library.</li>}
      </ul>
    </section>
  )
}
