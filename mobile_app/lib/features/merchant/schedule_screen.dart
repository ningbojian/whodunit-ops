import 'package:flutter/material.dart';

import '../../core/api_client.dart';
import '../../core/theme.dart';
import '../../ui/session_tile.dart';
import '../../ui/widgets.dart';
import 'schedule_actions.dart';
import 'schedule_assign_sheet.dart';
import 'schedule_timeline.dart';
import 'session_detail_screen.dart';

class ScheduleScreen extends StatefulWidget {
  const ScheduleScreen({
    super.key,
    required this.api,
    this.density = 'compact',
  });

  final ApiClient api;

  /// 排期密度：compact（紧凑列表，默认）| timeline（时间轴）。
  /// 由 MerchantShell 的 AppBar 菜单切换并持久化。
  final String density;

  @override
  State<ScheduleScreen> createState() => _ScheduleScreenState();
}

class _ScheduleScreenState extends State<ScheduleScreen> {
  Map<String, dynamic>? _schedule;
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
      // 只读加载：房间/DM 的补齐走「补齐未分配」的预览+勾选+应用流程，
      // 首次打开绝不静默写入。
      final schedule = await widget.api.get('/api/app/schedule');
      if (!mounted) {
        return;
      }
      setState(() {
        _schedule = schedule;
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
        _error = '网络异常，无法加载排期。';
      });
    }
  }

  Future<void> _openCreate() async {
    final scripts = asMapList(_schedule?['scripts']);
    final changed = await showModalBottomSheet<bool>(
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
        child: _SessionSheet(
          api: widget.api,
          scriptNames: scripts
              .map((item) => textOf(item['script_name']))
              .where((item) => item.isNotEmpty)
              .toList(),
        ),
      ),
    );
    if (changed == true) {
      _load();
    }
  }

  /// 「补齐未分配」：预览勾选底部卡片；应用成功（true）后重新拉取排期。
  Future<void> _openAssign() async {
    final changed = await showModalBottomSheet<bool>(
      context: context,
      isScrollControlled: true,
      backgroundColor: AppColors.bg1,
      shape: const RoundedRectangleBorder(
        borderRadius: BorderRadius.vertical(top: Radius.circular(20)),
      ),
      builder: (context) => ScheduleAssignSheet(api: widget.api),
    );
    if (changed == true) {
      _load();
    }
  }

  Future<void> _openDetail(Map<String, dynamic> session) async {
    final changed = await Navigator.of(context).push<bool>(
      MaterialPageRoute(
        fullscreenDialog: false,
        builder: (context) => SessionDetailScreen(
          api: widget.api,
          session: session,
          rooms: asMapList(_schedule?['rooms']),
          dms: asMapList(_schedule?['dms']),
        ),
      ),
    );
    if (changed == true) {
      _load();
    }
  }

  @override
  Widget build(BuildContext context) {
    final sessions = asMapList(_schedule?['sessions']);
    sessions.sort(
      (a, b) => textOf(a['start_time']).compareTo(textOf(b['start_time'])),
    );

    return RefreshIndicator(
      onRefresh: _load,
      child: ListView(
        physics: const AlwaysScrollableScrollPhysics(),
        padding: const EdgeInsets.fromLTRB(16, 10, 16, 28),
        children: [
          Panel(
            padding: const EdgeInsets.all(14),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Row(
                  children: [
                    const Expanded(
                      child: Column(
                        crossAxisAlignment: CrossAxisAlignment.start,
                        children: [
                          Text(
                            '未来 7 天',
                            style: TextStyle(
                              color: AppColors.text,
                              fontWeight: FontWeight.w700,
                              fontSize: 15,
                            ),
                          ),
                          SizedBox(height: 4),
                          Text(
                            '场次时间固定；系统只自动分配房间和 DM，不改开场时间。',
                            style: TextStyle(
                              color: AppColors.textFaint,
                              fontSize: 11,
                              height: 1.4,
                            ),
                          ),
                        ],
                      ),
                    ),
                    const SizedBox(width: 10),
                    FilledButton(
                      onPressed: () => _openCreate(),
                      child: const Text('新增场次'),
                    ),
                  ],
                ),
                const SizedBox(height: 10),
                // 窄屏安全：按钮单独一行，可换行，不与标题抢宽度。
                Wrap(
                  spacing: 8,
                  runSpacing: 6,
                  children: [
                    OutlinedButton(
                      onPressed: _loading ? null : () => _openAssign(),
                      style: OutlinedButton.styleFrom(
                        minimumSize: const Size(0, 40),
                        padding: const EdgeInsets.symmetric(horizontal: 14),
                        textStyle: const TextStyle(
                          fontSize: 13,
                          fontWeight: FontWeight.w600,
                        ),
                      ),
                      child: const Text('补齐未分配'),
                    ),
                  ],
                ),
              ],
            ),
          ),
          if (_loading)
            const Padding(
              padding: EdgeInsets.all(28),
              child: Center(child: CircularProgressIndicator()),
            ),
          if (_error != null) ...[
            const SizedBox(height: 14),
            InlineError(message: _error!, onRetry: _load),
          ],
          const SizedBox(height: 20),
          const SectionTitle(title: '场次'),
          if (!_loading && _error == null && sessions.isEmpty)
            const EmptyHint(
              title: '未来 7 天还没有场次',
              message: '新增时只填剧本和开场时间，人数/价格可以先空着；'
                  '保存后系统会自动排房和 DM。',
              icon: Icons.calendar_month_outlined,
            )
          else if (widget.density == 'timeline')
            ..._buildTimeline(sessions)
          else
            ..._buildCompactGroups(sessions),
        ],
      ),
    );
  }

  List<Widget> _buildCompactGroups(List<Map<String, dynamic>> sessions) {
    final children = <Widget>[];
    String? lastDate;
    for (final session in sessions) {
      final date = dateOf(session['start_time']);
      if (date != lastDate) {
        lastDate = date;
        children.add(_DateHeader(date: date));
      }
      children.add(
        SessionTile(
          session: session,
          compact: true,
          onTap: () => _openDetail(session),
        ),
      );
    }
    return children;
  }

  List<Widget> _buildTimeline(List<Map<String, dynamic>> sessions) {
    final byDate = <String, List<Map<String, dynamic>>>{};
    for (final session in sessions) {
      // 时间轴按营业日分组：凌晨场归入前一天，与 +14h 跨午夜映射一致；
      // 紧凑列表的自然日分组不改。
      byDate
          .putIfAbsent(
            timelineDateKey(textOf(session['start_time'])),
            () => [],
          )
          .add(session);
    }
    final children = <Widget>[];
    byDate.forEach((date, items) {
      children.add(_DateHeader(date: date));
      children.add(
        _TimelineDay(
          sessions: items,
          onTap: (session) => _openDetail(session),
        ),
      );
      children.add(const SizedBox(height: 10));
    });
    return children;
  }
}

