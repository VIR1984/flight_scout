// src/components/Sidebar.jsx
const NAV = [
  { id: 'chat',     icon: '💬', label: 'Поиск билетов' },
  { id: 'deals',    icon: '🔥', label: 'Горящие предложения', badge: '12' },
  { id: 'tracker',  icon: '📊', label: 'Отслеживание цен' },
  { id: 'anywhere', icon: '🗺️', label: 'Куда угодно' },
  { id: 'multi',    icon: '🔁', label: 'Мультирейс' },
]

export function Sidebar({ activePage, onNavigate }) {
  return (
    <aside className="sidebar">
      <div className="logo">
        <div className="logo-icon">✈️</div>
        Scout
      </div>

      <nav className="nav">
        {NAV.map(item => (
          <button
            key={item.id}
            className={`nav-item ${activePage === item.id ? 'active' : ''}`}
            onClick={() => onNavigate(item.id)}
          >
            <span className="nav-icon">{item.icon}</span>
            {item.label}
            {item.badge && <span className="nav-badge">{item.badge}</span>}
          </button>
        ))}
      </nav>

      <div className="sidebar-bottom">
        <div className="user-card">
          <div className="avatar">А</div>
          <div className="user-info">
            <div className="user-name">Аноним</div>
            <div className="user-plan">Scout Free</div>
          </div>
        </div>
      </div>
    </aside>
  )
}

export default Sidebar
