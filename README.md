# Naye client ka setup (about 3 minutes)

Har client ka apna GitHub repo, apna Google Drive, apna refresh token. Kisi ka data kisi ke saath share nahi hota.

## Tum (operator), ek baar
1. Is repo ko GitHub par **Template repository** bana do (Settings > Template repository).
2. Agar GitHub Organization hai: `GOOGLE_CLIENT_SECRET` ko **organization secret** bana do, taake har client repo khud inherit kare.
   (Private repos par org secrets ke liye plan check kar lo; warna har repo ma ye secret manually daalna hoga.)
3. Frontend deploy karo (Vercel) aur ye env vars set karo: `VITE_GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`.
   `/api/exchange` isi secret se kaam karta hai. Google OAuth consent screen ko **Publish** karo (Testing ma token 7 din ma expire hota hai).

## Har client ke liye
1. Template se naya repo banao (**private** rakho, logs me topics/titles hote hain).
2. Client frontend khol kar Google login kare, **Control room** ma Gemini keys daale aur save kare.
3. Control room > **Connect backend** > Copy token.
4. Repo > Settings > Secrets and variables > Actions > New secret: `GOOGLE_REFRESH_TOKEN` = token.
   (`GOOGLE_CLIENT_SECRET` agar org secret nahi hai to wo bhi yahin.)
5. Actions tab > "Frame AI - build waiting videos" > Run workflow. Is ke baad har 6 ghante khud chalega.

## Masle
- "Google refused the refresh token": Connect backend dobara karo aur secret badal do.
- "Folder 'Frame AI' not visible": token kisi aur Google account ka hai, ya frontend ka client id alag hai.
- GitHub free private repos ma ~2000 min/month hain; workflow ki cron kam kar do ya paid minutes lo.

## Singles aur Script wali videos
- **Singles**: frontend ka *Singles* tab `Frame AI/Singles/` folder use karta hai (apni sheet `Singles - Record`, apna narrator/thumbnail style, koi shared description nahi).
  Backend usay playlist ki tarah build karta hai; video `Singles/Output/` mein jati hai. Agar Production Plan mein Singles (ya koi nayi playlist) abhi save nahi hui to wo plan ke aakhir mein khud shamil ho jati hai. Paused project phir bhi skip hota hai.
- **Script wali video**: sheet ki row mein `Input Type = Script` aur `Script` column bhara ho (ya sirf `Script` bhara ho) to Gemini script nahi likhta. Script jaisi hai waisi narration banti hai:
  saaf hoti hai (markdown, SRT), jumlon par ~`scene_sec` seconds ke scenes mein baanti jati hai, aur har scene ko chhota heading milta hai
  (markdown `#` heading ho to wahi, warna ek sasta Gemini call). Uske baad audio, visuals aur render pehle jaise chalte hain.
- Row mein Language khali ho aur script Urdu script mein ho to language khud `ur` ho jati hai (aur Latin script ho to `en`).
- Prompt wali rows (`Input Type = Prompt` ya khali) bilkul pehle ki tarah chalti hain.
- CLI: `python pipeline.py "Video title" --script-file script.txt`

## Remotion + Manim (engine per scene)
- Har scene ke liye Gemini khud chunta hai: **Remotion** (React/TSX: flows, cards, timelines, counters, text, Urdu) ya **Manim** (sirf asli math: axes par function graph, geometry, vector/matrix transform). Default hamesha Remotion hai.
- `engine` setting (VideoConfig / Extra Config JSON / CLI `--engine`): `auto` (default) | `remotion` | `manim`.
- Agar ek engine scene ko repair attempts ke baad bhi render na kar sake to dusra engine try hota hai, phir saada title card. Video kabhi ek scene ki wajah se nahi rukti.
- Remotion ke liye Node.js 18+ chahiye; pehli run par `build/remotion_project` mein khud install hota hai (aur Chrome download). Node na ho to sab scenes Manim se bante hain.
- Routing `build/<slug>/4b_route.json` mein aur har scene ka istemal-shuda engine `5_engines.json` mein save hota hai.
- Purane standalone tools (`run.py`, `scene_video.py`, `remotion_video.py`) bhi saath hain: `python run.py "Scene 1: ..."`.

## Scenes: apni description ya apni code file (Manim / Remotion)
Frontend ke **Scenes** section se har video ki scene list sheet ke `Scenes (JSON)` column mein aati hai (ya seedha text: `Scene 1: ...`). Har scene ya to **description** hai (Frame khud banata hai) ya **apni file** (`.py` Manim, `.tsx .jsx .ts .js .mjs .zip` Remotion) jo **jaisi hai waisi** chalti hai. Backend ab ye sab use karta hai:
- **Kitne scenes?** `scene_plan.py` pehle hisaab lagata hai ke video kitne scenes utha sakti hai (length, `scene_sec`, `scene_min_sec`, `max_scenes`). Aap ka scene-pace `scene_sec` se 1.5x tez ya 1.5x dheema ho to list jaisi hai waisi rehti hai.
  - **Kam scenes** -> Frame naye scenes khud banata hai (hook, bridge/analogy, worked example, recap), aap ke scenes apni jagah rehte hain. (Script wali video mein ye "free" parts hote hain jinhe Frame narration se design karta hai.)
  - **Zyada scenes** -> sab se kam zaroori scenes hata diye jate hain (pehla aur aakhri scene, aur apni file wale scenes sab se aakhir mein jate hain). Hata hue scene ki description pados wale scene mein "also cover" ban kar jud jati hai, to idea zaya nahi hota.
- **Kaun sa scene kahan?**
  - *Prompt* video: script aap ki storyboard ke gird likhi jati hai (ek scene = ek script scene, aap ki tarteeb mein; chapters scenes ki consecutive run lete hain).
  - *Script / Source text* wali video: narration jumlon par kaati jati hai aur Gemini har scene ko uska hissa batata hai (title, description aur apni file ke on-screen texts dekh kar). Jawab ghalat ya Gemini down ho to scenes narration par barabar phaila diye jate hain.
  - Apni file wale scene ke liye narration us file ke on-screen text dekh kar likhi jati hai.
- **Apni file**: Drive se `build/<slug>/scene_files` mein utarti hai, alag process mein, keys/tokens ke baghair, time limit ke saath chalti hai. Manim: pehli Scene class jis mein `construct()` ho. Remotion: component wali file video ke size aur narration ki lambai par wrap hoti hai; jis file/zip mein `registerRoot` + `<Composition>` ho wo apne size/length par jaisi hai chalti hai; `package.json` wali zip ko apna `npm install` milta hai. Aap ka clip video ki shakal se mukhtalif ho to wo kati nahi, blur background par poora dikhta hai; chhota ho to aakhri frame ruk jata hai.
- **Agar apni file chal na sake** (download/render error) to wahi scene description se (ya title card) ban jata hai; video fail nahi hoti. Report mein likha aata hai.
- **Report**: Output folder mein `<title>.scenes.json` (kept / added / dropped / folded, har scene kis engine se bana) aur sheet ke `Notes` mein ek line, e.g. `Scenes: 12 scenes from your 5, 2 your own file, 7 added by Frame`.
- **Settings** (Extra Config JSON ya project config): `"scene_fit": "exact"` = aap ki list jaisi hai waisi (sirf hard limit se kate, kabhi naye scene nahi), `"scene_min_sec": 6` = isse chhota scene kabhi plan nahi hota.
- CLI: `python pipeline.py "Title" --scenes-file scenes.txt` (text ya JSON; apni file: `{"source":"file","path":"my_scene.py"}`).
- Tests (offline, key ki zaroorat nahi): `python tests/test_scenes.py`
