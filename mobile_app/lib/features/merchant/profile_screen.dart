import 'package:flutter/material.dart';

import '../../core/api_client.dart';
import '../../core/auth_store.dart';
import '../../core/session.dart';
import '../../core/theme.dart';
import '../../ui/widgets.dart';

class ProfileScreen extends StatefulWidget {
  const ProfileScreen({
    super.key,
    required this.api,
    required this.auth,
    required this.session,
  });

  final ApiClient api;
  final AuthStore auth;
  final AppSession session;

  @override
  State<ProfileScreen> createState() => _ProfileScreenState();
}

class _ProfileScreenState extends State<ProfileScreen> {
  Map<String, dynamic>? _me;
  bool _loading = true;
  String? _error;

  @override
  void initState() {
    super.initState();
    _load();
  }

  Future<void> _load() async {
    setState(() {
      _loading = true;
      _error = null;
    });
    try {
      final me = await widget.api.get('/api/app/me');
      if (!mounted) {
        return;
      }
      setState(() {
        _me = me;
        _loading = false;
      });
    } on ApiException catch (exc) {
      if (!mounted) {
        return;
      }
      setState(() {
        _loading = false;
        _error = exc.message;
      });
    } catch (_) {
      if (!mounted) {
        return;
      }
      setState(() {
        _loading = false;
        _error = '网络异常，无法加载账号信息。';
      });
    }
  }

