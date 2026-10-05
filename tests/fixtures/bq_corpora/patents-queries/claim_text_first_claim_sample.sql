-- Adapted from google/patents-public-data: examples/claim-text/claim_text_extraction.ipynb, cell 19, string 0. Python format fields replaced:
-- Replaced '{}.{}.{}' by 'my_project_name.claims_analysis.20k_G06F_pubs_after_1994_split_first_claim'
SELECT * FROM `my_project_name.claims_analysis.20k_G06F_pubs_after_1994_split_first_claim` WHERE RAND() < 500/20000 
