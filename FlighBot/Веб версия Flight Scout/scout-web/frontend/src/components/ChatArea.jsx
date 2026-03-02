// src/components/ChatArea.jsx
import { useEffect, useRef } from 'react'
import Message from './Message'
import TypingIndicator from './TypingIndicator'

export default function ChatArea({ messages, isTyping }) {
  const bottomRef = useRef(null)

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages, isTyping])

  return (
    <div className="chat-area">
      <div className="chat-inner">
        {messages.map((msg, i) => (
          <Message key={msg.id ?? i} message={msg} />
        ))}
        {isTyping && <TypingIndicator />}
        <div ref={bottomRef} />
      </div>
    </div>
  )
}
