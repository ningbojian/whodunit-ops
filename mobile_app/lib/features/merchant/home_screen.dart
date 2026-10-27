import 'package:flutter/material.dart';

import '../../core/api_client.dart';
import '../../core/theme.dart';
import '../../ui/session_tile.dart';
import '../../ui/widgets.dart';
import 'run_detail_screen.dart';

class HomeScreen extends StatefulWidget {
  const HomeScreen({
    super.key,
    required this.api,
    required this.onOpenChat,
    required this.onOpenSchedule,
  });

  final ApiClient api;
  final VoidCallback onOpenChat;
  final VoidCallback onOpenSchedule;

  @override
  State<HomeScreen> createState() => _HomeScreenState();
}

class _HomeScreenState extends State<HomeScreen> {
  Map<String, dynamic>? _schedule;
  List<Map<String, dynamic>> _runs = const [];
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
      final schedule = await widget.api.get('/api/app/schedule');
      final runs = await widget.api.get('/api/app/runs');
      if (!mounted) {
        return;
      }
      setState(() {
        _schedule = schedule;
        _runs = asMapList(runs['runs']);
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
        _error = '网络异常，无法加载今日数据。';
      });
    }
  }

  List<Map<String, dynamic>> get _todaySessions {
    final sessions = asMapList(_schedule?['sessions']);
    final now = DateTime.now();
    final today = '${now.year.toString().padLeft(4, '0')}-'
        '${now.month.toString().padLeft(2, '0')}-'
        '${now.day.toString().padLeft(2, '0')}';
    final result = sessions
        .where((item) => dateOf(item['start_time']) == today)
        .toList();
    result.sort(
      (a, b) => textOf(a['start_time']).compareTo(textOf(b['start_time'])),
    );
    return result;
  }

  List<Map<String, dynamic>> get _openRuns {
    return _runs.where((item) {
      final run = asMap(item['run']);
      final status = textOf(run['status']);
      return status == 'pending' || status == 'active' || status == 'paused';
    }).toList();
  }

  @override
  Widget build(BuildContext context) {
    final allSessions = _todaySessions;
    final booked = allSessions.fold<int>(
      0,
      (sum, item) => sum + (intOf(item['booked_seats']) ?? 0),
    );
    final now = DateTime.now();
    const weekdays = ['周一', '周二', '周三', '周四', '周五', '周六', '周日'];
    final today = '${now.month} 月 ${now.day} 日 '
        '${weekdays[now.weekday - 1]}';

    return RefreshIndicator(
      onRefresh: _load,
      child: ListView(
        physics: const AlwaysScrollableScrollPhysics(),
        padding: const EdgeInsets.fromLTRB(16, 8, 16, 28),
        children: [
          Panel(
            borderColor: AppColors.cyan.withValues(alpha: 0.28),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(
                  today,
                  style: const TextStyle(
                    color: AppColors.textDim,
                    fontSize: 13,
                    letterSpacing: 0.5,
                  ),
                ),
                const SizedBox(height: 10),
                Row(
                  children: [
                    _Stat(value: '${allSessions.length}', label: '今日场次'),
                    const SizedBox(width: 12),
                    _Stat(value: '$booked', label: '已约人数'),
                    const SizedBox(width: 12),
                    _Stat(value: '${_openRuns.length}', label: '进行中场控'),
                  ],
                ),
                const SizedBox(height: 14),
                Row(
                  children: [
                    Expanded(
                      child: FilledButton.icon(
                        onPressed: widget.onOpenChat,
                        icon: const Icon(Icons.auto_awesome, size: 18),
                        label: const Text('对话录入'),
                      ),
                    ),
                    const SizedBox(width: 10),
                    Expanded(
                      child: OutlinedButton.icon(
                        onPressed: widget.onOpenSchedule,
                        icon: const Icon(Icons.calendar_month_outlined, size: 18),
                        label: const Text('查看排期'),
                      ),
                    ),
                  ],
                ),
              ],
            ),
          ),
          if (_loading) ...[
            const SizedBox(height: 18),
            const Center(child: CircularProgressIndicator()),
          ],
          if (_error != null) ...[
            const SizedBox(height: 16),
            InlineError(message: _error!, onRetry: _load),
          ],
          if (_openRuns.isNotEmpty) ...[
            const SizedBox(height: 22),
            const SectionTitle(title: '进行中场控'),
            for (final item in _openRuns)
              _RunTile(item: item, api: widget.api),
          ],
          const SizedBox(height: 22),
          SectionTitle(
            title: '今日场次',
            trailing: Text(
              '共 ${allSessions.length} 场',
              style: const TextStyle(color: AppColors.textFaint, fontSize: 12),
            ),
          ),
          if (!_loading && _error == null && allSessions.isEmpty)
            const EmptyHint(
              title: '今天还没有场次',
              message: '到对话里说一句“明晚八点开《雾隐山庄》”，补齐开场时间后'
                  '即可创建场次，自动排房和 DM。',
              icon: Icons.event_available_outlined,
            )
          else
            for (final item in allSessions) SessionTile(session: item),
          const SizedBox(height: 18),
          Container(
            padding: const EdgeInsets.all(14),
            decoration: BoxDecoration(
              color: AppColors.bg2.withValues(alpha: 0.5),
              borderRadius: BorderRadius.circular(14),
              border: Border.all(color: AppColors.line),
            ),
            child: const Row(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Icon(Icons.info_outline, color: AppColors.textFaint, size: 16),
                SizedBox(width: 8),
                Expanded(
                  child: Text(
                    '渠道单数据不是实时同步：只有导入大众点评导出文件后，'
                    '系统才会反映对应座位。内部预约写入会先复核容量。',
                    style: TextStyle(
                      color: AppColors.textFaint,
                      fontSize: 12,
                      height: 1.5,
                    ),
                  ),
                ),
              ],
            ),
          ),
        ],
      ),
    );
  }
}

