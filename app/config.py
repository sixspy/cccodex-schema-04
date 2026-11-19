import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class Config:
    LAB_NAME = "契约换版台"
    SECRET = os.environ.get("SECRET_KEY", "cc-schema-dev-secret")
    ADMIN_USER = "admin"
    ADMIN_PASSWORD = "123456"
    DEFAULT_DB = str(ROOT / "instance" / "schema_lab.db")

    # 齐套门槛已拍板：只取“打开即冻结缺边快照”这一边。
    # 另一侧（提交瞬间当场重扫父表缺边）不实现、不混挂；快照一旦冻结，
    # 齐套裁决只读快照，改边后必须显式“重新冻结”。
    GATE_MODE = "snapshot"
    GATE_CHIP = "快照"
    GATE_NOTE = "打开升版卡片即冻结父表缺边快照；齐套按快照裁决，改边后需重新冻结。"

    @classmethod
    def database(cls) -> str:
        return os.environ.get("DATABASE_PATH", cls.DEFAULT_DB)
