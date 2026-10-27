import 'dart:async';

import 'package:flutter/material.dart';
import 'package:uuid/uuid.dart';
import 'package:wakelock_plus/wakelock_plus.dart';

import '../../core/api_client.dart';
import '../../core/auth_store.dart';
import '../../core/clock.dart';
import '../../core/notifications.dart';
import '../../core/queue.dart';
import '../../core/session.dart';
import '../../core/theme.dart';
import '../../ui/widgets.dart';
import 'local_state.dart';

/// DM 场控台。
///
/// 倒计时到点只提醒、只把界面标红，绝不调用 `next`；阶段只能由 DM 手动切换。
class DmConsoleScreen extends StatefulWidget {
  const DmConsoleScreen({
    super.key,
    required this.session,
    required this.api,
    required this.auth,
    required this.queue,
    required this.clock,
    required this.notifications,
  });

  final AppSession session;
  final ApiClient api;
  final AuthStore auth;
  final ActionQueue queue;
  final ServerClock clock;
  final NotificationService notifications;

  @override
  State<DmConsoleScreen> createState() => _DmConsoleScreenState();
}

class _DmConsoleScreenState extends State<DmConsoleScreen>
    with WidgetsBindingObserver {
  Map<String, dynamic>? _run;
  Map<String, dynamic>? _session;
  List<Map<String, dynamic>> _stages = const [];
  List<String> _warnings = const [];
  Map<String, dynamic>? _hardware;
  bool _templatePreview = false;

  bool _loading = true;
  bool _busy = false;
  bool _offline = false;
  bool _signedOut = false;
  bool _wakelockOn = false;
  int _pendingCount = 0;
  String? _error;
  String? _notice;

  Timer? _ticker;

  String get _status => textOf(_run?['status']);

  bool get _isLive => _status == 'active' || _status == 'paused';

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
    _ticker = Timer.periodic(const Duration(seconds: 1), (_) {
      if (!mounted) {
        return;
      }
      if (_isLive) {
        setState(() {});
      }
    });
    _bootstrap();
  }

  @override
  void dispose() {
    _ticker?.cancel();
    WidgetsBinding.instance.removeObserver(this);
    _releaseWakelock();
    super.dispose();
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    if (state == AppLifecycleState.resumed && !_signedOut) {
      _resumeFromBackground();
    }
  }

  Future<void> _bootstrap() async {
    setState(() {
      _loading = true;
      _error = null;
    });
    await _syncQueue();
    if (_signedOut) {
      return;
    }
    await _fetchRun(silent: false);
    if (!mounted) {
      return;
    }
    setState(() => _loading = false);
  }

  Future<void> _resumeFromBackground() async {
    await _refreshAll();
  }

  Future<void> _refreshAll() async {
    if (_busy || _signedOut) {
      return;
    }
    await _syncQueue();
    if (_signedOut) {
      return;
    }
    await _fetchRun(silent: true);
  }

  bool _isAuthError(ApiException exc) {
    return exc.status == 401 &&
        (exc.code == 'token_invalid' ||
            exc.code == 'code_invalid' ||
            exc.code == 'unauthorized' ||
            exc.code == 'token_expired');
  }

  Future<bool> _fetchRun({required bool silent}) async {
    try {
      final view = await widget.api.get('/api/dm/run');
      widget.clock.syncFromIso(textOf(view['server_now']));
      if (!mounted) {
        return false;
      }
      setState(() {
        _applyView(view);
        _offline = false;
        _error = null;
        _loading = false;
      });
      await _afterStateChange();
      return true;
    } on ApiException catch (exc) {
      if (_isAuthError(exc)) {
        await _handleAuthLost();
        return false;
      }
      if (!mounted) {
        return false;
      }
      setState(() {
        _loading = false;
        if (exc.isNetwork) {
          _offline = true;
          if (_run == null) {
            _error = '当前处于离线状态，无法加载场控。恢复网络后会自动重试。';
          }
        } else if (!silent || _run == null) {
          _error = exc.message;
        }
      });
      return false;
    } catch (_) {
      if (!mounted) {
        return false;
      }
      setState(() {
        _loading = false;
        _offline = true;
        if (_run == null) {
          _error = '网络异常，无法加载场控。';
        }
      });
      return false;
    }
  }

  void _applyView(Map<String, dynamic> view) {
    if (view['run'] is Map) {
      _run = asMap(view['run']);
    }
    if (view['session'] is Map) {
      _session = asMap(view['session']);
    }
    if (view['stages'] is List) {
      _stages = asMapList(view['stages']);
    }
    if (view['warnings'] is List) {
      _warnings = asStringList(view['warnings']);
    }
    if (view['hardware'] is Map) {
      _hardware = asMap(view['hardware']);
    }
    if (view.containsKey('template_preview')) {
      _templatePreview = view['template_preview'] == true;
    }
  }

  Future<void> _syncQueue() async {
    List<PendingAction> pending;
    try {
      pending = await widget.queue.pending();
    } catch (_) {
      return;
    }
    if (pending.isEmpty) {
      if (mounted) {
        setState(() => _pendingCount = 0);
      }
      return;
    }
    if (mounted) {
      setState(() => _pendingCount = pending.length);
    }
    Map<String, dynamic>? payload;
    try {
      payload = await widget.api.post(
        '/api/dm/run/sync',
        body: {'actions': pending.map((item) => item.toRequest()).toList()},
      );
    } on ApiException catch (exc) {
      if (_isAuthError(exc)) {
        await _handleAuthLost();
        return;
      }
      if (exc.isNetwork) {
        if (mounted) {
          setState(() => _offline = true);
        }
        return;
      }
      payload = exc.body;
    } catch (_) {
      if (mounted) {
        setState(() => _offline = true);
      }
      return;
    }
    await _consumeSyncPayload(payload, pending);
  }

  Future<void> _consumeSyncPayload(
    Map<String, dynamic>? payload,
    List<PendingAction> pending,
  ) async {
    if (payload == null) {
      if (mounted) {
        setState(() {
          _notice = '补传暂时失败，离线操作仍保留，恢复网络后会再试。';
        });
      }
      return;
    }
    final results = asMapList(payload['results']);
    final applied = <String>[];
    String? failure;
    for (var index = 0; index < pending.length; index++) {
      final result = index < results.length ? results[index] : null;
      if (result != null && result['ok'] == true) {
        applied.add(pending[index].id);
      } else {
        failure = result == null
            ? '服务端没有返回这一条的结果'
            : textOf(result['error'], fallback: '操作未应用');
        break;
      }
    }
    var count = pending.length - applied.length;
    try {
      await widget.queue.removeAll(applied);
      if (failure != null) {
        // 后续动作依赖前一条的版本，一并丢弃并以服务端状态为准。
        final rest = await widget.queue.pending();
        await widget.queue.removeAll(rest.map((item) => item.id));
      }
      count = (await widget.queue.pending()).length;
    } catch (_) {
      failure ??= '本机队列清理失败';
    }
    final syncedRun = payload['run'];
    if (!mounted) {
      return;
    }
    setState(() {
      _pendingCount = count;
      _offline = false;
      if (syncedRun is Map) {
        _run = asMap(syncedRun);
        _markStages();
      }
      if (failure != null) {
        _notice = '离线操作未全部应用：$failure 已按服务器状态恢复。';
      } else if (pending.isNotEmpty) {
        _notice = '已补传 ${applied.length} 条离线操作；补传不会补发灯光/音效。';
      }
    });
  }

  /// 服务端 run 载荷不含阶段状态时，按 current_seq 重建当前幕标记。
  void _markStages() {
    final seq = intOf(_run?['current_seq']);
    for (final stage in _stages) {
      final current = intOf(stage['seq']);
      stage['is_current'] = current != null && current == seq;
      if (seq == null || current == null) {
        stage['state'] = 'upcoming';
      } else if (current < seq) {
        stage['state'] = 'completed';
      } else if (current == seq) {
        stage['state'] = 'current';
      } else {
        stage['state'] = 'upcoming';
      }
    }
  }

  Future<void> _afterStateChange() async {
    await _syncWakelock();
    await _syncNotifications();
  }

  Future<void> _syncNotifications() async {
    final run = _run;
    if (run == null) {
      return;
    }
    final status = textOf(run['status']);
    final seq = intOf(run['current_seq']);
    final current = currentStageOf(_stages);
    if (status == 'active' && seq != null) {
      final ends = _parseIso(run['stage_ends_at']);
      if (ends == null) {
        return;
      }
      await widget.notifications.scheduleStage(
        seq: seq,
        endsAtUtc: ends,
        warnBeforeSeconds: intOf(current?['warn_before_seconds']) ?? 60,
        prompt: textOf(current?['prompt_text']),
      );
      return;
    }
    if (status == 'completed' || status == 'abandoned') {
      await widget.notifications.cancelAll(
        _stages.map((stage) => intOf(stage['seq']) ?? 0),
      );
      return;
    }
    if (seq != null) {
      await widget.notifications.cancelStage(seq);
    }
  }

  Future<void> _syncWakelock() async {
    final shouldKeep = _isLive;
    if (shouldKeep == _wakelockOn) {
      return;
    }
    _wakelockOn = shouldKeep;
    try {
      if (shouldKeep) {
        await WakelockPlus.enable();
      } else {
        await WakelockPlus.disable();
      }
    } catch (_) {
      // 厂商限制或权限问题时静默；计时不依赖常亮。
    }
  }

  Future<void> _releaseWakelock() async {
    _wakelockOn = false;
    try {
      await WakelockPlus.disable();
    } catch (_) {
      // 忽略。
    }
  }

  DateTime? _parseIso(Object? value) {
    final text = textOf(value);
    if (text.isEmpty) {
      return null;
    }
    final parsed = DateTime.tryParse(text);
    if (parsed == null) {
      return null;
    }
    return parsed.isUtc ? parsed : parsed.toUtc();
  }

  Future<void> _act(
    String action, {
    int? minutes,
    String? mode,
  }) async {
    if (_busy || _signedOut) {
      return;
    }
    if (_pendingCount > 0) {
      await _syncQueue();
      if (_signedOut) {
        return;
      }
      if (_pendingCount > 0 && !_offline) {
        setState(() {
          _notice = '还有离线操作没有补传成功；先刷新同步，再继续操作。';
        });
        return;
      }
      if (_pendingCount == 0) {
        await _fetchRun(silent: true);
        if (_signedOut) {
          return;
        }
      }
    }
    final run = _run;
    if (run == null) {
      return;
    }
    final status = textOf(run['status']);
    if (status == 'completed' || status == 'abandoned') {
      return;
    }
    if (action == 'hardware_retry' && _offline) {
      setState(() => _notice = '硬件重试需要联网；离线时只显示文字提示。');
      return;
    }
    final at = widget.clock.nowUtc();
    final expected = intOf(run['state_version']) ?? 0;
    final eventId = const Uuid().v4();
    final local = applyLocalAction(
      run: run,
      stages: _stages,
      action: action,
      atUtc: at,
      minutes: minutes,
      mode: mode,
    );
    if (!local.ok) {
      setState(() => _notice = local.error);
      return;
    }
    setState(() {
      _busy = true;
      _notice = null;
      _hardware = null;
    });
    try {
      final result = await widget.api.post(
        '/api/dm/run/action',
        body: {
          'action': action,
          'client_event_id': eventId,
          'expected_version': expected,
          'at': at.toIso8601String(),
          if (minutes != null) 'minutes': minutes,
          if (mode != null) 'mode': mode,
        },
      );
      widget.clock.syncFromIso(textOf(result['server_now']));
      if (!mounted) {
        return;
      }
      setState(() {
        _applyView(result);
        _offline = false;
        _notice = _hardwareNotice(action);
      });
      await _afterStateChange();
      if (_status == 'completed') {
        try {
          await widget.queue.clear();
        } catch (_) {
          // 队列清理失败不影响已结束的场控。
        }
        if (mounted) {
          setState(() => _pendingCount = 0);
        }
      }
    } on ApiException catch (exc) {
      if (_isAuthError(exc)) {
        await _handleAuthLost();
        return;
      }
      if (exc.isNetwork) {
        if (action == 'hardware_retry') {
          if (mounted) {
            setState(() {
              _offline = true;
              _notice = '硬件重试需要联网；当前仍保持降级，只显示文字提示。';
            });
          }
          return;
        }
        var queued = false;
        try {
          queued = await widget.queue.enqueue(
            PendingAction(
              id: eventId,
              action: action,
              at: at.toIso8601String(),
              expectedVersion: expected,
              minutes: minutes,
              mode: mode,
            ),
          );
        } catch (_) {
          queued = false;
        }
        if (!mounted) {
          return;
        }
        if (!queued) {
          setState(() {
            _notice = '离线操作已达到 200 条上限，请恢复网络后再继续。';
          });
          return;
        }
        setState(() {
          _run = local.run;
          _stages = local.stages;
          _offline = true;
          _pendingCount += 1;
          _notice = '已离线：操作已本地生效并排队，恢复网络后自动补传。'
              '补传不会补发灯光/音效。';
        });
        await _afterStateChange();
        return;
      }
      if (exc.code == 'stale_state') {
        await _fetchRun(silent: true);
        if (mounted) {
          setState(() {
            _notice = '另一台设备已更新场控，界面已按服务器状态恢复。';
          });
        }
        return;
      }
      final body = exc.body;
      if (body != null && body['run'] is Map && body['stages'] is List) {
        if (mounted) {
          setState(() {
            _applyView(body);
            _notice = exc.message;
          });
        }
        await _afterStateChange();
      } else {
        await _fetchRun(silent: true);
        if (mounted) {
          setState(() => _notice = exc.message);
        }
      }
    } catch (_) {
      if (!mounted) {
        return;
      }
      setState(() => _notice = '网络异常，操作没有确认；请刷新后再试。');
    } finally {
      if (mounted) {
        setState(() => _busy = false);
      }
    }
  }

  String? _hardwareNotice(String action) {
    final hardware = _hardware;
    if (hardware == null) {
      return null;
    }
    final status = textOf(hardware['status']);
    final message = textOf(hardware['message']);
    if (status == 'failed') {
      return '硬件指令发送失败，已降级为文字提示；阶段仍已切换，不会回滚。'
          '${message.isEmpty ? '' : '（$message）'}';
    }
    if (status == 'ok') {
      return '硬件场景已下发。';
    }
    if (action == 'hardware_retry') {
      return message.isEmpty ? '硬件重试未成功。' : message;
    }
    return null;
  }

  Future<void> _handleAuthLost() async {
    if (_signedOut) {
      return;
    }
    if (mounted) {
      ScaffoldMessenger.of(context).showSnackBar(
        const SnackBar(
          content: Text('场控令牌已失效，请让店长重新生成场控码。'),
        ),
      );
    }
    await _logout();
  }

  Future<void> _logout() async {
    if (_signedOut) {
      return;
    }
    _signedOut = true;
    try {
      await widget.queue.clear();
    } catch (_) {
      // 队列清理失败不阻止退出。
    }
    try {
      await widget.notifications.cancelAll(
        _stages.map((stage) => intOf(stage['seq']) ?? 0),
      );
    } catch (_) {
      // 忽略。
    }
    await _releaseWakelock();
    await widget.session.signOutDm(auth: widget.auth, api: widget.api);
  }

  Future<void> _confirmLogout() async {
    final confirmed = await showDialog<bool>(
      context: context,
      builder: (context) => AlertDialog(
        backgroundColor: AppColors.bg2,
        title: const Text('退出场控台'),
        content: const Text(
          '会清除本机场控令牌和离线队列；进行中的场控不会结束。'
          '再次进入需要店长重新生成场控码。',
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
    if (confirmed == true) {
      await _logout();
    }
  }

  Future<void> _confirmFinish() async {
    final confirmed = await showDialog<bool>(
      context: context,
      builder: (context) => AlertDialog(
        backgroundColor: AppColors.bg2,
        title: const Text('结束场控'),
        content: const Text(
          '结束后本场不再计时，DM 令牌立即失效，阶段与事件会保留供复盘。\n'
          '倒计时到点不会自行结束场控，这一步只能由你确认。',
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.of(context).pop(false),
            child: const Text('继续场控'),
          ),
          FilledButton(
            style: FilledButton.styleFrom(backgroundColor: AppColors.danger),
            onPressed: () => Navigator.of(context).pop(true),
            child: const Text('确认结束'),
          ),
        ],
      ),
    );
    if (confirmed == true) {
      await _act('finish');
    }
  }

  Future<void> _customExtend() async {
    final controller = TextEditingController(text: '5');
    String? errorText;
    final minutes = await showDialog<int>(
      context: context,
      builder: (context) => StatefulBuilder(
        builder: (context, setDialogState) => AlertDialog(
          backgroundColor: AppColors.bg2,
          title: const Text('延长当前阶段'),
          content: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              TextField(
                controller: controller,
                keyboardType: TextInputType.number,
                autofocus: true,
                decoration: InputDecoration(
                  labelText: '分钟（1–20）',
                  errorText: errorText,
                ),
              ),
            ],
          ),
          actions: [
            TextButton(
              onPressed: () => Navigator.of(context).pop(),
              child: const Text('取消'),
            ),
            FilledButton(
              onPressed: () {
                final value = int.tryParse(controller.text.trim());
                if (value == null || value < 1 || value > 20) {
                  setDialogState(() => errorText = '请输入 1 到 20 之间的整数。');
                  return;
                }
                Navigator.of(context).pop(value);
              },
              child: const Text('延长'),
            ),
          ],
        ),
      ),
    );
    controller.dispose();
    if (minutes != null) {
      await _act('extend', minutes: minutes);
    }
  }

  @override
  Widget build(BuildContext context) {
    final run = _run;
    final now = widget.clock.nowUtc();
    final remaining = run == null ? null : remainingSeconds(run, now);
    final overtime = run == null ? 0 : overtimeSeconds(run, now);
    final current = currentStageOf(_stages);

    return Scaffold(
      appBar: AppBar(
        title: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            const Text('DM 场控台'),
            if (_session != null)
              Text(
                textOf(
                  _session!['script_name'],
                  fallback: textOf(widget.session.dmLabel, fallback: '本场'),
                ),
                style: const TextStyle(
                  color: AppColors.textFaint,
                  fontSize: 11,
                  fontWeight: FontWeight.w400,
                ),
              ),
          ],
        ),
        actions: [
          IconButton(
            onPressed: _busy ? null : _refreshAll,
            icon: const Icon(Icons.sync),
            tooltip: '同步',
          ),
          IconButton(
            onPressed: _confirmLogout,
            icon: const Icon(Icons.logout),
            tooltip: '退出',
          ),
        ],
      ),
      body: RefreshIndicator(
        onRefresh: _refreshAll,
        child: ListView(
          physics: const AlwaysScrollableScrollPhysics(),
          padding: const EdgeInsets.fromLTRB(16, 8, 16, 32),
          children: [
            if (_offline || _pendingCount > 0)
              _OfflineBanner(offline: _offline, pending: _pendingCount),
            if (_error != null) ...[
              const SizedBox(height: 12),
              InlineError(
                message: _error!,
                onRetry: _refreshAll,
              ),
            ],
            if (_loading && run == null)
              const Padding(
                padding: EdgeInsets.all(48),
                child: Center(child: CircularProgressIndicator()),
              )
            else if (run != null) ...[
              const SizedBox(height: 10),
              _HeaderPanel(
                run: run,
                session: _session,
                templatePreview: _templatePreview,
              ),
              const SizedBox(height: 14),
              _TimerPanel(
                run: run,
                stages: _stages,
                current: current,
                remaining: remaining,
                overtime: overtime,
              ),
              if (overtime > 0 && _status == 'active') ...[
                const SizedBox(height: 10),
                const _OvertimeBanner(),
              ],
              if (_notice != null) ...[
                const SizedBox(height: 12),
                _NoticePanel(
                  message: _notice!,
                  onClose: () => setState(() => _notice = null),
                ),
              ],
              if (_hardware != null && _hardware!['ok'] != true) ...[
                const SizedBox(height: 10),
                _HardwareMessage(hardware: _hardware!),
              ],
              for (final warning in _warnings) ...[
                const SizedBox(height: 8),
                _WarningLine(text: warning),
              ],
              if (_status == 'completed' || _status == 'abandoned') ...[
                const SizedBox(height: 14),
                _FinishedSummary(run: run, stages: _stages),
              ],
              const SizedBox(height: 18),
              _Controls(
                status: _status,
                busy: _busy,
                offline: _offline,
                onStart: () => _act('start'),
                onPause: () => _act('pause'),
                onResume: () => _act('resume'),
                onPrevious: () => _act('previous'),
                onNext: () => _act('next'),
                onExtend: (minutes) => _act('extend', minutes: minutes),
                onCustomExtend: _customExtend,
                onFinish: _confirmFinish,
                onExit: _logout,
              ),
              const SizedBox(height: 16),
              _ModePanel(
                run: run,
                busy: _busy,
                onMode: (mode) => _act('mode', mode: mode),
                onRetryHardware: () => _act('hardware_retry'),
              ),
              const SizedBox(height: 22),
              SectionTitle(
                title: '阶段',
                trailing: Text(
                  _stages.isEmpty ? '暂无模板' : '共 ${_stages.length} 幕',
                  style:
                      const TextStyle(color: AppColors.textFaint, fontSize: 12),
                ),
              ),
              if (_stages.isEmpty)
                const EmptyHint(
                  title: '本场没有阶段模板',
                  message: '先请店长在 Web 后台录入阶段模板，再生成场控码。',
                )
              else
                for (final stage in _stages)
                  _StageRow(
                    key: ValueKey('stage-${textOf(stage['stage_id'])}'),
                    stage: stage,
                  ),
              if (current != null) ...[
                const SizedBox(height: 8),
                _CueCard(
                  key: ValueKey('cue-${textOf(current['stage_id'])}'),
                  stage: current,
                ),
              ],
              const SizedBox(height: 18),
              const _FooterNote(),
            ],
          ],
        ),
      ),
    );
  }
}

