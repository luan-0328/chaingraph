"""轻量 schema 升级：把"模型里新增的列/索引"补到**已存在的**表上。

为什么需要这个文件
------------------
`Base.metadata.create_all()` 只会建**缺失的表**，对已存在的表一个字段都不动。
项目没有引 Alembic（v2.md 明确是单体 FastAPI + MySQL，不引入额外迁移框架），
所以老库升级只能靠"启动时对齐一次"。

于是就有了这里的策略：拿 SQLAlchemy 的 metadata 当"模型真相"，
和数据库里的真实结构做一次差集，缺什么补什么：

    1. 缺的列    → ALTER TABLE ... ADD COLUMN（可空加列 → 回填 → 收紧为 NOT NULL）
    2. 缺的索引  → CREATE INDEX / CREATE UNIQUE INDEX
    3. 新加的非空列在数据库里遗留的 NULL → 按默认值回填

为什么不是"直接 ALTER TABLE ADD COLUMN NOT NULL DEFAULT"
--------------------------------------------------------
MySQL 在 SQL_MODE=STRICT 下，给已有数据的表加 NOT NULL 列如果拿不到值会直接报错；
不同 MySQL 版本对 ADD COLUMN 的默认值处理也不完全一致。
这里用"三步走"（先可空 → UPDATE 回填 → MODIFY 收紧为非空），
在任何 MySQL 8 上都是幂等且安全，而且能顺便把老数据的 NULL 补齐。

边界（明确接受）
----------------
    - 只做"加列 / 加索引"，**不做**改类型、改约束、删列（那是 Alembic 的活）；
    - 只服务单实例启动。多实例同时启动时，重复加列会被 MySQL 以 1060/1061 拒绝，
      这里把这两个错误码当成"已经有人加过了"，不算失败（见 _is_duplicate_error）。
"""

import re

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.schema import CreateTable

from app.core.logging import get_logger

logger = get_logger(__name__)

# MySQL 错误码：重复的列 / 重复的键名（说明另一个实例刚好也加了同一列）
_DUPLICATE_ERRORS = (1060, 1061)

# 老数据回填用的默认值：模型层"新加的非空列"在数据库里遗留 NULL 时用它们补齐。
# 键是"表.列"，值是字面量（字符串按 SQL 字面量处理，数字直接拼）。
_BACKFILL = {
    "tickets.agent_status": "'IDLE'",
    "agent_runs.retry_count": "0",
    "agent_runs.execution_epoch": "0",
    "agent_runs.review_round": "0",
    # alive_at 只是心跳，历史 run 用 started_at / created_at 兜底即可
    "agent_runs.alive_at": "COALESCE(started_at, created_at)",
}

# 取值重命名的历史数据回填（表.列 → [(旧值, 新值)]）。
# 为什么需要：状态枚举改名（AgentRunStatus.PROCESSING → RUNNING）不改数据库结构，
# 但老行里存的还是旧字符串，前端/统计会看到不认识的值。启动时幂等翻译一次。
_RENAMES = {
    "agent_runs.status": [("PROCESSING", "RUNNING")],
}

# 需要把"模型里声明的默认值"补到数据库上的列（表.列）。
#
# 为什么需要单独一张名单：SQLAlchemy 的 `default=` 是**Python 侧**默认值 ——
# 走 ORM 插入时它帮忙填，但绕过 ORM 直接 INSERT（测试造数据、运维手工订正、
# 导入脚本）就会撞上 MySQL 严格模式的 1364 `Field 'x' doesn't have a default value`。
# 这些列都是"TRUE/FALSE 语义"的开关，补一个 DEFAULT 既不改语义，
# 也让底层表在脱离应用层时依然可用。
_SERVER_DEFAULT_COLUMNS = {
    "agent_runs.resumed": "0",
    "agent_runs.used_rag": "0",
    "agent_runs.used_tool": "0",
    "agent_runs.need_human": "0",
}

# 补列之后要顺带检查一遍这些列的 DEFAULT（即使没有新增列也不再走 `if not missing`)
_CHECK_DEFAULT_TABLES = {key.split(".", 1)[0] for key in _SERVER_DEFAULT_COLUMNS}


