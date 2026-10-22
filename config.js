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

// ---------------------------------------------------------------- RENDER ENGINES (Manim + Remotion)
export const ENGINES = [
  ['auto', 'Let AI choose', 'The AI reads the script and picks the better engine for each scene. It may use one or both.'],
  ['manim', 'Manim only', 'Maths, diagrams, graphs, step-by-step explanations. Slower to render, very precise.'],
  ['remotion', 'Remotion only', 'Text, UI-style cards, charts, branded motion graphics. Faster to render.'],
  ['both', 'Both (mix)', 'Manim for the technical scenes, Remotion for the rest. Every scene still goes to one of the two.']]
export const normEngine = s => { const k = String(s ?? '').toLowerCase().replace(/[^a-z]/g, ''); return /^(auto|manim|remotion|both)$/.test(k) ? k : '' }
export const engineLabel = k => (ENGINES.find(e => e[0] === k) || [])[1] || ''
export const engineHint = k => (ENGINES.find(e => e[0] === k) || [])[2] || ''
// Rough render seconds per scene. Change these two lines to match your own render machine.
const SEC_PER_SCENE = { manim: { low: 6, medium: 14, high: 40 }, remotion: { low: 4, medium: 8, high: 20 } }
const OVERHEAD = { base: 60, perScene: 3 }   // script + voice + joining the scenes
// Returns { scenes, sec, label }: how many scenes, how long the backend needs, and a readable range. It is an estimate, not a promise.
export function estimateRender({ minutes, scene_sec, quality, max_scenes, engine }) {
  const m = +minutes || 3, ss = +scene_sec || 15, q = SEC_PER_SCENE.manim[quality] ? quality : 'medium'
  const scenes = Math.min(Math.max(1, Math.ceil(m * 60 / ss)), +max_scenes || 60)
  const mn = SEC_PER_SCENE.manim[q], rm = SEC_PER_SCENE.remotion[q]
  const per = engine === 'manim' ? [mn, mn] : engine === 'remotion' ? [rm, rm] : [rm, mn]   // auto / both: anywhere between all-Remotion and all-Manim
  const f = x => Math.round((OVERHEAD.base + scenes * (OVERHEAD.perScene + x)) / 60)
  const lo = f(per[0]), hi = f(per[1])
  return { scenes, sec: [lo * 60, hi * 60], label: lo === hi ? `about ${lo} min` : `about ${lo}–${hi} min` }
}

// ---------------------------------------------------------------- PROJECT (playlist)
export const LANGS = [['en', 'English'], ['ur', 'Urdu']]
export const PROJECT_DEFAULTS = {
  language: 'en', screen_language: '', aspect: '16:9', description: '', audience: 'curious beginners', minutes: 3,
  voice: 'Kore', tts_style: 'calm, clear and friendly, like a good teacher', tts_engine: 'auto',
  tts_model: '', tts_strict: false, cloud_voices: '',
  engine: 'auto', engine_fixed: false, web_research: true,
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
  ['engine', 'Video engine', 'select', k => engineHint(k), ENGINES.map(e => [e[0], e[1]])],
  ['engine_fixed', 'Keep my engine choice fixed (no fallback to the other engine if it fails; the video is marked Failed instead)', 'bool', ''],
  ['web_research', 'Search the web about the topic before writing (recommended)', 'bool', 'The backend searches Google once per video and writes from those facts. If the search fails, the video is marked Failed instead of being written without it.'],
  ['scene_sec', 'Seconds per scene', 'number', 'Bigger = fewer scenes = fewer renders.'],
  ['chapter_min', 'Chapter length (minutes)', 'number', 'Only for videos longer than 4 minutes.'],
  ['min_steps_per_sentence', 'Animation steps per sentence (min)', 'number', ''],
  ['max_scenes', 'Max scenes', 'number', ''],
  ['quality', 'Render quality', 'select', '', [['low', 'Low'], ['medium', 'Medium'], ['high', 'High']]],
  ['retries', 'Auto-repair attempts (Manim code / Remotion code)', 'number', ''],
  ['allow_latex', 'Allow LaTeX (never with Urdu screen text)', 'bool', '']]