class _DateHeader extends StatelessWidget {
  const _DateHeader({required this.date});

  final String date;

  @override
  Widget build(BuildContext context) {
    const weekdays = ['周一', '周二', '周三', '周四', '周五', '周六', '周日'];
    String suffix = '';
    final parsed = DateTime.tryParse(date);
    if (parsed != null) {
      suffix = ' ${weekdays[parsed.weekday - 1]}';
    }
    return Padding(
      padding: const EdgeInsets.only(top: 8, bottom: 8),
      child: Row(
        children: [
          Text(
            '$date$suffix',
            style: const TextStyle(
              color: AppColors.textDim,
              fontSize: 12,
              fontWeight: FontWeight.w700,
              letterSpacing: 0.6,
            ),
          ),
          const SizedBox(width: 10),
          const Expanded(child: Divider(color: AppColors.line, height: 1)),
        ],
      ),
    );
  }
}

class _SessionSheet extends StatefulWidget {
  const _SessionSheet({
    required this.api,
    required this.scriptNames,
  });

  final ApiClient api;
  final List<String> scriptNames;

  @override
  State<_SessionSheet> createState() => _SessionSheetState();
}

class _SessionSheetState extends State<_SessionSheet> {
  late final TextEditingController _script;
  late final TextEditingController _start;
  late final TextEditingController _seats;
  late final TextEditingController _price;
  late final TextEditingController _notes;
  bool _busy = false;
  bool _changed = false;
  Map<String, dynamic>? _result;
  String? _error;
  String? _resolveMessage;

  @override
  void initState() {
    super.initState();
    _script = TextEditingController();
    _start = TextEditingController();
    _seats = TextEditingController();
    _price = TextEditingController();
    _notes = TextEditingController();
  }