class _OfflineBanner extends StatelessWidget {
  const _OfflineBanner({required this.offline, required this.pending});

  final bool offline;
  final int pending;

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.all(12),
      decoration: BoxDecoration(
        color: AppColors.amber.withValues(alpha: 0.10),
        borderRadius: BorderRadius.circular(12),
        border: Border.all(color: AppColors.amber.withValues(alpha: 0.34)),
      ),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          const Icon(Icons.wifi_off, color: AppColors.amber, size: 17),
          const SizedBox(width: 9),
          Expanded(
            child: Text(
              offline
                  ? '离线中：计时继续，已排队 $pending 条操作。'
                      '恢复网络后自动补传，补传不会补发灯光/音效。'
                  : '正在补传 $pending 条离线操作…',
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

class _HeaderPanel extends StatelessWidget {
  const _HeaderPanel({
    required this.run,
    required this.session,
    required this.templatePreview,
  });

  final Map<String, dynamic> run;
  final Map<String, dynamic>? session;
  final bool templatePreview;

  @override
  Widget build(BuildContext context) {
    final status = textOf(run['status']);
    final color = runStatusColor(status);
    return Panel(
      borderColor: color.withValues(alpha: 0.34),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Wrap(
            spacing: 8,
            runSpacing: 6,
            children: [
              Pill(label: runStatusLabel(status), color: color),
              Pill(
                label: textOf(run['mode']) == 'hardware' ? '硬件模式' : '软件模式',
                color: AppColors.violet,
                icon: Icons.lightbulb_outline,
              ),
              if (run['hardware_degraded'] == true)
                const Pill(label: '硬件已降级', color: AppColors.amber),
            ],
          ),
          const SizedBox(height: 14),
          Text(
            textOf(session?['script_name'], fallback: '场次'),
            style: const TextStyle(
              color: AppColors.text,
              fontSize: 21,
              fontWeight: FontWeight.w800,
            ),
          ),
          const SizedBox(height: 8),
          Wrap(
            spacing: 8,
            runSpacing: 6,
            children: [
              _Meta(
                icon: Icons.meeting_room_outlined,
                text: textOf(run['room_name'], fallback: '房间未分配'),
              ),
              _Meta(
                icon: Icons.schedule,
                text: textOf(session?['start_time'], fallback: '时间未知'),
              ),
              _Meta(
                icon: Icons.person_outline,
                text: textOf(run['dm_name'], fallback: '临时 DM'),
              ),
            ],
          ),
          if (templatePreview) ...[
            const SizedBox(height: 10),
            const Text(
              '尚未开始：当前显示模板预览；开始后以那时的模板快照为准。',
              style: TextStyle(
                color: AppColors.textFaint,
                fontSize: 11,
                height: 1.5,
              ),
            ),
          ],
        ],
      ),
    );
  }
}

class _Meta extends StatelessWidget {
  const _Meta({required this.icon, required this.text});

