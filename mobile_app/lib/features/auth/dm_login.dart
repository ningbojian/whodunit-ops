import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import '../../core/api_client.dart';
import '../../core/auth_store.dart';
import '../../core/device.dart';
import '../../core/session.dart';
import '../../core/theme.dart';

/// DM 登录：只接受店长生成的一次性场控码，不接受邮箱或商家账号。
class DmLoginScreen extends StatefulWidget {
  const DmLoginScreen({
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
  State<DmLoginScreen> createState() => _DmLoginScreenState();
}

class _DmLoginScreenState extends State<DmLoginScreen> {
  final _code = TextEditingController();
  bool _busy = false;
  String? _error;

  @override
  void dispose() {
    _code.dispose();
    super.dispose();
  }

  Future<void> _redeem() async {
    final code = _code.text.trim().toUpperCase();
    if (code.length != 8) {
      setState(() => _error = '场控码是 8 位字母数字组合。');
      return;
    }
    setState(() {
      _busy = true;
      _error = null;
    });
    try {
      final deviceId = await loadDeviceId();
      final result = await widget.api.post(
        '/api/dm/redeem',
        body: {'code': code, 'device_id': deviceId},
      );
      final token = result['token']?.toString() ?? '';
      if (token.isEmpty) {
        throw ApiException('bad_response', '服务端没有返回场控令牌。');
      }
      final session = _asMap(result['session']);
      final run = _asMap(result['run']);
      final label = session['script_name']?.toString() ??
          run['dm_name']?.toString() ??
          'DM 场控';
      await widget.session.signInDm(
        auth: widget.auth,
        api: widget.api,
        token: token,
        label: label,
      );
    } on ApiException catch (exc) {
      if (exc.code == 'code_invalid') {
        setState(() => _error = '场控码无效、已过期或已使用，请让店长重新生成。');
      } else if (exc.code == 'rate_limited') {
        setState(() => _error = '尝试次数过多，请稍后再试。');
      } else {
        setState(() => _error = exc.message);
      }
    } catch (_) {
      setState(() => _error = '网络异常，请稍后重试。');
    } finally {
      if (mounted) {
        setState(() => _busy = false);
      }
    }
  }

  static Map<String, dynamic> _asMap(Object? value) {
    if (value is Map<String, dynamic>) {
      return value;
    }
    if (value is Map) {
      return value.map((key, item) => MapEntry(key.toString(), item));
    }
    return const {};
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
            'DM 场控台',
            style: TextStyle(
              color: AppColors.text,
              fontSize: 28,
              fontWeight: FontWeight.w800,
            ),
          ),
          const SizedBox(height: 8),
          const Text(
            '输入店长给你的 8 位场控码。场控码只可使用一次，并有有效期。',
            style: TextStyle(color: AppColors.textDim, height: 1.5),
          ),
          const SizedBox(height: 28),
          TextField(
            controller: _code,
            enabled: !_busy,
            autofocus: true,
            textCapitalization: TextCapitalization.characters,
            style: const TextStyle(
              fontSize: 26,
              letterSpacing: 8,
              fontWeight: FontWeight.w700,
              color: AppColors.cyan,
            ),
            textAlign: TextAlign.center,
            inputFormatters: [
              FilteringTextInputFormatter.allow(RegExp('[a-zA-Z0-9]')),
              LengthLimitingTextInputFormatter(8),
              _UpperCaseFormatter(),
            ],
            decoration: const InputDecoration(
              hintText: 'ABCD2345',
              counterText: '',
            ),
            onSubmitted: (_) {
              if (!_busy && _code.text.trim().length == 8) {
                _redeem();
              }
            },
          ),
          const SizedBox(height: 20),
          FilledButton(
            onPressed: _busy ? null : _redeem,
            child: _busy
                ? const SizedBox(
                    width: 22,
                    height: 22,
                    child: CircularProgressIndicator(strokeWidth: 2),
                  )
                : const Text('进入场控'),
          ),
          if (_error != null) ...[
            const SizedBox(height: 18),
            Container(
              padding: const EdgeInsets.all(14),
              decoration: BoxDecoration(
                color: AppColors.danger.withValues(alpha: 0.10),
                borderRadius: BorderRadius.circular(12),
                border: Border.all(color: AppColors.danger.withValues(alpha: 0.34)),
              ),
              child: Text(
                _error!,
                style: const TextStyle(
                  color: AppColors.text,
                  height: 1.45,
                  fontSize: 13,
                ),
              ),
            ),
          ],
          const SizedBox(height: 28),
          const Text(
            'DM 端只能查看本场排期、房间和阶段模板；不能创建场次、修改开场时间，'
            '也不能代替店长操作。',
            style: TextStyle(color: AppColors.textFaint, fontSize: 12, height: 1.5),
          ),
        ],
      ),
    );
  }
}

class _UpperCaseFormatter extends TextInputFormatter {
  @override
  TextEditingValue formatEditUpdate(
    TextEditingValue oldValue,
    TextEditingValue newValue,
  ) {
    final upper = newValue.text.toUpperCase();
    return TextEditingValue(
      text: upper,
      selection: TextSelection.collapsed(offset: upper.length),
    );
  }
}
