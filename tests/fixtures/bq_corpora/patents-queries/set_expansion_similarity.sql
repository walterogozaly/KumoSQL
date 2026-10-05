-- Adapted from google/patents-public-data: examples/patent_set_expansion.ipynb, cell 14, string 0. str.replace placeholders filled in:
-- Replaced [cluster_center] by [0.1, 0.2, 0.3]
-- Replaced [cluster_label] by 1
-- Replaced [max_distance] by 0.5
-- Replaced [max_results] by 100
-- Replaced [cluster_input_list] by ('US-8000000-B2', 'US-2007186831-A1')
    #standardSQL

    CREATE TEMPORARY FUNCTION cosine_distance(patent ARRAY<FLOAT64>)
    RETURNS FLOAT64
    LANGUAGE js AS """
      var cluster_center = [0.1, 0.2, 0.3];

      var dotproduct = 0;
      var A = 0;
      var B = 0;
      for (i = 0; i < patent.length; i++){
        dotproduct += (patent[i] * cluster_center[i]);
        A += (patent[i]*patent[i]);
        B += (cluster_center[i]*cluster_center[i]);
      }
      A = Math.sqrt(A);
      B = Math.sqrt(B);
      var cosine_distance = 1 - (dotproduct)/(A)*(B);
      return cosine_distance;
    """;

    CREATE TEMPORARY FUNCTION manhattan_distance(patent ARRAY<FLOAT64>)
    RETURNS FLOAT64
    LANGUAGE js AS """
      var cluster_center = [0.1, 0.2, 0.3];
      var mdist = 0;
      for (i = 0; i < patent.length; i++){
        mdist += Math.abs(patent[i] - cluster_center[i]);
      }
      return mdist;
    """;
    

      SELECT DISTINCT
        1 as cluster,
        gpr.publication_number,
        cosine_distance(gpr.embedding_v1) AS cosine_distance
      FROM `patents-public-data.google_patents_research.publications` gpr
      WHERE 
        gpr.country = 'United States' AND
        gpr.publication_number not in ('US-8000000-B2', 'US-2007186831-A1') AND
        cosine_distance(gpr.embedding_v1) < 0.5
      ORDER BY
        cosine_distance
      LIMIT 100
  