// ---------------------------------------------------------------- narrator
export const STATUSES = ['Hold', 'Waiting', 'In Process', 'Completed', 'Uploaded', 'Failed']
// Hold       = the user is not ready yet. The backend must NOT touch it. This is the default for every new video.
// Waiting    = the user released it. The backend may pick it up now.
// the rest   = set by the backend only. Once a video leaves Hold/Waiting the user can no longer change its status.
export const USER_STATUSES = ['Hold', 'Waiting']
export const isLocked = s => !USER_STATUSES.includes(s)
export const canUserSet = (from, to) => from === to || (USER_STATUSES.includes(from) && USER_STATUSES.includes(to))
const norm = s => String(s ?? '').toLowerCase().replace(/[^a-z0-9]+/g, '')
export const normStatus = s => {   // also understands the old words (Draft/Ready/Rendering/Done)
  const k = norm(s)
  if (!k || /^(hold|onhold|draft|paused|pause)$/.test(k)) return 'Hold'   // empty = Hold, so nothing is processed by accident
  if (/^(waiting|ready|queued|pending)$/.test(k)) return 'Waiting'
  if (/process|progress|render|running/.test(k)) return 'In Process'
  if (/upload|publish/.test(k)) return 'Uploaded'
  if (/complete|done|finish/.test(k)) return 'Completed'
  if (/fail|error/.test(k)) return 'Failed'
  return 'Hold'
}
export const INPUT_TYPES = ['Prompt', 'Script', 'Source']   // Prompt = Frame writes the script from the topic. Script = you supply the finished script, used word for word. Source = you paste raw text (book, article, notes) and Frame adapts it.
// What Frame may do with a Source text. 'custom' means the user's own instruction is the whole brief.
export const SOURCE_MODES = [
  ['polish', 'Light polish', 'Keep the book\'s own words. Only clean it for speaking: remove page numbers, footnotes and headings, fix typos, split very long sentences. Almost no change.'],
  ['rewrite', 'Rewrite in my narrator\'s voice', 'Same facts, same order, new wording. Written to be heard, in the playlist\'s narrator style and audience level.'],
  ['reference', 'Use as reference', 'Frame reads the text and builds its own lesson from it. It may reorder, shorten, simplify and add examples, but must not add facts the text does not support.'],
  ['custom', 'Custom instruction only', 'Frame follows only the instruction you write below.'],
]
export const SOURCE_MODE_KEYS = SOURCE_MODES.map(m => m[0])
export const normSourceMode = s => { const k = String(s ?? '').trim().toLowerCase(); return SOURCE_MODE_KEYS.includes(k) ? k : (SOURCE_MODES.find(m => m[1].toLowerCase() === k)?.[0] || 'polish') }
export const MAX_INSTRUCTION = 2000
export const MAX_SCRIPT = 49000                    // one Google Sheets cell holds at most 50,000 characters
export const normInput = s => /source|adapt|book|rewrite/i.test(String(s ?? '')) ? 'Source' : /script/i.test(String(s ?? '')) ? 'Script' : 'Prompt'
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
  ['#', 'order'], ['Video Title', 'title'], ['Topic / Prompt', 'topic'], ['Input Type', 'inputType'], ['Script', 'script'], ['Source Mode', 'sourceMode'], ['Custom Instruction', 'instruction'], ['Minutes', 'minutes'], ['Status', 'status'], ['Description', 'description'],
  ['Audience', 'audience'], ['Language', 'language'], ['Screen Language', 'screen_language'], ['Aspect', 'aspect'], ['Voice', 'voice'], ['Engine', 'engine'], ['Engine Fixed', 'engineFixed'], ['Web Research', 'webResearch'],
  ['Thumbnail Text', 'thumbText'], ['Thumbnail Link', 'thumbLink'], ['Custom Clips (JSON)', 'clips'], ['Extra Config (JSON)', 'extra'],
  ['Output Link', 'output'], ['Video Folder ID', 'folderId'], ['Input Folder ID', 'inputFolderId'], ['Output Folder ID', 'outputFolderId'], ['Video Folder Link', 'folderLink'], ['Notes', 'notes'], ['Updated', 'updated']]

