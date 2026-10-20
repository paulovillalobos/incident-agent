-- Lookups by SKU (reservations) and by warehouse (stock reports).
CREATE INDEX idx_stock_sku_warehouse ON stock (sku, warehouse);
CREATE INDEX idx_stock_warehouse_sku ON stock (warehouse, sku);
