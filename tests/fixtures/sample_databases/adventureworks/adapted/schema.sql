-- ADAPTED, not upstream. BigQuery DDL for the AdventureWorks OLTP database (microsoft/sql-server-samples, the
-- release asset AdventureWorks-oltp-install-script.zip of the release "adventureworks"): the DDL is instawdb.sql, the
-- data its 69 CSV files. The unchanged script is in ../upstream/ with the MIT licence; the CSV files are downloaded at
-- run time and checked against ../members.sha256.
--
-- Adaptation: T-SQL types become BigQuery types (int, smallint and tinyint -> INT64; bit and the alias types Flag and
-- NameStyle -> BOOL; money NUMERIC(19, 4), smallmoney NUMERIC(10, 4); decimal(p, s) NUMERIC(p, s); datetime
-- DATETIME; date DATE; nvarchar, nchar, varchar and the alias types Name, Phone, AccountNumber and OrderNumber ->
-- STRING; varbinary(max) -> BYTES). Types BigQuery has no counterpart for are kept as STRING in the text the data
-- files write: uniqueidentifier (a GUID), xml, time (Shift.StartTime), and the CLR types hierarchyid and geography,
-- which the files write as hex (Employee.OrganizationNode, Document.DocumentNode, Address.SpatialLocation).
-- nchar values keep their padding (Document.Revision is 'x    ', Product.ProductLine 'R ').
-- Computed columns are ordinary columns holding the values the data files give: Customer.AccountNumber,
-- Employee.OrganizationLevel, Document.DocumentLevel, SalesOrderHeader.SalesOrderNumber and TotalDue,
-- SalesOrderDetail.LineTotal, PurchaseOrderHeader.TotalDue, PurchaseOrderDetail.LineTotal and StockedQty,
-- WorkOrder.StockedQty. Those upstream computes as ISNULL(expression, constant) are NOT NULL, as SQL Server infers them;
-- the other two (levels of a hierarchyid) are nullable.
-- Schema names (HumanResources, Person, Production, Purchasing, Sales, dbo) are dropped; no two tables share a name.
-- DatabaseLog, a heap the script's DDL trigger fills with the statements the script itself runs, is left out: it
-- holds the install log, not data. ErrorLog (empty after an install) is kept.
-- Primary and foreign keys are declared NOT ENFORCED, as BigQuery declares them. Dropped: IDENTITY, defaults,
-- CHECK constraints, ROWGUIDCOL and UNIQUE constraints and every index (BigQuery has no UNIQUE constraint, so the alternate
-- keys such as Person.rowguid and Product.ProductNumber are not declared keys), the XML schema collections, full-text
-- catalogs, triggers, functions, procedures and extended properties.
-- Tables, columns, column order, NOT NULL, primary keys and foreign keys are the upstream ones;
-- tools/sample_db_bench.py checks that against the upstream script on every run.

CREATE TABLE ErrorLog (
  ErrorLogID INT64 NOT NULL,
  ErrorTime DATETIME NOT NULL,
  UserName STRING NOT NULL,
  ErrorNumber INT64 NOT NULL,
  ErrorSeverity INT64,
  ErrorState INT64,
  ErrorProcedure STRING,
  ErrorLine INT64,
  ErrorMessage STRING NOT NULL,
  PRIMARY KEY (ErrorLogID) NOT ENFORCED
);

CREATE TABLE Address (
  AddressID INT64 NOT NULL,
  AddressLine1 STRING NOT NULL,
  AddressLine2 STRING,
  City STRING NOT NULL,
  StateProvinceID INT64 NOT NULL,
  PostalCode STRING NOT NULL,
  SpatialLocation STRING,
  rowguid STRING NOT NULL,
  ModifiedDate DATETIME NOT NULL,
  PRIMARY KEY (AddressID) NOT ENFORCED,
  FOREIGN KEY (StateProvinceID) REFERENCES StateProvince(StateProvinceID) NOT ENFORCED
);

CREATE TABLE AddressType (
  AddressTypeID INT64 NOT NULL,
  Name STRING NOT NULL,
  rowguid STRING NOT NULL,
  ModifiedDate DATETIME NOT NULL,
  PRIMARY KEY (AddressTypeID) NOT ENFORCED
);

CREATE TABLE AWBuildVersion (
  SystemInformationID INT64 NOT NULL,
  `Database Version` STRING NOT NULL,
  VersionDate DATETIME NOT NULL,
  ModifiedDate DATETIME NOT NULL,
  PRIMARY KEY (SystemInformationID) NOT ENFORCED
);

CREATE TABLE BillOfMaterials (
  BillOfMaterialsID INT64 NOT NULL,
  ProductAssemblyID INT64,
  ComponentID INT64 NOT NULL,
  StartDate DATETIME NOT NULL,
  EndDate DATETIME,
  UnitMeasureCode STRING NOT NULL,
  BOMLevel INT64 NOT NULL,
  PerAssemblyQty NUMERIC(8, 2) NOT NULL,
  ModifiedDate DATETIME NOT NULL,
  PRIMARY KEY (BillOfMaterialsID) NOT ENFORCED,
  FOREIGN KEY (ProductAssemblyID) REFERENCES Product(ProductID) NOT ENFORCED,
  FOREIGN KEY (ComponentID) REFERENCES Product(ProductID) NOT ENFORCED,
  FOREIGN KEY (UnitMeasureCode) REFERENCES UnitMeasure(UnitMeasureCode) NOT ENFORCED
);

CREATE TABLE BusinessEntity (
  BusinessEntityID INT64 NOT NULL,
  rowguid STRING NOT NULL,
  ModifiedDate DATETIME NOT NULL,
  PRIMARY KEY (BusinessEntityID) NOT ENFORCED
);

