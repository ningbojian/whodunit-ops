"""存储层：把业务代码与 Excel / SQLite / Postgres 的具体读写分开。

业务工具继续使用 pandas DataFrame；本包只负责把 DataFrame 按租户存取。
Excel 仍用于本地 CLI、演示和导入导出；Web 生产环境使用 Postgres。
"""

from app.store.base import Store
from app.store.excel import ExcelStore
from app.store.sql import SqlStore
from app.store.workspace import DatabaseWorkspaceManager

__all__ = ["Store", "ExcelStore", "SqlStore", "DatabaseWorkspaceManager"]
