import test from 'node:test'
import assert from 'node:assert/strict'
import { buildPatchBlock, parseConfigEntries, upsertPatchText } from '../tools/install-to-profile.mjs'

test('upsertPatchText：新增块保留其它条目', () => {
  const raw = '- id: other\n  name: other\n  config:\n    a: 1\n\n- id: dsh-autotable\n  disabled: false\n  config:\n    projectRoot: X\n'
  const out = upsertPatchText(raw, { emcliPath: 'C:/x/emcli.exe' })
  assert.match(out, /- id: other/)
  assert.match(out, /projectRoot: X/)
  assert.match(out, /- id: dsh-emobile/)
  assert.match(out, /emcliPath: "C:\/x\/emcli.exe"/)
})

test('upsertPatchText：重装时保留已有 allowSend/defaultLimit（M1 回归）', () => {
  const raw = [
    '- id: dsh-emobile',
    '  name: dsh-emobile',
    '  disabled: false',
    '  config:',
    '    emcliPath: "C:/old/emcli.exe"',
    '    allowSend: true',
    '    defaultLimit: 100',
    '',
    '- id: after',
    '  disabled: false',
  ].join('\n') + '\n'
  // 只切到 pythonPath，不传 emcliPath —— 旧值与其他自定义键都应保留
  const out = upsertPatchText(raw, { pythonPath: 'C:/conda/python.exe' })
  assert.match(out, /allowSend: true/)
  assert.match(out, /defaultLimit: 100/)
  assert.match(out, /emcliPath: "C:\/old\/emcli.exe"/)
  assert.match(out, /pythonPath: "C:\/conda\/python\.exe"/)
  assert.match(out, /- id: after/)
})

test('upsertPatchText：显式传值覆盖同名键', () => {
  const raw = '- id: dsh-emobile\n  name: dsh-emobile\n  disabled: false\n  config:\n    emcliPath: "C:/old.exe"\n    allowSend: true\n'
  const out = upsertPatchText(raw, { emcliPath: 'C:/new.exe' })
  assert.match(out, /emcliPath: "C:\/new\.exe"/)
  assert.equal(out.includes('C:/old.exe'), false)
  assert.match(out, /allowSend: true/)
})

test('parseConfigEntries 只取 config 下的扁平键', () => {
  const lines = [
    '- id: dsh-emobile',
    '  name: dsh-emobile',
    '  config:',
    '    emcliPath: "C:/a.exe"',
    '    allowSend: true',
    '- id: next',
  ]
  const entries = parseConfigEntries(lines, 0, 6)
  assert.deepEqual(entries.map((e) => e.key), ['emcliPath', 'allowSend'])
})

test('buildPatchBlock 输出合法结构', () => {
  assert.deepEqual(buildPatchBlock({}, []), ['- id: dsh-emobile', '  name: dsh-emobile', '  disabled: false'])
  const lines = buildPatchBlock({ emcliPath: 'C:/x.exe' }, [])
  assert.equal(lines[3], '  config:')
  assert.match(lines[4], /emcliPath/)
})
