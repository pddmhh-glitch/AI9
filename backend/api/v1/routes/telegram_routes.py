"""
API v1 Telegram Bot Management Routes
Multi-bot notification system administration
"""
from fastapi import APIRouter, Request, HTTPException, Header
from typing import Optional, List
from pydantic import BaseModel, Field
from datetime import datetime, timezone
import uuid
import json
import logging

from ..core.database import fetch_one, fetch_all, execute, get_pool
from ..core.config import get_api_settings
from ..core.notification_router import NotificationRouter, EventType, EVENT_METADATA

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin/telegram", tags=["Telegram Bots"])
settings = get_api_settings()


# ==================== AUTH ====================

async def require_admin(request: Request, authorization: str):
    """Verify admin authentication"""
    from ..core.security import decode_jwt_token
    
    token = authorization.replace("Bearer ", "")
    payload = decode_jwt_token(token)
    
    if not payload:
        raise HTTPException(status_code=401, detail="Invalid token")
    
    user_id = payload.get('sub') or payload.get('user_id')
    user = await fetch_one("SELECT * FROM users WHERE user_id = $1", user_id)
    
    if not user or user.get('role') not in ['admin', 'superadmin']:
        raise HTTPException(status_code=403, detail="Admin access required")
    
    return user


# ==================== MODELS ====================

class TelegramBotCreate(BaseModel):
    """Create a new Telegram bot"""
    name: str = Field(..., min_length=1, max_length=100)
    bot_token: str = Field(..., min_length=10)
    chat_id: str = Field(..., min_length=1)
    is_active: bool = True
    can_approve_payments: bool = False
    can_approve_wallet_loads: bool = False
    can_approve_withdrawals: bool = False
    description: Optional[str] = None


class TelegramBotUpdate(BaseModel):
    """Update a Telegram bot"""
    name: Optional[str] = None
    bot_token: Optional[str] = None
    chat_id: Optional[str] = None
    is_active: Optional[bool] = None
    can_approve_payments: Optional[bool] = None
    can_approve_wallet_loads: Optional[bool] = None
    can_approve_withdrawals: Optional[bool] = None
    description: Optional[str] = None


class EventPermissionUpdate(BaseModel):
    """Update event permissions for a bot"""
    event_type: str
    enabled: bool


class BulkPermissionUpdate(BaseModel):
    """Bulk update permissions"""
    permissions: List[EventPermissionUpdate]


# ==================== BOT CRUD ====================

@router.get("/bots")
async def list_telegram_bots(
    request: Request,
    authorization: str = Header(..., alias="Authorization")
):
    """
    GET /api/v1/admin/telegram/bots
    List all Telegram bots with their permissions
    """
    await require_admin(request, authorization)
    
    bots = await fetch_all("""
        SELECT bot_id, name, chat_id, is_active, 
               can_approve_payments, can_approve_wallet_loads, can_approve_withdrawals,
               description, created_at, updated_at
        FROM telegram_bots
        ORDER BY created_at DESC
    """)
    
    # Get permissions for each bot
    result = []
    for bot in bots:
        permissions = await fetch_all("""
            SELECT event_type, enabled
            FROM telegram_bot_event_permissions
            WHERE bot_id = $1
        """, bot['bot_id'])
        
        perm_dict = {p['event_type']: p['enabled'] for p in permissions}
        
        result.append({
            **dict(bot),
            "permissions": perm_dict,
            "created_at": bot['created_at'].isoformat() if bot['created_at'] else None,
            "updated_at": bot['updated_at'].isoformat() if bot['updated_at'] else None
        })
    
    return {"bots": result}


