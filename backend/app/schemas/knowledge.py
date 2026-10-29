"""知识库 DTO（v2.md §34 知识库、RAG 检索与入库）。

这些 DTO 是前后端契约的唯一依据（管理员维护知识 + Agent RAG 检索共用）。

命名约定：DTO 主键叫 document_id，ORM to_dict() 里叫 id（Service 层负责改名）。
"""

from pydantic import BaseModel, Field

from app.schemas.common import UtcDatetime


class KnowledgeCreate(BaseModel):
    """POST /admin/knowledge：新增知识文档。

    默认存成**草稿**（不检索）：新知识要显式发布才进向量库。
    `publish=true` 表示"创建完立刻切分 + 向量化 + 发布"，适合批量导入脚本。
    """

    title: str = Field(..., min_length=1, max_length=255)
    content: str = Field(..., min_length=1)
    category: str | None = Field(default=None, max_length=64)
    source: str | None = Field(default=None, max_length=255, description="来源文件名，如 refund_policy.md")
    publish: bool = Field(
        default=False, description="true = 创建后立即切分 + 向量化并发布（默认只存草稿）"
    )


class KnowledgeUpdate(BaseModel):
    """PATCH /admin/knowledge/{id}：部分更新知识文档。

    注意：**改了 content 会退回草稿并作废旧向量**，需要重新发布
    （改正文却不重算向量，检索就会命中过时知识）。
    发布/下线请用 publish / offline 接口，不要直接改 status。
    """

    title: str | None = Field(default=None, min_length=1, max_length=255)
    content: str | None = Field(default=None, min_length=1)
    category: str | None = Field(default=None, max_length=64)
    status: str | None = Field(
        default=None,
        max_length=16,
        description="DRAFT / ENABLED / DISABLED（推荐用 publish、offline 接口）",
    )


class KnowledgeInfo(BaseModel):
    """知识文档 DTO（列表与详情共用）。

    version   版本号：正文每变更一次 +1（知识变更留痕）
    status    DRAFT 草稿 / ENABLED 已发布（可检索）/ DISABLED 已下线
    chunk_count 已生成的分块数 —— 0 说明还没发布过
    """

    document_id: int
    title: str
    content: str
    category: str | None = None
    source: str | None = None
    version: str
    status: str
    chunk_count: int = Field(default=0, ge=0)
    created_at: UtcDatetime
    updated_at: UtcDatetime


class KnowledgeListResponse(BaseModel):
    """GET /admin/knowledge：知识文档分页列表出参。"""

    items: list[KnowledgeInfo] = Field(default_factory=list)
    total: int = Field(..., ge=0)
    limit: int = Field(..., ge=0)
    offset: int = Field(..., ge=0)


class KnowledgeSearchRequest(BaseModel):
    """POST /knowledge/search：RAG 检索请求（默认召回 4 条，与 Agent 的 top_k 一致）。"""

    query: str = Field(..., min_length=1)
    top_k: int = Field(default=4, ge=1, le=20)


class KnowledgeSearchHit(BaseModel):
    """RAG 检索命中项 —— 字段与 AgentResult.references 的每一项对齐。"""

    title: str | None = None
    source: str | None = None
    category: str | None = None
    content: str
    score: float = Field(..., description="余弦相似度，越大越相关")


class KnowledgeIngestResponse(BaseModel):
    """POST /admin/knowledge/ingest：从 knowledge/*.md 批量入库的执行结果。"""

    documents: int = Field(..., ge=0)
    chunks: int = Field(..., ge=0)
