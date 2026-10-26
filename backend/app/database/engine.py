"""SQLAlchemy 引擎与表结构读取。"""
import logging
import re
import threading

from sqlalchemy import Engine, create_engine, inspect, text
from sqlalchemy.engine import URL

from app.config import settings
from app.database.datasource_config import ExternalDataSource, get_datasource_by_id
from app.schemas import ColumnInfo, TableInfo

logger = logging.getLogger(__name__)

# 通用 SQL 类型到 SQLite 展示类型的映射（保证 /api/schema 输出确定性）
_TYPE_MAP = {
    "VARCHAR": "TEXT",
    "STRING": "TEXT",
    "TEXT": "TEXT",
    "CHAR": "TEXT",
    "INTEGER": "INTEGER",
    "BIGINT": "INTEGER",
    "SMALLINT": "INTEGER",
    "FLOAT": "REAL",
    "REAL": "REAL",
    "DOUBLE": "REAL",
    "NUMERIC": "REAL",
    "DECIMAL": "REAL",
    "BOOLEAN": "INTEGER",
    "DATETIME": "TEXT",
    "DATE": "TEXT",
    # ---- MySQL 特有类型 ----
    "TINYINT": "INTEGER",
    "MEDIUMINT": "INTEGER",
    "INT": "INTEGER",
    "BOOL": "INTEGER",
    "YEAR": "INTEGER",
    "BIT": "INTEGER",
    "ENUM": "TEXT",
    "SET": "TEXT",
    "JSON": "TEXT",
    "TIMESTAMP": "TEXT",
    "TIME": "TEXT",
    "BLOB": "TEXT",
    "TINYBLOB": "TEXT",
    "MEDIUMBLOB": "TEXT",
    "LONGBLOB": "TEXT",
    "TINYTEXT": "TEXT",
    "MEDIUMTEXT": "TEXT",
    "LONGTEXT": "TEXT",
    "BINARY": "TEXT",
    "VARBINARY": "TEXT",
    # ---- PostgreSQL 特有类型 ----
    "SERIAL": "INTEGER",
    "BIGSERIAL": "INTEGER",
    "SMALLSERIAL": "INTEGER",
    "INT2": "INTEGER",
    "INT4": "INTEGER",
    "INT8": "INTEGER",
    "JSONB": "TEXT",
    "UUID": "TEXT",
    "TIMESTAMPTZ": "TEXT",
    "BYTEA": "TEXT",
    "CITEXT": "TEXT",
    "INTERVAL": "TEXT",
    "MONEY": "REAL",
    "DOUBLE PRECISION": "REAL",
    "TIMESTAMP WITHOUT TIME ZONE": "TEXT",
    "TIMESTAMP WITH TIME ZONE": "TEXT",
    "TIME WITHOUT TIME ZONE": "TEXT",
    "TIME WITH TIME ZONE": "TEXT",
    "CHARACTER VARYING": "TEXT",
}

# 引擎缓存：按数据源键控复用，避免每次请求重复 create_engine。
# 会被事件循环线程与工具线程同时访问（/api/query 是同步 def，跑在线程池里），加锁保护。
_ENGINES: dict[str, Engine] = {}
_ENGINES_LOCK = threading.Lock()

# 数据源 type → SQLAlchemy drivername
_DRIVERNAME = {
    "mysql": "mysql+pymysql",
    "postgresql": "postgresql+psycopg2",
}

# 数据源 type → sqlglot 方言名（注意：sqlglot 用 "postgres"，不是 "postgresql"）
_TYPE_TO_DIALECT = {
    "sqlite": "sqlite",
    "upload": "sqlite",
    "doc": "sqlite",
    "mysql": "mysql",
    "postgresql": "postgres",
}


def _normalize_type(raw_type: str) -> str:
    """把 SQLAlchemy 报告的列类型归一化为 SQLite 展示类型。"""
    upper = (raw_type or "").upper()
    base = upper.split("(")[0].strip()  # 去掉长度/精度修饰，如 VARCHAR(255) -> VARCHAR
    return _TYPE_MAP.get(base, upper)