// ---------------------------------------------------------------- uploading a sheet into the Playlist sheet
const ALIAS = { title: 'Video Title', videoname: 'Video Title', name: 'Video Title', topic: 'Topic / Prompt', prompt: 'Topic / Prompt', scripttext: 'Script', sourcetext: 'Script', booktext: 'Script', sourcemode: 'Source Mode', instruction: 'Custom Instruction', instructions: 'Custom Instruction', customprompt: 'Custom Instruction', voiceover: 'Script', inputtype: 'Input Type', type: 'Input Type', mode: 'Input Type', length: 'Minutes', duration: 'Minutes',
  desc: 'Description', videodescription: 'Description', thumbnail: 'Thumbnail Link', thumb: 'Thumbnail Link', thumbnailurl: 'Thumbnail Link', thumblink: 'Thumbnail Link',
  webresearch: 'Web Research', research: 'Web Research', search: 'Web Research', engine: 'Engine', renderer: 'Engine', library: 'Engine', lib: 'Engine', aspectratio: 'Aspect', ratio: 'Aspect', shape: 'Aspect', thumbtext: 'Thumbnail Text', clips: 'Custom Clips (JSON)', customclips: 'Custom Clips (JSON)', overlays: 'Custom Clips (JSON)', extra: 'Extra Config (JSON)', extraconfig: 'Extra Config (JSON)', output: 'Output Link', link: 'Output Link', no: '#', num: '#', number: '#', order: '#' }
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
// One array per video, aligned to `head` (the final sheet header). Status defaults to Hold (an uploaded file can only say Hold or Waiting), # continues after `base`.
export function importRows(head, a, table, base, stamp) {
  const out = []
  table.slice(1).forEach(r => {
    const raw = {}; a.cols.forEach(c => { if (c.name) raw[c.name] = String(r[c.j] ?? '').trim() })
    const topic = raw['Topic / Prompt'] || '', title = raw['Video Title'] || '', script = raw['Script'] || ''
    if (!title && !topic && !script) return
    raw['Video Title'] = title || topic.slice(0, 60) || script.slice(0, 60); raw['Topic / Prompt'] = topic || (script ? '' : title)
    raw['Input Type'] = raw['Input Type'] ? normInput(raw['Input Type']) : (script ? (raw['Source Mode'] ? 'Source' : 'Script') : 'Prompt')
    if (raw['Input Type'] === 'Source') raw['Source Mode'] = normSourceMode(raw['Source Mode'])   // a filled Script column means "I gave the script"
    { const st = normStatus(raw['Status']); raw['Status'] = isLocked(st) ? 'Hold' : st }; raw['#'] = String(base + out.length + 1); raw['Updated'] = stamp
    out.push(head.map(h => raw[h] ?? ''))
  })
  return out
}

