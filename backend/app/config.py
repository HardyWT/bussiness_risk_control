"""全局配置（Pydantic Settings）。

对应文档：
- SPEC §0.3.1  环境变量清单（.env + 环境变量双源，环境变量优先）
- SPEC §0.3    技术栈（MySQL 8 + PyMySQL）
- SPEC §3.3    决策阈值（pass / mark / reject / veto / bonus）
- SPEC §9.6    LLM 配置（DashScope OpenAI 兼容接口）

⚠ 敏感配置（DB_PASSWORD / LLM_API_KEY / JWT_SECRET）不写默认值或仅给演示默认，
   由 `.env` 提供；`.env` 不入版本控制。
"""

from functools import lru_cache

from pydantic import computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=True,
    )

    # ---------------------------------------------------------------- 数据库
    DB_HOST: str = "localhost"
    DB_PORT: int = 3306
    DB_USER: str = "root"
    DB_PASSWORD: str = ""          # ⚠ 必填，由 .env 提供
    DB_NAME: str = "ecs"
    TEST_DB_NAME: str = "ecs_test"
    DB_CHARSET: str = "utf8mb4"
    DB_POOL_SIZE: int = 10
    DB_MAX_OVERFLOW: int = 20

    # ---------------------------------------------------------------- LLM（AI Agent）
    LLM_API_KEY: str = ""          # ⚠ 待项目方补充（阿里云百炼 DashScope API Key）
    LLM_BASE_URL: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    LLM_MODEL_NAME: str = "qwen-plus"
    LLM_TEMPERATURE: float = 0.1
    LLM_TIMEOUT_SECONDS: int = 30
    AGENT_MAX_CONTEXT_MESSAGES: int = 20

    # ---------------------------------------------------------------- 决策阈值
    RISK_PASS_THRESHOLD: int = 30      # < 30 → Pass
    RISK_MARK_THRESHOLD: int = 60      # 30~59 → Mark
    RISK_REVIEW_THRESHOLD: int = 80    # ≥ 80 → Reject；中间档 → Challenge / Review
    RISK_MULTI_RULE_BONUS: int = 3     # 每条额外命中规则加分
    RISK_VETO_MIN_SCORE: int = 90      # 一票否决最低分

    # ---------------------------------------------------------------- 认证
    JWT_SECRET: str = "ercs-demo-secret-please-change-in-prod"
    JWT_ALGORITHM: str = "HS256"
    JWT_EXPIRE_SECONDS: int = 8 * 3600
    LOGIN_MAX_FAIL: int = 5
    LOGIN_LOCK_SECONDS: int = 10 * 60

    # ---------------------------------------------------------------- 业务
    LOSS_DISCOUNT: float = 0.6         # 预估挽回资损折扣系数
    CHALLENGE_WINDOW_SECONDS: int = 120
    RULE_CACHE_TTL_SECONDS: int = 5
    MAX_GRAPH_NODES: int = 50
    FEATURE_TIMEOUT_MS: int = 500      # 单特征计算超时（降级）
    FEATURE_TOTAL_BUDGET_MS: int = 200 # 特征计算总预算（G-05）

    # ---------------------------------------------------------------- 事件
    SERVICE_ID: str = "ercs-demo-service"

    # ================================================================ 派生属性
    @computed_field
    @property
    def DATABASE_URL(self) -> str:
        """业务 + 风控同库（SPEC §0.3.1）。"""
        return (
            f"mysql+pymysql://{self.DB_USER}:{self.DB_PASSWORD}"
            f"@{self.DB_HOST}:{self.DB_PORT}/{self.DB_NAME}?charset={self.DB_CHARSET}"
        )

    def database_url(self, db_name: str | None = None) -> str:
        """指定库名的连接串（测试库用）。"""
        name = db_name or self.DB_NAME
        return (
            f"mysql+pymysql://{self.DB_USER}:{self.DB_PASSWORD}"
            f"@{self.DB_HOST}:{self.DB_PORT}/{name}?charset={self.DB_CHARSET}"
        )

    def server_url(self) -> str:
        """不带库名的连接串（用于 CREATE DATABASE）。"""
        return (
            f"mysql+pymysql://{self.DB_USER}:{self.DB_PASSWORD}"
            f"@{self.DB_HOST}:{self.DB_PORT}/?charset={self.DB_CHARSET}"
        )

    @property
    def llm_key_shape(self) -> str:
        """粗判 `LLM_API_KEY` 的形状，用于给出**可操作**的配置提示。

        返回值：
          - `ok`                   形如 `sk-xxx`，正常
          - `missing`              未填写
          - `looks_like_url`       填成了接口地址（最常见的误填）
          - `unexpected_prefix`    有值但不是 `sk-` 开头（可能是别的平台密钥）
        """
        key = (self.LLM_API_KEY or "").strip()
        if not key:
            return "missing"
        if key.startswith(("http://", "https://")):
            return "looks_like_url"
        if not key.startswith("sk-"):
            return "unexpected_prefix"
        return "ok"

    @property
    def llm_configured(self) -> bool:
        """LLM 是否可用（未配置/明显填错时 Agent 走降级路径，R-AG-01）。

        ⚠ `looks_like_url` 也判为不可用 —— 与其发一个注定 401 的请求，
           不如直接给出「密钥字段填成了地址」这种可操作的提示。
        """
        return self.llm_key_shape in ("ok", "unexpected_prefix")

    @property
    def llm_config_hint(self) -> str | None:
        """配置问题的可操作提示（供 /agent/status 与降级文案使用）。"""
        shape = self.llm_key_shape
        if shape == "missing":
            return ("未配置 LLM_API_KEY。请在 backend/.env 中填入阿里云百炼（DashScope）"
                    "的 API Key（形如 sk-xxxxxxxx），然后重启服务。")
        if shape == "looks_like_url":
            return ("LLM_API_KEY 填成了接口地址。该字段只放 sk- 开头的密钥；"
                    "接口地址请填到 LLM_BASE_URL。")
        if shape == "unexpected_prefix":
            return ("LLM_API_KEY 不是 sk- 开头，可能不是阿里云百炼的密钥，"
                    "调用时可能返回 401。")
        return None


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()


