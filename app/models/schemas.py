from pydantic import BaseModel, Field
from datetime import datetime
from typing import Optional, List

class FileMetadata(BaseModel):
    file_id: str
    file_unique_id: str
    filename: str
    mime_type: str
    file_size: int
    uploader_id: int
    created_at: datetime = Field(default_factory=datetime.utcnow)
    expiry_time: Optional[datetime] = None
    access_count: int = 0
    short_code: str
    chat_id: int
    message_id: int
    thumbnail_id: Optional[str] = None

class User(BaseModel):
    user_id: int
    username: Optional[str] = None
    first_name: str
    last_name: Optional[str] = None
    is_admin: bool = False
    is_banned: bool = False
    joined_at: datetime = Field(default_factory=datetime.utcnow)
    last_active: datetime = Field(default_factory=datetime.utcnow)

class AppSettings(BaseModel):
    force_sub_channels: List[int] = []
    auto_delete_hours: int = 24
    maintenance_mode: bool = False
    broadcast_in_progress: bool = False