def _engine_kwargs(ds_type: str) -> dict:
    """返回各方言的 create_engine 差异参数。"""
    if ds_type == "sqlite":
        return {"connect_args": {"check_same_thread": False}}
    if ds_type == "mysql":
        return {
            "connect_args": {"charset": "utf8mb4"},
            "pool_pre_ping": True,
            "pool_recycle": 3600,
        }
    if ds_type == "postgresql":
        return {"pool_pre_ping": True}
    return {}


def _build_db_url(ds: ExternalDataSource) -> URL:
    """用 URL.create 构造外部数据源连接串（避免密码含特殊字符时拼接出错）。"""
    query = {"charset": "utf8mb4"} if ds.type == "mysql" else {}
    return URL.create(
        drivername=_DRIVERNAME[ds.type],
        username=ds.user,
        password=ds.password,
        host=ds.host,
        port=ds.port,
        database=ds.database,
        query=query,
    )


def resolve_data_source_type(data_source_id: str) -> str:
    """返回数据源类型（upload / doc / mysql / postgresql）；未知抛 ValueError。"""
    if data_source_id.startswith("upload:"):
        return "upload"
    if data_source_id.startswith("doc:"):
        return "doc"
    ds = get_datasource_by_id(data_source_id)
    if ds is None:
        raise ValueError(f"未知数据源：{data_source_id}")
    return ds.type


def parse_doc_id(data_source_id: str) -> tuple[str, str | None]:
    """解析 doc 数据源 id：``doc:<doc_id>`` → ``(doc_id, None)``；
    ``doc:<doc_id>:<table_name>`` → ``(doc_id, table_name)``（按块寻址）。"""
    rest = data_source_id[len("doc:"):]
    if ":" in rest:
        doc_id, _, table_name = rest.partition(":")
        return doc_id, table_name or None
    return rest, None


def _resolve_doc_table(engine: Engine, doc_id: str, segment: str | None) -> str | None:
    """把 doc 数据源第三段解析为真实表名（供块 id 寻址 ``doc:<doc_id>:<块id>``）。

    - ``None`` → ``None``（整篇文档）
    - 已是**本文档**提取出的表名（形如 ``doc_<doc_id>_<idx>``）→ 原样返回
    - 否则视为块 id，经 ``get_doc_table_by_block`` 映射到表名；查不到返回 ``None``

    表名合法性只认「本文档提取出的表」：早前用 ``inspect(engine).get_table_names()`` 判断，
    那是**整个 docs.db** 的表名，会放行 ``doc:<文档A>:doc_<文档B>_3`` 读到别的文档的数据，
    连内部表 ``doc_meta`` / ``doc_blocks`` 也可寻址。
    """
    if segment is None:
        return None
    # 延迟 import 避免与 document_service 的循环依赖
    from app.services.document_service import get_doc_table_by_block, get_doc_tables

    if segment in get_doc_tables(doc_id):
        return segment
    return get_doc_table_by_block(doc_id, segment)


# 问句中的块 id，用于多表文档按块定位。
# 标记形态（与 document_service._BLOCK_MARKER 同源的写法，如「=== 3430815b5ad7 ===」）
_BLOCK_MARKER_RE = re.compile(r"===\s*([^=\s]{1,64})\s*===")
# 裸串形态：赛题数据的块 id 为 12~16 位十六进制
_BLOCK_ID_RE = re.compile(r"[0-9a-f]{12,16}", re.IGNORECASE)


def extract_block_id(question: str, doc_id: str | None = None) -> str | None:
    """从问句中提取块 id，用于多表文档按块定位。

    匹配面刻意收窄：``[0-9a-f]{12,16}`` 会命中订单号、哈希等**任何**普通十六进制串，
    直接拿去定位就可能把问题落到不相干的块上。故：

    1. 优先认 ``=== <id> ===`` 标记形态 —— 这是文档里的真实写法，不会误判；
    2. 裸十六进制串只作候选，必须**确实对应该文档的某个表格块**才采用；
    3. 拿不到 ``doc_id``（无从校验）时返回 None，宁可退回全局/RAG 兜底也不猜。

    （原在 ``agents/graph.py``，下沉到这里是为了让 ``build_source_schema_text``
    这条共用入口能在不 import ``graph`` 的前提下用它 —— ``graph`` 反向依赖
    ``services.query_service``，engine 再 import graph 会成环。）
    """
    q = question or ""
    m = _BLOCK_MARKER_RE.search(q)
    if m:
        return m.group(1)
    if not doc_id:
        return None
    # 延迟 import 避免与 document_service 的循环依赖
    from app.services.document_service import get_doc_table_by_block

    for cand in _BLOCK_ID_RE.findall(q):
        if get_doc_table_by_block(doc_id, cand):
            return cand
    return None


