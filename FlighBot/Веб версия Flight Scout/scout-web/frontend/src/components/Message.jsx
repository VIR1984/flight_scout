// src/components/Message.jsx
import FlightCard from './FlightCard'

// Простой markdown: **bold**, \n → <br>
function renderText(text) {
  if (!text) return null
  return text
    .replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>')
    .replace(/\*(.*?)\*/g, '<em>$1</em>')
    .split('\n')
    .map((line, i) => `${line}${i < text.split('\n').length - 1 ? '<br/>' : ''}`)
    .join('')
}

export default function Message({ message, onButtonClick }) {
  const isBot = message.role === 'bot'
  const time  = message.ts
    ? new Date(message.ts).toLocaleTimeString('ru', { hour: '2-digit', minute: '2-digit' })
    : ''

  return (
    <div className={`msg ${isBot ? 'bot' : 'user'}`}>
      {isBot && <div className="msg-avatar bot-avatar">✈️</div>}

      <div className="msg-content">
        <div
          className="msg-bubble"
          dangerouslySetInnerHTML={{ __html: renderText(message.text) }}
        />

        {/* Inline кнопки */}
        {isBot && message.buttons?.length > 0 && (
          <div className="inline-btns">
            {message.buttons.map(btn => (
              <button
                key={btn}
                className="inline-btn"
                onClick={() => onButtonClick?.(btn)}
              >
                {btn}
              </button>
            ))}
          </div>
        )}

        {/* Карточки рейсов */}
        {isBot && message.flights?.length > 0 && (
          <div className="flight-cards">
            {message.flights.map((f, i) => (
              <FlightCard key={i} flight={f} />
            ))}
          </div>
        )}

        <div className="msg-time">{time}</div>
      </div>

      {!isBot && <div className="msg-avatar user-avatar">А</div>}
    </div>
  )
}
