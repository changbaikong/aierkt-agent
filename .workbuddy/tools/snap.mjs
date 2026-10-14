// 无依赖 CDP 探针：本机 Chrome 无头打开 dev server，点某个导航页 → 截图 + 抓计算样式。
// 用法: node .workbuddy/tools/snap.mjs [url] [outPng]
//   env PAGE=聊天|设置|任务|记忆库|工作   （默认 聊天）
//   env FLOW=1                          点「气泡记录」而不是导航页
//   env CONV_ID=<会话id>                先注入 localStorage 指定会话
//   env PROBE='<js>'                    额外执行的表达式（返回可序列化值）
import { spawn } from 'node:child_process'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

const URL_ = process.argv[2] || 'http://127.0.0.1:5173/'
const OUT = process.argv[3] || path.resolve('.workbuddy/tools/out/snap.png')
const PAGE = process.env.FLOW === '1' ? '气泡记录' : (process.env.PAGE || '聊天')
const CHROME = fs.existsSync('C:/Program Files/Google/Chrome/Application/chrome.exe')
  ? 'C:/Program Files/Google/Chrome/Application/chrome.exe'
  : 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe'
const PORT = 9333

const profile = path.join(os.tmpdir(), `wb-cdp-${Date.now()}`)
const child = spawn(CHROME, [
  '--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
  '--disable-extensions', `--remote-debugging-port=${PORT}`, `--user-data-dir=${profile}`,
  '--window-size=1440,900', 'about:blank',
], { stdio: 'ignore' })

const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

async function devtoolsUrl() {
  for (let i = 0; i < 80; i++) {
    try {
      const r = await fetch(`http://127.0.0.1:${PORT}/json/version`)
      if (r.ok) return (await r.json()).webSocketDebuggerUrl
    } catch {}
    await sleep(250)
  }
  throw new Error('devtools endpoint not ready')
}

let ws, nextId = 1
const pending = new Map()
function send(method, params = {}, sessionId) {
  const id = nextId++
  const msg = { id, method, params }
  if (sessionId) msg.sessionId = sessionId
  ws.send(JSON.stringify(msg))
  return new Promise((res, rej) => pending.set(id, { res, rej }))
}

const wsu = await devtoolsUrl()
ws = new WebSocket(wsu)
await new Promise((r, j) => { ws.onopen = r; ws.onerror = j })
ws.onmessage = (ev) => {
  const m = JSON.parse(ev.data)
  if (m.id && pending.has(m.id)) {
    const { res, rej } = pending.get(m.id)
    pending.delete(m.id)
    m.error ? rej(new Error(m.error.message)) : res(m.result)
  }
}

const { targetId } = await send('Target.createTarget', { url: 'about:blank' })
const { sessionId } = await send('Target.attachToTarget', { targetId, flatten: true })
const S = sessionId
await send('Page.enable', {}, S)
await send('Runtime.enable', {}, S)
await send('Emulation.setDeviceMetricsOverride', { width: 1440, height: 900, deviceScaleFactor: 1, mobile: false }, S)
if (process.env.CONV_ID) {
  await send('Page.addScriptToEvaluateOnNewDocument', {
    source: `localStorage.setItem('arcueid-web-active-conversation', ${JSON.stringify(process.env.CONV_ID)});`,
  }, S)
}
if (process.env.EXTRA_INIT) {
  await send('Page.addScriptToEvaluateOnNewDocument', { source: process.env.EXTRA_INIT }, S)
}
await send('Page.navigate', { url: URL_ }, S)
await sleep(4000)

const evalJs = async (expression) => {
  const r = await send('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true }, S)
  if (r.exceptionDetails) return 'EXCEPTION: ' + (r.exceptionDetails.exception?.description || r.exceptionDetails.text)
  return r.result?.value
}

console.log('PAGE =', PAGE, '| title =', await evalJs('document.title'))
console.log('nav:', await evalJs('[...document.querySelectorAll(".nav-tab")].map(b=>b.textContent.trim()).join(" | ")'))
console.log('click:', await evalJs(`(()=>{const b=[...document.querySelectorAll('button')].find(x=>x.textContent.trim().includes(${JSON.stringify(PAGE)})); if(!b) return 'NO BUTTON'; b.click(); return 'ok'})()`))
await sleep(1800)

console.log('theme attr:', await evalJs('document.documentElement.dataset.theme || "(none)"'))
console.log('stack probe:', await evalJs(`(()=>{
  const t=[...document.querySelectorAll('.nav-tab')][0];
  if(!t) return 'no .nav-tab';
  const r=t.getBoundingClientRect();
  const hit=document.elementFromPoint(r.left+r.width/2, r.top+r.height/2);
  const nav=getComputedStyle(document.querySelector('.app-navigation'));
  return JSON.stringify({ topAtNavCenter: hit? (hit.className||hit.tagName) : null, navBg: nav.backgroundColor, navPos: nav.position });
})()`))
console.log('page probe:', await evalJs(`(()=>{
  const roots=[...document.querySelectorAll('.settings-page,.work-page,.memory-page,.app-shell')];
  const out=roots.map(e=>{const cs=getComputedStyle(e);return {cls:e.className,bg:cs.backgroundColor,color:cs.color}});
  const nav=[...document.querySelectorAll('.nav-tab')].map(b=>{const cs=getComputedStyle(b);return {t:b.textContent.trim(),bg:cs.backgroundColor,color:cs.color,border:cs.borderTopColor}});
  return JSON.stringify({roots:out,nav:nav.slice(0,2),body:getComputedStyle(document.body).backgroundColor});
})()`))
if (process.env.PROBE) console.log('PROBE:', await evalJs(process.env.PROBE))

const shot = await send('Page.captureScreenshot', { format: 'png' }, S)
fs.mkdirSync(path.dirname(OUT), { recursive: true })
fs.writeFileSync(OUT, Buffer.from(shot.data, 'base64'))
console.log('screenshot:', OUT)

ws.close()
child.kill()
try { fs.rmSync(profile, { recursive: true, force: true }) } catch {}
process.exit(0)
