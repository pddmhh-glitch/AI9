"""
API v1 Wallet Routes - Wallet Funding System
Payment QR retrieval, Wallet load requests, Status tracking
"""
from fastapi import APIRouter, Request, HTTPException, status, Header, UploadFile, File, Form
from typing import Optional, List
from pydantic import BaseModel, Field
from datetime import datetime, timezone
import uuid
import hashlib
import json
import base64
import httpx
import logging

from ..core.database import fetch_one, fetch_all, execute, get_pool
from ..core.config import get_api_settings
from .dependencies import check_rate_limiting

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/wallet", tags=["Wallet"])
settings = get_api_settings()


# ==================== MODELS ====================

class WalletLoadRequest(BaseModel):
    """Wallet load request body"""
    amount: float = Field(..., gt=0, description="Amount to load")
    payment_method: str = Field(..., description="Payment method (e.g., GCash, USDT, Bank)")
    proof_image: str = Field(..., description="Base64 encoded payment proof image")


class WalletLoadResponse(BaseModel):
    """Wallet load response"""
    success: bool
    request_id: str
    message: str
    status: str


# ==================== AUTH HELPER ====================

async def get_wallet_user(request: Request, portal_token: Optional[str], client_token: Optional[str]):
    """Authenticate wallet user from either token"""
    user_id = None
    
    if client_token:
        from ..core.security import decode_jwt_token
        payload = decode_jwt_token(client_token)
        if payload:
            user_id = payload.get('sub') or payload.get('user_id')
    elif portal_token:
        session = await fetch_one("""
            SELECT user_id FROM portal_sessions 
            WHERE session_token = $1 AND expires_at > NOW()
        """, portal_token)
        if session:
            user_id = session['user_id']
    
    if not user_id:
        raise HTTPException(status_code=401, detail="Authentication required")
    
    user = await fetch_one("SELECT * FROM users WHERE user_id = $1", user_id)
    if not user:
        raise HTTPException(status_code=401, detail="User not found")
    
    return user


async def log_wallet_transaction(user_id: str, txn_type: str, amount: float, 
                                  balance_before: float, balance_after: float,
                                  ref_type: str = None, ref_id: str = None, description: str = None):
    """Log immutable wallet transaction to ledger"""
    ledger_id = str(uuid.uuid4())
    await execute("""
        INSERT INTO wallet_ledger (ledger_id, user_id, transaction_type, amount, 
                                   balance_before, balance_after, reference_type, 
                                   reference_id, description, created_at)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, NOW())
    """, ledger_id, user_id, txn_type, amount, balance_before, balance_after, 
       ref_type, ref_id, description)
    return ledger_id


# ==================== PUBLIC ENDPOINTS ====================

@router.get("/qr")
async def get_payment_qr(
    request: Request,
    payment_method: Optional[str] = None,
    x_portal_token: Optional[str] = Header(None, alias="X-Portal-Token"),
    authorization: Optional[str] = Header(None, alias="Authorization")
):
    """
    GET /api/v1/wallet/qr
    Returns active payment QR codes for wallet funding
    """
    await check_rate_limiting(request)
    
    # Get client token
    client_token = authorization.replace("Bearer ", "") if authorization else None
    await get_wallet_user(request, x_portal_token, client_token)
    
    # Get active QR codes
    if payment_method:
        qr_codes = await fetch_all("""
            SELECT qr_id, payment_method, label, account_name, account_number, 
                   image_url, is_default
            FROM payment_qr 
            WHERE is_active = TRUE AND payment_method = $1
            ORDER BY is_default DESC, created_at DESC
        """, payment_method)
    else:
        qr_codes = await fetch_all("""
            SELECT qr_id, payment_method, label, account_name, account_number, 
                   image_url, is_default
            FROM payment_qr 
            WHERE is_active = TRUE
            ORDER BY payment_method, is_default DESC, created_at DESC
        """)
    
    # Get unique payment methods
    methods = await fetch_all("""
        SELECT DISTINCT payment_method FROM payment_qr WHERE is_active = TRUE
    """)
    
    return {
        "payment_methods": [m['payment_method'] for m in methods],
        "qr_codes": [dict(qr) for qr in qr_codes]
    }


