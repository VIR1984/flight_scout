// src/hooks/useSession.js
import { useState, useEffect } from 'react'

export function useSession() {
  const [sessionId, setSessionId] = useState(null)
  const [loading, setLoading]     = useState(true)

  useEffect(() => {
    async function init() {
      try {
        // Проверить существующую сессию
        const meRes = await fetch('/api/auth/me', { credentials: 'include' })
        const me    = await meRes.json()

        if (me.session_id) {
          setSessionId(me.session_id)
        } else {
          // Создать новую
          const newRes = await fetch('/api/auth/session', {
            method: 'POST',
            credentials: 'include',
          })
          const newSession = await newRes.json()
          setSessionId(newSession.session_id)
        }
      } catch (e) {
        console.error('Session error:', e)
        // Fallback: локальный UUID
        setSessionId(crypto.randomUUID())
      } finally {
        setLoading(false)
      }
    }
    init()
  }, [])

  return { sessionId, loading }
}
