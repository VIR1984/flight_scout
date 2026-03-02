// src/components/InputArea.jsx
import { useState, useRef } from 'react'

const QUICK_CHIPS = [
  '🔥 Горящие предложения',
  '🌍 Куда угодно',
  '📊 Отследить цену',
  '↩️ Обратный билет',
  '📅 Гибкие даты',
]

export default function InputArea({ onSend, disabled }) {
  const [text, setText] = useState('')
  const taRef = useRef(null)

  const handleSend = () => {
    const val = text.trim()
    if (!val || disabled) return
    onSend(val)
    setText('')
    if (taRef.current) {
      taRef.current.style.height = 'auto'
    }
  }

  const handleKey = (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      handleSend()
    }
  }

  const handleResize = (e) => {
    const el = e.target
    el.style.height = 'auto'
    el.style.height = Math.min(el.scrollHeight, 100) + 'px'
  }

  return (
    <div className="input-area">
      <div className="quick-actions">
        {QUICK_CHIPS.map(chip => (
          <button
            key={chip}
            className="qa-chip"
            onClick={() => onSend(chip)}
            disabled={disabled}
          >
            {chip}
          </button>
        ))}
      </div>

      <div className="input-row">
        <div className="input-wrapper">
          <textarea
            ref={taRef}
            rows={1}
            value={text}
            onChange={e => setText(e.target.value)}
            onInput={handleResize}
            onKeyDown={handleKey}
            placeholder="Например: Москва — Сочи, 15 апреля, 2 взрослых…"
            disabled={disabled}
          />
          <button className="input-icon-btn" title="Голос">🎤</button>
        </div>
        <button
          className="send-btn"
          onClick={handleSend}
          disabled={disabled || !text.trim()}
          title="Отправить"
        >
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none"
            stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
            <line x1="22" y1="2" x2="11" y2="13"/>
            <polygon points="22 2 15 22 11 13 2 9 22 2"/>
          </svg>
        </button>
      </div>
    </div>
  )
}
