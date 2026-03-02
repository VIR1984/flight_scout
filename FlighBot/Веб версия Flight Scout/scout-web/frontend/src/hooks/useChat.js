// src/hooks/useChat.js
import { useState, useEffect, useRef, useCallback } from 'react'

const WS_URL = import.meta.env.VITE_WS_URL || ''  // пусто → используем proxy

function getWsUrl(sessionId) {
  const base = WS_URL || `ws://${window.location.host}`
  return `${base}/ws/${sessionId}`
}

const WELCOME = {
  id: 'welcome',
  role: 'bot',
  text: 'Привет! Я **Scout** — ваш ИИ-помощник по поиску авиабилетов ✈️\n\nОткуда летим?',
  buttons: ['✏️ Ввести маршрут', '🌍 Куда угодно', '🔥 Горящие предложения'],
  ts: new Date(),
}

export function useChat(sessionId) {
  const [messages, setMessages] = useState([WELCOME])
  const [isTyping, setIsTyping]   = useState(false)
  const wsRef  = useRef(null)
  const idRef  = useRef(0)

  const nextId = () => ++idRef.current

  // ── Подключение WebSocket ─────────────────────────────────────
  useEffect(() => {
    if (!sessionId) return

    const ws = new WebSocket(getWsUrl(sessionId))
    wsRef.current = ws

    ws.onopen = () => console.log('[WS] connected')

    ws.onmessage = (event) => {
      const data = JSON.parse(event.data)

      if (data.type === 'typing') {
        setIsTyping(true)
        return
      }

      if (data.type === 'message') {
        setIsTyping(false)
        setMessages(prev => [...prev, {
          id:      nextId(),
          role:    'bot',
          text:    data.text,
          buttons: data.buttons || [],
          flights: data.flights || [],
          total:   data.total,
          ts:      new Date(),
        }])
      }
    }

    ws.onerror = (e) => console.error('[WS] error', e)
    ws.onclose = () => console.log('[WS] closed')

    return () => ws.close()
  }, [sessionId])

  // ── Отправить сообщение ────────────────────────────────────────
  const send = useCallback((text) => {
    if (!text.trim()) return

    // Добавить сообщение пользователя
    setMessages(prev => [...prev, {
      id:   nextId(),
      role: 'user',
      text,
      ts:   new Date(),
    }])

    // Отправить на сервер
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({ type: 'message', text }))
    } else {
      console.warn('[WS] не подключён')
    }
  }, [])

  // ── Сбросить чат ───────────────────────────────────────────────
  const resetChat = useCallback(() => {
    setMessages([WELCOME])
    setIsTyping(false)
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({ type: 'reset' }))
    }
  }, [])

  return { messages, send, resetChat, isTyping }
}
