// Every setting lives here once: its default, which level owns it, and how the three levels merge.
//   GLOBAL  -> Drive/Frame AI Config/frame_config.json      (all playlists: keys, models, rate limits, machine)
//   PROJECT -> <playlist folder>/project_config.json        (one playlist: narrator, language, look, defaults)
//   VIDEO   -> one row of the playlist's "Playlist" sheet   (topic, minutes + optional overrides)
// Names on the right match the VideoConfig fields in config.py.

export const csv = s => (Array.isArray(s) ? s : String(s || '').split(/[\n,]/)).map(x => String(x).trim()).filter(Boolean)
export const getPath = (o, p) => p.split('.').reduce((x, k) => x?.[k], o)
export const setPath = (o, p, v) => { const c = structuredClone(o), ks = p.split('.'); let t = c; ks.slice(0, -1).forEach(k => { t = t[k] ??= {} }); t[ks.at(-1)] = v; return c }
export const mergeDeep = (a, b) => {
  const isO = x => x && typeof x === 'object' && !Array.isArray(x)
  if (!isO(b)) return b === undefined ? a : b
  const o = { ...a }; for (const k in b) o[k] = isO(a?.[k]) && isO(b[k]) ? mergeDeep(a[k], b[k]) : b[k]; return o
}
const slug = s => s.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '').slice(0, 40) || 'playlist'

// ---------------------------------------------------------------- GLOBAL
export const GLOBAL_DEFAULTS = {
  keys: { gemini: [], tts: [], cloudTts: [] },   // one key per line. tts empty = reuse the Gemini keys
  models: {                                      // order = priority (first is tried first)
    light: ['gemini-3.5-flash-lite', 'gemini-3.1-flash-lite', 'gemini-2.5-flash-lite', 'gemini-3.5-flash', 'gemini-3.6-flash', 'gemini-3.7-flash', 'gemini-3.8-flash', 'gemini-2.5-flash'],
    medium: ['gemini-3.8-flash', 'gemini-3.7-flash', 'gemini-3.6-flash', 'gemini-3.5-flash', 'gemini-2.5-flash', 'gemini-3.5-flash-lite', 'gemini-3.1-flash-lite', 'gemini-2.5-flash-lite'],
    code: ['gemini-3.8-flash', 'gemini-2.5-pro', 'gemini-3.7-flash', 'gemini-3.6-flash', 'gemini-3.5-flash', 'gemini-2.5-flash', 'gemini-3.5-flash-lite', 'gemini-3.1-flash-lite', 'gemini-2.5-flash-lite', 'gemini-3-flash-preview'],
    tts: ['gemini-3.8-flash-tts', 'gemini-3.8-flash-lite-tts', 'gemini-3.1-flash-tts-preview', 'gemini-2.5-flash-preview-tts'],
    transcription: ['gemini-3.5-transcribe', 'gemini-3.5-transcribe-live'],
  },
  limits: { rpm: 5, tts_rpm: 4, max_wait: 240, tts_group_scenes: 4, tts_group_chars: 4000, scene_batch: 4 },
  tools: { urdu_font: 'Noto Naskh Arabic', workdir: 'build', output_dir: 'output', tts_cache_dir: 'tts_cache' },
}

export const GLOBAL_FIELDS = [
  ['Rate limits', [
    ['limits.rpm', 'Text requests per minute', 'number', 'Free tier. Lite models get x3 automatically.'],
    ['limits.tts_rpm', 'TTS requests per minute', 'number', ''],
    ['limits.max_wait', 'Longest quota wait (seconds)', 'number', 'Past this the run stops instead of sleeping.'],
    ['limits.tts_group_scenes', 'Scenes per TTS call', 'number', '1 turns batching off.'],
    ['limits.tts_group_chars', 'Characters per TTS call', 'number', ''],
    ['limits.scene_batch', 'Scenes written per Gemini call', 'number', '']]],
  ['Render machine', [
    ['tools.urdu_font', 'Urdu screen font', 'text', 'Must be installed on the machine that renders.'],
    ['tools.workdir', 'Work folder', 'text', 'Script, audio and clips while building.'],
    ['tools.output_dir', 'Output folder', 'text', 'A sub-folder per playlist is added.'],
    ['tools.tts_cache_dir', 'TTS cache folder', 'text', 'Survives deleting the work folder.']]]]

