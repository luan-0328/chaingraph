"""演示数据初始化（幂等）。

职责
----
把"项目刚拉下来就能演示"所需的最小数据一次性补齐，只做三件事：

    1. 演示账号（管理员 / 客服 / 两个普通用户）
    2. 演示商品（覆盖上下架、零库存、低于和高于自动退款阈值等边界）
    3. 知识文档（把 knowledge/*.md 作为**原始文档**写进 knowledge_documents）

为什么必须幂等
--------------
这个函数会被反复调用：启动脚本、pytest 的 fixture、手工补数据时都会跑。
如果每次无条件 INSERT，users 会直接撞 username 唯一索引报错，
products / knowledge_documents 则会不断堆出重复行，演示环境越跑越脏。
所以策略是"按业务唯一键逐条判断，缺什么补什么"——
username / 商品名 / source 就是各自的业务唯一键。

为什么不 import app.services
----------------------------
services 层 import repositories，而部分 service 又要在模块级依赖演示数据，
seed 再反向 import services 就会形成循环依赖，导入期直接炸。
这里只允许依赖 models / core / repositories。

为什么只写文档、不做切分与向量化
--------------------------------
切分 + embedding 是 RAG 入库服务的职责（对应 v2.md §35 的 RAG 流程）。
如果把向量化塞进 seed，演示数据初始化就被迫依赖 embedding 服务，
没配 EMBEDDING_API_KEY 的环境连初始化都跑不起来——这是没必要的耦合。

事务边界
--------
和 repositories 包内的其它模块保持一致：本函数只 db.flush()，不 commit。
commit 由调用方（core.database.session_scope 或启动脚本）负责，
这样 seed 可以和别的初始化动作放进同一个事务里，失败一起回滚。
"""

from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.core.security import hash_password
from app.core.time import utc_ago, utcnow
from app.models.knowledge import KnowledgeDocument, KnowledgeStatus
from app.models.product import Product, ProductStatus
from app.models.user import Role, User
from app.repositories import knowledge_repository

logger = get_logger(__name__)

# app/repositories/seed.py -> parents[2] == backend/
KNOWLEDGE_DIR = Path(__file__).resolve().parents[2] / "knowledge"

DEFAULT_PASSWORD = "123456"
ADMIN_PASSWORD = "admin123"

# 演示账号。管理员单独用一个更强的口令，是为了让"管理员口令不能等同于普通账号"
# 这条习惯从演示环境就开始养成；其余账号统一 123456，方便前端一键登录切换。
DEMO_USERS: tuple[dict, ...] = (
    {
        "username": "admin",
        "password": ADMIN_PASSWORD,
        "role": Role.ADMIN,
        "nickname": "系统管理员",
        "email": "admin@example.com",
        "phone": "13800000000",
        "created_days_ago": 120,
    },
    {
        "username": "service01",
        "password": DEFAULT_PASSWORD,
        "role": Role.CUSTOMER_SERVICE,
        "nickname": "客服小张",
        "email": "service01@example.com",
        "phone": "13800000001",
        "created_days_ago": 100,
    },
    {
        "username": "alice",
        "password": DEFAULT_PASSWORD,
        "role": Role.USER,
        "nickname": "爱丽丝",
        "email": "alice@example.com",
        "phone": "13800000002",
        "created_days_ago": 60,
    },
    {
        "username": "bob",
        "password": DEFAULT_PASSWORD,
        "role": Role.USER,
        "nickname": "鲍勃",
        "email": "bob@example.com",
        "phone": "13800000003",
        "created_days_ago": 30,
    },
)

