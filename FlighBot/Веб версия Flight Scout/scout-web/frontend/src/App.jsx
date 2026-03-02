// src/App.jsx  — главный компонент приложения Scout Web

import { useState, useEffect, useRef, useCallback } from 'react'
import Sidebar from './components/Sidebar'
import ChatArea from './components/ChatArea'
import InputArea from './components/InputArea'
import RightPanel from './components/RightPanel'
import Topbar from './components/Topbar'
import { useChat } from './hooks/useChat'
import { useSession } from './hooks/useSession'
import './styles/app.css'

export default function App() {
  const { sessionId, loading: sessionLoading } = useSession()
  const { messages, send, resetChat, isTyping } = useChat(sessionId)
  const [activePage, setActivePage] = useState('chat')

  if (sessionLoading) {
    return (
      <div className="app-loading">
        <div className="loading-plane">✈️</div>
      </div>
    )
  }

  return (
    <div className="app-container">
      <Sidebar activePage={activePage} onNavigate={setActivePage} />
      <main className="main">
        <Topbar onReset={resetChat} />
        <ChatArea messages={messages} isTyping={isTyping} />
        <InputArea onSend={send} disabled={!sessionId} />
      </main>
      <RightPanel sessionId={sessionId} />
    </div>
  )
}