  @override
  void dispose() {
    _script.dispose();
    _start.dispose();
    _seats.dispose();
    _price.dispose();
    _notes.dispose();
    super.dispose();
  }

  String _quickStart(int dayOffset, int hour) {
    final now = DateTime.now();
    final date = DateTime(now.year, now.month, now.day)
        .add(Duration(days: dayOffset));
    String two(int value) => value.toString().padLeft(2, '0');
    return '${date.year}-${two(date.month)}-${two(date.day)} ${two(hour)}:00';
  }

  Future<void> _submit() async {
    if (_busy) {
      return;
    }
    setState(() {
      _busy = true;
      _error = null;
      _resolveMessage = null;
    });
    final notes = _notes.text.trim();
    try {
      final result = await widget.api.post(
        '/api/app/sessions',
        body: {
          'script_name': _script.text.trim(),
          'start_time': _start.text.trim(),
          'total_seats': int.tryParse(_seats.text.trim()),
          'list_price': double.tryParse(_price.text.trim()),
          if (notes.isNotEmpty) 'notes': notes,
        },
      );
      _changed = true;
      if (!mounted) {
        return;
      }
      setState(() {
        _result = result;
        _busy = false;
      });
    } on ApiException catch (exc) {
      if (!mounted) {
        return;
      }
      setState(() {
        _busy = false;
        _error = _serverMessage(exc);
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

  /// 服务端 400 的建议问题放在 error.message 里；兼容 ApiClient 直接
  /// toString 的旧取法，优先取出嵌套对象中的 message。
  String _serverMessage(ApiException exc) {
    final error = exc.body?['error'];
    if (error is Map) {
      return textOf(error['message'], fallback: exc.message);
    }
    return exc.message;
  }

  Future<void> _resolve(
    String sessionId,
    String choice,
    String? targetId,
  ) async {
    setState(() {
      _busy = true;
      _error = null;
    });
    try {
      final result = await resolveSession(
        widget.api,
        sessionId: sessionId,
        choice: choice,
        targetId: targetId,
      );
      _changed = true;
      if (!mounted) {
        return;
      }
      setState(() {
        _busy = false;
        _resolveMessage = textOf(
          result['message'],
          fallback: '已处理。',
        );
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
    return ConstrainedBox(
      constraints: BoxConstraints(
        maxHeight: MediaQuery.of(context).size.height * 0.9,
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
                    '新增场次',
                    style: TextStyle(
                      color: AppColors.text,
                      fontSize: 20,
                      fontWeight: FontWeight.w800,
                    ),
                  ),
                ),
                IconButton(
                  onPressed: () => Navigator.of(context).pop(_changed),
                  icon: const Icon(Icons.close),
                  tooltip: '关闭',
                ),
              ],
            ),
            const SizedBox(height: 6),
            const Text(
              '必填只有剧本全名和开场时间。人数、价格、类型可以后补；'
              '房间和 DM 由系统自动分配。',
              style: TextStyle(
                color: AppColors.textFaint,
                fontSize: 12,
                height: 1.5,
              ),
            ),
            const SizedBox(height: 18),
            Autocomplete<String>(
              optionsBuilder: (value) {
                final query = value.text.trim();
                if (query.isEmpty) {
                  return widget.scriptNames.take(8);
                }
                return widget.scriptNames
                    .where((name) => name.contains(query))
                    .take(8);
              },
              onSelected: (value) => _script.text = value,
              fieldViewBuilder: (context, controller, focusNode, onSubmit) {
                return TextField(
                  controller: controller,
                  focusNode: focusNode,
                  enabled: !_busy,
                  onChanged: (value) => _script.text = value,
                  decoration: const InputDecoration(
                    labelText: '剧本全名',
                    hintText: '例如：雾隐山庄（也可以先写缩写）',
                  ),
                );
              },
            ),
            const SizedBox(height: 14),
            TextField(
              controller: _start,
              enabled: !_busy,
              decoration: const InputDecoration(
                labelText: '开场时间',
                hintText: '2026-10-21 20:00',
              ),
            ),
            const SizedBox(height: 8),
            Wrap(
              spacing: 8,
              runSpacing: 6,
              children: [
                for (final entry in [
                  ('今晚 19:00', _quickStart(0, 19)),
                  ('今晚 20:00', _quickStart(0, 20)),
                  ('明晚 19:00', _quickStart(1, 19)),
                  ('明晚 20:00', _quickStart(1, 20)),
                ])
                  ActionChip(
                    label: Text(
                      entry.$1,
                      style: const TextStyle(
                        color: AppColors.textDim,
                        fontSize: 12,
                      ),
                    ),
                    backgroundColor: AppColors.bg2.withValues(alpha: 0.6),
                    side: const BorderSide(color: AppColors.line),
                    onPressed: _busy
                        ? null
                        : () => setState(() => _start.text = entry.$2),
                  ),
              ],
            ),
            const SizedBox(height: 14),
            Row(
              children: [
                Expanded(
                  child: TextField(
                    controller: _seats,
                    enabled: !_busy,
                    keyboardType: TextInputType.number,
                    decoration: const InputDecoration(
                      labelText: '人数（可后补）',
                      hintText: '6',
                    ),
                  ),
                ),
                const SizedBox(width: 12),
                Expanded(
                  child: TextField(
                    controller: _price,
                    enabled: !_busy,
                    keyboardType: const TextInputType.numberWithOptions(
                      decimal: true,
                    ),
                    decoration: const InputDecoration(
                      labelText: '原价（可后补）',
                      hintText: '168',
                    ),
                  ),
                ),
              ],
            ),
            const SizedBox(height: 14),
            TextField(
              controller: _notes,
              enabled: !_busy,
              maxLines: 2,
              decoration: const InputDecoration(
                labelText: '备注（可选）',
                hintText: '例如：玩家想玩情感本',
              ),
            ),
            const SizedBox(height: 18),
            FilledButton(
              onPressed: _busy ? null : () => _submit(),
              child: _busy
                  ? const SizedBox(
                      width: 22,
                      height: 22,
                      child: CircularProgressIndicator(strokeWidth: 2),
                    )
                  : const Text('保存场次'),
            ),
            if (_error != null) ...[
              const SizedBox(height: 16),
              InlineError(message: _error!),
            ],
            if (_result != null) ...[
              const SizedBox(height: 18),
              _ResultPanel(
                result: _result!,
                busy: _busy,
                resolveMessage: _resolveMessage,
                onResolve: _resolve,
              ),
            ],
          ],
        ),
      ),
    );
  }
}

/// 创建成功的回执面板：只有「已建场次」这一种结果。
/// 不完整/需确认都是 HTTP 400，走表单上方的 InlineError，不进这里。
class _ResultPanel extends StatelessWidget {
  const _ResultPanel({
    required this.result,
    required this.busy,
    required this.onResolve,
    this.resolveMessage,
  });

