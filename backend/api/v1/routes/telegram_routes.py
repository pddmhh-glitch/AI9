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


# ==================== TELEGRAM WEBHOOK HANDLER ====================

@router.post("/webhook/{bot_token}")
async def telegram_webhook(request: Request, bot_token: str):
    """
    POST /api/v1/admin/telegram/webhook/{bot_token}
    Handle Telegram webhook callbacks (button presses, etc.)
    """
    try:
        body = await request.json()
        logger.info(f"Telegram webhook received: {json.dumps(body)[:500]}")
        
        # Find the bot by token
        bot = await fetch_one("""
            SELECT * FROM telegram_bots WHERE bot_token = $1 AND is_active = TRUE
        """, bot_token)
        
        if not bot:
            logger.warning(f"Unknown bot token in webhook")
            return {"ok": True}  # Always return ok to Telegram
        
        # Handle callback query (button press)
        if 'callback_query' in body:
            callback = body['callback_query']
            callback_id = callback.get('id')
            callback_data = callback.get('data', '')
            from_user = callback.get('from', {})
            message = callback.get('message', {})
            chat_id = message.get('chat', {}).get('id')
            message_id = message.get('message_id')
            
            logger.info(f"Callback received: {callback_data} from user {from_user.get('id')}")
            
            # Parse callback data (format: action_type:reference_id)
            if ':' in callback_data:
                action, reference_id = callback_data.split(':', 1)
            else:
                action = callback_data
                reference_id = None
            
            result = await process_telegram_callback(
                bot=bot,
                action=action,
                reference_id=reference_id,
                callback_id=callback_id,
                chat_id=chat_id,
                message_id=message_id,
                from_user=from_user
            )
            
            return {"ok": True, "result": result}
        
        # Handle regular message (for future use)
        if 'message' in body:
            logger.info(f"Regular message received (not handling)")
        
        return {"ok": True}
        
    except Exception as e:
        logger.error(f"Telegram webhook error: {e}")
        return {"ok": True}  # Always return ok


async def process_telegram_callback(
    bot: dict,
    action: str,
    reference_id: str,
    callback_id: str,
    chat_id: int,
    message_id: int,
    from_user: dict
):
    """Process Telegram callback button press"""
    import httpx
    
    bot_token = bot['bot_token']
    response_text = ""
    
    try:
        # Handle wallet load actions
        if action == 'wl_approve':
            result = await handle_wallet_action(reference_id, 'APPROVE', from_user)
            response_text = f"✅ Wallet load approved!\n\nAmount: ₱{result.get('amount', 0):,.2f}\nNew Balance: ₱{result.get('new_balance', 0):,.2f}"
            
        elif action == 'wl_reject':
            result = await handle_wallet_action(reference_id, 'REJECT', from_user)
            response_text = f"❌ Wallet load rejected.\n\nReason: Admin rejection"
            
        elif action == 'wl_view':
            result = await get_wallet_request_details(reference_id)
            response_text = f"""📋 *Wallet Load Details*

🆔 Request ID: `{reference_id[:8]}...`
👤 User: {result.get('display_name', 'N/A')} (@{result.get('username', 'N/A')})
💵 Amount: ₱{result.get('amount', 0):,.2f}
💳 Method: {result.get('payment_method', 'N/A')}
📅 Status: {result.get('status', 'unknown').upper()}
⏰ Created: {result.get('created_at', 'N/A')}"""
        
        # Handle order actions
        elif action == 'order_approve':
            result = await handle_order_action(reference_id, 'APPROVE', from_user)
            response_text = f"✅ Order approved!"
            
        elif action == 'order_reject':
            result = await handle_order_action(reference_id, 'REJECT', from_user)
            response_text = f"❌ Order rejected."
            
        elif action == 'order_view':
            result = await get_order_details(reference_id)
            response_text = f"📋 Order Details: {json.dumps(result, indent=2)[:500]}"
        
        else:
            response_text = f"⚠️ Unknown action: {action}"
        
        # Answer callback query (removes loading indicator)
        async with httpx.AsyncClient(timeout=30.0) as client:
            await client.post(
                f"https://api.telegram.org/bot{bot_token}/answerCallbackQuery",
                json={
                    "callback_query_id": callback_id,
                    "text": response_text[:200],  # Max 200 chars for popup
                    "show_alert": True
                }
            )
            
            # Update the original message to show action taken
            if action.endswith('_approve') or action.endswith('_reject'):
                action_taken = "APPROVED ✅" if action.endswith('_approve') else "REJECTED ❌"
                await client.post(
                    f"https://api.telegram.org/bot{bot_token}/editMessageReplyMarkup",
                    json={
                        "chat_id": chat_id,
                        "message_id": message_id,
                        "reply_markup": {
                            "inline_keyboard": [
                                [{"text": f"Action: {action_taken}", "callback_data": "done"}]
                            ]
                        }
                    }
                )
        
        return {"success": True, "action": action, "response": response_text}
        
    except Exception as e:
        logger.error(f"Error processing callback {action}: {e}")
        
        # Still answer callback to remove loading
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                await client.post(
                    f"https://api.telegram.org/bot{bot_token}/answerCallbackQuery",
                    json={
                        "callback_query_id": callback_id,
                        "text": f"❌ Error: {str(e)[:100]}",
                        "show_alert": True
                    }
                )
        except:
            pass
        
        return {"success": False, "error": str(e)}


