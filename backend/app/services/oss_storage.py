"""阿里云 OSS Python SDK V2 封装，私有上传、删除和短期签名查看均留在后端。"""

from datetime import timedelta
from functools import lru_cache

from app.core.config import settings
from app.core.exceptions import BusinessError


class OssStorage:
    """不使用默认凭据链，避免配置留空时误用主机上的其他云账号。"""

    def __init__(self):
        import alibabacloud_oss_v2 as oss
        if not all((settings.oss_access_key_id, settings.oss_access_key_secret, settings.oss_region, settings.oss_bucket)):
            raise BusinessError("图片上传暂不可用，请先使用文字说明", code="OSS_NOT_CONFIGURED", http_status=503)
        self.bucket = settings.oss_bucket
        credentials = oss.credentials.StaticCredentialsProvider(
            settings.oss_access_key_id, settings.oss_access_key_secret, settings.oss_security_token or None)
        self.client = oss.Client(oss.config.Config(region=settings.oss_region,
            endpoint=settings.oss_endpoint or None, credentials_provider=credentials,
            connect_timeout=5, readwrite_timeout=30, retry_max_attempts=2))

    def upload(self, bucket: str, key: str, data: bytes, content_type: str) -> None:
        """随机对象名且禁止覆盖；不设置公开读 ACL。"""
        import alibabacloud_oss_v2 as oss
        self.client.put_object(oss.PutObjectRequest(bucket=bucket, key=key, body=data,
            content_type=content_type, content_length=len(data), forbid_overwrite=True, acl="private"))

    def delete(self, bucket: str, key: str) -> None:
        """补偿失败上传或数据库提交失败产生的对象。"""
        import alibabacloud_oss_v2 as oss
        self.client.delete_object(oss.DeleteObjectRequest(bucket=bucket, key=key))

    def signed_url(self, bucket: str, key: str, seconds: int) -> str:
        """URL 只在业务鉴权通过后生成，不写入数据库或日志。"""
        import alibabacloud_oss_v2 as oss
        return self.client.presign(oss.GetObjectRequest(bucket=bucket, key=key),
                                   expires=timedelta(seconds=seconds)).url


@lru_cache(maxsize=1)
def get_storage() -> OssStorage:
    """延迟初始化，OSS 未配置时仍能启动 API 和 worker。"""
    return OssStorage()