  final IconData icon;
  final String text;

  @override
  Widget build(BuildContext context) {
    return Row(
      mainAxisSize: MainAxisSize.min,
      children: [
        Icon(icon, size: 14, color: AppColors.textFaint),
        const SizedBox(width: 4),
        Text(
          text,
          style: const TextStyle(color: AppColors.textDim, fontSize: 12),
        ),
      ],
    );
  }
}

class _TimerPanel extends StatelessWidget {
  const _TimerPanel({
    required this.run,
    required this.stages,
    required this.current,
    required this.remaining,
    required this.overtime,
  });

  final Map<String, dynamic> run;
  final List<Map<String, dynamic>> stages;
  final Map<String, dynamic>? current;
  final int? remaining;
  final int overtime;

  @override
  Widget build(BuildContext context) {
    final status = textOf(run['status']);
    final seq = intOf(run['current_seq']);
    final overdue = overtime > 0;
    final paused = status == 'paused';
    final pending = status == 'pending';
    final finished = status == 'completed' || status == 'abandoned';
    final label = pending
        ? '点击开始后计时'
        : paused
            ? '已暂停 · 剩余（暂停中不计时）'
            : finished
                ? '场控已结束'
                : overdue
                    ? '已超时 +${durationOf(overtime)} · 不会自动推进'
                    : '剩余时间';
    return Panel(
      borderColor: overdue
          ? AppColors.danger.withValues(alpha: 0.5)
          : AppColors.cyan.withValues(alpha: 0.22),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(
            label,
            style: TextStyle(
              color: overdue ? AppColors.danger : AppColors.textDim,
              fontSize: 12,
              fontWeight: overdue ? FontWeight.w700 : FontWeight.w500,
            ),
          ),
          const SizedBox(height: 8),
          Text(
            pending || finished
                ? '--:--'
                : durationOf(remaining ?? 0),
            style: TextStyle(
              fontSize: 54,
              height: 1.05,
              fontWeight: FontWeight.w800,
              letterSpacing: 2,
              color: overdue
                  ? AppColors.danger
                  : paused
                      ? AppColors.amber
                      : AppColors.text,
              fontFeatures: const [FontFeature.tabularFigures()],
            ),
          ),
          const SizedBox(height: 8),
          Text(
            current == null
                ? (pending && stages.isNotEmpty
                    ? '准备开始 · 第一幕 ${textOf(stages.first['name'])}'
                    : '尚未开始')
                : '第 ${seq ?? '?'} / ${stages.length} 幕 · '
                    '${textOf(current!['name'], fallback: '未命名阶段')} · '
                    '计划 ${intOf(current!['duration_minutes']) ?? 0} 分钟',
            style: const TextStyle(color: AppColors.textDim, fontSize: 13),
          ),
          if (paused) ...[
            const SizedBox(height: 6),
            const Text(
              '暂停期间剩余时间冻结；继续后从冻结点接着计时。',
              style: TextStyle(
                color: AppColors.textFaint,
                fontSize: 11,
                height: 1.5,
              ),
            ),
          ],
        ],
      ),
    );
  }
}

