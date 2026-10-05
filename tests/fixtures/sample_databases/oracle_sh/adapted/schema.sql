-- ADAPTED, not upstream. BigQuery DDL for the Oracle Sales History (SH) sample schema
-- (oracle-samples/db-sample-schemas, sales_history/sh_create.sql at commit
-- 6660bad68c07bd143430ace58565b3f727e17263; the unchanged Oracle scripts are in ../upstream/,
-- licence in ../upstream/LICENSE.txt; the six CSV files that hold most of the rows are downloaded
-- at run time from the same commit and are not in this repository).
--
-- Adaptation:
-- * Oracle types become BigQuery types: NUMBER and NUMBER(p) -> INT64 (every value in the data is a
--   whole number; the load checks it); NUMBER(p, s) -> NUMERIC(p, s); VARCHAR2(n) and CHAR(n) -> STRING;
--   DATE -> DATE (the upstream values carry no time of day).
-- * Primary and foreign keys are declared NOT ENFORCED, as BigQuery declares them. The two fact tables,
--   sales and costs, have no primary key upstream either (sales' own comment: rows are identified by the
--   combination of all foreign keys), and none is invented here.
-- * Dropped, because BigQuery has no such objects: range partitioning of sales and costs (a partition is
--   a storage choice, not a query-visible one), COMPRESS, bitmap and text indexes, dimensions
--   (CREATE DIMENSION), materialized views (their defining queries are workload queries), comments,
--   statistics, and WITH READ ONLY.
-- Tables, columns, column order, NOT NULL, primary keys and foreign keys are the upstream ones;
-- tools/sample_db_bench.py checks that against the upstream script on every run.

CREATE TABLE countries (
  country_id INT64 NOT NULL,
  country_iso_code STRING NOT NULL,
  country_name STRING NOT NULL,
  country_subregion STRING NOT NULL,
  country_subregion_id INT64 NOT NULL,
  country_region STRING NOT NULL,
  country_region_id INT64 NOT NULL,
  country_total STRING NOT NULL,
  country_total_id INT64 NOT NULL,
  PRIMARY KEY (country_id) NOT ENFORCED
);

CREATE TABLE customers (
  cust_id INT64 NOT NULL,
  cust_first_name STRING NOT NULL,
  cust_last_name STRING NOT NULL,
  cust_gender STRING NOT NULL,
  cust_year_of_birth INT64 NOT NULL,
  cust_marital_status STRING,
  cust_street_address STRING NOT NULL,
  cust_postal_code STRING NOT NULL,
  cust_city STRING NOT NULL,
  cust_city_id INT64 NOT NULL,
  cust_state_province STRING NOT NULL,
  cust_state_province_id INT64 NOT NULL,
  country_id INT64 NOT NULL,
  cust_main_phone_number STRING NOT NULL,
  cust_income_level STRING,
  cust_credit_limit INT64,
  cust_email STRING,
  cust_total STRING NOT NULL,
  cust_total_id INT64 NOT NULL,
  cust_src_id INT64,
  cust_eff_from DATE,
  cust_eff_to DATE,
  cust_valid STRING,
  PRIMARY KEY (cust_id) NOT ENFORCED,
  FOREIGN KEY (country_id) REFERENCES countries(country_id) NOT ENFORCED
);

CREATE TABLE promotions (
  promo_id INT64 NOT NULL,
  promo_name STRING NOT NULL,
  promo_subcategory STRING NOT NULL,
  promo_subcategory_id INT64 NOT NULL,
  promo_category STRING NOT NULL,
  promo_category_id INT64 NOT NULL,
  promo_cost NUMERIC(10, 2) NOT NULL,
  promo_begin_date DATE NOT NULL,
  promo_end_date DATE NOT NULL,
  promo_total STRING NOT NULL,
  promo_total_id INT64 NOT NULL,
  PRIMARY KEY (promo_id) NOT ENFORCED
);

CREATE TABLE products (
  prod_id INT64 NOT NULL,
  prod_name STRING NOT NULL,
  prod_desc STRING NOT NULL,
  prod_subcategory STRING NOT NULL,
  prod_subcategory_id INT64 NOT NULL,
  prod_subcategory_desc STRING NOT NULL,
  prod_category STRING NOT NULL,
  prod_category_id INT64 NOT NULL,
  prod_category_desc STRING NOT NULL,
  prod_weight_class INT64 NOT NULL,
  prod_unit_of_measure STRING,
  prod_pack_size STRING NOT NULL,
  supplier_id INT64 NOT NULL,
  prod_status STRING NOT NULL,
  prod_list_price NUMERIC(8, 2) NOT NULL,
  prod_min_price NUMERIC(8, 2) NOT NULL,
  prod_total STRING NOT NULL,
  prod_total_id INT64 NOT NULL,
  prod_src_id INT64,
  prod_eff_from DATE,
  prod_eff_to DATE,
  prod_valid STRING,
  PRIMARY KEY (prod_id) NOT ENFORCED
);