@router.post("/load-request")
async def create_wallet_load_request(
    request: Request,
    data: WalletLoadRequest,
    x_portal_token: Optional[str] = Header(None, alias="X-Portal-Token"),
    authorization: Optional[str] = Header(None, alias="Authorization")
):
    """
    POST /api/v1/wallet/load-request
    Create a wallet load request with payment proof
    """
    await check_rate_limiting(request)
    
    client_token = authorization.replace("Bearer ", "") if authorization else None
    user = await get_wallet_user(request, x_portal_token, client_token)
    
    # Validate amount
    if data.amount < 10:
        raise HTTPException(status_code=400, detail="Minimum load amount is 10")
    if data.amount > 100000:
        raise HTTPException(status_code=400, detail="Maximum load amount is 100,000")
    
    # Check for active QR for payment method
    qr = await fetch_one("""
        SELECT qr_id FROM payment_qr 
        WHERE payment_method = $1 AND is_active = TRUE
        ORDER BY is_default DESC LIMIT 1
    """, data.payment_method)
    
    if not qr:
        raise HTTPException(status_code=400, detail=f"Payment method '{data.payment_method}' is not available")
    
    # Generate image hash for duplicate detection
    try:
        # Take first 1000 chars of base64 for hash (enough to detect duplicates)
        image_sample = data.proof_image[:1000] if len(data.proof_image) > 1000 else data.proof_image
        proof_hash = hashlib.sha256(image_sample.encode()).hexdigest()
    except Exception:
        proof_hash = None
    
    # Check for duplicate proof
    if proof_hash:
        duplicate = await fetch_one("""
            SELECT request_id FROM wallet_load_requests 
            WHERE proof_image_hash = $1 AND status != 'rejected'
        """, proof_hash)
        if duplicate:
            raise HTTPException(
                status_code=400, 
                detail="This payment proof has already been submitted. Please use a unique screenshot."
            )
    
    # Check for pending requests limit (max 3 pending per user)
    pending_count = await fetch_one("""
        SELECT COUNT(*) as count FROM wallet_load_requests 
        WHERE user_id = $1 AND status = 'pending'
    """, user['user_id'])
    
    if pending_count and pending_count['count'] >= 3:
        raise HTTPException(
            status_code=400, 
            detail="You have too many pending requests. Please wait for them to be processed."
        )
    
    # Get client IP and create fingerprint
    client_ip = request.client.host if request.client else None
    user_agent = request.headers.get('user-agent', '')
    device_fp = hashlib.md5(f"{client_ip}:{user_agent}".encode()).hexdigest()[:32]
    
    # Create request
    request_id = str(uuid.uuid4())
    
    # NO PROOF URL STORED - image forwarded to Telegram only
    # Store only hash for duplicate detection
    await execute("""
        INSERT INTO wallet_load_requests 
        (request_id, user_id, amount, payment_method, qr_id, 
         proof_image_hash, status, ip_address, device_fingerprint, created_at)
        VALUES ($1, $2, $3, $4, $5, $6, 'pending', $7, $8, NOW())
    """, request_id, user['user_id'], data.amount, data.payment_method, 
       qr['qr_id'], proof_hash, client_ip, device_fp)
    
    # Log audit
    await execute("""
        INSERT INTO audit_logs (log_id, user_id, username, action, resource_type, resource_id, details, ip_address, created_at)
        VALUES ($1, $2, $3, 'wallet.load_request_created', 'wallet_load', $4, $5, $6, NOW())
    """, str(uuid.uuid4()), user['user_id'], user['username'], request_id,
       json.dumps({"amount": data.amount, "method": data.payment_method}), client_ip)
    
    # Send notification via NotificationRouter (multi-bot system)
    # Image forwarded to Telegram but NOT stored in DB
    from ..core.notification_router import emit_event, EventType
    
    await emit_event(
        event_type=EventType.WALLET_LOAD_REQUESTED,
        title="New Wallet Load Request",
        message=f"Client {user['display_name']} requested ₱{data.amount:,.2f} via {data.payment_method}.\n\nPlease review the payment proof and approve or reject.",
        reference_id=request_id,
        reference_type="wallet_load",
        user_id=user['user_id'],
        username=user['username'],
        display_name=user.get('display_name'),
        amount=data.amount,
        extra_data={
            "payment_method": data.payment_method,
            "proof_image": data.proof_image  # Forward to Telegram, not stored
        },
        requires_action=True,
        entity_type="wallet_load"  # STANDARDIZED: action:wallet_load:request_id
    )
    
    return {
        "success": True,
        "request_id": request_id,
        "message": "Wallet load request submitted successfully. Awaiting admin review.",
        "status": "pending"
    }