  final Map<String, dynamic> result;
  final bool busy;
  final String? resolveMessage;
  final Future<void> Function(String sessionId, String choice, String? target)
      onResolve;

  @override
  Widget build(BuildContext context) {
    final session = asMap(result['session']);
    final auto = asMap(result['auto_assign']);
    final autoStatus = textOf(auto['status']);
    final assigned = asMapList(auto['assigned']);
    final conflicts = asMapList(auto['conflicts']);
    final warnings = asStringList(result['warnings']);

    return Panel(
      borderColor: AppColors.mint.withValues(alpha: 0.3),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          const Pill(label: '场次已保存', color: AppColors.mint),
          const SizedBox(height: 12),
          Text(
            '${textOf(session['script_name'], fallback: '场次')} · '
            '${textOf(session['start_time'])}',
            style: const TextStyle(
              color: AppColors.text,
              fontWeight: FontWeight.w600,
            ),
          ),
          const SizedBox(height: 12),
          if (autoStatus == 'assigned' && assigned.isNotEmpty) ...[
            for (final item in assigned)
              Padding(
                padding: const EdgeInsets.only(bottom: 4),
                child: Text(
                  '已自动排到 ${textOf(item['room_name'], fallback: '未知房间')} / '
                  '${textOf(item['dm_name'], fallback: '未知 DM')}',
                  style: const TextStyle(
                    color: AppColors.mint,
                    fontSize: 13,
                    height: 1.5,
                  ),
                ),
              ),
          ] else if (autoStatus == 'resources_not_configured') ...[
            const Text(
              '场次已保存，但还没有房间或 DM 档案，暂时不能自动排期。'
              '请先在网页端资源页建档或载入演示资源。',
              style: TextStyle(
                color: AppColors.amber,
                fontSize: 13,
                height: 1.5,
              ),
            ),
          ] else if (autoStatus == 'unchanged') ...[
            const Text(
              '场次已保存；该场已有分配，本次没有改动。',
              style: TextStyle(
                color: AppColors.textDim,
                fontSize: 13,
                height: 1.5,
              ),
            ),
          ],
          for (final conflict in conflicts) ...[
            const SizedBox(height: 12),
            Container(
              padding: const EdgeInsets.all(12),
              decoration: BoxDecoration(
                color: AppColors.amber.withValues(alpha: 0.08),
                borderRadius: BorderRadius.circular(10),
                border: Border.all(color: AppColors.amber.withValues(alpha: 0.24)),
              ),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(
                    textOf(conflict['message'], fallback: '资源冲突，需要你决定。'),
                    style: const TextStyle(
                      color: AppColors.text,
                      fontSize: 13,
                      height: 1.5,
                    ),
                  ),
                  const SizedBox(height: 8),
                  Wrap(
                    spacing: 8,
                    runSpacing: 6,
                    children: [
                      for (final option in asMapList(conflict['options']))
                        _OptionButton(
                          option: option,
                          busy: busy,
                          onResolve: onResolve,
                          sessionId: textOf(conflict['session_id']),
                        ),
                    ],
                  ),
                ],
              ),
            ),
          ],
          if (warnings.isNotEmpty) ...[
            const SizedBox(height: 10),
            for (final warning in warnings)
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
          if (resolveMessage != null) ...[
            const SizedBox(height: 10),
            Text(
              resolveMessage!,
              style: const TextStyle(
                color: AppColors.cyan,
                fontSize: 12,
                height: 1.5,
              ),
            ),
          ],
        ],
      ),
    );
  }
}