async def handle_wallet_action(request_id: str, action: str, from_user: dict):
    """Handle wallet load approval/rejection from Telegram"""
    from ..core.database import get_pool
    
    load_request = await fetch_one("""
        SELECT * FROM wallet_load_requests WHERE request_id = $1
    """, request_id)
    
    if not load_request:
        raise Exception("Request not found")
    
    if load_request['status'] != 'pending':
        raise Exception(f"Request already {load_request['status']}")
    
    admin_id = f"telegram:{from_user.get('id', 'unknown')}"
    now = datetime.now(timezone.utc)
    
    if action == 'APPROVE':
        user = await fetch_one("SELECT * FROM users WHERE user_id = $1", load_request['user_id'])
        if not user:
            raise Exception("User not found")
        
        current_balance = float(user.get('real_balance', 0) or 0)
        new_balance = current_balance + load_request['amount']
        
        pool = await get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("""
                    UPDATE users SET real_balance = $1, updated_at = NOW()
                    WHERE user_id = $2
                """, new_balance, load_request['user_id'])
                
                await conn.execute("""
                    UPDATE wallet_load_requests 
                    SET status = 'approved', reviewed_by = $1, reviewed_at = $2, updated_at = NOW()
                    WHERE request_id = $3
                """, admin_id, now, request_id)
                
                await conn.execute("""
                    INSERT INTO wallet_ledger 
                    (ledger_id, user_id, transaction_type, amount, balance_before, balance_after,
                     reference_type, reference_id, description, created_at)
                    VALUES ($1, $2, 'credit', $3, $4, $5, 'wallet_load', $6, $7, NOW())
                """, str(uuid.uuid4()), load_request['user_id'], load_request['amount'],
                   current_balance, new_balance, request_id, 
                   f"Wallet load via {load_request['payment_method']} (Telegram approved)")
        
        # Emit notification
        from ..core.notification_router import emit_event, EventType
        await emit_event(
            event_type=EventType.WALLET_LOAD_APPROVED,
            title="Wallet Load Approved (via Telegram)",
            message=f"Wallet load of ₱{load_request['amount']:,.2f} approved via Telegram.\n\nNew balance: ₱{new_balance:,.2f}",
            reference_id=request_id,
            reference_type="wallet_load",
            user_id=load_request['user_id'],
            username=user.get('username'),
            display_name=user.get('display_name'),
            amount=load_request['amount'],
            extra_data={"new_balance": new_balance, "approved_via": "telegram"},
            requires_action=False
        )
        
        return {"amount": load_request['amount'], "new_balance": new_balance}
    
    elif action == 'REJECT':
        user = await fetch_one("SELECT * FROM users WHERE user_id = $1", load_request['user_id'])
        
        await execute("""
            UPDATE wallet_load_requests 
            SET status = 'rejected', reviewed_by = $1, reviewed_at = $2, 
                rejection_reason = 'Rejected via Telegram', updated_at = NOW()
            WHERE request_id = $3
        """, admin_id, now, request_id)
        
        # Emit notification
        from ..core.notification_router import emit_event, EventType
        await emit_event(
            event_type=EventType.WALLET_LOAD_REJECTED,
            title="Wallet Load Rejected (via Telegram)",
            message=f"Wallet load of ₱{load_request['amount']:,.2f} rejected via Telegram.",
            reference_id=request_id,
            reference_type="wallet_load",
            user_id=load_request['user_id'],
            username=user.get('username') if user else None,
            display_name=user.get('display_name') if user else None,
            amount=load_request['amount'],
            extra_data={"rejected_via": "telegram"},
            requires_action=False
        )
        
        return {"amount": load_request['amount'], "status": "rejected"}