# 演示商品。价格刻意跨越 500 元这条自动退款上限：
#     19.90 / 199 / 219 / 259 / 299  -> 命中"可自动退款"
#     899 / 1299 / 3999              -> 必须转人工审批
# 这样一条链路上能把 RefundService 的两个分支都走到，不用手工改数据。
# 库存同理：200（充足）、12（紧张）、0（售罄，下单应报库存不足）。
# 移动电源故意设为 OFF_SALE，用来验证"下架商品不可下单、但仍能在历史订单里查到"。
DEMO_PRODUCTS: tuple[dict, ...] = (
    {
        "name": "机械键盘（客制化三模）",
        "price": "1299.00",
        "stock": 120,
        "status": ProductStatus.ON_SALE,
        "description": "87 键客制化机械键盘，支持有线 / 蓝牙 / 2.4G 三模连接，热插拔轴座，RGB 背光。",
        "created_days_ago": 45,
    },
    {
        "name": "蓝牙耳机（主动降噪）",
        "price": "899.00",
        "stock": 85,
        "status": ProductStatus.ON_SALE,
        "description": "入耳式主动降噪蓝牙耳机，单次续航 8 小时，配充电盒总续航 32 小时。",
        "created_days_ago": 40,
    },
    {
        "name": "保温杯（316 不锈钢）",
        "price": "19.90",
        "stock": 200,
        "status": ProductStatus.ON_SALE,
        "description": "316 不锈钢内胆保温杯，容量 500ml，保温 12 小时，杯盖可拆洗。",
        "created_days_ago": 35,
    },
    {
        # stock=0：库存边界，用来验证下单时的库存校验与"售罄"提示
        "name": "双肩包（商务防泼水）",
        "price": "259.00",
        "stock": 0,
        "status": ProductStatus.ON_SALE,
        "description": "15.6 英寸笔记本商务双肩包，防泼水面料，带独立电脑仓与 USB 充电孔。",
        "created_days_ago": 30,
    },
    {
        "name": "显示器支架（气弹簧双臂）",
        "price": "219.00",
        "stock": 60,
        "status": ProductStatus.ON_SALE,
        "description": "气弹簧双臂显示器支架，适配 17-32 英寸，支持俯仰旋转与理线收纳。",
        "created_days_ago": 25,
    },
    {
        "name": "运动手环（血氧监测）",
        "price": "299.00",
        "stock": 150,
        "status": ProductStatus.ON_SALE,
        "description": "彩色大屏运动手环，支持心率与血氧监测、50 米防水、14 天续航。",
        "created_days_ago": 20,
    },
    {
        # OFF_SALE：下架边界，用来验证商品列表过滤与详情页的"已下架"表现
        "name": "移动电源（20000mAh）",
        "price": "199.00",
        "stock": 40,
        "status": ProductStatus.OFF_SALE,
        "description": "20000mAh 移动电源，支持 22.5W 快充与双向 Type-C，可上飞机。（老款，已下架）",
        "created_days_ago": 15,
    },
    {
        "name": "空气炸锅（嵌入式蒸烤炸一体机）",
        "price": "3999.00",
        "stock": 12,
        "status": ProductStatus.ON_SALE,
        "description": "嵌入式蒸烤炸一体机，5.5L 可视容量，12 种预设菜单，支持 360° 热风循环。",
        "created_days_ago": 10,
    },
)


# ======================================================================
# 内部工具
# ======================================================================
def _count(db: Session, model: type) -> int:
    """统计某张表的当前行数（汇总日志用）。"""
    return int(db.scalar(select(func.count()).select_from(model)) or 0)


def _read_knowledge_files(directory: Path) -> list[tuple[str, str, str]]:
    """扫描 knowledge/*.md，返回 [(source, title, content), ...]。

    title 取文件里第一个一级标题，而不是在代码里再维护一张"文件名 -> 标题"映射表：
    映射表一旦有人改了 md 的标题就会失同步，从正文里直接取永远是对的。
    取不到一级标题时退回文件名（去掉 .md），保证 title 非空——数据库里它是 NOT NULL。
    """
    items: list[tuple[str, str, str]] = []

    for path in sorted(directory.glob("*.md")):
        content = path.read_text(encoding="utf-8")
        title = path.stem

        for line in content.splitlines():
            stripped = line.strip()
            if stripped.startswith("# "):
                title = stripped[2:].strip()
                break

        items.append((path.name, title, content))

    return items