// ---------------------------------------------------------------- CUSTOM CLIPS (video level only)
// The user's own images / video clips that the backend overlays on ONE video. Saved on that video's row (column "Custom Clips (JSON)"),
// the files sit in that video's Drive folder: <video>/Input/clips/. They are never part of the project or the global config.
export const CLIP_POSITIONS = [['top-left', 'Top left'], ['top-right', 'Top right'], ['bottom-left', 'Bottom left'], ['bottom-right', 'Bottom right'], ['center', 'Center'], ['full', 'Full frame (covers the video)']]
export const CLIP_ACCEPT = 'image/*,video/*'
export const MAX_CLIPS = 20
const num = (x, d, lo, hi) => { const n = parseFloat(x); return Number.isFinite(n) ? Math.min(hi, Math.max(lo, n)) : d }
export const cleanClip = c => ({
  id: String(c.id || '').slice(0, 40) || Math.random().toString(36).slice(2, 10),
  name: String(c.name || '').slice(0, 120), kind: c.kind === 'video' ? 'video' : 'image',
  fileId: String(c.fileId || ''), url: String(c.url || ''),
  start: num(c.start, 0, 0, 36000), duration: num(c.duration, 5, 0.5, 36000),   // seconds, on the final video's timeline
  position: CLIP_POSITIONS.some(p => p[0] === c.position) ? c.position : 'bottom-right',
  width: num(c.width, 25, 5, 100), opacity: num(c.opacity, 100, 5, 100),         // percent of the frame width / percent visible
  loop: !!c.loop, mute: c.mute === undefined ? true : !!c.mute,                  // loop/mute matter for video clips only
  note: String(c.note || '').slice(0, 200),
})
export const parseClips = s => { try { const a = Array.isArray(s) ? s : JSON.parse(String(s || '').trim() || '[]'); return Array.isArray(a) ? a.filter(c => c && (c.fileId || c.url)).slice(0, MAX_CLIPS).map(cleanClip) : [] } catch { return [] } }
export const stringifyClips = a => a.length ? JSON.stringify(a.map(cleanClip)) : ''   // empty list = empty cell
// What the backend receives as config.custom_clips (snake_case, same names as VideoConfig). Empty list = the key is left out.
export const clipsFor = v => parseClips(v.clips).map((c, i) => ({
  order: i + 1, name: c.name, type: c.kind, file_id: c.fileId, url: c.url,
  start_sec: c.start, duration_sec: c.duration, position: c.position, width_pct: c.width, opacity_pct: c.opacity, loop: c.loop, mute: c.mute, note: c.note,
}))

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
  const kind = normInput(v.inputType), hasText = !!(v.script || '').trim()
  const isScript = kind === 'Script' && hasText   // a script row with no script text falls back to its prompt
  const sm = normSourceMode(v.sourceMode), isSource = kind === 'Source' && (hasText || (sm === 'custom' && !!(v.instruction || '').trim()))   // custom mode may run on the instruction alone
  const thumbnail = thumbnailFor(p, v)
  const engine = normEngine(v.engine) || normEngine(p.engine) || 'auto'                       // row > project > AI chooses
  const fixed = String(v.engineFixed ?? '').trim() ? /^(1|true|yes|y|fixed)$/i.test(String(v.engineFixed).trim()) : !!p.engine_fixed
  const wr = String(v.webResearch ?? '').trim().toLowerCase(), research = wr ? !/^(0|false|no|n|off)$/.test(wr) : p.web_research !== false   // row > project > ON
  const cfg = {
    description: [dv, dp].filter(Boolean).join('\n\n'), description_video: dv, description_project: dp, thumbnail,
    input_type: isSource ? 'source' : isScript ? 'script' : 'prompt', script: isScript ? v.script.trim() : '',
    source_text: isSource ? (v.script || '').trim() : '', source_mode: isSource ? sm : '',
    custom_instruction: (isSource || kind === 'Prompt') ? (v.instruction || '').trim() : '',
    topic: (v.topic || v.title || '').trim(), minutes: +v.minutes || (isScript || (isSource && (sm === 'polish' || sm === 'rewrite')) ? '' : +p.minutes || 3), audience: v.audience || p.audience,
    language: v.language || p.language, screen_language: v.screen_language || p.screen_language,
    aspect: normAspect(v.aspect) || normAspect(p.aspect) || '16:9',   // row > project > built-in 16:9
    voice: v.voice || p.voice, tts_style: p.tts_style, tts_engine: p.tts_engine, cloud_voices: p.cloud_voices,
    light_models: csv(g.models.light).join(','), medium_models: csv(g.models.medium).join(','), code_models: csv(g.models.code).join(','), tts_models: tts.join(','),
    rpm: +g.limits.rpm, tts_rpm: +g.limits.tts_rpm, max_wait: +g.limits.max_wait, tts_group_scenes: +g.limits.tts_group_scenes, tts_group_chars: +g.limits.tts_group_chars, scene_batch: +g.limits.scene_batch,
    scene_sec: +p.scene_sec, chapter_min: +p.chapter_min, min_steps_per_sentence: +p.min_steps_per_sentence, max_scenes: +p.max_scenes,
    quality: p.quality, retries: +p.retries, allow_latex: !!p.allow_latex, web_research: research,
    engine, engine_fixed: fixed, engine_fallback: !fixed && engine !== 'auto',   // fixed = never swap engines; fallback = may retry a failed scene with the other engine
    urdu_font: g.tools.urdu_font, workdir: g.tools.workdir, output_dir: `${g.tools.output_dir}/${slug(playlist)}`,
    // this video's own Drive folder: read the input from input_folder_id, write every output file into output_folder_id
    custom_clips: clipsFor(v),   // this video's own overlay clips (never inherited from project / global); backend downloads each file_id
    video_folder_id: v.folderId || '', input_folder_id: v.inputFolderId || '', output_folder_id: v.outputFolderId || '', video_folder_link: v.folderLink || '',
  }
  Object.keys(cfg).forEach(k => (cfg[k] === '' || (Array.isArray(cfg[k]) && !cfg[k].length && k === 'custom_clips')) && delete cfg[k])   // empty = let config.py use its own default
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
    on_hold: of('Hold'), in_process: of('In Process'), completed: of('Completed', 'Uploaded'), failed: of('Failed'),
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