CREATE TABLE BusinessEntityAddress (
  BusinessEntityID INT64 NOT NULL,
  AddressID INT64 NOT NULL,
  AddressTypeID INT64 NOT NULL,
  rowguid STRING NOT NULL,
  ModifiedDate DATETIME NOT NULL,
  PRIMARY KEY (BusinessEntityID, AddressID, AddressTypeID) NOT ENFORCED,
  FOREIGN KEY (AddressID) REFERENCES Address(AddressID) NOT ENFORCED,
  FOREIGN KEY (AddressTypeID) REFERENCES AddressType(AddressTypeID) NOT ENFORCED,
  FOREIGN KEY (BusinessEntityID) REFERENCES BusinessEntity(BusinessEntityID) NOT ENFORCED
);

CREATE TABLE BusinessEntityContact (
  BusinessEntityID INT64 NOT NULL,
  PersonID INT64 NOT NULL,
  ContactTypeID INT64 NOT NULL,
  rowguid STRING NOT NULL,
  ModifiedDate DATETIME NOT NULL,
  PRIMARY KEY (BusinessEntityID, PersonID, ContactTypeID) NOT ENFORCED,
  FOREIGN KEY (PersonID) REFERENCES Person(BusinessEntityID) NOT ENFORCED,
  FOREIGN KEY (ContactTypeID) REFERENCES ContactType(ContactTypeID) NOT ENFORCED,
  FOREIGN KEY (BusinessEntityID) REFERENCES BusinessEntity(BusinessEntityID) NOT ENFORCED
);

CREATE TABLE ContactType (
  ContactTypeID INT64 NOT NULL,
  Name STRING NOT NULL,
  ModifiedDate DATETIME NOT NULL,
  PRIMARY KEY (ContactTypeID) NOT ENFORCED
);

CREATE TABLE CountryRegionCurrency (
  CountryRegionCode STRING NOT NULL,
  CurrencyCode STRING NOT NULL,
  ModifiedDate DATETIME NOT NULL,
  PRIMARY KEY (CountryRegionCode, CurrencyCode) NOT ENFORCED,
  FOREIGN KEY (CountryRegionCode) REFERENCES CountryRegion(CountryRegionCode) NOT ENFORCED,
  FOREIGN KEY (CurrencyCode) REFERENCES Currency(CurrencyCode) NOT ENFORCED
);

CREATE TABLE CountryRegion (
  CountryRegionCode STRING NOT NULL,
  Name STRING NOT NULL,
  ModifiedDate DATETIME NOT NULL,
  PRIMARY KEY (CountryRegionCode) NOT ENFORCED
);

CREATE TABLE CreditCard (
  CreditCardID INT64 NOT NULL,
  CardType STRING NOT NULL,
  CardNumber STRING NOT NULL,
  ExpMonth INT64 NOT NULL,
  ExpYear INT64 NOT NULL,
  ModifiedDate DATETIME NOT NULL,
  PRIMARY KEY (CreditCardID) NOT ENFORCED
);

CREATE TABLE Culture (
  CultureID STRING NOT NULL,
  Name STRING NOT NULL,
  ModifiedDate DATETIME NOT NULL,
  PRIMARY KEY (CultureID) NOT ENFORCED
);

CREATE TABLE Currency (
  CurrencyCode STRING NOT NULL,
  Name STRING NOT NULL,
  ModifiedDate DATETIME NOT NULL,
  PRIMARY KEY (CurrencyCode) NOT ENFORCED
);

CREATE TABLE CurrencyRate (
  CurrencyRateID INT64 NOT NULL,
  CurrencyRateDate DATETIME NOT NULL,
  FromCurrencyCode STRING NOT NULL,
  ToCurrencyCode STRING NOT NULL,
  AverageRate NUMERIC(19, 4) NOT NULL,
  EndOfDayRate NUMERIC(19, 4) NOT NULL,
  ModifiedDate DATETIME NOT NULL,
  PRIMARY KEY (CurrencyRateID) NOT ENFORCED,
  FOREIGN KEY (FromCurrencyCode) REFERENCES Currency(CurrencyCode) NOT ENFORCED,
  FOREIGN KEY (ToCurrencyCode) REFERENCES Currency(CurrencyCode) NOT ENFORCED
);

CREATE TABLE Customer (
  CustomerID INT64 NOT NULL,
  PersonID INT64,
  StoreID INT64,
  TerritoryID INT64,
  AccountNumber STRING NOT NULL,
  rowguid STRING NOT NULL,
  ModifiedDate DATETIME NOT NULL,
  PRIMARY KEY (CustomerID) NOT ENFORCED,
  FOREIGN KEY (PersonID) REFERENCES Person(BusinessEntityID) NOT ENFORCED,
  FOREIGN KEY (StoreID) REFERENCES Store(BusinessEntityID) NOT ENFORCED,
  FOREIGN KEY (TerritoryID) REFERENCES SalesTerritory(TerritoryID) NOT ENFORCED
);

CREATE TABLE Department (
  DepartmentID INT64 NOT NULL,
  Name STRING NOT NULL,
  GroupName STRING NOT NULL,
  ModifiedDate DATETIME NOT NULL,
  PRIMARY KEY (DepartmentID) NOT ENFORCED
);

