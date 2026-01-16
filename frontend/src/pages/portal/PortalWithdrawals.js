import React, { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import axios from 'axios';
import { useAuth } from '../../context/AuthContext';
import PortalLayout from '../../components/PortalLayout';
import '../../styles/portal-design-system.css';
import { 
  ArrowDownCircle, CheckCircle, Clock, XCircle, Info
} from 'lucide-react';

const BACKEND_URL = process.env.REACT_APP_BACKEND_URL || 'http://localhost:8001';

const PortalWithdrawals = () => {
  const navigate = useNavigate();
  const { clientToken, portalToken } = useAuth();
  const [loading, setLoading] = useState(true);
  const [withdrawals, setWithdrawals] = useState([]);
  const [cashoutPreview, setCashoutPreview] = useState(null);

  const getAuthHeaders = () => {
    if (clientToken) return { Authorization: `Bearer ${clientToken}` };
    if (portalToken) return { 'X-Portal-Token': portalToken };
    return {};
  };

  useEffect(() => {
    fetchData();
  }, []);

  const fetchData = async () => {
    try {
      const [previewRes, txRes] = await Promise.all([
        axios.get(`${BACKEND_URL}/api/v1/portal/wallet/cashout-preview`, { headers: getAuthHeaders() }).catch(() => ({ data: null })),
        axios.get(`${BACKEND_URL}/api/v1/portal/transactions/enhanced?type_filter=withdrawal`, { headers: getAuthHeaders() }).catch(() => ({ data: { transactions: [] } }))
      ]);
      setCashoutPreview(previewRes.data);
      setWithdrawals(txRes.data?.transactions || []);
    } catch (error) {
      console.error('Failed to fetch withdrawal data:', error);
      // Mock data for UI demo
      setCashoutPreview({
        can_withdraw: true,
        preview: { payout_amount: 850.00 }
      });
      setWithdrawals([
        { order_id: 'w1', amount: 200, status: 'approved', game: 'Dragon Quest', created_at: new Date(Date.now() - 259200000).toISOString() },
        { order_id: 'w2', amount: 150, status: 'pending_review', game: 'Battle Arena', created_at: new Date(Date.now() - 86400000).toISOString() }
      ]);
    } finally {
      setLoading(false);
    }
  };

  const getStatusChip = (status) => {
    const statusMap = {
      'approved': { class: 'status-chip-success', label: 'Completed', icon: CheckCircle },
      'confirmed': { class: 'status-chip-success', label: 'Completed', icon: CheckCircle },
      'rejected': { class: 'status-chip-error', label: 'Rejected', icon: XCircle },
      'cancelled': { class: 'status-chip-error', label: 'Cancelled', icon: XCircle }
    };
    const config = statusMap[status] || { class: 'status-chip-warning', label: 'Pending', icon: Clock };
    const Icon = config.icon;
    return (
      <span className={`status-chip ${config.class}`}>
        <Icon style={{ width: 12, height: 12 }} />
        {config.label}
      </span>
    );
  };

  const totalPending = withdrawals
    .filter(w => !['approved', 'confirmed', 'rejected', 'cancelled'].includes(w.status))
    .reduce((sum, w) => sum + (w.amount || 0), 0);
  const totalCompleted = withdrawals
    .filter(w => ['approved', 'confirmed'].includes(w.status))
    .reduce((sum, w) => sum + (w.payout_amount || w.amount || 0), 0);

  if (loading) {
    return (
      <PortalLayout title="Withdrawals">
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', minHeight: '50vh' }}>
          <div className="animate-spin rounded-full h-8 w-8 border-t-2 border-b-2" style={{ borderColor: 'var(--portal-accent)' }}></div>
        </div>
      </PortalLayout>
    );
  }

  return (
    <PortalLayout title="Withdrawals">
      {/* Summary Stats */}
      <div className="stats-row portal-section" data-testid="withdrawal-stats">
        <div className="stat-card">
          <p className="stat-label">Pending</p>
          <p className="stat-value stat-value-warning">${totalPending.toFixed(2)}</p>
        </div>
        <div className="stat-card">
          <p className="stat-label">Completed</p>
          <p className="stat-value stat-value-success">${totalCompleted.toFixed(2)}</p>
        </div>
      </div>

      {/* Cashout Preview */}
      {cashoutPreview && (
        <div className="portal-card portal-section" data-testid="cashout-preview">
          <div className="portal-card-header">
            <div className="portal-card-title">
              <ArrowDownCircle style={{ width: 20, height: 20, color: cashoutPreview.can_withdraw ? 'var(--portal-success)' : 'var(--portal-text-muted)' }} />
              Cashout Preview
            </div>
          </div>
          
          {cashoutPreview.can_withdraw ? (
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
              <span style={{ color: 'var(--portal-text-secondary)', fontSize: 'var(--text-sm)' }}>Available</span>
              <span className="stat-value stat-value-success" style={{ fontSize: 'var(--text-lg)' }}>
                ${(cashoutPreview.preview?.payout_amount || 0).toFixed(2)}
              </span>
            </div>
          ) : (
            <div className="status-chip status-chip-neutral">
              {cashoutPreview.block_reason || 'No balance available'}
            </div>
          )}
        </div>
      )}

      {/* Withdrawal History */}
      <div className="portal-section">
        <p className="portal-section-title">History</p>
        
        {withdrawals.length === 0 ? (
          <div className="portal-empty" data-testid="empty-withdrawals">
            <ArrowDownCircle className="portal-empty-icon" />
            <p className="portal-empty-title">No withdrawals yet</p>
            <p className="portal-empty-text">Request a withdrawal through Wallet or Messenger</p>
          </div>
        ) : (
          <div className="portal-list" data-testid="withdrawals-list">
            {withdrawals.map((w) => (
              <div key={w.order_id || w.transaction_id} className="portal-list-item" data-testid={`withdrawal-${w.order_id || w.transaction_id}`}>
                <div className="portal-list-item-left">
                  <div className="portal-list-item-content">
                    <span className="portal-list-item-title">
                      {w.game || 'Withdrawal'}
                    </span>
                    <span className="portal-list-item-subtitle">
                      {w.created_at ? new Date(w.created_at).toLocaleDateString() : 'N/A'}
                    </span>
                  </div>
                </div>
                <div className="portal-list-item-right">
                  <p className="portal-list-item-value" style={{ color: 'var(--portal-error)', marginBottom: 'var(--space-xs)' }}>
                    -${(w.amount || 0).toFixed(2)}
                  </p>
                  {getStatusChip(w.status)}
                </div>
              </div>
            ))}
          </div>
        )}
      </div>

      {/* Help Info */}
      <div className="portal-info" data-testid="withdrawal-help">
        <Info className="portal-info-icon" />
        <p className="portal-info-text">
          To request a withdrawal, go to Wallet → Cashout tab or message us on Messenger.
        </p>
      </div>
    </PortalLayout>
  );
};

export default PortalWithdrawals;
