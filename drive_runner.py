"""
drive_runner.py - Frame AI frontend (Google Drive) -> backend pipeline -> Drive.

Per run (GitHub Actions, every 6 h):
  1. OAuth refresh token (same Google client as the frontend, scope drive.file) -> Drive + Sheets
  2. Read  Frame AI/Application Config/frame_config.json   (Gemini keys, models, limits)
           Production Plan sheet (active playlists, order, videos per turn)
           Projects sheet (folder id + sheet id per playlist)
           <playlist>/project_config.json + "<playlist> - Record" > Playlist tab
           Frame AI/Singles/  (stand-alone videos; works like a playlist with no shared description)
  3. For every row with Status == Waiting: merge config (global < project < row), make the video,
     upload it as "<Video Title>.mp4" into <playlist>/Output/ (Singles/Output/ for singles),
     write Status + Output Link back.
     A row is made from a PROMPT (Frame writes the script) or from its own SCRIPT (Input Type = Script,
     text in the Script column: used word for word, only split into scenes).
Nothing about YouTube / thumbnails / scenes / requirements is touched.
"""
import os

# must be set before tts.py is imported
os.environ.setdefault("TTS_CACHE_DIR", "tts_cache")

import json            # noqa: E402
import logging         # noqa: E402
import re              # noqa: E402
import shutil          # noqa: E402
import sys             # noqa: E402
import time            # noqa: E402
from dataclasses import fields   # noqa: E402
from datetime import datetime, timezone   # noqa: E402
from pathlib import Path          # noqa: E402

from google.oauth2.credentials import Credentials          # noqa: E402
from google.auth.transport.requests import Request         # noqa: E402
from googleapiclient.discovery import build                # noqa: E402
from googleapiclient.errors import HttpError              # noqa: E402
from googleapiclient.http import MediaFileUpload           # noqa: E402

from config import DEFAULT_TTS_MODELS, VideoConfig, csv, detect_language, normalize_aspect    # noqa: E402
from gemini_client import Gemini, QuotaExhausted           # noqa: E402
from pipeline import Services, make_video                  # noqa: E402
from scene_input import load_scenes                        # noqa: E402
from tts import TTS                                        # noqa: E402

log = logging.getLogger("edu")


RATE_REASONS = ("ratelimitexceeded", "userratelimitexceeded", "sharingratelimitexceeded")


def rate_limited(e):
    """True for Google's per-minute quota errors (429, or 403 with a rate-limit reason). Not for 'storage full'."""
    status = getattr(getattr(e, "resp", None), "status", None)
    if status == 429:
        return True
    if status == 403:
        body = (getattr(e, "content", b"") or b"").decode("utf-8", "ignore").lower()
        return any(r in body for r in RATE_REASONS)
    return False


def ex(req, tries=5):
    """Run a Drive/Sheets request. Short hiccups (5xx) are retried by the client library; per-minute quota errors
    (429) wait for the quota window to refill (quotas reset every minute) and then try again."""
    for i in range(tries):
        try:
            return req.execute(num_retries=5)
        except HttpError as e:
            if i == tries - 1 or not rate_limited(e):
                raise
            wait = min(65, 20 * (i + 1))
            log.warning("Google rate limit hit, waiting %ss then retrying (%d/%d)", wait, i + 1, tries - 1)
            time.sleep(wait)

FOLDER = "application/vnd.google-apps.folder"
SHEET = "application/vnd.google-apps.spreadsheet"
GH_TAG = "[gh-action]"
MAX_MIN = float(os.environ.get("MAX_RUN_MINUTES", "300"))   # don't START a new video after this
T0 = time.time()

FIELD_TYPES = {f.name: str(f.type) for f in fields(VideoConfig)}
LANG = {"english": "en", "en": "en", "urdu": "ur", "ur": "ur", "اردو": "ur"}
ROW_MAP = {"topic / prompt": "topic", "minutes": "minutes", "audience": "audience",
           "language": "language", "screen language": "screen_language", "voice": "voice",
           "input type": "input_type", "script": "script", "aspect": "aspect",
           "source mode": "source_mode", "custom instruction": "custom_instruction",
           "engine": "engine", "engine fixed": "engine_fixed", "web research": "web_research",
           "video folder id": "video_folder_id", "input folder id": "input_folder_id",
           "output folder id": "output_folder_id", "video folder link": "video_folder_link"}
