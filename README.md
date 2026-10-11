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
