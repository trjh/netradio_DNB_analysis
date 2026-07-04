function WasonEdit(e) {
  var sheet = e.source.getActiveSheet();
  var range = e.range;
  var filename = "";

  console.log('DNB label trigger invoked with ' + sheet.getName());

  // Check if the edited range matches your data range
  if (sheet.getName() == "File Analysis" && range.getColumn() == 4) {
    var data = range.getValue().split(':'); // Assuming data is tab-separated
    console.log('processing data:' + data);
    if (data.length == 2) {
      var typetext = data[0];
      var text = data[1];
      
      // Your criteria to determine "Type"
      var type = determineType(typetext);
      
      // Fill the "Type" and "Remaining Text" columns
      if (filename != "") {
        sheet.getRange(range.getRow(), 5).setValue(filename); // Assuming "Type" column is column D  
      }
      sheet.getRange(range.getRow(), 6).setValue(type); // Assuming "Type" column is column D
      sheet.getRange(range.getRow(), 7).setValue(text); // Assuming "Remaining Text" column is column E
    }
  }
}

function determineType(text) {
  // Implement your criteria to determine the "Type" based on the text
  // For example, you can use if statements or regular expressions
  // and return the determined type.
  // This function will depend on your specific criteria.
  console.log('determineType(' + text + ')');

  // Example criteria:
  if (text.includes("start")) {
    return "Start";
  } else if (text.includes("track sync")) {
    return "Sync - File";
  } else if (text.includes("orig sync")) {
    return "Sync - Orig";
  } else {
    return "Data";
  }
}