CREATE TABLE Document (
  DocumentNode STRING NOT NULL,
  DocumentLevel INT64,
  Title STRING NOT NULL,
  Owner INT64 NOT NULL,
  FolderFlag BOOL NOT NULL,
  FileName STRING NOT NULL,
  FileExtension STRING NOT NULL,
  Revision STRING NOT NULL,
  ChangeNumber INT64 NOT NULL,
  Status INT64 NOT NULL,
  DocumentSummary STRING,
  Document BYTES,
  rowguid STRING NOT NULL,
  ModifiedDate DATETIME NOT NULL,
  PRIMARY KEY (DocumentNode) NOT ENFORCED,
  FOREIGN KEY (Owner) REFERENCES Employee(BusinessEntityID) NOT ENFORCED
);

CREATE TABLE EmailAddress (
  BusinessEntityID INT64 NOT NULL,
  EmailAddressID INT64 NOT NULL,
  EmailAddress STRING,
  rowguid STRING NOT NULL,
  ModifiedDate DATETIME NOT NULL,
  PRIMARY KEY (BusinessEntityID, EmailAddressID) NOT ENFORCED,
  FOREIGN KEY (BusinessEntityID) REFERENCES Person(BusinessEntityID) NOT ENFORCED
);

CREATE TABLE Employee (
  BusinessEntityID INT64 NOT NULL,
  NationalIDNumber STRING NOT NULL,
  LoginID STRING NOT NULL,
  OrganizationNode STRING,
  OrganizationLevel INT64,
  JobTitle STRING NOT NULL,
  BirthDate DATE NOT NULL,
  MaritalStatus STRING NOT NULL,
  Gender STRING NOT NULL,
  HireDate DATE NOT NULL,
  SalariedFlag BOOL NOT NULL,
  VacationHours INT64 NOT NULL,
  SickLeaveHours INT64 NOT NULL,
  CurrentFlag BOOL NOT NULL,
  rowguid STRING NOT NULL,
  ModifiedDate DATETIME NOT NULL,
  PRIMARY KEY (BusinessEntityID) NOT ENFORCED,
  FOREIGN KEY (BusinessEntityID) REFERENCES Person(BusinessEntityID) NOT ENFORCED
);

CREATE TABLE EmployeeDepartmentHistory (
  BusinessEntityID INT64 NOT NULL,
  DepartmentID INT64 NOT NULL,
  ShiftID INT64 NOT NULL,
  StartDate DATE NOT NULL,
  EndDate DATE,
  ModifiedDate DATETIME NOT NULL,
  PRIMARY KEY (BusinessEntityID, StartDate, DepartmentID, ShiftID) NOT ENFORCED,
  FOREIGN KEY (DepartmentID) REFERENCES Department(DepartmentID) NOT ENFORCED,
  FOREIGN KEY (BusinessEntityID) REFERENCES Employee(BusinessEntityID) NOT ENFORCED,
  FOREIGN KEY (ShiftID) REFERENCES Shift(ShiftID) NOT ENFORCED
);

CREATE TABLE EmployeePayHistory (
  BusinessEntityID INT64 NOT NULL,
  RateChangeDate DATETIME NOT NULL,
  Rate NUMERIC(19, 4) NOT NULL,
  PayFrequency INT64 NOT NULL,
  ModifiedDate DATETIME NOT NULL,
  PRIMARY KEY (BusinessEntityID, RateChangeDate) NOT ENFORCED,
  FOREIGN KEY (BusinessEntityID) REFERENCES Employee(BusinessEntityID) NOT ENFORCED
);

CREATE TABLE Illustration (
  IllustrationID INT64 NOT NULL,
  Diagram STRING,
  ModifiedDate DATETIME NOT NULL,
  PRIMARY KEY (IllustrationID) NOT ENFORCED
);

CREATE TABLE JobCandidate (
  JobCandidateID INT64 NOT NULL,
  BusinessEntityID INT64,
  Resume STRING,
  ModifiedDate DATETIME NOT NULL,
  PRIMARY KEY (JobCandidateID) NOT ENFORCED,
  FOREIGN KEY (BusinessEntityID) REFERENCES Employee(BusinessEntityID) NOT ENFORCED
);

CREATE TABLE Location (
  LocationID INT64 NOT NULL,
  Name STRING NOT NULL,
  CostRate NUMERIC(10, 4) NOT NULL,
  Availability NUMERIC(8, 2) NOT NULL,
  ModifiedDate DATETIME NOT NULL,
  PRIMARY KEY (LocationID) NOT ENFORCED
);

CREATE TABLE Password (
  BusinessEntityID INT64 NOT NULL,
  PasswordHash STRING NOT NULL,
  PasswordSalt STRING NOT NULL,
  rowguid STRING NOT NULL,
  ModifiedDate DATETIME NOT NULL,
  PRIMARY KEY (BusinessEntityID) NOT ENFORCED,
  FOREIGN KEY (BusinessEntityID) REFERENCES Person(BusinessEntityID) NOT ENFORCED
);

CREATE TABLE Person (
  BusinessEntityID INT64 NOT NULL,
  PersonType STRING NOT NULL,
  NameStyle BOOL NOT NULL,
  Title STRING,
  FirstName STRING NOT NULL,
  MiddleName STRING,
  LastName STRING NOT NULL,
  Suffix STRING,
  EmailPromotion INT64 NOT NULL,
  AdditionalContactInfo STRING,
  Demographics STRING,
  rowguid STRING NOT NULL,
  ModifiedDate DATETIME NOT NULL,
  PRIMARY KEY (BusinessEntityID) NOT ENFORCED,
  FOREIGN KEY (BusinessEntityID) REFERENCES BusinessEntity(BusinessEntityID) NOT ENFORCED
);

