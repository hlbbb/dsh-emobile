import test from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { EventEmitter } from 'node:events'

import {
  candidateSpecs,
  condaRoots,
  EmcliError,
  extractJson,
  findOnPath,
  isShellScript,
  resolveEmcli,
  runEmcliJson,
  runProcess,
} from '../lib/emcli.js'

function tmpDir(prefix = 'dsh-emobile-test-') {
  return fs.mkdtempSync(path.join(os.tmpdir(), prefix))
}

/** 造一个假的 spawn：返回 EventEmitter 子进程。 */
function fakeSpawn({ stdout = '', stderr = '', code = 0, delay = 0 } = {}) {
  return () => {
    const child = new EventEmitter()
    child.stdout = new EventEmitter()
    child.stderr = new EventEmitter()
    child.kill = () => {}
    setTimeout(() => {
      if (stdout) child.stdout.emit('data', Buffer.from(stdout))
      if (stderr) child.stderr.emit('data', Buffer.from(stderr))
      child.emit('close', code)
    }, delay)
    return child
  }
}

test('findOnPath 按 PATH 目录找可执行文件（Windows 扩展名）', () => {
  const dir = tmpDir()
  fs.writeFileSync(path.join(dir, 'emcli.exe'), 'stub')
  const hits = findOnPath(['emcli'], { env: { PATH: dir }, platform: 'win32' })
  assert.equal(hits.length, 1)
  assert.match(hits[0], /emcli\.exe$/i)
})

test('candidateSpecs：配置项优先于 PATH，且包含 conda 根', () => {
  const home = tmpDir()
  const condaRoot = path.join(home, 'miniconda3')
  fs.mkdirSync(path.join(condaRoot, 'Scripts'), { recursive: true })
  fs.writeFileSync(path.join(condaRoot, 'Scripts', 'emcli.exe'), 'stub')
  fs.writeFileSync(path.join(condaRoot, 'python.exe'), 'stub')

  const specs = candidateSpecs({
    config: { emcliPath: 'X:/explicit/emcli.exe', pythonPath: 'X:/explicit/python.exe' },
    env: { PATH: '', CONDA_PREFIX: condaRoot },
    home,
    platform: 'win32',
  })
  assert.equal(specs[0].source, 'config.emcliPath')
  assert.equal(specs[1].source, 'config.pythonPath')
  assert.ok(specs.some((s) => s.source === `conda:${condaRoot}`))
  assert.ok(specs.some((s) => s.kind === 'python' && s.source.startsWith('conda-python:')))
})

test('condaRoots 读取 CONDA_PREFIX、~/.conda/environments.txt 与 envs/*', () => {
  const home = tmpDir()
  const baseEnv = path.join(home, 'miniconda3')
  const namedEnv = path.join(baseEnv, 'envs', 'myenv')
  fs.mkdirSync(namedEnv, { recursive: true })
  const registered = path.join(home, 'registered-env')
  fs.mkdirSync(registered, { recursive: true })
  fs.mkdirSync(path.join(home, '.conda'), { recursive: true })
  fs.writeFileSync(path.join(home, '.conda', 'environments.txt'), `${registered}\n# comment\n`)

  const roots = condaRoots({
    env: { CONDA_PREFIX: baseEnv, ProgramData: path.join(home, 'pd') },
    home,
    platform: 'win32',
  })
  const norm = roots.map((p) => p.toLowerCase())
  assert.ok(norm.includes(baseEnv.toLowerCase()))
  assert.ok(norm.includes(namedEnv.toLowerCase()))
  assert.ok(norm.includes(registered.toLowerCase()))
})

test('resolveEmcli：跳过失败候选，返回首个可用项并缓存', async () => {
  const cache = { value: null }
  const probeCalls = []
  const runProcessImpl = async (command) => {
    probeCalls.push(command)
    if (command === 'bad') return { code: 1, stdout: '', stderr: 'boom' }
    return { code: 0, stdout: 'cli-anything-e-mobile, version 1.0.0', stderr: '' }
  }
  const resolved = await resolveEmcli({
    config: { emcliPath: 'bad', pythonPath: 'good-python' },
    env: { PATH: '' },
    home: tmpDir(),
    platform: 'win32',
    cache,
    runProcessImpl,
  })
  assert.equal(resolved.command, 'good-python')
  assert.equal(resolved.version, '1.0.0')
  assert.equal(cache.value.command, 'good-python')
  // 再次解析直接走缓存，不再探测
  const before = probeCalls.length
  const again = await resolveEmcli({ config: {}, env: { PATH: '' }, home: tmpDir(), cache, runProcessImpl })
  assert.equal(again.command, 'good-python')
  assert.equal(probeCalls.length, before)
})