class _OvertimeBanner extends StatelessWidget {
  const _OvertimeBanner();

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.all(12),
      decoration: BoxDecoration(
        color: AppColors.danger.withValues(alpha: 0.12),
        borderRadius: BorderRadius.circular(12),
        border: Border.all(color: AppColors.danger.withValues(alpha: 0.4)),
      ),
      child: const Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Icon(Icons.timer_outlined, color: AppColors.danger, size: 17),
          SizedBox(width: 9),
          Expanded(
            child: Text(
              '当前阶段已超时。这只是一条提醒：剧情不会自动推进，'
              '请由 DM 判断是继续、延长还是切到下一幕。',
              style: TextStyle(
                color: AppColors.text,
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

class _NoticePanel extends StatelessWidget {
  const _NoticePanel({required this.message, required this.onClose});

  final String message;
  final VoidCallback onClose;

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.fromLTRB(12, 10, 6, 10),
      decoration: BoxDecoration(
        color: AppColors.cyan.withValues(alpha: 0.08),
        borderRadius: BorderRadius.circular(12),
        border: Border.all(color: AppColors.cyan.withValues(alpha: 0.26)),
      ),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          const Icon(Icons.info_outline, color: AppColors.cyan, size: 16),
          const SizedBox(width: 8),
          Expanded(
            child: Text(
              message,
              style: const TextStyle(
                color: AppColors.textDim,
                fontSize: 12,
                height: 1.5,
              ),
            ),
          ),
          IconButton(
            onPressed: onClose,
            icon: const Icon(Icons.close, size: 16),
            color: AppColors.textFaint,
            visualDensity: VisualDensity.compact,
            tooltip: '关闭提示',
          ),
        ],
      ),
    );
  }
}

