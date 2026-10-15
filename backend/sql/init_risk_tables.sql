-- ERCS v3.1 · 风控表 16 张（读写）
-- 由 scripts/init_db.py --ddl-only 从 ORM 元数据生成（单一事实来源）
-- 含建表语句、索引与外键约束。

SET FOREIGN_KEY_CHECKS = 0;

CREATE TABLE risk_agent_message (
	message_id VARCHAR(50) NOT NULL, 
	session_id VARCHAR(50) NOT NULL, 
	`role` VARCHAR(16) NOT NULL, 
	content TEXT NOT NULL, 
	tool_calls JSON, 
	create_time DATETIME NOT NULL, 
	PRIMARY KEY (message_id), 
	CONSTRAINT fk_msg_session FOREIGN KEY(session_id) REFERENCES risk_agent_session (session_id)
);
CREATE INDEX idx_msg_session ON risk_agent_message (session_id, create_time);

CREATE TABLE risk_agent_session (
	session_id VARCHAR(50) NOT NULL, 
	user_id VARCHAR(64) NOT NULL, 
	title VARCHAR(200), 
	message_count INTEGER NOT NULL, 
	create_time DATETIME NOT NULL, 
	update_time DATETIME, 
	PRIMARY KEY (session_id)
);
CREATE INDEX idx_sess_user ON risk_agent_session (user_id, update_time);

CREATE TABLE risk_assessment (
	assessment_id VARCHAR(50) NOT NULL, 
	event_id VARCHAR(50) NOT NULL, 
	user_id VARCHAR(64) NOT NULL, 
	rule_results JSON, 
	rule_count INTEGER NOT NULL, 
	final_score INTEGER NOT NULL, 
	risk_level VARCHAR(10) NOT NULL, 
	decision VARCHAR(16) NOT NULL, 
	is_veto TINYINT NOT NULL, 
	hit_source VARCHAR(16), 
	cost_ms NUMERIC(10, 3), 
	create_time DATETIME NOT NULL, 
	PRIMARY KEY (assessment_id)
);
CREATE INDEX idx_ast_decision ON risk_assessment (decision);
CREATE INDEX idx_ast_user ON risk_assessment (user_id, create_time);
CREATE INDEX idx_ast_time ON risk_assessment (create_time);

CREATE TABLE risk_audit_log (
	log_id VARCHAR(50) NOT NULL, 
	biz_type VARCHAR(32) NOT NULL, 
	biz_id VARCHAR(64) NOT NULL, 
	action VARCHAR(32) NOT NULL, 
	operator VARCHAR(64) NOT NULL, 
	before_value JSON, 
	after_value JSON, 
	detail JSON, 
	`role` VARCHAR(16), 
	create_time DATETIME NOT NULL, 
	PRIMARY KEY (log_id)
);
CREATE INDEX idx_audit_op ON risk_audit_log (operator, create_time);
CREATE INDEX idx_audit_biz ON risk_audit_log (biz_type, biz_id);
CREATE INDEX idx_audit_time ON risk_audit_log (create_time);

CREATE TABLE risk_blacklist (
	blacklist_id BIGINT NOT NULL AUTO_INCREMENT, 
	list_type VARCHAR(10) NOT NULL, 
	entity_type VARCHAR(16) NOT NULL, 
	entity_value VARCHAR(200) NOT NULL, 
	reason TEXT, 
	operator VARCHAR(64), 
	expire_time DATETIME, 
	hit_count INTEGER NOT NULL, 
	create_time DATETIME NOT NULL, 
	PRIMARY KEY (blacklist_id), 
	CONSTRAINT uk_list UNIQUE (list_type, entity_type, entity_value)
);
CREATE INDEX idx_bl_lookup ON risk_blacklist (entity_type, entity_value, list_type);

CREATE TABLE risk_case (
	case_id VARCHAR(50) NOT NULL, 
	assessment_id VARCHAR(50) NOT NULL, 
	event_id VARCHAR(50) NOT NULL, 
	user_id VARCHAR(64) NOT NULL, 
	case_status VARCHAR(16) NOT NULL, 
	case_category VARCHAR(20), 
	final_score INTEGER NOT NULL, 
	risk_level VARCHAR(10) NOT NULL, 
	assignee VARCHAR(64), 
	conclusion VARCHAR(16), 
	remark TEXT, 
	create_time DATETIME NOT NULL, 
	disposed_at DATETIME, 
	PRIMARY KEY (case_id)
);
CREATE INDEX idx_case_user ON risk_case (user_id);
CREATE INDEX idx_case_cat ON risk_case (case_category);
CREATE INDEX idx_case_status ON risk_case (case_status, create_time);
CREATE INDEX idx_case_ast ON risk_case (assessment_id);
CREATE INDEX idx_case_event ON risk_case (event_id);