@router.get("/load-status/{request_id}")
async def get_wallet_load_status(
    request: Request,
    request_id: str,
    x_portal_token: Optional[str] = Header(None, alias="X-Portal-Token"),
    authorization: Optional[str] = Header(None, alias="Authorization")
):
    """
    GET /api/v1/wallet/load-status/{request_id}
    Check status of a wallet load request
    """
    await check_rate_limiting(request)
    
    client_token = authorization.replace("Bearer ", "") if authorization else None
    user = await get_wallet_user(request, x_portal_token, client_token)
    
    load_request = await fetch_one("""
        SELECT request_id, amount, payment_method, status, rejection_reason, 
               created_at, reviewed_at
        FROM wallet_load_requests 
        WHERE request_id = $1 AND user_id = $2
    """, request_id, user['user_id'])
    
    if not load_request:
        raise HTTPException(status_code=404, detail="Request not found")
    
    return {
        "request_id": load_request['request_id'],
        "amount": load_request['amount'],
        "payment_method": load_request['payment_method'],
        "status": load_request['status'],
        "rejection_reason": load_request['rejection_reason'],
        "created_at": load_request['created_at'].isoformat() if load_request['created_at'] else None,
        "reviewed_at": load_request['reviewed_at'].isoformat() if load_request['reviewed_at'] else None
    }


@router.get("/load-history")
async def get_wallet_load_history(
    request: Request,
    limit: int = 20,
    offset: int = 0,
    x_portal_token: Optional[str] = Header(None, alias="X-Portal-Token"),
    authorization: Optional[str] = Header(None, alias="Authorization")
):
    """
    GET /api/v1/wallet/load-history
    Get user's wallet load request history
    """
    await check_rate_limiting(request)
    
    client_token = authorization.replace("Bearer ", "") if authorization else None
    user = await get_wallet_user(request, x_portal_token, client_token)
    
    requests = await fetch_all("""
        SELECT request_id, amount, payment_method, status, rejection_reason, 
               created_at, reviewed_at
        FROM wallet_load_requests 
        WHERE user_id = $1
        ORDER BY created_at DESC
        LIMIT $2 OFFSET $3
    """, user['user_id'], limit, offset)
    
    total = await fetch_one("""
        SELECT COUNT(*) as count FROM wallet_load_requests WHERE user_id = $1
    """, user['user_id'])
    
    return {
        "requests": [
            {
                **dict(r),
                "created_at": r['created_at'].isoformat() if r['created_at'] else None,
                "reviewed_at": r['reviewed_at'].isoformat() if r['reviewed_at'] else None
            }
            for r in requests
        ],
        "total": total['count'] if total else 0
    }


@router.get("/balance")
async def get_wallet_balance(
    request: Request,
    x_portal_token: Optional[str] = Header(None, alias="X-Portal-Token"),
    authorization: Optional[str] = Header(None, alias="Authorization")
):
    """
    GET /api/v1/wallet/balance
    Get current wallet balance (only source for game loading)
    """
    await check_rate_limiting(request)
    
    client_token = authorization.replace("Bearer ", "") if authorization else None
    user = await get_wallet_user(request, x_portal_token, client_token)
    
    # Get pending loads
    pending = await fetch_one("""
        SELECT COALESCE(SUM(amount), 0) as pending_amount
        FROM wallet_load_requests 
        WHERE user_id = $1 AND status = 'pending'
    """, user['user_id'])
    
    return {
        "wallet_balance": float(user.get('real_balance', 0) or 0),
        "bonus_balance": float(user.get('bonus_balance', 0) or 0),
        "play_credits": float(user.get('play_credits', 0) or 0),
        "pending_loads": float(pending['pending_amount'] or 0),
        "can_load_games": float(user.get('real_balance', 0) or 0) > 0
    }