CREATE TABLE PersonCreditCard (
  BusinessEntityID INT64 NOT NULL,
  CreditCardID INT64 NOT NULL,
  ModifiedDate DATETIME NOT NULL,
  PRIMARY KEY (BusinessEntityID, CreditCardID) NOT ENFORCED,
  FOREIGN KEY (BusinessEntityID) REFERENCES Person(BusinessEntityID) NOT ENFORCED,
  FOREIGN KEY (CreditCardID) REFERENCES CreditCard(CreditCardID) NOT ENFORCED
);

CREATE TABLE PersonPhone (
  BusinessEntityID INT64 NOT NULL,
  PhoneNumber STRING NOT NULL,
  PhoneNumberTypeID INT64 NOT NULL,
  ModifiedDate DATETIME NOT NULL,
  PRIMARY KEY (BusinessEntityID, PhoneNumber, PhoneNumberTypeID) NOT ENFORCED,
  FOREIGN KEY (BusinessEntityID) REFERENCES Person(BusinessEntityID) NOT ENFORCED,
  FOREIGN KEY (PhoneNumberTypeID) REFERENCES PhoneNumberType(PhoneNumberTypeID) NOT ENFORCED
);

CREATE TABLE PhoneNumberType (
  PhoneNumberTypeID INT64 NOT NULL,
  Name STRING NOT NULL,
  ModifiedDate DATETIME NOT NULL,
  PRIMARY KEY (PhoneNumberTypeID) NOT ENFORCED
);

CREATE TABLE Product (
  ProductID INT64 NOT NULL,
  Name STRING NOT NULL,
  ProductNumber STRING NOT NULL,
  MakeFlag BOOL NOT NULL,
  FinishedGoodsFlag BOOL NOT NULL,
  Color STRING,
  SafetyStockLevel INT64 NOT NULL,
  ReorderPoint INT64 NOT NULL,
  StandardCost NUMERIC(19, 4) NOT NULL,
  ListPrice NUMERIC(19, 4) NOT NULL,
  Size STRING,
  SizeUnitMeasureCode STRING,
  WeightUnitMeasureCode STRING,
  Weight NUMERIC(8, 2),
  DaysToManufacture INT64 NOT NULL,
  ProductLine STRING,
  Class STRING,
  Style STRING,
  ProductSubcategoryID INT64,
  ProductModelID INT64,
  SellStartDate DATETIME NOT NULL,
  SellEndDate DATETIME,
  DiscontinuedDate DATETIME,
  rowguid STRING NOT NULL,
  ModifiedDate DATETIME NOT NULL,
  PRIMARY KEY (ProductID) NOT ENFORCED,
  FOREIGN KEY (SizeUnitMeasureCode) REFERENCES UnitMeasure(UnitMeasureCode) NOT ENFORCED,
  FOREIGN KEY (WeightUnitMeasureCode) REFERENCES UnitMeasure(UnitMeasureCode) NOT ENFORCED,
  FOREIGN KEY (ProductModelID) REFERENCES ProductModel(ProductModelID) NOT ENFORCED,
  FOREIGN KEY (ProductSubcategoryID) REFERENCES ProductSubcategory(ProductSubcategoryID) NOT ENFORCED
);

CREATE TABLE ProductCategory (
  ProductCategoryID INT64 NOT NULL,
  Name STRING NOT NULL,
  rowguid STRING NOT NULL,
  ModifiedDate DATETIME NOT NULL,
  PRIMARY KEY (ProductCategoryID) NOT ENFORCED
);

CREATE TABLE ProductCostHistory (
  ProductID INT64 NOT NULL,
  StartDate DATETIME NOT NULL,
  EndDate DATETIME,
  StandardCost NUMERIC(19, 4) NOT NULL,
  ModifiedDate DATETIME NOT NULL,
  PRIMARY KEY (ProductID, StartDate) NOT ENFORCED,
  FOREIGN KEY (ProductID) REFERENCES Product(ProductID) NOT ENFORCED
);

CREATE TABLE ProductDescription (
  ProductDescriptionID INT64 NOT NULL,
  Description STRING NOT NULL,
  rowguid STRING NOT NULL,
  ModifiedDate DATETIME NOT NULL,
  PRIMARY KEY (ProductDescriptionID) NOT ENFORCED
);

CREATE TABLE ProductDocument (
  ProductID INT64 NOT NULL,
  DocumentNode STRING NOT NULL,
  ModifiedDate DATETIME NOT NULL,
  PRIMARY KEY (ProductID, DocumentNode) NOT ENFORCED,
  FOREIGN KEY (ProductID) REFERENCES Product(ProductID) NOT ENFORCED,
  FOREIGN KEY (DocumentNode) REFERENCES Document(DocumentNode) NOT ENFORCED
);

CREATE TABLE ProductInventory (
  ProductID INT64 NOT NULL,
  LocationID INT64 NOT NULL,
  Shelf STRING NOT NULL,
  Bin INT64 NOT NULL,
  Quantity INT64 NOT NULL,
  rowguid STRING NOT NULL,
  ModifiedDate DATETIME NOT NULL,
  PRIMARY KEY (ProductID, LocationID) NOT ENFORCED,
  FOREIGN KEY (LocationID) REFERENCES Location(LocationID) NOT ENFORCED,
  FOREIGN KEY (ProductID) REFERENCES Product(ProductID) NOT ENFORCED
);

CREATE TABLE ProductListPriceHistory (
  ProductID INT64 NOT NULL,
  StartDate DATETIME NOT NULL,
  EndDate DATETIME,
  ListPrice NUMERIC(19, 4) NOT NULL,
  ModifiedDate DATETIME NOT NULL,
  PRIMARY KEY (ProductID, StartDate) NOT ENFORCED,
  FOREIGN KEY (ProductID) REFERENCES Product(ProductID) NOT ENFORCED
);