def sync_schema(engine: Engine) -> list[str]:
    """把模型和数据库对齐，返回实际执行的 DDL/DML 列表（空列表 = 已经是最新的）。"""
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())

    changes: list[str] = []
    changes.extend(_apply_renames(engine, existing_tables))

    # 注意：这里只处理"表已经存在"的情况；新表交给 create_all
    from app.core.database import Base

    for table in Base.metadata.sorted_tables:
        if table.name not in existing_tables:
            continue
        changes.extend(_sync_table(engine, inspector, table))

    if changes:
        logger.info("schema 升级：执行了 %d 条语句", len(changes))
        for statement in changes:
            logger.info("  · %s", statement)
    else:
        logger.info("schema 已是最新（无需升级）")
    return changes


def _apply_renames(engine: Engine, existing_tables: set[str]) -> list[str]:
    """把历史枚举值翻译成新值（幂等：已经是新值就影响 0 行，不算变更）。"""
    applied: list[str] = []
    for key, pairs in _RENAMES.items():
        table_name, column = key.split(".", 1)
        if table_name not in existing_tables:
            continue
        for old_value, new_value in pairs:
            with engine.begin() as conn:
                affected = conn.execute(
                    text(
                        f"UPDATE `{table_name}` SET `{column}` = :new_value "
                        f"WHERE `{column}` = :old_value"
                    ),
                    {"new_value": new_value, "old_value": old_value},
                ).rowcount
            if affected:
                statement = (
                    f"UPDATE `{table_name}` SET `{column}`='{new_value}' "
                    f"WHERE `{column}`='{old_value}'  -- {affected} 行"
                )
                applied.append(statement)
                logger.info("历史枚举值回填：%s", statement)
    return applied


# ======================================================================
# 单表对齐
# ======================================================================
def _sync_table(engine: Engine, inspector, table) -> list[str]:  # noqa: ANN001
    """对齐一张表：补列 → 补默认值 → 补索引/唯一约束 → 补 CHECK。

    顺序是有讲究的，不能随意调换：

        ① 先补列 —— 后面的索引/约束可能引用了新列（例如生成列上的唯一索引）
        ② 再对齐默认值（含名单里的 DEFAULT 补齐）
        ③ 然后建索引/唯一约束 —— 必须等列真的存在
        ④ 最后 CHECK 约束 —— 它引用的是列表达式，同样要等列到位

    "没有新增列就直接 return" 这条提前返回只跳过 ②③④ 里**依赖新列**的部分；
    名单里的 DEFAULT 对齐（`_CHECK_DEFAULT_TABLES`）放在提前返回**之前**，
    否则老库永远补不上那几个默认值 —— 这正是它一开始被漏掉的原因。
    """
    applied: list[str] = []
    actual_columns = {column["name"]: column for column in inspector.get_columns(table.name)}
    missing = [column for column in table.columns if column.name not in actual_columns]
    if missing:
        applied.extend(_add_columns(engine, table, missing))
        # Inspector 会缓存列结构；DDL 后必须失效，否则本次启动会漏建新列的索引。
        inspector.clear_cache()

    # 名单里的列：即使这次没有新增列，也要确认 DEFAULT 补上了（幂等）
    if table.name in _CHECK_DEFAULT_TABLES:
        applied.extend(_ensure_server_defaults(engine, inspector, table))

    if not missing:
        if table.name in ("agent_run_events", "ticket_messages", "agent_runs"):
            # 上次启动可能已加列，但在建幂等索引前中断；再次启动仍需补齐索引。
            applied.extend(_add_indexes(engine, inspector, table, set(actual_columns)))
        return applied

    # 补列之后重新读一次结构：新加的列要在这里**一次性对齐** DEFAULT。
    # 为什么不把"DEFAULT 对齐"推广到所有老列：SQLAlchemy 对 BOOL / 枚举这类列
    # 本来就是 Python 侧填默认值（库里没有 DEFAULT 也照常工作），逐个 ALTER 只会
    # 在每次启动时刷一堆无意义的 DDL。只有**我们刚加的列**才需要补默认值 ——
    # 否则"绕过应用层直接 INSERT"会报 1364 Field doesn't have a default value。
    actual_columns = {column["name"]: column for column in inspector.get_columns(table.name)}
    for column in missing:
        actual = actual_columns.get(column.name)
        if actual is None:
            continue
        if _is_generated(column):
            continue
        if bool(actual["nullable"]) != bool(column.nullable) or _needs_default(column, actual):
            applied.extend(_align_column(engine, table, column))

    applied.extend(
        _add_indexes(engine, inspector, table, set(actual_columns))
    )
    applied.extend(_add_check_constraints(engine, table))
    return applied


