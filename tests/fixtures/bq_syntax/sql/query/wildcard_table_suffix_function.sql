SELECT _TABLE_SUFFIX AS sfx, COUNT(*) AS n FROM `bigquery-public-data.noaa_gsod.gsod19*` WHERE _TABLE_SUFFIX IN ('40', '41') GROUP BY sfx
