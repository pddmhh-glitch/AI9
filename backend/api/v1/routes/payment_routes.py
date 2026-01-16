"""
API v1 Payment Routes
Payment proof upload, verification, and Telegram approval
"""
from fastapi import APIRouter, Request, HTTPException, status, Header, UploadFile, File, Form
from typing import Optional
from pydantic import BaseModel, Field
from datetime import datetime, timezone
import uuid
import json
import base64
import httpx
import logging

from ..core.database import fetch_one, fetch_all, execute
from ..core.config import get_api_settings
from .dependencies import check_rate_limiting, require_auth, AuthResult

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/payments", tags=["Payments"])
settings = get_api_settings()


# ==================== MODELS ====================

class PaymentProofUpload(BaseModel):
    """Payment proof upload request"""
    order_id: str
    image_data: str = Field(..., description="Base64 encoded image")
    image_type: str = Field(default="image/jpeg", description="MIME type")


class OrderActionRequest(BaseModel):
    """Order action request (approve/reject)"""
    action: str = Field(..., description="approve or reject")
    reason: Optional[str] = Field(None, description="Rejection reason")
    admin_notes: Optional[str] = None


# ==================== ENDPOINTS ====================

@router.post(
    "/proof/{order_id}",
    summary="Upload payment proof",
    description="""
    Upload a payment screenshot for an order.
    Triggers Telegram notification with approval buttons.
    """
)
async def upload_payment_proof(
    request: Request,
    order_id: str,
    image_data: str = Form(..., description="Base64 encoded image"),
    image_type: str = Form(default="image/jpeg"),
    authorization: str = Header(None, alias="Authorization")
):
    """Upload payment proof image"""
    await check_rate_limiting(request)
    
    # Get order
    order = await fetch_one("SELECT * FROM orders WHERE order_id = $1", order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    
    # Check order status
    if order['status'] not in ['initiated', 'awaiting_payment_proof']:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot upload proof for order in '{order['status']}' status"
        )
    
    # Store image (in production, upload to S3/cloud storage)
    # For now, we store base64 reference
    proof_id = str(uuid.uuid4())
    proof_url = f"data:{image_type};base64,{image_data[:100]}...{proof_id}"  # Truncated for DB
    
    # Update order
    await execute('''
        UPDATE orders 
        SET payment_proof_url = $1, 
            payment_proof_uploaded_at = $2,
            status = 'pending_review',
            updated_at = NOW()
        WHERE order_id = $3
    ''', proof_url, datetime.now(timezone.utc), order_id)
    
    # Log audit
    await log_audit(order['user_id'], order['username'], "payment.proof_uploaded", "order", order_id)
    
    # Send to Telegram if configured
    telegram_result = await send_telegram_notification(
        order_id=order_id,
        order=order,
        image_data=image_data,
        image_type=image_type
    )
    
    return {
        "success": True,
        "message": "Payment proof uploaded successfully",
        "order_id": order_id,
        "status": "pending_review",
        "telegram_sent": telegram_result.get('sent', False)
    }