# ================================================================ 常量枚举
EVENT_TYPES = [
    "login",
    "coupon_receive",
    "order_create",
    "order_pay",
    "after_sale_apply",
    "logistics_complaint",
]

RULE_CATEGORIES = ["订单欺诈", "支付风险", "账户风险", "售后滥用", "地址风险", "物流风险"]
RISK_LEVELS = ["低", "中", "高", "极高"]
RULE_ACTIONS = ["通过", "标记", "人工审核", "拒绝"]

DECISIONS = ["Pass", "Mark", "Challenge", "Review", "Reject"]
CASE_STATUSES = ["待审核", "审核中", "已处置", "已关闭", "已归档"]
CASE_CONCLUSIONS = ["放行", "拦截", "拉黑"]

LIST_TYPES = ["black", "white", "gray"]
ENTITY_TYPES = ["user", "phone", "ip", "device", "address"]

FEATURE_DIMENSIONS = ["用户", "订单", "地址", "团伙"]

# 条件必填字段（SPEC §1.4 字段校验规则）
REQUIRE_DEVICE_TYPES = ["login", "order_create", "order_pay", "coupon_receive"]
REQUIRE_BIZ_NO_TYPES = ["coupon_receive", "order_create", "order_pay",
                        "after_sale_apply", "logistics_complaint"]

# ---------------------------------------------------------------- 演示账号（SPEC §10.1）
DEMO_USERS = [
    {"user_id": "U-0001", "username": "reviewer",   "password": "reviewer@123",   "display_name": "张审核", "role": "reviewer"},
    {"user_id": "U-0002", "username": "strategist", "password": "strategist@123", "display_name": "李策略", "role": "strategist"},
    {"user_id": "U-0003", "username": "analyst",    "password": "analyst@123",    "display_name": "赵分析", "role": "analyst"},
    {"user_id": "U-0004", "username": "admin",      "password": "admin@123",      "display_name": "王管理", "role": "admin"},
    {"user_id": "U-0005", "username": "ops",        "password": "ops@123",        "display_name": "赵运维", "role": "ops"},
]

# ---------------------------------------------------------------- 特征窗口（秒）
FEATURE_WINDOWS = {
    "device_account_cnt_24h": 24 * 3600,
    "device_order_cnt_1h": 1 * 3600,
    "device_coupon_cnt_1h": 1 * 3600,
    "ip_account_cnt_24h": 24 * 3600,
    "ip_order_cnt_1h": 1 * 3600,
    "addr_account_cnt_7d": 7 * 86400,
    "phone_refund_rate_24h": 24 * 3600,
    "user_event_cnt_1h": 1 * 3600,
}