SINGLES = "Singles"


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def esc(s):
    return s.replace("\\", "\\\\").replace("'", "\\'")


def col_letter(n):
    s = ""
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


# --------------------------------------------------------------------------- #
# Drive / Sheets
# --------------------------------------------------------------------------- #
class Drive:
    def __init__(self):
        missing = [k for k in ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_REFRESH_TOKEN")
                   if not os.environ.get(k)]
        if missing:
            sys.exit("Missing: " + ", ".join(missing) + ". Add GOOGLE_CLIENT_SECRET and GOOGLE_REFRESH_TOKEN "
                     "under Settings > Secrets and variables > Actions (see SETUP.md).")
        creds = Credentials(
            None, refresh_token=os.environ["GOOGLE_REFRESH_TOKEN"],
            token_uri="https://oauth2.googleapis.com/token",
            client_id=os.environ["GOOGLE_CLIENT_ID"],
            client_secret=os.environ["GOOGLE_CLIENT_SECRET"],
            scopes=["https://www.googleapis.com/auth/drive.file"])
        try:
            creds.refresh(Request())
        except Exception as e:  # noqa: BLE001
            sys.exit(f"Google refused the refresh token ({str(e)[:120]}). Open the frontend > Control room > "
                     "Connect backend, and replace the GOOGLE_REFRESH_TOKEN secret. If this keeps happening every "
                     "week, the OAuth consent screen is still in 'Testing' - publish it.")
        self.d = build("drive", "v3", credentials=creds, cache_discovery=False)
        self.s = build("sheets", "v4", credentials=creds, cache_discovery=False)

    def find(self, name, parent=None, mime=None):
        q = [f"name = '{esc(name)}'", "trashed = false"]
        if parent:
            q.append(f"'{parent}' in parents")
        if mime:
            q.append(f"mimeType = '{mime}'")
        r = ex(self.d.files().list(q=" and ".join(q), orderBy="modifiedTime desc", pageSize=10,
                                   fields="files(id,name)"))
        return (r.get("files") or [None])[0]

    def read_json(self, file_id):
        return json.loads(ex(self.d.files().get_media(fileId=file_id)).decode("utf-8"))

    def folder(self, name, parent):
        f = self.find(name, parent, FOLDER)
        if f:
            return f["id"]
        return ex(self.d.files().create(
            body={"name": name, "mimeType": FOLDER, "parents": [parent]}, fields="id"))["id"]

    def upload(self, path: Path, name, parent):
        mime = {".srt": "application/x-subrip", ".json": "application/json"}.get(Path(name).suffix.lower(), "video/mp4")
        media = MediaFileUpload(str(path), mimetype=mime, resumable=True,
                                chunksize=16 * 1024 * 1024)
        old = self.find(name, parent)
        if old:   # same title again -> replace, never duplicate
            req = self.d.files().update(fileId=old["id"], media_body=media, fields="id,webViewLink")
        else:
            req = self.d.files().create(body={"name": name, "parents": [parent]},
                                        media_body=media, fields="id,webViewLink")
        resp, fails = None, 0
        while resp is None:
            try:
                _, resp = req.next_chunk(num_retries=5)
            except HttpError as e:
                fails += 1
                if fails > 4 or not rate_limited(e):
                    raise
                log.warning("Google rate limit during upload, waiting 30s (%d/4)", fails)
                time.sleep(30)
        return resp["webViewLink"]

    def log_row(self, sheet_id, action, details):
        try:
            ex(self.s.spreadsheets().values().append(
                spreadsheetId=sheet_id, range="'Log'!A:C", valueInputOption="RAW",
                insertDataOption="INSERT_ROWS",
                body={"values": [[now(), action, details[:300]]]}), tries=2)
        except Exception as e:  # noqa: BLE001 - the log tab is nice-to-have
            log.debug("log row skipped: %s", e)


