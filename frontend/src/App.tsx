import { useCallback, useEffect, useState } from 'react'
import { api } from './api/client'
import type { Track } from './types'
import { LibraryView } from './views/LibraryView'
import { SetTimeline } from './views/SetTimeline'

type View = 'library' | 'set'

export default function App() {
  const [view, setView] = useState<View>('library')
  const [tracks, setTracks] = useState<Track[]>([])
  const [error, setError] = useState<string | null>(null)

  const refreshTracks = useCallback(async () => {
    try {
      setTracks(await api.listTracks())
      setError(null)
    } catch (e) {
      setError(String(e))
    }
  }, [])

  // Poll while any analysis is still in flight.
  useEffect(() => {
    void refreshTracks()
    const timer = setInterval(() => {
      void refreshTracks()
    }, 2000)
    return () => clearInterval(timer)
  }, [refreshTracks])

  return (
    <div className="app">
      <header>
        <h1>Mash-Up Maker</h1>
        <nav>
          <button className={view === 'library' ? 'active' : ''} onClick={() => setView('library')}>
            Library
          </button>
          <button className={view === 'set' ? 'active' : ''} onClick={() => setView('set')}>
            Set
          </button>
        </nav>
      </header>
      {error && <div className="error">Backend unreachable: {error}</div>}
      {view === 'library' && <LibraryView tracks={tracks} onChanged={refreshTracks} />}
      {view === 'set' && <SetTimeline tracks={tracks} />}
    </div>
  )
}