@router.post(
    "/action/{order_id}",
    summary="Process order action",
    description="Admin: Approve or reject an order"
)
async def process_order_action(
    request: Request,
    order_id: str,
    data: OrderActionRequest,
    authorization: str = Header(..., alias="Authorization")
):
    """Process order approval/rejection"""
    auth = await require_auth(request, authorization=authorization)
    
    # Check admin role
    admin = await fetch_one("SELECT role FROM users WHERE user_id = $1", auth.user_id)
    if not admin or admin['role'] != 'admin':
        raise HTTPException(status_code=403, detail="Admin access required")
    
    # Get order
    order = await fetch_one("SELECT * FROM orders WHERE order_id = $1", order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    
    if order['status'] not in ['pending_review', 'awaiting_payment_proof', 'initiated']:
        raise HTTPException(status_code=400, detail=f"Cannot process order in '{order['status']}' status")
    
    now = datetime.now(timezone.utc)
    
    if data.action == 'approve':
        new_status = 'approved'
        
        # Update user balances based on order type
        if order['order_type'] == 'deposit':
            await execute('''
                UPDATE users 
                SET real_balance = real_balance + $1,
                    bonus_balance = bonus_balance + $2,
                    deposit_count = deposit_count + 1,
                    total_deposited = total_deposited + $3,
                    updated_at = NOW()
                WHERE user_id = $4
            ''', order['amount'], order['bonus_amount'], order['amount'], order['user_id'])
        elif order['order_type'] == 'withdrawal':
            await execute('''
                UPDATE users 
                SET real_balance = real_balance - $1,
                    total_withdrawn = total_withdrawn + $1,
                    updated_at = NOW()
                WHERE user_id = $2
            ''', order['amount'], order['user_id'])
        
        await execute('''
            UPDATE orders 
            SET status = $1, approved_by = $2, approved_at = $3, updated_at = NOW()
            WHERE order_id = $4
        ''', new_status, auth.user_id, now, order_id)
        
        await log_audit(auth.user_id, auth.username, "order.approved", "order", order_id, {
            "amount": order['amount'],
            "type": order['order_type']
        })
        
    elif data.action == 'reject':
        new_status = 'rejected'
        
        await execute('''
            UPDATE orders 
            SET status = $1, rejection_reason = $2, approved_by = $3, approved_at = $4, updated_at = NOW()
            WHERE order_id = $5
        ''', new_status, data.reason or 'Rejected by admin', auth.user_id, now, order_id)
        
        await log_audit(auth.user_id, auth.username, "order.rejected", "order", order_id, {
            "reason": data.reason
        })
    else:
        raise HTTPException(status_code=400, detail="Invalid action. Use 'approve' or 'reject'")
    
    # Send webhook notification
    await trigger_order_webhook(order_id, f"order.{new_status}")
    
    return {
        "success": True,
        "message": f"Order {new_status}",
        "order_id": order_id,
        "status": new_status
    }


@router.post(
    "/telegram/callback",
    summary="Telegram callback handler",
    description="Handle inline button callbacks from Telegram"
)
async def telegram_callback(request: Request):
    """Handle Telegram inline button callbacks"""
    try:
        body = await request.json()
        
        callback_query = body.get('callback_query', {})
        if not callback_query:
            return {"ok": True}  # Not a callback
        
        data = callback_query.get('data', '')
        message = callback_query.get('message', {})
        callback_query_id = callback_query.get('id')
        
        # Parse callback data: action:id
        parts = data.split(':')
        if len(parts) != 2:
            return await answer_callback(callback_query_id, "Invalid callback data")
        
        action, item_id = parts
        from_user = callback_query.get('from', {})
        admin_name = from_user.get('first_name', 'Admin')
        admin_id = str(from_user.get('id', 'telegram'))
        
        # Handle WALLET LOAD requests (wl_approve, wl_reject, wl_view)
        if action.startswith('wl_'):
            return await handle_wallet_load_callback(
                action, item_id, callback_query_id, message, admin_name, admin_id
            )
        
        # Handle ORDER requests (approve, reject, view)
        order_id = item_id
        order = await fetch_one("SELECT * FROM orders WHERE order_id = $1", order_id)
        if not order:
            return await answer_callback(callback_query_id, "Order not found")
        
        if action == 'approve':
            await execute('''
                UPDATE orders SET status = 'approved', approved_at = $1, updated_at = NOW()
                WHERE order_id = $2
            ''', datetime.now(timezone.utc), order_id)
            
            # Update balances if deposit
            if order['order_type'] == 'deposit':
                await execute('''
                    UPDATE users 
                    SET real_balance = real_balance + $1,
                        bonus_balance = bonus_balance + $2,
                        deposit_count = deposit_count + 1,
                        total_deposited = total_deposited + $1
                    WHERE user_id = $3
                ''', order['amount'], order['bonus_amount'], order['user_id'])
            
            await answer_callback(callback_query_id, "✅ Order Approved!")
            await update_telegram_message(message, f"✅ APPROVED by {admin_name}")
            
        elif action == 'reject':
            await execute('''
                UPDATE orders SET status = 'rejected', rejection_reason = 'Rejected via Telegram', approved_at = $1, updated_at = NOW()
                WHERE order_id = $2
            ''', datetime.now(timezone.utc), order_id)
            
            await answer_callback(callback_query_id, "❌ Order Rejected")
            await update_telegram_message(message, f"❌ REJECTED by {from_user.get('first_name', 'Admin')}")
            
        elif action == 'view':
            # Just show details
            details = f"""
📋 Order Details:
ID: {order_id}
User: {order['username']}
Type: {order['order_type']}
Amount: ${order['amount']}
Bonus: ${order['bonus_amount']}
Total: ${order['total_amount']}
Status: {order['status']}
            """
            await answer_callback(callback_query_id, details[:200], show_alert=True)
        
        return {"ok": True}
        
    except Exception as e:
        logger.error(f"Telegram callback error: {e}")
        return {"ok": False, "error": str(e)}


async def handle_wallet_load_callback(action: str, request_id: str, callback_query_id: str, 
                                       message: dict, admin_name: str, admin_id: str):
    """Handle wallet load approve/reject from Telegram"""
    from ..core.database import get_pool
    
    # Get the wallet load request
    load_request = await fetch_one("""
        SELECT wlr.*, u.username, u.display_name, u.real_balance as current_balance
        FROM wallet_load_requests wlr
        LEFT JOIN users u ON wlr.user_id = u.user_id
        WHERE wlr.request_id = $1
    """, request_id)
    
    if not load_request:
        return await answer_callback(callback_query_id, "Request not found")
    
    if load_request['status'] != 'pending':
        return await answer_callback(callback_query_id, f"Already {load_request['status']}")
    
    now = datetime.now(timezone.utc)
    
    if action == 'wl_approve':
        current_balance = float(load_request.get('current_balance', 0) or 0)
        new_balance = current_balance + load_request['amount']
        
        # Use transaction for atomicity
        pool = await get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                # Update user balance
                await conn.execute("""
                    UPDATE users SET real_balance = $1, updated_at = NOW()
                    WHERE user_id = $2
                """, new_balance, load_request['user_id'])
                
                # Update request status
                await conn.execute("""
                    UPDATE wallet_load_requests 
                    SET status = 'approved', reviewed_by = $1, reviewed_at = $2, updated_at = NOW()
                    WHERE request_id = $3
                """, admin_id, now, request_id)
                
                # Log to immutable ledger
                await conn.execute("""
                    INSERT INTO wallet_ledger 
                    (ledger_id, user_id, transaction_type, amount, balance_before, balance_after,
                     reference_type, reference_id, description, created_at)
                    VALUES ($1, $2, 'credit', $3, $4, $5, 'wallet_load', $6, $7, NOW())
                """, str(uuid.uuid4()), load_request['user_id'], load_request['amount'],
                   current_balance, new_balance, request_id, 
                   f"Wallet load via {load_request['payment_method']} - Approved by {admin_name}")
        
        await answer_callback(callback_query_id, f"✅ Approved! ₱{load_request['amount']:,.2f} credited")
        await update_telegram_message(message, f"✅ APPROVED by {admin_name}\n💰 ₱{load_request['amount']:,.2f} credited to @{load_request['username']}")
        
    elif action == 'wl_reject':
        await execute("""
            UPDATE wallet_load_requests 
            SET status = 'rejected', reviewed_by = $1, reviewed_at = $2, 
                rejection_reason = 'Rejected via Telegram', updated_at = NOW()
            WHERE request_id = $3
        """, admin_id, now, request_id)
        
        await answer_callback(callback_query_id, "❌ Request Rejected")
        await update_telegram_message(message, f"❌ REJECTED by {admin_name}")
        
    elif action == 'wl_view':
        details = f"""
💰 Wallet Load Request Details:
ID: {request_id[:8]}...
User: @{load_request['username']} ({load_request['display_name']})
Amount: ₱{load_request['amount']:,.2f}
Method: {load_request['payment_method']}
Status: {load_request['status']}
Current Balance: ₱{load_request.get('current_balance', 0):,.2f}
        """
        await answer_callback(callback_query_id, details[:200], show_alert=True)
    
    return {"ok": True}


# ==================== HELPER FUNCTIONS ====================

async def send_telegram_notification(order_id: str, order: dict, image_data: str = None, image_type: str = None) -> dict:
    """Send order notification to Telegram with inline buttons"""
    try:
        # Get telegram config
        config = await fetch_one("SELECT * FROM telegram_config WHERE id = 'default'")
        if not config or not config.get('bot_token') or not config.get('admin_chat_id'):
            logger.info("Telegram not configured, skipping notification")
            return {"sent": False, "reason": "not_configured"}
        
        bot_token = config['bot_token']
        chat_id = config['admin_chat_id']
        
        # Build message
        order_type_emoji = "💰" if order['order_type'] == 'deposit' else "💸"
        message_text = f"""
{order_type_emoji} <b>New {order['order_type'].upper()} Request</b>

👤 User: {order['username']}
🎮 Game: {order.get('game_display_name', order.get('game_name', 'N/A'))}
💵 Amount: ${order['amount']:.2f}
🎁 Bonus: ${order['bonus_amount']:.2f}
📊 Total: ${order['total_amount']:.2f}

📅 Created: {order['created_at'].strftime('%Y-%m-%d %H:%M') if order.get('created_at') else 'N/A'}
🆔 Order: <code>{order_id}</code>
        """
        
        # Build inline keyboard
        inline_actions = config.get('inline_actions', [])
        if isinstance(inline_actions, str):
            inline_actions = json.loads(inline_actions)
        
        keyboard = {
            "inline_keyboard": [
                [
                    {"text": "✅ Approve", "callback_data": f"approve:{order_id}"},
                    {"text": "❌ Reject", "callback_data": f"reject:{order_id}"}
                ],
                [
                    {"text": "👁 View Details", "callback_data": f"view:{order_id}"}
                ]
            ]
        }
        
        async with httpx.AsyncClient() as client:
            # Send photo if available
            if image_data:
                # Decode and send as photo
                photo_bytes = base64.b64decode(image_data)
                response = await client.post(
                    f"https://api.telegram.org/bot{bot_token}/sendPhoto",
                    data={
                        "chat_id": chat_id,
                        "caption": message_text,
                        "parse_mode": "HTML",
                        "reply_markup": json.dumps(keyboard)
                    },
                    files={"photo": ("proof.jpg", photo_bytes, image_type or "image/jpeg")}
                )
            else:
                # Send text message
                response = await client.post(
                    f"https://api.telegram.org/bot{bot_token}/sendMessage",
                    json={
                        "chat_id": chat_id,
                        "text": message_text,
                        "parse_mode": "HTML",
                        "reply_markup": keyboard
                    }
                )
            
            result = response.json()
            
            if result.get('ok'):
                # Store message ID for later updates
                msg = result.get('result', {})
                await execute('''
                    UPDATE orders 
                    SET telegram_message_id = $1, telegram_chat_id = $2
                    WHERE order_id = $3
                ''', str(msg.get('message_id')), chat_id, order_id)
                
                return {"sent": True, "message_id": msg.get('message_id')}
            else:
                logger.error(f"Telegram API error: {result}")
                return {"sent": False, "error": result.get('description')}
                
    except Exception as e:
        logger.error(f"Telegram notification error: {e}")
        return {"sent": False, "error": str(e)}


async def answer_callback(callback_query_id: str, text: str, show_alert: bool = False) -> dict:
    """Answer Telegram callback query"""
    try:
        config = await fetch_one("SELECT bot_token FROM telegram_config WHERE id = 'default'")
        if not config:
            return {}
        
        async with httpx.AsyncClient() as client:
            await client.post(
                f"https://api.telegram.org/bot{config['bot_token']}/answerCallbackQuery",
                json={
                    "callback_query_id": callback_query_id,
                    "text": text,
                    "show_alert": show_alert
                }
            )
        return {}
    except Exception as e:
        logger.error(f"Answer callback error: {e}")
        return {}


async def update_telegram_message(message: dict, suffix: str):
    """Update Telegram message with result"""
    try:
        config = await fetch_one("SELECT bot_token FROM telegram_config WHERE id = 'default'")
        if not config:
            return
        
        chat_id = message.get('chat', {}).get('id')
        message_id = message.get('message_id')
        original_text = message.get('text', '') or message.get('caption', '')
        
        async with httpx.AsyncClient() as client:
            await client.post(
                f"https://api.telegram.org/bot{config['bot_token']}/editMessageCaption",
                json={
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "caption": f"{original_text}\n\n{suffix}",
                    "parse_mode": "HTML"
                }
            )
    except Exception as e:
        logger.error(f"Update message error: {e}")


async def trigger_order_webhook(order_id: str, event: str):
    """Trigger webhook for order event"""
    from ..services.webhook_service import deliver_webhook
    
    order = await fetch_one("SELECT * FROM orders WHERE order_id = $1", order_id)
    if not order:
        return
    
    # Get user's webhooks
    webhooks = await fetch_all(
        "SELECT * FROM webhooks WHERE user_id = $1 AND is_active = TRUE AND $2 = ANY(subscribed_events)",
        order['user_id'], event
    )
    
    for webhook in webhooks:
        await deliver_webhook(webhook, event, {
            "order_id": order_id,
            "username": order['username'],
            "amount": order['amount'],
            "status": order['status']
        })


async def log_audit(user_id, username, action, resource_type, resource_id, details=None):
    """Log audit event"""
    log_id = str(uuid.uuid4())
    await execute('''
        INSERT INTO audit_logs (log_id, user_id, username, action, resource_type, resource_id, details)
        VALUES ($1, $2, $3, $4, $5, $6, $7)
    ''', log_id, user_id, username, action, resource_type, resource_id,
       json.dumps(details) if details else None)