class _HardwareMessage extends StatelessWidget {
  const _HardwareMessage({required this.hardware});

  final Map<String, dynamic> hardware;

  @override
  Widget build(BuildContext context) {
    final status = textOf(hardware['status']);
    final message = textOf(hardware['message'], fallback: '硬件状态未知。');
    final failed = status == 'failed';
    final color = failed
        ? AppColors.danger
        : status == 'not_configured'
            ? AppColors.amber
            : AppColors.textFaint;
    return Padding(
      padding: const EdgeInsets.only(bottom: 4),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Icon(
            failed ? Icons.warning_amber_rounded : Icons.lightbulb_outline,
            size: 15,
            color: color,
          ),
          const SizedBox(width: 6),
          Expanded(
            child: Text(
              failed
                  ? '硬件失败已降级为文字提示：$message'
                  : '硬件：$message',
              style: TextStyle(color: color, fontSize: 11, height: 1.5),
            ),
          ),
        ],
      ),
    );
  }
}

class _WarningLine extends StatelessWidget {
  const _WarningLine({required this.text});

  final String text;

  @override
  Widget build(BuildContext context) {
    return Row(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        const Icon(Icons.info_outline, color: AppColors.amber, size: 17),
        const SizedBox(width: 8),
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
    );
  }
}

