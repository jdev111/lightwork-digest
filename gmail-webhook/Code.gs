function doPost(e) {
  try {
    var data = JSON.parse(e.postData.contents);
    if (!data.to || !data.subject) {
      return ContentService.createTextOutput(JSON.stringify({
        status: "error",
        message: "Missing required fields: to, subject"
      })).setMimeType(ContentService.MimeType.JSON);
    }
    GmailApp.sendEmail(data.to, data.subject, data.textBody || "", {
      htmlBody: data.htmlBody || "",
      name: data.fromName || "Lightwork Follow-Up Tracker"
    });
    return ContentService.createTextOutput(JSON.stringify({
      status: "ok",
      message: "Email sent to " + data.to
    })).setMimeType(ContentService.MimeType.JSON);
  } catch (err) {
    return ContentService.createTextOutput(JSON.stringify({
      status: "error",
      message: err.toString()
    })).setMimeType(ContentService.MimeType.JSON);
  }
}

function doGet(e) {
  return ContentService.createTextOutput(JSON.stringify({
    status: "ok",
    message: "Lightwork Email Webhook is running"
  })).setMimeType(ContentService.MimeType.JSON);
}
