-- ADAPTED, not upstream. BigQuery DDL for the Employees sample database (datacharmer/test_db, employees.sql at commit
-- e324b56193ca506ab7cc1ab143a9153d8c4535d7). Original data by Fusheng Wang and Carlo Zaniolo, current schema by
-- Giuseppe Maxia, data conversion by Patrick Crews. Copyright (C) 2007, 2008 MySQL AB. This adaptation is shared under
-- the same licence as the work it adapts: Creative Commons Attribution-Share Alike 3.0 Unported
-- (https://creativecommons.org/licenses/by-sa/3.0/). The upstream scripts and the data are NOT in this repository:
-- tools/sample_db_employees.py downloads them at run time from the pinned commit and checks their SHA-256.
--
-- Adaptation:
-- * MySQL types become BigQuery types: INT -> INT64; VARCHAR, CHAR and ENUM ('M','F') (loaded as its text) -> STRING;
--   DATE -> DATE.
-- * Primary and foreign keys are declared NOT ENFORCED, as BigQuery declares them. ON DELETE CASCADE, the UNIQUE key on
--   departments.dept_name, the engine and the character set are dropped (BigQuery has no such clauses; the UNIQUE key is
--   not declared to the provers).
-- * The views of the upstream scripts (dept_emp_latest_date, current_dept_emp, v_full_employees, v_full_departments) are
--   workload queries (workload.json), not tables.
-- Tables, columns, column order, NOT NULL, primary keys and foreign keys are the upstream ones;
-- tools/sample_db_bench.py checks that against the downloaded upstream script on every run.
-- dept_emp, titles and salaries have DATE columns in their composite keys (dept_emp: emp_no, dept_no; titles:
-- emp_no, title, from_date; salaries: emp_no, from_date).

CREATE TABLE employees (
  emp_no INT64 NOT NULL,
  birth_date DATE NOT NULL,
  first_name STRING NOT NULL,
  last_name STRING NOT NULL,
  gender STRING NOT NULL,
  hire_date DATE NOT NULL,
  PRIMARY KEY (emp_no) NOT ENFORCED
);

CREATE TABLE departments (
  dept_no STRING NOT NULL,
  dept_name STRING NOT NULL,
  PRIMARY KEY (dept_no) NOT ENFORCED
);

CREATE TABLE dept_manager (
  emp_no INT64 NOT NULL,
  dept_no STRING NOT NULL,
  from_date DATE NOT NULL,
  to_date DATE NOT NULL,
  PRIMARY KEY (emp_no, dept_no) NOT ENFORCED,
  FOREIGN KEY (emp_no) REFERENCES employees (emp_no) NOT ENFORCED,
  FOREIGN KEY (dept_no) REFERENCES departments (dept_no) NOT ENFORCED
);

CREATE TABLE dept_emp (
  emp_no INT64 NOT NULL,
  dept_no STRING NOT NULL,
  from_date DATE NOT NULL,
  to_date DATE NOT NULL,
  PRIMARY KEY (emp_no, dept_no) NOT ENFORCED,
  FOREIGN KEY (emp_no) REFERENCES employees (emp_no) NOT ENFORCED,
  FOREIGN KEY (dept_no) REFERENCES departments (dept_no) NOT ENFORCED
);

CREATE TABLE titles (
  emp_no INT64 NOT NULL,
  title STRING NOT NULL,
  from_date DATE NOT NULL,
  to_date DATE,
  PRIMARY KEY (emp_no, title, from_date) NOT ENFORCED,
  FOREIGN KEY (emp_no) REFERENCES employees (emp_no) NOT ENFORCED
);

CREATE TABLE salaries (
  emp_no INT64 NOT NULL,
  salary INT64 NOT NULL,
  from_date DATE NOT NULL,
  to_date DATE NOT NULL,
  PRIMARY KEY (emp_no, from_date) NOT ENFORCED,
  FOREIGN KEY (emp_no) REFERENCES employees (emp_no) NOT ENFORCED
);