class _FinishedSummary extends StatelessWidget {
  const _FinishedSummary({required this.run, required this.stages});

  final Map<String, dynamic> run;
  final List<Map<String, dynamic>> stages;

  String _finishedLabel(Object? value) {
    final text = textOf(value);
    if (text.isEmpty) {
      return '—';
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
    final planned = stages.fold<int>(
      0,
      (sum, stage) => sum + (intOf(stage['duration_minutes']) ?? 0),
    );
    final paused = intOf(run['paused_total_seconds']) ?? 0;
    final seq = intOf(run['current_seq']);
    final degraded = run['hardware_degraded'] == true;
    return Panel(
      borderColor: AppColors.line,
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          const Text(
            '本场汇总',
            style: TextStyle(
              color: AppColors.text,
              fontSize: 15,
              fontWeight: FontWeight.w700,
            ),
          ),
          const SizedBox(height: 12),
          _SummaryRow(label: '结束时间', value: _finishedLabel(run['finished_at'])),
          const SizedBox(height: 8),
          _SummaryRow(
            label: '停留阶段',
            value: stages.isEmpty
                ? '—'
                : '第 ${seq ?? '?'} / ${stages.length} 幕',
          ),
          const SizedBox(height: 8),
          _SummaryRow(
            label: '计划总时长',
            value: planned == 0 ? '—' : '$planned 分钟',
          ),
          const SizedBox(height: 8),
          _SummaryRow(
            label: '累计暂停',
            value: '${paused ~/ 60} 分 ${paused % 60} 秒',
          ),
          const SizedBox(height: 8),
          _SummaryRow(
            label: '硬件',
            value: degraded ? '曾失败并降级' : '正常或未使用',
          ),
          const SizedBox(height: 12),
          const Text(
            '逐阶段实际时长和事件时间线请在 Web 端场控详情查看；'
            '客户端只保留本场摘要。',
            style: TextStyle(
              color: AppColors.textFaint,
              fontSize: 11,
              height: 1.5,
            ),
          ),
        ],
      ),
    );
  }
}

class _SummaryRow extends StatelessWidget {
  const _SummaryRow({required this.label, required this.value});

  final String label;
  final String value;

  @override
  Widget build(BuildContext context) {
    return Row(
      children: [
        Expanded(
          child: Text(
            label,
            style: const TextStyle(color: AppColors.textDim, fontSize: 12),
          ),
        ),
        Text(
          value,
          style: const TextStyle(
            color: AppColors.text,
            fontSize: 12,
            fontWeight: FontWeight.w600,
          ),
        ),
      ],
    );
  }
}

class _Controls extends StatelessWidget {
  const _Controls({
    required this.status,
    required this.busy,
    required this.offline,
    required this.onStart,
    required this.onPause,
    required this.onResume,
    required this.onPrevious,
    required this.onNext,
    required this.onExtend,
    required this.onCustomExtend,
    required this.onFinish,
    required this.onExit,
  });

  final String status;
  final bool busy;
  final bool offline;
  final VoidCallback onStart;
  final VoidCallback onPause;
  final VoidCallback onResume;
  final VoidCallback onPrevious;
  final VoidCallback onNext;
  final void Function(int minutes) onExtend;
  final VoidCallback onCustomExtend;
  final VoidCallback onFinish;
  final VoidCallback onExit;

