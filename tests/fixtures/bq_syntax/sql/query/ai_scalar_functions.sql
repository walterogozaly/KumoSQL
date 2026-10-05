SELECT AI.GENERATE_BOOL('is the sky blue?', connection_id => 'kumosql.us.conn') AS b, AI.IF(('is it ok?', 'x'), connection_id => 'kumosql.us.conn') AS c