class Table:
    """One sheet tab. Columns are found by header name (case-insensitive), like the frontend does."""

    def __init__(self, dr: Drive, sheet_id, tab):
        self.dr, self.id, self.tab = dr, sheet_id, tab
        self.load()

    def load(self):
        vals = ex(self.dr.s.spreadsheets().values().get(
            spreadsheetId=self.id, range=f"'{self.tab}'")).get("values", [])
        self.header = [h.strip().lower() for h in vals[0]] if vals else []
        self.rows = []
        for i, r in enumerate(vals[1:], start=2):
            r = list(r) + [""] * (len(self.header) - len(r))
            self.rows.append((i, dict(zip(self.header, r))))

    def ensure(self, name):
        """Add a column to the sheet header when it is missing (update() would silently skip it)."""
        if name.lower() in self.header:
            return
        try:
            ex(self.dr.s.spreadsheets().values().update(
                spreadsheetId=self.id, range=f"'{self.tab}'!{col_letter(len(self.header) + 1)}1",
                valueInputOption="RAW", body={"values": [[name]]}))
            self.header.append(name.lower())
        except HttpError as e:
            log.warning("could not add the %r column (%s); the link is skipped", name, str(e)[:100])

    def update(self, rownum, values):
        data = []
        for k, v in values.items():
            k = k.lower()
            if k in self.header:
                data.append({"range": f"'{self.tab}'!{col_letter(self.header.index(k) + 1)}{rownum}",
                             "values": [[v]]})
        if data:
            ex(self.dr.s.spreadsheets().values().batchUpdate(
                spreadsheetId=self.id, body={"valueInputOption": "USER_ENTERED", "data": data}))


# --------------------------------------------------------------------------- #
# Config merge: global frame_config.json < project_config.json < Playlist row
# --------------------------------------------------------------------------- #
def flat(d, out=None):
    out = {} if out is None else out
    for k, v in (d or {}).items():
        if isinstance(v, dict):
            flat(v, out)
        else:
            out[k] = v
    return out


def klist(v):
    if isinstance(v, list):
        return [str(x).strip() for x in v if str(x).strip()]
    return [x.strip() for x in re.split(r"[,\n]", str(v or "")) if x.strip()]


def global_cfg(g):
    out = {}
    for k, f in (("light", "light_models"), ("medium", "medium_models"),
                 ("code", "code_models"), ("tts", "tts_models")):
        v = (g.get("models") or {}).get(k)
        if v:
            out[f] = ",".join(klist(v))
    for k in ("rpm", "tts_rpm", "max_wait", "tts_group_scenes", "tts_group_chars", "scene_batch"):
        v = flat(g.get("limits")).get(k)
        if v not in (None, ""):
            out[k] = v
    font = (g.get("tools") or {}).get("urdu_font")
    if font:
        out["urdu_font"] = font
    return out


def coerce(name, v):
    t = FIELD_TYPES[name]
    if t == "list":
        if isinstance(v, list):
            return v
        try:
            a = json.loads(str(v or "").strip() or "[]")
            return a if isinstance(a, list) else None
        except ValueError:
            return None
    if v is None or (isinstance(v, str) and not v.strip()):
        return None
    try:
        if t == "float":
            return float(v)
        if t == "int":
            return int(float(v))
        if t == "bool":
            return v if isinstance(v, bool) else str(v).strip().lower() in ("1", "true", "yes", "y")
    except ValueError:
        return None
    s = str(v).strip()
    return LANG.get(s.lower(), s.lower()) if name in ("language", "screen_language") else s


def only_fields(d):
    out = {}
    for k, v in d.items():
        if k in FIELD_TYPES:
            c = coerce(k, v)
            if c is not None:
                out[k] = c
    return out


def clip_for_backend(i, c):
    """Frontend clip (camelCase, saved in the sheet) -> the snake_case shape the backend uses (same as frontend clipsFor)."""
    return {"order": i, "name": c.get("name", ""), "type": c.get("kind", "image"), "file_id": c.get("fileId", ""),
            "url": c.get("url", ""), "start_sec": c.get("start", 0), "duration_sec": c.get("duration", 5),
            "position": c.get("position", "bottom-right"), "width_pct": c.get("width", 25),
            "opacity_pct": c.get("opacity", 100), "loop": bool(c.get("loop")), "mute": c.get("mute", True),
            "note": c.get("note", "")}


