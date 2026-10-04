-- ADAPTED, not upstream. BigQuery DDL for the Sakila database (datacharmer/test_db, sakila/sakila-mv-schema.sql
-- and sakila/sakila-mv-data.sql at commit e324b56193ca506ab7cc1ab143a9153d8c4535d7, the "Sakila Spatial 0.9" files
-- of Oracle Corporation, New BSD licence; the unchanged upstream MySQL files are in ../upstream/, the licence
-- is the header of each file).
--
-- Adaptation:
-- * MySQL types become BigQuery types: TINYINT, SMALLINT, MEDIUMINT, INT (all UNSIGNED too) and YEAR -> INT64;
--   BOOLEAN (TINYINT(1) in MySQL) -> INT64 holding 0 or 1, as Northwind's bit; VARCHAR, CHAR, TEXT, and ENUM/SET
--   (loaded as their text) -> STRING; DECIMAL(p, s) -> NUMERIC(p, s); DATETIME and TIMESTAMP -> DATETIME;
--   BLOB and GEOMETRY (MySQL's internal geometry bytes, address.location) -> BYTES.
-- * Primary and foreign keys are declared NOT ENFORCED, as BigQuery declares them. DEFAULT, AUTO_INCREMENT,
--   ON UPDATE, ON DELETE/UPDATE actions, UNIQUE keys (store.manager_staff_id; rental's rental_date, inventory_id,
--   customer_id), indexes, FULLTEXT and SPATIAL keys, the engine and the character set are dropped (BigQuery has no
--   such clauses; UNIQUE keys are not declared to the provers).
-- * address.location is the column the upstream script guards with /*!50705 ... */, which MySQL 5.7.5 and later
--   execute: it is loaded.
-- * film_text has no INSERT in the upstream data: the trigger ins_film of the schema script fills it from film, and
--   the harness does the same (one row per film).
-- Tables, columns, column order, NOT NULL, primary keys and foreign keys are the upstream ones;
-- tools/sample_db_bench.py checks that against the upstream script on every run.

CREATE TABLE actor (
  actor_id INT64 NOT NULL,
  first_name STRING NOT NULL,
  last_name STRING NOT NULL,
  last_update DATETIME NOT NULL,
  PRIMARY KEY (actor_id) NOT ENFORCED
);

CREATE TABLE address (
  address_id INT64 NOT NULL,
  address STRING NOT NULL,
  address2 STRING,
  district STRING NOT NULL,
  city_id INT64 NOT NULL,
  postal_code STRING,
  phone STRING NOT NULL,
  location BYTES NOT NULL,
  last_update DATETIME NOT NULL,
  PRIMARY KEY (address_id) NOT ENFORCED,
  FOREIGN KEY (city_id) REFERENCES city(city_id) NOT ENFORCED
);

CREATE TABLE category (
  category_id INT64 NOT NULL,
  name STRING NOT NULL,
  last_update DATETIME NOT NULL,
  PRIMARY KEY (category_id) NOT ENFORCED
);

CREATE TABLE city (
  city_id INT64 NOT NULL,
  city STRING NOT NULL,
  country_id INT64 NOT NULL,
  last_update DATETIME NOT NULL,
  PRIMARY KEY (city_id) NOT ENFORCED,
  FOREIGN KEY (country_id) REFERENCES country(country_id) NOT ENFORCED
);

CREATE TABLE country (
  country_id INT64 NOT NULL,
  country STRING NOT NULL,
  last_update DATETIME NOT NULL,
  PRIMARY KEY (country_id) NOT ENFORCED
);

CREATE TABLE customer (
  customer_id INT64 NOT NULL,
  store_id INT64 NOT NULL,
  first_name STRING NOT NULL,
  last_name STRING NOT NULL,
  email STRING,
  address_id INT64 NOT NULL,
  active INT64 NOT NULL,
  create_date DATETIME NOT NULL,
  last_update DATETIME,
  PRIMARY KEY (customer_id) NOT ENFORCED,
  FOREIGN KEY (address_id) REFERENCES address(address_id) NOT ENFORCED,
  FOREIGN KEY (store_id) REFERENCES store(store_id) NOT ENFORCED
);

CREATE TABLE film (
  film_id INT64 NOT NULL,
  title STRING NOT NULL,
  description STRING,
  release_year INT64,
  language_id INT64 NOT NULL,
  original_language_id INT64,
  rental_duration INT64 NOT NULL,
  rental_rate NUMERIC(4, 2) NOT NULL,
  length INT64,
  replacement_cost NUMERIC(5, 2) NOT NULL,
  rating STRING,
  special_features STRING,
  last_update DATETIME NOT NULL,
  PRIMARY KEY (film_id) NOT ENFORCED,
  FOREIGN KEY (language_id) REFERENCES language(language_id) NOT ENFORCED,
  FOREIGN KEY (original_language_id) REFERENCES language(language_id) NOT ENFORCED
);

CREATE TABLE film_actor (
  actor_id INT64 NOT NULL,
  film_id INT64 NOT NULL,
  last_update DATETIME NOT NULL,
  PRIMARY KEY (actor_id, film_id) NOT ENFORCED,
  FOREIGN KEY (actor_id) REFERENCES actor(actor_id) NOT ENFORCED,
  FOREIGN KEY (film_id) REFERENCES film(film_id) NOT ENFORCED
);

CREATE TABLE film_category (
  film_id INT64 NOT NULL,
  category_id INT64 NOT NULL,
  last_update DATETIME NOT NULL,
  PRIMARY KEY (film_id, category_id) NOT ENFORCED,
  FOREIGN KEY (film_id) REFERENCES film(film_id) NOT ENFORCED,
  FOREIGN KEY (category_id) REFERENCES category(category_id) NOT ENFORCED
);

CREATE TABLE film_text (
  film_id INT64 NOT NULL,
  title STRING NOT NULL,
  description STRING,
  PRIMARY KEY (film_id) NOT ENFORCED
);

CREATE TABLE inventory (
  inventory_id INT64 NOT NULL,
  film_id INT64 NOT NULL,
  store_id INT64 NOT NULL,
  last_update DATETIME NOT NULL,
  PRIMARY KEY (inventory_id) NOT ENFORCED,
  FOREIGN KEY (store_id) REFERENCES store(store_id) NOT ENFORCED,
  FOREIGN KEY (film_id) REFERENCES film(film_id) NOT ENFORCED
);

CREATE TABLE language (
  language_id INT64 NOT NULL,
  name STRING NOT NULL,
  last_update DATETIME NOT NULL,
  PRIMARY KEY (language_id) NOT ENFORCED
);

CREATE TABLE payment (
  payment_id INT64 NOT NULL,
  customer_id INT64 NOT NULL,
  staff_id INT64 NOT NULL,
  rental_id INT64,
  amount NUMERIC(5, 2) NOT NULL,
  payment_date DATETIME NOT NULL,
  last_update DATETIME,
  PRIMARY KEY (payment_id) NOT ENFORCED,
  FOREIGN KEY (rental_id) REFERENCES rental(rental_id) NOT ENFORCED,
  FOREIGN KEY (customer_id) REFERENCES customer(customer_id) NOT ENFORCED,
  FOREIGN KEY (staff_id) REFERENCES staff(staff_id) NOT ENFORCED
);

CREATE TABLE rental (
  rental_id INT64 NOT NULL,
  rental_date DATETIME NOT NULL,
  inventory_id INT64 NOT NULL,
  customer_id INT64 NOT NULL,
  return_date DATETIME,
  staff_id INT64 NOT NULL,
  last_update DATETIME NOT NULL,
  PRIMARY KEY (rental_id) NOT ENFORCED,
  FOREIGN KEY (staff_id) REFERENCES staff(staff_id) NOT ENFORCED,
  FOREIGN KEY (inventory_id) REFERENCES inventory(inventory_id) NOT ENFORCED,
  FOREIGN KEY (customer_id) REFERENCES customer(customer_id) NOT ENFORCED
);

CREATE TABLE staff (
  staff_id INT64 NOT NULL,
  first_name STRING NOT NULL,
  last_name STRING NOT NULL,
  address_id INT64 NOT NULL,
  picture BYTES,
  email STRING,
  store_id INT64 NOT NULL,
  active INT64 NOT NULL,
  username STRING NOT NULL,
  password STRING,
  last_update DATETIME NOT NULL,
  PRIMARY KEY (staff_id) NOT ENFORCED,
  FOREIGN KEY (store_id) REFERENCES store(store_id) NOT ENFORCED,
  FOREIGN KEY (address_id) REFERENCES address(address_id) NOT ENFORCED
);

CREATE TABLE store (
  store_id INT64 NOT NULL,
  manager_staff_id INT64 NOT NULL,
  address_id INT64 NOT NULL,
  last_update DATETIME NOT NULL,
  PRIMARY KEY (store_id) NOT ENFORCED,
  FOREIGN KEY (manager_staff_id) REFERENCES staff(staff_id) NOT ENFORCED,
  FOREIGN KEY (address_id) REFERENCES address(address_id) NOT ENFORCED
);