CREATE TABLE times (
  time_id DATE NOT NULL,
  day_name STRING NOT NULL,
  day_number_in_week INT64 NOT NULL,
  day_number_in_month INT64 NOT NULL,
  calendar_week_number INT64 NOT NULL,
  fiscal_week_number INT64 NOT NULL,
  week_ending_day DATE NOT NULL,
  week_ending_day_id INT64 NOT NULL,
  calendar_month_number INT64 NOT NULL,
  fiscal_month_number INT64 NOT NULL,
  calendar_month_desc STRING NOT NULL,
  calendar_month_id INT64 NOT NULL,
  fiscal_month_desc STRING NOT NULL,
  fiscal_month_id INT64 NOT NULL,
  days_in_cal_month INT64 NOT NULL,
  days_in_fis_month INT64 NOT NULL,
  end_of_cal_month DATE NOT NULL,
  end_of_fis_month DATE NOT NULL,
  calendar_month_name STRING NOT NULL,
  fiscal_month_name STRING NOT NULL,
  calendar_quarter_desc STRING NOT NULL,
  calendar_quarter_id INT64 NOT NULL,
  fiscal_quarter_desc STRING NOT NULL,
  fiscal_quarter_id INT64 NOT NULL,
  days_in_cal_quarter INT64 NOT NULL,
  days_in_fis_quarter INT64 NOT NULL,
  end_of_cal_quarter DATE NOT NULL,
  end_of_fis_quarter DATE NOT NULL,
  calendar_quarter_number INT64 NOT NULL,
  fiscal_quarter_number INT64 NOT NULL,
  calendar_year INT64 NOT NULL,
  calendar_year_id INT64 NOT NULL,
  fiscal_year INT64 NOT NULL,
  fiscal_year_id INT64 NOT NULL,
  days_in_cal_year INT64 NOT NULL,
  days_in_fis_year INT64 NOT NULL,
  end_of_cal_year DATE NOT NULL,
  end_of_fis_year DATE NOT NULL,
  PRIMARY KEY (time_id) NOT ENFORCED
);

CREATE TABLE channels (
  channel_id INT64 NOT NULL,
  channel_desc STRING NOT NULL,
  channel_class STRING NOT NULL,
  channel_class_id INT64 NOT NULL,
  channel_total STRING NOT NULL,
  channel_total_id INT64 NOT NULL,
  PRIMARY KEY (channel_id) NOT ENFORCED
);

CREATE TABLE sales (
  prod_id INT64 NOT NULL,
  cust_id INT64 NOT NULL,
  time_id DATE NOT NULL,
  channel_id INT64 NOT NULL,
  promo_id INT64 NOT NULL,
  quantity_sold INT64 NOT NULL,
  amount_sold NUMERIC(10, 2) NOT NULL,
  FOREIGN KEY (promo_id) REFERENCES promotions(promo_id) NOT ENFORCED,
  FOREIGN KEY (cust_id) REFERENCES customers(cust_id) NOT ENFORCED,
  FOREIGN KEY (prod_id) REFERENCES products(prod_id) NOT ENFORCED,
  FOREIGN KEY (channel_id) REFERENCES channels(channel_id) NOT ENFORCED,
  FOREIGN KEY (time_id) REFERENCES times(time_id) NOT ENFORCED
);

CREATE TABLE costs (
  prod_id INT64 NOT NULL,
  time_id DATE NOT NULL,
  promo_id INT64 NOT NULL,
  channel_id INT64 NOT NULL,
  unit_cost NUMERIC(10, 2) NOT NULL,
  unit_price NUMERIC(10, 2) NOT NULL,
  FOREIGN KEY (promo_id) REFERENCES promotions(promo_id) NOT ENFORCED,
  FOREIGN KEY (prod_id) REFERENCES products(prod_id) NOT ENFORCED,
  FOREIGN KEY (time_id) REFERENCES times(time_id) NOT ENFORCED,
  FOREIGN KEY (channel_id) REFERENCES channels(channel_id) NOT ENFORCED
);

CREATE TABLE supplementary_demographics (
  cust_id INT64 NOT NULL,
  education STRING,
  occupation STRING,
  household_size STRING,
  yrs_residence INT64,
  affinity_card INT64,
  cricket INT64,
  baseball INT64,
  tennis INT64,
  soccer INT64,
  golf INT64,
  unknown INT64,
  misc INT64,
  comments STRING,
  PRIMARY KEY (cust_id) NOT ENFORCED
);