CREATE TABLE ProductModel (
  ProductModelID INT64 NOT NULL,
  Name STRING NOT NULL,
  CatalogDescription STRING,
  Instructions STRING,
  rowguid STRING NOT NULL,
  ModifiedDate DATETIME NOT NULL,
  PRIMARY KEY (ProductModelID) NOT ENFORCED
);

CREATE TABLE ProductModelIllustration (
  ProductModelID INT64 NOT NULL,
  IllustrationID INT64 NOT NULL,
  ModifiedDate DATETIME NOT NULL,
  PRIMARY KEY (ProductModelID, IllustrationID) NOT ENFORCED,
  FOREIGN KEY (ProductModelID) REFERENCES ProductModel(ProductModelID) NOT ENFORCED,
  FOREIGN KEY (IllustrationID) REFERENCES Illustration(IllustrationID) NOT ENFORCED
);

CREATE TABLE ProductModelProductDescriptionCulture (
  ProductModelID INT64 NOT NULL,
  ProductDescriptionID INT64 NOT NULL,
  CultureID STRING NOT NULL,
  ModifiedDate DATETIME NOT NULL,
  PRIMARY KEY (ProductModelID, ProductDescriptionID, CultureID) NOT ENFORCED,
  FOREIGN KEY (ProductDescriptionID) REFERENCES ProductDescription(ProductDescriptionID) NOT ENFORCED,
  FOREIGN KEY (CultureID) REFERENCES Culture(CultureID) NOT ENFORCED,
  FOREIGN KEY (ProductModelID) REFERENCES ProductModel(ProductModelID) NOT ENFORCED
);

CREATE TABLE ProductPhoto (
  ProductPhotoID INT64 NOT NULL,
  ThumbNailPhoto BYTES,
  ThumbnailPhotoFileName STRING,
  LargePhoto BYTES,
  LargePhotoFileName STRING,
  ModifiedDate DATETIME NOT NULL,
  PRIMARY KEY (ProductPhotoID) NOT ENFORCED
);

CREATE TABLE ProductProductPhoto (
  ProductID INT64 NOT NULL,
  ProductPhotoID INT64 NOT NULL,
  Primary BOOL NOT NULL,
  ModifiedDate DATETIME NOT NULL,
  PRIMARY KEY (ProductID, ProductPhotoID) NOT ENFORCED,
  FOREIGN KEY (ProductID) REFERENCES Product(ProductID) NOT ENFORCED,
  FOREIGN KEY (ProductPhotoID) REFERENCES ProductPhoto(ProductPhotoID) NOT ENFORCED
);

CREATE TABLE ProductReview (
  ProductReviewID INT64 NOT NULL,
  ProductID INT64 NOT NULL,
  ReviewerName STRING NOT NULL,
  ReviewDate DATETIME NOT NULL,
  EmailAddress STRING NOT NULL,
  Rating INT64 NOT NULL,
  Comments STRING,
  ModifiedDate DATETIME NOT NULL,
  PRIMARY KEY (ProductReviewID) NOT ENFORCED,
  FOREIGN KEY (ProductID) REFERENCES Product(ProductID) NOT ENFORCED
);

CREATE TABLE ProductSubcategory (
  ProductSubcategoryID INT64 NOT NULL,
  ProductCategoryID INT64 NOT NULL,
  Name STRING NOT NULL,
  rowguid STRING NOT NULL,
  ModifiedDate DATETIME NOT NULL,
  PRIMARY KEY (ProductSubcategoryID) NOT ENFORCED,
  FOREIGN KEY (ProductCategoryID) REFERENCES ProductCategory(ProductCategoryID) NOT ENFORCED
);

CREATE TABLE ProductVendor (
  ProductID INT64 NOT NULL,
  BusinessEntityID INT64 NOT NULL,
  AverageLeadTime INT64 NOT NULL,
  StandardPrice NUMERIC(19, 4) NOT NULL,
  LastReceiptCost NUMERIC(19, 4),
  LastReceiptDate DATETIME,
  MinOrderQty INT64 NOT NULL,
  MaxOrderQty INT64 NOT NULL,
  OnOrderQty INT64,
  UnitMeasureCode STRING NOT NULL,
  ModifiedDate DATETIME NOT NULL,
  PRIMARY KEY (ProductID, BusinessEntityID) NOT ENFORCED,
  FOREIGN KEY (ProductID) REFERENCES Product(ProductID) NOT ENFORCED,
  FOREIGN KEY (UnitMeasureCode) REFERENCES UnitMeasure(UnitMeasureCode) NOT ENFORCED,
  FOREIGN KEY (BusinessEntityID) REFERENCES Vendor(BusinessEntityID) NOT ENFORCED
);

CREATE TABLE PurchaseOrderDetail (
  PurchaseOrderID INT64 NOT NULL,
  PurchaseOrderDetailID INT64 NOT NULL,
  DueDate DATETIME NOT NULL,
  OrderQty INT64 NOT NULL,
  ProductID INT64 NOT NULL,
  UnitPrice NUMERIC(19, 4) NOT NULL,
  LineTotal NUMERIC(19, 4) NOT NULL,
  ReceivedQty NUMERIC(8, 2) NOT NULL,
  RejectedQty NUMERIC(8, 2) NOT NULL,
  StockedQty NUMERIC(9, 2) NOT NULL,
  ModifiedDate DATETIME NOT NULL,
  PRIMARY KEY (PurchaseOrderID, PurchaseOrderDetailID) NOT ENFORCED,
  FOREIGN KEY (ProductID) REFERENCES Product(ProductID) NOT ENFORCED,
  FOREIGN KEY (PurchaseOrderID) REFERENCES PurchaseOrderHeader(PurchaseOrderID) NOT ENFORCED
);

