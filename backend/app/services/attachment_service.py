"""工单图片：真实解码验证、二次鉴权、私有存储及失败补偿。"""

from datetime import timedelta
from io import BytesIO
import re
import uuid
import warnings

from PIL import Image, UnidentifiedImageError
from sqlalchemy import select, update, or_, and_

from app.core.config import settings
from app.core.database import session_scope
from app.core.exceptions import BusinessError, InvalidStateError, PermissionDeniedError, ValidationError
from app.core.logging import get_logger
from app.core.time import utcnow
from app.models.ticket_attachment import TicketAttachment
from app.models.ticket import HandlingMode
from app.models.ticket_message import SenderType
from app.models.user import Role
from app.repositories import ticket_repository
from app.services.message_read_service import authorize
from app.services.oss_storage import get_storage

logger = get_logger(__name__)
FORMATS = {"PNG": ("image/png", "png"), "JPEG": ("image/jpeg", "jpg"), "WEBP": ("image/webp", "webp")}


def attachment_info(row) -> dict:
    """只返回展示信息，隐藏桶名和对象键，查看图片另走鉴权接口。"""
    return {"attachment_id": row.id, "ticket_id": row.ticket_id, "filename": row.filename,
            "content_type": row.content_type, "size": row.size}


def attach_metadata(db, messages: list[dict]) -> list[dict]:
    """批量装配图片元数据，详情轮询不请求 OSS 或生成签名 URL。"""
    if not messages:
        return messages
    rows = db.scalars(select(TicketAttachment).where(TicketAttachment.message_id.in_([m["id"] for m in messages]),
                                                  TicketAttachment.status == "READY")).all()
    grouped = {}
    for row in rows:
        grouped.setdefault(row.message_id, []).append(attachment_info(row))
    return [{**message, "attachments": grouped.get(message["id"], [])} for message in messages]


def authorize_upload(db, ticket_id: int, viewer: dict):
    """客服须领取人工工单；用户仍只能给自己的工单补充证据。"""
    ticket = authorize(db, ticket_id, viewer)
    if viewer["role"] in Role.STAFF:
        if ticket.handling_mode == HandlingMode.HUMAN_PENDING:
            raise InvalidStateError("Agent 尚未确认交接，请稍后上传")
        if ticket.assignee_id != viewer["id"]:
            raise PermissionDeniedError("请先领取工单，再上传图片")
    if ticket.status == "CLOSED":
        raise InvalidStateError("请先重新打开工单，再上传图片")
    return ticket


def validate_image(data: bytes, declared_type: str) -> tuple[str, str]:
    """不信任扩展名或浏览器 MIME；拒绝超限、伪装文件及解压炸弹。"""
    if not data or len(data) > settings.oss_max_upload_bytes:
        raise BusinessError("图片为空或超过 5 MB 限制", code="IMAGE_SIZE_LIMIT", http_status=413)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(data)) as image:
                detected = FORMATS.get(image.format)
                if image.width * image.height > 40_000_000 or detected is None:
                    raise ValidationError("仅支持不超过 4000 万像素的 PNG、JPEG、WebP 图片")
                image.verify()
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise ValidationError("图片内容无效，请上传 PNG、JPEG 或 WebP") from exc
    if declared_type != detected[0]:
        raise ValidationError("图片类型与实际内容不一致")
    return detected