CREATE TABLE risk_challenge_record (
	challenge_id VARCHAR(50) NOT NULL, 
	event_id VARCHAR(50) NOT NULL, 
	channel VARCHAR(16) NOT NULL, 
	target VARCHAR(32), 
	status VARCHAR(16) NOT NULL, 
	issued_at DATETIME NOT NULL, 
	resolved_at DATETIME, 
	PRIMARY KEY (challenge_id), 
	CONSTRAINT fk_ch_event FOREIGN KEY(event_id) REFERENCES risk_event (event_id)
);
CREATE INDEX idx_ch_pending ON risk_challenge_record (status, issued_at);
CREATE INDEX idx_ch_event ON risk_challenge_record (event_id);

CREATE TABLE risk_disposal_record (
	disposal_id VARCHAR(50) NOT NULL, 
	case_id VARCHAR(50) NOT NULL, 
	action_type VARCHAR(32) NOT NULL, 
	target_id VARCHAR(64) NOT NULL, 
	operator VARCHAR(64) NOT NULL, 
	result VARCHAR(16) NOT NULL, 
	detail JSON, 
	create_time DATETIME NOT NULL, 
	PRIMARY KEY (disposal_id), 
	CONSTRAINT fk_disp_case FOREIGN KEY(case_id) REFERENCES risk_case (case_id)
);
CREATE INDEX idx_disp_case ON risk_disposal_record (case_id);

CREATE TABLE risk_entity (
	entity_id VARCHAR(200) NOT NULL, 
	entity_type VARCHAR(16) NOT NULL, 
	entity_value VARCHAR(200) NOT NULL, 
	risk_tag_count INTEGER NOT NULL, 
	risk_tags JSON, 
	first_seen_at DATETIME NOT NULL, 
	PRIMARY KEY (entity_id), 
	CONSTRAINT uk_entity UNIQUE (entity_type, entity_value)
);

CREATE TABLE risk_entity_relation (
	relation_id VARCHAR(300) NOT NULL, 
	source_entity_id VARCHAR(200) NOT NULL, 
	target_entity_id VARCHAR(200) NOT NULL, 
	relation_type VARCHAR(24) NOT NULL, 
	evidence_count INTEGER NOT NULL, 
	create_time DATETIME NOT NULL, 
	PRIMARY KEY (relation_id)
);
CREATE INDEX idx_rel_source ON risk_entity_relation (source_entity_id);
CREATE INDEX idx_rel_target ON risk_entity_relation (target_entity_id);

CREATE TABLE risk_event (
	event_id VARCHAR(50) NOT NULL, 
	event_type VARCHAR(32) NOT NULL, 
	source_id VARCHAR(64) NOT NULL, 
	user_id VARCHAR(64) NOT NULL, 
	device_id VARCHAR(64), 
	ip VARCHAR(64), 
	phone VARCHAR(32), 
	address_id VARCHAR(64), 
	amount NUMERIC(12, 2) NOT NULL, 
	event_data JSON, 
	event_time DATETIME, 
	create_time DATETIME NOT NULL, 
	PRIMARY KEY (event_id)
);
CREATE INDEX idx_ev_type ON risk_event (event_type, create_time);
CREATE INDEX idx_ev_device ON risk_event (device_id, create_time);
CREATE INDEX idx_ev_ip ON risk_event (ip, create_time);
CREATE INDEX idx_ev_user ON risk_event (user_id, create_time);
CREATE INDEX idx_ev_time ON risk_event (create_time);

