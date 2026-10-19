-- 从所附 SQLite 原件只读导出的表结构；不包含业务数据。
-- 关系仅以数据库实际声明为准，不等于模型核实了业务关系。

CREATE TABLE "Category" 
(
  "Id" INTEGER PRIMARY KEY, 
  "CategoryName" VARCHAR(8000) NULL, 
  "Description" VARCHAR(8000) NULL 
);

CREATE TABLE "Customer" 
(
  "Id" VARCHAR(8000) PRIMARY KEY, 
  "CompanyName" VARCHAR(8000) NULL, 
  "ContactName" VARCHAR(8000) NULL, 
  "ContactTitle" VARCHAR(8000) NULL, 
  "Address" VARCHAR(8000) NULL, 
  "City" VARCHAR(8000) NULL, 
  "Region" VARCHAR(8000) NULL, 
  "PostalCode" VARCHAR(8000) NULL, 
  "Country" VARCHAR(8000) NULL, 
  "Phone" VARCHAR(8000) NULL, 
  "Fax" VARCHAR(8000) NULL 
);

CREATE TABLE "CustomerCustomerDemo" 
(
  "Id" VARCHAR(8000) PRIMARY KEY, 
  "CustomerTypeId" VARCHAR(8000) NULL 
);

CREATE TABLE "CustomerDemographic" 
(
  "Id" VARCHAR(8000) PRIMARY KEY, 
  "CustomerDesc" VARCHAR(8000) NULL 
);

CREATE TABLE "Employee" 
(
  "Id" INTEGER PRIMARY KEY, 
  "LastName" VARCHAR(8000) NULL, 
  "FirstName" VARCHAR(8000) NULL, 
  "Title" VARCHAR(8000) NULL, 
  "TitleOfCourtesy" VARCHAR(8000) NULL, 
  "BirthDate" VARCHAR(8000) NULL, 
  "HireDate" VARCHAR(8000) NULL, 
  "Address" VARCHAR(8000) NULL, 
  "City" VARCHAR(8000) NULL, 
  "Region" VARCHAR(8000) NULL, 
  "PostalCode" VARCHAR(8000) NULL, 
  "Country" VARCHAR(8000) NULL, 
  "HomePhone" VARCHAR(8000) NULL, 
  "Extension" VARCHAR(8000) NULL, 
  "Photo" BLOB NULL, 
  "Notes" VARCHAR(8000) NULL, 
  "ReportsTo" INTEGER NULL, 
  "PhotoPath" VARCHAR(8000) NULL 
);

CREATE TABLE "EmployeeTerritory" 
(
  "Id" VARCHAR(8000) PRIMARY KEY, 
  "EmployeeId" INTEGER NOT NULL, 
  "TerritoryId" VARCHAR(8000) NULL 
);

CREATE TABLE "Order" 
(
  "Id" INTEGER PRIMARY KEY, 
  "CustomerId" VARCHAR(8000) NULL, 
  "EmployeeId" INTEGER NOT NULL, 
  "OrderDate" VARCHAR(8000) NULL, 
  "RequiredDate" VARCHAR(8000) NULL, 
  "ShippedDate" VARCHAR(8000) NULL, 
  "ShipVia" INTEGER NULL, 
  "Freight" DECIMAL NOT NULL, 
  "ShipName" VARCHAR(8000) NULL, 
  "ShipAddress" VARCHAR(8000) NULL, 
  "ShipCity" VARCHAR(8000) NULL, 
  "ShipRegion" VARCHAR(8000) NULL, 
  "ShipPostalCode" VARCHAR(8000) NULL, 
  "ShipCountry" VARCHAR(8000) NULL 
);

CREATE TABLE "OrderDetail" 
(
  "Id" VARCHAR(8000) PRIMARY KEY, 
  "OrderId" INTEGER NOT NULL, 
  "ProductId" INTEGER NOT NULL, 
  "UnitPrice" DECIMAL NOT NULL, 
  "Quantity" INTEGER NOT NULL, 
  "Discount" DOUBLE NOT NULL 
);

CREATE TABLE "Product" 
(
  "Id" INTEGER PRIMARY KEY, 
  "ProductName" VARCHAR(8000) NULL, 
  "SupplierId" INTEGER NOT NULL, 
  "CategoryId" INTEGER NOT NULL, 
  "QuantityPerUnit" VARCHAR(8000) NULL, 
  "UnitPrice" DECIMAL NOT NULL, 
  "UnitsInStock" INTEGER NOT NULL, 
  "UnitsOnOrder" INTEGER NOT NULL, 
  "ReorderLevel" INTEGER NOT NULL, 
  "Discontinued" INTEGER NOT NULL 
);

CREATE TABLE "Region" 
(
  "Id" INTEGER PRIMARY KEY, 
  "RegionDescription" VARCHAR(8000) NULL 
);

CREATE TABLE "Shipper" 
(
  "Id" INTEGER PRIMARY KEY, 
  "CompanyName" VARCHAR(8000) NULL, 
  "Phone" VARCHAR(8000) NULL 
);

CREATE TABLE "Supplier" 
(
  "Id" INTEGER PRIMARY KEY, 
  "CompanyName" VARCHAR(8000) NULL, 
  "ContactName" VARCHAR(8000) NULL, 
  "ContactTitle" VARCHAR(8000) NULL, 
  "Address" VARCHAR(8000) NULL, 
  "City" VARCHAR(8000) NULL, 
  "Region" VARCHAR(8000) NULL, 
  "PostalCode" VARCHAR(8000) NULL, 
  "Country" VARCHAR(8000) NULL, 
  "Phone" VARCHAR(8000) NULL, 
  "Fax" VARCHAR(8000) NULL, 
  "HomePage" VARCHAR(8000) NULL 
);

CREATE TABLE "Territory" 
(
  "Id" VARCHAR(8000) PRIMARY KEY, 
  "TerritoryDescription" VARCHAR(8000) NULL, 
  "RegionId" INTEGER NOT NULL 
);
