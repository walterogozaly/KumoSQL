SELECT id FROM `kumosql.kumosql_messy.raw_users` WHERE state = @state AND age > @min_age AND id IN UNNEST(@ids)