def _ensure_server_defaults(engine: Engine, inspector, table) -> list[str]:  # noqa: ANN001
    """给名单里的列补上数据库层 DEFAULT（幂等：已经有就跳过）。

    只做 `ALTER TABLE ... ALTER COLUMN ... SET DEFAULT`，不动类型、不动可空性，
    因此对已有数据完全无影响 —— 只是让"绕过 ORM 的 INSERT"不再报 1364。
    """
    applied: list[str] = []
    actual = {column["name"]: column for column in inspector.get_columns(table.name)}
    for column in table.columns:
        key = f"{table.name}.{column.name}"
        default = _SERVER_DEFAULT_COLUMNS.get(key)
        if default is None:
            continue
        info = actual.get(column.name)
        if info is None or info.get("default") is not None:
            continue
        with engine.begin() as conn:
            _execute(
                conn,
                f"ALTER TABLE `{table.name}` ALTER COLUMN `{column.name}` SET DEFAULT {default}",
                applied,
            )
    return applied


def _needs_default(column, actual: dict) -> bool:  # noqa: ANN001
    """新加的列：模型声明了默认值、但库里没有 → 补上 DEFAULT。

    没有默认值的列（例如 tickets.agent_run_id）不需要处理：
    它们本来就允许 NULL。
    """
    if column.nullable:
        return False
    if actual.get("default") is not None:
        return False
    return (
        getattr(column.server_default, "arg", None) is not None
        or (column.default is not None and getattr(column.default, "is_scalar", False))
    )


def _is_generated(column) -> bool:  # noqa: ANN001
    """是不是数据库生成列（GENERATED ALWAYS AS ... STORED/VIRTUAL）。

    生成列必须特殊对待：它没有 DEFAULT，也不能被 MODIFY 重写表达式
    （MySQL 的列定义顺序是 `类型 AS (expr) STORED [NULL]`，
    而我们拼出来的定义是 `类型 NULL GENERATED ALWAYS AS (...) STORED` ——
    这个顺序 MySQL 会直接语法报错）。反正生成列生来就可空，本来也不需要收紧。
    """
    return getattr(column, "computed", None) is not None


def _add_check_constraints(engine: Engine, table) -> list[str]:  # noqa: ANN001
    """补 CHECK 约束（例如 products.stock >= 0）。

    为什么要单独处理：默认的 MySQL 会**解析但忽略** CHECK（8.0.16 之前），
    所以要显式确认当前实例支不支持 —— 不支持就只记一条警告，
    靠应用层的条件 UPDATE 兜底（那是主要防线，CHECK 是兜底）。
    """
    constraints = [
        constraint
        for constraint in table.constraints
        if constraint.__class__.__name__ == "CheckConstraint" and constraint.name
    ]
    if not constraints:
        return []

    with engine.connect() as conn:
        version = str(conn.execute(text("SELECT VERSION()")).scalar() or "")
        enforced = _mysql_supports_check(version)
        existing = {
            row[0]
            for row in conn.execute(
                text(
                    "SELECT CONSTRAINT_NAME FROM information_schema.table_constraints "
                    "WHERE table_schema = DATABASE() AND table_name = :table_name "
                    "AND constraint_type = 'CHECK'"
                ),
                {"table_name": table.name},
            )
        }

    if not enforced:
        logger.warning(
            "MySQL %s 不强制 CHECK 约束（需要 8.0.16+），%s 的约束只作声明：库存安全依赖应用层的条件 UPDATE",
            version,
            table.name,
        )
        return []

    applied: list[str] = []
    for constraint in constraints:
        if constraint.name in existing:
            continue
        with engine.begin() as conn:
            _execute(
                conn,
                f"ALTER TABLE `{table.name}` ADD CONSTRAINT `{constraint.name}` "
                f"CHECK ({constraint.sqltext})",
                applied,
            )
    return applied