  Future<void> _signOut() async {
    final confirmed = await showDialog<bool>(
      context: context,
      builder: (context) => AlertDialog(
        backgroundColor: AppColors.bg2,
        title: const Text('退出登录'),
        content: const Text(
          '退出后需要重新申请访问，并等开发者在邮件里批准。',
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.of(context).pop(false),
            child: const Text('取消'),
          ),
          FilledButton(
            onPressed: () => Navigator.of(context).pop(true),
            child: const Text('退出'),
          ),
        ],
      ),
    );
    if (confirmed != true) {
      return;
    }
    await widget.session.signOutMerchant(auth: widget.auth, api: widget.api);
  }

  String _formatLocal(Object? value) {
    final text = textOf(value);
    if (text.isEmpty) {
      return '未知';
    }
    final parsed = DateTime.tryParse(text);
    if (parsed == null) {
      return text;
    }
    final local = parsed.toLocal();
    String two(int number) => number.toString().padLeft(2, '0');
    return '${local.year}-${two(local.month)}-${two(local.day)} '
        '${two(local.hour)}:${two(local.minute)}';
  }

  @override
  Widget build(BuildContext context) {
    final me = _me ?? const <String, dynamic>{};
    final name = textOf(
      me['display_name'],
      fallback: widget.session.merchantName ?? '店长',
    );
    final email = textOf(me['email_masked'], fallback: '—');
    final remaining = intOf(me['remaining_seconds']);

    return RefreshIndicator(
      onRefresh: _load,
      child: ListView(
        physics: const AlwaysScrollableScrollPhysics(),
        padding: const EdgeInsets.fromLTRB(16, 12, 16, 28),
        children: [
          Panel(
            child: Column(
              children: [
                Row(
                  children: [
                    Container(
                      width: 56,
                      height: 56,
                      decoration: BoxDecoration(
                        color: AppColors.cyan.withValues(alpha: 0.12),
                        borderRadius: BorderRadius.circular(18),
                        border: Border.all(
                          color: AppColors.cyan.withValues(alpha: 0.3),
                        ),
                      ),
                      child: const Icon(
                        Icons.storefront_outlined,
                        color: AppColors.cyan,
                        size: 28,
                      ),
                    ),
                    const SizedBox(width: 14),
                    Expanded(
                      child: Column(
                        crossAxisAlignment: CrossAxisAlignment.start,
                        children: [
                          Text(
                            name,
                            style: const TextStyle(
                              color: AppColors.text,
                              fontSize: 19,
                              fontWeight: FontWeight.w700,
                            ),
                          ),
                          const SizedBox(height: 4),
                          Text(
                            email,
                            style: const TextStyle(
                              color: AppColors.textDim,
                              fontSize: 13,
                            ),
                          ),
                        ],
                      ),
                    ),
                    const Pill(label: '商家', color: AppColors.cyan),
                  ],
                ),
                const SizedBox(height: 18),
                if (_loading)
                  const Padding(
                    padding: EdgeInsets.all(12),
                    child: CircularProgressIndicator(),
                  )
                else if (_error != null)
                  InlineError(message: _error!, onRetry: _load)
                else ...[
                  _InfoRow(
                    label: '访问到期',
                    value: _formatLocal(me['access_expires_at']),
                  ),
                  const SizedBox(height: 10),
                  _InfoRow(
                    label: '剩余时间',
                    value: remaining == null
                        ? '未知'
                        : durationOf(remaining),
                  ),
                  const SizedBox(height: 12),
                  const Text(
                    '审批访问为 8 小时一批；到期后需要重新申请。',
                    style: TextStyle(
                      color: AppColors.textFaint,
                      fontSize: 11,
                      height: 1.5,
                    ),
                  ),
                ],
              ],
            ),
          ),
          const SizedBox(height: 22),
          const SectionTitle(title: '产品边界'),
          const Panel(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                _Boundary(
                  icon: Icons.chat_bubble_outline,
                  text: '不提供玩家直连对话；玩家侧审核机制只属于未来规划。',
                ),
                _Boundary(
                  icon: Icons.send_outlined,
                  text: '不自动抓取或发送微信消息，避免账号风控。',
                ),
                _Boundary(
                  icon: Icons.payments_outlined,
                  text: '不处理支付和订单履约；预约/收银仍由现有行业 SaaS 承担。',
                ),
                _Boundary(
                  icon: Icons.cloud_off_outlined,
                  text: '大众点评没有实时接口；只有导入导出文件后才更新座位。',
                ),
                _Boundary(
                  icon: Icons.desktop_windows_outlined,
                  text: '资源建档、阶段模板编辑和详细渠道单预览仍用桌面 Web 后台。',
                ),
              ],
            ),
          ),
          const SizedBox(height: 22),
          const SectionTitle(title: '账号与安全'),
          Panel(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                const Text(
                  '登录令牌只保存在系统安全存储中，不写入日志或链接。'
                  '换手机或撤销访问后，需要重新申请。',
                  style: TextStyle(
                    color: AppColors.textDim,
                    fontSize: 12,
                    height: 1.55,
                  ),
                ),
                const SizedBox(height: 14),
                OutlinedButton.icon(
                  onPressed: _signOut,
                  icon: const Icon(Icons.logout, size: 18),
                  label: const Text('退出登录'),
                  style: OutlinedButton.styleFrom(
                    foregroundColor: AppColors.danger,
                    side: BorderSide(
                      color: AppColors.danger.withValues(alpha: 0.4),
                    ),
                  ),
                ),
              ],
            ),
          ),
          const SizedBox(height: 22),
          const Center(
            child: Text(
              '迷雾运营台 · 客户端 0.1.0 (1)\n'
              '代码已实现，尚未上架应用商店',
              textAlign: TextAlign.center,
              style: TextStyle(
                color: AppColors.textFaint,
                fontSize: 11,
                height: 1.6,
              ),
            ),
          ),
        ],
      ),
    );
  }
}

class _InfoRow extends StatelessWidget {
  const _InfoRow({required this.label, required this.value});

  final String label;
  final String value;

  @override
  Widget build(BuildContext context) {
    return Row(
      children: [
        Expanded(
          child: Text(
            label,
            style: const TextStyle(color: AppColors.textDim, fontSize: 13),
          ),
        ),
        Text(
          value,
          style: const TextStyle(
            color: AppColors.text,
            fontSize: 13,
            fontWeight: FontWeight.w600,
          ),
        ),
      ],
    );
  }
}

class _Boundary extends StatelessWidget {
  const _Boundary({required this.icon, required this.text});

  final IconData icon;
  final String text;

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.only(bottom: 12),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Icon(icon, color: AppColors.textFaint, size: 16),
          const SizedBox(width: 10),
          Expanded(
            child: Text(
              text,
              style: const TextStyle(
                color: AppColors.textDim,
                fontSize: 12,
                height: 1.5,
              ),
            ),
          ),
        ],
      ),
    );
  }
}
