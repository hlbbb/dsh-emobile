import test from 'node:test'
import assert from 'node:assert/strict'
import { EventEmitter } from 'node:events'
import { createEmcliClient } from '../lib/client.js'

function probeStub(map) {
  return async (command) => map[command] || { code: 1, stdout: '', stderr: 'not found' }
}

/** 假 spawn：返回一个带 stdout/stderr 的 EventEmitter 子进程。 */
function fakeSpawn({ stdout = '', stderr = '', code = 0 } = {}) {
  return () => {
    const child = new EventEmitter()
    child.stdout = new EventEmitter()
    child.stderr = new EventEmitter()
    child.kill = () => {}
    setTimeout(() => {
      if (stdout) child.stdout.emit('data', Buffer.from(stdout))
      if (stderr) child.stderr.emit('data', Buffer.from(stderr))
      child.emit('close', code)
    }, 0)
    return child
  }
}

test('client.diagnose：命中首个可用候选并写入缓存', async () => {
  const client = createEmcliClient({
    config: { emcliPath: 'bad', pythonPath: 'good', probeTimeoutMs: 1000 },
    env: { PATH: '' },
    home: '/nonexistent',
    platform: 'win32',
    runProcessImpl: probeStub({
      bad: { code: 1, stdout: '', stderr: 'boom' },
      good: { code: 0, stdout: 'cli-anything-e-mobile, version 1.0.0', stderr: '' },
    }),
  })
  const diagnosis = await client.diagnose({ budgetMs: 3000 })
  assert.equal(diagnosis.ok, true)
  assert.equal(diagnosis.resolved.command, 'good')
  assert.equal(diagnosis.resolved.version, '1.0.0')
})

test('client.run：解析后以 --json 执行并解析结果', async () => {
  const client = createEmcliClient({
    config: { pythonPath: 'good', probeTimeoutMs: 1000 },
    env: { PATH: '' },
    home: '/nonexistent',
    platform: 'win32',
    runProcessImpl: probeStub({ good: { code: 0, stdout: 'v1.0.0', stderr: '' } }),
  })
  const res = await client.run(['policy', 'show'], {
    spawnImpl: fakeSpawn({ stdout: '{"ok":true,"count":1,"allowlist":["x"]}', code: 0 }),
  })
  assert.equal(res.ok, true)
  assert.equal(res.data.count, 1)
})