def build_job(gcfg, pcfg, row):
    d = {**gcfg, **flat(pcfg)}
    for col, f in ROW_MAP.items():
        if str(row.get(col, "")).strip():
            d[f] = row[col]
    if "aspect" in d:   # row > project: an unknown value on a row falls back to the project's (or the 16:9 default)
        ok = normalize_aspect(d["aspect"])
        if not ok:
            log.warning("  aspect %r is not one of 16:9, 9:16, 1:1, 4:5: using the project default", d["aspect"])
            ok = normalize_aspect(flat(pcfg).get("aspect")) or normalize_aspect(gcfg.get("aspect"))
        d["aspect"] = ok or "16:9"
    extra = str(row.get("extra config (json)", "")).strip()
    if extra:
        try:
            d.update(flat(json.loads(extra)))
        except ValueError:
            log.warning("  bad Extra Config JSON, ignored")
    clips = str(row.get("custom clips (json)", "")).strip()    # video level only: never inherited from project / global
    d.pop("custom_clips", None)
    if clips:
        try:
            raw = json.loads(clips)
            d["custom_clips"] = [clip_for_backend(i, c) for i, c in enumerate(raw if isinstance(raw, list) else [], 1)
                                 if isinstance(c, dict) and (c.get("fileId") or c.get("url"))]
        except ValueError:
            log.warning("  bad Custom Clips JSON, ignored")
    scenes = str(row.get("scenes (json)", "")).strip()          # video level only: the user's storyboard (descriptions / own files)
    d.pop("custom_scenes", None)
    if scenes:
        d["custom_scenes"] = load_scenes(scenes)               # frontend JSON, or plain text typed into the cell
    if not str(d.get("topic", "")).strip():
        d["topic"] = row.get("video title", "")        # a script row has no prompt: its title is the topic
    script, mode = str(d.get("script", "")).strip(), str(d.get("input_type", "")).strip().lower()
    if mode == "source" or (not mode and script and str(d.get("source_mode", "")).strip()):
        d["input_type"], d["source_text"] = "source", script      # the source text lives in the Script column
        d["script"] = ""
    if str(d.get("engine", "")).strip().lower() == "both":
        d["engine"] = "auto"
    fixed = str(d.get("engine_fixed", "")).strip().lower() in ("1", "true", "yes", "y", "fixed")
    d["engine_fallback"] = not fixed and str(d.get("engine", "auto")).strip().lower() != "auto"
    if script and mode not in ("prompt",) and not str(row.get("language", "")).strip():
        # the row did not pick a language: follow the script itself (an Urdu script must not be narrated as English)
        have = LANG.get(str(d.get("language", "en")).strip().lower(), "en")
        seen = detect_language(script)
        if seen and seen != have:
            log.info("  script is written in %s but the playlist language is %s: using %s", seen, have, seen)
            d["language"] = seen
    tm = str(d.get("tts_model") or "").strip()      # narrator's chosen TTS model goes first
    if tm:
        rest = [] if d.get("tts_strict") in (True, "true", "True", "TRUE") else \
            [m for m in csv(d.get("tts_models") or DEFAULT_TTS_MODELS) if m != tm]
        d["tts_models"] = ",".join([tm] + rest)
    job = only_fields(d)
    job["workdir"], job["output_dir"] = "build", "output"
    return job


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def safe_name(title):
    return re.sub(r'[\\/:*?"<>|]+', " ", title).strip()[:120] or "video"


MAX_SCENE_FILE = 60 * 1024 * 1024


def drive_id(url):
    m = re.search(r"/(?:d|folders)/([\w-]{10,})", url or "") or re.search(r"[?&]id=([\w-]{10,})", url or "")
    return m.group(1) if m else ""