export const LIST_PATHS = ['keys.gemini', 'keys.tts', 'keys.cloudTts', 'models.light', 'models.medium', 'models.code', 'models.tts', 'models.transcription']
export const LIST_INFO = {
  'keys.gemini': ['Gemini API keys', 'One per line. Used for scripts, plans and code. Several keys multiply your free quota.'],
  'keys.tts': ['Gemini TTS keys', 'Optional. Leave empty to reuse the Gemini keys above.'],
  'keys.cloudTts': ['Cloud TTS keys', 'Optional fallback engine (Google Cloud Text-to-Speech).'],
  'models.light': ['Light models', 'Cheap JSON work: concepts and key points.'],
  'models.medium': ['Medium models', 'Animation plan and outline.'],
  'models.code': ['Code models', 'Heavy work: plan, narration and Manim code.'],
  'models.tts': ['TTS models', 'Speech models, first = first choice. A playlist can pin one for a steady voice.'],
  'models.transcription': ['Transcription models', 'Stored for later. Not passed to VideoConfig yet.'],
}

// ---------------------------------------------------------------- ASPECT (frame shape)
// '16:9' = wide (YouTube), '9:16' = vertical (Shorts/Reels/TikTok), '1:1' = square, '4:5' = portrait feed post.
export const ASPECTS = [['16:9', '16:9 · Wide (YouTube)'], ['9:16', '9:16 · Vertical (Shorts, Reels, TikTok)'], ['1:1', '1:1 · Square'], ['4:5', '4:5 · Portrait (feed posts)']]
export const normAspect = s => { const m = String(s ?? '').replace(/\s/g, '').replace(/[x×/]/gi, ':').match(/^(\d+(?:\.\d+)?):(\d+(?:\.\d+)?)$/); if (!m) return ''; const k = `${+m[1]}:${+m[2]}`; return ASPECTS.some(a => a[0] === k) ? k : '' }
export const aspectHint = a => {
  const k = normAspect(a); if (!k) return ''
  const [w, h] = k.split(':').map(Number)
  if (w < h) return 'Vertical videos: the text is bigger and the layout stacks top to bottom.'
  if (w === h) return 'Square videos: text is a little bigger and the layout stays compact.'
  return 'Wide videos: text is normal size and the layout sits side by side.'
}

// ---------------------------------------------------------------- PROJECT (playlist)
export const LANGS = [['en', 'English'], ['ur', 'Urdu']]
export const PROJECT_DEFAULTS = {
  language: 'en', screen_language: '', aspect: '16:9', description: '', audience: 'curious beginners', minutes: 3,
  voice: 'Kore', tts_style: 'calm, clear and friendly, like a good teacher', tts_engine: 'auto',
  tts_model: '', tts_strict: false, cloud_voices: '',
  scene_sec: 15, chapter_min: 2, min_steps_per_sentence: 2, max_scenes: 60, quality: 'medium', retries: 2, allow_latex: false,
  thumb: { templateId: '', templateName: '', templateLink: '', font: 'Fredoka', size: 110, color: '#ffffff', stroke: '#2a2550', align: 'left', x: 6, y: 50, maxWidth: 58, shade: true },
}
export const PROJECT_FIELDS = [
  ['language', 'Narration language', 'select', 'Script, voice and audio.', LANGS],
  ['screen_language', 'On-screen text language', 'select', '', [['', 'Same as narration'], ...LANGS]],
  ['aspect', 'Default video shape (aspect ratio)', 'select', aspectHint, ASPECTS],
  ['description', 'Default description (added to every video)', 'textarea', "Goes under each video's own description. A video with no description of its own gets just this one."],
  ['audience', 'Audience', 'text', 'Sets the level of the script.'],
  ['minutes', 'Default length (minutes)', 'number', 'Used when a video row leaves Minutes empty.'],
  ['scene_sec', 'Seconds per scene', 'number', 'Bigger = fewer scenes = fewer renders.'],
  ['chapter_min', 'Chapter length (minutes)', 'number', 'Only for videos longer than 4 minutes.'],
  ['min_steps_per_sentence', 'Animation steps per sentence (min)', 'number', ''],
  ['max_scenes', 'Max scenes', 'number', ''],
  ['quality', 'Render quality', 'select', '', [['low', 'Low'], ['medium', 'Medium'], ['high', 'High']]],
  ['retries', 'Manim auto-repair attempts', 'number', ''],
  ['allow_latex', 'Allow LaTeX (never with Urdu screen text)', 'bool', '']]

