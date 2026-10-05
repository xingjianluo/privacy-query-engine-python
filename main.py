import logging

from app.api import create_app

# 服务入口：输出预算判定、迁移补录等业务日志
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

app = create_app()
