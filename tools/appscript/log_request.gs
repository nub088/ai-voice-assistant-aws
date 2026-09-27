/**
 * Clear Slope, Vapi intake logger
 *
 * Deploy as a Google Apps Script web app:
 *   Execute as: Me
 *   Who has access: Anyone
 *
 * The web app URL is the shared secret, keep it out of git.
 * Store it in .env as APPS_SCRIPT_WEB_APP_URL.
 */

var REQUIRED_FIELDS = [
  "caller_name",
  "callback_phone",
  "address",
  "identified_pest",
  "preferred_date",
  "preferred_time_window"
];

var COLUMNS = [
  "timestamp",
  "caller_name",
  "callback_phone",
  "address",
  "reported_pest",
  "identified_pest",
  "preferred_date",
  "preferred_time_window",
  "call_id",
  "notes"
];

function doPost(e) {
  try {
    var data = JSON.parse(e.postData.contents);

    for (var i = 0; i < REQUIRED_FIELDS.length; i++) {
      if (!data[REQUIRED_FIELDS[i]]) {
        return _json({ error: "missing required field: " + REQUIRED_FIELDS[i] }, 200);
      }
    }

    var sheet = SpreadsheetApp.getActiveSpreadsheet().getSheets()[0];

    if (sheet.getLastRow() === 0) {
      sheet.appendRow(COLUMNS);
    }

    sheet.appendRow([
      new Date().toISOString(),
      data.caller_name        || "",
      data.callback_phone     || "",
      data.address            || "",
      data.reported_pest      || "",
      data.identified_pest    || "",
      data.preferred_date     || "",
      data.preferred_time_window || "",
      data.call_id            || "",
      data.notes              || ""
    ]);

    return _json({ ok: true });

  } catch (err) {
    return _json({ error: err.message });
  }
}

function _json(obj) {
  return ContentService
    .createTextOutput(JSON.stringify(obj))
    .setMimeType(ContentService.MimeType.JSON);
}

/** Quick smoke test, run this manually from the Apps Script editor. */
function testDoPost() {
  var fake = {
    postData: {
      contents: JSON.stringify({
        caller_name: "Jane Test",
        callback_phone: "6045550000",
        address: "123 Fake St, Vancouver BC",
        reported_pest: "bugs in the kitchen",
        identified_pest: "cockroaches",
        preferred_date: "ASAP",
        preferred_time_window: "morning",
        call_id: "test-001",
        notes: "Ground floor unit, has a dog"
      })
    }
  };
  var result = doPost(fake);
  Logger.log(result.getContent());
}