// ---------------------------------------------------------------- narrator
export const STATUSES = ['Waiting', 'In Process', 'Completed', 'Uploaded', 'Failed']   // the backend moves a video along this list
const norm = s => String(s ?? '').toLowerCase().replace(/[^a-z0-9]+/g, '')
export const normStatus = s => {   // also understands the old words (Draft/Ready/Rendering/Done)
  const k = norm(s)
  if (!k || /^(waiting|draft|ready|queued|pending)$/.test(k)) return 'Waiting'
  if (/process|progress|render|running/.test(k)) return 'In Process'
  if (/upload|publish/.test(k)) return 'Uploaded'
  if (/complete|done|finish/.test(k)) return 'Completed'
  if (/fail|error/.test(k)) return 'Failed'
  return 'Waiting'
}
export const INPUT_TYPES = ['Prompt', 'Script']   // Prompt = Frame writes the script from the topic. Script = you supply the finished script.
export const MAX_SCRIPT = 49000                    // one Google Sheets cell holds at most 50,000 characters
export const normInput = s => /script/i.test(String(s ?? '')) ? 'Script' : 'Prompt'
export const STYLE_PRESETS = [
  'calm, clear and friendly, like a good teacher', 'upbeat and energetic, like a favourite YouTube explainer',
  'warm and slow, like a bedtime storyteller', 'serious and steady, like a documentary narrator',
  'gentle and patient, for absolute beginners', 'confident and crisp, like a news anchor']
export const SAMPLE = { en: 'Hi! Today we will see how a computer learns, one small step at a time.', ur: 'آج ہم آسان الفاظ میں سمجھیں گے کہ کمپیوٹر کیسے سیکھتا ہے۔' }
// Gemini prebuilt voices: [name, tone word from Google's voice list, how it tends to sit in a lesson]
export const VOICES = [
  ['Zephyr', 'Bright', 'light and cheerful opener'], ['Puck', 'Upbeat', 'lively, keeps kids awake'], ['Charon', 'Informative', 'clear explainer, steady pace'],
  ['Kore', 'Firm', 'confident teacher'], ['Fenrir', 'Excitable', 'big-reaction hype'], ['Leda', 'Youthful', 'young, fresh classroom voice'],
  ['Orus', 'Firm', 'solid and authoritative'], ['Aoede', 'Breezy', 'relaxed and flowing'], ['Callirrhoe', 'Easy-going', 'casual chat feel'],
  ['Autonoe', 'Bright', 'clean and optimistic'], ['Enceladus', 'Breathy', 'soft, close-to-the-mic'], ['Iapetus', 'Clear', 'plain and precise'],
  ['Umbriel', 'Easy-going', 'calm and unhurried'], ['Algieba', 'Smooth', 'polished, even flow'], ['Despina', 'Smooth', 'mellow and rounded'],
  ['Erinome', 'Clear', 'crisp diction'], ['Algenib', 'Gravelly', 'textured, character voice'], ['Rasalgethi', 'Informative', 'measured lecturer'],
  ['Laomedeia', 'Upbeat', 'positive and moving'], ['Achernar', 'Soft', 'gentle, good for slow topics'], ['Alnilam', 'Firm', 'direct and decisive'],
  ['Schedar', 'Even', 'level, no surprises'], ['Gacrux', 'Mature', 'seasoned and grounded'], ['Pulcherrima', 'Forward', 'upfront and assertive'],
  ['Achird', 'Friendly', 'approachable guide'], ['Zubenelgenubi', 'Casual', 'everyday conversation'], ['Vindemiatrix', 'Gentle', 'kind, patient'],
  ['Sadachbia', 'Lively', 'animated storyteller'], ['Sadaltager', 'Knowledgeable', 'expert tone'], ['Sulafat', 'Warm', 'warm and reassuring']]
export const ttsNote = m => /lite/.test(m) ? 'Lite: lighter, bigger free daily allowance' : /preview/.test(m) ? 'Preview build: can change or disappear' : /2\.5/.test(m) ? 'Older model: safe fallback' : 'Standard flash TTS: tried first, smaller free daily allowance'