def locate_doc_tables(data_source_id: str, question: str, table_names: list[str]) -> list[str]:
    """文档源：把问句定位到该文档提取出的具体表格，返回真实表名列表（定位不到返回空）。

    文档常被提取成几十到几百张表（赛题 Multi-step 是 48 张、Table_Query 是 500 张）。
    若把这些表的 schema **全部**写进 prompt，模型面对一堆列同构、名字无语义的表
    （真实表名是 ``doc_<doc_id>_<idx>``）无从选择，text2sql 与统计算子都必然选错表 ——
    这是文档问答答不准的头号原因，收窄后 prompt 只剩候选表。

    两级定位，精确优先：

    1. 题干直接带块 id（``extract_block_id``，如「=== 3430815b5ad7 ===」或确实
       对应本文档某表格的十六进制串）→ 直接映射到该块的表；
    2. 否则用向量检索召回 top-k 块，把其中的**表格块**映射回表名（正文块天然
       映射不到表，跳过即可）。

    返回空表示「没能定位」，由调用方回退全量 schema —— 最坏情况不劣于改造前，
    不阻断主链路（与项目「查询链路不设规则兜底」的降级口径一致）。

    **同步函数**：内部含 embedding HTTP + Milvus 往返，在 async 上下文里调用方
    必须自行 ``asyncio.to_thread``（``agents/graph.py`` 的 ``_locate_doc_tables``
    就是那层薄壳）。``/api/query`` 跑在 Starlette 线程池里，可直接同步调用。
    """
    # 延迟 import 避免与 document_service 的循环依赖
    from app.services.document_service import get_doc_table_by_block, retrieve_doc_scored

    doc_id, block_table = parse_doc_id(data_source_id)
    if not doc_id or block_table:
        return []  # 非文档源，或已是块级数据源（resolve_table_names 早已收窄到单表）

    allowed = set(table_names)  # 只认本数据源的表，避免把别的文档/上传库的表带进来

    # 第一级：题干里的块 id（精确）
    block_id = extract_block_id(question, doc_id)
    if block_id:
        t = get_doc_table_by_block(doc_id, block_id)
        if t:
            return [t] if t in allowed else []

    # 第二级：向量语义召回（粗）。用带阈值的版本：定位错表比不定位更糟 —— 收窄后
    # 的 schema 会让模型**确定地**用错表，而没有相关内容时回退全量至少还有选择的余地。
    try:
        chunks = retrieve_doc_scored(doc_id, question, settings.doc_top_k)
    except Exception:
        # Milvus / embedding 不可用：定位不了就交给调用方回退全量，不让整条查询挂掉
        logger.warning("文档块定位检索失败，将回退全量表结构：%s", data_source_id, exc_info=True)
        return []

    located: list[str] = []
    seen: set[str] = set()
    for c in chunks:
        # 同一张表在文档里常被复制成多个块（赛题 Multi-step 实测：48 个块去重后
        # 只有 23 张表，咖啡表 4 连、能源表 5 连，副本内容逐字相同）。不去重就会把
        # 4 张同构表一起交给模型 —— 它可能分别聚合再把结果相加，数值直接翻倍。
        # 副本内容字符串逐字一致，按它去重即可，不必比对几千行的表本身。
        content = str(c.get("content") or "")
        if content in seen:
            continue
        t = get_doc_table_by_block(doc_id, str(c.get("block_id") or "").strip())
        if t and t in allowed and t not in located:
            located.append(t)
            seen.add(content)  # 只在确实映射到表时记账，正文块不占用去重集合
    return located


# 外部数据源的缓存键前缀：upload / doc 用固定键 "uploads" / "docs"，
# 若外部连接恰好也叫这两个名字（id 可由保存连接时指定），会命中同一个键而**静默查错库**。
_EXTERNAL_KEY_PREFIX = "ext:"


