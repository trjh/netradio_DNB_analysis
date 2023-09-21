function processDataManually() {
  var sheet = SpreadsheetApp.getActiveSpreadsheet().getActiveSheet();
  var data = sheet.getDataRange().getValues();
  
  var masterOffset = 0;
  var trackNum = '';
  var trackTitle = '';
  var filename = '';
  var setfields = [1, 5, 6, 7, 8, 9, 10];

  // Initialize a dictionary-like structure to store sync points
  var syncPoints = {};

  // Loop through each row in the spreadsheet
  for (var i = 1; i < data.length; i++) {
    var row = data[i];
    
    // Parse the columns
    var timestamp = row[1];
    var label = row[3];
    var entryType = '';
    var note = '';
    var synclabel = '';
    console.log('Processing row ' + i + ' ts ' + timestamp + ' entry ' + label)
    
    // Extract track number and title from the third column (e.g., "start001: ID: promo1")
    var match = '';
    
    if (label == '') {
      for (var f = 0; f < setfields.length; f++) {
        sheet.getRange(i + 1, setfields[f]).setValue("");
      }
      continue;
    }
    else if (match = /start(\d+):\s*ID:\s*(.+)/.exec(label)) {
      console.log('found track ' + label)
      trackNum = match[1];
      trackTitle = match[2];
      entryType = 'TrackStart'
    
      // Split trackTitle into name and artist if possible
      var titleParts = trackTitle.split(' - ');
      var trackName = (titleParts.length > 1) ? titleParts[1] : trackTitle;
      var trackArtist = (titleParts.length > 1) ? titleParts[0] : '';
    }
    else if (match = /file (start)? sync: (.+):? ([0-9.]+)/.exec(label)) {
      console.log('found file (start) sync')
      if (match[1] == "start") {
        entryType = 'File Start Sync'
      } else {
        entryType = 'File Sync'
      }
      filename = match[2];
      masterOffset = parseFloat(match[3]);
    }
    // Detect track and original sync labels
    else if (trackSyncMatch = /track\s+sync:\s+(.)/.exec(label)) {
      synclabel = 'track' + trackSyncMatch[1];
      if (!(trackNum in syncPoints)) { syncPoints[trackNum] = {}; }
      syncPoints[trackNum][synclabel] = parseFloat(timestamp);
      entryType = 'Track Sync'
    }
    else if (origMatch = /orig(\d+)\s+sync:\s+(.)/.exec(label)) {
      synclabel = 'orig' + origMatch[2];
      if (!(origMatch[1] in syncPoints)) { syncPoints[origMatch[1]] = {}; }
      syncPoints[origMatch[1]][synclabel] = parseFloat(timestamp);
      entryType = 'Orig Sync'
    }
    else if (match = /[Oo]rig(\d+)\s+sync:\s+(.)(.*)/.exec(label)) {
      entryType = 'Fix Me'
    }
    else {
      entryType = 'Note';
      note = label;
    }

    if (trackNum in syncPoints) {
      // Calculate speed difference when you have all four values
      var syncPoint = syncPoints[trackNum];
      // if (syncPoint.origA && syncPoint.origB && syncPoint.trackA && syncPoint.trackB) 
      console.log('keys in syncPoints[' + trackNum + ']: ' + Object.keys(syncPoint).length + ' : ' + Object.keys(syncPoint))
      if (false) {
        var speedDiff = (syncPoint.trackB - syncPoint.trackA) / (syncPoint.origB - syncPoint.origA);
        // Store or log the speed difference as needed
        Logger.log('Track ' + trackNum + ' Speed Difference: ' + speedDiff);
        sheet.getRange(i + 1, 11).setValue(speedDiff);
      }
    }

    // Populate the spreadsheet columns
    sheet.getRange(i + 1, 1).setValue(masterOffset + parseFloat(row[1]));
    sheet.getRange(i + 1, 5).setValue(filename);
    sheet.getRange(i + 1, 6).setValue(trackNum);
    sheet.getRange(i + 1, 7).setValue(entryType);
    sheet.getRange(i + 1, 8).setValue(note);
    sheet.getRange(i + 1, 9).setValue(trackName);
    sheet.getRange(i + 1, 10).setValue(trackArtist);
  }
}