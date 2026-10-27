import 'package:flutter/material.dart';

import '../../core/api_client.dart';
import '../../core/theme.dart';
import '../../ui/run_code_sheet.dart';
import '../../ui/widgets.dart';
import 'schedule_actions.dart';

/// 场次详情页：全字段只读 + 场控码 + 换房/换 DM。
///
/// 紧凑列表与时间轴点块共用入口；返回 true 表示发生过变更（排期页需刷新）。
class SessionDetailScreen extends StatefulWidget {
  const SessionDetailScreen({
    super.key,
    required this.api,
    required this.session,
    this.rooms = const [],
    this.dms = const [],
  });

  final ApiClient api;
  final Map<String, dynamic> session;
  final List<Map<String, dynamic>> rooms;
  final List<Map<String, dynamic>> dms;

  @override
  State<SessionDetailScreen> createState() => _SessionDetailScreenState();
}

class _SessionDetailScreenState extends State<SessionDetailScreen> {
  bool _busy = false;

  Future<void> _openRun() async {
    await showModalBottomSheet<void>(
      context: context,
      isScrollControlled: true,
      backgroundColor: AppColors.bg1,
      shape: const RoundedRectangleBorder(
        borderRadius: BorderRadius.vertical(top: Radius.circular(20)),
      ),
      builder: (context) => Padding(
        padding: EdgeInsets.only(
          bottom: MediaQuery.of(context).viewInsets.bottom,
        ),
        child: RunCodeSheet(
          api: widget.api,
          session: widget.session,
          dms: widget.dms,
        ),
      ),
    );
  }

  bool _activeOf(Map<String, dynamic> item) {
    final raw = item['active'];
    if (raw is bool) {
      return raw;
    }
    return textOf(raw) != 'false';
  }