// ---------------------------------------------------------------- playlist sheet (header name -> field)
export const PLAYLIST_COLS = [
  ['#', 'order'], ['Video Title', 'title'], ['Topic / Prompt', 'topic'], ['Input Type', 'inputType'], ['Script', 'script'], ['Minutes', 'minutes'], ['Status', 'status'], ['Description', 'description'],
  ['Audience', 'audience'], ['Language', 'language'], ['Screen Language', 'screen_language'], ['Aspect', 'aspect'], ['Voice', 'voice'],
  ['Thumbnail Text', 'thumbText'], ['Thumbnail Link', 'thumbLink'], ['Extra Config (JSON)', 'extra'],
  ['Output Link', 'output'], ['Captions Link', 'captions'], ['Notes', 'notes'], ['Updated', 'updated']]

// ---------------------------------------------------------------- uploading a sheet into the Playlist sheet
const ALIAS = { title: 'Video Title', videoname: 'Video Title', name: 'Video Title', topic: 'Topic / Prompt', prompt: 'Topic / Prompt', scripttext: 'Script', voiceover: 'Script', inputtype: 'Input Type', type: 'Input Type', mode: 'Input Type', length: 'Minutes', duration: 'Minutes',
  desc: 'Description', videodescription: 'Description', thumbnail: 'Thumbnail Link', thumb: 'Thumbnail Link', thumbnailurl: 'Thumbnail Link', thumblink: 'Thumbnail Link',
  aspectratio: 'Aspect', ratio: 'Aspect', shape: 'Aspect', thumbtext: 'Thumbnail Text', extra: 'Extra Config (JSON)', extraconfig: 'Extra Config (JSON)', output: 'Output Link', link: 'Output Link', captions: 'Captions Link', subtitles: 'Captions Link', srt: 'Captions Link', no: '#', num: '#', number: '#', order: '#' }
const CANON = Object.fromEntries(PLAYLIST_COLS.filter(c => c[0] !== '#').map(([h]) => [norm(h), h]))
export const canonHeader = h => { const t = String(h ?? '').trim(); if (!t) return null; if (t === '#') return '#'; return CANON[norm(t)] || ALIAS[norm(t)] || null }

// Compare the uploaded table with the sheet: which columns match, which are new (they get ADDED to the sheet), which standard ones the file lacks.
export function analyzeTable(head, table) {
  const seen = new Set()
  const cols = (table[0] || []).map((h, j) => {
    const src = String(h ?? '').trim(); if (!src) return { j, src, name: null, known: false }
    let name = canonHeader(src), known = !!name
    if (!name || seen.has(name)) { name = src; known = false }
    seen.add(name); return { j, src, name, known }
  })
  const names = cols.filter(c => c.name).map(c => c.name)
  const skip = ['#', 'Updated']
  return {
    cols, names,
    matched: cols.filter(c => c.known && !skip.includes(c.name)).map(c => c.name),
    absent: PLAYLIST_COLS.map(c => c[0]).filter(h => !skip.includes(h) && !names.includes(h)),   // standard columns the file does not have: left empty / default
    added: [...new Set(names.filter(n => !head.includes(n)))],                                       // columns the sheet does not have yet: appended to the sheet
    extras: cols.filter(c => c.name && !c.known).map(c => c.name),
    hasKey: names.includes('Video Title') || names.includes('Topic / Prompt') || names.includes('Script'),
    count: table.slice(1).filter(r => cols.some(c => (c.name === 'Video Title' || c.name === 'Topic / Prompt' || c.name === 'Script') && String(r[c.j] ?? '').trim())).length,
  }
}
// One array per video, aligned to `head` (the final sheet header). Status defaults to Waiting, # continues after `base`.
export function importRows(head, a, table, base, stamp) {
  const out = []
  table.slice(1).forEach(r => {
    const raw = {}; a.cols.forEach(c => { if (c.name) raw[c.name] = String(r[c.j] ?? '').trim() })
    const topic = raw['Topic / Prompt'] || '', title = raw['Video Title'] || '', script = raw['Script'] || ''
    if (!title && !topic && !script) return
    raw['Video Title'] = title || topic.slice(0, 60) || script.slice(0, 60); raw['Topic / Prompt'] = topic || (script ? '' : title)
    raw['Input Type'] = raw['Input Type'] ? normInput(raw['Input Type']) : (script ? 'Script' : 'Prompt')   // a filled Script column means "I gave the script"
    raw['Status'] = normStatus(raw['Status']); raw['#'] = String(base + out.length + 1); raw['Updated'] = stamp
    out.push(head.map(h => raw[h] ?? ''))
  })
  return out
}