def external_engine_key(ds_id: str) -> str:
    """外部数据源的引擎缓存键（统一加前缀，避免与 upload/doc 的固定键相撞）。"""
    return f"{_EXTERNAL_KEY_PREFIX}{ds_id}"


def _get_or_create_engine(key: str, url: str | URL, connect_kwargs: dict | None = None) -> Engine:
    """按 key 复用引擎；不存在则创建。connect_kwargs 为各方言差异参数。"""
    with _ENGINES_LOCK:
        # 锁内 check-then-set：并发首访否则会各自建一个 Engine，落败的那个被丢弃
        # （create_engine 是惰性的，不会泄漏连接，但白建一个连接池没有意义）
        if key not in _ENGINES:
            _ENGINES[key] = create_engine(url, **(connect_kwargs or {}))
        return _ENGINES[key]


def build_external_engine(ds: ExternalDataSource) -> Engine:
    """由 ExternalDataSource 构造一个未缓存的引擎（供连接测试等一次性场景）。"""
    return create_engine(_build_db_url(ds), **_engine_kwargs(ds.type))


def invalidate_engine(key: str) -> None:
    """淘汰并释放某数据源的缓存引擎（连接配置变更/删除后调用）。"""
    with _ENGINES_LOCK:
        eng = _ENGINES.pop(key, None)
    if eng is not None:
        eng.dispose()


def dispose_all_engines() -> None:
    """释放所有缓存引擎的连接池（应用关停时调用）。"""
    with _ENGINES_LOCK:
        engines = list(_ENGINES.values())
        _ENGINES.clear()
    for eng in engines:
        try:
            eng.dispose()
        except Exception:
            logger.warning("关停时释放引擎失败，已跳过。", exc_info=True)


def get_engine(data_source_id: str = "") -> Engine:
    """返回数据源对应的 SQLAlchemy 引擎（惰性，不实际连接）。

    - ``upload:<表名>``：上传 Excel/CSV 导入的库（data/uploads.db）
    - 外部数据源 id：datasources.json 配置的 MySQL/PostgreSQL
    """
    if data_source_id.startswith("upload:"):
        table_name = data_source_id[len("upload:"):]
        if not table_name:
            raise ValueError(f"未知数据源：{data_source_id}")
        engine = _get_or_create_engine("uploads", settings.uploads_db_url, _engine_kwargs("sqlite"))
        if table_name not in inspect(engine).get_table_names():
            raise ValueError(f"未知数据源：{data_source_id}")
        return engine

    if data_source_id.startswith("doc:"):
        doc_id, table_name = parse_doc_id(data_source_id)
        if not doc_id:
            raise ValueError(f"未知数据源：{data_source_id}")
        engine = _get_or_create_engine("docs", settings.docs_db_url, _engine_kwargs("sqlite"))
        try:
            with engine.connect() as conn:
                row = conn.execute(
                    text("SELECT 1 FROM doc_meta WHERE doc_id = :d"), {"d": doc_id}
                ).first()
        except Exception:
            raise ValueError(f"未知数据源：{data_source_id}")
        if row is None:
            raise ValueError(f"未知数据源：{data_source_id}")
        if table_name is not None and _resolve_doc_table(engine, doc_id, table_name) is None:
            raise ValueError(f"未知数据源：{data_source_id}")
        return engine

    ds = get_datasource_by_id(data_source_id)
    if ds is None:
        raise ValueError(f"未知数据源：{data_source_id}")
    return _get_or_create_engine(external_engine_key(ds.id), _build_db_url(ds), _engine_kwargs(ds.type))


def get_dialect(data_source_id: str = "") -> str:
    """返回数据源对应的 sqlglot 方言名（sqlite / mysql / postgres）；未知兜底 sqlite。"""
    try:
        return _TYPE_TO_DIALECT.get(resolve_data_source_type(data_source_id), "sqlite")
    except ValueError:
        return "sqlite"


def get_schema_info(engine: Engine) -> list[TableInfo]:
    """读取库中所有表的列信息。"""
    insp = inspect(engine)
    tables: list[TableInfo] = []
    for table_name in insp.get_table_names():
        columns = [
            ColumnInfo(name=col["name"], type=_normalize_type(str(col["type"])))
            for col in insp.get_columns(table_name)
        ]
        tables.append(TableInfo(name=table_name, columns=columns))
    return tables


