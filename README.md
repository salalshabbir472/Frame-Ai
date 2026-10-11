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
