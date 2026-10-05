-- Adapted from google/patents-public-data: examples/claim-text/claim_text_extraction.ipynb, cell 18, string 0. Python format fields replaced:
-- Replaced '{}' by the JavaScript string that cell 17 defines first
-- Replaced '{}.{}' by 'my_project_name.claims_analysis'
#standardSQL
CREATE TEMPORARY FUNCTION get_first_claim(text STRING)
RETURNS STRING
LANGUAGE js AS """
// Regex to find a period followed by any number of spaces and '2.' or '2 .'
var pattern = new RegExp(/[.][\\s]+[2][\\s]*[.]/, 'g');
if (pattern.test(text)) {
  return text.split(pattern)[0];
}

// If none of the above worked, try to find a reference to claim 1.
if (text.indexOf('claim 1 ') > -1) {
  return text.split('claim 1 ')[0];
}

// Look for claim cancelations and return first non-canceled claim
text = text.replace(/canceled/i, 'canceled')
text = text.replace(/cancelled/i, 'canceled')
if (text.indexOf('(canceled)') > -1) {
	text = text.split('(canceled)')
	canceled = text[0];
	not_canceled = text[1];
	// Determine first non-cancelled claim
	if (canceled.indexOf('-') > -1 ) {
		next_claim = parseInt(canceled.split('-')[1]) + 1
	}
	else {
		next_claim = 2;
	}
	// Split on next_claim + 1
	return not_canceled.split(next_claim + 1)[0].trim();
}


// If none of the above worked, try to find a sentence starting with 'The'
// This should only happen after claim 1 has been defined.
if (text.indexOf(' The ') > -1) {
  return text.split(' The ')[0];
}

// if none of the above worked return the first 2000 characters. 
return text.slice(0,2000);
"""; 

 
SELECT 
pubs.*, 
get_first_claim(text) first_claim
FROM `my_project_name.claims_analysis.20k_G06F_pubs_after_1994` pubs
