/**
 * LinkedIn Saved Posts Archive — receiver for the local archiver script.
 *
 * Paste this into the Google Sheet: Extensions > Apps Script, replace Code.gs,
 * change SECRET_TOKEN below, then Deploy > New deployment > Web app
 * (Execute as: Me, Who has access: Anyone). Copy the /exec URL into config.json.
 */

const SECRET_TOKEN = 'CHANGE-ME-to-a-long-random-string';
const SHEET_NAME = ''; // empty = first sheet

const HEADERS = [
  'Archived At', 'Post URL', 'Post ID', 'Author', 'Author Headline',
  'Author Profile URL', 'Posted Date', 'Posted (as shown)', 'Post Text',
  'External Links', 'Media URLs', 'Status'
];
const COL = Object.fromEntries(HEADERS.map((h, i) => [h, i + 1]));

function getSheet_() {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  const sh = SHEET_NAME ? ss.getSheetByName(SHEET_NAME) : ss.getSheets()[0];
  const first = sh.getRange(1, 1, 1, HEADERS.length).getValues()[0];
  if (first.join('|') !== HEADERS.join('|')) {
    sh.getRange(1, 1, 1, HEADERS.length).setValues([HEADERS]).setFontWeight('bold');
    sh.setFrozenRows(1);
  }
  return sh;
}

function idIndex_(sh) {
  const last = sh.getLastRow();
  const map = {};
  if (last < 2) return map;
  const ids = sh.getRange(2, COL['Post ID'], last - 1, 1).getValues();
  ids.forEach((r, i) => { if (r[0]) map[String(r[0])] = i + 2; });
  return map;
}

function json_(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj))
    .setMimeType(ContentService.MimeType.JSON);
}

function doPost(e) {
  let body;
  try { body = JSON.parse(e.postData.contents); }
  catch (err) { return json_({ ok: false, error: 'bad json' }); }
  if (body.token !== SECRET_TOKEN) return json_({ ok: false, error: 'unauthorized' });

  const lock = LockService.getScriptLock();
  lock.waitLock(30000);
  try {
    const sh = getSheet_();
    const index = idIndex_(sh);

    if (body.action === 'ping') return json_({ ok: true });

    if (body.action === 'known_ids') return json_({ ok: true, ids: Object.keys(index) });

    if (body.action === 'append') {
      const results = [];
      const rows = [];
      (body.posts || []).forEach(p => {
        const id = String(p.post_id || '');
        if (!id) { results.push({ post_id: id, status: 'rejected' }); return; }
        if (index[id]) { results.push({ post_id: id, status: 'duplicate' }); return; }
        rows.push([
          p.archived_at || new Date().toISOString(), p.post_url || '', id,
          p.author || '', p.author_headline || '', p.author_url || '',
          p.posted_date || '', p.posted_relative || '', (p.text || '').slice(0, 49000),
          (p.links || []).join('\n'), (p.media || []).join('\n'), 'Archived'
        ]);
        index[id] = -1;
        results.push({ post_id: id, status: 'appended' });
      });
      if (rows.length) {
        const start = sh.getLastRow() + 1;
        // Force text format on ID column so long numbers are not mangled.
        sh.getRange(start, COL['Post ID'], rows.length, 1).setNumberFormat('@');
        sh.getRange(start, 1, rows.length, HEADERS.length).setValues(rows);
        SpreadsheetApp.flush();
        // Read back to confirm the write landed.
        const check = idIndex_(sh);
        results.forEach(r => {
          if (r.status === 'appended' && !check[r.post_id]) r.status = 'write_failed';
        });
      }
      return json_({ ok: true, results });
    }

    if (body.action === 'set_status') {
      const row = index[String(body.post_id)];
      if (!row) return json_({ ok: false, error: 'not found' });
      sh.getRange(row, COL['Status']).setValue(body.status);
      return json_({ ok: true });
    }

    return json_({ ok: false, error: 'unknown action' });
  } finally {
    lock.releaseLock();
  }
}
