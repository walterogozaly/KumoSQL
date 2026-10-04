-- ADAPTED, not upstream. BigQuery DDL for the Chinook database (lerocha/chinook-database v1.4.5,
-- ChinookDatabase/DataSources/Chinook_Sqlite.sql at commit 7f67772503d71ba90f19283c38e93923addb43fa;
-- the unchanged upstream script is ../upstream/Chinook_Sqlite.sql, MIT-style licence in ../upstream/LICENSE.md).
--
-- Adaptation: SQLite types become BigQuery types (INTEGER -> INT64, NVARCHAR(n) -> STRING,
-- NUMERIC(10,2) -> NUMERIC(10, 2), DATETIME -> DATETIME); primary and foreign keys are declared
-- NOT ENFORCED, as BigQuery declares them; indexes and ON DELETE/ON UPDATE actions are dropped.
-- Tables, columns, column order, NOT NULL, primary keys and foreign keys are the upstream ones;
-- tools/sample_db_bench.py checks that against the upstream script on every run.

CREATE TABLE Album (
  AlbumId INT64 NOT NULL,
  Title STRING NOT NULL,
  ArtistId INT64 NOT NULL,
  PRIMARY KEY (AlbumId) NOT ENFORCED,
  FOREIGN KEY (ArtistId) REFERENCES Artist(ArtistId) NOT ENFORCED
);

CREATE TABLE Artist (
  ArtistId INT64 NOT NULL,
  Name STRING,
  PRIMARY KEY (ArtistId) NOT ENFORCED
);

CREATE TABLE Customer (
  CustomerId INT64 NOT NULL,
  FirstName STRING NOT NULL,
  LastName STRING NOT NULL,
  Company STRING,
  Address STRING,
  City STRING,
  State STRING,
  Country STRING,
  PostalCode STRING,
  Phone STRING,
  Fax STRING,
  Email STRING NOT NULL,
  SupportRepId INT64,
  PRIMARY KEY (CustomerId) NOT ENFORCED,
  FOREIGN KEY (SupportRepId) REFERENCES Employee(EmployeeId) NOT ENFORCED
);

CREATE TABLE Employee (
  EmployeeId INT64 NOT NULL,
  LastName STRING NOT NULL,
  FirstName STRING NOT NULL,
  Title STRING,
  ReportsTo INT64,
  BirthDate DATETIME,
  HireDate DATETIME,
  Address STRING,
  City STRING,
  State STRING,
  Country STRING,
  PostalCode STRING,
  Phone STRING,
  Fax STRING,
  Email STRING,
  PRIMARY KEY (EmployeeId) NOT ENFORCED,
  FOREIGN KEY (ReportsTo) REFERENCES Employee(EmployeeId) NOT ENFORCED
);

CREATE TABLE Genre (
  GenreId INT64 NOT NULL,
  Name STRING,
  PRIMARY KEY (GenreId) NOT ENFORCED
);

CREATE TABLE Invoice (
  InvoiceId INT64 NOT NULL,
  CustomerId INT64 NOT NULL,
  InvoiceDate DATETIME NOT NULL,
  BillingAddress STRING,
  BillingCity STRING,
  BillingState STRING,
  BillingCountry STRING,
  BillingPostalCode STRING,
  Total NUMERIC(10, 2) NOT NULL,
  PRIMARY KEY (InvoiceId) NOT ENFORCED,
  FOREIGN KEY (CustomerId) REFERENCES Customer(CustomerId) NOT ENFORCED
);

CREATE TABLE InvoiceLine (
  InvoiceLineId INT64 NOT NULL,
  InvoiceId INT64 NOT NULL,
  TrackId INT64 NOT NULL,
  UnitPrice NUMERIC(10, 2) NOT NULL,
  Quantity INT64 NOT NULL,
  PRIMARY KEY (InvoiceLineId) NOT ENFORCED,
  FOREIGN KEY (InvoiceId) REFERENCES Invoice(InvoiceId) NOT ENFORCED,
  FOREIGN KEY (TrackId) REFERENCES Track(TrackId) NOT ENFORCED
);

CREATE TABLE MediaType (
  MediaTypeId INT64 NOT NULL,
  Name STRING,
  PRIMARY KEY (MediaTypeId) NOT ENFORCED
);

CREATE TABLE Playlist (
  PlaylistId INT64 NOT NULL,
  Name STRING,
  PRIMARY KEY (PlaylistId) NOT ENFORCED
);

CREATE TABLE PlaylistTrack (
  PlaylistId INT64 NOT NULL,
  TrackId INT64 NOT NULL,
  PRIMARY KEY (PlaylistId, TrackId) NOT ENFORCED,
  FOREIGN KEY (PlaylistId) REFERENCES Playlist(PlaylistId) NOT ENFORCED,
  FOREIGN KEY (TrackId) REFERENCES Track(TrackId) NOT ENFORCED
);

CREATE TABLE Track (
  TrackId INT64 NOT NULL,
  Name STRING NOT NULL,
  AlbumId INT64,
  MediaTypeId INT64 NOT NULL,
  GenreId INT64,
  Composer STRING,
  Milliseconds INT64 NOT NULL,
  Bytes INT64,
  UnitPrice NUMERIC(10, 2) NOT NULL,
  PRIMARY KEY (TrackId) NOT ENFORCED,
  FOREIGN KEY (AlbumId) REFERENCES Album(AlbumId) NOT ENFORCED,
  FOREIGN KEY (GenreId) REFERENCES Genre(GenreId) NOT ENFORCED,
  FOREIGN KEY (MediaTypeId) REFERENCES MediaType(MediaTypeId) NOT ENFORCED
);
