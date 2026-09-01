from __future__ import annotations

import hashlib
from pathlib import Path

import boto3
from botocore.client import Config


class ObjectStore:
    def __init__(self, endpoint: str, access_key: str, secret_key: str, bucket: str, local_root: Path):
        self.bucket = bucket
        self.local_root = local_root
        self.local_root.mkdir(parents=True, exist_ok=True)
        self.client = boto3.client("s3", endpoint_url=endpoint, aws_access_key_id=access_key,
                                   aws_secret_access_key=secret_key, region_name="us-east-1", config=Config(signature_version="s3v4"))
        try:
            self.client.head_bucket(Bucket=bucket)
        except Exception:
            self.client.create_bucket(Bucket=bucket)

    def put_bytes(self, key: str, body: bytes, content_type: str = "application/octet-stream") -> str:
        digest = hashlib.sha256(body).hexdigest()
        self.client.put_object(Bucket=self.bucket, Key=key, Body=body, ContentType=content_type, Metadata={"sha256": digest})
        return key

    def put_text(self, key: str, body: str, content_type: str = "text/plain; charset=utf-8") -> str:
        return self.put_bytes(key, body.encode("utf-8"), content_type)

    def get_bytes(self, key: str) -> bytes:
        return self.client.get_object(Bucket=self.bucket, Key=key)["Body"].read()

    def list_keys(self, prefix: str) -> list[str]:
        response = self.client.list_objects_v2(Bucket=self.bucket, Prefix=prefix)
        return [item["Key"] for item in response.get("Contents", [])]
