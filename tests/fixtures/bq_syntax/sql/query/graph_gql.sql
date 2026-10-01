SELECT * FROM GRAPH_TABLE(`kumosql.kumosql_messy.some_graph` MATCH (a)-[e]->(b) RETURN a.id AS a_id, b.id AS b_id)