def upload_image(*, ticket_id: int, viewer: dict, filename: str, declared_type: str, data: bytes) -> dict:
    """网络上传不持有工单行锁；提交消息前再次校验归属和人工交接状态。"""
    with session_scope() as db:
        authorize_upload(db, ticket_id, viewer)
    content_type, extension = validate_image(data, declared_type)
    storage = get_storage()
    key = f"tickets/{ticket_id}/{uuid.uuid4().hex}.{extension}"
    name = re.sub(r"[\x00-\x1f\x7f]", "", filename.replace("\\", "/").rsplit("/", 1)[-1])[:255] or f"图片.{extension}"
    with session_scope() as db:
        row = TicketAttachment(ticket_id=ticket_id, uploader_id=viewer["id"], bucket=storage.bucket,
            object_key=key, filename=name, content_type=content_type, size=len(data), status="PENDING")
        db.add(row)
        db.flush()
        attachment_id = row.id
    try:
        storage.upload(storage.bucket, key, data, content_type)
        with session_scope() as db:
            ticket = authorize_upload(db, ticket_id, viewer)
            row = db.get(TicketAttachment, attachment_id, with_for_update=True)
            if row is None or row.status != "PENDING":
                raise InvalidStateError("上传已失效，请重新选择图片")
            sender_type = SenderType.CUSTOMER_SERVICE if viewer["role"] in Role.STAFF else SenderType.USER
            message = ticket_repository.add_message(db, ticket_id=ticket_id, sender_type=sender_type,
                sender_id=viewer["id"], content="[图片]")
            row.message_id, row.status = message["id"], "READY"
            if ticket.status == "RESOLVED" and sender_type == SenderType.USER:
                ticket.status = "PROCESSING"
            message["attachments"] = [attachment_info(row)]
        return message
    except Exception as exc:
        # PENDING 记录使补偿失败仍可重试清理；绝不返回一条没有成功绑定的图片消息。
        # 提交结果不明时先核实，不能误删已成功绑定的证据图片。
        try:
            with session_scope() as db:
                row = db.get(TicketAttachment, attachment_id, with_for_update=True)
                if row and row.status == "READY":
                    message = next(m for m in ticket_repository.list_messages(db, ticket_id) if m["id"] == row.message_id)
                    message["attachments"] = [attachment_info(row)]
                    return message
                if row:
                    row.status = "CLEANUP_PENDING"
        except Exception as reconcile_error:
            raise BusinessError("图片提交结果待核实，请稍后刷新工单", code="UPLOAD_RESULT_UNKNOWN", http_status=503) from reconcile_error
        cleaned = False
        try:
            storage.delete(storage.bucket, key)
            cleaned = True
        except Exception:
            logger.warning("OSS 上传补偿待重试：attachment=%s", attachment_id)
        with session_scope() as db:
            row = db.get(TicketAttachment, attachment_id)
            if row and row.status != "READY":
                if cleaned:
                    db.delete(row)
                else:
                    row.status = "CLEANUP_PENDING"
        if isinstance(exc, BusinessError):
            raise
        raise BusinessError("图片上传失败，请稍后重试", code="OSS_UPLOAD_FAILED", http_status=502) from exc


def image_url(*, ticket_id: int, attachment_id: int, viewer: dict) -> dict:
    """鉴权后才生成短期签名；知道图片 ID 不代表能读取其他用户的附件。"""
    with session_scope() as db:
        authorize(db, ticket_id, viewer)
        row = db.get(TicketAttachment, attachment_id)
        if row is None or row.ticket_id != ticket_id or row.status != "READY":
            raise BusinessError("图片不存在", code="ATTACHMENT_NOT_FOUND", http_status=404)
        bucket, key = row.bucket, row.object_key
    try:
        url = get_storage().signed_url(bucket, key, settings.oss_signed_url_seconds)
    except BusinessError:
        raise
    except Exception as exc:
        raise BusinessError("暂时无法查看图片，请稍后重试", code="OSS_SIGN_FAILED", http_status=502) from exc
    return {"url": url, "expires_in": settings.oss_signed_url_seconds}


def cleanup_uploads(limit: int = 100) -> int:
    """只清理失败或超过一小时未完成的上传，不删除已绑定到消息的图片。"""
    try:
        storage = get_storage()
    except BusinessError:
        return 0
    with session_scope() as db:
        rows = db.scalars(select(TicketAttachment).where(or_(
            TicketAttachment.status == "CLEANUP_PENDING",
            and_(TicketAttachment.status.in_(("PENDING", "CLEANING")), TicketAttachment.updated_at < utcnow() - timedelta(hours=1)))
        ).limit(limit)).all()
        candidates = [(row.id, row.bucket, row.object_key, row.status) for row in rows]
    count = 0
    for attachment_id, bucket, key, expected in candidates:
        try:
            with session_scope() as db:
                claimed = db.execute(update(TicketAttachment).where(TicketAttachment.id == attachment_id,
                    TicketAttachment.status == expected).values(status="CLEANING"))
                if claimed.rowcount != 1:
                    continue
            storage.delete(bucket, key)
            with session_scope() as db:
                row = db.get(TicketAttachment, attachment_id)
                if row and row.status != "READY":
                    db.delete(row)
            count += 1
        except Exception:
            with session_scope() as db:
                db.execute(update(TicketAttachment).where(TicketAttachment.id == attachment_id,
                    TicketAttachment.status == "CLEANING").values(status="CLEANUP_PENDING"))
            logger.warning("OSS 清理稍后重试：attachment=%s", attachment_id)
    return count
