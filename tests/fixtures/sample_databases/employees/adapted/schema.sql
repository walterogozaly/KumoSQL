-- ADAPTED from datacharmer/test_db at e324b56193ca506ab7cc1ab143a9153d8c4535d7.
-- MySQL INT -> BigQuery INT64; VARCHAR, CHAR and ENUM -> STRING. MySQL keys become
-- BigQuery NOT ENFORCED keys. The source's tables and column order are preserved.

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
