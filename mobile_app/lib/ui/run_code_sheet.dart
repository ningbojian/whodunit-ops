import 'package:flutter/material.dart';

import '../core/api_client.dart';
import '../core/theme.dart';
import 'widgets.dart';

/// 生成一次性场控码的底部弹层。
///
/// 明文只显示一次；生成后同会话内不再重复请求。
/// 原为 schedule_screen 私有组件，抽公开供排期列表与场次详情共用。
class RunCodeSheet extends StatefulWidget {
  const RunCodeSheet({
    super.key,
    required this.api,
    required this.session,
    required this.dms,
  });

  final ApiClient api;
  final Map<String, dynamic> session;
  final List<Map<String, dynamic>> dms;

  @override
  State<RunCodeSheet> createState() => _RunCodeSheetState();
}

class _RunCodeSheetState extends State<RunCodeSheet> {
  String _dmId = '';
  String _mode = 'software';
  bool _busy = false;
  String? _error;
  Map<String, dynamic>? _result;

  Future<void> _create() async {
    setState(() {
      _busy = true;
      _error = null;
    });
    try {
      final result = await widget.api.post(
        '/api/app/runs',
        body: {
          'session_id': textOf(widget.session['session_id']),
          if (_dmId.isNotEmpty) 'dm_id': _dmId,
          'mode': _mode,
        },
      );
      if (!mounted) {
        return;
      }
      setState(() {
        _busy = false;
        _result = result;
      });
    } on ApiException catch (exc) {
      if (!mounted) {
        return;
      }
      setState(() {
        _busy = false;
        _error = exc.message;
      });
    } catch (_) {
      if (!mounted) {
        return;
      }
      setState(() {
        _busy = false;
        _error = '网络异常，请稍后重试。';
      });
    }
  }

  @override
  Widget build(BuildContext context) {
    final code = textOf(_result?['code']);
    final expires = textOf(_result?['code_expires_at']);
    return ConstrainedBox(
      constraints: BoxConstraints(
        maxHeight: MediaQuery.of(context).size.height * 0.82,
      ),
      child: SingleChildScrollView(
        padding: const EdgeInsets.fromLTRB(20, 18, 20, 28),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          mainAxisSize: MainAxisSize.min,
          children: [
            Row(
              children: [
                const Expanded(
                  child: Text(
                    '生成场控码',
                    style: TextStyle(
                      color: AppColors.text,
                      fontSize: 20,
                      fontWeight: FontWeight.w800,
                    ),
                  ),
                ),
                IconButton(
                  onPressed: () => Navigator.of(context).pop(),
                  icon: const Icon(Icons.close),
                ),
              ],
            ),
            const SizedBox(height: 4),
            Text(
              '${textOf(widget.session['script_name'])} · '
              '${textOf(widget.session['start_time'])}',
              style: const TextStyle(color: AppColors.textDim, fontSize: 13),
            ),
            const SizedBox(height: 18),
            if (code.isEmpty) ...[
              DropdownButtonFormField<String>(
                initialValue: _dmId,
                decoration: const InputDecoration(labelText: 'DM'),
                items: [
                  const DropdownMenuItem(value: '', child: Text('临时 DM（不指定）')),
                  for (final dm in widget.dms)
                    DropdownMenuItem(
                      value: textOf(dm['dm_id']),
                      child: Text(textOf(dm['name'], fallback: '未命名 DM')),
                    ),
                ],
                onChanged: _busy
                    ? null
                    : (value) => setState(() => _dmId = value ?? ''),
              ),
              const SizedBox(height: 14),
              const Text(
                '模式',
                style: TextStyle(color: AppColors.textDim, fontSize: 12),
              ),
              const SizedBox(height: 8),
              SegmentedButton<String>(
                segments: const [
                  ButtonSegment(value: 'software', label: Text('纯软件')),
                  ButtonSegment(value: 'hardware', label: Text('硬件模式')),
                ],
                selected: {_mode},
                onSelectionChanged: _busy
                    ? null
                    : (value) => setState(() => _mode = value.first),
              ),
              const SizedBox(height: 8),
              const Text(
                '硬件模式需要服务端已配置硬件 Skill；未配置时只能使用软件模式。',
                style: TextStyle(
                  color: AppColors.textFaint,
                  fontSize: 11,
                  height: 1.5,
                ),
              ),
              if (_error != null) ...[
                const SizedBox(height: 14),
                InlineError(message: _error!),
              ],
              const SizedBox(height: 18),
              FilledButton(
                onPressed: _busy ? null : _create,
                child: _busy
                    ? const SizedBox(
                        width: 22,
                        height: 22,
                        child: CircularProgressIndicator(strokeWidth: 2),
                      )
                    : const Text('生成一次性场控码'),
              ),
            ] else ...[
              Panel(
                borderColor: AppColors.cyan.withValues(alpha: 0.36),
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    const Text(
                      '把这串码发给 DM',
                      style: TextStyle(
                        color: AppColors.textDim,
                        fontSize: 12,
                      ),
                    ),
                    const SizedBox(height: 10),
                    SelectableText(
                      code,
                      style: const TextStyle(
                        color: AppColors.cyan,
                        fontSize: 32,
                        letterSpacing: 8,
                        fontWeight: FontWeight.w800,
                      ),
                    ),
                    const SizedBox(height: 10),
                    Text(
                      '有效期至 $expires。只显示这一次，关闭后无法再次查看；'
                      '需要时请生成新码（旧令牌会失效）。',
                      style: const TextStyle(
                        color: AppColors.textDim,
                        fontSize: 12,
                        height: 1.5,
                      ),
                    ),
                  ],
                ),
              ),
              const SizedBox(height: 16),
              OutlinedButton(
                onPressed: () => Navigator.of(context).pop(),
                child: const Text('完成'),
              ),
            ],
          ],
        ),
      ),
    );
  }
}
