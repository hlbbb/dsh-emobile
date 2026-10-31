import test from 'node:test'
import assert from 'node:assert/strict'
import {
  buildChatHistory,
  buildChatList,
  buildContactSearch,
  buildPolicyShow,
  buildSendFile,
  buildSendImage,
  buildSendText,
  gateSpec,
  SEND_NOT_ALLOWED_MESSAGE,
  shouldUseTextFile,
  TOOL_SPEC_BY_NAME,
  TOOL_SPECS,
  toolTimeoutMs,
} from '../lib/tool-specs.js'

const ALLOWED_TYPES = new Set(['string', 'number', 'integer', 'boolean', 'array', 'object'])

test('所有 Tool 规格结构合法且名称唯一', () => {
  const names = new Set()
  for (const spec of TOOL_SPECS) {
    assert.equal(typeof spec.name, 'string')
    assert.ok(/^emobile_/.test(spec.name), `工具名应带 emobile_ 前缀: ${spec.name}`)
    assert.equal(names.has(spec.name), false, `工具名重复: ${spec.name}`)
    names.add(spec.name)
    assert.equal(typeof spec.description, 'string')
    assert.ok(spec.description.length > 10)
    assert.ok(['read', 'send', 'diagnose'].includes(spec.kind))
    assert.equal(typeof spec.build, 'function')
    for (const [key, prop] of Object.entries(spec.parameters || {})) {
      assert.ok(ALLOWED_TYPES.has(prop.type), `${spec.name}.${key} 类型非法: ${prop.type}`)
      assert.equal(typeof prop.description, 'string', `${spec.name}.${key} 缺 description`)
    }
  }
  assert.ok(TOOL_SPECS.length >= 12)
})

test('读/写工具划分正确：发送类 3 个，且全部要求 confirm', () => {
  const send = TOOL_SPECS.filter((s) => s.kind === 'send')
  assert.deepEqual(send.map((s) => s.name).sort(), [
    'emobile_send_file',
    'emobile_send_image',
    'emobile_send_text',
  ])
  for (const spec of send) {
    assert.equal(spec.parameters.confirm?.type, 'boolean')
  }
})

test('gateSpec：默认禁止发送', () => {
  const spec = TOOL_SPEC_BY_NAME.emobile_send_text
  assert.equal(gateSpec(spec, { confirm: true }, { allowSend: false }), SEND_NOT_ALLOWED_MESSAGE)
})

test('gateSpec：allowSend=true 但未 confirm 仍拒绝', () => {
  const spec = TOOL_SPEC_BY_NAME.emobile_send_file
  const msg = gateSpec(spec, { confirm: false }, { allowSend: true })
  assert.match(msg, /confirm: true/)
})

test('gateSpec：allowSend=true 且 confirm=true 放行', () => {
  const spec = TOOL_SPEC_BY_NAME.emobile_send_image
  assert.equal(gateSpec(spec, { confirm: true }, { allowSend: true }), null)
})

test('gateSpec：读类永远放行', () => {
  const spec = TOOL_SPEC_BY_NAME.emobile_list_conversations
  assert.equal(gateSpec(spec, {}, { allowSend: false }), null)
})

test('buildChatList 使用 defaultLimit 与过滤开关', () => {
  const built = buildChatList({ unread: true, name: '示例' }, { config: { defaultLimit: 20 } })
  assert.deepEqual(built.argv, ['chat', 'list', '-n', '20', '--unread', '--name', '示例'])
  assert.deepEqual(buildChatList({ limit: 5 }, { config: {} }).argv, ['chat', 'list', '-n', '5'])
})

test('buildChatHistory 支持游标', () => {
  const built = buildChatHistory({ conversation: '示例群A', limit: 10, before: '123' })
  assert.deepEqual(built.argv, ['chat', 'history', '示例群A', '-n', '10', '--before', '123'])
})

test('buildContactSearch 支持 --simple', () => {
  assert.deepEqual(
    buildContactSearch({ keyword: '张三', limit: 8, simple: true }),
    { argv: ['contact', 'search', '张三', '-n', '8', '--simple'] },
  )
})

test('buildSendText：内联正文与 --text-file 两种路径', () => {
  assert.deepEqual(
    buildSendText({ conversation: '群A', text: '你好\n第二行' }).argv,
    ['chat', 'send-gui', '群A', '你好\n第二行', '--yes'],
  )
  assert.deepEqual(
    buildSendText({ conversation: '群A', textFile: 'C:/tmp/body.txt' }).argv,
    ['chat', 'send-gui', '群A', '--text-file', 'C:/tmp/body.txt', '--yes'],
  )
})

test('shouldUseTextFile：多行/超长/以 - 开头走 --text-file', () => {
  assert.equal(shouldUseTextFile('你好'), false)
  assert.equal(shouldUseTextFile('第一行\n第二行'), true)
  assert.equal(shouldUseTextFile('a\r\nb'), true)
  assert.equal(shouldUseTextFile('x'.repeat(1501)), true)
  assert.equal(shouldUseTextFile('-看起来像选项'), true)
  assert.equal(shouldUseTextFile(''), false)
})

test('buildSendFile/buildSendImage 带 --yes 与可选参数', () => {
  assert.deepEqual(
    buildSendFile({ conversation: '群A', filePath: 'C:/a.xlsx', attempts: 20, interval: 30, noWait: true }).argv,
    ['chat', 'send-file', '群A', 'C:/a.xlsx', '--yes', '--attempts', '20', '--interval', '30', '--no-wait'],
  )
  assert.deepEqual(
    buildSendImage({ conversation: '群A', filePath: 'C:/a.png', wait: 45 }).argv,
    ['chat', 'send-image', '群A', 'C:/a.png', '--yes', '--wait', '45'],
  )
})

test('buildPolicyShow 无参', () => {
  assert.deepEqual(buildPolicyShow(), { argv: ['policy', 'show'] })
})

test('toolTimeoutMs 区分读/写与固定值', () => {
  assert.equal(toolTimeoutMs({ kind: 'read' }, { requestTimeoutMs: 3000 }), 3000)
  assert.equal(toolTimeoutMs({ kind: 'send' }, { sendTimeoutMs: 9000 }), 9000)
  assert.equal(toolTimeoutMs({ kind: 'read', timeoutMs: 1234 }, { requestTimeoutMs: 3000 }), 1234)
  assert.equal(toolTimeoutMs({ kind: 'read' }, {}), 60000)
})
