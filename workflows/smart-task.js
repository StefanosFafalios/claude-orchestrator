export const meta = {
  name: 'smart-task',
  description: 'Cost- and quota-aware classify, route, implement, test and review loop with Claude/Codex/OpenRouter fallback',
  whenToUse:
    'Invoked by /cost-aware-orchestrator:smart-task. Requires args {task, timestamp, plugin_root}; optional {classification, decider, task_id, handoff, permission_mode, parent_task_id}. Workflow scripts have no shell or clock, so the caller supplies the timestamp and plugin root.',
  phases: [
    { title: 'Classify', detail: 'cheap OpenRouter decider, else the keyword classifier' },
    { title: 'Route', detail: 'engine state, config and routing decisions' },
    { title: 'Scout', detail: 'cheap repository lookup for nontrivial tasks' },
    { title: 'Plan', detail: 'architect pass for complex, critical or supercritical tasks' },
    { title: 'Implement', detail: 'routed engine implements' },
    { title: 'Test', detail: 'configured project test command' },
    { title: 'Fix', detail: 'failing tests: one fix pass, then one escalation' },
    { title: 'Review', detail: 'independent review, preferably on the other engine' },
    { title: 'Fallback', detail: 'usage limit: mark engine, write handoff, switch' },
    { title: 'Finalize', detail: 'record results in engine state' },
    { title: 'Docs', detail: 'update AI instruction docs after tests and review pass' },
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

const DOCS_SCHEMA = {
  type: 'object',
  required: ['changed', 'files'],
  properties: {
    changed: { type: 'boolean' },
    files: { type: 'array', items: { type: 'string' } },
    reason: { type: 'string' },
  },
}
// The body of agents/doc-updater.md, verbatim (a test checks it): Codex and OpenRouter get it in the prompt.
const DOC_RULES = `Check whether the finished task changed behavior, commands, paths, conventions or workflows that the project's AI instruction docs describe. If it did, make the smallest accurate edit; otherwise edit nothing and report \`no change\`.

Scope (edit only these):
- \`docs/AI_CONTEXT.md\` and any nested \`AI_CONTEXT.md\`, \`CLAUDE.md\`, \`AGENTS.md\`
- \`.claude/skills/**/SKILL.md\` and \`.claude/agents/*.md\`
- when the project is the cost-aware-orchestrator plugin itself (\`.claude-plugin/plugin.json\`), also its \`skills/\`, \`agents/\` and \`commands/\` docs

Rules:
- When \`CLAUDE.md\` or \`AGENTS.md\` only points to a shared \`AI_CONTEXT.md\` (the \`sync_instructions.py\` layout), edit the shared file; never turn a pointer back into a copy.
- Never edit user-facing docs (README, CHANGELOG, other files under \`docs/\`), code, tests or config.
- Describe only what the diff shows; never invent behavior.
- Use Bash only to read: \`git diff\`, \`git log\`, \`git status\`.
- Never commit.

Return JSON: \`{"changed": true|false, "files": [edited paths], "reason": "one line"}\`.`
// The scope above as paths. ponytail: plugin skill/agent/command docs count in any project (the workflow
// cannot see whether the project is the plugin); check .claude-plugin/plugin.json if that matters.
const DOC_SCOPE = /(^|\/)(AI_CONTEXT|CLAUDE|AGENTS)\.md$|^\.claude\/(skills\/.+\/SKILL|agents\/[^/]+)\.md$|^(skills|agents|commands)\/.+\.md$/

// ---------------------------------------------------------------------------
phase('Classify')
let classification = ARGS.classification
if (!classification || !ROUTES.includes(classification.route)) {
  const taskFile = q(`${RUN}/task.txt`)
  const keywords = `${PY('classify_task.py')} --task-file ${taskFile}`
  // The cheap decider never blocks: its exit 3 (or any failure) falls back to the keyword classifier.
  const classify = ARGS.decider === false ? keywords : `${PY('decide_route.py')} < ${taskFile} || ${keywords}`
  const r = await bash(script([`mkdir -p ${q(RUN)}`, heredoc(`${RUN}/task.txt`, TASK), classify]), 'classify', 'Classify')
  if (r.exit_code !== 0) throw new Error(`classification exited ${r.exit_code}: ${r.stderr.slice(-500)}`)
  classification = parseJson(r.stdout, 'classification')
}
let ROUTE = classification.route
if (classification.ask === true && ROUTES.includes(classification.alternative)) {
  // The workflow cannot ask the user: take the higher tier.
  ROUTE = ROUTES[Math.max(ROUTES.indexOf(ROUTE), ROUTES.indexOf(classification.alternative))]
  log(`decider unsure (${classification.confidence}): took ${ROUTE}`)
}
log(`${TASK_ID}: ${ROUTE} (${classification.reason})`)

phase('Route')
const routeCmd = (role, extra = '') => `${PY('route_engine.py')} --role ${role} --task-route ${ROUTE}${ROUTE_FLAGS}${extra}`

// implModels ['auto'] lets the router derive the implementer; otherwise it lists every model that wrote code,
// so review picks a model that is none of them and at least as capable as the strongest.
const implFlags = models => models.map(m => ` --implementation-model ${q(m)}`).join('')
async function routeAll(phaseName, implModels = ['auto']) {
  const r = await bash(
    script([
      `mkdir -p ${q(RUN)}`,
      heredoc(`${RUN}/task.txt`, TASK),
      `echo ===STATE===`, `${PY('engine_state.py')} status`,
      `echo ===CONFIG===`, `${PY('engine_state.py')} config`,
      `echo ===IMPLEMENTATION===`, routeCmd('implementation'),
      `echo ===SCOUT===`, routeCmd('scout'),
      `echo ===ARCHITECTURE===`, routeCmd('architecture'),
      `echo ===REVIEW===`, routeCmd('review', implFlags(implModels)),
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
    docs: { engine: 'none', changed: false, files: [] },
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

// Scout on the routed engine: Claude's scout agent, or the engine's wrapper read-only. A constrained
// pick (one atomic task, human review) is not worth the quota. Returns a stop result or null.
async function scoutPass() {
  const prompt = `Task (${ROUTE}): ${TASK}\n\nFind the files and symbols this task touches and summarize the impact.`
  const tried = []
  let sr = routing.SCOUT
  while (true) {
    if (sr.engine === 'none' || sr.scope === 'single_atomic_task' || sr.requires_human_review) {
      log(`scout skipped: ${sr.engine === 'none' ? sr.reason : `${sr.engine} is constrained`}`)
      return null
    }
    if (sr.engine === 'claude') {
      const scout = await agent(prompt, {
        agentType: `${PLUGIN}:scout`, label: 'scout', phase: 'Scout', schema: SCOUT_SCHEMA, model: sr.model, effort: sr.effort,
      })
      if (scout) {
        scoutFiles = scout.files || []
        context += `\n\nScout findings:\nFiles: ${scoutFiles.join(', ')}\nSymbols: ${(scout.symbols || []).join(', ')}\nImpact: ${scout.impact}`
        if (scout.route) await reroute('scout', scout.route, scout.route_reason)
      }
      return null
    }
    const w = await runWrapper(sr.engine, sr, `${RUN}/scout.md`, 'read-only', `scout:${sr.engine}`, 'Scout', `${prompt}\nDo not modify files.`)
    if (w.paused) return stop(w.paused)
    if (w.code !== 42) {
      if (w.code === 0) context += `\n\nScout findings (${sr.model}):\n${w.output.slice(-3000)}`
      else log(`scout failed (${sr.engine} exit ${w.code}); continuing without it`)
      return null
    }
    await markLimit(sr.engine, 'Scout')
    tried.push(sr.engine)
    const next = await routeRole('scout', tried, 'Scout')
    fallbackEvents.push({ phase: 'scout', from: sr.engine, to: next.engine === 'none' ? null : next.engine, reason: `${sr.engine} usage limit` })
    sr = next
  }
}

// A resumed handoff already carries its context: no scout or plan.
if (ROUTE !== 'trivial' && !ARGS.handoff) {
  phase('Scout')
  const scoutStop = await scoutPass()
  if (scoutStop) return scoutStop
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
const writers = new Set()  // every model that wrote code; none of them may review it

while (true) {
  enginesUsed.add(engine)
  writers.add(tier.model)
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
    writers.add(a.tier.model)
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
// The first routing assumed the routed implementer; re-route once the actual writers differ or an engine hit its limit.
if (ROUTE !== 'trivial' && (fallbackEvents.length || writers.size > 1 || tier.model !== routing.IMPLEMENTATION.model)) {
  routing = await routeAll('Review', [...writers])
}
let review = routing.REVIEW
let reviewFindings = []
let reviewEngine = review.engine
let styleFindings = []
const styleRoute = routing.STYLE_REVIEW
const styleReview = styleRoute.engine !== 'none' && (result.files_changed || []).length > 0
if (review.requires_human_review) requiresHumanReview = true

const SKIP_NOTE = '\nThe style review is skipped: also cover standards, best-practice and reduction.'
const reviewBrief = `Objective: ${TASK}
Task id: ${TASK_ID}; route: ${ROUTE}; implemented by: ${[...enginesUsed].join(' then ')}.
Review the current uncommitted changes (git diff HEAD plus untracked files outside .claude/).
Files reported changed: ${(result.files_changed || []).join(', ') || '(see diff)'}
Tests: ${tests.skipped ? 'not configured' : `${tests.command} → exit ${tests.exit_code}\n${tests.output_tail}`}${styleReview ? '' : SKIP_NOTE}`

// Runs the configured lint command and returns its output for the style reviewer.
let lintResult = { skipped: true }
async function runLint() {
  const lintCommand = ((CONFIG.commands || {}).lint || '').trim()
  if (!lintCommand) return 'not configured'
  const l = await bash(`out=$( ( ${lintCommand} ) 2>&1 ); code=$?; printf '%s' "$out" | tail -c 4000; exit $code`, 'lint', 'Review', 600000)
  lintResult = { command: lintCommand, exit_code: l.exit_code, passed: l.exit_code === 0, output_tail: l.stdout.slice(-2000) }
  return l.stdout
}

// Cheap standards/best-practice/reduction pass on the routed engine: Claude's style-reviewer, or the
// relay agent and the engine's wrapper. Returns {findings, exit_code?} or null when the agent failed.
function styleOn(r, lint) {
  const brief = `${reviewBrief}\nLint output (do not repeat these findings):\n${lint}`
  if (r.engine === 'claude') {
    return agent(brief, {
      agentType: `${PLUGIN}:style-reviewer`, label: 'review:style', phase: 'Review', schema: FINDINGS_SCHEMA, model: r.model, effort: r.effort,
    })
  }
  const request = `${RUN}/style-review.md`
  return agent(`${brief}\n\nThis is the style review: ask only for standards, best-practice and reduction findings.\n` +
    `Plugin scripts directory: ${SCRIPTS}\nWrite the request to ${request} and run it on ${r.engine} model ${r.model} with:\n` +
    `${WRAPPERS[r.engine](r, 'read-only')} ${q(request)}`, {
    agentType: `${PLUGIN}:codex-reviewer`, label: `review:style:${r.engine}`, phase: 'Review', schema: FINDINGS_SCHEMA, model: 'haiku', effort: 'low',
  })
}

// Findings, [] when skipped, or null when the style review failed (a wrapper error or an OpenRouter pause).
async function runStyleReview() {
  if (!styleReview) return []
  const lint = await runLint()
  let out = await styleOn(styleRoute, lint)
  if (out && out.exit_code === 42) {
    await markLimit(styleRoute.engine, 'Review')
    const next = await routeRole('style_review', [styleRoute.engine], 'Review')
    fallbackEvents.push({ phase: 'style_review', from: styleRoute.engine, to: next.engine === 'none' ? null : next.engine,
      reason: `${styleRoute.engine} usage limit` })
    if (next.engine === 'none') return []  // the router hands style to the main review
    out = await styleOn(next, lint)
  }
  return out && !out.exit_code ? out.findings : null
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
} else if (ROUTE === 'trivial') {
  // No model review for trivial work: tests and lint are the gate.
  reviewEngine = 'none'
  await runLint()
  if (lintResult.passed === false) requiresHumanReview = true
  log('review skipped: trivial task')
} else if (review.engine !== 'none') {
  let out
  ;[out, styleFindings] = await Promise.all([reviewOn(review), runStyleReview()])
  if (out && out.exit_code === 42) {
    phase('Fallback')
    await markLimit(review.engine, 'Fallback')
    const next = await routeRole('review', [review.engine], 'Fallback', implFlags([...writers]))
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

// ---------------------------------------------------------------------------
// Docs: once the work passed tests and review, a cheap pass keeps the AI instruction docs current.
let docs = { engine: 'none', changed: false, files: [] }

async function docsPass() {
  const changed = result.files_changed || []
  const brief = `Task (${ROUTE}): ${TASK}\nSummary: ${result.summary}\nFiles changed: ${changed.join(', ')}\n` +
    'The changes are uncommitted: read them with git diff HEAD and git status.'
  const tried = []
  let dr = await routeRole('docs', [], 'Docs')
  while (true) {
    if (dr.engine === 'none' || dr.scope === 'single_atomic_task' || dr.requires_human_review) {
      log(`docs skipped: ${dr.engine === 'none' ? dr.reason : `${dr.engine} is constrained`}`)
      return
    }
    if (dr.engine === 'claude') {
      const d = await agent(brief, {
        agentType: `${PLUGIN}:doc-updater`, label: 'docs', phase: 'Docs', schema: DOCS_SCHEMA, model: dr.model, effort: dr.effort,
      })
      if (d) docs = { engine: 'claude', changed: Boolean(d.changed), files: d.files || [] }
      else log('doc updater failed; docs unchecked')
      return
    }
    const w = await runWrapper(dr.engine, dr, `${RUN}/docs.md`, 'workspace-write', `docs:${dr.engine}`, 'Docs',
      `${DOC_RULES}\n\n${brief}\nPrint that JSON as your last line.`)
    if (w.paused) {
      requiresHumanReview = true
      docs = { engine: dr.engine, changed: false, files: [], paused: w.paused }
      return
    }
    if (w.code !== 42) {
      if (w.code !== 0) { log(`docs failed (${dr.engine} exit ${w.code}); docs unchecked`); return }
      let d = null
      try { d = parseJson(w.output, 'docs') } catch (e) { /* no JSON: fall back to the diff */ }
      // Without the JSON, report files the docs pass added to the diff.
      const files = d && Array.isArray(d.files) ? d.files : w.files.filter(f => !changed.includes(f))
      docs = { engine: dr.engine, changed: d ? Boolean(d.changed) : files.length > 0, files }
      // The scope is enforced only by the prompt and the docs pass runs after review: any reported or
      // newly changed file outside it needs a human.
      const outside = [...new Set([...files, ...w.files.filter(f => !changed.includes(f))])]
        .map(f => String(f).replace(/^\.\//, '')).filter(f => !DOC_SCOPE.test(f))
      if (outside.length) {
        docs.out_of_scope = outside
        requiresHumanReview = true
        log(`docs pass changed files outside its scope: ${outside.join(', ')}; human review required`)
      }
      return
    }
    await markLimit(dr.engine, 'Docs')
    tried.push(dr.engine)
    const next = await routeRole('docs', tried, 'Docs')
    fallbackEvents.push({ phase: 'docs', from: dr.engine, to: next.engine === 'none' ? null : next.engine, reason: `${dr.engine} usage limit` })
    dr = next
  }
}

if (ROUTE !== 'trivial' && success && !requiresHumanReview && (result.files_changed || []).length) {
  phase('Docs')
  await docsPass()
}

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
  docs,
  fallback_events: fallbackEvents,
  reroute_events: rerouteEvents,
  fix_events: fixEvents,
  requires_human_review: requiresHumanReview,
  summary: result.summary,
  remaining_work: result.remaining_work || '',
  handoffs,
}