test('resolveEmcli：全失败抛 EmcliError 并带候选明细', async () => {
  const runProcessImpl = async () => ({ code: 1, stdout: '', stderr: 'nope' })
  await assert.rejects(
    resolveEmcli({
      config: { emcliPath: 'a', pythonPath: 'b' },
      env: { PATH: '' },
      home: tmpDir(),
      platform: 'win32',
      runProcessImpl,
    }),
    (error) => {
      assert.ok(error instanceof EmcliError)
      assert.equal(error.code, 'EMCLI_NOT_FOUND')
      assert.ok(error.candidates.length >= 2)
      return true
    },
  )
})

test('extractJson 容忍前置日志行', () => {
  const parsed = extractJson('初始化...\n{"ok":true,"count":1}\n')
  assert.equal(parsed.ok, true)
  assert.equal(parsed.value.count, 1)
  assert.equal(extractJson('not json').ok, false)
})

test('runEmcliJson：成功解析并置 ok', async () => {
  const resolved = { command: 'emcli', args: [], version: '1.0.0' }
  const res = await runEmcliJson(resolved, ['chat', 'list'], {
    spawnImpl: fakeSpawn({ stdout: '{"ok":true,"count":2,"conversations":[]}', code: 0 }),
  })
  assert.equal(res.ok, true)
  assert.equal(res.data.count, 2)
  assert.deepEqual(res.argv, ['--json', 'chat', 'list'])
})

test('runEmcliJson：退出码 2 视为被白名单拦下', async () => {
  const resolved = { command: 'emcli', args: [], version: '1.0.0' }
  const res = await runEmcliJson(resolved, ['chat', 'send-file', '群', 'f.xlsx', '--yes'], {
    spawnImpl: fakeSpawn({ stdout: '{"ok":false,"blocked":true,"error":"blocked"}', code: 2 }),
  })
  assert.equal(res.ok, false)
  assert.equal(res.blocked, true)
})

test('runEmcliJson：非 JSON 输出报 BAD_JSON', async () => {
  const resolved = { command: 'emcli', args: [], version: '1.0.0' }
  const res = await runEmcliJson(resolved, ['status'], {
    spawnImpl: fakeSpawn({ stdout: 'plain text output', code: 1 }),
  })
  assert.equal(res.ok, false)
  assert.equal(res.code, 'BAD_JSON')
  assert.equal(res.parseError, true)
})

test('runProcess：拒绝 .cmd/.bat（安全，不经过 shell）', async () => {
  assert.equal(isShellScript('C:/x/emcli.cmd'), true)
  assert.equal(isShellScript('C:/x/emcli.bat'), true)
  assert.equal(isShellScript('C:/x/emcli.exe'), false)
  const res = await runProcess('C:/x/emcli.cmd', ['--version'], {})
  assert.equal(res.spawnError, true)
  assert.match(res.stderr, /拒绝执行批处理脚本/)
})

test('candidateSpecs：过滤掉 .cmd 候选', () => {
  const specs = candidateSpecs({ config: { emcliPath: 'C:/x/emcli.cmd' }, env: { PATH: '' }, home: '/nonexistent', platform: 'win32' })
  assert.equal(specs.some((s) => isShellScript(s.command)), false)
})

test('resolveEmcli：emcliPath 指向 .cmd 时抛安全错误', async () => {
  await assert.rejects(
    resolveEmcli({ config: { emcliPath: 'C:/x/emcli.cmd' }, env: { PATH: '' }, home: '/nonexistent', platform: 'win32' }),
    (error) => error instanceof EmcliError && error.code === 'EMCLI_SHELL_SCRIPT_REJECTED',
  )
})

test('runEmcliJson：超时返回 TIMEOUT', async () => {
  const resolved = { command: 'emcli', args: [], version: '1.0.0' }
  const res = await runEmcliJson(resolved, ['status'], {
    timeoutMs: 250,
    spawnImpl: fakeSpawn({ stdout: '{"ok":true}', code: 0, delay: 5000 }),
  })
  assert.equal(res.ok, false)
  assert.equal(res.code, 'TIMEOUT')
  assert.equal(res.timedOut, true)
})
