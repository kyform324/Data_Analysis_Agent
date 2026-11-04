-- 表结构创建语句

-- 商品分类表
CREATE TABLE IF NOT EXISTS categories(
    category_id INTEGER PRIMARY KEY AUTOINCREMENT,
    category_name TEXT NOT NULL UNIQUE
);

-- 商品规格表
CREATE TABLE IF NOT EXISTS products(
    product_id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_name TEXT NOT NULL,
    sku TEXT NOT NULL UNIQUE,
    price_cents INTEGER NOT NULL CHECK(price_cents >= 0),
    category_id INTEGER NOT NULL,
    FOREIGN KEY (category_id)
        REFERENCES categories(category_id)
);

-- 客户表
CREATE TABLE IF NOT EXISTS customers(
    customer_id INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_name TEXT NOT NULL,
    registered_at TEXT NOT NULL,
    province TEXT NOT NULL CHECK(province IN ('广东省','浙江省','江苏省','四川省','湖北省','山东省')),
    city TEXT NOT NULL CHECK(city IN (
        '深圳市','广州市','东莞市',
        '杭州市','宁波市','温州市',
        '南京市','苏州市','无锡市',
        '成都市','绵阳市',
        '武汉市','宜昌市',
        '青岛市','济南市'
    ))
);

-- 订单主表
CREATE TABLE IF NOT EXISTS orders(
    order_id INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id INTEGER NOT NULL,
    order_time TEXT NOT NULL,
    order_status TEXT NOT NULL CHECK(order_status IN ('待支付','已支付','运输中','已完成','已取消')),
    shipping_province TEXT NOT NULL CHECK(shipping_province IN ('广东省','浙江省','江苏省','四川省','湖北省','山东省')),
    shipping_city TEXT NOT NULL CHECK(shipping_city IN (
        '深圳市','广州市','东莞市',
        '杭州市','宁波市','温州市',
        '南京市','苏州市','无锡市',
        '成都市','绵阳市',
        '武汉市','宜昌市',
        '青岛市','济南市'
    )),
    FOREIGN KEY(customer_id)
        REFERENCES customers(customer_id)
);

-- 订单明细表
CREATE TABLE IF NOT EXISTS order_items(
    order_item_id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER NOT NULL,
    product_id INTEGER NOT NULL,
    unit_price_cents INTEGER NOT NULL CHECK(unit_price_cents >= 0),
    quantity INTEGER NOT NULL CHECK(quantity > 0),
    FOREIGN KEY(order_id)
        REFERENCES orders(order_id),
    FOREIGN KEY(product_id)
        REFERENCES products(product_id),
    UNIQUE(order_id,product_id)
);

-- 日终库存快照表
CREATE TABLE IF NOT EXISTS inventory_daily(
    inventory_id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER NOT NULL,
    snapshot_date TEXT NOT NULL,
    inventory_num INTEGER NOT NULL CHECK(inventory_num >= 0),
    recorded_at TEXT NOT NULL,
    FOREIGN KEY(product_id)
        REFERENCES products(product_id),
    UNIQUE(product_id,snapshot_date)
);

-- 库存流水表
CREATE TABLE IF NOT EXISTS inventory_movements(
    movement_id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER NOT NULL,
    movement_type TEXT NOT NULL CHECK(movement_type IN ('初始库存','补货','销售出库','退货入库','人工调整','报损')),
    quantity_delta INTEGER NOT NULL CHECK(quantity_delta <> 0),
    occurred_at TEXT NOT NULL,
    order_item_id INTEGER,
    note TEXT,
    FOREIGN KEY(product_id)
        REFERENCES products(product_id),
    FOREIGN KEY(order_item_id)
        REFERENCES order_items(order_item_id)
);

-- 商品状态历史变更表
CREATE TABLE IF NOT EXISTS product_status_history(
    status_history_id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER NOT NULL,
    status_after TEXT NOT NULL CHECK(status_after IN ('已上架','未上架','已删除')),
    status_changed TEXT NOT NULL,
    FOREIGN KEY(product_id)
        REFERENCES products(product_id)

);

-- 订单状态历史变更表
CREATE TABLE IF NOT EXISTS order_status_history(
    order_history_id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER NOT NULL,
    from_status TEXT CHECK(from_status IN ('待支付','已支付','运输中','已完成','已取消')),
    to_status TEXT NOT NULL CHECK(to_status IN ('待支付','已支付','运输中','已完成','已取消')),
    changed_at TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    change_note TEXT,
    FOREIGN KEY(order_id)
        REFERENCES orders(order_id)
);