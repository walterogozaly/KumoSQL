-- ADAPTED, not upstream. BigQuery DDL for the Oracle Human Resources (HR) sample schema
-- (oracle-samples/db-sample-schemas, human_resources/hr_create.sql at commit
-- 6660bad68c07bd143430ace58565b3f727e17263; the unchanged Oracle scripts are in ../upstream/,
-- licence in ../upstream/LICENSE.txt).
--
-- Adaptation:
-- * Oracle types become BigQuery types: NUMBER and NUMBER(p) -> INT64; NUMBER(p, s) -> NUMERIC(p, s);
--   VARCHAR2(n) and CHAR(n) -> STRING; DATE -> DATE (the upstream values carry no time of day).
-- * Primary and foreign keys are declared NOT ENFORCED, as BigQuery declares them. The foreign keys that
--   upstream adds by ALTER TABLE ... ADD, some without a column list (REFERENCES departments: the
--   parent's primary key), are declared with their columns.
-- * Dropped, because BigQuery has no such objects: CHECK constraints (employees.salary > 0,
--   job_history.end_date > start_date), the UNIQUE constraint on employees.email, indexes,
--   ORGANIZATION INDEX, sequences, the procedures and triggers of hr_code.sql, comments, and
--   WITH READ ONLY on the view.
-- Tables, columns, column order, NOT NULL, primary keys and foreign keys are the upstream ones;
-- tools/sample_db_bench.py checks that against the upstream script on every run.

CREATE TABLE countries (
  country_id STRING NOT NULL,
  country_name STRING,
  region_id INT64,
  PRIMARY KEY (country_id) NOT ENFORCED,
  FOREIGN KEY (region_id) REFERENCES regions(region_id) NOT ENFORCED
);

CREATE TABLE departments (
  department_id INT64 NOT NULL,
  department_name STRING NOT NULL,
  manager_id INT64,
  location_id INT64,
  PRIMARY KEY (department_id) NOT ENFORCED,
  FOREIGN KEY (location_id) REFERENCES locations(location_id) NOT ENFORCED,
  FOREIGN KEY (manager_id) REFERENCES employees(employee_id) NOT ENFORCED
);

CREATE TABLE employees (
  employee_id INT64 NOT NULL,
  first_name STRING,
  last_name STRING NOT NULL,
  email STRING NOT NULL,
  phone_number STRING,
  hire_date DATE NOT NULL,
  job_id STRING NOT NULL,
  salary NUMERIC(8, 2),
  commission_pct NUMERIC(2, 2),
  manager_id INT64,
  department_id INT64,
  PRIMARY KEY (employee_id) NOT ENFORCED,
  FOREIGN KEY (department_id) REFERENCES departments(department_id) NOT ENFORCED,
  FOREIGN KEY (job_id) REFERENCES jobs(job_id) NOT ENFORCED,
  FOREIGN KEY (manager_id) REFERENCES employees(employee_id) NOT ENFORCED
);

CREATE TABLE job_history (
  employee_id INT64 NOT NULL,
  start_date DATE NOT NULL,
  end_date DATE NOT NULL,
  job_id STRING NOT NULL,
  department_id INT64,
  PRIMARY KEY (employee_id, start_date) NOT ENFORCED,
  FOREIGN KEY (job_id) REFERENCES jobs(job_id) NOT ENFORCED,
  FOREIGN KEY (employee_id) REFERENCES employees(employee_id) NOT ENFORCED,
  FOREIGN KEY (department_id) REFERENCES departments(department_id) NOT ENFORCED
);

CREATE TABLE jobs (
  job_id STRING NOT NULL,
  job_title STRING NOT NULL,
  min_salary INT64,
  max_salary INT64,
  PRIMARY KEY (job_id) NOT ENFORCED
);

CREATE TABLE locations (
  location_id INT64 NOT NULL,
  street_address STRING,
  postal_code STRING,
  city STRING NOT NULL,
  state_province STRING,
  country_id STRING,
  PRIMARY KEY (location_id) NOT ENFORCED,
  FOREIGN KEY (country_id) REFERENCES countries(country_id) NOT ENFORCED
);

CREATE TABLE regions (
  region_id INT64 NOT NULL,
  region_name STRING,
  PRIMARY KEY (region_id) NOT ENFORCED
);
