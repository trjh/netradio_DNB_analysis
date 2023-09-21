function do_not_use() {
  var sheet = SpreadsheetApp.getActiveSpreadsheet().getActiveSheet();
  var data = sheet.getDataRange().getValues();

  // Initialize a dictionary-like structure to store sync points
  var syncPoints = {};

  // Loop through each row in the spreadsheet
  for (var i = 0; i < data.length; i++) {
    var row = data[i];

    // Parse the columns
    var timestamp = row[0];
    var trackNum = '';
    var label = '';

    // Detect orig labels
    var origMatch = /orig(\d+)\s+sync:\s+(.)/.exec(row[1]);
    if (origMatch) {
      trackNum = origMatch[1];
      label = 'orig' + origMatch[2];
    }

    // Detect track sync labels
    var trackSyncMatch = /track\s+sync:\s+(.)/.exec(row[1]);
    if (trackSyncMatch) {
      trackNum = ''; // You may need to determine trackNum based on context
      label = 'track' + trackSyncMatch[1];
    }

    // If trackNum is known and label is valid, store the timestamp
    if (trackNum && (label === 'origA' || label === 'origB' || label === 'trackA' || label === 'trackB')) {
      if (!syncPoints[trackNum]) {
        syncPoints[trackNum] = {};
      }
      syncPoints[trackNum][label] = parseFloat(timestamp);
    }
  }

  // Calculate speed difference when you have all four values
  for (var trackNum in syncPoints) {
    var syncPoint = syncPoints[trackNum];
    if (syncPoint.origA && syncPoint.origB && syncPoint.trackA && syncPoint.trackB) {
      var speedDiff = (syncPoint.trackB - syncPoint.trackA) / (syncPoint.origB - syncPoint.origA);
      // Store or log the speed difference as needed
      Logger.log('Track ' + trackNum + ' Speed Difference: ' + speedDiff);
    }
  }
}