# ======================================================================
# 三块数据
# ======================================================================
def _seed_users(db: Session) -> int:
    """补齐演示账号，返回新增数量。"""
    existing: set[str] = set(db.scalars(select(User.username)).all())

    created = 0
    for item in DEMO_USERS:
        if item["username"] in existing:
            continue

        db.add(
            User(
                username=item["username"],
                password_hash=hash_password(item["password"]),
                nickname=item["nickname"],
                email=item["email"],
                phone=item["phone"],
                role=item["role"],
                # 注册时间往前挪，让"新老用户"在列表排序上看起来有区别
                created_at=utc_ago(days=item["created_days_ago"]),
            )
        )
        created += 1

    db.flush()
    return created


def _seed_products(db: Session) -> int:
    """补齐演示商品，返回新增数量。

    以商品名作为业务唯一键：V1 没有 SKU / 货号概念，
    演示商品名手工维护且不会重名，用 name 判断最省事。
    """
    existing: set[str] = set(db.scalars(select(Product.name)).all())

    created = 0
    for item in DEMO_PRODUCTS:
        if item["name"] in existing:
            continue

        db.add(
            Product(
                name=item["name"],
                price=item["price"],
                stock=item["stock"],
                status=item["status"],
                description=item["description"],
                created_at=utc_ago(days=item["created_days_ago"]),
            )
        )
        created += 1

    db.flush()
    return created


def _seed_knowledge(db: Session) -> int:
    """把 knowledge/*.md 登记成知识文档，返回新增数量。

    已存在同 source 的文档就跳过 —— 不覆盖 content，是为了不把管理员
    在后台改过的知识内容在下次启动时又冲回文件里的版本。
    知识更新走管理端的知识库接口 + RAG 重新入库，不走 seed。
    """
    if not KNOWLEDGE_DIR.is_dir():
        logger.warning("知识库目录不存在，跳过知识文档初始化：%s", KNOWLEDGE_DIR)
        return 0

    created = 0
    for source, title, content in _read_knowledge_files(KNOWLEDGE_DIR):
        if knowledge_repository.get_document_by_source(db, source) is not None:
            continue

        db.add(
            KnowledgeDocument(
                title=title,
                content=content,
                source=source,
                # category 用文件名去后缀：检索时能直接按业务域过滤
                category=source.rsplit(".", 1)[0],
                version="1.0",
                status=KnowledgeStatus.ENABLED,
                # 知识是"刚导入的"，不跟着演示账号一起回拨时间
                created_at=utcnow(),
            )
        )
        created += 1

    db.flush()
    return created


# ======================================================================
# 对外入口
# ======================================================================
def seed_all(db: Session) -> None:
    """初始化全部演示数据（幂等，只 flush 不 commit）。

    调用方示例::

        with session_scope() as db:      # session_scope 负责 commit / rollback
            seed_all(db)

    重复调用只会补齐缺失项，不会产生重复行，也不会把已有数据改回去——
    这一点很关键：演示时手工改过的订单、库存、知识内容不会被下一次初始化擦掉。
    """
    users_created = _seed_users(db)
    products_created = _seed_products(db)
    knowledge_created = _seed_knowledge(db)

    # 日志打的是"表里现在一共有多少"，而不是"这次插了几条"：
    # 幂等函数第二次跑时新增为 0，只有总数才能说明数据到底就绪没有。
    logger.info(
        "演示数据就绪：users=%s products=%s knowledge=%s（本次新增 users=%s products=%s knowledge=%s）",
        _count(db, User),
        _count(db, Product),
        _count(db, KnowledgeDocument),
        users_created,
        products_created,
        knowledge_created,
    )
