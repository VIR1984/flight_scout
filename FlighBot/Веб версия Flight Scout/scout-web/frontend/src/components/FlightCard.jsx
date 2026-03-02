// src/components/FlightCard.jsx

const AIRLINE_NAMES = {
  SU: 'Аэрофлот', S7: 'S7 Airlines', DP: 'Победа',
  U6: 'Уральские', FZ: 'flydubai', PC: 'Pegasus',
  TG: 'Thai Airways', MS: 'EgyptAir',
}

export default function FlightCard({ flight }) {
  const {
    airline, flight_number, dep_time, arr_time,
    origin, dest, origin_name, dest_name,
    duration, price, stops_label, link,
  } = flight

  const airlineName = AIRLINE_NAMES[airline] || airline || '—'
  const isDirectFlight = stops_label === 'Прямой'

  return (
    <div className="flight-card" onClick={() => link && window.open(link, '_blank')}>
      <div className="fc-header">
        <div className="fc-airline">
          <div className="fc-logo">{airline?.[0] || '✈'}</div>
          <span>{airlineName} · {flight_number}</span>
        </div>
        <div className="fc-price">{price?.toLocaleString('ru')} ₽</div>
      </div>

      <div className="fc-route">
        <div className="fc-point">
          <div className="fc-time">{dep_time}</div>
          <div className="fc-iata">{origin}</div>
          <div className="fc-city">{origin_name}</div>
        </div>

        <div className="fc-line">
          <div className="fc-duration">{duration}</div>
          <div className="fc-track">
            <div className="track-dash" />
            <span className="track-icon">✈</span>
            <div className="track-dash" />
          </div>
          <div className={`fc-stops ${isDirectFlight ? 'direct' : 'layover'}`}>
            {stops_label}
          </div>
        </div>

        <div className="fc-point right">
          <div className="fc-time">{arr_time}</div>
          <div className="fc-iata">{dest}</div>
          <div className="fc-city">{dest_name}</div>
        </div>
      </div>

      <div className="fc-footer">
        <span className="fc-meta">Эконом</span>
        <a
          className="btn-book"
          href={link}
          target="_blank"
          rel="noopener noreferrer"
          onClick={e => e.stopPropagation()}
        >
          Перейти к покупке →
        </a>
      </div>
    </div>
  )
}
