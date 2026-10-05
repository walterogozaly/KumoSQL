-- ADAPTED, not upstream. BigQuery DDL for the Northwind database (microsoft/sql-server-samples,
-- samples/databases/northwind-pubs/instnwnd.sql at commit beaab06ef72831089ca80e5355d65e661fd19b26;
-- the unchanged upstream T-SQL script is ../upstream/instnwnd.sql, MIT licence in ../upstream/license.txt).
--
-- Adaptation:
-- * T-SQL types become BigQuery types: int, smallint -> INT64; nchar(n), nvarchar(n), ntext -> STRING;
--   money -> NUMERIC(19, 4) (money keeps four decimal places); real -> FLOAT64 (BigQuery has no
--   32-bit float); datetime -> DATETIME; image -> BYTES; bit -> INT64 holding 0 or 1 (the upstream
--   views compare Discontinued with 0 and 1).
-- * nchar values are loaded without their trailing padding (SQL Server ignores trailing spaces when
--   comparing strings; BigQuery does not).
-- * Primary and foreign keys are declared NOT ENFORCED, as BigQuery declares them. CHECK constraints,
--   DEFAULTs, IDENTITY, CLUSTERED/NONCLUSTERED, ON [PRIMARY] and indexes are dropped (BigQuery has no
--   such clauses); the keys added later by ALTER TABLE in the upstream script are declared here.
-- Tables, columns, column order, NOT NULL, primary keys and foreign keys are the upstream ones;
-- tools/sample_db_bench.py checks that against the upstream script on every run.

CREATE TABLE Employees (
  EmployeeID INT64 NOT NULL,
  LastName STRING NOT NULL,
  FirstName STRING NOT NULL,
  Title STRING,
  TitleOfCourtesy STRING,
  BirthDate DATETIME,
  HireDate DATETIME,
  Address STRING,
  City STRING,
  Region STRING,
  PostalCode STRING,
  Country STRING,
  HomePhone STRING,
  Extension STRING,
  Photo BYTES,
  Notes STRING,
  ReportsTo INT64,
  PhotoPath STRING,
  PRIMARY KEY (EmployeeID) NOT ENFORCED,
  FOREIGN KEY (ReportsTo) REFERENCES Employees(EmployeeID) NOT ENFORCED
);

CREATE TABLE Categories (
  CategoryID INT64 NOT NULL,
  CategoryName STRING NOT NULL,
  Description STRING,
  Picture BYTES,
  PRIMARY KEY (CategoryID) NOT ENFORCED
);

CREATE TABLE Customers (
  CustomerID STRING NOT NULL,
  CompanyName STRING NOT NULL,
  ContactName STRING,
  ContactTitle STRING,
  Address STRING,
  City STRING,
  Region STRING,
  PostalCode STRING,
  Country STRING,
  Phone STRING,
  Fax STRING,
  PRIMARY KEY (CustomerID) NOT ENFORCED
);

CREATE TABLE Shippers (
  ShipperID INT64 NOT NULL,
  CompanyName STRING NOT NULL,
  Phone STRING,
  PRIMARY KEY (ShipperID) NOT ENFORCED
);

CREATE TABLE Suppliers (
  SupplierID INT64 NOT NULL,
  CompanyName STRING NOT NULL,
  ContactName STRING,
  ContactTitle STRING,
  Address STRING,
  City STRING,
  Region STRING,
  PostalCode STRING,
  Country STRING,
  Phone STRING,
  Fax STRING,
  HomePage STRING,
  PRIMARY KEY (SupplierID) NOT ENFORCED
);

CREATE TABLE Orders (
  OrderID INT64 NOT NULL,
  CustomerID STRING,
  EmployeeID INT64,
  OrderDate DATETIME,
  RequiredDate DATETIME,
  ShippedDate DATETIME,
  ShipVia INT64,
  Freight NUMERIC(19, 4),
  ShipName STRING,
  ShipAddress STRING,
  ShipCity STRING,
  ShipRegion STRING,
  ShipPostalCode STRING,
  ShipCountry STRING,
  PRIMARY KEY (OrderID) NOT ENFORCED,
  FOREIGN KEY (CustomerID) REFERENCES Customers(CustomerID) NOT ENFORCED,
  FOREIGN KEY (EmployeeID) REFERENCES Employees(EmployeeID) NOT ENFORCED,
  FOREIGN KEY (ShipVia) REFERENCES Shippers(ShipperID) NOT ENFORCED
);

CREATE TABLE Products (
  ProductID INT64 NOT NULL,
  ProductName STRING NOT NULL,
  SupplierID INT64,
  CategoryID INT64,
  QuantityPerUnit STRING,
  UnitPrice NUMERIC(19, 4),
  UnitsInStock INT64,
  UnitsOnOrder INT64,
  ReorderLevel INT64,
  Discontinued INT64 NOT NULL,
  PRIMARY KEY (ProductID) NOT ENFORCED,
  FOREIGN KEY (CategoryID) REFERENCES Categories(CategoryID) NOT ENFORCED,
  FOREIGN KEY (SupplierID) REFERENCES Suppliers(SupplierID) NOT ENFORCED
);

CREATE TABLE `Order Details` (
  OrderID INT64 NOT NULL,
  ProductID INT64 NOT NULL,
  UnitPrice NUMERIC(19, 4) NOT NULL,
  Quantity INT64 NOT NULL,
  Discount FLOAT64 NOT NULL,
  PRIMARY KEY (OrderID, ProductID) NOT ENFORCED,
  FOREIGN KEY (OrderID) REFERENCES Orders(OrderID) NOT ENFORCED,
  FOREIGN KEY (ProductID) REFERENCES Products(ProductID) NOT ENFORCED
);

CREATE TABLE CustomerCustomerDemo (
  CustomerID STRING NOT NULL,
  CustomerTypeID STRING NOT NULL,
  PRIMARY KEY (CustomerID, CustomerTypeID) NOT ENFORCED,
  FOREIGN KEY (CustomerTypeID) REFERENCES CustomerDemographics(CustomerTypeID) NOT ENFORCED,
  FOREIGN KEY (CustomerID) REFERENCES Customers(CustomerID) NOT ENFORCED
);

CREATE TABLE CustomerDemographics (
  CustomerTypeID STRING NOT NULL,
  CustomerDesc STRING,
  PRIMARY KEY (CustomerTypeID) NOT ENFORCED
);

CREATE TABLE Region (
  RegionID INT64 NOT NULL,
  RegionDescription STRING NOT NULL,
  PRIMARY KEY (RegionID) NOT ENFORCED
);

CREATE TABLE Territories (
  TerritoryID STRING NOT NULL,
  TerritoryDescription STRING NOT NULL,
  RegionID INT64 NOT NULL,
  PRIMARY KEY (TerritoryID) NOT ENFORCED,
  FOREIGN KEY (RegionID) REFERENCES Region(RegionID) NOT ENFORCED
);

CREATE TABLE EmployeeTerritories (
  EmployeeID INT64 NOT NULL,
  TerritoryID STRING NOT NULL,
  PRIMARY KEY (EmployeeID, TerritoryID) NOT ENFORCED,
  FOREIGN KEY (EmployeeID) REFERENCES Employees(EmployeeID) NOT ENFORCED,
  FOREIGN KEY (TerritoryID) REFERENCES Territories(TerritoryID) NOT ENFORCED
);
