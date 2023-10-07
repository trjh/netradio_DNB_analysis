// Define global variables
var repoUrl = 'https://api.github.com/repos/trjh/netradio_DNB_analysis/contents/labels';
var data = [];
var sheet = SpreadsheetApp.getActiveSpreadsheet().getActiveSheet();

// Initialize a dictionary-like structure to store sync points
var syncPoints = {};

function GithubImport() {
  // Get the PAT from the "SECRETS" sheet, assuming it's stored in cell A2
  var secretsSheet = SpreadsheetApp.getActiveSpreadsheet().getSheetByName('SECRETS');
  var authToken = secretsSheet.getRange('A2').getValue();

  // Fetch the list of files in the GitHub repository
  var headers = { 'Authorization': 'token ' + authToken };
  var options = { 'method': 'GET', 'headers': headers };

  var repoResponse = UrlFetchApp.fetch(repoUrl, options);
  var repoData = JSON.parse(repoResponse.getContentText());

  console.log("INFO: repoData = " + repoData)
  // Loop through each file in the repository
  for (var i = 0; i < repoData.length; i++) {
    var file = repoData[i];

    // Check if the file is a .tsv file
    if (file.name.endsWith('.tsv')) {
      console.log('INFO: Reading file: ' + file.name)

      // Get the raw content of the .tsv file
      var fileContentResponse = UrlFetchApp.fetch(file.download_url);
      var fileContent = fileContentResponse.getContentText();

      // Call ParseTSV to process the file content
      ParseTSV(fileContent);
    }
  }

  // Call updatesheet to write data to the sheet
  updatesheet();
}

function ParseTSV(fileContent) {
  var tsvRows = fileContent.split('\n');
  var masterOffset = 0;
  var trackNum = '';
  var trackTitle = '';
  var wavFilename = '';

  for (var j = 0; j < tsvRows.length; j++) {
    var tsvRow = tsvRows[j].split('\t');
    if (tsvRow.length < 3) {
      console.log('WARN: Not enough fields in line: j=' + j + ' data:' + tsvRow)
      continue
    }

    // Parse the columns
    var timestamp = tsvRow[0];
    var label = tsvRow[2];

    // Set default entry type, note,
    // syncLabel/syncTrack/syncDiff for computing speed difference,
    // and default match result
    var entryType = '';
    var note = '';
    var syncLabel = '';
    var syncDiff = '';
    var syncNum = undefined;
    var match = '';
    

    // Log data
    console.log('DEBUG: Processing line ' + j + ' ts ' + timestamp + ' entry ' + label)
    if (isNotFloat(timestamp)) {
      console.log('WARN: Timestamp [' + timestamp + '] is not a float, j=' + j + ' data:' + tsvRow);
      continue;
    }

    if (label == '') {
      var rowData = ['', '', '', '', '', '', '', '', '', ''];
      data.push(rowData);
      continue;
    }
    else if (match = /(start(\d+):\s*)?ID(\d+)?:\s*(.+)/.exec(label)) {
      console.log('DEBUG: found track ' + label)
      trackNum = match[2] || match[3];
      trackTitle = match[4];
      if (match[1] !== undefined) {
        entryType = 'TrackStart'
      } else {
        entryType = 'TrackID'
      }

      // Split trackTitle into name and artist if possible
      var titleParts = trackTitle.split(' - ');
      var trackName = (titleParts.length > 1) ? titleParts[1] : trackTitle;
      var trackArtist = (titleParts.length > 1) ? titleParts[0] : '';
    }
    else if (match = /^file (start)? sync: (.+):? ([0-9.]+)\s*(.*)/.exec(label)) {
      console.log('DEBUG: found file (start) sync')
      if (match[1] == "start") {
        entryType = 'File Start Sync'
      } else {
        entryType = 'File Sync'
      }
      wavFilename = match[2];
      masterOffset = parseFloat(match[3]);
      note = wavFilename + " " + masterOffset + " " +match[4]
      trackNum = ""
      trackName = ""
    }
    // Detect track and original sync labels
    // match:         1 2      3      4     5                6  7
    else if (match = /((track)(\d+)?|(orig)(\d+))\s+sync:\s+(.)(.*)/.exec(label)) {
      entryType = match[2] ? 'Track Sync' : 'Orig Sync';
      syncLabel = (match[2] || match[4]) + match[6];
      syncNum = (match[5] || match[3] || trackNum)
      note = syncNum + " " + match[6] + match[7]

      // only calculate using track points A and B
      if (match[6] == "A" || match[6] == "B") {
        // make sure the first level is defined
        syncPoints[syncNum] = syncPoints[syncNum] || {};
        syncPoints[syncNum][syncLabel] = parseFloat(timestamp);
        console.log("DEBUG: added syncPoints[" + syncNum + "][" + syncLabel + "] = " + timestamp)
      }
    }
    else if (match = /orig(\d+)\s+(start|end|note):\s+(.*)/.exec(label)) {
      entryType = 'Orig ' + match[2].charAt(0).toUpperCase() + match[2].slice(1)
      note = match[1] + ": " + match[3]
    }
    else if (match = /^(file|mix) (start|end|note): (.*)/.exec(label)) {
      // any other valid type that is just a note
      entryType = match[1].charAt(0).toUpperCase() + match[1].slice(1) + ' ' +
                  match[2].charAt(0).toUpperCase() + match[2].slice(1)
      note = match[3];
      if (entryType == "File Start") {
        wavFilename = note;
        note = ""
        trackNum = ""
        trackName = ""
      }
    }
    else {
      entryType = 'Note';
      if (label.slice(0,6) == 'note: ') {
        note = label.slice(6)
      } else {
        note = label;
      }
    }

    // Calculate speed difference when you have all four values
    if (syncNum && (syncNum in syncPoints)) {
      var syncPoint = syncPoints[syncNum];
      console.log('DEBUG: keys in syncPoints[' + syncNum + ']: ' + Object.keys(syncPoint).length + ' : ' + Object.keys(syncPoint))
      if (Object.keys(syncPoint).length == 4) {
        syncDiff = (syncPoint.trackB - syncPoint.trackA) / (syncPoint.origB - syncPoint.origA);
        // Store or log the speed difference as needed
        Logger.log('DEBUG: Track ' + syncNum + ' Speed Difference: ' + syncDiff);
      }
    }

    // Instead of using 'sheet.getRange', you can push data into an array and set the values in one go
    var rowData = [
      masterOffset + parseFloat(timestamp),
      parseFloat(tsvRow[0]),
      parseFloat(tsvRow[1]),
      tsvRow[2],
      wavFilename,
      (syncNum || trackNum),
      entryType,
      note,
      (syncNum == undefined) ? trackName : "",
      (syncNum == undefined) ? trackArtist : "",
      (syncNum == undefined) ? "" : syncDiff,
    ];

    // Push the row data into the data array
    data.push(rowData);
  }
}

function updatesheet() {
  // Set the values in the sheet in one batch operation
  sheet.getRange(2, 1, data.length, data[0].length).setValues(data);
}

// Call GithubImport to start the process
// GithubImport();

function isNotFloat(value) {
  // Use parseFloat to attempt to convert the value to a floating-point number
  var floatValue = parseFloat(value);

  // Check if the conversion result is NaN (Not-a-Number) and if it's not equal to the original value
  // return isNaN(floatValue) || floatValue.toString() !== value.toString();

  // the above doesn't work as audacity pads with trailing zeros, so just return isNan
  return isNaN(floatValue);
}