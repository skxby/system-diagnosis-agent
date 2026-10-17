"""数据库连接与会话。

阶段 02 起，所有业务读写都必须走 repository 函数并强制带 project_id；
这里只提供 engine / session / Base，不做任何业务判断。
"""

from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings

settings = get_settings()

engine = create_engine(
    settings.database_url,
    # 连接被中间层掐断后自动重建，避免长跑的 worker 拿到死连接
    pool_pre_ping=True,
    connect_args={"connect_timeout": 5},
    # 池耗尽时最多等 30s，然后明确报错，而不是无限挂住
    pool_timeout=30,
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    """所有 ORM 模型的基类（阶段 02 起使用）。"""


def get_db() -> Generator[Session, None, None]:
    """FastAPI 依赖注入用的会话工厂。

    **请求成功即提交，异常即回滚。**

    为什么必须在这里 commit：路由函数只 `flush()`（为了让自增 id 立刻可用），
    若依赖退出时只 `close()`，事务会被丢弃 —— 下一个请求在**新会话**里读不到
    刚写入的数据（表现为"注册成功但立刻 401"），而且这个错误只在跨请求时
    才暴露，单会话测试根本发现不了。

    回滚是刻意的：请求中途失败时不要把半截写入留在库里。
    """
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
