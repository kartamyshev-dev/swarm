import { readFile, writeFile } from 'node:fs/promises'
import { pathToFileURL } from 'node:url'
const [modulePath, configPath, authPath, stdoutPath, stderrPath, destination] = process.argv.slice(2)
const config = JSON.parse(await readFile(configPath, 'utf8'))
const auth = JSON.parse(await readFile(authPath, 'utf8'))
const { createRedactor } = await import(pathToFileURL(modulePath).href)
const secrets = [config.loginom.password, config.loginom.api_key, ...Object.values(auth).flatMap(value => [value.access, value.refresh, value.key])].filter(Boolean)
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
