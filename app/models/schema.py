from typing import Any, Dict, Optional
from pydantic import BaseModel, Field, field_validator

class RunPayload(BaseModel):
    input: Dict[str, Any] = Field(..., description="Payload for the worker")
    webhook: Optional[str] = Field(None, description="Optional webhook URL")
    policy: Optional[Dict[str, Any]] = Field(None, description="executionTimeout (ms), ttl (ms), lowPriority")
    s3Config: Optional[Dict[str, str]] = Field(None, description="Optional S3-style config")

    @field_validator("policy")
    @classmethod
    def _validate_policy(cls, v):
        if v is None: return v
        for k in v:
            if k not in ("executionTimeout", "ttl", "lowPriority"):
                raise ValueError(f"unsupported policy key: {k}")
        return v
