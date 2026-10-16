-- ERCS v3.1 · 业务表 17 张（只读）
-- 由 scripts/init_db.py --ddl-only 从 ORM 元数据生成（单一事实来源）
-- 含建表语句、索引与外键约束。

SET FOREIGN_KEY_CHECKS = 0;

CREATE TABLE user_info (
	user_id VARCHAR(50) NOT NULL, 
	register_time DATETIME, 
	user_level VARCHAR(20), 
	PRIMARY KEY (user_id)
);

CREATE TABLE region (
	province VARCHAR(20) NOT NULL, 
	city VARCHAR(20) NOT NULL, 
	district VARCHAR(20) NOT NULL, 
	PRIMARY KEY (province, city, district)
);

CREATE TABLE receive_info (
	receive_id VARCHAR(50) NOT NULL, 
	user_id VARCHAR(50) NOT NULL, 
	receiver_name VARCHAR(50) NOT NULL, 
	receiver_phone VARCHAR(50) NOT NULL, 
	receive_province VARCHAR(50) NOT NULL, 
	receive_city VARCHAR(50) NOT NULL, 
	receive_district VARCHAR(50) NOT NULL, 
	receive_street_address VARCHAR(120) NOT NULL, 
	PRIMARY KEY (receive_id)
);
CREATE INDEX idx_receive_phone ON receive_info (receiver_phone);
CREATE INDEX idx_receive_user ON receive_info (user_id);

CREATE TABLE product_category (
	product_category VARCHAR(20) NOT NULL, 
	PRIMARY KEY (product_category)
);

CREATE TABLE sku_info (
	sku_id VARCHAR(50) NOT NULL, 
	sku_name VARCHAR(100) NOT NULL, 
	sku_price NUMERIC(10, 2) NOT NULL, 
	sku_category VARCHAR(20) NOT NULL, 
	sku_count INTEGER NOT NULL, 
	PRIMARY KEY (sku_id)
);

CREATE TABLE order_status (
	order_status VARCHAR(20) NOT NULL, 
	status_code INTEGER, 
	PRIMARY KEY (order_status)
);

CREATE TABLE order_info (
	order_id VARCHAR(50) NOT NULL, 
	create_time DATETIME NOT NULL, 
	payment_time DATETIME, 
	delivered_time DATETIME, 
	complete_time DATETIME, 
	user_id VARCHAR(50) NOT NULL, 
	receive_id VARCHAR(50), 
	order_status VARCHAR(20) NOT NULL, 
	PRIMARY KEY (order_id)
);
CREATE INDEX idx_order_status ON order_info (order_status);
CREATE INDEX idx_order_user ON order_info (user_id, create_time);
CREATE INDEX idx_order_receive ON order_info (receive_id);

CREATE TABLE order_detail (
	order_detail_id VARCHAR(50) NOT NULL, 
	order_id VARCHAR(50) NOT NULL, 
	sku_id VARCHAR(50) NOT NULL, 
	sku_name VARCHAR(100) NOT NULL, 
	sku_count INTEGER NOT NULL, 
	total_amount NUMERIC(12, 2) NOT NULL, 
	discount_amount NUMERIC(12, 2) NOT NULL, 
	final_amount NUMERIC(12, 2) NOT NULL, 
	PRIMARY KEY (order_detail_id)
);
CREATE INDEX idx_detail_order ON order_detail (order_id);

CREATE TABLE logistics (
	logistics_id VARCHAR(50) NOT NULL, 
	logistics_category VARCHAR(50), 
	create_time DATETIME, 
	delivered_time DATETIME, 
	PRIMARY KEY (logistics_id)
);

CREATE TABLE order_logistics (
	order_id VARCHAR(50) NOT NULL, 
	logistics_id VARCHAR(50) NOT NULL, 
	PRIMARY KEY (order_id, logistics_id)
);

CREATE TABLE logistics_company (
	company_name VARCHAR(50) NOT NULL, 
	PRIMARY KEY (company_name)
);

CREATE TABLE logistics_complaint (
	logistics_status VARCHAR(20) NOT NULL, 
	logistics_complaint VARCHAR(50) NOT NULL, 
	PRIMARY KEY (logistics_status, logistics_complaint)
);

CREATE TABLE logistics_complaints_record (
	record_id BIGINT NOT NULL AUTO_INCREMENT, 
	logistics_id VARCHAR(50), 
	complaint_time DATETIME, 
	user_id VARCHAR(50) NOT NULL, 
	logistics_status VARCHAR(20), 
	logistics_complaint VARCHAR(50), 
	PRIMARY KEY (record_id)
);
CREATE INDEX idx_complaint_user ON logistics_complaints_record (user_id, complaint_time);

CREATE TABLE postsale_status (
	postsale_status VARCHAR(20) NOT NULL, 
	is_refund TINYINT NOT NULL, 
	is_return TINYINT NOT NULL, 
	is_exchange TINYINT NOT NULL, 
	PRIMARY KEY (postsale_status)
);

CREATE TABLE postsale_reason (
	postsale_reason VARCHAR(50) NOT NULL, 
	product_category VARCHAR(20), 
	PRIMARY KEY (postsale_reason)
);

CREATE TABLE postsale (
	postsale_id VARCHAR(50) NOT NULL, 
	order_detail_id VARCHAR(50) NOT NULL, 
	refund_amount NUMERIC(12, 2) NOT NULL, 
	postsale_type VARCHAR(20) NOT NULL, 
	postsale_status VARCHAR(20) NOT NULL, 
	postsale_reason VARCHAR(50), 
	create_time DATETIME, 
	PRIMARY KEY (postsale_id)
);
CREATE INDEX idx_postsale_detail ON postsale (order_detail_id);

CREATE TABLE postsale_logistics (
	postsale_id VARCHAR(50) NOT NULL, 
	logistics_id VARCHAR(50) NOT NULL, 
	PRIMARY KEY (postsale_id, logistics_id)
);

SET FOREIGN_KEY_CHECKS = 1;