@router.get("/ledger")
async def get_wallet_ledger(
    request: Request,
    limit: int = 50,
    offset: int = 0,
    x_portal_token: Optional[str] = Header(None, alias="X-Portal-Token"),
    authorization: Optional[str] = Header(None, alias="Authorization")
):
    """
    GET /api/v1/wallet/ledger
    Get immutable wallet transaction ledger
    """
    await check_rate_limiting(request)
    
    client_token = authorization.replace("Bearer ", "") if authorization else None
    user = await get_wallet_user(request, x_portal_token, client_token)
    
    transactions = await fetch_all("""
        SELECT ledger_id, transaction_type, amount, balance_before, balance_after,
               reference_type, reference_id, description, created_at
        FROM wallet_ledger 
        WHERE user_id = $1
        ORDER BY created_at DESC
        LIMIT $2 OFFSET $3
    """, user['user_id'], limit, offset)
    
    return {
        "transactions": [
            {
                **dict(t),
                "created_at": t['created_at'].isoformat() if t['created_at'] else None
            }
            for t in transactions
        ]
    }


# ==================== TELEGRAM NOTIFICATION ====================

async def send_wallet_load_telegram(request_id: str, user: dict, amount: float, 
                                     payment_method: str, proof_image: str):
    """Send wallet load request notification to Telegram with approve/reject buttons"""
    try:
        # Get telegram config
        config = await fetch_one("SELECT * FROM telegram_config WHERE id = 'default'")
        if not config or not config.get('bot_token') or not config.get('admin_chat_id'):
            logger.info("Telegram not configured, skipping notification")
            return {"sent": False, "reason": "not_configured"}
        
        bot_token = config['bot_token']
        chat_id = config['admin_chat_id']
        
        # Build message
        message = f"""💰 *NEW WALLET LOAD REQUEST*

👤 *User:* {user['display_name']} (@{user['username']})
🆔 *User ID:* `{user['user_id'][:8]}...`
💵 *Amount:* ₱{amount:,.2f}
💳 *Method:* {payment_method}
📋 *Request ID:* `{request_id[:8]}...`
⏰ *Time:* {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}

Please review the payment proof below and approve or reject."""

        # Inline keyboard for actions
        keyboard = {
            "inline_keyboard": [
                [
                    {"text": "✅ Approve", "callback_data": f"wl_approve:{request_id}"},
                    {"text": "❌ Reject", "callback_data": f"wl_reject:{request_id}"}
                ],
                [
                    {"text": "👁 View Details", "callback_data": f"wl_view:{request_id}"}
                ]
            ]
        }
        
        async with httpx.AsyncClient(timeout=30.0) as client:
            # Send text message first
            msg_response = await client.post(
                f"https://api.telegram.org/bot{bot_token}/sendMessage",
                json={
                    "chat_id": chat_id,
                    "text": message,
                    "parse_mode": "Markdown",
                    "reply_markup": keyboard
                }
            )
            
            if msg_response.status_code == 200:
                msg_data = msg_response.json()
                message_id = msg_data.get('result', {}).get('message_id')
                
                # Try to send proof image
                if proof_image:
                    try:
                        # Decode base64 and send as photo
                        image_bytes = base64.b64decode(proof_image)
                        await client.post(
                            f"https://api.telegram.org/bot{bot_token}/sendPhoto",
                            data={"chat_id": chat_id, "caption": f"Payment proof for request {request_id[:8]}..."},
                            files={"photo": ("proof.jpg", image_bytes, "image/jpeg")}
                        )
                    except Exception as img_err:
                        logger.warning(f"Failed to send proof image: {img_err}")
                
                return {"sent": True, "message_id": message_id, "chat_id": chat_id}
            else:
                logger.error(f"Telegram API error: {msg_response.text}")
                return {"sent": False, "error": msg_response.text}
                
    except Exception as e:
        logger.error(f"Failed to send Telegram notification: {e}")
        return {"sent": False, "error": str(e)}


