export const meta = {
  name: 'smart-task',
  description: 'Cost- and quota-aware classify, route, implement, test and review loop with Claude/Codex/OpenRouter fallback',
  whenToUse:
    'Invoked by /cost-aware-orchestrator:smart-task. Requires args {task, timestamp, plugin_root}; optional {classification, task_id, handoff, permission_mode, parent_task_id}. Workflow scripts have no shell or clock, so the caller supplies the timestamp and plugin root.',
  phases: [
    { title: 'Classify', detail: 'deterministic keyword classifier' },
    { title: 'Route', detail: 'engine state, config and routing decisions' },
    { title: 'Scout', detail: 'cheap repository lookup for nontrivial tasks' },
    { title: 'Plan', detail: 'architect pass for complex, critical or supercritical tasks' },
    { title: 'Implement', detail: 'routed engine implements' },
    { title: 'Test', detail: 'configured project test command' },
    { title: 'Fix', detail: 'failing tests: one fix pass, then one escalation' },
    { title: 'Review', detail: 'independent review, preferably on the other engine' },
    { title: 'Fallback', detail: 'usage limit: mark engine, write handoff, switch' },
    { title: 'Finalize', detail: 'record results in engine state' },
  ],
}

// `args` may arrive as a JSON string depending on the invoking runtime.
const ARGS = typeof args === 'string' ? (() => { try { return JSON.parse(args) } catch (e) { return { task: args } } })() : (args || {})

const ROUTES = ['trivial', 'standard', 'complex', 'critical', 'supercritical']
const PLANNED = ['complex', 'critical', 'supercritical']
const PLUGIN = 'cost-aware-orchestrator'
const TASK = String(ARGS.task || '').trim()
const ROOT = String(ARGS.plugin_root || '')
if (!TASK) throw new Error('smart-task requires args.task')
// The plugin root is interpolated into shell commands: allow only a plain absolute path.
if (!/^\/[A-Za-z0-9._\/ -]+$/.test(ROOT)) throw new Error(`smart-task requires a plain absolute args.plugin_root, got ${JSON.stringify(ROOT)}`)

const SCRIPTS = `${ROOT}/scripts`
const q = s => `'${String(s).replace(/'/g, `'\\''`)}'`
const PY = name => `python3 ${q(`${SCRIPTS}/${name}`)}`

function fnv1a(text) {
  let h = 0x811c9dc5
  for (const ch of text) { h ^= ch.codePointAt(0); h = Math.imul(h, 0x01000193) >>> 0 }
  return h.toString(16).padStart(8, '0')
}

// At most 64 characters, so engine_state.py set-effort accepts every task id.
const SAFE_ID = /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/
const slug = TASK.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+/, '').slice(0, 32).replace(/-+$/, '') || 'task'
const TASK_ID = SAFE_ID.test(String(ARGS.task_id || '')) ? ARGS.task_id : `${slug}-${fnv1a(`${TASK}|${ARGS.timestamp || 'no-timestamp'}`)}`
const RUN = `.claude/orchestrator/runs/${TASK_ID}`
const PARENT = String(ARGS.parent_task_id || '')
if (PARENT && !/^[A-Za-z0-9._-]{1,64}$/.test(PARENT)) throw new Error(`invalid args.parent_task_id: ${JSON.stringify(PARENT)}`)
// The workflow never asks questions, so every route is headless; auto mode lets the router pick supercritical effort.
const ROUTE_FLAGS = ` --task-id ${q(TASK_ID)} --headless --permission-mode ${q(ARGS.permission_mode || 'default')}` +
  (PARENT ? ` --parent-task-id ${q(PARENT)}` : '')

// Newline-joined so heredoc delimiters stay alone on their lines; any failure aborts.
const script = lines => ['set -e', ...lines].join('\n')

// Write text to a file without any shell interpretation of its contents.
function heredoc(path, text) {
  const delim = `ORCH_${fnv1a(text)}_EOF`
  if (text.split('\n').includes(delim)) throw new Error('text collides with heredoc delimiter')
  return `cat > ${q(path)} <<'${delim}'\n${text}\n${delim}`
}

// ---------------------------------------------------------------------------
// bash(): workflow scripts cannot spawn processes, so a minimal Haiku agent runs
// each command verbatim and returns its exit code and output.
// ponytail: one cheap agent per command; a native workflow shell would remove this hop.
const BASH_SCHEMA = {
  type: 'object',
  required: ['exit_code', 'stdout', 'stderr'],
  properties: {
    exit_code: { type: 'number' },
    stdout: { type: 'string', description: 'verbatim, last 6000 characters at most' },
    stderr: { type: 'string', description: 'verbatim, last 2000 characters at most' },
  },
}

async function bash(command, label, phaseName, timeoutMs = 120000) {
  const r = await agent(
    `Run exactly this command once with the Bash tool (timeout ${timeoutMs} ms) from the project root.
