-- Seed: 100 warehouses x 10000 SKUs = 1,000,000 stock rows, plus a few thousand holds.
INSERT INTO warehouses (id, region)
WITH RECURSIVE w(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM w WHERE i < 100)
SELECT printf('WH-%03d', i), CASE i % 3 WHEN 0 THEN 'PT' WHEN 1 THEN 'ES' ELSE 'FR' END FROM w;

INSERT INTO stock (sku, warehouse, qty)
WITH RECURSIVE s(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM s WHERE i < 10000)
SELECT printf('SKU-%05d', s.i), w.id, 10000 FROM s CROSS JOIN warehouses AS w;

INSERT INTO holds (sku, warehouse, held)
WITH RECURSIVE h(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM h WHERE i < 4000)
SELECT printf('SKU-%05d', (i * 7) % 10000 + 1), printf('WH-%03d', (i * 13) % 100 + 1), i % 5 + 1 FROM h;