CREATE TABLE PurchaseOrderHeader (
  PurchaseOrderID INT64 NOT NULL,
  RevisionNumber INT64 NOT NULL,
  Status INT64 NOT NULL,
  EmployeeID INT64 NOT NULL,
  VendorID INT64 NOT NULL,
  ShipMethodID INT64 NOT NULL,
  OrderDate DATETIME NOT NULL,
  ShipDate DATETIME,
  SubTotal NUMERIC(19, 4) NOT NULL,
  TaxAmt NUMERIC(19, 4) NOT NULL,
  Freight NUMERIC(19, 4) NOT NULL,
  TotalDue NUMERIC(19, 4) NOT NULL,
  ModifiedDate DATETIME NOT NULL,
  PRIMARY KEY (PurchaseOrderID) NOT ENFORCED,
  FOREIGN KEY (EmployeeID) REFERENCES Employee(BusinessEntityID) NOT ENFORCED,
  FOREIGN KEY (VendorID) REFERENCES Vendor(BusinessEntityID) NOT ENFORCED,
  FOREIGN KEY (ShipMethodID) REFERENCES ShipMethod(ShipMethodID) NOT ENFORCED
);

CREATE TABLE SalesOrderDetail (
  SalesOrderID INT64 NOT NULL,
  SalesOrderDetailID INT64 NOT NULL,
  CarrierTrackingNumber STRING,
  OrderQty INT64 NOT NULL,
  ProductID INT64 NOT NULL,
  SpecialOfferID INT64 NOT NULL,
  UnitPrice NUMERIC(19, 4) NOT NULL,
  UnitPriceDiscount NUMERIC(19, 4) NOT NULL,
  LineTotal NUMERIC(19, 6) NOT NULL,
  rowguid STRING NOT NULL,
  ModifiedDate DATETIME NOT NULL,
  PRIMARY KEY (SalesOrderID, SalesOrderDetailID) NOT ENFORCED,
  FOREIGN KEY (SalesOrderID) REFERENCES SalesOrderHeader(SalesOrderID) NOT ENFORCED,
  FOREIGN KEY (SpecialOfferID, ProductID) REFERENCES SpecialOfferProduct(SpecialOfferID, ProductID) NOT ENFORCED
);

CREATE TABLE SalesOrderHeader (
  SalesOrderID INT64 NOT NULL,
  RevisionNumber INT64 NOT NULL,
  OrderDate DATETIME NOT NULL,
  DueDate DATETIME NOT NULL,
  ShipDate DATETIME,
  Status INT64 NOT NULL,
  OnlineOrderFlag BOOL NOT NULL,
  SalesOrderNumber STRING NOT NULL,
  PurchaseOrderNumber STRING,
  AccountNumber STRING,
  CustomerID INT64 NOT NULL,
  SalesPersonID INT64,
  TerritoryID INT64,
  BillToAddressID INT64 NOT NULL,
  ShipToAddressID INT64 NOT NULL,
  ShipMethodID INT64 NOT NULL,
  CreditCardID INT64,
  CreditCardApprovalCode STRING,
  CurrencyRateID INT64,
  SubTotal NUMERIC(19, 4) NOT NULL,
  TaxAmt NUMERIC(19, 4) NOT NULL,
  Freight NUMERIC(19, 4) NOT NULL,
  TotalDue NUMERIC(19, 4) NOT NULL,
  Comment STRING,
  rowguid STRING NOT NULL,
  ModifiedDate DATETIME NOT NULL,
  PRIMARY KEY (SalesOrderID) NOT ENFORCED,
  FOREIGN KEY (BillToAddressID) REFERENCES Address(AddressID) NOT ENFORCED,
  FOREIGN KEY (ShipToAddressID) REFERENCES Address(AddressID) NOT ENFORCED,
  FOREIGN KEY (CreditCardID) REFERENCES CreditCard(CreditCardID) NOT ENFORCED,
  FOREIGN KEY (CurrencyRateID) REFERENCES CurrencyRate(CurrencyRateID) NOT ENFORCED,
  FOREIGN KEY (CustomerID) REFERENCES Customer(CustomerID) NOT ENFORCED,
  FOREIGN KEY (SalesPersonID) REFERENCES SalesPerson(BusinessEntityID) NOT ENFORCED,
  FOREIGN KEY (ShipMethodID) REFERENCES ShipMethod(ShipMethodID) NOT ENFORCED,
  FOREIGN KEY (TerritoryID) REFERENCES SalesTerritory(TerritoryID) NOT ENFORCED
);

CREATE TABLE SalesOrderHeaderSalesReason (
  SalesOrderID INT64 NOT NULL,
  SalesReasonID INT64 NOT NULL,
  ModifiedDate DATETIME NOT NULL,
  PRIMARY KEY (SalesOrderID, SalesReasonID) NOT ENFORCED,
  FOREIGN KEY (SalesReasonID) REFERENCES SalesReason(SalesReasonID) NOT ENFORCED,
  FOREIGN KEY (SalesOrderID) REFERENCES SalesOrderHeader(SalesOrderID) NOT ENFORCED
);

CREATE TABLE SalesPerson (
  BusinessEntityID INT64 NOT NULL,
  TerritoryID INT64,
  SalesQuota NUMERIC(19, 4),
  Bonus NUMERIC(19, 4) NOT NULL,
  CommissionPct NUMERIC(10, 4) NOT NULL,
  SalesYTD NUMERIC(19, 4) NOT NULL,
  SalesLastYear NUMERIC(19, 4) NOT NULL,
  rowguid STRING NOT NULL,
  ModifiedDate DATETIME NOT NULL,
  PRIMARY KEY (BusinessEntityID) NOT ENFORCED,
  FOREIGN KEY (BusinessEntityID) REFERENCES Employee(BusinessEntityID) NOT ENFORCED,
  FOREIGN KEY (TerritoryID) REFERENCES SalesTerritory(TerritoryID) NOT ENFORCED
);