# ==================== WEBHOOK FOR TELEGRAM REVIEW ====================

@router.post("/review")
async def process_wallet_load_review(request: Request):
    """
    POST /api/v1/wallet/review
    Process wallet load approval/rejection from Telegram
    """
    try:
        body = await request.json()
        
        request_id = body.get('request_id')
        action = body.get('action')  # APPROVE or REJECT
        admin_id = body.get('admin_id')
        rejection_reason = body.get('reason')
        
        if not request_id or not action:
            raise HTTPException(status_code=400, detail="Missing request_id or action")
        
        # Get the load request
        load_request = await fetch_one("""
            SELECT * FROM wallet_load_requests WHERE request_id = $1
        """, request_id)
        
        if not load_request:
            raise HTTPException(status_code=404, detail="Request not found")
        
        if load_request['status'] != 'pending':
            raise HTTPException(status_code=400, detail=f"Request already {load_request['status']}")
        
        now = datetime.now(timezone.utc)
        
        if action.upper() == 'APPROVE':
            # Get user's current balance
            user = await fetch_one("SELECT * FROM users WHERE user_id = $1", load_request['user_id'])
            if not user:
                raise HTTPException(status_code=404, detail="User not found")
            
            current_balance = float(user.get('real_balance', 0) or 0)
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
                       f"Wallet load via {load_request['payment_method']}")
            
            # Emit approval notification
            from ..core.notification_router import emit_event, EventType
            await emit_event(
                event_type=EventType.WALLET_LOAD_APPROVED,
                title="Wallet Load Approved",
                message=f"Wallet load of ₱{load_request['amount']:,.2f} has been approved.\n\nNew balance: ₱{new_balance:,.2f}",
                reference_id=request_id,
                reference_type="wallet_load",
                user_id=load_request['user_id'],
                username=user.get('username'),
                display_name=user.get('display_name'),
                amount=load_request['amount'],
                extra_data={"new_balance": new_balance, "payment_method": load_request['payment_method']},
                requires_action=False
            )
            
            return {
                "success": True,
                "action": "approved",
                "request_id": request_id,
                "amount_credited": load_request['amount'],
                "new_balance": new_balance
            }
            
        elif action.upper() == 'REJECT':
            # Get user for notification
            user = await fetch_one("SELECT * FROM users WHERE user_id = $1", load_request['user_id'])
            
            await execute("""
                UPDATE wallet_load_requests 
                SET status = 'rejected', reviewed_by = $1, reviewed_at = $2, 
                    rejection_reason = $3, updated_at = NOW()
                WHERE request_id = $4
            """, admin_id, now, rejection_reason or "Rejected by admin", request_id)
            
            # Emit rejection notification
            from ..core.notification_router import emit_event, EventType
            await emit_event(
                event_type=EventType.WALLET_LOAD_REJECTED,
                title="Wallet Load Rejected",
                message=f"Wallet load of ₱{load_request['amount']:,.2f} has been rejected.\n\nReason: {rejection_reason or 'Rejected by admin'}",
                reference_id=request_id,
                reference_type="wallet_load",
                user_id=load_request['user_id'],
                username=user.get('username') if user else None,
                display_name=user.get('display_name') if user else None,
                amount=load_request['amount'],
                extra_data={"reason": rejection_reason or "Rejected by admin", "payment_method": load_request['payment_method']},
                requires_action=False
            )
            
            return {
                "success": True,
                "action": "rejected",
                "request_id": request_id,
                "reason": rejection_reason
            }
        else:
            raise HTTPException(status_code=400, detail="Invalid action. Use APPROVE or REJECT")
            
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error processing wallet load review: {e}")
        raise HTTPException(status_code=500, detail=str(e))