def fetch_scene_files(dr, cfg):
    """Download the user's own scene files (Manim .py / Remotion .tsx .zip ...) into build/<slug>/scene_files and set local_path.
    A file that cannot be fetched is left without local_path: that scene is then built by Frame from its description."""
    todo = [c for c in cfg.custom_scenes if c.get("source") == "file"]
    if not todo:
        return
    dest = Path("build") / cfg.slug / "scene_files"
    dest.mkdir(parents=True, exist_ok=True)
    for c in todo:
        name = re.sub(r"[^\w.\-]+", "_", c.get("file_name") or "scene")[:80]
        try:
            fid = c.get("file_id") or drive_id(c.get("url"))
            if not fid:
                raise ValueError("no Drive file id")
            data = ex(dr.d.files().get_media(fileId=fid))
            if len(data) > MAX_SCENE_FILE:
                raise ValueError("file is larger than 60 MB")
            p = dest / f"scene-{int(c['order']):02d}-{name}"
            p.write_bytes(data)
            c["local_path"] = str(p)
            log.info("  scene %s: downloaded your file %s (%d KB)", c["order"], c.get("file_name"), len(data) // 1024)
        except Exception as e:  # noqa: BLE001
            c["local_path"] = ""
            log.warning("  scene %s: could not download %s (%s); Frame builds it from the description", c["order"], c.get("file_name"), str(e)[:120])


def process_project(dr, svc, gcfg, name, prow, per_turn):
    pfolder = prow.get("folder id", "")
    sheet_id = prow.get("sheet id", "")
    if not pfolder:
        log.warning("[%s] no folder id, skipped", name)
        return "ok"
    if not sheet_id:
        f = dr.find(f"{name} - Record", pfolder, SHEET)
        sheet_id = f["id"] if f else ""
    if not sheet_id:
        log.warning("[%s] no record sheet, skipped", name)
        return "ok"
    pc = dr.find("project_config.json", pfolder)
    pcfg = dr.read_json(pc["id"]) if pc else {}
    try:
        table = Table(dr, sheet_id, "Playlist")
    except HttpError as e:
        if getattr(getattr(e, "resp", None), "status", None) == 400:   # tab not created yet = nothing was ever added
            log.info("[%s] no Playlist tab yet (no videos added), skipped", name)
            return "ok"
        raise

    todo = []
    for rn, row in table.rows:
        st = row.get("status", "").strip().lower()
        stale = st == "in process" and row.get("notes", "").startswith(GH_TAG)   # a cancelled earlier run
        if st == "waiting" or stale:
            todo.append((rn, row))
    if per_turn > 0:
        todo = todo[:per_turn]
    log.info("[%s] %d video(s) to make", name, len(todo))

    for rn, row in todo:
        if (time.time() - T0) / 60 > MAX_MIN:
            log.info("time budget used, remaining videos wait for the next run")
            return "budget"
        title = row.get("video title") or f"video-{rn}"
        table.update(rn, {"Status": "In Process", "Notes": f"{GH_TAG} started {now()}", "Updated": now()})
        dr.log_row(sheet_id, "Build started", title)
        try:
            cfg = VideoConfig(**build_job(gcfg, pcfg, row))
            log.info("[%s] #%d %s (%s)", name, rn - 1, title, {"script": "own script", "source": "source text (" + cfg.source_mode + ")"}.get(cfg.input_type, "prompt"))
            fetch_scene_files(dr, cfg)
            final = make_video(cfg, svc)
            out = cfg.output_folder_id or dr.folder("Output", pfolder)   # the frontend gives every video its own Output folder
            link = dr.upload(final, safe_name(title) + ".mp4", out)
            srt, caps = Path(final).with_suffix(".srt"), ""     # captions go next to the video, same name: "<title>.srt"
            if srt.exists():
                try:
                    caps = dr.upload(srt, safe_name(title) + ".srt", out)
                except Exception as e:  # noqa: BLE001   the video is already saved: a caption hiccup must not fail it
                    log.warning("[%s] #%d captions (.srt) were not uploaded: %s", name, rn - 1, str(e)[:150])
            if caps:
                table.ensure("Captions Link")
            note, rep_file = "", Path(final).with_suffix(".scenes.json")
            if rep_file.exists():                               # what became of the user's storyboard
                try:
                    from scene_plan import summary_line
                    note = summary_line(json.loads(rep_file.read_text(encoding="utf-8")))
                    dr.upload(rep_file, safe_name(title) + ".scenes.json", out)
                except Exception as e:  # noqa: BLE001   the video is already saved: a report hiccup must not fail it
                    log.warning("[%s] #%d scene report was not saved: %s", name, rn - 1, str(e)[:150])
            table.update(rn, {"Status": "Completed", "Output Link": link, "Captions Link": caps, "Notes": note, "Updated": now()})
            dr.log_row(sheet_id, "Video saved", title)
            shutil.rmtree(Path("build") / cfg.slug, ignore_errors=True)
            Path(final).unlink(missing_ok=True)
            for ext in (".srt", ".manifest.json", ".scenes.json"):
                Path(final).with_suffix(ext).unlink(missing_ok=True)
        except QuotaExhausted as e:
            table.update(rn, {"Status": "Waiting", "Notes": f"{GH_TAG} quota, retry next run", "Updated": now()})
            log.error("%s", e)
            return "quota"
        except Exception as e:  # noqa: BLE001
            log.exception("video failed: %s", title)
            table.update(rn, {"Status": "Failed", "Notes": f"{GH_TAG} {str(e)[:300]}", "Updated": now()})
            dr.log_row(sheet_id, "Video failed", f"{title}: {str(e)[:150]}")
    return "ok"


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%H:%M:%S")
    dr = Drive()
    root_id = os.environ.get("FRAME_ROOT_ID") or (dr.find("Frame AI", mime=FOLDER) or {}).get("id")
    if not root_id:
        sys.exit("Folder 'Frame AI' not visible. Wrong Google account / client id?")
    cfg_dir = (dr.find("Application Config", root_id, FOLDER) or dr.find("Application Config", mime=FOLDER))
    if not cfg_dir:
        sys.exit("'Application Config' folder not found")
    cfg_dir = cfg_dir["id"]

    fc = dr.find("frame_config.json", cfg_dir)
    if not fc:
        sys.exit("frame_config.json not found (open the frontend once and save the Control room)")
    g = dr.read_json(fc["id"])
    keys = g.get("keys") or {}
    gem_keys, cloud = klist(keys.get("gemini")), klist(keys.get("cloudTts"))
    tts_keys = klist(keys.get("tts")) or gem_keys
    if not gem_keys:
        sys.exit("keys.gemini is empty in frame_config.json")
    for k in set(gem_keys + tts_keys + cloud):
        print(f"::add-mask::{k}")

    gcfg = only_fields(global_cfg(g))
    probe = VideoConfig(topic="probe", **gcfg)
    svc = Services(
        Gemini(gem_keys, csv(probe.light_models), csv(probe.code_models), probe.rpm, probe.max_wait,
               medium_models=csv(probe.medium_models)),
        TTS(tts_keys, probe.tts_rpm, probe.max_wait, cloud))

    sheet = lambda n: (dr.find(n, cfg_dir, SHEET) or {}).get("id")      # noqa: E731
    pid = sheet("Projects")
    if not pid:
        sys.exit("'Projects' sheet not found")
    projects = {r.get("project name", ""): r for _, r in Table(dr, pid, "Projects").rows}

    # every buildable project, found by its Drive folder id: playlists (Projects sheet) + the Singles folder
    known = {}     # folder id -> (name, row of the Projects sheet)
    for pname, r in projects.items():
        if str(r.get("folder id", "")).strip():
            known[r["folder id"].strip()] = (pname, r)
    sf = dr.find(SINGLES, root_id, FOLDER)
    if sf:
        known[sf["id"]] = (SINGLES, {"folder id": sf["id"], "sheet id": ""})   # its sheet is "Singles - Record"

    queue, listed = [], set()     # queue = [(name, project row, videos_per_turn)]
    plan_id = sheet("Production Plan")
    if plan_id:
        plan = [r for _, r in Table(dr, plan_id, "Plan").rows]
        plan.sort(key=lambda r: int(r["order"]) if str(r.get("order", "")).strip().isdigit() else 9999)
        for r in plan:
            fid, pname = str(r.get("folder id (do not edit)", "")).strip(), str(r.get("project", "")).strip()
            hit = known.get(fid) or next((v for k, v in known.items() if v[0] == pname and not fid), None)
            if hit:
                listed.add(hit[1]["folder id"].strip())
            if r.get("state", "").strip().lower() != "active":
                continue                                # paused: stays out of the queue
            if not hit:
                log.warning("[%s] in plan but its folder was not found, skipped", pname)
                continue
            n = str(r.get("videos per turn", "")).strip()
            queue.append((hit[0], hit[1], int(n) if n.isdigit() else 0))
    # projects the plan does not mention yet (a new playlist, or Singles before the plan was saved) join the end,
    # active, like the frontend does when it opens the Production tab
    for fid, (pname, prow) in known.items():
        if fid not in listed:
            queue.append((pname, prow, 0))

    for name, prow, per_turn in queue:
        try:
            if process_project(dr, svc, gcfg, name, prow, per_turn) in ("quota", "budget"):
                break
        except Exception:  # noqa: BLE001 - one broken playlist must not stop the others
            log.exception("[%s] playlist failed", name)
    log.info("run finished in %.0f min", (time.time() - T0) / 60)


if __name__ == "__main__":
    main()