@router.post("/bots")
async def create_telegram_bot(
    request: Request,
    data: TelegramBotCreate,
    authorization: str = Header(..., alias="Authorization")
):
    """
    POST /api/v1/admin/telegram/bots
    Create a new Telegram bot
    """
    admin = await require_admin(request, authorization)
    
    # Validate bot token by testing it
    import httpx
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(f"https://api.telegram.org/bot{data.bot_token}/getMe")
            if response.status_code != 200:
                raise HTTPException(status_code=400, detail="Invalid bot token - could not verify with Telegram")
            
            bot_info = response.json().get('result', {})
            logger.info(f"Verified Telegram bot: {bot_info.get('username')}")
    except httpx.RequestError as e:
        raise HTTPException(status_code=400, detail=f"Could not verify bot token: {str(e)}")
    
    bot_id = str(uuid.uuid4())
    
    await execute("""
        INSERT INTO telegram_bots 
        (bot_id, name, bot_token, chat_id, is_active, 
         can_approve_payments, can_approve_wallet_loads, can_approve_withdrawals,
         description, created_by, created_at, updated_at)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, NOW(), NOW())
    """, bot_id, data.name, data.bot_token, data.chat_id, data.is_active,
       data.can_approve_payments, data.can_approve_wallet_loads, data.can_approve_withdrawals,
       data.description, admin['user_id'])
    
    # Create default permissions (all disabled)
    for event_type in EventType:
        await execute("""
            INSERT INTO telegram_bot_event_permissions (permission_id, bot_id, event_type, enabled)
            VALUES ($1, $2, $3, FALSE)
            ON CONFLICT (bot_id, event_type) DO NOTHING
        """, str(uuid.uuid4()), bot_id, event_type.value)
    
    return {
        "bot_id": bot_id,
        "message": "Telegram bot created successfully",
        "telegram_username": bot_info.get('username')
    }


@router.get("/bots/{bot_id}")
async def get_telegram_bot(
    request: Request,
    bot_id: str,
    authorization: str = Header(..., alias="Authorization")
):
    """
    GET /api/v1/admin/telegram/bots/{bot_id}
    Get a specific Telegram bot details
    """
    await require_admin(request, authorization)
    
    bot = await fetch_one("""
        SELECT bot_id, name, chat_id, is_active, 
               can_approve_payments, can_approve_wallet_loads, can_approve_withdrawals,
               description, created_at, updated_at
        FROM telegram_bots
        WHERE bot_id = $1
    """, bot_id)
    
    if not bot:
        raise HTTPException(status_code=404, detail="Bot not found")
    
    permissions = await fetch_all("""
        SELECT event_type, enabled
        FROM telegram_bot_event_permissions
        WHERE bot_id = $1
    """, bot_id)
    
    perm_dict = {p['event_type']: p['enabled'] for p in permissions}
    
    return {
        **dict(bot),
        "permissions": perm_dict,
        "created_at": bot['created_at'].isoformat() if bot['created_at'] else None,
        "updated_at": bot['updated_at'].isoformat() if bot['updated_at'] else None
    }