def _mysql_supports_check(version: str) -> bool:
    """MySQL >= 8.0.16 才真正强制 CHECK 约束。"""
    match = re.match(r"(\d+)\.(\d+)\.(\d+)", version)
    if match is None:
        return False
    major, minor, patch = (int(part) for part in match.groups())
    return (major, minor, patch) >= (8, 0, 16)


def _align_column(engine: Engine, table, column) -> list[str]:  # noqa: ANN001
    """把已存在列的定义对齐成模型里的样子（可空性 + DEFAULT）。"""
    applied: list[str] = []
    ddl_type = _column_type(column, table.name)
    with engine.begin() as conn:
        # 收紧为 NOT NULL 之前必须先把 NULL 补掉，否则 ALTER 会失败
        if not column.nullable:
            backfill = _backfill_expression(column, ddl_type, table.name)
            if backfill is not None:
                _execute(
                    conn,
                    f"UPDATE `{table.name}` SET `{column.name}` = {backfill} "
                    f"WHERE `{column.name}` IS NULL",
                    applied,
                )
        _execute(
            conn,
            f"ALTER TABLE `{table.name}` MODIFY COLUMN `{column.name}` "
            f"{_nullability_definition(ddl_type, nullable=bool(column.nullable))}",
            applied,
        )
    logger.info(
        "schema 升级：对齐 %s.%s 的定义（%s）",
        table.name,
        column.name,
        "NULL" if column.nullable else "NOT NULL",
    )
    return applied


def _add_columns(engine: Engine, table, columns: list) -> list[str]:  # noqa: ANN001
    """加列：先可空加列 → 回填 → 非空列收紧为 NOT NULL。

    生成列走单独一条路：表达式由数据库自己算，没有 DEFAULT 也不需要回填，
    直接 `ADD COLUMN ... GENERATED ALWAYS AS (...) STORED` 一步到位。
    """
    applied: list[str] = []
    with engine.begin() as conn:
        for column in columns:
            if _is_generated(column):
                # 生成列一步到位：表达式由数据库自己算，没有 DEFAULT 也不需要回填
                _execute(
                    conn,
                    f"ALTER TABLE `{table.name}` ADD COLUMN `{column.name}` "
                    f"{_generated_column_definition(column)}",
                    applied,
                )
                continue
            ddl_type = _column_type(column, table.name)
            # ① 先以"可空"的形式加进来：无论表里有没有老数据都不会失败
            _execute(
                conn,
                f"ALTER TABLE `{table.name}` ADD COLUMN `{column.name}` {ddl_type} NULL",
                applied,
            )
            # ② 回填（老数据不能留 NULL，否则后续 NOT NULL 收紧会失败）
            backfill = _backfill_expression(column, ddl_type, table.name)
            if backfill is not None:
                _execute(
                    conn,
                    f"UPDATE `{table.name}` SET `{column.name}` = {backfill} "
                    f"WHERE `{column.name}` IS NULL",
                    applied,
                )
            # ③ 对齐可空性（模型说非空就收紧；说可空就保持可空）
            #    注意：不是"一上来就收紧" —— 先可空加列能兼容任何老数据状况
            _execute(
                conn,
                f"ALTER TABLE `{table.name}` MODIFY COLUMN `{column.name}` "
                f"{_nullability_definition(ddl_type, nullable=bool(column.nullable))}",
                applied,
            )
    return applied


