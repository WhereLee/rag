// SSE 流式解析单测：data: 格式兼容（有无空格）、事件分发、兜底 done、Abort 传播。
// 核心价值：问答链路的 SSE 协议解析是前端高频回归点（浏览器 E2E 无法逐事件断言）。
import { describe, it, expect, vi, beforeEach } from 'vitest'

// 隔离外部依赖：router 依赖浏览器环境（createWebHistory），auth store 依赖 pinia 实例
vi.mock('../router', () => ({ default: {} }))
vi.mock('../stores/auth', () => ({
  useAuthStore: () => ({ token: 'test-token' })
}))

const { qaApi } = await import('./client')

/** 构造 SSE 响应体：把事件串按块切分（模拟网络分块，含半行边界）。 */
function sseResponse(chunks) {
  const encoder = new TextEncoder()
  const stream = new ReadableStream({
    start(controller) {
      for (const c of chunks) {
        controller.enqueue(encoder.encode(c))
      }
      controller.close()
    }
  })
  return { ok: true, body: stream }
}

async function collectEvents(chunks, signal) {
  global.fetch = vi.fn().mockResolvedValue(sseResponse(chunks))
  const events = []
  await qaApi.askStream('测试问题', 'sess-1', (evt) => events.push(evt), true, signal)
  return events
}

beforeEach(() => {
  vi.restoreAllMocks()
})

describe('qaApi.askStream SSE 解析', () => {
  it('解析 Python 格式（data: 带空格）完整事件流', async () => {
    const events = await collectEvents([
      'data: {"type":"meta","cached":false,"citations":[]}\n\n',
      'data: {"type":"thinking","text":"思考中"}\n\n',
      'data: {"type":"delta","text":"回答一"}\n\n',
      'data: {"type":"delta","text":"回答二"}\n\n',
      'data: {"type":"done","qa_log_id":42}\n\n'
    ])
    expect(events.map(e => e.type)).toEqual(['meta', 'thinking', 'delta', 'delta', 'done', 'done'])
    expect(events[0]).toMatchObject({ type: 'meta', cached: false })
    expect(events[4]).toMatchObject({ type: 'done', qa_log_id: 42 })
    // 流结束的兜底 done 无 qa_log_id（前端用它在异常中断时收尾，不得覆盖真实 id）
    expect(events[5]).toEqual({ type: 'done' })
  })

  it('兼容 Spring 格式（data: 无空格）', async () => {
    const events = await collectEvents([
      'data:{"type":"meta","cached":true}\n\n',
      'data:{"type":"delta","text":"缓存答案"}\n\n',
      'data:{"type":"done","qa_log_id":7}\n\n'
    ])
    expect(events[0]).toMatchObject({ type: 'meta', cached: true })
    expect(events[2]).toMatchObject({ type: 'done', qa_log_id: 7 })
  })

  it('跨 chunk 边界的半行缓冲（事件被网络分块拆开）', async () => {
    const events = await collectEvents([
      'data: {"type":"del',
      'ta","text":"被拆',
      '开"}\n\ndata: {"type":"done","qa_log_id":9}\n\n'
    ])
    expect(events[0]).toEqual({ type: 'delta', text: '被拆开' })
    expect(events[1]).toMatchObject({ type: 'done', qa_log_id: 9 })
  })

  it('单 chunk 包含多个事件', async () => {
    const events = await collectEvents([
      'data: {"type":"delta","text":"a"}\n\ndata: {"type":"delta","text":"b"}\n\ndata: {"type":"done"}\n\n'
    ])
    expect(events.filter(e => e.type === 'delta')).toHaveLength(2)
    expect(events.at(-1)).toEqual({ type: 'done' })
  })

  it('畸形行跳过不崩溃', async () => {
    const events = await collectEvents([
      'data: {"type":"meta"}\n\nnot-json-line\n\n',
      'data: {"type":"done","qa_log_id":1}\n\n'
    ])
    expect(events.some(e => e.type === 'done')).toBe(true)
    expect(events.some(e => e.type === 'error')).toBe(false)
  })

  it('AbortController 中止后 reject AbortError（停止生成）', async () => {
    const controller = new AbortController()
    controller.abort()
    // mock fetch 模拟 abort 语义：signal 已中止时立即 reject AbortError
    global.fetch = vi.fn().mockImplementation((_url, opts) => {
      if (opts?.signal?.aborted) {
        return Promise.reject(new DOMException('The operation was aborted.', 'AbortError'))
      }
      return Promise.resolve({ ok: true, body: new ReadableStream() })
    })
    await expect(
      qaApi.askStream('问题', '', () => {}, true, controller.signal)
    ).rejects.toMatchObject({ name: 'AbortError' })
  })

  it('HTTP 错误抛出带状态码的错误', async () => {
    global.fetch = vi.fn().mockResolvedValue({ ok: false, status: 400, text: () => 'bad' })
    await expect(qaApi.askStream('问题', '', () => {})).rejects.toThrow(/HTTP 400/)
  })
})