CREATE TABLE SalesPersonQuotaHistory (
  BusinessEntityID INT64 NOT NULL,
  QuotaDate DATETIME NOT NULL,
  SalesQuota NUMERIC(19, 4) NOT NULL,
  rowguid STRING NOT NULL,
  ModifiedDate DATETIME NOT NULL,
  PRIMARY KEY (BusinessEntityID, QuotaDate) NOT ENFORCED,
  FOREIGN KEY (BusinessEntityID) REFERENCES SalesPerson(BusinessEntityID) NOT ENFORCED
);

CREATE TABLE SalesReason (
  SalesReasonID INT64 NOT NULL,
  Name STRING NOT NULL,
  ReasonType STRING NOT NULL,
  ModifiedDate DATETIME NOT NULL,
  PRIMARY KEY (SalesReasonID) NOT ENFORCED
);

CREATE TABLE SalesTaxRate (
  SalesTaxRateID INT64 NOT NULL,
  StateProvinceID INT64 NOT NULL,
  TaxType INT64 NOT NULL,
  TaxRate NUMERIC(10, 4) NOT NULL,
  Name STRING NOT NULL,
  rowguid STRING NOT NULL,
  ModifiedDate DATETIME NOT NULL,
  PRIMARY KEY (SalesTaxRateID) NOT ENFORCED,
  FOREIGN KEY (StateProvinceID) REFERENCES StateProvince(StateProvinceID) NOT ENFORCED
);

CREATE TABLE SalesTerritory (
  TerritoryID INT64 NOT NULL,
  Name STRING NOT NULL,
  CountryRegionCode STRING NOT NULL,
  Group STRING NOT NULL,
  SalesYTD NUMERIC(19, 4) NOT NULL,
  SalesLastYear NUMERIC(19, 4) NOT NULL,
  CostYTD NUMERIC(19, 4) NOT NULL,
  CostLastYear NUMERIC(19, 4) NOT NULL,
  rowguid STRING NOT NULL,
  ModifiedDate DATETIME NOT NULL,
  PRIMARY KEY (TerritoryID) NOT ENFORCED,
  FOREIGN KEY (CountryRegionCode) REFERENCES CountryRegion(CountryRegionCode) NOT ENFORCED
);

CREATE TABLE SalesTerritoryHistory (
  BusinessEntityID INT64 NOT NULL,
  TerritoryID INT64 NOT NULL,
  StartDate DATETIME NOT NULL,
  EndDate DATETIME,
  rowguid STRING NOT NULL,
  ModifiedDate DATETIME NOT NULL,
  PRIMARY KEY (BusinessEntityID, StartDate, TerritoryID) NOT ENFORCED,
  FOREIGN KEY (BusinessEntityID) REFERENCES SalesPerson(BusinessEntityID) NOT ENFORCED,
  FOREIGN KEY (TerritoryID) REFERENCES SalesTerritory(TerritoryID) NOT ENFORCED
);

CREATE TABLE ScrapReason (
  ScrapReasonID INT64 NOT NULL,
  Name STRING NOT NULL,
  ModifiedDate DATETIME NOT NULL,
  PRIMARY KEY (ScrapReasonID) NOT ENFORCED
);

CREATE TABLE Shift (
  ShiftID INT64 NOT NULL,
  Name STRING NOT NULL,
  StartTime STRING NOT NULL,
  EndTime STRING NOT NULL,
  ModifiedDate DATETIME NOT NULL,
  PRIMARY KEY (ShiftID) NOT ENFORCED
);

CREATE TABLE ShipMethod (
  ShipMethodID INT64 NOT NULL,
  Name STRING NOT NULL,
  ShipBase NUMERIC(19, 4) NOT NULL,
  ShipRate NUMERIC(19, 4) NOT NULL,
  rowguid STRING NOT NULL,
  ModifiedDate DATETIME NOT NULL,
  PRIMARY KEY (ShipMethodID) NOT ENFORCED
);

CREATE TABLE ShoppingCartItem (
  ShoppingCartItemID INT64 NOT NULL,
  ShoppingCartID STRING NOT NULL,
  Quantity INT64 NOT NULL,
  ProductID INT64 NOT NULL,
  DateCreated DATETIME NOT NULL,
  ModifiedDate DATETIME NOT NULL,
  PRIMARY KEY (ShoppingCartItemID) NOT ENFORCED,
  FOREIGN KEY (ProductID) REFERENCES Product(ProductID) NOT ENFORCED
);

CREATE TABLE SpecialOffer (
  SpecialOfferID INT64 NOT NULL,
  Description STRING NOT NULL,
  DiscountPct NUMERIC(10, 4) NOT NULL,
  Type STRING NOT NULL,
  Category STRING NOT NULL,
  StartDate DATETIME NOT NULL,
  EndDate DATETIME NOT NULL,
  MinQty INT64 NOT NULL,
  MaxQty INT64,
  rowguid STRING NOT NULL,
  ModifiedDate DATETIME NOT NULL,
  PRIMARY KEY (SpecialOfferID) NOT ENFORCED
);