class _Stat extends StatelessWidget {
  const _Stat({required this.value, required this.label});

  final String value;
  final String label;

  @override
  Widget build(BuildContext context) {
    return Expanded(
      child: Container(
        padding: const EdgeInsets.symmetric(vertical: 12, horizontal: 10),
        decoration: BoxDecoration(
          color: AppColors.bg0.withValues(alpha: 0.42),
          borderRadius: BorderRadius.circular(12),
          border: Border.all(color: AppColors.line),
        ),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(
              value,
              style: const TextStyle(
                color: AppColors.text,
                fontSize: 24,
                fontWeight: FontWeight.w800,
              ),
            ),
            const SizedBox(height: 2),
            Text(
              label,
              style: const TextStyle(color: AppColors.textFaint, fontSize: 11),
            ),
          ],
        ),
      ),
    );
  }
}

class _RunTile extends StatelessWidget {
  const _RunTile({required this.item, required this.api});

  final Map<String, dynamic> item;
  final ApiClient api;

  @override
  Widget build(BuildContext context) {
    final run = asMap(item['run']);
    final session = asMap(item['session']);
    final status = textOf(run['status']);
    final color = runStatusColor(status);
    final seq = intOf(run['current_seq']);
    return Padding(
      padding: const EdgeInsets.only(bottom: 10),
      child: Material(
        color: Colors.transparent,
        child: InkWell(
          borderRadius: BorderRadius.circular(16),
          onTap: () {
            Navigator.of(context).push(
              MaterialPageRoute<void>(
                builder: (_) => RunDetailScreen(
                  api: api,
                  runId: textOf(run['run_id']),
                  initialTitle: textOf(session['script_name']),
                ),
              ),
            );
          },
          child: Ink(
            padding: const EdgeInsets.all(14),
            decoration: BoxDecoration(
              color: AppColors.bg2.withValues(alpha: 0.66),
              borderRadius: BorderRadius.circular(16),
              border: Border.all(color: color.withValues(alpha: 0.34)),
            ),
            child: Row(
              children: [
                Container(
                  width: 10,
                  height: 10,
                  decoration: BoxDecoration(
                    color: color,
                    borderRadius: BorderRadius.circular(999),
                  ),
                ),
                const SizedBox(width: 12),
                Expanded(
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Text(
                        textOf(session['script_name'], fallback: '场次'),
                        style: const TextStyle(
                          color: AppColors.text,
                          fontWeight: FontWeight.w700,
                        ),
                      ),
                      const SizedBox(height: 4),
                      Text(
                        '${textOf(run['room_name'], fallback: '房间未知')} · '
                        '${textOf(run['dm_name'], fallback: '临时 DM')} · '
                        '${seq == null ? '未开始' : '第 $seq 幕'}',
                        style: const TextStyle(
                          color: AppColors.textDim,
                          fontSize: 12,
                        ),
                      ),
                    ],
                  ),
                ),
                Pill(label: runStatusLabel(status), color: color),
                const SizedBox(width: 6),
                const Icon(Icons.chevron_right, color: AppColors.textFaint),
              ],
            ),
          ),
        ),
      ),
    );
  }

}