class _OptionButton extends StatelessWidget {
  const _OptionButton({
    required this.option,
    required this.busy,
    required this.onResolve,
    required this.sessionId,
  });

  final Map<String, dynamic> option;
  final bool busy;
  final Future<void> Function(String sessionId, String choice, String? target)
      onResolve;
  final String sessionId;

  @override
  Widget build(BuildContext context) {
    final type = textOf(option['type']);
    final label = textOf(option['label'], fallback: type);
    final target = textOf(option['target_id']);
    final applicable = type == 'switch_room' ||
        type == 'switch_dm' ||
        type == 'keep_unassigned';
    return OutlinedButton(
      onPressed: busy
          ? null
          : () {
              if (applicable) {
                onResolve(sessionId, type, target.isEmpty ? null : target);
              } else {
                showDialog<void>(
                  context: context,
                  builder: (context) => AlertDialog(
                    backgroundColor: AppColors.bg2,
                    title: Text(label),
                    content: const Text('这项操作需要店长确认；系统不会自动改时间、取消场次或新建资源。'),
                    actions: [
                      TextButton(
                        onPressed: () => Navigator.of(context).pop(),
                        child: const Text('知道了'),
                      ),
                    ],
                  ),
                );
              }
            },
      child: Text(label),
    );
  }
}

/// 单日时间轴：左 44px 小时刻度（10:00 → 次日 04:00），右侧场次点块。
class _TimelineDay extends StatelessWidget {
  const _TimelineDay({required this.sessions, required this.onTap});

  final List<Map<String, dynamic>> sessions;
  final void Function(Map<String, dynamic> session) onTap;

  static const double _scaleWidth = 44;
  static const double _pxPerMinute = 1.4;

