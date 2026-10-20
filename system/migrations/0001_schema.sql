-- Inventory schema: stock per (sku, warehouse) and reservation holds.
CREATE TABLE warehouses (id TEXT PRIMARY KEY, region TEXT NOT NULL);
CREATE TABLE stock (
    sku TEXT NOT NULL,
    warehouse TEXT NOT NULL,
    qty INTEGER NOT NULL
);
CREATE TABLE holds (
    sku TEXT NOT NULL,
    warehouse TEXT NOT NULL,
    held INTEGER NOT NULL,
    PRIMARY KEY (sku, warehouse)
);
