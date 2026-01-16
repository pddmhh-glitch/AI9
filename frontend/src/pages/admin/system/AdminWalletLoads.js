import React, { useState, useEffect } from 'react';
import { useAuth } from '../../../context/AuthContext';
import axios from 'axios';
import { 
  Wallet, Check, X, RefreshCw, Clock, Eye, User, 
  CreditCard, Image, AlertCircle, CheckCircle, XCircle
} from 'lucide-react';

const API = process.env.REACT_APP_BACKEND_URL;

const AdminWalletLoads = () => {
  const { token } = useAuth();
  const [loading, setLoading] = useState(true);
  const [requests, setRequests] = useState([]);
  const [statusFilter, setStatusFilter] = useState('pending');
  const [selectedRequest, setSelectedRequest] = useState(null);
  const [processing, setProcessing] = useState(false);
  const [rejectReason, setRejectReason] = useState('');

  const config = { headers: { Authorization: `Bearer ${token}` } };

  useEffect(() => {
    fetchRequests();
  }, [statusFilter]);

  const fetchRequests = async () => {
    setLoading(true);
    try {
      const params = statusFilter !== 'all' ? `?status_filter=${statusFilter}` : '';
      const response = await axios.get(`${API}/api/v1/admin/system/wallet-loads${params}`, config);
      setRequests(response.data.requests || []);
    } catch (error) {
      console.error('Failed to fetch wallet loads:', error);
    } finally {
      setLoading(false);
    }
  };

  const handleApprove = async (requestId) => {
    if (!window.confirm('Approve this wallet load request? This will credit the user\'s wallet.')) return;
    
    setProcessing(true);
    try {
      await axios.post(`${API}/api/v1/wallet/review`, {
        request_id: requestId,
        action: 'APPROVE',
        admin_id: 'admin-web'
      }, config);
      
      alert('Request approved successfully!');
      setSelectedRequest(null);
      fetchRequests();
    } catch (error) {
      alert(error.response?.data?.detail || 'Failed to approve request');
    } finally {
      setProcessing(false);
    }
  };

  const handleReject = async (requestId) => {
    if (!rejectReason.trim()) {
      alert('Please provide a reason for rejection');
      return;
    }
    
    setProcessing(true);
    try {
      await axios.post(`${API}/api/v1/wallet/review`, {
        request_id: requestId,
        action: 'REJECT',
        admin_id: 'admin-web',
        reason: rejectReason
      }, config);
      
      alert('Request rejected');
      setSelectedRequest(null);
      setRejectReason('');
      fetchRequests();
    } catch (error) {
      alert(error.response?.data?.detail || 'Failed to reject request');
    } finally {
      setProcessing(false);
    }
  };

  const getStatusBadge = (status) => {
    const styles = {
      pending: { bg: 'bg-yellow-500/20', text: 'text-yellow-400', icon: Clock },
      approved: { bg: 'bg-emerald-500/20', text: 'text-emerald-400', icon: CheckCircle },
      rejected: { bg: 'bg-red-500/20', text: 'text-red-400', icon: XCircle }
    };
    const style = styles[status] || styles.pending;
    const Icon = style.icon;
    
    return (
      <span className={`inline-flex items-center gap-1 px-2 py-1 rounded-full text-xs font-medium ${style.bg} ${style.text}`}>
        <Icon className="w-3 h-3" />
        {status.charAt(0).toUpperCase() + status.slice(1)}
      </span>
    );
  };

  const formatDate = (dateStr) => {
    if (!dateStr) return 'N/A';
    return new Date(dateStr).toLocaleString();
  };

  return (
    <div className="space-y-6" data-testid="wallet-loads-management">
      {/* Header */}
      <div className="flex items-center justify-between flex-wrap gap-4">
        <div>
          <h1 className="text-2xl font-bold text-white">Wallet Load Requests</h1>
          <p className="text-gray-400 text-sm">Review and approve wallet funding requests</p>
        </div>
        <div className="flex gap-2">
          {/* Status Filter */}
          <select
            value={statusFilter}
            onChange={(e) => setStatusFilter(e.target.value)}
            className="px-4 py-2 bg-gray-800 border border-gray-700 rounded-lg text-white"
          >
            <option value="pending">Pending</option>
            <option value="approved">Approved</option>
            <option value="rejected">Rejected</option>
            <option value="all">All</option>
          </select>
          <button
            onClick={fetchRequests}
            className="p-2 bg-gray-800 hover:bg-gray-700 rounded-lg transition"
          >
            <RefreshCw className={`w-5 h-5 text-gray-400 ${loading ? 'animate-spin' : ''}`} />
          </button>
        </div>
      </div>

      {/* Pending Count Alert */}
      {statusFilter === 'pending' && requests.length > 0 && (
        <div className="bg-yellow-500/10 border border-yellow-500/30 rounded-xl p-4 flex items-center gap-3">
          <AlertCircle className="w-5 h-5 text-yellow-400" />
          <p className="text-yellow-400">
            <strong>{requests.length}</strong> pending wallet load request{requests.length !== 1 ? 's' : ''} awaiting review
          </p>
        </div>
      )}

      {/* Requests Table */}
      {loading ? (
        <div className="flex items-center justify-center h-64">
          <RefreshCw className="w-8 h-8 text-emerald-500 animate-spin" />
        </div>
      ) : requests.length === 0 ? (
        <div className="bg-gray-900 border border-gray-800 rounded-xl p-8 text-center">
          <Wallet className="w-16 h-16 text-gray-600 mx-auto mb-4" />
          <h3 className="text-white font-medium mb-2">No {statusFilter !== 'all' ? statusFilter : ''} Requests</h3>
          <p className="text-gray-400 text-sm">
            {statusFilter === 'pending' 
              ? 'All caught up! No pending wallet load requests.' 
              : 'No requests found with this filter.'}
          </p>
        </div>
      ) : (
        <div className="bg-gray-900 border border-gray-800 rounded-xl overflow-hidden">
          <div className="overflow-x-auto">
            <table className="w-full">
              <thead className="bg-gray-800/50">
                <tr>
                  <th className="px-4 py-3 text-left text-sm font-medium text-gray-400">User</th>
                  <th className="px-4 py-3 text-left text-sm font-medium text-gray-400">Amount</th>
                  <th className="px-4 py-3 text-left text-sm font-medium text-gray-400">Method</th>
                  <th className="px-4 py-3 text-left text-sm font-medium text-gray-400">Status</th>
                  <th className="px-4 py-3 text-left text-sm font-medium text-gray-400">Date</th>
                  <th className="px-4 py-3 text-right text-sm font-medium text-gray-400">Actions</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-800">
                {requests.map((req) => (
                  <tr key={req.request_id} className="hover:bg-gray-800/30">
                    <td className="px-4 py-3">
                      <div className="flex items-center gap-2">
                        <div className="w-8 h-8 bg-gray-700 rounded-full flex items-center justify-center">
                          <User className="w-4 h-4 text-gray-400" />
                        </div>
                        <div>
                          <p className="text-white font-medium">{req.display_name || 'Unknown'}</p>
                          <p className="text-gray-500 text-xs">@{req.username}</p>
                        </div>
                      </div>
                    </td>
                    <td className="px-4 py-3">
                      <span className="text-emerald-400 font-bold">₱{req.amount?.toFixed(2)}</span>
                    </td>
                    <td className="px-4 py-3">
                      <span className="inline-flex items-center gap-1 px-2 py-1 bg-gray-700 rounded text-sm text-gray-300">
                        <CreditCard className="w-3 h-3" />
                        {req.payment_method}
                      </span>
                    </td>
                    <td className="px-4 py-3">
                      {getStatusBadge(req.status)}
                    </td>
                    <td className="px-4 py-3 text-gray-400 text-sm">
                      {formatDate(req.created_at)}
                    </td>
                    <td className="px-4 py-3 text-right">
                      <button
                        onClick={() => setSelectedRequest(req)}
                        className="px-3 py-1.5 bg-blue-500/20 text-blue-400 rounded hover:bg-blue-500/30 text-sm flex items-center gap-1 ml-auto"
                      >
                        <Eye className="w-4 h-4" />
                        View
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* Detail Modal */}
      {selectedRequest && (
        <div className="fixed inset-0 bg-black/80 z-50 flex items-center justify-center p-4">
          <div className="bg-gray-900 rounded-xl border border-gray-800 p-6 w-full max-w-lg max-h-[90vh] overflow-auto">
            <div className="flex items-center justify-between mb-6">
              <h3 className="text-xl font-bold text-white">Request Details</h3>
              <button onClick={() => { setSelectedRequest(null); setRejectReason(''); }} className="text-gray-400 hover:text-white">
                <X className="w-6 h-6" />
              </button>
            </div>

            <div className="space-y-4">
              {/* User Info */}
              <div className="bg-gray-800 rounded-lg p-4">
                <div className="flex items-center gap-3 mb-3">
                  <div className="w-12 h-12 bg-gray-700 rounded-full flex items-center justify-center">
                    <User className="w-6 h-6 text-gray-400" />
                  </div>
                  <div>
                    <p className="text-white font-medium">{selectedRequest.display_name}</p>
                    <p className="text-gray-400 text-sm">@{selectedRequest.username}</p>
                  </div>
                </div>
              </div>

              {/* Request Details */}
              <div className="grid grid-cols-2 gap-4">
                <div className="bg-gray-800 rounded-lg p-4">
                  <p className="text-gray-400 text-sm mb-1">Amount</p>
                  <p className="text-2xl font-bold text-emerald-400">₱{selectedRequest.amount?.toFixed(2)}</p>
                </div>
                <div className="bg-gray-800 rounded-lg p-4">
                  <p className="text-gray-400 text-sm mb-1">Method</p>
                  <p className="text-xl font-medium text-white">{selectedRequest.payment_method}</p>
                </div>
              </div>

              {/* Status & Timestamps */}
              <div className="bg-gray-800 rounded-lg p-4 space-y-2">
                <div className="flex justify-between">
                  <span className="text-gray-400">Status</span>
                  {getStatusBadge(selectedRequest.status)}
                </div>
                <div className="flex justify-between">
                  <span className="text-gray-400">Submitted</span>
                  <span className="text-white text-sm">{formatDate(selectedRequest.created_at)}</span>
                </div>
                {selectedRequest.reviewed_at && (
                  <div className="flex justify-between">
                    <span className="text-gray-400">Reviewed</span>
                    <span className="text-white text-sm">{formatDate(selectedRequest.reviewed_at)}</span>
                  </div>
                )}
                {selectedRequest.rejection_reason && (
                  <div className="mt-2 p-2 bg-red-500/10 rounded">
                    <p className="text-red-400 text-sm"><strong>Rejection reason:</strong> {selectedRequest.rejection_reason}</p>
                  </div>
                )}
              </div>

              {/* Proof Image */}
              {selectedRequest.proof_image_url && (
                <div className="bg-gray-800 rounded-lg p-4">
                  <p className="text-gray-400 text-sm mb-2">Payment Proof</p>
                  <div className="flex items-center justify-center bg-gray-900 rounded-lg p-4">
                    <Image className="w-16 h-16 text-gray-600" />
                    <p className="text-gray-500 text-sm ml-2">Proof image submitted</p>
                  </div>
                </div>
              )}

              {/* Security Info */}
              <div className="bg-gray-800 rounded-lg p-4 text-sm">
                <p className="text-gray-400 mb-2">Security Info</p>
                <div className="space-y-1 text-gray-500">
                  <p>IP: {selectedRequest.ip_address || 'N/A'}</p>
                  <p>Device: {selectedRequest.device_fingerprint?.slice(0, 16) || 'N/A'}...</p>
                </div>
              </div>

              {/* Actions for Pending Requests */}
              {selectedRequest.status === 'pending' && (
                <div className="space-y-4 pt-4 border-t border-gray-700">
                  <div>
                    <label className="text-gray-400 text-sm block mb-2">Rejection Reason (if rejecting)</label>
                    <input
                      type="text"
                      value={rejectReason}
                      onChange={(e) => setRejectReason(e.target.value)}
                      placeholder="e.g., Invalid proof, duplicate request"
                      className="w-full px-4 py-2 bg-gray-800 border border-gray-700 rounded-lg text-white"
                    />
                  </div>
                  
                  <div className="flex gap-3">
                    <button
                      onClick={() => handleReject(selectedRequest.request_id)}
                      disabled={processing}
                      className="flex-1 py-3 bg-red-600 hover:bg-red-700 text-white rounded-lg font-medium disabled:opacity-50 flex items-center justify-center gap-2"
                    >
                      <X className="w-5 h-5" />
                      Reject
                    </button>
                    <button
                      onClick={() => handleApprove(selectedRequest.request_id)}
                      disabled={processing}
                      className="flex-1 py-3 bg-emerald-600 hover:bg-emerald-700 text-white rounded-lg font-medium disabled:opacity-50 flex items-center justify-center gap-2"
                    >
                      <Check className="w-5 h-5" />
                      Approve
                    </button>
                  </div>
                </div>
              )}
            </div>
          </div>
        </div>
      )}
    </div>
  );
};

export default AdminWalletLoads;
