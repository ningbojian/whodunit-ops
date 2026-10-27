import 'package:flutter/material.dart';

import '../../core/api_client.dart';
import '../../core/theme.dart';
import '../../ui/widgets.dart';

/// 把勾选的 proposals 映射为 POST /api/app/schedule/apply 的请求体。
///
/// - `room_id`/`dm_id` 取预览里的 `after`（自动分配的目标房间与 DM）；
/// - `expect_room_id`/`expect_dm_id` 取 `before`，空串表示"当时确实没有分配"，
///   服务端据此做 preview_stale 校验，预览过期时一条都不写；
/// - `confirm: true` 表示店长已确认，服务端全有或全无一次性写入。
Map<String, dynamic> buildApplyPayload(List<Map<String, dynamic>> selected) {
  return {
    'confirm': true,
    'assignments': [
      for (final proposal in selected)
        {
          'session_id': textOf(proposal['session_id']),
          'room_id': textOf(asMap(proposal['after'])['room_id']),
          'dm_id': textOf(asMap(proposal['after'])['dm_id']),
          'expect_room_id': textOf(asMap(proposal['before'])['room_id']),
          'expect_dm_id': textOf(asMap(proposal['before'])['dm_id']),
        },
    ],
  };
}

/// 单条 proposal 的变更摘要：「房间：未分配 → A 房 / DM：未分配 → 小北」。
///
/// 名称缺失时回退到 id；before 完全没有分配时显示「未分配」。
String assignmentChangeText(Map<String, dynamic> proposal) {
  String labelOf(Map map, String idKey, String nameKey) {
    final text = textOf(map[nameKey], fallback: textOf(map[idKey]));
    return text.isEmpty ? '未分配' : text;
  }

  final before = asMap(proposal['before']);
  final after = asMap(proposal['after']);
  final room =
      '${labelOf(before, 'room_id', 'room_name')} → ${labelOf(after, 'room_id', 'room_name')}';
  final dm =
      '${labelOf(before, 'dm_id', 'dm_name')} → ${labelOf(after, 'dm_id', 'dm_name')}';
  return '房间：$room / DM：$dm';
}

/// 服务端 400 的建议问题放在 error.message 里；兼容 ApiClient 直接
/// toString 的旧取法，优先取出嵌套对象中的 message。
String serverErrorText(ApiException exc) {
  final error = exc.body?['error'];
  if (error is Map) {
    return textOf(error['message'], fallback: exc.message);
  }
  return exc.message;
}

/// 「补齐未分配」预览底部卡片：
/// 打开即 POST /api/app/schedule/auto-assign {preview:true}（只算不写），
/// proposals 逐条勾选后 POST /api/app/schedule/apply（带 expect 值）写入。
/// partial / conflicts 只读提示，移动端不在此直接处理。
class ScheduleAssignSheet extends StatefulWidget {
  const ScheduleAssignSheet({super.key, required this.api});

  final ApiClient api;

  @override
  State<ScheduleAssignSheet> createState() => _ScheduleAssignSheetState();
}

class _ScheduleAssignSheetState extends State<ScheduleAssignSheet> {
  List<Map<String, dynamic>> _proposals = const [];
  List<Map<String, dynamic>> _partial = const [];
  List<Map<String, dynamic>> _conflicts = const [];
  List<String> _warnings = const [];
  int _unchanged = 0;
  final Set<String> _selected = {};
  bool _loading = true;
  bool _applying = false;
  String? _error;

  @override
  void initState() {
    super.initState();
    _loadPreview();
  }

  String _idOf(Map<String, dynamic> proposal) =>
      textOf(proposal['session_id']);

  Future<void> _loadPreview() async {
    setState(() {
      _loading = true;
      _error = null;
    });
    try {
      final result = await widget.api.post(
        '/api/app/schedule/auto-assign',
        body: const {'preview': true},
      );
      if (!mounted) {
        return;
      }
      final rawProposals = asMapList(result['proposals']);
      final proposals = rawProposals
          .where((item) => item['changed'] != false)
          .toList();
      final unchangedRaw = result['unchanged'];
      final unchanged = (unchangedRaw is List ? unchangedRaw.length : 0) +
          rawProposals.where((item) => item['changed'] == false).length;
      setState(() {
        _proposals = proposals;
        _partial = asMapList(result['partial']);
        _conflicts = asMapList(result['conflicts']);
        _warnings = asStringList(result['warnings']);
        _unchanged = unchanged;
        _selected
          ..clear()
          ..addAll(
            proposals
                .map(_idOf)
                .where((id) => id.isNotEmpty),
          );
        _loading = false;
      });
    } on ApiException catch (exc) {
      if (!mounted) {
        return;
      }
      setState(() {
        _loading = false;
        _error = serverErrorText(exc);
      });
    } catch (_) {
      if (!mounted) {
        return;
      }
      setState(() {
        _loading = false;
        _error = '网络异常，请稍后重试。';
      });
    }
  }

  Future<void> _apply() async {
    if (_applying) {
      return;
    }
    final selected = _proposals
        .where((item) => _selected.contains(_idOf(item)))
        .toList();
    if (selected.isEmpty) {
      return;
    }
    setState(() {
      _applying = true;
      _error = null;
    });
    try {
      await widget.api.post(
        '/api/app/schedule/apply',
        body: buildApplyPayload(selected),
      );
      if (!mounted) {
        return;
      }
      Navigator.of(context).pop(true);
    } on ApiException catch (exc) {
      if (!mounted) {
        return;
      }
      setState(() {
        _applying = false;
        _error = serverErrorText(exc);
      });
    } catch (_) {
      if (!mounted) {
        return;
      }
      setState(() {
        _applying = false;
        _error = '网络异常，请稍后重试。';
      });
    }
  }

