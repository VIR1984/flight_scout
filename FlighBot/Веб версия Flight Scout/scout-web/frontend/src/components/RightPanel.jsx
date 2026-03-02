// src/components/RightPanel.jsx
import { useState, useEffect } from 'react'

const DEMO_DEALS = [
  { origin: 'MOW', destination: 'AER', price: 3800, depart_date: '2026-03-22', transfers: 0, tag: 'HOT' },
  { origin: 'MOW', destination: 'AYT', price: 6900, depart_date: '2026-04-05', transfers: 0, tag: 'SALE' },
  { origin: 'LED', destination: 'DXB', price: 11200, depart_date: '2026-04-10', transfers: 0, tag: 'NEW' },
  { origin: 'MOW', destination: 'BKK', price: 24500, depart_date: '2026-05-01', transfers: 1, tag: 'HOT' },
]

const TAG_STYLE = {
  HOT:  { bg: 'rgba(255,92,92,0.15)',  color: '#ff5c5c', label: '🔥 HOT' },
  SALE: { bg: 'rgba(245,197,66,0.12)', color: '#f5c542', label: '💰 −38%' },
  NEW:  { bg: 'rgba(79,139,255,0.12)', color: '#4f8bff', label: '✨ NEW' },
}

export default function RightPanel({ sessionId }) {
  const [tab,   setTab]   = useState('deals')
  const [deals, setDeals] = useState(DEMO_DEALS)

  useEffect(() => {
    // Попробовать загрузить реальные данные
    fetch('/api/deals/hot?origin=MOW&limit=8')
      .then(r => r.json())
      .then(d => { if (d.deals?.length) setDeals(d.deals) })
      .catch(() => {/* используем демо */})
  }, [])

  return (
    <aside className="right-panel">
      <div className="panel-header">
        <span>Сводка</span>
        <div className="panel-tabs">
          <button className={`panel-tab ${tab === 'deals' ? 'active' : ''}`} onClick={() => setTab('deals')}>Deals</button>
          <button className={`panel-tab ${tab === 'track' ? 'active' : ''}`} onClick={() => setTab('track')}>Track</button>
        </div>
      </div>

      <div className="panel-content">
        {tab === 'deals' && (
          <>
            <div className="section-label">🔥 Горящие сегодня</div>
            {deals.map((d, i) => {
              const tag = TAG_STYLE[d.tag] || TAG_STYLE.HOT
              return (
                <div className="mini-deal" key={i}>
                  <div className="mini-deal-top">
                    <span className="mini-route">{d.origin} → {d.destination}</span>
                    <span className="mini-price">{d.price?.toLocaleString('ru')} ₽</span>
                  </div>
                  <div className="mini-meta">{d.depart_date} · {d.transfers === 0 ? 'Прямой' : `${d.transfers} пер.`}</div>
                  <span className="mini-tag" style={{ background: tag.bg, color: tag.color }}>{tag.label}</span>
                </div>
              )
            })}

            <div className="section-label" style={{ marginTop: 20 }}>📈 Тренды</div>
            <div className="trends-card">
              {[['MOW→AER', '▼ −15%', '#22d46c'], ['MOW→AYT', '▲ +8%', '#ff5c5c'], ['LED→DXB', '▼ −22%', '#22d46c']].map(([r, ch, c]) => (
                <div className="trend-row" key={r}>
                  <span>{r}</span>
                  <span style={{ color: c }}>{ch}</span>
                </div>
              ))}
            </div>
          </>
        )}

        {tab === 'track' && (
          <>
            <div className="section-label">📊 Отслеживание</div>
            <div className="tracker-item">
              <div className="tracker-route">✈ MOW → AER · 15 марта</div>
              <div className="tracker-price-row">
                <span className="tracker-current">4 200 ₽</span>
                <span className="tracker-change down">▼ −18%</span>
              </div>
              <div className="tracker-bar-wrap">
                <div className="tracker-bar" style={{ width: '42%' }} />
              </div>
              <div className="tracker-range">
                <span>мин: 3 800 ₽</span>
                <span>макс: 7 500 ₽</span>
              </div>
            </div>
            <button className="btn btn-ghost w-full" style={{ marginTop: 12, fontSize: 13 }}>
              + Добавить маршрут
            </button>
          </>
        )}
      </div>
    </aside>
  )
}