Do not change it, do not run anything else, do not interpret or fix anything.
Report the exit code, stdout and stderr verbatim.

<command>
${command}
</command>`,
    { label, phase: phaseName, schema: BASH_SCHEMA, model: 'haiku', effort: 'low' },
  )
  if (!r) throw new Error(`bash helper failed to run ${label}`)
  return r
}

function parseJson(text, label) {
  const start = text.indexOf('{'), end = text.lastIndexOf('}')
  try { return JSON.parse(text.slice(start, end + 1)) } catch (e) { throw new Error(`${label}: unparseable JSON output`) }
}

function sections(stdout) {
  const parts = stdout.split(/^===([A-Z_]+)===\s*$/m)
  const out = {}
  for (let i = 1; i < parts.length; i += 2) out[parts[i]] = parts[i + 1].trim()
  return out
}

// ---------------------------------------------------------------------------
// Schemas for agent results.
const SCOUT_SCHEMA = {
  type: 'object',
  required: ['files', 'impact'],
  properties: {
    files: { type: 'array', items: { type: 'string' } },
    symbols: { type: 'array', items: { type: 'string' } },
    impact: { type: 'string' },
    route: { type: 'string', enum: ROUTES },
    route_reason: { type: 'string' },
  },
}
const PLAN_SCHEMA = {
  type: 'object',
  required: ['assessment', 'steps', 'risks', 'test_strategy'],
  properties: {
    assessment: { type: 'string' },
    steps: { type: 'array', items: { type: 'string' } },
    risks: { type: 'array', items: { type: 'string' } },
    test_strategy: { type: 'string' },
    rollback: { type: 'string' },
  },
}
const IMPL_SCHEMA = {
  type: 'object',
  required: ['status', 'summary', 'files_changed', 'completed_work', 'remaining_work', 'usage_limited'],
  properties: {
    status: { type: 'string', enum: ['done', 'partial', 'blocked'] },
    summary: { type: 'string' },
    files_changed: { type: 'array', items: { type: 'string' } },
    completed_work: { type: 'string' },
    remaining_work: { type: 'string', description: 'empty when done' },
    tests_run: { type: 'array', items: { type: 'string' } },
    risks: { type: 'array', items: { type: 'string' } },
    reroute: { type: 'string', enum: ['up', 'down'] },
    reroute_reason: { type: 'string' },
    usage_limited: { type: 'boolean', description: 'true only if a usage/quota limit message stopped the work' },
  },
}
const FINDINGS_SCHEMA = {
  type: 'object',
  required: ['findings'],
  properties: {
    exit_code: { type: 'number', description: 'codex wrapper exit code, when Codex was used' },
    findings: {
      type: 'array',
      items: {
        type: 'object',
        required: ['severity', 'title'],
        properties: {
          severity: { type: 'string', enum: ['blocker', 'high', 'medium', 'low'] },
          category: { type: 'string', enum: ['correctness', 'efficiency', 'best-practice', 'security', 'standards', 'reduction'] },
          title: { type: 'string' },
          file: { type: 'string' },
          detail: { type: 'string' },
        },
      },
    },
  },
}

// ---------------------------------------------------------------------------
phase('Classify')
let classification = ARGS.classification
if (!classification || !ROUTES.includes(classification.route)) {
  const r = await bash(
    script([`mkdir -p ${q(RUN)}`, heredoc(`${RUN}/task.txt`, TASK), `${PY('classify_task.py')} --task-file ${q(`${RUN}/task.txt`)}`]),
    'classify', 'Classify',
  )
  if (r.exit_code !== 0) throw new Error(`classify_task.py exited ${r.exit_code}: ${r.stderr.slice(-500)}`)
  classification = parseJson(r.stdout, 'classify_task.py')
}
let ROUTE = classification.route
log(`${TASK_ID}: ${ROUTE} (${classification.reason})`)

phase('Route')
const routeCmd = (role, extra = '') => `${PY('route_engine.py')} --role ${role} --task-route ${ROUTE}${ROUTE_FLAGS}${extra}`

// implModel 'auto' lets the router derive the implementer, so review picks a different, at least as capable model.
async function routeAll(phaseName, implModel = 'auto') {
  const r = await bash(
    script([
      `mkdir -p ${q(RUN)}`,
      heredoc(`${RUN}/task.txt`, TASK),
      `echo ===STATE===`, `${PY('engine_state.py')} status`,
      `echo ===CONFIG===`, `${PY('engine_state.py')} config`,
      `echo ===IMPLEMENTATION===`, routeCmd('implementation'),
      `echo ===SCOUT===`, routeCmd('scout'),
      `echo ===ARCHITECTURE===`, routeCmd('architecture'),
      `echo ===REVIEW===`, routeCmd('review', ` --implementation-model ${q(implModel)}`),
      `echo ===STYLE_REVIEW===`, routeCmd('style_review'),
    ]),
    'route', phaseName,
  )
  if (r.exit_code !== 0) throw new Error(`routing failed (exit ${r.exit_code}); invalid config stops orchestration: ${r.stderr.slice(-500)}`)
  const s = sections(r.stdout)
  const out = {}
  for (const key of ['STATE', 'CONFIG', 'IMPLEMENTATION', 'SCOUT', 'ARCHITECTURE', 'REVIEW', 'STYLE_REVIEW']) out[key] = parseJson(s[key] || '', key)
  return out
}

// One role, never on the excluded engines (each already hit its limit).
async function routeRole(role, exclude, phaseName, extra = '') {
  const r = await bash(routeCmd(role, `${exclude.map(e => ` --exclude-engine ${q(e)}`).join('')}${extra}`), `route:${role}`, phaseName)
  if (r.exit_code !== 0) throw new Error(`routing failed (exit ${r.exit_code}): ${r.stderr.slice(-500)}`)
  return parseJson(r.stdout, `route:${role}`)
}

let routing = await routeAll('Route')
const CONFIG = routing.CONFIG
let impl = routing.IMPLEMENTATION
log(`implementation → ${impl.engine} ${impl.model} ${impl.effort}: ${impl.reason}`)

const fallbackEvents = []
const rerouteEvents = []
const handoffs = []
let requiresHumanReview = Boolean(impl.requires_human_review)

// Stop the workflow for a human.
function stop(summary, filesChanged = []) {
  return {
    task_id: TASK_ID, route: ROUTE,
    implementation_engine: 'none', implementation_model: '', implementation_effort: '',
    review_engine: 'none', review_findings: [], files_changed: filesChanged, tests: { skipped: true },
    fallback_events: fallbackEvents, reroute_events: rerouteEvents, requires_human_review: true, summary, handoffs,
  }
}
const noEngine = (filesChanged = []) =>
  stop(`No engine available: ${impl.reason}. Wait for a quota reset or approve manual work.`, filesChanged)

async function reroute(source, to, reason) {
  const down = ROUTES.indexOf(to) < ROUTES.indexOf(ROUTE)
  if (rerouteEvents.length || !ROUTES.includes(to) || to === ROUTE || (down && ROUTES.indexOf(ROUTE) >= ROUTES.indexOf('critical'))) {
    log(`reroute ignored: ${source} ${ROUTE} → ${to} (${reason || 'no reason'})`)
    return false
  }
  const from = ROUTE
  ROUTE = to
  routing = await routeAll(source === 'scout' ? 'Scout' : 'Implement')
  impl = routing.IMPLEMENTATION
  requiresHumanReview ||= Boolean(impl.requires_human_review)
  rerouteEvents.push({ source, from, to, reason: reason || '' })
  log(`reroute: ${source} ${from} → ${to} (${reason || 'no reason'})`)
  return true
}

if (impl.engine === 'none') return noEngine()

// ---------------------------------------------------------------------------
// Engine runners. Each returns {limited, ok, summary, files_changed, completed_work, remaining_work}
// and, for an OpenRouter run paused over its budget, `paused: true` with the resume instructions as summary.

// Wrapper command prefix for a non-Claude engine; the prompt file follows. Exit 42 is a usage limit,
// 43 an OpenRouter run paused over a soft budget cap.
const WRAPPERS = {
  codex: (r, sandbox) => `CODEX_SANDBOX=${sandbox} CODEX_MODEL=${q(r.model)} CODEX_EFFORT=${q(r.effort)} bash ${q(`${SCRIPTS}/codex_with_fallback.sh`)}`,
  openrouter: (r, sandbox) => `OPENROUTER_SANDBOX=${sandbox} OPENROUTER_MODEL=${q(r.model)} OPENROUTER_EFFORT=${q(r.effort)} ${PY('openrouter_run.py')}`,
}

// ponytail: one Bash call caps a wrapper run at 10 minutes; split larger work into handoffs.
// `prompt`, when given, is first written to `file`.
async function runWrapper(engine, r, file, sandbox, label, phaseName, prompt) {
  const out = `${RUN}/${engine}`
  const res = await bash([
    `mkdir -p ${q(RUN)}`,
    ...(prompt === undefined ? [] : [heredoc(file, prompt)]),
    `${WRAPPERS[engine](r, sandbox)} ${q(file)} > ${q(`${out}.out`)} 2> ${q(`${out}.err`)}; code=$?`,
    `echo ===OUTPUT===; tail -c 3000 ${q(`${out}.out`)}; echo; tail -c 1000 ${q(`${out}.err`)}`,
    `echo ===FILES===; { git diff --name-only HEAD 2>/dev/null || git diff --name-only; git ls-files --others --exclude-standard; } | sort -u`,
    `if [ "$code" = 43 ]; then run=$(sed -n 's/^OPENROUTER_PAUSED //p' ${q(`${out}.out`)} | tail -n 1); echo ===PAUSED===; echo "$run"; ` +
      `python3 -c 'import json, sys; print(json.load(open(sys.argv[1]))["window"])' ".claude/orchestrator/openrouter-runs/$run/paused.json" 2>/dev/null || true; fi`,
    'exit $code',
  ].join('\n'), label, phaseName, 600000)
  const s = sections(res.stdout)
  const [run, window] = (s.PAUSED || '').split('\n')
  return {
    code: res.exit_code, output: s.OUTPUT || res.stderr,
    files: (s.FILES || '').split('\n').filter(f => f && !f.startsWith('.claude/')),
    paused: res.exit_code === 43 &&
      `OpenRouter paused over its ${window ? `${window} ` : ''}budget: run openrouter_run.py --resume ${run} or --stop ${run}.`,
  }
}

async function writeHandoff(source, target, completed, remaining, files, phaseName, testOutput = '') {
  const maxFiles = (CONFIG.codex || {}).max_files_per_fallback || 5
  const fileArgs = files.slice(0, target === 'claude' ? files.length : maxFiles).map(f => ` --relevant-file ${q(f)}`).join('')
  const r = await bash(
    script([
      `mkdir -p ${q(RUN)}`,
      heredoc(`${RUN}/task.txt`, TASK),
      heredoc(`${RUN}/completed.txt`, completed),
      heredoc(`${RUN}/remaining.txt`, remaining),
      heredoc(`${RUN}/test-output.txt`, testOutput),
      `${PY('write_handoff.py')} --task-id ${q(TASK_ID)} --objective @${q(`${RUN}/task.txt`)} --task-route ${ROUTE}` +
        ` --source-engine ${q(source)} --target-engine ${target}` +
        ` --completed-work @${q(`${RUN}/completed.txt`)} --remaining-work @${q(`${RUN}/remaining.txt`)}` +
        ` --test-output @${q(`${RUN}/test-output.txt`)}${fileArgs}`,
    ]),
    `handoff:${target}`, phaseName,
  )
  if (r.exit_code !== 0) throw new Error(`write_handoff.py exited ${r.exit_code}: ${r.stderr.slice(-500)}`)
  const path = r.stdout.trim().split('\n').pop()
  handoffs.push(path)
  return path
}

let scoutFiles = []
let context = ''
let planned = false

// Architect plan for complex and harder work; appends to `context`, runs at most once.
// Returns a stop result when a human must act first, otherwise null.
async function plan() {
  if (!PLANNED.includes(ROUTE)) return null
  const ar = routing.ARCHITECTURE
  // Headless supercritical outside auto mode: nobody chose the effort, so no top-model call.
  // (An unavailable architecture engine also has no effort; that only skips the plan.)
  if (ar.engine !== 'none' && ar.requires_human_review && !ar.effort) {
    // The generated task id hashes the timestamp, so the rerun must reuse it to find the recorded effort.
    return stop(`Supercritical effort needs a human: ${ar.reason}. Record it with ` +
      `engine_state.py set-effort --task-id ${TASK_ID} --effort <level>, then rerun with task_id: ${TASK_ID}.`)
  }
  if (planned || ARGS.handoff) return null
  planned = true
  phase('Plan')
  const prompt = `Task (${ROUTE}): ${TASK}${context}\n\nAssess and return a minimal implementation plan.`
  if (ar.engine === 'claude') {
    const p = await agent(prompt, {
      agentType: `${PLUGIN}:architect`, label: 'plan', phase: 'Plan', schema: PLAN_SCHEMA, model: ar.model, effort: ar.effort,
    })
    if (p) {
      context += `\n\nArchitect plan:\n${p.steps.map((s, i) => `${i + 1}. ${s}`).join('\n')}\nRisks: ${p.risks.join('; ')}\nTest strategy: ${p.test_strategy}`
      if (p.rollback) context += `\nRollback: ${p.rollback}`
    }
  } else if (ar.engine !== 'none') {
    const w = await runWrapper(ar.engine, ar, `${RUN}/plan.md`, 'read-only', `plan:${ar.engine}`, 'Plan', prompt)
    if (w.paused) return stop(w.paused)
    if (w.code === 0) context += `\n\nArchitect plan (${ar.model}):\n${w.output.slice(-3000)}`
    else log(`plan failed (${ar.engine} exit ${w.code}); continuing without a plan`)
  } else {
    log(`plan skipped: ${ar.reason}`)
  }
  return null
}

// A resumed handoff already carries its context: no scout or plan.
if (ROUTE !== 'trivial' && !ARGS.handoff) {
  phase('Scout')
  const sr = routing.SCOUT
  if (sr.engine === 'claude') {
    const scout = await agent(`Task (${ROUTE}): ${TASK}\n\nFind the files and symbols this task touches and summarize the impact.`, {
      agentType: `${PLUGIN}:scout`, label: 'scout', phase: 'Scout', schema: SCOUT_SCHEMA, model: sr.model, effort: sr.effort,
    })
    if (scout) {
      scoutFiles = scout.files || []
      context += `\n\nScout findings:\nFiles: ${scoutFiles.join(', ')}\nSymbols: ${(scout.symbols || []).join(', ')}\nImpact: ${scout.impact}`
      if (scout.route) await reroute('scout', scout.route, scout.route_reason)
    }
  } else {
    log(`scout skipped: ${sr.reason}`)
  }
}
if (impl.engine === 'none') return noEngine()
const planStop = await plan()
if (planStop) return planStop

async function runClaude(tier, handoffPath) {
  let brief
  if (handoffPath) {
    brief = `Read the orchestration handoff at ${handoffPath}. Complete only its "Remaining work". ` +
      `Do not redo anything listed under "Do not redo". Follow its constraints and output contract.`
  } else {
    const scope = impl.scope === 'single_atomic_task'
      ? '\n\nQuota is constrained: complete only the first atomic step and list everything else under remaining_work.'
      : ''
    brief = `Task (${ROUTE}): ${TASK}${context}${scope}`
  }
  phase('Implement')
  const r = await agent(brief, {
    agentType: `${PLUGIN}:implementer`, label: 'implement:claude', phase: 'Implement', schema: IMPL_SCHEMA,
    model: tier.model, effort: tier.effort,
  })
  if (!r) return { limited: false, ok: false, summary: 'Claude implementer failed (ordinary error, not handed off)', files_changed: [], completed_work: '', remaining_work: TASK }
  return { limited: Boolean(r.usage_limited), ok: r.status === 'done', ...r }
}

// Codex or OpenRouter: the wrapper implements from a handoff file in the workspace.
async function runExternal(engine, tier, handoffPath) {
  phase('Implement')
  const name = engine === 'codex' ? 'Codex' : 'OpenRouter'
  const w = await runWrapper(engine, tier, handoffPath, 'workspace-write', `implement:${engine}`, 'Implement')
  if (w.paused) return { paused: true, limited: false, ok: false, summary: w.paused, files_changed: w.files, completed_work: '', remaining_work: TASK }
  if (w.code === 42) {
    return { limited: true, ok: false, summary: `${name} usage limit`, files_changed: w.files,
      completed_work: `${name} stopped at its usage limit; any partial changes are in the current diff.`, remaining_work: TASK }
  }
  return {
    limited: false, ok: w.code === 0,
    summary: w.code === 0 ? w.output.slice(-1500) : `${name} failed (exit ${w.code}, ordinary error, not handed off): ${w.output.slice(-800)}`,
    files_changed: w.files, completed_work: w.code === 0 ? `${name} implementation run` : '', remaining_work: w.code === 0 ? '' : TASK,
  }
}

const runEngine = (engine, tier, handoffPath) =>
  engine === 'claude' ? runClaude(tier, handoffPath) : runExternal(engine, tier, handoffPath)

async function markLimit(engine, phaseName) {
  await bash(`${PY('engine_state.py')} mark-limit --engine ${engine} --message ${q(`${engine} usage limit during ${TASK_ID}`)}`, `mark-limit:${engine}`, phaseName)
}

// ---------------------------------------------------------------------------
// Implementation with fallback across engines, at most one attempt per engine.
const tried = new Set()
let engine = impl.engine
let tier = { model: impl.model, effort: impl.effort }
let handoffPath = ARGS.handoff && /^[A-Za-z0-9._\/-]+\.md$/.test(ARGS.handoff) ? ARGS.handoff : null
let result = null
const enginesUsed = new Set()

while (true) {
  enginesUsed.add(engine)
  if (engine !== 'claude' && !handoffPath) {
    handoffPath = await writeHandoff('claude', engine, '', `${TASK}${context}`, scoutFiles, 'Implement')
  }
  const out = await runEngine(engine, tier, handoffPath)
  if (out.paused) return stop(out.summary, out.files_changed)
  if (!out.limited && engine === 'claude' && !out.ok && out.reroute) {
    const index = ROUTES.indexOf(ROUTE) + (out.reroute === 'up' ? 1 : out.reroute === 'down' ? -1 : 99)
    if (await reroute('implementer', ROUTES[index], out.reroute_reason)) {
      if (impl.engine === 'none') return noEngine(out.files_changed || [])
      const before = context
      const halt = await plan()  // an upgrade into a planned route gets its plan now
      if (halt) return halt
      handoffPath = await writeHandoff(engine, impl.engine, out.completed_work || '', `${out.remaining_work || TASK}${context.slice(before.length)}`,
        out.files_changed && out.files_changed.length ? out.files_changed : scoutFiles, 'Implement')
      engine = impl.engine
      tier = { model: impl.model, effort: impl.effort }
      continue
    }
  }
  if (!out.limited) { result = out; break }

  phase('Fallback')
  tried.add(engine)
  await markLimit(engine, 'Fallback')
  const next = await routeRole('implementation', [...tried], 'Fallback')
  if (next.engine === 'none') {
    fallbackEvents.push({ phase: 'implementation', from: engine, to: null, reason: `${engine} usage limit; no fallback left` })
    requiresHumanReview = true
    result = out
    break
  }
  const completed = out.completed_work || ''
  const remaining = out.remaining_work || TASK
  handoffPath = await writeHandoff(engine, next.engine, completed, remaining, out.files_changed.length ? out.files_changed : scoutFiles, 'Fallback')
  fallbackEvents.push({ phase: 'implementation', from: engine, to: next.engine, handoff: handoffPath, reason: `${engine} usage limit` })
  log(`fallback: ${engine} → ${next.engine} with ${handoffPath}`)
  requiresHumanReview ||= Boolean(next.requires_human_review)
  engine = next.engine
  tier = { model: next.model, effort: next.effort }
}

// ---------------------------------------------------------------------------
phase('Test')
const testCommand = ((CONFIG.commands || {}).test || '').trim()
let tests = { command: testCommand || null, skipped: true }
const fixEvents = []

async function runTests(label, phaseName) {
  const t = await bash(
    `mkdir -p ${q(RUN)}; ( ${testCommand} ) > ${q(`${RUN}/test.out`)} 2>&1; code=$?; tail -c 4000 ${q(`${RUN}/test.out`)}; exit $code`,
    label, phaseName, 600000,
  )
  return { command: testCommand, skipped: false, exit_code: t.exit_code, passed: t.exit_code === 0, output_tail: t.stdout.slice(-2000) }
}

// Escalation after a failed fix pass: another engine at this route, else the architecture model
// when it differs from the implementer. Supercritical's architecture model is the top model, which never implements.
async function escalation() {
  const limited = fallbackEvents.map(e => e.from)
  const other = await routeRole('implementation', [engine, ...limited], 'Fix')
  if (other.engine !== 'none') return { engine: other.engine, tier: { model: other.model, effort: other.effort }, why: 'escalation to another engine' }
  const top = routing.ARCHITECTURE
  if (ROUTE !== 'supercritical' && top.engine !== 'none' && top.effort && top.model !== tier.model && !limited.includes(top.engine)) {
    return { engine: top.engine, tier: { model: top.model, effort: top.effort }, why: 'escalation to the architecture model' }
  }
  return null
}

if (testCommand && result.ok !== false) {
  tests = await runTests('test', 'Test')
  // Quality ladder, bounded: one fix pass on the same engine, then one escalation.
  for (const pass of ['fix', 'escalation']) {
    if (tests.passed) break
    const a = pass === 'fix' ? { engine, tier, why: 'fix pass on the same engine' } : await escalation()
    if (!a) break
    phase('Fix')
    log(`tests failed (exit ${tests.exit_code}): ${a.why} (${a.engine} ${a.tier.model})`)
    const hp = await writeHandoff(engine, a.engine, 'The implementation is in the current diff; the project tests fail.',
      'Make the failing tests pass with the smallest change. Do not rewrite or broaden the implementation.',
      result.files_changed && result.files_changed.length ? result.files_changed : scoutFiles, 'Fix', tests.output_tail)
    const out = await runEngine(a.engine, a.tier, hp)
    enginesUsed.add(a.engine)
    if (out.paused) return stop(out.summary, result.files_changed || [])
    if (out.limited) {
      await markLimit(a.engine, 'Fix')
      fallbackEvents.push({ phase: 'fix', from: a.engine, to: null, reason: `${a.engine} usage limit during fix` })
      break
    }
    fixEvents.push({ engine: a.engine, model: a.tier.model, why: a.why })
    result.files_changed = [...new Set([...(result.files_changed || []), ...(out.files_changed || [])])]
    tests = await runTests(`test:after-${a.engine}`, 'Fix')
  }
  if (!tests.passed) requiresHumanReview = true
}

// ---------------------------------------------------------------------------
phase('Review')
// The first routing assumed the routed implementer; re-route once the actual one differs or an engine hit its limit.
if (fallbackEvents.length || tier.model !== routing.IMPLEMENTATION.model) routing = await routeAll('Review', tier.model)
let review = routing.REVIEW
let reviewFindings = []
let reviewEngine = review.engine
let styleFindings = []
const styleRoute = routing.STYLE_REVIEW
const styleReview = styleRoute.engine === 'claude' && (result.files_changed || []).length > 0
if (review.requires_human_review) requiresHumanReview = true

const SKIP_NOTE = '\nThe style review is skipped: also cover standards, best-practice and reduction.'
const reviewBrief = `Objective: ${TASK}
Task id: ${TASK_ID}; route: ${ROUTE}; implemented by: ${[...enginesUsed].join(' then ')}.
Review the current uncommitted changes (git diff HEAD plus untracked files outside .claude/).
Files reported changed: ${(result.files_changed || []).join(', ') || '(see diff)'}
Tests: ${tests.skipped ? 'not configured' : `${tests.command} → exit ${tests.exit_code}\n${tests.output_tail}`}${styleReview ? '' : SKIP_NOTE}`

// Cheap standards/best-practice/reduction pass; null means the agent failed.
let lintResult = { skipped: true }
async function runStyleReview() {
  if (!styleReview) return []
  const lintCommand = ((CONFIG.commands || {}).lint || '').trim()
  let lint = 'not configured'
  if (lintCommand) {
    const l = await bash(`out=$( ( ${lintCommand} ) 2>&1 ); code=$?; printf '%s' "$out" | tail -c 4000; exit $code`, 'lint', 'Review', 600000)
    lint = l.stdout
    lintResult = { command: lintCommand, exit_code: l.exit_code, passed: l.exit_code === 0, output_tail: l.stdout.slice(-2000) }
  }
  const out = await agent(`${reviewBrief}\nLint output (do not repeat these findings):\n${lint}`, {
    agentType: `${PLUGIN}:style-reviewer`, label: 'review:style', phase: 'Review', schema: FINDINGS_SCHEMA,
    model: styleRoute.model, effort: styleRoute.effort,
  })
  return out ? out.findings : null
}

// Main review on the routed engine: Claude reviews directly, Codex/OpenRouter through the relay
// agent and the engine's wrapper. Returns {findings, exit_code?} or null when the agent failed.
function reviewOn(r) {
  const brief = styleFindings === null ? reviewBrief + SKIP_NOTE : reviewBrief
  if (r.engine === 'claude') {
    return agent(brief, {
      agentType: `${PLUGIN}:verifier`, label: 'review:claude', phase: 'Review', schema: FINDINGS_SCHEMA, model: r.model, effort: r.effort,
    })
  }
  const request = `${RUN}/review.md`
  return agent(`${brief}\n\nPlugin scripts directory: ${SCRIPTS}\nWrite the request to ${request} and run it on ${r.engine} model ${r.model} with:\n` +
    `${WRAPPERS[r.engine](r, 'read-only')} ${q(request)}`, {
    agentType: `${PLUGIN}:codex-reviewer`, label: `review:${r.engine}`, phase: 'Review', schema: FINDINGS_SCHEMA, model: 'haiku', effort: 'low',
  })
}

if (!result.ok && result.status !== 'partial') {
  reviewEngine = 'none'
  log('review skipped: implementation did not complete')
} else if (review.engine !== 'none') {
  let out
  ;[out, styleFindings] = await Promise.all([reviewOn(review), runStyleReview()])
  if (out && out.exit_code === 42) {
    phase('Fallback')
    await markLimit(review.engine, 'Fallback')
    const next = await routeRole('review', [review.engine], 'Fallback', ` --implementation-model ${q(tier.model)}`)
    if (next.engine === 'none') {
      fallbackEvents.push({ phase: 'review', from: review.engine, to: null, reason: `${review.engine} usage limit; no fallback left` })
      requiresHumanReview = true
      out = { findings: [] }
    } else {
      fallbackEvents.push({ phase: 'review', from: review.engine, to: next.engine, reason: `${review.engine} usage limit` })
      requiresHumanReview ||= Boolean(next.requires_human_review)
      reviewEngine = next.engine
      out = await reviewOn(next)
    }
  }
  // A wrapper error or an OpenRouter pause (exit 43) leaves the work unreviewed.
  reviewFindings = out && !out.exit_code ? out.findings : null
}
if (reviewFindings === null) { reviewFindings = []; requiresHumanReview = true; log('review agent failed; human review required') }
if (styleFindings === null) { styleFindings = []; requiresHumanReview = true; log('style review failed; human review required') }
if (reviewFindings.some(f => f.severity === 'blocker')) requiresHumanReview = true
const SEVERITY = ['blocker', 'high', 'medium', 'low']
const rank = f => (SEVERITY.indexOf(f.severity) + 5) % 5  // unknown severities sort last
reviewFindings = [...reviewFindings, ...styleFindings].sort((a, b) => rank(a) - rank(b))

// ---------------------------------------------------------------------------
phase('Finalize')
const success = result.ok && (tests.skipped || tests.passed)
const limitedEngines = new Set(fallbackEvents.map(e => e.from))
const records = [...enginesUsed, reviewEngine]
  .filter((e, i, all) => e !== 'none' && all.indexOf(e) === i && !limitedEngines.has(e))
  .map(e => `${PY('engine_state.py')} record-result --engine ${e} --task-id ${q(TASK_ID)} --status ${success ? 'success' : 'failed'} > /dev/null`)
if (records.length) await bash(records.join(' && '), 'record-result', 'Finalize')

return {
  task_id: TASK_ID,
  route: ROUTE,
  implementation_engine: engine,
  implementation_model: tier.model,
  implementation_effort: tier.effort,
  review_engine: reviewEngine,
  review_findings: reviewFindings,
  files_changed: result.files_changed || [],
  tests,
  lint: lintResult,
  fallback_events: fallbackEvents,
  reroute_events: rerouteEvents,
  fix_events: fixEvents,
  requires_human_review: requiresHumanReview,
  summary: result.summary,
  remaining_work: result.remaining_work || '',
  handoffs,
}