  @override
  Widget build(BuildContext context) {
    return ConstrainedBox(
      constraints: BoxConstraints(
        maxHeight: MediaQuery.of(context).size.height * 0.88,
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
                    '补齐未分配',
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
                  tooltip: '关闭',
                ),
              ],
            ),
            const SizedBox(height: 6),
            const Text(
              '先预览再写入：下面只是建议，勾选后点「应用所选」才会保存；'
              '只补房间和 DM，不改开场时间。',
              style: TextStyle(
                color: AppColors.textFaint,
                fontSize: 12,
                height: 1.5,
              ),
            ),
            const SizedBox(height: 14),
            if (_loading)
              const Padding(
                padding: EdgeInsets.all(20),
                child: Center(child: CircularProgressIndicator()),
              )
            else ...[
              if (_proposals.isEmpty && _error == null)
                const EmptyHint(
                  title: '没有需要补齐的场次',
                  message: '当前场次都已分配房间和 DM。',
                  icon: Icons.event_available_outlined,
                ),
              for (final proposal in _proposals) _buildProposalTile(proposal),
              if (_unchanged > 0) ...[
                const SizedBox(height: 8),
                Text(
                  '另有 $_unchanged 场保持不变，本次无需处理。',
                  style: const TextStyle(
                    color: AppColors.textFaint,
                    fontSize: 12,
                    height: 1.5,
                  ),
                ),
              ],
              if (_partial.isNotEmpty)
                _NoticeBox(
                  color: AppColors.amber,
                  text: '${_partial.length} 场只缺房间或 DM，'
                      '请到 Web『生成优化方案』处理。',
                ),
              for (final conflict in _conflicts)
                _NoticeBox(
                  color: AppColors.amber,
                  text: textOf(
                    conflict['message'],
                    fallback: '有资源冲突，本次不会自动处理。',
                  ),
                ),
              if (_warnings.isNotEmpty) ...[
                const SizedBox(height: 10),
                for (final warning in _warnings)
                  Padding(
                    padding: const EdgeInsets.only(bottom: 3),
                    child: Text(
                      warning,
                      style: const TextStyle(
                        color: AppColors.textFaint,
                        fontSize: 11,
                        height: 1.5,
                      ),
                    ),
                  ),
              ],
              if (_error != null) ...[
                const SizedBox(height: 14),
                InlineError(
                  message: _error!,
                  onRetry: _proposals.isEmpty ? _loadPreview : null,
                ),
              ],
              if (_proposals.isNotEmpty) ...[
                const SizedBox(height: 16),
                Row(
                  children: [
                    OutlinedButton(
                      onPressed: _applying
                          ? null
                          : () => setState(() {
                                _selected
                                  ..clear()
                                  ..addAll(
                                    _proposals
                                        .map(_idOf)
                                        .where((id) => id.isNotEmpty),
                                  );
                              }),
                      style: _compactButtonStyle,
                      child: const Text('全选'),
                    ),
                    const SizedBox(width: 8),
                    OutlinedButton(
                      onPressed: _applying
                          ? null
                          : () => setState(_selected.clear),
                      style: _compactButtonStyle,
                      child: const Text('全不选'),
                    ),
                  ],
                ),
                const SizedBox(height: 10),
                FilledButton(
                  onPressed: (_applying || _selected.isEmpty) ? null : _apply,
                  child: _applying
                      ? const SizedBox(
                          width: 22,
                          height: 22,
                          child: CircularProgressIndicator(strokeWidth: 2),
                        )
                      : Text('应用所选 (${_selected.length})'),
                ),
              ],
            ],
          ],
        ),
      ),
    );
  }

  static const _compactButtonStyle = ButtonStyle(
    minimumSize: WidgetStatePropertyAll(Size(0, 40)),
    padding: WidgetStatePropertyAll(EdgeInsets.symmetric(horizontal: 16)),
    textStyle: WidgetStatePropertyAll(
      TextStyle(fontSize: 13, fontWeight: FontWeight.w600),
    ),
  );

  Widget _buildProposalTile(Map<String, dynamic> proposal) {
    final id = _idOf(proposal);
    final start = textOf(proposal['start']);
    final script = textOf(proposal['script_name'], fallback: '未命名剧本');
    return CheckboxListTile(
      value: _selected.contains(id),
      onChanged: _applying
          ? null
          : (checked) => setState(() {
                if (checked == true) {
                  _selected.add(id);
                } else {
                  _selected.remove(id);
                }
              }),
      activeColor: AppColors.cyan,
      dense: true,
      contentPadding: EdgeInsets.zero,
      title: Text(
        '${dateOf(start)} ${clockOf(start)} 《$script》',
        style: const TextStyle(
          color: AppColors.text,
          fontSize: 13,
          fontWeight: FontWeight.w600,
          height: 1.4,
        ),
      ),
      subtitle: Text(
        assignmentChangeText(proposal),
        style: const TextStyle(
          color: AppColors.textDim,
          fontSize: 12,
          height: 1.4,
        ),
      ),
    );
  }
}

class _NoticeBox extends StatelessWidget {
  const _NoticeBox({required this.color, required this.text});

  final Color color;
  final String text;

  @override
  Widget build(BuildContext context) {
    return Container(
      margin: const EdgeInsets.only(top: 10),
      padding: const EdgeInsets.all(12),
      decoration: BoxDecoration(
        color: color.withValues(alpha: 0.08),
        borderRadius: BorderRadius.circular(10),
        border: Border.all(color: color.withValues(alpha: 0.24)),
      ),
      child: Text(
        text,
        style: const TextStyle(
          color: AppColors.text,
          fontSize: 12.5,
          height: 1.5,
        ),
      ),
    );
  }
}