@router.put("/bots/{bot_id}")
async def update_telegram_bot(
    request: Request,
    bot_id: str,
    data: TelegramBotUpdate,
    authorization: str = Header(..., alias="Authorization")
):
    """
    PUT /api/v1/admin/telegram/bots/{bot_id}
    Update a Telegram bot
    """
    await require_admin(request, authorization)
    
    # Check if bot exists
    existing = await fetch_one("SELECT * FROM telegram_bots WHERE bot_id = $1", bot_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Bot not found")
    
    # If updating token, validate it
    if data.bot_token:
        import httpx
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(f"https://api.telegram.org/bot{data.bot_token}/getMe")
                if response.status_code != 200:
                    raise HTTPException(status_code=400, detail="Invalid bot token")
        except httpx.RequestError:
            raise HTTPException(status_code=400, detail="Could not verify bot token")
    
    # Build update query
    updates = []
    params = []
    param_idx = 1
    
    for field in ['name', 'bot_token', 'chat_id', 'is_active', 
                  'can_approve_payments', 'can_approve_wallet_loads', 'can_approve_withdrawals', 'description']:
        value = getattr(data, field, None)
        if value is not None:
            updates.append(f"{field} = ${param_idx}")
            params.append(value)
            param_idx += 1
    
    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")
    
    updates.append("updated_at = NOW()")
    params.append(bot_id)
    
    query = f"UPDATE telegram_bots SET {', '.join(updates)} WHERE bot_id = ${param_idx}"
    await execute(query, *params)
    
    return {"message": "Bot updated successfully"}


@router.delete("/bots/{bot_id}")
async def delete_telegram_bot(
    request: Request,
    bot_id: str,
    authorization: str = Header(..., alias="Authorization")
):
    """
    DELETE /api/v1/admin/telegram/bots/{bot_id}
    Delete a Telegram bot
    """
    await require_admin(request, authorization)
    
    # Permissions will be cascade deleted
    await execute("DELETE FROM telegram_bots WHERE bot_id = $1", bot_id)
    
    return {"message": "Bot deleted successfully"}


# ==================== EVENT PERMISSIONS ====================

@router.get("/events")
async def list_events(
    request: Request,
    authorization: str = Header(..., alias="Authorization")
):
    """
    GET /api/v1/admin/telegram/events
    List all available event types
    """
    await require_admin(request, authorization)
    
    events = await NotificationRouter.get_all_events()
    
    # Group by category
    by_category = {}
    for event in events:
        cat = event['category']
        if cat not in by_category:
            by_category[cat] = []
        by_category[cat].append(event)
    
    return {
        "events": events,
        "by_category": by_category
    }


@router.post("/bots/{bot_id}/permissions")
async def update_bot_permissions(
    request: Request,
    bot_id: str,
    data: BulkPermissionUpdate,
    authorization: str = Header(..., alias="Authorization")
):
    """
    POST /api/v1/admin/telegram/bots/{bot_id}/permissions
    Update event permissions for a bot
    """
    await require_admin(request, authorization)
    
    # Verify bot exists
    bot = await fetch_one("SELECT * FROM telegram_bots WHERE bot_id = $1", bot_id)
    if not bot:
        raise HTTPException(status_code=404, detail="Bot not found")
    
    # Update each permission
    for perm in data.permissions:
        # Validate event type
        valid_events = [e.value for e in EventType]
        if perm.event_type not in valid_events:
            continue
        
        await execute("""
            INSERT INTO telegram_bot_event_permissions (permission_id, bot_id, event_type, enabled, created_at)
            VALUES ($1, $2, $3, $4, NOW())
            ON CONFLICT (bot_id, event_type) 
            DO UPDATE SET enabled = $4
        """, str(uuid.uuid4()), bot_id, perm.event_type, perm.enabled)
    
    return {"message": "Permissions updated successfully"}


@router.get("/bots/{bot_id}/permissions")
async def get_bot_permissions(
    request: Request,
    bot_id: str,
    authorization: str = Header(..., alias="Authorization")
):
    """
    GET /api/v1/admin/telegram/bots/{bot_id}/permissions
    Get all permissions for a bot
    """
    await require_admin(request, authorization)
    
    permissions = await fetch_all("""
        SELECT event_type, enabled
        FROM telegram_bot_event_permissions
        WHERE bot_id = $1
    """, bot_id)
    
    # Fill in missing events with default (disabled)
    perm_dict = {p['event_type']: p['enabled'] for p in permissions}
    
    all_events = await NotificationRouter.get_all_events()
    result = []
    for event in all_events:
        result.append({
            **event,
            "enabled": perm_dict.get(event['event_type'], False)
        })
    
    return {"permissions": result}


# ==================== PERMISSION MATRIX ====================

@router.get("/permission-matrix")
async def get_permission_matrix(
    request: Request,
    authorization: str = Header(..., alias="Authorization")
):
    """
    GET /api/v1/admin/telegram/permission-matrix
    Get full permission matrix (events x bots)
    """
    await require_admin(request, authorization)
    
    # Get all bots
    bots = await fetch_all("""
        SELECT bot_id, name, is_active, can_approve_payments, can_approve_wallet_loads, can_approve_withdrawals
        FROM telegram_bots
        ORDER BY name
    """)
    
    # Get all events
    events = await NotificationRouter.get_all_events()
    
    # Get all permissions
    permissions = await fetch_all("""
        SELECT bot_id, event_type, enabled
        FROM telegram_bot_event_permissions
    """)
    
    # Build matrix
    perm_lookup = {}
    for p in permissions:
        key = f"{p['bot_id']}:{p['event_type']}"
        perm_lookup[key] = p['enabled']
    
    matrix = []
    for event in events:
        row = {
            "event_type": event['event_type'],
            "label": event['label'],
            "category": event['category'],
            "requires_approval": event['requires_approval'],
            "bots": {}
        }
        for bot in bots:
            key = f"{bot['bot_id']}:{event['event_type']}"
            row["bots"][bot['bot_id']] = perm_lookup.get(key, False)
        matrix.append(row)
    
    return {
        "bots": [{"bot_id": b['bot_id'], "name": b['name'], "is_active": b['is_active']} for b in bots],
        "matrix": matrix
    }


@router.post("/permission-matrix")
async def update_permission_matrix(
    request: Request,
    authorization: str = Header(..., alias="Authorization")
):
    """
    POST /api/v1/admin/telegram/permission-matrix
    Bulk update permission matrix
    Body: { "updates": [{"bot_id": "...", "event_type": "...", "enabled": true/false}, ...] }
    """
    await require_admin(request, authorization)
    
    body = await request.json()
    updates = body.get('updates', [])
    
    for update in updates:
        bot_id = update.get('bot_id')
        event_type = update.get('event_type')
        enabled = update.get('enabled', False)
        
        if not bot_id or not event_type:
            continue
        
        await execute("""
            INSERT INTO telegram_bot_event_permissions (permission_id, bot_id, event_type, enabled, created_at)
            VALUES ($1, $2, $3, $4, NOW())
            ON CONFLICT (bot_id, event_type) 
            DO UPDATE SET enabled = $4
        """, str(uuid.uuid4()), bot_id, event_type, enabled)
    
    return {"message": f"Updated {len(updates)} permissions"}


# ==================== NOTIFICATION LOGS ====================

@router.get("/logs")
async def get_notification_logs(
    request: Request,
    event_type: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
    authorization: str = Header(..., alias="Authorization")
):
    """
    GET /api/v1/admin/telegram/logs
    Get notification logs
    """
    await require_admin(request, authorization)
    
    conditions = []
    params = []
    param_idx = 1
    
    if event_type:
        conditions.append(f"event_type = ${param_idx}")
        params.append(event_type)
        param_idx += 1
    
    if status:
        conditions.append(f"status = ${param_idx}")
        params.append(status)
        param_idx += 1
    
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    
    params.extend([limit, offset])
    
    logs = await fetch_all(f"""
        SELECT log_id, event_type, payload, sent_to_bot_ids, success_bot_ids, 
               failed_bot_ids, status, error_details, created_at
        FROM notification_logs
        {where}
        ORDER BY created_at DESC
        LIMIT ${param_idx} OFFSET ${param_idx + 1}
    """, *params)
    
    return {
        "logs": [
            {
                **dict(log),
                "created_at": log['created_at'].isoformat() if log['created_at'] else None
            }
            for log in logs
        ]
    }


# ==================== TEST NOTIFICATION ====================

@router.post("/bots/{bot_id}/test")
async def test_bot_notification(
    request: Request,
    bot_id: str,
    authorization: str = Header(..., alias="Authorization")
):
    """
    POST /api/v1/admin/telegram/bots/{bot_id}/test
    Send a test notification to a specific bot
    """
    await require_admin(request, authorization)
    
    bot = await fetch_one("""
        SELECT * FROM telegram_bots WHERE bot_id = $1
    """, bot_id)
    
    if not bot:
        raise HTTPException(status_code=404, detail="Bot not found")
    
    import httpx
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            message = f"""🔔 *Test Notification*

This is a test message from the Gaming Platform.

Bot: {bot['name']}
Time: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}

If you received this, your bot is configured correctly! ✅"""
            
            response = await client.post(
                f"https://api.telegram.org/bot{bot['bot_token']}/sendMessage",
                json={
                    "chat_id": bot['chat_id'],
                    "text": message,
                    "parse_mode": "Markdown"
                }
            )
            
            if response.status_code == 200:
                return {"success": True, "message": "Test notification sent successfully"}
            else:
                return {"success": False, "error": response.text}
                
    except Exception as e:
        return {"success": False, "error": str(e)}