// ---------------------------------------------------------------- merge the three levels into one VideoConfig
// own thumbnail wins; otherwise the backend writes the title on the project's default template
export const thumbnailFor = (p, v) => {
  const t = p.thumb || {}, own = (v.thumbLink || '').trim(), title_text = (v.thumbText || v.title || '').trim()
  return own ? { source: 'video', url: own }
    : { source: t.templateId ? 'default_template' : 'plain_background', template_url: t.templateLink || '', template_file_id: t.templateId || '', title_text,
        font: t.font, size: t.size, color: t.color, stroke: t.stroke, align: t.align, x: t.x, y: t.y, max_width: t.maxWidth, shade: t.shade }
}
// the video's own description first, then the playlist's shared one
export const descriptionFor = (p, v) => [(v.description || '').trim(), (p.description || '').trim()].filter(Boolean).join('\n\n')

export function resolveJob(g, p, v, playlist) {
  let extra = {}
  if (v.extra?.trim()) { try { extra = JSON.parse(v.extra) } catch { throw new Error(`Row ${v.row} (${v.title}): Extra Config is not valid JSON`) } }
  const pin = p.tts_model, rest = csv(g.models.tts).filter(x => x !== pin)
  const tts = p.tts_strict && pin ? [pin] : [...(pin ? [pin] : []), ...rest]   // a pinned model keeps the voice identical across the playlist
  const dv = (v.description || '').trim(), dp = (p.description || '').trim()
  const isScript = normInput(v.inputType) === 'Script' && (v.script || '').trim()   // a script row with no script text falls back to its prompt
  const thumbnail = thumbnailFor(p, v)
  const cfg = {
    description: [dv, dp].filter(Boolean).join('\n\n'), description_video: dv, description_project: dp, thumbnail,
    input_type: isScript ? 'script' : 'prompt', script: isScript ? v.script.trim() : '',
    topic: (v.topic || v.title || '').trim(), minutes: +v.minutes || (isScript ? '' : +p.minutes || 3), audience: v.audience || p.audience,
    language: v.language || p.language, screen_language: v.screen_language || p.screen_language,
    aspect: normAspect(v.aspect) || normAspect(p.aspect) || '16:9',   // row > project > built-in 16:9
    voice: v.voice || p.voice, tts_style: p.tts_style, tts_engine: p.tts_engine, cloud_voices: p.cloud_voices,
    light_models: csv(g.models.light).join(','), medium_models: csv(g.models.medium).join(','), code_models: csv(g.models.code).join(','), tts_models: tts.join(','),
    rpm: +g.limits.rpm, tts_rpm: +g.limits.tts_rpm, max_wait: +g.limits.max_wait, tts_group_scenes: +g.limits.tts_group_scenes, tts_group_chars: +g.limits.tts_group_chars, scene_batch: +g.limits.scene_batch,
    scene_sec: +p.scene_sec, chapter_min: +p.chapter_min, min_steps_per_sentence: +p.min_steps_per_sentence, max_scenes: +p.max_scenes,
    quality: p.quality, retries: +p.retries, allow_latex: !!p.allow_latex,
    urdu_font: g.tools.urdu_font, workdir: g.tools.workdir, output_dir: `${g.tools.output_dir}/${slug(playlist)}`,
  }
  Object.keys(cfg).forEach(k => cfg[k] === '' && delete cfg[k])   // empty = let config.py use its own default
  return { ...cfg, ...extra }
}

export function buildJobs(g, p, playlist, rows, all = false) {
  const gem = csv(g.keys.gemini), tts = csv(g.keys.tts).length ? csv(g.keys.tts) : gem
  return {
    playlist, exported: new Date().toISOString(), keys: { gemini: gem, tts, cloudTts: csv(g.keys.cloudTts) }, tts_cache_dir: g.tools.tts_cache_dir,
    jobs: rows.filter(r => all || r.status === 'Waiting').map(r => ({ row: r.row, title: r.title, config: resolveJob(g, p, r, playlist) })),
  }
}