def _nullability_definition(ddl_type: str, *, nullable: bool) -> str:
    """把推导出来的列定义拼成带 NULL / NOT NULL 的完整定义。

    两个坑：
        1. NOT NULL 必须写在 DEFAULT 之前，否则 MySQL 直接语法错误；
        2. 推导出来的片段可能自带 `DEFAULT '0'`（server_default），
           所以要先把 DEFAULT 摘出来再按正确顺序拼回去。
    """
    match = re.search(r"\s+DEFAULT\s+.+$", ddl_type, flags=re.IGNORECASE)
    if match is None:
        return f"{ddl_type} {'NULL' if nullable else 'NOT NULL'}"
    base = ddl_type[: match.start()].strip()
    return f"{base} {'NULL' if nullable else 'NOT NULL'}{match.group(0)}"


def _add_indexes(engine: Engine, inspector, table, known_columns: set[str]) -> list[str]:  # noqa: ANN001
    """补索引 / 唯一约束（只比对索引名，够用且不误伤手工建的索引）。"""
    applied: list[str] = []
    try:
        existing = {index["name"] for index in inspector.get_indexes(table.name)}
    except Exception:  # noqa: BLE001 —— 某些方言取不到就跳过，不影响启动
        return applied

    for index in table.indexes:
        if index.name in existing:
            continue
        index_columns = [column.name for column in index.columns if column.name in known_columns]
        if len(index_columns) != len(index.columns):
            continue
        columns_sql = ", ".join(f"`{name}`" for name in index_columns)
        unique = "UNIQUE " if index.unique else ""
        with engine.begin() as conn:
            _execute(
                conn,
                f"CREATE {unique}INDEX `{index.name}` ON `{table.name}` ({columns_sql})",
                applied,
            )

    # UniqueConstraint（例如 agent_runs.thread_id）在 inspector.get_indexes 里不一定出现，
    # 用 information_schema 再确认一次，避免重复建。
    for constraint in table.constraints:
        if constraint.__class__.__name__ != "UniqueConstraint" or not constraint.name:
            continue
        with engine.connect() as conn:
            exists = conn.execute(
                text(
                    "SELECT COUNT(*) FROM information_schema.statistics "
                    "WHERE table_schema = DATABASE() AND table_name = :table_name "
                    "AND index_name = :index_name"
                ),
                {"table_name": table.name, "index_name": constraint.name},
            ).scalar()
        if exists:
            continue
        columns_sql = ", ".join(f"`{column.name}`" for column in constraint.columns)
        with engine.begin() as conn:
            _execute(
                conn,
                f"CREATE UNIQUE INDEX `{constraint.name}` ON `{table.name}` ({columns_sql})",
                applied,
            )
    return applied


# ======================================================================
# 工具
# ======================================================================
def _execute(conn, statement: str, applied: list[str]) -> None:  # noqa: ANN001
    """执行一条 DDL 并记账；失败**不抛**，只记日志。

    为什么故意吞掉异常：迁移里任何一条语句失败都不该拦住应用启动 ——
    最坏情况只是"某个新功能不可用"，而启动失败是"整个系统不可用"。
    两种"可忽略"的情况分开处理，方便运维看日志：

        1060/1061（列/索引已存在）→ debug：这是多实例同时启动的正常竞态
        其它错误                  → error：真的有问题，需要人看
    """
    try:
        conn.execute(text(statement))
        applied.append(statement)
    except Exception as exc:  # noqa: BLE001
        if _is_duplicate_error(exc):
            logger.debug("DDL 已由其他实例执行过，跳过：%s", statement)
            return
        # 迁移失败要吵，但不能拦住应用启动（老库少一列只会让新功能不可用）
        logger.error("schema 升级语句失败：%s → %s", statement, exc)


def _is_duplicate_error(exc: Exception) -> bool:
    """是不是"已经有人建过了"（MySQL 1060 重复列 / 1061 重复键名）。

    多实例同时启动时两边都会尝试加同一列，后到的必然报这个错 —— 属于正常现象。
    """
    code = getattr(getattr(exc, "orig", None), "args", [None])[0]
    return code in _DUPLICATE_ERRORS


