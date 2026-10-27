import 'dart:async';

import 'package:flutter/material.dart';

import '../../core/api_client.dart';
import '../../core/auth_store.dart';
import '../../core/session.dart';
import '../../core/theme.dart';

/// 商家登录：邮箱申请 → 开发者在邮件里审批 → App 自动领取 Bearer token。
///
/// 审批仍发生在邮件里；App 不打开审批链接，也不保存密码。
class MerchantLoginScreen extends StatefulWidget {
  const MerchantLoginScreen({
    super.key,
    required this.session,
    required this.api,
    required this.auth,
    required this.onBack,
  });

  final AppSession session;
  final ApiClient api;
  final AuthStore auth;
  final VoidCallback onBack;

  @override
  State<MerchantLoginScreen> createState() => _MerchantLoginScreenState();
}

class _MerchantLoginScreenState extends State<MerchantLoginScreen> {
  final _email = TextEditingController();
  final _name = TextEditingController();
  Timer? _pollTimer;
  bool _busy = false;
  bool _waiting = false;
  bool _approvalSent = true;
  String? _requestId;
  String? _browserToken;
  String? _message;
  bool _messageIsError = true;

  @override
  void dispose() {
    _pollTimer?.cancel();
    _email.dispose();
    _name.dispose();
    super.dispose();
  }

  void _setMessage(String? text, {bool error = true}) {
    if (!mounted) {
      return;
    }
    setState(() {
      _message = text;
      _messageIsError = error;
    });
  }

  Future<void> _request() async {
    final email = _email.text.trim();
    if (email.isEmpty) {
      _setMessage('请先填写邮箱。');
      return;
    }
    setState(() {
      _busy = true;
      _message = null;
    });
    try {
      final result = await widget.api.post(
        '/api/app/auth/request',
        body: {
          'email': email,
          if (_name.text.trim().isNotEmpty) 'display_name': _name.text.trim(),
        },
      );
      final requestId = result['request_id']?.toString() ?? '';
      final browserToken = result['browser_token']?.toString() ?? '';
      if (requestId.isEmpty || browserToken.isEmpty) {
        throw ApiException('bad_response', '服务端没有返回申请凭证。');
      }
      await widget.auth.saveMerchantRequest(
        requestId: requestId,
        browserToken: browserToken,
      );
      if (!mounted) {
        return;
      }
      setState(() {
        _requestId = requestId;
        _browserToken = browserToken;
        _waiting = true;
        _approvalSent = result['approval_email_sent'] != false;
        _message = _approvalSent
            ? '申请已提交。审批邮件发到 $email，请在邮件里批准。'
            : '申请已提交，但审批邮件可能没有发出，请联系开发者。';
        _messageIsError = !_approvalSent;
      });
      _startPolling();
    } on ApiException catch (exc) {
      if (exc.code == 'active_access') {
        _setMessage('该邮箱已有有效访问。请在已登录的设备继续使用，或等访问到期后重新申请。');
      } else if (exc.code == 'request_cooldown' || exc.code == 'request_limited') {
        _setMessage('提交过于频繁，请稍后再试。');
      } else if (exc.code == 'invalid_email') {
        _setMessage('邮箱格式不正确。');
      } else {
        _setMessage(exc.message);
      }
    } catch (_) {
      _setMessage('网络异常，请稍后重试。');
    } finally {
      if (mounted) {
        setState(() => _busy = false);
      }
    }
  }

  void _startPolling() {
    _pollTimer?.cancel();
    _pollTimer = Timer.periodic(const Duration(seconds: 5), (_) => _poll());
    _poll();
  }

  Future<void> _poll() async {
    final requestId = _requestId;
    final browserToken = _browserToken;
    if (requestId == null || browserToken == null) {
      return;
    }
    try {
      final result = await widget.api.get(
        '/api/app/auth/status',
        query: {'request_id': requestId},
        headers: {'X-Browser-Token': browserToken},
      );
      final status = result['status']?.toString() ?? '';
      if (result['claimable'] == true) {
        _pollTimer?.cancel();
        await _claim(requestId, browserToken);
        return;
      }
      if (status == 'expired' || status == 'denied' || status == 'failed') {
        _pollTimer?.cancel();
        _setMessage('申请已失效，请重新提交。');
      }
    } on ApiException catch (exc) {
      if (exc.code == 'request_not_found' || exc.code == 'unauthorized') {
        _pollTimer?.cancel();
        _setMessage('申请记录已失效，请重新提交。');
      }
      // 其他错误（例如网络抖动）保留轮询，避免一次失败就中断。
    } catch (_) {
      // 网络错误继续轮询。
    }
  }