async def get_wallet_request_details(request_id: str):
    """Get wallet load request details"""
    result = await fetch_one("""
        SELECT wlr.*, u.username, u.display_name
        FROM wallet_load_requests wlr
        JOIN users u ON wlr.user_id = u.user_id
        WHERE wlr.request_id = $1
    """, request_id)
    
    if not result:
        return {"error": "Request not found"}
    
    return {
        "request_id": result['request_id'],
        "username": result['username'],
        "display_name": result['display_name'],
        "amount": float(result['amount']),
        "payment_method": result['payment_method'],
        "status": result['status'],
        "created_at": result['created_at'].strftime('%Y-%m-%d %H:%M UTC') if result['created_at'] else 'N/A'
    }


async def handle_order_action(order_id: str, action: str, from_user: dict):
    """Handle order approval/rejection from Telegram"""
    # Placeholder - implement based on your order system
    return {"order_id": order_id, "action": action}


async def get_order_details(order_id: str):
    """Get order details"""
    result = await fetch_one("""
        SELECT * FROM orders WHERE order_id = $1
    """, order_id)
    return dict(result) if result else {"error": "Order not found"}


# ==================== WEBHOOK SETUP ENDPOINT ====================

@router.post("/bots/{bot_id}/setup-webhook")
async def setup_bot_webhook(
    request: Request,
    bot_id: str,
    authorization: str = Header(..., alias="Authorization")
):
    """
    POST /api/v1/admin/telegram/bots/{bot_id}/setup-webhook
    Set up Telegram webhook for a bot to receive button callbacks
    """
    await require_admin(request, authorization)
    
    bot = await fetch_one("""
        SELECT * FROM telegram_bots WHERE bot_id = $1
    """, bot_id)
    
    if not bot:
        raise HTTPException(status_code=404, detail="Bot not found")
    
    # Get the backend URL from environment
    import os
    backend_url = os.environ.get('BACKEND_URL', '')
    
    if not backend_url:
        # Try to construct from request
        backend_url = str(request.base_url).rstrip('/')
    
    webhook_url = f"{backend_url}/api/v1/admin/telegram/webhook/{bot['bot_token']}"
    
    import httpx
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            # Set webhook
            response = await client.post(
                f"https://api.telegram.org/bot{bot['bot_token']}/setWebhook",
                json={
                    "url": webhook_url,
                    "allowed_updates": ["callback_query", "message"]
                }
            )
            
            result = response.json()
            
            if result.get('ok'):
                return {
                    "success": True,
                    "message": "Webhook set up successfully",
                    "webhook_url": webhook_url
                }
            else:
                return {
                    "success": False,
                    "error": result.get('description', 'Unknown error')
                }
                
    except Exception as e:
        return {"success": False, "error": str(e)}


@router.get("/bots/{bot_id}/webhook-info")
async def get_bot_webhook_info(
    request: Request,
    bot_id: str,
    authorization: str = Header(..., alias="Authorization")
):
    """
    GET /api/v1/admin/telegram/bots/{bot_id}/webhook-info
    Get current webhook info for a bot
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
            response = await client.get(
                f"https://api.telegram.org/bot{bot['bot_token']}/getWebhookInfo"
            )
            
            result = response.json()
            return {"success": True, "webhook_info": result.get('result', {})}
                
    except Exception as e:
        return {"success": False, "error": str(e)}


@router.delete("/bots/{bot_id}/webhook")
async def delete_bot_webhook(
    request: Request,
    bot_id: str,
    authorization: str = Header(..., alias="Authorization")
):
    """
    DELETE /api/v1/admin/telegram/bots/{bot_id}/webhook
    Remove webhook from a bot (switch to polling mode)
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
            response = await client.post(
                f"https://api.telegram.org/bot{bot['bot_token']}/deleteWebhook"
            )
            
            result = response.json()
            
            if result.get('ok'):
                return {"success": True, "message": "Webhook deleted"}
            else:
                return {"success": False, "error": result.get('description')}
                
    except Exception as e:
        return {"success": False, "error": str(e)}
