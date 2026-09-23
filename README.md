# LinkedIn Saved Posts Archiver

Copies your LinkedIn saved posts into the Google Sheet **LinkedIn Saved Posts Archive**
(full text, author, links, media, posted date) and unsaves each post **only after the
sheet confirms the row was written**.

## One-time setup (~10 min)

### 1. Connect the Google Sheet
1. Open the sheet *LinkedIn Saved Posts Archive* in your Google Drive.
2. **Extensions → Apps Script**. Replace the contents of `Code.gs` with `apps_script/Code.gs`.
3. Change `SECRET_TOKEN` to a long random string (e.g. run `openssl rand -hex 24`).
4. **Deploy → New deployment → Web app**. Execute as: *Me*. Who has access: *Anyone*.
   Authorize when asked, then copy the Web app URL (ends in `/exec`).

### 2. Install the script on your Mac
```bash
cd linkedin-saved-archiver
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp config.example.json config.json   # paste webapp_url and the same token
```
`browser_channel: "chrome"` uses your installed Google Chrome. If you don't have it,
set it to `""` and run `playwright install chromium`.

If your LinkedIn is in Spanish, set `"locale": "es-ES"`.

### 3. First run: test without changing anything
```bash
python archiver.py --dry-run --limit 3
```
A browser window opens. Log in to LinkedIn the first time (the session is kept in
`~/.linkedin-archiver/`). The script prints what it extracted from 3 posts.

## Everyday use
```bash
python archiver.py --no-unsave --limit 10   # fill the sheet, keep posts saved
python archiver.py                          # archive + unsave (25 per run by default)
python archiver.py --debug                  # save screenshots/HTML when something fails
```

## Safety rules built in
- A post is unsaved only if the sheet returns `appended` or `duplicate` for its ID
  (the Apps Script reads the row back after writing).
- If the text or author can't be read, the post is skipped and stays saved.
- Posts already in the sheet are never duplicated (dedupe by Post ID).
- Random 3–7 s pauses and a per-run cap keep traffic human-paced. LinkedIn's terms
  restrict automated access, so keep runs small and occasional.

## Columns
Archived At · Post URL · Post ID · Author · Author Headline · Author Profile URL ·
Posted Date (decoded from the post ID, exact) · Posted (as shown) · Post Text ·
External Links · Media URLs · Status

## When LinkedIn changes its page
LinkedIn changes its HTML now and then. If posts are skipped or Unsave isn't found,
run with `--debug` and look at the files in `debug/`. The selectors are in
`EXTRACT_JS` and `unsave_post()` in `archiver.py`. If the Unsave menu text in your
language isn't recognized, add it to `extra_unsave_labels` in `config.json`.