def resolve_table_names(data_source_id: str) -> list[str]:
    """返回数据源对应的目标表名列表（供 schema 咨询等按表确定性回答）。

    - ``upload:<表>`` → ``[表]``
    - ``doc:<doc_id>:<表>`` → ``[表]``（按块寻址）
    - ``doc:<doc_id>`` → 该文档提取出的全部表
    - 外部数据源 → 库中全部表（连接失败返回空列表）
    """
    if data_source_id.startswith("upload:"):
        table = data_source_id[len("upload:"):]
        return [table] if table else []
    if data_source_id.startswith("doc:"):
        doc_id, table_name = parse_doc_id(data_source_id)
        if table_name:
            engine = _get_or_create_engine("docs", settings.docs_db_url, _engine_kwargs("sqlite"))
            resolved = _resolve_doc_table(engine, doc_id, table_name)
            return [resolved] if resolved else []
        # 延迟 import 避免与 document_service 的循环依赖
        from app.services.document_service import get_doc_tables

        return get_doc_tables(doc_id)
    try:
        engine = get_engine(data_source_id)
        return inspect(engine).get_table_names()
    except Exception:
        return []


# 命名推断的自引用词干白名单（仅这些列名才会被推断为指向本表的外键）
_SELF_REF_STEMS = {"superior", "parent", "manager", "lead", "ref", "self"}


def _match_table(stem: str, table_by_lower: dict[str, str]) -> str | None:
    """词干匹配表名（同名，或加 s/es 的复数形式），返回实际表名或 None。"""
    if stem in table_by_lower:
        return table_by_lower[stem]
    for suffix in ("s", "es"):
        if stem + suffix in table_by_lower:
            return table_by_lower[stem + suffix]
    return None


def _pick_pk(table: str, stem: str, pks: list[str]) -> str:
    """从目标表主键中挑出最可能被引用的列（优先 id / {stem}_id，否则取首个）。"""
    for pk in pks:
        if pk.lower() in ("id", f"{stem}_id"):
            return pk
    return pks[0]


# 无主键时的候选引用列名（小写比较）：先看通用 id，再看按词干推出的 `{stem}_id`，最后退化取首列
_FALLBACK_REF_COLUMNS = ("id", "编号", "序号")


def _pick_ref_column(insp, target: str, stem: str, pk_map: dict[str, list[str]]) -> str | None:
    """挑出被引用的列：优先目标表主键，无主键时退化到 ``id`` 类列名，最后取首列。

    upload/doc 表由 ``df.to_sql(..., index=False)`` 建出，**既无主键也无外键**。
    早前「目标表无主键就跳过」使命名推断在这两类源上恒为空 ——
    赛题中级要求「自动识别关联关系」在文档多表数据源上完全失效
    （外部 MySQL/PostgreSQL 有真主键，故只有它跑得起来）。
    """
    pks = pk_map.get(target) or []
    if pks:
        return _pick_pk(target, stem, pks)
    cols = [c["name"] for c in insp.get_columns(target)]
    if not cols:
        return None
    lowered = {c.lower(): c for c in cols}
    for cand in ("id", f"{stem}_id", f"{stem}id", *_FALLBACK_REF_COLUMNS):
        if cand in lowered:
            return lowered[cand]
    return cols[0]  # 退化：无主键可依据，取首列（仅作 JOIN 提示，不强约束）


def _infer_relationships(insp, tables: list[str], pk_map: dict[str, list[str]]) -> list[dict]:
    """命名推断关联：列 ``*_id`` → 词干 → 匹配同名/复数表。

    主键列本身不作为外键推断；自引用（列词干为 _SELF_REF_STEMS 之一，如 superior/parent）
    仅当无同名表匹配时指向本表。引用列由 ``_pick_ref_column`` 决定（无主键时退化）。
    仅作提示、不强约束。
    """
    rels: list[dict] = []
    table_by_lower = {t.lower(): t for t in tables}
    for table in tables:
        pks = {p.lower() for p in pk_map.get(table, [])}
        for col in insp.get_columns(table):
            cl = col["name"].lower()
            if not cl.endswith("_id"):
                continue
            if cl in pks:
                continue  # 主键本身不作为外键推断
            stem = cl[:-3]
            if not stem:
                continue
            target = _match_table(stem, table_by_lower)
            if target is None:
                if stem in _SELF_REF_STEMS:
                    target = table  # 白名单词干 → 自引用
                else:
                    continue
            if target == table and stem not in _SELF_REF_STEMS:
                continue
            to_col = _pick_ref_column(insp, target, stem, pk_map)
            if to_col is None:
                continue  # 目标表没有任何列，无法确定引用列
            rels.append({
                "from_table": table,
                "from_column": col["name"],
                "to_table": target,
                "to_column": to_col,
                "kind": "inferred",
            })
    return rels