  @override
  Widget build(BuildContext context) {
    if (status == 'completed' || status == 'abandoned') {
      return Panel(
        borderColor: AppColors.line,
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            const Text(
              '本场场控已结束',
              style: TextStyle(
                color: AppColors.text,
                fontSize: 16,
                fontWeight: FontWeight.w700,
              ),
            ),
            const SizedBox(height: 6),
            const Text(
              'DM 令牌已失效，阶段与事件保留供复盘。',
              style: TextStyle(
                color: AppColors.textDim,
                fontSize: 12,
                height: 1.5,
              ),
            ),
            const SizedBox(height: 14),
            OutlinedButton(
              onPressed: onExit,
              child: const Text('退出场控台'),
            ),
          ],
        ),
      );
    }
    if (status == 'pending') {
      return Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          FilledButton.icon(
            onPressed: busy ? null : onStart,
            icon: const Icon(Icons.play_arrow_rounded, size: 24),
            label: const Text('开始场控'),
            style: FilledButton.styleFrom(
              minimumSize: const Size.fromHeight(58),
            ),
          ),
          const SizedBox(height: 8),
          const Text(
            '开始后才进入第一幕计时；此前只是模板预览。',
            textAlign: TextAlign.center,
            style: TextStyle(
              color: AppColors.textFaint,
              fontSize: 11,
              height: 1.5,
            ),
          ),
        ],
      );
    }
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        Row(
          children: [
            Expanded(
              child: OutlinedButton(
                onPressed: busy || status != 'active' ? null : onPrevious,
                style: OutlinedButton.styleFrom(
                  minimumSize: const Size.fromHeight(56),
                ),
                child: const Text('上一幕'),
              ),
            ),
            const SizedBox(width: 10),
            Expanded(
              child: FilledButton.tonalIcon(
                onPressed: busy
                    ? null
                    : status == 'paused'
                        ? onResume
                        : onPause,
                icon: Icon(
                  status == 'paused'
                      ? Icons.play_arrow_rounded
                      : Icons.pause_rounded,
                ),
                label: Text(status == 'paused' ? '继续' : '暂停'),
                style: FilledButton.styleFrom(
                  minimumSize: const Size.fromHeight(56),
                ),
              ),
            ),
            const SizedBox(width: 10),
            Expanded(
              child: OutlinedButton(
                onPressed: busy || status != 'active' ? null : onNext,
                style: OutlinedButton.styleFrom(
                  minimumSize: const Size.fromHeight(56),
                  foregroundColor: AppColors.cyan,
                ),
                child: const Text('下一幕'),
              ),
            ),
          ],
        ),
        const SizedBox(height: 14),
        const Text(
          '延长当前阶段',
          style: TextStyle(color: AppColors.textDim, fontSize: 12),
        ),
        const SizedBox(height: 8),
        Wrap(
          spacing: 8,
          runSpacing: 6,
          children: [
            for (final minutes in const [1, 3, 5, 10])
              ActionChip(
                label: Text(
                  '+$minutes 分钟',
                  style: const TextStyle(
                    color: AppColors.textDim,
                    fontSize: 12,
                  ),
                ),
                backgroundColor: AppColors.bg2.withValues(alpha: 0.6),
                side: const BorderSide(color: AppColors.line),
                onPressed: busy ? null : () => onExtend(minutes),
              ),
            ActionChip(
              avatar: const Icon(Icons.edit_outlined,
                  size: 15, color: AppColors.textDim),
              label: const Text(
                '自定义',
                style: TextStyle(color: AppColors.textDim, fontSize: 12),
              ),
              backgroundColor: AppColors.bg2.withValues(alpha: 0.6),
              side: const BorderSide(color: AppColors.line),
              onPressed: busy ? null : onCustomExtend,
            ),
          ],
        ),
        const SizedBox(height: 18),
        OutlinedButton.icon(
          onPressed: busy ? null : onFinish,
          icon: const Icon(Icons.stop_circle_outlined, size: 20),
          label: const Text('结束场控'),
          style: OutlinedButton.styleFrom(
            foregroundColor: AppColors.danger,
            side: BorderSide(color: AppColors.danger.withValues(alpha: 0.4)),
            minimumSize: const Size.fromHeight(52),
          ),
        ),
        if (offline) ...[
          const SizedBox(height: 8),
          const Text(
            '离线时仍可推进，操作会排队；硬件指令不会在补传时补发。',
            textAlign: TextAlign.center,
            style: TextStyle(
              color: AppColors.textFaint,
              fontSize: 11,
              height: 1.5,
            ),
          ),
        ],
      ],
    );
  }
}

class _ModePanel extends StatelessWidget {
  const _ModePanel({
    required this.run,
    required this.busy,
    required this.onMode,
    required this.onRetryHardware,
  });

  final Map<String, dynamic> run;
  final bool busy;
  final void Function(String mode) onMode;
  final VoidCallback onRetryHardware;

