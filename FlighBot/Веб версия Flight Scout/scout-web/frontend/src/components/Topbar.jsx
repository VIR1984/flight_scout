// src/components/Topbar.jsx
export default function Topbar({ onReset }) {
  return (
    <header className="topbar">
      <div>
        <div className="topbar-title">Поиск авиабилетов</div>
        <div className="topbar-sub">
          <span className="status-dot" />
          Scout активен · обновлено только что
        </div>
      </div>
      <div className="topbar-actions">
        <button className="btn btn-ghost" onClick={onReset}>
          Новый поиск
        </button>
      </div>
    </header>
  )
}