def get_relationships(engine: Engine, only_tables: list[str] | None = None) -> list[dict]:
    """返回库中的表关联关系：``[{from_table,from_column,to_table,to_column,kind}]``。

    Pass A 读显式外键（``inspect.get_foreign_keys``）；Pass B 对缺失显式外键的表
    （如 pandas 导入的 SQLite 上传表、文档表）做命名推断。二者去重。
    """
    insp = inspect(engine)
    tables = insp.get_table_names()
    if only_tables is not None:
        only = set(only_tables)
        tables = [t for t in tables if t in only]

    rels: list[dict] = []
    seen: set[tuple] = set()

    # Pass A：显式外键
    for table in tables:
        for fk in insp.get_foreign_keys(table):
            referred_table = fk.get("referred_table")
            if not referred_table:
                continue
            constrained = fk.get("constrained_columns") or []
            referred = fk.get("referred_columns") or []
            for c, rc in zip(constrained, referred):
                key = (table, c, referred_table, rc)
                if key in seen:
                    continue
                seen.add(key)
                rels.append({
                    "from_table": table,
                    "from_column": c,
                    "to_table": referred_table,
                    "to_column": rc,
                    "kind": "fk",
                })

    # Pass B：命名推断（与显式外键去重）
    pk_map = {t: insp.get_pk_constraint(t).get("constrained_columns", []) for t in tables}
    for rel in _infer_relationships(insp, tables, pk_map):
        key = (rel["from_table"], rel["from_column"], rel["to_table"], rel["to_column"])
        if key in seen:
            continue
        seen.add(key)
        rels.append(rel)

    return rels


def build_scoped_schema_text(engine: Engine, data_source_id: str) -> str:
    """拼接**仅属于该数据源**的表结构文本（含关联关系注释），供 text2sql / schema 咨询使用。

    upload 源共用同一个 ``uploads.db`` 引擎、doc 源共用一个 ``docs.db``：
    不限定表名就会把**别的数据源**的表一起写进 prompt，text2sql 可能 JOIN 到用户
    并未选中的表（串表）。故这两类源一律按归属过滤（``resolve_table_names``）；
    doc 源还会经 ``doc:<doc_id>:<块 id>`` 进一步收窄到单个块表。

    外部源沿用「读不到表就不限定」的旧行为，避免瞬时反射失败被误当成
    「库里没有表」而给出空 schema。

    **本函数不含取值区**（纯 DDL）。需要取值区的调用方一律走
    ``build_source_schema_text`` —— 它在本函数之上叠加了 doc 块定位与取值区。
    """
    try:
        ds_type = resolve_data_source_type(data_source_id)
    except ValueError:
        ds_type = None
    owned = resolve_table_names(data_source_id)
    if ds_type in ("upload", "doc"):
        only: list[str] | None = owned  # 宁可空 schema 也不串表
    else:
        only = owned or None
    return build_schema_text(engine, only_tables=only)


