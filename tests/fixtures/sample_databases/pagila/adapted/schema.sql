-- ADAPTED, not upstream. BigQuery DDL for the Pagila database (devrimgunduz/pagila, release v4.1.1,
-- pagila-schema.sql at commit 9baf49c4149e43229f6021e6218d6b2ac8ef4f34; the data comes from pagila-data.sql
-- of the same commit). The unchanged upstream files are in ../upstream/, with the PostgreSQL licence.
--
-- Adaptation: PostgreSQL types become BigQuery types (integer and smallint -> INT64, text -> STRING,
-- boolean -> BOOL, date -> DATE, numeric(p,s) -> NUMERIC(p, s), bytea -> BYTES). timestamp with time
-- zone becomes DATETIME holding the UTC wall-clock time (every upstream value is +00). Types BigQuery has
-- no counterpart for are kept as STRING in the text form the dump writes: the mpaa_rating enum, the year
-- domain (INT64, its 1901..2155 check dropped), uuid, the text[] column special_features
-- ('{Commentaries,"Deleted Scenes"}'), tsvector fulltext and the pgvector embedding ('[1,0,0.5,...]').
-- The partitioned table payment is one table: the 55 monthly partitions payment_p2022_01 ...
-- payment_p2026_07 are read into it (same columns). Upstream declares payment's three foreign keys
-- (customer, rental, staff) on the first six partitions only, so the table declares none here.
-- film.length_hours is a VIRTUAL generated column upstream (round(length / 60.0, 2)); the harness
-- computes it from the upstream expression, because the dump does not store it.
-- Primary and foreign keys are declared NOT ENFORCED, as BigQuery declares them. Dropped: defaults,
-- sequences, the five unique indexes (BigQuery has no UNIQUE constraint) and the other indexes, triggers,
-- ON UPDATE/ON DELETE actions, functions, the materialized view's data, the vector extension.
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
  activebool BOOL NOT NULL,
  create_date DATE NOT NULL,
  last_update DATETIME,
  active INT64,
  uuid STRING NOT NULL,
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
  last_update DATETIME NOT NULL,
  special_features STRING,
  fulltext STRING NOT NULL,
  length_hours NUMERIC(4, 2),
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
  FOREIGN KEY (category_id) REFERENCES category(category_id) NOT ENFORCED,
  FOREIGN KEY (film_id) REFERENCES film(film_id) NOT ENFORCED
);

CREATE TABLE film_embedding (
  film_id INT64 NOT NULL,
  embedding STRING NOT NULL,
  last_update DATETIME NOT NULL,
  PRIMARY KEY (film_id) NOT ENFORCED,
  FOREIGN KEY (film_id) REFERENCES film(film_id) NOT ENFORCED
);

CREATE TABLE inventory (
  inventory_id INT64 NOT NULL,
  film_id INT64 NOT NULL,
  store_id INT64 NOT NULL,
  last_update DATETIME NOT NULL,
  PRIMARY KEY (inventory_id) NOT ENFORCED,
  FOREIGN KEY (film_id) REFERENCES film(film_id) NOT ENFORCED,
  FOREIGN KEY (store_id) REFERENCES store(store_id) NOT ENFORCED
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
  rental_id INT64 NOT NULL,
  amount NUMERIC(5, 2) NOT NULL,
  payment_date DATETIME NOT NULL,
  uuid STRING NOT NULL,
  PRIMARY KEY (payment_date, payment_id) NOT ENFORCED
);

CREATE TABLE rental (
  rental_id INT64 NOT NULL,
  rental_date DATETIME NOT NULL,
  inventory_id INT64 NOT NULL,
  customer_id INT64 NOT NULL,
  return_date DATETIME,
  staff_id INT64 NOT NULL,
  last_update DATETIME NOT NULL,
  uuid STRING NOT NULL,
  PRIMARY KEY (rental_id) NOT ENFORCED,
  FOREIGN KEY (customer_id) REFERENCES customer(customer_id) NOT ENFORCED,
  FOREIGN KEY (inventory_id) REFERENCES inventory(inventory_id) NOT ENFORCED,
  FOREIGN KEY (staff_id) REFERENCES staff(staff_id) NOT ENFORCED
);

CREATE TABLE staff (
  staff_id INT64 NOT NULL,
  first_name STRING NOT NULL,
  last_name STRING NOT NULL,
  address_id INT64 NOT NULL,
  email STRING,
  store_id INT64 NOT NULL,
  active BOOL NOT NULL,
  username STRING NOT NULL,
  password STRING,
  last_update DATETIME NOT NULL,
  picture BYTES,
  PRIMARY KEY (staff_id) NOT ENFORCED,
  FOREIGN KEY (address_id) REFERENCES address(address_id) NOT ENFORCED,
  FOREIGN KEY (store_id) REFERENCES store(store_id) NOT ENFORCED
);

CREATE TABLE store (
  store_id INT64 NOT NULL,
  manager_staff_id INT64 NOT NULL,
  address_id INT64 NOT NULL,
  last_update DATETIME NOT NULL,
  PRIMARY KEY (store_id) NOT ENFORCED,
  FOREIGN KEY (address_id) REFERENCES address(address_id) NOT ENFORCED
);