  Future<void> _openReassign() async {
    final currentRoom = textOf(widget.session['room_id']);
    final currentDm = textOf(widget.session['dm_id']);
    // 弹层只负责选目标并 pop 返回 (choice, targetId)；
    // 真正的 resolve 在弹层关闭后执行——否则 _resolve 里的
    // Navigator.pop(true) 会先关掉弹层而不是详情页，变更结果永远传不回调用方。
    final picked = await showModalBottomSheet<({String choice, String targetId})>(
      context: context,
      isScrollControlled: true,
      backgroundColor: AppColors.bg1,
      shape: const RoundedRectangleBorder(
        borderRadius: BorderRadius.vertical(top: Radius.circular(20)),
      ),
      builder: (context) => ConstrainedBox(
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
                      '换房 / 换 DM',
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
              const Text(
                '只改房间或 DM 的指派，不改开场时间；系统会按资源冲突即时校验。',
                style: TextStyle(color: AppColors.textFaint, fontSize: 12),
              ),
              const SizedBox(height: 14),
              const Text(
                '房间',
                style: TextStyle(color: AppColors.textDim, fontSize: 12),
              ),
              const SizedBox(height: 6),
              for (final room in widget.rooms)
                ListTile(
                  dense: true,
                  contentPadding: EdgeInsets.zero,
                  enabled: !_busy && _activeOf(room),
                  leading: Icon(
                    textOf(room['room_id']) == currentRoom
                        ? Icons.check_circle_outline
                        : Icons.meeting_room_outlined,
                    size: 18,
                    color: textOf(room['room_id']) == currentRoom
                        ? AppColors.mint
                        : AppColors.textFaint,
                  ),
                  title: Text(
                    textOf(room['name'], fallback: '未命名房间'),
                    style: const TextStyle(color: AppColors.text, fontSize: 14),
                  ),
                  subtitle: textOf(room['capacity']).isEmpty
                      ? null
                      : Text(
                          '容量 ${textOf(room['capacity'])}',
                          style: const TextStyle(
                            color: AppColors.textFaint,
                            fontSize: 11,
                          ),
                        ),
                  onTap: () => Navigator.of(context).pop(
                    (choice: 'switch_room', targetId: textOf(room['room_id'])),
                  ),
                ),
              if (widget.rooms.isEmpty)
                const Padding(
                  padding: EdgeInsets.symmetric(vertical: 8),
                  child: Text(
                    '还没有可选房间；请先在 Web「资源」页建档。',
                    style: TextStyle(color: AppColors.textFaint, fontSize: 12),
                  ),
                ),
              const SizedBox(height: 10),
              const Text(
                'DM',
                style: TextStyle(color: AppColors.textDim, fontSize: 12),
              ),
              const SizedBox(height: 6),
              for (final dm in widget.dms)
                ListTile(
                  dense: true,
                  contentPadding: EdgeInsets.zero,
                  enabled: !_busy && _activeOf(dm),
                  leading: Icon(
                    textOf(dm['dm_id']) == currentDm
                        ? Icons.check_circle_outline
                        : Icons.person_outline,
                    size: 18,
                    color: textOf(dm['dm_id']) == currentDm
                        ? AppColors.mint
                        : AppColors.textFaint,
                  ),
                  title: Text(
                    textOf(dm['name'], fallback: '未命名 DM'),
                    style: const TextStyle(color: AppColors.text, fontSize: 14),
                  ),
                  subtitle: _dmSubtitle(dm),
                  onTap: () => Navigator.of(context).pop(
                    (choice: 'switch_dm', targetId: textOf(dm['dm_id'])),
                  ),
                ),
              if (widget.dms.isEmpty)
                const Padding(
                  padding: EdgeInsets.symmetric(vertical: 8),
                  child: Text(
                    '还没有可选 DM；请先在 Web「资源」页建档。',
                    style: TextStyle(color: AppColors.textFaint, fontSize: 12),
                  ),
                ),
            ],
          ),
        ),
      ),
    );
    if (picked == null || !mounted) {
      return;
    }
    await _resolve(picked.choice, picked.targetId);
  }

  Future<void> _resolve(String choice, String targetId) async {
    if (_busy) {
      return;
    }
    setState(() => _busy = true);
    try {
      final result = await resolveSession(
        widget.api,
        sessionId: textOf(widget.session['session_id']),
        choice: choice,
        targetId: targetId,
      );
      if (!mounted) {
        return;
      }
      setState(() => _busy = false);
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(content: Text(textOf(result['message'], fallback: '已处理。'))),
      );
      Navigator.of(context).pop(true);
    } on ApiException catch (exc) {
      if (!mounted) {
        return;
      }
      setState(() => _busy = false);
      ScaffoldMessenger.of(context)
          .showSnackBar(SnackBar(content: Text(exc.message)));
    } catch (_) {
      if (!mounted) {
        return;
      }
      setState(() => _busy = false);
      ScaffoldMessenger.of(context).showSnackBar(
        const SnackBar(content: Text('网络异常，请稍后重试。')),
      );
    }
  }

  /// DM 副行：技能 +（服务端载荷带了历史时）带过 N 个本。
  /// 老版服务端没有 distinct_scripts 字段 → 只显示技能，向后兼容。
  Widget? _dmSubtitle(Map<String, dynamic> dm) {
    final parts = <String>[];
    final skills = textOf(dm['skills']);
    if (skills.isNotEmpty) {
      parts.add(skills);
    }
    final distinct = intOf(dm['distinct_scripts']);
    if (distinct != null) {
      parts.add('带过 $distinct 个本');
    }
    if (parts.isEmpty) {
      return null;
    }
    return Text(
      parts.join(' · '),
      style: const TextStyle(color: AppColors.textFaint, fontSize: 11),
    );
  }

  (String, Color) _statusBadge(String status) {
    if (status == 'cancelled') {
      return ('已取消', AppColors.danger);
    }
    if (status == 'pending') {
      return ('待开场', AppColors.textDim);
    }
    if (status == 'started') {
      return ('已开场', AppColors.mint);
    }
    return (status, AppColors.amber);
  }

  Widget _field(String label, String value, {Color? valueColor}) {
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 7),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          SizedBox(
            width: 64,
            child: Text(
              label,
              style: const TextStyle(color: AppColors.textDim, fontSize: 13),
            ),
          ),
          Expanded(
            child: Text(
              value,
              style: TextStyle(
                color: valueColor ?? AppColors.text,
                fontSize: 13,
                height: 1.4,
              ),
            ),
          ),
        ],
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    final session = widget.session;
    final status =
        sessionStatusKey(session['status'], fallback: '待开场');
    final (statusLabel, statusColor) = _statusBadge(status);
    final booked = intOf(session['booked_seats']) ?? 0;
    final total = intOf(session['total_seats']);
    final seats = total == null ? '$booked 人（容量待补）' : '$booked / $total 人';
    return Scaffold(
      appBar: AppBar(title: const Text('场次详情')),
      body: ListView(
        padding: const EdgeInsets.fromLTRB(16, 10, 16, 28),
        children: [
          Panel(
            padding: const EdgeInsets.all(16),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                _field(
                  '剧本',
                  textOf(session['script_name'], fallback: '未命名剧本'),
                ),
                _field('时间', textOf(session['start_time'], fallback: '待定')),
                _field(
                  '门店',
                  textOf(session['store_name'], fallback: '未填门店'),
                ),
                _field('状态', statusLabel, valueColor: statusColor),
                _field('座位', seats),
                _field(
                  '房间',
                  textOf(session['room_name'], fallback: '待排房'),
                ),
                _field('DM', textOf(session['dm_name'], fallback: '待排 DM')),
                _field('价格', moneyOf(session['list_price'])),
                _field('备注', textOf(session['notes'], fallback: '无')),
                Padding(
                  padding: const EdgeInsets.only(top: 8),
                  child: Text(
                    'ID ${textOf(session['session_id'], fallback: '—')}',
                    style: const TextStyle(
                      color: AppColors.textFaint,
                      fontSize: 11,
                    ),
                  ),
                ),
              ],
            ),
          ),
          const SizedBox(height: 20),
          const SectionTitle(title: '操作'),
          const SizedBox(height: 4),
          FilledButton(
            onPressed: _busy ? null : _openRun,
            child: const Text('生成场控码'),
          ),
          const SizedBox(height: 10),
          OutlinedButton(
            onPressed: _busy ? null : _openReassign,
            child: const Text('换房 / 换 DM'),
          ),
          const SizedBox(height: 10),
          const Text(
            '系统不会自动改开场时间或取消场次；取消请回对话里说明，由店长确认。',
            style: TextStyle(color: AppColors.textFaint, fontSize: 11, height: 1.5),
          ),
        ],
      ),
    );
  }
}