  @override
  Widget build(BuildContext context) {
    final status = textOf(run['status']);
    final mode = textOf(run['mode'], fallback: 'software');
    final degraded = run['hardware_degraded'] == true;
    if (status == 'completed' || status == 'abandoned') {
      return const SizedBox.shrink();
    }
    return Panel(
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          const Text(
            '本场模式',
            style: TextStyle(
              color: AppColors.text,
              fontSize: 14,
              fontWeight: FontWeight.w700,
            ),
          ),
          const SizedBox(height: 4),
          const Text(
            '软件模式只推送提示；硬件模式在切幕时调用已配置的硬件 Skill。'
            '硬件失败会自动降级为文字提示，不阻断阶段。',
            style: TextStyle(
              color: AppColors.textFaint,
              fontSize: 11,
              height: 1.5,
            ),
          ),
          const SizedBox(height: 12),
          SegmentedButton<String>(
            segments: const [
              ButtonSegment(value: 'software', label: Text('软件提示')),
              ButtonSegment(value: 'hardware', label: Text('硬件模式')),
            ],
            selected: {mode == 'hardware' ? 'hardware' : 'software'},
            onSelectionChanged: busy || status == 'pending'
                ? null
                : (value) {
                    if (value.first != mode) {
                      onMode(value.first);
                    }
                  },
          ),
          if (mode == 'hardware') ...[
            const SizedBox(height: 10),
            Row(
              children: [
                Icon(
                  degraded ? Icons.warning_amber_rounded : Icons.check_circle,
                  size: 16,
                  color: degraded ? AppColors.amber : AppColors.mint,
                ),
                const SizedBox(width: 6),
                Expanded(
                  child: Text(
                    degraded
                        ? '硬件已降级：后续切幕只显示文字提示。'
                        : '硬件状态正常；切幕时下发灯光/音效。',
                    style: TextStyle(
                      color: degraded ? AppColors.amber : AppColors.textDim,
                      fontSize: 12,
                      height: 1.5,
                    ),
                  ),
                ),
                if (degraded)
                  TextButton(
                    onPressed: busy ? null : onRetryHardware,
                    child: const Text('重试硬件'),
                  ),
              ],
            ),
          ],
        ],
      ),
    );
  }
}

class _StageRow extends StatelessWidget {
  const _StageRow({super.key, required this.stage});

  final Map<String, dynamic> stage;

  @override
  Widget build(BuildContext context) {
    final state = textOf(stage['state']);
    final color = switch (state) {
      'completed' => AppColors.mint,
      'current' => AppColors.cyan,
      _ => AppColors.textFaint,
    };
    final label = switch (state) {
      'completed' => '已完成',
      'current' => '当前',
      _ => '未开始',
    };
    final current = state == 'current';
    return Padding(
      padding: const EdgeInsets.only(bottom: 8),
      child: Panel(
        padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 12),
        borderColor: color.withValues(alpha: current ? 0.55 : 0.16),
        color: current ? AppColors.cyan.withValues(alpha: 0.07) : null,
        child: Row(
          children: [
            Text(
              '${intOf(stage['seq']) ?? '-'}',
              style: TextStyle(
                color: color,
                fontWeight: FontWeight.w800,
                fontSize: 16,
              ),
            ),
            const SizedBox(width: 12),
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(
                    textOf(stage['name'], fallback: '未命名阶段'),
                    style: TextStyle(
                      color: current ? AppColors.text : AppColors.textDim,
                      fontWeight:
                          current ? FontWeight.w700 : FontWeight.w500,
                    ),
                  ),
                  const SizedBox(height: 2),
                  Text(
                    '${intOf(stage['duration_minutes']) ?? 0} 分钟'
                    '${intOf(stage['warn_before_seconds']) != null ? ' · 提前 ${intOf(stage['warn_before_seconds'])} 秒提醒' : ''}',
                    style: const TextStyle(
                      color: AppColors.textFaint,
                      fontSize: 11,
                    ),
                  ),
                ],
              ),
            ),
            const SizedBox(width: 8),
            Pill(label: label, color: color),
          ],
        ),
      ),
    );
  }
}

class _CueCard extends StatefulWidget {
  const _CueCard({super.key, required this.stage});

  final Map<String, dynamic> stage;

  @override
  State<_CueCard> createState() => _CueCardState();
}

class _CueCardState extends State<_CueCard> {
  bool _expanded = false;

  @override
  Widget build(BuildContext context) {
    final cue = textOf(widget.stage['cue_text']);
    final prompt = textOf(widget.stage['prompt_text']);
    if (cue.isEmpty && prompt.isEmpty) {
      return const SizedBox.shrink();
    }
    return Panel(
      borderColor: AppColors.violet.withValues(alpha: 0.28),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            children: [
              const Icon(Icons.theater_comedy_outlined,
                  color: AppColors.violet, size: 17),
              const SizedBox(width: 8),
              const Expanded(
                child: Text(
                  'DM 提示',
                  style: TextStyle(
                    color: AppColors.text,
                    fontWeight: FontWeight.w700,
                  ),
                ),
              ),
              if (cue.isNotEmpty)
                TextButton(
                  onPressed: () {
                    setState(() => _expanded = !_expanded);
                  },
                  child: Text(_expanded ? '收起' : '看提示'),
                ),
            ],
          ),
          if (prompt.isNotEmpty) ...[
            const SizedBox(height: 6),
            Text(
              '到点提醒：$prompt',
              style: const TextStyle(
                color: AppColors.textDim,
                fontSize: 12,
                height: 1.5,
              ),
            ),
          ],
          if (cue.isNotEmpty && _expanded) ...[
            const SizedBox(height: 10),
            SelectableText(
              cue,
              style: const TextStyle(
                color: AppColors.text,
                fontSize: 13,
                height: 1.65,
              ),
            ),
          ],
          const SizedBox(height: 8),
          const Text(
            '只在本机显示，不会发给玩家。',
            style: TextStyle(color: AppColors.textFaint, fontSize: 11),
          ),
        ],
      ),
    );
  }
}

class _FooterNote extends StatelessWidget {
  const _FooterNote();

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.all(13),
      decoration: BoxDecoration(
        color: AppColors.bg2.withValues(alpha: 0.5),
        borderRadius: BorderRadius.circular(12),
        border: Border.all(color: AppColors.line),
      ),
      child: const Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Icon(Icons.shield_outlined, color: AppColors.textFaint, size: 16),
          SizedBox(width: 8),
          Expanded(
            child: Text(
              '场控只读取本场排期、房间和预录入阶段模板，不能新建场次或剧本。'
              '倒计时到点只提醒，绝不自动推进；阶段控制权始终在 DM 手上。',
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