def build_source_schema_text(engine: Engine, data_source_id: str, question: str = "") -> str:
    """按数据源产出**送进 text2sql 的 schema 文本**（全链路唯一入口）。

    与 ``build_scoped_schema_text`` 的区别有两处，都是「让模型看得见数据」：

    1. **doc 源先收窄**：表数 > 1 时经 ``locate_doc_tables`` 定位到问句真正指向的表
       （原先这段逻辑只写在 ``agents/graph.py._prepare_query_node`` 里，于是
       ``/api/query`` 与 ``/query/stream`` 两条旁路对 doc 源会把该文档**全部几百张表**
       的 DDL 原样喂给模型，模型只能瞎选表）；
    2. **表数有界时附带取值区**：见 ``build_schema_text`` 的 ``with_samples``。

    ``question`` 只在 doc 源定位时用到；非 doc 源传空串即可。
    """
    schema_text: str | None = None
    if data_source_id.startswith("doc:"):
        table_names = resolve_table_names(data_source_id)
        if len(table_names) > 1:
            located = locate_doc_tables(data_source_id, question, table_names)
            if located:
                schema_text = build_schema_text(engine, located, with_samples=True)
            else:
                # 定位失败只降级不阻断；留痕以便区分「检索没命中」与「本来就没有候选表」。
                # 这里用 info 不用 warning：检索没命中是**正常状态**而非降级
                # （与 reconcile_orphan_vectors 的口径一致）。
                logger.info(
                    "文档 %s 未能定位到候选表（共 %d 张），回退全量表结构。",
                    data_source_id,
                    len(table_names),
                )

    if schema_text is None:
        owned = resolve_table_names(data_source_id)
        # 回退全量（doc 源数百张表）**刻意不带取值区**：239 张表各带样本实测多出
        # 约 7.9 万字符（DDL 本身 3.9 万），prompt 直接翻三倍；而块 id / 向量定位
        # 已能覆盖绝大多数情况，回退路径带取值的收益远小于 prompt 爆炸的代价。
        if owned and len(owned) <= settings.schema_max_tables:
            schema_text = build_schema_text(engine, owned, with_samples=True)
        else:
            schema_text = build_scoped_schema_text(engine, data_source_id)
    return schema_text


#: 取值区里单个单元格 / 单个取值的展示长度上限（防止一个超长文本把 prompt 撑爆）
_VALUE_CELL_MAX_CHARS = 120


def _cell_text(value) -> str:
    """把单元格值渲染成**单行**文本（换行折叠为空格、超长截断）。"""
    if value is None:
        return ""
    s = str(value).replace("\r", " ").replace("\n", " ").strip()
    if len(s) > _VALUE_CELL_MAX_CHARS:
        s = s[:_VALUE_CELL_MAX_CHARS] + "…"
    return s


def _table_value_lines(engine: Engine, insp, table_name: str, quote) -> list[str]:
    """产出单张表的取值区注释行（**以 ``--`` 注释行书写**，留在模型已熟悉的 DDL 块内）。

    背景：文档抽出的表常是「行标签 + 数值」的透视表，语义全在某一列的**取值**里
    （如 ``col = 'less : amount representing interest'`` → 73512）。schema 文本早前
    只有 DDL、零取值，模型没见过任何字面量，只能按问题字面猜一个不存在的值 → 必然 0 行。

    - 行数 ≤ ``settings.schema_full_dump_rows`` → 给**全部行**；
    - 否则 → 前 ``settings.schema_sample_rows`` 行，外加**文本列的完整去重取值**
      （上限 ``settings.schema_max_distinct_values`` 个），覆盖「答案行落在样本窗口之外」。

    读表失败由调用方捕获跳过（与 ``schema_service.build_schema_overview`` 同口径），
    不让一张坏表毁掉整份 schema。
    """
    qt = quote(table_name)
    cols = [c["name"] for c in insp.get_columns(table_name)]
    if not cols:
        return []

    with engine.connect() as conn:
        total = int(conn.execute(text(f"SELECT COUNT(*) FROM {qt}")).scalar() or 0)
        if total <= 0:
            return []

        full = total <= settings.schema_full_dump_rows
        limit = total if full else min(settings.schema_sample_rows, total)
        qcols = ", ".join(quote(c) for c in cols)
        rows = conn.execute(text(f"SELECT {qcols} FROM {qt} LIMIT {limit}")).fetchall()

        head = "全部数据" if full else f"样本（前 {limit} 行）"
        lines = [f"-- 表 {qt} 共 {total} 行，{head}："]
        lines.append("-- " + " | ".join(quote(c) for c in cols))
        lines += ["-- " + " | ".join(_cell_text(v) for v in row) for row in rows]

        if full:
            return lines

        # 大表：补文本列的**全部去重取值** —— 答案是某个行标签时，样本窗口通常盖不到它
        for col in insp.get_columns(table_name):
            if _normalize_type(str(col["type"])) != "TEXT":
                continue
            qc = quote(col["name"])
            vals = conn.execute(
                text(
                    f"SELECT DISTINCT {qc} FROM {qt} WHERE {qc} IS NOT NULL "
                    f"LIMIT {settings.schema_max_distinct_values + 1}"
                )
            ).scalars().all()
            # 多取一个来判定「是否超上限」，超了就整个不给（截断的取值清单会误导模型）
            if not vals or len(vals) > settings.schema_max_distinct_values:
                continue
            rendered = ", ".join(_cell_text(v) for v in vals)
            lines.append(f"-- 列 {qc} 的全部取值（共 {len(vals)} 个）：{rendered}")
    return lines