  @override
  Widget build(BuildContext context) {
    const height = timelineSpanMinutes * _pxPerMinute;
    // 小时刻度见 timelineHourMarks（10..23 然后次日 0..4）；
    // 04:00 为视窗底界，网格线 clamp 到 height-1 贴底收边
    // （否则恰在容器外 1px 的线会被 Stack 默认裁剪吃掉）。
    const hourMarks = timelineHourMarks;
    double hourTop(int hour) =>
        timelineOffsetMinutes(DateTime(2026, 1, 1, hour)) * _pxPerMinute;
    return Row(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        SizedBox(
          width: _scaleWidth,
          height: height,
          child: Stack(
            clipBehavior: Clip.none,
            children: [
              for (final hour in hourMarks)
                Positioned(
                  top: hourTop(hour) - 6,
                  left: 0,
                  right: 4,
                  child: Text(
                    '${hour.toString().padLeft(2, '0')}:00',
                    textAlign: TextAlign.right,
                    style: const TextStyle(
                      color: AppColors.textFaint,
                      fontSize: 10,
                    ),
                  ),
                ),
            ],
          ),
        ),
        const SizedBox(width: 6),
        Expanded(
          child: SizedBox(
            height: height,
            child: Stack(
              children: [
                for (final hour in hourMarks)
                  Positioned(
                    // 04:00 线恰在容器底界外 1px，clamp 回底边参与收边。
                    top: hourTop(hour).clamp(0.0, height - 1),
                    left: 0,
                    right: 0,
                    child: Container(
                      height: 1,
                      color: AppColors.line.withValues(alpha: 0.45),
                    ),
                  ),
                for (final session in sessions)
                  _TimelineBlock(
                    session: session,
                    pxPerMinute: _pxPerMinute,
                    onTap: () => onTap(session),
                  ),
              ],
            ),
          ),
        ),
      ],
    );
  }
}

/// 时间轴上的单个场次块；视窗外（05:00–09:59 开场）clamp 到顶边，
/// 04:00 之后贴底边保持部分可见——两者都带「视窗外」标记。
class _TimelineBlock extends StatelessWidget {
  const _TimelineBlock({
    required this.session,
    required this.pxPerMinute,
    required this.onTap,
  });

  final Map<String, dynamic> session;
  final double pxPerMinute;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    final start = DateTime.tryParse(textOf(session['start_time']));
    if (start == null) {
      return Positioned(
        top: 0,
        left: 0,
        right: 0,
        child: Text(
          '${textOf(session['script_name'], fallback: '未命名剧本')}（时间无效）',
          style: const TextStyle(color: AppColors.danger, fontSize: 10),
        ),
      );
    }
    final offset = timelineOffsetMinutes(start);
    final visible = timelineVisible(offset);
    final minutes = clampDurationMinutes(intOf(session['duration_minutes']));
    final blockHeight = (minutes * pxPerMinute).clamp(26.0, 240.0);
    // 视窗内块用真实 top；04:00 之后开场的块贴底保持部分可见
    // （纯函数里只对窗外块 clamp，避免相邻深夜场次被位移出虚假重叠）。
    final top = timelineBlockTop(
      offset,
      blockHeight: blockHeight,
      pxPerMinute: pxPerMinute,
    );
    final cancelled = sessionStatusKey(session['status']) == 'cancelled';
    final booked = intOf(session['booked_seats']) ?? 0;
    final total = intOf(session['total_seats']);
    final accent = cancelled
        ? AppColors.danger
        : total != null && booked >= total
            ? AppColors.amber
            : AppColors.cyan;
    return Positioned(
      top: top,
      left: 0,
      right: 0,
      child: Material(
        color: Colors.transparent,
        child: InkWell(
          onTap: onTap,
          borderRadius: BorderRadius.circular(8),
          child: Ink(
            height: blockHeight,
            decoration: BoxDecoration(
              color: accent.withValues(alpha: 0.12),
              borderRadius: BorderRadius.circular(8),
              border: Border(
                left: BorderSide(color: accent, width: 3),
                top: const BorderSide(color: AppColors.line),
                right: const BorderSide(color: AppColors.line),
                bottom: const BorderSide(color: AppColors.line),
              ),
            ),
            child: Padding(
              padding: const EdgeInsets.fromLTRB(8, 3, 8, 3),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                mainAxisSize: MainAxisSize.min,
                children: [
                  Text(
                    '${clockOf(session['start_time'])} '
                    '${textOf(session['script_name'], fallback: '未命名剧本')}'
                    '${cancelled ? '（已取消）' : ''}',
                    maxLines: 1,
                    overflow: TextOverflow.ellipsis,
                    style: TextStyle(
                      color: cancelled ? AppColors.danger : AppColors.text,
                      fontSize: 12,
                      fontWeight: FontWeight.w600,
                    ),
                  ),
                  Text(
                    visible
                        ? (total == null
                            ? '$booked 人'
                            : '$booked / $total 人')
                        : '时间在 10:00–04:00 视窗外',
                    maxLines: 1,
                    overflow: TextOverflow.ellipsis,
                    style: const TextStyle(
                      color: AppColors.textFaint,
                      fontSize: 10,
                    ),
                  ),
                ],
              ),
            ),
          ),
        ),
      ),
    );
  }
}