  Future<void> _claim(String requestId, String browserToken) async {
    try {
      final result = await widget.api.post(
        '/api/app/auth/claim',
        body: {'request_id': requestId, 'browser_token': browserToken},
      );
      final token = result['token']?.toString() ?? '';
      if (token.isEmpty) {
        throw ApiException('bad_response', '服务端没有返回登录令牌。');
      }
      await widget.session.signInMerchant(
        auth: widget.auth,
        api: widget.api,
        token: token,
        displayName: result['display_name']?.toString(),
      );
    } on ApiException catch (exc) {
      if (exc.code == 'not_ready') {
        // 状态接口刚变为可领取，服务端事务尚未完全落盘；继续轮询。
        _startPolling();
        return;
      }
      _pollTimer?.cancel();
      _setMessage(exc.message);
    } catch (_) {
      _startPolling();
    }
  }

  @override
  Widget build(BuildContext context) {
    return SingleChildScrollView(
      padding: const EdgeInsets.fromLTRB(24, 16, 24, 32),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          TextButton.icon(
            onPressed: widget.onBack,
            icon: const Icon(Icons.arrow_back, size: 18),
            label: const Text('返回'),
            style: TextButton.styleFrom(
              foregroundColor: AppColors.textDim,
              alignment: Alignment.centerLeft,
              padding: EdgeInsets.zero,
            ),
          ),
          const SizedBox(height: 16),
          const Text(
            '商家登录',
            style: TextStyle(
              color: AppColors.text,
              fontSize: 28,
              fontWeight: FontWeight.w800,
            ),
          ),
          const SizedBox(height: 8),
          const Text(
            '输入邮箱申请访问。开发者在邮件里批准后，App 会自动进入工作台。',
            style: TextStyle(color: AppColors.textDim, height: 1.5),
          ),
          const SizedBox(height: 28),
          TextField(
            controller: _email,
            enabled: !_waiting && !_busy,
            keyboardType: TextInputType.emailAddress,
            autofillHints: const [AutofillHints.email],
            decoration: const InputDecoration(
              labelText: '邮箱',
              hintText: 'you@example.com',
            ),
          ),
          const SizedBox(height: 14),
          TextField(
            controller: _name,
            enabled: !_waiting && !_busy,
            decoration: const InputDecoration(
              labelText: '称呼（可选）',
              hintText: '例如：迷雾剧场店长',
            ),
          ),
          const SizedBox(height: 20),
          FilledButton(
            onPressed: _busy || _waiting ? null : _request,
            child: _busy
                ? const SizedBox(
                    width: 22,
                    height: 22,
                    child: CircularProgressIndicator(strokeWidth: 2),
                  )
                : const Text('提交申请'),
          ),
          if (_message != null) ...[
            const SizedBox(height: 20),
            _MessageCard(
              text: _message!,
              error: _messageIsError,
            ),
          ],
          if (_waiting) ...[
            const SizedBox(height: 20),
            _WaitingCard(
              email: _email.text.trim(),
              approvalSent: _approvalSent,
            ),
          ],
          const SizedBox(height: 28),
          const Text(
            '登录有效期与访问审批窗口一致；到期后需要重新申请。',
            style: TextStyle(color: AppColors.textFaint, fontSize: 12, height: 1.5),
          ),
        ],
      ),
    );
  }
}

class _MessageCard extends StatelessWidget {
  const _MessageCard({required this.text, required this.error});

  final String text;
  final bool error;

  @override
  Widget build(BuildContext context) {
    final color = error ? AppColors.danger : AppColors.mint;
    return Container(
      padding: const EdgeInsets.all(14),
      decoration: BoxDecoration(
        color: color.withValues(alpha: 0.10),
        borderRadius: BorderRadius.circular(12),
        border: Border.all(color: color.withValues(alpha: 0.34)),
      ),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Icon(
            error ? Icons.error_outline : Icons.mark_email_read_outlined,
            color: color,
            size: 18,
          ),
          const SizedBox(width: 10),
          Expanded(
            child: Text(
              text,
              style: const TextStyle(color: AppColors.text, height: 1.45, fontSize: 13),
            ),
          ),
        ],
      ),
    );
  }
}

class _WaitingCard extends StatelessWidget {
  const _WaitingCard({required this.email, required this.approvalSent});

  final String email;
  final bool approvalSent;

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.all(16),
      decoration: BoxDecoration(
        color: AppColors.bg2.withValues(alpha: 0.7),
        borderRadius: BorderRadius.circular(14),
        border: Border.all(color: AppColors.line),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          const Row(
            children: [
              SizedBox(
                width: 16,
                height: 16,
                child: CircularProgressIndicator(strokeWidth: 2),
              ),
              SizedBox(width: 10),
              Text(
                '等待审批',
                style: TextStyle(
                  color: AppColors.text,
                  fontWeight: FontWeight.w700,
                ),
              ),
            ],
          ),
          const SizedBox(height: 10),
          Text(
            approvalSent
                ? '已向 $email 发送审批邮件。批准后这里会自动登录，无需刷新。'
                : '未能确认审批邮件已发出。请联系开发者检查邮件配置。',
            style: const TextStyle(color: AppColors.textDim, height: 1.5, fontSize: 13),
          ),
        ],
      ),
    );
  }
}