def build_schema_text(
    engine: Engine,
    only_tables: list[str] | None = None,
    with_samples: bool = False,
) -> str:
    """拼接 CREATE TABLE 语句文本（含表关联关系注释），供 text2sql 的 prompt 使用。

    ``with_samples=True`` 时在每张表后追加**取值区**（见 ``_table_value_lines``），
    让模型能写出正确的过滤字面量。两道硬约束：

    - **表数上限** ``settings.schema_max_tables``：超过就整份不附取值（并有日志）。
      这条是防御性的 —— 调用方 ``build_source_schema_text`` 本就不该在有几百张表时
      开取值，这里兜住以免将来某处直接调用把 prompt 撑爆；
    - **字符预算** ``settings.schema_max_chars``：累计超出即停止追加，末尾注明省略了多少，
      并 ``logger.info`` 留痕（静默截断等于骗人）。
    """
    insp = inspect(engine)
    table_names = insp.get_table_names()
    if only_tables is not None:
        only = set(only_tables)
        table_names = [t for t in table_names if t in only]

    # 用方言正确的引号包裹标识符（SQLite/PG 双引号、MySQL 反引号），
    # 使含中文/空格/数字开头的真实表名/列名在 prompt 里可被 text2sql 原样照抄并正确执行。
    quote = engine.dialect.identifier_preparer.quote

    attach = with_samples and bool(table_names)
    if attach and len(table_names) > settings.schema_max_tables:
        logger.info(
            "表数 %d 超过 schema_max_tables=%d，本次不附取值区。",
            len(table_names),
            settings.schema_max_tables,
        )
        attach = False

    statements: list[str] = []
    used_chars = 0
    omitted = 0
    for table_name in table_names:
        pks = set(insp.get_pk_constraint(table_name).get("constrained_columns", []))
        col_defs: list[str] = []
        for col in insp.get_columns(table_name):
            ctype = _normalize_type(str(col["type"]))
            suffix = " PRIMARY KEY" if col["name"] in pks else ""
            col_defs.append(f"  {quote(col['name'])} {ctype}{suffix}")
        ddl = f"CREATE TABLE {quote(table_name)} (\n" + ",\n".join(col_defs) + "\n);"

        if attach:
            try:
                value_lines = _table_value_lines(engine, insp, table_name, quote)
            except Exception:
                # 单表读值失败（表被并发删除、权限不足…）不应毁掉整份 schema
                logger.warning("读取表 %s 的取值失败，该表不附取值区。", table_name, exc_info=True)
                value_lines = []
            if value_lines:
                added = sum(len(ln) + 1 for ln in value_lines)
                if used_chars + added > settings.schema_max_chars:
                    omitted += 1
                else:
                    used_chars += added
                    ddl = ddl + "\n" + "\n".join(value_lines)
        statements.append(ddl)

    if omitted:
        logger.info(
            "取值区字符预算 %d 用尽，%d 张表的取值被省略。",
            settings.schema_max_chars,
            omitted,
        )
        statements.append(f"-- （另有 {omitted} 张表的取值区因超出字符预算已省略）")

    # 追加关联关系提示（JOIN 依据），帮助 text2sql 生成多表连接
    rels = get_relationships(engine, only_tables)
    if rels:
        lines = ["-- 表关联关系（JOIN 依据）"]
        lines += [
            f"-- {quote(r['from_table'])}.{quote(r['from_column'])}"
            f" -> {quote(r['to_table'])}.{quote(r['to_column'])}"
            for r in rels
        ]
        statements.append("\n".join(lines))

    return "\n\n".join(statements)
