// Run workflows/smart-task.js outside Claude Code with a mocked agent().
// bash() helper requests execute for real in the project; Claude subagents return
// scripted results. Usage: node workflow_harness.mjs <project-dir> <scenario-json>
// Prints {result, calls, logs} as JSON.
import { readFileSync } from 'node:fs'
import { spawnSync } from 'node:child_process'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const root = join(dirname(fileURLToPath(import.meta.url)), '..')
const [project, scenarioJson] = process.argv.slice(2)
const scenario = JSON.parse(scenarioJson)
const calls = []
const logs = []

async function agent(prompt, opts = {}) {
  const command = /<command>\n([\s\S]*)\n<\/command>/.exec(prompt)
  if (command) {
    const r = spawnSync('bash', ['-c', command[1]], { cwd: project, encoding: 'utf8', env: { ...process.env, CLAUDE_PROJECT_DIR: project } })
    calls.push({ label: opts.label, kind: 'bash', exit_code: r.status, command: command[1] })
    return { exit_code: r.status, stdout: r.stdout, stderr: r.stderr }
  }
  calls.push({ label: opts.label, kind: 'agent', agentType: opts.agentType, model: opts.model, effort: opts.effort, prompt, schema: opts.schema })
  const scripted = scenario.agents[opts.label]
  if (Array.isArray(scripted)) return scripted.shift() ?? null
  return scripted ?? null
}

const src = readFileSync(join(root, 'workflows', 'smart-task.js'), 'utf8').replace(/^export const meta/m, 'const meta')
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor
const run = new AsyncFunction('args', 'agent', 'phase', 'log', src)
const result = await run(
  { task: scenario.task, timestamp: '20261029T000000Z', plugin_root: root, ...(scenario.args || {}) },
  agent, () => {}, line => logs.push(String(line)),
)
console.log(JSON.stringify({ result, calls, logs }))
