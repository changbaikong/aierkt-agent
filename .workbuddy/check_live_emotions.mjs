const base = 'http://127.0.0.1:8765'

async function runScenario(name, message) {
  const conversationId = `expression-${name}-${Date.now()}`
  const controller = new AbortController()
  const events = []
  const streamResponse = await fetch(`${base}/api/conversations/${conversationId}/events`, {
    signal: controller.signal,
  })
  const reader = streamResponse.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  let resolveCompleted
  const completed = new Promise((resolve) => { resolveCompleted = resolve })
  const reading = (async () => {
    try {
      while (true) {
        const { value, done } = await reader.read()
        if (done) break
        buffer += decoder.decode(value, { stream: true })
        const blocks = buffer.split('\n\n')
        buffer = blocks.pop() || ''
        for (const block of blocks) {
          const event = block.match(/^event: (.+)$/m)?.[1]
          const data = block.match(/^data: (.+)$/m)?.[1]
          if (!event || !data) continue
          const payload = JSON.parse(data)
          events.push({ event, ...payload })
          if (event === 'chat.completed' || event === 'chat.failed') resolveCompleted()
        }
      }
    } catch (error) {
      if (error.name !== 'AbortError') throw error
    }
  })()

  await fetch(`${base}/api/chat`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ message, conversation_id: conversationId }),
  })
  await Promise.race([
    completed,
    new Promise((_, reject) => setTimeout(() => reject(new Error(`${name} timed out`)), 90000)),
  ])
  controller.abort()
  await reading
  const cues = events.filter((event) => event.event === 'portrait.changed' && event.source === 'llm')
  const terminal = events.find((event) => event.event === 'chat.completed' || event.event === 'chat.failed')
  return { name, cues: cues.map(({ expression, text_offset, cue_seq }) => ({ expression, text_offset, cue_seq })), reply: terminal?.reply, failed: terminal?.error }
}

const results = []
results.push(await runScenario('angry', '我故意把你最珍惜的礼物摔碎了，还觉得很好笑。'))
results.push(await runScenario('sad', '这是我们最后一次见面了。以后永远不会再见，我也不会再回来。'))
console.log(JSON.stringify(results, null, 2))
