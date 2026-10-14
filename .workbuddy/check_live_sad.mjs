const base = 'http://127.0.0.1:8765'

async function run(message, suffix) {
  const conversationId = `expression-sad-${suffix}-${Date.now()}`
  const controller = new AbortController()
  const response = await fetch(`${base}/api/conversations/${conversationId}/events`, { signal: controller.signal })
  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  const events = []
  let buffer = ''
  let resolveDone
  const done = new Promise((resolve) => { resolveDone = resolve })
  const reading = (async () => {
    try {
      while (true) {
        const chunk = await reader.read()
        if (chunk.done) break
        buffer += decoder.decode(chunk.value, { stream: true })
        const blocks = buffer.split('\n\n')
        buffer = blocks.pop() || ''
        for (const block of blocks) {
          const event = block.match(/^event: (.+)$/m)?.[1]
          const raw = block.match(/^data: (.+)$/m)?.[1]
          if (!event || !raw) continue
          const payload = JSON.parse(raw)
          events.push({ event, ...payload })
          if (event === 'chat.completed' || event === 'chat.failed') resolveDone()
        }
      }
    } catch (error) {
      if (error.name !== 'AbortError') throw error
    }
  })()
  await fetch(`${base}/api/chat`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ message, conversation_id: conversationId }),
  })
  await Promise.race([done, new Promise((_, reject) => setTimeout(() => reject(new Error('timeout')), 90000))])
  controller.abort(); await reading
  return {
    cues: events.filter((event) => event.event === 'portrait.changed' && event.source === 'llm')
      .map(({ expression, text_offset, cue_seq }) => ({ expression, text_offset, cue_seq })),
    reply: events.find((event) => event.event === 'chat.completed')?.reply,
  }
}

console.log(JSON.stringify([
  await run('这是我们最后一次见面了。以后永远不会再见，我也不会再回来。', 'goodbye'),
  await run('我把我们一起拍的照片全烧掉了，那些回忆再也找不回来了。', 'loss'),
], null, 2))
