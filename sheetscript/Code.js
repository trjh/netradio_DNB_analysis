// Define global variables
var repoUrl = 'https://api.github.com/repos/trjh/netradio_DNB_analysis/contents/labels';
var data = [];
var sheet = SpreadsheetApp.getActiveSpreadsheet().getActiveSheet();

// Initialize a dictionary-like structure to store sync points
var syncPoints = {};

function GithubImport() {
  // Fetch the list of files in the GitHub repository
  var repoResponse = UrlFetchApp.fetch(repoUrl);
  var repoData = JSON.parse(repoResponse.getContentText());

  // Loop through each file in the repository
  for (var i = 0; i < repoData.length; i++) {
    var file = repoData[i];

    // Check if the file is a .tsv file
    if (file.name.endsWith('.tsv')) {
      console.log('Reading file: ' + file.name)

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
      console.log('Not enough fields in line: j=' + j + ' data:' + tsvRow)
      continue
    }

    // Parse the columns
    var timestamp = tsvRow[0];
    var label = tsvRow[2];

    // Set default entry type, note, synclabel for computing speed difference,
    // and default match result
    var entryType = '';
    var note = '';
    var synclabel = '';
    var match = '';
    var speedDiff = '';

    // Log data
    console.log('Processing line ' + j + ' ts ' + timestamp + ' entry ' + label)
    if (isNotFloat(timestamp)) {
      console.log('Problem line: j=' + j + ' data:' + tsvRow);
      continue;
    }

    if (label == '') {
      var rowData = ['', '', '', '', '', '', '', '', '', ''];
      data.push(rowData);
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
      wavFilename = match[2];
      masterOffset = parseFloat(match[3]);
      note = wavFilename + " " + masterOffset
    }
    // Detect track and original sync labels
    else if (match = /track\s+sync:\s+(.)(.*)/.exec(label)) {
      synclabel = 'track' + match[1];
      if (!(trackNum in syncPoints)) { syncPoints[trackNum] = {}; }
      syncPoints[trackNum][synclabel] = parseFloat(timestamp);
      entryType = 'Track Sync'
      note = match[1] + match[2]
    }
    else if (match = /orig(\d+)\s+sync:\s+(.)(.*)/.exec(label)) {
      synclabel = 'orig' + match[2];
      if (!(match[1] in syncPoints)) { syncPoints[match[1]] = {}; }
      syncPoints[match[1]][synclabel] = parseFloat(timestamp);
      entryType = 'Orig Sync'
      note = match[1] + " " + match[2] + match[3]
    }
    else if (match = /orig(\d+)\s+start:\s+(.*)/.exec(label)) {
      entryType = 'Orig Start'
      note = match[1] + ": " + match[2]
    }
    else if (match = /orig(\d+)\s+end:\s+(.*)/.exec(label)) {
      entryType = 'Orig End'
      note = match[1] + ": " + match[2]
    }
    else if (match = /file note: (.*)/.exec(label)) {
      entryType = 'File Note';
      note = match[1];
    }
    else if (match = /file (start|end): (.*)/.exec(label)) {
      entryType = 'File ' + match[1].charAt(0).toUpperCase() + match[1].slice(1)
      note = match[2]
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
    if (trackNum in syncPoints) {
      var syncPoint = syncPoints[trackNum];
      console.log('keys in syncPoints[' + trackNum + ']: ' + Object.keys(syncPoint).length + ' : ' + Object.keys(syncPoint))
      if (Object.keys(syncPoint).length == 4) {
        speedDiff = (syncPoint.trackB - syncPoint.trackA) / (syncPoint.origB - syncPoint.origA);
        // Store or log the speed difference as needed
        Logger.log('Track ' + trackNum + ' Speed Difference: ' + speedDiff);
      }
    }

    // Instead of using 'sheet.getRange', you can push data into an array and set the values in one go
    var rowData = [
      masterOffset + parseFloat(timestamp),
      parsefloat(tsvRow[0]),
      parsefloat(tsvRow[1]),
      tsvRow[2],
      wavFilename,
      trackNum,
      entryType,
      note,
      trackName,
      trackArtist,
      speedDiff,
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
GithubImport();

function isNotFloat(value) {
  // Use parseFloat to attempt to convert the value to a floating-point number
  var floatValue = parseFloat(value);

  // Check if the conversion result is NaN (Not-a-Number) and if it's not equal to the original value
  return isNaN(floatValue) || floatValue.toString() !== value.toString();
}
