import { chmod, readFile, writeFile } from 'node:fs/promises'
import { pathToFileURL } from 'node:url'
const [modulePath, configPath, authPath, stdoutPath, stderrPath, destination] = process.argv.slice(2)
const config = JSON.parse(await readFile(configPath, 'utf8'))
const auth = JSON.parse(await readFile(authPath, 'utf8'))
const previous = []
if (process.argv.slice(8).includes('--secrets-stdin')) {
  const chunks = []
  for await (const chunk of process.stdin) chunks.push(chunk)
  const value = JSON.parse(Buffer.concat(chunks).toString('utf8'))
  if (!Array.isArray(value) || value.some(item => typeof item !== 'string')) throw new Error('Invalid secret input')
  previous.push(...value)
}
const { createRedactor } = await import(pathToFileURL(modulePath).href)
const secrets = [config.loginom.password, config.loginom.api_key, ...previous, ...Object.values(auth).flatMap(value => [value.access, value.refresh, value.key])].filter(Boolean)
const redactor = createRedactor(secrets)
const lines = []
for (const line of (await readFile(stdoutPath, 'utf8')).split(/\r?\n/)) {
  if (!line) continue
  let value
  try { value = redactor.redact(JSON.parse(line)) }
  catch { value = { type: 'text', text: redactor.text(line) } }
  lines.push(JSON.stringify(value))
}
await writeFile(destination + '/events.jsonl', lines.join('\n') + '\n', { mode: 0o600 })
await writeFile(destination + '/stderr.txt', redactor.text(await readFile(stderrPath, 'utf8')), { mode: 0o600 })
for (const name of ['result.json', 'cleanup.json']) {
  const path = destination + '/oracle/' + name
  const body = await readFile(path, 'utf8').catch(error => {
    if (error.code === 'ENOENT') return null
    throw error
  })
  if (body === null) continue
  await writeFile(path, JSON.stringify(redactor.redact(JSON.parse(body))) + '\n', { mode: 0o600 })
  await chmod(path, 0o600)
}