def _column_type(column, table_name: str) -> str:  # noqa: ANN001
    """从模型列定义里拿到 MySQL 类型的 DDL 片段。

    直接借用 SQLAlchemy 的 DDL 编译器（`CreateTable` 生成出来的列定义），
    这样 VARCHAR 长度、DECIMAL 精度、JSON、BOOLEAN 这些都和 create_all 完全一致，
    不需要在这里手写一份"类型映射表"再跟模型对不上。

    生成列不走这里（见 _generated_column_definition）：它们的表达式里可能
    合法地含有 `NULL` 字面量，而下面这行 `replace(" NULL", "")` 会把它吃掉 ——
    `ELSE NULL END` 会变成 `ELSE END`，直接语法错误。与其把这段解析改得更聪明，
    不如让生成列从模型定义直接拼 —— 它本来就不需要"从建表语句里反推"。
    """
    compiled = str(CreateTable(column.table).compile(dialect=_mysql_dialect()))
    # 注意：DDL 里的列名带不带反引号取决于标识符是否需要引用（保留字才带），
    # 所以这里用"反引号可选"的正则，而不是硬编码前缀。
    pattern = re.compile(rf"^`?{re.escape(column.name)}`?\s+(?P<definition>.+)$")
    for line in compiled.splitlines():
        match = pattern.match(line.strip().rstrip(","))
        if match is None:
            continue
        definition = match.group("definition")
        definition = definition.replace(" NOT NULL", "").replace(" NULL", "").strip()
        if "AUTO_INCREMENT" in definition:  # 自增列不可能后补，出现即异常
            raise RuntimeError(f"{table_name}.{column.name} 是自增主键，无法通过补列迁移")
        return definition
    raise RuntimeError(f"无法推导列定义：{table_name}.{column.name}")


def _mysql_dialect():  # noqa: ANN202
    from sqlalchemy.dialects import mysql

    return mysql.dialect()


def _generated_column_definition(column) -> str:
    """拼出生成列的完整 DDL 片段（直接来自模型，不经过建表语句反推）。

    形如：`BIGINT GENERATED ALWAYS AS (CASE WHEN ... THEN order_id ELSE NULL END) STORED`

    生成列生来可空、且没有 DEFAULT，所以只需要"类型 + 表达式 + 存储方式"三件事，
    `to_dict()` 里也不该出现它（它是给数据库唯一索引用的，不是业务字段）。
    """
    dialect = _mysql_dialect()
    ddl_type = column.type.compile(dialect=dialect)
    expression = str(column.computed.sqltext)
    storage = "STORED" if getattr(column.computed, "persisted", None) else "VIRTUAL"
    return f"{ddl_type} GENERATED ALWAYS AS ({expression}) {storage}"


def _sql_literal(value) -> str:  # noqa: ANN001
    """把 Python 标量转成 SQL 字面量（回填老数据时拼进 UPDATE 用）。

    只为回填服务，值都来自我们自己的模型默认值（不是用户输入），
    所以这里手工拼串是可接受的；仍然做了单引号转义以防将来被复用。
    """
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return str(value)
    return "'" + str(value).replace("'", "''") + "'"


def _backfill_expression(column, ddl_type: str, table_name: str):  # noqa: ANN001, ANN202
    """老数据回填表达式：新加的非空列在已有行里必须拿到一个值。"""
    key = f"{table_name}.{column.name}"
    if key in _BACKFILL:
        return _BACKFILL[key]
    if column.nullable:
        return None
    if column.default is not None and getattr(column.default, "is_scalar", False):
        return _sql_literal(column.default.arg)
    server_default = getattr(column.server_default, "arg", None)
    if server_default is not None and hasattr(server_default, "text"):
        return server_default.text
    # 兜底：按类型给一个"空值"（布尔/数字给 0，字符串给空串）
    upper = ddl_type.upper()
    if "BOOL" in upper or "INT" in upper or "DECIMAL" in upper or "FLOAT" in upper or "DOUBLE" in upper:
        return "0"
    if "CHAR" in upper or "TEXT" in upper:
        return "''"
    return None