CREATE TABLE risk_feature (
	feature_id BIGINT NOT NULL AUTO_INCREMENT, 
	event_id VARCHAR(50) NOT NULL, 
	dimension VARCHAR(16) NOT NULL, 
	feature_name VARCHAR(64) NOT NULL, 
	feature_value NUMERIC(15, 4), 
	baseline_value NUMERIC(15, 4), 
	compute_time DATETIME NOT NULL, 
	PRIMARY KEY (feature_id), 
	CONSTRAINT fk_feat_event FOREIGN KEY(event_id) REFERENCES risk_event (event_id)
);
CREATE INDEX idx_feat_baseline ON risk_feature (compute_time, feature_name);
CREATE INDEX idx_feat_entity ON risk_feature (dimension, feature_name);
CREATE INDEX idx_feat_event ON risk_feature (event_id);

CREATE TABLE risk_rule (
	rule_id VARCHAR(50) NOT NULL, 
	rule_name VARCHAR(100) NOT NULL, 
	rule_category VARCHAR(20) NOT NULL, 
	event_type VARCHAR(20) NOT NULL, 
	rule_condition JSON NOT NULL, 
	risk_level VARCHAR(10) NOT NULL, 
	risk_score INTEGER NOT NULL, 
	action VARCHAR(20) NOT NULL, 
	challengeable TINYINT NOT NULL, 
	priority INTEGER NOT NULL, 
	is_enabled TINYINT NOT NULL, 
	version INTEGER NOT NULL, 
	description TEXT, 
	updated_by VARCHAR(64), 
	create_time DATETIME NOT NULL, 
	update_time DATETIME, 
	PRIMARY KEY (rule_id)
);
CREATE INDEX idx_rule_scene ON risk_rule (event_type, is_enabled, priority);

CREATE TABLE risk_rule_scene (
	scene_code VARCHAR(32) NOT NULL, 
	scene_name VARCHAR(64) NOT NULL, 
	biz_event VARCHAR(32) NOT NULL, 
	pass_threshold INTEGER NOT NULL, 
	mark_threshold INTEGER NOT NULL, 
	reject_threshold INTEGER NOT NULL, 
	multi_rule_bonus INTEGER NOT NULL, 
	veto_min_score INTEGER NOT NULL, 
	enabled TINYINT NOT NULL, 
	PRIMARY KEY (scene_code)
);

CREATE TABLE risk_user (
	user_id VARCHAR(16) NOT NULL, 
	username VARCHAR(64) NOT NULL, 
	password_hash VARCHAR(255) NOT NULL, 
	display_name VARCHAR(64) NOT NULL, 
	`role` VARCHAR(16) NOT NULL, 
	status VARCHAR(16) NOT NULL, 
	fail_count INTEGER NOT NULL, 
	locked_at DATETIME, 
	last_login_at DATETIME, 
	create_time DATETIME NOT NULL, 
	PRIMARY KEY (user_id), 
	UNIQUE (username)
);
CREATE INDEX idx_user_username ON risk_user (username);

CREATE TABLE risk_user_profile (
	user_id VARCHAR(64) NOT NULL, 
	risk_score INTEGER NOT NULL, 
	risk_level VARCHAR(10) NOT NULL, 
	total_orders INTEGER NOT NULL, 
	total_amount NUMERIC(15, 2) NOT NULL, 
	total_refunds INTEGER NOT NULL, 
	refund_rate NUMERIC(5, 4) NOT NULL, 
	avg_order_amount NUMERIC(12, 2) NOT NULL, 
	address_count INTEGER NOT NULL, 
	complaint_count INTEGER NOT NULL, 
	assessment_count INTEGER NOT NULL, 
	risk_tags JSON, 
	last_assessment_time DATETIME, 
	update_time DATETIME, 
	PRIMARY KEY (user_id)
);

SET FOREIGN_KEY_CHECKS = 1;

-- ============================================================
-- 审计日志不可篡改保护（SPEC §6.3 / R-MA-03）
-- ⚠ 这两条触发器是「审计不可篡改」承诺的**数据库侧**保障，
--    与应用层「不提供改删接口」构成双保护，缺一不可。
-- ============================================================
DROP TRIGGER IF EXISTS trg_audit_no_update;
DROP TRIGGER IF EXISTS trg_audit_no_delete;

DELIMITER $$
CREATE TRIGGER trg_audit_no_update BEFORE UPDATE ON risk_audit_log
FOR EACH ROW BEGIN
    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = '审计日志不可修改';
END$$
CREATE TRIGGER trg_audit_no_delete BEFORE DELETE ON risk_audit_log
FOR EACH ROW BEGIN
    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = '审计日志不可删除';
END$$
DELIMITER ;