CREATE TABLE SpecialOfferProduct (
  SpecialOfferID INT64 NOT NULL,
  ProductID INT64 NOT NULL,
  rowguid STRING NOT NULL,
  ModifiedDate DATETIME NOT NULL,
  PRIMARY KEY (SpecialOfferID, ProductID) NOT ENFORCED,
  FOREIGN KEY (ProductID) REFERENCES Product(ProductID) NOT ENFORCED,
  FOREIGN KEY (SpecialOfferID) REFERENCES SpecialOffer(SpecialOfferID) NOT ENFORCED
);

CREATE TABLE StateProvince (
  StateProvinceID INT64 NOT NULL,
  StateProvinceCode STRING NOT NULL,
  CountryRegionCode STRING NOT NULL,
  IsOnlyStateProvinceFlag BOOL NOT NULL,
  Name STRING NOT NULL,
  TerritoryID INT64 NOT NULL,
  rowguid STRING NOT NULL,
  ModifiedDate DATETIME NOT NULL,
  PRIMARY KEY (StateProvinceID) NOT ENFORCED,
  FOREIGN KEY (CountryRegionCode) REFERENCES CountryRegion(CountryRegionCode) NOT ENFORCED,
  FOREIGN KEY (TerritoryID) REFERENCES SalesTerritory(TerritoryID) NOT ENFORCED
);

CREATE TABLE Store (
  BusinessEntityID INT64 NOT NULL,
  Name STRING NOT NULL,
  SalesPersonID INT64,
  Demographics STRING,
  rowguid STRING NOT NULL,
  ModifiedDate DATETIME NOT NULL,
  PRIMARY KEY (BusinessEntityID) NOT ENFORCED,
  FOREIGN KEY (BusinessEntityID) REFERENCES BusinessEntity(BusinessEntityID) NOT ENFORCED,
  FOREIGN KEY (SalesPersonID) REFERENCES SalesPerson(BusinessEntityID) NOT ENFORCED
);

CREATE TABLE TransactionHistory (
  TransactionID INT64 NOT NULL,
  ProductID INT64 NOT NULL,
  ReferenceOrderID INT64 NOT NULL,
  ReferenceOrderLineID INT64 NOT NULL,
  TransactionDate DATETIME NOT NULL,
  TransactionType STRING NOT NULL,
  Quantity INT64 NOT NULL,
  ActualCost NUMERIC(19, 4) NOT NULL,
  ModifiedDate DATETIME NOT NULL,
  PRIMARY KEY (TransactionID) NOT ENFORCED,
  FOREIGN KEY (ProductID) REFERENCES Product(ProductID) NOT ENFORCED
);

CREATE TABLE TransactionHistoryArchive (
  TransactionID INT64 NOT NULL,
  ProductID INT64 NOT NULL,
  ReferenceOrderID INT64 NOT NULL,
  ReferenceOrderLineID INT64 NOT NULL,
  TransactionDate DATETIME NOT NULL,
  TransactionType STRING NOT NULL,
  Quantity INT64 NOT NULL,
  ActualCost NUMERIC(19, 4) NOT NULL,
  ModifiedDate DATETIME NOT NULL,
  PRIMARY KEY (TransactionID) NOT ENFORCED
);

CREATE TABLE UnitMeasure (
  UnitMeasureCode STRING NOT NULL,
  Name STRING NOT NULL,
  ModifiedDate DATETIME NOT NULL,
  PRIMARY KEY (UnitMeasureCode) NOT ENFORCED
);

CREATE TABLE Vendor (
  BusinessEntityID INT64 NOT NULL,
  AccountNumber STRING NOT NULL,
  Name STRING NOT NULL,
  CreditRating INT64 NOT NULL,
  PreferredVendorStatus BOOL NOT NULL,
  ActiveFlag BOOL NOT NULL,
  PurchasingWebServiceURL STRING,
  ModifiedDate DATETIME NOT NULL,
  PRIMARY KEY (BusinessEntityID) NOT ENFORCED,
  FOREIGN KEY (BusinessEntityID) REFERENCES BusinessEntity(BusinessEntityID) NOT ENFORCED
);

CREATE TABLE WorkOrder (
  WorkOrderID INT64 NOT NULL,
  ProductID INT64 NOT NULL,
  OrderQty INT64 NOT NULL,
  StockedQty INT64 NOT NULL,
  ScrappedQty INT64 NOT NULL,
  StartDate DATETIME NOT NULL,
  EndDate DATETIME,
  DueDate DATETIME NOT NULL,
  ScrapReasonID INT64,
  ModifiedDate DATETIME NOT NULL,
  PRIMARY KEY (WorkOrderID) NOT ENFORCED,
  FOREIGN KEY (ProductID) REFERENCES Product(ProductID) NOT ENFORCED,
  FOREIGN KEY (ScrapReasonID) REFERENCES ScrapReason(ScrapReasonID) NOT ENFORCED
);

CREATE TABLE WorkOrderRouting (
  WorkOrderID INT64 NOT NULL,
  ProductID INT64 NOT NULL,
  OperationSequence INT64 NOT NULL,
  LocationID INT64 NOT NULL,
  ScheduledStartDate DATETIME NOT NULL,
  ScheduledEndDate DATETIME NOT NULL,
  ActualStartDate DATETIME,
  ActualEndDate DATETIME,
  ActualResourceHrs NUMERIC(9, 4),
  PlannedCost NUMERIC(19, 4) NOT NULL,
  ActualCost NUMERIC(19, 4),
  ModifiedDate DATETIME NOT NULL,
  PRIMARY KEY (WorkOrderID, ProductID, OperationSequence) NOT ENFORCED,
  FOREIGN KEY (LocationID) REFERENCES Location(LocationID) NOT ENFORCED,
  FOREIGN KEY (WorkOrderID) REFERENCES WorkOrder(WorkOrderID) NOT ENFORCED
);