// ---------------------------------------------------------------- PRODUCTION (all projects together)
export const byOrder = (a, b) => (+a.order || a.row) - (+b.order || b.row)
const NEW_PLAN_ENTRY = p => ({ id: p.folderId, name: p.name, state: 'active', perTurn: 1, pauseMin: 0, pauseEvery: 1 })

// Keep the saved plan and the real list of projects in step: new projects join at the end, deleted ones leave.
export function syncPlan(plan, projects) {
  const saved = plan?.projects || [], ids = new Set(projects.map(p => p.folderId)), have = new Set(saved.map(x => x.id))
  return {
    order: plan?.order === 'sequential' ? 'sequential' : 'rotate',
    projects: [...saved.filter(x => ids.has(x.id)).map(x => ({ ...x, name: projects.find(p => p.folderId === x.id).name })), ...projects.filter(p => !have.has(p.folderId)).map(NEW_PLAN_ENTRY)],
  }
}

// waiting = { projectId: [Waiting rows] }. Returns the exact build order.
//   rotate     -> A, B, C, A, B, C ...   (perTurn videos from each project per round)
//   sequential -> every A, then every B ...
// A paused project is skipped. pauseAfterMin = how long to wait after that video before starting the next one.
export function buildQueue(plan, waiting) {
  const ps = plan.projects.filter(x => x.state === 'active'), left = {}, made = {}, out = []
  ps.forEach(x => { left[x.id] = [...(waiting[x.id] || [])].sort(byOrder) })
  const take = (x, n) => {
    for (let i = 0; i < n && left[x.id].length; i++) {
      const row = left[x.id].shift(), k = made[x.id] = (made[x.id] || 0) + 1, every = Math.max(1, +x.pauseEvery || 1)
      out.push({ projectId: x.id, playlist: x.name, row, pauseAfterMin: +x.pauseMin > 0 && k % every === 0 ? +x.pauseMin : 0 })
    }
  }
  if (plan.order === 'sequential') ps.forEach(x => take(x, Infinity))
  else while (ps.some(x => left[x.id].length)) ps.forEach(x => take(x, Math.max(1, +x.perTurn || 1)))
  if (out.length) out[out.length - 1].pauseAfterMin = 0
  return out
}
export const waitingOf = data => Object.fromEntries(data.map(d => [d.p.folderId, d.rows.filter(r => r.status === 'Waiting')]))
export const countStatus = rows => Object.fromEntries(STATUSES.map(s => [s, rows.filter(r => r.status === s).length]))

// production_record.json: what has been made, what is running, what comes next.
export function makeRecord(plan, data, queue) {
  const all = data.flatMap(d => d.rows.map(r => ({ playlist: d.p.name, row: r.row, title: r.title, status: r.status, output: r.output || '', updated: r.updated || '' })))
  const of = (...s) => all.filter(r => s.includes(r.status))
  const sum = Object.fromEntries(STATUSES.map(s => [s, of(s).length]))
  return {
    order: plan.order,
    next: queue[0] ? { playlist: queue[0].playlist, row: queue[0].row.row, title: queue[0].row.title } : null,
    in_process: of('In Process'), completed: of('Completed', 'Uploaded'), failed: of('Failed'),
    queue: queue.map((q, i) => ({ position: i + 1, playlist: q.playlist, row: q.row.row, title: q.row.title, pause_after_min: q.pauseAfterMin })),
    paused_projects: plan.projects.filter(x => x.state === 'paused').map(x => x.name), summary: sum,
  }
}

// One jobs file for every project, already in build order.
export function buildJobsAll(g, plan, data, queue) {
  const gem = csv(g.keys.gemini), tts = csv(g.keys.tts).length ? csv(g.keys.tts) : gem, by = Object.fromEntries(data.map(d => [d.p.folderId, d]))
  return {
    exported: new Date().toISOString(), order: plan.order, keys: { gemini: gem, tts, cloudTts: csv(g.keys.cloudTts) }, tts_cache_dir: g.tools.tts_cache_dir,
    jobs: queue.map((q, i) => ({ position: i + 1, playlist: q.playlist, row: q.row.row, title: q.row.title, pause_after_min: q.pauseAfterMin, config: resolveJob(g, by[q.projectId].cfg, q.row, q.playlist) })),
  }
}
